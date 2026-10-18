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
   (or ``PROPOSAL_REPLY`` when only a proposal was made). In a grounded turn, any location/time/actor in the reply
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
from collections.abc import Callable
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
    scrub_claims,
    unqualified_home_locations,
    unsupported_claims,
    usual_location_rewrite,
)
from nasa_agent.llm import LLMError, LLMTimeout
from nasa_agent.tools import (
    GROUNDING_TOOLS,
    MAX_CITATION_TEXT,
    citation_key,
    clean_text,
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
SCOPED_TOOLS = frozenset({"recent_changes", "list_unknowns"})
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
    "propose_update": "Preparing a proposed change",
}

log = logging.getLogger("nasa_agent.harness")


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
    changes = data.get("changes") if isinstance(data, dict) else None
    if isinstance(changes, list) and changes:
        # recent_changes (AC-AGT-13): drop whole changes from the end, keep the structure and the scope
        lo, hi, best = 0, len(changes) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            sent = changes[:mid]
            keys = {(c.get("entity_id"), c.get("at")) for c in sent if isinstance(c, dict)}
            cand = json.dumps(
                {
                    **payload,
                    "data": {**data, "changes": sent, "omitted": len(changes) - mid + int(data.get("omitted") or 0)},
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

    async def _adjust_call(self, call: dict[str, Any], window, place_is_named, notes: list[str]) -> dict[str, Any]:
        """AC-AGT-20/22: the arguments actually run. A `location` of recent_changes/list_unknowns is dropped unless the
        current message names a place; a relative window parsed from the message replaces the model's since/until."""
        name = call["function"]["name"]
        if name not in SCOPED_TOOLS and name != "who_moved":
            return call
        args = _parse_arguments(call["function"]["arguments"])
        if not isinstance(args, dict):
            return call  # validation reports it
        changed = False
        loc = args.get("location")
        if name in SCOPED_TOOLS and loc is not None and not (isinstance(loc, str) and await place_is_named(loc)):
            args.pop("location")
            changed = True
        if window is not None and name == "recent_changes":
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
        tool_calls: list[dict[str, Any]] = []
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
        window = parse_time_window(question, self._now())  # AC-AGT-22
        notes: list[str] = []  # harness notes appended to the reply (AC-AGT-22 clamp)

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
            requested = [await self._adjust_call(c, window, place_is_named, notes) for c in requested]
            pre = [self.toolbox.validate(c["function"]["name"], c["function"]["arguments"]) for c in requested]
            if any(p is not None for p in pre) and invalid_step_seen:
                for call, err in zip(requested, pre, strict=True):
                    if err is not None:
                        self._record(request_id, tool_calls, err.name, call["function"]["arguments"], err, 0.0)
                stop_reason = "invalid_tool_calls"
                break
            invalid_step_seen = invalid_step_seen or any(p is not None for p in pre)
            turn.append({"role": "assistant", "content": content, "tool_calls": requested})
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

        if not grounded:
            if empty_unknowns and not proposals:
                labels = [await self._verified_label(lid, question) if lid else None for lid in empty_unknowns]
                reply = none_unknowns_reply(labels)  # AC-AGT-23: an honest empty answer, with its scope
            elif proposals:
                reply = PROPOSAL_REPLY
            elif stop_reason in ("invalid_tool_calls", "text_tool_call"):
                reply = INVALID_TOOL_REPLY
            else:
                reply = NO_RECORD_REPLY
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
        if model_reply and applied_locations:
            reply = await self._state_scope(reply, applied_locations)  # AC-AGT-20
        if model_reply:
            reply = normalize_markdown(reply)  # AC-AGT-24
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
    seen: set[tuple[Any, Any, Any, Any]] = set()
    out = []
    for c in citations:
        key = citation_key(c)
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


def _when(value: Any) -> str | None:
    dt = _parse_iso(value)
    return f"{dt:%H:%M} UTC on {dt:%Y-%m-%d}" if dt else None


def _safe_label(label: Any, entity_id: Any, own: dict[str, Any]) -> str:
    short = f"[{str(entity_id)[:8]}]" if entity_id else ""
    text = clean_text(label, MAX_CITATION_TEXT)
    if text and unsupported_claims(text, json.dumps(own, default=str)):
        text = None
    return f"The {text} {short}".strip() if text else f"Record {short}".strip()


def _place(lid: Any, names: dict[str, str | None]) -> str | None:
    if not lid:
        return None
    name = names.get(lid)
    return f"{lid}, {name}" if name else str(lid)


_DANGLING_RE = re.compile(r"(?:\s+(?:in|at|on|to|from|into|near|by|the|of|and))+\s*$", re.IGNORECASE)


_ACTOR_TEXT_RE = re.compile(r"[A-Za-z0-9 ,'()/-]{1,60}")
_ACTION_RE = re.compile(r"[a-z_]{1,32}")
MAX_ACTOR_WORDS = 7


def _actor(value: Any) -> str:
    """Actor text for the deterministic summary: scrubbed of places/times and allow-listed (short, plain words), so
    free text from the pipeline/operator cannot carry instructions to the operator (review 2)."""
    text = scrub_claims(clean_text(value))
    if text:
        text = _DANGLING_RE.sub("", text).strip(" ,;")  # "who put it in NOD3 at 09:00" -> "who put it"
    if not text or not _ACTOR_TEXT_RE.fullmatch(text) or len(text.split()) > MAX_ACTOR_WORDS:
        return "an unidentified person"
    if re.match(r"(?:in|wearing|with)\b", text, re.IGNORECASE):
        return f"person {text}"
    return text


def _event_sentence(label: str, ev: dict[str, Any], names: dict[str, str | None], fallback_loc: Any = None) -> str:
    kind = str(ev.get("event_type") or ev.get("change") or "handled").replace("_", " ")
    frm, to = ev.get("from_location"), ev.get("to_location")
    when = _when(ev.get("occurred_at") or ev.get("at"))
    actor = ev.get("actor_desc") if "actor_desc" in ev else ev.get("actor")
    if kind == "moved" and (frm or to):
        route = (f" from {frm}" if frm else "") + (f" to {to}" if to else "")
        where = ""
    else:
        route = ""
        place = _place(frm or to or fallback_loc, names)
        where = f" ({place})" if place else ""
    return f"{label} was {kind}{route} by {_actor(actor)}" + (f" at {when}" if when else "") + f"{where}."


def _summary_lines(records: list[tuple[str, Any]], names: dict[str, str | None]) -> list[str]:
    lines: list[str] = []
    for name, data in records:
        if name == "who_moved" and isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                for ev in entry.get("moves") or []:
                    own = {k: ev.get(k) for k in ("from_location", "to_location", "occurred_at")}
                    label = _safe_label(entry.get("label"), entry.get("entity_id"), own)
                    lines.append(_event_sentence(label, ev, names))  # review 9: never the item's current place
        elif name == "recent_changes" and isinstance(data, dict):
            for c in data.get("changes") or []:
                if not isinstance(c, dict):
                    continue
                own = {k: c.get(k) for k in ("from_location", "to_location", "location", "at")}
                label = _safe_label(c.get("label"), c.get("entity_id"), own)
                where = _place(c.get("location"), names)
                when = _when(c.get("at"))
                if c.get("change") == "new_item":
                    lines.append(f"{label} was first seen" + (f" in {where}" if where else "")
                                 + (f" at {when}" if when else "") + ".")
                elif c.get("change") == "review_resolved":
                    action = c.get("action") if isinstance(c.get("action"), str) else None
                    action = action if action and _ACTION_RE.fullmatch(action) else None
                    lines.append(f"A review of {label[0].lower()}{label[1:]} was resolved"
                                 + (f" ({action})" if action else "") + f" by {_actor(c.get('actor'))}"
                                 + (f" at {when}" if when else "") + (f" ({where})" if where else "") + ".")
                else:
                    lines.append(_event_sentence(label, c, names, c.get("location")))
        elif name in _ITEM_TOOLS:
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict):
                    continue
                own = {k: item.get(k) for k in ("last_location", "last_location_name", "last_seen_at")}
                label = _safe_label(item.get("label"), item.get("entity_id"), own)
                lid = item.get("last_location")
                where = f"{lid}, {item['last_location_name']}" if lid and item.get("last_location_name") else lid
                when = _when(item.get("last_seen_at"))
                lines.append(f"{label} was last seen" + (f" in {where}" if where else "")
                             + (f" at {when}" if when else "") + ".")
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
