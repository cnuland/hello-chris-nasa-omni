"""Decision-model eval (AC-EVAL-7): same-or-new pairs; accuracy, calibration (ECE), latency vs the rule baseline.

``evaluate_model`` runs every pair through a model. Transport failures, malformed responses and choices outside
{same, new} count as wrong answers (``n_errors``); calibration and latency are computed over the valid answers, with
the confidence of an answer being the probability the model gave to its chosen option. Accuracy is also reported per
stratum (``pair.meta["kind"]``: positive / hard_negative / class_negative).

What the model sees (review round):
- the production question: perception's reconciler asks ``same_or_new`` as a *choice over candidate entity ids +
  NEW* (AC-PER-8/12, Jev ``/v1/systemone`` shape). A pair is the k = 1 case of that question: one candidate
  (option ``CANDIDATE_OPTION``) + ``NEW``; choosing the candidate means "same". ``build_question`` produces exactly
  the arguments of ``perception.decision.DecisionClient.choose`` and the request body is byte-identical to that
  client's (contract test). Only the systemone transport is mirrored here (no chat fallback).
- only whitelisted, label-free fields: observation {description, location, t}, candidate {label, catalog_id,
  last_location, last_seen_t}, context {similarity, catalog_id} (``MODEL_INPUT_CONTEXT_KEYS``), plus both crops as
  PNG data URIs (``image``). ``pair_id``, ``label``, ``meta`` and crop paths are never sent.
- ``context.similarity`` is computed from the two crops by an embedder (``crop_similarity``; deterministic
  ``CropEmbedder`` by default, any ``embed(crop) -> vector`` such as perception's DINOv2 can be plugged in).
"""

from __future__ import annotations

import base64
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from nasa_eval.dataset import confined_path, read_jsonl
from nasa_eval.metrics import expected_calibration_error

log = logging.getLogger(__name__)

CHOICES = ("same", "new")
QUESTION = "same_or_new"  # perception.reconciler.SAME_OR_NEW
NEW_OPTION = "NEW"  # perception.NEW
CANDIDATE_OPTION = "E1"  # the single candidate's entity id in the k = 1 question
# == perception.decision.DEFAULT_INSTRUCTIONS (the contract test compares the request bodies)
DEFAULT_INSTRUCTIONS = (
    "You adjudicate inventory decisions for the International Space Station. Choose exactly one "
    "option; prefer NEW only when no option fits."
)
SAME_OR_NEW_INSTRUCTIONS = (
    "Is the observed object the same physical item as one of the candidate inventory entities? Choose that "
    "candidate's id, or NEW if it is a different item that needs its own entity."
)
MODEL_INPUT_CONTEXT_KEYS = ("similarity", "catalog_id")
OBSERVATION_KEYS = ("description", "location", "t")
CANDIDATE_KEYS = ("label", "catalog_id", "last_location", "last_seen_t")
PAIR_KINDS = ("positive", "hard_negative", "class_negative")


class DecisionModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecisionPair:
    pair_id: str
    label: str  # "same" | "new"
    observation: dict[str, Any] = field(default_factory=dict)
    candidate: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)  # evaluation-only (stratum), never sent to a model
    base_dir: Path | None = None  # directory the crop paths are relative to (crops are sent as pixels)


# ------------------------------------------------------------------------------------------------------- crops
class CropEmbedder:
    """Deterministic, weight-free crop embedding: a 4x4x4 RGB colour histogram plus a mean-centred 8x8 grey
    thumbnail, each L2-normalised. Same pixels => same vector; robust to small shifts and brightness changes."""

    def embed(self, crop: Any) -> np.ndarray:
        from PIL import Image

        arr = np.asarray(crop, dtype=np.uint8)[..., :3]
        q = (arr // 64).reshape(-1, 3).astype(np.int64)
        hist = np.bincount(q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2], minlength=64).astype(np.float64)
        grey = np.asarray(Image.fromarray(arr).convert("L").resize((8, 8), Image.BILINEAR), dtype=np.float64)
        thumb = (grey - grey.mean()).reshape(-1)
        parts = [v / n if (n := np.linalg.norm(v)) > 0 else v for v in (hist, thumb)]
        return np.concatenate(parts) / math.sqrt(2.0)


def _load_crop(path: str | Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8).copy()


def similarity_of(a: Any, b: Any, embedder: Any = None) -> float:
    """Cosine similarity of two crops (ndarrays) under ``embedder`` (default CropEmbedder), clipped to [0, 1]."""
    emb = embedder or CropEmbedder()
    va, vb = np.asarray(emb.embed(a), dtype=np.float64), np.asarray(emb.embed(b), dtype=np.float64)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(min(1.0, max(0.0, float(va @ vb) / (na * nb))))


def crop_similarity(a: str | Path, b: str | Path, embedder: Any = None) -> float:
    """``similarity_of`` for two crop image files."""
    return similarity_of(_load_crop(a), _load_crop(b), embedder)


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_question(pair: DecisionPair) -> tuple[str, dict[str, str], dict[str, Any], str]:
    """(question, options, state, instructions) for ``DecisionClient.choose`` - the k = 1 production question."""
    obs = {k: pair.observation[k] for k in OBSERVATION_KEYS if k in pair.observation}
    cand: dict[str, Any] = {"entity_id": CANDIDATE_OPTION}
    cand.update({k: pair.candidate[k] for k in CANDIDATE_KEYS if k in pair.candidate})
    if pair.base_dir is not None:
        for side, target in ((pair.observation, obs), (pair.candidate, cand)):
            crop = confined_path(pair.base_dir, str(side.get("crop", "")))
            if crop is not None and crop.is_file():
                target["image"] = _data_uri(crop)
    context = {k: pair.context[k] for k in MODEL_INPUT_CONTEXT_KEYS if k in pair.context}
    state = {"observation": obs, "candidates": [cand], "context": context}
    label = pair.candidate.get("label") or "existing inventory entity"
    options = {
        CANDIDATE_OPTION: f"the existing entity {CANDIDATE_OPTION} ({label})",
        NEW_OPTION: "a different physical item that needs its own new entity",
    }
    return QUESTION, options, state, SAME_OR_NEW_INSTRUCTIONS


@dataclass(frozen=True)
class DecisionOutput:
    choice: str
    probabilities: dict[str, float]
    latency_ms: float


class DecisionModel(Protocol):
    name: str

    def decide(self, pair: DecisionPair) -> DecisionOutput: ...


class RuleBaseline:
    """'same' iff context['similarity'] >= threshold; probabilities {'same': sim, 'new': 1 - sim} (clipped 0..1)."""

    name = "rule-baseline"

    def __init__(self, threshold: float = 0.85):
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0, 1]")
        self.threshold = threshold

    def decide(self, pair: DecisionPair) -> DecisionOutput:
        t0 = time.perf_counter()
        sim = pair.context.get("similarity")
        if isinstance(sim, bool) or not isinstance(sim, int | float) or math.isnan(sim):
            choice, probs = "new", {"same": 0.0, "new": 1.0}
        else:
            s = min(1.0, max(0.0, float(sim)))
            choice = "same" if s >= self.threshold else "new"
            probs = {"same": s, "new": 1.0 - s}
        return DecisionOutput(choice, probs, (time.perf_counter() - t0) * 1000.0)


class SystemOneDecisionModel:
    """DiffusionGemma (or any decision model) on vLLM ``POST /v1/systemone``, asked the production question.

    Request (== perception.decision.DecisionClient): {"model", "instructions", "state": {...},
    "questions": {"same_or_new": {"type": "choice", "instructions": str, "criteria": {"E1": str, "NEW": str}}}}
    Response: {"answers": {"same_or_new": {"choice": "E1" | "NEW", "probabilities": {option: p}, "confidence"?}}}
    The candidate option maps to "same", NEW to "new".
    """

    def __init__(self, base_url: str, model: str, *, timeout_s: float = 10.0):
        if not base_url or not model:
            raise ValueError("base_url and model are required")
        self.base_url = base_url
        self._model = model
        self.timeout_s = timeout_s

    @property
    def name(self) -> str:
        return self._model

    def _url(self) -> str:
        return self.base_url.rstrip("/") + "/v1/systemone"

    def decide(self, pair: DecisionPair) -> DecisionOutput:
        question, criteria, state, instructions = build_question(pair)
        body = {
            "model": self._model,
            "instructions": DEFAULT_INSTRUCTIONS,
            "state": state,
            "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        t0 = time.perf_counter()
        try:
            resp = httpx.post(self._url(), json=body, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise DecisionModelError(f"decision model unreachable: {exc}") from exc
        latency_ms = (time.perf_counter() - t0) * 1000.0
        if resp.status_code // 100 != 2:
            raise DecisionModelError(f"decision model returned HTTP {resp.status_code}")
        try:
            answers = resp.json()["answers"]
            answer = answers.get(question)
            if answer is None and len(answers) == 1:
                answer = next(iter(answers.values()))
            choice = str(answer["choice"])
            raw_probs = answer.get("probabilities") or {}
            probs = {str(k): float(v) for k, v in raw_probs.items() if str(k) in criteria}
            if not probs and isinstance(answer.get("confidence"), int | float):
                probs = {choice: float(answer["confidence"])}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DecisionModelError(f"malformed decision response: {exc!r}") from exc
        if choice not in criteria:
            raise DecisionModelError(f"decision model chose {choice!r}, expected one of {sorted(criteria)}")
        to_label = {CANDIDATE_OPTION: "same", NEW_OPTION: "new"}
        return DecisionOutput(to_label[choice], {to_label[k]: v for k, v in probs.items()}, max(latency_ms, 1e-6))


def load_pairs(path: str | Path) -> list[DecisionPair]:
    """Load and validate pairs.jsonl. Raises ValueError listing every invalid line (including crop paths outside
    the pairs directory). A legacy ``context.hard_negative`` is moved to ``meta`` so it is never sent."""
    path = Path(path)
    rows, errors = read_jsonl(path, "pairs")
    base = path.parent
    out: list[DecisionPair] = []
    for line_no, r in rows:
        for side in ("observation", "candidate"):
            crop = r[side]["crop"]
            if confined_path(base, crop) is None:
                errors.append(f"{path.name}:{line_no}: {side}.crop {crop!r} points outside {base}")
        context = dict(r["context"])
        meta = dict(r.get("meta") or {})
        if "hard_negative" in context:
            meta.setdefault("hard_negative", bool(context.pop("hard_negative")))
        out.append(
            DecisionPair(r["pair_id"], r["label"], dict(r["observation"]), dict(r["candidate"]), context, meta, base)
        )
    if errors:
        raise ValueError("; ".join(errors))
    return out


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 100])."""
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def evaluate_model(pairs: Iterable[DecisionPair], model: DecisionModel, *, n_bins: int = 10) -> dict[str, Any]:
    """{n, n_errors, accuracy, ece, latency_p50_ms, latency_p95_ms, latency_mean_ms}. Errors count as wrong."""
    pairs = list(pairs)
    if not pairs:
        raise ValueError("empty pair set")
    n_correct = n_errors = 0
    by_kind: dict[str, list[bool]] = {}
    confidences: list[float] = []
    outcomes: list[bool] = []
    latencies: list[float] = []
    for pair in pairs:
        stratum = by_kind.setdefault(str(pair.meta.get("kind") or "unlabelled"), [])
        try:
            out = model.decide(pair)
        except Exception as exc:  # noqa: BLE001 - any model failure is scored as a wrong answer
            n_errors += 1
            stratum.append(False)
            log.warning("decision error model=%s pair=%s: %s", model.name, pair.pair_id, exc)
            continue
        if out.choice not in CHOICES:
            n_errors += 1
            stratum.append(False)
            log.warning("invalid choice model=%s pair=%s choice=%r", model.name, pair.pair_id, out.choice)
            continue
        ok = out.choice == pair.label
        stratum.append(ok)
        n_correct += ok
        conf = out.probabilities.get(out.choice)
        conf = 1.0 if conf is None or isinstance(conf, bool) or math.isnan(conf) else min(1.0, max(0.0, conf))
        confidences.append(conf)
        outcomes.append(ok)
        latencies.append(float(out.latency_ms))
    return {
        "name": model.name,
        "n": len(pairs),
        "n_errors": n_errors,
        "n_correct": n_correct,
        "accuracy": n_correct / len(pairs),
        "accuracy_by_kind": {k: sum(v) / len(v) for k, v in sorted(by_kind.items()) if v},
        "ece": expected_calibration_error(confidences, outcomes, n_bins=n_bins) if confidences else None,
        "latency_p50_ms": _percentile(latencies, 50) if latencies else None,
        "latency_p95_ms": _percentile(latencies, 95) if latencies else None,
        "latency_mean_ms": sum(latencies) / len(latencies) if latencies else None,
    }


def compare(
    pairs: Iterable[DecisionPair], model: DecisionModel, baseline: DecisionModel, *, n_bins: int = 10
) -> dict[str, Any]:
    """{"model": {...}, "baseline": {...}, "delta": {accuracy, ece, latency_p50_ms}} (delta = model - baseline)."""
    pairs = list(pairs)
    m = evaluate_model(pairs, model, n_bins=n_bins)
    b = evaluate_model(pairs, baseline, n_bins=n_bins)

    def delta(key: str) -> float | None:
        return None if m[key] is None or b[key] is None else m[key] - b[key]

    return {
        "model": m,
        "baseline": b,
        "delta": {"accuracy": delta("accuracy"), "ece": delta("ece"), "latency_p50_ms": delta("latency_p50_ms")},
    }
