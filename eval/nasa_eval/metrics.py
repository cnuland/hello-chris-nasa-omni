"""Pure metric functions (AC-EVAL-3, plus ECE / kappa used by AC-EVAL-4/7). No I/O.

Conventions:
- Ratio with an empty denominator: precision/recall of pair- and cluster-based metrics are vacuously 1.0;
  detection/event precision/recall with nothing to count are 0.0; F1 = 0.0 when P + R == 0.
- Entity resolution functions take ``pred`` and ``gold`` mappings item -> cluster id. Pairwise and B-cubed are
  computed over the UNION of the items: an item only in ``gold`` (a missed gold appearance) is a singleton predicted
  cluster, an item only in ``pred`` (a spurious sighting) is a singleton gold cluster. So misses lower recall and
  spurious sightings lower precision; an empty prediction is never perfect. Entity assignment, duplicate and
  false-merge rates use the items present in both mappings and divide by every gold object in ``gold``.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_EVENT_TOLERANCE_S = 5.0
_EPS = 1e-9


@dataclass(frozen=True)
class PRF:
    precision: float
    recall: float
    f1: float
    tp: int = 0
    fp: int = 0
    fn: int = 0


@dataclass(frozen=True)
class Detection:
    t: float
    catalog_id: str
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class EventRecord:
    type: str
    entity: str
    t: float
    actor: str | None = None


@dataclass(frozen=True)
class EventScores:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int
    actor_accuracy: float | None


@dataclass(frozen=True)
class LocationAccuracy:
    module: float
    zone: float
    n: int


# ------------------------------------------------------------------------------------------------------- helpers
def _f1(p: float, r: float) -> float:
    return 0.0 if p + r <= 0 else 2 * p * r / (p + r)


def _order_key(x: Any) -> tuple[str, Any]:
    """Sort key that orders homogeneous ids naturally and mixed types deterministically."""
    return (type(x).__name__, x)


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection over union of two ``[x1, y1, x2, y2]`` boxes (0.0 for degenerate / disjoint boxes)."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return 0.0 if union <= 0 else inter / union


def prf_from_counts(tp: int, fp: int, fn: int) -> PRF:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return PRF(p, r, _f1(p, r), tp, fp, fn)


# ------------------------------------------------------------------------------------------------------- detection
def detection_prf(
    pred: Iterable[Detection], gold: Iterable[Detection], *, iou_threshold: float = 0.5, time_tolerance_s: float = 0.5
) -> dict[str, PRF]:
    """Per-class P/R/F1. A prediction matches a gold detection of the same class within ``time_tolerance_s`` whose
    IoU >= ``iou_threshold`` (inclusive; IoU is not checked when either box is missing). Matching is one-to-one,
    greedy by IoU then time difference."""
    pred, gold = list(pred), list(gold)
    classes = sorted({d.catalog_id for d in pred} | {d.catalog_id for d in gold})
    out: dict[str, PRF] = {}
    for cls in classes:
        ps = [d for d in pred if d.catalog_id == cls]
        gs = [d for d in gold if d.catalog_id == cls]
        cands = []
        for i, p in enumerate(ps):
            for j, g in enumerate(gs):
                dt = abs(p.t - g.t)
                if dt > time_tolerance_s + _EPS:
                    continue
                ov = 1.0 if p.bbox is None or g.bbox is None else iou(p.bbox, g.bbox)
                if ov + _EPS < iou_threshold:
                    continue
                cands.append((-ov, dt, i, j))
        cands.sort()
        used_p: set[int] = set()
        used_g: set[int] = set()
        for _, _, i, j in cands:
            if i not in used_p and j not in used_g:
                used_p.add(i)
                used_g.add(j)
        tp = len(used_p)
        out[cls] = prf_from_counts(tp, len(ps) - tp, len(gs) - tp)
    return out


def macro_f1(per_class: Mapping[str, PRF]) -> float:
    """Mean F1 over classes with gold support (tp + fn > 0); 0.0 when there is none."""
    supported = [r.f1 for r in per_class.values() if r.tp + r.fn > 0]
    return sum(supported) / len(supported) if supported else 0.0


# ------------------------------------------------------------------------------------------------------- entity resolution
def _shared(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> list[Hashable]:
    return [k for k in gold if k in pred]


def _pairs(n: int) -> int:
    return n * (n - 1) // 2


class _Singleton:
    """A cluster id that equals nothing but itself (padding for items missing on one side)."""

    __slots__ = ("key",)

    def __init__(self, key: Hashable):
        self.key = key

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<singleton {self.key!r}>"


def _padded(
    pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]
) -> tuple[list[Hashable], dict[Hashable, Hashable], dict[Hashable, Hashable]]:
    keys = list(gold) + [k for k in pred if k not in gold]
    p = {k: pred[k] if k in pred else _Singleton(("missed", k)) for k in keys}
    g = {k: gold[k] if k in gold else _Singleton(("spurious", k)) for k in keys}
    return keys, p, g


def pairwise_prf(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> PRF:
    """Pairwise P/R/F1 over the union of items: a pair is positive when both items are in the same cluster."""
    keys, p_of, g_of = _padded(pred, gold)
    cells = Counter((p_of[k], g_of[k]) for k in keys)
    pred_sizes = Counter(p_of[k] for k in keys)
    gold_sizes = Counter(g_of[k] for k in keys)
    tp = sum(_pairs(n) for n in cells.values())
    pred_pairs = sum(_pairs(n) for n in pred_sizes.values())
    gold_pairs = sum(_pairs(n) for n in gold_sizes.values())
    p = tp / pred_pairs if pred_pairs else 1.0
    r = tp / gold_pairs if gold_pairs else 1.0
    return PRF(p, r, _f1(p, r), tp, pred_pairs - tp, gold_pairs - tp)


def bcubed_prf(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> PRF:
    """B-cubed P/R/F1 averaged over the union of items (vacuously 1.0 when there is none)."""
    keys, p_of, g_of = _padded(pred, gold)
    if not keys:
        return PRF(1.0, 1.0, 1.0)
    cells = Counter((p_of[k], g_of[k]) for k in keys)
    pred_sizes = Counter(p_of[k] for k in keys)
    gold_sizes = Counter(g_of[k] for k in keys)
    p = sum(cells[(p_of[k], g_of[k])] / pred_sizes[p_of[k]] for k in keys) / len(keys)
    r = sum(cells[(p_of[k], g_of[k])] / gold_sizes[g_of[k]] for k in keys) / len(keys)
    return PRF(p, r, _f1(p, r))


def assign_entities(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> dict[Hashable, Hashable]:
    """Map each predicted entity to the gold object holding the plurality of its sightings (ties: smallest gold id).
    Predicted entities with no sighting in ``gold`` are absent from the result."""
    counts: dict[Hashable, Counter] = defaultdict(Counter)
    for k in _shared(pred, gold):
        counts[pred[k]][gold[k]] += 1
    return {e: min(c.items(), key=lambda kv: (-kv[1], _order_key(kv[0])))[0] for e, c in counts.items()}


def duplicate_rate(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> float:
    """sum over gold objects of max(0, predicted entities assigned to it - 1) / number of gold objects.

    Equals (matched predicted entities - gold objects) / gold objects when every gold object is found, and never
    goes negative when some are missed."""
    objects = set(gold.values())
    if not objects:
        return 0.0
    per_gold = Counter(assign_entities(pred, gold).values())
    return sum(max(0, n - 1) for n in per_gold.values()) / len(objects)


def spurious_entity_rate(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> float:
    """Share of predicted entities none of whose items matches a gold item (0.0 with no predicted entity)."""
    entities = set(pred.values())
    if not entities:
        return 0.0
    matched = set(assign_entities(pred, gold))
    return len(entities - matched) / len(entities)


def false_merge_rate(pred: Mapping[Hashable, Hashable], gold: Mapping[Hashable, Hashable]) -> float:
    """Fraction of gold objects that share a predicted entity with at least one other gold object."""
    objects = set(gold.values())
    if not objects:
        return 0.0
    members: dict[Hashable, set] = defaultdict(set)
    for k in _shared(pred, gold):
        members[pred[k]].add(gold[k])
    merged: set = set()
    for gs in members.values():
        if len(gs) > 1:
            merged |= gs
    return len(merged) / len(objects)


# ------------------------------------------------------------------------------------------------------- location
def module_of(location_id: str, locations: Mapping[str, tuple[str | None, str]]) -> str | None:
    """Module ancestor (kind == 'module') of a location; ``locations``: id -> (parent_id, kind)."""
    current: str | None = location_id
    visited: set[str] = set()
    while current is not None and current in locations and current not in visited:
        visited.add(current)
        parent, kind = locations[current]
        if kind == "module":
            return current
        current = parent
    return None


def location_accuracy(
    pred: Mapping[str, str | None], gold: Mapping[str, str], locations: Mapping[str, tuple[str | None, str]]
) -> LocationAccuracy:
    """Top-1 location accuracy over the gold objects: exact id (zone level) and module level.
    A missing prediction counts as wrong; predictions for objects not in ``gold`` are ignored."""
    n = len(gold)
    if n == 0:
        return LocationAccuracy(0.0, 0.0, 0)
    zone = module = 0
    for obj, g_loc in gold.items():
        p_loc = pred.get(obj)
        if p_loc is None:
            continue
        zone += p_loc == g_loc
        g_mod = module_of(g_loc, locations)
        module += g_mod is not None and module_of(p_loc, locations) == g_mod
    return LocationAccuracy(module / n, zone / n, n)


# ------------------------------------------------------------------------------------------------------- events
def normalise_actor(s: str | None) -> str:
    return " ".join((s or "").casefold().split())


def _default_actor_match(pred_actor: str | None, gold_actor: str | None) -> bool:
    return pred_actor is not None and normalise_actor(pred_actor) == normalise_actor(gold_actor)


def _max_matching(adj: list[list[int]], n_right: int) -> dict[int, int]:
    """Maximum bipartite matching (augmenting paths). Returns left index -> right index."""
    match_right: list[int | None] = [None] * n_right

    def augment(u: int, seen: set[int]) -> bool:
        for v in adj[u]:
            if v in seen:
                continue
            seen.add(v)
            if match_right[v] is None or augment(match_right[v], seen):
                match_right[v] = u
                return True
        return False

    for u in range(len(adj)):
        augment(u, set())
    return {u: v for v, u in enumerate(match_right) if u is not None}


def event_prf(
    pred: Iterable[EventRecord],
    gold: Iterable[EventRecord],
    *,
    tolerance_s: float = DEFAULT_EVENT_TOLERANCE_S,
    actor_match: Callable[[str | None, str | None], bool] | None = None,
) -> EventScores:
    """Event P/R/F1 with a one-to-one maximum matching on (type, entity, |dt| <= tolerance_s), plus actor-attribution
    accuracy over matched pairs whose gold event has an actor (None when there is no such pair)."""
    pred, gold = list(pred), list(gold)
    match = actor_match or _default_actor_match
    adj: list[list[int]] = []
    for g in gold:
        cands = [
            (abs(p.t - g.t), j)
            for j, p in enumerate(pred)
            if p.type == g.type and p.entity == g.entity and abs(p.t - g.t) <= tolerance_s + _EPS
        ]
        adj.append([j for _, j in sorted(cands)])
    matched = _max_matching(adj, len(pred))
    tp = len(matched)
    prf = prf_from_counts(tp, len(pred) - tp, len(gold) - tp)
    judged = [(pred[j], gold[i]) for i, j in sorted(matched.items()) if gold[i].actor]
    actor_acc = sum(bool(match(p.actor, g.actor)) for p, g in judged) / len(judged) if judged else None
    return EventScores(prf.precision, prf.recall, prf.f1, prf.tp, prf.fp, prf.fn, actor_acc)


# ------------------------------------------------------------------------------------------------------- unknowns
def unknown_recall(gold_unknowns: Iterable[str], reviewed: Iterable[str]) -> float:
    """Share of gold unknown objects that reached the review queue (vacuously 1.0 with no gold unknowns)."""
    g = set(gold_unknowns)
    if not g:
        return 1.0
    return len(g & set(reviewed)) / len(g)


# ------------------------------------------------------------------------------------------------------- calibration
def expected_calibration_error(confidences: Sequence[float], correct: Sequence[bool], n_bins: int = 10) -> float:
    """ECE with ``n_bins`` equal-width bins over [0, 1] (1.0 falls in the last bin)."""
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have the same length")
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    if not confidences:
        return 0.0
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for c, ok in zip(confidences, correct, strict=True):
        c = float(c)
        if math.isnan(c) or c < 0.0 or c > 1.0:
            raise ValueError(f"confidence {c} outside [0, 1]")
        bins[min(int(c * n_bins), n_bins - 1)].append((c, bool(ok)))
    total = len(confidences)
    ece = 0.0
    for items in bins.values():
        acc = sum(ok for _, ok in items) / len(items)
        conf = sum(c for c, _ in items) / len(items)
        ece += abs(acc - conf) * len(items) / total
    return ece


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float:
    """Cohen's kappa between two raters' labels (1.0 when both are constant and agree)."""
    if len(a) != len(b):
        raise ValueError("label sequences must have the same length")
    if not a:
        raise ValueError("cohen_kappa needs at least one label pair")
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)
