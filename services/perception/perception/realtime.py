"""AC-PER-4 optional realtime omni client over ``ws(s)://.../v1/realtime?duplex=0`` (vLLM-Omni turn-based).

Per window: ``session.update`` (text output), one ``conversation.item.create`` user message carrying ``input_image``
parts (data URIs), ``input_audio_buffer.append`` with base64 PCM16 **24 kHz** mono audio (resampled from 16 kHz),
``input_audio_buffer.commit``, ``response.create`` with text output; collect ``response.output_text.delta`` until
``response.done``; parse/validate like OmniClient. Server ``error`` events / connection failures -> status "error".
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from .media import resample_pcm16
from .omni import build_system_prompt, frame_data_uri
from .schema import parse_observation
from .types import OmniResult, Window

logger = logging.getLogger(__name__)

REALTIME_SAMPLE_RATE = 24000
APPEND_SAMPLES = REALTIME_SAMPLE_RATE  # one second of audio per input_audio_buffer.append
_TEXT_DELTAS = ("response.output_text.delta", "response.text.delta")


class RealtimeOmniClient:
    def __init__(self, ws_url: str, *, model: str, catalog_names: Sequence[str] = (), timeout_s: float = 60.0):
        """``ws_url`` is the server root (``ws://host:port``); the client connects to ``/v1/realtime?duplex=0``.
        An ``http(s)://`` root is converted to ``ws(s)://``."""
        self.ws_url = ws_url
        self.model = model
        self.catalog_names = list(catalog_names)
        self.timeout_s = timeout_s

    @property
    def endpoint(self) -> str:
        root = self.ws_url.rstrip("/")
        if root.startswith("https://"):
            root = "wss://" + root[len("https://") :]
        elif root.startswith("http://"):
            root = "ws://" + root[len("http://") :]
        root = root.removesuffix("/v1")
        return root + "/v1/realtime?duplex=0"

    def _events(self, window: Window, context: str | None) -> list[dict[str, Any]]:
        session = {
            "model": self.model,
            "modalities": ["text"],
            "output_modalities": ["text"],
            "instructions": build_system_prompt(self.catalog_names, context),
        }
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    f"{len(window.frames)} frames from {window.t_start.isoformat()} to {window.t_end.isoformat()}."
                ),
            }
        ]
        content += [{"type": "input_image", "image_url": frame_data_uri(f.image)} for f in window.frames]
        events: list[dict[str, Any]] = [
            {"type": "session.update", "session": session},
            {"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": content}},
        ]
        if window.audio is not None and len(window.audio.samples):
            pcm = resample_pcm16(window.audio.samples, window.audio.sample_rate, REALTIME_SAMPLE_RATE)
            pcm = np.asarray(pcm, dtype="<i2")
            for start in range(0, len(pcm), APPEND_SAMPLES):
                events.append(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm[start : start + APPEND_SAMPLES].tobytes()).decode(),
                    }
                )
            events.append({"type": "input_audio_buffer.commit"})
        events.append({"type": "response.create", "response": {"modalities": ["text"], "output_modalities": ["text"]}})
        return events

    def analyze(self, window: Window, *, context: str | None = None) -> OmniResult:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect

        started = time.monotonic()
        deadline = started + self.timeout_s
        text_parts: list[str] = []
        final_text: str | None = None

        def result(
            status: str, raw: str = "", error: str | None = None, observation: dict[str, Any] | None = None
        ) -> OmniResult:
            return OmniResult(
                status=status,
                observation=observation,
                raw=raw,
                model=self.model,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )

        try:
            with connect(
                self.endpoint, open_timeout=self.timeout_s, close_timeout=2, max_size=None, compression=None
            ) as ws:
                for event in self._events(window, context):
                    ws.send(json.dumps(event))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return result("error", "".join(text_parts), "realtime response timed out")
                    message = json.loads(ws.recv(timeout=remaining))
                    etype = message.get("type")
                    if etype in _TEXT_DELTAS:
                        text_parts.append(str(message.get("delta", "")))
                    elif etype in ("response.output_text.done", "response.text.done"):
                        final_text = message.get("text")
                    elif etype == "error":
                        err = message.get("error") or {}
                        logger.warning("realtime server error", extra={"error": err})
                        return result("error", "".join(text_parts), f"server error: {err.get('message', err)}")
                    elif etype == "response.done":
                        break
        except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
            logger.warning(
                "realtime omni failed", extra={"endpoint": self.endpoint, "error": f"{type(exc).__name__}: {exc}"}
            )
            return result("error", "".join(text_parts), f"{type(exc).__name__}: {exc}")
        raw = "".join(text_parts) or (final_text or "")
        observation, errors = parse_observation(raw)
        if observation is None:
            return result("invalid", raw, "; ".join(errors[:5]))
        return result("ok", raw, observation=observation)
