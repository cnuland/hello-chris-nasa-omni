"""AC-PER-5/6 device selection (cluster integration 2026-10-17): OWLv2 on CPU measured ~14 s per frame on 4 threads,
about 20x slower than real time, so the fast path must be able to run on a GPU. PERCEPTION_DEVICE = cpu | cuda | auto."""
import pytest

from perception.config import ConfigError, PerceptionConfig


def test_device_defaults_to_cpu():
    assert PerceptionConfig.from_env({}).device == "cpu"


@pytest.mark.parametrize("raw,expected", [("cpu", "cpu"), ("cuda", "cuda"), ("CUDA", "cuda"), (" auto ", "auto"),
                                          ("cuda:0", "cuda:0")])
def test_device_parsed(raw, expected):
    assert PerceptionConfig.from_env({"PERCEPTION_DEVICE": raw}).device == expected


@pytest.mark.parametrize("raw", ["gpu", "tpu", "cuda:x", "mps"])
def test_device_rejects_unknown(raw):
    with pytest.raises(ConfigError, match="PERCEPTION_DEVICE"):
        PerceptionConfig.from_env({"PERCEPTION_DEVICE": raw})


def test_resolve_device_auto_falls_back_to_cpu_without_cuda(monkeypatch):
    from perception.config import resolve_device

    assert resolve_device("auto", cuda_available=lambda: False) == "cpu"
    assert resolve_device("auto", cuda_available=lambda: True) == "cuda"
    assert resolve_device("cuda:1", cuda_available=lambda: False) == "cuda:1"  # explicit choice is honoured


def test_build_collaborators_puts_detector_and_embedder_on_the_configured_device():
    from perception.__main__ import build_collaborators

    cfg = PerceptionConfig.from_env({"INVENTORY_API_URL": "http://api", "OMNI_URL": "http://omni",
                                     "DECISION_URL": "http://dec", "PERCEPTION_DEVICE": "cuda"})
    c = build_collaborators(cfg)
    assert c["detector"].device == "cuda"
    assert c["embedder"].device == "cuda"
