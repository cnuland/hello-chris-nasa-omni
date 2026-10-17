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
