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

Logs carry ids, tool names, status and latency only - never message content or tool payloads (no raw images).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from nasa_agent.config import MAX_STEPS_LIMIT
from nasa_agent.context import SYSTEM_PROMPT, build_context, estimate_tokens, truncate_to_tokens
from nasa_agent.grounding import (
    catalog_evidence,
    evidence_text,
    unqualified_home_locations,
    unsupported_claims,
    usual_location_reply,
)
from nasa_agent.llm import LLMError
from nasa_agent.tools import GROUNDING_TOOLS, citation_key

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
INVALID_KINDS = frozenset({"validation", "unknown_tool"})

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

    # ------------------------------------------------------------ loop
    async def run(
        self, messages: list[dict[str, Any]], *, session_id: str | None = None, request_id: str | None = None
    ) -> dict[str, Any]:
        """Returns {request_id, session_id, reply, citations, tool_calls, proposals}."""
        request_id = request_id or str(uuid.uuid4())
        started = time.perf_counter()
        history = [
            {"role": m["role"], "content": m.get("content")} for m in messages if m.get("role") in ("user", "assistant")
        ]
        question = next(
            (m["content"] for m in reversed(history) if m["role"] == "user" and isinstance(m.get("content"), str)), ""
        )

        block = await self._render_block(question, request_id)
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
        payload_budget = int(getattr(self.settings, "tool_result_token_budget", 1500))
        turn: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        evidence: list[Any] = []
        other_evidence: list[Any] = []  # non-catalog grounding payloads (sightings, events, windows)
        catalog_entries: list[dict[str, Any]] = []
        grounded = False
        invalid_step_seen = False
        final: str | None = None
        stop_reason = "step_limit"
        steps = 0
        skipped = 0

        for step in range(1, max_steps + 1):
            steps = step
            content, requested = parse_assistant(
                await self.llm.complete(context + turn, tools, tool_choice="auto"), step
            )
            if not requested:
                final = content or ""
                stop_reason = "answer"
                break
            if step == max_steps:
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
                        keep = {e["catalog_id"] for e in sent}
                        citations.extend(c for c in result.citations if c.get("catalog_id") in keep)
                        catalog_entries.extend(sent)
                        evidence.append(catalog_evidence(sent))
                elif result.has_data and name in GROUNDING_TOOLS:
                    grounded = True
                    citations.extend(result.citations)
                    evidence.append(result.data)
                    other_evidence.append(result.data)
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

        rejected = 0
        rewritten = 0
        if not grounded:
            reply = PROPOSAL_REPLY if proposals else NO_RECORD_REPLY
        elif stop_reason == "answer":
            if not (final and final.strip()):
                reply = EMPTY_REPLY
            else:
                rejected = len(unsupported_claims(final, evidence_text(evidence)))
                reply = NO_RECORD_REPLY if rejected else final
                if not rejected and catalog_entries:
                    homes = unqualified_home_locations(final, catalog_entries, evidence_text(other_evidence))
                    if homes:
                        rewritten = len(homes)
                        reply = usual_location_reply(homes)
                        keep = {e["catalog_id"] for e in homes}
                        citations = [c for c in citations if c.get("catalog_id") in keep]
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
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return result


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
