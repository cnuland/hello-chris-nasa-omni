"""AC-EVAL-4: chatbot QA eval. qa.jsonl (question, expected entity/location/actor, answer type); exact scoring on
entity/location ids from citations; optional LLM judge for free text (judge != generator) with rubric and agreement
logging."""
from __future__ import annotations

import json

import jsonschema
import pytest
from conftest import SEED_LOCATIONS, chat_completion, write_dataset

from nasa_eval.dataset import QAItem, load_schema, validate_dataset
from nasa_eval.qa import (
    ANSWER_TYPES,
    AgentClient,
    AgentError,
    JudgeVerdict,
    OpenAIJudge,
    load_qa,
    score_qa,
    score_qa_item,
)

ENTITY_TO_GOLD = {"e-drill": "drill-1", "e-dup-drill": "drill-1", "e-wa": "wrench-a", "e-crew": "crew-blue"}
LOCS = set(SEED_LOCATIONS)

Q_ENTITY = QAItem("q1", "Which item was moved to the port rack?", "entity", {"entities": ["drill-1"]})
Q_LOC = QAItem("q2", "Where is the drill?", "location", {"location": "LAB1P3"})
Q_ACTOR = QAItem("q3", "Who moved the drill?", "actor", {"actor": "crew member, blue shirt"})
Q_FREE = QAItem("q4", "Describe the drill", "free_text", {}, rubric="Mentions pistol grip and that it is in Destiny")


def resp(reply: str = "", citations=()) -> dict:
    return {"reply": reply, "citations": list(citations), "tool_calls": []}


class StubJudge:
    def __init__(self, model: str = "judge-model", passed: bool | None = True, error: str | None = None):
        self.model = model
        self.calls: list[dict] = []
        self._passed, self._error = passed, error

    def judge(self, *, question, reply, rubric, reference=None):
        self.calls.append({"question": question, "reply": reply, "rubric": rubric})
        if self._error:
            return JudgeVerdict(None, None, "", self.model, error=self._error)
        return JudgeVerdict(1.0 if self._passed else 0.0, self._passed, "ok", self.model)


# ---------------------------------------------------------------- qa.jsonl format
def test_ac_eval_04_answer_types():
    assert set(ANSWER_TYPES) == {"entity", "location", "actor", "free_text"}


@pytest.mark.parametrize("row,ok", [
    ({"qa_id": "a", "question": "q", "answer_type": "entity", "expected": {"entities": ["drill-1"]}}, True),
    ({"qa_id": "a", "question": "q", "answer_type": "location", "expected": {"location": "LAB1"}}, True),
    ({"qa_id": "a", "question": "q", "answer_type": "actor", "expected": {"actor": "blue shirt"}}, True),
    ({"qa_id": "a", "question": "q", "answer_type": "free_text", "expected": {}, "rubric": "r"}, True),
    ({"qa_id": "a", "question": "q", "answer_type": "entity", "expected": {"entities": []}}, False),
    ({"qa_id": "a", "question": "q", "answer_type": "location", "expected": {}}, False),
    ({"qa_id": "a", "question": "q", "answer_type": "actor", "expected": {}}, False),
    ({"qa_id": "a", "question": "q", "answer_type": "free_text", "expected": {}}, False),     # rubric required
    ({"qa_id": "a", "question": "q", "answer_type": "poem", "expected": {}}, False),
    ({"question": "q", "answer_type": "entity", "expected": {"entities": ["x"]}}, False),
])
def test_ac_eval_04_qa_schema(row, ok):
    errors = list(jsonschema.Draft202012Validator(load_schema("qa")).iter_errors(row))
    assert (errors == []) is ok, errors


def test_ac_eval_04_qa_expected_entities_must_exist_in_gold(tmp_path):
    qa = [{"qa_id": "a", "question": "q", "answer_type": "entity", "expected": {"entities": ["ghost-9"]}}]
    errors = validate_dataset(write_dataset(tmp_path, qa=qa))
    assert any("ghost-9" in e for e in errors)


def test_ac_eval_04_load_qa(tmp_path):
    p = tmp_path / "qa.jsonl"
    p.write_text(json.dumps({"qa_id": "q2", "question": "Where is the drill?", "answer_type": "location",
                             "expected": {"location": "LAB1P3"}}) + "\n\n")
    assert load_qa(p) == [Q_LOC]


# ---------------------------------------------------------------- exact scoring from citations
def test_ac_eval_04_entity_correct_when_expected_cited():
    r = score_qa_item(Q_ENTITY, resp("It was the drill.", [{"entity_id": "e-drill", "at": "t"}]),
                      entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r.correct is True and r.qa_id == "q1" and r.answer_type == "entity"


def test_ac_eval_04_entity_wrong_when_not_cited_even_if_named_in_text():
    r = score_qa_item(Q_ENTITY, resp("It was drill-1, the drill.", [{"entity_id": "e-wa", "at": "t"}]),
                      entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r.correct is False


def test_ac_eval_04_entity_duplicate_entity_of_same_gold_counts():
    r = score_qa_item(Q_ENTITY, resp("", [{"entity_id": "e-dup-drill", "at": "t"}]),
                      entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r.correct is True


def test_ac_eval_04_entity_multiple_expected_all_required():
    q = QAItem("q", "What tools are in the lab?", "entity", {"entities": ["drill-1", "wrench-a"]})
    one = score_qa_item(q, resp("", [{"entity_id": "e-drill"}]), entity_to_gold=ENTITY_TO_GOLD)
    both = score_qa_item(q, resp("", [{"entity_id": "e-drill"}, {"entity_id": "e-wa"}, {"window_id": "w1"}]),
                         entity_to_gold=ENTITY_TO_GOLD)
    assert one.correct is False and both.correct is True


def test_ac_eval_04_unmapped_citations_ignored_not_crash():
    r = score_qa_item(Q_ENTITY, resp("", [{"window_id": "w1", "at": "t"}, {"entity_id": "e-nobody"}]),
                      entity_to_gold=ENTITY_TO_GOLD)
    assert r.correct is False


def test_ac_eval_04_no_citations_is_wrong():
    assert score_qa_item(Q_ENTITY, resp("the drill"), entity_to_gold=ENTITY_TO_GOLD).correct is False
    assert score_qa_item(Q_ENTITY, {"reply": "x"}, entity_to_gold=ENTITY_TO_GOLD).correct is False


def test_ac_eval_04_location_from_citation_field():
    r = score_qa_item(Q_LOC, resp("It is in the lab.", [{"entity_id": "e-drill", "location_id": "LAB1P3"}]),
                      entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r.correct is True


def test_ac_eval_04_location_exact_id_token_in_reply():
    ok = score_qa_item(Q_LOC, resp("The drill is at LAB1P3 (Destiny port rack 3).", [{"entity_id": "e-drill"}]),
                       entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert ok.correct is True
    module_only = score_qa_item(Q_LOC, resp("The drill is in LAB1.", [{"entity_id": "e-drill"}]),
                                entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert module_only.correct is False, "exact id, not module-level"
    wrong = score_qa_item(Q_LOC, resp("It is in LAB1S2.", []), entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert wrong.correct is False


def test_ac_eval_04_location_conflicting_ids_is_wrong():
    r = score_qa_item(Q_LOC, resp("Either LAB1P3 or NOD2D4.", []), entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r.correct is False, "hedging across several locations is not a top-1 answer"


def test_ac_eval_04_actor_by_descriptor_or_cited_person():
    by_text = score_qa_item(Q_ACTOR, resp("The CREW MEMBER, blue shirt moved it.", []), entity_to_gold=ENTITY_TO_GOLD)
    assert by_text.correct is True
    q = QAItem("q", "Who moved it?", "actor", {"actor": "crew-blue"})
    by_cite = score_qa_item(q, resp("Someone.", [{"entity_id": "e-crew"}]), entity_to_gold=ENTITY_TO_GOLD)
    assert by_cite.correct is True
    wrong = score_qa_item(Q_ACTOR, resp("The crew member in the red shirt.", []), entity_to_gold=ENTITY_TO_GOLD)
    assert wrong.correct is False


def test_ac_eval_04_free_text_without_judge_is_skipped():
    r = score_qa_item(Q_FREE, resp("A pistol-grip drill in Destiny."), entity_to_gold=ENTITY_TO_GOLD)
    assert r.correct is None


def test_ac_eval_04_free_text_with_judge_uses_rubric():
    j = StubJudge(passed=True)
    r = score_qa_item(Q_FREE, resp("A pistol-grip drill in Destiny."), entity_to_gold=ENTITY_TO_GOLD, judge=j)
    assert r.correct is True
    assert j.calls == [{"question": Q_FREE.question, "reply": "A pistol-grip drill in Destiny.",
                        "rubric": Q_FREE.rubric}]


def test_ac_eval_04_judge_error_is_unscored_not_crash():
    r = score_qa_item(Q_FREE, resp("x"), entity_to_gold=ENTITY_TO_GOLD, judge=StubJudge(error="bad json"))
    assert r.correct is None and "bad json" in json.dumps(r.detail)


def test_ac_eval_04_judge_not_used_for_exact_types():
    j = StubJudge(passed=True)
    score_qa_item(Q_ENTITY, resp("", []), entity_to_gold=ENTITY_TO_GOLD, judge=j)
    assert j.calls == []


# ---------------------------------------------------------------- aggregate + judge != generator + agreement log
def test_ac_eval_04_score_qa_aggregates():
    items = [Q_ENTITY, Q_LOC, Q_ACTOR, Q_FREE]
    responses = {"q1": resp("", [{"entity_id": "e-drill"}]), "q2": resp("in NOD2D4", []),
                 "q3": resp("crew member, blue shirt", []), "q4": resp("drill")}
    rep = score_qa(items, responses, entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert rep["n"] == 4 and rep["n_scored"] == 3 and rep["n_skipped"] == 1
    assert rep["accuracy"] == pytest.approx(2 / 3)
    assert rep["accuracy_by_type"]["entity"] == 1.0 and rep["accuracy_by_type"]["location"] == 0.0
    assert [r.qa_id for r in rep["results"]] == ["q1", "q2", "q3", "q4"]


def test_ac_eval_04_missing_response_counts_wrong():
    rep = score_qa([Q_ENTITY], {}, entity_to_gold=ENTITY_TO_GOLD)
    assert rep["n_scored"] == 1 and rep["accuracy"] == 0.0


def test_ac_eval_04_judge_must_differ_from_generator():
    with pytest.raises(ValueError):
        score_qa([Q_FREE], {"q4": resp("x")}, entity_to_gold={}, judge=StubJudge(model="qwen3-8b"),
                 generator_model="qwen3-8b")


def test_ac_eval_04_openai_judge_rejects_same_model():
    with pytest.raises(ValueError):
        OpenAIJudge("http://127.0.0.1:9", "qwen3-8b", generator_model="qwen3-8b")


def test_ac_eval_04_agreement_log_and_kappa(tmp_path):
    items = [QAItem(f"f{i}", f"q{i}", "free_text", {}, rubric="r") for i in range(4)]
    responses = {f"f{i}": resp(f"a{i}") for i in range(4)}

    class SeqJudge(StubJudge):
        verdicts = iter([True, True, False, False])

        def judge(self, **kw):
            super().judge(**kw)
            p = next(self.verdicts)
            return JudgeVerdict(float(p), p, "r", self.model)

    log = tmp_path / "agreement.jsonl"
    human = {"f0": True, "f1": False, "f2": False, "f3": False}
    rep = score_qa(items, responses, entity_to_gold={}, judge=SeqJudge(), generator_model="qwen3-8b",
                   human_labels=human, agreement_log=log)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["qa_id"] for r in rows] == ["f0", "f1", "f2", "f3"]
    for r in rows:
        assert r["judge_model"] == "judge-model" and r["generator_model"] == "qwen3-8b"
        assert r["rubric"] == "r" and "passed" in r and "score" in r and "human_label" in r
    agr = rep["judge_agreement"]
    assert agr["n"] == 4 and agr["raw_agreement"] == pytest.approx(0.75) and agr["cohen_kappa"] == pytest.approx(0.5)


def test_ac_eval_04_agreement_log_appends(tmp_path):
    log = tmp_path / "agreement.jsonl"
    for _ in range(2):
        score_qa([Q_FREE], {"q4": resp("x")}, entity_to_gold={}, judge=StubJudge(), generator_model="g",
                 agreement_log=log)
    assert len(log.read_text().splitlines()) == 2


def test_ac_eval_04_no_human_labels_no_kappa(tmp_path):
    rep = score_qa([Q_FREE], {"q4": resp("x")}, entity_to_gold={}, judge=StubJudge(), generator_model="g",
                   reports_dir=tmp_path)
    assert rep["judge_agreement"] is None or rep["judge_agreement"].get("cohen_kappa") is None


# ---------------------------------------------------------------- HTTP clients against fake servers
def test_ac_eval_04_openai_judge_http(fake_http):
    fake_http.route("POST", "/v1/chat/completions",
                    lambda body: (200, chat_completion('{"score": 0.8, "passed": true, "rationale": "fine"}')))
    j = OpenAIJudge(fake_http.url, "judge-llm", generator_model="qwen3-8b")
    v = j.judge(question="Describe the drill", reply="A drill", rubric="RUBRIC-XYZ", reference="ref")
    assert v == JudgeVerdict(0.8, True, "fine", "judge-llm", None)
    req = fake_http.requests[-1]
    assert req["body"]["model"] == "judge-llm"
    sent = json.dumps(req["body"]["messages"])
    assert "RUBRIC-XYZ" in sent and "A drill" in sent and "Describe the drill" in sent


def test_ac_eval_04_openai_judge_tolerates_fenced_json(fake_http):
    content = 'Verdict:\n```json\n{"score": 0.2, "passed": false, "rationale": "missing"}\n```'
    fake_http.route("POST", "/v1/chat/completions", lambda body: (200, chat_completion(content)))
    v = OpenAIJudge(fake_http.url, "j", generator_model="g").judge(question="q", reply="r", rubric="x")
    assert v.passed is False and v.score == pytest.approx(0.2) and v.error is None


@pytest.mark.parametrize("status,content", [(200, "I think it's fine"), (200, '{"score": 7, "passed": "yes"}'),
                                            (500, None)])
def test_ac_eval_04_openai_judge_bad_output_is_error_verdict(fake_http, status, content):
    fake_http.route("POST", "/v1/chat/completions",
                    lambda body: (status, chat_completion(content) if content else {"error": "boom"}))
    v = OpenAIJudge(fake_http.url, "j", generator_model="g").judge(question="q", reply="r", rubric="x")
    assert v.error and v.passed is None and v.score is None


def test_ac_eval_04_agent_client_posts_chat(fake_http):
    answer = {"reply": "In LAB1P3", "citations": [{"entity_id": "e-drill", "at": "2026-10-01T12:00:10Z"}],
              "tool_calls": [{"name": "where_is"}]}
    fake_http.route("POST", "/chat", lambda body: (200, answer))
    out = AgentClient(fake_http.url).ask("Where is the drill?")
    assert out == answer
    body = fake_http.requests[-1]["body"]
    assert body["messages"] == [{"role": "user", "content": "Where is the drill?"}]


def test_ac_eval_04_agent_client_http_error_raises(fake_http):
    fake_http.route("POST", "/chat", lambda body: (503, {"error": "down"}))
    with pytest.raises(AgentError):
        AgentClient(fake_http.url).ask("Where is the drill?")


# ---------------------------------------------------------------- review round: scoring cannot be gamed
CREW = ("crew member, blue shirt", "crew member, red shirt", "crew member, green shirt")


def test_ac_eval_04_entity_over_citation_is_wrong():
    everything = [{"entity_id": e} for e in ("e-drill", "e-wa", "e-crew")]
    r = score_qa_item(Q_ENTITY, resp("", everything), entity_to_gold=ENTITY_TO_GOLD)
    assert r.correct is False, "citing every entity must not score"
    assert r.detail["citation_precision"] == pytest.approx(1 / 3) and r.detail["citation_recall"] == 1.0


def test_ac_eval_04_entity_unmapped_entity_citation_counts_against_precision():
    r = score_qa_item(Q_ENTITY, resp("", [{"entity_id": "e-drill"}, {"entity_id": "e-nobody"}]),
                      entity_to_gold=ENTITY_TO_GOLD)
    assert r.correct is False and r.detail["citation_precision"] == pytest.approx(0.5)


def test_ac_eval_04_actor_listing_every_descriptor_is_wrong():
    reply = "It was the " + " or the ".join(CREW) + "."
    r = score_qa_item(Q_ACTOR, resp(reply), entity_to_gold=ENTITY_TO_GOLD, actor_descriptors=CREW)
    assert r.correct is False
    ok = score_qa_item(Q_ACTOR, resp("The crew member, blue shirt moved it."), entity_to_gold=ENTITY_TO_GOLD,
                       actor_descriptors=CREW)
    assert ok.correct is True


def test_ac_eval_04_actor_generic_descriptor_inside_specific_one_is_not_a_second_actor():
    r = score_qa_item(Q_ACTOR, resp("crew member, blue shirt"), entity_to_gold=ENTITY_TO_GOLD,
                      actor_descriptors=(*CREW, "crew member"))
    assert r.correct is True


def test_ac_eval_04_location_uses_most_recent_citation():
    """A 'moved' answer cites the old and the new location: the most recent citation (by 'at') is the answer."""
    cites = [{"entity_id": "e-drill", "at": "2026-10-01T12:00:00Z", "location_id": "LAB1S2"},
             {"entity_id": "e-drill", "at": "2026-10-01T12:00:10Z", "location_id": "LAB1P3"}]
    r = score_qa_item(Q_LOC, resp("It was moved from LAB1S2 to LAB1P3.", cites), entity_to_gold=ENTITY_TO_GOLD,
                      location_ids=LOCS)
    assert r.correct is True and r.detail["answer"] == "LAB1P3"
    s2_latest = [dict(cites[0], at="2026-10-01T12:00:20Z"), cites[1]]   # now LAB1S2 is the most recent citation
    r2 = score_qa_item(Q_LOC, resp("", s2_latest), entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r2.correct is False


def test_ac_eval_04_location_conflicting_citations_without_time_is_hedging():
    cites = [{"entity_id": "e-drill", "location_id": "LAB1S2"}, {"entity_id": "e-drill", "location_id": "LAB1P3"}]
    r = score_qa_item(Q_LOC, resp("", cites), entity_to_gold=ENTITY_TO_GOLD, location_ids=LOCS)
    assert r.correct is False


# ---------------------------------------------------------------- review round: judge independence + agreement log
def test_ac_eval_04_judge_requires_generator_model():
    for gen in (None, "", "  "):
        with pytest.raises(ValueError, match="generator"):
            score_qa([Q_FREE], {"q4": resp("x")}, entity_to_gold={}, judge=StubJudge(), generator_model=gen)
    with pytest.raises(ValueError, match="generator"):
        OpenAIJudge("http://127.0.0.1:9", "judge", generator_model="")


def test_ac_eval_04_agreement_log_written_by_default(tmp_path):
    score_qa([Q_FREE], {"q4": resp("x")}, entity_to_gold={}, judge=StubJudge(), generator_model="g",
             reports_dir=tmp_path, dataset_name="synth-7")
    logs = list((tmp_path / "qa").glob("*-synth-7-judge.jsonl"))
    assert len(logs) == 1 and json.loads(logs[0].read_text().splitlines()[0])["qa_id"] == "q4"


# ---------------------------------------------------------------- review round: runnable QA path
def test_ac_eval_04_ask_all_collects_agent_responses(fake_http):
    from nasa_eval.qa import ask_all

    fake_http.route("POST", "/chat", lambda body: (200, resp("ok", [{"entity_id": "e-drill"}])))
    out = ask_all([Q_ENTITY, Q_LOC], AgentClient(fake_http.url))
    assert set(out) == {"q1", "q2"} and out["q1"]["citations"] == [{"entity_id": "e-drill"}]
    fake_http.route("POST", "/chat", lambda body: (503, {"error": "down"}))
    failed = ask_all([Q_ENTITY], AgentClient(fake_http.url))
    assert failed["q1"].get("error"), "an agent failure is recorded and scored as wrong, not raised"


def test_ac_eval_04_cli_qa_scores_against_a_run_report(tmp_path, fake_http):
    from nasa_eval.cli import main

    qa = [{"qa_id": "q1", "question": "Which item was moved to the port rack?", "answer_type": "entity",
           "expected": {"entities": ["drill-1"]}},
          {"qa_id": "q2", "question": "Where is the drill?", "answer_type": "location",
           "expected": {"location": "LAB1P3"}}]
    ds = write_dataset(tmp_path / "datasets", qa=qa)
    run_report = tmp_path / "20261017T090000Z-tiny.json"
    run_report.write_text(json.dumps({"kind": "pipeline", "dataset": "tiny", "metrics": {},
                                      "entity_to_gold": {"e-drill": "drill-1"}, "database": "eval_abc"}))

    def chat(body):
        q = body["messages"][-1]["content"]
        if "Where" in q:
            return 200, resp("It is in LAB1P3.", [{"entity_id": "e-drill", "location_id": "LAB1P3"}])
        return 200, resp("The drill.", [{"entity_id": "e-drill"}])

    fake_http.route("POST", "/chat", chat)
    out = tmp_path / "reports"
    rc = main(["qa", "--dataset", str(ds), "--run-report", str(run_report), "--agent-url", fake_http.url,
               "--reports-dir", str(out)])
    assert rc == 0
    (rep_path,) = (out / "qa").glob("*-tiny-qa.json")
    rep = json.loads(rep_path.read_text())
    assert rep["kind"] == "qa" and rep["accuracy"] == pytest.approx(1.0) and rep["n"] == 2
    assert "metrics" not in rep, "QA reports are never mistaken for pipeline reports by the gate"
