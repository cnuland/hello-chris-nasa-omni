"""AC-PER-16/17 speech handling: silence gate, hallucination filter, speech-act classification, claim parsing."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .decision import DecisionModel
from .media import rms
from .types import AudioChunk, DecisionResult

logger = logging.getLogger(__name__)

SPEECH_ACTS = ("statement", "question", "other")
SPEECH_ACT_QUESTION = "speech_act"

_INTERROGATIVES = {"where", "what", "who", "whom", "whose", "which", "when", "why", "how"}
_AUXILIARIES = {
    "is",
    "are",
    "am",
    "was",
    "were",
    "do",
    "does",
    "did",
    "can",
    "could",
    "will",
    "would",
    "should",
    "shall",
    "may",
    "might",
    "must",
    "have",
    "has",
    "had",
    "isn't",
    "aren't",
    "don't",
    "doesn't",
    "didn't",
    "can't",
    "won't",
    "shouldn't",
}
_MODAL_QUESTION = re.compile(r"\b(should|shall|can|could|may|must) (i|we)\b")


@dataclass(frozen=True)
class SpeechClaim:
    """A placement claim such as "putting the drill in node 2" -> (drill-pistol, NOD2, "moved")."""

    catalog_id: str
    location_id: str
    event_type: str = "moved"
    text: str = ""


def is_silent(chunk: AudioChunk | None, threshold: float) -> bool:
    """True when the chunk is missing/empty or its RMS is below ``threshold``."""
    if chunk is None or chunk.samples is None or len(chunk.samples) == 0:
        return True
    return rms(chunk.samples) < threshold


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text).lower())


def is_hallucination(text: str, max_repeats: int = 3) -> bool:
    """True when the same phrase (word n-gram, case/punctuation-insensitive) repeats more than ``max_repeats``
    times in a row (the classic ASR loop: "thank you thank you thank you thank you")."""
    words = _tokens(text)
    n_words = len(words)
    for n in range(1, n_words // (max_repeats + 1) + 1):
        for start in range(n_words - n * (max_repeats + 1) + 1):
            phrase = words[start : start + n]
            count, pos = 1, start + n
            while words[pos : pos + n] == phrase:
                count += 1
                pos += n
            if count > max_repeats:
                return True
    return False


def clean_transcript(text: str | None, *, silent: bool, max_repeats: int = 3) -> str:
    """'' for empty, hallucinated, or any text on a silent window; else the stripped text."""
    if text is None or silent:
        return ""
    cleaned = str(text).strip()
    if not cleaned or is_hallucination(cleaned, max_repeats):
        return ""
    return cleaned


def classify_utterance_rules(text: str) -> str:
    """Rule fallback: '?' or a leading interrogative / auxiliary (where, what, who, is, are, can, ...) or a modal
    question such as "should I" -> question; empty -> other; otherwise statement."""
    stripped = str(text or "").strip()
    words = _tokens(stripped)
    if not words:
        return "other"
    if "?" in stripped:
        return "question"
    if words[0] in _INTERROGATIVES or words[0] in _AUXILIARIES:
        return "question"
    if _MODAL_QUESTION.search(" ".join(words)):
        return "question"
    return "statement"


def classify_utterance(text: str, decision: DecisionModel | None = None) -> str:
    """statement / question / other via the decision model (choice over SPEECH_ACTS); rule fallback when there is
    no decision model or it fails (needs_review / error / unknown choice)."""
    return classify_utterance_logged(text, decision)[0]


def classify_utterance_logged(text: str, decision: DecisionModel | None = None) -> tuple[str, DecisionResult | None]:
    """Like ``classify_utterance`` but also returns the decision-model read (for the payload's decision log)."""
    if not str(text or "").strip():
        return "other", None
    if decision is None:
        return classify_utterance_rules(text), None
    try:
        result = decision.choose(
            SPEECH_ACT_QUESTION,
            list(SPEECH_ACTS),
            state={"utterance": text},
            instructions="Classify the utterance as a statement, a question or other.",
        )
    except Exception as exc:  # noqa: BLE001 - the decision model is an untrusted remote dependency
        logger.warning("speech_act decision failed; using rules", extra={"error": str(exc)})
        return classify_utterance_rules(text), None
    if result.needs_review or result.choice not in SPEECH_ACTS:
        return classify_utterance_rules(text), result
    return result.choice, result


def split_utterances(text: str) -> list[str]:
    """Split a transcript into sentences/utterances."""
    parts = re.split(r"(?<=[.!?])\s+", str(text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _catalog_phrases(catalog: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    phrases = []
    for item in catalog:
        if item.get("category") == "person":
            continue  # people are not placed somewhere by speech claims
        for name in (item.get("class_name"), *(item.get("synonyms") or [])):
            norm = " ".join(_tokens(name or ""))
            if norm:
                phrases.append((norm, item["catalog_id"]))
    phrases.sort(key=lambda p: -len(p[0]))
    return phrases


# Placement verbs (any tense) that move an item somewhere, and copulas for "the drill is in node 2".
_PLACEMENT_VERBS = {
    *[
        "put",
        "puts",
        "putting",
        "place",
        "places",
        "placed",
        "placing",
        "stow",
        "stows",
        "stowed",
        "stowing",
        "move",
        "moves",
        "moved",
        "moving",
    ],
    *[
        "leave",
        "leaves",
        "leaving",
        "left",
        "bring",
        "brings",
        "brought",
        "bringing",
        "take",
        "takes",
        "took",
        "taken",
        "taking",
    ],
    *[
        "carry",
        "carries",
        "carried",
        "carrying",
        "store",
        "stores",
        "stored",
        "storing",
        "drop",
        "drops",
        "dropped",
        "dropping",
        "set",
        "sets",
    ],
    *[
        "setting",
        "return",
        "returns",
        "returned",
        "returning",
        "stash",
        "stashed",
        "stashing",
        "pack",
        "packed",
        "packing",
        "hang",
        "hung",
    ],
    *[
        "transfer",
        "transferred",
        "transferring",
        "relocate",
        "relocated",
        "relocating",
        "tuck",
        "tucked",
        "stick",
        "stuck",
    ],
}
_COPULAS = {"is", "are", "was", "were", "sits", "sitting", "stays", "lives", "goes", "went"}
# Destination prepositions: the word right before the location (an optional "the" may sit in between).
_DESTINATION_PREPS = {"in", "into", "to", "at", "on", "onto", "inside", "toward", "towards"}
_NEGATIONS = {
    "not",
    "never",
    "no",
    "nothing",
    "nowhere",
    "cannot",
    "don't",
    "doesn't",
    "didn't",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "won't",
    "wouldn't",
    "can't",
    "couldn't",
    "shouldn't",
    "haven't",
    "hasn't",
    "hadn't",
}


def _claim_words(text: str) -> list[str]:
    """Tokens with the contraction "'s" expanded to "is" ("the drill's in node 2")."""
    words = []
    for w in _tokens(text):
        if w.endswith("'s") and len(w) > 2:
            words += [w[:-2], "is"]
        else:
            words.append(w)
    return words


def parse_speech_claim(text: str, catalog: Sequence[dict[str, Any]], resolver: Any) -> SpeechClaim | None:
    """Return a placement claim, else None. Model and crew speech is untrusted, so the rules are strict:

    * the utterance names a catalog item (class name or synonym) and is not negated (not, never, didn't, ...);
    * a placement verb (put, stow, left, moved, taking, ...) or a copula ("is", for "the drill is in node 2")
      appears after the start of the utterance and before the location;
    * the location (``resolver.resolve``) comes after the item and directly follows a destination preposition (in,
      into, to, at, on, ...), optionally with "the" in between. "from <loc>" / "out of <loc>" are never
      destinations. When several destinations are named, the last one wins ("from the lab to node 2" -> node 2).
    """
    words = _claim_words(text)
    if not words or any(w in _NEGATIONS for w in words):
        return None
    joined = f" {' '.join(words)} "
    catalog_id, item_span = None, None
    for phrase, cid in _catalog_phrases(catalog):
        idx = joined.find(f" {phrase} ")
        if idx != -1:
            start = joined[: idx + 1].count(" ") - 1
            catalog_id, item_span = cid, (start, start + len(phrase.split()))
            break
    if catalog_id is None or item_span is None:
        return None
    location_id = None
    for start in range(item_span[1], len(words)):
        prep_at = start - 1
        if words[start] == "the":
            continue  # handled through the span that starts after "the" (the resolver also accepts "the lab")
        if prep_at >= 0 and words[prep_at] == "the":
            prep_at -= 1
        if prep_at < item_span[0] or words[prep_at] not in _DESTINATION_PREPS:
            continue
        if not any(w in _PLACEMENT_VERBS or w in _COPULAS for w in words[:prep_at]):
            continue
        for n in range(min(6, len(words) - start), 0, -1):
            found = resolver.resolve(" ".join(words[start : start + n]))
            if found:
                location_id = found
                break
    if location_id is None:
        return None
    return SpeechClaim(catalog_id=catalog_id, location_id=location_id, event_type="moved", text=str(text).strip())
