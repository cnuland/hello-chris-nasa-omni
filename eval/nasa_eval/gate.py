"""Regression gates (AC-EVAL-6). ``eval/thresholds.yaml`` maps metric -> {min: x} or {max: x}.

``run_gate`` exit codes: 0 = all thresholds met, 1 = at least one regression, 2 = missing/invalid report or
thresholds. Boundaries are inclusive. A metric that is missing, None, NaN or not a number is a failure.

Only *pipeline* reports are gated (``kind == "pipeline"``, or a legacy report with a ``metrics`` object and no
``kind``); decision-eval and QA reports are ignored. Without an explicit report, the newest pipeline report of EVERY
dataset in the reports directory is gated, so a regression on one dataset is not hidden by a later run of another.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_TS_PREFIX = re.compile(r"^(\d{8}T\d{6}Z)-")


@dataclass(frozen=True)
class Threshold:
    metric: str
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class GateFailure:
    metric: str
    value: Any
    threshold: Threshold
    reason: str


@dataclass
class GateResult:
    passed: bool
    failures: list[GateFailure] = field(default_factory=list)


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and not math.isnan(value)


def load_thresholds(path: str | Path) -> dict[str, Threshold]:
    """Parse thresholds.yaml. Raises FileNotFoundError when absent and ValueError when malformed."""
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"{p}: invalid YAML ({exc})") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{p}: expected a non-empty mapping metric -> {{min|max: number}}")
    out: dict[str, Threshold] = {}
    for metric, spec in data.items():
        if not isinstance(spec, dict) or not spec:
            raise ValueError(f"{p}: {metric}: expected a mapping with 'min' and/or 'max'")
        unknown = set(spec) - {"min", "max"}
        if unknown:
            raise ValueError(f"{p}: {metric}: unknown keys {sorted(unknown)} (allowed: min, max)")
        for k, v in spec.items():
            if not _number(v):
                raise ValueError(f"{p}: {metric}.{k} must be a number, got {v!r}")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"{p}: {metric}: min {lo} > max {hi}")
        out[str(metric)] = Threshold(str(metric), None if lo is None else float(lo), None if hi is None else float(hi))
    return out


def check(metrics: Mapping[str, Any], thresholds: Mapping[str, Threshold]) -> GateResult:
    """Compare metrics with thresholds; every violated threshold is reported."""
    failures: list[GateFailure] = []
    for name, th in thresholds.items():
        if name not in metrics:
            failures.append(GateFailure(name, None, th, "missing"))
            continue
        value = metrics[name]
        if not _number(value):
            failures.append(GateFailure(name, value, th, f"not a number: {value!r}"))
            continue
        if th.min is not None and value < th.min:
            failures.append(GateFailure(name, value, th, f"{value} < min {th.min}"))
        elif th.max is not None and value > th.max:
            failures.append(GateFailure(name, value, th, f"{value} > max {th.max}"))
    return GateResult(not failures, failures)


def _dataset_of(path: Path) -> str:
    return path.stem[len(_TS_PREFIX.match(path.name).group(0)) :]  # type: ignore[union-attr]


def _is_gateable(path: Path) -> bool:
    """Pipeline reports (and unreadable files, so that a corrupt report fails loudly instead of being skipped)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    kind = data.get("kind")
    return kind == "pipeline" or (kind is None and isinstance(data.get("metrics"), dict))


def latest_reports(reports_dir: str | Path) -> dict[str, Path]:
    """dataset name -> its newest pipeline report ``<ts>-<dataset>.json`` in reports_dir (by timestamp)."""
    d = Path(reports_dir)
    candidates = [p for p in d.glob("*.json") if _TS_PREFIX.match(p.name)] if d.is_dir() else []
    out: dict[str, Path] = {}
    for p in sorted(candidates, key=lambda p: (_TS_PREFIX.match(p.name).group(1), p.name)):  # type: ignore[union-attr]
        if _is_gateable(p):
            out[_dataset_of(p)] = p
    return out


def latest_report(reports_dir: str | Path) -> Path:
    """Newest pipeline ``*.json`` report in reports_dir (by the timestamp in its name). FileNotFoundError if none."""
    reports = list(latest_reports(reports_dir).values())
    if not reports:
        raise FileNotFoundError(f"no <ts>-<dataset>.json pipeline report in {reports_dir}")
    return max(reports, key=lambda p: (_TS_PREFIX.match(p.name).group(1), p.name))  # type: ignore[union-attr]


def _describe(th: Threshold) -> str:
    parts = []
    if th.min is not None:
        parts.append(f">= {th.min:g}")
    if th.max is not None:
        parts.append(f"<= {th.max:g}")
    return " and ".join(parts)


def _gate_one(path: Path, thresholds: Mapping[str, Threshold]) -> int:
    try:
        report = json.loads(path.read_text())
        metrics = report["metrics"]
        if not isinstance(metrics, dict):
            raise TypeError("'metrics' is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"gate: cannot read report {path}: {exc}", file=sys.stderr)
        return 2
    result = check(metrics, thresholds)
    print(f"gate: report {path}")
    failed = {f.metric: f for f in result.failures}
    for name, th in thresholds.items():
        status = "FAIL" if name in failed else "ok"
        detail = f" ({failed[name].reason})" if name in failed else ""
        print(f"  {status:4} {name} = {metrics.get(name)!r} (required {_describe(th)}){detail}")
    if result.passed:
        print("gate: PASSED")
        return 0
    print(f"gate: FAILED {path.name} ({', '.join(failed)})", file=sys.stderr)
    log.warning("gate failed report=%s failures=%s", path, list(failed))
    return 1


def run_gate(
    report_path: str | Path | None = None,
    *,
    thresholds_path: str | Path | None = None,
    reports_dir: str | Path | None = None,
) -> int:
    """0 = all thresholds met, 1 = at least one regression, 2 = missing/invalid report or thresholds.

    With ``report_path`` only that report is gated; otherwise the newest pipeline report of every dataset."""
    from nasa_eval import REPORTS_ROOT, THRESHOLDS_PATH

    try:
        thresholds = load_thresholds(thresholds_path or THRESHOLDS_PATH)
    except (OSError, ValueError) as exc:
        print(f"gate: cannot load thresholds: {exc}", file=sys.stderr)
        return 2
    if report_path is not None:
        paths = [Path(report_path)]
    else:
        paths = list(latest_reports(reports_dir or REPORTS_ROOT).values())
        if not paths:
            print(
                f"gate: cannot read report: no <ts>-<dataset>.json pipeline report in {reports_dir or REPORTS_ROOT}",
                file=sys.stderr,
            )
            return 2
    codes = [_gate_one(p, thresholds) for p in paths]
    return 2 if 2 in codes else (1 if 1 in codes else 0)
