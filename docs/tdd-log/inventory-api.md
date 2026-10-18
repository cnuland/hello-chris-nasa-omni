# TDD log: inventory-api (`services/inventory-api`)

Spec: `docs/spec/10-inventory-api.md` (AC-API-1 to AC-API-23). Contracts: `contracts/db/*.sql`,
`contracts/openapi/inventory-api.yaml`.

## Red phase (2026-10-17, tester)

### Skeleton

- `pyproject.toml`: package `inventory_api`. Runtime deps are FastAPI, uvicorn, psycopg 3 (binary + pool), pgvector,
  boto3, python-multipart and pydantic 2. The `[test]` extra adds pytest, httpx, moto[s3] and ruff. Pytest skips
  `-m model` by default (this component has no model tests).
- `Containerfile`: base image UBI9 `python-311`. Build it from the repo root so `contracts/db` can be bundled as
  `migrations/`. At start it runs `python -m inventory_api.migrations`, then `uvicorn inventory_api.main:app` on port
  8080.
- Stubs (they raise `NotImplementedError`; no behaviour is implemented):
  - `inventory_api/main.py`: `create_app(settings=None, object_store=None)` and the module-level `app`. Every
    OpenAPI route is registered, plus `GET /stock`.
  - `inventory_api/config.py`: `Settings` dataclass and `Settings.from_env()`.
  - `inventory_api/storage.py`:
    - `ObjectStore` protocol, `S3ObjectStore(client, bucket)`, `StoredObject`
    - exceptions `ObjectNotFound` and `InvalidKey`
    - helpers `validate_key()` and `uri_to_url()`
  - `inventory_api/migrations.py`: `migration_files()`, `apply_migrations(url, dir)` and `main()`.
- Test infrastructure (`tests/conftest.py`):
  - **Database:** one `test_api_<hex>` database per session, built by executing `contracts/db/*.sql` directly. It is
    dropped at the end (the backends are terminated first). The admin URL can be overridden with
    `TEST_PG_ADMIN_URL`.
  - **Per-test reset:** before every test, all tables are truncated and `002_seed.sql` is re-applied.
  - **Object store:** moto `mock_aws` provides S3, bucket `inventory-test`.
  - **App client:** `TestClient(create_app(settings, S3ObjectStore(moto_client, bucket)), raise_server_exceptions=False)`.
  - **Data factories:** `make_entity`, `make_sighting`, `make_event`, `make_exemplar`, `make_review`, `make_window`.
  - **Assertion helpers:** `table_counts` (used for "nothing written" checks), `audit_rows`, and `ok(resp)`, which
    asserts the status before decoding JSON.

### Test command

```
cd services/inventory-api
uv venv --python 3.11 .venv && uv pip install --python .venv -e '.[test]'
.venv/bin/python -m pytest -q -p no:warnings
```

### Result

**273 tests collected: 272 failed, 1 passed, 0 errors** (about 23 s). `ruff check` is clean.

- The one pass is `test_containerfile_uses_ubi9_python311_and_uvicorn`, a packaging check for a file this phase
  creates on purpose.
- Two tests passed by accident in the first run and were tightened before the red step was closed:
  - `config_requires_database_url` passed because `NotImplementedError` is a subclass of `RuntimeError`.
  - `non_location_change_creates_no_event` passed because it never checked the status code.

Failure-reason histogram (from `--tb=line`):

| Reason | Count |
|---|---|
| `AssertionError: expected 200, got 500: Internal Server Error` (`ok()` helper) | about 156 |
| `assert 500 == 422` | 33 |
| `assert 500 == 200` | 32 |
| `NotImplementedError` (direct unit calls: storage, config, migrations) | 17 |
| `assert 500 == 201` | 16 |
| `assert 500 == 409` | 9 |
| `assert 500 == 404` | 5 |
| `assert 500 in (404, 422)` | 1 |
| `assert not True` (config test asserts the error is not `NotImplementedError`) | 1 |

There are no import errors, syntax errors, fixture errors or JSON-decode errors.

Sample of failure lines:

```
tests/test_ac_api_01_health_and_infra.py:21: assert 500 == 200
tests/conftest.py:215: AssertionError: expected 200, got 500: Internal Server Error
tests/test_ac_api_08_patch_item.py:93: assert 500 in (404, 422)
tests/test_ac_api_10_11_detail_history.py:66: AssertionError: assert 500 == 404
tests/test_ac_api_14_catalog_locations_events.py:75: assert 500 == 422
tests/test_ac_api_18_19_23_ingest.py:243: AssertionError: decision_missing_choice: 500 Internal Server Error
tests/test_ac_api_20_media.py:44: AssertionError: ('/media/frames%5C..%5Csecret.txt', 500)
tests/test_ac_api_20_media.py: NotImplementedError   (S3ObjectStore.put / validate_key / uri_to_url)
tests/test_ac_api_01_health_and_infra.py: NotImplementedError   (Settings.from_env / apply_migrations)
```

### AC coverage map

| AC | Tests | File | What is covered |
|---|---|---|---|
| AC-API-1 | 4 (+3 infra) | `test_ac_api_01_health_and_infra.py` | 200 `{status:"ok",db:true}`; 503 with `db:false` when the DB is unreachable (the app must still start); config from env; `DATABASE_URL` required. Infra: migration order, the runner builds schema + seed and is idempotent, Containerfile. |
| AC-API-2 | 12 | `test_ac_api_02_06_items_list.py` | `{items,total}` shape including `catalog_name` and `last_location_name`; empty inventory; default `-last_seen_at` with nulls last; all 6 sort values; nulls last for ascending sort too; limit/offset pages; `total` counts before pagination; offset past the end; default limit 50; invalid limit/offset → 422. |
| AC-API-3 | 7 | same | Each filter on its own (kind, repeatable status, catalog_id, inclusive min_confidence, seen_after/before window, needs_review true/false), and all filters combined with AND. |
| AC-API-4 | 6 | same | Self + descendants; multi-level (`USOS`); leaf does not match parent or siblings; **hierarchy, not string prefix** (trap location `NOD2X` under `USOS`); deeper custom container child; unknown location → empty 200. |
| AC-API-5 | 9 | same | Match on label (case-insensitive), description, catalog display_name and synonyms (`PGT`, `power drill`); no match; relevance ranking when no sort is given; explicit sort overrides relevance; SQL-injection safety; `%` is treated literally. |
| AC-API-6 | 3 | same | Merged/dismissed hidden by default (from both items and total) and from `q`; included when requested via `status`. |
| AC-API-7 | 16 | `test_ac_api_07_add_item.py` | 201 + Item; entity `created_by=operator`, status `confirmed`, `last_location`; image stored in S3 with its content-type; `snapshot_url` is served by `/media`; `added` event with source operator; audit row with actor; kind defaults to object. Returns 422 with **nothing written** (DB and S3) for: missing image, empty image, non-image content-type, non-image bytes declared as `image/png`, missing label/location_id, unknown location/catalog, bad kind. |
| AC-API-8 | 15 | `test_ac_api_08_patch_item.py` | Only provided fields change; clear catalog_id with null; attributes/needs_review; location change → `moved` event (operator, from→to); same location or a non-location edit → no event; audit before/after + note; 404; malformed id not 500; merged → 409 with nothing written; invalid status (`merged`, bogus), location, catalog or attributes → 422 with nothing written. |
| AC-API-9 | 13 | `test_ac_api_09_merge.py` | Source `merged` + `merged_into`; sightings, events and exemplars re-pointed; target last_seen advanced only when the source is newer; audit detail includes the moved sighting/event/exemplar ids and the prior status (undo); 409 for self, a merged target, an already-merged source, or different kinds; 404 for an unknown source/target; 422 when the target is missing; source hidden from the list afterwards. |
| AC-API-10 | 7 | `test_ac_api_10_11_detail_history.py` | Detail fields; sightings and events capped at 20, newest first; exemplar_count; last_moved_by/at come from the latest `moved` event (not `used`); Event contract fields + `evidence_url`; 404; malformed id. |
| AC-API-11 | 3 | same | Sightings + events + this entity's audit rows merged and sorted desc (other entities' audit rows excluded), with kind/at/location_id/image_url/summary; empty timeline; 404. |
| AC-API-12 | 5 | `test_ac_api_12_13_review.py` | Open items only, newest first; `image_url` on every item; `candidates` expanded as Items; status/kind/limit filters; invalid enum → 422. |
| AC-API-13 | 23 | same | assign_catalog (label = display_name, or an explicit label); new_catalog creates then assigns (bad id → 422, atomic); merge_into (AC-9 semantics, plus 409 across kinds); dismiss; every action marks the review resolved/dismissed with operator/time/resolution.action, clears needs_review, and writes audit with the operator; already resolved/dismissed → 409; second resolve → 409; missing/invalid payloads (6 variants) → 422 with nothing written; missing operator → 422; unknown review → 404; review leaves the open queue. |
| AC-API-14 | 30 | `test_ac_api_14_catalog_locations_events.py` | GET catalog (24 seed items with all fields); POST 201 (full and minimal, with defaults); duplicate → 409 (unchanged); 9 bad-id patterns → 422; pattern boundaries accepted; missing class/display name, bad category or unknown home_location → 422; GET locations (30, with hierarchy); events: all, newest first, entity/type/time-window filters, combined filters, limit, bad params → 422, unknown entity → empty. |
| AC-API-15 | 9 | `test_ac_api_15_16_search_stats.py` | Entity hits; window hits over scene_summary **and** transcript; full-text stemming; mixed types, each with type/id/snippet/time; merged/dismissed excluded; limit; `q` required (422); no hits; tsquery punctuation does not cause a 500. |
| AC-API-16 | 2 | same | by_status, by_kind, by_module (zones roll up to their module), open_reviews_by_kind (resolved reviews excluded); empty DB. |
| AC-API-17 | 15 | `test_ac_api_17_candidates.py` | Ranked by cosine similarity (values checked); Candidate fields; **max** over exemplars with one row per entity; top-k; default k = 10; catalog filter; merged/dismissed excluded; entities with no exemplars are not returned; bad embedding (wrong dimension, empty, non-numeric, nulls) → 422; missing embedding → 422; k ≤ 0 → 422. |
| AC-API-18 | 36 | `test_ac_api_18_19_23_ingest.py` | Full window recorded (window, new entity via ref, sighting with bbox, exemplar, event, review, decision_log); window-only payload; existing entity updated only when the sighting is newer (identity untouched); an older sighting is stored but does not roll the entity back; latest of two sightings in one payload wins; refs across several new entities; existing entity_id used in events/reviews; exemplar cap of 20 with the lowest quality dropped (including a low-quality newcomer); **26 invalid-part cases → 422 with every table count unchanged and the pre-existing entity untouched**; a late DB-level failure rolls back everything. |
| AC-API-19 | 7 | same | no_capture sighting stored without frame_uri; no entity snapshot and no exemplar crop_uri from a no_capture zone; an existing entity keeps its old snapshot; no_capture event has no evidence_uri; a no_capture review → 422 with nothing written (two variants); capture-allowed zones keep their URIs. |
| AC-API-20 | 23 | `test_ac_api_20_media.py` | Streams bytes with the right content-type (jpeg, png); missing → 404; 6 encoded traversal URLs are rejected even when objects with `..` keys exist in the bucket; `validate_key` rejects 7 unsafe keys and accepts normal ones; S3ObjectStore round-trip (URI form), not-found and traversal; `uri_to_url` mapping. |
| AC-API-21 | 8 | `test_ac_api_21_actor.py`, ingest file | Actor recorded for POST /items, PATCH, merge, resolve, POST /catalog and ingest (`pipeline`); a write without `operator` still gets a non-empty, non-`pipeline` actor. |
| AC-API-22 | 8 | `test_ac_api_02_06_items_list.py` | Bad status (single and among repeated values), kind, sort, min_confidence > 1 or < 0, needs_review, seen_after → 422, never 500. |
| AC-API-23 | 9 | `test_ac_api_18_19_23_ingest.py` | Stock insert; newer `seen_at` wins; older update ignored; zero count allowed; 4 invalid stock updates → 422 atomic; GET /stock list fields, filters by catalog_id and by location (with descendants, also combined). |

### Interface decisions made by the tests (the developer must follow them; the reviewer should check them)

The OpenAPI leaves these response bodies open, so the tests pin them down:

1. **App factory:** `create_app(settings: Settings, object_store: ObjectStore)`. The DB connection is lazy, so the
   app starts even when Postgres is down and `/healthz` returns 503 `{status: <not "ok">, db: false}`.
2. **URIs:** stored URIs are `s3://<S3_BUCKET>/<key>`. API URLs (`snapshot_url`, `image_url`, `evidence_url`,
   timeline `image_url`) are `/media/<key>`.
3. **`POST /ingest/window` response:** 201 with `{"window_id": uuid, "entities": {<ref>: <entity_id>}}`.
4. **`GET /search` response:** a list (or `{"hits": [...]}`) of `{type: "entity"|"window", id, snippet, time}`.
   `time` is the entity's `last_seen_at` or the window's `t_start`.
5. **`GET /stats` response:** `{by_status, by_kind, by_module, open_reviews_by_kind}`, each a dict of counts.
   `by_module` rolls zones up to their `module` ancestor.
6. **`GET /stock` response:** a list (or `{"items": [...]}`) of `{catalog_id, location_id, est_count, last_seen_at}`.
7. **Status codes:**
   - Unknown references in write bodies (location, catalog, entity in a payload) → 422.
   - Unknown path ids → 404.
   - Malformed path UUID → 404 or 422 (never 500).
8. **Audit `op` values:**
   - POST /items → `create`, with the entity id.
   - PATCH → `update`, with `detail.before` and `detail.after`.
   - merge → `merge`, with both ids in `entity_ids`. The detail lists the moved sighting/event/exemplar ids and the
     source's previous status.
9. **Merge last_seen rule:** the target's `last_seen_at` advances only when the source is newer (as the spec says).
   The tests do not require `last_location` to follow.
10. **Non-image check:** a non-image upload is detected by content-type **and** by magic bytes.

### Possible contract changes (not made; listed for the reviewer)

- `CatalogItem` (POST /catalog body) has no `operator` field, but AC-API-21 requires an actor for every write.
  `test_ac_api_21_post_catalog_records_actor` sends `operator` as an extra body field. Proposal: add an optional
  `operator` to the POST /catalog request body.
- `GET /stock` sits at the wrong indentation level in `inventory-api.yaml` (under `components`, not `paths`). It
  should move to `paths`.
- The response bodies for `/ingest/window`, `/search` and `/stats` are unspecified in the OpenAPI. Items 3–5 above
  should be added to the contract.

## Green phase (2026-10-17, developer)

### Result

**273 tests collected: 273 passed, 0 failed, 0 skipped** (about 26 s). `ruff check .` is clean and
`ruff format --check inventory_api` is clean (the tests were left exactly as written).

```
cd services/inventory-api
.venv/bin/python -m pytest -q -p no:warnings
```

### Test changes

None. No test was edited, skipped or weakened.

### Modules

| Module | Responsibility |
|---|---|
| `config.py` | `Settings.from_env()`. `DATABASE_URL` is required and raises `ConfigError` (which names the variable) when missing. Also reads the `S3_*` settings, `MIGRATIONS_DIR`, pool size and timeouts, `MAX_UPLOAD_BYTES` and `LOG_LEVEL`. |
| `db.py` | `Database`: a lazy `psycopg_pool.ConnectionPool` (dict rows) that opens on first use, so the app starts while Postgres is down. Also provides `ping()` for `/healthz` and `close()` on lifespan shutdown. |
| `deps.py` | `AppContext`, which resolves settings, the pool and the object store lazily. Also the `Ctx` dependency and `operator_actor()`, which never returns an empty actor or `pipeline`; it falls back to `unattributed-operator`. |
| `errors.py` | `NotFound` / `Conflict` / `Unprocessable` mapped to 404 / 409 / 422. A pool timeout or `OperationalError` becomes a 503. |
| `storage.py` | `S3ObjectStore` (boto3, path-style addressing for RustFS) with put, get and best-effort delete. `validate_key` rejects: empty keys, keys over 1024 chars, a leading `/`, `\`, control characters, and empty, `.` or `..` segments. `uri_to_url` maps `s3://<bucket>/<key>` to `/media/<key>`; other buckets and unsafe keys map to `None`. |
| `migrations.py` | Applies `contracts/db/*.sql` in lexical order, one transaction per file, under a Postgres advisory lock so that replicas starting together do not race. Runs as `python -m inventory_api.migrations`. |
| `schemas.py` | Pydantic request models that mirror the OpenAPI enums and patterns (details in the next section). |
| `repo.py` | Shared SQL: the Item SELECT (catalog name, location name and the latest `moved` event via LATERAL), the location-subtree recursive CTE, serializers, `write_audit` and LIKE escaping. |
| `merging.py` | `merge_entities()`, shared by `/items/{id}/merge` and review `merge_into` (details below). |
| `catalog_ops.py` | Catalog insert, shared by `POST /catalog` and review `new_catalog`. Uses `ON CONFLICT DO NOTHING` and returns 409 on a duplicate. |
| `images.py` | Magic-byte sniffing for png, jpeg, gif, webp, tiff and bmp. |
| `routers/` | `health`, `items`, `review`, `catalog` (catalog, locations, events and stock), `search` (search and stats), `media` and `ingest`. |
| `logging_config.py` | JSON-lines structured logging, installed at app startup. |

**`schemas.py` validation rules:**

- 768-dimension embeddings must be strict numbers, finite and not the zero vector.
- Exactly one of `entity_id` or `new_entity` per assignment.
- Exactly one of `entity_ref` or `entity_id` per event.
- Refs must be unique and must resolve.
- `t_end >= t_start`.
- Resolve-action payload rules are enforced.

**`merging.py` behaviour:**

- Locks both rows with `FOR UPDATE`.
- 409 for self-merge, an already merged source or target, or different kinds. 404 for an unknown id.
- Re-points sightings, events and exemplars and records their ids.
- Advances the target's `last_seen_at`/`last_location` only when the source is newer, and moves `first_seen_at` earlier when the source was seen first.
- The audit detail holds the source's previous status and review flags, the target's previous values, the moved ids and the note.

### Behaviour notes for the reviewer

- **Pool lifecycle:** the pool is per app and closed on lifespan shutdown, so each test's pool is gone before the
  next `TRUNCATE`.
- **`/healthz`:** pings with `HEALTH_TIMEOUT_S` (default 2 s). It returns 503 `{status:"degraded", db:false}`.
- **`/items`:** runs a count query, then a page-of-ids query, then fetches the Items, so the per-row LATERAL only runs
  for the page.
  - `min_confidence` is compared as `::real`, so an inclusive 0.7 matches a stored REAL 0.7.
  - `q` is an escaped ILIKE substring match on label, description, catalog display_name and synonyms.
  - Relevance order: exact label match, then label substring, then catalog name or synonym, then description. pg_trgm
    `similarity` breaks ties. An explicit `sort` overrides relevance.
- **`POST /items`:**
  1. Validates the image: content-type `image/*`, recognised magic bytes, non-empty and at most `MAX_UPLOAD_BYTES`.
  2. Checks that the location and catalog exist.
  3. Uploads to `items/<uuid>/snapshot.<ext>` with the sniffed content-type.
  4. Inserts the entity, the `added` event and the `create` audit row in one transaction. If the transaction fails,
     the uploaded object is deleted.
  - The entity gets `confidence=1.0`, `last_seen_at=first_seen_at=now()` and `created_by=operator`.
- **`PATCH`:**
  - The audit `before`/`after` holds only the fields that were provided, plus `note`.
  - The `moved` event's `actor_desc` is the operator.
  - Explicit `null` is rejected for non-nullable fields.
- **Review resolve:**
  - `assign_catalog`/`new_catalog` write a `classify` audit row. `new_catalog` also writes a `create` audit row for
    the catalog item.
  - `merge_into` writes one `merge` row. Its unknown target returns 422, because it is a body reference; the direct
    `/merge` endpoint returns 404, as the tests require.
  - `dismiss` writes a `dismiss` row.
  - A review whose entity has since been merged returns 409, except for `dismiss`.
- **`/ingest/window`:** one transaction.
  - **Pre-validation:** camera, every location, every catalog id, and existing entity ids. Existing ids are resolved
    through the `merged_into` chain to the surviving entity, so a window that races an operator merge is not lost.
  - **Database errors:** any remaining `IntegrityError`/`DataError` rolls everything back and returns 422.
  - **Entity updates:** only a newer sighting updates the entity (SQL guard `last_seen_at < seen_at`). The snapshot
    is the exemplar crop, else the frame, and is kept when both are absent.
  - **Exemplar cap:** 20 per entity. Ties drop the oldest.
  - **Reviews:** set `needs_review`/`review_reason` on their entity.
  - **Audit:** one `create` row per new entity with actor `pipeline`.
  - **Stock:** upsert with `WHERE stock.last_seen_at < EXCLUDED.last_seen_at`.
- **Privacy (AC-API-19):**
  - `no_capture` is inherited from ancestors. A container inside crew quarters is also private.
  - A sighting with no location uses the camera's location for the privacy check.
  - For private sightings, `frame_uri`, the exemplar `crop_uri` and the snapshot are dropped.
  - An event's `evidence_uri` is dropped if either `from` or `to` is private.
  - A review is rejected (422) when its location is private. When it has no location, it is rejected if any sighting
    of its entity in this payload is private.
  - Embeddings are still stored. They are vectors, not images, and dedup needs them.
- **`/ingest/candidates`:** an exact grouped scan, `max(1 - cosine distance)` per entity. At POC scale it does not use
  HNSW, because HNSW post-filtering can miss rows when a `catalog_id` filter is applied. `location_id` is accepted but
  not used yet.
- **`/search`:**
  - **Entity hits:** the same ILIKE matching, excluding merged/dismissed. `time` is `last_seen_at`, else `created_at`.
  - **Window hits:** `plainto_tsquery('english')`, which stems and is safe against punctuation. The snippet comes from
    `ts_headline` without markup.
  - **Ordering:** the two ranked lists are interleaved, then cut to `limit`.
- **`/stats`:**
  - `by_status` counts all entities. `by_kind` and `by_module` count only live entities (not merged or dismissed).
  - `by_module` walks ancestors to the nearest `module`.
  - Enum dimensions are zero-filled.
- **`/media`:** the decoded key is validated before S3 is called; an invalid key returns 400. Responses carry
  `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`, so objects uploaded as
  HTML cannot run script on the API origin.

### Packaging

- `pyproject.toml`: `packages` now includes `inventory_api.routers`. Without it, a non-editable install (the
  Containerfile's `pip install .`) would leave out the routers.
- Smoke test against a scratch `test_api_smoke` DB, dropped afterwards:
  - `python -m inventory_api.migrations` applied both files.
  - `uvicorn inventory_api.main:app` served `/healthz`, `/stats`, `/items`, `/search` and `/ingest/window`.

### Contract changes still proposed (not made)

The red phase proposed three changes: `operator` on the POST /catalog body, moving `/stock` under `paths`, and
documenting the response bodies for `/ingest/window`, `/search` and `/stats`. All three still stand.

Also proposed:
- `Item` has extra response fields (`merged_into`, `created_by`, `created_at`, `updated_at`, `parent_entity`,
  `ims_barcode`).
- `Location` has `no_capture`.
- `Candidate` has `kind`.
- Sightings in `ItemDetail` have `frame_url`.
- `POST /items/{id}/merge` returns `{merged_entity_id, target: Item, moved}`.
- `/media` returns 400 for an unsafe key.

## Review round 1 (2026-10-17, developer)

### Result

**336 tests collected: 336 passed, 0 failed, 0 skipped** (about 32 s), with `-m 'not model'` (no model tests in this
component). `ruff check .` is clean and `ruff format --check inventory_api` is clean. No `test_api_*` database is left
behind.

Process: first I added the 63 new tests in `tests/test_review_round_fixes.py` and ran them before changing any code:
55 failed and 8 passed. The 8 that passed are guard tests (for example "a public review is still accepted"). Each
failure was for the reported reason. For example, the race test found the sighting on the merged source
(`assert 1 == 0`), and the NUL tests got `500 == 422`. Then I fixed the code. No existing test was edited or weakened.

### Findings and resolutions

| # | Sev | Finding | Resolution |
|---|-----|---------|------------|
| 1 | high | Review privacy bypasses (non-private `location_id` wins; entity's no_capture `last_location`; no camera fallback; reuse of a dropped crop) | **Fixed.** New `_decide_privacy` in `routers/ingest.py`. A review is private if any of these is no_capture (ancestors included): its `location_id`; any sighting of the same target in the payload (with the camera fallback); the existing entity's stored `last_location` when the payload does not sight it; the camera location when the review names nothing else. A review whose `image_uri` is a frame, crop or evidence URI dropped for privacy in this payload is also rejected. Tests: `test_ac_api_19_review_*` (5). |
| 2 | med | Event evidence kept when there is no from/to; no camera fallback; person exemplars from no_capture zones stored | **Fixed.** An event is private if from/to, any sighting of its entity in the payload, or (when there are none of those) the camera location is no_capture, or if its evidence is a dropped URI. Exemplars of `kind='person'` from private sightings are no longer stored (new or existing entity). Object exemplars keep the embedding without the crop, because dedup needs it. This is a deliberate split, and it replaces the green-phase note "Embeddings are still stored". Sightings with no location use the camera's location (a test now covers this). Tests: `test_ac_api_19_event_*`, `*_person_exemplar_*`, `*_object_exemplar_*`, `*_private_camera_*`. |
| 3 | med | Ingest that only updates writes no audit row | **Fixed.** Every ingest writes exactly one `op='update'`, `actor='pipeline'` row. `entity_ids` lists every touched entity (created, updated, event and review targets). `detail` holds `{window_id, request_sha256, entities, created_entity_ids, updated_entity_ids, event_ids, review_ids, decisions, stock_keys, privacy_dropped_uris}`. The per-entity `create` rows stay. Test: `test_ac_api_21_update_only_ingest_writes_pipeline_audit_row`. |
| 4 | med | 500s for NUL bytes / halfvec overflow | **Fixed.** A global `psycopg.DataError` handler in `errors.py` returns 422; the transaction has already rolled back by then. This covers every query param and body field, including jsonb `\u0000` and the multipart label (the uploaded object is deleted on failure). `_check_embedding` rejects `abs(v) > 65504`. Tests: parametrized over `/items` q/location/catalog_id, `/search`, `/events`, `/stock`, PATCH label/attributes, POST /catalog, POST /items, and embeddings 1e6/-7e4/65520 (65504 is accepted). |
| 5 | med | Ingest racing a merge attaches rows to the merged source | **Fixed.** `_resolve_existing` locks the resolved survivors `FOR UPDATE` in `entity_id` order, the same order `merge_entities` uses, then re-checks status. If a merge committed while it waited, it resolves the chain again. I used `FOR UPDATE` rather than `FOR SHARE`: two ingests both holding SHARE and then UPDATE-ing the same row would deadlock. Test: `test_ac_api_18_ingest_racing_a_merge_attaches_to_the_survivor`. A real second connection holds an uncommitted merge, the test waits until the ingest shows up as a Lock waiter in `pg_stat_activity`, then commits. Five repeated runs were stable. |
| 6 | med | PATCH move overwritten by a late window | **Fixed.** When PATCH changes `last_location`, it also sets `last_seen_at = GREATEST(last_seen_at, now())`. Older pipeline sightings are still recorded but no longer move the item. A PATCH that does not move the item leaves `last_seen_at` alone. Tests: `test_ac_api_08_operator_move_*`, `test_ac_api_08_patch_without_location_change_keeps_last_seen`. |
| 7 | med | Merge ignores the exemplar cap and leaves references to the source; merge into a dismissed target allowed | **Fixed** in `merging.py`. After re-pointing, the merge applies the 20-exemplar cap with the same order as ingest. The deleted rows (id, original owner, `embedding::text`, quality, crop_uri, created_at) are stored in `detail.dropped_exemplars`. The merge also re-points `event.actor_entity`, `entity.parent_entity` and open reviews (`entity_id`). It replaces the source in open reviews' `candidate_ids`, de-duplicated with order kept, and records the previous arrays. It sets `needs_review` on the target when reviews move to it. The review that is resolving a `merge_into` is not re-pointed (`keep_review_id`). `target_previous` now includes `needs_review`/`review_reason`. Merging into a `dismissed` target returns 409. Tests: the cap, actor/parent, reviews/candidates, review-merge and dismissed-target tests. `test_ac_api_09_merge_detail_is_sufficient_to_undo` performs a full undo using only the audit detail and compares a snapshot of entity, sighting, event, exemplar and review state to the state before the merge. |
| 8 | med | No idempotency for `/ingest/window` retries | **Fixed without a contract change.** The request digest is SHA-256 of the canonical validated JSON. It is stored in the ingest audit row. An identical request replays the original 201 body and writes nothing. A transaction-scoped advisory lock on the digest serializes concurrent retries. Payloads that differ, even for the same window, are still recorded (the existing stock tests rely on this). Limitation: the lookup scans `audit_log`, which has no index on `detail`. That is fine at POC scale. **Proposed contract change** (not made): `observation_window.idempotency_key TEXT UNIQUE` (or a client-supplied `window_id`), optional in `IngestWindow.window`, so the lookup is indexed. Tests: `test_ac_api_18_identical_retry_is_not_duplicated`, `test_ac_api_18_different_payload_same_window_is_recorded`. |
| 9 | low | POST /items into a no_capture zone | **Fixed.** 422 (ancestors included, via `repo.location_no_capture`), checked before anything is uploaded. Test asserts no DB rows and no S3 objects. **Spec clarification proposed:** AC-API-7/19 should say that operator photos are refused in no_capture zones. |
| 10 | low | resolve records operator 'pipeline' | **Fixed.** Uses `operator_actor()`. Test added. |
| 11 | low | Raw image_uri passthrough / unvalidated URIs | **Fixed.** At ingest, `frame_uri`, `crop_uri`, `evidence_uri` and `image_uri` must be `s3://<bucket>/<safe key>` (checked with `uri_to_key`), else 422 before any DB work. `raw_uri` is not an image and is left alone. `_serialize_review` no longer falls back to the raw string. Tests: 16 parametrized cases plus the `javascript:` passthrough. |
| 12 | low | /media buffers whole objects; S3 errors become 500 | **Fixed.** `StoredObject` now carries a `chunks` iterator over `iter_chunks(64 KiB)` that always closes the body. `.data` stays as a convenience. `/media` returns a `StreamingResponse` with Content-Length when known. Any other `ClientError` (for example AccessDenied) raises `ObjectStoreError` and returns 502; a `BotoCoreError` (for example EndpointConnectionError) raises `ObjectStoreUnavailable` and returns 503; both are logged. Tests use a stub S3 client that asserts `read()` is never called and the body is closed. |
| 13 | low | Weak merge-undo / privacy tests | **Fixed** by the tests above: explicit `target_previous` values, a real undo round-trip, the camera fallback, the update-only audit and the race. |
| 14 | low | q not fuzzy; candidates ignore location_id | **q fixed.** `/items` and `/search` entity hits also match when `similarity(label, q) > 0.4` (`repo.FUZZY_LABEL_THRESHOLD`). For example, 'hamer' finds 'hammer'. I chose 0.4 over pg_trgm's default 0.3 to limit false positives; the reviewer's 'drlil' scores 0.2 against 'drill' and still does not match. Ranking is unchanged. **location_id on /ingest/candidates: rejected for now.** It stays accepted and reserved. Exact float ties are too rare for a tie-break to matter, and zone boosting belongs in the reconciler's decision model. **Proposed contract change:** document `location_id` as reserved in the OpenAPI. |
| 15 | low | Maintainability (subtree `.replace`, connection held during upload, blank label) | **Fixed.** `repo.location_subtree_sql(param)` builds the named-parameter CTE from one template. POST /items returns the validation connection before the upload and borrows a new one for the insert. A whitespace-only label returns 422 (test added). |

### Contract changes proposed this round (not made)

- `observation_window.idempotency_key` (UNIQUE) plus an optional `window.idempotency_key` in `IngestWindow` (#8).
- OpenAPI: `/ingest/candidates` `location_id` documented as reserved (#14). `/media` can return 502/503 (#12).
  `/items/{id}/merge` can return 409 for a dismissed target (#7). `/ingest/window` returns 422 for image URIs outside
  the bucket (#11). POST /items returns 422 for no_capture locations (#9).
- Spec AC-API-19 wording: person exemplars from no_capture zones are not stored; object embeddings are kept without
  crops (#2).

## UI-review contract additions (2026-10-17, developer)

Source: Playwright UI review `docs/ui-review/REPORT.md` and `findings.json`. The findings addressed here are:

- UI-03: duplicate cards cannot be decided (23 of 37 had no candidates)
- UI-04: actor shown only for moves
- UI-08: whole frames without a box
- UI-09: an edit resets "Last seen"
- UI-11: no review filters
- UI-12: history flooded with pipeline audits
- UI-13: events show only ids

Spec: AC-API-24 to AC-API-32 in `docs/spec/10-inventory-api.md`. Contracts: `contracts/openapi/inventory-api.yaml` (new
`LastHandled`, `ReviewEntity` and `ReviewCandidate` schemas, plus new parameters and fields) and the new
`contracts/db/003_location_provenance.sql`.

### Result

- **inventory-api:** 396 passed, 0 failed (336 before + 60 new in `tests/test_ac_api_24_32_ui_review.py`).
  `ruff check .` and `ruff format --check inventory_api` are clean.
- **e2e:** 25 passed (6 new in `tests/e2e/test_e2e_ui_review_contract.py`; one existing test changed, see below).
- **`make test`:** all suites pass.

### Process

1. Wrote the ACs first, then the OpenAPI changes.
2. Wrote 55 red tests. 54 failed for the intended reason:
   - `/review/{id}`: 404 (route missing)
   - `location_set_at`: `UndefinedColumn`
   - `X-Total-Count`, `last_handled`, `image_bbox`, `entity_label`, `count`: `KeyError`
   - filters: ignored, so the result sets were wrong
   - `sort=label`, `order=up`, `offset=-1`, `types=bogus`, `collapse=maybe`: 200 instead of 422
3. The one that passed is a guard (`limit=0` was already 422).
4. Went to green, then did the adversarial review below.
5. Added 5 guard tests from the review. They passed on first run because the green code already handled those
   cases; they stay as regression guards.

### Decisions (the dashboard relies on these)

- **Field names.** No field in the requested contract was renamed or left out.
  - A review candidate is the full Item (old fields such as `kind` and `snapshot_url` are kept) plus `thumbnail_url`,
    `sighting_count` and `similarity`.
  - `subject` has the same shape without `similarity`.
  - Additions beyond the contract:
    - `candidates_source` (`stored` | `suggested` | `none`), so the UI can label server suggestions
    - `location_set_at` and `location_set_by` on Items
    - `count`, `first_at` and `audit_ids` on timeline entries
- **`thumbnail_url`** is the entity's snapshot (a crop when one exists, otherwise its frame). Both it and
  `snapshot_url` are null when `last_location` is no_capture (ancestors included).
- **`image_url` can now be null,** but only for a legacy review stored with a no_capture `location_id`. Ingest has
  rejected such reviews since review round 1, so live data should never show this. The OpenAPI type is
  `[string, null]`.
- **Suggested candidates (AC-API-25):**
  - **Pool:** same `kind` (a merge across kinds is a 409 anyway) and same `catalog_id`, which is what perception's
    expected-count rule counts per class. Without a catalog_id: same normalised key of the label **or of the
    candidate's catalog class_name**. This is a small widening, so an unclassified "camera" also finds catalogued
    cameras.
  - **Order:** similarity to the subject's best exemplar (`quality DESC, created_at DESC`, the cap order), then same
    module, then most recent.
  - **Cost:** one SQL statement per page (LATERAL over the subjects). This is an exact scan of the pool's
    exemplars, fine at POC scale.
- **`similarity` for stored candidates** is computed the same way (null when either side has no exemplar).
- **`image_bbox`** comes from the review entity's sighting whose `frame_uri` equals the review `image_uri`, nearest
  `observed_at`. Perception sets the review image to `crop_uri or frame_uri`, so crop images get `null`: the crop is
  the object. A stored bbox outside 0..1, or with x2<=x1 or y2<=y1, gives `null`.
- **`GET /review`:**
  - Default order: `created_at DESC, observed_at DESC, review_id`, as before.
  - `limit` stays 1..500, and `offset` is new.
  - `q` matches the review description or the entity label, with LIKE wildcards escaped.
  - `X-Total-Count` counts the matches before pagination. The dashboard reaches the API through its same-origin
    `/api` proxy, so no CORS `Expose-Headers` is needed.
- **`GET /events`:**
  - `types` is validated against the event-type enum (422 on an unknown type) and ANDed with `type`; an empty
    `types=` is ignored.
  - The label and location-name joins also apply to `GET /items/{id}` `events`.
- **`last_handled`** is a second LATERAL in `ITEM_SELECT`, so every endpoint that returns Items has it in the same
  query (no N+1). It excludes operator-sourced `added` events.
- **Location provenance (AC-API-31)** replaces the GREATEST rule from review round 1, finding #6.
  - **PATCH:** a move sets `location_set_at = now()` and `location_set_by = <operator actor>`; it no longer touches
    `last_seen_at`.
  - **Ingest:** a sighting newer than `last_seen_at` still refreshes `last_seen_at`, confidence and snapshot. It
    moves the item only if `seen_at > COALESCE(location_set_at, -infinity)`, and then sets (`seen_at`, `pipeline`).
    Both conditions are read from the pre-UPDATE row.
  - **New entities:** a pipeline entity gets (`seen_at`, `pipeline`); an operator item gets (`now()`, operator).
  - **Merge:** the location follows the newer `COALESCE(location_set_at, last_seen_at)`. `last_seen_at` is still the
    max. `target_previous` records `location_set_at` and `location_set_by` for undo.
- **Migration 003:** `ADD COLUMN IF NOT EXISTS` (nullable, no default, so it is metadata-only and fast), then two
  backfills guarded by `location_set_at IS NULL`, so a re-run changes nothing.
  - **Backfill 1 (data repair):** finds items moved under the old rule. That PATCH wrote `last_seen_at = now()` and
    an operator `moved` event with `occurred_at = now()` in one transaction, so the two are exactly equal. For these
    items it records the move as provenance (by = the event's actor) and restores `last_seen_at` to the real last
    sighting. For operator-created items, `first_seen_at` also counts as seen.
  - **Live example:** `af8031d3` will show 22:57:14 UTC again, not 00:06:31Z.
  - **Backfill 2:** sets `location_set_at = last_seen_at` and `location_set_by = 'pipeline'`, or `'operator'` for
    operator-created rows.
  - **Test:** checked with the real runner against a 001+002 database holding data, upgraded twice.
- **History (AC-API-32):** I chose **collapse by default**, so the dashboard gets the fix with no change.
  `?collapse=false` returns the raw audit list.
  - A routine entry is `op='update'` by `pipeline` that did not create the entity and whose `event_ids`/`review_ids`
    contain none of the entity's events or reviews.
  - Each run of 2 or more consecutive routine entries in timeline order becomes one entry: newest `at` and `id`,
    `count`, `first_at`, `audit_ids`, and the summary "N pipeline updates (sighting refresh: last_seen_at/confidence)".
  - Every other audit entry has `count: 1`.
  - Operator update summaries now list the changes: `update by amy (last_location: LAB1S2 -> NOD1D1)`.

### Test changes

- **inventory-api unit tests:** none edited.
  - The GREATEST-rule tests from review round 1 still pass unchanged, because they assert only the protection
    (`test_ac_api_08_operator_move_not_overwritten_by_older_late_window`: the location stays) and
    `test_ac_api_08_patch_without_location_change_keeps_last_seen`. No test asserted that `last_seen_at` advanced.
  - The new AC-API-31 tests encode the replacement behaviour.
- **`tests/e2e/test_e2e_contracts.py::test_e2e_contract_review_candidates_expand_to_items`:** changed.
  - Before: `len(candidates) == len(candidate_ids)` for every review.
  - After: the equality is kept for `candidates_source != "suggested"`. For suggested candidates the test asserts
    `candidate_ids == []` and 1..8 candidates.
  - Reason: AC-API-25 intentionally returns server suggestions when nothing is stored.
- **`tests/e2e/test_e2e_pipeline.py`:** not touched (the perception agent's revisit change is kept).

### Adversarial self-review

| # | Concern | Finding / resolution |
|---|---------|----------------------|
| 1 | SQL injection in the new filters | All values are bound parameters. `q` uses `like_pattern` (wildcards escaped); `location` uses the subtree CTE; `types` values are checked against the enum, then bound as an array. `sort`/`order` are `Literal`-validated and only choose between fixed SQL fragments. NUL bytes return 422 through the global DataError handler. Guards: `test_ac_api_28_filters_are_not_injectable[q/location]`, `*_nul_bytes_*`, `test_ac_api_29_types_filter` (an injection string in `types` returns 422). |
| 2 | Privacy leaks via candidates or thumbnails | Suggestions can include entities whose `last_location` is in crew quarters or the WHC. Their `thumbnail_url` **and** `snapshot_url` are nulled, so the old Item field cannot leak what the new field hides. The subject gets the same treatment, and a legacy review in a private zone gets no image or bbox. `image_bbox` is coordinates only. Guards: `test_ac_api_27_*` (5 zones, stored and suggested, list and detail). **Not changed (out of scope):** `GET /items` still returns `snapshot_url` for an item an operator moved into a private zone. That snapshot was necessarily taken in a public zone, because ingest drops private frames. |
| 3 | N+1 queries | `_expand` runs a fixed 6 statements whatever the page size: private locations, suggestions, stored-pair similarity, image bbox, items with `last_handled`, sighting counts. Guard: `test_ac_api_25_review_queue_query_count_does_not_grow_with_reviews` counts `Connection.execute` calls for 1 and for 6 subjects (2 and 12 reviews). History adds 2 lookups only when there are pipeline audits. |
| 4 | Migration safety on the live DB | It is idempotent: `IF NOT EXISTS`, and the backfills only touch NULL rows. It runs in one transaction under the runner's advisory lock. The ALTER is metadata-only (nullable, no default), so it holds an ACCESS EXCLUSIVE lock only briefly. The repair matches only exact `occurred_at = last_seen_at` with an operator `moved` event, a fingerprint that only the old GREATEST PATCH produces. Pipeline moves are excluded (tested). **Deploy order:** the new API code reads the new columns, so 003 must run before the new pods. The image bundles `contracts/db`, but the `inventory-migrations` ConfigMap must be re-created (`scripts/bootstrap.sh`) so the migrate Job sees 003. An old pod still running after 003 would PATCH without setting provenance; this is harmless, because COALESCE falls back to `last_seen_at`. |
| 5 | Similarity semantics | The subject's "best" exemplar is chosen by quality, not by closeness. A test proves that a candidate identical to a low-quality subject exemplar does not rank first. |

### Notes for other components

- **Dashboard:** `TimelineEntry` gains optional `count`, `first_at` and `audit_ids`. `ReviewItem.image_url` is
  typed nullable.
- **Timestamp formats:** `POST /items` returns timestamps as `+00:00` while the other endpoints use `Z`. This was
  already the case and is unchanged; parse the values, do not compare strings. Seen in the new e2e test.
- **Agent:** nothing breaks. It can now use `GET /events?types=moved,used,handed_over` plus `entity_label` to answer
  "who used the drill" (UI-02).

## UI review round 2 contract (2026-10-18, developer)

Source: docs/ui-review/round2 (REPORT.md and findings.json). The spec additions are AC-API-33..38 in
docs/spec/10-inventory-api.md, plus the AC-API-25 order amendment. Perception's part is AC-PER-34
(docs/spec/20-perception.md, docs/tdd-log/perception.md).

### Result
- **inventory-api:** 396 -> **450 passed** (54 new tests in `tests/test_ac_api_33_38_ui_review_round2.py`); ruff is
  clean.
- **perception:** 754 -> **758 passed** (4 new `test_ac_per_08_bbox_source_*` tests). The task brief quoted 749,
  which was stale; AC-PER-33 had already added tests.
- **tests/e2e:** 2 new tests. 26 passed and 1 failed. The failure is `test_e2e_08_agent_refuses_an_ungrounded_location`,
  where the agent's reply now gives a grounded summary instead of "don't have a record". It comes from the agent
  changes in progress in services/agent (grounding.py and harness.py, not mine). Nothing in this change touches it.
- **make test:**
  - agent 501, eval 351 + 1 skipped + gates.
  - The dashboard had 1 failure in `tests/ui2_detail_overview.test.tsx`, a dashboard agent file that was being
    edited; it passes 22/22 when re-run alone.

### Process (red first)
- **Red:**
  - The first run of the new file gave 49 failed and 4 passed. The 4 that passed were guards: the label sort tests
    and two ranking guards.
  - **Label sort:** the local test DB is en_US.utf8, which already sorts case-insensitively, so it hid UI-16. The
    cluster's CNPG DB uses `C`. The fixture `c_collation` switches `entity.label` to `COLLATE "C"` for those tests
    and restores it afterwards. With it, the sort test is red as it should be.
- **First green:** 1 failure, which was a wrong expectation in the test. With 12+12 exemplars, the merge cap drops 4
  source exemplars, so only 8 are moved back and 4 are re-inserted. The test was fixed to assert both numbers.
- **Self-review, issue found:** an ingest that waited on the merge's row lock has a window `created_at` from before
  the merge. A "window created after the merge" rule would therefore miss its sighting.
  - **Fix:** the merge now records `sighting_watermark`, which is max(sighting_id) inside the locked merge.
    Ingest locks the target before inserting, so later sightings always have higher ids.
  - The comparison set of the target's "own" exemplars now excludes the probe exemplars.
  - **Proven red-then-green:** `test_ac_api_33_ingest_that_waited_on_the_merge_lock_still_counts_as_after_the_merge`
    fails when the watermark is ignored.

### Decisions (the dashboard codes against these exact names)
- **`POST /items/{id}/unmerge`:**
  - **Request:** `{operator (required, non-empty), note?}`.
  - **200:** `{unmerged_entity_id, target_entity_id, source: Item, target: Item, restored: {sighting_ids, event_ids,
    exemplar_ids, reinserted_exemplar_ids, actor_event_ids, child_entity_ids, review_ids, candidate_review_ids,
    reopened_review_id, target_fields}}`.
  - **Errors:** 404 for an unknown id; 409 as listed in AC-API-33; 422 for a bad body or id.
  - **Audit:** the row has op `split` (the schema's CHECK already allows it, so no constraint change is needed),
    with `detail.action = "unmerge"`.
- **Item:**
  - `merged_into: {entity_id, label} | null`, which **was a bare uuid**. No in-repo client read it. The agent's
    `get_item` passes it through, and an object is fine for the LLM.
  - `merged_at: datetime | null` and `merged_from: [{entity_id, label, merged_at}]`.
  - `snapshot_bbox: [x1,y1,x2,y2] | null` and `snapshot_bbox_source: "detector" | "omni" | null`.
- **Review:** `image_bbox_source: "detector" | "omni" | null`. Each candidate gains `score: number | null` (null only
  for a stored candidate when the review has no entity) and `match_reasons: string[]`. The vocabulary is fixed:
  `similar appearance`, `similar description`, `same zone`, `same module`, `seen around the same time`.
- **Item detail:** `sightings[].bbox_source`.
- **Events:** `location_role: "at" | "from_to" | null` and `actor_kind: "operator" | "person" | null`. History event
  entries carry both, and their summary reads "used at USOS by X".
- **Ranking:**
  - The score is computed in SQL with fixed weights (`review.py`: `W_*`, `REASON_*`).
  - `"similar description"` uses description-only trigram similarity. Same-catalog candidates share the label, so
    a label-inclusive score would always flag it.
  - Stored candidates get scores too, but keep the order the pipeline stored.
- **Unmerge, target fields:** a field is restored only if it still equals the value the merge wrote. That value is
  derived from the unchanged source row plus `target_previous`, so it also works for merges recorded before this
  change. This keeps later sightings and operator moves of the target (tested).
- **Unmerge, review merges:** the resolving review is reopened, since an undone decision should come back to the
  queue. Its previous resolution is kept in the audit.

### Adversarial self-review
| # | Concern | Finding / resolution |
|---|---------|----------------------|
| 1 | Unmerge clobbers newer target state | Fields are restored only while they hold the merge's value. Rows are moved back only `WHERE entity_id = target`. Candidate lists are restored only when unchanged. Tests cover an operator move after the merge and a new target sighting after the merge. |
| 2 | Sightings that belong to the source stay on the target | 409 when a post-merge sighting's exemplar is closer to the source's exemplars. The lock-wait race is closed by the watermark (tested). **Accepted:** omni-only sightings (no exemplar) cannot be attributed and stay on the target; the response lists what moved back. |
| 3 | Concurrency | Unmerge locks source and target `FOR UPDATE` in entity_id order (as merge does) and re-checks `merged_into` after the lock. **Residual:** an ingest that already resolved the source id to the target before the unmerge commits still attaches to the target. This is the same window as for merges, and the data stays valid. |
| 4 | N+1 | Merge links take one statement per `fetch_items` call. Candidate scoring replaces the old pair-similarity statement. `test_ac_api_25_review_queue_query_count_does_not_grow_with_reviews` still passes (equal counts for 1 and 6 subjects). |
| 5 | Migration on live data | `ADD COLUMN IF NOT EXISTS` (nullable, so metadata-only). The CHECK is added only if `pg_constraint` lacks it; it scans `sighting` once and old rows are NULL. `CREATE INDEX IF NOT EXISTS` for the merged_into partial index and the audit GIN index. It runs under the runner's advisory lock at API start. Tested: SQL run twice on a DB with data after dropping the column, and the real `apply_migrations` run twice on a 001..003 DB holding entities, sightings and a merge audit. |
| 6 | Float underflow | The time term is `0.2 / (1 + h)`, not `exp(-h)`, because Postgres raises on float underflow. |
| 7 | Privacy | `snapshot_bbox(_source)` is nulled with the thumbnail for review entities in no_capture zones (tested). Boxes are coordinates only. |

### Test changes
- **`tests/e2e/test_e2e_ui_review_contract.py`:** 2 tests added (new fields with OpenAPI validation; scored
  suggestions; a review merge, then unmerge, checked against `UnmergeResult` with the target restored and the review
  reopened). No existing test was edited.
- **inventory-api and perception:** no existing test was edited.

### Notes for other components
- **Dashboard:**
  - `merged_into` is an object.
  - `score` can be null only on stored candidates of an entity-less review.
  - Draw `image_bbox_source`/`snapshot_bbox_source` = `omni` dashed, and `detector` solid. Null means legacy/unknown.
- **Deploy:** 004 must run before the new pods serve. The API applies migrations at start; the image bundles
  `contracts/db`. If the migrate Job is used, re-create the `inventory-migrations` ConfigMap (`scripts/bootstrap.sh`).
  Perception can roll out before or after the API: an older API ignores the extra field (`extra="ignore"`).
- **Live data:** review 85c4f568 (`d34865e0...` into `99eba10e...`) can now be undone with
  `POST /items/d34865e0-e208-486f-a248-b8873bd92fc9/unmerge`. Its merge audit predates the watermark, so the
  window-time rule applies.
