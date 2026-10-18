"""Deterministic operator-English rendering (AC-AGT-29/30) and count-claim helpers (AC-AGT-28).

Every line is built from structured tool data only. Free text (labels, appearance and actor descriptions) is
whitespace-collapsed, scrubbed of location/time claims and allow-listed before it is shown; a label that carries a
claim of its own is replaced by its short id.

Line format: ``YYYY-MM-DD HH:MM UTC: <Subject> <verb phrase>``; places read "Node 3 Tranquility (NOD3)"; actors read
"by operator <name>" or "by person (<appearance>)". A person track is never "moved by" anyone: it is seen, leaves or
arrives (UI3-04).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from nasa_agent.grounding import DATE_RE, LOCATION_ID_RE, NODE_RE, TIME_RE, scrub_claims, unsupported_claims
from nasa_agent.tools import PEOPLE_CAVEAT, clean_text

#: Lines per rendered list page.
RENDER_PAGE = 10
CHANGES_MORE = "ask 'show more changes' to continue"
PEOPLE_MORE = "ask 'show more people' to continue"
EVENT_VERBS = {
    "moved": "moved", "used": "used", "handed_over": "handed over", "added": "added", "removed": "removed",
    "stowed": "stowed", "unstowed": "unstowed", "consumed": "consumed", "seen": "seen",
}
_FREE_TEXT_RE = re.compile(r"[A-Za-z0-9 ,'/&+.-]{1,80}")
_OPERATOR_RE = re.compile(r"[A-Za-z0-9._@-]{1,40}")
_ACTION_RE = re.compile(r"[a-z_]{1,32}")
_LEADING_ROLE_RE = re.compile(
    r"^(?:(?:a|an|the|one|person|people|crew\s*member|crewmember|astronaut|cosmonaut|individual|someone|wearing|"
    r"dressed\s+in|in|with)\b[\s,:-]*)+", re.IGNORECASE)
_DANGLING_RE = re.compile(r"(?:\s+(?:in|at|on|to|from|into|near|by|the|of|and|who|put|it))+\s*$", re.IGNORECASE)
MAX_APPEARANCE_WORDS = 12


# ---------------------------------------------------------------- primitives
def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def stamp(value: Any) -> str | None:
    """ "2026-10-18 03:14 UTC" (the dashboard format) or None."""
    dt = parse_iso(value) if not isinstance(value, datetime) else value.astimezone(UTC)
    return f"{dt:%Y-%m-%d %H:%M} UTC" if dt else None


def window_text(since: Any, until: Any) -> str:
    """ "between 2026-10-18 02:20 and 2026-10-18 03:20 UTC" (both full dates: read back by the pager)."""
    a, b = parse_iso(since), parse_iso(until)
    if not (a and b):
        return "in the requested window"
    return f"between {a:%Y-%m-%d %H:%M} and {b:%Y-%m-%d %H:%M} UTC"


def place(lid: Any, names: dict[str, str | None], given: Any = None) -> str | None:
    """ "Node 1 Unity (NOD1)", or the bare id when the name is unknown; None without a location."""
    if not lid:
        return None
    name = clean_text(given, 80) or names.get(str(lid))
    if not name or name == lid:
        return str(lid)
    if name.endswith(")") and "(" in name:
        return f"{name[:-1]}, {lid})"  # "Destiny (US Lab, LAB1)", not "Destiny (US Lab) (LAB1)"
    return f"{name} ({lid})"


def appearance_text(value: Any) -> str | None:
    """A person's appearance as shown in "Person (...)": scrubbed of places/times, leading role words ("wearing",
    "a crew member in") removed, plain words only. None when nothing safe is left."""
    text = scrub_claims(clean_text(value))
    if not text:
        return None
    text = text.replace("(", " ").replace(")", " ")
    text = _LEADING_ROLE_RE.sub("", " ".join(text.split())).strip(" ,;:-")
    text = _DANGLING_RE.sub("", text).strip(" ,;:-")
    if not text or not _FREE_TEXT_RE.fullmatch(text) or len(text.split()) > MAX_APPEARANCE_WORDS:
        return None
    return text


def person_subject(appearance: Any) -> str:
    text = appearance_text(appearance)
    return f"Person ({text})" if text else "Person"


def actor_phrase(actor: Any, *, operator: bool) -> str | None:
    """ "operator ui-review-3" / "person (blue shirt, headset)" / "an unidentified person"; None without an actor."""
    raw = clean_text(actor)
    if not raw:
        return None
    if operator:
        name = raw.strip()
        if not _OPERATOR_RE.fullmatch(name):
            return "an operator"
        return name if name.lower().startswith("operator") else f"operator {name}"
    text = appearance_text(raw)
    if text:
        return f"person ({text})"
    scrubbed = _LEADING_ROLE_RE.sub("", scrub_claims(raw) or "").strip(" ,;:-")
    return "a person" if not scrubbed else "an unidentified person"


def subject_label(label: Any, entity_id: Any, own: dict[str, Any], *, capital: bool = True) -> str:
    """The label of an object (first letter capitalised), or "Record [1234abcd]" when it is missing or carries a
    location/time/actor claim of its own."""
    text = clean_text(label, 80)
    if text and (unsupported_claims(text, json.dumps(own, default=str)) or not _FREE_TEXT_RE.fullmatch(text)):
        text = None
    if not text:
        return f"Record [{str(entity_id)[:8]}]" if entity_id else "Record"
    return text[0].upper() + text[1:] if capital else text


def _is_operator(c: dict[str, Any]) -> bool:
    return c.get("source") == "operator" or c.get("actor_kind") == "operator"


# ---------------------------------------------------------------- lines
def change_line(c: dict[str, Any], names: dict[str, str | None]) -> str:
    """One recent_changes / who_moved event as operator English (AC-AGT-29/30)."""
    kind = str(c.get("change") or c.get("event_type") or "handled")
    when = stamp(c.get("at") or c.get("occurred_at"))
    frm = place(c.get("from_location"), names, c.get("from_location_name"))
    to = place(c.get("to_location"), names, c.get("to_location_name"))
    at = place(c.get("location"), names, c.get("location_name"))
    own = {k: c.get(k) for k in ("from_location", "to_location", "location", "at", "occurred_at")}
    if c.get("subject_kind") == "person":
        who = person_subject(c.get("appearance"))
        if kind == "new_item":
            text = f"{who} first seen" + (f" in {at or to or frm}" if (at or to or frm) else "")
        elif frm and to and frm != to:
            text = f"{who} left {frm} and was seen in {to}"
        elif frm and not to:
            text = f"{who} left {frm}"
        elif to or at or frm:
            text = f"{who} seen in {to or at or frm}"
        else:
            text = f"{who} seen"
    elif kind == "new_item":
        label = subject_label(c.get("label"), c.get("entity_id"), own, capital=False)
        text = (f"New {label}" if not label.startswith("Record") else f"New record {label[7:]}") + " seen" + (
            f" in {at}" if at else "")
    elif kind == "review_resolved":
        label = subject_label(c.get("label"), c.get("entity_id"), own, capital=False)
        action = c.get("action") if isinstance(c.get("action"), str) and _ACTION_RE.fullmatch(c["action"]) else None
        actor = actor_phrase(c.get("actor"), operator=True)
        text = (f"Review of {label} resolved" + (f" ({action.replace('_', ' ')})" if action else "")
                + (f" by {actor}" if actor else "") + (f" in {at}" if at else ""))
    else:
        label = subject_label(c.get("label"), c.get("entity_id"), own)
        verb = EVENT_VERBS.get(kind, "handled")
        if kind == "moved":
            if frm and to:
                text = f"{label} moved from {frm} to {to}"
            elif frm:
                text = f"{label} moved out of {frm}"
            elif to:
                text = f"{label} moved to {to}"
            else:
                text = f"{label} moved"
        else:
            where = frm or to or at
            text = f"{label} {verb}" + (f" in {where}" if where else "")
        actor = actor_phrase(c.get("actor", c.get("actor_desc")), operator=_is_operator(c))
        if actor:
            text += f" by {actor}"
    return f"{when}: {text}" if when else text


def item_line(item: dict[str, Any], names: dict[str, str | None]) -> str:
    """ "Laptop: last seen in Node 1 Unity (NOD1) at 2026-10-18 03:14 UTC" (person tracks: "Person (...)")."""
    own = {k: item.get(k) for k in ("last_location", "last_location_name", "last_seen_at")}
    if item.get("kind") == "person":
        who = person_subject(item.get("description") or item.get("appearance"))
    else:
        who = subject_label(item.get("label"), item.get("entity_id"), own)
    where = place(item.get("last_location"), names, item.get("last_location_name"))
    when = stamp(item.get("last_seen_at"))
    tail = (f" in {where}" if where else "") + (f" at {when}" if when else "")
    return f"{who}: last seen{tail}" if tail else f"{who}: no sighting on record"


def _numbered(lines: list[str], start: int) -> list[str]:
    return [f"{start + i}. {line}" for i, line in enumerate(lines)]


def _cites_for(rows: list[dict[str, Any]], citations: list[dict[str, Any]], at_key: str = "at") -> list[dict[str, Any]]:
    keys = {(r.get("entity_id"), r.get(at_key)) for r in rows}
    return [c for c in citations if (c.get("entity_id"), c.get("at")) in keys]


# ---------------------------------------------------------------- whole answers
def changes_reply(data: dict[str, Any], citations: list[dict[str, Any]],
                  names: dict[str, str | None]) -> tuple[str, list[dict[str, Any]]]:
    """A non-empty recent_changes page as a numbered list with a header carrying the window and the range."""
    changes = [c for c in data.get("changes") or [] if isinstance(c, dict)]
    offset = int(data.get("offset") or 0)
    total = int(data.get("total") or (offset + len(changes) + int(data.get("omitted") or 0)))
    shown = changes[:RENDER_PAGE]
    scope = place(data.get("location"), names)
    head = (f"Changes{f' in {scope}' if scope else ''} {window_text(data.get('since'), data.get('until'))} "
            f"(showing {offset + 1}-{offset + len(shown)} of {total}):")
    lines = [head, *_numbered([change_line(c, names) for c in shown], offset + 1)]
    more = total - offset - len(shown)
    text = "\n".join(lines)
    if more > 0:
        text += f"\n\nand {more} more; {CHANGES_MORE}."
    return text, _cites_for(shown, citations)


def people_reply(data: dict[str, Any], citations: list[dict[str, Any]],
                 names: dict[str, str | None]) -> tuple[str, list[dict[str, Any]]]:
    """people_seen as the track total, the per-module counts, the caveat and a page of tracks (AC-AGT-27/29)."""
    total = int(data.get("total_tracks") or 0)
    offset = int(data.get("offset") or 0)
    scope = place(data.get("location"), names, data.get("location_name"))
    where = f" in {scope}" if scope else ""
    window = window_text(data.get("since"), data.get("until"))
    tracks = [t for t in data.get("tracks") or [] if isinstance(t, dict)]
    shown = tracks[:RENDER_PAGE]
    lines = [item_line({**t, "kind": "person", "last_location": t.get("location"),
                        "last_location_name": t.get("location_name")}, names) for t in shown]
    if offset:
        head = f"Person tracks{where} {window} (showing {offset + 1}-{offset + len(shown)} of {total}):"
        parts = [head, "\n".join(_numbered(lines, offset + 1))]
    else:
        if total == 0:
            return f"No person tracks were seen{where} {window}.", []
        counts = ", ".join(f"{place(m.get('location'), names, m.get('location_name')) or 'unknown location'} "
                           f"{int(m.get('count') or 0)}" for m in data.get("by_module") or [] if isinstance(m, dict))
        noun = "person track was" if total == 1 else "person tracks were"
        lead = f"{total} {noun} seen{where} {window}" + (f": {counts}." if counts else ".")
        if data.get("complete") is False:
            lead += " (Per-module counts cover only the first tracks scanned.)"
        parts = [lead, str(data.get("caveat") or PEOPLE_CAVEAT)]
        if shown:
            parts.append(f"Most recent tracks (showing 1-{len(shown)} of {total}):\n" + "\n".join(_numbered(lines, 1)))
    text = "\n\n".join(parts)
    more = total - offset - len(shown)
    if more > 0 and shown:
        text += f"\n\nand {more} more; {PEOPLE_MORE}."
    return text, _cites_for(shown, citations, "last_seen_at")


def count_reply(data: dict[str, Any], names: dict[str, str | None]) -> str:
    """count_items as one sentence: total, filters and per-module counts (AC-AGT-28)."""
    total = int(data.get("total") or 0)
    f = data.get("filters") or {}
    person = f.get("kind") == "person"
    noun = ("person track" if person else "inventory item") + ("" if total == 1 else "s")
    desc = ""
    q = clean_text(f.get("q"), 60)
    if q and _FREE_TEXT_RE.fullmatch(q) and not unsupported_claims(q, "{}"):
        desc += f' match "{q}"' if total != 1 else f' matches "{q}"'
    if f.get("status"):
        desc += " with status " + " or ".join(str(s) for s in f["status"])
    if f.get("location"):
        desc += f" in {place(f['location'], names)}"
    if f.get("seen_after") and stamp(f["seen_after"]):
        desc += f" seen since {stamp(f['seen_after'])}"
    verb = "" if desc.startswith((" match", " matches")) else (" are on record" if total != 1 else " is on record")
    counts = ", ".join(f"{place(m.get('location'), names, m.get('location_name')) or 'unknown location'} "
                       f"{int(m.get('count') or 0)}" for m in data.get("by_module") or [] if isinstance(m, dict))
    text = f"{total} {noun}{desc}{verb}" + (f": {counts}." if counts and total else ".")
    if data.get("complete") is False:
        text += " (Per-module counts cover only the first items scanned.)"
    if person:
        text += " " + PEOPLE_CAVEAT
    return text


# ---------------------------------------------------------------- paging (AC-AGT-29)
_SHOW_MORE_WORDS = frozenset({
    "show", "me", "the", "more", "next", "rest", "remaining", "continue", "please", "changes", "change", "people",
    "persons", "person", "tracks", "of", "them", "those", "list", "give", "ok", "okay", "yes", "page", "results",
    "records", "events", "others", "go", "on", "keep", "going", "can", "you", "and", "see",
})
_SHOW_MORE_TRIGGERS = frozenset({"more", "next", "rest", "remaining", "continue", "others", "going"})
_WINDOW_RE = re.compile(r"between (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) and (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC")
_RANGE_RE = re.compile(r"\(showing (\d+)-(\d+) of (\d+)\)")
_SCOPE_RE = re.compile(r"(?:Changes|tracks were seen|track was seen|Person tracks) in [^\n]*?\(([A-Z][A-Z0-9_-]{1,15})\)"
                       r" between")


def is_show_more(question: str) -> bool:
    """True for a bare request for the next page ("show more", "more", "next", "continue", "show more changes")."""
    words = re.findall(r"[a-z]+", (question or "").lower())
    return bool(words) and len(words) <= 8 and set(words) <= _SHOW_MORE_WORDS and bool(set(words) & _SHOW_MORE_TRIGGERS)


def parse_page_marker(reply: str, question: str = "") -> dict[str, Any] | None:
    """{tool, since, until, location, offset} read back from a rendered reply that ends with a paging hint."""
    if not isinstance(reply, str):
        return None
    tool = None
    q = (question or "").lower()
    if CHANGES_MORE in reply and not ("people" in q or "person" in q or "track" in q):
        tool = "recent_changes"
    elif PEOPLE_MORE in reply and "change" not in q:
        tool = "people_seen"
    if tool is None:
        return None
    window = _WINDOW_RE.search(reply)
    ranges = _RANGE_RE.findall(reply)
    if not window or not ranges:
        return None
    since = datetime.strptime(window.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    until = datetime.strptime(window.group(2), "%Y-%m-%d %H:%M").replace(tzinfo=UTC) + timedelta(minutes=1)
    _, end, total = (int(x) for x in ranges[-1])
    if end >= total:
        return None
    scope = _SCOPE_RE.search(reply)
    return {"tool": tool, "since": since, "until": until, "location": scope.group(1) if scope else None,
            "offset": end}


# ---------------------------------------------------------------- count claims (AC-AGT-28)
COUNT_Q_RE = re.compile(r"\b(?:how\s+many|count(?:s|ed|ing)?|number\s+of|tally)\b", re.IGNORECASE)
PEOPLE_Q_RE = re.compile(
    r"\b(?:people|persons?|crew(?:\s*members?)?|crewmembers?|crewmates?|astronauts?|cosmonauts?|humans?|individuals?|"
    r"anyone|anybody|somebody|someone|person\s+tracks?)\b", re.IGNORECASE)
HANDLING_Q_RE = re.compile(
    r"\b(?:use[ds]?|using|move[ds]?|moving|handle[ds]?|handling|took|take|taken|touch(?:ed|es)?|carr(?:y|ied)|"
    r"stow(?:ed)?|grab(?:bed)?|hand(?:ed)?\s+over)\b", re.IGNORECASE)
_NUM_WORDS = {
    "zero": 0, "no": None, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "dozen": 12,
}
_NUM = r"(\d{1,6}|" + "|".join(k for k, v in _NUM_WORDS.items() if v is not None) + r")"
_DURATION_RE = re.compile(_NUM + r"\s*-?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|"
                          r"h|m|s)\b", re.IGNORECASE)
_LIST_MARKER_RE = re.compile(r"(^|\n|[;:]\s)(\s*)\d{1,3}[.)](?=\s)")
_NOTE_RE = re.compile(r"\((?:showing the first \d+ of \d+; ask for more|answer shortened; ask for more)\)")
_NOT_PLURAL = frozenset({
    "this", "was", "has", "its", "is", "us", "as", "plus", "less", "across", "always", "perhaps", "unless", "yes",
    "thus", "status", "focus", "bus", "gas", "class", "glass", "pass", "process", "access", "address", "does",
    "goes", "news", "series", "species", "lens", "canvas", "bonus", "minus", "versus", "previous", "various", "nexus",
})
_COUNT_NOUN_RE = re.compile(
    r"\b" + _NUM + r"\s+(?:(?!(?:in|at|on|of|from|to|by|and|or|with|for|was|were|is|are)\b)[A-Za-z][\w-]*\s+){0,3}?"
    r"(people|persons|crew(?:\s*members)?|crewmembers|crewmates|astronauts|cosmonauts|individuals|[A-Za-z][\w-]*s)\b",
    re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"(?<![\w.:-])(\d{1,6})(?![\w:]|\.\d)")


def _strip_non_counts(text: str) -> str:
    text = _NOTE_RE.sub(" ", text)
    text = _LIST_MARKER_RE.sub(r"\1\2", text)
    for pattern in (DATE_RE, TIME_RE, LOCATION_ID_RE, NODE_RE, _DURATION_RE):
        text = pattern.sub(" ", text)
    return re.sub(r"\[[0-9a-f]{8}\]", " ", text)


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else _NUM_WORDS.get(word.lower())


def count_claims(text: str, *, counting: bool) -> list[int]:
    """Numbers stated as counts: "<number> <up to 3 words> <plural noun>"; in a counting question also every other bare
    number. Times, dates, durations, location ids, "Node N", list markers and harness notes are not counts."""
    if not isinstance(text, str) or not text:
        return []
    body = _strip_non_counts(text)
    out: list[int] = []
    for m in _COUNT_NOUN_RE.finditer(body):
        noun = m.group(2).lower()
        if noun in _NOT_PLURAL or len(noun) < 4 or m.group(2).isupper():  # "USOS" is a place, not a plural
            continue
        n = _num(m.group(1))
        if n is not None:
            out.append(n)
    if counting:
        nums = [int(x) for x in _BARE_NUM_RE.findall(body)]
        for n in nums:
            if n not in out:
                out.append(n)
    return out


def is_count_question(question: str) -> bool:
    return bool(COUNT_Q_RE.search(question or ""))


#: "how many should there be / are expected" is a catalog question (expected_count), not a sighting count.
EXPECTED_Q_RE = re.compile(r"\b(?:should|expected|supposed|normally|usually|nominal(?:ly)?|meant\s+to)\b",
                           re.IGNORECASE)


def is_people_count_question(question: str) -> bool:
    q = question or ""
    return bool(COUNT_Q_RE.search(q) and PEOPLE_Q_RE.search(q) and not HANDLING_Q_RE.search(q)
                and not EXPECTED_Q_RE.search(q))


def is_sighting_count_question(question: str) -> bool:
    """A counting question about what is on record (not the catalog's expected count)."""
    q = question or ""
    return bool(COUNT_Q_RE.search(q) and not EXPECTED_Q_RE.search(q))
