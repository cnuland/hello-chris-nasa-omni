"""Claim-level grounding check (AC-AGT-4).

A reply in a grounded turn may only state locations, times and actors that appear in the data returned by this turn's
grounding tools. Checked claim types (deterministic, conservative):

- location ids (``LAB1S2``, ``NOD3``, ``JPM1F5`` ...) and "Node N" module names;
- clock times (``09:00``, ``12:30Z``) and ISO dates (``2026-10-01``);
- actors in "<moved|taken|placed|...> ... by <actor>" phrases: every content word of the actor must appear.

Catalog facts (AC-AGT-11): only the structured fields of ``lookup_catalog`` entries (``catalog_evidence``) count as
evidence; operator-entered free text (display name, synonyms, category) never supports a claim. A location that only a
catalog ``home_location`` supports must be stated as the usual stowage location in the same sentence
(``unqualified_home_locations``); otherwise the harness answers with ``usual_location_reply``.

Limits (documented in docs/tdd-log/agent.md): the check is lexical. It cannot tell that a *supported* location is
attributed to the wrong item ("the camera is in LAB1S2" when only the drill's record says LAB1S2), and it does not
catch actors named before the verb ("a commander moved it"). Location ids/times of other items are rejected only when
they are absent from every grounding payload of the turn.
"""

from __future__ import annotations

import json
import re
from typing import Any

LOCATION_ID_RE = re.compile(r"\b[A-Z]{3,6}\d(?:[A-Z]\d+)?\b")
NODE_RE = re.compile(r"\bnode\s+(\d+)\b", re.IGNORECASE)
TIME_RE = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d])")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
ACTOR_RE = re.compile(
    r"\b(?:moved|move|taken|took|carried|placed|put|stowed|removed|used|handled|relocated|grabbed|picked\s+up)\b"
    r"[^.;!?]*?\bby\s+(.+?)(?=\s+(?:at|on|from|to|into|around|near|before|after|during|while|and)\b|[.;,!?)]|$)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"a", "an", "the", "in", "with", "wearing", "of", "who", "was", "is", "someone", "person"})


def evidence_text(payloads: list[Any]) -> str:
    """All grounding payloads of the turn as one searchable string."""
    return "\n".join(json.dumps(p, default=str) for p in payloads)


def unsupported_claims(reply: str, evidence: str) -> list[str]:
    """Return the claim strings in ``reply`` that ``evidence`` does not support (empty list -> all supported)."""
    lower = evidence.lower()
    words = set(_WORD_RE.findall(lower))
    out: list[str] = []
    for loc in LOCATION_ID_RE.findall(reply):
        if loc not in evidence:
            out.append(loc)
    for num in NODE_RE.findall(reply):
        if f"node {num}" not in lower and f"nod{num}" not in lower:
            out.append(f"Node {num}")
    for hour, minute in TIME_RE.findall(reply):
        hhmm = f"{int(hour):02d}:{minute}"
        if hhmm not in evidence:
            out.append(hhmm)
    for date in DATE_RE.findall(reply):
        if date not in evidence:
            out.append(date)
    for actor in ACTOR_RE.findall(reply):
        content = [w for w in _WORD_RE.findall(actor.lower()) if w not in _STOPWORDS]
        if content and any(w not in words for w in content):
            out.append(f"by {actor.strip()}")
    return out


# ---------------------------------------------------------------- catalog facts (AC-AGT-11)
#: catalog_id is left out on purpose: it is operator-chosen text too (a slug like "nod3-kit-2026-10-01" must not
#: support "Node 3" or a date). home_location is a location FK and home_location_name comes from GET /locations.
CATALOG_EVIDENCE_FIELDS = ("expected_count", "is_fungible", "home_location", "home_location_name")
#: Words that mark a location as the usual stowage place rather than a current position (same sentence/clause).
USUAL_RE = re.compile(
    r"\b(?:usual|usually|normal|normally|home|stowage|kept|typically|nominally|designated|assigned)\b", re.IGNORECASE
)
#: Current-position wording: a clause with a catalog-only location must not contain these.
CURRENT_RE = re.compile(
    r"\b(?:now|currently|right now|at the moment|at present|presently|last seen|last spotted|seen)\b", re.IGNORECASE
)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(r",\s+|\s+(?:and|but|while|whereas|although|though)\s+", re.IGNORECASE)
USUAL_LOCATION_LEAD = "I can only give the usual stowage location from the catalog, not a current sighting:"


def catalog_evidence(entries: list[Any]) -> list[dict[str, Any]]:
    """The structured, citable part of lookup_catalog entries (free text excluded: it is never evidence)."""
    return [{k: e.get(k) for k in CATALOG_EVIDENCE_FIELDS} for e in entries if isinstance(e, dict)]


def _mentions_id(text: str, location_id: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(location_id)}(?![A-Za-z0-9])", text) is not None


def _mentions_home(sentence: str, entry: dict[str, Any]) -> bool:
    home = entry.get("home_location")
    name = entry.get("home_location_name")
    if home and _mentions_id(sentence, home):
        return True
    return bool(name) and name.lower() in sentence.lower()


def _sighted(entry: dict[str, Any], other_evidence: str) -> bool:
    """True when a non-catalog grounding payload of the turn also names this location (a real sighting/event)."""
    home = entry.get("home_location")
    name = entry.get("home_location_name")
    if home and _mentions_id(other_evidence, home):
        return True
    return bool(name) and name.lower() in other_evidence.lower()


def _names_a_location(clause: str, entries: list[dict[str, Any]]) -> bool:
    if LOCATION_ID_RE.search(clause) or NODE_RE.search(clause):
        return True
    return any(_mentions_home(clause, e) for e in entries)


def unqualified_home_locations(reply: str, entries: list[dict[str, Any]], other_evidence: str) -> list[dict[str, Any]]:
    """Catalog entries whose home location the reply states as if it were the current position, while no
    entity/window result of the turn supports that location. Per clause (split on sentence punctuation, commas and
    and/but/while...): a clause naming a catalog-only home location needs a usual-stowage qualifier and no
    current-position wording ("now", "last seen"...). A current-position clause that names no location ("it is there
    right now") taints every catalog-only home location of its sentence."""
    catalog_only = [e for e in entries if e.get("home_location") and not _sighted(e, other_evidence)]
    out: list[dict[str, Any]] = []
    for sentence in _SENTENCE_RE.split(reply):
        clauses = [c for c in _CLAUSE_SPLIT_RE.split(sentence) if c.strip()]
        dangling = any(CURRENT_RE.search(c) and not _names_a_location(c, entries) for c in clauses)
        for entry in catalog_only:
            if entry in out or not _mentions_home(sentence, entry):
                continue
            bad = dangling or any(
                _mentions_home(c, entry) and (not USUAL_RE.search(c) or CURRENT_RE.search(c)) for c in clauses
            )
            if bad:
                out.append(entry)
    return out


def usual_location_reply(entries: list[dict[str, Any]]) -> str:
    """Deterministic reply stating catalog home locations as usual stowage locations only. The operator-entered
    display name is used only if it carries no location/time/actor claim of its own; otherwise the catalog_id."""
    parts = []
    for e in entries:
        where = e["home_location"] + (f" ({e['home_location_name']})" if e.get("home_location_name") else "")
        name = e.get("display_name")
        if not name or unsupported_claims(name, evidence_text(catalog_evidence([e]))):
            name = e["catalog_id"]
        parts.append(f"{name} [{e['catalog_id']}] is usually kept in {where}")
    return f"{USUAL_LOCATION_LEAD} " + "; ".join(parts) + "."
