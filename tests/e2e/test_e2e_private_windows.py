"""End-to-end: private-window text redaction (UI review round 3, finding UI3-02; perception AC-PER-43, inventory-api
AC-API-39).

The eval scene has one quiet window in which GoldOmni lists a sleeping crew member (live Gemma text "dark hair,
sleeping in a blue sleeping bag") under the neutral summary "Crew member resting in the module". Perception treats it
as a sleep cue; afterwards observation_window holds no text for it, /search finds nothing, and its objects are still
sighted as location-only facts."""

from __future__ import annotations

import psycopg

SLEEP_SUMMARY = "Crew member resting in the module"


def test_e2e_12_sleep_cue_window_keeps_no_text_and_is_not_searchable(replay, api, database_url):
    with psycopg.connect(database_url) as conn:
        private = conn.execute(
            "SELECT window_id::text, privacy_reason, scene_summary, transcript, audio_events, raw_uri "
            "FROM observation_window WHERE is_private ORDER BY t_start").fetchall()
        sleep = [w for w in private if w[1] == "sleep_cue"]
        assert sleep, private  # one per pass (test_e2e_11 may have replayed the scene again)
        for _wid, reason, summary, transcript, audio, raw in private:
            assert reason in {"sleep_cue", "privacy_hold", "private_zone"}
            assert (summary, transcript, audio, raw) == (None, None, [], None)
        assert conn.execute("SELECT count(*) FROM observation_window WHERE scene_summary = %s",
                            (SLEEP_SUMMARY,)).fetchone()[0] == 0
        ids = [w[0] for w in private]
        sightings = conn.execute(
            "SELECT s.location_id, s.frame_uri, s.description, e.kind FROM sighting s "
            "JOIN entity e ON e.entity_id = s.entity_id WHERE s.window_id = ANY(%s::uuid[])", (ids,)).fetchall()
        sleep_sightings = conn.execute("SELECT count(*) FROM sighting WHERE window_id = %s::uuid",
                                       (sleep[0][0],)).fetchone()[0]
        audit = conn.execute("SELECT count(*) FROM audit_log WHERE detail->>'private' = 'true'").fetchone()[0]
        capturable = conn.execute("SELECT count(*) FROM observation_window WHERE NOT is_private "
                                  "AND scene_summary IS NOT NULL").fetchone()[0]
    # the sleep window's objects are still facts: a place and a time, no media, no description, no person
    assert sleep_sightings > 0
    assert sightings and all(loc for loc, *_ in sightings)
    assert all(frame is None and desc is None and kind == "object" for _, frame, desc, kind in sightings)
    assert audit == len(private)
    assert capturable > 0  # capturable windows keep their text

    for q in ("resting", "sleeping", "sleeping bag", "Crew member resting in the module"):
        hits = api.get("/search", params={"q": q})
        assert hits.status_code == 200, hits.text
        assert [h for h in hits.json() if h["type"] == "window"] == [], q
    # capturable windows are still searchable, with the flag the agent filters on
    window_hits = [h for h in api.get("/search", params={"q": "inventory survey"}).json() if h["type"] == "window"]
    assert window_hits and all(h["is_private"] is False for h in window_hits)

    stats = api.get("/stats").json()
    assert stats["windows"]["private"] == len(private) and stats["windows"]["total"] > len(private)
