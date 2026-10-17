"""AC-EVAL-5: runner ``python -m nasa_eval run --dataset X --target {local,cluster}`` replays the dataset through
perception into a fresh DB, scores it and writes ``eval/reports/<ts>-<dataset>.{json,md}``.

Unit tests inject a fake replay that writes pipeline rows straight into the fresh database. The real perception
path (perception.replay.run, AC-PER-18) is covered by @pytest.mark.integration tests, skipped until
services/perception is importable (Integration phase).
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest
from conftest import DB_PREFIX, EVAL_DIR, TINY_GOLD, DbWriter, list_databases, write_dataset

from nasa_eval import runner
from nasa_eval.dataset import Dataset, DatasetValidationError
from nasa_eval.runner import ConfigError, RunResult, drop_database, provision_database, report_basename, write_report

TS = datetime(2026, 10, 17, 9, 5, 3, tzinfo=UTC)
PERCEPTION_AVAILABLE = importlib.util.find_spec("perception") is not None


def perfect_replay(calls: list | None = None):
    def _replay(dataset, database_url):
        if calls is not None:
            calls.append((dataset, database_url))
        DbWriter(database_url).perfect_replay_of(TINY_GOLD)
        return {"windows": 3}
    return _replay


@pytest.fixture
def ds_dir(tmp_path):
    return write_dataset(tmp_path / "datasets", frames=12)


def _ours() -> set[str]:
    return {d for d in list_databases() if d.startswith(DB_PREFIX)}


# ---------------------------------------------------------------- report naming / writing
def test_ac_eval_05_report_basename():
    assert report_basename(TS, "tiny") == "20261017T090503Z-tiny"
    plus2 = datetime(2026, 10, 17, 11, 5, 3, tzinfo=timezone(timedelta(hours=2)))
    assert report_basename(plus2, "tiny") == "20261017T090503Z-tiny", "always UTC"


def test_ac_eval_05_write_report_json_and_md(tmp_path):
    report = {"dataset": "tiny", "target": "local", "generated_at": TS.isoformat(),
              "metrics": {"pairwise_f1": 0.9, "duplicate_rate": 0.05}, "per_class": {}, "counts": {}}
    out = tmp_path / "new" / "reports"
    jp, mp = write_report(report, out, TS, "tiny")
    assert jp == out / "20261017T090503Z-tiny.json" and mp == out / "20261017T090503Z-tiny.md"
    assert json.loads(jp.read_text()) == report
    md = mp.read_text()
    assert "tiny" in md and "pairwise_f1" in md and "0.9" in md and "|" in md


# ---------------------------------------------------------------- fresh database provisioning
@pytest.mark.db
def test_ac_eval_05_provision_and_drop_database(admin_url):
    name, url = provision_database(admin_url, DB_PREFIX)
    try:
        assert name.startswith(DB_PREFIX) and url.endswith("/" + name)
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT count(*) FROM catalog_item").fetchone()[0] > 0, "contracts + seed applied"
            assert conn.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
    finally:
        drop_database(admin_url, name, prefix=DB_PREFIX)
    assert name not in list_databases()


@pytest.mark.db
def test_ac_eval_05_provision_gives_distinct_databases(admin_url):
    a, _ = provision_database(admin_url, DB_PREFIX)
    b, _ = provision_database(admin_url, DB_PREFIX)
    try:
        assert a != b
    finally:
        drop_database(admin_url, a, prefix=DB_PREFIX)
        drop_database(admin_url, b, prefix=DB_PREFIX)


@pytest.mark.db
@pytest.mark.parametrize("name", ["inventory", "postgres", "other_db"])
def test_ac_eval_05_drop_refuses_foreign_databases(admin_url, name):
    with pytest.raises(ValueError):
        drop_database(admin_url, name, prefix=DB_PREFIX)


def test_ac_eval_05_provision_refuses_empty_or_inventory_prefix():
    with pytest.raises(ValueError):
        provision_database("postgresql://postgres:test@localhost:55432/postgres", "")
    with pytest.raises(ValueError):
        provision_database("postgresql://postgres:test@localhost:55432/postgres", "inventory")


# ---------------------------------------------------------------- run()
@pytest.mark.db
def test_ac_eval_05_run_local_perfect_replay(admin_url, ds_dir, tmp_path):
    calls: list = []
    before = _ours()
    res = runner.run(ds_dir, target="local", reports_dir=tmp_path / "reports", admin_url=admin_url,
                     db_prefix=DB_PREFIX, replay=perfect_replay(calls), clock=lambda: TS)
    assert isinstance(res, RunResult)
    assert res.json_path == tmp_path / "reports" / "20261017T090503Z-tiny.json"
    assert res.md_path == tmp_path / "reports" / "20261017T090503Z-tiny.md"
    rep = json.loads(res.json_path.read_text())
    assert rep["dataset"] == "tiny" and rep["target"] == "local"
    assert rep["metrics"]["pairwise_f1"] == pytest.approx(1.0) and rep["metrics"]["duplicate_rate"] == 0.0
    assert rep["metrics"]["unknown_recall"] == 1.0
    assert res.metrics == rep["metrics"]
    assert "per_class" in rep and "counts" in rep and "generated_at" in rep
    # replay got the loaded dataset and a URL to the fresh database
    (dataset, url), = calls
    assert isinstance(dataset, Dataset) and dataset.name == "tiny"
    assert url.rsplit("/", 1)[1] == res.database_name and res.database_name.startswith(DB_PREFIX)
    # fresh DB is dropped afterwards
    assert res.database_name not in list_databases()
    assert _ours() == before


@pytest.mark.db
def test_ac_eval_05_each_run_uses_a_fresh_db(admin_url, ds_dir, tmp_path):
    seen: list[int] = []

    def counting_replay(dataset, url):
        with psycopg.connect(url) as conn:
            seen.append(conn.execute("SELECT count(*) FROM entity").fetchone()[0])
        DbWriter(url).perfect_replay_of(TINY_GOLD)

    for i in range(2):
        runner.run(ds_dir, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                   replay=counting_replay, clock=lambda i=i: TS + timedelta(seconds=i))
    assert seen == [0, 0]
    assert len(list((tmp_path / "r").glob("*.json"))) == 2


@pytest.mark.db
def test_ac_eval_05_keep_db(admin_url, ds_dir, tmp_path):
    res = runner.run(ds_dir, target="local", reports_dir=tmp_path, admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=perfect_replay(), clock=lambda: TS, keep_db=True)
    try:
        assert res.database_name in list_databases()
    finally:
        drop_database(admin_url, res.database_name, prefix=DB_PREFIX)


@pytest.mark.db
def test_ac_eval_05_replay_failure_drops_db_and_writes_no_report(admin_url, ds_dir, tmp_path):
    before = _ours()

    def boom(dataset, url):
        raise RuntimeError("perception crashed")

    with pytest.raises(RuntimeError, match="perception crashed"):
        runner.run(ds_dir, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                   replay=boom, clock=lambda: TS)
    assert _ours() == before
    assert not (tmp_path / "r").exists() or not list((tmp_path / "r").glob("*.json"))


@pytest.mark.db
def test_ac_eval_05_admin_url_from_env(admin_url, ds_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_PG_ADMIN_URL", admin_url)
    res = runner.run(ds_dir, target="local", reports_dir=tmp_path, db_prefix=DB_PREFIX, replay=perfect_replay(),
                     clock=lambda: TS)
    assert res.metrics["pairwise_f1"] == pytest.approx(1.0)


@pytest.mark.db
def test_ac_eval_05_invalid_dataset_fails_before_provisioning(admin_url, tmp_path):
    bad = write_dataset(tmp_path, name="bad", raw_gold_lines=["{}"])
    before = _ours()
    with pytest.raises(DatasetValidationError):
        runner.run(bad, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                   replay=perfect_replay(), clock=lambda: TS)
    assert _ours() == before


def test_ac_eval_05_invalid_target_rejected(ds_dir, tmp_path):
    with pytest.raises(ValueError):
        runner.run(ds_dir, target="moon", reports_dir=tmp_path, replay=perfect_replay())
    assert runner.TARGETS == ("local", "cluster")


def test_ac_eval_05_cluster_target_requires_configuration(ds_dir, tmp_path, monkeypatch):
    for var in ("EVAL_CLUSTER_PG_ADMIN_URL", "EVAL_PG_ADMIN_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError):
        runner.run(ds_dir, target="cluster", reports_dir=tmp_path, replay=perfect_replay())


def test_ac_eval_05_no_secrets_in_runner_source():
    src = (EVAL_DIR / "nasa_eval" / "runner.py").read_text()
    assert not re.search(r"postgres(ql)?://[^\s\"']*:[^\s\"'@]+@", src), "no credentials in code"


# ---------------------------------------------------------------- CLI
def test_ac_eval_05_cli_run_rejects_unknown_target(ds_dir):
    from nasa_eval.cli import main

    with pytest.raises(SystemExit) as ei:
        main(["run", "--dataset", str(ds_dir), "--target", "moon"])
    assert ei.value.code == 2


def test_ac_eval_05_cli_parser_run_arguments(ds_dir):
    from nasa_eval.cli import build_parser

    ns = build_parser().parse_args(["run", "--dataset", str(ds_dir), "--target", "cluster"])
    assert ns.target == "cluster" and str(ns.dataset) == str(ds_dir)
    ns2 = build_parser().parse_args(["run", "--dataset", "synth-7"])
    assert ns2.target == "local", "local is the default target"


def test_ac_eval_05_python_m_nasa_eval_help():
    out = subprocess.run([sys.executable, "-m", "nasa_eval", "--help"], check=False, capture_output=True, text=True, cwd=EVAL_DIR)
    assert out.returncode == 0, out.stderr
    for cmd in ("validate", "generate", "run", "gate", "decision-eval"):
        assert cmd in out.stdout


@pytest.mark.skipif(PERCEPTION_AVAILABLE, reason="only meaningful while services/perception is not installed")
def test_ac_eval_05_default_replay_without_perception_is_clear_error(ds_dir):
    from nasa_eval.dataset import load_dataset

    with pytest.raises(ConfigError, match="perception"):
        runner.default_replay(load_dataset(ds_dir), "postgresql://unused/db")


# ---------------------------------------------------------------- review round: robustness
def test_ac_eval_05_report_basename_rejects_unsafe_names():
    for bad in ("a/b", "../x", "..", ""):
        with pytest.raises(ValueError):
            report_basename(TS, bad)


@pytest.mark.db
def test_ac_eval_05_report_written_before_db_is_dropped(admin_url, ds_dir, tmp_path, monkeypatch):
    """If dropping the database fails, the scored result is not lost."""
    real_drop = runner.drop_database
    dropped: list[str] = []

    def failing_drop(url, name, *, prefix):
        dropped.append(name)
        real_drop(url, name, prefix=prefix)
        raise RuntimeError("drop failed")

    monkeypatch.setattr(runner, "drop_database", failing_drop)
    with pytest.raises(RuntimeError, match="drop failed"):
        runner.run(ds_dir, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                   replay=perfect_replay(), clock=lambda: TS)
    assert dropped and (tmp_path / "r" / "20261017T090503Z-tiny.json").exists()


@pytest.mark.db
def test_ac_eval_05_referential_errors_fail_before_provisioning(admin_url, tmp_path):
    gold = [dict(TINY_GOLD[0], appearances=[{"t": 0, "location": "LAB1S9"}])]   # typo in a zone id
    bad = write_dataset(tmp_path, name="typo", gold=gold)
    before = _ours()
    with pytest.raises(DatasetValidationError, match="LAB1S9"):
        runner.run(bad, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                   replay=perfect_replay(), clock=lambda: TS)
    from conftest import manifest_dict

    cam = write_dataset(tmp_path, name="cam", manifest=manifest_dict(name="cam", camera="cam-nowhere"))
    with pytest.raises(DatasetValidationError, match="cam-nowhere"):
        runner.run(cam, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                   replay=perfect_replay(), clock=lambda: TS)
    assert _ours() == before


@pytest.mark.db
def test_ac_eval_05_report_kind_and_source(admin_url, ds_dir, tmp_path):
    res = runner.run(ds_dir, target="local", reports_dir=tmp_path, admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=perfect_replay(), clock=lambda: TS)
    rep = json.loads(res.json_path.read_text())
    assert rep["kind"] == "pipeline" and rep["source"] == "replay"


@pytest.mark.db
def test_ac_eval_05_run_with_qa_agent_keeps_db_until_questions_are_answered(admin_url, tmp_path):
    qa = [{"qa_id": "q1", "question": "Which item was moved?", "answer_type": "entity",
           "expected": {"entities": ["drill-1"]}}]
    d = write_dataset(tmp_path / "datasets", qa=qa, frames=2)
    seen: list[str] = []

    class Agent:
        def __init__(self, url):
            self.url = url

        def ask(self, question, *, session_id=None):
            with psycopg.connect(self.url) as conn:   # the fresh DB still exists while QA runs
                eid = conn.execute("SELECT entity_id::text FROM entity WHERE catalog_id = 'drill-pistol'").fetchone()[0]
            seen.append(question)
            return {"reply": "the drill", "citations": [{"entity_id": eid}], "tool_calls": []}

    res = runner.run(d, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=perfect_replay(), clock=lambda: TS, agent_factory=lambda url: Agent(url))
    rep = json.loads(res.json_path.read_text())
    assert seen == ["Which item was moved?"]
    assert rep["qa"]["accuracy"] == pytest.approx(1.0) and rep["qa"]["n"] == 1
    assert res.database_name not in list_databases()


def test_ac_eval_05_default_replay_needs_inventory_api(ds_dir, monkeypatch):
    from nasa_eval import collaborators

    monkeypatch.setattr(collaborators, "_import_inventory_api", lambda: (_ for _ in ()).throw(
        ConfigError("inventory-api (services/inventory-api) is not importable")))
    with pytest.raises(ConfigError, match="inventory-api|perception"):
        from nasa_eval.dataset import load_dataset

        runner.default_replay(load_dataset(ds_dir), "postgresql://unused/db")


# ---------------------------------------------------------------- Integration phase (real perception replay)
INT_PREFIX = "test_int_"  # Integration phase: integration databases use their own prefix
PIPELINE_AVAILABLE = PERCEPTION_AVAILABLE and importlib.util.find_spec("inventory_api") is not None


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason="services/perception (perception.replay.run, AC-PER-18) and "
                                                    "services/inventory-api must be importable; Integration phase")
def test_ac_eval_05_integration_synthetic_scene_through_perception(admin_url, tmp_path, monkeypatch):
    """Self-contained: gold-scripted fakes (local target) + in-process inventory-api bound to the fresh DB."""
    from nasa_eval.gate import check, load_thresholds
    from nasa_eval.synth import generate_scene, write_scene

    monkeypatch.delenv("EVAL_INVENTORY_API_URL", raising=False)
    monkeypatch.delenv("CAMERA_ZONES", raising=False)
    monkeypatch.delenv("DECISION_URL", raising=False)
    d = write_scene(generate_scene(7), tmp_path / "datasets", "synth-7")
    res = runner.run(d, target="local", reports_dir=tmp_path / "reports", admin_url=admin_url, db_prefix=INT_PREFIX)
    assert res.database_name.startswith(INT_PREFIX) and res.database_name not in list_databases()
    assert res.json_path.exists() and res.md_path.exists()
    rep = json.loads(res.json_path.read_text())
    assert rep["source"] == "replay" and rep["replay_stats"]["posted"] == rep["replay_stats"]["windows"] > 0
    gate = check(res.metrics, load_thresholds(EVAL_DIR / "thresholds.yaml"))
    assert gate.passed, gate.failures


@pytest.mark.integration
@pytest.mark.skipif(not PERCEPTION_AVAILABLE, reason="services/perception not importable yet (Integration phase)")
def test_ac_eval_05_integration_perception_replay_signature():
    import inspect

    from perception import replay

    params = inspect.signature(replay.run).parameters
    for p in ("camera_id", "start_time", "api", "omni", "detector", "embedder", "decision", "asr", "storage",
              "config"):
        assert p in params
