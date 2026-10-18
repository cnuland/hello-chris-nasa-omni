"""Grounded tool-calling loop (AC-AGT-3, AC-AGT-4, AC-AGT-5, AC-AGT-7, AC-AGT-8, AC-AGT-10).

One chat turn:
1. Render the SQL inventory block for the latest user question and read the global memory summary.
2. Build the context (system prompt; block + memory as a delimited data message; budgeted history).
3. Call the LLM (``tools``, ``tool_choice="auto"``) at most ``min(settings.max_steps, 6)`` times. Every assistant
   message is validated (untrusted model output); a malformed one is an ``LLMError``. Requested tools are executed and
   their (size-capped) results fed back as ``role: tool`` messages. Invalid calls are counted per step: all invalid
   calls of the first step that has any are fed back as tool errors; a later step with an invalid call ends the loop
   without running any of its calls. Tools requested in the last allowed step are not run (nobody would read them).
4. Grounding (per claim): only GROUNDING_TOOLS ground a turn. Without grounding data the reply is ``NO_RECORD_REPLY``
   (or ``PROPOSAL_REPLY`` when only a proposal was made; AC-AGT-38/39: when lookups ran and found nothing, the reply
   names what was checked and what the catalog says about an item never seen). In a grounded turn, any location/time/actor in the reply
   that does not appear in this turn's grounding payloads replaces the reply with ``NO_RECORD_REPLY``.
5. Catalog facts (AC-AGT-11): ``lookup_catalog`` grounds with its structured fields only and is cited by
   ``catalog_id``. A location only a catalog home_location supports, stated without a usual-stowage qualifier, makes
   the reply a deterministic "usually kept in ..." answer citing those catalog entries.

6. Follow-ups (AC-AGT-12): history is prepared by ``prepare_history`` (alternating roles, canned replies neutralised,
   earlier replies cut, follow-up marker); a follow-up whose first step calls no tool is nudged once (``TOOL_NUDGE``).
7. Latency (AC-AGT-15): after a step with grounding data at most ``grounded_extra_steps`` more tool steps run, then
   the final call uses ``tool_choice="none"`` (as does the last allowed step). LLM calls share the turn deadline;
   a timeout returns the cited records deterministically when there are any, else raises ``LLMTimeout``.
8. Progress (AC-AGT-16): ``on_event`` receives ``status`` dicts (phase llm/tool/checking) while the loop runs.

Logs carry ids, tool names, status, latency and prompt-size estimates only - never message content or tool payloads.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from nasa_agent.config import MAX_STEPS_LIMIT
from nasa_agent.context import (
    SYSTEM_PROMPT,
    build_context,
    estimate_request_tokens,
    estimate_tokens,
    prepare_history,
    truncate_to_tokens,
)
from nasa_agent.grounding import (
    LOCATION_ID_RE,
    NODE_RE,
    TIME_RE,
    catalog_evidence,
    catalog_facts_reply,
    evidence_text,
    unqualified_home_locations,
    unsupported_claims,
    usual_location_rewrite,
)
from nasa_agent.llm import LLMError, LLMTimeout
from nasa_agent.render import (
    DUPLICATE_CAVEAT,
    EVENT_VERBS,
    RENDER_PAGE,
    change_line,
    changes_reply,
    count_claims,
    count_reply,
    facts_text,
    has_window_words,
    is_changes_question,
    is_count_question,
    is_now_question,
    is_people_count_question,
    is_people_presence_question,
    is_show_more,
    is_sighting_count_question,
    is_where_question,
    item_line,
    items_reply,
    list_item_count,
    list_kind,
    lookup_noun,
    may_route,
    never_seen_sentence,
    page_key,
    parse_page_marker,
    people_reply,
    place,
    previous_range_end,
    private_note,
    private_reply,
    safe_query_text,
    shown_note,
    stamp,
    top_items,
    wanted_list,
)
from nasa_agent.tools import (
    GROUNDING_TOOLS,
    PEOPLE_CAVEAT,
    change_cursor,
    citation_key,
    evidence_view,
)

NO_RECORD_REPLY = "I don't have a record of that."
STEP_LIMIT_REPLY = (
    "I could not finish answering within the tool-step limit. Please ask a narrower question; the "
    "cited records are what I looked up."
)
INVALID_TOOL_REPLY = (
    "I could not complete the lookup because the tool request was invalid. Please rephrase the "
    "question; the cited records are what I looked up."
)
EMPTY_REPLY = "I looked up the cited records but could not produce an answer. Please ask again."
PROPOSAL_REPLY = (
    "I prepared the proposed change listed below. Nothing has been changed: an operator must confirm it in the "
    "dashboard. I have no other record to report."
)
TIMEOUT_RECORDS_LEAD = (
    "The language model did not finish in time, so here are the matching records without a written summary:"
)
TIMEOUT_DETAIL = (
    "The language model did not answer within {seconds:g} s (it may be busy with another request). Nothing was "
    "guessed. Please try again in a moment, or ask a narrower question."
)
LLM_DOWN_DETAIL = "The language model is unavailable right now. Please retry in a moment."
#: AC-AGT-12: asked once when a follow-up's first step answers without calling any tool.
TOOL_NUDGE = (
    "Please answer my last question by calling the inventory tools now. Earlier answers in this conversation are not "
    "evidence."
)
NO_ANSWER_PLACEHOLDER = "(no answer was given)"
#: AC-AGT-20: tools whose `location` argument must come from the current message.
SCOPED_TOOLS = frozenset({"recent_changes", "list_unknowns", "people_seen", "count_items"})
#: AC-AGT-29: a tool step made only of these ends the turn with their deterministic rendering (no further LLM call).
RENDER_TOOLS = frozenset({"recent_changes", "people_seen", "count_items"})
#: AC-AGT-35: item tools whose multi-item result answers a where-question deterministically.
ITEM_LIST_TOOLS = frozenset({"where_is", "search_items"})
#: AC-AGT-34: item tools whose `total` is stated when a model-written list is capped.
CAPPED_LIST_TOOLS = frozenset({"where_is", "search_items", "list_unknowns"})
#: AC-AGT-33: tools whose `offset` the harness owns.
PAGED_TOOLS = frozenset({"recent_changes", "people_seen"})
#: AC-AGT-20/22: tools whose since/until the harness sets from a relative window in the current message.
WINDOWED_TOOLS = frozenset({"recent_changes", "people_seen"})
RECENT_MAX_DAYS = timedelta(days=7)
CLAMP_NOTE = "(Changes are only searched up to 7 days back.)"
#: AC-AGT-20: the current message refers to an earlier place ("in there", "that module", "the same location").
PLACE_REFERENCE_RE = re.compile(
    r"\b(?:(?:in|inside|from|at|over|out\s+of)\s+there|(?:that|this|the\s+same|same)\s+"
    r"(?:module|location|zone|area|place|node|segment|rack|bay|spot))\b",
    re.IGNORECASE,
)
INVALID_KINDS = frozenset({"validation", "unknown_tool"})
#: Harness replies that carry no facts: neutralised in the history of later turns (AC-AGT-12).
CANNED_REPLIES = frozenset(
    {NO_RECORD_REPLY, STEP_LIMIT_REPLY, INVALID_TOOL_REPLY, EMPTY_REPLY, PROPOSAL_REPLY, TIMEOUT_RECORDS_LEAD}
)
MAX_TIMEOUT_RECORDS = 5
#: Human-readable status text per tool (AC-AGT-16; the dashboard shows ``text``).
TOOL_STATUS = {
    "search_items": "Searching the inventory",
    "get_item": "Reading the item record",
    "item_history": "Reading the item history",
    "where_is": "Looking up where it was last seen",
    "who_moved": "Checking who handled it",
    "recent_changes": "Checking recent changes",
    "list_unknowns": "Listing unknown objects",
    "inventory_stats": "Counting the inventory",
    "search_observations": "Searching the video observations",
    "recall_memory": "Reading background memory",
    "lookup_catalog": "Checking the catalog",
    "people_seen": "Counting person tracks",
    "count_items": "Counting inventory items",
    "propose_update": "Preparing a proposed change",
}

log = logging.getLogger("nasa_agent.harness")
#: AC-AGT-37: the chat session of the running turn (page cursors are kept per session).
_SESSION: ContextVar[str | None] = ContextVar("nasa_agent_session", default=None)
#: AC-AGT-37: page cursors kept (oldest dropped first).
MAX_PAGE_CURSORS = 512
#: AC-AGT-38: words of a "what is in <private place>?" question once the place names are removed.
_CONTENTS_WORDS = frozenset({
    "what", "whats", "is", "are", "in", "inside", "the", "there", "anything", "any", "items", "item", "stuff",
    "things", "stored", "kept", "of", "which", "show", "me", "list", "do", "we", "have", "s", "contents", "a", "at",
    "on", "currently", "now", "right", "objects", "object", "inventory", "tell", "about", "our", "my", "please",
    "can", "you", "see", "recorded", "record", "records",
})
_CONTENTS_TRIGGERS = frozenset({"what", "whats", "anything", "items", "stuff", "things", "contents", "objects",
                                "inventory", "stored", "kept", "list", "show"})
#: AC-AGT-38: words that may name a private area (the curated perception private-zone terms); others need an id.
_PRIVATE_HINT_RE = re.compile(r"\b(?:quarters?|cqs?|sleep|toilet|whc|bathroom|lavatory|restroom|hygiene|private)\b",
                              re.IGNORECASE)
_PEOPLE_TRIGGERS = frozenset({"who", "whos", "anyone", "anybody", "someone", "somebody", "people", "crew"})
#: AC-AGT-38: the reply to "who was in <private area>?" (says nothing about what is there).
PRIVATE_PEOPLE_REPLY = "I don't report who was in a private area."
_WORD_RE = re.compile(r"[a-z]+|\d+")
#: AC-AGT-38: words of a cited label that do not identify it on their own.
_GENERIC_LABEL_WORDS = frozenset({"unknown", "object", "item", "thing", "record", "person", "the", "and", "with",
                                  "new", "small", "large", "big"})
#: AC-AGT-38: a model reply that says nothing is on record (its citations are then dropped unless it names them).
_NEGATIVE_RE = re.compile(r"\b(?:no record|not have a record|don't have a record|do not have any|no (?:items?|"
                          r"objects?|records?|data|information)\b|nothing (?:is |was )?(?:recorded|on record))",
                          re.IGNORECASE)


# ---------------------------------------------------------------- untrusted LLM output
class _Function(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: StrictStr = Field(..., min_length=1, max_length=128)
    arguments: Any = None


class _ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictStr | None = None
    type: StrictStr | None = None
    function: _Function


class _ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: StrictStr | None = None
    text: StrictStr | None = None


class _AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: StrictStr | list[_ContentPart] | None = None
    tool_calls: list[_ToolCall] | None = None


def parse_assistant(message: Any, step: int) -> tuple[str | None, list[dict[str, Any]]]:
    """Validate one assistant message. Returns (text content, normalised tool calls with ids). Raises LLMError."""
    try:
        msg = _AssistantMessage.model_validate(message)
    except ValidationError as exc:
        raise LLMError(f"LLM returned a malformed assistant message ({exc.error_count()} problem(s))") from exc
    content = msg.content
    if isinstance(content, list):
        content = "".join(p.text or "" for p in content if p.type in (None, "text"))
    calls = []
    for i, call in enumerate(msg.tool_calls or []):
        if call.type not in (None, "function"):
            raise LLMError(f"LLM returned an unsupported tool call type {call.type!r}")
        args = call.function.arguments
        calls.append(
            {
                "id": call.id or f"call_{step}_{i}",
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": args if isinstance(args, str) else json.dumps(args if args is not None else {}),
                },
            }
        )
    return content, calls


def _parse_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw if raw is not None else {}


def fit_payload(payload: dict[str, Any], max_tokens: int) -> str:
    """JSON for a tool message within ``max_tokens``: whole list items are dropped from the end (``truncated``,
    ``omitted``); a non-list payload is cut as text. Always valid JSON."""
    text = json.dumps(payload, default=str)
    if estimate_tokens(text) <= max_tokens:
        return text
    data = payload.get("data")
    list_key, at_key = ("tracks", "last_seen_at") if isinstance(data, dict) and "tracks" in data else ("changes", "at")
    changes = data.get(list_key) if isinstance(data, dict) else None
    if isinstance(changes, list) and changes:
        # recent_changes (AC-AGT-13) / people_seen tracks: drop whole rows from the end, keep the structure and scope
        lo, hi, best = 0, len(changes) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            sent = changes[:mid]
            keys = {(c.get("entity_id"), c.get(at_key)) for c in sent if isinstance(c, dict)}
            omitted_key = "omitted" if list_key == "changes" else "tracks_omitted"
            cand = json.dumps(
                {
                    **payload,
                    "data": {**data, list_key: sent,
                             omitted_key: len(changes) - mid + int(data.get(omitted_key) or 0)},
                    "citations": [c for c in payload.get("citations") or [] if (c.get("entity_id"), c.get("at")) in keys],
                    "truncated": True,
                },
                default=str,
            )
            if estimate_tokens(cand) <= max_tokens:
                best, lo = cand, mid + 1
            else:
                hi = mid - 1
        if best is not None:
            return best
    if isinstance(data, list) and data:
        lo, hi, best = 1, len(data) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = json.dumps(
                {
                    **payload,
                    "data": data[:mid],
                    "citations": (payload.get("citations") or [])[:mid],
                    "truncated": True,
                    "omitted": len(data) - mid,
                },
                default=str,
            )
            if estimate_tokens(cand) <= max_tokens:
                best, lo = cand, mid + 1
            else:
                hi = mid - 1
        if best is not None:
            return best
    raw = json.dumps(data if data is not None else payload, default=str)
    budget = max_tokens
    while budget > 0:
        cand = json.dumps(
            {"ok": payload.get("ok", False), "truncated": True, "data": truncate_to_tokens(raw, budget)}, default=str
        )
        if estimate_tokens(cand) <= max_tokens:
            return cand
        budget -= max(1, budget // 10)
    return json.dumps({"ok": payload.get("ok", False), "truncated": True})


class AgentHarness:
    """Model-agnostic harness (OpenAI-compatible tool calling) over a :class:`~nasa_agent.tools.Toolbox`."""

    def __init__(self, llm, toolbox, *, settings, memory=None, inventory_block=None):
        self.llm = llm
        self.toolbox = toolbox
        self.settings = settings
        self.memory = memory
        self.inventory_block = inventory_block
        #: AC-AGT-37: {page_key: {since, until, cursor, location, mode}} of rendered pages with a "show more" hint
        self._pages: OrderedDict[str, dict[str, Any]] = OrderedDict()

    # ------------------------------------------------------------ context sources
    async def _render_block(self, question: str, request_id: str) -> str:
        if self.inventory_block is None:
            return ""
        try:
            return await asyncio.to_thread(
                self.inventory_block.render, question, self.settings.inventory_block_token_budget
            )
        except Exception as exc:  # noqa: BLE001 - degrade, but say so in the prompt
            log.warning("inventory block unavailable", extra={"request_id": request_id, "error": type(exc).__name__})
            return "INVENTORY WORKING SET: unavailable (database error) - rely on tool results only."

    async def _global_summary(self, request_id: str) -> str | None:
        if self.memory is None:
            return None
        try:
            return await asyncio.to_thread(self.memory.get_global)
        except Exception as exc:  # noqa: BLE001 - memory is auxiliary
            log.warning("global memory unavailable", extra={"request_id": request_id, "error": type(exc).__name__})
            return None

    def _record(self, request_id, tool_calls, name, raw_args, result, latency_ms) -> None:
        log.info(
            "tool call",
            extra={
                "request_id": request_id,
                "tool": name,
                "ok": result.ok,
                "error_kind": result.error_kind,
                "latency_ms": latency_ms,
            },
        )
        tool_calls.append(
            {
                "name": name,
                "arguments": _parse_arguments(raw_args),
                "ok": result.ok,
                "error_kind": result.error_kind,
                "latency_ms": latency_ms,
            }
        )

    def _now(self) -> datetime:
        now = getattr(self.toolbox, "now", None)
        return now() if callable(now) else datetime.now(UTC)

    async def _place_matches(self, question: str, location: str) -> bool:
        """AC-AGT-20: keep a model-chosen location only if the current message names that place (or a place above
        it), or refers back to an earlier place ("in there", "that module")."""
        if PLACE_REFERENCE_RE.search(question):
            return True
        pattern = bool(NODE_RE.search(question) or LOCATION_ID_RE.search(question))
        try:
            mentioned = await self.toolbox.mentioned_locations(question)
            if mentioned is None:
                return pattern  # GET /locations unavailable: only the "Node N" / id pattern is visible
            resolved = await self.toolbox.resolve_location(location)
            return bool(resolved and mentioned) and await self.toolbox.location_within(resolved, mentioned)
        except Exception:  # noqa: BLE001 - be conservative: strip unless the text shows a place
            return pattern

    async def _adjust_call(self, call: dict[str, Any], window, place_is_named, notes: list[str],
                           question_place: str | None = None, page_offset: int = 0,
                           now_mode: bool = False) -> dict[str, Any]:
        """AC-AGT-20/22/32/33: the arguments actually run. The single place the current message names replaces the
        model's location (search_items: ``filters.location``); otherwise a model location is dropped unless the
        message names it. A relative window parsed from the message replaces the model's since/until. The harness
        sets the paging offset."""
        name = call["function"]["name"]
        if name not in SCOPED_TOOLS and name not in ("who_moved", "search_items"):
            return call
        args = _parse_arguments(call["function"]["arguments"])
        if not isinstance(args, dict):
            return call  # validation reports it
        changed = False
        if name == "search_items":
            filters = args.get("filters")
            if question_place is None or not (filters is None or isinstance(filters, dict)):
                return call
            if (filters or {}).get("location") == question_place:
                return call
            args["filters"] = {**(filters or {}), "location": question_place}  # AC-AGT-32
            return {**call, "function": {**call["function"], "arguments": json.dumps(args)}}
        loc = args.get("location")
        if name in SCOPED_TOOLS and question_place is not None:
            if loc != question_place:
                args["location"] = question_place  # AC-AGT-32: the current question's place, whatever the model sent
                changed = True
        elif name in SCOPED_TOOLS and loc is not None and not (isinstance(loc, str) and await place_is_named(loc)):
            args.pop("location")
            changed = True
        if name == "people_seen":
            # AC-AGT-36: "who is in X now" (last seen there) or "who was in X" (any sighting there), from the message
            want = "now" if now_mode and args.get("location") else None
            if args.get("mode") != want and (want or "mode" in args):
                if want:
                    args["mode"] = want
                else:
                    args.pop("mode", None)
                changed = True
        if name in PAGED_TOOLS:
            # AC-AGT-33: the harness owns paging; the model's offset is never used
            if page_offset and args.get("offset") != page_offset:
                args["offset"] = page_offset
                changed = True
            elif not page_offset and "offset" in args:
                args.pop("offset")
                changed = True
        if window is not None and name in WINDOWED_TOOLS:
            since, until = window
            floor = self._now() - RECENT_MAX_DAYS + timedelta(minutes=2)
            if since < floor:
                since = floor  # review 1: the tool keeps 7 days; a longer window is clamped and said so
                if CLAMP_NOTE not in notes:
                    notes.append(CLAMP_NOTE)
            args["since"] = _iso_z(since)
            if until is not None:
                args["until"] = _iso_z(until)
            else:
                args.pop("until", None)
            changed = True
        elif window is not None and name == "who_moved" and args.get("since") is not None:
            # review 3: only a model-made since is replaced; a time phrase that does not scope "who" adds nothing
            args["since"] = _iso_z(window[0])
            changed = True
        elif window is not None and name == "count_items" and args.get("seen_after") is not None:
            args["seen_after"] = _iso_z(window[0])
            changed = True
        if not changed:
            return call
        return {**call, "function": {**call["function"], "arguments": json.dumps(args)}}

    async def _state_scope(self, reply: str, locations: list[str]) -> str:
        """AC-AGT-20: a reply under a location filter names it; otherwise "In <name> (<id>): " is prefixed."""
        for lid in dict.fromkeys(locations):
            names = getattr(self.toolbox, "names_location", None)
            label_of = getattr(self.toolbox, "location_label", None)
            try:
                named = await names(reply, lid) if names else lid in reply
                label = await label_of(lid) if label_of else lid
            except Exception:  # noqa: BLE001 - scope wording must never break a turn
                named, label = lid in reply, lid
            if not named:
                reply = f"In {label}: {reply}"
        return reply

    async def _location_names(self) -> dict[str, str | None]:
        get = getattr(self.toolbox, "location_names", None)
        if get is None:
            return {}
        try:
            return await get()
        except Exception:  # noqa: BLE001 - names are decoration only
            return {}

    # ------------------------------------------------------------ loop
    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Returns {request_id, session_id, reply, citations, tool_calls, proposals}. Raises LLMError/LLMTimeout."""
        request_id = request_id or str(uuid.uuid4())
        _SESSION.set(session_id)
        started = time.perf_counter()
        deadline = started + float(getattr(self.settings, "turn_deadline_s", 45.0))

        async def emit(event: dict[str, Any]) -> None:
            if on_event is None:
                return
            try:
                result = on_event(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - progress reporting must never break a turn
                log.warning("status event failed", extra={"request_id": request_id, "error": type(exc).__name__})

        raw_history = [
            {"role": m["role"], "content": m.get("content")} for m in messages if m.get("role") in ("user", "assistant")
        ]
        question = next(
            (m["content"] for m in reversed(raw_history) if m["role"] == "user" and isinstance(m.get("content"), str)),
            "",
        )
        history = prepare_history(
            raw_history,
            canned_replies=CANNED_REPLIES,
            placeholder=NO_ANSWER_PLACEHOLDER,
            reply_tokens=int(getattr(self.settings, "history_reply_tokens", 150)),
        )
        follow_up = len(history) > 1
        page_calls: list[dict[str, Any]] = []  # a failed paging attempt stays visible in tool_calls
        page_offset = 0  # AC-AGT-33: the harness owns paging
        if follow_up and is_show_more(question):
            # AC-AGT-29: "show more" after a paged, rendered list is answered in code (no LLM call). AC-AGT-37: a
            # "show more changes/people" finds the latest list of that kind, however many turns back; a bare "more"
            # only continues the previous reply.
            replies = [m["content"] for m in reversed(raw_history[:-1])
                       if m["role"] == "assistant" and isinstance(m.get("content"), str)]
            previous = replies[0] if replies else ""
            wanted = wanted_list(question)
            if wanted is not None:
                previous = next((r for r in replies if list_kind(r) == wanted), previous)
            page_offset = previous_range_end(previous)
            marker = parse_page_marker(previous, question)
            if marker is not None:
                paged = await self._next_page(marker, request_id, session_id, emit, started, page_calls)
                if paged is not None:
                    return paged

        scope_box: list[Any] = []

        async def question_place() -> str | None:
            """AC-AGT-32: the single place of the current message (looked up once, only when a tool needs it)."""
            if not scope_box:
                scope_box.append(await self._question_scope(question, request_id))
            return scope_box[0].location if scope_box[0] is not None else None

        window = parse_time_window(question, self._now())  # AC-AGT-22
        notes: list[str] = []  # harness notes appended to the reply (AC-AGT-22 clamp)
        private = await self._private_answer(question, request_id, session_id, started)
        if private is not None:
            return private  # AC-AGT-38: what is recorded in a private area, said in code
        if may_route(question):
            await question_place()
            routed = await self._route(question, scope_box[0], window, notes, request_id, session_id, emit, started,
                                       page_calls)
            if routed is not None:
                return routed  # AC-AGT-35: answered without the model

        block = await self._render_block(question, request_id)
        clock = f"Current time (UTC): {self._now().astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        block = f"{clock}\n{block}" if block else clock
        summary = await self._global_summary(request_id)
        context = build_context(
            history,
            system_prompt=SYSTEM_PROMPT,
            inventory_block=block,
            global_summary=summary,
            token_budget=self.settings.context_token_budget,
        )

        tools = self.toolbox.specs()
        max_steps = max(1, min(int(self.settings.max_steps), MAX_STEPS_LIMIT))
        extra_steps = max(0, int(getattr(self.settings, "grounded_extra_steps", 1)))
        payload_budget = int(getattr(self.settings, "tool_result_token_budget", 800))
        turn: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = page_calls
        citations: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        evidence: list[Any] = []
        other_evidence: list[Any] = []  # non-catalog grounding payloads (sightings, events, windows)
        catalog_entries: list[dict[str, Any]] = []
        prompt_tokens: list[int] = []
        grounded = False
        substantive = False  # a grounding result with claimable facts (not just an empty recent_changes window)
        empty_scopes: list[dict[str, Any]] = []
        records: list[tuple[str, Any]] = []  # (tool, data) of grounding results with data, for the AC-AGT-19 summary
        empty_unknowns: list[str | None] = []  # resolved locations of empty list_unknowns results (AC-AGT-23)
        applied_locations: list[str] = []  # location filters actually applied (AC-AGT-20)
        allowed_counts: set[int] = set()  # tool totals of this turn (AC-AGT-28)
        turn_results: list[tuple[str, Any]] = []  # (tool, ToolResult) with data, in order
        rendered: list[tuple[str, Any, Any]] = []  # AC-AGT-29: results answered by the deterministic renderer
        non_render_data = False  # a grounding result that the deterministic renderer cannot answer
        where_q = is_where_question(question)
        item_args: list[tuple[str, Any, Any]] = []  # (tool, result, args) of item tools with data (AC-AGT-34)
        empty_lookups: list[tuple[str, Any]] = []  # (tool, args) of ok lookups that found nothing (AC-AGT-38/39)

        def renderable(name: str, result: Any) -> bool:
            if not result.ok:
                return False
            if name in RENDER_TOOLS:
                return True
            # AC-AGT-35: a multi-item where_is/search_items result answers a where-question as a list
            return where_q and name in ITEM_LIST_TOOLS and isinstance(result.data, list) and len(result.data) >= 2

        async def place_is_named(location: str) -> bool:
            return await self._place_matches(question, location)
        tool_names = set(self.toolbox.names()) if hasattr(self.toolbox, "names") else set()
        finish_reason: str | None = None
        malformed_retried = False
        grounded_at: int | None = None
        invalid_step_seen = False
        nudged = False
        final: str | None = None
        stop_reason = "step_limit"
        steps = 0
        skipped = 0

        for step in range(1, max_steps + 1):
            steps = step
            final_call = step == max_steps or (grounded_at is not None and step > grounded_at + extra_steps)
            await emit({"step": step, "phase": "llm",
                        "text": "Reading the question" if step == 1 else
                        ("Writing the answer" if final_call or grounded else f"Thinking (step {step})")})
            request = context + turn
            message = None
            for attempt in (0, 1):
                prompt_tokens.append(estimate_request_tokens(request, tools))
                remaining = deadline - time.perf_counter()
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(
                        self.llm.complete(request, tools, tool_choice="none" if final_call else "auto"), remaining
                    )
                except (TimeoutError, LLMTimeout):
                    message = None
                    break
                try:
                    content, requested = parse_assistant(message, step)
                    break
                except LLMError:
                    # AC-AGT-22: one repair attempt for a malformed assistant message, then a clear LLMError
                    if attempt or malformed_retried:
                        raise
                    malformed_retried = True
                    log.warning("malformed assistant message, asking again",
                                extra={"request_id": request_id, "step": step})
            if message is None:
                stop_reason = "timeout"
                break
            finish_reason = message.get("finish_reason") if isinstance(message, dict) else None
            text_calls = [] if requested else text_tool_calls(content, tool_names, step)
            if text_calls:
                # AC-AGT-22: a tool call written as text is run as a real call, never shown to the operator
                log.info("text tool call repaired", extra={"request_id": request_id, "step": step,
                                                            "tool_count": len(text_calls)})
                requested, content = text_calls, None
            if not requested:
                if follow_up and not nudged and not tool_calls and step < max_steps:
                    nudged = True  # AC-AGT-12: the model copied an earlier answer instead of looking it up
                    turn += [{"role": "assistant", "content": content or ""}, {"role": "user", "content": TOOL_NUDGE}]
                    continue
                final = content or ""
                stop_reason = "answer"
                break
            if final_call:
                skipped = len(requested)  # results could never reach the model
                if text_calls:
                    stop_reason = "text_tool_call"
                break
            place = (await question_place() if any(c["function"]["name"] in SCOPED_TOOLS | {"search_items"}
                                                   for c in requested) else None)
            requested = [await self._adjust_call(c, window, place_is_named, notes, place, page_offset,
                                                 is_now_question(question))
                         for c in requested]
            pre = [self.toolbox.validate(c["function"]["name"], c["function"]["arguments"]) for c in requested]
            if any(p is not None for p in pre) and invalid_step_seen:
                for call, err in zip(requested, pre, strict=True):
                    if err is not None:
                        self._record(request_id, tool_calls, err.name, call["function"]["arguments"], err, 0.0)
                stop_reason = "invalid_tool_calls"
                break
            invalid_step_seen = invalid_step_seen or any(p is not None for p in pre)
            turn.append({"role": "assistant", "content": content, "tool_calls": requested})
            step_results: list[tuple[str, Any, Any]] = []
            for call, err in zip(requested, pre, strict=True):
                name = call["function"]["name"]
                raw_args = call["function"]["arguments"]
                await emit({
                    "step": step,
                    "phase": "tool",
                    "tool": name if err is None or err.error_kind != "unknown_tool" else "unknown",
                    "args_summary": "invalid arguments" if err is not None else self.toolbox.summarize_args(
                        name, raw_args),
                    "text": TOOL_STATUS.get(name, "Calling a tool") if err is None else "Retrying an invalid lookup",
                })
                t0 = time.perf_counter()
                result = err if err is not None else await self.toolbox.call(name, raw_args)
                self._record(
                    request_id, tool_calls, name, raw_args, result, round((time.perf_counter() - t0) * 1000, 2)
                )
                content_json = fit_payload(result.to_message_payload(), payload_budget)
                parsed_args = _parse_arguments(raw_args)
                step_results.append((name, result, parsed_args))
                if result.ok and not result.has_data:
                    empty_lookups.append((name, parsed_args))
                if result.has_data:
                    allowed_counts |= tool_totals(name, result, content_json)
                    turn_results.append((name, result))
                    if name in GROUNDING_TOOLS and not renderable(name, result):
                        non_render_data = True
                    if name in CAPPED_LIST_TOOLS and isinstance(result.data, list):
                        item_args.append((name, result, parsed_args))
                    await self._emit_facts(emit, step, name, result, parsed_args, renderable(name, result))
                if result.has_data and name == "lookup_catalog":
                    # Only the entries that survived the token budget reach the model: only they ground and are cited.
                    sent = _sent_catalog_entries(content_json)
                    if sent:
                        grounded = True
                        substantive = True
                        keep = {e["catalog_id"] for e in sent}
                        citations.extend(c for c in result.citations if c.get("catalog_id") in keep)
                        catalog_entries.extend(sent)
                        evidence.append(catalog_evidence(sent))
                elif result.has_data and name == "recent_changes":
                    # Only the changes that survived the token budget ground and are cited (AC-AGT-13 review).
                    sent_data = _sent_data(content_json)
                    if isinstance(sent_data, dict):
                        grounded = True
                        keys = {(c.get("entity_id"), c.get("at")) for c in sent_data.get("changes") or []}
                        citations.extend(c for c in result.citations if (c.get("entity_id"), c.get("at")) in keys)
                        view = evidence_view(name, sent_data)
                        evidence.append(view)
                        other_evidence.append(view)
                        if sent_data.get("location"):
                            applied_locations.append(sent_data["location"])
                        if view["changes"]:
                            substantive = True
                            records.append((name, sent_data))
                        else:
                            empty_scopes.append(sent_data)
                elif result.has_data and name in GROUNDING_TOOLS:
                    grounded = True
                    substantive = True
                    citations.extend(result.citations)
                    view = evidence_view(name, result.data)
                    evidence.append(view)
                    other_evidence.append(view)
                    records.append((name, result.data))
                if result.ok and name == "list_unknowns":
                    scoped = (result.scope or {}).get("location")
                    if scoped:
                        applied_locations.append(scoped)
                    if not result.has_data:
                        empty_unknowns.append(scoped)
                if result.has_data and name == "propose_update":
                    proposal = {
                        k: result.data[k] for k in ("proposal_id", "entity_id", "fields", "status") if k in result.data
                    }
                    proposals.append(proposal)
                    evidence.append(proposal)
                turn.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": name,
                        "content": content_json,
                    }
                )
            if grounded and grounded_at is None:
                grounded_at = step
            if (step_results and not proposals and not non_render_data
                    and all(renderable(n, r) for n, r, _ in step_results)):
                rendered = step_results  # AC-AGT-29: deterministic answer, no further LLM call
                stop_reason = "rendered"
                break

        rejected = 0
        rewritten = 0
        if stop_reason == "timeout":
            self._log_turn(request_id, session_id, steps, tool_calls, skipped, grounded, 0, 0, stop_reason,
                           prompt_tokens, nudged, started)
            if not (grounded and citations):
                seconds = float(getattr(self.settings, "turn_deadline_s", 45.0))
                raise LLMTimeout(TIMEOUT_DETAIL.format(seconds=seconds))
            cited_records = _dedup(citations)
            return {
                "request_id": request_id,
                "session_id": session_id,
                "reply": records_reply(cited_records),
                "citations": cited_records,
                "tool_calls": tool_calls,
                "proposals": proposals,
            }
        if stop_reason == "answer":
            await emit({"step": steps, "phase": "checking", "text": "Checking the answer against the records"})
        fallback: str | None = None
        model_reply = False
        all_evidence = evidence_text(evidence)

        async def summary() -> str | None:
            names = await self._location_names()
            return grounded_summary_reply(records, _dedup(citations), names, all_evidence) if records else (
                catalog_facts_reply(catalog_entries) if catalog_entries else None)

        if stop_reason == "rendered":
            reply, citations = await self._render(rendered)
        elif not grounded:
            if empty_unknowns and not proposals:
                labels = [await self._verified_label(lid, question) if lid else None for lid in empty_unknowns]
                reply = none_unknowns_reply(labels)  # AC-AGT-23: an honest empty answer, with its scope
            elif proposals:
                reply = PROPOSAL_REPLY
            elif stop_reason in ("invalid_tool_calls", "text_tool_call"):
                reply = INVALID_TOOL_REPLY
            else:
                reply = NO_RECORD_REPLY
                empty = await self._empty_answer(empty_lookups) if empty_lookups else None
                if empty is not None:
                    # AC-AGT-38/39: say what was checked, and what the catalog knows about an item never seen
                    reply, citations = empty
                    grounded, fallback = True, "empty_lookup"
        elif stop_reason == "answer":
            text = final or ""
            if finish_reason == "length" and text.strip():
                text = trim_cut_reply(text, len(_dedup(citations)))  # AC-AGT-21
                truncated_away = not text
            else:
                truncated_away = False
            if not text.strip() and not truncated_away:
                reply = EMPTY_REPLY
            else:
                rejected = len(unsupported_claims(text, all_evidence)) if text else 1
                reply, model_reply = text, True
                if empty_scopes and not substantive:
                    # The only data is an empty recent_changes window: answer "none" deterministically from the real
                    # scope, never in the model's words (AC-AGT-13 review; AC-AGT-20: a stripped location such as
                    # "USOS" has no digit, so the lexical check could not catch it in the model's wording).
                    reply, rejected, model_reply = none_found_reply(empty_scopes), 0, False
                elif rejected:
                    # AC-AGT-19: records exist, so never "no record": a deterministic summary of the tool data
                    reply, model_reply = (await summary()) or NO_RECORD_REPLY, False
                    if reply != NO_RECORD_REPLY:
                        fallback = "grounded_summary"
                if not rejected and catalog_entries:
                    homes = unqualified_home_locations(text, catalog_entries, evidence_text(other_evidence))
                    if homes:
                        rewritten = len(homes)
                        reply, kept_any = usual_location_rewrite(text, homes, catalog_entries)
                        if not kept_any:
                            keep = {e["catalog_id"] for e in homes}
                            citations = [
                                c for c in citations
                                if c.get("catalog_id") in keep and not c.get("entity_id") and not c.get("window_id")
                            ]
        elif stop_reason == "text_tool_call":
            reply = (await summary() if substantive else None) or INVALID_TOOL_REPLY
            fallback = "grounded_summary" if reply != INVALID_TOOL_REPLY else None
        elif stop_reason == "invalid_tool_calls":
            reply = INVALID_TOOL_REPLY
        else:
            reply = STEP_LIMIT_REPLY

        # AC-AGT-28: counting questions use an uncapped counting path, and every stated count is a tool total
        counting = is_count_question(question)
        people_r = _last(turn_results, "people_seen")
        count_r = _last(turn_results, "count_items")
        forced: str | None = None
        if not proposals and is_people_count_question(question):
            if people_r is None:
                people_r = await self._forced_call(
                    "people_seen", self._people_args(await question_place(), window, notes, is_now_question(question)),
                    request_id, tool_calls, emit)
                forced = "people_count" if people_r is not None else None
            if people_r is not None:
                reply, citations = await self._render([("people_seen", people_r)])
                grounded, rejected, model_reply = True, 0, False
                fallback = fallback or forced
        elif (not proposals and is_sighting_count_question(question) and count_r is None and not catalog_entries
              and (args := derive_count_args(tool_calls))):
            count_r = await self._forced_call("count_items", args, request_id, tool_calls, emit)
            if count_r is not None:
                reply, citations = await self._render([("count_items", count_r)])
                grounded, rejected, model_reply, fallback = True, 0, False, fallback or "item_count"
        if model_reply:
            unverified = [n for n in count_claims(reply, counting=counting) if n not in allowed_counts]
            if unverified:
                model_reply, fallback = False, "count_unverified"
                if people_r is not None or count_r is not None:
                    reply, citations = await self._render(
                        [r for r in (("people_seen", people_r), ("count_items", count_r)) if r[1] is not None])
                else:
                    reply = (await summary()) or NO_RECORD_REPLY
                rejected = rejected or len(unverified)
        if model_reply and people_r is not None and "non-biometric" not in reply:
            reply = f"{reply.rstrip()}\n\n{PEOPLE_CAVEAT}"  # AC-AGT-27: the track caveat is never left out
        if model_reply and applied_locations:
            reply = await self._state_scope(reply, applied_locations)  # AC-AGT-20
        if model_reply:
            reply = normalize_markdown(reply)  # AC-AGT-24
            reply = await self._capped_note(reply, item_args)  # AC-AGT-34
            citations = cited_in_reply(reply, _dedup(citations),
                                       [r.citations for _n, r in turn_results if r.citations])  # AC-AGT-38
        if notes and grounded and reply not in CANNED_REPLIES:
            reply = f"{reply} {' '.join(notes)}"
        cited = grounded and (not rejected or fallback is not None)

        result = {
            "request_id": request_id,
            "session_id": session_id,
            "reply": reply,
            "citations": _dedup(citations) if cited else [],
            "tool_calls": tool_calls,
            "proposals": proposals,
        }
        self._log_turn(request_id, session_id, steps, tool_calls, skipped, grounded, rejected, rewritten, stop_reason,
                       prompt_tokens, nudged, started, fallback=fallback, finish_reason=finish_reason)
        return result

    async def _render(self, results: list[tuple[Any, ...]]) -> tuple[str, list[dict[str, Any]]]:
        """AC-AGT-29/35: the deterministic operator-English answer for (tool, result[, args]) results, with its
        citations: RENDER_TOOLS, and where_is/search_items item lists (AC-AGT-34: with the tool total)."""
        names = await self._location_names()
        texts: list[str] = []
        cites: list[dict[str, Any]] = []
        listed_items = False
        for name, result, *rest in results:
            data = result.data if isinstance(result.data, dict) else {}
            args = rest[0] if rest and isinstance(rest[0], dict) else {}
            if name in ITEM_LIST_TOOLS:
                if listed_items:
                    continue  # where_is and search_items for the same question: one list
                listed_items = True
                items = [i for i in result.data or [] if isinstance(i, dict)]
                shown = top_items(items)
                total = (result.scope or {}).get("total")
                query = args.get("name") if name == "where_is" else args.get("q")
                text = items_reply(items, total, query, names, await self._has_duplicates(shown))
                keep = {i.get("entity_id") for i in shown}
                cited = [c for c in result.citations if c.get("entity_id") in keep]
            elif name == "recent_changes":
                if data.get("changes"):
                    text, cited = changes_reply(data, result.citations, names)
                    shown = [c for c in data["changes"] if isinstance(c, dict)][:RENDER_PAGE]
                    self._remember(text, {"since": data.get("since"), "until": data.get("until"),
                                          "cursor": change_cursor(shown[-1]) if shown else None,
                                          "location": data.get("location")})
                elif data.get("offset"):
                    text, cited = "There are no more changes in that window.", []
                else:
                    text, cited = none_found_reply([data]), []
            elif name == "people_seen":
                text, cited = people_reply(data, result.citations, names)
                self._remember(text, {"since": data.get("since"), "until": data.get("until"),
                                      "location": data.get("location"), "mode": data.get("mode")})
            else:
                text, cited = count_reply(data, names), []
            texts.append(text)
            cites += cited
        return "\n\n".join(texts), _dedup(cites)

    async def _has_duplicates(self, items: list[dict[str, Any]]) -> bool:
        """AC-AGT-34: some listed item needs review, or an open possible_duplicate review names one of them."""
        if any(i.get("needs_review") is True for i in items):
            return True
        flagged = getattr(self.toolbox, "duplicate_flagged", None)
        if flagged is None:
            return False
        try:
            return bool(await flagged([str(i.get("entity_id")) for i in items if i.get("entity_id")]))
        except Exception:  # noqa: BLE001 - a caveat must never break a turn
            return False

    async def _capped_note(self, reply: str, item_args: list[tuple[str, Any, Any]]) -> str:
        """AC-AGT-34: a model-written answer from a capped item list states the tool total ("(3 of 9 items shown.)")
        and, when a listed item may be a duplicate, the duplicate caveat."""
        for _name, result, _args in reversed(item_args):
            total = (result.scope or {}).get("total")
            items = [i for i in result.data or [] if isinstance(i, dict)]
            if not isinstance(total, int) or total <= len(items):
                continue
            listed = list_item_count(reply)
            if not listed or listed >= total:
                return reply  # not a list answer, or every item is listed
            reply = f"{reply.rstrip()}\n\n{shown_note(listed, total)}"
            if await self._has_duplicates(items):
                reply = f"{reply}\n\n{DUPLICATE_CAVEAT}"
            return reply
        return reply

    async def _emit_facts(self, emit, step: int, name: str, result: Any, args: Any, rendering: bool) -> None:
        """AC-AGT-35: a status line with the facts a tool just returned (structured totals only)."""
        try:
            text = facts_text(name, result.data, args, (result.scope or {}).get("total"),
                              await self._location_names(), rendering)
        except Exception:  # noqa: BLE001 - progress reporting must never break a turn
            text = None
        if text:
            await emit({"step": step, "phase": "facts", "tool": name, "text": text})

    async def _question_scope(self, question: str, request_id: str):
        """AC-AGT-32: Toolbox.question_places for the current message; None when unavailable."""
        places = getattr(self.toolbox, "question_places", None)
        if places is None:
            return None
        try:
            return await places(question)
        except Exception as exc:  # noqa: BLE001 - no forced place then (AC-AGT-20 still applies)
            log.warning("question places unavailable", extra={"request_id": request_id, "error": type(exc).__name__})
            return None

    async def _route(self, question: str, scope, window, notes: list[str], request_id: str, session_id: str | None,
                     emit, started: float, tool_calls: list[dict[str, Any]]) -> dict[str, Any] | None:
        """AC-AGT-35: a message that is only a people or a what-changed question is answered by running the tool and
        the deterministic renderer, with no LLM call. None to use the model."""
        if PLACE_REFERENCE_RE.search(question):
            return None
        rest = scope.rest if scope is not None else question
        if scope is not None and len(scope.places) > 1 and scope.location is None:
            return None  # several places: the model decides
        place = scope.location if scope is not None else None
        if window is None and has_window_words(rest):
            return None  # a time phrase the AC-AGT-22 parser cannot read ("since 10"): the model decides
        if is_people_presence_question(rest):
            name, args = "people_seen", self._people_args(place, window, notes, is_now_question(question))
        elif is_changes_question(rest):
            name, args = "recent_changes", self._window_args(window, notes)
            if place:
                args["location"] = place
        else:
            return None
        result = await self._forced_call(name, args, request_id, tool_calls, emit)
        if result is None:
            return None
        reply, citations = await self._render([(name, result, args)])
        if notes:
            reply = f"{reply} {' '.join(notes)}"
        self._log_turn(request_id, session_id, 0, tool_calls, 0, True, 0, 0, "routed", [], False, started)
        return {"request_id": request_id, "session_id": session_id, "reply": reply, "citations": citations,
                "tool_calls": tool_calls, "proposals": []}

    async def _forced_call(self, name: str, args: dict[str, Any], request_id: str, tool_calls: list[dict[str, Any]],
                           emit) -> Any:
        """A counting tool run by the harness itself (AC-AGT-28); the result, or None when it failed."""
        raw = json.dumps(args)
        await emit({"step": 0, "phase": "tool", "tool": name, "args_summary": self.toolbox.summarize_args(name, raw),
                    "text": TOOL_STATUS.get(name, "Calling a tool")})
        t0 = time.perf_counter()
        result = await self.toolbox.call(name, raw)
        self._record(request_id, tool_calls, name, raw, result, round((time.perf_counter() - t0) * 1000, 2))
        if result.ok and result.has_data:
            await self._emit_facts(emit, 0, name, result, args, True)
            return result
        return None

    def _people_args(self, place: str | None, window, notes: list[str], now_mode: bool = False) -> dict[str, Any]:
        """people_seen arguments for a counting/presence question: the AC-AGT-22 window (default: today), the
        single place the question names (AC-AGT-32) and, for "who is in X now", mode "now" (AC-AGT-36)."""
        args = self._window_args(window, notes)
        if place:
            args["location"] = place
            if now_mode:
                args["mode"] = "now"
        return args

    def _window_args(self, window, notes: list[str]) -> dict[str, Any]:
        """since/until from the AC-AGT-22 window (clamped to 7 days, with the note); {} without a window."""
        args: dict[str, Any] = {}
        if window is not None:
            since, until = window
            floor = self._now() - RECENT_MAX_DAYS + timedelta(minutes=2)
            if since < floor:
                since = floor
                if CLAMP_NOTE not in notes:
                    notes.append(CLAMP_NOTE)
            args["since"] = _iso_z(since)
            if until is not None:
                args["until"] = _iso_z(until)
        return args

    def _remember(self, text: str, info: dict[str, Any]) -> None:
        """AC-AGT-37: keep the exact window and cursor of a rendered page that ends with a "show more" hint."""
        marker = parse_page_marker(text)
        if marker is None:
            return
        self._pages[page_key(marker, _SESSION.get())] = {**info, "tool": marker["tool"]}
        self._pages.move_to_end(page_key(marker, _SESSION.get()))
        while len(self._pages) > MAX_PAGE_CURSORS:
            self._pages.popitem(last=False)

    async def _next_page(self, marker: dict[str, Any], request_id: str, session_id: str | None, emit,
                         started: float, tool_calls: list[dict[str, Any]]) -> dict[str, Any] | None:
        """AC-AGT-29: the next page of a rendered list, read back from an earlier reply; None to use the LLM.
        AC-AGT-37: when this agent rendered that page, the page continues from its exact window end and (changes)
        from its last row (key-set cursor), so rows that arrive later neither repeat nor shift the page. Otherwise
        (an agent restart) the window and range are read back from the reply text."""
        kept = self._pages.get(page_key(marker, session_id))
        if kept is not None and kept.get("tool") == marker["tool"] and kept.get("since") and kept.get("until"):
            args: dict[str, Any] = {"since": kept["since"], "until": kept["until"], "offset": marker["offset"]}
            if kept.get("cursor") and marker["tool"] == "recent_changes":
                args["cursor"] = kept["cursor"]
            if kept.get("mode") == "now":
                args["mode"] = "now"
        else:
            now = self._now()
            until = min(marker["until"], now)
            if until <= marker["since"]:
                return None
            args = {"since": _iso_z(marker["since"]), "until": _iso_z(until), "offset": marker["offset"]}
            if marker.get("mode") == "now":
                args["mode"] = "now"
        if marker.get("location"):
            args["location"] = marker["location"]
        result = await self._forced_call(marker["tool"], args, request_id, tool_calls, emit)
        if result is None:
            return None
        reply, citations = await self._render([(marker["tool"], result)])
        self._log_turn(request_id, session_id, 0, tool_calls, 0, True, 0, 0, "paged", [], False, started)
        return {"request_id": request_id, "session_id": session_id, "reply": reply, "citations": citations,
                "tool_calls": tool_calls, "proposals": []}

    async def _private_answer(self, question: str, request_id: str, session_id: str | None,
                              started: float) -> dict[str, Any] | None:
        """AC-AGT-38: "What is in the crew quarters?" is answered in code: the inventory items recorded in the private
        area(s) the message names (label and place only), what was checked, and what the system keeps from there.
        A people-presence question about a private area ("Who was in the crew quarters?") gets a fixed reply: who
        was in a private area is not reported. None (the normal path) for any other message."""
        words = set(_WORD_RE.findall((question or "").lower().replace("'", "")))
        if not words & (_CONTENTS_TRIGGERS | _PEOPLE_TRIGGERS) or len(words) > 16:
            return None
        if not (_PRIVATE_HINT_RE.search(question) or LOCATION_ID_RE.search(question)):
            return None  # cheap gate: no GET /locations for ordinary questions
        scope_of = getattr(self.toolbox, "private_places", None)
        if scope_of is None:
            return None
        try:
            scope = await scope_of(question)
        except Exception:  # noqa: BLE001 - the normal path answers then
            return None
        if scope is None:
            return None
        if is_people_presence_question(scope.rest):
            reply = f"{private_note(scope.crew_quarters, scope.label)} {PRIVATE_PEOPLE_REPLY}"
            self._log_turn(request_id, session_id, 0, [], 0, False, 0, 0, "private_area", [], False, started)
            return {"request_id": request_id, "session_id": session_id, "reply": reply, "citations": [],
                    "tool_calls": [], "proposals": []}
        if not set(_WORD_RE.findall(scope.rest.lower().replace("'", ""))) <= _CONTENTS_WORDS:
            return None
        tool_calls: list[dict[str, Any]] = []
        t0 = time.perf_counter()
        try:
            items = await self.toolbox.items_in(scope.locations)
            ok = True
        except Exception as exc:  # noqa: BLE001 - never guess: the model path answers then
            log.warning("private area lookup failed", extra={"request_id": request_id, "error": type(exc).__name__})
            return None
        tool_calls.append({"name": "private_area_items", "arguments": {"locations": list(scope.locations)},
                           "ok": ok, "error_kind": None, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)})
        names = await self._location_names()
        reply = private_reply(scope.label, scope.crew_quarters, items, names)
        shown = {i.get("entity_id") for i in items[:RENDER_PAGE]}
        citations = [_private_cite(i) for i in items if i.get("entity_id") in shown]
        self._log_turn(request_id, session_id, 0, tool_calls, 0, True, 0, 0, "private_area", [], False, started)
        return {"request_id": request_id, "session_id": session_id, "reply": reply, "citations": citations,
                "tool_calls": tool_calls, "proposals": []}

    async def _empty_answer(self, lookups: list[tuple[str, Any]]) -> tuple[str, list[dict[str, Any]]] | None:
        """AC-AGT-38/39: the answer when every lookup of the turn found nothing. Names what was checked ("I checked
        items matching "sextant": none are recorded."). For an item name: if nothing by that name was ever seen,
        "No drill has been seen yet in this run (catalog: ...)."; if items exist but nobody handled them, the items
        checked. None when there is nothing honest to add."""
        names = await self._location_names()
        checked: list[str] = []
        extra: list[str] = []
        cites: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        lookup = getattr(self.toolbox, "name_lookup", None)
        for tool, args in lookups:
            args = args if isinstance(args, dict) else {}
            if tool in ("where_is", "who_moved", "search_items"):
                noun = lookup_noun(args.get("name") if tool != "search_items" else args.get("q"))
                if noun is None or lookup is None:
                    continue
                try:
                    found = await lookup(noun)
                except Exception as exc:  # noqa: BLE001 - the plain reply stands
                    log.warning("name lookup failed", extra={"tool": tool, "error": type(exc).__name__})
                    continue
                if not found["total"]:
                    if noun.lower() not in seen_names:
                        seen_names.add(noun.lower())
                        if tool in ("search_items", "where_is"):
                            checked.append(f'items matching "{noun}"')
                        extra.append(never_seen_sentence(noun, found["catalog"], names))
                        cites += [{"catalog_id": e["catalog_id"], "label": e.get("display_name")}
                                  for e in found["catalog"]]
                elif tool == "who_moved":
                    n = found["total"]
                    kinds = [EVENT_VERBS.get(t, t) for t in args.get("event_types") or [] if t in EVENT_VERBS]
                    what = ("events (" + ", ".join(kinds) + ")") if kinds else "moves, uses and hand-overs"
                    since = stamp(args.get("since")) if args.get("since") else None
                    checked.append(f'{what} of {n} item{"" if n == 1 else "s"} matching "{noun}"'
                                   + (f" since {since}" if since else ""))
                    extra.append("\n".join(item_line(i, names) for i in found["items"]))
                    cites += [_private_cite(i) for i in found["items"]]
                else:
                    checked.append(f'items matching "{noun}"')
            elif tool == "list_unknowns":
                where = place(args.get("location"), names) if args.get("location") else None
                checked.append("open unknown objects" + (f" in {where}" if where else ""))
            elif tool == "search_observations":
                q = safe_query_text(args.get("q"))
                checked.append(f'video observations mentioning "{q}"' if q else "video observations")
            elif tool in ("get_item", "item_history"):
                checked.append("that item record")
        if not checked and not extra:
            return None
        parts = ([f"I checked {', '.join(dict.fromkeys(checked))}: none are recorded."] if checked else []) + extra
        sep = "\n" if any(": last seen" in x or ": no sighting" in x for x in extra) else " "
        return sep.join(x for x in parts if x).strip(), cites

    async def _verified_label(self, location_id: str, question: str) -> str:
        """Review 8: a scope is printed only when it is a known location or the operator typed it."""
        label = await self._label(location_id)
        if label != location_id or re.search(rf"(?<![A-Za-z0-9]){re.escape(location_id)}(?![A-Za-z0-9])",
                                              question, re.IGNORECASE):
            return label
        return UNVERIFIED_SCOPE

    async def _label(self, location_id: str) -> str:
        label_of = getattr(self.toolbox, "location_label", None)
        if label_of is None:
            return location_id
        try:
            return await label_of(location_id)
        except Exception:  # noqa: BLE001 - names are decoration only
            return location_id

    def _log_turn(self, request_id, session_id, steps, tool_calls, skipped, grounded, rejected, rewritten,
                  stop_reason, prompt_tokens, nudged, started, *, fallback=None, finish_reason=None) -> None:
        log.info(
            "chat turn",
            extra={
                "request_id": request_id,
                "session_id": session_id,
                "steps": steps,
                "tool_count": len(tool_calls),
                "skipped_tool_calls": skipped,
                "grounded": grounded,
                "unsupported_claims": rejected,
                "usual_location_rewrites": rewritten,
                "stop": stop_reason,
                "follow_up_nudged": nudged,
                "fallback": fallback,
                "finish_reason": finish_reason,
                "prompt_tokens_est": list(prompt_tokens),
                "prompt_tokens_total": sum(prompt_tokens),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )


def _private_cite(item: dict[str, Any]) -> dict[str, Any]:
    """A citation for a rendered item row (label, last location and time; never its description)."""
    cite = {"entity_id": item.get("entity_id"), "at": item.get("last_seen_at"), "label": item.get("label"),
            "location": item.get("last_location")}
    if item.get("last_location_name"):
        cite["location_name"] = item["last_location_name"]
    return {k: v for k, v in cite.items() if v is not None}


def _label_words(label: Any) -> tuple[str | None, list[str]]:
    """(whole label without its parenthetical, identifying words) of a cited label, lower case."""
    text = str(label or "").lower()
    inner = " ".join(re.findall(r"\((.*?)\)", text))
    base = re.sub(r"\s*\(.*?\)\s*", " ", text).strip()
    words = [w for w in _WORD_RE.findall(base) if len(w) >= 3 and w not in _GENERIC_LABEL_WORDS]
    head = words[-1:] if words else []
    phrase = inner.split(",")[0].strip() if inner else ""
    return (base or None), head + ([phrase] if phrase else [])


def _names_row(c: dict[str, Any], text: str, words: set[str]) -> bool:
    """AC-AGT-38: the reply states this cited row: its id (or 8-character short id), catalog_id, whole label, the
    label's head noun ("latch" for "yellow latch"), a person's first appearance phrase, or its HH:MM time."""
    ident = str(c.get("entity_id") or c.get("window_id") or "").lower()
    if ident and (ident in text or ident[:8] in text):
        return True
    if c.get("catalog_id") and str(c["catalog_id"]).lower() in text:
        return True
    whole, keys = _label_words(c.get("label"))
    if whole and whole in text:
        return True
    for k in keys:
        if (" " in k and k in text) or k in words or f"{k}s" in words or (k.endswith("s") and k[:-1] in words):
            return True
    at = _hhmm(c.get("at"))
    return bool(at) and at in text


def cited_in_reply(reply: str, citations: list[dict[str, Any]], groups: list[list[dict[str, Any]]]
                   ) -> list[dict[str, Any]]:
    """AC-AGT-38: a model-written reply cites only rows it uses. Per tool result (``groups``): if the reply states some
    of its rows, only those are cited; if it states none, the result is cited only when the reply is not a
    "no record" answer (a summary such as "Two unknowns" still uses it). Citations from no group are kept."""
    text = (reply or "").lower()
    words = set(_WORD_RE.findall(text))
    negative = bool(_NEGATIVE_RE.search(reply or ""))
    present = {citation_key(c) for c in citations}
    grouped: set[Any] = set()
    keep: set[Any] = set()
    for group in groups:
        rows = [c for c in group if citation_key(c) in present]
        grouped |= {citation_key(c) for c in rows}
        named = [c for c in rows if _names_row(c, text, words)]
        if named:
            keep |= {citation_key(c) for c in named}
        elif not negative:
            keep |= {citation_key(c) for c in rows}
    return [c for c in citations if citation_key(c) in keep or citation_key(c) not in grouped]


def _last(results: list[tuple[str, Any]], name: str) -> Any:
    return next((r for n, r in reversed(results) if n == name), None)


def tool_totals(name: str, result: Any, content_json: str | None = None) -> set[int]:
    """Numbers a reply may state as counts (AC-AGT-28): totals and per-module counts of this turn's tools, the API
    ``total`` of an item query (never the length of a capped list) and the catalog ``expected_count`` of the entries
    the model saw."""
    data = result.data
    out: set[int] = set()

    def add(value: Any) -> None:
        if isinstance(value, int) and not isinstance(value, bool):
            out.add(value)

    if name == "lookup_catalog":
        for e in _sent_catalog_entries(content_json) if content_json else []:
            add(e.get("expected_count"))
        return out

    if isinstance(data, dict):
        for key in ("total_tracks", "total", "tracks_omitted", "omitted"):
            add(data.get(key))
        for m in data.get("by_module") or []:
            if isinstance(m, dict):
                add(m.get("count"))
    if isinstance(result.scope, dict):
        add(result.scope.get("total"))
        total = result.scope.get("total")
        if isinstance(data, list) and isinstance(total, int) and 0 <= total <= len(data):
            out |= set(range(total + 1))  # a complete (uncapped) list: sub-counts of it are verifiable
    if isinstance(data, dict) and isinstance(data.get("changes"), list) and not data.get("offset"):
        total = data.get("total")
        if isinstance(total, int) and 0 <= total <= len(data["changes"]):
            out |= set(range(total + 1))
    return out


def derive_count_args(tool_calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """count_items arguments from the model's own item query of this turn (search_items filters, where_is name,
    list_unknowns scope); None when there is none (AC-AGT-28)."""
    for t in reversed(tool_calls):
        args = t.get("arguments")
        if not t.get("ok") or not isinstance(args, dict):
            continue
        if t.get("name") == "search_items" and isinstance(args.get("q"), str):
            f = args.get("filters") if isinstance(args.get("filters"), dict) else {}
            return {"q": args["q"], **{k: f[k] for k in ("kind", "status", "catalog_id", "location", "seen_after")
                                       if f.get(k)}}
        if t.get("name") == "where_is" and isinstance(args.get("name"), str):
            return {"q": args["name"]}
        if t.get("name") == "list_unknowns":
            return {"status": ["unknown"], **({"location": args["location"]} if args.get("location") else {})}
    return None


def none_found_reply(scopes: list[dict[str, Any]]) -> str:
    """Deterministic "no changes" answer from empty recent_changes windows (their scope only)."""
    parts = []
    for sc in scopes:
        since, until = _parse_iso(sc.get("since")), _parse_iso(sc.get("until"))
        where = f" in {sc['location']}" if sc.get("location") else ""
        if since and until:
            parts.append(f"No changes were recorded{where} between {since:%Y-%m-%d %H:%M} and {until:%H:%M} UTC")
        else:
            parts.append(f"No changes were recorded{where} in the requested window")
    return "; ".join(dict.fromkeys(parts)) + "."


def records_reply(citations: list[dict[str, Any]]) -> str:
    """Deterministic, honest timeout reply (AC-AGT-15): up to 5 cited records as "label (location) at HH:MM UTC".
    A label (free text) that carries a location/time/actor claim of its own is replaced by the short id."""
    parts = []
    for c in citations[:MAX_TIMEOUT_RECORDS]:
        short_id = (c.get("entity_id") or c.get("window_id") or "record")[:8]
        label = c.get("label")
        own = json.dumps({k: c.get(k) for k in ("location", "location_name", "at")})
        if label and unsupported_claims(label, own):
            label = None
        label = label or c.get("catalog_id") or short_id
        where = c.get("location")
        if where and c.get("location_name"):
            where = f"{where}, {c['location_name']}"
        text = label + (f" ({where})" if where else "")
        at = _hhmm(c.get("at"))
        parts.append(text + (f" at {at} UTC" if at else ""))
    more = len(citations) - len(parts)
    tail = f"; and {more} more" if more > 0 else ""
    return f"{TIMEOUT_RECORDS_LEAD} " + "; ".join(parts) + f"{tail}. Please try again for a written answer."


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _hhmm(value: Any) -> str | None:
    dt = _parse_iso(value)
    return dt.strftime("%H:%M") if dt else None


def _sent_data(content_json: str) -> Any:
    """The ``data`` of the (budget-fitted) tool message actually sent to the model."""
    try:
        return json.loads(content_json).get("data")
    except (ValueError, AttributeError):
        return None


def _sent_catalog_entries(content_json: str) -> list[dict[str, Any]]:
    """lookup_catalog entries present in the (budget-fitted) tool message actually sent to the model."""
    try:
        data = json.loads(content_json).get("data")
    except (ValueError, AttributeError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("catalog_id"), str)]


def _dedup(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AC-AGT-33: one citation per entity (the first); window and catalog citations by their full key."""
    seen: set[Any] = set()
    out = []
    for c in citations:
        key = ("entity", c["entity_id"]) if c.get("entity_id") else citation_key(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------- AC-AGT-22: relative time windows
_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90,
}
_UNITS = {"m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
          "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
          "day": "days", "days": "days", "week": "weeks", "weeks": "weeks"}
_LAST = r"\b(?:last|past|previous)\s+"
_N_UNITS_RE = re.compile(
    _LAST + r"(\d{1,4}|" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + r")\s*"
    r"(minutes?|mins?|m|hours?|hrs?|h|days?|weeks?)\b", re.IGNORECASE)
_HALF_HOUR_RE = re.compile(r"\bhalf\s+(?:an\s+)?hour\b", re.IGNORECASE)
_ONE_UNIT_RE = re.compile(_LAST + r"(hour|day|week)\b", re.IGNORECASE)
_TODAY_RE = re.compile(r"\btoday\b", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"\byesterday\b", re.IGNORECASE)
_UNSUPPORTED_DAY_RE = re.compile(r"\bday\s+before\s+yesterday\b|\bof\s+yesterday\b", re.IGNORECASE)
_SINCE_CLOCK_RE = re.compile(
    r"\bsince\s+(\d{1,2})(?::([0-5]\d))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\s*(?:utc|z)?\b|\s*[?.!,]|$)", re.IGNORECASE)


def parse_time_window(text: str, now: datetime) -> tuple[datetime, datetime | None] | None:
    """(since, until or None) for a relative window in the user's message, computed from ``now`` (UTC), or None when
    the message has no parseable phrase (AC-AGT-22)."""
    if not isinstance(text, str) or not text:
        return None
    now = now.astimezone(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if _UNSUPPORTED_DAY_RE.search(text):
        return None  # "the day before yesterday", "the last hour of yesterday": keep the model's arguments
    m = _SINCE_CLOCK_RE.search(text)
    if m and (m.group(2) is not None or m.group(3)):
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
        if ampm:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        elif hour > 23:
            return None
        since = midnight.replace(hour=hour, minute=minute)
        if _YESTERDAY_RE.search(text[m.end():]):
            return since - timedelta(days=1), None  # "since 10am yesterday"
        return (since - timedelta(days=1) if since > now else since), None
    if _YESTERDAY_RE.search(text):
        return midnight - timedelta(days=1), midnight
    m = _N_UNITS_RE.search(text)
    if m:
        raw = m.group(1).lower()
        amount = int(raw) if raw.isdigit() else _NUM_WORDS[raw]
        unit = _UNITS[m.group(2).lower()]
        if amount > 0:
            return now - timedelta(**{unit: amount}), None
    if _HALF_HOUR_RE.search(text):
        return now - timedelta(minutes=30), None
    m = _ONE_UNIT_RE.search(text)
    if m:
        return now - timedelta(**{_UNITS[m.group(1).lower()]: 1}), None
    if _TODAY_RE.search(text):
        return midnight, None
    return None


# ---------------------------------------------------------------- AC-AGT-22: tool calls written as text
_TOOL_TAG_RE = re.compile(r"^\s*<tool_call>\s*(.*?)\s*</tool_call>\s*$", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def text_tool_calls(content: str | None, names: list[str] | set[str], step: int) -> list[dict[str, Any]]:
    """Tool calls the model wrote as text (the whole content is ``{"name": <tool>, "arguments": {...}}``, a list of
    them, optionally in <tool_call> tags or a ```json fence). Empty list otherwise."""
    if not isinstance(content, str) or "name" not in content:
        return []
    text = content.strip()
    for pattern in (_TOOL_TAG_RE, _FENCE_RE):
        m = pattern.match(text)
        if m:
            text = m.group(1)
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    calls = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or item.get("name") not in names:
            return []
        args = item.get("arguments", item.get("parameters", {}))
        calls.append({"id": f"call_text_{step}_{i}", "type": "function",
                      "function": {"name": item["name"],
                                   "arguments": args if isinstance(args, str) else json.dumps(args)}})
    return calls


# ---------------------------------------------------------------- AC-AGT-21: answers cut at max_tokens
_LIST_ITEM_RE = re.compile(r"^(?:\d+[.)]|[-*+])\s+\S")
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
SHORTENED_NOTE = "(answer shortened; ask for more)"


def showing_note(k: int, n: int) -> str:
    return f"(showing the first {k} of {n}; ask for more)"


def _close_markdown(text: str) -> str:
    text = text.rstrip()
    if text.count("**") % 2:
        head, _, tail = text.rpartition("**")
        text = (head + tail).rstrip()
    return text.rstrip(" -*:;,")


def trim_cut_reply(text: str, n_records: int) -> str:
    """A reply cut at max_tokens, ended at the last complete line (multi-line) or sentence, with a notice. Empty
    string when nothing complete is left."""
    lines = text.rstrip().split("\n")
    if len(lines) > 1:
        kept = lines[:-1]
        while kept and not kept[-1].strip():
            kept.pop()
        body = "\n".join(kept)
    else:
        ends = list(_SENTENCE_END_RE.finditer(text))
        body = text[: ends[-1].end()] if ends else ""
    body = _close_markdown(body) if body.strip() else ""
    k = sum(1 for line in body.split("\n") if _LIST_ITEM_RE.match(line))
    if not body or not (k or TIME_RE.search(body) or LOCATION_ID_RE.search(body) or NODE_RE.search(body)):
        return ""  # review 6: nothing factual survived; the caller answers with the grounded summary
    if k:
        note = showing_note(k, n_records) if n_records > k else SHORTENED_NOTE
        return f"{body}\n\n{note}"
    return f"{body} {SHORTENED_NOTE}"


# ---------------------------------------------------------------- AC-AGT-24: chat Markdown
_ISO_Z_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]00:?00)(?:\s*UTC\b)?")
_ORDERED_RE = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")
_CONTINUATION_RE = re.compile(r"^\s{2,}(?:(?:[-*+]|\d+[.)])\s+)?(\S.*)$")


def normalize_markdown(text: str) -> str:
    """One flat numbered list per run of ordered items (renumbered 1..n across blank lines, indented sub-items folded
    into their parent with "; "), and ISO-Z timestamps in the dashboard format "YYYY-MM-DD HH:MM UTC"."""
    if not isinstance(text, str) or not text:
        return text
    text = _ISO_Z_RE.sub(r"\1 \2 UTC", text)
    out: list[str] = []
    pending: list[str] = []
    counter = 0
    in_list = False
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```") or in_code:
            if line.lstrip().startswith("```"):
                in_code = not in_code
            out.extend(pending)
            pending, in_list = [], False
            out.append(line)
            continue
        m = _ORDERED_RE.match(line)
        if m:
            if not in_list:
                out.extend(pending)
                counter = 0
            pending = []
            counter += 1
            out.append(f"{counter}. {m.group(2)}")
            in_list = True
            continue
        if in_list:
            c = _CONTINUATION_RE.match(line)
            if c and not pending:
                out[-1] = f"{out[-1].rstrip()}; {c.group(1).strip()}"
                continue
            if not line.strip():
                pending.append(line)
                continue
            in_list = False
        out.extend(pending)
        pending = []
        out.append(line)
    out.extend(pending)
    return "\n".join(out)


# ---------------------------------------------------------------- AC-AGT-19: deterministic grounded summary
MAX_SUMMARY_RECORDS = 5
SUMMARY_LEAD = "Here is what the records show:"
_ITEM_TOOLS = frozenset({"where_is", "search_items", "list_unknowns", "get_item"})


def _summary_lines(records: list[tuple[str, Any]], names: dict[str, str | None]) -> list[str]:
    """AC-AGT-19 summary lines in the AC-AGT-29 operator English (an event never gets the item's current place)."""
    lines: list[str] = []
    for name, data in records:
        if name == "who_moved" and isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                for ev in entry.get("moves") or []:
                    if isinstance(ev, dict):
                        lines.append(change_line({
                            **ev, "change": ev.get("event_type"), "at": ev.get("occurred_at"),
                            "actor": ev.get("actor_desc"), "entity_id": entry.get("entity_id"),
                            "label": entry.get("label"), "subject_kind": entry.get("subject_kind"),
                            "appearance": entry.get("appearance")}, names))
        elif name == "recent_changes" and isinstance(data, dict):
            lines += [change_line(c, names) for c in data.get("changes") or [] if isinstance(c, dict)]
        elif name == "people_seen" and isinstance(data, dict):
            lines += [item_line({**t, "kind": "person", "last_location": t.get("location"),
                                 "last_location_name": t.get("location_name")}, names)
                      for t in data.get("tracks") or [] if isinstance(t, dict)]
        elif name in _ITEM_TOOLS:
            lines += [item_line(item, names) for item in (data if isinstance(data, list) else [data])
                      if isinstance(item, dict)]
    return list(dict.fromkeys(lines))


def grounded_summary_reply(records: list[tuple[str, Any]], citations: list[dict[str, Any]],
                           names: dict[str, str | None], evidence: str) -> str | None:
    """Deterministic answer from this turn's tool data, used when the claim check rejects the model's wording (never
    "no record" when records exist). Falls back to the cited records; None when there is nothing to say."""
    lines = _summary_lines(records, names)
    if lines:
        shown = lines[:MAX_SUMMARY_RECORDS]
        text = shown[0] if len(shown) == 1 else SUMMARY_LEAD + "\n" + "\n".join(
            f"{i}. {line}" for i, line in enumerate(shown, 1))
        if len(lines) > len(shown):
            text += "\n\n" + showing_note(len(shown), len(lines))
        if not unsupported_claims(text, evidence):
            return text
    if citations:
        return records_reply(citations).replace(TIMEOUT_RECORDS_LEAD, SUMMARY_LEAD, 1).replace(
            " Please try again for a written answer.", "")
    return None


UNVERIFIED_SCOPE = "the requested location"


def none_unknowns_reply(labels: list[str | None]) -> str:
    """AC-AGT-23: an empty list_unknowns result, stated with its scope."""
    where = [lbl for lbl in dict.fromkeys(labels) if lbl]
    if not where:
        return "There are no unknown objects on record."
    return f"There are no unknown objects in {' or '.join(where)}."


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
