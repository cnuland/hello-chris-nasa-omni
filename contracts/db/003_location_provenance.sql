-- 003: location provenance (AC-API-31, UI review finding UI-09). Idempotent: safe to re-run on every start.
--
-- last_seen_at means "last camera sighting" again. The operator-move protection that review-round fix #6 got by
-- bumping last_seen_at to now() now lives in location_set_at: pipeline ingest only moves an item when the sighting is
-- newer than location_set_at (and last_seen_at).
ALTER TABLE entity ADD COLUMN IF NOT EXISTS location_set_at TIMESTAMPTZ;
ALTER TABLE entity ADD COLUMN IF NOT EXISTS location_set_by TEXT;   -- 'pipeline' | operator name

-- Backfill 1: items an operator moved under the old GREATEST rule. That PATCH wrote last_seen_at = now() and a
-- source='operator' moved event with occurred_at = now() in one transaction, so the two are exactly equal. Record the
-- move as provenance and restore last_seen_at to the real last sighting (for operator-created items, their creation
-- time counts as seen). Only rows not yet backfilled are touched, so a re-run changes nothing.
UPDATE entity e
   SET location_set_at = e.last_seen_at,
       location_set_by = (
           SELECT ev.actor_desc FROM event ev
            WHERE ev.entity_id = e.entity_id AND ev.event_type = 'moved' AND ev.source = 'operator'
              AND ev.occurred_at = e.last_seen_at
            ORDER BY ev.created_at DESC LIMIT 1),
       last_seen_at = COALESCE(
           GREATEST((SELECT max(s.seen_at) FROM sighting s WHERE s.entity_id = e.entity_id),
                    CASE WHEN e.created_by = 'operator' THEN e.first_seen_at END),
           e.last_seen_at)
 WHERE e.location_set_at IS NULL AND e.last_seen_at IS NOT NULL AND e.last_location IS NOT NULL
   AND EXISTS (SELECT 1 FROM event ev
                WHERE ev.entity_id = e.entity_id AND ev.event_type = 'moved' AND ev.source = 'operator'
                  AND ev.occurred_at = e.last_seen_at);

-- Backfill 2: everything else. The current location was set when the item was last seen, by whoever created it.
UPDATE entity
   SET location_set_at = last_seen_at,
       location_set_by = CASE WHEN created_by = 'operator' THEN 'operator' ELSE 'pipeline' END
 WHERE location_set_at IS NULL AND last_seen_at IS NOT NULL AND last_location IS NOT NULL;
