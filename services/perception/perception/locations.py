"""AC-PER-11 location resolution.

``resolve(hint)`` maps a free-text hint ("NOD2", "node 2", "Node two", "Harmony", "Destiny", "lab starboard rack 2")
to a known location_id, else None. ``is_no_capture(location_id)`` is True when the location or any ancestor is
flagged ``no_capture``. ``module_of(location_id)`` returns the enclosing module id (or the id itself for a module).

Matching is exact on normalised aliases (no fuzzy matching: a wrong location is worse than none). Aliases are the
location id, the display name, the display name split around parentheses for modules and segments only
("Destiny (US Lab)" -> "destiny", "us lab"; a zone's "(port)" is a qualifier, not a name), "node N" for NODn
modules, and a small table of common ISS module names.

AC-PER-23: when no exact alias matches, a relaxed match ignores filler words ("the", "module", "laboratory", "iss",
"international space station", ...) on both sides, also trying "laboratory" as "lab"; a relaxed key shared by two
locations is ambiguous and resolves to nothing. ``is_private_hint`` is True when the hint resolves to a no_capture
location or names a private place (a no_capture location's core name such as "crew quarters" / "whc", or a curated
term such as "toilet"). ``window_policy`` returns the window location, privacy and the fixed-camera hint conflict
flag (see ``window_policy``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}

# Common and official names (research/04 §B; AC-PER-39), keyed by seed location id. Only ids present in the seed
# are registered (no invented codes: the seed has no Quest, Poisk, Rassvet or PMA).
ISS_ALIASES: dict[str, tuple[str, ...]] = {
    "NOD1": ("unity", "node 1"),
    "NOD2": ("harmony", "node 2"),
    "NOD3": ("tranquility", "tranquillity", "node 3"),
    "LAB1": ("destiny", "us lab", "u s lab", "the lab", "lab", "destiny lab", "us laboratory", "u s laboratory",
             "destiny laboratory"),
    "COL1": ("columbus", "columbus module", "columbus laboratory", "columbus lab", "european laboratory",
             "european lab"),
    "JPM1": ("kibo", "jem", "jpm", "kibo pm", "jem pm", "japanese experiment module", "kibo pressurized module",
             "jem pressurized module"),
    "JLP1": ("jlp", "elm ps", "kibo logistics module", "jem logistics module", "experiment logistics module"),
    "PMM1": ("leonardo", "pmm", "permanent multipurpose module"),
    "CUP1": ("cupola",),
    "ZVEZDA": ("zvezda", "service module", "sm"),
    "ZARYA": ("zarya", "fgb", "functional cargo block"),
    "NAUKA": ("nauka", "mlm", "multipurpose laboratory module"),
    "USOS": ("us segment", "us orbital segment"),
    "ROS": ("russian segment",),
}

# AC-PER-39 relaxed keys that are only a generic word: never registered ("Service Module" stays an exact alias).
RELAXED_STOP_KEYS = frozenset(
    {"multipurpose", "service", "experiment", "japanese experiment", "logistics", "pressurized", "european", "us",
     "u s"}
)

# AC-PER-39 mention table for free text (hint fallback and scene summaries): distinctive names only. Generic names
# ("lab", "the lab", "SM", "node" alone, relaxed keys) are not mentions. Longest phrase first, whole words.
MODULE_MENTIONS: dict[str, tuple[str, ...]] = {
    "NOD1": ("unity", "node 1"),
    "NOD2": ("harmony", "node 2"),
    "NOD3": ("tranquility", "tranquillity", "node 3"),
    "LAB1": ("destiny", "destiny lab", "destiny laboratory", "us lab", "u s lab", "us laboratory", "u s laboratory"),
    "COL1": ("columbus", "european laboratory", "european lab"),
    "JPM1": ("kibo", "jem", "jpm", "japanese experiment module", "kibo pressurized module", "jem pressurized module"),
    "JLP1": ("jlp", "kibo logistics module", "jem logistics module", "experiment logistics module",
             "kibo s logistics module", "jem s logistics module", "elm ps"),
    "PMM1": ("leonardo", "pmm", "permanent multipurpose module"),
    "CUP1": ("cupola",),
    "ZVEZDA": ("zvezda", "service module"),
    "ZARYA": ("zarya", "fgb", "functional cargo block"),
    "NAUKA": ("nauka", "mlm", "multipurpose laboratory module"),
}
# Modules the seed lacks: a mention places the camera elsewhere (AC-PER-41) but never resolves. Keys are not ids.
UNSEEDED_MENTIONS: dict[str, tuple[str, ...]] = {
    "Quest": ("quest", "quest airlock", "joint airlock"),
    "PMA": ("pma", "pma 1", "pma 2", "pma 3", "pressurized mating adapter"),
    "Poisk": ("poisk", "mini research module 2", "mrm2"),
    "Rassvet": ("rassvet", "mini research module 1", "mrm1"),
    "Pirs": ("pirs",),
    "Prichal": ("prichal",),
    "BEAM": ("beam",),
    "Bishop": ("bishop", "bishop airlock"),
}
# Mention phrases that are ordinary English words: a one-word phrase (or "US ...") counts only when written as below
# in the original text, or in capitals ("UNITY").
_CASED_MENTIONS: dict[str, str] = {
    "unity": "Unity", "harmony": "Harmony", "destiny": "Destiny", "tranquility": "Tranquility",
    "tranquillity": "Tranquillity", "columbus": "Columbus", "leonardo": "Leonardo", "us": "US", "beam": "BEAM",
    "quest": "Quest", "bishop": "Bishop",
}
# Review round: names that are also ordinary words or common names need module context: the next word is a module
# word, or (except STRICT) the previous word is an in / direction / not-inside word ("Unity is important" is not NOD1;
# "Columbus Day", "Leonardo da Vinci" are not modules).
_CONTEXT_MENTIONS = frozenset({"unity", "harmony", "destiny", "tranquility", "tranquillity", "columbus", "leonardo"})
_STRICT_CONTEXT_MENTIONS = frozenset({"leonardo"})
_MODULE_WORDS = frozenset({"module", "node", "lab", "laboratory", "pmm"})
_IN_WORDS = frozenset({"in", "inside", "within", "aboard"})
# Direction and not-inside words: the camera is not (or not only) in the module that follows.
_DIRECTION_WORDS = frozenset(
    {"to", "into", "toward", "towards", "from", "onto", "leaving", "leaves", "exits", "exiting", "outside", "near",
     "behind", "beyond", "past", "across", "opposite", "between"}
)
_DIRECTION_BEFORE_OF = frozenset({"front", "out", "outside"})  # "in front of", "out of", "outside of"
_DIRECTION_AFTER = frozenset({"hatch", "hatchway"})  # "the Kibo hatch": seen from outside
_SKIP_BEFORE_MENTION = frozenset({"the", "a", "an"})
_BARRIER_CHARS = frozenset(",;:!?()[]{}—–")  # punctuation a mention never spans ("." handled apart)


# AC-PER-23 words ignored by the relaxed match (never location names on their own).
FILLER_WORDS = frozenset(
    {"the", "a", "an", "module", "laboratory", "iss", "international", "space", "station", "of", "in", "inside",
     "aboard", "on", "board", "onboard"}
)
# Curated private-place terms (crew quarters, hygiene). Any hint containing one makes the window private.
PRIVATE_TERMS: tuple[str, ...] = (
    "crew quarters", "crew quarter", "sleep station", "sleeping quarters", "toilet", "whc", "waste and hygiene",
    "hygiene compartment", "bathroom", "lavatory", "restroom",
    # AC-PER-39 review round: abbreviations and partial names that a resolved module would otherwise make capturable
    "cq", "cqs", "quarters", "hygiene area",
)


def _strip_filler(key: str) -> str:
    return " ".join(w for w in key.split() if w not in FILLER_WORDS)


@dataclass(frozen=True)
class WindowScope:
    """``location``: the window location; ``no_capture``: the window is private; ``hint_conflict``: a fixed
    camera's hint named a location in another module (AC-PER-23: sightings get low confidence); ``source``: where
    the location came from (``hint``, ``summary`` or ``camera``, AC-PER-40); ``elsewhere``: the hint or summary
    places the camera in a module the seed lacks (AC-PER-41: breaks the module carry)."""

    location: str | None
    no_capture: bool
    hint_conflict: bool = False
    source: str = "camera"
    elsewhere: bool = False
    mentions: frozenset[str] = frozenset()  # AC-PER-41 review: modules the hint/summary mention (any marker)


@dataclass(frozen=True)
class ModuleScan:
    """AC-PER-39 mention-scan decision: ``module`` (a seed module id) or None; ``elsewhere`` names the unseeded
    module the text places the camera in; ``mentions`` = (target, marker) in text order, marker in/dir/plain."""

    module: str | None = None
    elsewhere: str | None = None
    mentions: tuple[tuple[str, str], ...] = ()


_UNSEEDED = "~"  # prefix of unseeded mention targets (never a location id)


def _tokens(text: str) -> tuple[list[str], list[str], list[bool]]:
    """Original-case tokens, their normalised forms and barrier flags (``barrier[i]``: punctuation such as a comma, a
    semicolon or a sentence-ending period separates token i from token i-1; an abbreviation dot as in "U.S." does
    not). Number words become digits only right after a capitalised "Node" with no barrier ("Node two" -> "node 2",
    but not "the node one crew member" or "the node, one crew member")."""
    original: list[str] = []
    words: list[str] = []
    barrier: list[bool] = []
    last_end = 0
    for m in re.finditer(r"[A-Za-z0-9]+", str(text)):
        gap = str(text)[last_end : m.start()]
        prev = original[-1] if original else ""
        stop = any(c in _BARRIER_CHARS for c in gap) or ("." in gap and len(prev) > 1)
        word = m.group(0).lower()
        if original and original[-1] in ("Node", "NODE") and not stop:
            word = _NUMBER_WORDS.get(word, word)
        original.append(m.group(0))
        words.append(word)
        barrier.append(bool(original[:-1]) and stop)
        last_end = m.end()
    return original, words, barrier


def normalise_hint(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower()).split()
    return " ".join(_NUMBER_WORDS.get(w, w) for w in words)


class LocationResolver:
    def __init__(self, locations: Sequence[dict[str, Any]]):
        self.locations = list(locations)
        self._by_id = {loc["location_id"]: loc for loc in self.locations}
        self._aliases: dict[str, str] = {}
        # Pass 1: ids and full display names win over derived aliases.
        for loc in self.locations:
            lid = loc["location_id"]
            self._add(lid, lid)
            if loc.get("display_name"):
                self._add(loc["display_name"], lid)
        # Pass 2: derived aliases, curated names, "node N".
        for loc in self.locations:
            lid = loc["location_id"]
            name = loc.get("display_name") or ""
            # Parentheses carry an alternative name for modules ("Destiny (US Lab)") but only a bare qualifier for
            # zones ("Node 2 crew quarters (port)"): "port"/"deck" must never resolve to a crew-quarters zone.
            if "(" in name and loc.get("kind") in ("module", "segment"):
                outside = re.sub(r"\(.*?\)", " ", name)
                self._add(outside, lid)
                for inside in re.findall(r"\((.*?)\)", name):
                    self._add(inside, lid)
            for alias in ISS_ALIASES.get(lid, ()):
                self._add(alias, lid)
            m = re.fullmatch(r"NOD(\d+)", lid)
            if m:
                self._add(f"node {m.group(1)}", lid)
        # AC-PER-23 relaxed aliases (filler words removed); a key claimed by two locations is ambiguous (None).
        self._relaxed: dict[str, str | None] = {}
        self._relaxed_ids: dict[str, set[str]] = {}  # every location claiming a relaxed key (privacy check)
        for key, lid in self._aliases.items():
            relaxed = _strip_filler(key)
            if not relaxed or relaxed in RELAXED_STOP_KEYS:  # AC-PER-39: a generic word alone never resolves
                continue
            self._relaxed_ids.setdefault(relaxed, set()).add(lid)
            if relaxed in self._relaxed and self._relaxed[relaxed] != lid:
                self._relaxed[relaxed] = None
            else:
                self._relaxed[relaxed] = lid
        self._private_terms = self._build_private_terms()
        self._mentions = self._build_mentions()

    def _build_private_terms(self) -> tuple[str, ...]:
        """Curated terms plus the core name of every no_capture location: its display name without the
        parenthetical qualifier and without the words of its module's id / name (e.g. "crew quarters", "whc")."""
        terms = {normalise_hint(t) for t in PRIVATE_TERMS}
        for loc in self.locations:
            if not loc.get("no_capture"):
                continue
            terms.add(normalise_hint(loc["location_id"]))
            if loc.get("kind") in ("module", "segment"):
                continue
            name = normalise_hint(re.sub(r"\(.*?\)", " ", loc.get("display_name") or ""))
            module = self._by_id.get(self.module_of(loc["location_id"]) or "", {})
            module_words = set(normalise_hint(module.get("display_name") or "").split())
            module_words |= set(normalise_hint(module.get("location_id") or "").split())
            mid = re.fullmatch(r"NOD(\d+)", str(module.get("location_id") or ""))
            if mid:
                module_words |= {"node", mid.group(1)}
            core = " ".join(w for w in name.split() if w not in module_words and w not in FILLER_WORDS)
            if core and not core.isdigit():
                terms.add(core)
        return tuple(sorted(t for t in terms if t))

    def _build_mentions(self) -> list[tuple[tuple[str, ...], str]]:
        """AC-PER-39 mention phrases (normalised tokens) -> seed module id, or ``~Name`` for a module the seed lacks
        (skipped when the seed does have it). Longest phrase first."""
        out: list[tuple[tuple[str, ...], str]] = []
        for lid, phrases in MODULE_MENTIONS.items():
            if self._by_id.get(lid, {}).get("kind") != "module":
                continue  # only seed modules: no invented codes
            out += [(tuple(normalise_hint(p).split()), lid) for p in phrases]
        for name, phrases in UNSEEDED_MENTIONS.items():
            if any(normalise_hint(p) in self._aliases for p in phrases):
                continue
            out += [(tuple(normalise_hint(p).split()), _UNSEEDED + name) for p in phrases]
        return sorted(out, key=lambda m: -len(m[0]))

    def scan_modules(self, text: str | None) -> ModuleScan:
        """AC-PER-39 mention scan of free text (a hint that did not resolve, or a scene summary).

        Mentions are whole-word phrases (longest first, non-overlapping, never across punctuation such as a comma or
        a sentence end). Ordinary English words count only when capitalised as names and, for common words, with
        module context (see ``_CONTEXT_MENTIONS``). A Kibo/JEM mention directly followed by a logistics-module
        mention ("JEM Experiment Logistics Module", "Kibo's ...") is part of that JLP1 name. A mention is *in* when
        the preceding word (skipping articles and an adjacent mention of the same module) is in/inside/within/aboard,
        *dir* when it is a direction or not-inside word (to, into, toward(s), from, onto, leaving, exits, outside,
        near, behind, beyond, past, across, opposite, between, "in front of", "out of") or the mention is followed by
        "hatch", else *plain*; a mention after "and"/"or" right after another mention takes that mention's marker. One module mentioned -> that module unless every mention is *dir*; several -> the
        single module with an *in* mention, else none. An unseeded module decided this way is ``elsewhere``."""
        if not text:
            return ModuleScan()
        original, words, barrier = _tokens(text)
        n_words = len(words)

        def prev_word(i: int) -> int:
            """Index of the word before position i, skipping articles; -1 at the start or across a barrier."""
            j = i - 1
            while j >= 0 and not barrier[j + 1]:
                if words[j] not in _SKIP_BEFORE_MENTION:
                    return j
                j -= 1
            return -1

        found: list[tuple[int, int, str]] = []  # (start, end, target)
        taken = [False] * n_words
        for phrase, target in self._mentions:
            n = len(phrase)
            for i in range(n_words - n + 1):
                if tuple(words[i : i + n]) != phrase or any(taken[i : i + n]) or any(barrier[i + 1 : i + n]):
                    continue
                cased = _CASED_MENTIONS.get(phrase[0])
                if cased is not None and (n == 1 or phrase[0] == "us") and original[i] not in (cased, cased.upper()):
                    continue  # "harmony" the word, "us" the pronoun, "a beam of light"
                if n == 1 and phrase[0] in _CONTEXT_MENTIONS:
                    nxt = words[i + 1] if i + 1 < n_words and not barrier[i + 1] else ""
                    j = prev_word(i)
                    before = words[j] if j >= 0 else ""
                    framed = before in _IN_WORDS or before in _DIRECTION_WORDS or before in ("of", "and", "or")
                    if nxt not in _MODULE_WORDS and (phrase[0] in _STRICT_CONTEXT_MENTIONS or not framed):
                        continue
                found.append((i, i + n, target))
                for k in range(i, i + n):
                    taken[k] = True
        found.sort()
        # "JEM Experiment Logistics Module", "Kibo's ...": the JPM1 mention is part of the JLP1 name
        kept: list[tuple[int, int, str]] = []
        for idx, (start, end, target) in enumerate(found):
            if target == "JPM1" and idx + 1 < len(found) and found[idx + 1][2] == "JLP1":
                gap = found[idx + 1][0] - end
                if not any(barrier[end : found[idx + 1][0] + 1]) and (
                    gap == 0 or (gap == 1 and words[end] == "s")
                ):
                    continue
            kept.append((start, end, target))
        found = kept
        mentions: list[tuple[str, str]] = []
        for idx, (start, end, target) in enumerate(found):
            marker = "plain"
            j = prev_word(start)
            if idx > 0 and found[idx - 1][2] == target and found[idx - 1][1] - 1 == j:
                marker = mentions[idx - 1][1]  # "in the Japanese Experiment Module Kibo": Kibo inherits "in"
            elif j >= 0 and words[j] in ("and", "or") and idx > 0 and found[idx - 1][1] - 1 == prev_word(j):
                marker = mentions[idx - 1][1]  # "between Harmony and Kibo": both dir; "in Kibo or JEM": both in
            elif j >= 0 and words[j] in _IN_WORDS:
                marker = "in"
            elif j >= 0 and (
                words[j] in _DIRECTION_WORDS
                or (words[j] == "of" and j > 0 and not barrier[j] and words[j - 1] in _DIRECTION_BEFORE_OF)
            ):
                marker = "dir"
            if end < n_words and not barrier[end] and words[end] in _DIRECTION_AFTER:
                marker = "dir"
            mentions.append((target, marker))
        targets = list(dict.fromkeys(t for t, _m in mentions))
        decided: str | None = None
        if len(targets) == 1:
            if any(m != "dir" for _t, m in mentions):
                decided = targets[0]
        elif len(targets) > 1:
            inside = list(dict.fromkeys(t for t, m in mentions if m == "in"))
            if len(inside) == 1:
                decided = inside[0]
        if decided is None:
            return ModuleScan(None, None, tuple(mentions))
        if decided.startswith(_UNSEEDED):
            return ModuleScan(None, decided[len(_UNSEEDED):], tuple(mentions))
        return ModuleScan(decided, None, tuple(mentions))

    def _resolve_hint(self, hint: str | None, *, scan: bool = True) -> tuple[str | None, ModuleScan]:
        """(location, scan) for an omni ``location_hint``: exact, relaxed, then (``scan``) the mention scan
        (AC-PER-39). A hint naming a private place is not scanned (it keeps the AC-PER-23 result and stays private)."""
        found = self.resolve(hint)
        if found is not None or hint is None or not scan or self.is_private_hint(hint):
            return found, ModuleScan()
        result = self.scan_modules(hint)
        return result.module, result

    def resolve_hint(self, hint: str | None) -> str | None:
        """AC-PER-39 location for an omni ``location_hint`` (``resolve`` plus the mention scan)."""
        return self._resolve_hint(hint)[0]

    def is_roaming(self, camera_location: str | None) -> bool:
        """A capturable camera configured outside any module (a segment, e.g. cam-handheld -> USOS)."""
        return (
            self.exists(camera_location)
            and self.module_of(camera_location) is None
            and not self.is_no_capture(camera_location)
        )

    def _add(self, alias: str, location_id: str) -> None:
        key = normalise_hint(alias)
        if key:
            self._aliases.setdefault(key, location_id)

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    def resolve(self, hint: str | None) -> str | None:
        if hint is None:
            return None
        key = normalise_hint(hint)
        if not key:
            return None
        if key in self._aliases:
            return self._aliases[key]
        if key.startswith("the ") and key[4:] in self._aliases:
            return self._aliases[key[4:]]
        return self._resolve_relaxed(key)

    @staticmethod
    def _relaxed_keys(key: str) -> list[str]:
        relaxed = _strip_filler(key)
        if not relaxed:
            return []  # only filler ("laboratory module", "the ISS"): never guess
        with_lab = _strip_filler(" ".join("lab" if w == "laboratory" else w for w in key.split()))
        return [relaxed, with_lab] if with_lab != relaxed else [relaxed]

    def _resolve_relaxed(self, key: str) -> str | None:
        for k in self._relaxed_keys(key):
            if k in self._relaxed:
                return self._relaxed[k]
        return None

    def names_private_place(self, text: str | None) -> bool:
        """True when ``text`` contains a private-place term (a no_capture location id or core name, or a curated
        term such as "crew quarters" / "toilet"), as whole words."""
        key = normalise_hint(text) if text else ""
        if not key:
            return False
        padded = f" {key} "
        return any(f" {term} " in padded for term in self._private_terms)

    def is_private_hint(self, hint: str | None) -> bool:
        """True when ``hint`` resolves to a no_capture location or names a private place (AC-PER-23)."""
        if hint is None:
            return False
        key = normalise_hint(hint)
        if not key:
            return False
        if self.is_no_capture(self.resolve(hint)):
            return True
        # an ambiguous relaxed key resolves to nothing, but if any claimant is private the hint is private
        for k in self._relaxed_keys(key):
            if any(self.is_no_capture(lid) for lid in self._relaxed_ids.get(k, ())):
                return True
        return self.names_private_place(hint)

    def exists(self, location_id: str | None) -> bool:
        return location_id is not None and location_id in self._by_id

    def _ancestors(self, location_id: str | None):
        seen: set[str] = set()
        current = location_id
        while current is not None and current in self._by_id and current not in seen:
            seen.add(current)
            yield self._by_id[current]
            current = self._by_id[current].get("parent_id")

    def is_no_capture(self, location_id: str | None) -> bool:
        return any(bool(loc.get("no_capture")) for loc in self._ancestors(location_id))

    def module_of(self, location_id: str | None) -> str | None:
        for loc in self._ancestors(location_id):
            if loc.get("kind") == "module":
                return loc["location_id"]
        return None

    def same_module(self, a: str | None, b: str | None) -> bool:
        """True when both locations are known and lie in the same module (or are the same location)."""
        if a is None or b is None:
            return False
        if a == b:
            return True
        ma, mb = self.module_of(a), self.module_of(b)
        return ma is not None and ma == mb

    def within(self, location: str | None, container: str | None) -> bool:
        """``location`` is ``container`` or lies inside it (both must be known)."""
        if location is None or container is None:
            return False
        return any(loc["location_id"] == container for loc in self._ancestors(location))

    def window_location(self, camera_location: str | None, hint: str | None) -> str | None:
        """Camera default location, overridden by ``hint`` when it resolves to a known location id."""
        return self.resolve(hint) or camera_location

    def window_policy(
        self, camera_location: str | None, hint: str | None, summary: str | None = None
    ) -> WindowScope:
        """Window location, privacy and hint conflict for a camera, an untrusted model ``hint`` and, for a roaming
        camera, the model's ``summary`` (AC-PER-11/23/39/40).

        Privacy fails closed and a hint can only make a window more private:
        * a camera without a known location is private (a configuration mismatch must not leak images);
        * a private camera stays private, and a hint may only refine the location inside the camera's own zone;
        * otherwise the window is private when the camera, the resolved hint or the hint text is private
          (``is_private_hint``), or the location is private, whatever the location precedence below decides.
        Location precedence for a capturable camera: a resolvable hint (``resolve_hint``) sets the location, except
        a hint naming an ancestor of the camera location (never coarsen). A camera located outside any module (a
        segment, e.g. the handheld camera in USOS) is roaming: when the hint gives no module, the summary's mention
        scan may (inside a segment the hint named, if any); for a fixed camera (module or zone) a hint in another
        module is accepted but flagged ``hint_conflict``, and the summary is not used.
        """
        roaming = self.is_roaming(camera_location)
        hinted, hint_scan = self._resolve_hint(hint, scan=roaming)  # review: fixed cameras do not scan hints
        hint_elsewhere = hint_scan.elsewhere is not None
        mentions = {t for t, _m in hint_scan.mentions}
        private_hint = self.is_private_hint(hint)
        if not self.exists(camera_location):
            return WindowScope(hinted, True, source="hint" if hinted else "camera", elsewhere=hint_elsewhere)
        if self.is_no_capture(camera_location):
            inside = hinted is not None and self.within(hinted, camera_location)
            return WindowScope(hinted if inside else camera_location, True, source="hint" if inside else "camera")
        location, conflict, source = camera_location, False, "camera"
        if hinted is not None and not self.within(camera_location, hinted):
            location, source = hinted, "hint"
            camera_module = self.module_of(camera_location)
            conflict = camera_module is not None and self.module_of(hinted) != camera_module
        elsewhere = hint_elsewhere
        if roaming and self.module_of(location) is None and not hint_elsewhere:
            summary_scan = self.scan_modules(summary)  # AC-PER-40
            mentions |= {t for t, _m in summary_scan.mentions}
            if summary_scan.module is not None and (hinted is None or self.within(summary_scan.module, hinted)):
                location, source = summary_scan.module, "summary"
            elif summary_scan.module is None and summary_scan.elsewhere is not None:
                elsewhere = True
        no_capture = private_hint or self.is_no_capture(location) or self.is_no_capture(hinted)
        return WindowScope(location, no_capture, conflict, source, elsewhere, frozenset(mentions))

    def window_scope(self, camera_location: str | None, hint: str | None) -> tuple[str | None, bool]:
        """(window location, no_capture): ``window_policy`` without the conflict flag."""
        scope = self.window_policy(camera_location, hint)
        return scope.location, scope.no_capture


def caption_scene_change(resolver: LocationResolver, hint: Any) -> str | None:
    """AC-PER-61 (UI5-04): "caption" when the window has a location hint (signage the model read) that resolves to no
    location, e.g. "NOD 3" read from a "To NOD 3" sign in Node 1, which is evidence the camera left the anchored
    module; else None. A hint that resolves (a module, or a segment) is handled by the window policy itself."""
    if not isinstance(hint, str) or not hint.strip() or resolver.resolve_hint(hint) is not None:
        return None
    words = normalise_hint(hint).split()
    if not words or all(w in _PLACEHOLDER_HINT_WORDS for w in words):
        return None  # review round: "unknown", "N/A", "ISS", "station", "Node": no signage, no evidence of a move
    return "caption"


#: AC-PER-61 review round: hint words that name no place (placeholders, the whole station, a bare module word)
_PLACEHOLDER_HINT_WORDS = frozenset(
    ["unknown", "null", "none", "n", "a", "na", "nil", "not", "visible", "unclear", "unspecified", "iss", "the",
     "international", "space", "station", "interior", "inside", "of", "node", "module", "lab", "laboratory"]
)


@dataclass(frozen=True)
class CarryDecision:
    """AC-PER-41 outcome for one window: ``location`` to use, ``carried`` when inherited from the anchor
    (``carried_from`` = the anchor window's end, ``gap_s`` = seconds from it to this window's start)."""

    location: str | None
    carried: bool
    source: str
    carried_from: datetime | None = None
    gap_s: float | None = None
    ended: str | None = None  # AC-PER-61: the anchor was cleared by a scene change ("caption")


class ModuleCarry:
    """AC-PER-41 module carry for one roaming camera.

    The anchor is the last window that resolved a module itself (hint or summary) and is not no_capture. A capturable
    window without a module inherits the anchor's module when 0 <= its start - the anchor's end <= ``carry_s``
    (overlap counts as 0; a window starting before the anchor never inherits), its hint or summary mentions no other
    module (``WindowScope.mentions``), the anchored module still exists and is capturable (catalog refresh) and,
    when its hint named a segment, the module lies in that segment. Carried windows do not move the anchor. A
    no_capture window, or one placed in a module the seed lacks (``WindowScope.elsewhere``), clears it. ``carry_s``
    <= 0 or not finite turns the carry off.
    """

    def __init__(self, resolver: LocationResolver, carry_s: float = 45.0):
        self.resolver = resolver
        self.carry_s = carry_s
        self._anchor: tuple[str, datetime, datetime] | None = None  # (module, t_start, t_end)

    def reset(self) -> None:
        self._anchor = None

    def step(
        self,
        t_start: datetime,
        t_end: datetime,
        scope: WindowScope,
        *,
        no_capture: bool,
        roaming: bool,
        scene_change: str | None = None,
    ) -> CarryDecision:
        """``scene_change`` (AC-PER-61): evidence that the camera left the anchored module; a window without a module
        of its own then clears the anchor instead of inheriting it."""
        own = CarryDecision(scope.location, False, scope.source)
        if not roaming:
            return own
        if no_capture or scope.elsewhere:
            self._anchor = None
            return own
        module = self.resolver.module_of(scope.location)
        if module is not None:
            self._anchor = (module, t_start, t_end)
            return own
        if self._anchor is None or not (self.carry_s and 0 < float(self.carry_s) < math.inf):
            return own
        if scene_change:
            self._anchor = None
            return CarryDecision(scope.location, False, scope.source, ended=scene_change)
        anchor_module, anchor_start, anchor_end = self._anchor
        if not self.resolver.exists(anchor_module) or self.resolver.is_no_capture(anchor_module):
            self._anchor = None  # review: a catalog refresh removed the module or made it private
            return own
        if t_start < anchor_start:
            return own
        if scope.mentions - {anchor_module}:
            return own  # review: the window mentions another module (or one the seed lacks): contrary evidence
        gap = max((t_start - anchor_end).total_seconds(), 0.0)
        if gap > float(self.carry_s):
            return own
        if scope.source == "hint" and not self.resolver.within(anchor_module, scope.location):
            return own  # the hint named a segment the anchored module is not in
        return CarryDecision(anchor_module, True, "carried", anchor_end, round(gap, 3))
