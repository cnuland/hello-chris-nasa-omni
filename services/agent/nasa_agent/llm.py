"""OpenAI-compatible chat-completions client (AGENT_LLM_URL) and the LLM protocol used by the harness."""

from __future__ import annotations

from typing import Any, Protocol

import httpx


class LLMError(RuntimeError):
    """The LLM endpoint failed (transport error, non-200 or malformed response)."""


class LLMTimeout(LLMError):
    """The LLM did not answer in time (HTTP timeout or the turn deadline, AC-AGT-15)."""


class LLMClient(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str = "auto"
    ) -> dict[str, Any]:
        """Return the assistant message dict {role, content, tool_calls?, finish_reason?} of choices[0]."""
        ...


class OpenAICompatLLM:
    """Minimal ``POST {base_url}/chat/completions`` client (vLLM, any OpenAI-compatible server)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        max_tokens: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.transport = transport
        self.timeout = timeout
        self.max_tokens = max_tokens

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str = "auto"
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"LLM timed out after {self.timeout:g} s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM returned HTTP {resp.status_code}")
        try:
            choice = resp.json()["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM returned a malformed chat completion") from exc
        if not isinstance(message, dict):
            raise LLMError("LLM returned a malformed chat completion")
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        if isinstance(finish, str):
            message = {**message, "finish_reason": finish}  # AC-AGT-21: "length" = cut at max_tokens
        return message
