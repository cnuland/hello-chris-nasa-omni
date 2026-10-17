"""AC-API-1 healthz, plus the packaging/infra the task requires (config from env, migrations runner, Containerfile)."""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from inventory_api.config import Settings
from inventory_api.main import create_app
from inventory_api.migrations import apply_migrations, migration_files
from tests.conftest import ADMIN_URL, CONTRACTS_DB, DB_PREFIX, _url_for

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_ac_api_01_healthz_ok_when_db_reachable(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": True}


def test_ac_api_01_healthz_503_when_db_unreachable(store):
    bad = Settings(database_url="postgresql://postgres:test@127.0.0.1:1/nope?connect_timeout=1", s3_bucket=store.bucket)
    app = create_app(settings=bad, object_store=store)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body.get("db") is False
    assert body.get("status") != "ok"


def test_ac_api_01_config_comes_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example:5432/inv")
    monkeypatch.setenv("S3_BUCKET", "frames")
    monkeypatch.setenv("S3_ENDPOINT", "http://rustfs:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("S3_SECRET_KEY", "sk")
    s = Settings.from_env()
    assert s.database_url == "postgresql://u:p@db.example:5432/inv"
    assert s.s3_bucket == "frames"
    assert s.s3_endpoint == "http://rustfs:9000"
    assert s.s3_access_key == "ak"
    assert s.s3_secret_key == "sk"


def test_ac_api_01_config_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(Exception) as excinfo:
        Settings.from_env()
    assert not isinstance(excinfo.value, NotImplementedError)
    assert "DATABASE_URL" in str(excinfo.value)


def test_migrations_files_are_contracts_in_lexical_order():
    files = migration_files(CONTRACTS_DB)
    names = [Path(f).name for f in files]
    assert names == sorted(p.name for p in CONTRACTS_DB.glob("*.sql"))
    assert names[0] == "001_init.sql"


@pytest.fixture()
def empty_db_url():
    dbname = f"{DB_PREFIX}mig_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        yield _url_for(dbname)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


def test_migrations_runner_builds_schema_and_seed_and_is_idempotent(empty_db_url):
    applied = apply_migrations(empty_db_url, CONTRACTS_DB)
    assert [Path(a).name for a in applied][:2] == ["001_init.sql", "002_seed.sql"]
    apply_migrations(empty_db_url, CONTRACTS_DB)  # second run must not fail or duplicate
    with psycopg.connect(empty_db_url) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert {"entity", "sighting", "event", "review_item", "audit_log", "stock", "entity_exemplar"} <= tables
        assert conn.execute("SELECT count(*) FROM location WHERE location_id='NOD2D4'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM catalog_item WHERE catalog_id='drill-pistol'").fetchone()[0] == 1


def test_containerfile_uses_ubi9_python311_and_uvicorn():
    text = (SERVICE_ROOT / "Containerfile").read_text()
    assert "ubi9/python-311" in text
    assert "inventory_api.main:app" in text
    assert "inventory_api.migrations" in text
