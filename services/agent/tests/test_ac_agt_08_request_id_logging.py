"""AC-AGT-8: each chat request gets a uuid; logs record tool calls and latency, not raw images.

Logging contract (decided here): loggers under ``nasa_agent``; one record per tool call with extras
``request_id``, ``tool``, ``ok``, ``latency_ms``; one record per chat request with extras ``request_id``,
``latency_ms`` (and no ``tool``). The response carries ``request_id`` and the ``X-Request-ID`` header.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from nasa_agent.main import create_app
from tests.conftest import user
from tests.fakes import E_DRILL, FakeInventoryBlock, FakeLLM, assistant_text, assistant_tools, tool_call

B64_IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg" * 20
DATA_URI = f"data:image/png;base64,{B64_IMAGE}"


def grounded():
    return FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})), assistant_text("LAB1S2.")])


def agent_records(caplog):
    return [r for r in caplog.records if r.name.startswith("nasa_agent")]


def everything_logged(caplog) -> str:
    return "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)


@pytest.fixture()
def client_for(plain_settings, inventory, fake_memory):
    made = []

    def _make(llm):
        c = TestClient(create_app(plain_settings, llm=llm, inventory=inventory, memory=fake_memory,
                                  inventory_block=FakeInventoryBlock(), dreamer=None), raise_server_exceptions=False)
        c.__enter__()
        made.append(c)
        return c

    yield _make
    for c in made:
        c.__exit__(None, None, None)


def test_ac_agt_08_chat_response_has_uuid_request_id_and_header(client_for):
    c = client_for(FakeLLM([assistant_text("a"), assistant_text("b")]))
    r1 = c.post("/chat", json={"messages": user("hi")})
    r2 = c.post("/chat", json={"messages": user("hi")})
    id1, id2 = r1.json()["request_id"], r2.json()["request_id"]
    assert uuid.UUID(id1).version == 4 and uuid.UUID(id2).version == 4
    assert id1 != id2
    assert r1.headers["x-request-id"] == id1


async def test_ac_agt_08_harness_generates_or_keeps_request_id(make_harness):
    res = await make_harness(FakeLLM([assistant_text("a")])).run(user("hi"))
    assert uuid.UUID(res["request_id"])
    given = str(uuid.uuid4())
    res2 = await make_harness(FakeLLM([assistant_text("a")])).run(user("hi"), request_id=given)
    assert res2["request_id"] == given


async def test_ac_agt_08_tool_calls_logged_with_request_id_and_latency(make_harness, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent")
    rid = str(uuid.uuid4())
    await make_harness(grounded()).run(user("where is the drill?"), request_id=rid)
    tool_recs = [r for r in agent_records(caplog) if getattr(r, "tool", None) == "where_is"]
    assert tool_recs, "one log record per tool call"
    rec = tool_recs[0]
    assert rec.request_id == rid
    assert isinstance(rec.latency_ms, (int, float)) and rec.latency_ms >= 0
    assert rec.ok is True


async def test_ac_agt_08_request_summary_logged_with_latency(make_harness, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent")
    rid = str(uuid.uuid4())
    await make_harness(grounded()).run(user("where is the drill?"), request_id=rid)
    summary = [r for r in agent_records(caplog)
               if getattr(r, "request_id", None) == rid and getattr(r, "tool", None) is None
               and isinstance(getattr(r, "latency_ms", None), (int, float))]
    assert summary, "a per-request record with latency_ms is required"


async def test_ac_agt_08_failed_tool_calls_are_logged_too(make_harness, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent")
    llm = FakeLLM([assistant_tools(tool_call("get_item", {"entity_id": "nope"})), assistant_text("x")])
    await make_harness(llm).run(user("q"))
    recs = [r for r in agent_records(caplog) if getattr(r, "tool", None) == "get_item"]
    assert recs and recs[0].ok is False


async def test_ac_agt_08_raw_images_never_logged(make_harness, fake_inventory, caplog):
    caplog.set_level(logging.DEBUG)
    for item in fake_inventory.items:
        if item["entity_id"] == E_DRILL:
            item["snapshot_url"] = DATA_URI
    history = [{"role": "user", "content": f"what is this? {DATA_URI}"}]
    llm = FakeLLM([assistant_tools(tool_call("get_item", {"entity_id": E_DRILL})), assistant_text("A drill.")])
    await make_harness(llm).run(history)
    assert agent_records(caplog), "the harness must log something"
    assert B64_IMAGE[:60] not in everything_logged(caplog)


def test_ac_agt_08_http_request_logs_request_id(client_for, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent")
    c = client_for(grounded())
    rid = c.post("/chat", json={"messages": user("where is the drill?")}).json()["request_id"]
    assert any(getattr(r, "request_id", None) == rid for r in agent_records(caplog))
