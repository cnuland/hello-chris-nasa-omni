-- NASA Omni Inventory — schema v1 (contract). Postgres 16 + pgvector >= 0.7 (halfvec).
-- Source of truth for the inventory (ARCHITECTURE R3-R9). Applied in order by services/inventory-api migrations.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- locations: module -> zone (camera-configured) ----------
CREATE TABLE IF NOT EXISTS location (
    location_id   TEXT PRIMARY KEY,                 -- e.g. 'NODE2', 'NODE2.DECK', 'LAB.RACK-S2'
    parent_id     TEXT REFERENCES location(location_id),
    kind          TEXT NOT NULL CHECK (kind IN ('segment','module','zone','container')),
    display_name  TEXT NOT NULL,
    no_capture    BOOLEAN NOT NULL DEFAULT FALSE    -- privacy: crew quarters, WHC/toilet, medical — no frames stored
);

CREATE TABLE IF NOT EXISTS camera (
    camera_id     TEXT PRIMARY KEY,
    location_id   TEXT NOT NULL REFERENCES location(location_id),   -- default zone for the camera's view
    description   TEXT
);

-- ---------- closed-world catalog ----------
CREATE TABLE IF NOT EXISTS catalog_item (
    catalog_id     TEXT PRIMARY KEY,                -- e.g. 'drill-pistol-grip'
    class_name     TEXT NOT NULL,                   -- canonical label used by detectors/omni prompts
    display_name   TEXT NOT NULL,
    synonyms       TEXT[] NOT NULL DEFAULT '{}',
    category       TEXT NOT NULL DEFAULT 'object' CHECK (category IN ('object','person','container')),
    is_fungible    BOOLEAN NOT NULL DEFAULT FALSE,  -- tracked as counts per zone (food packs, wipes)
    expected_count INTEGER,                         -- closed-world prior; NULL = unknown
    home_location  TEXT REFERENCES location(location_id),
    reference_image_uri TEXT,
    text_embedding halfvec(768),                    -- label/synonym embedding for label->catalog mapping
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- entities: one row per physical item instance or person-track ----------
CREATE TABLE IF NOT EXISTS entity (
    entity_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id      TEXT REFERENCES catalog_item(catalog_id),        -- NULL while unknown/unclassified
    kind            TEXT NOT NULL DEFAULT 'object' CHECK (kind IN ('object','person')),
    status          TEXT NOT NULL DEFAULT 'provisional'
                    CHECK (status IN ('confirmed','provisional','unknown','merged','dismissed')),
    merged_into     UUID REFERENCES entity(entity_id),
    label           TEXT NOT NULL,                  -- best current name ("pistol-grip drill", "crew member, blue shirt")
    description     TEXT,                           -- latest consolidated description
    attributes      JSONB NOT NULL DEFAULT '{}',    -- color, size, text on label, ...
    last_location   TEXT REFERENCES location(location_id),
    last_seen_at    TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ,
    confidence      REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    snapshot_uri    TEXT,                           -- best crop/frame (RustFS)
    parent_entity   UUID REFERENCES entity(entity_id),               -- containment: item in CTB/bag
    ims_barcode     TEXT,                                            -- IMS label (Code 39) when read
    needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
    review_reason   TEXT,
    created_by      TEXT NOT NULL DEFAULT 'pipeline' CHECK (created_by IN ('pipeline','operator')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'merged' OR merged_into IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS entity_status_idx   ON entity(status);
CREATE INDEX IF NOT EXISTS entity_catalog_idx  ON entity(catalog_id);
CREATE INDEX IF NOT EXISTS entity_location_idx ON entity(last_location);
CREATE INDEX IF NOT EXISTS entity_seen_idx     ON entity(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS entity_label_trgm   ON entity USING gin (label gin_trgm_ops);

-- appearance exemplars for dedup (multi-view gallery, capped per entity)
CREATE TABLE IF NOT EXISTS entity_exemplar (
    exemplar_id  BIGSERIAL PRIMARY KEY,
    entity_id    UUID NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    embedding    halfvec(768) NOT NULL,             -- DINOv2-base crop embedding (L2-normalised)
    quality      REAL NOT NULL DEFAULT 0.5,
    crop_uri     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS exemplar_entity_idx ON entity_exemplar(entity_id);
CREATE INDEX IF NOT EXISTS exemplar_hnsw ON entity_exemplar USING hnsw (embedding halfvec_cosine_ops);

-- fungible stock: counts per zone (no instance identity)
CREATE TABLE IF NOT EXISTS stock (
    catalog_id    TEXT NOT NULL REFERENCES catalog_item(catalog_id),
    location_id   TEXT NOT NULL REFERENCES location(location_id),
    est_count     INTEGER NOT NULL CHECK (est_count >= 0),
    last_seen_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (catalog_id, location_id)
);

-- ---------- raw perception: observation windows and sightings ----------
CREATE TABLE IF NOT EXISTS observation_window (
    window_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id     TEXT NOT NULL REFERENCES camera(camera_id),
    t_start       TIMESTAMPTZ NOT NULL,
    t_end         TIMESTAMPTZ NOT NULL,
    scene_summary TEXT,                             -- omni model's window description
    transcript    TEXT,                             -- speech heard in the window
    audio_events  JSONB NOT NULL DEFAULT '[]',
    model         TEXT NOT NULL,                    -- model id + digest for audit
    raw_uri       TEXT,                             -- full model response in RustFS
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (t_end >= t_start)
);
CREATE INDEX IF NOT EXISTS window_time_idx ON observation_window(t_start DESC);
CREATE INDEX IF NOT EXISTS window_fts ON observation_window
    USING gin (to_tsvector('english', coalesce(scene_summary,'') || ' ' || coalesce(transcript,'')));

CREATE TABLE IF NOT EXISTS sighting (
    sighting_id   BIGSERIAL PRIMARY KEY,
    entity_id     UUID NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    window_id     UUID REFERENCES observation_window(window_id) ON DELETE SET NULL,
    location_id   TEXT REFERENCES location(location_id),
    seen_at       TIMESTAMPTZ NOT NULL,
    bbox          REAL[],                           -- [x1,y1,x2,y2] normalised 0..1, NULL if none
    confidence    REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    frame_uri     TEXT,
    description   TEXT
);
CREATE INDEX IF NOT EXISTS sighting_entity_time ON sighting(entity_id, seen_at DESC);

-- ---------- events: who moved what, where, when ----------
CREATE TABLE IF NOT EXISTS event (
    event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type    TEXT NOT NULL CHECK (event_type IN ('moved','added','removed','used','handed_over','seen','stowed','unstowed','consumed')),
    entity_id     UUID NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    actor_entity  UUID REFERENCES entity(entity_id),                 -- person-track entity
    actor_desc    TEXT,                                             -- "crew member, blue shirt"
    from_location TEXT REFERENCES location(location_id),
    to_location   TEXT REFERENCES location(location_id),
    occurred_at   TIMESTAMPTZ NOT NULL,
    confidence    REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_uri  TEXT,                                             -- frame at time of movement
    source        TEXT NOT NULL DEFAULT 'pipeline' CHECK (source IN ('pipeline','operator','speech')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS event_entity_time ON event(entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS event_time ON event(occurred_at DESC);

-- ---------- review queue (unknowns, low-confidence matches, likely duplicates) ----------
CREATE TABLE IF NOT EXISTS review_item (
    review_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          TEXT NOT NULL CHECK (kind IN ('unknown_object','possible_duplicate','low_confidence','conflict')),
    entity_id     UUID REFERENCES entity(entity_id) ON DELETE CASCADE,
    candidate_ids UUID[] NOT NULL DEFAULT '{}',     -- for duplicates/low-confidence: candidate existing entities
    image_uri     TEXT NOT NULL,                    -- REQUIRED: NASA must see what it is classifying
    description   TEXT NOT NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    location_id   TEXT REFERENCES location(location_id),
    model_guess   TEXT,
    probabilities JSONB NOT NULL DEFAULT '{}',      -- decision-model scores, for audit
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','dismissed')),
    resolution    JSONB,                            -- {action, catalog_id|target_entity_id, by, note}
    resolved_by   TEXT,
    resolved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_open_idx ON review_item(status, created_at DESC);

-- ---------- audit: merges/splits/edits are reversible and attributable ----------
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id      BIGSERIAL PRIMARY KEY,
    op            TEXT NOT NULL CHECK (op IN ('create','update','merge','split','classify','dismiss','delete')),
    entity_ids    UUID[] NOT NULL DEFAULT '{}',
    actor         TEXT NOT NULL,                    -- 'pipeline' | 'decision-model' | operator name
    detail        JSONB NOT NULL DEFAULT '{}',
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- decisions: every decision-model read is stored with its probabilities ----------
CREATE TABLE IF NOT EXISTS decision_log (
    decision_id   BIGSERIAL PRIMARY KEY,
    question      TEXT NOT NULL,                    -- 'same_or_new' | 'event_type' | 'zone'
    subject       JSONB NOT NULL,
    choice        TEXT NOT NULL,
    probabilities JSONB NOT NULL DEFAULT '{}',
    model         TEXT NOT NULL,
    latency_ms    INTEGER,
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- agent memory tier 3 ("dreaming"): regenerated summaries, inventory-first ----------
CREATE TABLE IF NOT EXISTS agent_memory (
    memory_id     BIGSERIAL PRIMARY KEY,
    scope         TEXT NOT NULL CHECK (scope IN ('entity','location','global')),
    scope_key     TEXT NOT NULL,                    -- entity_id | location_id | 'global'
    summary       TEXT NOT NULL,
    priority      REAL NOT NULL DEFAULT 0.5,        -- inventory facts > chatter
    source_from   TIMESTAMPTZ,
    source_to     TIMESTAMPTZ,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, scope_key)
);
