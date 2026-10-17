"""Test infrastructure for nasa_eval (docs/spec/50-eval.md).

- Paths and seed data parsed from contracts/db/002_seed.sql (catalog ids, location ids, hierarchy).
- ``write_dataset`` builds a gold dataset directory (manifest.yaml + gold.jsonl [+ qa.jsonl] [+ frames]).
- Postgres: one fresh ``test_eval_<hex>`` database per session built from contracts/db/*.sql and dropped at the end.
  ``admin_url`` can be overridden with TEST_PG_ADMIN_URL. The ``inventory`` database is never touched.
- ``fake_http``: a local scripted HTTP server (OpenAI-compatible judge, agent /chat, /v1/systemone).
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

EVAL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EVAL_DIR.parent
CONTRACTS_DB = REPO_ROOT / "contracts" / "db"
SEED_SQL = (CONTRACTS_DB / "002_seed.sql").read_text()
ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres:test@localhost:55432/postgres")
DB_PREFIX = "test_eval_"

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _seed_rows(table: str) -> list[str]:
    block = SEED_SQL.split(f"INSERT INTO {table}")[1].split("ON CONFLICT")[0]
    return [line.strip() for line in block.splitlines() if line.strip().startswith("(")]


# location_id -> (parent_id, kind)
SEED_LOCATIONS: dict[str, tuple[str | None, str]] = {}
for _row in _seed_rows("location "):
    _m = re.match(r"\('([^']+)',\s*(NULL|'[^']*'),\s*'([^']+)'", _row)
    if _m:
        SEED_LOCATIONS[_m.group(1)] = (None if _m.group(2) == "NULL" else _m.group(2).strip("'"), _m.group(3))
SEED_CATALOG_IDS: set[str] = {
    m.group(1) for r in _seed_rows("catalog_item ") if (m := re.match(r"\('([^']+)'", r))
}
assert "LAB1S2" in SEED_LOCATIONS and SEED_LOCATIONS["LAB1S2"] == ("LAB1", "zone")
assert "drill-pistol" in SEED_CATALOG_IDS

EVENT_TYPES = ("moved", "added", "removed", "used", "handed_over", "seen", "stowed", "unstowed", "consumed")


# ---------------------------------------------------------------- dataset builders
def manifest_dict(**over: Any) -> dict[str, Any]:
    m = {
        "name": "tiny",
        "video": "frames",
        "audio": None,
        "camera": "cam-lab1-fwd",
        "start_time": "2026-10-01T12:00:00Z",
        "fps": 1,
    }
    m.update(over)
    return m


def gold_obj(gid: str, catalog_id: str | None, appearances: list[dict], events: list[dict] | None = None,
             is_unknown: bool = False) -> dict[str, Any]:
    return {"gold_object_id": gid, "catalog_id": catalog_id, "is_unknown": is_unknown,
            "appearances": appearances, "events": events or []}


def app(t: float, location: str, bbox: list[float] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"t": t, "location": location}
    if bbox is not None:
        d["bbox"] = bbox
    return d


# A small hand-made gold set: drill seen then moved S2 -> P3; two identical wrenches; an unknown; a laptop that
# disappears (removed).
TINY_GOLD = [
    gold_obj("drill-1", "drill-pistol",
             [app(0, "LAB1S2", [0.10, 0.10, 0.20, 0.20]), app(1, "LAB1S2", [0.10, 0.10, 0.20, 0.20]),
              app(10, "LAB1P3", [0.60, 0.60, 0.70, 0.70])],
             [{"type": "moved", "t": 8.0, "actor": "crew member, blue shirt", "to_location": "LAB1P3"}]),
    gold_obj("wrench-a", "wrench", [app(2, "LAB1S2", [0.30, 0.10, 0.40, 0.20]),
                                     app(3, "LAB1S2", [0.30, 0.10, 0.40, 0.20])]),
    gold_obj("wrench-b", "wrench", [app(2, "LAB1O3", [0.30, 0.50, 0.40, 0.60]),
                                     app(3, "LAB1O3", [0.30, 0.50, 0.40, 0.60])]),
    gold_obj("unk-1", None, [app(5, "LAB1O3", [0.80, 0.10, 0.90, 0.20])], is_unknown=True),
    gold_obj("laptop-1", "laptop", [app(0, "LAB1P3", [0.50, 0.20, 0.60, 0.30])],
             [{"type": "removed", "t": 6.0, "actor": "crew member, red shirt", "to_location": None}]),
]


def write_dataset(root: Path, *, name: str = "tiny", manifest: dict | None = None, gold: list | None = None,
                  qa: list | None = None, raw_gold_lines: list[str] | None = None,
                  frames: int = 0, size: tuple[int, int] = (32, 24)) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump(manifest if manifest is not None else manifest_dict(name=name)))
    if raw_gold_lines is not None:
        (d / "gold.jsonl").write_text("\n".join(raw_gold_lines) + "\n")
    else:
        (d / "gold.jsonl").write_text("".join(json.dumps(g) + "\n" for g in (gold if gold is not None else TINY_GOLD)))
    if qa is not None:
        (d / "qa.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qa))
    if frames:
        import numpy as np
        from PIL import Image

        (d / "frames").mkdir(exist_ok=True)
        for i in range(frames):
            arr = np.full((size[1], size[0], 3), i * 10 % 256, dtype=np.uint8)
            Image.fromarray(arr).save(d / "frames" / f"{i:06d}.png")
    return d


@pytest.fixture
def tiny_dataset(tmp_path: Path) -> Path:
    return write_dataset(tmp_path, frames=12)


# ---------------------------------------------------------------- Postgres
def _url_for(dbname: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{dbname}"


def list_databases() -> set[str]:
    import psycopg

    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        return {r[0] for r in conn.execute("SELECT datname FROM pg_database").fetchall()}


@pytest.fixture(scope="session")
def admin_url() -> str:
    import psycopg

    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:  # pragma: no cover - environment
        pytest.skip(f"local Postgres not reachable at {ADMIN_URL}: {exc}")
    return ADMIN_URL


@pytest.fixture(scope="session")
def session_db(admin_url: str):
    """A fresh test_eval_<hex> database built from contracts/db/*.sql (dropped at session end)."""
    import psycopg

    dbname = f"{DB_PREFIX}{uuid.uuid4().hex[:12]}"
    assert dbname.startswith(DB_PREFIX) and dbname != "inventory"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    url = _url_for(dbname)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for sql_file in sorted(CONTRACTS_DB.glob("*.sql")):
                conn.execute(sql_file.read_text())
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = %s AND pid <> pg_backend_pid()", (dbname,))
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


DATA_TABLES = ["agent_memory", "decision_log", "audit_log", "review_item", "event", "sighting",
               "observation_window", "stock", "entity_exemplar", "entity"]


@pytest.fixture
def db_url(session_db: str) -> str:
    """Session DB with all pipeline-written tables emptied (seed catalog/locations kept)."""
    import psycopg

    with psycopg.connect(session_db, autocommit=True) as conn:
        conn.execute("TRUNCATE " + ", ".join(DATA_TABLES) + " CASCADE")
    return session_db


class DbWriter:
    """Insert pipeline-like rows (entities, sightings, events, reviews) into an inventory database."""

    def __init__(self, url: str):
        self.url = url

    def _exec(self, sql: str, params: tuple) -> Any:
        import psycopg

        with psycopg.connect(self.url, autocommit=True) as conn:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def entity(self, catalog_id: str | None, *, status: str = "provisional", label: str | None = None,
               last_location: str | None = None, last_seen_s: float | None = None, merged_into: str | None = None,
               kind: str = "object") -> str:
        return str(self._exec(
            "INSERT INTO entity (catalog_id, kind, status, merged_into, label, last_location, last_seen_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING entity_id",
            (catalog_id, kind, status, merged_into, label or (catalog_id or "unknown"), last_location,
             at(last_seen_s) if last_seen_s is not None else None)))

    def sighting(self, entity_id: str, t: float, location: str | None, bbox: list[float] | None,
                 confidence: float = 0.9) -> str:
        return str(self._exec(
            "INSERT INTO sighting (entity_id, location_id, seen_at, bbox, confidence) VALUES (%s,%s,%s,%s,%s) "
            "RETURNING sighting_id", (entity_id, location, at(t), bbox, confidence)))

    def event(self, entity_id: str, event_type: str, t: float, actor_desc: str | None = None,
              to_location: str | None = None) -> str:
        return str(self._exec(
            "INSERT INTO event (event_type, entity_id, actor_desc, to_location, occurred_at, confidence) "
            "VALUES (%s,%s,%s,%s,%s,0.8) RETURNING event_id", (event_type, entity_id, actor_desc, to_location, at(t))))

    def review(self, entity_id: str | None, kind: str = "unknown_object", status: str = "open") -> str:
        return str(self._exec(
            "INSERT INTO review_item (kind, entity_id, image_uri, description, observed_at, status) "
            "VALUES (%s,%s,'s3://crops/x.jpg','unknown thing',%s,%s) RETURNING review_id",
            (kind, entity_id, T0, status)))

    def perfect_replay_of(self, gold: list[dict]) -> dict[str, str]:
        """Write a perfect pipeline result for a gold list; returns gold_object_id -> entity_id."""
        mapping: dict[str, str] = {}
        for g in gold:
            last = max(g["appearances"], key=lambda a: a["t"])
            eid = self.entity(g["catalog_id"], status="unknown" if g["is_unknown"] else "provisional",
                              last_location=last["location"], last_seen_s=last["t"])
            mapping[g["gold_object_id"]] = eid
            for a in g["appearances"]:
                self.sighting(eid, a["t"], a["location"], a.get("bbox"))
            for e in g["events"]:
                self.event(eid, e["type"], e["t"], e.get("actor"), e.get("to_location"))
            if g["is_unknown"]:
                self.review(eid, "unknown_object")
        return mapping


@pytest.fixture
def db_writer(db_url: str) -> DbWriter:
    return DbWriter(db_url)


# ---------------------------------------------------------------- fake HTTP server
class FakeHttp:
    """Scripted HTTP server. ``route(method, path, handler)`` where handler(body_json) -> (status, json_body)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], Callable[[Any], tuple[int, Any]]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # silence
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode(errors="replace")
                path = self.path.split("?")[0]
                fake.requests.append({"method": method, "path": path, "body": body,
                                      "headers": dict(self.headers.items())})
                handler = fake.routes.get((method, path))
                status, payload = handler(body) if handler else (404, {"error": "no route"})
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                self._handle("POST")

            def do_GET(self) -> None:
                self._handle("GET")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def route(self, method: str, path: str, handler: Callable[[Any], tuple[int, Any]]) -> None:
        self.routes[(method, path)] = handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_http():
    srv = FakeHttp()
    yield srv
    srv.close()


def chat_completion(content: str, model: str = "judge") -> dict[str, Any]:
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


# ---------------------------------------------------------------- services/perception on sys.path (review round)
PERCEPTION_SRC = REPO_ROOT / "services" / "perception"


@pytest.fixture
def perception_on_path(monkeypatch):
    """Make ``perception`` (services/perception, numpy/jsonschema/pillow/httpx only for the parts we use) importable
    for one test, then forget it again so other tests still see the eval venv without perception."""
    import importlib
    import sys

    if not (PERCEPTION_SRC / "perception" / "__init__.py").exists():  # pragma: no cover - repo layout
        pytest.skip("services/perception not found")
    before = {k for k in sys.modules if k == "perception" or k.startswith("perception.")}
    monkeypatch.syspath_prepend(str(PERCEPTION_SRC))
    importlib.invalidate_caches()
    yield PERCEPTION_SRC
    for k in [k for k in sys.modules if (k == "perception" or k.startswith("perception.")) and k not in before]:
        del sys.modules[k]
