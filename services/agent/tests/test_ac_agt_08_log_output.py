"""AC-AGT-8 logging, as actually written to the process output (review round 1).

caplog captures LogRecord objects and hides whether a handler exists. These tests go through the handler that
``nasa_agent.logs.configure_logging`` installs (and that ``create_app``, the MCP server and the dreaming CLI call): one
JSON object per line with message, level, logger and the structured extras (request_id, tool, ok, latency_ms, ...).
"""
from __future__ import annotations

import io
import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from nasa_agent.logs import HANDLER_NAME, configure_logging
from nasa_agent.main import create_app
from tests.conftest import user
from tests.fakes import FakeInventoryBlock, FakeLLM, assistant_text, assistant_tools, tool_call


def lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(raw) for raw in buf.getvalue().splitlines() if raw.strip()]


@pytest.fixture(autouse=True)
def _restore_logger():
    logger = logging.getLogger("nasa_agent")
    before = (list(logger.handlers), logger.level)
    yield
    logger.handlers[:] = before[0]
    logger.setLevel(before[1])


def grounded():
    return FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})), assistant_text("LAB1S2.")])


async def test_ac_agt_08_formatted_output_has_request_id_tool_and_latency(make_harness):
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    rid = str(uuid.uuid4())
    await make_harness(grounded()).run(user("where is the drill?"), request_id=rid)
    out = lines(buf)
    tool = [r for r in out if r.get("tool") == "where_is"]
    assert tool, buf.getvalue()
    assert tool[0]["request_id"] == rid and tool[0]["ok"] is True and isinstance(tool[0]["latency_ms"], (int, float))
    turn = [r for r in out if r["message"] == "chat turn"]
    assert turn and turn[0]["request_id"] == rid and "latency_ms" in turn[0] and turn[0]["level"] == "INFO"


def test_ac_agt_08_configure_logging_is_idempotent():
    configure_logging("INFO", stream=io.StringIO())
    configure_logging("INFO", stream=io.StringIO())
    named = [h for h in logging.getLogger("nasa_agent").handlers if h.get_name() == HANDLER_NAME]
    assert len(named) == 1


def test_ac_agt_08_log_level_from_env(monkeypatch, make_harness):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    buf = io.StringIO()
    configure_logging(stream=buf)
    logging.getLogger("nasa_agent.harness").info("tool call", extra={"request_id": "x"})
    logging.getLogger("nasa_agent.harness").warning("bad thing", extra={"request_id": "y"})
    out = lines(buf)
    assert [r["message"] for r in out] == ["bad thing"]


def test_ac_agt_08_create_app_installs_handler_and_chat_is_logged(plain_settings, inventory, fake_memory, capsys):
    app = create_app(plain_settings, llm=grounded(), inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        rid = c.post("/chat", json={"messages": user("where is the drill?")}).json()["request_id"]
    err = capsys.readouterr().err
    recs = [json.loads(raw) for raw in err.splitlines() if raw.startswith("{")]
    mine = [r for r in recs if r.get("request_id") == rid]
    assert any(r.get("tool") == "where_is" and "latency_ms" in r for r in mine), err
    assert any(r["message"] == "chat request" and "latency_ms" in r for r in mine), err


def test_ac_agt_08_formatter_never_writes_message_content(make_harness):
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    logging.getLogger("nasa_agent.harness").info("tool call", extra={"request_id": "r", "tool": "t", "ok": True})
    assert "where is the drill" not in buf.getvalue()


def test_ac_agt_08_mcp_and_dreaming_entrypoints_configure_logging(monkeypatch, database_url):
    import uvicorn

    import mcp_server
    from nasa_agent import dreaming

    logger = logging.getLogger("nasa_agent")
    logger.handlers[:] = [h for h in logger.handlers if h.get_name() != HANDLER_NAME]
    monkeypatch.setenv("INVENTORY_API_URL", "http://inventory.test")
    monkeypatch.setenv("AGENT_LLM_URL", "http://llm.test/v1")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    mcp_server.main()
    assert any(h.get_name() == HANDLER_NAME for h in logger.handlers)
    logger.handlers[:] = [h for h in logger.handlers if h.get_name() != HANDLER_NAME]
    assert dreaming.main(["--once", "--hours", "1"]) == 0
    assert any(h.get_name() == HANDLER_NAME for h in logger.handlers)
