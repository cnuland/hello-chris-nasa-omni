"""AC-PER-12 Decision client: /v1/systemone (choice) or chat fallback; returns {choice, probabilities, latency_ms,
model}; every call is logged into `decisions` of the ingest payload; timeout/error -> NEW with probabilities={} and
the item marked for review."""
from __future__ import annotations

import json
import math

import httpx
import pytest

from perception import NEW
from perception.decision import DecisionClient
from perception.types import DecisionResult

OPTIONS = {"e1": "laptop seen in LAB1 an hour ago", "e2": "laptop seen in NOD2 yesterday", NEW: "a different laptop"}


class Server:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        r = self.handler(request)
        if isinstance(r, Exception):
            raise r
        return r

    def body(self, i=0):
        return json.loads(self.requests[i].content)


def systemone_answer(choice, probs, name=None):
    def handler(request):
        body = json.loads(request.content)
        q = name or next(iter(body["questions"]))
        answer = {"type": "choice", "choice": choice, "confidence": probs[choice], "probabilities": probs}
        return httpx.Response(200, json={"answers": {q: answer}})
    return handler


def make(server, **kw) -> DecisionClient:
    return DecisionClient("http://decision.test", model="diffusiongemma",
                          http_client=httpx.Client(transport=httpx.MockTransport(server)), **kw)


def test_ac_per_12_systemone_choice_request_shape():
    srv = Server(systemone_answer("e1", {"e1": 0.8, "e2": 0.1, NEW: 0.1}))
    make(srv).choose("same_or_new", OPTIONS, state={"observation": {"label": "laptop"}})
    req = srv.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/systemone"
    body = srv.body()
    assert body["model"] == "diffusiongemma"
    assert body["state"] == {"observation": {"label": "laptop"}}
    (_qname, q), = body["questions"].items()
    assert q["type"] == "choice"
    assert list(q["criteria"].keys()) == ["e1", "e2", NEW]


def test_ac_per_12_parses_choice_probabilities_latency_model():
    srv = Server(systemone_answer("e1", {"e1": 0.8, "e2": 0.15, NEW: 0.05}))
    res = make(srv).choose("same_or_new", OPTIONS)
    assert isinstance(res, DecisionResult)
    assert res.choice == "e1"
    assert res.probabilities == pytest.approx({"e1": 0.8, "e2": 0.15, NEW: 0.05})
    assert isinstance(res.latency_ms, int) and res.latency_ms >= 0
    assert res.model == "diffusiongemma"
    assert res.needs_review is False


def test_ac_per_12_list_options_accepted():
    srv = Server(systemone_answer("question", {"statement": 0.1, "question": 0.85, "other": 0.05}))
    res = make(srv).choose("speech_act", ["statement", "question", "other"])
    assert res.choice == "question"
    assert set(srv.body()["questions"][next(iter(srv.body()["questions"]))]["criteria"]) == {"statement", "question",
                                                                                              "other"}


def test_ac_per_12_timeout_returns_new_with_empty_probabilities_and_review():
    srv = Server(lambda r: httpx.ReadTimeout("slow"))
    res = make(srv).choose("same_or_new", OPTIONS)
    assert res.choice == NEW and res.probabilities == {} and res.needs_review is True


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="engine dead"),
    httpx.Response(503, text="loading"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"answers": {}}),
    httpx.Response(200, json={"answers": {"same_or_new": {"choice": "e99", "probabilities": {"e99": 1.0}}}}),
])
def test_ac_per_12_errors_and_malformed_answers_fall_back_to_new(response):
    res = make(Server(lambda r: response)).choose("same_or_new", OPTIONS)
    assert res.choice == NEW and res.probabilities == {} and res.needs_review is True


def test_ac_per_12_connection_error_falls_back_to_new():
    res = make(Server(lambda r: httpx.ConnectError("refused"))).choose("same_or_new", OPTIONS)
    assert res.choice == NEW and res.needs_review is True


def test_ac_per_12_timeout_setting_is_used():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return systemone_answer("e1", {"e1": 0.9, "e2": 0.05, NEW: 0.05})(request)

    make(Server(handler), timeout_s=1.5).choose("same_or_new", OPTIONS)
    assert seen["timeout"] and seen["timeout"].get("read") == pytest.approx(1.5)


def _chat_logprobs(request):
    if request.url.path == "/v1/systemone":
        return httpx.Response(404, json={"detail": "Not Found"})
    assert request.url.path == "/v1/chat/completions"
    top = [{"token": "A", "logprob": math.log(0.7)}, {"token": "C", "logprob": math.log(0.2)},
           {"token": "B", "logprob": math.log(0.1)}]
    return httpx.Response(200, json={"model": "diffusiongemma", "choices": [{
        "message": {"role": "assistant", "content": "A"},
        "logprobs": {"content": [{"token": "A", "logprob": math.log(0.7), "top_logprobs": top}]}}]})


def test_ac_per_12_chat_fallback_when_systemone_missing():
    srv = Server(_chat_logprobs)
    res = make(srv).choose("same_or_new", OPTIONS)
    paths = [r.url.path for r in srv.requests]
    assert "/v1/chat/completions" in paths
    assert res.choice == "e1"
    assert res.probabilities["e1"] > res.probabilities[NEW] > res.probabilities["e2"]
    assert res.needs_review is False


def test_ac_per_12_chat_mode_lists_lettered_options_and_requests_logprobs():
    srv = Server(_chat_logprobs)
    make(srv, mode="chat").choose("same_or_new", OPTIONS)
    assert [r.url.path for r in srv.requests] == ["/v1/chat/completions"]
    body = srv.body()
    assert body.get("logprobs") is True
    text = json.dumps(body["messages"])
    for letter, opt in zip("ABC", OPTIONS, strict=True):
        assert f"{letter}" in text and OPTIONS[opt][:20] in text


def test_ac_per_12_chat_without_logprobs_uses_content_letter():
    def handler(request):
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"role": "assistant",
                                                                                 "content": "B"}}]})
    res = make(Server(handler), mode="chat").choose("same_or_new", OPTIONS)
    assert res.choice == "e2"
