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

import re
from collections.abc import Sequence
from dataclasses import dataclass
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

# Common names used by crews and in procedures (research/04 §B), keyed by seed location id.
ISS_ALIASES: dict[str, tuple[str, ...]] = {
    "NOD1": ("unity", "node 1"),
    "NOD2": ("harmony", "node 2"),
    "NOD3": ("tranquility", "tranquillity", "node 3"),
    "LAB1": ("destiny", "us lab", "u s lab", "the lab", "lab", "destiny lab"),
    "COL1": ("columbus", "columbus module"),
    "JPM1": ("kibo", "jem", "jpm", "kibo pm", "jem pm"),
    "JLP1": ("jlp", "elm ps", "kibo logistics module"),
    "PMM1": ("leonardo", "pmm"),
    "CUP1": ("cupola",),
    "ZVEZDA": ("zvezda", "service module"),
    "ZARYA": ("zarya", "fgb"),
    "NAUKA": ("nauka", "mlm"),
    "USOS": ("us segment", "us orbital segment"),
    "ROS": ("russian segment",),
}


# AC-PER-23 words ignored by the relaxed match (never location names on their own).
FILLER_WORDS = frozenset(
    {"the", "a", "an", "module", "laboratory", "iss", "international", "space", "station", "of", "in", "inside",
     "aboard", "on", "board", "onboard"}
)
# Curated private-place terms (crew quarters, hygiene). Any hint containing one makes the window private.
PRIVATE_TERMS: tuple[str, ...] = (
    "crew quarters", "crew quarter", "sleep station", "sleeping quarters", "toilet", "whc", "waste and hygiene",
    "hygiene compartment", "bathroom", "lavatory", "restroom",
)


def _strip_filler(key: str) -> str:
    return " ".join(w for w in key.split() if w not in FILLER_WORDS)


@dataclass(frozen=True)
class WindowScope:
    """``location``: the window location; ``no_capture``: the window is private; ``hint_conflict``: a fixed
    camera's hint named a location in another module (AC-PER-23: sightings get low confidence)."""

    location: str | None
    no_capture: bool
    hint_conflict: bool = False


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
            if not relaxed:
                continue
            self._relaxed_ids.setdefault(relaxed, set()).add(lid)
            if relaxed in self._relaxed and self._relaxed[relaxed] != lid:
                self._relaxed[relaxed] = None
            else:
                self._relaxed[relaxed] = lid
        self._private_terms = self._build_private_terms()

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

    def window_policy(self, camera_location: str | None, hint: str | None) -> WindowScope:
        """Window location, privacy and hint conflict for a camera and an untrusted model ``hint`` (AC-PER-11/23).

        Privacy fails closed and a hint can only make a window more private:
        * a camera without a known location is private (a configuration mismatch must not leak images);
        * a private camera stays private, and a hint may only refine the location inside the camera's own zone;
        * otherwise the window is private when the camera, the resolved hint or the hint text is private
          (``is_private_hint``), whatever the location precedence below decides.
        Location precedence for a capturable camera: a resolvable hint sets the location, except a hint naming an
        ancestor of the camera location (never coarsen). A camera located outside any module (a segment, e.g. the
        handheld camera in USOS) is roaming; for a fixed camera (module or zone) a hint in another module is
        accepted but flagged ``hint_conflict``.
        """
        hinted = self.resolve(hint)
        private_hint = self.is_private_hint(hint)
        if not self.exists(camera_location):
            return WindowScope(hinted, True)
        if self.is_no_capture(camera_location):
            inside = hinted is not None and self.within(hinted, camera_location)
            return WindowScope(hinted if inside else camera_location, True)
        location, conflict = camera_location, False
        if hinted is not None and not self.within(camera_location, hinted):
            location = hinted
            camera_module = self.module_of(camera_location)
            conflict = camera_module is not None and self.module_of(hinted) != camera_module
        no_capture = private_hint or self.is_no_capture(location) or self.is_no_capture(hinted)
        return WindowScope(location, no_capture, conflict)

    def window_scope(self, camera_location: str | None, hint: str | None) -> tuple[str | None, bool]:
        """(window location, no_capture): ``window_policy`` without the conflict flag."""
        scope = self.window_policy(camera_location, hint)
        return scope.location, scope.no_capture
