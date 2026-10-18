-- 006: crop privacy (AC-API-41; UI review round 4 finding UI4-01). Idempotent: safe on every start.
--
-- sighting.person_overlap marks a sighting whose object crop overlapped a person region (perception AC-PER-49, or the
-- API's own check against sighting.person_boxes). Its crop and exemplar are never stored.
-- entity_exemplar.sighting_id links an exemplar to the sighting it was cut from, so a review's Classify can refuse to
-- carry an exemplar of a person_overlap sighting into the catalog. Legacy exemplars have no link (null).
-- Guarded (like 005): on a restart nothing is altered, so no ACCESS EXCLUSIVE lock is taken on every API start.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'sighting'
                      AND column_name = 'person_overlap') THEN
        ALTER TABLE sighting ADD COLUMN person_overlap BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'entity_exemplar'
                      AND column_name = 'sighting_id') THEN
        ALTER TABLE entity_exemplar ADD COLUMN sighting_id BIGINT REFERENCES sighting(sighting_id) ON DELETE SET NULL;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS exemplar_sighting_idx ON entity_exemplar(sighting_id) WHERE sighting_id IS NOT NULL;
