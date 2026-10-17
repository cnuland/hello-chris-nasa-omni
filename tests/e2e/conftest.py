"""End-to-end fixtures: one synthetic eval scene through the whole system.

Session flow (shared by every test, built once):

1. ``database_url``: a fresh ``test_int_e2e_<hex>`` database built from ``contracts/db/*.sql``; dropped at the end.
   The ``inventory`` database is never opened.
2. ``inventory_api``: the real inventory-api app served in-process by uvicorn on 127.0.0.1 (real HTTP, so perception's
   sync client and the agent's async client both talk to it exactly as in the cluster). Its object store is an
   in-memory store **shared with perception**, so ``/media`` serves the frames and crops perception uploaded.
3. ``dataset``: synthetic scene ``nasa_eval.synth.generate_scene(SEED)`` (the drill is moved LAB1O3 -> LAB1S2 by
   "crew member, green shirt"; identical twins; a removed object; one unknown).
4. ``replay``: ``perception.replay.run`` (AC-PER-18) over the scene with the eval ``local`` collaborators (gold-
   scripted omni/detector/ASR fakes, appearance embedder, abstaining FakeDecision) and the manifest's camera zones.

The agent is created per test with a scripted OpenAI-compatible LLM (``ScriptedLLM``) reached over
``httpx.MockTransport``, so the agent's real ``OpenAICompatLLM`` wire format is exercised.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg import sql

REPO = Path(__file__).resolve().parents[2]
CONTRACTS_DB = REPO / "contracts" / "db"
ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres:test@localhost:55432/postgres")
DB_PREFIX = "test_int_"
SEED = 2
BUCKET = "inventory"


# ------------------------------------------------------------------------------------------------ database
def _url_for(name: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


def list_databases() -> set[str]:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        return {r[0] for r in conn.execute("SELECT datname FROM pg_database").fetchall()}


def _drop(name: str) -> None:
    assert name.startswith(DB_PREFIX) and name != "inventory", name
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    try:
        psycopg.connect(ADMIN_URL, connect_timeout=3).close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment
        pytest.skip(f"local Postgres not reachable at TEST_PG_ADMIN_URL: {exc}")
    name = f"{DB_PREFIX}e2e_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = _url_for(name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for f in sorted(CONTRACTS_DB.glob("*.sql")):
                conn.execute(f.read_text())
        yield url
    finally:
        _drop(name)
        assert name not in list_databases()


# ------------------------------------------------------------------------------------------------ object store
class SharedObjectStore:
    """One in-memory bucket implementing both perception's Storage (``put``) and inventory-api's ObjectStore
    (``put``/``get``/``delete``) protocols, with inventory-api's key validation."""

    def __init__(self, bucket: str = BUCKET):
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes, content_type: str) -> str:
        from inventory_api.storage import make_uri, validate_key

        with self._lock:
            self.objects[validate_key(key)] = (bytes(data), content_type)
        return make_uri(self.bucket, key)

    def get(self, key: str) -> Any:
        from inventory_api.storage import ObjectNotFound, StoredObject, validate_key

        validate_key(key)
        with self._lock:
            if key not in self.objects:
                raise ObjectNotFound(key)
            data, content_type = self.objects[key]
        return StoredObject(iter([data]), content_type, len(data))

    def delete(self, key: str) -> None:
        with self._lock:
            self.objects.pop(key, None)


@pytest.fixture(scope="session")
def object_store() -> SharedObjectStore:
    return SharedObjectStore()


# ------------------------------------------------------------------------------------------------ inventory-api
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def inventory_api(database_url, object_store) -> Iterator[str]:
    """Base URL of the real inventory-api, served by uvicorn in a background thread of this process."""
    import uvicorn
    from inventory_api.config import Settings
    from inventory_api.main import create_app

    app = create_app(Settings(database_url=database_url, s3_bucket=BUCKET), object_store=object_store)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, name="inventory-api", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("inventory-api did not start")
        time.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    try:
        assert httpx.get(f"{base}/healthz").json() == {"status": "ok", "db": True}
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def api(inventory_api) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=inventory_api, timeout=10) as client:
        yield client


# ------------------------------------------------------------------------------------------------ scene + replay
@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    from nasa_eval.dataset import load_dataset
    from nasa_eval.synth import generate_scene, write_scene

    return load_dataset(write_scene(generate_scene(SEED), tmp_path_factory.mktemp("e2e"), f"synth-{SEED}"))


@pytest.fixture(scope="session")
def replay(dataset, inventory_api, object_store):
    """perception.replay.run over the scene; returns ReplayStats. ``environ={}``: no live endpoint from the shell
    environment (DECISION_URL, CAMERA_ZONES, ...) can leak into the e2e run."""
    from nasa_eval.collaborators import build_collaborators, replay_config
    from perception import replay as perception_replay
    from perception.inventory_client import HttpInventoryClient

    client = HttpInventoryClient(inventory_api, timeout_s=30)
    try:
        collab = build_collaborators(dataset, "local", catalog=client.get_catalog(), environ={})
        collab["storage"] = object_store
        return perception_replay.run(
            dataset.iter_source(),
            camera_id=dataset.manifest.camera,
            start_time=dataset.manifest.start_time,
            api=client,
            config=replay_config(dataset, environ={}),
            **collab,
        )
    finally:
        client.close()


# ------------------------------------------------------------------------------------------------ gold helpers
@pytest.fixture(scope="session")
def gold(dataset) -> dict[str, Any]:
    return {g.gold_object_id: g for g in dataset.gold}


@pytest.fixture(scope="session")
def drill(dataset):
    return next(g for g in dataset.gold if g.catalog_id == "drill-pistol")


@pytest.fixture(scope="session")
def drill_move(drill):
    return next(e for e in drill.events if e.type == "moved")


# ------------------------------------------------------------------------------------------------ agent
class ScriptedLLM:
    """OpenAI-compatible ``/v1/chat/completions`` over ``httpx.MockTransport``. ``policy(messages, tools)`` returns
    the assistant message for each request; every request body is recorded in ``requests``."""

    def __init__(self, policy: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]):
        self.policy = policy
        self.requests: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/v1/chat/completions":
            return httpx.Response(404, json={"error": "not found"})
        body = json.loads(request.content)
        self.requests.append(body)
        message = self.policy(body["messages"], body.get("tools") or [])
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(self.requests)}",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
            },
        )


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    }


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parsed ``role: tool`` payloads of the current turn, oldest first."""
    return [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]


@pytest.fixture()
def make_agent(inventory_api, database_url):
    """make_agent(policy) -> (TestClient over the real agent app, ScriptedLLM)."""
    from fastapi.testclient import TestClient
    from nasa_agent.config import Settings
    from nasa_agent.llm import OpenAICompatLLM
    from nasa_agent.main import create_app

    clients: list[Any] = []

    def factory(policy):
        llm = ScriptedLLM(policy)
        settings = Settings(
            agent_llm_url="http://agent-llm.e2e/v1", inventory_api_url=inventory_api, database_url=database_url
        )
        app = create_app(
            settings, llm=OpenAICompatLLM(settings.agent_llm_url, settings.agent_llm_model, transport=llm.transport)
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, llm

    yield factory
    for c in clients:
        c.__exit__(None, None, None)
