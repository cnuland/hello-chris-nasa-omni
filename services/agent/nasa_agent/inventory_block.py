"""SQL-derived, budgeted Inventory Working Set block rendered for every chat turn (AC-AGT-10).

The block is current database state (never a summary) and has three sections, in priority order:
1. open review items (unknowns, possible duplicates, ...);
2. items moved within ``recent_hours`` (with the actor description);
3. live items in the module(s) named in the question (by id such as ``NOD2`` or display name such as ``Node 2``).
Merged and dismissed entities are never listed. Lines are added in that order until the token budget is reached.
The renderer only runs SELECTs, in a read-only transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from nasa_agent.context import estimate_tokens

HEADER = "INVENTORY WORKING SET (live database state; cite entity ids):"
EMPTY_LINE = "(none)"
SECTION_LIMIT = 200
_GENERIC_WORDS = {"node", "module", "the", "us", "lab"}

OPEN_REVIEWS_SQL = """
SELECT r.review_id, r.kind, r.description, r.observed_at, r.location_id,
       e.entity_id, e.label, e.status, e.last_location
FROM review_item r
LEFT JOIN entity e ON e.entity_id = r.entity_id
WHERE r.status = 'open' AND (e.entity_id IS NULL OR e.status NOT IN ('merged', 'dismissed'))
ORDER BY r.observed_at DESC, r.review_id
LIMIT %s
"""

RECENT_MOVES_SQL = """
SELECT ev.event_id, ev.actor_desc, ev.from_location, ev.to_location, ev.occurred_at,
       e.entity_id, e.label, e.status, e.last_location
FROM event ev
JOIN entity e ON e.entity_id = ev.entity_id
WHERE ev.event_type = 'moved' AND ev.occurred_at > %s AND ev.occurred_at <= %s
  AND e.status NOT IN ('merged', 'dismissed')
ORDER BY ev.occurred_at DESC, ev.event_id
LIMIT %s
"""

MODULE_ITEMS_SQL = """
WITH RECURSIVE sub(location_id) AS (
    SELECT location_id FROM location WHERE location_id = ANY(%s)
    UNION
    SELECT l.location_id FROM location l JOIN sub ON l.parent_id = sub.location_id
)
SELECT e.entity_id, e.label, e.status, e.kind, e.last_location, e.last_seen_at, e.confidence, e.needs_review
FROM entity e
WHERE e.last_location IN (SELECT location_id FROM sub) AND e.status NOT IN ('merged', 'dismissed')
ORDER BY e.last_seen_at DESC NULLS LAST, e.entity_id
LIMIT %s
"""


@dataclass(frozen=True)
class _Location:
    location_id: str
    parent_id: str | None
    kind: str
    display_name: str
    no_capture: bool = False  # a private zone (crew quarters, WHC): AC-AGT-38


def _ts(value: datetime | None) -> str:
    if value is None:
        return "unknown time"
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def module_aliases(loc: _Location) -> set[str]:
    """Ways a question can name a module: id, display name, name without parenthetical, 'Node 2', 'Destiny'."""
    name = loc.display_name.strip()
    aliases = {loc.location_id, name}
    base = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    aliases.add(base)
    for inner in re.findall(r"\((.*?)\)", name):
        if inner.strip():
            aliases.add(inner.strip())
    words = base.split()
    if len(words) >= 2 and words[1].isdigit():
        aliases.add(f"{words[0]} {words[1]}")  # "Node 2 Harmony" -> "Node 2"
        aliases.update(words[2:3])  # -> "Harmony"
    elif words and words[0].lower() not in _GENERIC_WORDS and len(words[0]) >= 4:
        aliases.add(words[0])  # "Destiny (US Lab)" -> "Destiny"
    return {a for a in aliases if a}


class InventoryBlockRenderer:
    """Renders the per-turn inventory block from Postgres (read-only)."""

    def __init__(self, database_url: str, *, recent_hours: float = 24.0):
        self.database_url = database_url
        self.recent_hours = recent_hours

    # ------------------------------------------------------------ module resolution
    @staticmethod
    def _modules_in_question(question: str, locations: list[_Location]) -> list[str]:
        by_id = {loc.location_id: loc for loc in locations}

        def module_of(loc: _Location | None) -> str | None:
            seen = set()
            while loc is not None and loc.location_id not in seen:
                if loc.kind == "module":
                    return loc.location_id
                seen.add(loc.location_id)
                loc = by_id.get(loc.parent_id) if loc.parent_id else None
            return None

        found: set[str] = set()
        for loc in locations:
            if loc.kind == "module":
                names = module_aliases(loc)
            elif loc.kind in ("zone", "container"):
                names = {loc.location_id}
            else:
                continue
            for alias in names:
                if re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", question, flags=re.IGNORECASE):
                    module = module_of(loc)
                    if module:
                        found.add(module)
                    break
        return sorted(found)

    # ------------------------------------------------------------ render
    def render(self, question: str, token_budget: int, now: datetime | None = None) -> str:
        """Open reviews, recently moved items and items in the asked-about module(s), within ``token_budget``."""
        now = now or datetime.now(UTC)
        since = now - timedelta(hours=self.recent_hours)
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.transaction():
                locations = [
                    _Location(**r)
                    for r in conn.execute(
                        "SELECT location_id, parent_id, kind, display_name FROM location ORDER BY location_id"
                    )
                ]
                reviews = conn.execute(OPEN_REVIEWS_SQL, (SECTION_LIMIT,)).fetchall()
                moves = conn.execute(RECENT_MOVES_SQL, (since, now, SECTION_LIMIT)).fetchall()
                modules = self._modules_in_question(question or "", locations)
                module_items = conn.execute(MODULE_ITEMS_SQL, (modules, SECTION_LIMIT)).fetchall() if modules else []

        sections: list[tuple[str, list[str]]] = [
            ("Open reviews:", [self._review_line(r) for r in reviews]),
            (f"Moved in the last {self.recent_hours:g} h:", [self._move_line(m) for m in moves]),
        ]
        if modules:
            sections.append((f"Items in {', '.join(modules)}:", [self._item_line(i) for i in module_items]))
        return self._fit(sections, token_budget)

    @staticmethod
    def _fit(sections: list[tuple[str, list[str]]], token_budget: int) -> str:
        lines = [HEADER]

        def fits(extra: list[str]) -> bool:
            return estimate_tokens("\n".join(lines + extra)) <= token_budget

        if not fits([]):
            return ""
        for title, rows in sections:
            if not fits([title, rows[0] if rows else EMPTY_LINE]):
                break
            lines.append(title)
            if not rows:
                lines.append(EMPTY_LINE)
                continue
            omitted = 0
            for row in rows:
                if omitted == 0 and fits([row]):
                    lines.append(row)
                else:
                    omitted += 1
            if omitted and fits([f"  ... {omitted} more"]):
                lines.append(f"  ... {omitted} more")
        return "\n".join(lines)

    @staticmethod
    def _review_line(r: dict) -> str:
        where = r.get("last_location") or r.get("location_id") or "unknown location"
        what = f"{r['label']} [{r['entity_id']}] status={r['status']}" if r.get("entity_id") else "(no entity)"
        return (
            f"- review {r['review_id']} {r['kind']}: {what} at {where}; "
            f'"{r["description"]}" observed {_ts(r["observed_at"])}'
        )

    @staticmethod
    def _move_line(m: dict) -> str:
        actor = m.get("actor_desc") or "unknown actor"
        return (
            f"- {m['label']} [{m['entity_id']}] moved {m.get('from_location') or '?'} -> "
            f"{m.get('to_location') or '?'} by {actor} at {_ts(m['occurred_at'])}; "
            f"now at {m.get('last_location') or '?'}"
        )

    @staticmethod
    def _item_line(i: dict) -> str:
        review = " needs_review" if i.get("needs_review") else ""
        return (
            f"- {i['label']} [{i['entity_id']}] {i['kind']} status={i['status']} at {i['last_location']}, "
            f"last seen {_ts(i['last_seen_at'])}, conf {i['confidence']:.2f}{review}"
        )
