"""AC-EVAL-3: pure metric functions, each checked on hand-computed cases.

Hand computations are written next to each case so a reviewer can re-derive them.
"""
from __future__ import annotations

import inspect

import pytest
from conftest import SEED_LOCATIONS

from nasa_eval import metrics as m
from nasa_eval.metrics import PRF, Detection, EventRecord

A = (0.10, 0.10, 0.20, 0.20)
A_SHIFT = (0.11, 0.10, 0.21, 0.20)       # IoU with A = 0.009/0.011 = 0.818
B = (0.30, 0.10, 0.40, 0.20)
FAR = (0.80, 0.80, 0.90, 0.90)


# ---------------------------------------------------------------- IoU / PRF helpers
@pytest.mark.parametrize("a,b,expected", [
    ((0, 0, 2, 2), (1, 1, 3, 3), 1 / 7),
    ((0, 0, 1, 1), (0, 0, 1, 1), 1.0),
    ((0, 0, 1, 1), (2, 2, 3, 3), 0.0),
    ((0, 0, 1, 1), (1, 0, 2, 1), 0.0),          # touching edges
    ((0, 0, 0.5, 0.25), (0, 0, 0.25, 0.25), 0.5),
])
def test_ac_eval_03_iou(a, b, expected):
    assert m.iou(a, b) == pytest.approx(expected)


def test_ac_eval_03_iou_degenerate_box_is_zero():
    assert m.iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_ac_eval_03_prf_from_counts():
    r = m.prf_from_counts(tp=3, fp=1, fn=2)
    assert (r.precision, r.recall) == pytest.approx((0.75, 0.6))
    assert r.f1 == pytest.approx(2 * 0.75 * 0.6 / 1.35)
    assert (r.tp, r.fp, r.fn) == (3, 1, 2)
    z = m.prf_from_counts(0, 0, 0)
    assert (z.precision, z.recall, z.f1) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------- detection / class P R F1 per catalog class
def test_ac_eval_03_detection_per_class_hand_case():
    gold = [Detection(0, "drill-pistol", A), Detection(0, "wrench", B), Detection(1, "drill-pistol", A)]
    pred = [
        Detection(0, "drill-pistol", A_SHIFT),   # TP drill
        Detection(0, "wrench", FAR),             # FP wrench (no overlap) ; gold wrench@0 stays FN
        Detection(1, "wrench", A),               # wrong class: FP wrench and gold drill@1 is FN
        Detection(5, "laptop", A),               # FP laptop, no gold laptop
    ]
    r = m.detection_prf(pred, gold)
    assert set(r) == {"drill-pistol", "wrench", "laptop"}
    d = r["drill-pistol"]
    assert (d.tp, d.fp, d.fn) == (1, 0, 1)
    assert (d.precision, d.recall, d.f1) == pytest.approx((1.0, 0.5, 2 / 3))
    w = r["wrench"]
    assert (w.tp, w.fp, w.fn) == (0, 2, 1) and w.f1 == 0.0
    lap = r["laptop"]
    assert (lap.tp, lap.fp, lap.fn) == (0, 1, 0) and lap.precision == 0.0


def test_ac_eval_03_macro_f1_over_classes_with_gold_support():
    per = {"a": PRF(1, 0.5, 2 / 3, 1, 0, 1), "b": PRF(0, 0, 0, 0, 2, 1), "c": PRF(0, 0, 0, 0, 1, 0)}
    # class c has no gold (tp + fn == 0) -> excluded: (2/3 + 0) / 2
    assert m.macro_f1(per) == pytest.approx(1 / 3)
    assert m.macro_f1({}) == 0.0


def test_ac_eval_03_detection_one_to_one_matching():
    gold = [Detection(0, "wrench", B)]
    pred = [Detection(0, "wrench", B), Detection(0, "wrench", B)]
    w = m.detection_prf(pred, gold)["wrench"]
    assert (w.tp, w.fp, w.fn) == (1, 1, 0)


def test_ac_eval_03_detection_time_tolerance():
    gold = [Detection(0, "wrench", B)]
    assert m.detection_prf([Detection(0.4, "wrench", B)], gold)["wrench"].tp == 1
    assert m.detection_prf([Detection(0.6, "wrench", B)], gold)["wrench"].tp == 0
    assert m.detection_prf([Detection(0.6, "wrench", B)], gold, time_tolerance_s=1.0)["wrench"].tp == 1


def test_ac_eval_03_detection_iou_threshold_inclusive():
    gold = [Detection(0, "wrench", (0, 0, 0.5, 0.25))]
    pred = [Detection(0, "wrench", (0, 0, 0.25, 0.25))]                       # IoU exactly 0.5
    assert m.detection_prf(pred, gold)["wrench"].tp == 1
    assert m.detection_prf(pred, gold, iou_threshold=0.6)["wrench"].tp == 0


def test_ac_eval_03_detection_without_bbox_matches_on_class_and_time():
    gold = [Detection(3, "laptop", None)]
    assert m.detection_prf([Detection(3, "laptop", A)], gold)["laptop"].tp == 1


def test_ac_eval_03_detection_no_predictions():
    r = m.detection_prf([], [Detection(0, "wrench", B)])
    assert (r["wrench"].tp, r["wrench"].fn, r["wrench"].recall) == (0, 1, 0.0)


# ---------------------------------------------------------------- entity resolution: pairwise and B-cubed
# gold: A = {s1,s2,s3}, B = {s4,s5};  pred: X = {s1,s2}, Y = {s3,s4}, Z = {s5}
GOLD_ER = {"s1": "A", "s2": "A", "s3": "A", "s4": "B", "s5": "B"}
PRED_ER = {"s1": "X", "s2": "X", "s3": "Y", "s4": "Y", "s5": "Z"}


def test_ac_eval_03_pairwise_hand_case():
    # gold pairs: 12 13 23 45 (4); pred pairs: 12 34 (2); TP = {12} -> P = 1/2, R = 1/4, F1 = 1/3
    r = m.pairwise_prf(PRED_ER, GOLD_ER)
    assert (r.tp, r.fp, r.fn) == (1, 1, 3)
    assert (r.precision, r.recall, r.f1) == pytest.approx((0.5, 0.25, 1 / 3))


def test_ac_eval_03_bcubed_hand_case():
    # per-item precision: s1 1, s2 1, s3 1/2, s4 1/2, s5 1      -> P = 4/5
    # per-item recall:    s1 2/3, s2 2/3, s3 1/3, s4 1/2, s5 1/2 -> R = 8/15 ; F1 = 0.64
    r = m.bcubed_prf(PRED_ER, GOLD_ER)
    assert (r.precision, r.recall, r.f1) == pytest.approx((0.8, 8 / 15, 0.64))


@pytest.mark.parametrize("fn", [m.pairwise_prf, m.bcubed_prf])
def test_ac_eval_03_er_perfect_and_label_invariant(fn):
    assert fn(GOLD_ER, GOLD_ER).f1 == pytest.approx(1.0)
    relabelled = {k: {"A": "q", "B": "r"}[v] for k, v in GOLD_ER.items()}
    assert fn(relabelled, GOLD_ER).f1 == pytest.approx(1.0)


# Review round: ER metrics are computed over the UNION of predicted and gold items. A gold-only item (a missed
# appearance) is a singleton predicted cluster; a predicted-only item (a spurious sighting) is a singleton gold
# cluster. Misses therefore lower recall and spurious sightings lower precision (they used to be ignored).
def test_ac_eval_03_er_missed_gold_items_lower_recall():
    gold = dict(GOLD_ER, s6="A")               # s6 never predicted
    r = m.pairwise_prf(PRED_ER, gold)
    # gold pairs: A has 4 members (6 pairs) + B 1 = 7; predicted pairs 2; TP 1
    assert (r.tp, r.fp, r.fn) == (1, 1, 6)
    assert (r.precision, r.recall) == pytest.approx((0.5, 1 / 7))
    assert m.bcubed_prf(PRED_ER, gold).recall < m.bcubed_prf(PRED_ER, GOLD_ER).recall


def test_ac_eval_03_er_spurious_pred_items_lower_precision():
    pred = dict(PRED_ER, s99="X")              # a false detection attached to X
    r = m.pairwise_prf(pred, GOLD_ER)
    # X = {s1, s2, s99}: 3 pairs, 1 TP; Y 1 pair -> predicted pairs 4, TP 1; gold pairs 4
    assert (r.tp, r.fp, r.fn) == (1, 3, 3)
    b = m.bcubed_prf(pred, GOLD_ER)
    # precision: 2/3 2/3 1/3 (s99) 1/2 1/2 1 ; recall: 2/3 2/3 1 (s99) 1/3 1/2 1/2 -> both 11/18
    assert (b.precision, b.recall) == pytest.approx((11 / 18, 11 / 18))


@pytest.mark.parametrize("fn", [m.pairwise_prf, m.bcubed_prf])
def test_ac_eval_03_er_empty_prediction_is_not_perfect(fn):
    r = fn({}, GOLD_ER)
    assert r.recall < 1.0 and r.f1 < 1.0
    assert m.pairwise_prf({}, GOLD_ER).f1 == 0.0
    assert m.bcubed_prf({}, GOLD_ER).recall == pytest.approx((3 * (1 / 3) + 2 * (1 / 2)) / 5)


def test_ac_eval_03_er_both_empty_is_vacuous():
    assert m.pairwise_prf({}, {}).f1 == 1.0 and m.bcubed_prf({}, {}).f1 == 1.0


def test_ac_eval_03_pairwise_vacuous_conventions():
    singletons = {"a": 1, "b": 2, "c": 3}
    r = m.pairwise_prf(singletons, singletons)
    assert (r.precision, r.recall, r.f1) == (1.0, 1.0, 1.0)
    one_cluster = {"a": "G", "b": "G", "c": "G"}
    r2 = m.pairwise_prf(singletons, one_cluster)       # no predicted pairs: P vacuous 1, R 0
    assert (r2.precision, r2.recall, r2.f1) == (1.0, 0.0, 0.0)


def test_ac_eval_03_bcubed_all_merged():
    # pred puts all 5 in one cluster: precision_i = |gold(i)|/5 -> (3*3/5 + 2*2/5)/5 = 13/25; recall 1
    r = m.bcubed_prf({k: "ALL" for k in GOLD_ER}, GOLD_ER)
    assert (r.precision, r.recall) == pytest.approx((13 / 25, 1.0))


# ---------------------------------------------------------------- duplicate rate / false merges / assignment
# gold: A = {1,2,3,4}, B = {5,6}, C = {7}; pred: X {1,2}, Y {3,4}, Z {5,6}, W {7}, V {8 (false detection)}
GOLD_DUP = {1: "A", 2: "A", 3: "A", 4: "A", 5: "B", 6: "B", 7: "C"}
PRED_DUP = {1: "X", 2: "X", 3: "Y", 4: "Y", 5: "Z", 6: "Z", 7: "W", 8: "V"}


def test_ac_eval_03_assign_entities_plurality():
    assert m.assign_entities(PRED_DUP, GOLD_DUP) == {"X": "A", "Y": "A", "Z": "B", "W": "C"}


def test_ac_eval_03_assign_entities_tie_smallest_gold_id():
    assert m.assign_entities({1: "P", 2: "P"}, {1: "B", 2: "A"}) == {"P": "A"}


def test_ac_eval_03_duplicate_rate_hand_case():
    # matched pred entities = 4 (X, Y, Z, W; V matches nothing); gold objects = 3 -> (4 - 3) / 3
    assert m.duplicate_rate(PRED_DUP, GOLD_DUP) == pytest.approx(1 / 3)


def test_ac_eval_03_duplicate_rate_perfect_is_zero():
    assert m.duplicate_rate(GOLD_DUP, GOLD_DUP) == 0.0


def test_ac_eval_03_duplicate_rate_never_negative_when_objects_missed():
    # gold A split into 2 entities but C never detected: 3 matched pred vs 3 gold would hide the duplicate;
    # extras are counted per covered gold object -> 1 / 3
    gold = {1: "A", 2: "A", 5: "B", 7: "C"}
    pred = {1: "X", 2: "Y", 5: "Z"}
    assert m.duplicate_rate(pred, gold) == pytest.approx(1 / 3)
    assert m.duplicate_rate({}, gold) == 0.0


def test_ac_eval_03_duplicate_rate_three_way_split():
    gold = {1: "A", 2: "A", 3: "A"}
    assert m.duplicate_rate({1: "x", 2: "y", 3: "z"}, gold) == pytest.approx(2.0)


def test_ac_eval_03_spurious_entity_rate():
    # V has only a predicted-only sighting (8) -> 1 of 5 predicted entities is spurious
    assert m.spurious_entity_rate(PRED_DUP, GOLD_DUP) == pytest.approx(1 / 5)
    assert m.spurious_entity_rate(GOLD_DUP, GOLD_DUP) == 0.0
    assert m.spurious_entity_rate({}, GOLD_DUP) == 0.0


def test_ac_eval_03_false_merge_ignores_spurious_items():
    # a spurious sighting (8) inside X is not a second gold object
    assert m.false_merge_rate({1: "X", 2: "X", 8: "X"}, {1: "A", 2: "A"}) == 0.0


def test_ac_eval_03_false_merge_rate_hand_cases():
    # Y = {s3, s4} spans A and B -> both gold objects are falsely merged -> 2 / 2
    assert m.false_merge_rate(PRED_ER, GOLD_ER) == pytest.approx(1.0)
    # gold A{1,2} B{3} C{4}; pred X{1,2,3} Y{4} -> A and B merged -> 2 / 3
    assert m.false_merge_rate({1: "X", 2: "X", 3: "X", 4: "Y"}, {1: "A", 2: "A", 3: "B", 4: "C"}) == \
        pytest.approx(2 / 3)
    assert m.false_merge_rate(PRED_DUP, GOLD_DUP) == 0.0, "splits are duplicates, not false merges"


# ---------------------------------------------------------------- location top-1 (module + zone)
def test_ac_eval_03_module_of():
    assert m.module_of("LAB1S2", SEED_LOCATIONS) == "LAB1"
    assert m.module_of("LAB1", SEED_LOCATIONS) == "LAB1"
    assert m.module_of("NOD2D4", SEED_LOCATIONS) == "NOD2"
    assert m.module_of("USOS", SEED_LOCATIONS) is None
    assert m.module_of("NOT-A-PLACE", SEED_LOCATIONS) is None


def test_ac_eval_03_location_accuracy_hand_case():
    gold = {"drill": "LAB1P3", "wrench-a": "LAB1S2", "wrench-b": "LAB1O3", "unk": "LAB1O3"}
    pred = {"drill": "LAB1P3",       # module + zone correct
            "wrench-a": "LAB1",      # module correct, zone wrong
            "wrench-b": "NOD2D4",    # both wrong
            "unk": None,             # not found -> wrong
            "extra": "LAB1S2"}       # not in gold -> ignored
    r = m.location_accuracy(pred, gold, SEED_LOCATIONS)
    assert (r.module, r.zone, r.n) == (pytest.approx(0.5), pytest.approx(0.25), 4)


def test_ac_eval_03_location_accuracy_missing_prediction_is_wrong():
    r = m.location_accuracy({}, {"a": "LAB1S2"}, SEED_LOCATIONS)
    assert (r.module, r.zone, r.n) == (0.0, 0.0, 1)


def test_ac_eval_03_location_accuracy_empty_gold():
    r = m.location_accuracy({}, {}, SEED_LOCATIONS)
    assert r.n == 0


# ---------------------------------------------------------------- events (type + entity + |dt| <= tol) and actor
def test_ac_eval_03_event_default_tolerance_is_5s():
    assert m.DEFAULT_EVENT_TOLERANCE_S == 5.0
    assert inspect.signature(m.event_prf).parameters["tolerance_s"].default == 5.0


def test_ac_eval_03_event_prf_hand_case():
    gold = [EventRecord("moved", "drill", 8.0, "crew member, blue shirt"),
            EventRecord("removed", "laptop", 6.0, "crew member, red shirt"),
            EventRecord("moved", "wrench-a", 20.0, None)]
    pred = [EventRecord("moved", "drill", 12.5, "Crew member,  blue shirt"),   # dt 4.5, actor same after normalising
            EventRecord("removed", "laptop", 6.2, "crew member, blue shirt"),  # match, wrong actor
            EventRecord("moved", "wrench-a", 26.0, None),                      # dt 6 > 5 -> FP (gold stays FN)
            EventRecord("used", "drill", 8.0, None)]                           # wrong type -> FP
    r = m.event_prf(pred, gold)
    assert (r.tp, r.fp, r.fn) == (2, 2, 1)
    assert (r.precision, r.recall) == pytest.approx((0.5, 2 / 3))
    assert r.f1 == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
    assert r.actor_accuracy == pytest.approx(0.5)


def test_ac_eval_03_event_tolerance_boundary_inclusive():
    gold = [EventRecord("moved", "e", 10.0)]
    assert m.event_prf([EventRecord("moved", "e", 15.0)], gold).tp == 1
    assert m.event_prf([EventRecord("moved", "e", 4.99)], gold).tp == 0
    assert m.event_prf([EventRecord("moved", "e", 15.0)], gold, tolerance_s=2.0).tp == 0


def test_ac_eval_03_event_requires_same_entity():
    r = m.event_prf([EventRecord("moved", "other", 10.0)], [EventRecord("moved", "e", 10.0)])
    assert (r.tp, r.fp, r.fn) == (0, 1, 1)


def test_ac_eval_03_event_one_to_one_maximum_matching():
    gold = [EventRecord("moved", "e", 0.0), EventRecord("moved", "e", 8.0)]
    pred = [EventRecord("moved", "e", 4.0), EventRecord("moved", "e", 12.0)]
    assert m.event_prf(pred, gold).tp == 2
    dup = m.event_prf([EventRecord("moved", "e", 0.0)] * 2, [EventRecord("moved", "e", 0.0)])
    assert (dup.tp, dup.fp) == (1, 1)


def test_ac_eval_03_actor_accuracy_ignores_gold_without_actor_and_none_when_no_match():
    r = m.event_prf([EventRecord("moved", "e", 0.0, "x")], [EventRecord("moved", "e", 0.0, None)])
    assert r.tp == 1 and r.actor_accuracy is None
    assert m.event_prf([], [EventRecord("moved", "e", 0.0, "x")]).actor_accuracy is None


def test_ac_eval_03_custom_actor_match():
    gold = [EventRecord("moved", "e", 0.0, "blue shirt")]
    pred = [EventRecord("moved", "e", 0.0, "crew member in a blue shirt")]
    assert m.event_prf(pred, gold).actor_accuracy == 0.0
    r = m.event_prf(pred, gold, actor_match=lambda p, g: g in (p or ""))
    assert r.actor_accuracy == 1.0


def test_ac_eval_03_event_no_predictions():
    r = m.event_prf([], [EventRecord("moved", "e", 0.0)])
    assert (r.precision, r.recall, r.f1, r.fn) == (0.0, 0.0, 0.0, 1)


# ---------------------------------------------------------------- unknown-detection recall
def test_ac_eval_03_unknown_recall():
    assert m.unknown_recall({"u1", "u2"}, {"u1", "not-unknown"}) == pytest.approx(0.5)
    assert m.unknown_recall(["u1"], []) == 0.0
    assert m.unknown_recall([], ["x"]) == 1.0, "no gold unknowns: vacuously 1.0"


# ---------------------------------------------------------------- calibration and agreement helpers
def test_ac_eval_03_ece_hand_case():
    # bin [0.9,1.0): acc 0.5 conf 0.9 -> |0.4| * 2/4 ; bin [0.6,0.7): acc 1 conf 0.6 -> |0.4| * 2/4 ; ECE 0.4
    assert m.expected_calibration_error([0.9, 0.9, 0.6, 0.6], [True, False, True, True]) == pytest.approx(0.4)


def test_ac_eval_03_ece_edges():
    assert m.expected_calibration_error([1.0, 1.0], [True, True]) == pytest.approx(0.0)
    assert m.expected_calibration_error([1.0, 1.0], [True, False]) == pytest.approx(0.5)   # 1.0 in the last bin
    assert m.expected_calibration_error([0.75] * 4, [1, 1, 1, 0]) == pytest.approx(0.0)
    assert m.expected_calibration_error([0.9, 0.6], [1, 1], n_bins=1) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        m.expected_calibration_error([0.5], [True, False])
    with pytest.raises(ValueError):
        m.expected_calibration_error([1.2], [True])


def test_ac_eval_03_cohen_kappa_hand_cases():
    # po = 3/4; pe = .5*.25 + .5*.75 = .5 -> kappa = .5
    assert m.cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == pytest.approx(0.5)
    assert m.cohen_kappa([1, 0], [0, 1]) == pytest.approx(-1.0)
    assert m.cohen_kappa([True, True], [True, True]) == 1.0          # pe == 1 and full agreement
    with pytest.raises(ValueError):
        m.cohen_kappa([1], [1, 0])


def test_ac_eval_03_metrics_module_is_pure():
    src = inspect.getsource(m)
    for forbidden in ("psycopg", "httpx", "open(", "requests"):
        assert forbidden not in src, f"metrics must be pure (found {forbidden})"
