"""Ground truth for real footage from captions (AC-EVAL-10, docs/spec/50-eval.md).

NASA's public-domain "Step Inside the International Space Station" tour names each module in a short title cue as
the camera enters it ("Columbus Laboratory Module" at 00:00:13). ``parse_srt`` turns the SRT into cues and
``module_timeline`` turns the title cues into module segments keyed by the seed location codes
(``contracts/db/002_seed.sql``).

The SRT is downloaded, untrusted data: it is only parsed as text (no eval, no markup rendering, no paths taken from
it). A cue is a module **title** only when its whole text is a module name plus filler words, so descriptions that
mention a module inside a sentence ("Known as Node 2, Harmony connects ...") never start a segment.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from nasa_eval import CONTRACTS_DB

SEED_SQL = CONTRACTS_DB / "002_seed.sql"

_TIME = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
_TIMING = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_SEED_ROW = re.compile(r"\(\s*'([^']+)'\s*,\s*(?:NULL|'[^']*')\s*,\s*'([^']+)'\s*,\s*'([^']*)'")

# Words of the seed display names that do not identify one module.
_GENERIC = frozenset({"node", "module", "service", "us", "lab", "laboratory", "jem", "pressurized", "logistics", "the"})
# Ambiguous in the seed (JPM1 and JLP1 are both "Kibo JEM ... Module"): the tour's Kibo is the pressurized module.
PINNED_ALIASES: dict[str, str] = {"kibo": "JPM1"}
# ISS modules the seed has no location for: their title cue starts an unmapped (not scored) segment, so the previous
# module does not wrongly extend over them.
UNMAPPED_MODULES: tuple[tuple[str, ...], ...] = (
    ("pressurized", "mating", "adapters"),
    ("pressurized", "mating", "adapter"),
    ("pma",),
    ("quest",),
    ("poisk",),
    ("rassvet",),
    ("pirs",),
    ("prichal",),
    ("beam",),
    ("bishop",),
)
# Words a title cue may carry besides the module name ("Nauka Multipurpose Laboratory Module", "Quest Airlock").
_FILLER = frozenset(
    {
        "module", "modules", "laboratory", "lab", "node", "service", "multipurpose", "research", "experiment",
        "japanese", "european", "the", "airlock", "mini", "pressurized", "logistics", "jem", "us", "u", "s",
    }
)


class Cue(NamedTuple):
    t_start: float
    t_end: float
    text: str


@dataclass(frozen=True)
class Segment:
    t_start: float
    t_end: float
    code: str | None  # seed module code; None = a module the seed lacks (not scored)
    name: str  # the title cue text


@dataclass(frozen=True)
class ModuleTimeline:
    segments: tuple[Segment, ...]

    def module_at(self, t: float) -> Segment | None:
        """The segment covering video time ``t`` (start inclusive, end exclusive); None before the first title cue
        or after the end of the video."""
        for seg in self.segments:
            if seg.t_start <= t < seg.t_end:
                return seg
        return None

    def codes(self) -> list[str]:
        """Distinct mapped module codes in order of first appearance."""
        out: list[str] = []
        for seg in self.segments:
            if seg.code is not None and seg.code not in out:
                out.append(seg.code)
        return out


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return round(int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000, 3)


def parse_srt(text: str) -> list[Cue]:
    """SRT text -> cues sorted by start. Tolerates a BOM, CRLF and a missing index line; blocks without a valid
    timing line are skipped. Multi-line cue text is joined with single spaces."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        for i, line in enumerate(lines[:2]):
            m = _TIMING.match(line)
            if m:
                start, end = _seconds(*m.groups()[:4]), _seconds(*m.groups()[4:])
                body = " ".join(" ".join(lines[i + 1 :]).split())
                if end >= start:
                    cues.append(Cue(start, end, body))
                break
    return sorted(cues, key=lambda c: (c.t_start, c.t_end))


def load_seed_modules(seed_sql: str | Path = SEED_SQL) -> dict[str, str]:
    """Module location code -> display name from the seed's ``INSERT INTO location`` block (kind = 'module')."""
    sql = Path(seed_sql).read_text()
    block = sql.split("INSERT INTO location", 1)[1].split("ON CONFLICT", 1)[0]
    return {code: name for code, kind, name in _SEED_ROW.findall(block) if kind == "module"}


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("’", "'").replace("'s", " ")
    return re.findall(r"[a-z]+|\d+", text)


def module_aliases(modules: Mapping[str, str]) -> dict[str, str]:
    """Single-word aliases (lower case) -> module code: the distinctive words of each seed display name. A word that
    names more than one module is dropped unless pinned in ``PINNED_ALIASES``."""
    owners: dict[str, set[str]] = {}
    for code, name in modules.items():
        for word in _tokens(name):
            if word not in _GENERIC and not word.isdigit():
                owners.setdefault(word, set()).add(code)
    aliases = {w: next(iter(c)) for w, c in owners.items() if len(c) == 1}
    aliases.update({w: c for w, c in PINNED_ALIASES.items() if c in modules})
    return aliases


def match_module_title(text: str, aliases: Mapping[str, str]) -> tuple[str | None, str] | None:
    """``(code, name)`` when ``text`` is a module title cue (code None for an ISS module the seed lacks), else None.
    The cue must name exactly one module and every other word must be a filler word or a number."""
    tokens = _tokens(text)
    if not tokens:
        return None
    found: set[str | None] = set()
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        for phrase in sorted(UNMAPPED_MODULES, key=len, reverse=True):
            if tuple(tokens[i : i + len(phrase)]) == phrase:
                found.add(None)
                i += len(phrase)
                break
        else:
            if tokens[i] in aliases:
                found.add(aliases[tokens[i]])
            else:
                rest.append(tokens[i])
            i += 1
    if len(found) != 1 or any(w not in _FILLER and not w.isdigit() for w in rest):
        return None
    return next(iter(found)), " ".join(text.split())


def module_timeline(
    cues: Iterable[Cue], modules: Mapping[str, str] | None = None, video_end_s: float | None = None
) -> ModuleTimeline:
    """Module segments from the title cues. A segment runs from its title cue to the next title cue of another
    module (a repeated title of the same module extends it) or to ``video_end_s`` (default: the last cue's end)."""
    cues = sorted(cues, key=lambda c: (c.t_start, c.t_end))
    aliases = module_aliases(modules if modules is not None else load_seed_modules())
    starts: list[tuple[float, str | None, str]] = []
    for cue in cues:
        hit = match_module_title(cue.text, aliases)
        if hit is None:
            continue
        code, name = hit
        if starts and (starts[-1][1], starts[-1][1] or starts[-1][2]) == (code, code or name):
            continue  # same module titled again
        starts.append((cue.t_start, code, name))
    end = video_end_s if video_end_s is not None else max((c.t_end for c in cues), default=0.0)
    segments = []
    for i, (t, code, name) in enumerate(starts):
        t_next = starts[i + 1][0] if i + 1 < len(starts) else end
        if t_next > t:
            segments.append(Segment(t, t_next, code, name))
    return ModuleTimeline(tuple(segments))
