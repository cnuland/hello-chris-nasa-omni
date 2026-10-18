"""AC-PER-7 label -> catalog mapping.

Order: exact class_name / catalog_id match (case/whitespace-insensitive) -> synonym match -> fuzzy string similarity
to class names, display names and synonyms -> embedding similarity (optional TextEmbedder) to the same names.
Best score below ``floor`` (default 0.55) -> ``UNKNOWN``. Only ids from the given catalog are ever returned.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np

from . import UNKNOWN
from .types import LabelMatch

DEFAULT_FLOOR = 0.55


def normalise_label(text: str) -> str:
    """Lower-case, punctuation to spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(text).lower())).strip()


def label_key(text: str) -> str:
    """AC-PER-31 grouping key for UNKNOWN-vs-UNKNOWN comparisons only: ``normalise_label`` without spaces, so
    "spacesuit", "space suit" and "Space-Suit" are one key. Display and search keep ``normalise_label``."""
    return normalise_label(text).replace(" ", "")


# AC-PER-50: words that end a label's noun phrase ("handle on the hatch" -> "handle", "window showing Earth" -> "window")
_PHRASE_END = frozenset(
    ["of", "with", "on", "in", "inside", "at", "near", "behind", "under", "over", "above", "below", "beneath", "through", "from", "to", "for", "into", "onto", "by", "and", "or", "showing", "visible", "seen", "leading", "holding", "attached", "mounted", "outside", "against", "between", "around"]
)


_DETERMINERS = frozenset(["the", "a", "an", "his", "her", "their", "its", "this", "that", "some"])


def label_heads(text: str) -> list[str]:
    """AC-PER-50/49 keys of a label's noun phrase, for denylist matching: the phrase is ``normalise_label(text)``
    without leading determiners ("the man" -> "man"), cut before the first preposition/participle (never at the first
    word); the keys (``label_key`` form, spaces removed) are the whole phrase, its last two words and its last word
    (the head noun), in that order. Empty for an empty label."""
    words = normalise_label(text).split()
    while len(words) > 1 and words[0] in _DETERMINERS:
        words = words[1:]
    for i, w in enumerate(words):
        if i > 0 and w in _PHRASE_END:
            words = words[:i]
            break
    if not words:
        return []
    keys = ["".join(words), "".join(words[-2:]), words[-1]]
    return list(dict.fromkeys(keys))


def noun_phrase(text: str, max_words: int = 6) -> str:
    """AC-PER-54 detector query of a label: ``normalise_label`` without leading determiners, cut before the first
    preposition/participle (as ``label_heads``), and at most the last ``max_words`` words (the head noun is last).
    OWLv2 caps a text query at 16 tokens and does best on a short noun phrase."""
    words = normalise_label(text).split()
    while len(words) > 1 and words[0] in _DETERMINERS:
        words = words[1:]
    for i, w in enumerate(words):
        if i > 0 and w in _PHRASE_END:
            words = words[:i]
            break
    return " ".join(words[-max_words:]) if max_words > 0 else ""


# AC-PER-50 (UI4-05): station structure and outside views are not inventory. Keys in label_key form; matched against
# label_heads(). Applied only to labels that map to UNKNOWN, so a catalogued item is always kept.
STRUCTURE_KEYS = frozenset(
    label_key(k)
    for k in (
        "hatch", "hatchway", "hatch handle", "window", "porthole", "viewport", "handle", "handrail", "handhold",
        "wall", "panel", "rack", "cable", "cables", "cabling", "wire", "wires", "wiring", "cover", "fabric cover",
        "light", "lights", "lamp", "vent", "grille", "duct", "fan", "ceiling", "floor", "deck", "bulkhead", "hull",
        "module", "node", "station", "space station", "iss", "structure", "station structure", "airlock", "cupola",
        "opening", "port", "docking port", "frame", "bracket", "mount", "rail", "interior", "background", "view",
        "spacecraft", "vehicle", "capsule", "dragon", "soyuz", "progress", "cygnus", "solar array", "truss",
        "radiator", "earth", "planet", "space", "stars", "robotic arm", "robot arm", "canadarm", "canadarm2",
        # AC-PER-59 (UI5-02): fixed parts and fixed exercise equipment seen live in round 5
        "latch", "latches", "hatch frame", "insulation", "foil", "foil insulation", "thermal insulation",
        "treadmill", "exercise equipment", "exercise machine", "exercise device", "ergometer", "cycle ergometer",
        "ared",
    )
)
# Review round: broad nouns that also head real loose items ("memory module", "portable fan", "camera mount",
# "laptop cover", "picture frame") are structure only as the whole label
STRUCTURE_WHOLE_ONLY = frozenset(label_key(k) for k in ("module", "fan", "mount", "frame", "cover"))

# AC-PER-59: words that only describe colour, material, finish, size or shape. A label made of these plus a generic
# head noun names nothing in particular ("grey object", "silver cylinder"); "cover" after only these words is a fixed
# cover ("white cloth cover"), after an item noun it is the item's cover ("laptop cover", kept).
MODIFIER_WORDS = frozenset(
    (
        "white", "black", "grey", "gray", "silver", "blue", "green", "red", "orange", "yellow", "purple",
        "pink", "brown", "beige", "tan", "gold", "golden", "dark", "light", "bright", "pale", "clear",
        "transparent", "translucent", "metallic", "shiny", "matte", "metal", "plastic", "fabric", "cloth",
        "foam", "rubber", "paper", "cardboard", "glass", "wooden", "leather", "mesh", "padded", "soft", "hard",
        "protective", "storage", "small", "large", "big", "tiny", "little", "long", "short", "round",
        "rectangular", "square", "cylindrical", "spherical", "flat", "thin", "thick", "smooth", "curved",
        "tall", "wide", "narrow", "huge", "giant", "medium", "colored", "coloured", "multicolored",
        "multicoloured"
    )
)
GENERIC_NOUNS = frozenset(
    (
        "object", "objects", "item", "items", "thing", "things", "cylinder", "cylinders", "container",
        "containers", "device", "devices", "box", "boxes", "piece", "pieces", "unit", "units", "component",
        "components", "equipment", "structure", "shape", "part", "parts", "material", "stuff", "article"
    )
)
# AC-PER-59: words in a description that make a generic label identifiable (printed text, a serial, a logo, ...)
_DISTINGUISHING = re.compile(
    r"\b(text|label(?:l?ed)?|labels|logo|serial|barcode|sticker|written|printed|marked|markings?|lettering|"
    r"brand|writing|number(?:ed)?|screen|display)\b"
    r"|\b[a-z]+-?[0-9]+[a-z0-9]*\b|\b[0-9]+-?[a-z]{2,}[0-9]*\b(?<!cm)(?<!mm)"  # identifiers such as A12, SN-2231, 3D
    r"|(?<!\w)[\"'][^\"']+[\"'](?!\w)"  # quoted text, not possessives
)
_NEGATION = frozenset(["no", "not", "never", "without", "none", "nor"])
_NEGATION_WINDOW = 3  # review round: a cue preceded by a negation within this many words does not count


def _negated(text: str, start: int) -> bool:
    before = re.findall(r"[a-z]+", text[:start])[-_NEGATION_WINDOW:]
    return any(w in _NEGATION for w in before) or " ".join(before[-2:]) == "no longer"


def _unnegated_match(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _negated(text, m.start()) for m in pattern.finditer(text))
# AC-PER-59 (UI5-02): an unknown described as fixed in place is structure. "attached to" counts for walls and hatches
# only: pouches and bags attached to a rail or rack are stowed loose items (16 real listings in the live run).
_FIXED_IN_PLACE = re.compile(
    r"\b(?:mounted|fixed|bolted|built|installed|affixed|screwed)\s+(?:on|onto|to|into|in)\s+(?:the\s+|a\s+|an\s+)?"
    r"(?:\w+\s+)?(?:wall|walls|rack|racks|hatch|hatchway|panel|panels|ceiling|floor|bulkhead|frame|structure)\b"
    r"|\battached\s+to\s+(?:the\s+|a\s+|an\s+)?(?:\w+\s+)?(?:wall|walls|hatch|hatchway|bulkhead|frame)\b"
    r"|\bon\s+the\s+hatch\s+(?:frame|rim|ring)\b"
)


def _phrase_words(text: str) -> list[str]:
    """``normalise_label`` words without leading determiners, cut before the first preposition/participle."""
    words = normalise_label(text).split()
    while len(words) > 1 and words[0] in _DETERMINERS:
        words = words[1:]
    for i, w in enumerate(words):
        if i > 0 and w in _PHRASE_END:
            return words[:i]
    return words


def is_structure_label(text: str) -> bool:
    """AC-PER-50: the label names station structure or an outside view (hatch, window, handrail, spacecraft, Earth, ...).
    AC-PER-59: "cover" is also structure as a head noun after modifier words only ("white cloth cover").
    Callers apply it only to labels that map to UNKNOWN."""
    heads = label_heads(text)
    if not heads:
        return False
    if heads[0] in STRUCTURE_KEYS or any(k in STRUCTURE_KEYS and k not in STRUCTURE_WHOLE_ONLY for k in heads[1:]):
        return True
    words = _phrase_words(text)
    return len(words) > 1 and words[-1] in ("cover", "covers") and all(w in MODIFIER_WORDS for w in words[:-1])


def is_fixed_in_place(text: str | None) -> bool:
    """AC-PER-59: the text (an unknown's label or description) says the object is fixed in place: "mounted on the
    wall", "fixed to the rack", "attached to the hatch", "on the hatch frame", "built into the wall"."""
    return bool(text) and _unnegated_match(_FIXED_IN_PLACE, " ".join(str(text).lower().split()))


def is_generic_label(label: str, description: str | None = None) -> bool:
    """AC-PER-59: a generic colour+noun label: its noun phrase is modifier words (colour, material, size, shape) and a
    generic head noun ("grey object", "silver cylinder", "blue container", "object"), and the description has nothing
    distinguishing (printed text, a label, a serial, digits, quotes, a screen)."""
    words = _phrase_words(label)
    if not words or words[-1] not in GENERIC_NOUNS or not all(w in MODIFIER_WORDS for w in words[:-1]):
        return False
    return not _unnegated_match(_DISTINGUISHING, " ".join(str(description or "").lower().split()))


def _bigrams(text: str) -> Counter[str]:
    padded = f" {text} "
    return Counter(padded[i : i + 2] for i in range(len(padded) - 1))


def fuzzy_similarity(a: str, b: str) -> float:
    """Conservative string similarity in [0, 1]: the minimum of character-bigram Dice and difflib's ratio.

    Both must agree, so short accidental overlaps ("the" vs "tape", "strange orb" vs "stowage bag") stay low while
    spelling variants ("multimetre", "flash light", "laptops") score high."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    dice = 2.0 * sum((ba & bb).values()) / float(sum(ba.values()) + sum(bb.values()))
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return min(dice, ratio)


class TextEmbedder(Protocol):
    def embed_text(self, text: str) -> np.ndarray: ...


class CatalogMapper:
    def __init__(
        self,
        catalog: Sequence[dict[str, Any]],
        *,
        text_embedder: TextEmbedder | None = None,
        floor: float = DEFAULT_FLOOR,
    ):
        self.catalog = list(catalog)
        self.text_embedder = text_embedder
        self.floor = floor
        self._by_id = {c["catalog_id"]: c for c in self.catalog}
        self._exact: dict[str, str] = {}
        self._synonyms: dict[str, str] = {}
        self._names: list[tuple[str, str]] = []  # (normalised name, catalog_id), for fuzzy / embedding
        for item in self.catalog:
            cid = item["catalog_id"]
            for key in (item.get("class_name"), cid):
                if key:
                    self._exact.setdefault(normalise_label(key), cid)
            for syn in item.get("synonyms") or []:
                self._synonyms.setdefault(normalise_label(syn), cid)
            for name in (item.get("class_name"), item.get("display_name"), *(item.get("synonyms") or [])):
                if name and normalise_label(name):
                    self._names.append((normalise_label(name), cid))
        self._name_vectors: list[tuple[np.ndarray, str]] | None = None
        self._cache: dict[str, LabelMatch] = {}

    def item(self, catalog_id: str | None) -> dict[str, Any] | None:
        return self._by_id.get(catalog_id) if catalog_id else None

    def map(self, label: str) -> LabelMatch:
        key = normalise_label(label or "")
        if key not in self._cache:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[key] = self._map(key, str(label or "").strip())
        return self._cache[key]

    def _match(self, catalog_id: str, score: float, method: str) -> LabelMatch:
        return LabelMatch(
            catalog_id=catalog_id,
            score=float(score),
            method=method,
            class_name=self._by_id[catalog_id].get("class_name"),
        )

    def _map(self, key: str, raw: str) -> LabelMatch:
        if not key:
            return LabelMatch(catalog_id=UNKNOWN, score=0.0, method="none")
        if key in self._exact:
            return self._match(self._exact[key], 1.0, "exact")
        if key in self._synonyms:
            return self._match(self._synonyms[key], 1.0, "synonym")
        best_score, best_id = 0.0, None
        for name, cid in self._names:
            score = fuzzy_similarity(key, name)
            if score > best_score:
                best_score, best_id = score, cid
        if best_id is not None and best_score >= self.floor and not self._kind_clash(key, best_id):
            # A fuzzy hit is never "exact": cap just below 1.0 (e.g. "flash light" vs "flashlight").
            return self._match(best_id, min(best_score, 0.999), "fuzzy")
        emb_score, emb_id = self._embedding_match(raw)
        if emb_id is not None and emb_score >= self.floor and not self._kind_clash(key, emb_id):
            return self._match(emb_id, emb_score, "embedding")
        return LabelMatch(catalog_id=UNKNOWN, score=float(max(best_score, emb_score, 0.0)), method="none")

    def _kind_clash(self, label: str, catalog_id: str) -> bool:
        """AC-PER-68 mapping guard: the label names another kind of thing than the class ("orange tool" vs "orange
        ball"): both families known and disjoint."""
        family = kind_family(label)
        families = class_families(self._by_id.get(catalog_id))
        return family is not None and bool(families) and family not in families

    def _embedding_match(self, text: str) -> tuple[float, str | None]:
        if self.text_embedder is None:
            return 0.0, None
        if self._name_vectors is None:
            self._name_vectors = [(_unit(self.text_embedder.embed_text(name)), cid) for name, cid in self._names]
        query = _unit(self.text_embedder.embed_text(text))
        best_score, best_id = -1.0, None
        for vec, cid in self._name_vectors:
            score = float(np.dot(query, vec))
            if score > best_score:
                best_score, best_id = score, cid
        return max(best_score, 0.0), best_id


def _unit(vector: Any) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float64).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# ------------------------------------------------------------------------------------------------ AC-PER-64
# Head-noun families for structure memory text matching: an operator who dismissed an "insulated hose" also dismissed
# the "white padded tube" that is the same duct.
HEAD_FAMILIES: dict[str, str] = {
    **{w: "conduit" for w in ("tube", "tubes", "tubing", "hose", "hoses", "hosing", "duct", "ducts", "ducting",
                              "pipe", "pipes", "piping", "conduit", "conduits")},
    **{w: "cover" for w in ("cover", "covers", "covering", "coverings", "blanket", "blankets", "wrap", "wrapping",
                            "shroud", "shrouds", "padding")},
    **{w: "container" for w in ("box", "boxes", "case", "cases", "container", "containers", "enclosure",
                                "enclosures", "housing", "housings")},
    **{w: "device" for w in ("device", "devices", "unit", "units", "display", "displays", "screen", "screens",
                             "monitor", "monitors", "console", "consoles")},
    **{w: "strap" for w in ("strap", "straps", "bungee", "bungees")},
}
_TOKEN_COLOURS = {"gray": "grey", "silver": "grey"}
# words that say nothing about what the thing is or looks like (function words, size, placement in the frame)
TOKEN_STOP_WORDS = frozenset(
    ["a", "an", "the", "and", "or", "of", "with", "without", "for", "from", "to", "into", "onto", "on", "in", "at", "by", "over", "under", "near", "along", "through", "behind", "inside", "outside", "above", "below", "beneath", "between", "around", "against", "across", "its", "it", "this", "that", "these", "those", "another", "other", "some", "any", "one", "two", "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "which", "who", "there", "here", "also", "very", "partially", "seen", "visible", "appears", "looks", "shown", "showing", "large", "small", "big", "tiny", "little", "module", "modules", "station", "node", "wall", "walls", "running", "runs", "run", "attached", "mounted", "fixed", "hanging", "floating", "side", "left", "right", "top", "bottom", "upper", "lower", "middle", "centre", "center", "foreground", "background", "frame", "view", "area", "part", "corner", "edge", "front", "back", "rear"]
)


def head_family(text: str) -> str | None:
    """AC-PER-64: the family of a label's head noun (``noun_phrase``'s last word), else the head noun itself."""
    words = noun_phrase(text).split()
    if not words:
        return None
    return HEAD_FAMILIES.get(words[-1], words[-1])


def content_tokens(*texts: str | None) -> set[str]:
    """AC-PER-64: lower-case words of the texts, family words folded to their family, colour aliases folded, stop and
    placement words removed."""
    out: set[str] = set()
    for text in texts:
        for word in re.findall(r"[a-z]+", str(text or "").lower()):
            if word in TOKEN_STOP_WORDS or len(word) < 2:
                continue
            out.add(HEAD_FAMILIES.get(word, _TOKEN_COLOURS.get(word, word)))
    return out


def token_overlap(a: set[str], b: set[str]) -> float:
    """|A ∩ B| / min(|A|, |B|); 0 unless both sides have at least 2 tokens."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    return len(a & b) / min(len(a), len(b))



# ------------------------------------------------------------------------------------------------ AC-PER-68
_KINDS: dict[str, tuple[str, ...]] = {
    "tool": ("tool", "tools", "drill", "drills", "driver", "screwdriver", "wrench", "wrenches", "spanner", "pliers",
             "hammer", "ratchet", "pgt"),
    "vacuum": ("vacuum", "vacuum cleaner", "vacuums", "hoover"),
    "sphere": ("ball", "balls", "sphere", "spheres", "globe", "orb"),
    "bag": ("bag", "bags", "pouch", "pouches", "sack", "ctb", "tote"),
    "camera": ("camera", "cameras", "camcorder", "lens", "dslr"),
    "computer": ("laptop", "laptops", "computer", "notebook", "thinkpad", "tablet", "ipad"),
    "light": ("flashlight", "torch", "lamp", "headlamp"),
    "tape": ("tape",),
    "cable": ("cable", "cables", "cord", "cords", "wire"),
    "cutter": ("scissors", "shears"),
    "meter": ("multimeter", "meter", "voltmeter"),
    "headset": ("headset", "headphones", "earphones"),
    "robot": ("robot", "astrobee"),
    "food": ("food", "meal"),
    "drink": ("drink", "beverage", "coffee", "juice"),
}
KIND_FAMILIES: dict[str, str] = {word: family for family, words in _KINDS.items() for word in words}
_ING_NOUNS = frozenset({"ring", "string", "tubing", "piping", "ducting", "wiring", "cabling", "clothing",
                        "padding", "covering", "coverings", "wrapping", "bedding", "housing", "fitting", "fittings",
                        "railing", "ceiling", "lighting", "earring", "earrings", "awning", "building"})
# a description's noun phrase also ends at these (participles and relative pronouns)
_DESCRIPTION_END = _PHRASE_END | {"being", "used", "that", "which", "who", "is", "are", "next", "hanging", "floating",
                                  "lying", "strapped", "stowed", "secured"}


def _noun_words(text: str, ends: frozenset[str] = _PHRASE_END) -> list[str]:
    words = normalise_label(text).split()
    while len(words) > 1 and words[0] in _DETERMINERS | {"another", "second", "other", "pair"}:
        words = words[1:]
    if words and words[0] == "of":
        words = words[1:]
    description = ends is _DESCRIPTION_END
    for i, w in enumerate(words):
        # review round: a description's phrase also ends at a verb form ("wearing", "containing", "typing")
        if i > 0 and (w in ends or (description and w.endswith("ing") and len(w) > 4 and w not in _ING_NOUNS)):
            return words[:i]
    return words


def kind_family(text: str | None, *, description: bool = False) -> str | None:
    """AC-PER-68: the kind family of a label (or, ``description``, a description's first noun phrase): the last two
    words, else the head noun, looked up in KIND_FAMILIES; None when unknown."""
    words = _noun_words(str(text or ""), _DESCRIPTION_END if description else _PHRASE_END)
    if not words:
        return None
    return KIND_FAMILIES.get(" ".join(words[-2:])) or KIND_FAMILIES.get(words[-1])


def class_families(item: dict[str, Any] | None) -> set[str]:
    """AC-PER-68: the kind families of a catalog class's class name, display name and synonyms."""
    if not item:
        return set()
    names = [item.get("class_name"), item.get("display_name"), *(item.get("synonyms") or [])]
    out = {f for f in (kind_family(str(n)) for n in names if n) if f}
    # review round: every word counts ("drink pouch" is a drink and a bag; "food package" is food)
    out |= {KIND_FAMILIES[w] for n in names if n for w in normalise_label(str(n)).split() if w in KIND_FAMILIES}
    return out


def description_phrases(description: str | None) -> list[str]:
    """AC-PER-68: the description's first noun phrase, its last two words and its head noun (normalised)."""
    words = _noun_words(str(description or ""), _DESCRIPTION_END)
    if not words:
        return []
    return list(dict.fromkeys([" ".join(words), " ".join(words[-2:]), words[-1]]))


# ------------------------------------------------------------------------------------------------ AC-PER-69
WATCHES = frozenset({"watch", "watches", "wristwatch", "wristwatches", "smartwatch"})
WEARABLES = frozenset({"helmet", "helmets", "glasses", "goggles", "gloves", "glove", "cap", "hat", "harness",
                       "earpiece", "headlamp", "lanyard", "badge", "bracelet", "ring", "necklace", "earring",
                       "earrings", "visor"})
# review round: "worn" (the object is worn), never "wearing" (someone near it wears something); a body place needs a
# possessive ("on her wrist", not "on the face of the rack" / "mounted on the arm")
_WORN = re.compile(
    r"\b(?:being\s+)?worn\b"
    r"|\bon\s+(?:his|her|their|someone's|the\s+person's|the\s+crew\s+member's|the\s+astronaut's|a\s+person's)\s+"
    r"(?:left\s+|right\s+)?(?:wrist|head|face|arm|neck|ear|ears|waist)\b")


def is_worn_item(label: str | None, description: str | None = None, people_present: bool = False,
                 held: bool = False) -> bool:
    """AC-PER-69: an object worn by a crew member: a watch (always personal); a wearable when people are reported or
    it is held; or a label/description that says it is worn ("worn", "wearing", "on his wrist"), unnegated."""
    words = _phrase_words(str(label or ""))
    head = words[-1] if words else ""
    if head in WATCHES:
        return True
    if head in WEARABLES and (people_present or held):
        return True
    text = " ".join(f"{label or ''} {description or ''}".lower().split())
    return _unnegated_match(_WORN, text)
