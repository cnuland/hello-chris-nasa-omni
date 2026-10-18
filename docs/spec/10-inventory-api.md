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

## UI-review contract additions (docs/ui-review findings UI-03, UI-04, UI-08, UI-09, UI-11, UI-12, UI-13)

All additions are optional fields or optional query parameters, so existing clients keep working.

- AC-API-24 **Rich review candidates.** In `GET /review` and `GET /review/{id}`, each element of `candidates` is an
  Item (all the old Item fields are kept) extended with `thumbnail_url`, `sighting_count` and `similarity`. The
  dashboard relies on these fields: `{entity_id, label, catalog_id, status, last_location, last_location_name,
  last_seen_at, confidence, thumbnail_url, sighting_count, similarity}`.
  - `similarity` is the highest cosine similarity between any exemplar of the candidate and the review entity's best
    exemplar (highest quality, then newest). It is `null` when either side has no exemplar.
  - `sighting_count` is the candidate's number of sightings.
  - Each review also has `subject`, which has the same shape without `similarity`. It describes the review's own
    entity and is `null` when the review has no entity.
  - `candidates_source` is `"stored"` when the candidates come from `candidate_ids`, `"suggested"` when the server
    filled them (AC-API-25), and `"none"` when there are none.
  - `candidate_ids` always holds the ids stored on the review.
- AC-API-25 **Server-side duplicate candidates.** For a `possible_duplicate` review with an empty stored
  `candidate_ids` and an entity, the API suggests up to 8 candidates.
  - **Pool:** the other entities of the same kind that are not `merged` or `dismissed`, with the same `catalog_id` as
    the review entity. When the review entity has no catalog_id, the pool is the entities whose label, or whose
    catalog class_name, has the same normalised key as the review entity's label. The key is lower case with spaces
    and hyphens removed.
  - **Order:** `similarity` desc (nulls last). When there is no similarity, same-module entities come first (module
    of `last_location`, by hierarchy), then `last_seen_at` desc (nulls last), then `entity_id`.
  - All suggestions for one page of reviews are computed in one SQL statement, not one per review.
- AC-API-26 **`image_bbox`** on every review is `[x1,y1,x2,y2]`, normalised 0..1, for the object in `image_url`.
  - The API uses the bbox of the sighting of the review's entity whose `frame_uri` equals the review's `image_uri`;
    when there are several, it takes the one nearest `observed_at`.
  - It is `null` when no such sighting exists, for example when the image is a crop (the crop is the object), or when
    the stored bbox is not a valid normalised box.
- AC-API-27 **Review privacy.** `thumbnail_url` (and `snapshot_url`) of a candidate or subject is `null` when its
  `last_location` is no_capture, ancestors included (crew quarters NOD2S5/D5/P5/O5, WHC NOD3F4). A stored review whose
  `location_id` is no_capture returns `image_url: null` and `image_bbox: null`. Ingest already rejects such reviews
  (AC-API-19); this is defence in depth.
- AC-API-28 **Review filters and detail.**
  - `GET /review` accepts these parameters, combined with AND:
    - `kind`
    - `location`: the review's `location_id` within the location's subtree, like /items
    - `q`: case-insensitive substring over the review description and the entity label; LIKE wildcards in `q`
      match literally
    - `sort=created_at|observed_at` (default `created_at`) with `order=desc|asc` (default `desc`)
    - `limit` (1..500, default 50) and `offset` (default 0)
  - The body stays a JSON array. The `X-Total-Count` response header holds the number of matches before
    pagination. Invalid enum values return 422.
  - `GET /review/{id}` returns one review in the same shape: 404 if it is unknown, 422 if the id is malformed.
- AC-API-29 **Event context.** Each event from `GET /events` (and in `GET /items/{id}` `events`) also carries
  `entity_label`, `entity_catalog_id`, `from_location_name` and `to_location_name`, each nullable. `GET /events` accepts
  `types=moved,used,...` (comma-separated). It is ANDed with `type`, and an unknown type in `types` returns 422.
- AC-API-30 **`last_handled`.** Every Item (list, detail, and anywhere else Items are returned) carries
  `last_handled: {event_type, actor_desc, occurred_at} | null`. It is the latest event of type moved, used,
  handed_over, added, removed, stowed, unstowed or consumed, excluding operator-sourced `added` events (item creation).
  It is computed in the same query as the Items (no N+1).
- AC-API-31 **Location provenance (replaces the review-round fix #6 `GREATEST` rule).**
  - New columns `entity.location_set_at timestamptz` and `entity.location_set_by text` (migration
    `003_location_provenance.sql`, idempotent, backfilled), exposed on Items.
  - **PATCH:** a `PATCH /items/{id}` that changes `last_location` sets `location_set_at = now()` and
    `location_set_by = <operator>`, and does **not** change `last_seen_at`.
  - **POST:** `POST /items` sets them to now() and the operator.
  - **Ingest, new entities:** set them to the sighting's `seen_at` and `pipeline`.
  - **Ingest, existing entities:** `last_seen_at`, confidence and snapshot still advance when the sighting is newer
    than `last_seen_at`. The location moves only when the sighting's `seen_at` is later than both `last_seen_at` and
    `location_set_at`, and then provenance becomes (`seen_at`, `pipeline`).
  - **Merge:** the target takes the source's location when the source's location is newer, comparing
    `COALESCE(location_set_at, last_seen_at)`. The previous provenance values are recorded for undo.
  - When `location_set_at` is null (legacy rows), `last_seen_at` stands in for it.
- AC-API-32 **History noise.** `GET /items/{id}/history` collapses by default (`collapse=true`).
  - **Collapsing:** each run of 2 or more consecutive (in timeline order) *routine* pipeline audit entries becomes one
    `audit` entry with `count`, `first_at`, `audit_ids` and a summary such as "14 pipeline updates (sighting
    refresh: last_seen_at/confidence)".
  - **Routine entries:** an entry is routine when `op='update'`, `actor='pipeline'`, the entity was not created by
    it, and none of its events or reviews belong to the entity.
  - **Other audit entries:** every other audit entry carries `count: 1`. Operator `update` summaries list the changed
    fields (`field: old -> new`).
  - **Raw audit:** `collapse=false` returns the raw audit rows unchanged in content.
