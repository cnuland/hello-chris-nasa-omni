"""AC-EVAL-7: decision-model eval. A same-or-new pair set (positives/negatives with crops + context) measures
DiffusionGemma choice accuracy, calibration (ECE) and latency vs the rule baseline."""
from __future__ import annotations

import hashlib
import json
import os

import jsonschema
import pytest
from PIL import Image

from nasa_eval.dataset import load_schema
from nasa_eval.decision_eval import (
    CANDIDATE_OPTION,
    MODEL_INPUT_CONTEXT_KEYS,
    DecisionModelError,
    DecisionOutput,
    DecisionPair,
    RuleBaseline,
    SystemOneDecisionModel,
    compare,
    crop_similarity,
    evaluate_model,
    load_pairs,
)
from nasa_eval.synth import generate_pairs

PAIR_ROW = {"pair_id": "p1", "label": "same",
            "observation": {"crop": "crops/p1-a.png", "description": "grey pistol-grip drill", "location": "LAB1S2",
                            "t": 12.0},
            "candidate": {"crop": "crops/p1-b.png", "label": "pistol-grip drill", "last_location": "LAB1S2",
                          "last_seen_t": 3.0},
            "context": {"similarity": 0.91, "catalog_id": "drill-pistol"}}


def pairs4() -> list[DecisionPair]:
    return [DecisionPair(f"p{i}", lab, {"description": f"obs {i}"}, {"label": f"cand {i}"}, {"similarity": s})
            for i, (lab, s) in enumerate([("same", 0.95), ("same", 0.80), ("new", 0.30), ("new", 0.86)])]


class Scripted:
    """Fake decision model returning scripted outputs in order (or raising)."""

    def __init__(self, outputs, name="fake-dgemma"):
        self.outputs = list(outputs)
        self.name = name

    def decide(self, pair):
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def out(choice, p, ms):
    other = "new" if choice == "same" else "same"
    return DecisionOutput(choice, {choice: p, other: round(1 - p, 6)}, ms)


# ---------------------------------------------------------------- pair set format
def test_ac_eval_07_pairs_schema():
    v = jsonschema.Draft202012Validator(load_schema("pairs"))
    assert list(v.iter_errors(PAIR_ROW)) == []
    for bad in (dict(PAIR_ROW, label="maybe"), {k: v_ for k, v_ in PAIR_ROW.items() if k != "candidate"},
                dict(PAIR_ROW, observation={"description": "no crop"})):
        assert list(v.iter_errors(bad))


def test_ac_eval_07_load_pairs(tmp_path):
    p = tmp_path / "pairs.jsonl"
    p.write_text(json.dumps(PAIR_ROW) + "\n")
    (pair,) = load_pairs(p)
    assert pair.pair_id == "p1" and pair.label == "same" and pair.context["similarity"] == 0.91
    assert pair.observation["description"] == "grey pistol-grip drill"


def test_ac_eval_07_load_pairs_rejects_invalid(tmp_path):
    p = tmp_path / "pairs.jsonl"
    p.write_text(json.dumps(dict(PAIR_ROW, label="maybe")) + "\n")
    with pytest.raises(ValueError):
        load_pairs(p)


def test_ac_eval_07_generate_pairs_balanced_with_crops(tmp_path):
    path = generate_pairs(3, tmp_path, n_positive=6, n_negative=4)
    assert path == tmp_path / "pairs.jsonl"
    pairs = load_pairs(path)
    assert sum(p.label == "same" for p in pairs) == 6 and sum(p.label == "new" for p in pairs) == 4
    assert len({p.pair_id for p in pairs}) == 10
    for p in pairs:
        for side in (p.observation, p.candidate):
            img = Image.open(tmp_path / side["crop"])
            assert img.mode == "RGB"
        assert 0.0 <= p.context["similarity"] <= 1.0
    negs = [p for p in pairs if p.label == "new"]
    # review round: the stratum lives in evaluation-only ``meta`` (never sent to the model), not in ``context``
    assert any(p.context.get("catalog_id") and p.meta.get("hard_negative") for p in negs), \
        "negatives include same-class look-alikes (identical twins)"


def test_ac_eval_07_generate_pairs_deterministic(tmp_path):
    def digest(d):
        return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(d.rglob("*")) if f.is_file()}

    generate_pairs(3, tmp_path / "a", n_positive=3, n_negative=3)
    generate_pairs(3, tmp_path / "b", n_positive=3, n_negative=3)
    assert digest(tmp_path / "a") == digest(tmp_path / "b")


# ---------------------------------------------------------------- rule baseline
def test_ac_eval_07_rule_baseline():
    b = RuleBaseline()
    assert b.name == "rule-baseline"
    p = pairs4()
    assert b.decide(p[0]).choice == "same"
    assert b.decide(p[1]).choice == "new"
    assert b.decide(p[3]).choice == "same"
    edge = DecisionPair("e", "same", context={"similarity": 0.85})
    assert b.decide(edge).choice == "same", "threshold inclusive"
    o = b.decide(p[2])
    assert o.probabilities == pytest.approx({"same": 0.30, "new": 0.70}) and o.latency_ms >= 0
    assert RuleBaseline(threshold=0.5).decide(p[1]).choice == "same"


def test_ac_eval_07_rule_baseline_missing_similarity_is_new():
    assert RuleBaseline().decide(DecisionPair("x", "new")).choice == "new"


# ---------------------------------------------------------------- accuracy / ECE / latency
def test_ac_eval_07_evaluate_model_hand_case():
    # labels same, same, new, new ; choices same(.9) new(.9 wrong) new(.6) new(.6) -> acc .75 ; ECE .4
    model = Scripted([out("same", 0.9, 100), out("new", 0.9, 200), out("new", 0.6, 300), out("new", 0.6, 400)])
    r = evaluate_model(pairs4(), model)
    assert r["n"] == 4 and r["n_errors"] == 0
    assert r["accuracy"] == pytest.approx(0.75)
    assert r["ece"] == pytest.approx(0.4)
    assert r["latency_p50_ms"] == pytest.approx(250)
    assert r["latency_mean_ms"] == pytest.approx(250)
    assert 385 <= r["latency_p95_ms"] <= 400


def test_ac_eval_07_errors_count_as_wrong():
    model = Scripted([out("same", 0.9, 10), DecisionModelError("timeout"), out("new", 0.6, 10),
                      DecisionOutput("maybe", {"maybe": 1.0}, 10)])
    r = evaluate_model(pairs4(), model)
    assert r["n"] == 4 and r["n_errors"] == 2
    assert r["accuracy"] == pytest.approx(0.5)


def test_ac_eval_07_compare_against_baseline():
    model = Scripted([out("same", 0.9, 120), out("same", 0.7, 120), out("new", 0.8, 120), out("new", 0.9, 120)])
    rep = compare(pairs4(), model, RuleBaseline())
    assert rep["model"]["accuracy"] == pytest.approx(1.0)
    # baseline: p0 same ok, p1 new wrong, p2 new ok, p3 same wrong -> 0.5
    assert rep["baseline"]["accuracy"] == pytest.approx(0.5)
    assert rep["delta"]["accuracy"] == pytest.approx(0.5)
    assert rep["delta"]["ece"] == pytest.approx(rep["model"]["ece"] - rep["baseline"]["ece"])
    assert rep["delta"]["latency_p50_ms"] == pytest.approx(rep["model"]["latency_p50_ms"]
                                                           - rep["baseline"]["latency_p50_ms"])
    assert rep["model"]["name"] == "fake-dgemma" and rep["baseline"]["name"] == "rule-baseline"


def test_ac_eval_07_empty_pair_set_rejected():
    with pytest.raises(ValueError):
        evaluate_model([], RuleBaseline())


# ---------------------------------------------------------------- DiffusionGemma via /v1/systemone (fake server)
def _answer(choice, probs):
    return {"answers": {"same_or_new": {"choice": choice, "probabilities": probs}}}


def test_ac_eval_07_systemone_client(fake_http):
    """Review round: the request is the production AC-PER-8/12 question (choice over candidate ids + NEW, criteria
    dict, Jev systemone shape), not a hand-written binary same/new question. The k=1 case: one candidate + NEW."""
    fake_http.route("POST", "/v1/systemone",
                    lambda body: (200, _answer(CANDIDATE_OPTION, {CANDIDATE_OPTION: 0.83, "NEW": 0.17})))
    m = SystemOneDecisionModel(fake_http.url, "diffusiongemma-26b")
    assert m.name == "diffusiongemma-26b"
    pair = DecisionPair("p1", "same", PAIR_ROW["observation"], PAIR_ROW["candidate"], PAIR_ROW["context"])
    o = m.decide(pair)
    assert o.choice == "same" and o.probabilities == pytest.approx({"same": 0.83, "new": 0.17})
    assert o.latency_ms > 0
    body = fake_http.requests[-1]["body"]
    assert body["model"] == "diffusiongemma-26b"
    assert isinstance(body["instructions"], str) and body["instructions"]
    assert isinstance(body["state"], dict)
    assert list(body["questions"]) == ["same_or_new"]
    q = body["questions"]["same_or_new"]
    assert q["type"] == "choice" and set(q["criteria"]) == {CANDIDATE_OPTION, "NEW"}
    sent = json.dumps(body["state"])
    assert "grey pistol-grip drill" in sent and "pistol-grip drill" in sent
    nw = SystemOneDecisionModel(fake_http.url, "dg")
    fake_http.route("POST", "/v1/systemone", lambda body: (200, _answer("NEW", {CANDIDATE_OPTION: 0.1, "NEW": 0.9})))
    assert nw.decide(pair).choice == "new"


@pytest.mark.parametrize("status,payload", [
    (500, {"error": "boom"}),
    (200, {"answers": {"same_or_new": {"choice": "Z", "probabilities": {"Z": 1.0}}}}),
    (200, {"unexpected": True}),
])
def test_ac_eval_07_systemone_bad_responses_raise(fake_http, status, payload):
    fake_http.route("POST", "/v1/systemone", lambda body: (status, payload))
    with pytest.raises(DecisionModelError):
        SystemOneDecisionModel(fake_http.url, "dg").decide(DecisionPair("p", "same"))


def test_ac_eval_07_systemone_unreachable_raises():
    with pytest.raises(DecisionModelError):
        SystemOneDecisionModel("http://127.0.0.1:9", "dg", timeout_s=0.5).decide(DecisionPair("p", "same"))


def test_ac_eval_07_cli_decision_eval_writes_report(fake_http, tmp_path):
    from nasa_eval.cli import main

    fake_http.route("POST", "/v1/systemone",
                    lambda body: (200, _answer(CANDIDATE_OPTION, {CANDIDATE_OPTION: 0.7, "NEW": 0.3})))
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text(json.dumps(PAIR_ROW) + "\n" + json.dumps(dict(PAIR_ROW, pair_id="p2", label="new")) + "\n")
    out_path = tmp_path / "decision.json"
    rc = main(["decision-eval", "--pairs", str(pairs), "--decision-url", fake_http.url, "--model", "dg",
               "--out", str(out_path)])
    assert rc == 0
    rep = json.loads(out_path.read_text())
    assert rep["model"]["accuracy"] == pytest.approx(0.5) and rep["model"]["n"] == 2
    assert set(rep) >= {"model", "baseline", "delta"}


def test_ac_eval_07_cli_decision_eval_requires_url(tmp_path, monkeypatch):
    from nasa_eval.cli import main

    monkeypatch.delenv("DECISION_URL", raising=False)
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text(json.dumps(PAIR_ROW) + "\n")
    assert main(["decision-eval", "--pairs", str(pairs), "--model", "dg", "--out", str(tmp_path / "o.json")]) == 2


@pytest.mark.model
@pytest.mark.skipif(not os.environ.get("DECISION_URL"), reason="needs a live DiffusionGemma at DECISION_URL")
def test_ac_eval_07_live_diffusiongemma_beats_baseline(tmp_path):
    path = generate_pairs(1, tmp_path, n_positive=25, n_negative=25)
    rep = compare(load_pairs(path), SystemOneDecisionModel(os.environ["DECISION_URL"],
                                                           os.environ.get("DECISION_MODEL", "diffusiongemma")),
                  RuleBaseline())
    assert rep["model"]["n_errors"] == 0


# ---------------------------------------------------------------- review round: no label leakage, crops really used
LEAK_MARKERS = ("hard_negative", "meta", "label\": \"same", "label\": \"new", "class_negative", "positive")


def test_ac_eval_07_generated_context_carries_no_label_correlated_fields(tmp_path):
    pairs = load_pairs(generate_pairs(5, tmp_path, n_positive=20, n_negative=20))
    for p in pairs:
        assert set(p.context) <= set(MODEL_INPUT_CONTEXT_KEYS), p.context
        assert p.context.get("catalog_id"), "catalog_id is unconditional (observation class)"
        assert p.meta["kind"] in ("positive", "hard_negative", "class_negative")
        assert p.meta["hard_negative"] == (p.meta["kind"] == "hard_negative")
    # the leak rule from the review ('new iff hard_negative or catalog_id is null') has nothing to read any more
    leak = sum(("new" if (p.context.get("hard_negative") or p.context.get("catalog_id") is None) else "same") == p.label
               for p in pairs) / len(pairs)
    assert leak == pytest.approx(0.5)


def test_ac_eval_07_similarity_is_computed_from_the_crops(tmp_path):
    path = generate_pairs(5, tmp_path, n_positive=6, n_negative=6)
    for p in load_pairs(path):
        sim = crop_similarity(tmp_path / p.observation["crop"], tmp_path / p.candidate["crop"])
        assert p.context["similarity"] == pytest.approx(round(sim, 4))
    same = crop_similarity(tmp_path / p.observation["crop"], tmp_path / p.observation["crop"])
    assert same == pytest.approx(1.0)


def test_ac_eval_07_request_never_contains_label_metadata_and_carries_crops(tmp_path, fake_http):
    path = generate_pairs(5, tmp_path, n_positive=8, n_negative=8)
    fake_http.route("POST", "/v1/systemone", lambda body: (200, _answer("NEW", {"NEW": 0.9})))
    m = SystemOneDecisionModel(fake_http.url, "dg")
    pairs = load_pairs(path)
    for p in pairs:
        m.decide(p)
    assert len(fake_http.requests) == len(pairs)
    for req, p in zip(fake_http.requests, pairs, strict=True):
        state = req["body"]["state"]
        assert state["observation"]["image"].startswith("data:image/png;base64,")
        assert state["candidates"][0]["image"].startswith("data:image/png;base64,")
        stripped = json.loads(json.dumps(req["body"]))
        stripped["state"]["observation"].pop("image")
        stripped["state"]["candidates"][0].pop("image")
        raw = json.dumps(stripped)          # base64 pixels could contain any short string by chance
        for marker in LEAK_MARKERS:
            assert marker not in raw, (marker, p.pair_id)
        assert p.pair_id not in raw, "pair ids may encode the label ordering"
        assert "crops/" not in raw, "crop paths are useless to the model; the pixels are sent"


def test_ac_eval_07_legacy_hard_negative_in_context_moves_to_meta(tmp_path, fake_http):
    from PIL import Image as _Image

    for n in ("a.png", "b.png"):
        _Image.new("RGB", (8, 8), (1, 2, 3)).save(tmp_path / n)
    row = {"pair_id": "x", "label": "new", "observation": {"crop": "a.png"}, "candidate": {"crop": "b.png"},
           "context": {"similarity": 0.8, "catalog_id": None, "hard_negative": True}}
    (tmp_path / "pairs.jsonl").write_text(json.dumps(row) + "\n")
    (pair,) = load_pairs(tmp_path / "pairs.jsonl")
    assert "hard_negative" not in pair.context and pair.meta["hard_negative"] is True
    fake_http.route("POST", "/v1/systemone", lambda body: (200, _answer("NEW", {"NEW": 0.9})))
    SystemOneDecisionModel(fake_http.url, "dg").decide(pair)
    assert "hard_negative" not in json.dumps(fake_http.requests[-1]["body"])


def test_ac_eval_07_crop_paths_confined_to_pairs_dir(tmp_path):
    row = dict(PAIR_ROW, observation={"crop": "../../etc/passwd"})
    (tmp_path / "pairs.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="outside"):
        load_pairs(tmp_path / "pairs.jsonl")
    row = dict(PAIR_ROW, observation={"crop": "/etc/passwd"})
    (tmp_path / "pairs.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="outside"):
        load_pairs(tmp_path / "pairs.jsonl")


def test_ac_eval_07_accuracy_reported_per_stratum():
    pairs = [DecisionPair("a", "same", meta={"kind": "positive"}), DecisionPair("b", "new", meta={"kind": "hard_negative"}),
             DecisionPair("c", "new", meta={"kind": "hard_negative"}), DecisionPair("d", "new")]
    model = Scripted([out("same", 0.9, 1), out("same", 0.9, 1), out("new", 0.9, 1), out("new", 0.9, 1)])
    r = evaluate_model(pairs, model)
    assert r["accuracy_by_kind"] == pytest.approx({"positive": 1.0, "hard_negative": 0.5, "unlabelled": 1.0})


def test_ac_eval_07_request_matches_perception_decision_client(fake_http, perception_on_path):
    """Contract test: eval sends byte-for-byte the same systemone body as perception.decision.DecisionClient."""
    from perception.decision import DecisionClient

    from nasa_eval.decision_eval import build_question

    fake_http.route("POST", "/v1/systemone", lambda body: (200, _answer("NEW", {"NEW": 0.9})))
    pair = DecisionPair("p1", "same", PAIR_ROW["observation"], PAIR_ROW["candidate"], PAIR_ROW["context"])
    SystemOneDecisionModel(fake_http.url, "dg").decide(pair)
    ours = fake_http.requests[-1]["body"]
    question, options, state, instructions = build_question(pair)
    DecisionClient(fake_http.url, model="dg").choose(question, options, state=state, instructions=instructions)
    theirs = fake_http.requests[-1]["body"]
    assert ours == theirs
