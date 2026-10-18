"""AC-PER-35/37 helpers: non-biometric appearance text for person tracks, and sleep cues for privacy.

``appearance_descriptors(text)`` turns an omni appearance string ("blue flight suit with American flag patch") into
descriptors: garment -> set of colours, plus distinctive words (flag, patch, ...). Generic words (crew, astronaut, ...)
and anything outside the clothing / colour vocabulary (activities, scenery) are ignored.

``appearance_similarity(a, b)`` in [0, 1]: 0 when both name the same garment in incompatible colours ("blue shirt" vs
"red shirt"; "dark" alone stands for the dark colours, "light"/"pale" for the light ones, so "dark hair" vs "blonde
hair" clashes); otherwise the weighted overlap coefficient of the descriptor keys (hair weighs half: most crew members
have dark hair; distinctive accessories such as gloves, headset, visor, helmet, spacesuit weigh double, AC-PER-46). ``appearance_closeness`` is the two-sided (Dice) version, used to break ties. Approximate by design:
two crew members in similar outfits look the same to it (docs/spec/20-perception.md AC-PER-35).

``sleep_cue(text)``: the first sleep cue ("sleeping bag", "sleeping", "asleep", ...) in ``text`` as a whole-word match,
else None (AC-PER-37).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MAX_TEXT = 200  # observation.schema.json people[].appearance maxLength

# multi-word phrases first, rewritten to one token
_PHRASES: tuple[tuple[str, str], ...] = (
    # AC-PER-46: sleeve length is neither colour nor garment; dropped so "grey long-sleeve shirt" stays grey
    (r"\b(?:long|short) ?sleeve[sd]?\b", " "),
    (r"\bsleeveless\b", " "),
    (r"\btank tops?\b", "shirt"),
    (r"\bspace ?suits?\b", "spacesuit"),
    (r"\beva suits?\b", "spacesuit"),
    (r"\bextravehicular(?: activity)?(?: suits?)?\b", "spacesuit"),  # AC-PER-65
    (r"\borlan(?: suits?)?\b", "spacesuit"),
    (r"\bemu(?: suits?)?\b", "spacesuit"),
    (r"\b(?:u\.? ?s\.? ?a?\.?|usa)\s*(?=flags?\b)", "american "),  # "US flag", "U.S. flag" -> American flag
    (r"\bflags\b", "flag"),
    (r"\blogos\b", "logo"),
    (r"\bt ?shirts?\b", "shirt"),
    (r"\btees?\b", "shirt"),
    (r"\bflight suits?\b", "suit"),
    (r"\bjump ?suits?\b", "suit"),
    (r"\bcoveralls?\b", "suit"),
    (r"\bsleeping bags?\b", "sleepingbag"),
    (r"\bpolo shirts?\b", "shirt"),
    (r"\bsweat ?shirts?\b", "sweater"),
    (r"\bhooded\b", "hoodie"),
)
_GARMENT_ALIASES = {
    "shirts": "shirt", "blouse": "shirt", "top": "shirt", "tops": "shirt", "tank": "shirt",
    "trousers": "pants", "slacks": "pants", "jeans": "pants", "cargo": "pants", "leggings": "pants",
    "shorts": "shorts", "suits": "suit", "jackets": "jacket", "fleece": "jacket", "hoodies": "hoodie",
    "sweaters": "sweater", "jumper": "sweater", "vests": "vest", "polo": "shirt", "polos": "shirt", "caps": "cap",
    "hats": "hat", "haired": "hair",
    "headsets": "headset", "headphones": "headset", "glasses": "glasses", "goggles": "glasses",
    "spectacles": "glasses", "gloves": "gloves", "socks": "socks", "shoes": "shoes", "sneakers": "shoes",
    "patches": "patch", "headband": "headband", "lanyard": "lanyard", "badge": "badge", "hair": "hair",
    "beard": "beard", "skirt": "skirt",
    "spacesuits": "spacesuit", "helmets": "helmet", "visors": "visor", "watches": "watch", "wristwatch": "watch",
    "harnesses": "harness",
}
GARMENTS = frozenset(
    {"shirt", "pants", "shorts", "suit", "jacket", "hoodie", "sweater", "vest", "cap", "hat", "headset",
     "glasses", "gloves", "socks", "shoes", "patch", "headband", "lanyard", "badge", "hair", "beard", "skirt",
     "sleepingbag", "spacesuit", "helmet", "visor", "watch", "harness"}
)
_COLOUR_ALIASES = {
    "gray": "grey", "silver": "grey", "charcoal": "grey", "navy": "blue", "teal": "blue", "turquoise": "blue",
    "maroon": "red", "burgundy": "red", "crimson": "red", "olive": "green", "lime": "green", "gold": "yellow",
    "golden": "yellow", "tan": "khaki", "beige": "khaki", "blond": "blonde", "violet": "purple",
}
COLOURS = frozenset(
    {"black", "white", "grey", "blue", "red", "green", "yellow", "orange", "purple", "pink", "brown", "khaki",
     "blonde"}
)
# a modifier without a specific colour stands for a colour family ("dark hair" clashes with "blonde hair")
DARK = frozenset({"black", "blue", "brown", "grey", "green", "red", "purple"})
LIGHT = frozenset({"white", "grey", "blue", "yellow", "khaki", "pink", "green", "blonde", "orange"})
MODIFIERS = {"dark": DARK, "light": LIGHT, "pale": LIGHT, "bright": COLOURS}
DISTINCTIVE = frozenset(
    {"flag", "american", "nasa", "esa", "jaxa", "roscosmos", "logo", "stripes", "striped", "bald", "ponytail",
     "mustache", "moustache", "camouflage", "plaid", "checkered"}
)
_BOUNDARIES = frozenset({"with", "plus"})  # "and"/"or" may join colours: "white and blue shirt"
MAX_GAP = 1  # non-colour words allowed between a colour and its garment ("blue NASA polo")
# AC-PER-46: hair is generic (most crew have dark hair); a distinctive accessory says more than a garment
ACCESSORY_WEIGHT = 2.0
ACCESSORIES = frozenset(  # review round: "watch" is common, not distinctive (weight 1, no evidence on its own)
    {"gloves", "headset", "visor", "helmet", "glasses", "harness", "spacesuit", "lanyard", "badge"}
)
WEIGHTS = {"hair": 0.5, **{k: ACCESSORY_WEIGHT for k in ACCESSORIES}}
INSIGNIA = frozenset({"flag", "patch", "american", "nasa", "esa", "jaxa", "roscosmos", "logo"})
EVIDENCE_KEYS = ACCESSORIES | INSIGNIA


def sanitise_text(value: Any, limit: int = MAX_TEXT) -> str:
    """Invisible format characters (soft hyphen, zero-width space, bidi overrides) deleted, other control characters
    turned into spaces, whitespace collapsed, at most ``limit`` characters ("" for None)."""
    if value is None:
        return ""
    out = []
    for ch in str(value):
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append(" " if cat.startswith("C") else ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()[:limit].strip()


def _normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", sanitise_text(text, 10_000)).lower()
    t = re.sub(r"[-_]", " ", t)
    for pattern, repl in _PHRASES:
        t = re.sub(pattern, repl, t)
    return t


Colours = frozenset[str] | None  # allowed colours of a descriptor; None = no colour named (compatible with any)


MAX_DESCRIPTORS = 8  # review round (AC-PER-46): real Gemma appearances name at most 5
MAX_GARMENT_COLOURS = 3  # real: at most 2 ("white and blue shirt")


def appearance_descriptors(text: str | None) -> dict[str, Colours]:
    """{garment or distinctive word: allowed colours or None}. Specific colours win over a modifier ("light grey" ->
    grey); a modifier alone gives its family; several colours ("white and blue") are all allowed. An implausibly
    broad description (more than MAX_DESCRIPTORS keys, or a garment in more than MAX_GARMENT_COLOURS specific colours)
    yields {} (featureless): it would otherwise be compatible with, and match, every crew member."""
    out = _descriptors(text)
    if len(out) > MAX_DESCRIPTORS:
        return {}
    if any(c is not None and c not in (DARK, LIGHT, COLOURS) and len(c) > MAX_GARMENT_COLOURS for c in out.values()):
        return {}
    return out


def _descriptors(text: str | None) -> dict[str, Colours]:
    out: dict[str, Colours] = {}
    if not text:
        return out
    for chunk in re.split(r"[,;:/()\[\]|.]+", _normalise(text)):
        colours: set[str] = set()
        family: frozenset[str] | None = None
        gap = 0
        for raw in re.findall(r"[a-z0-9]+", chunk):
            word = _COLOUR_ALIASES.get(raw, raw)
            if word in _BOUNDARIES:
                colours, family, gap = set(), None, 0
            elif word in COLOURS:
                colours.add(word)
                gap = 0
            elif word in MODIFIERS:
                family = MODIFIERS[word]
                gap = 0
            elif word in ("and", "or"):
                continue
            else:
                garment = _GARMENT_ALIASES.get(word, word)
                if garment in GARMENTS:
                    allowed = frozenset(colours) if colours else family
                    previous = out.get(garment)
                    if garment in out and (previous is None or allowed is None):
                        out[garment] = previous if allowed is None else allowed
                    else:
                        out[garment] = allowed if previous is None else previous | allowed
                    colours, family, gap = set(), None, 0
                    continue
                if word in DISTINCTIVE:
                    out.setdefault(word, None)
                if colours or family:
                    gap += 1
                    if gap > MAX_GAP:
                        colours, family, gap = set(), None, 0
    return out


def _weight(key: str) -> float:
    return WEIGHTS.get(key, 1.0)


def _compare(a: str | None, b: str | None) -> tuple[bool, float, float]:
    """(clash, overlap coefficient, Dice) of two appearance texts."""
    da, db = appearance_descriptors(a), appearance_descriptors(b)
    if not da or not db:
        return False, 0.0, 0.0
    la, lb = _hair_length(a), _hair_length(b)
    if la and lb and not (la & lb):
        return True, 0.0, 0.0  # AC-PER-66: "long hair" vs "short hair" is two people, like two colours
    lega, legb = {k for k in da if k in LEGWEAR}, {k for k in db if k in LEGWEAR}
    if lega and legb and not (lega & legb):
        return True, 0.0, 0.0  # AC-PER-66: trousers vs shorts (vs a skirt) is two people
    shared = 0.0
    for key in da.keys() & db.keys():
        ca, cb = da[key], db[key]
        if ca is not None and cb is not None and not (ca & cb):
            return True, 0.0, 0.0  # the same garment in incompatible colours: a different person
        shared += _weight(key)
    wa, wb = sum(_weight(k) for k in da), sum(_weight(k) for k in db)
    overlap = shared / min(wa, wb) if min(wa, wb) > 0 else 0.0
    dice = 2.0 * shared / (wa + wb) if wa + wb > 0 else 0.0
    return False, round(overlap, 6), round(dice, 6)


def appearance_similarity(a: str | None, b: str | None) -> float:
    return _compare(a, b)[1]


def appearance_closeness(a: str | None, b: str | None) -> float:
    """Two-sided (Dice) similarity: "headset" is as similar to "blue shirt, headset" as "blue shirt, headset" is
    (overlap 1.0), but less close (0.67 against 1.0). Breaks ties between equally similar candidates."""
    return _compare(a, b)[2]


def appearance_evidence(a: str | None, b: str | None) -> bool:
    """Review round (AC-PER-45/47): a positive, specific shared trait: a garment both texts colour compatibly (hair
    excluded: generic), or a shared distinctive accessory or insignia. Colourless garments ("a shirt") and a watch are
    not evidence on their own."""
    da, db = appearance_descriptors(a), appearance_descriptors(b)
    for key in da.keys() & db.keys():
        ca, cb = da[key], db[key]
        if key in EVIDENCE_KEYS:
            return True
        if key != "hair" and ca is not None and cb is not None and ca & cb:
            return True
    return False


def appearance_conflict(a: str | None, b: str | None) -> bool:
    """True when both texts name a garment in incompatible colours (evidence of two different people)."""
    return _compare(a, b)[0]


# ------------------------------------------------------------------------------------------------ AC-PER-37
SLEEP_CUES: tuple[str, ...] = (
    "sleeping bags", "sleeping bag", "sleepingbags", "sleepingbag", "sleep stations", "sleep station", "sleep pods",
    "sleep pod", "sleep rack", "sleep restraint", "crew quarters", "sleeping", "asleep", "sleeps", "slumbering",
    "slumber", "napping", "dozing", "dozes", "bunks", "bunk",
)
_SLEEP_RE = [(cue, re.compile(r"\b" + r"\s+".join(map(re.escape, cue.split())) + r"\b")) for cue in SLEEP_CUES]


def sleep_cue(text: str | None) -> str | None:
    """First sleep cue of ``SLEEP_CUES`` found in ``text`` (whole words, case / punctuation-insensitive)."""
    if not text:
        return None
    t = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", sanitise_text(text, 10_000)).lower())
    for cue, pattern in _SLEEP_RE:
        if pattern.search(t):
            return cue
    return None


def find_sleep_cue(obs: dict[str, Any]) -> tuple[str, str] | None:
    """(cue, field) for the first sleep cue in people[].appearance/activity, objects[].label/description, or
    scene_summary of an observation; None when there is none."""
    for i, p in enumerate(obs.get("people") or []):
        if isinstance(p, dict):
            for key in ("appearance", "activity"):
                cue = sleep_cue(p.get(key) if isinstance(p.get(key), str) else None)
                if cue:
                    return cue, f"people[{i}].{key}"
    for i, o in enumerate(obs.get("objects") or []):
        if isinstance(o, dict):
            for key in ("label", "description"):
                cue = sleep_cue(o.get(key) if isinstance(o.get(key), str) else None)
                if cue:
                    return cue, f"objects[{i}].{key}"
    summary = obs.get("scene_summary")
    cue = sleep_cue(summary if isinstance(summary, str) else None)
    return (cue, "scene_summary") if cue else None



# ------------------------------------------------------------------------------------------------ AC-PER-65
# what an empty suit can "wear": everything else (hair, shirt, trousers, glasses, headset, watch, ...) is a person's
SUIT_KEYS = frozenset({"spacesuit", "suit", "gloves", "helmet", "visor", "shoes"}) | INSIGNIA
_BODY_WORDS = re.compile(
    r"\b(face|faces|skin|smiling|smile|smiles|eyes|eye|beard|bearded|moustache|mustache|man|woman|men|women|he|she|"
    r"his|her|hair|bald)\b")
_HUMAN_ACTIVITY = re.compile(
    r"\b(talk|talks|talking|speak|speaks|speaking|smil\w*|laugh\w*|eat|eats|eating|drink|drinks|drinking|typ\w*|"
    r"writ\w*|wav\w*|gestur\w*|explain\w*|point|points|pointing|hold|holds|holding|use|uses|using|operat\w*|"
    r"carr\w*|open|opens|opening|clos\w*|adjust\w*)\b")


def _truthy(value: Any) -> bool:
    """Review round: model output may say "true", 1 or "yes" for a boolean."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "y")
    return bool(value) and value is not False


def is_empty_suit(appearance: str | None, activity: str | None = None, is_speaking: Any = False) -> bool:
    """AC-PER-65: a people[] appearance that describes an empty spacesuit (a stowed EMU), not a crew member: it names a
    spacesuit (or a suit coloured only white) and nothing only a person has (other garments, hair, face words), the
    entry is not speaking, and the activity has no clearly human verb."""
    if _truthy(is_speaking) or not appearance:
        return False
    desc = _descriptors(appearance)
    suit = "spacesuit" in desc or desc.get("suit") == frozenset({"white"})
    if not suit or any(k not in SUIT_KEYS for k in desc):
        return False
    if _BODY_WORDS.search(_normalise(appearance)):
        return False
    return not (activity and _HUMAN_ACTIVITY.search(_normalise(str(activity))))



# ------------------------------------------------------------------------------------------------ AC-PER-66
LEGWEAR = frozenset({"pants", "shorts", "skirt"})
_HAIR_LENGTH = re.compile(r"\b(long|short)(?:\s+[a-z]+)?\s+hair(?:ed)?\b|\b(long|short)\s*haired\b")


def _hair_length(text: str | None) -> set[str]:
    """{"long"} / {"short"} when the text names a hair length ("long dark hair", "short-haired"); sleeve lengths are
    removed by the normaliser first."""
    if not text:
        return set()
    return {m.group(1) or m.group(2) for m in _HAIR_LENGTH.finditer(_normalise(text))}


_OBJECT_DETERMINERS = frozenset(["the", "a", "an", "another", "second", "other", "some", "one", "two", "this", "that"])
_PHRASE_CUT = frozenset(
    ["of", "with", "on", "in", "inside", "at", "near", "behind", "under", "over", "above", "below", "beneath",
     "through", "from", "to", "for", "into", "onto", "by", "and", "or", "showing", "visible", "seen", "leading",
     "holding", "attached", "mounted", "outside", "against", "between", "around", "next", "hanging", "floating",
     "being", "used", "that", "which", "lying", "stowed", "strapped", "secured", "displaying", "containing",
     "featuring", "labelled", "labeled", "filled", "covered", "showing", "with"])


def _phrase_colours(text: str | None) -> frozenset[str] | None:
    words = re.findall(r"[a-z]+", unicodedata.normalize("NFKC", str(text or "")).lower())
    while words and words[0] in _OBJECT_DETERMINERS:
        words = words[1:]
    for i, w in enumerate(words):
        if i > 0 and (w in _PHRASE_CUT or (w.endswith("ing") and len(w) > 4)):
            words = words[:i]
            break
    colours: set[str] = set()
    family: frozenset[str] | None = None
    for raw in words:
        word = _COLOUR_ALIASES.get(raw, raw)
        if word in COLOURS:
            colours.add(word)
        elif word in MODIFIERS and word != "bright":
            family = MODIFIERS[word] if family is None else family | MODIFIERS[word]
    if colours:
        return frozenset(colours)
    return family


def object_colours(label: str | None, description: str | None = None) -> frozenset[str] | None:
    """AC-PER-66: the colours of an object: the colour words of its label's noun phrase, else of its description's
    first noun phrase (aliases folded; "dark"/"light" alone give a family); None when neither names a colour."""
    return _phrase_colours(label) or _phrase_colours(description)


def object_colour_conflict(a: tuple[str | None, str | None], b: tuple[str | None, str | None]) -> bool:
    """AC-PER-66: both (label, description) pairs name colours and the colour sets are disjoint."""
    ca, cb = object_colours(*a), object_colours(*b)
    return ca is not None and cb is not None and not (_lighting(ca) & _lighting(cb))


def _lighting(colours: frozenset[str]) -> frozenset[str]:
    """Review round: white and grey (silver) are one colour under station lighting."""
    return colours | {"white", "grey"} if colours & {"white", "grey"} else colours
