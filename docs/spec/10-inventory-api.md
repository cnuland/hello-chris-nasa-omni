# Spec: inventory-api (services/inventory-api)

Implements `contracts/openapi/inventory-api.yaml` over `contracts/db`. It is the only writer to Postgres.

- AC-API-1 `GET /healthz` returns 200 `{status:"ok", db:true}` when the DB is reachable, 503 otherwise.
- AC-API-2 `GET /items` returns `{items,total}`. Default sort is `-last_seen_at`, with nulls last. `limit`/`offset`
  paginate. `total` counts all matches before pagination.
- AC-API-3 Filters combine with AND: `kind`, repeatable `status`, `catalog_id`, `min_confidence`, the
  `seen_after`/`seen_before` window, and `needs_review`.
- AC-API-4 The `location` filter matches the location and **all descendants** (e.g. `NOD2` matches `NOD2D4`).
- AC-API-5 `q` matches label, description and catalog display_name/synonyms case-insensitively (trigram/ILIKE).
  Results are ranked by relevance when `q` is set and no explicit sort is given.
- AC-API-6 `merged` and `dismissed` entities are excluded unless explicitly requested via `status`.
- AC-API-7 `POST /items` (multipart) **requires an image**: missing or non-image → 422. On success it stores the image
  in the object store, creates the entity (`created_by=operator`, status `confirmed`, `last_location` = given) and an
  `added` event (source `operator`), writes an audit_log row, and returns 201 with `snapshot_url`.
- AC-API-8 `PATCH /items/{id}` updates only the provided fields. If `last_location` changed, it creates a `moved` event
  (source `operator`, from→to). It writes an audit_log row with before/after. 404 for an unknown id, 409 for a merged
  entity.
- AC-API-9 `POST /items/{id}/merge` sets the source `status=merged` and `merged_into` the target. It re-points
  sightings, events and exemplars to the target, updates the target's last_seen if the source is newer, and logs the
  merge in audit_log with enough detail to undo it. Merging into self, into a merged entity, or across kinds → 409.
- AC-API-10 `GET /items/{id}` includes up to 20 recent sightings, 20 events, `exemplar_count`, `last_moved_by` and
  `last_moved_at`.
- AC-API-11 `GET /items/{id}/history` returns sightings + events + audit entries merged, sorted by time desc.
- AC-API-12 `GET /review` lists open items newest first, with `candidates` expanded as Items. Every review item has
  `image_url`.
- AC-API-13 `POST /review/{id}/resolve` handles each action:
  - `assign_catalog` sets the entity's catalog_id, status `confirmed` and label = catalog display name (unless `label`
    is given).
  - `new_catalog` creates the catalog item, then assigns it.
  - `merge_into` merges the review entity into the target (as AC-API-9).
  - `dismiss` sets the entity to `dismissed`.
  - All actions mark the review `resolved`/`dismissed` with operator and time, clear `needs_review` and write an
    audit_log row.
  - An already-resolved review → 409; a missing action payload → 422.
- AC-API-14 `GET /catalog`, `POST /catalog` (409 for a duplicate id, 422 for a bad id pattern), `GET /locations`,
  `GET /events` (filters entity_id/type/after/before).
- AC-API-15 `GET /search?q=` returns entity hits and observation-window hits (full-text over
  scene_summary + transcript), each with type, id, snippet and time.
- AC-API-16 `GET /stats` returns counts by status, by kind and by module, plus open review counts by kind.
- AC-API-17 `POST /ingest/candidates` returns the top-k entities by max cosine similarity over exemplars. It filters
  by catalog_id when given and excludes merged/dismissed entities. Each candidate carries `similarity`,
  `last_location`, `last_seen_at`.
- AC-API-18 `POST /ingest/window` is **atomic**: it inserts the window, creates new entities (by `ref`), inserts
  sightings, updates each entity's last_location/last_seen_at/confidence/snapshot (only if the sighting is newer),
  stores exemplars capped at 20 per entity (lowest quality dropped), and inserts events, reviews and decision_log.
  Any invalid part → 422 with nothing written.
- AC-API-19 **Privacy**: a sighting or review whose location is `no_capture` is stored **without** frame/image URIs.
  For reviews in no-capture zones, the API rejects the review (422), because a review needs an image.
- AC-API-20 `GET /media/{key}` streams the object from S3 (RustFS) with the right content-type, 404 if missing. The
  key must not allow path traversal.
- AC-API-21 Every write endpoint records `actor` (operator name or `pipeline`) in audit_log.
- AC-API-22 `GET /items?status=` with a value outside the enum → 422, never a 500.
- AC-API-23 `/ingest/window` accepts `stock_updates` (upsert per (catalog_id, location_id); a newer `seen_at` wins).
  `GET /stock` lists them, filterable by catalog_id and location (with descendants).
