"""AC-PER-3 Omni client: OpenAI-compatible /v1/chat/completions with image_url data URIs + input_audio (wav), a system
prompt with catalog class names and the observation schema, text-only output; first JSON object parsed and validated
against contracts/schemas/observation.schema.json; one repair retry; else an `invalid` result."""
from __future__ import annotations

import base64
import io
import json
import wave

import httpx
import numpy as np
import pytest
from support import CATALOG, SCHEMA_PATH, obj, observation, sine, ts

from perception.omni import OmniClient
from perception.schema import extract_first_json_object, load_observation_schema, validate_observation
from perception.types import AudioChunk, Keyframe, Window

CLASS_NAMES = sorted({c["class_name"] for c in CATALOG})
VALID = observation([obj("drill", [0.1, 0.1, 0.4, 0.5])], transcript="putting the drill away")


def window(n_frames: int = 3, audio: bool = True) -> Window:
    frames = [Keyframe(timestamp=ts(i), image=np.full((252, 448, 3), 30 * i, dtype=np.uint8), pts=float(i), index=i)
              for i in range(n_frames)]
    chunk = AudioChunk(timestamp=ts(0), samples=sine(5.0)) if audio else None
    return Window(t_start=frames[0].timestamp, t_end=frames[-1].timestamp, frames=frames, audio=chunk)


def reply(content: str, model: str = "qwen-omni") -> httpx.Response:
    return httpx.Response(200, json={"id": "x", "model": model, "object": "chat.completion",
                                     "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                  "finish_reason": "stop"}]})


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if self.responses else reply(json.dumps(VALID))
        if isinstance(r, Exception):
            raise r
        return r

    def body(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].content)


def client(rec: Recorder) -> OmniClient:
    return OmniClient("http://omni.test", model="qwen-omni", catalog_names=CLASS_NAMES,
                      http_client=httpx.Client(transport=httpx.MockTransport(rec)))


def user_parts(body: dict) -> list[dict]:
    parts = []
    for m in body["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            parts.extend(m["content"])
    return parts


def system_text(body: dict) -> str:
    sys_msgs = [m for m in body["messages"] if m["role"] == "system"]
    assert sys_msgs, "a system prompt is required"
    c = sys_msgs[0]["content"]
    return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c)


# ------------------------------------------------------------------------------------------------ request shape
def test_ac_per_03_posts_to_chat_completions():
    rec = Recorder(reply(json.dumps(VALID)))
    client(rec).analyze(window())
    assert len(rec.requests) == 1
    assert rec.requests[0].method == "POST"
    assert rec.requests[0].url.path == "/v1/chat/completions"
    assert rec.body()["model"] == "qwen-omni"


def test_ac_per_03_images_sent_as_jpeg_data_uris_one_per_frame():
    rec = Recorder(reply(json.dumps(VALID)))
    client(rec).analyze(window(n_frames=4))
    images = [p for p in user_parts(rec.body()) if p.get("type") == "image_url"]
    assert len(images) == 4
    for p in images:
        url = p["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1])[:2] == b"\xff\xd8"


def test_ac_per_03_audio_sent_as_input_audio_wav():
    rec = Recorder(reply(json.dumps(VALID)))
    w = window()
    client(rec).analyze(w)
    audio = [p for p in user_parts(rec.body()) if p.get("type") == "input_audio"]
    assert len(audio) == 1
    assert audio[0]["input_audio"]["format"] == "wav"
    raw = base64.b64decode(audio[0]["input_audio"]["data"])
    with wave.open(io.BytesIO(raw)) as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2
        assert wf.getnframes() == len(w.audio.samples)


def test_ac_per_03_no_audio_part_when_window_has_no_audio():
    rec = Recorder(reply(json.dumps(VALID)))
    client(rec).analyze(window(audio=False))
    assert not [p for p in user_parts(rec.body()) if p.get("type") == "input_audio"]


def test_ac_per_03_system_prompt_lists_catalog_class_names():
    rec = Recorder(reply(json.dumps(VALID)))
    client(rec).analyze(window())
    text = system_text(rec.body())
    for name in ("drill", "cargo transfer bag", "multimeter", "astrobee robot", "food package"):
        assert name in text, name


def test_ac_per_03_system_prompt_contains_observation_schema():
    rec = Recorder(reply(json.dumps(VALID)))
    client(rec).analyze(window())
    text = system_text(rec.body())
    for key in ("scene_summary", "location_hint", "objects", "people", "events", "transcript", "audio_events",
                "frame_index", "held_by"):
        assert key in text, key


def test_ac_per_03_text_only_output_modalities():
    rec = Recorder(reply(json.dumps(VALID)))
    client(rec).analyze(window())
    body = rec.body()
    present = [k for k in ("modalities", "output_modalities") if k in body]
    assert present, "modalities/output_modalities must be set"
    assert all(body[k] == ["text"] for k in present)


# ------------------------------------------------------------------------------------------------ parsing
def test_ac_per_03_valid_reply_is_ok_and_validated():
    rec = Recorder(reply(json.dumps(VALID)))
    res = client(rec).analyze(window())
    assert res.ok and res.status == "ok"
    assert res.observation == VALID
    assert res.attempts == 1
    assert res.model == "qwen-omni"
    assert json.dumps(VALID) in res.raw


def test_ac_per_03_parses_first_json_object_inside_prose_and_fences():
    text = "Here is the observation:\n```json\n" + json.dumps(VALID) + "\n```\nAnything else?"
    res = client(Recorder(reply(text))).analyze(window())
    assert res.ok and res.observation == VALID


def test_ac_per_03_first_of_two_json_objects_wins():
    other = observation(scene_summary="second")
    text = json.dumps(VALID) + "\n" + json.dumps(other)
    res = client(Recorder(reply(text))).analyze(window())
    assert res.ok and res.observation["scene_summary"] == VALID["scene_summary"]


def test_ac_per_03_retries_once_with_repair_instruction_on_invalid_json():
    rec = Recorder(reply("Sure! {\"scene_summary\": \"broken"), reply(json.dumps(VALID)))
    res = client(rec).analyze(window())
    assert len(rec.requests) == 2
    assert res.ok and res.attempts == 2
    first, second = rec.body(0), rec.body(1)
    assert len(second["messages"]) > len(first["messages"])
    repair = second["messages"][-1]
    assert repair["role"] == "user"
    text = repair["content"] if isinstance(repair["content"], str) else json.dumps(repair["content"])
    assert "JSON" in text


def test_ac_per_03_schema_invalid_reply_also_triggers_repair():
    bad = dict(VALID)
    del bad["audio_events"]
    rec = Recorder(reply(json.dumps(bad)), reply(json.dumps(VALID)))
    res = client(rec).analyze(window())
    assert len(rec.requests) == 2 and res.ok


def test_ac_per_03_two_failures_give_invalid_result_and_stop():
    rec = Recorder(reply("no json here"), reply("still no json"), reply(json.dumps(VALID)))
    res = client(rec).analyze(window())
    assert len(rec.requests) == 2, "retry exactly once"
    assert res.status == "invalid" and not res.ok
    assert res.observation is None
    assert res.attempts == 2
    assert "still no json" in res.raw


@pytest.mark.parametrize("mutate", [
    lambda o: o["objects"][0].update(confidence=1.5),
    lambda o: o["objects"][0].update(bbox=[0.1, 0.2, 0.3]),
    lambda o: o.update(unexpected="field"),
    lambda o: o["objects"][0].pop("label"),
    lambda o: o.update(people=[{"ref": "crew-1", "appearance": "blue"}]),
    lambda o: o.update(events=[{"type": "teleported", "object_label": "drill", "confidence": 0.5}]),
])
def test_ac_per_03_schema_violations_are_invalid(mutate):
    bad = json.loads(json.dumps(VALID))
    mutate(bad)
    res = client(Recorder(reply(json.dumps(bad)), reply(json.dumps(bad)))).analyze(window())
    assert res.status == "invalid" and res.observation is None


def test_ac_per_03_http_error_is_reported_not_raised():
    res = client(Recorder(httpx.Response(500, text="boom"), httpx.Response(500, text="boom"))).analyze(window())
    assert not res.ok and res.observation is None
    assert res.status in ("error", "invalid")


def test_ac_per_03_transport_error_is_reported_not_raised():
    rec = Recorder(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    res = client(rec).analyze(window())
    assert not res.ok and res.status == "error"


# ------------------------------------------------------------------------------------------------ schema helpers
def test_ac_per_03_bundled_schema_matches_contract():
    assert load_observation_schema() == json.loads(SCHEMA_PATH.read_text())


def test_ac_per_03_extract_first_json_object_edge_cases():
    assert extract_first_json_object('noise {"a": "} tricky {", "b": {"c": 1}} tail') == {"a": "} tricky {",
                                                                                           "b": {"c": 1}}
    assert extract_first_json_object("no braces") is None
    assert extract_first_json_object("{broken") is None
    assert extract_first_json_object('[1, 2] then {"x": 1}') == {"x": 1}
    assert extract_first_json_object("{bad} then {\"ok\": true}") == {"ok": True}


def test_ac_per_03_validate_observation_reports_errors():
    assert validate_observation(VALID) == []
    assert validate_observation({"objects": []})
    assert validate_observation("not an object")
