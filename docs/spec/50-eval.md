# Spec: eval (eval/) — identification pipeline evaluation

- AC-EVAL-1 **Gold dataset format** `eval/datasets/<name>/`: `manifest.yaml` (video, audio, camera, start time) +
  `gold.jsonl` (per-object: gold_object_id, catalog_id, appearances [t, location, bbox?], events [type, t, actor,
  to_location], is_unknown). Validated by a JSON schema.
- AC-EVAL-2 **Synthetic generator** builds deterministic scenes by compositing catalog object crops (with seeds) onto
  ISS-like backgrounds over time (appear, move zone, disappear, identical twins, an unknown object), plus TTS-free
  scripted transcripts. It emits video frames + gold. Same seed ⇒ identical output.
- AC-EVAL-3 **Metrics** (pure functions, unit-tested on hand-computed cases):
  - detection/class precision, recall, F1 per catalog class;
  - entity resolution: pairwise P/R/F1 and B-cubed P/R/F1 over sightings → predicted entity vs gold object;
  - **duplicate rate** = (pred entities matched to gold − gold objects) / gold objects;
  - **false-merge rate**;
  - location top-1 accuracy at audit time (module + zone);
  - event P/R/F1 with type + entity + |Δt| ≤ tolerance (default 5 s), and actor-attribution accuracy;
  - unknown-detection recall (gold unknowns that reached the review queue).
- AC-EVAL-4 **Chatbot QA eval**: a question set (`qa.jsonl`: question, expected entity/location/actor, answer type).
  Scoring is exact on entity/location ids from citations, plus an optional LLM-judge for free text (judge ≠
  generator) with rubric and agreement logging.
- AC-EVAL-5 **Runner** `python -m eval run --dataset X --target {local,cluster}` replays the dataset through
  perception into a fresh DB, scores it, and writes `eval/reports/<ts>-<dataset>.{json,md}`.
- AC-EVAL-6 **Gates**: `eval/thresholds.yaml` holds minimums (e.g. duplicate_rate ≤ 0.10, pairwise_f1 ≥ 0.85,
  location_acc ≥ 0.8, unknown_recall ≥ 0.9). `python -m eval gate` exits non-zero when any metric regresses below its
  threshold. Wired into `make test`.
- AC-EVAL-7 **Decision-model eval**: a same-or-new pair set (positives/negatives with crops + context) measures
  DiffusionGemma choice accuracy, calibration (ECE) and latency vs the rule baseline.
- AC-EVAL-10 **Real-video location eval from captions** (`eval/nasa_eval/real/`, `make eval-real`). Ground truth
  for real footage comes from the public-domain NASA "Step Inside the International Space Station" tour captions
  (`tour.srt`, treated as untrusted data: parsed, never executed).
  - `srt_truth.parse_srt` returns cues `(t_start_s, t_end_s, text)` (BOM/CRLF tolerant; malformed blocks skipped).
  - `srt_truth.module_timeline` derives module segments. Only **title cues** start a segment: a cue whose whole text
    is a module name plus filler words ("Columbus Laboratory Module", "Japanese Experiment Module “Kibo”",
    "Cupola"). Descriptions that mention a module in a sentence, the title card and topic cards are ignored. Module
    names map to the module codes of `contracts/db/002_seed.sql` (distinctive words of the display names; "kibo" is
    pinned to JPM1). A named module that the seed lacks (Quest airlock, PMA, Poisk, ...) starts an **unmapped**
    segment (code `null`, not scored). A segment lasts until the next title cue or the end of the video.
  - `score_run` scores a run against the timeline. Input: the inventory API (read-only: `GET /locations`,
    `GET /items?status=<all>` paged, `GET /items/{id}/history`) or a JSON export
    `{"windows": [{t_start, t_end, sightings: [{location_id, seen_at}]}], "locations": [...]}` produced by
    `psql` via `oc exec` (`python -m nasa_eval.real.score_run --print-export-sql`). The API has no window list, so in
    API mode windows are rebuilt from sighting times (a sighting more than `window_gap_s`, default 10 s, after
    the first sighting of the current window starts a new one);
    windows without any sighting are invisible there and only the export sees them.
  - **Alignment assumption** (configurable): video offset of a window = (`t_start` − first window `t_start`) +
    `first_offset_s` (default 0: the replay starts at video t=0 and the first window's frames start there at 1 fps),
    taken modulo `video_duration_s` (default 897 s) when the replay loops. The window is judged at its mid time.
  - Metrics: `window_location_acc` = scored windows whose predicted module (modal module of its sightings, zones
    rolled up to their module, segment-level `USOS`/`ROS` ignored; none ⇒ `unresolved`) equals the caption module at
    the window's mid time / scored windows (windows before the first title cue or in an unmapped segment are not
    scored). `module_coverage` = caption modules reached by ≥1 scored window that were predicted correctly at least
    once / caption modules reached. Plus the per-module confusion matrix (truth → predicted → count).
  - Output `eval/reports/real/<ts>-real-<name>.{json,md}` (`kind: "real_video"`, git-ignored, never gated).
    `thresholds.yaml` holds a **report-only** `report_only.real_video` section (target `window_location_acc ≥ 0.7`);
    `gate` ignores `report_only`, the report lists each target as met / missed.
