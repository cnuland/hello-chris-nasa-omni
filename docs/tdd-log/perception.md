# TDD log: perception (`services/perception`)

Spec: `docs/spec/20-perception.md` (AC-PER-1 to AC-PER-18). Contracts: `contracts/schemas/observation.schema.json`,
`contracts/openapi/inventory-api.yaml` (`/ingest/*`, `/items`, `/catalog`, `/locations`), `contracts/db/*.sql`.

## Red phase (2026-10-17, tester)

### Skeleton

- `pyproject.toml`: package `perception`.
  - Runtime deps: numpy, Pillow, httpx, websockets, jsonschema, pydantic, imageio-ffmpeg (ships the ffmpeg binary)
    and boto3.
  - The `[model]` extra adds torch and transformers for OWLv2 / DINOv2. They are lazy-imported, and the model tests
    are marked `@pytest.mark.model`, which is skipped by default.
  - The `[test]` extra adds pytest, pytest-timeout, moto[s3] and ruff.
  - The real inventory-api (`../inventory-api`, editable) is also installed in the test venv. It serves two purposes:
    the AC-PER-14 end-to-end test, and payload validation in the fake inventory.
  - Console script: `perception`.
- `Containerfile`:
  - Base image: UBI9 `python-311`.
  - Build from the repo root, so that the observation schema contract is bundled into the package.
  - Runs `python -m perception replay --source "$VIDEO_SOURCE" --camera "$CAMERA_ID" --loop`.
- Stubs: every function or method with behaviour raises `NotImplementedError`. Only plain dataclasses and
  constructors are real:
  - `perception/types.py` (real dataclasses): `Keyframe`, `AudioChunk`, `Window`, `Detection`, `OmniResult`,
    `DecisionResult`, `AsrResult`, `LabelMatch`, `ReplayStats`.
  - `perception/config.py`: `PerceptionConfig` with the spec defaults; `from_env()` is a stub.
  - Pipeline modules:
    - `ingest.py` (AC-1)
    - `window.py` (AC-2)
    - `media.py`
    - `schema.py` (AC-3)
    - `omni.py` (AC-3), with `create_omni_client` choosing the client (AC-4 flag)
    - `realtime.py` (AC-4)
  - Model interfaces:
    - `detector.py` (AC-5), including `OwlV2Detector`
    - `embedder.py` (AC-6), including `Dinov2Embedder`
  - Mapping and resolution: `labels.py` (AC-7), `locations.py` (AC-11).
  - Reconciliation: `reconciler.py` (AC-8..11, 13, 16), `decision.py` (AC-12), `storage.py` (AC-13).
  - Audio and speech: `asr.py` (AC-15), `speech.py` (AC-16/17).
  - Service edges: `inventory_client.py` (HTTP client over the OpenAPI contract), `replay.py` (AC-18),
    `__main__.py` (CLI).
  - `fakes.py`: `FakeOmni`, `FakeDetector`, `FakeEmbedder`, `FakeDecision`, `FakeASR`, `MemoryStorage`,
    `make_frame`, `object_color`.
  - `perception/observation.schema.json`: a bundled copy of the contract. A test asserts it is identical to
    `contracts/schemas/observation.schema.json`.
- Test infrastructure:
  - **Media** (`tests/conftest.py`): media is generated per session with the imageio-ffmpeg binary:
    - a 6 s test pattern with 44.1 kHz stereo sine audio;
    - a video-only clip;
    - a local HTTP server for the URL-source case.
  - **`FakeInventory`** (`tests/support.py`): an in-memory `InventoryClient`. Every posted payload must survive a JSON
    round trip and validate against `inventory_api.schemas.IngestWindow`. The payload is then applied, so tests can
    span several windows. It supports:
    - candidates by cosine similarity;
    - `count_live_entities`, `list_items` and `patch_item`;
    - reviews setting `needs_review`;
    - stock upserts.
  - **Seed data:** parsed from `contracts/db/002_seed.sql`.
  - **Scene builders:** `scene_window` and `detections_for`.
  - **AC-PER-14 database:**
    - one fresh `test_per_<hex>` database per session, built from `contracts/db/*.sql` and dropped at the end;
    - each test truncates the tables and reseeds;
    - the real inventory-api app runs in-process (`create_app` + moto S3, served through FastAPI `TestClient`, which
      is an `httpx.Client`).

  The test infrastructure itself (`FakeInventory`, payload validation, the fake realtime WebSocket server and the
  multipart parser) was checked with a throwaway script before the red step closed.

### Test command

```
cd services/perception
uv venv --python 3.11 .venv
uv pip install --python .venv -e '.[test]' -e '../inventory-api[test]'
.venv/bin/python -m pytest -q -p no:warnings          # model tests: add  -m model  (needs '.[model]' + weights)
```

It needs the local Postgres (`postgresql://postgres:test@localhost:55432`; override with `TEST_PG_ADMIN_URL`) for the
5 tests in `test_ac_per_14_end_to_end.py`.

### Result

**334 tests collected (2 `model` tests deselected): 328 failed, 6 passed, 0 errors**, in about 8 s. `ruff check` is clean.

The 6 passes are interface constants or packaging that the skeleton defines on purpose:

- `test_ac_per_02_defaults`
- `test_ac_per_05_default_min_score_is_0_15`
- `test_ac_per_07_default_floor`
- `test_ac_per_18_config_defaults_match_spec`
- `test_ac_per_18_containerfile_runs_replay_cli`
- `test_ac_per_05_06_model_modules_import_lazily` (the stubs do not import torch/transformers; it must stay green)

Failure-reason histogram (`--tb=line`):

| Reason | Count |
|---|---|
| `NotImplementedError` (raised by a stub on the call path) | 324 |
| CLI subprocess exit code 1 instead of 0/2 (`main` raises `NotImplementedError`) | 3 |
| `AssertionError: {''}` (FakeEmbedder cross-process stability: the subprocess raised `NotImplementedError`) | 1 |

There are no import errors, syntax errors or fixture/setup errors. The ffmpeg media fixtures, the HTTP media server,
the `test_per_*` database and the in-process inventory-api all set up correctly; the AC-PER-14 tests reach
`HttpInventoryClient` / `replay.run` and fail there. No `test_per_*` database is left behind.

Where the failures are raised (top locations, `--tb=line`):

```
  73 perception/fakes.py:27: NotImplementedError      (make_frame: every scene-based reconciler/replay test)
  37 perception/labels.py:30: NotImplementedError     (CatalogMapper.map)
  29 perception/fakes.py:78: NotImplementedError      (FakeOmni.analyze / FakeDetector)
  21 perception/omni.py:40: NotImplementedError       (OmniClient.analyze)
  21 perception/locations.py:19: NotImplementedError  (LocationResolver.resolve)
  15 perception/window.py:26: NotImplementedError     (WindowBuilder.push)
  14 perception/decision.py:39: NotImplementedError   (DecisionClient.choose)
  11 perception/ingest.py:23: NotImplementedError     (iter_keyframes)
   9 perception/realtime.py:28: NotImplementedError   (RealtimeOmniClient.analyze)
   8 perception/reconciler.py:49: NotImplementedError (band)
   2 perception/storage.py:21: NotImplementedError    (frame_key)
   3 tests/test_ac_per_18_replay.py: assert 1 == 2 / CompletedProcess(...).returncode  (CLI)
```

Many reconciler tests stop at `make_frame` (the fake scene painter) before they reach `Reconciler.process`. As the
fakes are implemented, those tests will move on to fail in the reconciler itself.

### AC coverage map

| AC | Tests | File | What is covered |
|---|---|---|---|
| AC-PER-1 | 17 | `test_ac_per_01_ingest.py` | Default 1 fps; fps=2; RGB uint8 frames; timestamp = start_time + pts (tz-aware); strictly monotonic; 16 kHz mono int16 audio chunks (5 s default, short tail); chunk timestamps aligned; configured chunk length; decoded audio is not silence; loop keeps wall-clock monotonic past the media duration; `max_loops`; URL source over HTTP; video without audio → no chunks; missing file and non-video → `IngestError`; merged `ingest()` stream in time order; start_time offset. |
| AC-PER-2 | 17 | `test_ac_per_02_windows.py` | Defaults 6/4 s/448; emission every stride (first frame, then ≥ stride after the previous t_end); last-N frames in order; early windows are short; configured N/stride; irregular frame times; `flush()` tail and idempotence; landscape/portrait downscale to the long side; small frames are never upscaled; audio chunk with the largest overlap; no overlapping audio → None; sequential indexes. |
| AC-PER-3 | 24 | `test_ac_per_03_omni_client.py` | `POST /v1/chat/completions`; one JPEG data URI per frame; `input_audio` WAV (mono, 16 kHz, 16-bit, exact length); no audio part without audio; the system prompt lists catalog class names and the schema keys; `modalities`/`output_modalities` == ["text"]; first JSON object parsed from prose/fences/two objects; one repair retry (the extra user message mentions JSON) for invalid JSON and for schema-invalid JSON; two failures → `invalid` (exactly 2 requests); 6 schema-violation variants; HTTP/transport errors are reported, not raised; bundled schema == contract; `extract_first_json_object` edge cases; `validate_observation`. |
| AC-PER-4 | 14 | `test_ac_per_04_realtime.py` | Fake WebSocket server (`websockets.sync`): path `/v1/realtime?duplex=0`; `input_image` items as JPEG data URIs; audio appended as **24 kHz** PCM16 (length checked, content survives); order append < commit < response.create, images before response.create; text-only output (response or session level); deltas collected into a validated observation; server `error` event, invalid text and unreachable server → non-ok; `OMNI_REALTIME` flag selects `RealtimeOmniClient` (factory + env parsing). |
| AC-PER-5 | 11 (+1 model) | `test_ac_per_05_06_detector_embedder.py` | min_score 0.15 default; `filter_detections` keeps the boundary and drops below it; FakeDetector scripted per call / callable / colour-painted objects (bbox recovered, normalised), deterministic and filtered; `make_frame`/`object_color`; torch/transformers not imported by any module or constructor (subprocess check); `@model` OWLv2 smoke test. |
| AC-PER-6 | 7 (+1 model) | same | 768-d float32 unit vectors; same crop → same vector across instances; deterministic per object id regardless of crop size and background margin; distinct objects nearly orthogonal; stable across processes (hashlib, not `hash()`); `@model` DINOv2 same-crop test. |
| AC-PER-7 | 40 | `test_ac_per_07_label_mapping.py` | Floor 0.55; exact (case/space-insensitive), shared class names; 8 synonym cases; 5 fuzzy cases (score in [floor, 1)); 6 below-floor → UNKNOWN; embedding fallback via a TextEmbedder; embedding below floor → UNKNOWN; exact wins over embedding; custom floor; only catalog ids or UNKNOWN ever returned; a free-text label becomes an `unknown` entity with NULL catalog_id; the reconciler never calls a catalog-writing endpoint. |
| AC-PER-8 | 47 | `test_ac_per_08_reconciler.py` | `band()` boundaries (0.85/0.5, custom); `score_candidate` strong ≥ 0.85, weak < 0.5, a moved object (different zone, 60 s) ≥ 0.5, range [0,1], monotonic per factor; IoU; candidates requested with the crop embedding and catalog_id; new entity with exemplar/sighting/bbox/location; window fields; high band assigns without a decision; low band and no candidates → new provisional; middle band asks `same_or_new` over candidate ids + `NEW`, assigns at ≥ 0.6 (boundary included), otherwise provisional + `possible_duplicate` review (candidate ids, entity_ref, image, timestamp, probabilities); `NEW` choice; decision failure → new + review + logged `NEW`/{}; gates: class mismatch, impossible transit (not held / held / after 20 s / same module / configured), one entity per window (best keeps it); expected_count exceeded (existing, within the same window), not exceeded, merged not counted, NULL never, re-sighting not counted; UNKNOWN → `unknown` entity + `unknown_object` review (crop JPEG in storage under `crops/`, description, frame timestamp); a re-sighted unknown matches without a second review; linking by IoU (detector box wins) and by label; one assignment per object; invalid/error omni never written; post failure reported; empty window still posts the summary; ISO timestamps. |
| AC-PER-9 | 5 | `test_ac_per_09_10_fungible_events.py` | stock_update est_count = max per-frame count (3,5,2 → 5) and seen_at inside the window; no instances or entities for fungible classes; mixed fungible + instance; one update per class (synonyms aggregated); stock location follows location_hint. |
| AC-PER-10 | 14 | same | `moved` omni event → matched existing entity, actor_desc from `people[ref].appearance`, occurred_at = frame time, to/from location, source `pipeline`, evidence JPEG at `frames/<cam>/<date>/`; 4 other event types pass through with entity_ref; unknown actor ref → no actor_desc; event for an unobserved, unknown object dropped; out-of-range frame_index; unmappable to-hint → null; speech: "I'm putting the drill in node 2" → `moved`, source `speech`, existing drill, NOD2, confidence ≤ 0.6; the transcript argument overrides the omni transcript (PGT/Harmony); no catalog item or no location → no event; speech never creates entities. |
| AC-PER-11 | 33 | `test_ac_per_11_13_location_storage.py` | Resolver: ids, case, display names, "node two", Harmony/Destiny/US Lab/Columbus/Kibo/Zvezda/cupola, zone names; unknown hints → None; no_capture flags plus inheritance to children; `module_of`; `window_location` default/override; reconciler: camera default, hint override, unmappable hint, camera map from config; no_capture window: nothing stored (frames/crops/raw), no frame/crop URIs, no reviews, summary kept, no event evidence, no_capture camera default. |
| AC-PER-12 | 14 | `test_ac_per_12_decision_client.py` | `/v1/systemone` choice request (model, state, ordered criteria); parses choice/probabilities/latency_ms/model; list options; timeout, 500, 503, non-JSON, missing answer, unknown option, connection error → `NEW`, {} and needs_review; timeout setting propagated; chat fallback on systemone 404 (lettered options, logprobs → probabilities); chat mode request shape; content-letter fallback. Payload logging of decisions is asserted in AC-PER-8 tests. |
| AC-PER-13 | 8 | `test_ac_per_11_13_location_storage.py` | frame/crop/raw key layouts (UTC date, no `//` or `:`, unique per timestamp/index); a window uploads the frame at frame_index (JPEG, `image/jpeg`), the crop and the raw response; `raw_uri` is set; every payload URI is `s3://<bucket>/` and exists in storage; MemoryStorage URI; S3Storage with moto (content type); `S3Storage.from_config` from env. No-capture skipping is asserted in the AC-PER-11 tests. |
| AC-PER-14 | 5 | `test_ac_per_14_end_to_end.py` | Real inventory-api + `test_per_*` DB: a 20-window scenario (laptop ×10, drill moved LAB1→NOD2 with one omni `moved` event, unknown ×5) → exactly 3 non-merged entities, 1 `moved` event (drill, to NOD2), 1 `unknown_object` review (crop URI, description), sightings 10/10/5, 20 windows; HttpInventoryClient: catalog/locations, post_window + candidates, count/list/patch, 422 → `InventoryError` with nothing written. |
| AC-PER-15 | 11 | `test_ac_per_15_17_audio.py` | `POST /v1/audio/transcriptions` multipart (model, prompt with context, 16 kHz mono WAV); 4 failure modes → ok=False; context contains catalog names, synonyms, module codes and names, and is bounded; pipeline: ASR text is the stored transcript and the ASR gets 16 kHz chunks with context; omni fallback when ASR is unavailable or not configured; an empty ASR result is not replaced by the omni transcript. |
| AC-PER-16 | 31 | same | Rule fallback: 11 cases (statements, "?", "should I" without "?", interrogatives, auxiliaries, tag question, empty → other); the decision model wins (`speech_act` over statement/question/other); decision failure / no model → rules; `split_utterances`; claim parsing (4 positive, 4 negative); questions → no speech event (rules and decision model); confidence ≤ 0.6; entity flagged needs_review; a visual sighting in the claimed location within 10 min clears the flag (PATCH needs_review=false); after 10 min or in another location it does not; mixed utterances → only the statement. |
| AC-PER-17 | 13 | same | `is_silent` (zeros, low amplitude, loud, None, empty); hallucination: phrase repeated > 3 times (words, punctuation, sentences) vs exactly 3 times / normal / empty; `clean_transcript`; silent window skips ASR and discards text (including the omni text); no audio skips ASR; a hallucinated ASR transcript is not stored and makes no speech event; configurable VAD threshold. |
| AC-PER-18 | 25 | `test_ac_per_18_replay.py` | `replay.run` with an iterable source (stats frames/windows/posted/invalid/errors; omni and api calls per window); one entity across windows; config window settings; flush tail; omni receives downscaled windows; detector labels = catalog class names; audio items as int16 ndarray, float ndarray (converted to PCM16) and AudioChunk; invalid omni counted and not written; post errors counted; video path source; decision count; fakes (FakeOmni scripted dict/str/None/past-end, FakeDecision list/callable/exception/exhausted/default, FakeASR list/None/past-end/unavailable/callable); config defaults, env parsing, garbage → ConfigError; CLI `--help`, missing args → 2, unknown command → 2, `main()` builds collaborators from env and calls `replay.run` (asr None without ASR_URL), prints stats JSON; Containerfile. |

### Interface decisions made by the tests (the developer must follow them; the reviewer should check them)

1. **Window emission:** a window is emitted on the first frame, then on each frame ≥ `stride_s` after the previous
   window's `t_end`. It holds the last N frames; `t_start`/`t_end` are the first and last frame times. Audio is the
   chunk with the largest overlap. `flush()` emits the pending tail. Replay pushes an item's audio before its frame.
2. **OmniResult.status:** `ok` / `invalid` / `error`. `invalid` and `error` results are never posted:
   `Reconciler.process` returns `WindowOutcome(status="invalid")` and replay counts them in `stats.invalid`.
3. **Decision question names:** `same_or_new` (options = candidate entity id strings + `"NEW"`) and `speech_act`
   (options statement/question/other). A failed decision is `DecisionResult(choice="NEW", probabilities={},
   needs_review=True)`.
4. **Fake scenes:** `make_frame` paints objects in `object_color(id)`. `FakeEmbedder` keys on the dominant colour, so
   `vector_for_object(id)` predicts the crop embedding.
5. **Reconciler seams:**
   - `Reconciler(api=, embedder=, decision=, storage=, config=, camera_id=, catalog=, locations=, scorer=)`.
   - `process(window, omni_result, detections_per_frame, transcript=None)`.
   - The injectable `scorer(CandidateFeatures) -> p` lets band tests be deterministic.
6. **InventoryClient:** `get_catalog`, `get_locations`, `candidates`, `post_window`, `count_live_entities`,
   `list_items`, `patch_item`. `HttpInventoryClient` raises `InventoryError(status_code)` on non-2xx.
7. **Storage keys:** keys use the UTC date. URIs are `s3://<bucket>/<key>`, the form inventory-api validates.

### Contract notes (no contract was edited)

- **Camera → location:** there is no `GET /cameras` in the OpenAPI, although the `camera` table exists. Perception
  takes the mapping from config (`CAMERA_LOCATIONS`, default = the seed cameras). Suggested contract addition:
  `GET /cameras`.
- **Clearing speech-claim flags:** these use `PATCH /items/{id}` with `needs_review`. PATCH cannot set
  `review_reason`. Setting the flag is possible through a payload review or PATCH; the test accepts either.
- **`/ingest/window` ref resolution:** the end-to-end test relies on the response shape
  `{"window_id", "entities": {ref: id}}`, which the inventory-api tests pin down.

## Green phase (2026-10-17, developer)

### Result

```
cd services/perception
.venv/bin/python -m pytest -q -p no:warnings
334 passed, 2 deselected in ~4 s      # the 2 deselected are the @model OWLv2 / DINOv2 tests (need real weights)
.venv/bin/ruff check .                # All checks passed (repo-root ruff rule set)
.venv/bin/ruff format --check perception   # 23 files already formatted
```

- The AC-PER-14 tests ran against the local Postgres.
- No `test_per_*` database is left behind afterwards (checked with `pg_database`).

### Test changes

None. No test was edited, skipped or weakened.

### Implementation notes (for the reviewer)

#### Ingest (`ingest.py`)

- **Process model:**
  - A probe (`ffmpeg -i`) reads the stream layout and the duration.
  - One ffmpeg process decodes video (`-vf fps=N`, raw RGB24).
  - A second process decodes audio (`-ac 1 -ar 16000 -f s16le -t <duration>`). The `-t` clamp drops AAC padding,
    which would otherwise add a 14 ms fourth chunk.
- **Timestamps:**
  - Frame pts = `k / fps` plus a per-pass offset of the media duration, so loops stay strictly monotonic.
  - A pass boundary can never emit a non-increasing timestamp.
- **Shutdown:** subprocesses are killed when a generator is closed (for example `islice` on an endless loop).
- **HTTP sources without byte-range support:** the URL-source test uses Python's `SimpleHTTPRequestHandler`, which
  ignores `Range`. ffmpeg cannot reach the `moov` atom at the end of an MP4 served that way.
  - Fix: finite HTTP(S) files whose response has a `Content-Length` but no `Accept-Ranges: bytes` are spooled to a
    temp file first, capped by `PERCEPTION_MAX_SPOOL_MB` (default 2048).
  - Passed to ffmpeg unchanged: live streams (no Content-Length), HLS/DASH playlists and range-capable servers.
  - Known limitation: `ingest()` fetches such a URL twice, once for video and once for audio.

#### Window builder

- Emission follows tester decision 1.
- Downscaling uses PIL bilinear. The interiors of uniform rectangles keep their exact colour, which the colour-keyed
  fakes rely on after downscaling (AC-PER-14, AC-PER-18).

#### Omni (`omni.py`, `realtime.py`)

- **Shared system prompt:** catalog class names, rules, and the full schema JSON.
- **Repair retry:** appends the assistant reply plus a user message that lists the schema errors and asks for "ONLY
  the corrected JSON object".
- **HTTP or transport failure:** returns `error` right away, with no retry.
- **Realtime client:**
  - Sends `session.update` with text-only modalities.
  - Sends one `conversation.item.create` holding `input_text` plus the `input_image` parts.
  - Appends 24 kHz PCM16 audio in 1 s chunks, then sends `commit` and then `response.create` (text only).
  - Collects `response.output_text.delta` until `response.done`.
  - Returns `error` on a server `error` event, a timeout or a connect failure.

#### Label mapping

- Order: exact (class name or catalog id) → synonym → fuzzy → optional text embedding.
- The fuzzy score is `min(character-bigram Dice, difflib ratio)`, capped at 0.999. Both measures must agree:
  - "the" against "tape" scores 0.44 with Dice, while difflib alone gives 0.57.
  - "strange orb" against "stowage bag" scores 0.35, while difflib alone gives 0.64.
  - Spelling variants still score 0.73–0.87.
- The embedding fallback runs only when the fuzzy score is below the floor.

#### Locations

- Only exact matches on normalised aliases; there is no fuzzy matching.
- Alias sources:
  - the location id;
  - the display name, and its parts outside and inside parentheses;
  - "node N";
  - a small curated table of ISS module names (`ISS_ALIASES`: Harmony, Destiny, US Lab, Kibo, Zvezda, …).
- Number words are normalised ("node two" → "node 2").

#### Reconciler

- **Score:** `p = 0.70·sim + 0.15·class + 0.10·zone + 0.05·exp(−recency/1 h)`, clamped to [0, 1].
  - zone = same module.
  - A moved object (sim 0.98, other zone, 60 s) scores 0.885.
- **Gates run before scoring**, so an injected scorer cannot bypass them:
  - The class gate means UNKNOWN matches only unclassified entities. This lets a re-sighted unknown attach to its
    `unknown` entity with no second review.
  - The transit gate applies when `|Δt| < min_transit_s`, both modules are known and they differ, and the object is
    not held.
- **One entity per window:** objects are decided in descending order of their best p; each assignment removes that
  entity from later objects' candidate lists.
- **Middle band:**
  - Up to 4 candidates with p ≥ ask_p, plus `NEW`, go to `same_or_new`.
  - The choice is accepted when its probability is ≥ 0.6.
  - A confident `NEW` creates a new entity with no review.
  - Anything else creates a new provisional entity plus a `possible_duplicate` review (candidate ids and
    probabilities).
  - Every read, failures included, is logged in `decisions`.
- **Linking:**
  - An omni object first links to an unused detector box by IoU ≥ `link_iou` (0.3) in its `frame_index` frame. When
    `frame_index` is missing, all frames are searched, latest first.
  - Failing that, it links by mapped label; UNKNOWN objects link by raw label.
  - The detector box wins over the omni box.
- **Fungible stock:** `est_count` = the maximum per-frame count over the omni objects and the detector boxes of that
  class; `seen_at` = `t_end`.
- **Events:**
  - An omni event attaches to the window's assignment of that class, preferring the one held by `actor_ref`, and
    refers to it by `entity_ref`.
  - `from_location` = the matched entity's previous location; `to_location` = the resolved hint, or null.
  - `occurred_at` and the evidence frame come from `frame_index`, or from the last frame when `frame_index` is out of
    range.
- **No-capture windows (AC-PER-11):**
  - No uploads at all (frames, crops, raw response) and no reviews.
  - Unknown objects are skipped, because they could only be resolved from an image.
  - Known objects are still sighted, without URIs and with their exemplar embedding. This matches inventory-api's
    privacy rules, which keep object embeddings and drop the URIs.
- **Speech claims:**
  - Each utterance is classified (decision model `speech_act`, logged, with the rule fallback).
  - Only statements are parsed.
  - The target entity is the one sighted in this window, otherwise the most recently seen live entity of that class
    (`list_items`). A spoken claim never creates an entity.
  - Event confidence = `0.6 × P(statement)`, or `0.6 × 0.75` under the rules.
  - After a successful post, the entity is flagged with `PATCH needs_review=true`. The reconciler keeps a pending
    claim (`pending_claims`).
  - A later sighting inside the claimed location (the location or a descendant) within 600 s clears it with
    `PATCH needs_review=false`.
  - Claims expire after 600 s.
  - A claim that the same window also confirms visually is not flagged.
- **Storage:** raw responses are stored verbatim under `raw/…/<t_end>.json`. Crop keys use a per-reconciler sequence
  number, so crops from overlapping windows are never overwritten.

#### Replay

- **Setup:** catalog and locations are fetched once. The detector labels are the sorted catalog class names.
- **Detector cache:** detections are cached per frame, because overlapping windows reuse frames.
- **Transcripts:**
  - An audio chunk is transcribed only with the first window it is attached to. Later windows that reuse the chunk
    store `transcript=""`, so speech is never stored or mined twice.
  - Silent or missing audio → no ASR, and the transcript is `""`, discarding any omni text as well.
  - An ASR failure (`ok=False`), or no ASR at all → the omni transcript is used.
  - Every transcript goes through `clean_transcript` (consecutive-repeat hallucination filter, more than 3 repeats).
- **Catalog names for clients built before the catalog was known:** an omni client built with no catalog names (the
  CLI builds it before the catalog is known) gets them from `replay.run`.
- **Extra keyword arguments:** `run` accepts `loop` / `max_loops` for video sources (an extension of the AC-PER-18
  signature).

#### CLI

- argparse: `--help` → 0; usage errors → 2; a missing required env var → 2.
- Runtime failure → 1.
- Stats are printed as JSON on the last stdout line.
- Logs are JSON lines on stderr (`perception/logging_config.py`, `LOG_LEVEL`).

#### Model implementations

- `OwlV2Detector` and `Dinov2Embedder` import torch/transformers inside `_load()` and `detect()` / `embed()` only.
- OWLv2 boxes are post-processed against the padded square and then normalised to the frame.
- These paths are only exercised by the `@model` tests, which are skipped by default.

### Contract notes

No contract change was needed. The tester's suggestion still stands: add `GET /cameras`. Today the camera → location
map comes from `CAMERA_LOCATIONS`.

## Review round (2026-10-17, developer fix round)

### Result

- Command: `cd services/perception && .venv/bin/python -m pytest -q tests` → **380 passed, 2 deselected** (the two
  deselected tests are the `@model` OWLv2/DINOv2 tests). Before this round: 334 passed.
- `ruff check .` passes, and `ruff format --check perception` passes.
- The bundled `perception/observation.schema.json` is still byte-identical to the contract.
- No `test_per_*` database is left behind.
- The reviewer's probes `/tmp/perprobe/probe1.py` and `probe2.py` now show the fixed behaviour: no uploads, no speech
  events, and `DecisionError` instead of a raise.

### Process

1. Every finding first got a test in `tests/test_review_round_fixes.py`. 46 tests were added. 39 of them failed
   before the fix. The other 7 already passed and now pin current behaviour, e.g. the copula claim "the drill is in
   node 2 now" and the `["not","a","dict"]` chat body.
2. Then the code was fixed.
3. No existing test was changed or weakened.
4. The test infrastructure in `tests/support.py` was extended: `FakeInventory.list_events` (GET /events semantics,
   newest first) and the `needs_review` filter on `list_items`.

### Findings and resolutions

| # | Sev | Finding | Resolution |
|---|-----|---------|------------|
| 1 | high | A model hint can switch privacy off | **Fixed.** New `LocationResolver.window_scope(camera_location, hint) -> (location, no_capture)` fails closed:<br>• a no_capture camera stays private;<br>• a hint is accepted only inside the camera's own zone;<br>• otherwise the window is private when either the camera or the hinted location is private.<br>Tests: `test_ac_per_11_model_hint_cannot_switch_privacy_off_for_no_capture_camera[Node 2, NOD2, LAB1, Destiny, Columbus]` (no objects, no reviews, no raw_uri, location stays NOD2P5), and `..._no_capture_resolved_location_is_still_private`. |
| 2 | med | Camera missing from CAMERA_LOCATIONS → fail-open | **Fixed.**<br>• A camera with no known location is private: `window_scope` returns no_capture.<br>• The CLI exits 2 with a clear message when `--camera` has no location.<br>Tests: `..._unknown_camera_fails_closed`, `..._unknown_camera_with_hint_still_fails_closed`, `..._cli_refuses_camera_without_configured_location`.<br>Reading locations from the API needs `GET /cameras` (contract note below). |
| 3 | med | Speech claims ignore verb, direction and negation | **Fixed.** `parse_speech_claim` now requires all of:<br>• no negation;<br>• a placement verb or a copula before the location;<br>• the location after the item, directly after a destination preposition (in/into/to/at/on/…, optional "the").<br>"from X" and "out of X" are never destinations. With several destinations the last one wins. "'s" is expanded to "is".<br>Tests: 7 negative and 5 positive parametrised parser cases. Reconciler level: no event and no PATCH for "came from" or "used … in". |
| 4 | med | Claim confirmation only works inside one process | **Fixed.** `pending_claims` is removed and the claim state now lives in inventory. When a window sights an existing entity, `_check_speech_flags` does:<br>• `GET /items?needs_review=true` (one call, and only when the window has matched existing entities);<br>• for flagged entities without a `review_reason`: `GET /events?entity_id&type=moved`, then takes the latest `source='speech'` event.<br>Outcomes:<br>• Sighting inside the claimed location within `speech_confirm_s` → PATCH `needs_review=false` with a note.<br>• Sighting after the horizon → a `conflict` review with the sighting's image, so the operator has something to resolve. inventory-api then sets `review_reason='conflict'`, so the review is raised only once.<br>Tests: two reconcilers (two cameras) sharing one FakeInventory; restart; expired claim gives exactly one conflict review; the real inventory-api round trip (`..._cross_camera_confirmation_through_real_inventory_api`, Postgres + HttpInventoryClient, new `HttpInventoryClient.list_events`). |
| 5 | med | Clearing the speech flag also clears review flags | **Fixed.** A flag is cleared only when the entity's `review_reason` is null and a speech claim event exists. A flag set by a review item or a merge (reason set), or by an operator (no speech event), is never touched.<br>Test: `..._confirming_sighting_does_not_clear_flag_of_open_review`.<br>Not fixed: the PATCHes are still not atomic with the window and are audited as operator "perception". Making them transactional needs a contract addition (below). |
| 6 | med | Unknown objects can be duplicated | **Fixed:**<br>• UNKNOWN uses `k = max(candidates_k, unknown_candidates_k=50)` (the API allows up to 200).<br>• An UNKNOWN observation may match classified entities with `class_match=False` instead of a hard gate, so an entity NASA classified after review is still found. The decision model is asked in the middle band.<br>• Catalog and locations are reloaded every `catalog_refresh_s` (default 300 s, `PERCEPTION_CATALOG_REFRESH_S`, injectable `clock`). Replay then rebuilds the detector labels, the omni class names (CLI-built client) and the ASR context.<br>Tests: look-alikes crowding out the unknown; unknown label → classified entity; catalog TTL refresh; replay detector labels refresh. |
| 7 | med | No dedup of omni events across overlapping windows | **Fixed.** The reconciler keeps `(entity_id, event_type, to_location) → occurred_at` for recorded pipeline events, filled after a successful post with resolved ids. A repeat within `event_dedup_s` (default 10 s, `PERCEPTION_EVENT_DEDUP_S`) is dropped and extends the horizon.<br>Tests: two overlapping windows give 1 moved event, and a move 5 min later is recorded; replay with **default** windowing (6 frames, stride 4 s), where every window reports the move, gives 1 moved event. |
| 8 | low | ASR skipped when omni fails | **Fixed.** When omni fails, the window's audio is still transcribed and the transcript is carried into the next written window.<br>Test: `test_ac_per_15_speech_in_window_with_failed_omni_is_not_lost` (transcript kept, speech event produced).<br>Not done: concatenating all overlapping chunks for omni (prompt-size trade-off). |
| 9 | low | Decision client can raise IndexError and AttributeError | **Fixed.** The chat response shape is validated (choices, message, logprobs entries), and IndexError/AttributeError are also caught. A 404/405 from systemone is remembered, so later calls go straight to chat.<br>Tests: 5 malformed bodies; systemone requested once over 3 calls. |
| 10 | low | Ingest pts are counters, and the test is circular | **Deferred**, with reasons:<br>• Reading real pts needs `showinfo` parsing or ffprobe per frame, plus a/v start-offset alignment.<br>• That is a larger ingest change with live-stream implications, and the POC sources (files, synthetic iterables) start both streams at pts 0.<br>Proposed fix: probe per-stream `start_time` and offset audio and video by it. Test with an `-itsoffset` fixture. |
| 11 | low | Private windows store the transcript, utterances and person exemplars | **Fixed.** In private windows:<br>• `window.transcript = ""`;<br>• `speech_act` decision subjects omit the utterance;<br>• person assignments get `exemplar: null`.<br>Speech claims are still parsed, because item locations are not private.<br>Tests: `..._no_capture_window_stores_no_transcript_or_utterances`, `..._no_capture_window_sends_no_person_exemplar`. |
| 12 | low | Moved event with an unresolvable hint gets to_location=null; speech claim picks the wrong instance | **Partly fixed:**<br>• A speech claim's target now prefers the live entity whose last location is in the camera's module, else the most recent one. Test: `..._speech_claim_prefers_entity_near_the_camera`.<br>• **Rejected:** defaulting `to_location` to the sighting location. A "moved" object seen in this module may be carried *out* of it, so the camera location is not evidence of the destination. The existing pinned test `test_ac_per_10_event_to_location_hint_unknown_is_null` encodes null = unknown. |
| 13 | low | Credentials in logs | **Fixed.** `ingest.redact_source` strips userinfo, query and fragment. `_scrub` removes the raw URL and credentials from ffmpeg stderr in `IngestError` messages, and chained exceptions are dropped (`from None`). The replay start log uses the redacted source.<br>Tests: `test_redact_source_strips_credentials_and_query`, `test_ingest_error_does_not_leak_credentials`. |

### Contract changes suggested (not edited)

- `GET /cameras` (camera → location, no_capture). Perception could then read camera locations from the same table
  that inventory-api uses, instead of from `CAMERA_LOCATIONS`.
- `/ingest/candidates`: an `unclassified: true` filter. This would replace the larger-k workaround for UNKNOWN.
- Speech-claim flagging in `/ingest/window`, so it is transactional and attributed to "pipeline":
  - either `events[].flag_entity: true` with `review_reason='speech_claim'`, plus `assignments[].confirm_claim`;
  - or a `speech_claim` review kind.
  Today perception uses the operator PATCH (with notes), identifies speech flags as "needs_review with a null
  review_reason and a speech moved event", and escalates expired claims as `conflict` reviews.

## Integration phase (2026-10-17, integration engineer)

Details in `docs/tdd-log/integration.md` §1. All new tests were written first and seen failing for the right reason;
no existing test was changed.

| Change | Code | Tests |
|---|---|---|
| Camera-configured zones (ARCHITECTURE decision 5): `CAMERA_ZONES` / `PerceptionConfig.camera_zones`; an object's location is the zone containing its box centre (known, inside the window location, never `no_capture`), else the window location. | `config.py` `parse_camera_zones`, `reconciler.py` `_object_location` | `test_ac_per_11_camera_zones.py` (15) |
| Zone qualifiers in parentheses ("Node 2 crew quarters (port)") are no longer aliases. "port rack" had resolved to the private zone NOD2P5. | `locations.py` | `test_ac_per_11_16_generic_location_words.py` (14) |
| Identical twins: near-tied candidates (within `NEAR_TIE_P` = 0.02) are ranked location-first, then score, then entity id. Ties had been decided by the API's random-UUID order, so twins swapped between runs. | `reconciler.py` `_rank` | `test_ac_per_08_twin_tiebreak.py` (7) |

Spec follow-up proposed: mention camera zones in AC-PER-11 and the near-tie rule in AC-PER-8.

## Real-video dedup failure (cluster integration 2026-10-17)

Spec: AC-PER-19 to AC-PER-23 in `docs/spec/20-perception.md`. These also amend AC-PER-2, 3, 8 and 11. Tests:
`tests/test_ac_per_19_23_real_video_dedup.py` (196) and `eval/tests/test_ac_eval_05_omni_repeats.py` (7).

### What was measured on the cluster

The input was a real NASA public-domain ISS tour video, run through vLLM-Omni with Qwen2.5-Omni-7B.

- **Result:** after about 6 windows the DB held 10 `headset`, 7 `tablet`, 7 `camera` and 6 `laptop` entities,
  nearly all of them the same one or two physical objects.
- **Cause 1, omni repeats each object once per frame.** The real reply is saved as
  `tests/fixtures/omni_iss_tour_window0.json`. It lists `laptop` 6 times and `headset` 6 times (frame_index 0..5,
  identical boxes). The reconciler made one instance per entry, so one window created 6 laptops.
- **Cause 2, crops were cut from the 448 px omni frame.** The exemplar crops were under 1 KB: blurry hair, skin and
  shirt. DINOv2 cosine between same-label crops averaged 0.43–0.53, against 0.33 for different labels, so nothing
  ever reached the auto-merge band.
- **Cause 3, omni boxes are coarse.** They are identical across frames, yet they were still cropped and embedded
  when no detector box linked.
- **Cause 4, the hint was ignored.** "Columbus Laboratory Module" did not resolve, so everything was filed under
  the camera's LAB1.

### Red

| Step | Result |
|---|---|
| New perception file (before any code) | 142 failed, 47 passed |
| Existing suite at that point | 430 passed, untouched |

The 47 that passed are guards that already held: two side-by-side laptops stay separate, different classes stay
separate, and similar cases.

The failures were for the right reasons:
- the real fixture gave `{'laptop': 6, 'headset': 6}` instead of 1 + 1;
- an omni-only box got an exemplar;
- `Keyframe` had no `detect_image` and `WindowBuilder` had no `detect_long_side`;
- "Columbus Laboratory Module" resolved to None;
- `LocationResolver.window_policy` and `is_private_hint` were missing;
- the five new `PERCEPTION_*` config vars were missing;
- **a privacy leak:** hints such as "Node 2 crew quarters" or "Node 3 WHC module" did not resolve, so a
  `cam-handheld`, `cam-lab1-fwd` or `cam-nod2-aft` window stored crops, frames and the raw reply.

### Green

**Perception: 619 passed, 2 deselected. Ruff clean. No existing test changed.**

Implementation by file:
- `window.py` / `types.py`:
  - `Keyframe.detect_image` and `Keyframe.hires`;
  - `WindowBuilder(detect_long_side=1280)` keeps the original capped at 1280, never smaller than the omni image;
  - no copy is made when no resize is needed, and `detect_image` is None when it would equal `image`.
- `replay.py`: the detector runs on `frame.hires`; omni still gets `image`.
- `config.py`:
  - new vars `PERCEPTION_DETECT_LONG_SIDE` (1280; 0 = omni image; ≤ 4096), `PERCEPTION_MIN_CROP_PX` (32),
    `PERCEPTION_CONSOLIDATE_IOU` (0.5; ≤ 1), `PERCEPTION_NO_EXEMPLAR_MATCH_S` (600) and
    `PERCEPTION_HINT_CONFLICT_CONFIDENCE` (0.5; ≤ 1);
  - an upper-bound check for these vars.
- `omni.py` prompt:
  - "list each distinct physical object ONCE per window … frame_index = the frame where it is clearest";
  - `location_hint` = where the camera is, not a destination mentioned in speech.
- `locations.py`:
  - relaxed alias matching (filler words ignored, "laboratory" tried as "lab", ambiguous keys resolve to None);
  - `is_private_hint` (private resolution, or a core or curated private term);
  - `window_policy` → `WindowScope(location, no_capture, hint_conflict)`, with `window_scope` kept as a wrapper;
  - ancestor hints never coarsen the camera location.
- `reconciler.py`:
  - `_consolidate`: leader clustering by confidence, not transitive, fungible excluded;
  - two-pass linking: IoU links for every instance first, then label links. Before, an earlier object's label link
    could steal the box a later object overlapped. The best member is kept by: detector-linked, box agrees,
    detector score, omni confidence, later frame. `held_by` comes from any member;
  - `_exemplar_ok` gate: detector-linked and crop short side ≥ 32 px on `hires`;
  - `_match_without_exemplar` (class and place);
  - crops and evidence frames come from `hires`;
  - sighting confidence is capped on `hint_conflict`.

### Location-hint policy chosen (AC-PER-23)

- **Hint wins, for every capturable camera.** AC-PER-11 and the AC-PER-14 end-to-end scenario both depend on this:
  `cam-lab1-fwd` with hint NOD2 files the drill in NOD2. It is also what makes tour footage replayed on a fixed
  camera id land in COL1.
- **Fixed camera, hint in another module.** A fixed camera is one located in a module or zone. When its hint
  names a location in another module, the window is flagged `hint_conflict`: every sighting's confidence is
  capped at 0.5 and a warning is logged.
- **Roaming camera.** A camera located outside any module (`cam-handheld` → USOS) takes the hint without a flag.
- **Ancestor hints.** A hint naming an ancestor ("US segment") never replaces a more specific camera location.
- **Privacy only ever increases.** See the review findings below.

### Adversarial review: findings and fixes (test first)

Review red: 5 failed and 2 guards passed. Green after the fixes: **626 passed, 2 deselected**.

| # | Question | Finding | Resolution |
|---|---|---|---|
| R1 | Can consolidation merge two different same-class objects? | Side-by-side or touching laptops stay separate, and so do disjoint boxes in different frames (tests). Clustering is leader-based, so a chain A~B~C does not glue A to C (test). **Gap:** two *stacked* laptops listed for the same frame with omni boxes at IoU ≥ 0.5 merged. | **Fixed.** Same-frame listings whose boxes overlap stay separate when the detector found two distinct same-class boxes there, each best-matching one listing (`test_review_stacked_..._stay_separate`). A same-frame repeat with one detection is still one instance (guard). **Residual:** two different laptops where one listing has no box may merge, and so may stacked objects that the detector does not separate. A boxless listing has no location evidence, so this is accepted and documented in AC-PER-19. |
| R2 | Is full-res → normalised scaling correct under letterboxing or padding? | Both images are aspect-preserving downscales of the same frame, with no letterbox or padding. Normalised boxes agree within rounding: ≤ 1.5 px of the omni image for 16:9, 9:16, 1000x333 and 481x1999 (test). OWLv2 pads bottom/right to a square, and `OwlV2Detector` post-processes against the padded side then divides by the true w/h (unchanged, correct). Omni boxes are no longer cut into exemplars (AC-PER-22). Qwen's 28-px resize grid only affects omni boxes, which are used for consolidation, linking (IoU ≥ 0.3) and the sighting box. | No change needed. Crop tests use the hires array with scaled coordinates for 1280x720, 720x1280 and 333x1000. |
| R3 | Can a no_capture zone leak through the new hint path? | **Pre-existing leak, found by the red run.** Unresolvable private hints ("Node 2 crew quarters", "Node 3 WHC", "toilet") left the window capturable. The new relaxed matching also creates ambiguous keys (they resolve to nothing), and a parent-module hint from inside crew quarters ("Node 2") was never private. | **Fixed:**<br>• `is_private_hint`: a private resolution (exact or relaxed), any private claimant of an ambiguous relaxed key, or a private term (no_capture ids, derived core names "crew quarters" / "whc", curated terms) makes the window private for every camera kind, including a fixed camera whose hint names another module.<br>• The window is also private when omni's `scene_summary` names a private place.<br>Tests: every no_capture location (NOD2S5/D5/P5/O5, NOD3F4) by id and by "<display name> module", on USOS/LAB1/NOD2/COL1A1 cameras, and full reconciler windows store nothing (no frames, crops, raw or reviews; empty transcript). Speech claims also use the relaxed resolver; they never store images. |
| R4 | Memory and performance of full-res frames? | The builder deque holds ≤ `window_frames` keyframes, so hires memory is ≤ 6 × 1280² × 3 ≈ 30 MB, and `PERCEPTION_DETECT_LONG_SIDE` > 4096 is rejected. Frames at or below the cap are shared, not copied; small frames keep one array. The detection cache stores boxes only. Costs: one extra bilinear resize per frame (above 1280 only); larger evidence JPEGs (≈ 100–200 KB at 1280x720, against ≈ 30 KB); OWLv2 and DINOv2 compute is unchanged (they resize internally to 960 / 224). | Bounded by config validation. Test: `..._builder_retains_at_most_window_frames_hires_frames`. |
| R5 | (found while reviewing R1–R4) Do intermittent detector boxes still duplicate? | **Yes.** Windows with omni-only boxes create an entity *without* an exemplar, which is never an appearance candidate. The first window whose detector box links then created a second entity. On the cluster OWLv2 links were sporadic, so this was the common case. | **Fixed.** A detector-linked instance with no assignable candidate and no review tries the class-and-place match, excluding every entity it was compared with by appearance. Its exemplar becomes the entity's first. Tests: alternating omni-only and detector windows of the real fixture give 1 laptop + 1 headset; an entity judged different by appearance is never adopted (guard). |

Accepted trade-off (AC-PER-22): a sighting without an exemplar, or an adopting one, attaches to the most recently
seen same-class entity in the same module within 600 s. With two laptops in one module, a coarse sighting may
refresh the wrong one, but it never creates a duplicate.

### Eval (AC-EVAL-5 scenario "omni repeats objects per frame")

- **`GoldOmni` profiles.** `GoldOmni` now has `profile` = `repeat_per_frame` (the new default) or `once` (the old
  behaviour). The local target sets it from `EVAL_OMNI_PROFILE`, and an invalid value is a ConfigError.
  `repeat_per_frame` lists each gold object once per frame it is visible in, all with the same (latest) box, as the
  real model did.
- **Unit tests.** 5 new unit tests and 2 integration runs (synth-7 through perception, one per profile, strict
  gate). Red: 5 failed (no `profile`). The 2 integration runs already passed because perception was fixed first.
- **Proof that the gate catches the failure.** An ad-hoc run (not committed) with perception's consolidation
  disabled and `repeat_per_frame` gave `duplicate_rate = 5.0` and `pairwise_f1 = 0.27`, and the gate FAILED. With
  the fix both runs give `duplicate_rate = 0.0` and `pairwise_f1 = 1.0`.
- **Final runs.** `make eval` (synth-7, now `repeat_per_frame`): gate PASSED, with all gated metrics at 0.0 / 1.0.
  `make test`: all suites passed (inventory-api 336, perception 626, agent 336, eval 336 + 1 skipped + gates,
  dashboard 190, e2e 19).

### Test changes

None. No existing test was changed or weakened.

### Deployment notes

- `k8s/app/perception.yaml` sets `CAMERA_ID=cam-lab1-fwd` (the Podman unit uses `cam-handheld`). For the handheld ISS tour video set
  `CAMERA_ID=cam-handheld`, so that hints set the location without the low-confidence flag. On `cam-lab1-fwd` the
  tour still lands in COL1, but with sighting confidence ≤ 0.5.
- The new `PERCEPTION_*` vars all have working defaults. Optional: `PERCEPTION_DETECT_LONG_SIDE` (1280; raise only
  with memory headroom), `PERCEPTION_MIN_CROP_PX` (32), `PERCEPTION_CONSOLIDATE_IOU` (0.5),
  `PERCEPTION_NO_EXEMPLAR_MATCH_S` (600) and `PERCEPTION_HINT_CONFLICT_CONFIDENCE` (0.5).
- Entities already duplicated in the cluster DB are not merged by this change. They need operator merges or a DB
  reset.

## Omni output budget and truncation salvage (cluster integration 2026-10-17)

### Measured on the cluster

vLLM-Omni serving Qwen2.5-Omni-7B (fp16, TP2, T4 GPUs) on a real NASA ISS tour video. vLLM metrics over 53 window
requests:

- **Prompt:** about 2,200 tokens per request.
- **Output:** about **1,000 generated tokens** per request at about 28 tok/s, so a mean latency of about 36 s per
  window, against a 4 s stride.
- **Truncation:** **18 of 53 (34 %) finished with `length`** at `max_tokens` = 1500. The cut-off JSON failed
  validation ("omni reply invalid"), was retried (another ~55 s) and then dropped.
- **Cause:** a verbose model. The real reply (`tests/fixtures/omni_iss_tour_window0.json`) is pretty-printed, lists
  each object six times and echoes "No speech detected." as its transcript. That is 3,888 characters
  pretty-printed (≈ 1,100 tokens, matching the metric), 2,467 compact.

Spec: AC-PER-24 to AC-PER-28 in `docs/spec/20-perception.md` cover the prompt budget, config, truncation salvage,
the client-side object cap and usage logging.

### Red

New file: `tests/test_ac_per_24_28_output_budget.py`. The new names are imported lazily, so each test fails on its
own rather than the whole module failing at collection.

- **Result:** **76 failed, 1 passed** (the guard that an untruncated window keeps its confidence).
- **Failure reasons:**
  - `perception.salvage` missing (25);
  - ConfigError not raised for the new vars (14);
  - `cap_objects` / `interpret_reply` missing;
  - unexpected keyword arguments (`max_objects`, `asr_transcript`, `repetition_penalty`, `truncated`,
    `omni_max_tokens`);
  - 2 requests where 1 was expected (salvaged replies were still retried);
  - no `omni usage` log record.

  Every failure was a missing feature, not a test bug.

Cases covered:

- **Prompt:** caps, salience order, the 12-word description limit, attributes, 8 people and 8 events, compact
  single-line JSON with no prose or fences, key order with `location_hint` always present, and the ASR-dependent
  transcript rule. The realtime client shares the same prompt.
- **Config:** defaults, env parsing, and 14 bad values.
- **Request body:** `max_tokens` and `repetition_penalty`, which is absent when it is 1.0.
- **Salvage on real data:** the fixture serialised compactly and cut at 7 named offsets:
  - mid-string;
  - mid-object;
  - mid-bbox array;
  - just after a closing brace;
  - just after the following comma;
  - just before a closing brace;
  - inside the first object.

  Each salvaged result validates and keeps exactly the complete objects. An exhaustive test also cuts at every
  byte offset of the real reply.
- **Other salvage cases:** cuts in people, events, transcript and audio_events; prose and code fences before the
  JSON; a pretty-printed reply.
- **No salvage:** 8 cases with nothing usable or a private hint; a complete schema-invalid object.
- **`length` envelope:** salvaged with **no retry**. An unterminated reply with `stop` is also salvaged. An
  unsalvageable `length` reply keeps the repair path. Salvage also works on the repair attempt.
- **Reconciler:** the truncated flag caps confidence and logs a warning. Truncated or capped windows write no
  stock updates.
- **Usage logging:** one record per attempt, token sums, the truncated flag, and null usage.

### Green

New module `perception/salvage.py`:

- It walks the top-level object and decodes each value with `json.JSONDecoder.raw_decode`.
- Arrays keep only the elements that decode whole.
- A top-level number at the end of the text is treated as cut off.
- It requires a complete `scene_summary` and `location_hint`.
- It fills the empty fields, validates, and rejects a result with nothing useful in it.

Changes to `perception/omni.py`:

- `build_system_prompt(..., max_objects, asr_transcript)`.
- `cap_objects` and `interpret_reply`: the first complete object, capped and validated, else the salvaged prefix
  with `truncated` set.
- The client sends `max_tokens` = 700 and `repetition_penalty` (only when it is not 1.0), logs `omni usage`
  (window, attempt, prompt and completion tokens, finish_reason, latency_ms, truncated, dropped_objects) and
  never retries a salvaged reply.
- `create_omni_client` passes the config values; `asr_transcript` = `bool(ASR_URL)`.

Other changes:

- `OmniResult` gains the sidecar fields `truncated`, `dropped_objects`, `finish_reason`, `prompt_tokens`,
  `completion_tokens` and the `incomplete` property.
- `PerceptionConfig` gains `OMNI_MAX_TOKENS`, `OMNI_MAX_OBJECTS`, `OMNI_REPETITION_PENALTY` and
  `OMNI_TRUNCATED_CONFIDENCE`, with upper bounds and strictly positive checks.
- The realtime client uses the same prompt budget and `interpret_reply`. `FakeOmni` interprets scripted strings
  through `interpret_reply`.
- The reconciler logs a truncated window, caps its sighting confidence at `omni_truncated_confidence`, and skips
  stock updates for incomplete windows.

Green: **703 passed, 2 deselected**. ruff clean.

### Adversarial review: findings and fixes (test first)

Review red: **5 failed** (4 in the first round, 1 after the eval finding) **and 3 guards passed**. Two privacy
guards were also added and passed. Final: **713 passed, 2 deselected**.

| # | Question | Finding | Resolution |
|---|---|---|---|
| R1 | Can braces in strings, escaped quotes or unicode fool the salvage parser? | No. Only the JSON decoder decides where a value ends. Tests cover every byte cut of a reply whose strings hold `"{weird}" [label] }],{`, escaped quotes, backslashes, `Ü`, `✓` and an emoji, with `ensure_ascii` both True and False. A cut inside `Ü` drops that element. **Gap:** prose before the reply that looks like a key (`Note: {"a" is ...}`) made salvage give up at the first candidate. | **Fixed.** The parser tries the next `{"` candidate, and complete non-observation objects are stepped over (`test_review_salvage_skips_prose_...`, plus a guard). Residual: a raw control character inside a string stops parsing at that element (strict JSON), the same as the existing parser. |
| R2 | Can a cut-off `moved` event produce a wrong movement? | No. An element is kept only if it decodes whole. A test cuts at every offset inside a `moved` event, including just before its closing brace, and the event never appears. Events come after objects, so an event's object is always complete. **Gap:** the object *cap* could drop the only listing of an object that an event names. The reconciler then drops the event ("event for an object not observed"), so a real movement is lost. | **Fixed.** Instances named by events, or held by a person, are kept first (`test_review_cap_keeps_objects_referenced_by_events`, `..._held_objects`, `..._with_event_labels_argument`). |
| R3 | Does the cap drop the only instance of an unknown object? | The first version was label-diverse, so a lone unknown survived. **Gap found by the new eval profile:** a second, physically different object with the *same* label (two laptops) was crowded out by eight per-frame repeats of the first. The eval scored pairwise_f1 0.34 and sighting_recall 0.82. | **Fixed.** Diversity is per *instance*: same label with IoU ≥ 0.5, or a boxless listing, counts as one instance, as in AC-PER-19. Repeats are dropped first (`test_review_cap_keeps_every_distinct_instance_before_repeats`, plus overlap and boxless guards). The eval returned to 1.0 on every gated metric. **Choice:** unknowns get no extra priority over catalog labels. The client cannot run the AC-PER-7 mapping, and non-catalog labels are often clutter. An unknown's only listing is dropped only when the window holds more than 12 distinct instances. Documented in AC-PER-27. |
| R4 | Is any privacy path affected (no_capture: crew quarters, WHC)? | **Designed in, fail closed.** A reply cut before or inside `location_hint` is never salvaged, because it might have been about to name crew quarters (test: every cut inside "Node 2 crew quarters" gives None). The prompt orders the keys so that the hint comes second. The `scene_summary` privacy check needs a complete summary, which salvage requires. Guard: a salvaged reply with the hint "Node 2 crew quarters" or "Node 3 WHC module" on `cam-handheld` stores nothing (no frames, crops, raw or reviews; empty transcript). | No further change. |
| R5 | Found while reviewing R3: do truncated or capped windows corrupt stock? | **Yes, as first designed.** `stock` is overwritten with `est_count`, and a cut-off or capped list undercounts fungible items. | Incomplete windows write no `stock_updates` (tests for both flags). Residual (documented in the spec): a model that obeys the prompt budget in a cluttered scene can still under-list without any visible cap. |

### Eval (AC-EVAL-5 scenario "omni reply cut off at max_tokens")

- **New profile.** `GoldOmni` has a new profile `truncated` (`EVAL_OMNI_PROFILE=truncated`). It produces the
  `repeat_per_frame` reply plus a long transcript echo, serialised compactly and cut inside the transcript. The
  reply goes through perception's own `interpret_reply` with `finish_reason` `length`, so salvage and the cap are
  exercised by the real perception code.
- **Tests.** 4 new tests: profile known, reply cut and salvaged, profile from the environment, and an integration
  run against the strict gate that also asserts `invalid == 0`. Red: 4 failed (unknown profile).
- **The gate's catch.** The integration run then failed the gate (pairwise_f1 0.34). The cause was R3 above, fixed
  test-first. An ad-hoc diagnosis isolated it: without the cap every metric was 1.0, and the truncated flag alone
  made no difference.
- **Final runs.**
  - `make eval`, default profile: gate PASSED, every gated metric 1.0 / 0.0.
  - `EVAL_OMNI_PROFILE=truncated make eval`: gate PASSED, every gated metric 1.0 / 0.0.
  - `make test`: all suites passed: inventory-api 336, perception 713, agent 340, eval 340 + 1 skipped + gates,
    dashboard 190, e2e 19.

### Test changes

None of the existing tests was changed or weakened.

### Expected effect and deployment notes

- **Expected effect.** The measured ~1,000 tokens came from pretty-printing (≈ 35 % of the characters), six-fold
  object repeats and the transcript echo. The same window under the budget (compact, each object once, short
  descriptions, `transcript` "") is 938 characters, ≈ 250–300 tokens against ≈ 1,100.
  - **Worst case:** a reply that uses the full budget (12 objects with 12-word descriptions, 8 people, 8 events)
    stays under the 700-token cap.
  - **Typical case:** 2–6 objects per window should average well below 400 generated tokens, about 9–14 s at
    28 tok/s against about 36 s.
  - **Cut-off replies:** a reply that still hits the cap is salvaged instead of being retried and then dropped.

  These numbers are estimates until re-measured with vLLM metrics. Watch the `omni usage` log records.
- **Env vars.** `OMNI_MAX_TOKENS` (700), `OMNI_MAX_OBJECTS` (12), `OMNI_REPETITION_PENALTY` (1.05; `1.0` disables
  it) and `OMNI_TRUNCATED_CONFIDENCE` (0.5). All have working defaults, and no deploy change is required.
  `repetition_penalty` is a vLLM extension parameter; if a server rejects it, set `OMNI_REPETITION_PENALTY=1.0`.
- **Real time is still out of reach.** Even at ≈ 300 tokens a window takes ≈ 11 s against the 4 s stride.
  Matching the stride needs a longer stride, fewer frames, or more throughput.

## Within-window event repeats (cluster integration 2026-10-17)
- **Found live:** one omni reply produced three identical `used` events for one drill, with the same actor and frame
  and confidence 0.7/0.5/0.4. All three were stored, because `_is_repeat` compares only against previous windows.
- **Red:** 2 failed (the collapse cases) and 2 guards passed (different actors and different types stay separate).
- **Green:** `_collapse_window_repeats` post-processes `_visual_events`. The suite is 717 passed; ruff is clean.
- **Test changes:** none.

## Revisit dedup (cluster integration 2026-10-17)

Spec: docs/spec/20-perception.md AC-PER-29 to AC-PER-32. Tests: tests/test_ac_per_29_32_revisit_dedup.py and
eval/tests/test_ac_eval_08_revisit.py.

- **Measured (docs/QUESTIONS.md #29).** The full NASA ISS tour video ran in a loop on the cluster: 101 windows of 6
  frames, a 12 s stride, 8 modules. New entities per 10-window bucket:
  - windows 0–29: 19, 22, 16;
  - windows 30–79: 7, 6, 9, 5, 10;
  - windows 80–89: **22**, right after the video looped back to Columbus about 15 min later;
  - windows 90–100: 2, 3.

  The expected-count rule opened 35 `possible_duplicate` reviews, but it only flags duplicates afterwards.
- **Cause, verified in the code.**
  - *Omni-only sightings.* These have no exemplar (AC-PER-22). `_match_without_exemplar` dropped every entity last
    seen more than `no_exemplar_match_s` (600 s) ago, so a revisit created new entities.
  - *Detector-linked path.* Verified correct. `/ingest/candidates` (inventory-api) searches every live exemplar of
    the class, with no time or place filter. The impossible-transit gate in `_features` rejects only the candidate
    last seen in another module within `min_transit_s`. It never blocks the Columbus entity last seen 15 min ago
    (guard test). Two gaps remained:
    - the ask-band decision state had no `seen_ago_s` and no descriptions, because candidates carry none;
    - UNKNOWN labels compared `normalise_label` exactly ("spacesuit" ≠ "space suit"), and the lookup used free-text
      `q`.
- **Red.** 27 new perception tests: 20 failed for the expected reasons and 7 guards passed.
  - Failures: new entities on the revisit, `KeyError: 'seen_ago_s'`, missing `label_key`, and 3 `/items` queries
    for 3 laptops where 1 was expected.
  - Guards: the transit gate, another module, a coarse place, a recent candidate winning, a confident NEW, two
    distinct laptops by appearance, and two different unknown labels.
  - Suite at red: 20 failed, 724 passed.
  - Eval: 11 new tests, first red at collection (`RevisitResult` missing).
- **Green.**
  - `reconciler._match_without_exemplar` now uses one cached `GET /items?location=<module>` per (class key, module)
    per window. A recent candidate is handled as in AC-PER-22. A single stale candidate is attached. Several stale
    candidates go to `_decide_revisit`, which calls the decision model with descriptions, last places, times and
    `seen_ago_s`. Its fallback is the same-zone candidate, else the most recent, plus a `low_confidence` review;
    with `expected_count` reached, a confident NEW also takes the fallback.
  - `labels.label_key` is used for UNKNOWN consolidation, place matching and event targeting.
  - `_decide` context now includes `seen_ago_s` and descriptions (one `GET /items?catalog_id=` per class, only on an
    ask).
  - Suite: 744 passed.
- **Eval gate.**
  - `nasa_eval run` now replays the scene a second time, shifted by its duration plus `REVISIT_GAP_S` = 900 s,
    after scoring and QA. The first-pass metrics are untouched.
  - In the local target the gold detector misses every other catalog object on the revisit, so both perception paths
    are exercised.
  - `revisit_new_entity_rate` (new entities on the revisit / gold objects) is gated at ≤ 0.05.
  - The committed reference snapshot records a correct revisit (`revisit` block, 0 new). A report without a measured
    revisit has `None`, which the gate treats as a failure.
  - `make eval` with the pre-fix reconciler/labels: **revisit_new_entity_rate = 0.5** (3 of 6 objects duplicated: 2
    omni-only catalog objects and the unknown). Gate FAILED, and the eval integration test failed on the same
    metric.
  - After the fix: **0.0**, gate PASSED.
- **Adversarial review.**
  - *Wrong merge, fixed test-first.* The AC-PER-22 "recent" rule took the most recent candidate regardless of zone.
    In window 2 of a revisit both same-class objects had just been attached, so the twins swapped zones. Among recent
    candidates, the one in the observation's own zone now comes first. Red: 2 failed. Green: 749 passed.
  - *Wrong merge, accepted and documented in the spec.*
    - A single stale candidate is attached even from another zone of the module.
    - Adoption by a detector-linked instance now has no time limit.
    - A detector-linked ask band with the decision model down keeps AC-PER-8 (new entity plus `possible_duplicate`).
  - *Privacy, guards passing.* In a `no_capture` window (Node 2 crew quarters hint) the revisit attaches without a
    review or any upload. The decision subject holds ids, locations and times, never descriptions. In a capture
    window whose candidates were last seen in crew quarters, the review carries the Node 2 frame and the
    observation's own words only. Private zones are never sighting locations (`_object_location`).
  - *Robustness, guards passing.* With inventory-api down, the sighting stays new and the window is posted. A
    raising decision model leads to the fallback.
  - *Performance.*
    - One `/items` query per (class, module) per window, against one per omni-only instance before.
    - Live counts are cached per window.
    - Decision calls happen only when every candidate is stale (the first revisit window), so later windows match
      by recency.
    - `/ingest/candidates` is unchanged (one per detector-linked instance).
    - Accepted: UNKNOWN candidates are capped at the 200 most recent entities of a module.
- **Test changes.**
  - `test_ac_per_22_gated_sighting_after_the_horizon_is_new` now covers the case that keeps the horizon (a segment,
    `USOS`). Past the horizon a module now attaches its single candidate (AC-PER-29).
  - `FakeInventory.list_items` supports the `location` subtree filter, as inventory-api does.
  - eval `test_ac_eval_03_synth_perfect_per_frame_passes_repo_gate`: a perfect snapshot now includes a perfect
    revisit.
  - tests/e2e `test_e2e_11`: runs the revisit pass before gating, and asserts that it creates no entity.
- **Final runs.**
  - `make eval`: gate PASSED, with `revisit_new_entity_rate` 0.0 and every other gated metric at 1.0 / 0.0.
  - `make test`: all suites passed: inventory-api 336, perception 749, agent 340, eval 351 + 1 skipped + gates,
    dashboard 190, e2e 19.
- **Env vars.** None new. `PERCEPTION_NO_EXEMPLAR_MATCH_S` now bounds only the "recent" rule: past it, a module
  attaches its single candidate or asks the decision model, while a segment keeps the horizon.

## AC-PER-33 box sanity (Gemma 4 A/B, 2026-10-18)
- **Red:** 4 failed, 1 guard passed.
- **First green:** 1 regression. AC-PER-3's "3-number bbox is invalid" test failed, because the sanitiser nulled
  malformed boxes. **Resolved in favour of AC-PER-3:** only well-formed boxes are normalised, and malformed ones still
  fail validation.
- **Final:** 754 passed; ruff is clean. **Test changes:** none.

## AC-PER-34 bbox provenance (UI review round 2, UI2-07; owned by the inventory-api change)
- **Why:** the review image's box often framed an empty patch. It was omni's coarse box, because no detector box was
  linked, and nothing told the UI that the box was approximate.
- **Red:** 4 failed (`KeyError: 'bbox_source'`). The cases are: detector-linked by IoU, detector-linked by label,
  omni-only box, and no box.
- **Green:** `_assignment` sets `sighting.bbox_source`. It is `"detector"` when `o.detection` is set (the bbox is then
  the detector's box, see `_link`), `"omni"` when only omni's box exists, and null when there is no bbox. This is a
  one-line change.
- **Final:** 758 passed (754 before + 4). **Test changes:** none.
- **Contract:** `SightingIn.bbox_source` (`detector|omni|null`, optional) in inventory-api; migration
  `004_ui_review_round2.sql`.

## Person tracks from people[] and sleep-cue privacy (Gemma 4 run, 2026-10-18)
- **Why (docs/QUESTIONS.md #33):** Gemma 4 12B reports people only in `people[]`. The reconciler built person
  entities only from objects labelled "person" (Qwen listed both), so the Gemma run had 0 person tracks against 8–10
  with Qwen. The same run described "dark hair, sleeping in a blue sleeping bag" in Harmony and the window was stored.
- **Spec:** AC-PER-35 (person tracks from `people[]`, appearance-text matching through the AC-PER-29 class-and-place
  machinery), AC-PER-36 (person privacy), AC-PER-37 (sleep cues), AC-PER-38 (eval gates), plus a review-round section.
- **Existing code checked first:** `_assignment` already set `kind=person` from the catalog category, and a no_capture
  window already dropped person exemplars. But a detector-linked person object in a capture window was embedded
  (DINOv2) and its crop uploaded. **Decision (AC-PER-36):** person sightings never get a crop or an exemplar (a
  person crop shows the face, and its embedding is a re-identification vector). In a no_capture window no person
  sighting is recorded and `actor_desc` is null.
- **Red (perception):** tests/test_ac_per_35_38_people_privacy.py had 44 failed and 1 passed. The pass was the guard
  "no person class → people ignored". The failures were:
  - 13 × `ModuleNotFoundError: perception.people`;
  - person assignments missing (`IndexError` / empty lists);
  - `person_match_floor` missing from the config;
  - no `window.privacy` key;
  - sleep-cue windows still uploading frames and raw replies.
- **Green:**
  - New module `perception/people.py` with `sanitise_text`, `appearance_descriptors`, `appearance_similarity`
    and `sleep_cue` / `find_sleep_cue`.
  - In `reconciler.py`:
    - `_with_people` pairs person objects with people entries and creates people-only observations;
    - `_match_people` reuses `_place_items`, `_same_place`, `_attach_item` and the `no_exemplar_match_s` horizon,
      and assigns globally best first;
    - persons are never embedded (`_match_instances`);
    - the sleep cue is checked after the AC-PER-23 privacy rules, before anything is uploaded;
    - `window.privacy` records the private reason;
    - `actor_desc` is sanitised, and null in no_capture windows.
  - New setting `PERCEPTION_PERSON_MATCH_FLOOR`.
- **First green: 8 regressions, all expected.**
  - 7 tests in test_ac_per_19_23_real_video_dedup counted every live entity. The real Qwen fixture has one
    `people[]` entry ("man wearing black shirt and gray shorts"), which is now a person track. The helpers now
    count objects only. The six-window test also asserts that this man is exactly one person track across the six
    windows (real-data person dedup).
  - `test_ac_per_11_no_capture_window_sends_no_person_exemplar` now asserts the stricter AC-PER-36: no person
    assignment at all.
  - `FakeInventory` now stores `new_entity.description` (inventory-api returns it on `/items`).
- **Eval (AC-PER-38):**
  - **Red:** test_ac_eval_09 failed at collection (`PredWindow` missing).
  - **Changes:**
    - `GoldOmni` has a new `gemma_people` profile, now the default. It is `repeat_per_frame` objects plus every
      gold actor in every window, with varied text; an event's actor keeps the exact gold text.
    - In every profile, one quiet window lists the sleeping crew member, with the cue-free summary
      `SLEEP_SUMMARY`.
    - The snapshot now carries entity descriptions, sighting `window_id`/`frame_uri` and window rows.
    - New metrics, each with a threshold: `person_track_recall`, `person_tracks_per_person`,
      `sleep_window_media` and `sleep_window_person_sightings`.
    - The reference snapshot was regenerated. Object entities and sightings are unchanged; person tracks and
      windows were added.
  - **Expected test changes:**
    - the perfect-snapshot gate test gained person tracks and a sleep window;
    - two assertions about the default profile.
- **Integration finding (make test, tests/e2e):** the first draft put the sleeper in the window that starts at the
  scene start. That window also holds the transcript line "Starting the tool inventory survey", and
  `test_e2e_06`/`test_e2e_09` search for it. A private window stores no transcript, so 2 e2e tests failed.
  - **Spec amended:** the sleeper goes in the first *quiet* window, with no event, no unknown object and no
    transcript line within 6 s. The scorer finds that window by its stored summary.
  - Red: 6 failed; then green.
- **`make eval` before / after the perception fix** (perception reverted to HEAD, then restored; eval code is the same
  in both runs):
  - before: gate FAILED, with `person_track_recall` = 0.0 and `sleep_window_media` = 5 (10 with the first-draft
    window). `person_tracks_per_person` was 0.0 and `sleep_window_person_sightings` 0; both passed vacuously.
  - after: gate PASSED. `person_track_recall` = 1.0, `person_tracks_per_person` = 1.0, `sleep_window_media` = 0,
    `sleep_window_person_sightings` = 0. Every earlier gate is unchanged (1.0 / 0.0).
- **Adversarial review:** an independent read-only reviewer found 8 issues.
  - **Fixed, test-first** (perception `test_review_*`: 21 new, 20 red; eval: 1 new, red):
    - **P1 sleep-cue bypasses.** "sleepingbag", a soft hyphen or zero-width space inside "sleeping", and
      "sleeps"/"napping"/"dozing"/"sleep pod" were missed. `sanitise_text` now deletes format characters, and the
      cue list was extended.
    - **P1 lenient matching.** "dark hair" matched "blonde hair, red shirt". Colour families were added for
      dark/light.
    - **P1 tie steal.** "blue shirt, headset" took the entity "headset" (another person's) from its exact match. A
      Dice tie-break now applies.
    - **P2 unsanitised person-object descriptions.** Now sanitised.
    - **P2 clashing leftover pairing.** A red-shirt object was relabelled "green shirt". Clashing leftovers are no
      longer paired.
    - **P2 privacy not carried into the next overlapping window.** Fixed with `PERCEPTION_PRIVACY_HOLD_S` (default
      30 s).
    - **P3 parsing.** "white and blue shirt", "red haired … white shirt" and polo vs shirt are now handled.
    - **P3 eval.** No gate for a person sighting in the sleep window. Added `sleep_window_person_sightings`.
  - **Accepted (spec review round):**
    - lenient overlap against a short first description (the documented over-merge);
    - greedy rather than optimal assignment;
    - private windows keep their scene summary;
    - names in `appearance` are not stripped;
    - `person_track_recall` cannot see a track that absorbed another actor's sightings.
  - **Checked by the reviewer, no defect:** schema validation closes the crash routes; no person crop or exemplar
    is produced on any path; the cue check runs before any upload.
- **Final runs:**
  - perception: 824 passed (758 + 66); ruff clean.
  - eval: 365 passed + 1 skipped (351 + 14); ruff clean.
  - `make test`: all suites passed. inventory-api 450, perception 824, agent 510, eval 365 + gates, dashboard
    341, e2e 27.
  - `make eval`: gate PASSED.
- **New env vars:** `PERCEPTION_PERSON_MATCH_FLOOR` (0.5) and `PERCEPTION_PRIVACY_HOLD_S` (30).
- **Not done (outside this scope):** `/ingest/window` does not accept a person-entity link for events
  (`event.actor_entity` exists in the DB). Events still carry `actor_desc` only, which needs a contracts change.

## ISS module resolution (real-video eval, 2026-10-18; AC-PER-39..42)

Measured: `make eval-real` on the live Gemma 4 run scored `window_location_acc` 0.391 (9 of 23 scored windows).
No window had the wrong module; all 14 misses stayed at segment level (USOS).

- **Real data pulled (read-only).**
  - Windows were listed from `observation_window` via `oc exec … psql`, and every stored raw reply (48) was fetched
    through `/api/media/…`.
  - Fixtures:
    - `tests/fixtures/gemma4_tour/window_*.json`: 12 raw replies (Columbus, Kibo, Unity, Cupola, Zvezda, Nauka,
      and the "NOD 3", "To FGB", PMA-2 and Poisk hints).
    - `tests/fixtures/real_tour_run_2026-10-18.json`: the first 51 windows with hint, summary, `no_capture`,
      old prediction, and caption truth from `srt_truth` with the same alignment as `score_run`.
  - Findings from the data:
    - Kibo and Nauka had hints in official form, which did not resolve.
    - Gemma also writes signage as hints ("To COL", "To FGB", "NOD 3", "NoD3").
    - The Zarya windows have neither a hint nor a module name, so no resolution rule can fix them.
- **Spec:** AC-PER-39 (aliases and the mention scan), AC-PER-40 (summary fallback for roaming cameras), AC-PER-41
  (module carry, anchored to real evidence) and AC-PER-42 (real-data regression).
- **Red:** collection error (`ModuleCarry` missing), then with a stub 153 failed and 9 passed. The 9 passing were
  guards on existing behaviour.
- **Green:**
  - `locations.py`:
    - aliases (`ISS_ALIASES`), `RELAXED_STOP_KEYS`, `MODULE_MENTIONS` and `UNSEEDED_MENTIONS`;
    - `scan_modules`, `resolve_hint` and `is_roaming`;
    - `window_policy(camera, hint, summary)` with `source`, `elsewhere` and `mentions`;
    - `ModuleCarry`.
  - `reconciler.py`: the summary is passed to `window_policy`. The carry runs after every privacy decision (hint,
    summary private place, sleep cue, privacy hold). The payload gets `window.location` = {location_id, source,
    carried_from, gap_s}, and carried sightings are capped at `module_carry_confidence`.
  - `config.py`: `PERCEPTION_MODULE_CARRY_S` (default 45, 0 = off, max 600) and
    `PERCEPTION_MODULE_CARRY_CONFIDENCE` (default 0.5, ≤ 1).
- **Design decision: anchored carry, not chained.** The gap is measured from the last window that resolved a module
  itself, and carried windows do not extend it. `test_ac_per_42_chained_carry_would_assign_wrong_modules` shows why:
  chaining window to window carries "NoD3" (unscored) through 9 unnamed windows into the next pass's Columbus and
  Kibo windows, which would be wrong modules. At the live cadence (5 s every 30 s), a carry reaches one window.
- **Adversarial review:** an independent read-only reviewer found 9 issues.
  - **Fixed, test-first** (`test_review_*`: 44 new; 34 of the first 42 were red):
    - **P1 punctuation.** Mentions spanned commas and sentences, and number words became node numbers ("Inside
      the node, one crew member" gave NOD1).
    - **P1 JLP names.** "JEM Experiment Logistics Module" and "Kibo's logistics module" gave JPM1; "ELM-PS" gave
      nothing.
    - **P1/P2 refresh.** After a catalog refresh, the carry could go into a module that had become private or was
      removed.
    - **P2 contrary evidence.** A window mentioning another module was still carried.
    - **P2 fixed cameras.** Fixed cameras scanned hints ("Destiny visible through the hatch" moved the NOD2
      camera).
    - **P2 not-inside markers.** Outside, near, behind, in front of, out of, between and "… hatch" were missing,
      and "and" now takes the previous mention's marker.
    - **P3 common words.** "Unity is important", "Columbus Day" and "Leonardo da Vinci" resolved. These words now
      need module context.
    - **P3 case rule.** It applied to multi-word phrases ("the quest airlock").
    - **P3 config.** An infinite carry set in code was not treated as off.
    - **Privacy hardening.** "cq", "cqs", "quarters" and "hygiene area" are now private terms, because a hint such
      as "Harmony CQ" now resolves to NOD2.
  - **Accepted** (spec review round):
    - "through" stays neutral;
    - non-ISS uses of distinctive names ("service module of the car", "SM");
    - the signage hints "NOD 3" and "To COL" are not resolved;
    - a carry reaches one window at the live cadence.
- **Re-score (prediction; the live run still uses the old code until redeploy).** The saved replies were replayed
  through `window_policy` + `ModuleCarry`. Private windows stored no raw reply, so their hint is unknown and only
  their summary was used. Each window's sightings were relocated in a `score_run --export` file:

  | run | window_location_acc | correct / scored | unresolved | module_coverage |
  |---|---|---|---|---|
  | exported 41 windows, old code (the reported run) | 0.391 | 9 / 23 | 14 | 0.667 |
  | same 41 windows, new resolution | **0.696** | 16 / 23 | 7 | 0.889 |
  | 51 windows (run continued), old | 0.333 | 10 / 30 | 20 | 0.667 |
  | 51 windows, new | 0.633 | 19 / 30 | 11 | 0.889 |

  Confusion on the 41 windows, new (truth → predicted): COL1 {COL1 1, unresolved 1}; JPM1 {JPM1 2, unresolved 2};
  NOD2 {NOD2 2}; LAB1 {LAB1 1, unresolved 1}; NOD1 {NOD1 2}; CUP1 {CUP1 4}; ZARYA {unresolved 2}; ZVEZDA
  {ZVEZDA 2, unresolved 1}; NAUKA {NAUKA 2}. No window was assigned the wrong module.

  The 7 remaining misses:
  - 2 Zarya windows with no evidence;
  - 2 Kibo windows in pass 2, where the summary never names Kibo;
  - pass 2 Columbus ("To COL" signage, with no anchor before it);
  - pass 2 Destiny (a privacy-hold window: no carry into a private window);
  - the third Zvezda window (55 s after the anchor).
- **Final runs:**
  - perception: 1032 passed (824 + 208); ruff clean.
  - `make test` suites: inventory-api 450, perception 1032, eval 401 + 1 skipped with both gates PASSED, e2e 27.
    test-agent and test-dashboard fail on another agent's in-progress round-3 work (`nasa_agent.render` missing,
    `ui3_*` tests), in files this change does not touch.
  - `make eval`: gate PASSED, with every metric unchanged.
- **New env vars:** `PERCEPTION_MODULE_CARRY_S` (45) and `PERCEPTION_MODULE_CARRY_CONFIDENCE` (0.5).

## Private-window text redaction (UI3-02) — AC-PER-43, AC-PER-44 (2026-10-18)

- **Spec:** AC-PER-43 withdraws AC-PER-11's "summarised only". A private window's payload has `private: true`,
  `privacy_reason` (`private_zone` | `sleep_cue` | `privacy_hold`), `scene_summary`/`transcript` null,
  `audio_events` [], no cue word in `privacy` (field path only), null sighting and new-entity descriptions, and the
  catalog class as decision-subject `label`. AC-PER-44 (orchestrator request): a person track's own movement gets
  `actor_desc = null`.
- **Red:** `tests/test_ac_per_43_private_window_text.py`, 7 tests (sleep cue, private zone by hint and by summary,
  privacy hold, existing entity + same_or_new decision, AC-PER-36 re-check, capturable control), every one asserting
  that a marker word placed in all model text fields is absent from the whole payload. Then 1 test for AC-PER-44
  ("spacesuit, dark visor" on the person's own `moved`).
- **Green:** `reconciler.py` (`_build` window doc, `_assignment`, `_subject_label`, `_visual_events`).
- **AC-PER-36 verified:** no person sighting and `actor_desc` null in a private window (re-tested on the new path).
- **Existing tests edited (spec change, each marked AC-PER-43):** `transcript == ""` -> `is None` (3 places);
  `test_ac_per_11_no_capture_window_is_summarised` -> `..._is_not_summarised`; `privacy` no longer has `cue`; the
  reasons `location`/`scene_summary` are now `private_zone`.
- **eval (outside this component, minimal):** the scorer found the sleep-cue window by its stored summary, which is
  now null. `PredWindow.privacy_reason` (read from the DB when the column exists, exported only when set) and
  `_sleep_window_ids` also match `privacy_reason == "sleep_cue"`. Committed baseline unchanged; gate PASSED.
- **Counts:** perception 1040 passed (1032 + 8).

## Person-track over-counting — AC-PER-45..48 (2026-10-18)

- **Problem (measured live):** the Gemma 4 run of the NASA tour created 25 person tracks for 7 to 11 crew (USOS 17,
  NOD1 2, NOD3 2, ZVEZDA 2, COL1 1, CUP1 1). Person sightings matched only tracks in the same module.
- **Real data:** when this round started, the live `inventory` DB had 0 person entities: the perception pod had
  just been redeployed and the run restarted. The raw replies of the Gemma run were read-only downloaded from rustfs
  (`raw/cam-handheld/2026-10-18/*.878740.json`, 99 stored windows, 03:02:48 to 03:57:48; the tour loops about every
  15 min; private windows store no reply). Saved as `services/perception/tests/fixtures/gemma4_tour_people_2026-10-18.json`:
  time, people[] verbatim, the summary/hint/object labels, and per window the `module` that AC-PER-39..42 resolution
  gives it (72 of 99 still `USOS`).
- **Normaliser measured before deciding** (72 distinct real appearance strings, 2556 pairs):
  - 9 strings lost their shirt colour to "long-sleeve"/"short-sleeved" (two words between colour and garment), so
    "grey long-sleeve shirt, dark pants" vs "blue shirt, dark pants" = 1.00. The AC-PER-35 replay put
    "grey long-sleeved shirt, black pants" on a blue-shirt track.
  - "white spacesuit", "spacesuit, dark visor" were featureless (no descriptor). They attached to the most recent
    track, e.g. "spacesuit, dark visor" joined "white shirt, blue pants".
  - "orange helmet", "watch", "white harness" were ignored; "US flag" was not "American flag".
  - Already fine: t-shirt/tee/polo → shirt, flight suit/jumpsuit → suit, navy → blue, "dark blue" → blue, pose and
    activity words (floating, looking, holding) never count (now tested on the real strings).
  - The task's examples: "blue flight suit with American flag patch" vs "blue shirt, dark hair" 0.00 and vs "dark blue
    shirt" 0.00 (different garments); "blue shirt, dark hair" vs "dark blue shirt" 1.00.
  - Result, old → new normaliser: featureless strings 6 → 4; pairs ≥ floor 812 → 527; colour-clash pairs 738 → 996.
- **Spec:** AC-PER-45 (cross-module continuity, `PERCEPTION_PERSON_CROSS_MODULE_S` 600 s), AC-PER-46 (normaliser:
  sleeve length, tank top, spacesuit, helmet/visor/watch/harness, US flag, accessory weight 2), AC-PER-47 (soft cap
  at the catalog `expected_count`, `PERCEPTION_PERSON_SOFT_CAP`, default on; seed `crew-member` = 7), AC-PER-48 (eval
  crew transit pass, gate ≤ 1.3; real-data regression).
- **Red:** `tests/test_ac_per_45_48_person_continuity.py`, 51 tests: 29 failed, and 22 passed as characterisation
  (safety rules, pose words, below-cap behaviour). One AC-PER-35 test edited (marked AC-PER-45): a track in another
  module now has to be > 600 s old to stay separate. eval: `tests/test_ac_eval_09_crew_transit.py` (6), and the
  threshold assertion in `test_ac_eval_09_people_privacy.py` moved 1.5 → 1.3.
- **Green:** `config.py` (2 vars), `people.py` (phrases, vocabulary, weights), `reconciler.py` (`_match_people` split
  into `_person_candidates` + `_assign_people`; same-place and cross-module pools; an explicit colour-clash check in
  every tier, also for a floor of 0; the soft-cap pass only for sightings left over); eval `collaborators.py`
  (`crew_transit` GoldOmni profile, `transit_with_collaborators`), `runner.py` (transit pass after the revisit,
  `person_tracks_per_person` re-measured on the final DB, `counts.transit_new_person_tracks`), `thresholds.yaml`.
- **Eval, before → after (`make eval`, seed 7):** `person_tracks_per_person` **4.0 → 1.0** (gate ≤ 1.3: FAILED →
  PASSED; reports `20261018T043025Z-synth-7` and `20261018T043300Z-synth-7`). The 2 actors were 2 tracks in LAB1 plus
  one new track each in Unity, Columbus and Kibo. Every other metric is unchanged.
- **Real fixture re-scored offline** (the reconciler on cam-handheld, FakeInventory, live 30 s cadence; final code
  after the review round):

  | matching | person tracks | first seen at USOS | clashing pairs within a track |
  |---|---|---|---|
  | AC-PER-35 (old code, HEAD, old normaliser) | **37** | 23 | 51 / 288 |
  | new normaliser, same module only (cross-module 0, cap off) | 44 | — | 1 / 187 |
  | + cross-module 600 s, cap off | 35 | — | 4 / 317 |
  | cap only (cross-module 0) | 24 | — | 1 / 260 |
  | **defaults (cross-module 600 s + cap at 7)** | **22** | 10 | 4 / 430 |

  *Clashing pairs* is a purity proxy with no ground truth: pairs of sightings on one track whose appearances clash in
  colour. The old row's 51 comes mostly from the old normaliser dropping colours. The rows that isolate the new
  matching are rows 2 to 5. Before the review fixes, defaults gave 19 tracks with 32 clashing pairs (cross-module
  alone: 28). Cross-module matching cannot link the loop's passes (15 min > 600 s); the cap at 7 does. For example,
  the flag-patch flight suit is 1 track instead of 4. The offline 37 is higher than the live 25: the live run was
  measured partway through and on older module resolution.
- **Remaining over-merge (accepted, documented):** the track first described as "blue shirt, dark hair" takes 20
  sightings in 7 places ("blue shirt, orange helmet", "blue shirt, long hair" in the Cupola, ...). These are probably
  2 to 4 crew in NASA blue polos whose descriptions never clash.
- **Adversarial review** (an independent reviewer agent plus my own probes). Findings and fixes (test-first; red
  first unless marked):
  1. *High: cross-module matching multiplied clashing pairs inside tracks* (3 → 28, same normaliser). Weak tiers now
     need Dice ≥ floor plus `appearance_evidence` (a compatibly coloured shared garment, or a shared accessory or
     insignia). Each track's latest 5 sighting texts are remembered in process, and a clash with any of them blocks
     the match. Tested on the reviewer's repros and the fixture purity (≤ 5).
  2. *High/medium: vague or crafted descriptions absorbed everyone* ("astronaut in a shirt"; 8 colourless garments
     + watch through the cap; "watch" alone). Colourless garments and a watch are no longer evidence; watch weighs 1.
     My own probe: a "every colour, every garment" text scored 1.0 against all crew; more than 8 keys, or more than 3
     colours on one garment, now makes the text featureless.
  3. *Medium: the eval cannot see over-merging* (gate is a max; synthetic shirts always clash; 3 actors < 7, so the
     cap never runs). `person_track_recall` is now re-measured after the transit pass. Look-alike and cap scenes
     are left to the unit tests and the real fixture (documented in the spec).
  4. *Low: the cap took sightings that lost a candidate within the window* (contrary to the spec). Fixed: only
     sightings with no candidate at all.
  5. *Low: listing failures and the 50-row limit.* The warning text is fixed; the rest is documented as residual.
  6. *Low: "long sleeves", "american flags".* Fixed.
  7. *Low: the spacesuit test accepted "flight suit".* Tightened; it still passes.
  - Not red first (written after the fix, as regression): the terse cross-module under-merge, the latest-sightings
    clash test, the private-window memory check.
  - *Checked fine:*
    - Private windows record no person sighting and feed no memory.
    - No log line carries appearance text.
    - The new regexes are linear (10k chars in under 15 ms; text capped at 200).
    - Fullwidth "Ｕ.Ｓ." normalises; "bus flag" and "emus" are not mis-mapped.
    - The ranking matches the spec.
    - Two people of one window never share a track (in all passes and in the fixture).
    - The clash check holds with a floor of 0.
    - The eval transit pass is deterministic and runs last.
  - *Privacy:* appearance text only, never crops or embeddings of people (AC-PER-36 unchanged). The in-memory text
    is the stored sighting description.
- **Final runs:**
  - perception: 1110 passed (1040 + 70 new test cases, 48 functions in the new file); ruff clean.
  - `make test`: inventory-api 465, perception 1110, agent 560, eval 408 + 1 skipped (gates PASSED), dashboard 389,
    e2e 28.
  - `make eval`: gate PASSED (`person_tracks_per_person` 1.0, `person_track_recall` 1.0).
  - `make lint`: clean.
- **New env vars:** `PERCEPTION_PERSON_CROSS_MODULE_S` (600, 0 = off) and `PERCEPTION_PERSON_SOFT_CAP` (true).

## UI review round 4: crop privacy, structure filter, event places (AC-PER-49..52) (2026-10-18)

- **Spec first:** docs/spec/20-perception.md "Amendments: crop privacy, structure filter, event places". Root cause of
  UI4-01, read in the code: in `_link` pass 1 an omni box could IoU-link to a detector box of **any** label, including
  the detector's own `person` box (the catalog class name "person" is already an OWLv2 query), and that detector box
  was then cropped, embedded and uploaded.
- **Red:** tests/test_ac_per_49_52_crop_privacy_structure.py, 83 failing for the right reasons (missing
  `perception.privacy` / `is_structure_label`, crops uploaded over people, "left USOS" events emitted, structure
  entities created); 3 passing (geometry helpers that exist already).
- **Test edits before green (no weakening):** a geometry case of mine was wrong (the "leg sliver" crop covered 25 % of
  the crop, so the 10 % rule correctly fires; the sliver was narrowed); the drill-move test needed an exemplar to reach
  the existing entity; my count of distinct real unknown labels was wrong (8, not 9; 7 removed, not 8).
- **Green:** `perception/privacy.py` (overlap rule, body labels), `labels.label_heads` / `is_structure_label`,
  reconciler (structure + body filtering before linking, person regions per frame, person/face detections link only to
  person instances / nothing, crop dropped with `person_overlap`, `person_boxes`, window statistics, event places),
  replay (queries `person` + `face`, `ReplayStats.structure_filtered/person_crops_dropped`), prompt.
- **Existing test amended (justified):** `test_ac_per_10_event_to_location_hint_unknown_is_null` asserted
  `to_location is None` for an unresolvable hint. AC-PER-51 now fills the sighting place (LAB1); the test still proves
  the hint text never becomes a location.
- **Denylist on the real Gemma 4 replies** (gemma4_tour/ + gemma4_tour_people_2026-10-18.json):
  19 listings map to UNKNOWN; **17 (89 %) are removed** (hatch 7, spacecraft 3, window 2, hatchway 2, handle 1, fabric
  cover 1, bracket 1); kept: `watch` x2. 7 of 8 distinct unknown labels. All 6 live open unknowns of round 4 (window,
  hatch, hatchway, handle, fabric cover, docked spacecraft) are removed. No listing that maps to a catalog class is ever
  filtered (asserted, plus every seed class name and synonym).
- **Adversarial review** (independent reviewer agent; all fixes test-first unless marked):
  1. *P1: "head" fuzzy-maps to `headset`*, so with my first catalog exception a head box became a headset instance and
     its crop was uploaded. Body labels are now items only for exact/synonym catalog names, never face/head.
  2. *P1: "the man", "a woman" escaped the whole-label body match.* Leading determiners are dropped; figure/silhouette
     added.
  3. *P2: face kind not sent to the API* → `person_box_kinds`; inverted boxes ordered; zero-area crop fails closed.
  4. *P3: box cap could cut the overlapping box* → overlapping regions first.
  5. *P3: broad structure heads dropped loose items* (memory module, portable fan, camera mount, laptop cover, picture
     frame) → whole-label only for module/fan/mount/frame/cover; `robot arm` added. (`robotic arm` as a body label was
     found by my own probe before the review; test added with the fix, not red first.)
  6. *P3: filled destinations broke cross-window event dedup* → dedup key on the destination's module.
  7. *Test quality:* the person-link test's docstring claimed an unknown case it did not exercise; renamed, and an
     unknown-vs-person-box test added. Pipeline tests for `head` and `the man` added.
  - Accepted (spec): detector misses with no omni box; recall cost for held objects; remaining broad heads.
- **Final runs:** perception **1228 passed** (1110 → +118 cases in the new file); ruff clean. `make test` all suites
  passed (inventory-api 506, perception 1228, agent 632, eval 408 + 1 skipped, dashboard 422, e2e 29). `make eval`
  gate PASSED.

## AC-PER-53 unknowns prompt (final Gemma run, 2026-10-18)
- **Found live:** after AC-PER-50 ("skip structure"), plus "use catalog names when they fit", Gemma produced 0
  non-structure unknowns in a full first pass, which defeats the unknown-classification requirement.
- **Red:** 4 of 4 failed. **Green:** 1232 passed; ruff is clean. **Live effect:** measured on the next run (see
  QUESTIONS #38).

## AC-PER-54..57 detector boxes for unknown objects (live Gemma 4 run, 2026-10-18)
- **Found live:** the unknowns "orange tool" (COL1), "orange sphere" and "white wrapped object" (JPM1) had sighting
  `bbox`/`bbox_source` null, so the review showed a bare 1080×608 frame. Read in the code and in the raw replies
  (fetched read-only, now tests/fixtures/gemma4_unknowns_2026-10-18.json): OWLv2 was only asked for catalog names +
  person/face, so an UNKNOWN label could never link, and Gemma's boxes are on a 0..1000 scale in `[y1, x1, y2, x2]`
  order, which AC-PER-33 nulls (reported, not fixed here: it changes linking for every object).
- **Spec first:** docs/spec/20-perception.md "Amendments: detector boxes for unknown objects" (AC-PER-54..57).
- **Red:** tests/test_ac_per_54_57_unknown_queries.py, 56 of 59 failing for the right reasons (no `query_detect`,
  no config, no `mentions_person`); 3 passing (fixture shape, and two that hold already).
- **Green:** `Reconciler._query_unknowns` (after linking, before people/locations; only in capturable windows),
  `privacy.mentions_person`, config `unknown_queries_max`/`unknown_query_min_score`, unknown reviews use the frame
  (AC-PER-56), replay passes `query_detect` (detector on the frame's hires image with only the new texts),
  `ReplayStats` counters, window statistics.
- **Existing tests amended (justified, AC-PER-56 amends AC-PER-13):** `test_ac_per_08_unknown_class_creates_unknown_
  entity_and_review` and `test_ac_per_14_scripted_scenario_end_to_end` asserted the unknown review image is the crop.
  Both now assert the frame, plus that the crop is still stored as the exemplar; the e2e test also asserts, through
  the real inventory-api, that `GET /review` returns `image_bbox` = the detector box with `image_bbox_source:
  detector`.
- **Test bugs fixed before green:** `seen_at` format (`+00:00`), and the replay test assumed one window (the first
  window is emitted on the first frame, AC-PER-2).
- **Real OWLv2** (base-patch16-ensemble, CPU, temp venv; `-m model` test passes):
  - live Kibo frame (06:28:33, frame 0): "orange sphere" 0.604, px [565, 253, 604, 290], exactly the sphere;
    tour video t = 55 s (not 2 min: the run's first window is video t = 0): 0.450, px [559, 264, 598, 301].
  - "orange tool": best 0.277 is the wrong object (the sphere through the far hatch); the tool scores 0.142.
    "white wrapped object": 0.058. Default threshold set to **0.3** (links the sphere, rejects the wrong tool box).
  - Latency: ≈ 0.50 s per call on CPU with 1 query, 0.52 s with 6; the image encoder dominates, so the cost is
    one detection per frame with queries (≈ 0.35 s on the T4; usually 1-2 per window with unknowns).
- **Adversarial review** (independent reviewer agent; no P1; fixes test-first):
  1. *P2: detector-missed person.* A query box could crop a crew member reported only in `people[]`. Fail closed: no
     query link when people are reported and no frame of the window has a person region. Checked on the live window
     that this keeps the sphere (OWLv2 boxes the person on frame 4 at 0.32, none on frame 0).
  2. *P2: query box on an unlinked catalog detection* (same laptop seen on another frame). Every main-call detection
     of the frame is now claimed.
  3. *P2 (likely): long labels* would overflow OWLv2's 16-token text cap and fail the frame's batch. Query = short
     noun phrase (`labels.noun_phrase`, ≤ 6 words), and the detector truncates (checked with the real model).
  4. *P3:* spec call bound reworded; more person/body words (crewman, spacewalker, kid, child, guy, someone, hair,
     eye, neck, ...); vacuous person-word test now asserts no calls; a test renamed; failed calls are not counted;
     `ReplayStats.unknown_query_person_rejected`; a catalog-object AC-PER-49 frame-fallback test (the AC-PER-49 tests
     use an unknown, which is now always framed).
  - Accepted (spec residual risks): widened detector-miss risk, repeated per-window cost, omni's box not used to pick
    among query boxes, eval cannot score query links (its fake detector never returns unknowns).

## AC-PER-58..63 box format and UI review round 5 (live Gemma 4 run, 2026-10-18)
- **Measured first** (read-only): 34 raw replies of the live run (`observation_window.raw_uri` -> dashboard media
  proxy), saved as tests/fixtures/gemma4_boxes_2026-10-18.json with the video offset of each window (the run loops
  tour-medium.mp4, 896.8 s; frame k = window offset + k s). 142 boxes, all integers 0..1000. Drawn on the real frames
  (ffmpeg): `[y1,x1,y2,x2]/1000` lands on the treadmill belt, the laptop, the orange sphere, the wristwatch and the
  camera; `[x1,y1,x2,y2]/1000` does not. The old clamp kept 1 of 142 boxes ("green fabric cover" -> the whole frame).
- **Spec first:** docs/spec/20-perception.md "Amendments: box format and UI review round 5" (AC-PER-58..63) and
  docs/spec/10-inventory-api.md AC-API-45.
- **AC-PER-58 red -> green:** tests/test_ac_per_58_box_format.py (import errors, then 3 wrong expectations of mine
  fixed against the data: before = 1 survivor, not 0; after = 141, not 142, one box is 1.7% wide). `box_order_for`,
  per-reply scale, `OMNI_BOX_ORDER`, Gemma prompt + patched prompt schema, chat + realtime clients.
- **Effect measured with the real OWLv2** (CPU, temp venv, catalog + person/face queries on every frame of the 34
  windows, then the real `Reconciler._link`): before 1 omni box, 37/139 instances linked (36 by label, 1 by IoU to a
  wrong whole-frame box). With the boxes alone: 39 IoU links, but 29 to another class's detection (laptop -> cable,
  camera -> cargo transfer bag, food package -> laptop). Added test-first: a catalog instance never takes another
  class's box by IoU; an unknown only at IoU >= 0.5 (`PERCEPTION_LINK_IOU_CROSS_CLASS`). After: 140/141 instances
  carry a box, 42 linked (15 by IoU: 11 same class, 4 unknown cross-class, 2 of those to `cable` boxes).
- **AC-PER-59 red -> green:** tests/test_ac_per_59_unknown_flood.py on the round-5 labels and on all 35 unknown
  listings of the run (before 8 structure / 27 to the queue; after 13 structure / 6 generic held / 16 to the queue).
  Deviation from the request, by data: "attached to a rack/rail" is not structure (16 real pouches and bags).
- **AC-PER-60 red -> green:** live decision log read first: the Zvezda laptop match was the decision model at 0.83 with
  similarity 0.61 (p 0.63). Generic classes need the auto band across modules; module changes emit `moved`.
- **AC-PER-61:** caption rule test-first. A summary transit rule was also implemented and measured on the 51 labelled
  windows: -1 correct carry, 0 gains, so it was removed (spec: rejected). Fixture truth corrected for the two
  "NOD 3" windows (frames viewed: a node with "To NOD 3" signs, not the Cupola); AC-PER-42 figures amended
  (16/23 -> 14/23 on the first 41, 19/30 -> 17/30 overall, 0 wrong either way; the old carry is 2 wrong).
  gemma4_tour_people fixture `module` (replayed resolution) amended for the 3 "NOD 3" windows. Person attach checked:
  not an attach; Gemma swapped garment colours; clash rule holds.
- **AC-PER-63:** existing tests amended: AC-PER-10 event tests now use a drill with a previous place (a first
  sighting no longer gets a move); AC-PER-44 / AC-PER-51 person moves are `seen` arrivals.
- **Runs:** perception 1455 passed (1314 -> +141); inventory-api 515; ruff clean; `make test` all suites passed
  (api 515, perception 1455, agent 668, eval 408 + 1 skipped, dashboard 471, e2e 29); `make eval` gate PASSED.
- **Adversarial review** (independent reviewer agent; no P1; fixes test-first in
  tests/test_ac_per_58_63_review_round.py, 24 red then green): P2 held-review flood (only ask-band look-alikes);
  P2 held review blocked same-place adoption (moved after adoption); P3 no synthesized move in carried / truncated /
  hint-conflict windows; P3 placeholder hints do not end the carry; P3 negation, possessive and size-digit false
  positives in the label regexes; P3 two-phase IoU linking (same class first); P3 inf clamped, NaN -> null box;
  warning on grid replies to an xyxy client. Accepted (spec): model alias, mixed replies, cable boxes, label links
  ignore omni's box, broad modifiers, garment swaps, generic recall, FakeOmni order. API findings: see
  inventory-api.md. Real-data link figures unchanged after the fixes.
- **Final runs:** perception **1483 passed** (1314 -> +169), ruff clean.

- **After review:** `make test` all suites passed (api 517, perception 1483, agent 668, eval 408 + 1 skipped, dashboard
  471, e2e 29); `make eval` gate PASSED.

## AC-PER-64..71 data quality, UI review round 6 (live Gemma 4 run, 2026-10-18)
- **Measured first** (read-only: replica inventory-db-2, raw replies via the media proxy): the live run's 63 windows
  saved as tests/fixtures/gemma4_tour_two_pass_2026-10-18.json (people/objects/events verbatim, private windows without
  text) and the DINOv2 CLS embeddings of its 93 stored frames as tests/fixtures/gemma4_tour_frame_embeddings_2026-10-18
  .json. Real strings for every test below come from these and the DB.
- **Spec first:** docs/spec/20-perception.md "Amendments: data quality, UI review round 6" (AC-PER-64..71).
- **Order note (honest):** for AC-PER-64 the label helpers (`head_family`, `content_tokens`) and config were written
  before the first red run; the reconciler tests were red for the right reason (no suppression). For AC-PER-65/66/67/
  68/69 minimal stubs returning "no" were added first so the red step failed on assertions, not import errors.
- **AC-PER-64 structure memory.** Red 12/30. Green: `_Memory`, `_structure_memories` (TTL cache, keeps the old copy
  on failure, 404 = none), `_remembered` (embedding ≥ assign_p / label key / same-place text), `HttpInventoryClient.
  list_structure_memory`, `ReplayStats.structure_memory_suppressed`. Calibration change before green (test and spec):
  token floor 0.5 → **0.6**, because the dismissed "hatch cover" against the open "white covering" is exactly 0.5
  (the hose → tube case is 0.8). Test-design fix before green: the "below the auto band" case used a label of the same
  family, so the text path matched; label changed. *Existing test amended (justified):*
  `test_ac_per_07_reconciler_never_writes_catalog` allowed-call set gains the read `list_structure_memory`.
  Live case: the tube was raised at 08:10, before the 08:16:55 dismissal, from another video offset; both at USOS, so
  "same place" includes the same segment-level location (text evidence), which the spec says.
- **AC-PER-65 empty spacesuits.** Red 17/29. Green: `people.is_empty_suit` (+ "extravehicular", "Orlan" phrases),
  `Reconciler._suits_as_objects` before AC-PER-49/50. Data-driven refinements, each with a test: an `actor_ref` of an
  event entry is a person (kept `test_ac_per_44_person_moving_itself_gets_no_actor` valid unchanged), except its own
  "moved" (object = person class): live 07:56:30 "spacesuit with black and grey components" was only the actor of its
  own move, while 08:02:00 / 08:17:00 "white suit" have an arm/hand moving (kept as people).
- **AC-PER-66 colour, module unknown, spread, private-only.** Red 21/32. Green: `people.object_colours/
  object_colour_conflict`, hair-length and legwear clashes in `_compare`, `_colour_clash` (entity description + 5
  latest sighting texts in memory), `_same_known_module` replaces `_cross_module` for the AC-PER-60 hold,
  `_spread_exceeded` (+ `_person_modules` memory), `_private_only` (AC-API-44 `snapshot_withheld_reason`).
  - Bug found by `make eval` (crew transit `person_tracks_per_person` 2.0 > 1.3): the spread rule counted the module the
    track was already in; fixed (a module seen within the window is not new), test added after the fix
    (`test_a_module_the_track_was_just_in_is_not_new`).
  - Design change measured on eval: strong evidence = overlap ≥ 0.8 (the Dice condition split the eval's single actor
    at every "blue shirt, khaki pants" variant against its minimal first text).
  - Test bug before green: the spread-expiry case used 900 s, beyond the AC-PER-45 cross-module horizon; now 700 s.
  - *Existing tests amended (justified):* `test_ac_per_45_same_person_through_three_modules_in_five_minutes_is_one_
    track` now expects the split at the third module (it is the live blue-shirt over-merge) and a new test proves a
    distinctive look still crosses three modules; `test_ac_per_48_real_sequence_produces_far_fewer_tracks` bound 0.6 →
    0.71 of AC-PER-35's 37 tracks (measured 21 → 26 with the hair/legwear clashes) plus a new purity assertion
    (clashing pairs inside tracks ≤ 1, was 3); `test_ac_per_43_existing_entity_sighted_privately_gets_no_description`
    now asserts no same_or_new decision in a private window (AC-PER-71) instead of the class-name decision subject.
- **AC-PER-71 private windows.** In the same red run (2 of its 5 tests red; the other 3 held already). Green:
  `_match_private`, no embedding in no_capture windows.
- **AC-PER-67 revisit location.** Red 11/17. Green: `locations.RevisitLocator`, `Reconciler._revisit_step`, config.
  Rule refined on data before green: a caption scene change does not block the visual match ("To COL" at 08:19:00 is
  not even a mention; truth COL1). *Existing tests amended (justified):* five carry tests in test_ac_per_39_42 /
  test_ac_per_60_63 run with `revisit_location_sim=0`: their synthetic windows paint identical frames, which match at
  cosine 1.0; they test the carry in isolation.
- **AC-PER-68/69/70.** Red 26/37. Green: `labels.kind_family/class_families/description_phrases/is_worn_item`,
  `CatalogMapper._kind_clash`, `Reconciler._check_label` (remap or `label_doubt`: confidence cap + low_confidence
  review), worn filter next to the structure filter, `_within_one_place` for omni and speech moves. Test bugs before
  green: two tests did not account for the person assignment of people[]. *Existing tests amended (justified):*
  `test_ac_per_51_object_moved_and_seen_in_the_same_place_keeps_no_destination` now asserts the event is dropped
  (it is the live "moved from LAB1 to USOS" rendering); round-5 cap tests swap "wristwatch" for the real loose label
  "green bag" (the watch is worn now); `test_ac_per_10_same_event_in_overlapping_windows_is_recorded_once` uses a later
  move to Destiny and `test_ac_per_14_replay_default_windowing_dedups_repeated_omni_event` seeds the drill in LAB1
  (NOD2 → NOD2 is no move any more). The dedup of overlapping-window moves is now mostly covered by AC-PER-70 itself.
- **UI6-03 root cause** (spec AC-PER-67): not a regression; title-caption sampling phase drifts 3.2 s per pass (30 s
  stride, 896.8 s loop). eval-real on the live API (4 passes, API mode): pass 1 11/15 = 0.733, pass 2 6/14 = 0.429,
  pass 3 5/13 = 0.385 (1 wrong: a privacy-hold window, reply not stored), pass 4 4/13 = 0.308; export mode for passes
  1–2: 11/15 and 6/15. Simulated on the real embeddings over 4 passes, the revisit location (0.94) adds +1 in pass 2
  (08:04:30 → JPM1, 0.941) and +1 in pass 3 (08:19:00 → COL1, 0.943), 0 wrong, nothing in uncaptioned stretches.
- **Effects on the real two-pass fixture** (replay through the reconciler, FakeInventory, no detector, revisit off;
  HEAD vs new, script /tmp/ui6/measure_two_pass.py): person tracks 18 → 20, of which stowed-suit tracks 3 → 0 (the
  one remaining "white suit" has an arm moving), suits as objects 0 → 4; most modules on one person track within
  10 min 3 → 2; colour-clashing sighting pairs inside object entities 68 → 7; unknown entities 28 → 24 (wristwatch ×3,
  yellow helmet filtered as worn); with the two live dismissals as memory 24 → 21 (insulated hose, hatch cover, white
  padded tube suppressed; "white covering" kept); label-doubted sightings 1 (the "vacuum cleaner" drill); moved events
  without a destination 2 → 0. "orange tool" no longer maps to the operator class "Orange ball".
- **Runs:** perception **1633 passed** (1483 → +150), ruff clean; `make eval` gate PASSED (person_tracks_per_person
  1.0); `make test` all suites passed (api 545, perception 1633, agent 668, eval 408 + 1 skipped, dashboard 473,
  e2e 29 + 1 new); `make lint` clean.
- **New env vars:** PERCEPTION_STRUCTURE_MEMORY_REFRESH_S (60, 0 = off), PERCEPTION_STRUCTURE_MEMORY_TOKEN_FLOOR
  (0.6), PERCEPTION_PERSON_SPREAD_S (600, 0 = off), PERCEPTION_PERSON_SPREAD_MODULES (2), PERCEPTION_PERSON_SPREAD_FLOOR
  (0.8), PERCEPTION_REVISIT_LOCATION_SIM (0.94, 0 = off), PERCEPTION_REVISIT_LOCATION_MARGIN (0.04),
  PERCEPTION_LABEL_CONFLICT_CONFIDENCE (0.4).
- **Adversarial review** (independent reviewer agent; 4 P1, 6 P2, 7 P3; each reproduced red in
  tests/test_ac_per_64_71_review_round.py, 22 of 26 red, then fixed; one vacuous case found by me and rewritten:
  "bag" maps to a catalog class, now "strap"):
  1. *P1 remap through a verb* ("crew member wearing a headset" → headset): phrases end at "-ing" verb forms.
  2. *P1 kind guard rejected coffee/drink/meal/food* (fungible seed classes): class families from every name word;
     every seed name and synonym asserted to map.
  3. *P1 worn phrasing about a nearby person* ("... crew member wearing a blue shirt", "on the face of the rack"): only
     "worn" counts; body places need a possessive.
  4. *P1 converted suit dropped as worn* ("astronaut wearing white spacesuit"): converted suits are exempt.
  5. *P2 memory scope:* spec aligned with the live USOS/USOS case (text: same module or same location outside
     modules); nearby label evidence needs a specific (≥ 2 words, not generic) label.
  6. *P2 colour gate missed candidates outside the 50-row listing:* uses the candidate row and the sighting memory.
  7. *P2 colour false clashes* ("laptop displaying a blue screen", "bag containing red tools"; white vs grey): more
     phrase cuts; white/grey compatible.
  8. *P2 refinements dropped* (speech USOS → NOD2): only coarsening moves are dropped (perception and API).
  9. *P2 privacy:* a converted person object keeps no box (no crop/exemplar of a possibly suited crew member).
  10. *P3:* embedding dimension guard; truthy `is_speaking`; carried/revisit modules not counted for the spread.
  - *Existing tests amended in the review round (justified):* `test_object_colour_conflict_on_the_live_chain`
    (white vs grey no longer clash, green still does); the AC-PER-65 person-object test now asserts no box and no
    exemplar.
  - Accepted (spec residual risks): continuity cost of the spread/clash rules, private-only check limited to the class
    listing, per-window cost (class listing per class, 6 frame embeddings per roaming window), memory reactivation
    handled API-side, in-memory per-entity histories not pruned.
- **After review:** perception **1660 passed** (1483 → +177), ruff clean. Re-measured on the two-pass fixture: person
  tracks 18 → 20 (stowed-suit tracks 3 → 0 by the spec's rule), unknown entities 28 → 24 (21 with the two live
  dismissals as memory), moved without destination 2 → 0, label-doubted 1; colour-clashing pairs inside object
  entities 68 → 18 by the naive metric of the measuring script, which still counts white vs grey (now deliberately
  compatible). People fixture (AC-PER-48): 25 tracks (bound 26), 1 clashing pair. `make test` all suites passed (api
  549, perception 1660, agent 668, eval 408 + 1 skipped, dashboard 473, e2e 30); `make eval` gate PASSED; `make lint`
  clean.
