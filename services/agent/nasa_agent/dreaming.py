"""Dreaming (AC-AGT-6): inventory-first per-entity / per-location / global summaries into agent_memory.

Summaries are regenerated (never hand-edited) from the last N hours of observation windows and events plus the
current inventory. Each summary starts with inventory facts (current location, status, unresolved reviews); recent
events and then chatter (scene descriptions, transcripts) follow and are truncated to ``token_budget`` tokens. A run
is deterministic for a given (database state, ``now``, ``hours``), writes only ``agent_memory`` and upserts, so it
is idempotent. Run as ``python -m nasa_agent.dreaming --once`` (CronJob) or via ``POST /memory/dream``.

Integrity rules: every entity / module that already has a row is regenerated on each run (so status and unresolved
reviews are always current even without new activity); rows of merged, dismissed or deleted entities and of
non-module location keys are deleted; upserts are guarded by ``source_to`` so an older run that finishes late never
overwrites a newer one. Speech transcripts never enter the global summary (it is sent with every chat turn); chatter
in entity/location summaries is JSON-quoted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from nasa_agent.context import estimate_tokens, truncate_to_tokens
from nasa_agent.logs import configure_logging

log = logging.getLogger("nasa_agent.dreaming")

PRIORITY_GLOBAL = 1.0
PRIORITY_LOCATION = 0.7
PRIORITY_ENTITY_REVIEW = 0.9
PRIORITY_ENTITY_UNKNOWN = 0.8
PRIORITY_ENTITY = 0.6
PRIORITY_CHATTER_ONLY = 0.3
MAX_LIST = 50

ENTITIES_SQL = """
SELECT e.entity_id::text AS entity_id, e.label, e.kind, e.status, e.catalog_id, e.last_location,
       e.last_seen_at, e.confidence, e.needs_review, e.review_reason, c.display_name AS catalog_name
FROM entity e LEFT JOIN catalog_item c ON c.catalog_id = e.catalog_id
WHERE e.status NOT IN ('merged', 'dismissed') AND (
       EXISTS (SELECT 1 FROM agent_memory m WHERE m.scope = 'entity' AND m.scope_key = e.entity_id::text)
    OR (e.last_seen_at > %(since)s AND e.last_seen_at <= %(now)s)
    OR EXISTS (SELECT 1 FROM sighting s WHERE s.entity_id = e.entity_id
               AND s.seen_at > %(since)s AND s.seen_at <= %(now)s)
    OR EXISTS (SELECT 1 FROM event ev WHERE ev.entity_id = e.entity_id
               AND ev.occurred_at > %(since)s AND ev.occurred_at <= %(now)s)
    OR EXISTS (SELECT 1 FROM review_item r WHERE r.entity_id = e.entity_id AND r.status = 'open'))
ORDER BY e.entity_id
"""

LIVE_ENTITIES_SQL = """
SELECT e.entity_id::text AS entity_id, e.label, e.kind, e.status, e.last_location, e.last_seen_at, e.needs_review
FROM entity e WHERE e.status NOT IN ('merged', 'dismissed')
ORDER BY e.last_seen_at DESC NULLS LAST, e.entity_id
"""

OPEN_REVIEWS_SQL = """
SELECT r.review_id::text AS review_id, r.kind, r.description, r.observed_at, r.location_id,
       r.entity_id::text AS entity_id, e.label, e.last_location
FROM review_item r LEFT JOIN entity e ON e.entity_id = r.entity_id
WHERE r.status = 'open' AND (e.entity_id IS NULL OR e.status <> 'merged')
ORDER BY r.observed_at DESC, r.review_id
"""

EVENTS_SQL = """
SELECT ev.event_id::text AS event_id, ev.event_type, ev.entity_id::text AS entity_id, ev.actor_desc,
       ev.from_location, ev.to_location, ev.occurred_at, e.label
FROM event ev JOIN entity e ON e.entity_id = ev.entity_id
WHERE ev.occurred_at > %(since)s AND ev.occurred_at <= %(now)s AND e.status <> 'merged'
ORDER BY ev.occurred_at DESC, ev.event_id
"""

WINDOWS_SQL = """
SELECT w.window_id::text AS window_id, w.camera_id, c.location_id AS camera_location, w.t_start, w.scene_summary,
       w.transcript
FROM observation_window w JOIN camera c ON c.camera_id = w.camera_id
WHERE w.t_start > %(since)s AND w.t_start <= %(now)s
ORDER BY w.t_start DESC, w.window_id
"""

#: Guarded upsert: an older run (smaller source_to) never overwrites a newer row.
DREAM_UPSERT_SQL = """
INSERT INTO agent_memory (scope, scope_key, summary, priority, source_from, source_to, generated_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (scope, scope_key) DO UPDATE SET
    summary = EXCLUDED.summary, priority = EXCLUDED.priority, source_from = EXCLUDED.source_from,
    source_to = EXCLUDED.source_to, generated_at = EXCLUDED.generated_at
WHERE agent_memory.source_to IS NULL OR agent_memory.source_to <= EXCLUDED.source_to
"""

DELETE_DEAD_ENTITY_ROWS_SQL = """
DELETE FROM agent_memory m
WHERE m.scope = 'entity' AND NOT EXISTS (
    SELECT 1 FROM entity e WHERE e.entity_id::text = m.scope_key AND e.status NOT IN ('merged', 'dismissed'))
"""

DELETE_NON_MODULE_LOCATION_ROWS_SQL = """
DELETE FROM agent_memory m
WHERE m.scope = 'location' AND NOT EXISTS (
    SELECT 1 FROM location l WHERE l.location_id = m.scope_key AND l.kind = 'module')
"""

EXISTING_LOCATION_KEYS_SQL = "SELECT scope_key FROM agent_memory WHERE scope = 'location'"

SIGHTINGS_SQL = """
SELECT s.entity_id::text AS entity_id, s.window_id::text AS window_id, s.location_id
FROM sighting s
WHERE s.window_id IS NOT NULL AND s.seen_at > %(since)s AND s.seen_at <= %(now)s
ORDER BY s.seen_at DESC, s.sighting_id
"""


def _ts(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else "unknown time"


def compose(facts: list[str], details: list[str], token_budget: int) -> str:
    """Facts first (always kept, hard-cut only if they alone exceed the budget), then details while they fit; the
    first detail that does not fit is truncated and the rest dropped."""
    text = " ".join(facts)
    if estimate_tokens(text) >= token_budget:
        return truncate_to_tokens(text, token_budget)
    for detail in details:
        candidate = f"{text} {detail}" if text else detail
        if estimate_tokens(candidate) <= token_budget:
            text = candidate
            continue
        room = token_budget - estimate_tokens(text + " ")
        if room > 3:
            text = f"{text} {truncate_to_tokens(detail, room)}"
        break
    return truncate_to_tokens(text, token_budget)


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _chatter(window: dict[str, Any], *, transcripts: bool = True) -> str:
    """Untrusted model/speech text, JSON-quoted so it reads as a quotation, not as instructions."""
    parts = []
    if window.get("scene_summary"):
        parts.append(f"seen: {_quote(window['scene_summary'])}")
    if transcripts and window.get("transcript"):
        parts.append(f"heard: {_quote(window['transcript'])}")
    return f"[{_ts(window['t_start'])} {window['camera_id']}] " + "; ".join(parts) if parts else ""


def _move(ev: dict[str, Any], with_label: bool = False) -> str:
    who = ev.get("actor_desc") or "unknown actor"
    what = f"{ev['label']} [{ev['entity_id']}] " if with_label else ""
    if ev["event_type"] == "moved":
        return (
            f"{what}moved {ev.get('from_location') or '?'} -> {ev.get('to_location') or '?'} by {who} "
            f"at {_ts(ev['occurred_at'])}."
        )
    return f"{what}{ev['event_type']} by {who} at {_ts(ev['occurred_at'])}."


class Dreamer:
    """Builds and upserts the agent_memory summaries."""

    def __init__(self, database_url: str, *, token_budget: int = 400):
        if token_budget <= 0:
            raise ValueError("token_budget must be > 0")
        self.database_url = database_url
        self.token_budget = token_budget

    def run(self, hours: float = 6.0, now: datetime | None = None) -> dict[str, Any]:
        """Summarise the last ``hours`` before ``now``; upsert agent_memory. Returns
        {"entities": int, "locations": int, "global": int}."""
        if hours is None or hours <= 0:
            raise ValueError("hours must be > 0")
        now = now or datetime.now(UTC)
        since = now - timedelta(hours=hours)
        started = time.perf_counter()
        window = {"since": since, "now": now}

        with psycopg.connect(self.database_url, row_factory=dict_row) as conn, conn.transaction():
            locations = {
                r["location_id"]: r
                for r in conn.execute("SELECT location_id, parent_id, kind, display_name FROM location")
            }
            entities = conn.execute(ENTITIES_SQL, window).fetchall()
            live = conn.execute(LIVE_ENTITIES_SQL).fetchall()
            reviews = conn.execute(OPEN_REVIEWS_SQL).fetchall()
            events = conn.execute(EVENTS_SQL, window).fetchall()
            windows = conn.execute(WINDOWS_SQL, window).fetchall()
            sightings = conn.execute(SIGHTINGS_SQL, window).fetchall()

            existing_locations = {r["scope_key"] for r in conn.execute(EXISTING_LOCATION_KEYS_SQL)}
            summaries = self._build(
                locations, entities, live, reviews, events, windows, sightings, now, existing_locations
            )
            for scope, key, text, priority in summaries:
                conn.execute(DREAM_UPSERT_SQL, (scope, key, text, priority, since, now))
            removed = conn.execute(DELETE_DEAD_ENTITY_ROWS_SQL).rowcount
            removed += conn.execute(DELETE_NON_MODULE_LOCATION_ROWS_SQL).rowcount

        report = {
            "entities": sum(1 for s in summaries if s[0] == "entity"),
            "locations": sum(1 for s in summaries if s[0] == "location"),
            "global": sum(1 for s in summaries if s[0] == "global"),
            "removed": removed,
        }
        log.info(
            "dream run",
            extra={**report, "hours": hours, "latency_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return report

    # ------------------------------------------------------------ summaries
    def _build(self, locations, entities, live, reviews, events, windows, sightings, now, existing_locations=()):
        def module_of(location_id: str | None) -> str | None:
            seen = set()
            while location_id and location_id in locations and location_id not in seen:
                loc = locations[location_id]
                if loc["kind"] == "module":
                    return location_id
                seen.add(location_id)
                location_id = loc["parent_id"]
            return None

        def where(location_id: str | None) -> str:
            if not location_id:
                return "unknown location"
            name = locations.get(location_id, {}).get("display_name")
            return f"{location_id} ({name})" if name else location_id

        windows_by_id = {w["window_id"]: w for w in windows}
        reviews_by_entity: dict[str, list] = defaultdict(list)
        for r in reviews:
            if r["entity_id"]:
                reviews_by_entity[r["entity_id"]].append(r)
        events_by_entity: dict[str, list] = defaultdict(list)
        for ev in events:
            events_by_entity[ev["entity_id"]].append(ev)
        windows_by_entity: dict[str, list] = defaultdict(list)
        windows_by_module: dict[str, list] = defaultdict(list)
        for s in sightings:
            w = windows_by_id.get(s["window_id"])
            if w is None:
                continue
            if w not in windows_by_entity[s["entity_id"]]:
                windows_by_entity[s["entity_id"]].append(w)
            mod = module_of(s["location_id"])
            if mod and w not in windows_by_module[mod]:
                windows_by_module[mod].append(w)
        for w in windows:
            mod = module_of(w["camera_location"])
            if mod and w not in windows_by_module[mod]:
                windows_by_module[mod].append(w)

        out: list[tuple[str, str, str, float]] = []
        active_modules: set[str] = set()

        # ---- per entity
        for e in entities:
            eid = e["entity_id"]
            name = e["label"] + (f" ({e['catalog_name']})" if e.get("catalog_name") else "")
            head = (
                f"{name} [{eid}], {e['kind']}, status {e['status']}, at {where(e['last_location'])}, "
                f"last seen {_ts(e['last_seen_at'])}, confidence {e['confidence']:.2f}."
            )
            facts = [head]
            open_reviews = reviews_by_entity.get(eid, [])
            for r in open_reviews:
                facts.append(f"Unresolved review {r['review_id']} ({r['kind']}): {r['description']}.")
            if e["needs_review"] and not open_reviews:
                facts.append(f"Needs review: {e['review_reason'] or 'unspecified'}.")
            details = [_move(ev) for ev in events_by_entity.get(eid, [])]
            details += [
                c
                for c in (
                    _chatter(w)
                    for w in sorted(
                        windows_by_entity.get(eid, []), key=lambda w: (w["t_start"], w["window_id"]), reverse=True
                    )
                )
                if c
            ]
            if open_reviews:
                priority = PRIORITY_ENTITY_REVIEW
            elif e["status"] in ("unknown", "provisional") or e["needs_review"]:
                priority = PRIORITY_ENTITY_UNKNOWN
            else:
                priority = PRIORITY_ENTITY
            out.append(("entity", eid, compose(facts, details, self.token_budget), priority))
            if mod := module_of(e["last_location"]):
                active_modules.add(mod)
        for ev in events:
            for loc in (ev["from_location"], ev["to_location"]):
                if mod := module_of(loc):
                    active_modules.add(mod)
        active_modules.update(windows_by_module)
        active_modules.update(k for k in existing_locations if locations.get(k, {}).get("kind") == "module")

        # ---- per module
        live_by_module: dict[str, list] = defaultdict(list)
        for e in live:
            if mod := module_of(e["last_location"]):
                live_by_module[mod].append(e)
        for mod in sorted(active_modules):
            items = live_by_module.get(mod, [])
            mod_reviews = [r for r in reviews if module_of(r["last_location"] or r["location_id"]) == mod]
            facts = [f"Module {where(mod)}: {len(items)} live items."]
            if mod_reviews:
                facts.append(
                    "Unresolved reviews: "
                    + "; ".join(
                        f"{r['label'] or 'unlinked'} [{r['entity_id']}] {r['kind']} - {r['description']}"
                        for r in mod_reviews[:MAX_LIST]
                    )
                    + "."
                )
            details = []
            if items:
                details.append(
                    "Items: "
                    + "; ".join(
                        f"{i['label']} [{i['entity_id']}] {i['status']} at {i['last_location']}"
                        for i in items[:MAX_LIST]
                    )
                    + "."
                )
            mod_events = [ev for ev in events if mod in (module_of(ev["from_location"]), module_of(ev["to_location"]))]
            details += [_move(ev, with_label=True) for ev in mod_events]
            details += [
                c
                for c in (
                    _chatter(w)
                    for w in sorted(
                        windows_by_module.get(mod, []), key=lambda w: (w["t_start"], w["window_id"]), reverse=True
                    )
                )
                if c
            ]
            # Inventory listing is a fact: keep it ahead of events/chatter but let the budget cut its tail.
            out.append(("location", mod, compose(facts, details, self.token_budget), PRIORITY_LOCATION))

        # ---- global
        by_status: dict[str, int] = defaultdict(int)
        for e in live:
            by_status[e["status"]] += 1
        facts = [
            f"Inventory at {_ts(now)}: {len(live)} live entities ("
            + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
            + f"); {len(reviews)} open reviews."
        ]
        if reviews:
            facts.append(
                "Unresolved reviews: "
                + "; ".join(
                    f"{r['label'] or 'unlinked'} [{r['entity_id']}] {r['kind']} at "
                    f"{r['last_location'] or r['location_id'] or '?'} - {r['description']}"
                    for r in reviews[:MAX_LIST]
                )
                + "."
            )
        details = [_move(ev, with_label=True) for ev in events]
        details += [c for c in (_chatter(w, transcripts=False) for w in windows) if c]
        out.append(("global", "global", compose(facts, details, self.token_budget), PRIORITY_GLOBAL))
        return out


async def dream_forever(dreamer: Any, *, interval_s: float, hours: float, stop: asyncio.Event) -> None:
    """Periodic job: call ``dreamer.run(hours=hours)`` every ``interval_s`` until ``stop`` is set. A failed run is
    logged and retried at the next interval."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(dreamer.run, hours=hours)
        except Exception:  # the job must survive DB blips
            log.exception("dream run failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


def _env_number(name: str, default: float, cast=float):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = cast(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def main(argv: list[str] | None = None) -> int:
    """CLI for the CronJob: ``python -m nasa_agent.dreaming --once [--hours H]`` (DATABASE_URL from env)."""
    parser = argparse.ArgumentParser(prog="nasa-agent-dream", description="Build agent_memory summaries.")
    parser.add_argument("--once", action="store_true", help="run once and exit (CronJob mode)")
    parser.add_argument("--hours", type=float, default=None, help="look-back window (default DREAM_WINDOW_HOURS)")
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between runs without --once (default DREAM_INTERVAL_SECONDS)",
    )
    args = parser.parse_args(argv)
    configure_logging()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL is required")
    hours = args.hours if args.hours is not None else _env_number("DREAM_WINDOW_HOURS", 6.0)
    if hours <= 0:
        parser.error("--hours must be > 0")
    dreamer = Dreamer(database_url, token_budget=_env_number("DREAM_TOKEN_BUDGET", 400, int))

    if args.once:
        try:
            dreamer.run(hours=hours)
        except Exception:
            log.exception("dream run failed")
            return 1
        return 0

    interval = args.interval if args.interval is not None else _env_number("DREAM_INTERVAL_SECONDS", 900.0)

    async def _loop() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
        await dream_forever(dreamer, interval_s=interval, hours=hours, stop=stop)

    asyncio.run(_loop())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
