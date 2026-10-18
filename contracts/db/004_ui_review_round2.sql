-- 004: UI review round 2 (AC-API-33/34/37; findings UI2-05, UI2-07). Idempotent: safe to re-run on every start.
--
-- sighting.bbox_source says where a sighting's bbox came from: 'detector' (an OWLv2 box linked to the omni object) or
-- 'omni' (the omni model's own coarse box). Old sightings stay NULL (unknown); there is nothing reliable to backfill
-- from, and NULL is a valid value.
ALTER TABLE sighting ADD COLUMN IF NOT EXISTS bbox_source TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'sighting_bbox_source_check' AND conrelid = 'sighting'::regclass) THEN
        ALTER TABLE sighting ADD CONSTRAINT sighting_bbox_source_check
            CHECK (bbox_source IS NULL OR bbox_source IN ('detector', 'omni'));
    END IF;
END
$$;

-- merged_from on a target item (the live entities merged into it) and unmerge look-ups.
CREATE INDEX IF NOT EXISTS entity_merged_into_idx ON entity(merged_into) WHERE merged_into IS NOT NULL;

-- merged_at and the unmerge endpoint find a merge's audit row by its entity ids (entity_ids @> ARRAY[source, target]).
CREATE INDEX IF NOT EXISTS audit_log_entity_ids_gin ON audit_log USING gin (entity_ids);
