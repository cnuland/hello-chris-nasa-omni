"""Observation JSON schema (contracts/schemas/observation.schema.json) loading, JSON extraction and validation."""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

BUNDLED_SCHEMA = Path(__file__).resolve().parent / "observation.schema.json"


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_observation_schema() -> dict[str, Any]:
    """Return the observation schema. Source: env OBSERVATION_SCHEMA_PATH, else the bundled copy (identical to
    contracts/schemas/observation.schema.json)."""
    path = os.environ.get("OBSERVATION_SCHEMA_PATH") or str(BUNDLED_SCHEMA)
    return copy.deepcopy(_load(path))


@lru_cache(maxsize=4)
def _validator(path: str) -> Draft202012Validator:
    return Draft202012Validator(_load(path))


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Return the first complete, parseable JSON object in ``text`` (prose and ``` fences allowed), else None."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    return None


def validate_observation(obj: Any) -> list[str]:
    """Return a list of validation error messages (empty when ``obj`` is a valid observation)."""
    path = os.environ.get("OBSERVATION_SCHEMA_PATH") or str(BUNDLED_SCHEMA)
    errors = sorted(_validator(path).iter_errors(obj), key=lambda e: [str(p) for p in e.absolute_path])
    out = []
    for err in errors:
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{where}: {err.message}")
    return out


def parse_observation(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse a raw model reply: (observation, []) when its first JSON object is a valid observation, else
    (None, errors)."""
    obj = extract_first_json_object(text or "")
    if obj is None:
        return None, ["no JSON object found in the reply"]
    errors = validate_observation(obj)
    if errors:
        return None, errors
    return obj, []


def empty_observation() -> dict[str, Any]:
    """A valid observation with nothing in it."""
    return {
        "scene_summary": "",
        "location_hint": None,
        "objects": [],
        "people": [],
        "events": [],
        "transcript": "",
        "audio_events": [],
    }
