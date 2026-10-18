"""Test-only helpers: seed data (parsed from contracts/db/002_seed.sql), an in-memory InventoryClient
(FakeInventory) that validates every payload against the inventory-api request models, and scene builders.

These helpers deliberately contain behaviour: they are test infrastructure, not part of the perception package.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = REPO_ROOT / "contracts"
SEED_SQL = CONTRACTS / "db" / "002_seed.sql"
SCHEMA_PATH = CONTRACTS / "schemas" / "observation.schema.json"

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
BUCKET = "inventory-test"
DIM = 768

try:  # validate payloads against the real inventory-api request model when it is installed
    from inventory_api.schemas import IngestWindow as _IngestWindowModel
except ImportError:  # pragma: no cover - inventory-api not installed
    _IngestWindowModel = None


def ts(seconds: float = 0) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


# ------------------------------------------------------------------------------------------------ seed parsing
def _split_tuple(body: str) -> list[str]:
    out, cur, depth, quote = [], "", 0, False
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            cur += ch
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    cur += "'"
                    i += 1
                else:
                    quote = False
        elif ch == "'":
            quote = True
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return out


def _value(tok: str) -> Any:
    if tok == "NULL":
        return None
    if tok in ("true", "false"):
        return tok == "true"
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if tok.startswith("'") and tok.endswith("'"):
        inner = tok[1:-1].replace("''", "'")
        if inner.startswith("{") and inner.endswith("}"):
            items = re.findall(r'"([^"]*)"|([^,{}"]+)', inner[1:-1])
            return [a or b.strip() for a, b in items if (a or b.strip())]
        return inner
    return tok


def _parse_inserts(table: str) -> list[list[Any]]:
    sql = SEED_SQL.read_text()
    m = re.search(rf"INSERT INTO {table} \(([^)]*)\) VALUES(.*?)ON CONFLICT", sql, re.DOTALL)
    assert m, table
    cols = [c.strip() for c in m.group(1).split(",")]
    rows = []
    for line in m.group(2).splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("("):
            continue
        rows.append(dict(zip(cols, [_value(t) for t in _split_tuple(line[1:-1])], strict=True)))
    return rows


LOCATIONS: list[dict[str, Any]] = _parse_inserts("location")
CAMERAS: list[dict[str, Any]] = _parse_inserts("camera")
CATALOG: list[dict[str, Any]] = _parse_inserts("catalog_item")
CATALOG_BY_ID = {c["catalog_id"]: c for c in CATALOG}
LOCATION_IDS = {loc["location_id"] for loc in LOCATIONS}
CAMERA_LOCATIONS = {c["camera_id"]: c["location_id"] for c in CAMERAS}


# ------------------------------------------------------------------------------------------------ vectors
def unit(v: Sequence[float]) -> np.ndarray:
    a = np.asarray(v, dtype=np.float64)
    return (a / np.linalg.norm(a)).astype(np.float32)


def random_unit(seed: int, dim: int = DIM) -> np.ndarray:
    return unit(np.random.default_rng(seed).normal(size=dim))


def blend(a: np.ndarray, b: np.ndarray, cos: float) -> np.ndarray:
    """Unit vector with cosine ``cos`` to ``a`` (b is used for the orthogonal part)."""
    a = unit(a)
    b = np.asarray(b, dtype=np.float64)
    b = b - np.dot(b, a) * a
    b = b / np.linalg.norm(b)
    return unit(cos * a + math.sqrt(max(0.0, 1 - cos * cos)) * b)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


# ------------------------------------------------------------------------------------------------ fake inventory
HIDDEN = {"merged", "dismissed"}


def _within(location_id: str | None, ancestor: str, locations: Sequence[dict[str, Any]]) -> bool:
    """True when ``location_id`` is ``ancestor`` or below it in the location tree."""
    parent = {loc["location_id"]: loc.get("parent_id") for loc in locations}
    seen: set[str] = set()
    current = location_id
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = parent.get(current)
    return False


class FakeInventory:
    """In-memory InventoryClient. Every posted payload must survive a JSON round trip (as over HTTP) and validate
    against inventory-api's IngestWindow model; it is then applied to the in-memory state."""

    def __init__(self, *, catalog=None, locations=None, ignore_catalog_filter: bool = False, fail_post: bool = False):
        self.catalog = list(catalog if catalog is not None else CATALOG)
        self.locations = list(locations if locations is not None else LOCATIONS)
        self.ignore_catalog_filter = ignore_catalog_filter
        self.fail_post = fail_post
        self.entities: dict[str, dict[str, Any]] = {}
        self.posted: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.stock: dict[tuple[str, str], dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.candidate_calls: list[dict[str, Any]] = []
        self.calls: list[str] = []

    # -- setup
    def add_entity(self, *, catalog_id: str | None, label: str | None = None, embedding=None,
                   last_location: str | None = None, last_seen_at: datetime | None = None,
                   status: str = "provisional", kind: str = "object", entity_id: str | None = None) -> str:
        eid = entity_id or str(uuid.uuid4())
        self.entities[eid] = {
            "entity_id": eid, "catalog_id": catalog_id, "kind": kind, "status": status,
            "label": label or (CATALOG_BY_ID[catalog_id]["class_name"] if catalog_id else "unknown object"),
            "last_location": last_location, "last_seen_at": last_seen_at, "first_seen_at": last_seen_at,
            "embeddings": [unit(embedding)] if embedding is not None else [], "needs_review": False,
            "review_reason": None, "sightings": [],
        }
        return eid

    def live(self) -> list[dict[str, Any]]:
        return [e for e in self.entities.values() if e["status"] not in HIDDEN]

    # -- InventoryClient protocol
    def get_catalog(self):
        self.calls.append("get_catalog")
        return [dict(c) for c in self.catalog]

    def get_locations(self):
        self.calls.append("get_locations")
        return [dict(loc) for loc in self.locations]

    def candidates(self, embedding, *, catalog_id=None, location_id=None, k=10):
        self.calls.append("candidates")
        q = np.asarray(list(embedding), dtype=np.float64)
        assert q.shape == (DIM,), f"candidate embedding must be {DIM}-d, got {q.shape}"
        assert abs(np.linalg.norm(q) - 1.0) < 1e-3, "candidate embedding must be L2-normalised"
        self.candidate_calls.append({"embedding": q, "catalog_id": catalog_id, "location_id": location_id, "k": k})
        rows = []
        for e in self.live():
            if not e["embeddings"]:
                continue
            if catalog_id and not self.ignore_catalog_filter and e["catalog_id"] != catalog_id:
                continue
            sim = max(cosine(q, v) for v in e["embeddings"])
            rows.append({
                "entity_id": e["entity_id"], "catalog_id": e["catalog_id"], "label": e["label"], "kind": e["kind"],
                "status": e["status"], "similarity": sim, "last_location": e["last_location"],
                "last_seen_at": iso(e["last_seen_at"]) if e["last_seen_at"] else None,
            })
        rows.sort(key=lambda r: -r["similarity"])
        return rows[:k]

    def post_window(self, payload):
        self.calls.append("post_window")
        wire = json.loads(json.dumps(payload))  # must be JSON-serialisable, exactly like the HTTP client
        if _IngestWindowModel is not None:
            _IngestWindowModel.model_validate(wire)
        if self.fail_post:
            raise RuntimeError("inventory-api unavailable")
        self.posted.append(wire)
        return self._apply(wire)

    def count_live_entities(self, catalog_id):
        self.calls.append("count_live_entities")
        return sum(1 for e in self.live() if e["catalog_id"] == catalog_id)

    def list_items(self, **filters):
        self.calls.append("list_items")
        items = self.live()
        if filters.get("catalog_id"):
            items = [e for e in items if e["catalog_id"] == filters["catalog_id"]]
        if filters.get("status"):
            st = filters["status"]
            st = [st] if isinstance(st, str) else list(st)
            items = [e for e in self.entities.values() if e["status"] in st and
                     (not filters.get("catalog_id") or e["catalog_id"] == filters["catalog_id"])]
        if filters.get("location"):  # GET /items?location=X: last_location in the subtree of X (like the API)
            items = [e for e in items if _within(e["last_location"], str(filters["location"]), self.locations)]
        if filters.get("q"):
            q = str(filters["q"]).lower()
            items = [e for e in items if q in e["label"].lower()]
        if filters.get("needs_review") is not None:
            items = [e for e in items if bool(e["needs_review"]) == bool(filters["needs_review"])]
        items = sorted(items, key=lambda e: e["last_seen_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
        limit = int(filters.get("limit") or 50)
        return [self._item(e) for e in items[:limit]]

    def list_events(self, *, entity_id=None, type=None, after=None, before=None, limit=100):
        """GET /events: newest first, like inventory-api (``entity_id`` is the resolved entity)."""
        self.calls.append("list_events")
        rows = []
        for ev in self.events:
            if entity_id is not None and ev["resolved_entity"] != str(entity_id):
                continue
            if type is not None and ev.get("event_type") != type:
                continue
            at = parse_dt(ev["occurred_at"])
            if (after is not None and at < parse_dt(after)) or (before is not None and at > parse_dt(before)):
                continue
            rows.append({"event_id": str(uuid.uuid4()), "event_type": ev.get("event_type"),
                         "entity_id": ev["resolved_entity"], "actor_desc": ev.get("actor_desc"),
                         "from_location": ev.get("from_location"), "to_location": ev.get("to_location"),
                         "occurred_at": ev["occurred_at"], "confidence": ev.get("confidence"),
                         "evidence_url": ev.get("evidence_uri"), "source": ev.get("source")})
        rows.sort(key=lambda r: parse_dt(r["occurred_at"]), reverse=True)
        return rows[: int(limit)]

    def patch_item(self, entity_id, fields):
        self.calls.append("patch_item")
        self.patches.append((str(entity_id), dict(fields)))
        e = self.entities[str(entity_id)]
        for k, v in fields.items():
            if k in ("needs_review", "label", "description", "status", "last_location", "catalog_id"):
                e[k] = v
        return self._item(e)

    # -- internals
    def _item(self, e):
        return {k: (iso(v) if isinstance(v, datetime) else v) for k, v in e.items()
                if k not in ("embeddings", "sightings")}

    def _apply(self, p):
        refs: dict[str, str] = {}
        for a in p["assignments"]:
            s = a["sighting"]
            seen = parse_dt(s["seen_at"])
            if a.get("entity_id"):
                eid = str(a["entity_id"])
                e = self.entities[eid]
                if e["last_seen_at"] is None or e["last_seen_at"] < seen:
                    e["last_seen_at"] = seen
                    e["last_location"] = s.get("location_id") or e["last_location"]
            else:
                ne = a["new_entity"]
                eid = self.add_entity(catalog_id=ne.get("catalog_id"), label=ne["label"],
                                      last_location=s.get("location_id"), last_seen_at=seen,
                                      status=ne.get("status", "provisional"), kind=ne.get("kind", "object"))
                self.entities[eid]["new_entity"] = ne
            e = self.entities[eid]
            e["sightings"].append(s)
            if a.get("exemplar"):
                e["embeddings"].append(unit(a["exemplar"]["embedding"]))
            if a.get("ref"):
                refs[a["ref"]] = eid
        for ev in p.get("events", []):
            ev = dict(ev)
            ev["resolved_entity"] = refs[ev["entity_ref"]] if ev.get("entity_ref") else str(ev["entity_id"])
            self.events.append(ev)
        for rv in p.get("reviews", []):
            rv = dict(rv)
            target = refs.get(rv.get("entity_ref")) if rv.get("entity_ref") else rv.get("entity_id")
            rv["resolved_entity"] = str(target) if target else None
            if target:
                self.entities[str(target)]["needs_review"] = True
                self.entities[str(target)]["review_reason"] = rv["kind"]
            self.reviews.append(rv)
        for su in p.get("stock_updates", []):
            key = (su["catalog_id"], su["location_id"])
            old = self.stock.get(key)
            if old is None or parse_dt(old["seen_at"]) <= parse_dt(su["seen_at"]):
                self.stock[key] = su
        self.decisions.extend(p.get("decisions", []))
        return {"window_id": str(uuid.uuid4()), "entities": refs}

    # -- assertion helpers
    @property
    def last(self) -> dict[str, Any]:
        assert self.posted, "nothing was posted to /ingest/window"
        return self.posted[-1]


# ------------------------------------------------------------------------------------------------ payload helpers
def assignment_for_bbox(payload, bbox, tol: float = 0.02):
    """The assignment whose sighting bbox matches ``bbox`` (within tol), else None."""
    for a in payload["assignments"]:
        b = a["sighting"].get("bbox")
        if b and all(abs(x - y) <= tol for x, y in zip(b, bbox, strict=True)):
            return a
    return None


def resolved_entity(payload, response_refs, assignment) -> str | None:
    if assignment.get("entity_id"):
        return str(assignment["entity_id"])
    return response_refs.get(assignment.get("ref"))


def uri_key(uri: str, bucket: str = BUCKET) -> str:
    prefix = f"s3://{bucket}/"
    assert uri.startswith(prefix), uri
    return uri[len(prefix):]


# ------------------------------------------------------------------------------------------------ observations
def obj(label: str, bbox=None, *, frame_index: int | None = 0, confidence: float = 0.9, description: str | None = None,
        held_by: str | None = None, attributes: dict | None = None) -> dict[str, Any]:
    o: dict[str, Any] = {"label": label, "description": description or f"a {label}", "confidence": confidence,
                         "bbox": list(bbox) if bbox is not None else None, "frame_index": frame_index}
    if held_by is not None:
        o["held_by"] = held_by
    if attributes:
        o["attributes"] = attributes
    return o


def observation(objects=(), *, people=(), events=(), transcript: str = "", location_hint: str | None = None,
                scene_summary: str = "crew working in the module", audio_events=()) -> dict[str, Any]:
    return {"scene_summary": scene_summary, "location_hint": location_hint, "objects": list(objects),
            "people": list(people), "events": list(events), "transcript": transcript,
            "audio_events": list(audio_events)}


def sine(seconds: float, *, rate: int = 16000, freq: float = 440.0, amp: float = 8000.0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def silence(seconds: float, *, rate: int = 16000) -> np.ndarray:
    return np.zeros(int(seconds * rate), dtype=np.int16)


# ------------------------------------------------------------------------------------------------ scenes
def scene_window(frames_objects, *, t0: datetime = T0, step_s: float = 1.0, audio=None, index: int = 0,
                 height: int = 480, width: int = 640):
    """Window whose frame i paints ``frames_objects[i]`` = [(object_id, bbox), ...] (perception.fakes.make_frame)."""
    from perception.fakes import make_frame
    from perception.types import Keyframe, Window

    frames = [
        Keyframe(timestamp=t0 + timedelta(seconds=i * step_s), image=make_frame(objs, height=height, width=width),
                 pts=i * step_s, index=i)
        for i, objs in enumerate(frames_objects)
    ]
    return Window(t_start=frames[0].timestamp, t_end=frames[-1].timestamp, frames=frames, audio=audio, index=index)


def detections_for(frames_objects, labels: dict[str, str], score: float = 0.9):
    from perception.types import Detection

    return [[Detection(labels[oid], tuple(bbox), score) for oid, bbox in objs] for objs in frames_objects]
