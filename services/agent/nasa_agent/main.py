"""FastAPI app: /healthz, /chat, /chat/stream (SSE), /memory/dream. Run with
``uvicorn --factory nasa_agent.main:create_app``.

Streaming design (SSE contract: docs/spec/30-agent.md AC-AGT-16): while the tool loop runs ``/chat/stream`` sends
``status`` events (phase llm/tool/checking) and SSE comment keepalives (``: keepalive``) every
``AGENT_STREAM_KEEPALIVE_S`` seconds. The reply is only known after the grounding check (AC-AGT-4), so it is then sent
as word-chunked ``token`` events and ``done``; failures are one ``error`` event ``{detail, kind, retryable,
request_id}``. A client disconnect cancels the loop task and with it the in-flight LLM request.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from nasa_agent.config import Settings
from nasa_agent.dreaming import Dreamer, dream_forever
from nasa_agent.harness import LLM_DOWN_DETAIL, TIMEOUT_DETAIL, AgentHarness
from nasa_agent.inventory_block import InventoryBlockRenderer
from nasa_agent.inventory_client import InventoryClient
from nasa_agent.llm import LLMError, LLMTimeout, OpenAICompatLLM
from nasa_agent.logs import configure_logging
from nasa_agent.memory import MemoryStore
from nasa_agent.tools import Toolbox

log = logging.getLogger("nasa_agent.api")

#: Input caps (the context builder never cuts the latest user message, so it must be bounded here).
MAX_MESSAGE_CHARS = 8000
MAX_REQUEST_CHARS = 100_000


# ---------------------------------------------------------------- request models
class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"]
    content: StrictStr = Field(..., max_length=MAX_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)
    session_id: str | None = Field(None, max_length=128)

    @field_validator("messages")
    @classmethod
    def _last_is_user(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("the last message must come from the user")
        if sum(len(m.content) for m in messages) > MAX_REQUEST_CHARS:
            raise ValueError(f"the conversation exceeds {MAX_REQUEST_CHARS} characters; start a new session")
        return messages


class DreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: float | None = Field(None, gt=0, le=24 * 365 * 50)


# ---------------------------------------------------------------- SSE helpers
_TOKEN_RE = re.compile(r"\S+\s*|\s+")


def split_tokens(text: str) -> list[str]:
    """Split a reply into word-level chunks that concatenate back to ``text``."""
    chunks = _TOKEN_RE.findall(text)
    if len(chunks) == 1 and len(text) > 1:
        mid = len(text) // 2
        chunks = [text[:mid], text[mid:]]
    return chunks or [text]


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def error_body(exc: BaseException, request_id: str, deadline_s: float) -> dict[str, Any]:
    """``{detail, kind, retryable, request_id}`` for /chat errors and the SSE ``error`` event (AC-AGT-15/16)."""
    if isinstance(exc, LLMTimeout):
        detail = str(exc) if str(exc).startswith("The language model") else TIMEOUT_DETAIL.format(seconds=deadline_s)
        return {"detail": detail, "kind": "timeout", "retryable": True, "request_id": request_id}
    if isinstance(exc, LLMError):
        return {"detail": LLM_DOWN_DETAIL, "kind": "llm", "retryable": True, "request_id": request_id}
    return {"detail": "Internal error in the agent. Please retry; if it persists, report the request id.",
            "kind": "internal", "retryable": True, "request_id": request_id}


async def stream_chat_events(
    run: Callable[[Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[dict[str, Any]]],
    *,
    keepalive_s: float,
    request_id: str,
    deadline_s: float = 45.0,
) -> AsyncIterator[str]:
    """SSE frames for one chat turn. ``run(on_event)`` runs the harness. Status events are forwarded as they happen;
    closing or cancelling this generator (client disconnect) cancels the run task and waits for it, so the in-flight
    LLM HTTP request is closed and no further tool or LLM call starts."""
    started = time.perf_counter()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_event(event: dict[str, Any]) -> None:
        queue.put_nowait(event)

    task = asyncio.create_task(run(on_event))
    getter: asyncio.Future | None = None
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({task, getter}, timeout=keepalive_s, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield sse("status", getter.result())
                continue
            getter.cancel()
            if task in done:
                break
            yield ": keepalive\n\n"
        while not queue.empty():
            yield sse("status", queue.get_nowait())
        try:
            result = task.result()
        except Exception as exc:  # noqa: BLE001 - reported to the client as an SSE error event
            log.warning(
                "stream failure",
                extra={"request_id": request_id, "error": type(exc).__name__, "latency_ms": _ms(started)},
            )
            yield sse("error", error_body(exc, request_id, deadline_s))
            return
        for chunk in split_tokens(result["reply"]):
            yield sse("token", {"text": chunk})
        yield sse("done", result)
        log.info("chat stream", extra={"request_id": request_id, "latency_ms": _ms(started), "status": 200})
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            log.info("chat stream cancelled", extra={"request_id": request_id, "latency_ms": _ms(started)})


# ---------------------------------------------------------------- app factory
def create_app(
    settings: Settings | None = None,
    *,
    llm=None,
    inventory=None,
    memory=None,
    inventory_block=None,
    dreamer=None,
    llm_transport=None,
) -> FastAPI:
    """Build the agent API. Every dependency is injectable (tests); defaults come from ``Settings.from_env``."""
    settings = settings or Settings.from_env()
    configure_logging()
    owns_inventory = inventory is None
    inventory = inventory or InventoryClient(settings.inventory_api_url, timeout=settings.http_timeout_s)
    llm = llm or OpenAICompatLLM(
        settings.agent_llm_url,
        settings.agent_llm_model,
        api_key=settings.agent_llm_api_key,
        timeout=settings.http_timeout_s,
        max_tokens=settings.max_output_tokens,
        transport=llm_transport,
    )
    memory = memory if memory is not None else MemoryStore(settings.database_url)
    inventory_block = inventory_block or InventoryBlockRenderer(
        settings.database_url, recent_hours=settings.inventory_recent_hours
    )
    toolbox = Toolbox(inventory, memory)
    harness = AgentHarness(llm, toolbox, settings=settings, memory=memory, inventory_block=inventory_block)
    state: dict[str, Any] = {"dreamer": dreamer}

    def get_dreamer() -> Dreamer:
        if state["dreamer"] is None:
            state["dreamer"] = Dreamer(settings.database_url, token_budget=settings.dream_token_budget)
        return state["dreamer"]

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = None
        if settings.dream_in_process:
            task = asyncio.create_task(
                dream_forever(
                    get_dreamer(),
                    interval_s=settings.dream_interval_seconds,
                    hours=settings.dream_window_hours,
                    stop=stop,
                )
            )
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            if owns_inventory:
                await inventory.aclose()

    app = FastAPI(title="NASA Omni agent", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def _run(body: ChatRequest, request_id: str, on_event=None) -> dict[str, Any]:
        messages = [m.model_dump() for m in body.messages]
        return await harness.run(messages, session_id=body.session_id, request_id=request_id, on_event=on_event)

    @app.post("/chat")
    async def chat(body: ChatRequest) -> JSONResponse:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        headers = {"X-Request-ID": request_id}
        try:
            result = await _run(body, request_id)
        except LLMError as exc:
            log.warning(
                "llm failure",
                extra={"request_id": request_id, "error": type(exc).__name__, "latency_ms": _ms(started)},
            )
            status = 504 if isinstance(exc, LLMTimeout) else 502
            return JSONResponse(error_body(exc, request_id, settings.turn_deadline_s), status_code=status,
                                headers=headers)
        except Exception as exc:  # never a bare 500 without a request id
            log.exception(
                "chat failure",
                extra={"request_id": request_id, "error": type(exc).__name__, "latency_ms": _ms(started)},
            )
            return JSONResponse(
                {"detail": "internal error", "request_id": request_id}, status_code=500, headers=headers
            )
        log.info("chat request", extra={"request_id": request_id, "latency_ms": _ms(started), "status": 200})
        return JSONResponse(result, headers=headers)

    @app.post("/chat/stream")
    async def chat_stream(body: ChatRequest) -> StreamingResponse:
        request_id = str(uuid.uuid4())

        return StreamingResponse(
            stream_chat_events(
                lambda on_event: _run(body, request_id, on_event),
                keepalive_s=settings.stream_keepalive_seconds,
                request_id=request_id,
                deadline_s=settings.turn_deadline_s,
            ),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/memory/dream")
    async def dream(
        body: Annotated[DreamRequest | None, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        request_id = str(uuid.uuid4())
        headers = {"X-Request-ID": request_id}
        if settings.admin_token and not _bearer_ok(authorization, settings.admin_token):
            log.warning("dream request unauthorized", extra={"request_id": request_id})
            return JSONResponse(
                {"detail": "unauthorized", "request_id": request_id},
                status_code=401,
                headers={**headers, "WWW-Authenticate": "Bearer"},
            )
        hours = body.hours if body is not None and body.hours is not None else settings.dream_window_hours
        started = time.perf_counter()
        try:
            report = await asyncio.to_thread(get_dreamer().run, hours=hours)
        except psycopg.OperationalError as exc:
            log.warning("dream db unavailable", extra={"request_id": request_id, "error": type(exc).__name__})
            return JSONResponse(
                {"detail": "database unavailable", "request_id": request_id}, status_code=503, headers=headers
            )
        log.info("dream request", extra={"request_id": request_id, "latency_ms": _ms(started), "hours": hours})
        return JSONResponse({**report, "hours": hours, "request_id": request_id}, headers=headers)

    return app


def _bearer_ok(authorization: str | None, token: str) -> bool:
    scheme, _, value = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), token.encode())


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
