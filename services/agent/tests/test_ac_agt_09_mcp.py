"""AC-AGT-9: MCP server (services/agent/mcp_server.py, official ``mcp`` SDK, streamable HTTP) exposing the same read
tools plus ``propose_update``; no raw-SQL tool.

Interface: ``build_mcp_server(toolbox)`` -> ``mcp.server.mcpserver.MCPServer``; ``create_http_app(toolbox)`` -> ASGI
app serving streamable HTTP at ``/mcp``. Read tools are annotated readOnlyHint=true; propose_update is
readOnlyHint=false, destructiveHint=false. Tool failures come back as ``is_error`` results.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from mcp import Client
from mcp.server.mcpserver import MCPServer

import mcp_server
from tests.fakes import E_DRILL, MISSING_ID

READ = {"search_items", "get_item", "item_history", "where_is", "who_moved", "recent_changes", "list_unknowns",
        "inventory_stats", "search_observations", "recall_memory", "lookup_catalog"}


def result_text(res) -> str:
    parts = [json.dumps(res.structured_content, default=str)] if res.structured_content else []
    parts += [getattr(c, "text", "") for c in res.content]
    return "\n".join(parts)


@pytest.fixture()
def make_server(toolbox):
    """Factory (not a built server) so a missing implementation fails the test instead of erroring the fixture."""
    return lambda: mcp_server.build_mcp_server(toolbox)


def test_ac_agt_09_build_returns_official_sdk_server(make_server):
    assert isinstance(make_server(), MCPServer)


async def test_ac_agt_09_lists_read_tools_plus_propose_update(make_server):
    async with Client(make_server()) as c:
        tools = (await c.list_tools()).tools
    names = {t.name for t in tools}
    assert names == READ | {"propose_update"}


async def test_ac_agt_09_no_raw_sql_tool(make_server):
    async with Client(make_server()) as c:
        tools = (await c.list_tools()).tools
    for t in tools:
        assert "sql" not in t.name.lower() and "query" not in t.name.lower()
        assert "sql" not in json.dumps(t.input_schema).lower()


async def test_ac_agt_09_input_schemas_match_agent_tools(make_server, toolbox):
    agent = {s["function"]["name"]: s["function"]["parameters"] for s in toolbox.specs()}
    async with Client(make_server()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    for name, params in agent.items():
        assert set(tools[name].input_schema.get("required", [])) == set(params.get("required", [])), name


async def test_ac_agt_09_annotations_read_only_vs_propose(make_server):
    async with Client(make_server()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    for name in READ:
        assert tools[name].annotations is not None and tools[name].annotations.read_only_hint is True, name
    ann = tools["propose_update"].annotations
    assert ann is not None and ann.read_only_hint is False and ann.destructive_hint is False


async def test_ac_agt_09_call_where_is(make_server):
    async with Client(make_server()) as c:
        res = await c.call_tool("where_is", {"name": "drill"})
    assert not res.is_error
    text = result_text(res)
    assert E_DRILL in text and "LAB1S2" in text


async def test_ac_agt_09_results_carry_citations(make_server):
    async with Client(make_server()) as c:
        res = await c.call_tool("where_is", {"name": "drill"})
    assert "citations" in result_text(res)


async def test_ac_agt_09_propose_update_returns_proposal_and_never_patches(make_server, fake_inventory):
    async with Client(make_server()) as c:
        res = await c.call_tool("propose_update", {"entity_id": E_DRILL, "fields": {"last_location": "NOD2D4"}})
    assert not res.is_error
    text = result_text(res)
    assert "proposal_id" in text and "pending" in text and "NOD2D4" in text
    assert fake_inventory.methods <= {"GET"}


@pytest.mark.parametrize("name,args", [
    ("get_item", {"entity_id": "not-a-uuid"}),
    ("where_is", {"name": ""}),
    ("propose_update", {"entity_id": E_DRILL, "fields": {"status": "merged"}}),
])
async def test_ac_agt_09_invalid_arguments_are_tool_errors(make_server, fake_inventory, name, args):
    async with Client(make_server()) as c:
        res = await c.call_tool(name, args)
    assert res.is_error
    assert fake_inventory.methods <= {"GET"}


async def test_ac_agt_09_not_found_is_reported(make_server):
    async with Client(make_server()) as c:
        res = await c.call_tool("get_item", {"entity_id": MISSING_ID})
    assert res.is_error or "not_found" in result_text(res)


# ---------------------------------------------------------------- streamable HTTP transport
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve_http(toolbox):
    app = mcp_server.create_http_app(toolbox)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "MCP HTTP server did not start"
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        srv.should_exit = True
        t.join(timeout=10)


async def test_ac_agt_09_streamable_http_list_and_call(toolbox):
    with serve_http(toolbox) as http_url:
        await _list_and_call(http_url)


async def _list_and_call(http_url):
    async with Client(http_url) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == READ | {"propose_update"}
        res = await c.call_tool("where_is", {"name": "drill"})
    assert not res.is_error and "LAB1S2" in result_text(res)


def test_ac_agt_09_main_reads_port_from_env(monkeypatch):
    seen = {}

    def fake_run(app, host=None, port=None, **kw):
        seen.update(host=host, port=port, app=app)

    monkeypatch.setenv("MCP_PORT", "18090")
    monkeypatch.setenv("INVENTORY_API_URL", "http://inventory.test")
    monkeypatch.setenv("AGENT_LLM_URL", "http://llm.test/v1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused@127.0.0.1:1/unused")
    monkeypatch.setattr(uvicorn, "run", fake_run)
    mcp_server.main()
    assert seen["port"] == 18090 and seen["app"] is not None
