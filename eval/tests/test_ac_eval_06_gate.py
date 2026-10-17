"""AC-EVAL-6: gates. eval/thresholds.yaml holds minimums/maximums (duplicate_rate <= 0.10, pairwise_f1 >= 0.85,
location_acc >= 0.8, unknown_recall >= 0.9). ``python -m nasa_eval gate`` exits non-zero when any metric regresses
past its threshold. Wired into ``make test``."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

import pytest
import yaml
from conftest import EVAL_DIR

from nasa_eval.gate import GateResult, Threshold, check, latest_report, latest_reports, load_thresholds, run_gate

GOOD = {"duplicate_rate": 0.05, "pairwise_f1": 0.9, "location_acc": 0.85, "unknown_recall": 1.0, "event_f1": 0.7}
TH = {"duplicate_rate": Threshold("duplicate_rate", max=0.10), "pairwise_f1": Threshold("pairwise_f1", min=0.85),
      "location_acc": Threshold("location_acc", min=0.8), "unknown_recall": Threshold("unknown_recall", min=0.9)}


def write_thresholds(path, data):
    path.write_text(yaml.safe_dump(data))
    return path


def write_report(d, name, metrics):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps({"dataset": "tiny", "target": "local", "metrics": metrics}))
    return p


@pytest.fixture
def th_file(tmp_path):
    return write_thresholds(tmp_path / "thresholds.yaml", {
        "duplicate_rate": {"max": 0.10}, "pairwise_f1": {"min": 0.85},
        "location_acc": {"min": 0.8}, "unknown_recall": {"min": 0.9}})


# ---------------------------------------------------------------- repo thresholds.yaml
def test_ac_eval_06_repo_thresholds_file_has_spec_values():
    th = load_thresholds(EVAL_DIR / "thresholds.yaml")
    assert th["duplicate_rate"].max == pytest.approx(0.10) and th["duplicate_rate"].min is None
    assert th["pairwise_f1"].min == pytest.approx(0.85)
    assert th["location_acc"].min == pytest.approx(0.8)
    assert th["unknown_recall"].min == pytest.approx(0.9)


def test_ac_eval_06_load_thresholds(th_file):
    assert load_thresholds(th_file) == TH


@pytest.mark.parametrize("bad", [
    {"pairwise_f1": {}},                         # neither min nor max
    {"pairwise_f1": {"minimum": 0.8}},           # unknown key
    {"pairwise_f1": {"min": "high"}},            # not a number
    {"pairwise_f1": {"min": 0.9, "max": 0.5}},   # empty interval
    {"pairwise_f1": 0.85},                       # not a mapping
])
def test_ac_eval_06_malformed_thresholds_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        load_thresholds(write_thresholds(tmp_path / "t.yaml", bad))


# ---------------------------------------------------------------- check()
def test_ac_eval_06_check_passes_when_all_met():
    r = check(GOOD, TH)
    assert isinstance(r, GateResult) and r.passed and r.failures == []


def test_ac_eval_06_check_boundaries_inclusive():
    assert check({"duplicate_rate": 0.10, "pairwise_f1": 0.85, "location_acc": 0.8, "unknown_recall": 0.9}, TH).passed


@pytest.mark.parametrize("metric,value", [("pairwise_f1", 0.849), ("location_acc", 0.5), ("unknown_recall", 0.0),
                                          ("duplicate_rate", 0.1001), ("duplicate_rate", 1.5)])
def test_ac_eval_06_check_fails_on_regression(metric, value):
    r = check(dict(GOOD, **{metric: value}), TH)
    assert not r.passed
    assert [f.metric for f in r.failures] == [metric]
    assert r.failures[0].value == value and r.failures[0].threshold == TH[metric]


def test_ac_eval_06_check_reports_every_failure():
    r = check({"duplicate_rate": 0.5, "pairwise_f1": 0.1, "location_acc": 0.1, "unknown_recall": 0.1}, TH)
    assert {f.metric for f in r.failures} == set(TH)


@pytest.mark.parametrize("value", [None, float("nan"), "0.9"])
def test_ac_eval_06_missing_or_invalid_metric_is_failure(value):
    m = dict(GOOD)
    m["pairwise_f1"] = value
    assert not check(m, TH).passed
    m.pop("pairwise_f1")
    r = check(m, TH)
    assert not r.passed and r.failures[0].metric == "pairwise_f1"


def test_ac_eval_06_extra_metrics_ignored():
    assert check(dict(GOOD, something_else=-1), TH).passed


# ---------------------------------------------------------------- latest report + run_gate
def test_ac_eval_06_latest_report(tmp_path):
    d = tmp_path / "reports"
    write_report(d, "20261017T090000Z-a.json", GOOD)
    newest = write_report(d, "20261017T100000Z-a.json", GOOD)
    write_report(d, "20261016T230000Z-zzz.json", GOOD)
    (d / "20261018T000000Z-a.md").write_text("# md")
    assert latest_report(d) == newest
    with pytest.raises(FileNotFoundError):
        latest_report(tmp_path / "empty")


def test_ac_eval_06_run_gate_exit_codes(tmp_path, th_file, capsys):
    ok = write_report(tmp_path / "r", "20261017T090000Z-a.json", GOOD)
    bad = write_report(tmp_path / "r2", "20261017T090000Z-a.json", dict(GOOD, pairwise_f1=0.5))
    assert run_gate(ok, thresholds_path=th_file) == 0
    assert run_gate(bad, thresholds_path=th_file) == 1
    out = capsys.readouterr()
    assert "pairwise_f1" in out.out + out.err
    assert run_gate(tmp_path / "nope.json", thresholds_path=th_file) == 2
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json")
    assert run_gate(garbage, thresholds_path=th_file) == 2
    assert run_gate(ok, thresholds_path=tmp_path / "missing.yaml") == 2


def test_ac_eval_06_run_gate_uses_latest_report(tmp_path, th_file):
    d = tmp_path / "reports"
    write_report(d, "20261017T090000Z-a.json", GOOD)
    write_report(d, "20261017T100000Z-a.json", dict(GOOD, duplicate_rate=0.3))
    assert run_gate(None, thresholds_path=th_file, reports_dir=d) == 1
    assert run_gate(None, thresholds_path=th_file, reports_dir=tmp_path / "none") == 2


def test_ac_eval_06_cli_gate(tmp_path, th_file):
    from nasa_eval.cli import main

    ok = write_report(tmp_path / "r", "20261017T090000Z-a.json", GOOD)
    assert main(["gate", "--report", str(ok), "--thresholds", str(th_file)]) == 0
    bad = write_report(tmp_path / "r", "20261017T100000Z-a.json", dict(GOOD, unknown_recall=0.2))
    assert main(["gate", "--report", str(bad), "--thresholds", str(th_file)]) == 1
    assert main(["gate", "--reports-dir", str(tmp_path / "r"), "--thresholds", str(th_file)]) == 1


def test_ac_eval_06_python_m_gate_exits_nonzero_on_regression(tmp_path, th_file):
    bad = write_report(tmp_path / "r", "20261017T090000Z-a.json", dict(GOOD, duplicate_rate=0.5))
    ok = write_report(tmp_path / "r", "20261017T080000Z-a.json", GOOD)
    cmd = [sys.executable, "-m", "nasa_eval", "gate", "--thresholds", str(th_file), "--report"]
    assert subprocess.run(cmd + [str(bad)], cwd=EVAL_DIR, check=False, capture_output=True).returncode == 1
    assert subprocess.run(cmd + [str(ok)], cwd=EVAL_DIR, check=False, capture_output=True).returncode == 0


# ---------------------------------------------------------------- make test
def test_ac_eval_06_wired_into_make_test():
    """``make -n test`` (dry run) in eval/ must run the test suite and the gate."""
    if shutil.which("make") is None:
        pytest.skip("make not installed")
    assert (EVAL_DIR / "Makefile").exists(), "eval/Makefile with a test target is required"
    out = subprocess.run(["make", "-n", "test"], cwd=EVAL_DIR, check=False, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "pytest" in out.stdout
    assert re.search(r"nasa_eval\s+gate", out.stdout)


# ---------------------------------------------------------------- review round: report kinds, per-dataset gating
def test_ac_eval_06_decision_report_never_gated(tmp_path, th_file):
    """A decision-eval report (no 'metrics') newer than the pipeline report must not break or hide the gate."""
    d = tmp_path / "reports"
    pipeline = write_report(d, "20261017T090000Z-a.json", GOOD)
    (d / "20261017T100000Z-decision.json").write_text(json.dumps({"kind": "decision", "model": {}, "baseline": {}}))
    assert latest_report(d) == pipeline
    assert run_gate(None, thresholds_path=th_file, reports_dir=d) == 0


def test_ac_eval_06_only_pipeline_kind_is_gated(tmp_path, th_file):
    d = tmp_path / "reports"
    write_report(d, "20261017T090000Z-a.json", dict(GOOD, pairwise_f1=0.1))
    (d / "20261017T100000Z-a-qa.json").write_text(json.dumps({"kind": "qa", "metrics": {"accuracy": 1.0}}))
    assert run_gate(None, thresholds_path=th_file, reports_dir=d) == 1, "the qa report must not mask the pipeline one"


def test_ac_eval_06_gate_latest_report_per_dataset(tmp_path, th_file):
    """A regression on dataset A stays visible after dataset B is run."""
    d = tmp_path / "reports"
    write_report(d, "20261017T080000Z-a.json", GOOD)
    write_report(d, "20261017T090000Z-a.json", dict(GOOD, duplicate_rate=0.4))
    write_report(d, "20261017T100000Z-b.json", GOOD)
    assert set(latest_reports(d)) == {"a", "b"}
    assert latest_reports(d)["a"].name == "20261017T090000Z-a.json"
    assert run_gate(None, thresholds_path=th_file, reports_dir=d) == 1
    write_report(d, "20261017T110000Z-a.json", GOOD)
    assert run_gate(None, thresholds_path=th_file, reports_dir=d) == 0


def test_ac_eval_06_cli_decision_eval_default_out_is_not_a_pipeline_report(fake_http, tmp_path, th_file, monkeypatch):
    import nasa_eval
    from nasa_eval.cli import main

    monkeypatch.setattr(nasa_eval, "REPORTS_ROOT", tmp_path / "reports")
    write_report(tmp_path / "reports", "20000101T000000Z-a.json", GOOD)
    fake_http.route("POST", "/v1/systemone", lambda body: (200, {"answers": {
        "same_or_new": {"choice": "NEW", "probabilities": {"NEW": 0.7}}}}))
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text(json.dumps({"pair_id": "p1", "label": "new", "observation": {"crop": "a.png"},
                                 "candidate": {"crop": "b.png"}, "context": {"similarity": 0.2}}) + "\n")
    from PIL import Image

    for n in ("a.png", "b.png"):
        Image.new("RGB", (8, 8), (10, 20, 30)).save(tmp_path / n)
    assert main(["decision-eval", "--pairs", str(pairs), "--decision-url", fake_http.url, "--model", "dg"]) == 0
    written = list((tmp_path / "reports" / "decision").glob("*-decision.json"))
    assert len(written) == 1 and json.loads(written[0].read_text())["kind"] == "decision"
    assert main(["gate", "--thresholds", str(th_file)]) == 0


# ---------------------------------------------------------------- review round: make test gates committed data
COMMITTED_DATASET = EVAL_DIR / "datasets" / "synth-ci"
COMMITTED_SNAPSHOT = EVAL_DIR / "baselines" / "synth-ci.snapshot.json"


def test_ac_eval_06_committed_dataset_and_snapshot_exist():
    from nasa_eval.dataset import load_dataset, seed_ids, validate_dataset
    from nasa_eval.snapshot import load_snapshot_json

    cat, loc = seed_ids()
    assert validate_dataset(COMMITTED_DATASET, catalog_ids=cat, location_ids=loc) == []
    snap = load_snapshot_json(COMMITTED_SNAPSHOT)
    assert snap.sightings and snap.entities
    assert load_dataset(COMMITTED_DATASET).gold


def test_ac_eval_06_cli_score_snapshot_writes_pipeline_report(tmp_path, th_file):
    from nasa_eval.cli import main

    out = tmp_path / "reports"
    rc = main(["score", "--dataset", str(COMMITTED_DATASET), "--snapshot", str(COMMITTED_SNAPSHOT),
               "--reports-dir", str(out)])
    assert rc == 0
    (rep_path,) = out.glob("*-synth-ci.json")
    rep = json.loads(rep_path.read_text())
    assert rep["kind"] == "pipeline" and rep["dataset"] == "synth-ci" and rep["source"] == "snapshot"
    assert rep["metrics"]["pairwise_f1"] > 0.85
    assert main(["gate", "--reports-dir", str(out), "--thresholds", str(th_file)]) == 0


def _bad_snapshot(tmp_path):
    from nasa_eval.snapshot import Snapshot, load_snapshot_json, save_snapshot_json

    good = load_snapshot_json(COMMITTED_SNAPSHOT)
    bad = tmp_path / "bad.snapshot.json"
    save_snapshot_json(Snapshot(locations=good.locations), bad)
    return bad


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_ac_eval_06_make_gate_ci_passes_on_committed_snapshot(tmp_path):
    out = subprocess.run(["make", "gate-ci", f"CI_REPORTS={tmp_path / 'ci'}", f"PY={sys.executable}"],
                         cwd=EVAL_DIR, check=False, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "PASSED" in out.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_ac_eval_06_make_gate_ci_fails_on_regressed_snapshot(tmp_path):
    bad = _bad_snapshot(tmp_path)
    out = subprocess.run(["make", "gate-ci", f"CI_SNAPSHOT={bad}", f"CI_REPORTS={tmp_path / 'ci'}",
                          f"PY={sys.executable}"], cwd=EVAL_DIR, check=False, capture_output=True, text=True)
    assert out.returncode != 0, out.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_ac_eval_06_make_test_runs_strict_ci_gate():
    out = subprocess.run(["make", "-n", "test"], cwd=EVAL_DIR, check=False, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert any("nasa_eval score" in ln and "--snapshot" in ln for ln in lines), out.stdout
    ci_gates = [ln for ln in lines if re.search(r"nasa_eval\s+gate", ln) and "reports/ci" in ln]
    assert ci_gates and all("--allow-missing-report" not in ln for ln in ci_gates), out.stdout


def test_ac_eval_06_committed_gold_is_reproducible_from_its_seed():
    """The committed synth-ci gold is exactly what `generate --seed 7` produces (frames are not committed)."""
    from nasa_eval.dataset import gold_to_dict, load_dataset
    from nasa_eval.synth import SceneConfig, generate_scene

    committed = load_dataset(COMMITTED_DATASET)
    regenerated = generate_scene(7, SceneConfig(width=160, height=90))
    assert [gold_to_dict(g) for g in committed.gold] == [gold_to_dict(g) for g in regenerated.gold]
    assert [(x.t, x.speaker, x.text) for x in committed.transcript_lines()] == \
        [(x.t, x.speaker, x.text) for x in regenerated.transcript]
