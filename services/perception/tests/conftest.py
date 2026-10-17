"""Test infrastructure for perception.

- Media fixtures are generated once per session with the ffmpeg binary shipped by imageio-ffmpeg (no network).
- Reconciler tests use FakeInventory (tests/support.py), which validates every payload against inventory-api's
  request model. The end-to-end test (AC-PER-14) runs the real inventory-api app in-process against a fresh
  ``test_per_<hex>`` Postgres database built from contracts/db/*.sql and dropped afterwards (TEST_PG_ADMIN_URL
  overrides the admin URL). The database named ``inventory`` is never touched.
"""
from __future__ import annotations

import functools
import http.server
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import BUCKET, CAMERA_LOCATIONS, CATALOG, CONTRACTS, LOCATIONS, FakeInventory

ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres:test@localhost:55432/postgres")
DB_PREFIX = "test_per_"


# ------------------------------------------------------------------------------------------------ media
def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=60)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def sample_video(media_dir) -> Path:
    """6 s, 320x240 @ 25 fps test pattern + 440 Hz stereo 44.1 kHz audio (ingest must resample to 16 kHz mono)."""
    path = media_dir / "sample.mp4"
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
        "-ac", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])
    return path


@pytest.fixture(scope="session")
def video_only(media_dir) -> Path:
    """4 s video without an audio stream."""
    path = media_dir / "video_only.mp4"
    _run_ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-an", str(path)])
    return path


@pytest.fixture(scope="session")
def http_media_url(media_dir, sample_video):
    """Base URL of a local HTTP server serving the media directory (for the 'URL source' case)."""
    handler = functools.partial(_QuietHandler, directory=str(media_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


# ------------------------------------------------------------------------------------------------ collaborators
@pytest.fixture()
def inv() -> FakeInventory:
    return FakeInventory()


@pytest.fixture()
def config():
    from perception.config import PerceptionConfig

    return PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET)


@pytest.fixture()
def storage():
    from perception.fakes import MemoryStorage

    return MemoryStorage(BUCKET)


@pytest.fixture()
def embedder():
    from perception.fakes import FakeEmbedder

    return FakeEmbedder()


@pytest.fixture()
def make_reconciler(inv, storage, embedder, config):
    """Factory: make_reconciler(camera_id=..., decision=..., scorer=..., api=..., **config_overrides)."""
    from perception.fakes import FakeDecision
    from perception.reconciler import Reconciler

    def factory(camera_id="cam-lab1-fwd", *, decision=None, scorer=None, api=None, **overrides):
        for k, v in overrides.items():
            setattr(config, k, v)
        return Reconciler(api=api or inv, embedder=embedder, decision=decision or FakeDecision(), storage=storage,
                          config=config, camera_id=camera_id, catalog=CATALOG, locations=LOCATIONS, scorer=scorer)

    return factory


# ------------------------------------------------------------------------------------------------ database (AC-PER-14)
def _url_for(dbname: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def database_url():
    psycopg = pytest.importorskip("psycopg")
    dbname = f"{DB_PREFIX}{uuid.uuid4().hex[:12]}"
    assert dbname.startswith(DB_PREFIX) and dbname != "inventory"
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment problem, reported loudly
        pytest.fail(f"local Postgres test server not reachable at {ADMIN_URL}: {exc}")
    with admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    url = _url_for(dbname)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for sql_file in sorted((CONTRACTS / "db").glob("*.sql")):
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
    import psycopg

    tables = ["agent_memory", "decision_log", "audit_log", "review_item", "event", "sighting", "observation_window",
              "stock", "entity_exemplar", "entity", "catalog_item", "camera", "location"]
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
        conn.execute((CONTRACTS / "db" / "002_seed.sql").read_text())
        yield conn


@pytest.fixture()
def api_app_client(db, database_url):
    """The real inventory-api app (services/inventory-api) in-process, with moto S3, as an httpx.Client."""
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from fastapi.testclient import TestClient
    from inventory_api.config import Settings
    from inventory_api.main import create_app
    from inventory_api.storage import S3ObjectStore

    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        s3.create_bucket(Bucket=BUCKET)
        app = create_app(settings=Settings(database_url=database_url, s3_bucket=BUCKET),
                         object_store=S3ObjectStore(s3, BUCKET))
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
