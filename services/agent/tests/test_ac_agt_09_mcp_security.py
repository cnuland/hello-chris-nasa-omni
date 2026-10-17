"""AC-AGT-9 exposure hardening + schema parity (review round 1).

- ``create_http_app(toolbox, allowed_hosts=[...])`` rejects a foreign Host header (DNS-rebinding protection).
- ``create_http_app(toolbox, auth_token=...)`` requires ``Authorization: Bearer <token>`` (env ``MCP_AUTH_TOKEN``).
- ``main()`` defaults to 127.0.0.1 and refuses a non-loopback ``MCP_HOST`` without ``MCP_ALLOWED_HOSTS``.
- MCP input schemas match the agent tool schemas in property names, required set and per-property JSON types.
"""
from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from mcp import Client

import mcp_server

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(app):
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        t.join(timeout=10)


def test_ac_agt_09_allowed_hosts_rejects_foreign_host(toolbox):
    app = mcp_server.create_http_app(toolbox, allowed_hosts=["agent-mcp:8090"])
    with serve(app) as base:
        bad = httpx.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "host": "evil.example"})
        good = httpx.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "host": "agent-mcp:8090"})
    assert bad.status_code == 421
    assert good.status_code == 200, good.text


def test_ac_agt_09_bearer_token_required_when_configured(toolbox):
    app = mcp_server.create_http_app(toolbox, auth_token="s3cret-test")
    with serve(app) as base:
        none = httpx.post(f"{base}/mcp", json=INIT, headers=HEADERS)
        wrong = httpx.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer nope"})
        ok = httpx.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer s3cret-test"})
    assert none.status_code == 401 and wrong.status_code == 401
    assert ok.status_code == 200, ok.text


@pytest.fixture()
def mcp_env(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_URL", "http://inventory.test")
    monkeypatch.setenv("AGENT_LLM_URL", "http://llm.test/v1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused@127.0.0.1:1/unused")
    for k in ("MCP_HOST", "MCP_ALLOWED_HOSTS", "MCP_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host=None, port=None, **kw: seen.update(host=host, port=port))
    return seen


def test_ac_agt_09_main_defaults_to_loopback(mcp_env):
    mcp_server.main()
    assert mcp_env["host"] == "127.0.0.1"


def test_ac_agt_09_main_refuses_public_bind_without_allowed_hosts(mcp_env, monkeypatch):
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    with pytest.raises(SystemExit):
        mcp_server.main()
    assert "host" not in mcp_env


def test_ac_agt_09_main_public_bind_with_allowed_hosts(mcp_env, monkeypatch):
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "agent-mcp.nasa-app.svc:8090")
    mcp_server.main()
    assert mcp_env["host"] == "0.0.0.0"


def _types(prop: dict) -> set[str]:
    """JSON types a property accepts (anyOf/type, ignoring 'null', $ref -> 'object')."""
    out: set[str] = set()
    for alt in prop.get("anyOf", [prop]):
        if "$ref" in alt:
            out.add("object")
        t = alt.get("type")
        if isinstance(t, list):
            out.update(t)
        elif t:
            out.add(t)
        if "enum" in alt and not t:
            out.add("string")
    out.discard("null")
    return out


async def test_ac_agt_09_input_schema_properties_and_types_match(toolbox):
    agent = {s["function"]["name"]: s["function"]["parameters"] for s in toolbox.specs()}
    async with Client(mcp_server.build_mcp_server(toolbox)) as c:
        tools = {t.name: t.input_schema for t in (await c.list_tools()).tools}
    for name, params in agent.items():
        mcp_props = tools[name].get("properties", {})
        assert set(mcp_props) == set(params.get("properties", {})), name
        for prop, schema in params.get("properties", {}).items():
            assert _types(mcp_props[prop]) == _types(schema), (name, prop, mcp_props[prop], schema)
