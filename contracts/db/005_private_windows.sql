-- 005: private windows keep no text (AC-API-39; UI review round 3 finding UI3-02). Idempotent: safe on every start.
--
-- observation_window.is_private / privacy_reason mark no_capture windows (crew quarters, WHC, sleep cues, the
-- privacy hold). Ingest stores no summary, transcript or audio events for them and /search never returns them.
--
-- One-shot redaction of rows stored before this migration. It runs only in the transaction that adds is_private, so
-- a restart never re-applies it to newer rows (those follow the ingest rule). A stored window is private when:
--   raw_uri IS NULL                                  perception uploads the raw reply for every capturable window
--   AND none of its sightings has a frame_uri        ... and a frame for every sighting of one
--   AND (it has a sighting                           a no_capture window that still sighted known objects
--        OR its text matches a private cue)          a private window with no sighting at all (a sleeper alone)
-- Its summary, transcript and audio events are nulled, its sightings' descriptions are nulled, the description of a
-- pipeline entity created by it (first sighting there, description unchanged since) is nulled, and agent_memory rows
-- whose source span covers it are deleted (dreaming regenerates them without the text).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'observation_window'
                      AND column_name = 'is_private') THEN
        ALTER TABLE observation_window ADD COLUMN is_private BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE observation_window ADD COLUMN IF NOT EXISTS privacy_reason TEXT;

        CREATE TEMP TABLE redact_window ON COMMIT DROP AS
        SELECT w.window_id, w.t_start, w.t_end
          FROM observation_window w
         WHERE w.raw_uri IS NULL
           AND NOT EXISTS (SELECT 1 FROM sighting s WHERE s.window_id = w.window_id AND s.frame_uri IS NOT NULL)
           AND (EXISTS (SELECT 1 FROM sighting s WHERE s.window_id = w.window_id)
                OR (coalesce(w.scene_summary, '') || ' ' || coalesce(w.transcript, '')) ~*
                   '\m(sleep[a-z]*|asleep|bunks?|crew quarters|quarters|cqs?|toilets?|whc|hygiene|napping|dozing|slumber[a-z]*)\M');

        -- entity descriptions first: they are compared with the sighting text that is nulled next
        UPDATE entity e
           SET description = NULL
          FROM (SELECT DISTINCT ON (s.entity_id) s.entity_id, s.window_id, s.description
                  FROM sighting s ORDER BY s.entity_id, s.seen_at, s.sighting_id) first_s
          JOIN redact_window r ON r.window_id = first_s.window_id
         WHERE e.entity_id = first_s.entity_id AND e.created_by = 'pipeline'
           AND e.description IS NOT NULL AND e.description = first_s.description;

        UPDATE sighting s SET description = NULL
          FROM redact_window r WHERE s.window_id = r.window_id AND s.description IS NOT NULL;

        UPDATE observation_window w
           SET is_private = TRUE, privacy_reason = 'legacy_redaction',
               scene_summary = NULL, transcript = NULL, audio_events = '[]'::jsonb
          FROM redact_window r WHERE w.window_id = r.window_id;

        DELETE FROM agent_memory m
         WHERE EXISTS (SELECT 1 FROM redact_window r
                        WHERE (m.source_from IS NULL OR m.source_from <= r.t_end)
                          AND (m.source_to IS NULL OR m.source_to >= r.t_start));
    END IF;
END
$$;

ALTER TABLE observation_window ADD COLUMN IF NOT EXISTS privacy_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'observation_window_privacy_reason_check'
                      AND conrelid = 'observation_window'::regclass) THEN
        ALTER TABLE observation_window ADD CONSTRAINT observation_window_privacy_reason_check
            CHECK (privacy_reason IS NULL
                   OR privacy_reason IN ('private_zone', 'sleep_cue', 'privacy_hold', 'legacy_redaction'));
    END IF;
END
$$;

-- /stats counts private windows; /search filters them out
CREATE INDEX IF NOT EXISTS window_private_idx ON observation_window(is_private) WHERE is_private;
