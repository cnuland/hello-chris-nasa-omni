"""Infrastructure: configuration from environment only (00-conventions), packaging, Containerfile, memory store.

Supports AC-AGT-3 (AGENT_LLM_URL, step cap), AC-AGT-6/7/10 (configurable budgets) and AC-AGT-2 (recall_memory reads
agent_memory through MemoryStore).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nasa_agent.config import Settings
from nasa_agent.memory import MemoryStore

ROOT = Path(__file__).resolve().parents[1]

BASE_ENV = {
    "AGENT_LLM_URL": "http://agent-llm.nasa-inference.svc:8000/v1",
    "INVENTORY_API_URL": "http://inventory-api.nasa-app.svc:8080",
    "DATABASE_URL": "postgresql://agent@db/inventory",
}


def test_ac_agt_03_settings_from_env_required_values():
    s = Settings.from_env(BASE_ENV)
    assert s.agent_llm_url == BASE_ENV["AGENT_LLM_URL"]
    assert s.inventory_api_url == BASE_ENV["INVENTORY_API_URL"]
    assert s.database_url == BASE_ENV["DATABASE_URL"]
    assert s.max_steps == 6
    assert s.agent_llm_api_key is None


@pytest.mark.parametrize("missing", ["AGENT_LLM_URL", "INVENTORY_API_URL", "DATABASE_URL"])
def test_ac_agt_03_settings_missing_required_env_is_value_error(missing):
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        Settings.from_env(env)


def test_ac_agt_07_settings_budgets_and_dream_config_from_env():
    env = dict(BASE_ENV, AGENT_LLM_MODEL="qwen3-8b", AGENT_LLM_API_KEY="k", AGENT_CONTEXT_TOKEN_BUDGET="3000",
               AGENT_INVENTORY_TOKEN_BUDGET="800", DREAM_WINDOW_HOURS="12", DREAM_INTERVAL_SECONDS="600",
               DREAM_TOKEN_BUDGET="250", AGENT_MAX_STEPS="4")
    s = Settings.from_env(env)
    assert s.agent_llm_model == "qwen3-8b" and s.agent_llm_api_key == "k"
    assert s.context_token_budget == 3000 and s.inventory_block_token_budget == 800
    assert s.dream_window_hours == 12 and s.dream_interval_seconds == 600 and s.dream_token_budget == 250
    assert s.max_steps == 4


def test_ac_agt_03_settings_never_allow_more_than_six_steps():
    try:
        s = Settings.from_env(dict(BASE_ENV, AGENT_MAX_STEPS="10"))
    except ValueError:
        return
    assert s.max_steps <= 6


@pytest.mark.parametrize("var,value", [("AGENT_CONTEXT_TOKEN_BUDGET", "lots"), ("AGENT_MAX_STEPS", "0"),
                                       ("DREAM_WINDOW_HOURS", "-1")])
def test_ac_agt_07_settings_reject_invalid_numbers(var, value):
    with pytest.raises(ValueError):
        Settings.from_env(dict(BASE_ENV, **{var: value}))


def test_no_secrets_or_credentials_in_source():
    pat = re.compile(r"(postgres(ql)?://[^\s'\"]*:[^\s'\"@]+@)|(sk-[A-Za-z0-9]{16,})|(Bearer [A-Za-z0-9]{16,})")
    for path in list((ROOT / "nasa_agent").rglob("*.py")) + [ROOT / "mcp_server.py"]:
        assert not pat.search(path.read_text()), path


def test_containerfile_ubi9_python311_serves_app_factory():
    text = (ROOT / "Containerfile").read_text()
    assert "ubi9/python-311" in text
    assert "nasa_agent.main:create_app" in text and "--factory" in text
    assert "mcp_server.py" in text
    assert "USER 1001" in text


# ---------------------------------------------------------------- MemoryStore over the real schema
def test_ac_agt_02_memory_store_upsert_get_and_global(database_url, db):
    store = MemoryStore(database_url)
    assert store.get("entity", "nope") is None
    assert store.get_global() is None
    store.upsert("global", "global", "first summary", priority=0.9)
    store.upsert("global", "global", "second summary", priority=0.9)
    assert store.get_global() == "second summary"
    row = store.get("global", "global")
    assert row["summary"] == "second summary" and row["scope"] == "global" and row["scope_key"] == "global"
    assert db.execute("SELECT count(*) FROM agent_memory").fetchone()[0] == 1


def test_ac_agt_02_memory_store_rejects_bad_scope(database_url, db):
    with pytest.raises(ValueError):
        MemoryStore(database_url).upsert("planet", "x", "s")


async def test_ac_agt_02_recall_memory_tool_over_real_store(database_url, db, inventory):
    from nasa_agent.tools import Toolbox

    db.execute("INSERT INTO agent_memory (scope, scope_key, summary) VALUES ('location', 'NOD2', 'Node 2 is busy')")
    tb = Toolbox(inventory, MemoryStore(database_url))
    r = await tb.call("recall_memory", {"scope": "location", "key": "NOD2"})
    assert r.ok and "Node 2 is busy" in str(r.data)
