"""AC-EVAL-1: gold dataset format (manifest.yaml + gold.jsonl), validated by a JSON schema."""
from __future__ import annotations

import copy
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from conftest import (
    EVENT_TYPES,
    SEED_CATALOG_IDS,
    SEED_LOCATIONS,
    TINY_GOLD,
    app,
    gold_obj,
    manifest_dict,
    write_dataset,
)

from nasa_eval import dataset as ds
from nasa_eval.dataset import (
    Appearance,
    DatasetValidationError,
    GoldEvent,
    GoldObject,
    load_dataset,
    load_schema,
    resolve_dataset,
    validate_dataset,
)


# ---------------------------------------------------------------- schemas
@pytest.mark.parametrize("name", ["manifest", "gold", "qa", "pairs"])
def test_ac_eval_01_schema_is_valid_draft_2020_12(name):
    schema = load_schema(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get("$schema", "").endswith("2020-12/schema")


def test_ac_eval_01_schema_files_ship_with_package():
    files = {p.name for p in (Path(ds.__file__).parent / "schemas").glob("*.json")}
    assert {"manifest.schema.json", "gold.schema.json", "qa.schema.json", "pairs.schema.json"} <= files


def test_ac_eval_01_unknown_schema_name_rejected():
    with pytest.raises((KeyError, ValueError, FileNotFoundError)):
        load_schema("nope")


def _gold_validator():
    return jsonschema.Draft202012Validator(load_schema("gold"))


def test_ac_eval_01_gold_schema_accepts_all_documented_fields():
    v = _gold_validator()
    for g in TINY_GOLD:
        assert list(v.iter_errors(g)) == [], g["gold_object_id"]


def test_ac_eval_01_gold_schema_bbox_optional_and_nullable():
    v = _gold_validator()
    g = gold_obj("x", "wrench", [{"t": 0, "location": "LAB1S2"}, {"t": 1, "location": "LAB1S2", "bbox": None}])
    assert list(v.iter_errors(g)) == []


@pytest.mark.parametrize("field", ["gold_object_id", "catalog_id", "appearances", "events", "is_unknown"])
def test_ac_eval_01_gold_schema_requires_each_field(field):
    g = copy.deepcopy(TINY_GOLD[0])
    del g[field]
    assert list(_gold_validator().iter_errors(g)), f"missing {field} must be rejected"


@pytest.mark.parametrize("mutate", [
    lambda g: g["appearances"][0].update(bbox=[0.1, 0.1, 0.2]),             # 3 numbers
    lambda g: g["appearances"][0].update(bbox=[0.1, 0.1, 0.2, 1.5]),        # > 1
    lambda g: g["appearances"][0].update(bbox=[-0.1, 0.1, 0.2, 0.3]),       # < 0
    lambda g: g["appearances"][0].update(t=-1),                             # negative time
    lambda g: g["appearances"][0].pop("location"),
    lambda g: g["appearances"][0].pop("t"),
    lambda g: g["events"][0].update(type="teleported"),                     # not an event type
    lambda g: g["events"][0].pop("t"),
    lambda g: g["events"][0].pop("type"),
    lambda g: g.update(is_unknown="no"),
    lambda g: g.update(extra_field=1),                                      # additionalProperties false
    lambda g: g.update(appearances=[]),                                     # a gold object is seen at least once
])
def test_ac_eval_01_gold_schema_rejects_malformed(mutate):
    g = copy.deepcopy(TINY_GOLD[0])
    mutate(g)
    assert list(_gold_validator().iter_errors(g))


def test_ac_eval_01_gold_schema_unknown_iff_null_catalog():
    v = _gold_validator()
    assert list(v.iter_errors(gold_obj("u", None, [app(0, "LAB1S2")], is_unknown=True))) == []
    assert list(v.iter_errors(gold_obj("u", "wrench", [app(0, "LAB1S2")], is_unknown=True)))
    assert list(v.iter_errors(gold_obj("u", None, [app(0, "LAB1S2")], is_unknown=False)))


@pytest.mark.parametrize("etype", EVENT_TYPES)
def test_ac_eval_01_gold_schema_accepts_every_db_event_type(etype):
    g = gold_obj("x", "wrench", [app(0, "LAB1S2")], [{"type": etype, "t": 1, "actor": None, "to_location": None}])
    assert list(_gold_validator().iter_errors(g)) == []


def test_ac_eval_01_manifest_schema():
    v = jsonschema.Draft202012Validator(load_schema("manifest"))
    assert list(v.iter_errors(manifest_dict())) == []
    assert list(v.iter_errors(manifest_dict(audio="audio.wav"))) == []
    for missing in ("video", "audio", "camera", "start_time"):
        m = manifest_dict()
        del m[missing]
        assert list(v.iter_errors(m)), f"manifest without {missing} must be invalid"


# ---------------------------------------------------------------- validate_dataset
def test_ac_eval_01_valid_dataset_has_no_errors(tiny_dataset):
    assert validate_dataset(tiny_dataset) == []


def test_ac_eval_01_validate_reports_line_number_for_bad_gold(tmp_path):
    lines = [json.dumps(TINY_GOLD[0]), json.dumps({"gold_object_id": "broken"})]
    d = write_dataset(tmp_path, raw_gold_lines=lines)
    errors = validate_dataset(d)
    assert errors and any("2" in e and "gold.jsonl" in e for e in errors), errors


def test_ac_eval_01_validate_reports_invalid_json_line(tmp_path):
    d = write_dataset(tmp_path, raw_gold_lines=[json.dumps(TINY_GOLD[0]), "{not json"])
    errors = validate_dataset(d)
    assert errors and any("gold.jsonl" in e for e in errors)


def test_ac_eval_01_validate_missing_files(tmp_path):
    d = write_dataset(tmp_path)
    (d / "gold.jsonl").unlink()
    assert any("gold.jsonl" in e for e in validate_dataset(d))
    d2 = write_dataset(tmp_path, name="nomanifest")
    (d2 / "manifest.yaml").unlink()
    assert any("manifest.yaml" in e for e in validate_dataset(d2))


def test_ac_eval_01_validate_bad_manifest_start_time(tmp_path):
    d = write_dataset(tmp_path, manifest=manifest_dict(start_time="yesterday"))
    assert any("start_time" in e for e in validate_dataset(d))


def test_ac_eval_01_validate_naive_start_time_rejected(tmp_path):
    d = write_dataset(tmp_path, manifest=manifest_dict(start_time="2026-10-01T12:00:00"))
    assert any("start_time" in e for e in validate_dataset(d))


def test_ac_eval_01_duplicate_gold_object_ids_rejected(tmp_path):
    d = write_dataset(tmp_path, gold=[TINY_GOLD[0], TINY_GOLD[0]])
    assert any("drill-1" in e for e in validate_dataset(d))


def test_ac_eval_01_bbox_must_be_ordered(tmp_path):
    g = gold_obj("x", "wrench", [app(0, "LAB1S2", [0.5, 0.5, 0.4, 0.6])])
    assert validate_dataset(write_dataset(tmp_path, gold=[g]))


def test_ac_eval_01_empty_gold_rejected(tmp_path):
    assert validate_dataset(write_dataset(tmp_path, gold=[]))


def test_ac_eval_01_referential_checks_when_ids_given(tmp_path):
    g_bad_loc = gold_obj("x", "wrench", [app(0, "MARS1")])
    g_bad_cat = gold_obj("y", "lightsaber", [app(0, "LAB1S2")])
    g_bad_evt = gold_obj("z", "wrench", [app(0, "LAB1S2")],
                         [{"type": "moved", "t": 3, "actor": None, "to_location": "NOWHERE"}])
    d = write_dataset(tmp_path, gold=[g_bad_loc, g_bad_cat, g_bad_evt])
    assert validate_dataset(d) == [], "without id sets only the schema is checked"
    errors = validate_dataset(d, catalog_ids=SEED_CATALOG_IDS, location_ids=SEED_LOCATIONS)
    joined = " ".join(errors)
    assert "MARS1" in joined and "lightsaber" in joined and "NOWHERE" in joined


def test_ac_eval_01_tiny_gold_passes_referential_checks(tiny_dataset):
    assert validate_dataset(tiny_dataset, catalog_ids=SEED_CATALOG_IDS, location_ids=SEED_LOCATIONS) == []


# ---------------------------------------------------------------- load_dataset
def test_ac_eval_01_load_dataset_parses_typed_objects(tiny_dataset):
    data = load_dataset(tiny_dataset)
    assert data.name == "tiny"
    assert data.manifest.camera == "cam-lab1-fwd"
    assert data.manifest.audio is None
    assert data.manifest.start_time == datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    assert len(data.gold) == len(TINY_GOLD)
    drill = next(g for g in data.gold if g.gold_object_id == "drill-1")
    assert isinstance(drill, GoldObject)
    assert drill.catalog_id == "drill-pistol" and drill.is_unknown is False
    assert drill.appearances[0] == Appearance(t=0.0, location="LAB1S2", bbox=(0.10, 0.10, 0.20, 0.20))
    assert drill.events == [GoldEvent(type="moved", t=8.0, actor="crew member, blue shirt", to_location="LAB1P3")]
    unk = next(g for g in data.gold if g.gold_object_id == "unk-1")
    assert unk.is_unknown and unk.catalog_id is None


def test_ac_eval_01_load_dataset_raises_with_all_errors(tmp_path):
    lines = [json.dumps({"gold_object_id": "a"}), json.dumps({"gold_object_id": "b"})]
    d = write_dataset(tmp_path, raw_gold_lines=lines)
    with pytest.raises(DatasetValidationError) as ei:
        load_dataset(d)
    assert len(ei.value.errors) >= 2


def test_ac_eval_01_dataset_validation_error_is_value_error():
    assert issubclass(DatasetValidationError, ValueError)


def test_ac_eval_01_qa_file_loaded_when_present(tmp_path):
    qa = [{"qa_id": "q1", "question": "Where is the drill?", "answer_type": "location",
           "expected": {"location": "LAB1P3"}}]
    data = load_dataset(write_dataset(tmp_path, qa=qa, frames=1))
    assert [q.qa_id for q in data.qa] == ["q1"]
    assert data.qa[0].expected == {"location": "LAB1P3"}


def test_ac_eval_01_iter_source_yields_timestamped_frames(tiny_dataset):
    data = load_dataset(tiny_dataset)
    items = list(data.iter_source())
    assert len(items) == 12
    ts = [t for t, _, _ in items]
    assert ts[0] == datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    assert all(b > a for a, b in itertools.pairwise(ts)), "timestamps strictly increasing"
    assert (ts[1] - ts[0]).total_seconds() == pytest.approx(1.0)       # fps = 1
    _t, frame, audio = items[3]
    assert isinstance(frame, np.ndarray) and frame.dtype == np.uint8 and frame.shape == (24, 32, 3)
    assert int(frame[0, 0, 0]) == 30
    assert audio is None


def test_ac_eval_01_resolve_dataset_by_name_and_path(tmp_path):
    d = write_dataset(tmp_path, name="foo")
    assert resolve_dataset(d) == d
    assert resolve_dataset("foo", datasets_root=tmp_path) == d
    with pytest.raises(FileNotFoundError):
        resolve_dataset("missing", datasets_root=tmp_path)


def test_ac_eval_01_cli_validate_exit_codes(tmp_path, tiny_dataset):
    from nasa_eval.cli import main

    assert main(["validate", str(tiny_dataset)]) == 0
    bad = write_dataset(tmp_path, name="bad", raw_gold_lines=["{}"])
    assert main(["validate", str(bad)]) == 1


# ---------------------------------------------------------------- review round: names and paths are confined
@pytest.mark.parametrize("name", ["a/b", "../evil", "..", ".", "x y", "", "a\\b"])
def test_ac_eval_01_manifest_name_must_be_a_safe_basename(tmp_path, name):
    d = write_dataset(tmp_path, name="ok", manifest=manifest_dict(name=name))
    assert any("name" in e for e in validate_dataset(d)), name


@pytest.mark.parametrize("field,value", [("video", "../frames"), ("video", "/etc"), ("audio", "../a.wav"),
                                         ("audio", "/tmp/a.wav"), ("transcript", "../../t.jsonl")])
def test_ac_eval_01_media_paths_must_stay_inside_the_dataset(tmp_path, field, value):
    d = write_dataset(tmp_path, manifest=manifest_dict(**{field: value}))
    errors = validate_dataset(d)
    assert any(field in e and "outside" in e for e in errors), errors


def test_ac_eval_01_directory_name_used_as_default_name_must_be_safe(tmp_path):
    m = manifest_dict()
    del m["name"]
    d = write_dataset(tmp_path, name="has space", manifest=m)
    assert any("name" in e for e in validate_dataset(d))


# ---------------------------------------------------------------- review round: scripted transcript reaches perception
def test_ac_eval_01_iter_source_synthesises_audio_for_transcript_lines(tmp_path):
    d = write_dataset(tmp_path, manifest=manifest_dict(transcript="transcript.jsonl"), frames=8)
    (d / "transcript.jsonl").write_text(json.dumps({"t": 2.0, "speaker": "crew", "text": "Moving the drill."}) + "\n")
    items = list(load_dataset(d).iter_source())
    chunks = [a for _, _, a in items]
    assert all(isinstance(c, np.ndarray) and c.dtype == np.int16 and len(c) == ds.SYNTH_AUDIO_RATE for c in chunks)
    rms = [float(np.sqrt(np.mean(c.astype(np.float64) ** 2))) for c in chunks]
    assert rms[0] == 0.0 and rms[1] == 0.0, "silence before the line"
    assert rms[2] > 300 and rms[3] > 300, "speech-like signal while the line is spoken (above perception's VAD)"
    assert rms[7] == 0.0
    assert all(a is None for _, _, a in load_dataset(d).iter_source(synthetic_audio=False))


def test_ac_eval_01_transcript_lines_loaded(tmp_path):
    d = write_dataset(tmp_path, manifest=manifest_dict(transcript="transcript.jsonl"), frames=2)
    (d / "transcript.jsonl").write_text(json.dumps({"t": 2.0, "speaker": "crew", "text": "Hi."}) + "\n")
    data = load_dataset(d)
    assert [(x.t, x.speaker, x.text) for x in data.transcript_lines()] == [(2.0, "crew", "Hi.")]
