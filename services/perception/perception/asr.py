"""AC-PER-15 ASR client: OpenAI-compatible ``POST {base_url}/v1/audio/transcriptions`` (Qwen3-ASR-1.7B on vLLM).

Multipart fields: ``file`` (WAV, 16 kHz mono), ``model``, ``prompt`` = context built from catalog names and
module codes (ISS vocabulary biasing), ``response_format=json``. Failures -> AsrResult(ok=False). Never raises.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from .media import resample_pcm16, to_wav_bytes
from .types import AsrResult, AudioChunk

logger = logging.getLogger(__name__)
ASR_SAMPLE_RATE = 16000


class AsrModel(Protocol):
    def transcribe(self, chunk: AudioChunk, *, context: str = "") -> AsrResult: ...


def build_asr_context(
    catalog: Sequence[dict[str, Any]], locations: Sequence[dict[str, Any]], max_chars: int = 2000
) -> str:
    """Comma-separated vocabulary: catalog class/display names and synonyms, module codes and module display
    names. At most ``max_chars`` characters (terms are never cut in half; the most useful terms come first)."""
    terms: list[str] = []
    modules = [loc for loc in locations if loc.get("kind") in ("module", "segment")]
    terms += [loc["location_id"] for loc in modules]
    terms += [c["class_name"] for c in catalog if c.get("class_name")]
    for c in catalog:
        terms += [s for s in (c.get("synonyms") or []) if s]
    for loc in modules:
        name = loc.get("display_name") or ""
        terms += [part.strip() for part in name.replace("(", ",").replace(")", ",").split(",") if part.strip()]
    terms += [c["display_name"] for c in catalog if c.get("display_name")]
    seen: set[str] = set()
    out = ""
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        candidate = term if not out else f"{out}, {term}"
        if len(candidate) > max_chars:
            continue
        seen.add(key)
        out = candidate
    return out


class AsrClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "Qwen/Qwen3-ASR-1.7B",
        http_client: httpx.Client | None = None,
        timeout_s: float = 30.0,
        language: str | None = "en",
    ):
        self.base_url = base_url
        self.model = model
        self.http_client = http_client
        self.timeout_s = timeout_s
        self.language = language

    def transcribe(self, chunk: AudioChunk, *, context: str = "") -> AsrResult:
        samples = chunk.samples
        if chunk.sample_rate != ASR_SAMPLE_RATE:
            samples = resample_pcm16(samples, chunk.sample_rate, ASR_SAMPLE_RATE)
        files = {"file": ("audio.wav", to_wav_bytes(samples, ASR_SAMPLE_RATE), "audio/wav")}
        data: dict[str, str] = {"model": self.model, "response_format": "json"}
        if context:
            data["prompt"] = context
        if self.language:
            data["language"] = self.language
        url = self.base_url.rstrip("/") + "/v1/audio/transcriptions"
        started = time.monotonic()
        try:
            client = self.http_client or httpx.Client()
            try:
                resp = client.post(url, files=files, data=data, timeout=self.timeout_s)
            finally:
                if self.http_client is None:
                    client.close()
            latency = int((time.monotonic() - started) * 1000)
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            body: Any = resp.json()
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str):
                raise TypeError("response has no 'text'")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("ASR call failed", extra={"url": url, "error": str(exc)})
            return AsrResult(
                text="", ok=False, model=self.model, error=str(exc), latency_ms=int((time.monotonic() - started) * 1000)
            )
        return AsrResult(text=text.strip(), ok=True, model=self.model, latency_ms=latency)
