"""Test infrastructure for the agent.

- Postgres: one fresh ``test_agt_<hex>`` database per session, built from contracts/db/*.sql and dropped at the end
  (admin URL overridable with TEST_PG_ADMIN_URL). The ``inventory`` database is never touched. Each DB test starts
  from truncated tables + re-applied 002_seed.sql.
- inventory-api is faked in-memory (tests/fakes.py FakeInventory via httpx.MockTransport).
- The LLM is a scripted fake (FakeLLM implements the harness protocol; FakeLLMServer fakes the OpenAI HTTP API).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from nasa_agent.config import Settings
from tests.fakes import INVENTORY_URL, LLM_URL, FakeInventory, FakeMemory

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DB = REPO_ROOT / "contracts" / "db"
ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres:test@localhost:55432/postgres")
DB_PREFIX = "test_agt_"

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)

DATA_TABLES = [
    "agent_memory", "decision_log", "audit_log", "review_item", "event", "sighting", "observation_window",
    "stock", "entity_exemplar", "entity", "catalog_item", "camera", "location",
]


def ts(minutes: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _url_for(dbname: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def database_url():
    dbname = f"{DB_PREFIX}{uuid.uuid4().hex[:12]}"
    assert dbname.startswith(DB_PREFIX) and dbname != "inventory"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    url = _url_for(dbname)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for sql_file in sorted(CONTRACTS_DB.glob("*.sql")):
                conn.execute(sql_file.read_text())
        yield url
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


@pytest.fixture()
def db(database_url):
    """Autocommit connection to a freshly reset (truncated + reseeded) test database."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE " + ", ".join(DATA_TABLES) + " RESTART IDENTITY CASCADE")
        conn.execute((CONTRACTS_DB / "002_seed.sql").read_text())
        yield conn


@pytest.fixture()
def settings(database_url):
    return Settings(agent_llm_url=LLM_URL, inventory_api_url=INVENTORY_URL, database_url=database_url)


@pytest.fixture()
def fake_inventory():
    return FakeInventory()


@pytest.fixture()
def inventory(fake_inventory):
    from nasa_agent.inventory_client import InventoryClient

    return InventoryClient(INVENTORY_URL, transport=fake_inventory.transport)


@pytest.fixture()
def fake_memory():
    return FakeMemory(
        rows={("entity", "11111111-1111-4111-8111-111111111111"): "Drill is usually kept in LAB1S2."},
        global_summary="GLOBAL-MEMORY: 5 live entities, 1 open unknown in Node 2.",
    )


@pytest.fixture()
def toolbox(inventory, fake_memory):
    from nasa_agent.tools import Toolbox

    return Toolbox(inventory, fake_memory)


# ---------------------------------------------------------------- DB factories (direct SQL, test-only)
def make_entity(db, *, label="thing", kind="object", status="confirmed", catalog_id=None, description=None,
                last_location=None, last_seen_at=None, confidence=0.9, needs_review=False, merged_into=None,
                attributes=None) -> str:
    row = db.execute(
        """INSERT INTO entity (catalog_id, kind, status, merged_into, label, description, attributes, last_location,
                               last_seen_at, first_seen_at, confidence, needs_review)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING entity_id""",
        (catalog_id, kind, status, merged_into, label, description, json.dumps(attributes or {}), last_location,
         last_seen_at, last_seen_at, confidence, needs_review),
    ).fetchone()
    return str(row[0])


def make_window(db, *, camera_id="cam-lab1-fwd", t_start=None, t_end=None, scene_summary=None, transcript=None,
                model="qwen2.5-omni-7b@test") -> str:
    t_start = t_start or T0
    t_end = t_end or (t_start + timedelta(seconds=5))
    row = db.execute(
        """INSERT INTO observation_window (camera_id, t_start, t_end, scene_summary, transcript, model)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING window_id""",
        (camera_id, t_start, t_end, scene_summary, transcript, model),
    ).fetchone()
    return str(row[0])


def make_sighting(db, entity_id, *, seen_at, window_id=None, location_id=None, confidence=0.8,
                  description=None) -> int:
    row = db.execute(
        """INSERT INTO sighting (entity_id, window_id, location_id, seen_at, confidence, description)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING sighting_id""",
        (entity_id, window_id, location_id, seen_at, confidence, description),
    ).fetchone()
    return row[0]


def make_event(db, entity_id, *, event_type="moved", occurred_at, actor_desc=None, from_location=None,
               to_location=None, confidence=0.9, source="pipeline") -> str:
    row = db.execute(
        """INSERT INTO event (event_type, entity_id, actor_desc, from_location, to_location, occurred_at, confidence,
                              source)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING event_id""",
        (event_type, entity_id, actor_desc, from_location, to_location, occurred_at, confidence, source),
    ).fetchone()
    return str(row[0])


def make_review(db, *, entity_id=None, kind="unknown_object", description="unknown thing", observed_at=None,
                location_id=None, status="open", image_uri="s3://frames/r/x.jpg") -> str:
    row = db.execute(
        """INSERT INTO review_item (kind, entity_id, image_uri, description, observed_at, location_id, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING review_id""",
        (kind, entity_id, image_uri, description, observed_at or T0, location_id, status),
    ).fetchone()
    return str(row[0])


def table_counts(db) -> dict[str, int]:
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in DATA_TABLES}


def memory_rows(db) -> dict[tuple[str, str], dict]:
    rows = db.execute(
        "SELECT scope, scope_key, summary, priority, source_from, source_to FROM agent_memory ORDER BY 1, 2"
    ).fetchall()
    return {(r[0], r[1]): {"summary": r[2], "priority": r[3], "source_from": r[4], "source_to": r[5]} for r in rows}


# ---------------------------------------------------------------- harness helpers (no Postgres needed)
@pytest.fixture()
def plain_settings():
    """Settings for tests that never open a DB connection."""
    return Settings(agent_llm_url=LLM_URL, inventory_api_url=INVENTORY_URL,
                    database_url="postgresql://unused@127.0.0.1:1/unused")


@pytest.fixture()
def make_harness(toolbox, fake_memory, plain_settings):
    """Factory: make_harness(FakeLLM, settings=None, inventory_block=None) -> AgentHarness."""
    from nasa_agent.harness import AgentHarness
    from tests.fakes import FakeInventoryBlock

    def _make(llm, *, settings=None, inventory_block=None, memory=None):
        return AgentHarness(llm, toolbox, settings=settings or plain_settings,
                            memory=memory if memory is not None else fake_memory,
                            inventory_block=inventory_block or FakeInventoryBlock())

    return _make


def user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]
