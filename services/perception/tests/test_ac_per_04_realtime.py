"""AC-PER-4 optional realtime client (flag) over /v1/realtime?duplex=0: input_image items, audio appended to the input
buffer as 24 kHz PCM16, commit, response.create with text output, text deltas collected. Tested against a fake
WebSocket server."""
from __future__ import annotations

import base64
import json
import threading

import numpy as np
import pytest
from support import obj, observation, sine, ts
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from perception.config import PerceptionConfig
from perception.omni import OmniClient, create_omni_client
from perception.realtime import RealtimeOmniClient
from perception.types import AudioChunk, Keyframe, Window

VALID = observation([obj("laptop", [0.2, 0.2, 0.6, 0.6])], scene_summary="laptop on the rack")


class FakeRealtimeServer:
    """Records every client event; on response.create streams the scripted text as response.output_text.delta."""

    def __init__(self, text: str, *, mode: str = "ok"):
        self.text = text
        self.mode = mode
        self.events: list[dict] = []
        self.paths: list[str] = []
        self._server = serve(self._handler, "127.0.0.1", 0, max_size=None, compression=None)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    def _send(self, ws, **event):
        ws.send(json.dumps(event))

    def _handler(self, ws):
        self.paths.append(ws.request.path)
        self._send(ws, type="session.created", session={"id": "sess_1"})
        try:
            for message in ws:
                event = json.loads(message)
                self.events.append(event)
                t = event.get("type")
                if t == "session.update":
                    self._send(ws, type="session.updated", session=event.get("session", {}))
                elif t == "response.create":
                    if self.mode == "error":
                        self._send(ws, type="error", error={"type": "server_error", "message": "boom"})
                        continue
                    self._send(ws, type="response.created", response={"id": "resp_1"})
                    for i in range(0, len(self.text), 7):
                        self._send(ws, type="response.output_text.delta", response_id="resp_1",
                                   delta=self.text[i:i + 7])
                    self._send(ws, type="response.output_text.done", response_id="resp_1", text=self.text)
                    self._send(ws, type="response.done", response={"id": "resp_1", "status": "completed"})
        except ConnectionClosed:
            return


def window(n_frames: int = 2, seconds: float = 5.0) -> Window:
    frames = [Keyframe(timestamp=ts(i), image=np.full((100, 160, 3), 50 + i, dtype=np.uint8), pts=float(i), index=i)
              for i in range(n_frames)]
    return Window(t_start=ts(0), t_end=ts(n_frames - 1), frames=frames,
                  audio=AudioChunk(timestamp=ts(0), samples=sine(seconds)))


def run(server: FakeRealtimeServer, w: Window | None = None):
    c = RealtimeOmniClient(server.url, model="qwen3-omni", catalog_names=["laptop", "drill"], timeout_s=10)
    return c.analyze(w or window())


def _image_parts(events):
    parts = []
    for e in events:
        if e.get("type") == "conversation.item.create":
            parts.extend(p for p in e["item"].get("content", []) if p.get("type") == "input_image")
    return parts


def test_ac_per_04_connects_to_realtime_duplex0():
    with FakeRealtimeServer(json.dumps(VALID)) as srv:
        run(srv)
    assert srv.paths and srv.paths[0].startswith("/v1/realtime")
    assert "duplex=0" in srv.paths[0]


def test_ac_per_04_sends_frames_as_input_image_items():
    with FakeRealtimeServer(json.dumps(VALID)) as srv:
        run(srv, window(n_frames=3))
    parts = _image_parts(srv.events)
    assert len(parts) == 3
    assert all(p["image_url"].startswith("data:image/jpeg;base64,") for p in parts)


def test_ac_per_04_appends_audio_as_24khz_pcm16():
    with FakeRealtimeServer(json.dumps(VALID)) as srv:
        run(srv, window(seconds=5.0))
    appends = [e for e in srv.events if e.get("type") == "input_audio_buffer.append"]
    assert appends, "audio must be appended to the input buffer"
    pcm = b"".join(base64.b64decode(e["audio"]) for e in appends)
    n_samples = len(pcm) // 2
    assert abs(n_samples - 5 * 24000) <= 240, n_samples  # 16 kHz -> 24 kHz resample
    samples = np.frombuffer(pcm, dtype="<i2")
    assert np.abs(samples).max() > 1000, "audio content must survive the resample"


def test_ac_per_04_event_order_append_commit_then_response_create():
    with FakeRealtimeServer(json.dumps(VALID)) as srv:
        run(srv)
    types = srv.types()
    assert "input_audio_buffer.commit" in types and "response.create" in types
    last_append = max(i for i, t in enumerate(types) if t == "input_audio_buffer.append")
    commit = types.index("input_audio_buffer.commit")
    create = types.index("response.create")
    images = max(i for i, t in enumerate(types) if t == "conversation.item.create")
    assert last_append < commit < create
    assert images < create


def test_ac_per_04_requests_text_output_only():
    with FakeRealtimeServer(json.dumps(VALID)) as srv:
        run(srv)
    create = next(e for e in srv.events if e.get("type") == "response.create")
    resp = create.get("response", {})
    mods = resp.get("output_modalities") or resp.get("modalities")
    if mods is None:  # may be set at session level instead
        upd = [e for e in srv.events if e.get("type") == "session.update"]
        assert upd, "text output must be requested in response.create or session.update"
        mods = upd[-1]["session"].get("output_modalities") or upd[-1]["session"].get("modalities")
    assert mods == ["text"]


def test_ac_per_04_collects_text_deltas_into_validated_observation():
    with FakeRealtimeServer("Observation: " + json.dumps(VALID)) as srv:
        res = run(srv)
    assert res.ok and res.observation == VALID
    assert json.dumps(VALID) in res.raw


def test_ac_per_04_server_error_event_gives_error_result():
    with FakeRealtimeServer(json.dumps(VALID), mode="error") as srv:
        res = run(srv)
    assert not res.ok and res.observation is None


def test_ac_per_04_invalid_text_gives_non_ok_result():
    with FakeRealtimeServer("no json, sorry") as srv:
        res = run(srv)
    assert not res.ok and res.observation is None


def test_ac_per_04_unreachable_server_gives_error_result():
    c = RealtimeOmniClient("ws://127.0.0.1:9", model="m", timeout_s=2)
    res = c.analyze(window())
    assert res.status == "error"


def test_ac_per_04_flag_selects_realtime_client():
    cfg = PerceptionConfig(omni_url="http://omni.test:8000", omni_realtime=True)
    assert isinstance(create_omni_client(cfg, catalog_names=["drill"]), RealtimeOmniClient)
    cfg_default = PerceptionConfig(omni_url="http://omni.test:8000")
    assert isinstance(create_omni_client(cfg_default, catalog_names=["drill"]), OmniClient)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes"])
def test_ac_per_04_flag_from_env(monkeypatch, raw):
    monkeypatch.setenv("OMNI_URL", "http://omni.test:8000")
    monkeypatch.setenv("OMNI_REALTIME", raw)
    assert PerceptionConfig.from_env().omni_realtime is True
