"""AC-PER-26 truncation salvage: recover a schema-valid observation from the complete prefix of a cut-off reply.

The top-level object is walked key by key and every value is decoded with ``json.JSONDecoder.raw_decode``, so only
the JSON decoder decides where a string, object or array ends: braces, brackets, escaped quotes and ``\\u`` escapes
inside strings never count as structure. A value that does not decode is incomplete:

- arrays (``objects``, ``people``, ``events``, ``audio_events``) keep the elements that decode whole (fully closed);
  a partial element is dropped, so a cut-off ``moved`` event is never reported;
- a partial scalar (``scene_summary``, ``location_hint``, ``transcript``) is dropped;
- missing required fields are filled with schema-valid empties.

Fail closed for privacy: a complete ``scene_summary`` AND a complete ``location_hint`` are required (a reply cut
before or inside the hint may have been about to name crew quarters or the WHC). The result must validate against
the observation schema and carry something (a non-empty summary or at least one object, person or event); else None.
A reply whose top-level object is complete is never salvaged (schema-invalid complete replies go to repair).
"""

from __future__ import annotations

import json
import re
from typing import Any

from .schema import empty_observation, validate_observation

_DECODER = json.JSONDecoder()
_WS = re.compile(r"[ \t\n\r]*")
_ARRAY_KEYS = ("objects", "people", "events", "audio_events")


def _skip_ws(text: str, pos: int) -> int:
    return _WS.match(text, pos).end()  # type: ignore[union-attr]


def _decode(text: str, pos: int) -> tuple[Any, int] | None:
    try:
        return _DECODER.raw_decode(text, pos)
    except json.JSONDecodeError:
        return None


def _partial_array(text: str, pos: int) -> list[Any]:
    """Elements of the array starting at ``text[pos] == '['`` that decode whole, up to the first one that does not."""
    out: list[Any] = []
    pos = _skip_ws(text, pos + 1)
    while pos < len(text):
        decoded = _decode(text, pos)
        if decoded is None:
            break
        value, pos = decoded
        out.append(value)
        pos = _skip_ws(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos = _skip_ws(text, pos + 1)
        else:
            break
    return out


def parse_prefix(text: str, start: int) -> tuple[dict[str, Any], set[str], bool, int]:
    """Walk the object starting at ``text[start] == '{'``. Returns (complete values, keys whose value was cut off,
    closed, end): ``closed`` is True when the object's closing brace was reached (the object is complete)."""
    values: dict[str, Any] = {}
    partial: set[str] = set()
    pos = _skip_ws(text, start + 1)
    while pos < len(text):
        if text[pos] == "}":
            return values, partial, True, pos + 1
        key = _decode(text, pos)
        if key is None or not isinstance(key[0], str):
            break
        name, pos = key
        pos = _skip_ws(text, pos)
        if pos >= len(text) or text[pos] != ":":
            break
        pos = _skip_ws(text, pos + 1)
        if pos >= len(text):
            partial.add(name)
            break
        decoded = _decode(text, pos)
        if decoded is not None and isinstance(decoded[0], int | float) and not isinstance(decoded[0], bool):
            # a number is not self-delimiting: "0.9" may be the start of "0.95"
            after = _skip_ws(text, decoded[1])
            if after >= len(text):
                decoded = None
        if decoded is None:
            partial.add(name)
            if text[pos] == "[":
                values[name] = _partial_array(text, pos)
            break
        values[name], pos = decoded
        partial.discard(name)
        pos = _skip_ws(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos = _skip_ws(text, pos + 1)
        elif pos < len(text) and text[pos] == "}":
            continue
        else:
            break
    return values, partial, False, pos


def _candidates(text: str):
    """Start offsets of top-level-looking objects: '{' followed (after whitespace) by a '"' key."""
    idx = text.find("{")
    while idx != -1:
        after = _skip_ws(text, idx + 1)
        if after < len(text) and text[after] == '"':
            yield idx
        idx = text.find("{", idx + 1)


def salvage_observation(text: str) -> dict[str, Any] | None:
    """A schema-valid observation from the complete prefix of a cut-off reply, or None (see module docstring)."""
    if not text:
        return None
    skip_until = -1
    for start in _candidates(text):
        if start < skip_until:
            continue  # inside a complete object already rejected
        values, partial, closed, end = parse_prefix(text, start)
        if closed:
            skip_until = end
            continue
        salvaged = _finish(values, partial)
        if salvaged is not None:
            return salvaged
        # prose such as '{"a" is a key}' before the reply: try the next candidate (nested elements of a cut-off
        # reply never carry scene_summary + location_hint, so they cannot be mistaken for the observation)
    return None


def _finish(values: dict[str, Any], partial: set[str]) -> dict[str, Any] | None:
    if "scene_summary" not in values or "scene_summary" in partial:
        return None
    if "location_hint" not in values or "location_hint" in partial:
        return None  # privacy: the cut-off hint might have named a no_capture place
    obs = empty_observation()
    for key, value in values.items():
        if key in partial and key not in _ARRAY_KEYS:
            continue
        obs[key] = value
    if validate_observation(obs):
        return None
    useful = bool(str(obs.get("scene_summary") or "").strip()) or any(obs[k] for k in ("objects", "people", "events"))
    return obs if useful else None
