"""AC-PER-18 public replay interface: perception.replay.run(source, *, camera_id, start_time, api, omni, detector,
embedder, decision, asr, storage, config) -> ReplayStats; CLI `python -m perception replay --source PATH --camera ID`;
source = video path or iterable of (timestamp, frame, audio|None); injectable collaborators with fakes in
perception.fakes."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from support import BUCKET, CAMERA_LOCATIONS, T0, FakeInventory, obj, observation, sine

import perception.replay
from perception.config import PerceptionConfig
from perception.fakes import FakeASR, FakeDecision, FakeDetector, FakeEmbedder, FakeOmni, MemoryStorage, make_frame
from perception.types import AsrResult, AudioChunk, DecisionResult, OmniResult, ReplayStats, Window

BOX = (0.2, 0.2, 0.5, 0.6)
SERVICE_DIR = Path(__file__).resolve().parents[1]


def cfg(**kw) -> PerceptionConfig:
    return PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET, **kw)


def synthetic(n: int, *, step_s: float = 1.0, audio_every: int | None = None):
    items = []
    for i in range(n):
        audio = sine(5.0) if audio_every and i % audio_every == 0 else None
        items.append((T0 + timedelta(seconds=i * step_s), make_frame([("laptop-A", BOX)]), audio))
    return items


def run(source, *, inv=None, omni=None, asr=None, config=None, storage=None, detector=None):
    inv = inv or FakeInventory()
    stats = perception.replay.run(
        source, camera_id="cam-lab1-fwd", start_time=T0, api=inv,
        omni=omni or FakeOmni([observation([obj("laptop", BOX)])] * 50),
        detector=detector or FakeDetector({"laptop-A": "laptop"}), embedder=FakeEmbedder(), decision=FakeDecision(),
        asr=asr, storage=storage or MemoryStorage(BUCKET), config=config or cfg())
    return stats, inv


# ------------------------------------------------------------------------------------------------ replay.run
def test_ac_per_18_iterable_source_runs_through_pipeline():
    omni = FakeOmni([observation([obj("laptop", BOX)])] * 10)
    stats, inv = run(synthetic(9), omni=omni)
    assert isinstance(stats, ReplayStats)
    assert stats.frames == 9
    assert stats.windows == 3  # default stride 4 s: t=0, 4, 8
    assert stats.posted == 3 and stats.invalid == 0 and stats.errors == 0
    assert len(omni.calls) == 3 and len(inv.posted) == 3


def test_ac_per_18_same_object_across_windows_is_one_entity():
    stats, inv = run(synthetic(9))
    assert len(inv.live()) == 1
    assert sum(len(e["sightings"]) for e in inv.entities.values()) == stats.windows


def test_ac_per_18_window_settings_from_config():
    stats, _ = run(synthetic(5), config=cfg(window_frames=1, stride_s=1.0))
    assert stats.windows == 5


def test_ac_per_18_trailing_frames_are_flushed():
    stats, _ = run(synthetic(10))  # windows at 0,4,8 + flush for 9
    assert stats.windows == 4


def test_ac_per_18_omni_receives_window_frames():
    omni = FakeOmni([observation([])] * 5)
    run(synthetic(5), omni=omni)
    assert all(isinstance(w, Window) for w in omni.calls)
    assert [len(w.frames) for w in omni.calls] == [1, 5]
    assert all(f.image.shape[1] <= 448 for w in omni.calls for f in w.frames)


def test_ac_per_18_detector_called_with_catalog_class_names():
    det = FakeDetector({"laptop-A": "laptop"})
    run(synthetic(1), detector=det)
    assert det.calls
    _, labels = det.calls[0]
    assert {"drill", "laptop", "cargo transfer bag"} <= set(labels)


def test_ac_per_18_audio_ndarray_items_are_accepted():
    asr = FakeASR(["the laptop is on the rack"])
    stats, _inv = run(synthetic(1, audio_every=1), asr=asr)
    assert stats.audio_chunks == 1 and stats.asr_calls == 1
    chunk, _ = asr.calls[0]
    assert isinstance(chunk, AudioChunk) and chunk.timestamp == T0 and chunk.sample_rate == 16000


def test_ac_per_18_float_audio_is_converted_to_pcm16():
    asr = FakeASR(["hello"])
    item = (T0, make_frame([("laptop-A", BOX)]), (sine(5.0).astype(np.float32) / 32768.0))
    run([item], asr=asr)
    chunk, _ = asr.calls[0]
    assert chunk.samples.dtype == np.int16 and np.abs(chunk.samples).max() > 4000


def test_ac_per_18_audio_chunk_items_are_accepted():
    asr = FakeASR(["hello"])
    item = (T0, make_frame([("laptop-A", BOX)]), AudioChunk(timestamp=T0, samples=sine(5.0)))
    stats, _ = run([item], asr=asr)
    assert stats.asr_calls == 1


def test_ac_per_18_invalid_omni_windows_counted_and_not_written():
    omni = FakeOmni(["this is not json", observation([obj("laptop", BOX)])])
    stats, inv = run(synthetic(5), omni=omni)
    assert stats.windows == 2 and stats.invalid == 1 and stats.posted == 1
    assert len(inv.posted) == 1


def test_ac_per_18_post_errors_counted_not_raised():
    stats, _ = run(synthetic(5), inv=FakeInventory(fail_post=True))
    assert stats.errors == 2 and stats.posted == 0


def test_ac_per_18_video_path_source(sample_video):
    stats, inv = run(str(sample_video), omni=FakeOmni([observation([])] * 10), asr=FakeASR(["a", "b", "c"]))
    assert 5 <= stats.frames <= 7
    assert stats.windows >= 2
    assert stats.audio_chunks == 2
    assert stats.posted == stats.windows
    w = inv.posted[0]["window"]
    assert w["camera_id"] == "cam-lab1-fwd"


def test_ac_per_18_decisions_counted():
    inv = FakeInventory()
    from support import blend, random_unit

    inv.add_entity(catalog_id="laptop", embedding=blend(FakeEmbedder().vector_for_object("laptop-A"),
                                                        random_unit(1), 0.75),
                   last_location="NOD2", last_seen_at=T0 - timedelta(days=1))
    stats, _ = run(synthetic(1), inv=inv)
    assert stats.decisions == len(inv.decisions)


# ------------------------------------------------------------------------------------------------ fakes
def test_ac_per_18_fake_omni_scripted_sequence_and_validation():
    w = Window(t_start=T0, t_end=T0, frames=[])
    good = observation([obj("laptop", BOX)])
    bad = {"scene_summary": "missing keys"}
    omni = FakeOmni([good, bad, json.dumps(good), None])
    r = [omni.analyze(w) for _ in range(5)]
    assert [x.status for x in r[:4]] == ["ok", "invalid", "ok", "error"]
    assert r[0].observation == good and r[2].observation == good
    assert r[4].ok and r[4].observation["objects"] == []
    assert len(omni.calls) == 5
    assert all(isinstance(x, OmniResult) for x in r)


def test_ac_per_18_fake_decision_script_forms():
    seq = FakeDecision([{"choice": "a", "probabilities": {"a": 0.9}}, DecisionResult(choice="b",
                                                                                      probabilities={"b": 0.7})])
    assert seq.choose("q", ["a", "b"]).choice == "a"
    assert seq.choose("q", ["a", "b"]).choice == "b"
    exhausted = seq.choose("q", ["a", "b"])
    assert exhausted.choice == "NEW" and exhausted.needs_review is True
    failing = FakeDecision(lambda q, o, s: RuntimeError("down")).choose("q", ["a"])
    assert failing.choice == "NEW" and failing.probabilities == {} and failing.needs_review
    assert FakeDecision().choose("q", ["a"]).needs_review is True
    d = FakeDecision(lambda q, o, s: {"choice": "a", "probabilities": {"a": 1.0}})
    res = d.choose("speech_act", ["a"], state={"x": 1})
    assert res.model == "fake-decision" and d.calls == [("speech_act", ["a"], {"x": 1})]


def test_ac_per_18_fake_asr_script_forms():
    chunk = AudioChunk(timestamp=T0, samples=sine(1.0))
    asr = FakeASR(["one", None])
    a, b, c = (asr.transcribe(chunk, context="ctx") for _ in range(3))
    assert isinstance(a, AsrResult) and a.ok and a.text == "one"
    assert b.ok is False
    assert c.ok and c.text == ""
    assert asr.calls[0] == (chunk, "ctx")
    assert FakeASR(available=False).transcribe(chunk).ok is False
    assert FakeASR(lambda ch: f"{ch.duration_s:.0f}s").transcribe(chunk).text == "1s"


# ------------------------------------------------------------------------------------------------ config
def test_ac_per_18_config_defaults_match_spec():
    c = PerceptionConfig()
    assert (c.fps, c.audio_chunk_s, c.audio_sample_rate) == (1.0, 5.0, 16000)
    assert (c.window_frames, c.stride_s, c.long_side) == (6, 4.0, 448)
    assert (c.min_score, c.label_floor, c.min_transit_s) == (0.15, 0.55, 20.0)
    assert (c.assign_p, c.ask_p, c.decision_accept) == (0.85, 0.5, 0.6)
    assert (c.speech_max_confidence, c.speech_confirm_s, c.hallucination_repeats) == (0.6, 600.0, 3)


def test_ac_per_18_config_from_env(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_URL", "http://inventory-api:8080")
    monkeypatch.setenv("OMNI_URL", "http://omni:8000")
    monkeypatch.setenv("DECISION_URL", "http://decision:8000")
    monkeypatch.setenv("ASR_URL", "http://asr:8000")
    monkeypatch.setenv("PERCEPTION_FPS", "2")
    monkeypatch.setenv("PERCEPTION_STRIDE_S", "3.5")
    monkeypatch.setenv("PERCEPTION_WINDOW_FRAMES", "8")
    monkeypatch.setenv("CAMERA_LOCATIONS", "cam-x=COL1,cam-y=JPM1")
    c = PerceptionConfig.from_env()
    assert c.inventory_api_url == "http://inventory-api:8080" and c.omni_url == "http://omni:8000"
    assert c.decision_url == "http://decision:8000" and c.asr_url == "http://asr:8000"
    assert (c.fps, c.stride_s, c.window_frames) == (2.0, 3.5, 8)
    assert c.camera_locations["cam-x"] == "COL1" and c.camera_locations["cam-y"] == "JPM1"


def test_ac_per_18_config_from_env_defaults_without_vars(monkeypatch):
    for k in ("ASR_URL", "OMNI_REALTIME", "PERCEPTION_FPS", "CAMERA_LOCATIONS"):
        monkeypatch.delenv(k, raising=False)
    c = PerceptionConfig.from_env()
    assert c.asr_url is None and c.omni_realtime is False and c.fps == 1.0
    assert c.camera_locations["cam-lab1-fwd"] == "LAB1"


def test_ac_per_18_config_from_env_rejects_garbage(monkeypatch):
    from perception.config import ConfigError

    monkeypatch.setenv("PERCEPTION_FPS", "fast")
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env()


# ------------------------------------------------------------------------------------------------ CLI
def _cli(*args):
    return subprocess.run([sys.executable, "-m", "perception", *args], check=False, capture_output=True, text=True,
                          timeout=60, cwd=SERVICE_DIR)


def test_ac_per_18_cli_help_lists_replay_options():
    proc = _cli("replay", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--source" in proc.stdout and "--camera" in proc.stdout


def test_ac_per_18_cli_requires_source_and_camera():
    proc = _cli("replay")
    assert proc.returncode == 2
    assert "--source" in proc.stderr


def test_ac_per_18_cli_main_calls_replay_run(monkeypatch, sample_video, capsys):
    import perception.__main__ as cli

    captured = {}

    def fake_run(source, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return ReplayStats(frames=6, windows=2, posted=2)

    monkeypatch.setattr(perception.replay, "run", fake_run)
    for k, v in {"INVENTORY_API_URL": "http://inventory-api:8080", "OMNI_URL": "http://omni:8000",
                 "DECISION_URL": "http://decision:8000", "S3_BUCKET": "inventory"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ASR_URL", raising=False)
    rc = cli.main(["replay", "--source", str(sample_video), "--camera", "cam-lab1-fwd"])
    assert rc == 0
    assert captured["source"] == str(sample_video)
    assert captured["camera_id"] == "cam-lab1-fwd"
    for k in ("api", "omni", "detector", "embedder", "decision", "storage", "config", "start_time"):
        assert captured.get(k) is not None, k
    assert captured["asr"] is None, "no ASR_URL -> no ASR client"
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1])["windows"] == 2


def test_ac_per_18_cli_unknown_command_is_usage_error():
    proc = _cli("explode")
    assert proc.returncode == 2


def test_ac_per_18_containerfile_runs_replay_cli():
    text = (SERVICE_DIR / "Containerfile").read_text()
    assert "ubi9/python-311" in text
    assert "python -m perception replay" in text
    assert "observation.schema.json" in text
