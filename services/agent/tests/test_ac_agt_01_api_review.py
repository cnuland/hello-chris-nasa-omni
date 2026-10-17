"""AC-AGT-1 / AC-AGT-6 / AC-AGT-10 API details from review round 1.

- ``/chat/stream`` sends SSE comment keepalives (``: keepalive``) while the tool loop runs; tokens are only emitted
  after the grounding check (documented design).
- ``POST /memory/dream`` requires ``Authorization: Bearer <AGENT_ADMIN_TOKEN>`` when that variable is set, and maps a
  database outage to 503 with a request_id.
- Production wiring: ``create_app`` with the real ``InventoryBlockRenderer`` and ``MemoryStore`` against Postgres puts
  the SQL inventory block and the dreamed global summary into the chat context of one turn.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import psycopg
from fastapi.testclient import TestClient

from nasa_agent.config import Settings
from nasa_agent.context import DATA_OPEN, SYSTEM_PROMPT
from nasa_agent.dreaming import Dreamer
from nasa_agent.main import create_app
from tests.conftest import make_entity, make_review, ts, user
from tests.fakes import FakeInventoryBlock, FakeLLM, FakeMemory, assistant_text


class SlowLLM(FakeLLM):
    async def complete(self, messages, tools, tool_choice="auto"):
        await asyncio.sleep(0.3)
        return await super().complete(messages, tools, tool_choice)


def test_ac_agt_01_stream_sends_keepalives_while_loop_runs(plain_settings, inventory, fake_memory):
    settings = replace(plain_settings, stream_keepalive_seconds=0.05)
    app = create_app(settings, llm=SlowLLM([assistant_text("hello")]), inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        text = c.post("/chat/stream", json={"messages": user("hi")}).text
    assert ": keepalive" in text
    assert text.index(": keepalive") < text.index("event: token")


def test_ac_agt_01_settings_keepalive_and_admin_token_from_env():
    env = {"AGENT_LLM_URL": "u", "INVENTORY_API_URL": "i", "DATABASE_URL": "d",
           "AGENT_STREAM_KEEPALIVE_S": "2.5", "AGENT_ADMIN_TOKEN": " tok ", "AGENT_TOOL_RESULT_TOKEN_BUDGET": "900"}
    s = Settings.from_env(env)
    assert s.stream_keepalive_seconds == 2.5 and s.admin_token == "tok" and s.tool_result_token_budget == 900
    d = Settings.from_env({"AGENT_LLM_URL": "u", "INVENTORY_API_URL": "i", "DATABASE_URL": "d"})
    assert d.admin_token is None


class _Dreamer:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = 0

    def run(self, hours=6.0, now=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return {"entities": 0, "locations": 0, "global": 1}


def test_ac_agt_06_dream_endpoint_requires_admin_token_when_set(plain_settings, inventory):
    dreamer = _Dreamer()
    app = create_app(replace(plain_settings, admin_token="adm-test"), llm=FakeLLM([]), inventory=inventory,
                     memory=FakeMemory(), inventory_block=FakeInventoryBlock(), dreamer=dreamer)
    with TestClient(app) as c:
        assert c.post("/memory/dream").status_code == 401
        assert c.post("/memory/dream", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert dreamer.calls == 0
        assert c.post("/memory/dream", headers={"authorization": "Bearer adm-test"}).status_code == 200
    assert dreamer.calls == 1


def test_ac_agt_06_dream_endpoint_db_down_is_503(plain_settings, inventory):
    app = create_app(plain_settings, llm=FakeLLM([]), inventory=inventory, memory=FakeMemory(),
                     inventory_block=FakeInventoryBlock(), dreamer=_Dreamer(psycopg.OperationalError("down")))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/memory/dream")
    assert r.status_code == 503
    assert r.json()["request_id"] == r.headers["x-request-id"]


def test_ac_agt_10_production_wiring_block_and_memory_from_postgres(settings, inventory, database_url, db):
    pouch = make_entity(db, label="unknown white pouch", status="unknown", last_location="NOD2O4",
                        last_seen_at=ts(30), needs_review=True)
    make_review(db, entity_id=pouch, description="white pouch with velcro", location_id="NOD2O4", observed_at=ts(30))
    Dreamer(database_url).run(hours=6, now=ts(60))
    llm = FakeLLM([assistant_text("hi")])
    app = create_app(settings, llm=llm, inventory=inventory, dreamer=None)  # real renderer + MemoryStore
    with TestClient(app) as c:
        r = c.post("/chat", json={"messages": user("what is unknown in Node 2?")})
    assert r.status_code == 200, r.text
    sent = llm.requests[0]["messages"]
    assert sent[0] == {"role": "system", "content": SYSTEM_PROMPT}
    data = sent[1]["content"]
    assert DATA_OPEN in data
    assert "INVENTORY WORKING SET" in data and pouch in data, "SQL inventory block from Postgres"
    assert "MEMORY SUMMARY" in data and "live entities" in data, "dreamed global summary from agent_memory"
