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
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
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
    catalog_evidence,
    evidence_text,
    unqualified_home_locations,
    unsupported_claims,
    usual_location_rewrite,
)
from nasa_agent.llm import LLMError, LLMTimeout
from nasa_agent.tools import GROUNDING_TOOLS, citation_key, evidence_view

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
            prompt_tokens.append(estimate_request_tokens(request, tools))
            remaining = deadline - time.perf_counter()
            try:
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(
                    self.llm.complete(request, tools, tool_choice="none" if final_call else "auto"), remaining
                )
            except (TimeoutError, LLMTimeout):
                stop_reason = "timeout"
                break
            content, requested = parse_assistant(message, step)
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
                break
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
                        if view["changes"]:
                            substantive = True
                        else:
                            empty_scopes.append(sent_data)
                elif result.has_data and name in GROUNDING_TOOLS:
                    grounded = True
                    substantive = True
                    citations.extend(result.citations)
                    view = evidence_view(name, result.data)
                    evidence.append(view)
                    other_evidence.append(view)
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
        if not grounded:
            reply = PROPOSAL_REPLY if proposals else NO_RECORD_REPLY
        elif stop_reason == "answer":
            if not (final and final.strip()):
                reply = EMPTY_REPLY
            else:
                rejected = len(unsupported_claims(final, evidence_text(evidence)))
                reply = NO_RECORD_REPLY if rejected else final
                if rejected and empty_scopes and not substantive:
                    # The only data is an empty recent_changes window: answer "none" deterministically from the
                    # scope instead of letting the scope ground the model's words (AC-AGT-13 review).
                    reply, rejected = none_found_reply(empty_scopes), 0
                if not rejected and catalog_entries:
                    homes = unqualified_home_locations(final, catalog_entries, evidence_text(other_evidence))
                    if homes:
                        rewritten = len(homes)
                        reply, kept_any = usual_location_rewrite(final, homes, catalog_entries)
                        if not kept_any:
                            keep = {e["catalog_id"] for e in homes}
                            citations = [
                                c for c in citations
                                if c.get("catalog_id") in keep and not c.get("entity_id") and not c.get("window_id")
                            ]
        elif stop_reason == "invalid_tool_calls":
            reply = INVALID_TOOL_REPLY
        else:
            reply = STEP_LIMIT_REPLY
        cited = grounded and not rejected

        result = {
            "request_id": request_id,
            "session_id": session_id,
            "reply": reply,
            "citations": _dedup(citations) if cited else [],
            "tool_calls": tool_calls,
            "proposals": proposals,
        }
        self._log_turn(request_id, session_id, steps, tool_calls, skipped, grounded, rejected, rewritten, stop_reason,
                       prompt_tokens, nudged, started)
        return result

    def _log_turn(self, request_id, session_id, steps, tool_calls, skipped, grounded, rejected, rewritten,
                  stop_reason, prompt_tokens, nudged, started) -> None:
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
