"""AC-PER-12 decision client: ``POST {base_url}/v1/systemone`` (choice), chat-completions fallback.

systemone request (Jev contract): ``{"model", "instructions", "state": {...}, "questions": {"<name>": {"type":
"choice", "instructions": str, "criteria": {option: description, ...}}}}``; response ``{"answers": {"<name>":
{"choice": str, "probabilities": {option: p}, "confidence": p}}}``.
Chat fallback (used when ``mode == "chat"`` or systemone answers 404/405; a missing systemone endpoint is remembered,
so later calls go straight to chat): options are lettered A, B, C...; the
request asks for one letter with ``logprobs``/``top_logprobs``; probabilities come from the first token's
top_logprobs (else the letter in the content with probabilities {}).
On timeout / HTTP error / malformed answer / unknown option -> DecisionResult(choice="NEW", probabilities={},
needs_review=True). Never raises.
"""

from __future__ import annotations

import json
import logging
import math
import string
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx

from . import NEW
from .types import DecisionResult

logger = logging.getLogger(__name__)

DEFAULT_INSTRUCTIONS = (
    "You adjudicate inventory decisions for the International Space Station. Choose exactly one "
    "option; prefer NEW only when no option fits."
)


class DecisionModel(Protocol):
    def choose(
        self,
        question: str,
        options: Mapping[str, str] | Sequence[str],
        *,
        state: dict[str, Any] | None = None,
        instructions: str | None = None,
    ) -> DecisionResult: ...


class _Fallback(Exception):
    """systemone is not served here: use the chat endpoint."""


class DecisionError(Exception):
    """The decision service answered, but not usably."""


def _criteria(options: Mapping[str, str] | Sequence[str]) -> dict[str, str]:
    if isinstance(options, Mapping):
        return {str(k): str(v) for k, v in options.items()}
    return {str(o): str(o) for o in options}


class DecisionClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        http_client: httpx.Client | None = None,
        timeout_s: float = 2.0,
        mode: str = "systemone",
    ):
        if mode not in ("systemone", "chat"):
            raise ValueError("mode must be 'systemone' or 'chat'")
        self.base_url = base_url
        self.model = model
        self.http_client = http_client
        self.timeout_s = timeout_s
        self.mode = mode
        self._systemone_missing = False

    def choose(
        self,
        question: str,
        options: Mapping[str, str] | Sequence[str],
        *,
        state: dict[str, Any] | None = None,
        instructions: str | None = None,
    ) -> DecisionResult:
        criteria = _criteria(options)
        started = time.monotonic()
        try:
            if self.mode == "systemone" and not self._systemone_missing:
                try:
                    choice, probs, model = self._systemone(question, criteria, state, instructions)
                except _Fallback:
                    self._systemone_missing = True
                    logger.info("systemone not available; using chat fallback", extra={"question": question})
                    choice, probs, model = self._chat(question, criteria, state, instructions)
            else:
                choice, probs, model = self._chat(question, criteria, state, instructions)
        except (
            httpx.HTTPError,
            DecisionError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
        ) as exc:
            latency = int((time.monotonic() - started) * 1000)
            logger.warning(
                "decision call failed; defaulting to NEW",
                extra={"question": question, "error": f"{type(exc).__name__}: {exc}"},
            )
            return DecisionResult(
                choice=NEW,
                probabilities={},
                latency_ms=latency,
                model=self.model,
                needs_review=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        latency = int((time.monotonic() - started) * 1000)
        return DecisionResult(choice=choice, probabilities=probs, latency_ms=latency, model=model or self.model)

    # ------------------------------------------------------------------ transports
    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        url = self.base_url.rstrip("/") + path
        if self.http_client is not None:
            return self.http_client.post(url, json=body, timeout=self.timeout_s)
        with httpx.Client() as client:
            return client.post(url, json=body, timeout=self.timeout_s)

    def _systemone(
        self, question: str, criteria: dict[str, str], state: dict[str, Any] | None, instructions: str | None
    ) -> tuple[str, dict[str, float], str]:
        body = {
            "model": self.model,
            "instructions": DEFAULT_INSTRUCTIONS,
            "state": state or {},
            "questions": {question: {"type": "choice", "instructions": instructions or question, "criteria": criteria}},
        }
        resp = self._post("/v1/systemone", body)
        if resp.status_code in (404, 405):
            raise _Fallback()
        if resp.status_code >= 400:
            raise DecisionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict) or not answers:
            raise DecisionError("response has no answers")
        answer = answers.get(question)
        if answer is None and len(answers) == 1:
            answer = next(iter(answers.values()))
        if not isinstance(answer, dict):
            raise DecisionError(f"no answer for {question!r}")
        choice = str(answer.get("choice"))
        if choice not in criteria:
            raise DecisionError(f"answer {choice!r} is not one of the options")
        raw_probs = answer.get("probabilities") or {}
        probs = {str(k): float(v) for k, v in raw_probs.items() if str(k) in criteria}
        if not probs and isinstance(answer.get("confidence"), int | float):
            probs = {choice: float(answer["confidence"])}
        return choice, probs, str(data.get("model") or self.model)

    def _chat(
        self, question: str, criteria: dict[str, str], state: dict[str, Any] | None, instructions: str | None
    ) -> tuple[str, dict[str, float], str]:
        keys = list(criteria)
        if len(keys) > len(string.ascii_uppercase):
            raise DecisionError("too many options for lettered chat fallback")
        letters = {string.ascii_uppercase[i]: k for i, k in enumerate(keys)}
        lines = [f"{letter}) {key}: {criteria[key]}" for letter, key in letters.items()]
        user = (
            f"Question: {instructions or question}\n"
            f"State: {json.dumps(state or {}, default=str)}\n"
            "Options:\n" + "\n".join(lines) + "\nAnswer with the single letter of the best option."
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": DEFAULT_INSTRUCTIONS + " Reply with one letter only."},
                {"role": "user", "content": user},
            ],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": min(20, max(5, len(keys))),
        }
        resp = self._post("/v1/chat/completions", body)
        if resp.status_code >= 400:
            raise DecisionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise DecisionError("chat response has no choices")
        choice0 = choices[0]
        model = str(data.get("model") or self.model)
        probs: dict[str, float] = {}
        logprobs = (choice0.get("logprobs") or {}).get("content") or []
        if logprobs and isinstance(logprobs[0], dict):
            for entry in logprobs[0].get("top_logprobs") or []:
                if not isinstance(entry, dict):
                    continue
                letter = str(entry.get("token", "")).strip().upper()
                if letter in letters and letters[letter] not in probs:
                    probs[letters[letter]] = math.exp(float(entry["logprob"]))
        if probs:
            total = sum(probs.values())
            probs = {k: v / total for k, v in probs.items()}
            return max(probs, key=probs.get), probs, model
        message = choice0.get("message")
        content = str((message if isinstance(message, dict) else {}).get("content") or "").strip().upper()
        for ch in content:
            if ch in letters:
                return letters[ch], {}, model
            if ch.isalpha():
                break
        raise DecisionError(f"chat answer {content!r} is not an option letter")
