"""Reference pipeline snapshot for the committed CI dataset (AC-EVAL-6 review round).

``make test`` must gate something deterministic that is committed to the repository. Until a recorded perception run
exists (``nasa_eval run --keep-db`` + ``nasa_eval snapshot export``), ``eval/baselines/synth-ci.snapshot.json`` is
this *reference* snapshot: what a correct windowed pipeline writes, with realistic imperfections, so the gate and the
scorer are exercised on per-window data rather than on a copy of the gold:

- one sighting per object per perception window (6 frames every 4 s, AC-PER-2), stamped at the window's last frame;
- one window in which the mover is missed;
- one spurious entity (a false detection seen once);
- the ``moved`` event reported 2 s late; the ``removed`` event without an actor (no speech attribution);
- an ``unknown_object`` review for the unknown object;
- a correct revisit (perception AC-PER-32): the scene replayed after ``REVISIT_GAP_S`` creates no new entity;
- one person track per gold actor, described by the actor's appearance (perception AC-PER-35);
- the window rows, each with a raw reply and its sightings with a frame, except the sleep-cue window (summary
  ``SLEEP_SUMMARY``, perception AC-PER-37), which stores neither.

Replace the committed file with a recorded run once perception is integrated (the snapshot's ``source`` says which).
"""

from __future__ import annotations

from datetime import timedelta

from nasa_eval.dataset import Dataset
from nasa_eval.scoring import SLEEP_SUMMARY
from nasa_eval.snapshot import PredEntity, PredEvent, PredReview, PredSighting, PredWindow, RevisitResult, Snapshot

STRIDE_S = 4.0
WINDOW_FRAMES = 6
EVENT_DELAY_S = 2.0
SLEEP_REF_WINDOW = 3  # the reference's sleep-cue window (private: no raw reply, no frames)


def _seed_locations() -> dict[str, tuple[str | None, str]]:
    import re
    from pathlib import Path

    from nasa_eval import CONTRACTS_DB

    sql = (Path(CONTRACTS_DB) / "002_seed.sql").read_text()
    block = sql.split("INSERT INTO location", 1)[1].split("ON CONFLICT", 1)[0]
    out: dict[str, tuple[str | None, str]] = {}
    for m in re.finditer(r"^\s*\('([^']+)',\s*(NULL|'[^']*'),\s*'([^']+)'", block, re.MULTILINE):
        out[m.group(1)] = (None if m.group(2) == "NULL" else m.group(2).strip("'"), m.group(3))
    return out


def reference_snapshot(data: Dataset, *, stride_s: float = STRIDE_S, window_frames: int = WINDOW_FRAMES) -> Snapshot:
    """Deterministic per-window snapshot of ``data`` (see module docstring)."""
    start = data.manifest.start_time
    n_frames = 1 + max(round(a.t * data.fps) for g in data.gold for a in g.appearances)
    window_ends = [k / data.fps for k in range(window_frames - 1, n_frames, round(stride_s * data.fps))]
    snap = Snapshot(locations=_seed_locations())
    span = (window_frames - 1) / data.fps
    sleep_w = SLEEP_REF_WINDOW
    for w, t_end in enumerate(window_ends):
        raw = None if w == sleep_w else f"s3://inventory/raw/ref/{w:04d}.json"
        summary = SLEEP_SUMMARY if w == sleep_w else "objects on the racks"
        snap.windows.append(
            PredWindow(
                f"ref-w{w:04d}", start + timedelta(seconds=t_end - span), start + timedelta(seconds=t_end), raw, summary
            )
        )
    mover = next((g.gold_object_id for g in data.gold if any(e.type == "moved" for e in g.events)), None)
    n = 0
    for g in data.gold:
        eid = f"ref-{g.gold_object_id}"
        last = None
        for w, t_end in enumerate(window_ends):
            seen = [a for a in g.appearances if t_end - (window_frames - 1) / data.fps - 1e-9 <= a.t <= t_end + 1e-9]
            if not seen or (g.gold_object_id == mover and w == 2):
                continue
            a = max(seen, key=lambda x: x.t)
            n += 1
            frame = None if w == sleep_w else f"s3://inventory/frames/ref/{w:04d}.jpg"
            snap.sightings.append(
                PredSighting(f"ref-s{n:04d}", eid, start + timedelta(seconds=t_end), a.location, a.bbox,
                             window_id=f"ref-w{w:04d}", frame_uri=frame)
            )
            last = (t_end, a.location)
        if last is None:
            continue
        snap.entities[eid] = PredEntity(
            eid,
            g.catalog_id,
            "unknown" if g.is_unknown else "provisional",
            "object",
            last[1],
            start + timedelta(seconds=last[0]),
        )
        for e in g.events:
            delay = EVENT_DELAY_S if e.type == "moved" else 0.0
            actor = e.actor if e.type == "moved" else None
            snap.events.append(PredEvent(e.type, eid, start + timedelta(seconds=e.t + delay), actor, e.to_location))
        if g.is_unknown:
            snap.reviews.append(PredReview("unknown_object", eid))
    # one false detection, seen once, far from every gold box
    t_ghost = window_ends[len(window_ends) // 2]
    snap.entities["ref-ghost"] = PredEntity(
        "ref-ghost", "headset", "provisional", "object", "LAB1O3", start + timedelta(seconds=t_ghost)
    )
    snap.sightings.append(
        PredSighting("ref-s9999", "ref-ghost", start + timedelta(seconds=t_ghost), "LAB1O3", (0.40, 0.02, 0.45, 0.07))
    )
    # one person track per gold actor (people[] only, perception AC-PER-35)
    actors = sorted({e.actor for g in data.gold for e in g.events if e.actor})
    for i, actor in enumerate(actors):
        t_last = max(e.t for g in data.gold for e in g.events if e.actor == actor)
        snap.entities[f"ref-person-{i + 1}"] = PredEntity(
            f"ref-person-{i + 1}", "crew-member", "provisional", "person", "LAB1", start + timedelta(seconds=t_last),
            description=actor,
        )
    from nasa_eval.runner import REVISIT_GAP_S

    snap.revisit = RevisitResult(gap_s=REVISIT_GAP_S, objects=len(data.gold), new_entity_ids=[])
    snap.sightings.sort(key=lambda s: (s.seen_at, s.sighting_id))
    snap.events.sort(key=lambda e: (e.occurred_at, e.entity_id))
    return snap
