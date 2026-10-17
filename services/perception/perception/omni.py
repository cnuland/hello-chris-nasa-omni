"""AC-PER-3 omni client over OpenAI-compatible ``POST {base_url}/v1/chat/completions``.

Request: a system message listing the catalog class names and the observation schema; a user message with one
``image_url`` part per frame (``data:image/jpeg;base64,...``) and, when the window has audio, one ``input_audio``
part (``{"data": <b64 wav>, "format": "wav"}``); ``modalities``/``output_modalities`` = ["text"].
Response: the first JSON object of ``choices[0].message.content`` is validated against the schema. On failure, one
retry with a repair instruction; if that fails too -> OmniResult(status="invalid"). Transport/HTTP errors ->
OmniResult(status="error"). Never raises for model/server problems.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from .config import PerceptionConfig
from .media import encode_jpeg, resample_pcm16, to_wav_bytes
from .schema import load_observation_schema, parse_observation
from .types import OmniResult, Window

logger = logging.getLogger(__name__)

OMNI_AUDIO_RATE = 16000


class OmniModel(Protocol):
    def analyze(self, window: Window, *, context: str | None = None) -> OmniResult: ...


def build_system_prompt(catalog_names: Sequence[str], context: str | None = None) -> str:
    """System prompt shared by the chat and realtime clients."""
    names = ", ".join(sorted({str(n) for n in catalog_names if str(n).strip()})) or "(none provided)"
    schema = json.dumps(load_observation_schema(), separators=(",", ":"))
    prompt = (
        "You observe the inside of the International Space Station through a camera and a microphone. "
        "For the frames (in time order, frame_index starts at 0) and the audio you are given, report what you see "
        "and hear as ONE JSON object and nothing else.\n"
        f"Known catalog classes (use these exact names as object labels when they fit): {names}.\n"
        "Rules: list each distinct physical object ONCE per window, even when it is visible in several frames; "
        "frame_index = the frame where it is clearest, and bbox is [x1,y1,x2,y2] normalised to 0..1 on that frame; "
        "two objects get two entries only if they are two physically different objects. People get refs P1, P2, "
        "... with a non-biometric appearance (clothing, headset), never a name; held_by is the person ref holding "
        "an object; events are only what you actually saw or heard; transcript is the speech heard, verbatim; "
        "location_hint is the module/zone where the camera is, only if signage or speech says so (not a destination "
        "mentioned in speech), else null.\n"
        f"The JSON object must validate against this JSON schema: {schema}"
    )
    if context:
        prompt += f"\nContext: {context}"
    return prompt


def frame_data_uri(image: Any) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(encode_jpeg(image)).decode("ascii")


class OmniClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        catalog_names: Sequence[str] = (),
        http_client: httpx.Client | None = None,
        timeout_s: float = 60.0,
        max_tokens: int = 1500,
    ):
        self.base_url = base_url
        self.model = model
        self.catalog_names = list(catalog_names)
        self.http_client = http_client
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    def build_messages(self, window: Window, *, context: str | None = None) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{len(window.frames)} frames from {window.t_start.isoformat()} to {window.t_end.isoformat()}"
                    + (" with the audio of that time." if window.audio is not None else " (no audio).")
                ),
            }
        ]
        for frame in window.frames:
            parts.append({"type": "image_url", "image_url": {"url": frame_data_uri(frame.image)}})
        if window.audio is not None and len(window.audio.samples):
            samples = window.audio.samples
            if window.audio.sample_rate != OMNI_AUDIO_RATE:
                samples = resample_pcm16(samples, window.audio.sample_rate, OMNI_AUDIO_RATE)
            wav = base64.b64encode(to_wav_bytes(samples, OMNI_AUDIO_RATE)).decode("ascii")
            parts.append({"type": "input_audio", "input_audio": {"data": wav, "format": "wav"}})
        return [
            {"role": "system", "content": build_system_prompt(self.catalog_names, context)},
            {"role": "user", "content": parts},
        ]

    def _complete(self, messages: list[dict[str, Any]]) -> tuple[str, str]:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "modalities": ["text"],
            "output_modalities": ["text"],
        }
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        if self.http_client is not None:
            resp = self.http_client.post(url, json=body, timeout=self.timeout_s)
        else:
            with httpx.Client() as client:
                resp = client.post(url, json=body, timeout=self.timeout_s)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {resp.text[:200]}", request=resp.request, response=resp
            )
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content), str(data.get("model") or self.model)

    def analyze(self, window: Window, *, context: str | None = None) -> OmniResult:
        started = time.monotonic()
        messages = self.build_messages(window, context=context)
        raw, model = "", self.model
        errors: list[str] = []
        for attempt in (1, 2):
            try:
                raw, model = self._complete(messages)
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                logger.warning(
                    "omni request failed",
                    extra={"window": window.index, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"},
                )
                return OmniResult(
                    status="error",
                    raw=raw,
                    model=model,
                    attempts=attempt,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            observation, errors = parse_observation(raw)
            if observation is not None:
                return OmniResult(
                    status="ok",
                    observation=observation,
                    raw=raw,
                    model=model,
                    attempts=attempt,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            logger.info("omni reply invalid", extra={"window": window.index, "attempt": attempt, "errors": errors[:5]})
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply was not a valid JSON object for the observation schema ("
                        + "; ".join(errors[:5])
                        + "). Reply again with ONLY the corrected JSON object, no prose "
                        "and no code fences."
                    ),
                },
            ]
        return OmniResult(
            status="invalid",
            raw=raw,
            model=model,
            attempts=2,
            latency_ms=int((time.monotonic() - started) * 1000),
            error="; ".join(errors[:5]),
        )


def create_omni_client(
    config: PerceptionConfig, *, catalog_names: Sequence[str] = (), http_client: httpx.Client | None = None
) -> OmniModel:
    """OmniClient by default; RealtimeOmniClient when ``config.omni_realtime`` (flag OMNI_REALTIME)."""
    if not config.omni_url:
        raise ValueError("OMNI_URL is not configured")
    if config.omni_realtime:
        from .realtime import RealtimeOmniClient

        return RealtimeOmniClient(config.omni_url, model=config.omni_model, catalog_names=catalog_names)
    return OmniClient(config.omni_url, model=config.omni_model, catalog_names=catalog_names, http_client=http_client)
