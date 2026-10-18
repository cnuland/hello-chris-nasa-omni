"""AC-API-39 private windows (UI review round 3, finding UI3-02).

Private (no_capture) windows keep no text: the API stores is_private/privacy_reason, drops the summary, transcript,
audio events and sighting/new-entity/actor descriptions of a private window whatever the client sends, hides private
windows from /search, counts them in /stats, and migration 005 redacts what is already stored (one-shot)."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import psycopg
import pytest

from inventory_api.migrations import apply_migrations
from tests.conftest import ADMIN_URL, BUCKET, CONTRACTS_DB, _url_for, audit_rows, iso, make_window, ok, ts, vec

FRAME = f"s3://{BUCKET}/frames/cam-nod2-aft/0001.jpg"
SECRET = "zebramark person sleeping in a sleeping bag"


def body(*, private=None, reason=None, privacy=None, camera="cam-nod2-aft", location="NOD2", raw=True):
    window = {"camera_id": camera, "t_start": iso(10), "t_end": iso(10.1), "scene_summary": SECRET,
              "transcript": f"{SECRET} said good night", "audio_events": [{"label": "zebramark snoring"}],
              "model": "gemma4"}
    if raw:
        window["raw_uri"] = f"s3://{BUCKET}/raw/w1.json"
    if private is not None:
        window["private"] = private
    if reason is not None:
        window["privacy_reason"] = reason
    if privacy is not None:
        window["privacy"] = privacy
    return {
        "window": window,
        "assignments": [{
            "ref": "o1",
            "new_entity": {"catalog_id": "laptop", "kind": "object", "label": "laptop",
                           "description": f"laptop by the {SECRET}"},
            "sighting": {"location_id": location, "seen_at": iso(10), "confidence": 0.8,
                         "description": f"laptop near {SECRET}"},
            "exemplar": {"embedding": vec((0, 1.0)), "quality": 0.7},
        }],
        "events": [{"event_type": "used", "entity_ref": "o1", "actor_desc": f"crew member, {SECRET}",
                    "occurred_at": iso(10), "confidence": 0.7}],
        "decisions": [{"question": "speech_act", "subject": {"camera_id": camera, "utterance": SECRET,
                                                             "description": SECRET, "label": "laptop"},
                       "choice": "statement", "probabilities": {}, "model": "dg"}],
    }


def window_row(db, window_id):
    return db.execute(
        "SELECT is_private, privacy_reason, scene_summary, transcript, audio_events FROM observation_window "
        "WHERE window_id = %s", (window_id,)).fetchone()


def assert_no_secret_stored(db):
    for table in ("observation_window", "sighting", "entity", "event", "decision_log", "audit_log"):
        dump = json.dumps([list(r) for r in db.execute(f"SELECT * FROM {table}").fetchall()], default=str).lower()
        assert "zebramark" not in dump, table
        assert "sleeping" not in dump, table


# ------------------------------------------------------------------ ingest
def test_ac_api_39_private_window_stores_no_text_whatever_the_client_sends(client, db):
    r = ok(client.post("/ingest/window", json=body(private=True, reason="sleep_cue")), 201)
    assert window_row(db, r["window_id"]) == (True, "sleep_cue", None, None, [])
    eid = r["entities"]["o1"]
    assert db.execute("SELECT description FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    assert db.execute("SELECT description FROM entity WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    assert db.execute("SELECT actor_desc FROM event WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    subject = db.execute("SELECT subject FROM decision_log").fetchone()[0]
    assert subject == {"camera_id": "cam-nod2-aft", "label": "laptop"}
    assert_no_secret_stored(db)
    # the sighting itself is still a location fact
    assert db.execute("SELECT location_id FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0] == "NOD2"


@pytest.mark.parametrize("over, reason", [
    ({"privacy": {"no_capture": True, "reason": "location"}}, "private_zone"),  # older perception: privacy only
    ({"private": True, "reason": "made_up"}, "private_zone"),  # unknown category -> private_zone
    ({"private": True, "reason": "privacy_hold"}, "privacy_hold"),
    ({"location": "NOD2S5"}, "private_zone"),  # a no_capture sighting makes the window private (AC-API-19)
    ({"private": False, "location": "NOD2S5"}, "private_zone"),  # a client cannot declare it capturable
])
def test_ac_api_39_window_privacy_is_decided_by_the_api_too(client, db, over, reason):
    r = ok(client.post("/ingest/window", json=body(**over)), 201)
    assert window_row(db, r["window_id"]) == (True, reason, None, None, [])
    assert_no_secret_stored(db)


def test_ac_api_39_no_capture_camera_makes_the_window_private(client, db):
    db.execute("INSERT INTO camera (camera_id, location_id) VALUES ('cam-cq', 'NOD2S5')")
    r = ok(client.post("/ingest/window", json=body(camera="cam-cq", location=None)), 201)
    assert window_row(db, r["window_id"])[:3] == (True, "private_zone", None)


def test_ac_api_39_capturable_window_keeps_its_text(client, db):
    r = ok(client.post("/ingest/window", json=body(private=False)), 201)
    assert window_row(db, r["window_id"]) == (False, None, SECRET, f"{SECRET} said good night",
                                              [{"label": "zebramark snoring"}])
    eid = r["entities"]["o1"]
    assert db.execute("SELECT description FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0] == \
        f"laptop near {SECRET}"


def test_ac_api_39_ingest_audit_records_the_flag_and_no_text(client, db):
    ok(client.post("/ingest/window", json=body(private=True, reason="sleep_cue")), 201)
    ok(client.post("/ingest/window", json={**body(private=False), "window": {**body()["window"], "t_start": iso(20),
                                                                             "t_end": iso(20.1)}}), 201)
    flags = [a[3]["private"] for a in audit_rows(db, "update")]
    assert flags == [True, False]


# ------------------------------------------------------------------ read side
def test_ac_api_39_search_never_returns_a_private_window(client, db):
    ok(client.post("/ingest/window", json=body(private=True, reason="sleep_cue")), 201)
    # even a private row that still holds text (e.g. written before migration 005's redaction) is hidden
    w = make_window(db, t_start=ts(30), scene_summary="person sleeping in a bunk")
    db.execute("UPDATE observation_window SET is_private = TRUE WHERE window_id = %s", (w,))
    visible = make_window(db, t_start=ts(40), scene_summary="sleeping bag stowed in the cargo bag")
    for q in ("sleeping", "zebramark", "bunk", "good night"):
        hits = ok(client.get("/search", params={"q": q}))
        assert [h["id"] for h in hits if h["type"] == "window"] == ([visible] if q == "sleeping" else []), q
    [hit] = [h for h in ok(client.get("/search", params={"q": "stowed"})) if h["type"] == "window"]
    assert hit["is_private"] is False  # the flag is exposed under this exact name (agent search_observations)


def test_ac_api_39_stats_count_private_windows(client, db):
    ok(client.post("/ingest/window", json=body(private=True, reason="sleep_cue")), 201)
    make_window(db, t_start=ts(30), scene_summary="crew at the rack")
    assert ok(client.get("/stats"))["windows"] == {"total": 2, "private": 1}


# ------------------------------------------------------------------ migration 005 on a copy with live-like rows
def _fresh_db():
    name = f"test_api_mig5_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return name


def _drop(name):
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_ac_api_39_migration_005_redacts_existing_private_windows_once():
    name = _fresh_db()
    url = _url_for(name)
    try:
        with psycopg.connect(url, autocommit=True) as c:
            for f in ("001_init.sql", "002_seed.sql", "003_location_provenance.sql", "004_ui_review_round2.sql"):
                c.execute((CONTRACTS_DB / f).read_text())

            def window(minute, summary, *, raw, transcript=None):
                return c.execute(
                    "INSERT INTO observation_window (camera_id, t_start, t_end, scene_summary, transcript, "
                    "audio_events, model, raw_uri) VALUES ('cam-handheld', %s, %s, %s, %s, '[{\"label\": \"x\"}]', "
                    "'gemma4', %s) RETURNING window_id",
                    (ts(minute), ts(minute) + timedelta(seconds=5), summary, transcript,
                     f"s3://{BUCKET}/raw/{minute}.json" if raw else None)).fetchone()[0]

            def entity(desc, first_minute):
                return c.execute("INSERT INTO entity (label, description, first_seen_at, last_seen_at) "
                                 "VALUES ('laptop', %s, %s, %s) RETURNING entity_id",
                                 (desc, ts(first_minute), ts(first_minute))).fetchone()[0]

            def sighting(eid, wid, minute, desc, frame=None):
                c.execute("INSERT INTO sighting (entity_id, window_id, location_id, seen_at, confidence, frame_uri, "
                          "description) VALUES (%s, %s, 'NOD2', %s, 0.8, %s, %s)", (eid, wid, ts(minute), frame, desc))

            # capturable window with a cue word in its text (a stowed sleeping bag): kept
            kept_cue = window(1, "a sleeping bag stowed on the wall", raw=True)
            e_old = entity("black laptop", 1)
            sighting(e_old, kept_cue, 1, "black laptop", FRAME)
            # private sleep window (no raw reply, sightings without frames): redacted
            sleep = window(5, "A person is sleeping in a sleeping bag inside the Harmony Module", raw=False)
            e_new = entity("laptop next to the sleeper", 5)
            sighting(e_new, sleep, 5, "laptop next to the sleeper")
            sighting(e_old, sleep, 5, "black laptop again")
            # privacy-hold window: neutral summary, no raw reply, sightings without frames: redacted
            hold = window(6, "A view through a hatch into another module", raw=False)
            sighting(e_old, hold, 6, "black laptop on the rack")
            # private window without sightings (a sleeper alone): redacted by the cue pattern
            alone = window(7, "crew member resting in the CQ", raw=False, transcript="good night")
            # no raw reply, no sightings, no cue: not enough evidence, kept
            kept_plain = window(8, "crew eating lunch", raw=False)
            # no raw reply but a stored frame: a capturable window, kept
            kept_frame = window(9, "person sleeping on the screen of a laptop", raw=False)
            sighting(e_old, kept_frame, 9, "laptop showing a film", FRAME)
            c.execute("INSERT INTO agent_memory (scope, scope_key, summary, source_from, source_to) VALUES "
                      "('global', 'global', 'seen: person sleeping', %s, %s), "
                      "('location', 'LAB1', 'lab racks', %s, %s)", (ts(0), ts(60), ts(100), ts(200)))

        for _ in range(2):
            apply_migrations(url, CONTRACTS_DB)

        with psycopg.connect(url, autocommit=True) as c:
            rows = {r[0]: r[1:] for r in c.execute(
                "SELECT window_id, is_private, privacy_reason, scene_summary, transcript, audio_events "
                "FROM observation_window")}
            for wid in (sleep, hold, alone):
                assert rows[wid] == (True, "legacy_redaction", None, None, []), wid
            assert rows[kept_cue][:3] == (False, None, "a sleeping bag stowed on the wall")
            assert rows[kept_plain][:3] == (False, None, "crew eating lunch")
            assert rows[kept_frame][:2] == (False, None)
            descs = {(str(r[0]), r[1]): r[2] for r in c.execute(
                "SELECT window_id, seen_at, description FROM sighting")}
            assert descs[(str(sleep), ts(5))] is None and descs[(str(hold), ts(6))] is None
            assert descs[(str(kept_cue), ts(1))] == "black laptop"
            ents = dict(c.execute("SELECT entity_id, description FROM entity").fetchall())
            assert ents[e_new] is None  # created by the private window with its text
            assert ents[e_old] == "black laptop"  # created by a capturable window
            assert [r[0] for r in c.execute("SELECT scope FROM agent_memory")] == ["location"]

            # one-shot: a window written later is not touched by a re-run (new rows follow the ingest rule)
            later = c.execute(
                "INSERT INTO observation_window (camera_id, t_start, t_end, scene_summary, model) "
                "VALUES ('cam-handheld', %s, %s, 'person sleeping', 'm') RETURNING window_id",
                (ts(90), ts(90))).fetchone()[0]
        apply_migrations(url, CONTRACTS_DB)
        with psycopg.connect(url) as c:
            assert c.execute("SELECT is_private, scene_summary FROM observation_window WHERE window_id=%s",
                             (later,)).fetchone() == (False, "person sleeping")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("UPDATE observation_window SET privacy_reason = 'cue: sleeping bag'")
    finally:
        _drop(name)


# ------------------------------------------------------------------ AC-API-40 entity_kind on events
def test_ac_api_40_events_carry_entity_kind(client, db):
    from tests.conftest import make_entity, make_event

    person = make_entity(db, label="crew member", kind="person", last_seen_at=ts(1))  # no catalog_id, like live
    thing = make_entity(db, label="drill", catalog_id="drill-pistol", last_seen_at=ts(1))
    make_event(db, person, event_type="moved", occurred_at=ts(1), to_location="NOD1")
    make_event(db, thing, event_type="moved", occurred_at=ts(2), to_location="LAB1")
    kinds = {e["entity_id"]: e["entity_kind"] for e in ok(client.get("/events"))}
    assert kinds == {person: "person", thing: "object"}
    detail = ok(client.get(f"/items/{person}"))
    assert [e["entity_kind"] for e in detail["events"]] == ["person"]


# ------------------------------------------------------------------ adversarial review round
def test_review_private_window_review_is_rejected_even_outside_no_capture_zones(client, db):
    """A review stores a description and an image. A window flagged private (e.g. a sleep cue in NOD2, which is not
    itself no_capture) must not store one: 422 and nothing written, as AC-API-19 does for no_capture zones."""
    from tests.conftest import table_counts

    b = body(private=True, reason="sleep_cue")
    b["reviews"] = [{"kind": "unknown_object", "entity_ref": "o1", "image_uri": FRAME,
                     "description": f"unknown thing near {SECRET}", "observed_at": iso(10), "location_id": "NOD2"}]
    before = table_counts(db)
    r = client.post("/ingest/window", json=b)
    assert r.status_code == 422, r.text
    assert table_counts(db) == before


def test_review_private_window_drops_frames_crops_and_evidence_too(client, db):
    """Defence in depth: a client that flags the window private but still sends image URIs gets none stored."""
    b = body(private=True, reason="sleep_cue")
    b["assignments"][0]["sighting"]["frame_uri"] = FRAME
    b["assignments"][0]["exemplar"]["crop_uri"] = f"s3://{BUCKET}/crops/c.jpg"
    b["events"][0]["evidence_uri"] = FRAME
    eid = ok(client.post("/ingest/window", json=b), 201)["entities"]["o1"]
    assert db.execute("SELECT frame_uri FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    assert db.execute("SELECT snapshot_uri FROM entity WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    assert db.execute("SELECT crop_uri FROM entity_exemplar WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    assert db.execute("SELECT evidence_uri FROM event WHERE entity_id=%s", (eid,)).fetchone()[0] is None
