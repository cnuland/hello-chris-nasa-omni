"""nasa_eval: evaluation of the identification pipeline (docs/spec/50-eval.md).

Run as ``python -m nasa_eval <command>`` (commands: validate, generate, run, score, snapshot, gate, qa,
decision-eval).
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

EVAL_ROOT = Path(__file__).resolve().parents[1]  # eval/
REPO_ROOT = EVAL_ROOT.parent
CONTRACTS_DB = REPO_ROOT / "contracts" / "db"
DATASETS_ROOT = EVAL_ROOT / "datasets"
REPORTS_ROOT = EVAL_ROOT / "reports"
THRESHOLDS_PATH = EVAL_ROOT / "thresholds.yaml"
