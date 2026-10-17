"""Chatbot QA eval (AC-EVAL-4).

Exact scoring on entity / location ids taken from the agent's citations (agent spec AC-AGT-1:
``{reply, citations:[{entity_id|window_id, at, location_id?}], tool_calls}``), plus an optional LLM judge for
free-text answers. The judge model must differ from the generator model; every judged item is appended to an
agreement log (JSONL) and judge-vs-human agreement is reported as raw agreement + Cohen's kappa.

Scoring rules (tightened in the review round so that over-answering cannot score):
- ``entity``: correct iff the set of gold objects cited equals the expected set exactly: citation precision AND
  recall are 1.0 (a cited DB entity maps to a gold object through ``entity_to_gold``; duplicate entities of the same
  gold object count as that object; a cited entity that maps to no gold object is a wrong citation). Naming it only
  in the text is not enough. ``detail`` reports citation precision / recall.
- ``location``: the answer is the ``location_id`` of the citations; when they disagree, the most recent citation by
  ``at`` wins ("where is X now"; a moved answer citing old and new place is fine). Disagreeing citations without
  usable ``at`` are hedging (wrong). Without citation locations, the single known location-id token in the reply.
  Module-level answers to a zone question are wrong.
- ``actor``: the reply names exactly one known actor descriptor and it is the expected one (a descriptor contained
  in a longer matched one, e.g. "crew member" inside "crew member, blue shirt", is not a second actor); or the
  expected gold person id is cited.
- ``free_text``: judged by an LLM judge against the item's rubric; unscored (``correct=None``) without a judge or
  when the judge fails. A judge always needs the generator model name (judge != generator is enforced).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from nasa_eval.dataset import QAItem, qa_from_dict, read_jsonl
from nasa_eval.metrics import cohen_kappa, normalise_actor

log = logging.getLogger(__name__)

ANSWER_TYPES = ("entity", "location", "actor", "free_text")

JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluator of answers given by an ISS inventory assistant. Grade the assistant's reply to the "
    "question against the rubric only. Respond with a single JSON object and nothing else: "
    '{"score": <number between 0 and 1>, "passed": <true|false>, "rationale": "<one sentence>"}.'
)


class AgentError(RuntimeError):
    """The agent endpoint failed (transport error or non-2xx)."""


@dataclass(frozen=True)
class JudgeVerdict:
    score: float | None
    passed: bool | None
    rationale: str
    model: str
    error: str | None = None


class Judge(Protocol):
    model: str

    def judge(self, *, question: str, reply: str, rubric: str, reference: str | None = None) -> JudgeVerdict: ...


@dataclass
class QAResult:
    qa_id: str
    answer_type: str
    correct: bool | None  # None = not scored (free_text without judge, or judge error)
    detail: dict[str, Any] = field(default_factory=dict)


def _join(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if path.startswith("/v1/") and base.endswith("/v1"):
        path = path[3:]
    return base + path


_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BRACES = re.compile(r"\{.*\}", re.DOTALL)


def parse_judge_output(content: str) -> dict[str, Any]:
    """Extract and validate the judge's JSON object (plain, fenced, or embedded in prose).
    Raises ValueError / TypeError when there is none or it does not match the expected shape."""
    candidates = [content.strip()]
    candidates += [m.group(1) for m in _FENCED.finditer(content)]
    candidates += [m.group(0) for m in _BRACES.finditer(content)]
    for text in candidates:
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            break
    else:
        raise ValueError("judge reply contains no JSON object")
    score, passed, rationale = obj.get("score"), obj.get("passed"), obj.get("rationale", "")
    if isinstance(score, bool) or not isinstance(score, int | float) or not 0.0 <= float(score) <= 1.0:
        raise ValueError(f"judge score must be a number in [0, 1], got {score!r}")
    if not isinstance(passed, bool):
        raise TypeError(f"judge passed must be a boolean, got {passed!r}")
    if not isinstance(rationale, str):
        raise TypeError("judge rationale must be a string")
    return {"score": float(score), "passed": passed, "rationale": rationale}


class OpenAIJudge:
    """LLM judge over an OpenAI-compatible /v1/chat/completions. Expects the model to answer with a JSON object
    ``{"score": 0..1, "passed": bool, "rationale": str}``. Raises ValueError if model == generator_model.

    The optional bearer token comes from ``api_key`` or the ``EVAL_JUDGE_API_KEY`` environment variable."""

    def __init__(
        self, base_url: str, model: str, *, generator_model: str, timeout_s: float = 30.0, api_key: str | None = None
    ):
        if not model:
            raise ValueError("judge model is required")
        _check_independent(model, generator_model)
        self.base_url = base_url
        self.model = model
        self.generator_model = generator_model
        self.timeout_s = timeout_s
        self._api_key = api_key if api_key is not None else os.environ.get("EVAL_JUDGE_API_KEY")

    def _messages(self, question: str, reply: str, rubric: str, reference: str | None) -> list[dict[str, str]]:
        parts = [f"Question:\n{question}", f"Rubric:\n{rubric}"]
        if reference:
            parts.append(f"Reference answer:\n{reference}")
        parts.append(f"Assistant reply:\n{reply}")
        return [{"role": "system", "content": JUDGE_SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}]

    def judge(self, *, question: str, reply: str, rubric: str, reference: str | None = None) -> JudgeVerdict:
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 256,
            "messages": self._messages(question, reply, rubric, reference),
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = httpx.post(
                _join(self.base_url, "/v1/chat/completions"), json=body, headers=headers, timeout=self.timeout_s
            )
        except httpx.HTTPError as exc:
            return JudgeVerdict(None, None, "", self.model, error=f"transport error: {exc}")
        if resp.status_code // 100 != 2:
            return JudgeVerdict(None, None, "", self.model, error=f"HTTP {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = parse_judge_output(content or "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return JudgeVerdict(None, None, "", self.model, error=f"invalid judge output: {exc}")
        return JudgeVerdict(parsed["score"], parsed["passed"], parsed["rationale"], self.model)


def _check_independent(judge_model: str, generator_model: str | None) -> None:
    """Judge != generator: the generator model name is mandatory whenever a judge is used."""
    if generator_model is None or not str(generator_model).strip():
        raise ValueError("a judge needs the generator model name (generator_model) to enforce judge != generator")
    if judge_model.strip().casefold() == generator_model.strip().casefold():
        raise ValueError(f"judge model {judge_model!r} must differ from the generator model")


class AgentClient:
    """Client for the agent's ``POST /chat`` (AC-AGT-1)."""

    def __init__(self, base_url: str, *, timeout_s: float = 60.0):
        if not base_url:
            raise ValueError("agent base_url is required")
        self.base_url = base_url
        self.timeout_s = timeout_s

    def ask(self, question: str, *, session_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"messages": [{"role": "user", "content": question}]}
        if session_id:
            body["session_id"] = session_id
        try:
            resp = httpx.post(_join(self.base_url, "/chat"), json=body, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise AgentError(f"agent unreachable: {exc}") from exc
        if resp.status_code // 100 != 2:
            raise AgentError(f"agent returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise AgentError("agent returned non-JSON") from exc
        if not isinstance(data, dict):
            raise AgentError("agent response is not a JSON object")
        return data


def load_qa(path: str | Path) -> list[QAItem]:
    """Load and validate a qa.jsonl file. Raises ValueError listing every invalid line."""
    rows, errors = read_jsonl(Path(path), "qa")
    if errors:
        raise ValueError("; ".join(errors))
    return [qa_from_dict(r) for _, r in rows]


# ------------------------------------------------------------------------------------------------------- scoring
def _citations(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cites = response.get("citations") or []
    return [c for c in cites if isinstance(c, Mapping)] if isinstance(cites, list) else []


def _cited_gold(response: Mapping[str, Any], entity_to_gold: Mapping[str, str]) -> set[str]:
    return {
        entity_to_gold[str(c["entity_id"])]
        for c in _citations(response)
        if c.get("entity_id") is not None and str(c["entity_id"]) in entity_to_gold
    }


def _entity_citations(response: Mapping[str, Any], entity_to_gold: Mapping[str, str]) -> list[str | None]:
    """One entry per distinct cited entity id: its gold object, or None when it maps to no gold object."""
    ids = []
    for c in _citations(response):
        eid = c.get("entity_id")
        if eid is not None and str(eid) not in ids:
            ids.append(str(eid))
    return [entity_to_gold.get(e) for e in ids]


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _citation_location(response: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """(answer, candidates) from citation ``location_id``s; latest by ``at`` wins when they disagree."""
    cites = [c for c in _citations(response) if c.get("location_id")]
    candidates = sorted({str(c["location_id"]) for c in cites})
    if len(candidates) <= 1:
        return (candidates[0] if candidates else None), candidates
    timed = [(_parse_at(c.get("at")), str(c["location_id"])) for c in cites]
    if any(t is None for t, _ in timed):
        return None, candidates
    latest = max(t for t, _ in timed)
    at_latest = {loc for t, loc in timed if t == latest}
    return (next(iter(at_latest)) if len(at_latest) == 1 else None), candidates


def _named_actors(reply: str, known: Iterable[str]) -> set[str]:
    text = normalise_actor(reply)
    found = {normalise_actor(k) for k in known if normalise_actor(k) and normalise_actor(k) in text}
    return {a for a in found if not any(a != b and a in b for b in found)}


def _location_tokens(reply: str, known: set[str]) -> set[str]:
    if not known or not reply:
        return set()
    alternatives = "|".join(re.escape(x) for x in sorted(known, key=len, reverse=True))
    return set(re.findall(rf"(?<![A-Za-z0-9])({alternatives})(?![A-Za-z0-9])", reply))


def score_qa_item(
    item: QAItem,
    response: Mapping[str, Any],
    *,
    entity_to_gold: Mapping[str, str],
    location_ids: Iterable[str] = (),
    actor_descriptors: Iterable[str] = (),
    judge: Judge | None = None,
) -> QAResult:
    """Score one agent response for ``item`` (see module docstring for the rules)."""
    reply = response.get("reply") if isinstance(response.get("reply"), str) else ""
    kind = item.answer_type
    if kind == "entity":
        expected = set(item.expected.get("entities") or [])
        cited_list = _entity_citations(response, entity_to_gold)
        cited = {g for g in cited_list if g is not None}
        n_cited = len({g for g in cited_list if g is not None}) + sum(1 for g in cited_list if g is None)
        precision = len(cited & expected) / n_cited if n_cited else 0.0
        recall = len(cited & expected) / len(expected) if expected else 0.0
        return QAResult(
            item.qa_id,
            kind,
            bool(expected) and precision == 1.0 and recall == 1.0,
            {
                "expected": sorted(expected),
                "cited": sorted(cited),
                "unmapped_citations": sum(1 for g in cited_list if g is None),
                "citation_precision": precision,
                "citation_recall": recall,
            },
        )
    if kind == "location":
        expected_loc = item.expected.get("location")
        answer, candidates = _citation_location(response)
        source = "citations"
        if not candidates:
            tokens = _location_tokens(reply, set(location_ids) | ({expected_loc} if expected_loc else set()))
            candidates, source = sorted(tokens), "reply"
            answer = candidates[0] if len(candidates) == 1 else None
        return QAResult(
            item.qa_id,
            kind,
            answer is not None and answer == expected_loc,
            {"expected": expected_loc, "answer": answer, "candidates": list(candidates), "source": source},
        )
    if kind == "actor":
        expected_actor = str(item.expected.get("actor") or "")
        named = _named_actors(reply, {*actor_descriptors, expected_actor})
        by_text = bool(expected_actor) and named == {normalise_actor(expected_actor)}
        by_cite = expected_actor in _cited_gold(response, entity_to_gold)
        return QAResult(
            item.qa_id,
            kind,
            by_text or by_cite,
            {"expected": expected_actor, "named": sorted(named), "by_text": by_text, "by_citation": by_cite},
        )
    if kind == "free_text":
        if judge is None:
            return QAResult(item.qa_id, kind, None, {"skipped": "no judge configured"})
        reference = item.expected.get("reference") or item.expected.get("answer")
        verdict = judge.judge(
            question=item.question,
            reply=reply,
            rubric=item.rubric or "",
            **({"reference": str(reference)} if reference else {}),
        )
        detail = {
            "judge": {
                "model": verdict.model,
                "score": verdict.score,
                "passed": verdict.passed,
                "rationale": verdict.rationale,
                "error": verdict.error,
            }
        }
        if verdict.error or verdict.passed is None:
            return QAResult(item.qa_id, kind, None, detail)
        return QAResult(item.qa_id, kind, bool(verdict.passed), detail)
    raise ValueError(f"unknown answer_type {kind!r}")


def ask_all(items: Iterable[QAItem], agent: Any, *, session_prefix: str = "eval") -> dict[str, dict[str, Any]]:
    """Ask every question (one fresh session each) and collect the responses by qa_id. An agent failure is stored
    as ``{"reply": "", "citations": [], "error": str}`` (scored as wrong) instead of aborting the run."""
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        try:
            out[it.qa_id] = dict(agent.ask(it.question, session_id=f"{session_prefix}-{it.qa_id}"))
        except Exception as exc:  # noqa: BLE001 - any agent failure is a wrong answer, recorded
            log.warning("agent failed qa_id=%s: %s", it.qa_id, exc)
            out[it.qa_id] = {"reply": "", "citations": [], "tool_calls": [], "error": f"{type(exc).__name__}: {exc}"}
    return out


def default_agreement_log(reports_dir: str | Path | None, dataset_name: str, now: datetime | None = None) -> Path:
    """``<reports_dir>/qa/<ts>-<dataset>-judge.jsonl`` (reports_dir defaults to eval/reports)."""
    if reports_dir is None:
        from nasa_eval import REPORTS_ROOT

        reports_dir = REPORTS_ROOT
    ts = (now or datetime.now(UTC)).astimezone(UTC)
    return Path(reports_dir) / "qa" / f"{ts:%Y%m%dT%H%M%SZ}-{dataset_name}-judge.jsonl"


def score_qa(
    items: Iterable[QAItem],
    responses: Mapping[str, Mapping[str, Any]],
    *,
    entity_to_gold: Mapping[str, str],
    location_ids: Iterable[str] = (),
    actor_descriptors: Iterable[str] = (),
    judge: Judge | None = None,
    generator_model: str | None = None,
    human_labels: Mapping[str, bool] | None = None,
    agreement_log: str | Path | None = None,
    reports_dir: str | Path | None = None,
    dataset_name: str = "qa",
) -> dict[str, Any]:
    """Return {n, n_scored, n_skipped, accuracy, accuracy_by_type, judge_agreement, agreement_log, results}.

    A missing response is scored as an empty reply (wrong for exact answer types). With a judge, the generator
    model is mandatory and every judged item is appended to ``agreement_log`` (default
    ``<reports_dir>/qa/<ts>-<dataset>-judge.jsonl``)."""
    if judge is not None:
        _check_independent(judge.model, generator_model)
    locs = set(location_ids)
    actors = tuple(actor_descriptors)
    items = list(items)
    results = [
        score_qa_item(
            it,
            responses.get(it.qa_id) or {},
            entity_to_gold=entity_to_gold,
            location_ids=locs,
            actor_descriptors=actors,
            judge=judge,
        )
        for it in items
    ]
    scored = [r for r in results if r.correct is not None]
    by_type: dict[str, list[bool]] = {}
    for r in scored:
        by_type.setdefault(r.answer_type, []).append(bool(r.correct))

    # judge agreement log + judge-vs-human agreement
    log_rows: list[dict[str, Any]] = []
    pairs: list[tuple[bool, bool]] = []
    for it, r in zip(items, results, strict=True):
        j = r.detail.get("judge")
        if j is None:
            continue
        human = (human_labels or {}).get(it.qa_id)
        log_rows.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "qa_id": it.qa_id,
                "question": it.question,
                "rubric": it.rubric,
                "reply": (responses.get(it.qa_id) or {}).get("reply"),
                "judge_model": j["model"],
                "generator_model": generator_model,
                "score": j["score"],
                "passed": j["passed"],
                "rationale": j["rationale"],
                "error": j["error"],
                "human_label": human,
            }
        )
        if human is not None and j["passed"] is not None:
            pairs.append((bool(j["passed"]), bool(human)))
    log_path: Path | None = None
    if log_rows:
        log_path = (
            Path(agreement_log) if agreement_log is not None else default_agreement_log(reports_dir, dataset_name)
        )
        path = log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in log_rows:
                fh.write(json.dumps(row) + "\n")
    agreement = None
    if pairs:
        jl, hl = [p[0] for p in pairs], [p[1] for p in pairs]
        agreement = {
            "n": len(pairs),
            "raw_agreement": sum(a == b for a, b in pairs) / len(pairs),
            "cohen_kappa": cohen_kappa(jl, hl),
        }

    return {
        "n": len(results),
        "n_scored": len(scored),
        "n_skipped": len(results) - len(scored),
        "accuracy": sum(bool(r.correct) for r in scored) / len(scored) if scored else None,
        "accuracy_by_type": {t: sum(v) / len(v) for t, v in by_type.items()},
        "judge_model": judge.model if judge is not None else None,
        "generator_model": generator_model,
        "judge_agreement": agreement,
        "agreement_log": str(log_path) if log_path else None,
        "results": results,
    }
