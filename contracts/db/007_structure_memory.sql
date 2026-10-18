-- 007: structure memory (AC-API-46; UI review round 6 finding UI6-05). Idempotent: safe to re-run on every start.
--
-- One row per "Not inventory" dismissal (POST /review/{id}/resolve, action dismiss, note not_inventory) of an
-- uncatalogued entity: what the operator said is station structure, where, and how it looked. Perception reads the
-- active rows (GET /structure-memory) and suppresses new unknowns that match one in the same place (AC-PER-64).
-- label_key is perception's AC-PER-31 key (lower case, non-alphanumerics removed). module_id is the module that
-- contains location_id (null at segment level). embedding is the entity's best exemplar (null when it had none).
-- No backfill: earlier dismissals carry no reliable note.
CREATE TABLE IF NOT EXISTS structure_memory (
    memory_id           BIGSERIAL PRIMARY KEY,
    review_id           UUID UNIQUE REFERENCES review_item(review_id) ON DELETE SET NULL,
    entity_id           UUID REFERENCES entity(entity_id) ON DELETE SET NULL,
    label               TEXT NOT NULL,
    label_key           TEXT NOT NULL,
    description_tokens  TEXT[] NOT NULL DEFAULT '{}',
    location_id         TEXT REFERENCES location(location_id),
    module_id           TEXT REFERENCES location(location_id),
    embedding           halfvec(768),
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS structure_memory_created_idx ON structure_memory(created_at DESC);
CREATE INDEX IF NOT EXISTS structure_memory_entity_idx ON structure_memory(entity_id) WHERE entity_id IS NOT NULL;
