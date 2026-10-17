"""FastAPI app: /healthz, /chat, /chat/stream (SSE), /memory/dream. Run with
``uvicorn --factory nasa_agent.main:create_app``.

Streaming design: the reply is only known after the whole tool loop and the grounding check (AC-AGT-4), so
``/chat/stream`` sends SSE comment keepalives (``: keepalive``) every ``AGENT_STREAM_KEEPALIVE_S`` seconds while the loop
runs (keeps proxies/routes from idling the connection out), then the checked reply as ``token`` events and ``done``.
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
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from nasa_agent.config import Settings
from nasa_agent.dreaming import Dreamer, dream_forever
from nasa_agent.harness import AgentHarness
from nasa_agent.inventory_block import InventoryBlockRenderer
from nasa_agent.inventory_client import InventoryClient
from nasa_agent.llm import LLMError, OpenAICompatLLM
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


# ---------------------------------------------------------------- app factory
def create_app(
    settings: Settings | None = None, *, llm=None, inventory=None, memory=None, inventory_block=None, dreamer=None
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

    async def _run(body: ChatRequest, request_id: str) -> dict[str, Any]:
        messages = [m.model_dump() for m in body.messages]
        return await harness.run(messages, session_id=body.session_id, request_id=request_id)

    @app.post("/chat")
    async def chat(body: ChatRequest) -> JSONResponse:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        headers = {"X-Request-ID": request_id}
        try:
            result = await _run(body, request_id)
        except LLMError as exc:
            log.warning("llm failure", extra={"request_id": request_id, "error": str(exc), "latency_ms": _ms(started)})
            return JSONResponse(
                {"detail": f"agent LLM unavailable: {exc}", "request_id": request_id}, status_code=502, headers=headers
            )
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

        async def events() -> AsyncIterator[str]:
            started = time.perf_counter()
            task = asyncio.create_task(_run(body, request_id))
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=settings.stream_keepalive_seconds)
                    if done:
                        break
                    yield ": keepalive\n\n"
                result = task.result()
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the client as an SSE error event
                kind = "llm" if isinstance(exc, LLMError) else "internal"
                log.warning(
                    "stream failure",
                    extra={"request_id": request_id, "error": type(exc).__name__, "latency_ms": _ms(started)},
                )
                yield sse("error", {"detail": f"{kind} failure", "request_id": request_id})
                return
            for chunk in split_tokens(result["reply"]):
                yield sse("token", {"text": chunk})
            yield sse("done", result)
            log.info("chat stream", extra={"request_id": request_id, "latency_ms": _ms(started), "status": 200})

        return StreamingResponse(
            events(),
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
