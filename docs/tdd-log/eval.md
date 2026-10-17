# TDD log: eval (`eval/`)

Spec: `docs/spec/50-eval.md` (AC-EVAL-1 to AC-EVAL-7). Contracts used: `contracts/db/*.sql` (fresh test databases,
seed catalog and location ids), `contracts/openapi/inventory-api.yaml` (sighting, event and review shapes). The agent
`/chat` shape comes from `docs/spec/30-agent.md` AC-AGT-1, and the replay interface from `docs/spec/20-perception.md`
AC-PER-18.

## Red phase (2026-10-17, tester)

### Skeleton

- `eval/pyproject.toml`: package `nasa_eval`, run as `python -m nasa_eval`.
  - Runtime deps: numpy, pillow, pyyaml, jsonschema, psycopg[binary], httpx.
  - The `[test]` extra adds pytest and ruff.
  - Pytest markers: `model` (deselected by default), `integration`, `db`.
- Stubs. Every function raises `NotImplementedError`; only the dataclasses and constants that fix the API are real.
  - `__init__.py`: paths (`EVAL_ROOT`, `CONTRACTS_DB`, `DATASETS_ROOT`, `REPORTS_ROOT`, `THRESHOLDS_PATH`).
  - `__main__.py` and `cli.py`: `build_parser()` and `main(argv) -> exit code`. Sub-commands are `validate`,
    `generate`, `run`, `gate` and `decision-eval`.
  - `dataset.py`:
    - dataclasses `Manifest`, `GoldObject`, `Appearance`, `GoldEvent`, `QAItem`, `Dataset` (with `iter_source()`);
    - functions `load_schema`, `validate_dataset`, `load_dataset`, `resolve_dataset`;
    - exception `DatasetValidationError`.
  - `synth.py`: `SceneConfig`, `Scene`, `TranscriptLine`, `SUPPORTED_CLASSES`, `draw_crop`, `generate_scene`,
    `write_scene`, `generate_pairs`.
  - `metrics.py` (pure):
    - `iou`, `prf_from_counts`, `detection_prf`, `macro_f1`;
    - `pairwise_prf`, `bcubed_prf`, `assign_entities`, `duplicate_rate`, `false_merge_rate`;
    - `module_of`, `location_accuracy`, `event_prf`, `unknown_recall`;
    - `expected_calibration_error`, `cohen_kappa`.
  - `snapshot.py`: `Snapshot`, `Pred*` dataclasses and `read_snapshot(database_url)`.
  - `scoring.py`: `METRIC_KEYS`, `appearance_key`, `gold_locations_at`, `match_sightings`, `score`.
  - `qa.py`: `OpenAIJudge`, `AgentClient`, `AgentError`, `JudgeVerdict`, `QAResult`, `load_qa`, `score_qa_item`,
    `score_qa`.
  - `gate.py`: `Threshold`, `GateResult`, `load_thresholds`, `check`, `latest_report`, `run_gate`.
  - `runner.py`: `report_basename`, `write_report`, `provision_database`, `drop_database`, `default_replay`, `run`,
    `ConfigError`.
  - `decision_eval.py`: `DecisionPair`, `DecisionOutput`, `RuleBaseline`, `SystemOneDecisionModel`, `load_pairs`,
    `evaluate_model`, `compare`, `DecisionModelError`.
- Not created on purpose, because tests require the developer to add them:
  - JSON schemas in `nasa_eval/schemas/{manifest,gold,qa,pairs}.schema.json`;
  - `eval/thresholds.yaml`;
  - `eval/Makefile` (a `test` target that runs pytest and `python -m nasa_eval gate`).
- Test infrastructure (`eval/tests/conftest.py`):
  - **Seed data:** seed location hierarchy and catalog ids, parsed from `contracts/db/002_seed.sql`.
  - **Dataset fixtures:** a hand-made `TINY_GOLD` (a moved drill, identical wrench twins, an unknown, and a removed
    laptop) and `write_dataset()`.
  - **Database:**
    - One `test_eval_<hex>` database per session, built from `contracts/db/*.sql`. Data tables are truncated per
      test, and the database is dropped at the end. Override the admin URL with `TEST_PG_ADMIN_URL`. The
      `inventory` database is never touched.
    - After the run, no `test_eval_*` databases are left behind.
    - `DbWriter` inserts pipeline-like rows; its `perfect_replay_of(gold)` writes a perfect pipeline result.
  - **HTTP:** `FakeHttp` is a local scripted HTTP server used for the judge, the agent `/chat` and `/v1/systemone`.

### Test command

```
cd eval
uv venv --python 3.11 .venv && uv pip install --python .venv -e '.[test]'
.venv/bin/python -m pytest -q -p no:warnings
```

`-m model` tests are deselected by default. Lint with `.venv/bin/ruff check .` (clean).

### Result

**257 tests collected.** Default run: **225 failed, 23 errors, 6 passed, 2 skipped, 1 deselected** (about 7 s). No
import, syntax or collection errors.

- **The 23 errors** are fixture setup that calls the stubbed `generate_scene(7)` (module fixture in the synth tests)
  or `load_dataset()` (the `data` fixture in the scoring tests). They raise `NotImplementedError`, which is the
  right reason.
- **The 6 passes are structural.** They check API constants and signatures that the stubs already fix:
  - `DatasetValidationError` is a `ValueError`;
  - the default event tolerance is 5 s;
  - `metrics` imports no I/O;
  - `appearance_key`;
  - `ANSWER_TYPES`;
  - the runner source contains no credentials.
- **The 2 skips** are the `integration` runner tests, which need `services/perception` (`perception.replay.run`,
  AC-PER-18). It is not importable yet, so they are completed in the Integration phase.
- **The 1 deselected** test is `@pytest.mark.model`: live DiffusionGemma at `DECISION_URL`.

Failure-reason sample (`--tb=short`, `E` lines):

| Reason | Count |
|---|---|
| `NotImplementedError` (stub) | 241 |
| `AssertionError: eval/Makefile with a test target is required` | 1 |
| `assert {'gold.schema.json', ...} <= set()` (schemas not shipped yet) | 1 |
| `assert {'camera-dslr', 'laptop', ...} <= set()` (`SUPPORTED_CLASSES` empty) | 1 |
| `subprocess.CalledProcessError` (cross-process determinism: generator raises in the child) | 1 |
| `assert 1 == 0` (`python -m nasa_eval --help` and `gate` sub-process exit codes) | 2 |
| `Regex pattern did not match` (`pytest.raises(RuntimeError, match="perception crashed")` got `NotImplementedError`) | 1 |

### AC coverage map

| AC | File | Tests | What is covered |
|---|---|---|---|
| AC-EVAL-1 | `test_ac_eval_01_dataset.py` | 54 | See "AC-EVAL-1 detail" below |
| AC-EVAL-2 | `test_ac_eval_02_synth.py` | 35 | See "AC-EVAL-2 detail" below |
| AC-EVAL-3 | `test_ac_eval_03_metrics.py` | 46 | Hand-computed metric cases; see "AC-EVAL-3 detail" below |
| AC-EVAL-3 | `test_ac_eval_03_scoring.py` | 14 | Scoring a run snapshot, and reading snapshots from Postgres; see "AC-EVAL-3 detail" below |
| AC-EVAL-4 | `test_ac_eval_04_qa.py` | 41 | See "AC-EVAL-4 detail" below |
| AC-EVAL-5 | `test_ac_eval_05_runner.py` | 23 (2 integration, skipped) | See "AC-EVAL-5 detail" below |
| AC-EVAL-6 | `test_ac_eval_06_gate.py` | 25 | See "AC-EVAL-6 detail" below |
| AC-EVAL-7 | `test_ac_eval_07_decision_eval.py` | 19 (1 model, deselected) | See "AC-EVAL-7 detail" below |

#### AC-EVAL-1 detail

- All four schemas are valid Draft 2020-12 and ship with the package.
- Gold schema: each required field; bbox optional, nullable, exactly 4 numbers in [0, 1]; `t >= 0`; every database
  event type; unknown if and only if `catalog_id` is null; `additionalProperties`; non-empty appearances.
- Manifest schema: required keys.
- `validate_dataset`:
  - errors carry the line number;
  - invalid JSON, missing files, and a bad or naive `start_time`;
  - duplicate `gold_object_id`, unordered bbox, empty gold;
  - optional referential checks against seed catalog and location ids.
- `load_dataset` returns typed objects and raises with all errors. `qa.jsonl` is loaded.
- `iter_source()` timestamps follow `start_time + i/fps` and frames are ndarrays.
- `resolve_dataset`, and `validate` CLI exit codes 0 and 1.

#### AC-EVAL-2 detail

- `draw_crop`: shape and dtype, determinism, distinct per class, seed variation, unknown key rejected.
- Frame count and timing match the config.
- Gold locations and classes are seeded ids, and appearance times are frame times.
- Each bbox lies inside its zone region, and the object pixels really are composited.
- The scenario covers every case in the spec:
  - appear;
  - move zone, with a `moved` event, actor and `to_location`;
  - disappear, with `removed`;
  - identical twins visible together;
  - exactly one unknown;
  - a scripted transcript near the move.
- Determinism: same seed gives an identical scene; a different seed differs; written output is byte-identical,
  including across processes with different `PYTHONHASHSEED`.
- The written dataset validates, round-trips losslessly (PNG frames) and has `audio: null` (no TTS).
- `generate` CLI.

#### AC-EVAL-3 detail

`test_ac_eval_03_metrics.py`:

- IoU, including touching and degenerate boxes.
- Detection P/R/F1 per class:
  - wrong-class and false-positive cases;
  - one-to-one matching;
  - time tolerance;
  - IoU threshold inclusive;
  - no bbox.
- Macro-F1 is averaged over classes with gold support.
- Pairwise and B-cubed (hand case 5 sightings / 2 gold / 3 pred: pairwise 0.5 / 0.25 / 0.333, B-cubed
  0.8 / 0.533 / 0.64):
  - perfect, and invariant to relabelling;
  - only shared sightings count;
  - vacuous conventions;
  - all-merged case.
- Plurality assignment, with ties going to the smallest gold id.
- Duplicate rate:
  - hand case 1/3;
  - 0 when perfect;
  - never negative when objects are missed;
  - a 3-way split gives 2.0.
- False-merge rate: 1.0 and 2/3 cases; splits are not merges.
- `module_of`.
- Location module and zone accuracy (0.5 / 0.25); a missing prediction counts as wrong.
- Event P/R/F1:
  - default tolerance 5 s, inclusive boundary;
  - entity must match;
  - maximum one-to-one matching;
  - actor accuracy (normalised, and excluding gold events with no actor);
  - custom actor matcher.
- Unknown recall, including vacuous 1.0.
- ECE hand case 0.4 and edge cases.
- Cohen's kappa 0.5 and -1.
- The metrics module does no I/O.

`test_ac_eval_03_scoring.py`:

- `gold_locations_at` at audit time.
- `match_sightings`: time + IoU, twin disambiguation, one-to-one.
- `score()` on a perfect run, a split duplicate (0.2) and merged twins (false merge 0.4, pairwise P = 5/9).
- Unknown recall needs an `unknown_object` review.
- Event tolerance and actor.
- Zone wrong but module right.
- Audit time override, and an empty run with no NaN.
- `read_snapshot` from a `test_eval_*` database: fields, merged-entity resolution, and a perfect DB run end to end.

#### AC-EVAL-4 detail

- qa schema for each answer type, plus negatives (empty `entities`, missing `location` / `actor` / `rubric`, bad
  type).
- Expected entities must exist in gold, and `load_qa` works.
- Exact entity scoring:
  - the expected entity must be cited; naming it in text is not enough;
  - a duplicate DB entity of the same gold object counts;
  - all expected entities are required;
  - unmapped citations are ignored, and no citations means wrong.
- Location scoring:
  - from the citation `location_id`, or an exact id token in the reply;
  - module-only is wrong, and hedging between two ids is wrong.
- Actor scoring: by descriptor, or by cited person entity.
- Free text: skipped without a judge; with a judge the rubric is passed; a judge error leaves the item unscored;
  the judge is not used for exact answer types.
- Aggregate accuracy by type, and a missing response counts as wrong.
- The judge must differ from the generator (`score_qa` and the `OpenAIJudge` constructor).
- The agreement log is JSONL and appends; raw agreement and kappa are reported.
- `OpenAIJudge` against a fake server: rubric, question and reply sent; fenced JSON tolerated; bad output or HTTP 500
  becomes an error verdict.
- `AgentClient` posts `/chat` and raises `AgentError` on 5xx.

#### AC-EVAL-5 detail

- `<ts>-<dataset>` naming, always in UTC.
- JSON and MD reports are written.
- `provision_database` / `drop_database` against real Postgres: contracts and seed are applied; databases are
  distinct; `inventory`, `postgres` and foreign names are refused; an empty prefix is refused.
- `run()` with an injected perfect replay:
  - report paths and contents are correct;
  - the replay receives the `Dataset` and the fresh DB URL;
  - the DB is dropped afterwards, and every run starts from a fresh, empty DB;
  - `keep_db` works.
- A replay failure drops the DB and writes no report.
- The admin URL can come from the env (`EVAL_PG_ADMIN_URL`).
- An invalid dataset fails before any DB is created; an invalid target is rejected.
- The `cluster` target without config raises `ConfigError`.
- No credentials in the source.
- CLI: unknown target exits 2; `--target` defaults to `local`; `python -m nasa_eval --help` lists all commands.
- `default_replay` without perception raises a clear `ConfigError`.
- **Integration** (skipped): a synthetic scene through `perception.replay.run` must pass `thresholds.yaml`, and the
  replay signature is checked.

#### AC-EVAL-6 detail

- The repo `thresholds.yaml` has the spec values (0.10 / 0.85 / 0.8 / 0.9).
- Load and malformed-file cases.
- Pass when thresholds are met; boundaries are inclusive.
- Every regression is reported. Missing, None, NaN or non-numeric metrics fail; extra metrics are ignored.
- `latest_report`.
- `run_gate` exit codes: 0 pass, 1 regression, 2 missing or invalid report or thresholds. Without `--report` it uses
  the latest report.
- CLI `gate`, and `python -m nasa_eval gate` returns non-zero on regression.
- `make -n test` in `eval/` runs pytest and the gate.

#### AC-EVAL-7 detail

- Pairs schema and `load_pairs`, including rejecting invalid rows.
- `generate_pairs`: balanced positives and negatives with PNG crops and similarity context; hard same-class
  negatives (twins); deterministic.
- `RuleBaseline`: threshold 0.85 inclusive, its probabilities, and a missing similarity gives `new`.
- `evaluate_model` hand case: accuracy 0.75, ECE 0.4, latency p50 / mean / p95. Errors and invalid choices count as
  wrong. An empty set is rejected.
- `compare` deltas against the baseline.
- `SystemOneDecisionModel` against a fake `/v1/systemone`: request shape, and HTTP 500, invalid choice, malformed or
  unreachable responses raise `DecisionModelError`.
- CLI `decision-eval` writes a report; it exits 2 without a URL.
- `model` test: live DiffusionGemma.

### Interpretation decisions taken by the tester (the reviewer should check these)

1. **Entry point name.** The spec says `python -m eval ...`; the team brief says package `nasa_eval`. Tests use
   `python -m nasa_eval`. A top-level package named `eval` would shadow the builtin name, so the spec text should be
   updated to `nasa_eval`.
2. **Gold `t` units.** `t` is seconds since `manifest.start_time`. Synthetic frames are a `frames/` directory of PNGs
   (manifest `video: "frames"`), and `audio` is null because transcripts are TTS-free.
3. **Duplicate rate.** Defined as the sum over gold objects of `max(0, matched entities - 1)`, divided by the number
   of gold objects. This equals the spec formula when every gold object is found, and never goes negative when some
   are missed. Predicted entities are matched to gold by plurality of their matched sightings.
4. **False-merge rate.** The fraction of gold objects that share a predicted entity with another gold object
   (research/02 §6.1).
5. **Location metrics.**
   - `location_acc` (the gated metric) is zone-level exact match; `location_module_acc` is module-level.
   - When a gold object has duplicate entities, the most recently seen one is scored.
   - The audit time defaults to the last gold appearance.
6. **Events.**
   - Matching is a one-to-one *maximum* matching on (type, entity, |dt| <= tolerance).
   - Actor attribution compares normalised strings (casefold, collapsed whitespace) over matched pairs whose gold
     event has an actor.
7. **Unknown recall.** A gold unknown counts as recalled if its matched entity has an `unknown_object` review in any
   status.
8. **Vacuous conventions.** Pairwise and B-cubed P/R are 1.0 when there are no pairs; detection and event P/R with an
   empty denominator are 0.0; unknown recall with no gold unknowns is 1.0.
9. **QA.**
   - Entity answers are correct when all expected gold objects are among the cited entities (mapped DB entity to
     gold).
   - Location answers come from a citation `location_id` or exactly one known location-id token in the reply.
   - Actor answers match by descriptor substring or a cited person entity.
   - **Contract note:** AC-AGT-1 citations do not define `location_id`. Tests accept it if present, and also fall back
     to the reply text. Adding an optional `location_id` to agent citations would make location scoring fully
     citation-based.
10. **`/v1/systemone` request/response shape.** The shape in `decision_eval.SystemOneDecisionModel`'s docstring is
    assumed; research/06 does not pin the wire format. Adjust it in the Integration phase if the vLLM endpoint
    differs (the same applies to perception's decision client, AC-PER-12).
11. **"Wired into `make test`".**
    - There is no root `Makefile` yet, and the tester may only edit `eval/`. The test requires `eval/Makefile` with a
      `test` target that runs pytest and `python -m nasa_eval gate`.
    - The root `make test` (or CI) must call `make -C eval test`. That needs whoever owns the repo root to add it.
12. **Runner database names.** The runner uses its own prefix (default `eval_`, tests pass `test_eval_`). It refuses
    to drop `inventory`, `postgres`, or any name without its prefix. The admin URL comes from `EVAL_PG_ADMIN_URL` for
    `local`, or `EVAL_CLUSTER_PG_ADMIN_URL` for `cluster`, and is never hard-coded.

## Green phase (2026-10-17, developer)

### Result

Final full run (`cd eval && .venv/bin/python -m pytest -q -p no:warnings`): **254 passed, 2 skipped, 1 deselected**
(257 collected, about 11 s). The 2 skips are the `integration` runner tests (services/perception is not installed in
the eval venv, so they wait for the Integration phase). The 1 deselected test is the `model` test, which needs a live
DiffusionGemma at `DECISION_URL`. `ruff check .` is clean and `ruff format` has been applied to `nasa_eval/`.
`make test` runs pytest and then `python -m nasa_eval gate`. No `test_eval_*` or `eval_*` databases are left behind,
and the `inventory` database was never opened.

### Test changes

None. No test was edited, weakened, skipped or deleted.

### What was implemented

- **Schemas** (`nasa_eval/schemas/*.schema.json`, Draft 2020-12, shipped as package data):
  - gold: `additionalProperties: false`; `if/then/else` enforces "unknown if and only if `catalog_id` is null";
  - manifest: the optional keys are `name` (defaults to the directory name), `fps`, `transcript`, `description`,
    `seed`, and `zones` (normalised zone rectangles written by the generator);
  - qa: the `expected` shape is conditional on `answer_type`; `free_text` requires a `rubric`;
  - pairs: `observation` and `candidate` must each have a `crop`.
- **`dataset.py`** (validation):
  - Every error carries `file:line`. Schema errors are collected first, then semantic checks: duplicate ids, bbox
    ordering, empty gold, QA entities that must exist in gold, and optional referential ids.
  - YAML is read with a SafeLoader subclass that does not convert timestamps. Without it, PyYAML would silently turn
    `start_time` into a datetime and a naive value would get past the timezone check.
- **`dataset.py`** (loading):
  - `iter_source()` reads the PNG/JPEG frames directory. When the manifest names a 16-bit WAV file, it also slices
    the audio per frame.
  - `seed_ids()` parses `contracts/db/002_seed.sql` for `validate --referential`.
- **`synth.py`**:
  - Procedural icons for 10 catalog classes plus `unknown`, drawn with PIL on dark class "cards". All randomness comes
    from `numpy.random.default_rng([seed, class index])`, never `hash()`.
  - Scenes use three Destiny rack zones (`LAB1S2`, `LAB1O3`, `LAB1P3`). The seed varies the classes, timings and
    positions; it also picks the actors and slots, which never overlap.
  - Each scene contains:
    - a mover with a `moved` event, an actor and a scripted line;
    - identical twins (the same crop) in two zones;
    - an object that appears part-way through;
    - an object that is removed (`removed` event);
    - exactly one unknown.
  - Output is byte-identical for the same seed, including across processes.
  - `generate_pairs` writes positives (an augmented second view of the same instance) and negatives that alternate
    between hard same-class twins (`hard_negative: true`) and other-class items.
  - `context.similarity` is simulated with overlapping ranges, so the rule baseline makes realistic mistakes.
- **`metrics.py`**: pure functions with no I/O, as specified in the red-phase decisions.
  - Detection matching is greedy one-to-one by IoU and then time.
  - Event matching is a maximum bipartite matching (augmenting paths).
  - ECE uses equal-width bins, with 1.0 in the last bin.
- **`snapshot.py`**:
  - Reads `location`, `entity`, `sighting`, `event` and `review_item`.
  - Merged entities are dropped from the live set. Their sightings, events and reviews are re-attributed by following
    the `merged_into` chain.
- **`scoring.py`**:
  - `match_sightings` processes sightings in their given order. Each takes the best free appearance: highest IoU,
    then same location, then smallest dt.
  - ER metrics are keyed by gold appearance key. The duplicate and false-merge rates divide by every gold object.
  - Location uses the pipeline's final `last_location` of the most recently seen entity per gold object. If that is
    missing, it falls back to the location of the entity's last sighting.
  - Person-kind entities are excluded from item scoring.
- **`qa.py`**:
  - Exact scoring follows the red-phase rules. Location tokens are matched on alphanumeric boundaries, so `LAB1` does
    not match inside `LAB1P3`.
  - `OpenAIJudge` asks for strict JSON and accepts it plain, fenced, or embedded in prose. Shape errors and HTTP or
    transport failures become error verdicts. The optional bearer token comes from `EVAL_JUDGE_API_KEY`.
  - `score_qa` refuses judge == generator, appends a JSONL row to the agreement log for every judged item, and
    reports raw agreement and kappa against human labels.
- **`gate.py`** and `eval/thresholds.yaml`: the thresholds hold the spec values. `run_gate` prints a per-metric table
  and returns 0, 1 or 2.
- **`runner.py`**:
  - **Configuration:** the admin URL comes from `EVAL_PG_ADMIN_URL` (`local`) or `EVAL_CLUSTER_PG_ADMIN_URL`
    (`cluster`); a missing one raises `ConfigError`.
  - **Database safety:** database names are created and dropped with `psycopg.sql.Identifier`. A prefix that collides
    with `inventory`, `postgres` or `template*` is refused. Dropping refuses reserved names and any name outside the
    prefix.
  - **Run flow:** the dataset is validated before any database work. The database is dropped in a `finally` block,
    and a replay failure writes no report.
  - **Report:** it embeds an informational gate summary. The Markdown report has tables for metrics, per-class results
    and counts.
- **`cli.py`**: `validate [--referential]`, `generate [--pairs N]`,
  `run --dataset --target {local,cluster} [--keep-db]`, `gate [--report|--reports-dir] [--thresholds]
  [--allow-missing-report]`, `decision-eval --pairs [--decision-url|$DECISION_URL] [--model|$DECISION_MODEL]`.
- **`decision_eval.py`**:
  - Exceptions and choices outside {same, new} count as wrong (`n_errors`).
  - ECE and latency are computed over the valid answers. Percentiles use linear interpolation.
  - The `/v1/systemone` client follows the shape in its docstring (red-phase decision 10).
- **`eval/Makefile`**: `test` runs pytest, then `nasa_eval gate $(GATE_ARGS)`; `eval` runs generate, run and a strict
  gate; there are also `lint` and `venv` targets.

### Notes for the reviewer and the Integration phase

1. **`make test` gate on a fresh checkout.** `GATE_ARGS` defaults to `--allow-missing-report`, so `make test` passes
   while `eval/reports/` holds no report yet. Without the flag the gate would always exit 2 on a fresh checkout. Once
   any report exists, a regression still fails `make test` with exit 1. For a strict run, use `make eval` or
   `make test GATE_ARGS=`.
2. **`default_replay` needs an inventory-api.** It imports `perception.replay`, `perception.fakes` and
   `perception.inventory_client.HttpInventoryClient`. Perception writes through the inventory-api, not straight into
   Postgres, so the runner also needs `EVAL_INVENTORY_API_URL`. That must be an inventory-api serving the fresh eval
   database. Two things are still open for the Integration phase:
   - **Database binding:** how the API gets bound to each fresh database, for example by starting the API in-process
     with `DATABASE_URL`.
   - **Fake collaborators:** the scripted `FakeOmni` / `FakeDetector` must be driven from the gold data. The synthetic
     crops are not `perception.fakes.object_color` rectangles.
3. **Contract notes** (no contract was edited):
   - **Agent citations:** an optional `location_id` on agent citations (AC-AGT-1) would make QA location scoring fully
     citation-based. Today it falls back to exact location-id tokens in the reply.
   - **Entry point:** spec text `python -m eval` should read `python -m nasa_eval` (red-phase decision 1).
4. **Repository root:** the root `make test` (or CI) still has to call `make -C eval test` (red-phase decision 11).

## Review round (2026-10-17, developer, fix round)

Every high and medium finding was handled test-first: the test that exposes the bug was written and seen failing
before the code changed. **Red run** after adding the new and strengthened tests: 43 failed, 198 passed, 3 skipped,
plus 3 collection errors (`test_ac_eval_03_scoring.py`, `test_ac_eval_06_gate.py` and
`test_ac_eval_07_decision_eval.py` imported APIs that did not exist yet). **Green run**:
`cd eval && .venv/bin/python -m pytest -q -p no:warnings` gives **325 passed, 2 skipped, 1 deselected**. The 2
skips are the perception integration tests: `perception.replay.run` still raises `NotImplementedError`, and
perception is not installed in the eval venv. The deselected test is the live DiffusionGemma `model` test. `ruff check
.` is clean, and `ruff format` was applied to `nasa_eval/`. `make test` exits 0. No `test_eval_*` or `eval_*`
databases are left behind.

The inventory-api is now installed in the eval venv (`uv pip install -e ../services/inventory-api`, part of the new
`pipeline` extra). As a result, `test_ac_eval_05_inventory_api_bound_in_process_to_fresh_db` runs: it binds the real
inventory-api in-process to a fresh `test_eval_*` database and reads `/catalog` and `/locations`. In a venv without
the inventory-api (`make venv` only), this test skips and the totals are 324 passed and 3 skipped.

### Tests changed (strengthened or replaced; none weakened, skipped or deleted)

| Test | Change | Why |
|---|---|---|
| `test_ac_eval_03_er_only_shared_sightings_count` | Replaced by `..._missed_gold_items_lower_recall`, `..._spurious_pred_items_lower_precision`, `..._empty_prediction_is_not_perfect` and `..._both_empty_is_vacuous`, with hand-computed values | It made the vacuous behaviour a requirement (finding 1) |
| `test_ac_eval_03_score_empty_run` | Also asserts `pairwise_f1 == 0`, `sighting_recall == 0`, `object_recall == 0`, and that the repo gate FAILS | Finding 1 |
| `test_ac_eval_03_gold_locations_at_audit_time` | `laptop-1` (removed at t=6) is out of scope at t=10 and still in `LAB1P3` at t=5 | Finding 6 |
| `test_ac_eval_03_score_audit_time_override` | At `audit_t=5` a perfect pipeline scores 1.0, and a wrong sighting at t=3 scores 0.8 | It required the final-state mismatch (finding 6) |
| `test_ac_eval_03_score_wrong_location` | 0.8 becomes 0.75: the same wrong zone, but the denominator is now 4 because the removed laptop is out of scope | Finding 6 |
| `test_ac_eval_07_generate_pairs_balanced_with_crops` | The hard-negative flag is read from `meta`, not `context` | Finding 2 |
| `test_ac_eval_07_systemone_client`, `..._bad_responses_raise`, `..._cli_decision_eval_writes_report` | Production request/response shape: `questions` is a dict, `criteria` is candidate id + `NEW`, `state` is an object | Finding 9 (AC-EVAL-7 vs production) |
| `test_ac_eval_04_no_human_labels_no_kappa` | Passes `reports_dir=tmp_path` | The agreement log is now always written (finding 8), and must not go into the repo |

### Findings and resolutions

1. **HIGH: ER metrics vacuous on empty or sparse output (AC-EVAL-3/6). Fixed.**
   - **Union of items.** `metrics.pairwise_prf` and `bcubed_prf` now run over the union of predicted and gold
     items. A gold-only item (a missed appearance) is padded as a singleton predicted cluster, and a predicted-only
     item (a spurious sighting) as a singleton gold cluster.
   - **What the scorer feeds them** (`scoring.score`): every predicted object sighting, plus every gold appearance
     that no sighting of the same object covers within the match tolerance.
   - **New metrics:**
     - `sighting_recall`: covered appearances / appearances;
     - `object_recall`: gold objects with at least one entity / gold objects;
     - `spurious_entity_rate`: predicted entities that match no gold object / predicted entities.
   - **New gate:** `thresholds.yaml` gains `sighting_recall: {min: 0.8}`.
   - **Result on synth seed 7:**

     | Run | pairwise_f1 | Gate |
     |---|---|---|
     | Empty | 0.0 | Fails |
     | One sighting per object | below 0.1 (`sighting_recall` below 0.3) | Fails on `pairwise_f1` and `sighting_recall` |
     | Perfect per-frame | 1.0 | Passes |
     | Perfect per-window | 1.0 | Passes |

   - **Duplicate rate.** It keeps the per-gold formula, `Σ max(0, entities − 1) / gold objects`. See finding 15.
2. **HIGH: decision pairs leak their labels; crops unused (AC-EVAL-7). Fixed.**
   - **Context.** `generate_pairs` writes only `context = {similarity, catalog_id}`. `catalog_id` is always the
     observation's class. The stratum goes into an evaluation-only `meta = {kind, hard_negative}`. The pairs schema
     gains `meta`, and `load_pairs` moves a legacy `context.hard_negative` into `meta`.
   - **What decide() sends.** `build_question` sends only whitelisted fields. It never sends `pair_id`, `label`,
     `meta` or crop paths.
   - **Crops.** Both crops are sent as PNG data URIs (`image`).
   - **Similarity.** `similarity` is computed from the two crops with an embedder interface: the weight-free,
     deterministic `CropEmbedder` by default; any `embed(crop)` such as perception's DINOv2 can be plugged in.
   - **Per-stratum accuracy.** It is reported as `accuracy_by_kind`.
   - **Effect.** The leak rule from the review now scores 0.5, which is exactly the share of positives. The rule
     baseline on 400 pairs scores 0.718 at threshold 0.85: 1.0 on class negatives, 0.54 on hard negatives and 0.665
     on positives. Hard negatives are the stratum where the model has to use the pixels.
   - **Tests:** `test_ac_eval_07_generated_context_carries_no_label_correlated_fields`,
     `..._similarity_is_computed_from_the_crops`,
     `..._request_never_contains_label_metadata_and_carries_crops`,
     `..._legacy_hard_negative_in_context_moves_to_meta`, `..._crop_paths_confined_to_pairs_dir` and
     `..._accuracy_reported_per_stratum`.
3. **HIGH: a decision report breaks `make test` (AC-EVAL-6). Fixed.**
   - **Report locations.** Decision reports go to `reports/decision/<ts>-decision.json` with `kind: decision`. QA
     reports go to `reports/qa/` with `kind: qa`. Pipeline reports carry `kind: pipeline` and `source`
     (`replay` or `snapshot`).
   - **What the gate considers.** `latest_report` and `latest_reports` only consider pipeline reports, plus legacy
     reports that have `metrics` and no `kind`. Unreadable files stay candidates, so a corrupt report still fails
     with exit 2.
   - **Tests:** `test_ac_eval_06_decision_report_never_gated`, `..._only_pipeline_kind_is_gated`,
     `..._cli_decision_eval_default_out_is_not_a_pipeline_report` and `test_ac_eval_05_report_kind_and_source`.
4. **HIGH: the gate in `make test` was a no-op. Fixed.**
   - **What `make test` runs now:** `unit gate-ci gate-local`. `gate-ci` scores a committed, deterministic snapshot
     against a committed gold dataset with the new `score --snapshot` command (no replay, no database), then gates
     the result strictly, with no `--allow-missing-report`.
   - **Committed data.**
     - `eval/datasets/synth-ci/`: manifest, gold and transcript of seed 7. Frames are not committed; they
       regenerate with `generate --seed 7 --name synth-ci`. A test checks the committed gold is exactly what the
       seed produces.
     - `eval/baselines/synth-ci.snapshot.json`: the reference per-window snapshot, `source: reference`, from
       `nasa_eval/baseline.py`. It has realistic flaws: one missed window, one spurious entity, a `moved` event 2 s
       late, and a `removed` event without an actor.
     - Replace the snapshot with a recorded perception run once one exists, using the new
       `snapshot export --db-url` after `run --keep-db`.
   - **New files:** `eval/.gitignore` ignores generated reports under `reports/**` and the synth-ci frames.
   - **Tests:**
     - `make gate-ci` passes on the committed snapshot and fails on a regressed one (`CI_SNAPSHOT=…`);
     - `make -n test` contains `score --snapshot` and a strict CI gate;
     - the committed data validates against the seed ids.
   - **Repository root:** a root Makefile or CI still has to call `make -C eval test`. That is outside `eval/`.
5. **HIGH: the runner was not truly proven; the default replay could not produce output (AC-EVAL-5). Fixed, up to
   what perception currently implements.** The new `nasa_eval/collaborators.py` provides the following.
   - **`local` target: fakes scripted from gold.**
     - `GoldDetector` returns the gold boxes of the frame it is given. It recognises the frame by thumbnail
       nearest-neighbour, so it is independent of call order and of perception's 448-px downscale.
     - `GoldOmni` answers each window from its frame timestamps: the gold objects (catalog `class_name` labels, a
       non-catalog label for unknowns), the gold events (each reported once even though windows overlap, with
       actors as `P1..` people), and the transcript lines. Its output is validated with perception's observation
       schema.
     - `gold_asr` is a FakeASR that returns the scripted lines by audio time.
     - The rest are `FakeEmbedder` and `MemoryStorage`. The decision model is the real `DecisionClient` when
       `DECISION_URL` is set. Otherwise it is FakeDecision, which abstains; that is conservative, because a
       decision-band case then shows up as a duplicate or review rather than a silent merge.
   - **`cluster` target: perception's real clients from `PerceptionConfig.from_env`.**
     - `OMNI_URL` and `DECISION_URL` are required, and `ASR_URL` is optional.
     - The detector and embedder are OWLv2 and DINOv2 (lazy loading).
     - Storage is `MemoryStorage`, so the eval never writes to the deployment bucket.
   - **API binding.** `bind_inventory_api(url)` runs the inventory-api in-process (FastAPI TestClient plus an
     in-memory object store) on the fresh database. `EVAL_INVENTORY_API_URL` is gone.
   - **Integration test.** It is now self-contained and skips only while perception or the inventory-api is not
     importable.
   - **Tests:** `test_ac_eval_05_collaborators.py`, 9 tests. They put `services/perception` on `sys.path` for one
     test through the new `perception_on_path` fixture.
   - **Still open:** perception's `replay.run` and `Reconciler.process` are still stubs, so the end-to-end replay
     cannot run yet. That is Integration phase.
6. **MEDIUM: audit-time location ignored events and used the final state. Fixed.**
   - **Gold location.** `gold_locations_at` takes the latest evidence among appearances and events. An event wins a
     tie, an event's `to_location` moves the object, and `removed` or `consumed` puts it out of scope (out of the
     denominator).
   - **Predicted location.** It comes from the pipeline's sightings and events up to `audit_t`. A predicted removal
     counts as wrong while the gold object is still present. The entity's final `last_location` only applies once
     `audit_t` is at or after its `last_seen_at`.
   - **Default audit time.** It now also covers event times.
   - **Tests:** `..._gold_location_follows_moved_event_without_appearance`, `..._score_removed_object_out_of_scope`
     and `..._score_predicted_removal_too_early_is_wrong`, plus the updated tests in the table above.
7. **MEDIUM: one-to-one matching hid same-frame duplicates; per-frame tolerance. Fixed.**
   - **Matching.** The new `assign_sightings` is many-to-one and is used for entity resolution and coverage.
     One-to-one matching is kept only for detection P/R (`detection_prf`) and for the existing `match_sightings`
     helper.
   - **Tolerance.** `score(..., match_time_tolerance_s=…)` is configurable. The default is
     `DEFAULT_MATCH_TOLERANCE_S = 5.0`, one perception window span (6 frames at 1 fps, stride 4 s). The review
     suggested the stride (4 s), but a sighting stamped with its window's end time can be up to 5 s after the frame
     where the object was seen. The reference snapshot showed exactly that case: the removed object's last window
     ends 5 s after its last appearance.
   - **Tests:** `test_ac_eval_03_score_same_frame_duplicate_entities` (`duplicate_rate` 0.2) and
     `..._synth_per_window_sightings_match_with_window_tolerance`. With a 0.5 s tolerance, a per-window pipeline
     drops below 0.5 `sighting_recall`.
8. **MEDIUM: QA scoring could be gamed. Fixed.**
   - **Entity answers** need an exact set: citation precision and recall are both 1.0, and they are reported in
     `detail`. A cited entity with no gold mapping counts as a wrong citation.
   - **Actor answers** must name exactly one known descriptor, and it must be the expected one. Descriptors come from
     the gold events. A generic descriptor contained in a longer one does not count as a second actor.
   - **Location answers** take the most recent citation by `at`. Conflicting citations without a usable `at` count
     as hedging.
   - **Tests:** over-citation, unmapped citation, listing every descriptor, nested descriptor, latest citation,
     and conflicting citations without time.
9. **MEDIUM: judge independence, agreement logging, no runnable QA path. Fixed.**
   - **Judge independence.** `score_qa` and `OpenAIJudge` raise when the generator model is missing, empty or blank
     while a judge is configured.
   - **Agreement log.** It is always written, by default to `reports/qa/<ts>-<dataset>-judge.jsonl`.
   - **Runnable path.** The new helpers are `qa.ask_all`, where an agent failure is recorded and scored as wrong,
     and `runner.qa_section`. `run(..., agent_factory=…)` asks the questions to an agent bound to the same fresh
     database before it is dropped, and the report gets a `qa` section.
   - **New CLI command:** `qa --dataset X --run-report R --agent-url U [--judge-url --judge-model
     --generator-model]` works against a database kept with `run --keep-db`. It writes
     `reports/qa/<ts>-<dataset>-qa.json`.
   - **Not done here:** starting the agent service in-process (its `create_app` needs `DATABASE_URL`,
     `INVENTORY_API_URL` and an LLM) is left to the Integration phase.
10. **MEDIUM: eval measured a different task than production (AC-EVAL-7). Fixed.**
    - **Request.** `SystemOneDecisionModel` sends the production question: `same_or_new` as a choice over candidate
      ids plus `NEW` (`criteria` dict, perception's `DEFAULT_INSTRUCTIONS`, `state` object). A pair is the k = 1
      case, one candidate `E1` plus `NEW`, and that binary reduction is documented in the module docstring.
    - **Contract test.** `test_ac_eval_07_request_matches_perception_decision_client` checks that the eval body
      equals the body `perception.decision.DecisionClient.choose` sends for the same arguments (`build_question`).
    - **Not covered:** the state layout the reconciler will send is not implemented in perception yet. Align it in
      the Integration phase.
11. **MEDIUM: the synthetic transcript never reached the pipeline. Fixed.**
    - **Audio.** When there is no audio file, `Dataset.iter_source()` yields 16 kHz int16 chunks with a tone while a
      scripted line is spoken (3 s per line, RMS about 2100, above perception's VAD threshold of 300) and silence
      otherwise.
    - **Transcript.** `Dataset.transcript_lines()` reads the transcript. The local target's `gold_asr` returns the
      lines by audio time, and `GoldOmni` also carries them in the omni transcript.
    - **Not done:** person silhouettes are not drawn. Actors reach the pipeline through the omni `people` and
      `actor_ref`.
12. **MEDIUM: unsanitised dataset name and paths. Fixed.**
    - **Names.** The manifest `name` must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`. A directory name used as the default
      name is checked too.
    - **Paths.** `video`, `audio` and `transcript` must resolve inside the dataset directory. Pair crop paths must
      resolve inside the pairs directory.
    - **Reports.** `report_basename` rejects unsafe names. `run()` writes the report before dropping the database.
    - **Tests:** `test_ac_eval_01_manifest_name_must_be_a_safe_basename` (7 names),
      `..._media_paths_must_stay_inside_the_dataset` (5 cases), `..._directory_name_used_as_default_name_must_be_safe`,
      `test_ac_eval_05_report_basename_rejects_unsafe_names` and `test_ac_eval_05_report_written_before_db_is_dropped`.
13. **LOW: gate scope. Fixed.** Without `--report`, the gate checks the newest pipeline report of every dataset
    (`latest_reports`). Test: `test_ac_eval_06_gate_latest_report_per_dataset`.
14. **LOW: referential validation skipped in `run`. Fixed.** `run()` checks catalog and location ids from gold,
    events and QA, and the manifest camera (the new `seed_camera_ids`), against the seed before provisioning.
    Test: `test_ac_eval_05_referential_errors_fail_before_provisioning`.
15. **LOW: spec deviations not recorded. Recorded here, with proposed spec wording.**
    - **Duplicate rate (kept as a deliberate deviation).** The literal formula `(matched entities − gold objects) /
      gold objects` goes negative when objects are missed, and it hides duplicates; an empty run would score −1.0.
      The implementation uses `Σ_gold max(0, entities assigned − 1) / gold objects`. It equals the spec formula when
      every object is found. Unmatched predicted entities are reported separately as `spurious_entity_rate`.
      Proposed AC-EVAL-3 wording: "duplicate rate = Σ over gold objects of max(0, predicted entities matched to it −
      1) / gold objects".
    - **Procedural crops instead of catalog crops (deviation).** The synth draws procedural icons per catalog class,
      because the seed's `reference_image_uri` values point into S3 and no images are committed.
      Proposed AC-EVAL-2 wording: "...by compositing catalog object crops (procedurally drawn per catalog class when
      no reference images are available)...".
    - **No `no_capture` scenario (deferred).** No synth scene has a `no_capture` segment, so the gated
      `stored_frames_in_no_capture == 0` metric is not implemented. Measuring it needs perception's AC-PER-11
      no-capture handling, which is not implemented yet. It also needs a `location_hint` path into a `no_capture`
      location: `GoldOmni` would emit it, and `MemoryStorage` keys would be checked by time. Proposed for the
      Integration phase.

### Notes for the Integration phase

- **Pipeline venv.** `make venv-pipeline` installs perception and the inventory-api. The `pipeline` extra uses
  `[tool.uv.sources]` path dependencies. Once `perception.replay.run` is implemented, the self-contained integration
  test (`test_ac_eval_05_integration_synthetic_scene_through_perception`) runs `run(target="local")` end to end with
  the gold fakes.
- **Zone-level location.** The omni observation schema has only a window-level `location_hint`. Unless perception
  maps boxes to camera zones (the synth manifest carries `zones` rectangles), `location_acc` (zone level) on synthetic
  scenes will be low. This is a perception design point to settle.
- **Contract note, unchanged.** An optional `location_id` on agent citations (AC-AGT-1) would make location QA fully
  citation-based.

## Integration phase (2026-10-17, integration engineer)

Details in `docs/tdd-log/integration.md` §1. Summary:

- **Integration tests now run** (the `pipeline` extra is installed). The real replay of seed 7 first failed the
  gate:
  - `duplicate_rate` 0.167: the `local` target used perception's colour-keyed `FakeEmbedder`, which splits moved
    synth objects after downscale.
  - `location_acc` 0.0: there were no camera zones.
- **Fixes:**
  - `ThumbnailEmbedder` for the `local` target;
  - `Manifest.zones`;
  - `collaborators.replay_config()`, which passes the manifest zones to perception as `camera_zones`.
- **New tests:** `test_ac_eval_05_manifest_carries_camera_zones`, `..._replay_config_applies_manifest_zones` and
  `..._local_embedder_reidentifies_synth_crops`.
- **Test changes after red (strengthening only):**
  - `test_ac_eval_05_build_local_collaborators` now expects `ThumbnailEmbedder`.
  - `test_ac_eval_05_integration_synthetic_scene_through_perception` uses the `test_int_` prefix and asserts the
    database is dropped, `source == "replay"` and every window was posted.
- **Result:** 329 passed, 1 skipped (by design), 1 deselected. `make test` passes in `eval/`.
- **Proposal:** add `false_merge_rate: {max: 0.05}` to `thresholds.yaml`. A twin swap passed the current gates.
