# TDD log: integration (`tests/e2e`, root `Makefile`, cross-component fixes)

Role: integration engineer, 2026-10-17. Every component existed and its own suite was green before this phase:

| Suite | Result at the start |
|---|---|
| inventory-api | 336 passed |
| perception | 380 passed, 2 deselected (`model`) |
| agent | 283 passed |
| eval | 325 passed, 2 skipped (integration, waiting for perception), 1 deselected (`model`) |
| dashboard | 187 passed (12 files) |

Test databases in this phase use the `test_int_` prefix. Only throwaway `test_*` databases were created, and all were
dropped; the `inventory` database was never opened. No contract file was edited.

## 1. Eval integration tests (AC-EVAL-5/6), un-skipped

I installed the eval `pipeline` extra (perception + inventory-api, editable) into `eval/.venv`. That un-skipped
`test_ac_eval_05_integration_synthetic_scene_through_perception` and `..._perception_replay_signature`.

**Red (real failure, not a skip).** The real replay of synth seed 7 ran end to end (16 windows, all posted), but the
gate failed:

| Metric | Value | Threshold |
|---|---|---|
| `duplicate_rate` | 0.167 | ≤ 0.10 |
| `location_acc` | 0.0 | ≥ 0.8 |

Diagnosis (kept database, reconciler trace):

1. **`location_acc = 0.0` (perception design gap).**
   - **Symptom:** every sighting was stored at module level (`LAB1`), because perception only knew the camera's
     default location plus a window-level `location_hint`. The gold is zone-level (`LAB1S2`, `LAB1O3`, `LAB1P3`).
   - **Gap:** ARCHITECTURE decision 5 says "module → zone (camera-configured)", but nothing in perception
     configured zones.
   - **Already there:** the synth manifest already carried the camera's zone rectangles (`zones:`), and
     `nasa_eval.dataset.Manifest` dropped them.
2. **`duplicate_rate = 0.167` (fake mismatch in eval's `local` collaborators).**
   - **Symptom:** the moved multimeter became a second entity. Its candidate similarity fell from 1.0 to −0.02
     after the move.
   - **Cause:** eval used perception's `FakeEmbedder`, which keys on the *exact dominant colour* of perception's
     own painted test rectangles. On eval's procedural icons, after perception's 448-px bilinear downscale, that
     colour changes with the sub-pixel position.

**Fixes (each test-first; red seen for the right reason, with stubs, not import errors):**

- **perception: camera-configured zones** (`perception/config.py`, `perception/reconciler.py`).
  - **Config:** `PerceptionConfig.camera_zones` comes from the env var `CAMERA_ZONES` (JSON
    `{camera: {zone_id: [x1,y1,x2,y2]}}`), validated by `parse_camera_zones`, which raises `ConfigError` on bad
    input.
  - **Object location:** an object's location is the smallest configured zone containing its box centre. The zone
    must be known, must not be `no_capture`, and must lie inside the window location. Otherwise the object gets
    the window location, as before.
  - **Where it is used:**
    - sighting `location_id`;
    - review `location_id`;
    - the `/ingest/candidates` query;
    - the transit gate and zone feature;
    - the decision-model subject and state;
    - speech-claim confirmation.
  - **Fail-closed rule:** a private zone is never assigned, and privacy never changes. (Assigning one would have
    the API strip frames and reject reviews, AC-API-19.)
  - **Tests:** `tests/test_ac_per_11_camera_zones.py`, 15 tests.
    - Red: 10 failed (`NotImplementedError` / `'LAB1' != 'LAB1S2'`), 5 passed (fallback behaviour unchanged).
- **eval: `ThumbnailEmbedder`** (`nasa_eval/collaborators.py`) for the `local` target.
  - **What it is:** a 16×16 RGB thumbnail, mean-centred and L2-normalised, giving exactly 768 dimensions.
    It is weight-free and deterministic.
  - **Measured on downscaled synth crops:** the same object at another position scores ≥ 0.99, identical twins
    0.93, and any other object ≤ 0.32.
- **eval: `Manifest.zones` and `replay_config(dataset)`.** The manifest zones of the dataset's camera are added to
  perception's config, unless `CAMERA_ZONES` already configures that camera.
  - **Tests (in `tests/test_ac_eval_05_collaborators.py`):**
    - `test_ac_eval_05_manifest_carries_camera_zones`;
    - `..._replay_config_applies_manifest_zones`;
    - `..._local_embedder_reidentifies_synth_crops`, which reproduced the −0.02 split in red.

**Green.** Synth seed 7 now scores 1.0 or 0.0 (as appropriate) on every gated metric. A sweep over seeds 1 to 30 with
the real local collaborators passed with perfect entity resolution, location, event and actor scores on every seed.

### Two more perception bugs found by the seed sweep

| # | Symptom | Root cause | Fix (test-first) |
|---|---|---|---|
| 3 | Seed 1: `event_f1` 0.8. The crew line "I'm moving the multimeter over to the **port** rack" produced a speech `moved` event to **NOD2P5**, a private crew-quarters zone. | `LocationResolver` registered the text inside *every* display name's parentheses as an alias. So "Node 2 crew quarters (port)" made "port" (also "deck", "stbd", "ovhd", "toilet") resolve to a zone. | Parenthetical aliases only for modules and segments ("Destiny (US Lab)"). `tests/test_ac_per_11_16_generic_location_words.py`: 14 tests, 7 red. |
| 4 | Seeds 2 and 26, repeated runs: twin swap, so the same run gave `pairwise_f1` 1.0 or 0.941 and `false_merge_rate` 0 or 0.333 (nondeterministic). | Identical twins score exactly or nearly the same against both twin entities. `/ingest/candidates` breaks ties by `entity_id`, a random UUID, so the winner changed from run to run. | `reconciler._rank`: among candidates within `NEAR_TIE_P = 0.02` of the best score, the one last seen in the observation's own location ranks first. Then score, then entity id. Object processing order is also deterministic (window index). `tests/test_ac_per_08_twin_tiebreak.py`: 7 tests, 4 red (order-dependent). A clearly better appearance still beats location. |

### Eval test changes after red (justified)

| Test | Change | Why |
|---|---|---|
| `test_ac_eval_05_build_local_collaborators` | The `local` embedder is now asserted to be `ThumbnailEmbedder`, not perception's `FakeEmbedder`. | `FakeEmbedder` cannot re-identify the synth crops after downscale (finding 2). This is a strengthening: the new test `..._local_embedder_reidentifies_synth_crops` pins the required behaviour. |
| `test_ac_eval_05_integration_synthetic_scene_through_perception` | Uses the `test_int_` prefix. Also asserts that the database is dropped, `source == "replay"`, and every window was posted. `CAMERA_ZONES` and `DECISION_URL` are unset for the test. | Integration-phase database prefix rule. Strengthened, not weakened. |

## 2. End-to-end suite: `tests/e2e` (19 tests)

**Venv:** `tests/e2e/.venv`, built from `requirements.txt`: all four Python components editable, plus pytest and ruff.

**Session fixtures** (`conftest.py`):

1. **Database:** a fresh `test_int_e2e_<hex>` database built from `contracts/db/*.sql`, dropped at the end. The
   teardown asserts it is gone.
2. **inventory-api:** the real app served by **uvicorn in a background thread** (real HTTP). One in-memory object
   store is **shared by perception and the API**, so `/media` serves what perception uploaded.
3. **Scene:** synth **seed 2**. The drill is moved LAB1O3 → LAB1S2 by "crew member, green shirt"; the scene also has
   identical twins, a removed object and one unknown. Seed 2 is one of the twin-swap seeds of finding 4.
4. **Replay:** `perception.replay.run` with eval's `local` collaborators and `replay_config`. It uses `environ={}`,
   so no live endpoint can leak in from the shell.
5. **Agent:** the real agent app, its real `OpenAICompatLLM`, and a **scripted** OpenAI-compatible LLM reached over
   `httpx.MockTransport`.

`test_e2e_pipeline.py`:

| # | Proves |
|---|---|
| 01 | Every window is posted: no errors and no invalid windows (AC-PER-18, AC-API-18). |
| 02 | Exactly one live entity per gold object (twins stay two, the moved object stays one). The unknown is an `unknown` entity, and nothing is merged or dismissed. |
| 03 | The drill's `last_location` equals the gold destination zone, and `last_moved_by` equals the gold actor. `snapshot_url`, the descendant location filter, detail (exemplars, sightings, one move) and history (sorted desc). |
| 04 | `/events` equals the gold events (type, catalog, actor, `to_location`). The evidence frame is served by `/media` as JPEG. |
| 05 | `/review` holds exactly one open `unknown_object` for the unknown entity, in the gold zone. Its `image_url` is served, and the entity has `needs_review`. |
| 06 | `/search` returns window hits from the transcript and entity hits. `/stats` agrees with the items. |
| 07 | **"where is the drill?"**: the agent calls `where_is` over the real API. The reply has the zone and the mover, the only citation is the drill entity (with `at`), and there are 2 LLM calls with `tool_choice=auto`. eval's `score_qa_item` marks the answer correct as both an `entity` and a `location` answer. |
| 08 | Grounding: an LLM that answers with the drill's *old* zone gets "I don't have a record of that." and no citations. |
| 09 | Every agent read tool works against real API responses (the agent suite only has an in-memory fake API). `who_moved` returns the gold actor. |
| 10 | Dreaming over the real schema writes global, location and entity summaries, and is idempotent. |
| 11 | eval scores the database of this run (`read_snapshot` → `score`), writes a pipeline report with a QA section (accuracy 1.0), and `run_gate` returns 0. Exact metrics are asserted (`false_merge_rate` is not gated, so a twin swap would otherwise pass). |

`test_e2e_contracts.py` (cross-component contract checks on real pipeline data):

- `/items`, `/review`, `/events`, `/catalog`, `/locations`, `/items/{id}` and `/items/{id}/history` validate against
  the OpenAPI component schemas (jsonschema 2020-12 with `#/components` refs). These are the producer side of the
  dashboard's `src/api/types.ts` and the agent tools.
- Review `candidates` are expanded for every `candidate_id`.
- The agent's `/chat/stream` speaks the dialect `dashboard/src/lib/sse.ts` parses: `token {text}*`, then
  `done = /chat response`. The tokens concatenate to `done.reply`.

**Mutation checks** (temporary code edits, then restored):

- Dropping camera zones makes 4 e2e tests fail.
- Swapping back to `FakeEmbedder` is caught by eval's `..._local_embedder_reidentifies_synth_crops` and by the eval
  integration test on seed 7.
- The OpenAPI validator rejects a missing required field and a bad enum.

## 3. Root `Makefile`

| Target | What it does |
|---|---|
| `make test` | inventory-api, perception, agent (pytest); `make -C eval test` (unit + integration + strict CI gate + local-report gate); dashboard (vitest); `tests/e2e`. |
| `make eval` | Synth scene (`SEED`, default 7) into a temp dir → `nasa_eval run --target local --db-prefix test_int_` → strict `nasa_eval gate`. |
| `make lint` | ruff on every Python component and `tests/e2e`; `tsc --noEmit` on the dashboard. |
| `make venvs` | Creates any missing venv (uv, Python 3.11; eval with `[test,pipeline]`) and `node_modules`. |

`TEST_PG_ADMIN_URL` defaults to the local test container documented in `docs/spec/00-conventions.md`, and can be
overridden.

## 4. Final results

| Suite | Result |
|---|---|
| inventory-api | 336 passed |
| perception | 418 passed, 2 deselected (+36 integration-phase tests; +2 from a concurrent perception change outside this phase) |
| agent | 283 passed |
| eval | 329 passed, 1 skipped (by design: "only meaningful while perception is not installed"), 1 deselected (`model`). Strict CI gate PASSED. |
| dashboard | 187 passed |
| e2e | 19 passed |

`make test` exits 0, and so do `make lint` and `make eval` (seed 7: every gated metric is perfect; the gate PASSED).

## 5. Cross-component mismatches: found and fixed, or reported

Fixed (code only; no contract was edited):

1. perception ↔ architecture/eval gold: camera-configured zones were missing (location granularity). Fixed above.
2. eval ↔ perception fakes: the `local` target used a fake embedder built for perception's painted rectangles.
   Fixed above.
3. perception location aliases: zone qualifiers in parentheses resolved generic words to private zones. Fixed above.
4. perception ↔ inventory-api candidate ordering: equal-similarity ties were decided by a random UUID. Fixed in
   perception (location-aware near-tie ranking).
5. eval `Manifest` dropped the manifest's `zones`. Fixed.

Reported, not changed (they need spec or contract owners):

- **Spec AC-PER-11** should mention camera zones (`CAMERA_ZONES`; box centre → zone; never a `no_capture` zone).
  **AC-PER-8** should mention the near-tie location rule.
- **`eval/thresholds.yaml` does not gate `false_merge_rate`.** The twin swap of finding 4 passed the gate with
  `pairwise_f1` 0.94. Proposal: `false_merge_rate: {max: 0.05}`. The e2e suite asserts it exactly.
- **AC-AGT-1 citations carry no `location_id`.** QA location scoring falls back to location-id tokens in the reply.
  This is unchanged from eval's note, and the e2e QA passes through the token path.
- **`eval/baselines/synth-ci.snapshot.json`** is still the reference snapshot. It can now be replaced by a recorded
  perception run (`nasa_eval run --keep-db` + `snapshot export`).
- **`/ingest/candidates`** could break similarity ties deterministically by recency or zone on the API side as well.
  This is optional now that perception ranks near ties itself.
- **`dashboard` has no ESLint config.** `make lint` uses `tsc --noEmit` for it.
