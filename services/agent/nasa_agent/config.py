"""Configuration from environment variables only (no secrets in code)."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: Hard cap from AC-AGT-3: the tool-calling loop makes at most this many LLM calls per chat request.
MAX_STEPS_LIMIT = 6

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    agent_llm_url: str  # AGENT_LLM_URL (required), OpenAI-compatible base URL incl. /v1
    inventory_api_url: str  # INVENTORY_API_URL (required)
    database_url: str  # DATABASE_URL (required) - agent_memory + SQL inventory block
    agent_llm_model: str = "agent-llm"  # AGENT_LLM_MODEL
    agent_llm_api_key: str | None = None  # AGENT_LLM_API_KEY (optional)
    max_steps: int = MAX_STEPS_LIMIT  # AGENT_MAX_STEPS (never above 6)
    context_token_budget: int = 6000  # AGENT_CONTEXT_TOKEN_BUDGET
    inventory_block_token_budget: int = 1500  # AGENT_INVENTORY_TOKEN_BUDGET
    dream_window_hours: float = 6.0  # DREAM_WINDOW_HOURS
    dream_interval_seconds: float = 900.0  # DREAM_INTERVAL_SECONDS
    dream_token_budget: int = 400  # DREAM_TOKEN_BUDGET (per summary)
    http_timeout_s: float = 30.0  # AGENT_HTTP_TIMEOUT_S
    inventory_recent_hours: float = 24.0  # AGENT_INVENTORY_RECENT_HOURS (moves window of the inventory block)
    dream_in_process: bool = False  # DREAM_IN_PROCESS: also run the periodic dreaming job inside the API pod
    tool_result_token_budget: int = 1500  # AGENT_TOOL_RESULT_TOKEN_BUDGET: cap per tool result fed back to the LLM
    stream_keepalive_seconds: float = 10.0  # AGENT_STREAM_KEEPALIVE_S: SSE comment interval while the loop runs
    admin_token: str | None = None  # AGENT_ADMIN_TOKEN (optional): bearer token required by POST /memory/dream

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from ``env`` (default ``os.environ``). Missing required variables -> ValueError naming them."""
        env = os.environ if env is None else env

        missing = [k for k in ("AGENT_LLM_URL", "INVENTORY_API_URL", "DATABASE_URL") if not env.get(k, "").strip()]
        if missing:
            raise ValueError(f"missing required environment variable(s): {', '.join(missing)}")

        def num(name: str, default: Any, cast: Callable[[str], Any]) -> Any:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = cast(raw.strip())
            except ValueError as exc:
                raise ValueError(f"{name} must be a number, got {raw!r}") from exc
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {raw!r}")
            return value

        return cls(
            agent_llm_url=env["AGENT_LLM_URL"].strip(),
            inventory_api_url=env["INVENTORY_API_URL"].strip(),
            database_url=env["DATABASE_URL"].strip(),
            agent_llm_model=env.get("AGENT_LLM_MODEL", "").strip() or cls.agent_llm_model,
            agent_llm_api_key=env.get("AGENT_LLM_API_KEY", "").strip() or None,
            max_steps=min(num("AGENT_MAX_STEPS", MAX_STEPS_LIMIT, int), MAX_STEPS_LIMIT),
            context_token_budget=num("AGENT_CONTEXT_TOKEN_BUDGET", cls.context_token_budget, int),
            inventory_block_token_budget=num("AGENT_INVENTORY_TOKEN_BUDGET", cls.inventory_block_token_budget, int),
            dream_window_hours=num("DREAM_WINDOW_HOURS", cls.dream_window_hours, float),
            dream_interval_seconds=num("DREAM_INTERVAL_SECONDS", cls.dream_interval_seconds, float),
            dream_token_budget=num("DREAM_TOKEN_BUDGET", cls.dream_token_budget, int),
            http_timeout_s=num("AGENT_HTTP_TIMEOUT_S", cls.http_timeout_s, float),
            inventory_recent_hours=num("AGENT_INVENTORY_RECENT_HOURS", cls.inventory_recent_hours, float),
            dream_in_process=env.get("DREAM_IN_PROCESS", "").strip().lower() in _TRUE,
            tool_result_token_budget=num("AGENT_TOOL_RESULT_TOKEN_BUDGET", cls.tool_result_token_budget, int),
            stream_keepalive_seconds=num("AGENT_STREAM_KEEPALIVE_S", cls.stream_keepalive_seconds, float),
            admin_token=env.get("AGENT_ADMIN_TOKEN", "").strip() or None,
        )

    @property
    def effective_max_steps(self) -> int:
        """``max_steps`` clamped to [1, 6] (AC-AGT-3)."""
        return max(1, min(self.max_steps, MAX_STEPS_LIMIT))
