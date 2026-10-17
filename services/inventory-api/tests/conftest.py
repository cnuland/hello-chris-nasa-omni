"""Test infrastructure for inventory-api.

- One fresh Postgres database per test session (``test_api_<hex>``), built from contracts/db/*.sql, dropped at the end.
  The admin URL can be overridden with TEST_PG_ADMIN_URL. The ``inventory`` database is never touched.
- Every test starts from a clean dataset: all tables truncated and 002_seed.sql re-applied.
- S3 is faked with moto; the app gets an S3ObjectStore over the moto client.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import psycopg
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from inventory_api.config import Settings
from inventory_api.main import create_app
from inventory_api.storage import S3ObjectStore

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DB = REPO_ROOT / "contracts" / "db"
ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres:test@localhost:55432/postgres")
DB_PREFIX = "test_api_"
BUCKET = "inventory-test"

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)

# 1x1 PNG
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

DATA_TABLES = [
    "agent_memory", "decision_log", "audit_log", "review_item", "event", "sighting", "observation_window",
    "stock", "entity_exemplar", "entity", "catalog_item", "camera", "location",
]


def ts(minutes: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iso(minutes: float = 0) -> str:
    return ts(minutes).isoformat().replace("+00:00", "Z")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture()
def store(s3):
    return S3ObjectStore(s3, BUCKET)


@pytest.fixture()
def settings(database_url):
    return Settings(database_url=database_url, s3_bucket=BUCKET)


@pytest.fixture()
def client(db, settings, store):
    app = create_app(settings=settings, object_store=store)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------- data factories (direct SQL, test-only)
def vec(*hot: tuple[int, float], dim: int = 768) -> list[float]:
    """Sparse vector helper: vec((0, 1.0), (1, 0.5)) -> L2-normalised 768-d list."""
    v = [0.0] * dim
    for i, x in hot:
        v[i] = x
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def make_entity(db, *, label="thing", kind="object", status="provisional", catalog_id=None, description=None,
                last_location=None, last_seen_at=None, first_seen_at=None, confidence=0.5, snapshot_uri=None,
                needs_review=False, review_reason=None, merged_into=None, created_by="pipeline",
                attributes=None) -> str:
    row = db.execute(
        """INSERT INTO entity (label, kind, status, catalog_id, description, last_location, last_seen_at,
               first_seen_at, confidence, snapshot_uri, needs_review, review_reason, merged_into, created_by,
               attributes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING entity_id""",
        (label, kind, status, catalog_id, description, last_location, last_seen_at, first_seen_at or last_seen_at,
         confidence, snapshot_uri, needs_review, review_reason, merged_into, created_by,
         json.dumps(attributes or {})),
    ).fetchone()
    return str(row[0])


def make_sighting(db, entity_id, *, seen_at, location_id=None, confidence=0.8, frame_uri=None, description=None,
                  window_id=None) -> int:
    return db.execute(
        """INSERT INTO sighting (entity_id, window_id, location_id, seen_at, confidence, frame_uri, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING sighting_id""",
        (entity_id, window_id, location_id, seen_at, confidence, frame_uri, description),
    ).fetchone()[0]


def make_event(db, entity_id, *, event_type="moved", occurred_at, actor_desc=None, from_location=None,
               to_location=None, confidence=0.9, source="pipeline", evidence_uri=None) -> str:
    return str(db.execute(
        """INSERT INTO event (event_type, entity_id, actor_desc, from_location, to_location, occurred_at, confidence,
               source, evidence_uri) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING event_id""",
        (event_type, entity_id, actor_desc, from_location, to_location, occurred_at, confidence, source,
         evidence_uri),
    ).fetchone()[0])


def make_exemplar(db, entity_id, embedding, *, quality=0.5, crop_uri=None) -> int:
    return db.execute(
        "INSERT INTO entity_exemplar (entity_id, embedding, quality, crop_uri) VALUES (%s,%s::halfvec,%s,%s) "
        "RETURNING exemplar_id",
        (entity_id, _vec_literal(embedding), quality, crop_uri),
    ).fetchone()[0]


def make_review(db, *, entity_id=None, kind="unknown_object", candidate_ids=(), image_uri=f"s3://{BUCKET}/r/x.jpg",
                description="unidentified object", observed_at=None, location_id=None, model_guess=None,
                status="open", created_at=None) -> str:
    observed_at = observed_at or T0
    return str(db.execute(
        """INSERT INTO review_item (kind, entity_id, candidate_ids, image_uri, description, observed_at, location_id,
               model_guess, status, created_at)
           VALUES (%s,%s,%s::uuid[],%s,%s,%s,%s,%s,%s,COALESCE(%s, now())) RETURNING review_id""",
        (kind, entity_id, list(candidate_ids), image_uri, description, observed_at, location_id, model_guess, status,
         created_at),
    ).fetchone()[0])


def make_window(db, *, camera_id="cam-lab1-fwd", t_start=None, t_end=None, scene_summary=None, transcript=None,
                model="qwen2.5-omni-7b") -> str:
    t_start = t_start or T0
    t_end = t_end or t_start + timedelta(seconds=5)
    return str(db.execute(
        """INSERT INTO observation_window (camera_id, t_start, t_end, scene_summary, transcript, model)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING window_id""",
        (camera_id, t_start, t_end, scene_summary, transcript, model),
    ).fetchone()[0])


def table_counts(db) -> dict[str, int]:
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in DATA_TABLES}


def audit_rows(db, op=None):
    sql = "SELECT op, entity_ids, actor, detail, at FROM audit_log"
    params: tuple = ()
    if op:
        sql += " WHERE op = %s"
        params = (op,)
    return db.execute(sql + " ORDER BY audit_id", params).fetchall()


def ok(resp, status: int = 200):
    """Assert the HTTP status first (so failures read 'expected 200, got 500: ...'), then return the JSON body."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text[:300]}"
    return resp.json()
