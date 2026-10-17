"""Regression guard from cluster integration (2026-10-17): the container installs ``.[model]``; a runtime import that
the real OWLv2 processor needs (scipy) was missing, so every window failed while the fake-backed unit tests passed."""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _model_extra() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    reqs = data["project"]["optional-dependencies"]["model"]
    return {r.split(">")[0].split("=")[0].split("[")[0].strip().lower() for r in reqs}


def test_model_extra_has_owlv2_runtime_deps():
    assert {"torch", "transformers", "scipy"} <= _model_extra()
