"""Gold dataset format (AC-EVAL-1).

``eval/datasets/<name>/``:
- ``manifest.yaml``: name, video, audio (nullable), camera, start_time (ISO-8601 with tz), optional fps, transcript.
- ``gold.jsonl``: one gold object per line: gold_object_id, catalog_id (null iff is_unknown), is_unknown,
  appearances [{t, location, bbox?}], events [{type, t, actor?, to_location?}]. ``t`` = seconds since start_time.
- ``qa.jsonl`` (optional, AC-EVAL-4): qa_id, question, answer_type (entity|location|actor|free_text), expected, rubric?
- ``pairs.jsonl`` (optional, AC-EVAL-7): decision-model same-or-new pairs.
Each file is validated by a JSON schema shipped in ``nasa_eval/schemas/{manifest,gold,qa,pairs}.schema.json``.
"""

from __future__ import annotations

import json
import logging
import re
import wave
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

log = logging.getLogger(__name__)

SCHEMA_NAMES = ("manifest", "gold", "qa", "pairs")
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
FRAME_SUFFIXES = (".png", ".jpg", ".jpeg")
DEFAULT_FPS = 1.0
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # dataset names end up in report file names
MEDIA_FIELDS = ("video", "audio", "transcript")
# Synthetic audio for TTS-free scenes (review round): a tone while a scripted line is spoken, silence otherwise, so
# perception's VAD lets the window through to ASR and the (fake) ASR returns the scripted text.
SYNTH_AUDIO_RATE = 16000
SYNTH_LINE_DURATION_S = 3.0
SYNTH_TONE_HZ = 220.0
SYNTH_TONE_AMPLITUDE = 3000  # RMS ~2100, well above perception's default VAD threshold (300)


def is_safe_name(name: str) -> bool:
    return bool(SAFE_NAME.match(name or "")) and len(name) <= 100


def confined_path(base: Path, rel: str) -> Path | None:
    """``base / rel`` resolved, or None when ``rel`` is absolute or escapes ``base`` (``..``, symlinks)."""
    if not rel or Path(rel).is_absolute():
        return None
    root = base.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


class DatasetValidationError(ValueError):
    """Raised by load_dataset when a dataset is invalid. ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Appearance:
    t: float
    location: str
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class GoldEvent:
    type: str
    t: float
    actor: str | None = None
    to_location: str | None = None


@dataclass
class GoldObject:
    gold_object_id: str
    catalog_id: str | None
    appearances: list[Appearance] = field(default_factory=list)
    events: list[GoldEvent] = field(default_factory=list)
    is_unknown: bool = False


@dataclass(frozen=True)
class Manifest:
    name: str
    video: str
    audio: str | None
    camera: str
    start_time: datetime
    fps: float | None = None
    transcript: str | None = None
    zones: dict[str, tuple[float, float, float, float]] | None = None


@dataclass(frozen=True)
class TranscriptLine:
    t: float
    speaker: str
    text: str


@dataclass(frozen=True)
class QAItem:
    qa_id: str
    question: str
    answer_type: str
    expected: dict[str, Any]
    rubric: str | None = None


@dataclass
class Dataset:
    path: Path
    manifest: Manifest
    gold: list[GoldObject]
    qa: list[QAItem] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def fps(self) -> float:
        return float(self.manifest.fps or DEFAULT_FPS)

    def frame_paths(self) -> list[Path]:
        """Sorted frame files when ``manifest.video`` is a directory of images."""
        video = self.path / self.manifest.video
        if not video.is_dir():
            raise ValueError(f"{video} is not a frames directory; pass the video path to perception directly")
        return sorted(p for p in video.iterdir() if p.suffix.lower() in FRAME_SUFFIXES)

    def transcript_lines(self) -> list[TranscriptLine]:
        """Scripted transcript (``manifest.transcript``, JSONL of {t, speaker, text}) sorted by time; [] if none."""
        if not self.manifest.transcript:
            return []
        path = confined_path(self.path, self.manifest.transcript)
        if path is None or not path.is_file():
            return []
        rows, errors = read_jsonl(path)
        if errors:
            raise ValueError("; ".join(errors))
        lines = [TranscriptLine(float(r["t"]), str(r.get("speaker", "")), str(r["text"])) for _, r in rows]
        return sorted(lines, key=lambda x: (x.t, x.speaker, x.text))

    def iter_source(self, *, synthetic_audio: bool | None = None) -> Iterator[tuple[datetime, Any, Any]]:
        """Yield ``(timestamp, frame ndarray HxWx3 uint8, audio_chunk|None)`` for perception.replay (AC-PER-18).

        Frame ``i`` is stamped ``start_time + i / fps``. When the manifest names a 16-bit PCM WAV file, each frame
        carries the audio samples covering ``[t_i, t_i + 1/fps)`` as a 1-D int16 array (mono, first channel).
        Without audio, ``synthetic_audio`` (default: on when there is a scripted transcript) yields 16 kHz int16
        chunks carrying a tone while a transcript line is spoken (``SYNTH_LINE_DURATION_S`` from its ``t``) and
        silence otherwise; with it off the audio chunk is None.
        """
        import numpy as np
        from PIL import Image

        fps = self.fps
        audio = _WavSlicer(self.path / self.manifest.audio) if self.manifest.audio else None
        lines = self.transcript_lines() if audio is None else []
        synth = (bool(lines) if synthetic_audio is None else synthetic_audio) and audio is None
        try:
            for i, frame_path in enumerate(self.frame_paths()):
                with Image.open(frame_path) as img:
                    frame = np.asarray(img.convert("RGB"), dtype=np.uint8).copy()
                t_rel = i / fps
                if audio:
                    chunk = audio.slice(t_rel, 1.0 / fps)
                elif synth:
                    chunk = _synthetic_chunk(lines, t_rel, 1.0 / fps)
                else:
                    chunk = None
                yield self.manifest.start_time + timedelta(seconds=t_rel), frame, chunk
        finally:
            if audio:
                audio.close()


def _synthetic_chunk(lines: list[TranscriptLine], t0: float, duration: float) -> Any:
    import numpy as np

    n = round(duration * SYNTH_AUDIO_RATE)
    times = t0 + np.arange(n) / SYNTH_AUDIO_RATE
    speaking = np.zeros(n, dtype=bool)
    for line in lines:
        speaking |= (times >= line.t) & (times < line.t + SYNTH_LINE_DURATION_S)
    tone = SYNTH_TONE_AMPLITUDE * np.sin(2 * np.pi * SYNTH_TONE_HZ * times)
    return np.where(speaking, tone, 0.0).astype(np.int16)


class _WavSlicer:
    """Reads consecutive slices of a PCM WAV file as int16 mono numpy arrays."""

    def __init__(self, path: Path):
        self._wav = wave.open(str(path), "rb")  # noqa: SIM115 - closed by close()
        if self._wav.getsampwidth() != 2:
            self._wav.close()
            raise ValueError(f"{path}: only 16-bit PCM WAV audio is supported")
        self.rate = self._wav.getframerate()
        self.channels = self._wav.getnchannels()

    def slice(self, t: float, duration: float) -> Any:
        import numpy as np

        start = round(t * self.rate)
        n = round(duration * self.rate)
        if start >= self._wav.getnframes():
            return None
        self._wav.setpos(start)
        data = np.frombuffer(self._wav.readframes(n), dtype="<i2")
        return data[:: self.channels].copy() if self.channels > 1 else data.copy()

    def close(self) -> None:
        self._wav.close()


# ---------------------------------------------------------------------------------------------------------- schemas
@cache
def _schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise KeyError(f"unknown schema {name!r}; expected one of {SCHEMA_NAMES}")
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


def load_schema(name: str) -> dict[str, Any]:
    """Return the JSON schema (Draft 2020-12) called ``name`` (one of SCHEMA_NAMES)."""
    return json.loads(json.dumps(_schema(name)))  # defensive copy


@cache
def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema(name))


def schema_errors(name: str, instance: Any) -> list[str]:
    """Human-readable schema errors for ``instance`` against schema ``name`` (empty = valid)."""
    out = []
    for err in sorted(_validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path)
        out.append(f"{where or '<root>'}: {err.message}")
    return out


# ---------------------------------------------------------------------------------------------------------- parsing
class _NoTimestampLoader(yaml.SafeLoader):
    """SafeLoader that keeps ISO timestamps as strings (so start_time is validated, not silently coerced)."""


_NoTimestampLoader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def read_yaml(path: Path) -> Any:
    return yaml.load(path.read_text(), Loader=_NoTimestampLoader)  # SafeLoader subclass: no arbitrary objects


def parse_start_time(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp that carries a timezone. Raises TypeError / ValueError otherwise."""
    if not isinstance(value, str):
        raise TypeError(f"start_time must be an ISO-8601 string, got {value!r}")
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"start_time {value!r} is not ISO-8601") from exc
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"start_time {value!r} has no timezone")
    return ts


def read_jsonl(path: Path, schema: str | None = None) -> tuple[list[tuple[int, Any]], list[str]]:
    """Read a JSONL file. Returns ([(line_no, obj)], errors); blank lines are skipped."""
    rows: list[tuple[int, Any]] = []
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{line_no}: invalid JSON ({exc.msg})")
            continue
        errs = schema_errors(schema, obj) if schema else []
        errors.extend(f"{path.name}:{line_no}: {e}" for e in errs)
        if not errs:
            rows.append((line_no, obj))
    return rows, errors


def _manifest_from(raw: dict[str, Any], default_name: str) -> Manifest:
    return Manifest(
        name=raw.get("name") or default_name,
        video=raw["video"],
        audio=raw.get("audio"),
        camera=raw["camera"],
        start_time=parse_start_time(raw["start_time"]),
        fps=float(raw["fps"]) if raw.get("fps") is not None else None,
        transcript=raw.get("transcript"),
        zones={str(k): tuple(float(v) for v in box) for k, box in raw["zones"].items()}
        if isinstance(raw.get("zones"), dict)
        else None,
    )


def gold_from_dict(d: dict[str, Any]) -> GoldObject:
    return GoldObject(
        gold_object_id=d["gold_object_id"],
        catalog_id=d["catalog_id"],
        appearances=[
            Appearance(
                t=float(a["t"]),
                location=a["location"],
                bbox=tuple(float(x) for x in a["bbox"]) if a.get("bbox") is not None else None,
            )
            for a in d["appearances"]
        ],
        events=[
            GoldEvent(type=e["type"], t=float(e["t"]), actor=e.get("actor"), to_location=e.get("to_location"))
            for e in d["events"]
        ],
        is_unknown=bool(d["is_unknown"]),
    )


def gold_to_dict(g: GoldObject) -> dict[str, Any]:
    return {
        "gold_object_id": g.gold_object_id,
        "catalog_id": g.catalog_id,
        "is_unknown": g.is_unknown,
        "appearances": [
            {"t": a.t, "location": a.location, **({"bbox": list(a.bbox)} if a.bbox is not None else {})}
            for a in g.appearances
        ],
        "events": [{"type": e.type, "t": e.t, "actor": e.actor, "to_location": e.to_location} for e in g.events],
    }


def qa_from_dict(d: dict[str, Any]) -> QAItem:
    return QAItem(
        qa_id=d["qa_id"],
        question=d["question"],
        answer_type=d["answer_type"],
        expected=dict(d["expected"]),
        rubric=d.get("rubric"),
    )


# ---------------------------------------------------------------------------------------------------------- validation
def _check_gold_semantics(
    rows: list[tuple[int, dict[str, Any]]], catalog_ids: set[str] | None, location_ids: set[str] | None
) -> list[str]:
    errors: list[str] = []
    seen: dict[str, int] = {}
    for line_no, g in rows:
        where = f"gold.jsonl:{line_no}"
        gid = g["gold_object_id"]
        if gid in seen:
            errors.append(f"{where}: duplicate gold_object_id {gid!r} (first on line {seen[gid]})")
        else:
            seen[gid] = line_no
        for i, a in enumerate(g["appearances"]):
            bbox = a.get("bbox")
            if bbox is not None and not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
                errors.append(f"{where}: {gid} appearances/{i}: bbox {bbox} must satisfy x1 < x2 and y1 < y2")
            if location_ids is not None and a["location"] not in location_ids:
                errors.append(f"{where}: {gid} appearances/{i}: unknown location {a['location']!r}")
        if catalog_ids is not None and g["catalog_id"] is not None and g["catalog_id"] not in catalog_ids:
            errors.append(f"{where}: {gid}: unknown catalog_id {g['catalog_id']!r}")
        for i, e in enumerate(g["events"]):
            to = e.get("to_location")
            if location_ids is not None and to is not None and to not in location_ids:
                errors.append(f"{where}: {gid} events/{i}: unknown to_location {to!r}")
    return errors


def _check_qa_semantics(
    rows: list[tuple[int, dict[str, Any]]], gold_ids: set[str], location_ids: set[str] | None
) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, q in rows:
        where = f"qa.jsonl:{line_no}"
        if q["qa_id"] in seen:
            errors.append(f"{where}: duplicate qa_id {q['qa_id']!r}")
        seen.add(q["qa_id"])
        if q["answer_type"] == "entity":
            for gid in q["expected"]["entities"]:
                if gid not in gold_ids:
                    errors.append(f"{where}: expected entity {gid!r} is not a gold_object_id")
        if (
            q["answer_type"] == "location"
            and location_ids is not None
            and q["expected"]["location"] not in location_ids
        ):
            errors.append(f"{where}: unknown expected location {q['expected']['location']!r}")
    return errors


def validate_dataset(
    path: str | Path,
    *,
    catalog_ids: Iterable[str] | None = None,
    location_ids: Iterable[str] | None = None,
) -> list[str]:
    """Validate a dataset directory. Returns a list of human-readable errors (empty = valid).

    Schema checks always run; referential checks (catalog ids, location ids) run only when the id sets are given.
    """
    errors, _ = _validate(Path(path), catalog_ids=catalog_ids, location_ids=location_ids)
    return errors


def _validate(
    d: Path,
    *,
    catalog_ids: Iterable[str] | None = None,
    location_ids: Iterable[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    cat = set(catalog_ids) if catalog_ids is not None else None
    loc = set(location_ids) if location_ids is not None else None
    errors: list[str] = []
    parsed: dict[str, Any] = {}
    if not d.is_dir():
        return [f"{d}: dataset directory not found"], parsed

    manifest_path = d / "manifest.yaml"
    if not manifest_path.is_file():
        errors.append("manifest.yaml: missing")
    else:
        try:
            raw = read_yaml(manifest_path)
        except yaml.YAMLError as exc:
            raw = None
            errors.append(f"manifest.yaml: invalid YAML ({exc})")
        if raw is not None:
            m_errs = [f"manifest.yaml: {e}" for e in schema_errors("manifest", raw)]
            errors.extend(m_errs)
            if not m_errs:
                try:
                    parsed["manifest"] = _manifest_from(raw, d.name)
                except (TypeError, ValueError) as exc:
                    errors.append(f"manifest.yaml: start_time: {exc}")
                if not raw.get("name") and not is_safe_name(d.name):
                    errors.append(
                        f"manifest.yaml: name: no name given and the directory name {d.name!r} is not a safe "
                        "dataset name (letters, digits, '.', '_', '-')"
                    )
                for fld in MEDIA_FIELDS:
                    value = raw.get(fld)
                    if value and confined_path(d, value) is None:
                        errors.append(f"manifest.yaml: {fld}: {value!r} points outside the dataset directory")

    gold_path = d / "gold.jsonl"
    gold_rows: list[tuple[int, dict[str, Any]]] = []
    if not gold_path.is_file():
        errors.append("gold.jsonl: missing")
    else:
        gold_rows, g_errs = read_jsonl(gold_path, "gold")
        errors.extend(g_errs)
        if not gold_rows and not g_errs:
            errors.append("gold.jsonl: no gold objects")
        errors.extend(_check_gold_semantics(gold_rows, cat, loc))
        parsed["gold"] = gold_rows

    qa_path = d / "qa.jsonl"
    if qa_path.is_file():
        qa_rows, q_errs = read_jsonl(qa_path, "qa")
        errors.extend(q_errs)
        errors.extend(_check_qa_semantics(qa_rows, {g["gold_object_id"] for _, g in gold_rows}, loc))
        parsed["qa"] = qa_rows
    return errors, parsed


def load_dataset(path: str | Path) -> Dataset:
    """Validate and load a dataset directory; raises DatasetValidationError when invalid."""
    d = Path(path)
    errors, parsed = _validate(d)
    if errors:
        raise DatasetValidationError(errors)
    data = Dataset(
        path=d,
        manifest=parsed["manifest"],
        gold=[gold_from_dict(g) for _, g in parsed["gold"]],
        qa=[qa_from_dict(q) for _, q in parsed.get("qa", [])],
    )
    log.debug("loaded dataset name=%s gold=%d qa=%d", data.name, len(data.gold), len(data.qa))
    return data


def resolve_dataset(name_or_path: str | Path, datasets_root: str | Path | None = None) -> Path:
    """An existing directory path is returned as is; otherwise ``<datasets_root>/<name>``. FileNotFoundError if absent."""
    p = Path(name_or_path)
    if p.is_dir():
        return p
    if datasets_root is None:
        from nasa_eval import DATASETS_ROOT

        datasets_root = DATASETS_ROOT
    candidate = Path(datasets_root) / str(name_or_path)
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(f"dataset {name_or_path!r} not found (looked at {p} and {candidate})")


def seed_ids(contracts_dir: str | Path | None = None) -> tuple[set[str], set[str]]:
    """(catalog ids, location ids) declared in ``contracts/db/002_seed.sql`` for referential checks."""
    if contracts_dir is None:
        from nasa_eval import CONTRACTS_DB

        contracts_dir = CONTRACTS_DB
    sql = (Path(contracts_dir) / "002_seed.sql").read_text()

    def ids(table: str) -> set[str]:
        parts = re.split(rf"INSERT INTO {table}\s*\(", sql)
        out: set[str] = set()
        for part in parts[1:]:
            block = part.split("ON CONFLICT")[0]
            out |= {m.group(1) for m in re.finditer(r"^\s*\(\s*'([^']+)'", block, re.MULTILINE)}
        return out

    return ids("catalog_item"), ids("location")


def seed_camera_ids(contracts_dir: str | Path | None = None) -> set[str]:
    """Camera ids declared in ``contracts/db/002_seed.sql``."""
    if contracts_dir is None:
        from nasa_eval import CONTRACTS_DB

        contracts_dir = CONTRACTS_DB
    sql = (Path(contracts_dir) / "002_seed.sql").read_text()
    out: set[str] = set()
    for part in re.split(r"INSERT INTO camera\s*\(", sql)[1:]:
        block = part.split("ON CONFLICT")[0]
        out |= {m.group(1) for m in re.finditer(r"^\s*\(\s*'([^']+)'", block, re.MULTILINE)}
    return out
