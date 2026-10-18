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
  - **Order:** by `score` (AC-API-35, which replaces the round-1 order "similarity, then same module, then
    recency"), then `similarity` desc (nulls last), `last_seen_at` desc (nulls last) and `entity_id`.
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

## UI review round 2 contract (docs/ui-review/round2: UI2-05, UI2-06, UI-16, UI2-07, UI2-09/UI-13)

All additions are optional response fields, an optional ingest field and one new endpoint. One field changes type:
`Item.merged_into`, from a bare uuid to an object. No client read it.

- AC-API-33 **Unmerge.** `POST /items/{id}/unmerge` with body `{operator, note?}`. `operator` is required (422 when
  it is missing or empty), and `{id}` is the merged (source) entity.
  - **What it uses:** the latest `merge` audit row for (source, current `merged_into`).
  - **What it moves back:** sightings, events, exemplars, actor references (`event.actor_entity`), children
    (`entity.parent_entity`) and re-pointed open reviews. Only rows still where the merge put them are moved.
    - **Candidate lists:** each review's `candidate_ids` is restored only while it still equals what the merge made
      of it.
    - **Dropped exemplars:** the exemplars the merge dropped for the cap are re-inserted with their original ids.
      Then the cap of 20 is re-applied to both entities, and anything dropped then is kept in the audit.
  - **Source:** gets back its previous `status`, `needs_review` and `review_reason`, and `merged_into` becomes null.
  - **Target fields:** `last_seen_at`, location (`last_location`, `location_set_at`, `location_set_by`) and
    `first_seen_at` get their pre-merge values, but only where they still hold exactly what the merge wrote. A
    later sighting or operator edit of the target is kept. `needs_review` and `review_reason` are restored when
    re-pointed reviews were moved back and no open review remains on the target.
  - **Review merges:** when the merge came from a review (`merge_into`), that review is reopened (`status=open`,
    resolution cleared).
  - **409 cases:**
    - the entity is not merged;
    - the target has itself been merged since;
    - there is no merge audit row with undo detail (legacy);
    - any target sighting recorded after the merge has an exemplar (stored by the same ingest) more similar to the
      source's moved exemplars than to the target's own.
  - **"After the merge":** a sighting id above the merge's new `sighting_watermark`. For older merges without one, it
    means a window created after the merge.
  - **Sightings without an exemplar** (omni-only) cannot be attributed and stay on the target.
  - **Writes:** one `split` audit row: `{action: "unmerge", merge_audit_id, source_entity_id, target_entity_id,
    restored, dropped_exemplars, previous_resolution, note}`.
  - **Response 200:** `{unmerged_entity_id, target_entity_id, source: Item, target: Item, restored: {sighting_ids,
    event_ids, exemplar_ids, reinserted_exemplar_ids, actor_event_ids, child_entity_ids, review_ids,
    candidate_review_ids, reopened_review_id, target_fields}}`.
- AC-API-34 **Merge links on every Item.** These fields are computed in one extra statement per page, not per item:
  - `merged_into: {entity_id, label} | null`: the survivor, set when `status=merged`.
  - `merged_at: datetime | null`: the time of that merge's audit row. It is null for a legacy merge with no audit.
  - `merged_from: [{entity_id, label, merged_at}]`: the entities merged into this one, oldest first.
- AC-API-35 **Ranked duplicate candidates.**
  - **Score:** every review candidate (suggested and stored) carries `score`, which is the sum of:
    - `1.0 x max(cosine, 0)`, when both sides have exemplars;
    - `0.5 x` pg_trgm `similarity(lower(label || ' ' || description))`;
    - `0.3` for the same zone (same `last_location` of kind zone or container), or `0.15` for the same module;
    - `0.2 / (1 + |delta last_seen_at| in hours)`.
  - `similarity` is unchanged: cosine, or null.
  - **match_reasons:** a list built in this order:
    - `"similar appearance"` when cosine >= 0.7;
    - `"similar description"` when both have a description and their trigram similarity is >= 0.3;
    - `"same zone"`, or else `"same module"`;
    - `"seen around the same time"` when the gap is <= 1 h.
  - **Order:** suggestions are ordered by `score` desc. Stored candidates keep their stored order.
- AC-API-36 **Label sort.** `sort=label` orders by `lower(label)`, then `label`, then `entity_id` (`-label` reverses
  the first two). It is case-insensitive under any database collation; the cluster's CNPG DB uses C.
- AC-API-37 **bbox provenance.**
  - **Storage:** `sighting.bbox_source` holds `detector`, `omni` or null (migration `004_ui_review_round2.sql`,
    idempotent, CHECK constraint, no backfill). Ingest accepts the optional `sighting.bbox_source`, ignores it when
    `bbox` is null, and returns 422 for any other value.
  - **Reviews:** `image_bbox_source` is returned next to `image_bbox`, and both are null together.
  - **Item detail:** each sighting has `bbox` and `bbox_source`.
  - **Items:** every Item has `snapshot_bbox` and `snapshot_bbox_source`. They come from the newest sighting of the
    item whose `frame_uri` equals the item's snapshot, so they are null when the snapshot is a crop. In review
    entities from no_capture locations they are nulled with the thumbnail.
  - Migration 004 also adds `entity_merged_into_idx` and a GIN index `audit_log_entity_ids_gin`.
- AC-API-38 **Event wording.**
  - **On every event** (`/events`, item detail `events`):
    - `location_role` is `"from_to"` for `moved`, or for any event whose from and to are both set and differ. It is
      `"at"` when the event names one place (or from = to), and null with no location.
    - `actor_kind` is `"operator"` when `source=operator` and `"person"` otherwise. It is null when the event names
      no actor (no `actor_desc` and no `actor_entity`).
  - **History event entries** carry the same two fields, and their summary reads `used at USOS by X`. Only moves
    read `moved A -> B by X`.

## UI review round 3: private-window text redaction (docs/ui-review/round3 finding UI3-02)

Private (`no_capture`) windows stored no media but kept their `scene_summary`, which `/search` (and so the agent's
`search_observations`) returned. Perception now sends no text for them (perception AC-PER-43); the API enforces it
on its own (defence in depth) and redacts what is already stored.

- AC-API-39 **Private windows.**
  - **Storage:** migration `005_private_windows.sql` (idempotent) adds `observation_window.is_private boolean not
    null default false` and `privacy_reason text` (CHECK: null or `private_zone` / `sleep_cue` / `privacy_hold` /
    `legacy_redaction`).
  - **Ingest:** a window is private when **any** of these holds: `window.private` is true; `window.privacy.no_capture`
    is true (older perception); the camera's location is no_capture; any sighting or event of the payload is private
    under AC-API-19. A private window is stored with `is_private = true`, `privacy_reason` = the client's category
    when it is one of the three, else `private_zone`, and `scene_summary`, `transcript` = null and
    `audio_events = []` **whatever the client sent**. Every sighting of a private window is stored with
    `description = null`; a new entity created by it gets `description = null`; its events get `actor_desc = null`;
    and decision subjects lose the text keys `utterance`, `description`, `transcript`, `scene_summary`, `text` and
    `evidence`. When the client flags the window private (or the camera is no_capture) every sighting and event is
    private under AC-API-19 (no frame, crop, snapshot or evidence URI) and any review is 422 with nothing written.
    A capturable window is stored with `is_private = false`, `privacy_reason = null`. The ingest audit row
    gains `"private": true|false` (no text; the audit never held window text).
  - **Search:** `/search` never returns a private window (`WHERE NOT w.is_private`), even if text were present.
    No other endpoint returns window text.
  - **Stats:** `/stats` gains `windows: {total, private}`: counting private windows is honest and reveals nothing.
  - **Existing rows (migration 005, one-shot redaction).** A stored window is treated as private and redacted when
    `raw_uri IS NULL` **and** none of its sightings has a `frame_uri` **and** (it has a sighting **or** its summary or
    transcript matches the private-cue pattern: sleep/asleep/sleeping bag/bunk/crew quarters/CQ/toilet/WHC/
    hygiene). Perception uploads the raw reply for every capturable window (AC-PER-13), so a window without a raw
    reply and without any stored frame is a no_capture window; the cue pattern only adds windows with no sightings
    at all (a sleeper alone is never sighted). Such rows get `is_private = true`, `privacy_reason =
    'legacy_redaction'`, null summary and transcript, `audio_events = '[]'`; their sightings' descriptions are nulled;
    descriptions of pipeline-created entities whose first sighting is in such a window and still equal that
    sighting's description are nulled; and `agent_memory` rows whose `source_from..source_to` span covers such a window
    are deleted (dreaming regenerates them without the text). The redaction runs only in the same transaction that
    adds the `is_private` column, so a later restart never re-applies it to new rows (new rows follow the ingest
    rule).
  - *Accepted.* Over-privacy: a capturable window that sights an object in a no_capture zone, or whose speech claim
    moves an item into one, loses its summary and transcript. Perception never places a capturable window's sighting
    in a no_capture zone (AC-PER-11 zones), so in practice this is the speech-claim case.
- AC-API-40 **`entity_kind` on events** (orchestrator request, round 3). Every event (`/events`, item detail
  `events`) carries `entity_kind` = the event entity's `kind` (`"object"` | `"person"`). A person track has no
  `entity_catalog_id`, so the agent and dashboard no longer guess. Optional and additive (OpenAPI `Event`).
- **Window hits expose `is_private`** (always `false`, because private windows are never returned). The agent's
  `search_observations` filters on exactly this name.

## UI review round 4 (docs/ui-review/round4: UI4-01, UI4-06, UI4-07, UI4-10)

All additions are optional ingest fields, optional response fields and migration `006_crop_privacy.sql`
(idempotent, applied at start like every migration). Existing clients keep working.

- AC-API-41 **No crop over a person, defence in depth** (UI4-01, P1; perception AC-PER-49).
  - **Ingest fields** on `sighting`: `person_boxes` (optional, at most 20 boxes `[x1,y1,x2,y2]` with values in 0..1;
    anything else is 422) and `person_overlap` (optional boolean).
  - **Rule.** A sighting *overlaps a person* when `person_overlap` is true, or it has `person_boxes` and an exemplar
    crop and either its `bbox` is null (the crop cannot be checked) or `bbox` overlaps any person box under the
    perception rule (`inventory_api.privacy.overlaps_person`: IoU > 0.05, or the intersection is > 10 % of the crop,
    or the crop covers ≥ 25 % of the top third of the person box).
  - **Effect.** For such a sighting the crop and the exemplar are **not stored** (no embedding, no `crop_uri`), the
    entity snapshot falls back to the frame, and `sighting.person_overlap = true` is stored. A review whose
    `image_uri` is that crop is stored with the sighting's frame instead (422 when the sighting has no stored frame:
    a review needs an image). The window is still recorded; nothing else changes.
  - **Person entities.** An exemplar or crop for an entity of kind `person` is never stored, in any window (a person
    crop is face-bearing; perception AC-PER-36 already sends none).
  - **Classify guard.** Exemplars now record their `sighting_id`. On `assign_catalog` / `new_catalog`, every exemplar
    of the review's entity whose sighting is `person_overlap` is deleted before the entity joins the catalog class,
    and the deleted ids are listed in the classify audit (`withheld_exemplar_ids`). A crop of a person can therefore
    never become a catalog exemplar.
  - **Item detail** sightings carry `person_overlap` and `is_private` (the sighting's window is private).
  - Migration 006: `sighting.person_overlap boolean not null default false`, `entity_exemplar.sighting_id bigint`
    (FK, on delete set null). Merge and unmerge keep the link.
- AC-API-42 **Event context, round 4** (UI4-06). Every event (`/events`, item detail `events`) also carries:
  - `entity_appearance`: the entity's description when it is a person track (its appearance text), else null;
  - `entity_last_location`, `entity_last_location_name`: the entity's current location and its display name;
  - **sighting place:** when an event other than `removed` / `consumed` (the entity leaves the inventory; a
    destination would be false) has no `to_location`, it is filled from the entity's sighting nearest to
    `occurred_at` within ±300 s (ties: the later one), with `to_location_name`. `location_source` is `"event"` when
    the stored event names a place, `"sighting"` when `to_location` was filled this way, null when no place is known.
    `location_role` is computed after the fill; a filled destination equal to `from_location` (or without one) is
    `"at"` (the entity was seen there; it did not leave), so a "seen" event always has its place and a person's
    "left X" is never shown while that person was sighted in X at the time.
- AC-API-43 **Segment-level items in /stats** (UI4-07). `by_module` also has a key for each segment (`USOS`, `ROS`)
  counting the live entities located at the segment itself (or below it but outside any module): the items perception
  could not place inside a module. `by_location_level = {module, segment, unknown}` counts live entities located in a
  module (zones included), at segment level, and with no (or an unknown) location. The three add up to the live
  entity count (`sum(by_kind)`), and `sum(by_module) = module + segment`.
- AC-API-44 **Why an item has no image** (UI4-10). Every Item carries `snapshot_withheld_reason`: `"private_window"`
  when it has no snapshot and at least one of its sightings is in a private window (its image was withheld for crew
  privacy), else null. Review subjects and candidates whose thumbnail is withheld because their location is
  no_capture (AC-API-27) also say `"private_window"`.

### Review round (AC-API-41..44)
- *Withheld crops are deleted.* Perception uploads a crop before the API can refuse it, and `/media` serves any key, so
  after commit every withheld crop that nothing references (exemplar, snapshot, review image, frame) is deleted from the
  bucket (best effort, logged). Test: `test_review_withheld_crop_is_deleted_from_the_object_store`.
- *Face boxes.* Ingest accepts `sighting.person_box_kinds` (one of "person" | "face" per person box, else 422); a face
  box's head zone is the whole box, as in perception. A person box must have x2 > x1 and y2 > y1 (else 422).
- *Event role.* A destination filled from a sighting is `"at"` when it is the origin, inside it, contains it, or in the
  same module (not only when equal): "moved from LAB1" sighted in LAB1S2, or "from USOS" sighted in NOD1, did not leave.
- *Migration 006* alters tables only inside a guarded DO block, so a restart takes no ACCESS EXCLUSIVE lock.
- *Accepted:* the Classify guard can only fire for a sighting flagged after ingest (e.g. by an operator purge), because
  ingest never stores an exemplar for a person_overlap sighting; legacy exemplars (no `sighting_id`, such as the one
  purged by hand in round 4) are not covered. The event place fill does not look at the window's privacy: locations
  are facts the API already exposes, never media.

## UI review round 5 (docs/ui-review/round5: UI5-05)

- AC-API-45 **Location downgrade hold** (UI5-05; amends AC-API-31; perception AC-PER-62).
  - *Found live:* person 28513d12 went NOD1 06:30 -> CUP1 06:31 -> USOS 06:32, and 44% of items ended at segment
    level: a later segment-only sighting (the handheld camera could not name the module) overwrote a module location.
  - *Rule (ingest, existing entities):* the sighting's location does not replace `last_location` when all of these
    hold: the sighting's location is outside any module (a segment); the current `last_location` is a module or inside
    one (zone, container); that module lies inside the sighting's location; and `seen_at -
    COALESCE(location_set_at, last_seen_at)` is between 0 and `LOCATION_DOWNGRADE_HOLD_S` (default 600 s, 0 = off).
    `last_seen_at`, confidence and snapshot still advance; `last_location` and its provenance are kept; the sighting
    is stored at segment level. A legacy row without `location_set_at` gets it frozen at the stand-in value, so the
    hold ends (otherwise every held sighting would advance `last_seen_at` and extend it).
  - A sighting in another segment, or in any module, moves the item as before (AC-API-31). After the hold, a segment
    sighting moves it. An operator-placed module is held too.
  - Tests: tests/test_ac_api_45_location_downgrade.py.
  - *Review round:* a naive `seen_at` is read as UTC (it raised a 500 in the hold check); nothing is held or frozen
    unless the sighting is newer than `last_seen_at` (the UPDATE would not apply).
