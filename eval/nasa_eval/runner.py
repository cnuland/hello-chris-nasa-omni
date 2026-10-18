"""Runner (AC-EVAL-5): replay a dataset through perception into a fresh DB, score, write reports.

``python -m nasa_eval run --dataset X --target {local,cluster}`` writes ``eval/reports/<ts>-<dataset>.{json,md}``.

Configuration (environment only, never hard-coded):
- ``EVAL_PG_ADMIN_URL``: admin connection URL of the local Postgres (target ``local``);
- ``EVAL_CLUSTER_PG_ADMIN_URL``: admin connection URL of the cluster Postgres (target ``cluster``);
- target ``cluster`` also needs the model endpoints (OMNI_URL, DECISION_URL, optional ASR_URL), see
  ``nasa_eval.collaborators``.
Each run validates the dataset (schema + catalog / location / camera ids of the seed), creates ``<prefix><hex>``
from ``contracts/db/*.sql``, binds an in-process inventory-api to it, replays the dataset through perception with
the target's collaborators, scores the result, replays the dataset a second time after ``REVISIT_GAP_S`` (perception
AC-PER-32: ``revisit_new_entity_rate``), optionally asks the dataset's QA questions to an agent bound to the
same database, writes the report and only then drops the database (unless ``keep_db``). The runner refuses to drop
``inventory``, ``postgres`` or any database without its prefix.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from nasa_eval.dataset import (
    Dataset,
    DatasetValidationError,
    is_safe_name,
    load_dataset,
    resolve_dataset,
    seed_camera_ids,
    seed_ids,
    validate_dataset,
)
from nasa_eval.scoring import revisit_new_entity_rate, score
from nasa_eval.snapshot import RevisitResult, Snapshot, read_snapshot

log = logging.getLogger(__name__)

TARGETS = ("local", "cluster")
DEFAULT_DB_PREFIX = "eval_"
ADMIN_URL_ENV = {"local": "EVAL_PG_ADMIN_URL", "cluster": "EVAL_CLUSTER_PG_ADMIN_URL"}
RESERVED_DATABASES = ("inventory", "postgres", "template0", "template1")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

ReplayFn = Callable[[Dataset, str], Any]  # (dataset, database_url) -> stats
RevisitFn = Callable[[Dataset, str, float], Any]  # (dataset, database_url, time offset s) -> stats
# Perception AC-PER-32: the looped ISS tour came back to Columbus about 15 min later and duplicated its objects.
REVISIT_GAP_S = 900.0
AgentFactory = Callable[[str], Any]  # database_url -> agent with ask(question, session_id=...) -> /chat response


class ConfigError(RuntimeError):
    """Missing / invalid runner configuration (environment)."""


@dataclass
class RunResult:
    json_path: Path
    md_path: Path
    metrics: dict[str, Any]
    database_name: str


# ------------------------------------------------------------------------------------------------------- reports
def report_basename(ts: datetime, dataset_name: str) -> str:
    """``YYYYmmddTHHMMSSZ-<dataset>`` (UTC). Raises ValueError for a name that is not a safe basename."""
    if not is_safe_name(dataset_name):
        raise ValueError(f"unsafe dataset name {dataset_name!r} for a report file name")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return f"{ts.astimezone(UTC):%Y%m%dT%H%M%SZ}-{dataset_name}"


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable Markdown version of a run report."""
    lines = [f"# Eval report: {report.get('dataset', '?')}", ""]
    for key in ("kind", "source", "target", "generated_at", "database"):
        if report.get(key) is not None:
            lines.append(f"- **{key}**: {report[key]}")
    gate = report.get("gate")
    if isinstance(gate, dict):
        verdict = "PASSED" if gate.get("passed") else "FAILED"
        lines.append(
            f"- **gate**: {verdict}" + (f" ({', '.join(gate.get('failed', []))})" if gate.get("failed") else "")
        )
    lines += ["", "## Metrics", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in (report.get("metrics") or {}).items()]
    per_class = report.get("per_class") or {}
    if per_class:
        lines += ["", "## Detection per class", "", "| class | precision | recall | f1 |", "|---|---|---|---|"]
        lines += [
            f"| {c} | {_fmt(r.get('precision'))} | {_fmt(r.get('recall'))} | {_fmt(r.get('f1'))} |"
            for c, r in sorted(per_class.items())
        ]
    counts = report.get("counts") or {}
    if counts:
        lines += ["", "## Counts", "", "| count | value |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in counts.items()]
    qa = report.get("qa")
    if isinstance(qa, dict):
        lines += [
            "",
            "## QA",
            "",
            f"- accuracy: {_fmt(qa.get('accuracy'))} ({qa.get('n_scored')}/{qa.get('n')} scored)",
        ]
        lines += [f"- {t}: {_fmt(v)}" for t, v in (qa.get("accuracy_by_type") or {}).items()]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], reports_dir: str | Path, ts: datetime, dataset_name: str) -> tuple[Path, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = report_basename(ts, dataset_name)
    json_path, md_path = out / f"{base}.json", out / f"{base}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    md_path.write_text(render_markdown(report))
    log.info("report written json=%s md=%s", json_path, md_path)
    return json_path, md_path


# ------------------------------------------------------------------------------------------------------- databases
def _check_prefix(prefix: str) -> None:
    if not prefix or not _IDENT.match(prefix):
        raise ValueError(f"invalid database prefix {prefix!r}: must be a non-empty lower-case identifier")
    for reserved in RESERVED_DATABASES:
        if reserved.startswith(prefix) or prefix.startswith(reserved):
            raise ValueError(f"database prefix {prefix!r} collides with reserved database {reserved!r}")


def _url_with_db(admin_url: str, name: str) -> str:
    parts = urlsplit(admin_url)
    if not parts.scheme.startswith("postgres"):
        raise ConfigError("admin URL must be a postgresql:// URL")
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def provision_database(admin_url: str, prefix: str, contracts_dir: str | Path | None = None) -> tuple[str, str]:
    """Create ``<prefix><hex>`` from contracts/db/*.sql. Returns (name, url)."""
    _check_prefix(prefix)
    import psycopg
    from psycopg import sql

    if contracts_dir is None:
        from nasa_eval import CONTRACTS_DB

        contracts_dir = CONTRACTS_DB
    sql_files = sorted(Path(contracts_dir).glob("*.sql"))
    if not sql_files:
        raise ConfigError(f"no contract SQL files in {contracts_dir}")
    name = f"{prefix}{uuid.uuid4().hex[:12]}"
    url = _url_with_db(admin_url, name)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for f in sql_files:
                conn.execute(f.read_text())
    except Exception:
        drop_database(admin_url, name, prefix=prefix)
        raise
    log.info("provisioned database name=%s from %d contract files", name, len(sql_files))
    return name, url


def drop_database(admin_url: str, name: str, *, prefix: str) -> None:
    """Drop a database created by provision_database. Refuses names not starting with prefix and 'inventory'."""
    _check_prefix(prefix)
    if name in RESERVED_DATABASES or not name.startswith(prefix) or not _IDENT.match(name):
        raise ValueError(f"refusing to drop database {name!r} (not a {prefix}* eval database)")
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    log.info("dropped database name=%s", name)


# ------------------------------------------------------------------------------------------------------- replay
def default_replay(dataset: Dataset, database_url: str, *, target: str = "local") -> Any:
    """Replay through ``perception.replay.run`` (AC-PER-18) with the collaborators of ``target`` and an in-process
    inventory-api bound to ``database_url`` (see ``nasa_eval.collaborators``). Needs ``perception`` and
    ``inventory_api`` importable (``pip install -e '.[pipeline]'``); ConfigError otherwise."""
    from nasa_eval.collaborators import replay_with_collaborators

    return replay_with_collaborators(dataset, database_url, target=target)


def default_revisit(dataset: Dataset, database_url: str, offset_s: float, *, target: str = "local") -> Any:
    """The revisit pass through perception (see ``nasa_eval.collaborators.revisit_with_collaborators``)."""
    from nasa_eval.collaborators import revisit_with_collaborators

    return revisit_with_collaborators(dataset, database_url, offset_s, target=target)


def scene_duration_s(dataset: Dataset) -> float:
    """Frames / fps (the time one pass over the dataset takes)."""
    return len(dataset.frame_paths()) / dataset.fps


def _jsonable(stats: Any) -> Any:
    if stats is None:
        return None
    if dataclasses.is_dataclass(stats) and not isinstance(stats, type):
        return dataclasses.asdict(stats)
    if isinstance(stats, dict | list | str | int | float | bool):
        return stats
    return repr(stats)


def _admin_url_for(target: str, admin_url: str | None) -> str:
    if admin_url:
        return admin_url
    env = ADMIN_URL_ENV[target]
    value = os.environ.get(env)
    if not value:
        raise ConfigError(f"target {target!r} needs the Postgres admin URL in ${env}")
    return value


def referential_errors(data: Dataset) -> list[str]:
    """Catalog / location ids (gold, events, QA) and the manifest camera checked against contracts/db/002_seed.sql."""
    catalog_ids, location_ids = seed_ids()
    errors = validate_dataset(data.path, catalog_ids=catalog_ids, location_ids=location_ids)
    if data.manifest.camera not in seed_camera_ids():
        errors.append(f"manifest.yaml: camera: unknown camera {data.manifest.camera!r}")
    return errors


def actor_descriptors(data: Dataset) -> list[str]:
    return sorted({e.actor for g in data.gold for e in g.events if e.actor})


def build_report(
    data: Dataset,
    result: dict[str, Any],
    *,
    now: datetime,
    source: str,
    target: str | None = None,
    database: str | None = None,
    stats: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The pipeline report dict (``kind: pipeline``) written by ``run`` and ``score --snapshot``."""
    report: dict[str, Any] = {
        "kind": "pipeline",
        "source": source,
        "dataset": data.name,
        "dataset_path": str(data.path),
        "target": target,
        "generated_at": (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(UTC).isoformat(),
        "database": database,
        "metrics": result["metrics"],
        "per_class": result["per_class"],
        "counts": result["counts"],
        "entity_to_gold": result["entity_to_gold"],
        "audit_t": result["audit_t"],
        "match_time_tolerance_s": result.get("match_time_tolerance_s"),
        "replay_stats": _jsonable(stats),
        **(extra or {}),
    }
    report["gate"] = _gate_summary(result["metrics"])
    return report


def qa_section(data: Dataset, snapshot: Snapshot, entity_to_gold: dict[str, str], agent: Any) -> dict[str, Any]:
    """Ask the dataset's QA questions to ``agent`` and score them (AC-EVAL-4), JSON-safe."""
    from nasa_eval.qa import ask_all, score_qa

    responses = ask_all(data.qa, agent)
    res = score_qa(
        data.qa,
        responses,
        entity_to_gold=entity_to_gold,
        location_ids=set(snapshot.locations),
        actor_descriptors=actor_descriptors(data),
        dataset_name=data.name,
    )
    res["results"] = [dataclasses.asdict(r) for r in res["results"]]
    res["responses"] = responses
    return res


# ------------------------------------------------------------------------------------------------------- run
def run(
    dataset: str | Path,
    *,
    target: str,
    reports_dir: str | Path | None = None,
    admin_url: str | None = None,
    db_prefix: str = DEFAULT_DB_PREFIX,
    replay: ReplayFn | None = None,
    clock: Callable[[], datetime] | None = None,
    keep_db: bool = False,
    agent_factory: AgentFactory | None = None,
    revisit: RevisitFn | None = None,
) -> RunResult:
    """Validate the dataset, replay it into a fresh database, score the result, optionally run the QA questions
    against ``agent_factory(database_url)``, replay the revisit (``revisit(dataset, url, duration + REVISIT_GAP_S)``,
    default: through perception when ``replay`` is the default too; an injected ``replay`` without ``revisit``
    measures none, so ``revisit_new_entity_rate`` is None), write the reports, then drop the database (also on
    failure) unless ``keep_db``. A replay failure propagates and no report is written."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
    data = load_dataset(resolve_dataset(dataset))  # DatasetValidationError before any DB work
    ref_errors = referential_errors(data)
    if ref_errors:
        raise DatasetValidationError(ref_errors)
    url_admin = _admin_url_for(target, admin_url)
    _check_prefix(db_prefix)
    replay_fn = replay or (lambda d, u: default_replay(d, u, target=target))
    revisit_fn = revisit if revisit is not None or replay is not None else (
        lambda d, u, off: default_revisit(d, u, off, target=target)
    )
    now = (clock or (lambda: datetime.now(UTC)))()
    if reports_dir is None:
        from nasa_eval import REPORTS_ROOT

        reports_dir = REPORTS_ROOT

    name, url = provision_database(url_admin, db_prefix)
    log.info("run dataset=%s target=%s database=%s", data.name, target, name)
    try:
        stats = replay_fn(data, url)
        snapshot = read_snapshot(url)
        result = score(data, snapshot)
        extra: dict[str, Any] = {}
        if agent_factory is not None and data.qa:
            extra["qa"] = qa_section(data, snapshot, result["entity_to_gold"], agent_factory(url))
        if revisit_fn is not None:  # after scoring and QA: the first-pass metrics never see the revisit
            offset_s = scene_duration_s(data) + REVISIT_GAP_S
            extra["revisit_stats"] = _jsonable(revisit_fn(data, url, offset_s))
            before = set(snapshot.entities)
            after = read_snapshot(url)
            snapshot.revisit = RevisitResult(
                gap_s=REVISIT_GAP_S,
                objects=len(data.gold),
                new_entity_ids=sorted(e for e, pe in after.entities.items() if e not in before and pe.kind == "object"),
            )
            result["metrics"]["revisit_new_entity_rate"] = revisit_new_entity_rate(snapshot.revisit)
            result["counts"]["revisit_new_entities"] = len(snapshot.revisit.new_entity_ids)
            log.info("revisit dataset=%s new_entities=%d rate=%s", data.name, len(snapshot.revisit.new_entity_ids),
                     result["metrics"]["revisit_new_entity_rate"])
        report = build_report(
            data,
            result,
            now=now,
            source="replay",
            target=target,
            database=name if keep_db else None,
            stats=stats,
            extra=extra,
        )
        json_path, md_path = write_report(report, reports_dir, now, data.name)  # before the DB goes away
    finally:
        if not keep_db:
            drop_database(url_admin, name, prefix=db_prefix)
    return RunResult(json_path, md_path, result["metrics"], name)


def score_snapshot(
    dataset: str | Path,
    snapshot: Snapshot,
    *,
    reports_dir: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    source_path: str | None = None,
) -> RunResult:
    """Score a recorded pipeline snapshot (no replay, no database) and write a pipeline report."""
    data = load_dataset(resolve_dataset(dataset))
    now = (clock or (lambda: datetime.now(UTC)))()
    if reports_dir is None:
        from nasa_eval import REPORTS_ROOT

        reports_dir = REPORTS_ROOT
    result = score(data, snapshot)
    report = build_report(data, result, now=now, source="snapshot", extra={"snapshot_path": source_path})
    json_path, md_path = write_report(report, reports_dir, now, data.name)
    return RunResult(json_path, md_path, result["metrics"], "")


def _gate_summary(metrics: dict[str, Any]) -> dict[str, Any] | None:
    """Informational gate verdict embedded in the report (the authoritative check is ``nasa_eval gate``)."""
    from nasa_eval import THRESHOLDS_PATH
    from nasa_eval.gate import check, load_thresholds

    try:
        res = check(metrics, load_thresholds(THRESHOLDS_PATH))
    except (OSError, ValueError) as exc:
        log.warning("thresholds unavailable for the report gate summary: %s", exc)
        return None
    return {"passed": res.passed, "failed": [f.metric for f in res.failures]}
