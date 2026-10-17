"""MCP server (AC-AGT-9): the agent's read tools + propose_update over streamable HTTP (official ``mcp`` SDK).

No raw-SQL tool (research/05). Each MCP tool is generated from the agent's pydantic argument model, validated and
executed by the same :class:`nasa_agent.tools.Toolbox` the chat harness uses, so behaviour and schemas stay in sync.
Run: ``python mcp_server.py`` (MCP_PORT, default 8090; MCP_HOST, default 127.0.0.1; path /mcp).
``MCP_ALLOWED_HOSTS`` (comma-separated Host header values, e.g. ``agent-mcp.nasa-app.svc:8090``) turns on DNS
rebinding protection and is required for any non-loopback ``MCP_HOST`` (the server refuses to start otherwise).
``MCP_AUTH_TOKEN`` (optional) requires ``Authorization: Bearer <token>`` on every request.
"""

from __future__ import annotations

import hmac
import inspect
import ipaddress
import logging
import os
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nasa_agent.logs import configure_logging

log = logging.getLogger("nasa_agent.mcp")

INSTRUCTIONS = (
    "NASA ISS inventory tools. Read tools query inventory-api (Postgres is the source of truth). propose_update only "
    "returns a pending proposal; an operator confirms it in the dashboard. Describe crew by role/appearance only."
)


def _to_plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return str(value)


def _make_tool_fn(toolbox, name: str):
    """Build an async function whose signature mirrors the tool's pydantic model (so the MCP input schema has the
    same properties and required arguments) and which delegates to ``toolbox.call``."""
    model = toolbox.argument_model(name)
    params = []
    for field_name, info in model.model_fields.items():
        default = inspect.Parameter.empty if info.is_required() else None
        params.append(
            inspect.Parameter(field_name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=info.annotation)
        )

    async def tool_fn(**kwargs: Any) -> dict[str, Any]:
        args = {k: _to_plain(v) for k, v in kwargs.items() if v is not None}
        started = time.perf_counter()
        result = await toolbox.call(name, args)
        log.info(
            "mcp tool call",
            extra={
                "tool": name,
                "ok": result.ok,
                "error_kind": result.error_kind,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        if not result.ok:
            raise ToolError(f"{result.error_kind}: {result.error}")
        return {"data": result.data, "citations": result.citations, "has_data": result.has_data}

    tool_fn.__name__ = name
    tool_fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    return tool_fn


def build_mcp_server(toolbox) -> MCPServer:
    """Return an ``mcp.server.mcpserver.MCPServer`` exposing ``toolbox``'s tools."""
    server = MCPServer(name="nasa-inventory", instructions=INSTRUCTIONS, version="0.1.0")
    for name in toolbox.names():
        read_only = toolbox.is_read_only(name)
        annotations = ToolAnnotations(
            read_only_hint=read_only, destructive_hint=False, idempotent_hint=read_only, open_world_hint=False
        )
        server.add_tool(
            _make_tool_fn(toolbox, name), name=name, description=toolbox.description(name), annotations=annotations
        )
    return server


class BearerAuthMiddleware:
    """Pure ASGI middleware: HTTP requests need ``Authorization: Bearer <token>`` (lifespan passes through)."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw = dict(scope.get("headers") or []).get(b"authorization", b"")
            scheme, _, value = raw.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(value.strip(), self.token):
                log.warning("mcp request unauthorized", extra={"path": scope.get("path")})
                response = JSONResponse(
                    {"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_http_app(
    toolbox, *, host: str = "127.0.0.1", allowed_hosts: list[str] | None = None, auth_token: str | None = None
) -> Starlette:
    """Starlette ASGI app serving the MCP server over streamable HTTP at /mcp (optionally bearer-protected)."""
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"http://{h}" for h in allowed_hosts],
        )
    app = build_mcp_server(toolbox).streamable_http_app(
        streamable_http_path="/mcp", transport_security=security, host=host
    )
    if auth_token:
        app.add_middleware(BearerAuthMiddleware, token=auth_token)
    return app


def main() -> None:
    import uvicorn

    from nasa_agent.config import Settings
    from nasa_agent.inventory_client import InventoryClient
    from nasa_agent.memory import MemoryStore
    from nasa_agent.tools import Toolbox

    configure_logging()
    settings = Settings.from_env()
    port = int(os.environ.get("MCP_PORT", "8090"))
    host = os.environ.get("MCP_HOST", "").strip() or "127.0.0.1"
    allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not _is_loopback(host) and not allowed:
        log.error("refusing to bind a non-loopback MCP_HOST without MCP_ALLOWED_HOSTS", extra={"host": host})
        raise SystemExit("MCP_HOST is not loopback: set MCP_ALLOWED_HOSTS (DNS-rebinding protection) to serve on it")
    toolbox = Toolbox(
        InventoryClient(settings.inventory_api_url, timeout=settings.http_timeout_s), MemoryStore(settings.database_url)
    )
    app = create_http_app(
        toolbox,
        host=host,
        allowed_hosts=allowed or None,
        auth_token=os.environ.get("MCP_AUTH_TOKEN", "").strip() or None,
    )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
