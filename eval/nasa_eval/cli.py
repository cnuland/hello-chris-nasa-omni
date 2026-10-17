"""Command line: ``python -m nasa_eval {validate,generate,run,score,snapshot,gate,qa,decision-eval}``
(AC-EVAL-1/2/4/5/6/7).

Report locations: pipeline reports ``reports/<ts>-<dataset>.json`` (gated), QA reports ``reports/qa/``,
decision-model reports ``reports/decision/`` (never gated).

Exit codes: 0 success / gate passed, 1 validation failure / gate failed, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Return the argparse parser with sub-commands validate, generate, run, gate, decision-eval."""
    from nasa_eval import DATASETS_ROOT
    from nasa_eval.runner import DEFAULT_DB_PREFIX, TARGETS

    p = argparse.ArgumentParser(
        prog="python -m nasa_eval", description="Evaluation of the NASA Omni identification pipeline."
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", metavar="{validate,generate,run,score,snapshot,gate,qa,decision-eval}")

    v = sub.add_parser("validate", help="validate a gold dataset directory (AC-EVAL-1)")
    v.add_argument("dataset", help="dataset directory or name under eval/datasets")
    v.add_argument(
        "--referential", action="store_true", help="also check catalog / location ids against contracts/db/002_seed.sql"
    )

    g = sub.add_parser("generate", help="generate a deterministic synthetic scene (AC-EVAL-2)")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--out", type=Path, default=DATASETS_ROOT, help="output root (default eval/datasets)")
    g.add_argument("--name", help="dataset name (default synth-<seed>)")
    g.add_argument("--duration", type=float, default=60.0, help="seconds")
    g.add_argument("--fps", type=float, default=1.0)
    g.add_argument("--width", type=int, default=640)
    g.add_argument("--height", type=int, default=360)
    g.add_argument(
        "--pairs",
        type=int,
        default=0,
        metavar="N",
        help="also write N positive + N negative decision pairs into <dataset>/pairs/",
    )

    r = sub.add_parser("run", help="replay a dataset into a fresh DB, score it and write reports (AC-EVAL-5)")
    r.add_argument("--dataset", required=True, help="dataset directory or name under eval/datasets")
    r.add_argument("--target", choices=TARGETS, default="local")
    r.add_argument("--reports-dir", type=Path, default=None, help="default eval/reports")
    r.add_argument("--db-prefix", default=DEFAULT_DB_PREFIX)
    r.add_argument("--keep-db", action="store_true", help="do not drop the fresh database afterwards")

    sc = sub.add_parser("score", help="score a recorded pipeline snapshot (no replay) and write a report (AC-EVAL-6)")
    sc.add_argument("--dataset", required=True, help="dataset directory or name under eval/datasets")
    sc.add_argument("--snapshot", type=Path, required=True, help="snapshot JSON (nasa_eval snapshot export)")
    sc.add_argument("--reports-dir", type=Path, default=None, help="default eval/reports")

    sn = sub.add_parser("snapshot", help="record a pipeline result as snapshot JSON")
    sn_sub = sn.add_subparsers(dest="snapshot_command", metavar="{export,reference}")
    se = sn_sub.add_parser("export", help="read entities/sightings/events/reviews from an inventory DB")
    se.add_argument("--db-url", default=None, help="database URL (default $EVAL_SNAPSHOT_DB_URL)")
    se.add_argument("--out", type=Path, required=True)
    sr = sn_sub.add_parser("reference", help="write the reference (per-window) snapshot of a dataset")
    sr.add_argument("--dataset", required=True)
    sr.add_argument("--out", type=Path, required=True)

    gt = sub.add_parser("gate", help="check a report against thresholds.yaml (AC-EVAL-6)")
    gt.add_argument("--report", type=Path, default=None, help="report JSON (default: newest in --reports-dir)")
    gt.add_argument("--reports-dir", type=Path, default=None, help="default eval/reports")
    gt.add_argument("--thresholds", type=Path, default=None, help="default eval/thresholds.yaml")
    gt.add_argument(
        "--allow-missing-report",
        action="store_true",
        help="exit 0 (with a notice) when no report exists yet instead of 2",
    )

    q = sub.add_parser("qa", help="ask a dataset's qa.jsonl to an agent serving a kept run DB and score it (AC-EVAL-4)")
    q.add_argument("--dataset", required=True, help="dataset directory or name under eval/datasets")
    q.add_argument("--run-report", type=Path, required=True, help="report of `run --keep-db` (entity_to_gold)")
    q.add_argument("--agent-url", default=None, help="agent base URL (default $EVAL_AGENT_URL)")
    q.add_argument("--judge-url", default=None, help="OpenAI-compatible judge URL (default $EVAL_JUDGE_URL)")
    q.add_argument("--judge-model", default=None, help="judge model (default $EVAL_JUDGE_MODEL)")
    q.add_argument("--generator-model", default=None, help="the agent's model (default $EVAL_GENERATOR_MODEL)")
    q.add_argument("--reports-dir", type=Path, default=None, help="default eval/reports (QA reports go to qa/)")

    d = sub.add_parser("decision-eval", help="decision model vs rule baseline on same-or-new pairs (AC-EVAL-7)")
    d.add_argument("--pairs", type=Path, required=True, help="pairs.jsonl")
    d.add_argument("--decision-url", default=None, help="vLLM base URL (default $DECISION_URL)")
    d.add_argument("--model", default=None, help="served model name (default $DECISION_MODEL or diffusiongemma)")
    d.add_argument("--threshold", type=float, default=0.85, help="rule-baseline similarity threshold")
    d.add_argument("--timeout", type=float, default=10.0, help="per-request timeout (s)")
    d.add_argument(
        "--out", type=Path, default=None, help="report JSON (default eval/reports/decision/<ts>-decision.json)"
    )
    return p


def _cmd_validate(args: argparse.Namespace) -> int:
    from nasa_eval.dataset import resolve_dataset, seed_ids, validate_dataset

    try:
        path = resolve_dataset(args.dataset)
    except FileNotFoundError as exc:
        print(f"validate: {exc}", file=sys.stderr)
        return 1
    kwargs = {}
    if args.referential:
        catalog_ids, location_ids = seed_ids()
        kwargs = {"catalog_ids": catalog_ids, "location_ids": location_ids}
    errors = validate_dataset(path, **kwargs)
    for e in errors:
        print(f"{path}: {e}", file=sys.stderr)
    print(f"validate: {path}: {f'INVALID ({len(errors)} errors)' if errors else 'ok'}")
    return 1 if errors else 0


def _cmd_generate(args: argparse.Namespace) -> int:
    from nasa_eval.synth import SceneConfig, generate_pairs, generate_scene, write_scene

    cfg = SceneConfig(duration_s=args.duration, fps=args.fps, width=args.width, height=args.height)
    name = args.name or f"synth-{args.seed}"
    d = write_scene(generate_scene(args.seed, cfg), args.out, name)
    print(f"generate: wrote {d}")
    if args.pairs:
        p = generate_pairs(args.seed, d / "pairs", n_positive=args.pairs, n_negative=args.pairs)
        print(f"generate: wrote {p}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from nasa_eval.dataset import DatasetValidationError
    from nasa_eval.runner import ConfigError, run

    try:
        res = run(
            args.dataset,
            target=args.target,
            reports_dir=args.reports_dir,
            db_prefix=args.db_prefix,
            keep_db=args.keep_db,
        )
    except DatasetValidationError as exc:
        for e in exc.errors:
            print(f"run: {e}", file=sys.stderr)
        return 1
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    print(f"run: wrote {res.json_path} and {res.md_path}")
    for k, v in res.metrics.items():
        print(f"  {k} = {v}")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    from nasa_eval.dataset import DatasetValidationError
    from nasa_eval.runner import score_snapshot
    from nasa_eval.snapshot import load_snapshot_json

    try:
        snap = load_snapshot_json(args.snapshot)
        res = score_snapshot(args.dataset, snap, reports_dir=args.reports_dir, source_path=str(args.snapshot))
    except DatasetValidationError as exc:
        for e in exc.errors:
            print(f"score: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"score: {exc}", file=sys.stderr)
        return 2
    print(f"score: wrote {res.json_path} and {res.md_path}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from nasa_eval.snapshot import read_snapshot, save_snapshot_json

    if args.snapshot_command == "export":
        url = args.db_url or os.environ.get("EVAL_SNAPSHOT_DB_URL")
        if not url:
            print("snapshot: no database URL (--db-url or $EVAL_SNAPSHOT_DB_URL)", file=sys.stderr)
            return 2
        save_snapshot_json(read_snapshot(url), args.out, source="recorded", recorded_at=datetime.now(UTC).isoformat())
    elif args.snapshot_command == "reference":
        from nasa_eval.baseline import reference_snapshot
        from nasa_eval.dataset import load_dataset, resolve_dataset

        data = load_dataset(resolve_dataset(args.dataset))
        save_snapshot_json(reference_snapshot(data), args.out, source="reference", dataset=data.name)
    else:
        print("snapshot: expected 'export' or 'reference'", file=sys.stderr)
        return 2
    print(f"snapshot: wrote {args.out}")
    return 0


def _cmd_qa(args: argparse.Namespace) -> int:
    from nasa_eval import REPORTS_ROOT
    from nasa_eval.dataset import DatasetValidationError, load_dataset, resolve_dataset
    from nasa_eval.qa import AgentClient, OpenAIJudge, ask_all, score_qa
    from nasa_eval.runner import actor_descriptors, report_basename

    agent_url = args.agent_url or os.environ.get("EVAL_AGENT_URL")
    if not agent_url:
        print("qa: no agent URL (--agent-url or $EVAL_AGENT_URL)", file=sys.stderr)
        return 2
    try:
        data = load_dataset(resolve_dataset(args.dataset))
        run_report = json.loads(Path(args.run_report).read_text())
        entity_to_gold = dict(run_report["entity_to_gold"])
    except DatasetValidationError as exc:
        for e in exc.errors:
            print(f"qa: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"qa: {exc}", file=sys.stderr)
        return 2
    if not data.qa:
        print(f"qa: dataset {data.name} has no qa.jsonl", file=sys.stderr)
        return 2
    judge_url = args.judge_url or os.environ.get("EVAL_JUDGE_URL")
    judge_model = args.judge_model or os.environ.get("EVAL_JUDGE_MODEL")
    generator = args.generator_model or os.environ.get("EVAL_GENERATOR_MODEL")
    reports_dir = args.reports_dir or REPORTS_ROOT
    try:
        judge = OpenAIJudge(judge_url, judge_model, generator_model=generator or "") if judge_url else None
        responses = ask_all(data.qa, AgentClient(agent_url))
        res = score_qa(
            data.qa,
            responses,
            entity_to_gold=entity_to_gold,
            actor_descriptors=actor_descriptors(data),
            judge=judge,
            generator_model=generator,
            reports_dir=reports_dir,
            dataset_name=data.name,
        )
    except ValueError as exc:
        print(f"qa: {exc}", file=sys.stderr)
        return 2
    import dataclasses

    now = datetime.now(UTC)
    report = {
        "kind": "qa",
        "dataset": data.name,
        "run_report": str(args.run_report),
        "database": run_report.get("database"),
        "generated_at": now.isoformat(),
        **{k: v for k, v in res.items() if k != "results"},
        "results": [dataclasses.asdict(r) for r in res["results"]],
        "responses": responses,
    }
    out = Path(reports_dir) / "qa" / f"{report_basename(now, data.name)}-qa.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    acc = res["accuracy"]
    print(f"qa: {data.name} accuracy={'n/a' if acc is None else f'{acc:.3f}'} ({res['n_scored']}/{res['n']}) -> {out}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    from nasa_eval import REPORTS_ROOT
    from nasa_eval.gate import latest_report, run_gate

    if args.report is None and args.allow_missing_report:
        try:
            latest_report(args.reports_dir or REPORTS_ROOT)
        except FileNotFoundError:
            print(
                f"gate: no report in {args.reports_dir or REPORTS_ROOT} yet; nothing to gate "
                "(run `python -m nasa_eval run` first)"
            )
            return 0
    return run_gate(args.report, thresholds_path=args.thresholds, reports_dir=args.reports_dir)


def _cmd_decision_eval(args: argparse.Namespace) -> int:
    from nasa_eval import REPORTS_ROOT
    from nasa_eval.decision_eval import RuleBaseline, SystemOneDecisionModel, compare, load_pairs

    url = args.decision_url or os.environ.get("DECISION_URL")
    if not url:
        print("decision-eval: no decision model URL (--decision-url or $DECISION_URL)", file=sys.stderr)
        return 2
    model_name = args.model or os.environ.get("DECISION_MODEL") or "diffusiongemma"
    try:
        pairs = load_pairs(args.pairs)
        report = compare(
            pairs,
            SystemOneDecisionModel(url, model_name, timeout_s=args.timeout),
            RuleBaseline(threshold=args.threshold),
        )
    except (OSError, ValueError) as exc:
        print(f"decision-eval: {exc}", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    report = {"kind": "decision", "pairs": str(args.pairs), "generated_at": now.isoformat(), **report}
    out = args.out or (REPORTS_ROOT / "decision" / f"{now:%Y%m%dT%H%M%SZ}-decision.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    m, b = report["model"], report["baseline"]
    print(
        f"decision-eval: {m['name']} accuracy={m['accuracy']:.3f} errors={m['n_errors']} | "
        f"{b['name']} accuracy={b['accuracy']:.3f} -> {out}"
    )
    return 0


_COMMANDS = {
    "validate": _cmd_validate,
    "generate": _cmd_generate,
    "run": _cmd_run,
    "score": _cmd_score,
    "snapshot": _cmd_snapshot,
    "gate": _cmd_gate,
    "qa": _cmd_qa,
    "decision-eval": _cmd_decision_eval,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else os.environ.get("EVAL_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    return _COMMANDS[args.command](args)
