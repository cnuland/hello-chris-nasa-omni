# Spec: perception (services/perception)

Ingests video + audio, runs the fast path (detector + embeddings) and the omni window, reconciles results into
inventory updates and posts them to `/ingest/window`.

- AC-PER-1 **Ingest** reads a video source (file path, looping allowed, or URL) with ffmpeg and yields keyframes at a
  configured fps (default 1) plus time-aligned audio chunks (16 kHz mono PCM, default 5 s). Timestamps are monotonic
  wall-clock (`start_time + media pts`).
- AC-PER-2 **Window builder** groups the last N keyframes (default 6) with the overlapping audio chunk into a window
  every `stride` seconds (default 4). Frames are downscaled to the configured long side (default 448 px).
- AC-PER-3 **Omni client** sends a window to an OpenAI-compatible `/v1/chat/completions`:
  - images as `image_url` data URIs and audio as `input_audio` (wav);
  - a system prompt with the catalog class names and the observation schema;
  - `output_modalities`/`modalities` set to text only.
  It parses the first JSON object in the reply and validates it against `contracts/schemas/observation.schema.json`.
  On invalid JSON it retries once with a repair instruction; if that also fails, it emits an `invalid` result and
  never writes it.
- AC-PER-4 An optional **realtime client** (flag) uses `/v1/realtime?duplex=0`: it sends `input_image` items and
  appends audio to the input buffer (24 kHz PCM16), commits, sends `response.create` with text output, and collects
  the text deltas. It is tested against a fake WebSocket server.
- AC-PER-5 The **detector** interface `detect(frame, labels) -> [Detection(label, bbox, score)]` has an OWLv2
  implementation (transformers, CPU allowed) and a deterministic fake for tests. Detections below `min_score` (0.15)
  are dropped.
- AC-PER-6 The **embedder** interface `embed(crop) -> 768-d L2-normalised vector` has a DINOv2-base implementation
  and a deterministic fake. The same crop gives the same vector.
- AC-PER-7 **Label → catalog mapping** uses exact or synonym match first, then fuzzy and embedding similarity to
  catalog names. Below the floor (0.55), the result is `UNKNOWN`. It never creates entities keyed on free text alone.
- AC-PER-8 **Reconciler** decision per observed object, with the omni objects linked to detector boxes by IoU or
  label:
  1. retrieve candidates (`/ingest/candidates`);
  2. apply hard gates: class mismatch; impossible transit (different module within `min_transit_s`, default 20 s,
     for a non-held object); the same entity matched twice in one window → only the best keeps it;
  3. score: `p = f(similarity, class match, zone match, recency)`;
  4. bands: p ≥ 0.85 assign; 0.5 ≤ p < 0.85 ask the decision model (`choice` over candidates + `NEW`) and assign if
     its top probability ≥ 0.6, else create a provisional entity plus a `possible_duplicate` review; p < 0.5 create a
     new provisional entity;
  5. a catalog class whose live (non-merged) entity count would exceed `expected_count` → `possible_duplicate`
     review;
  6. `UNKNOWN` class → `unknown` entity + `unknown_object` review with crop image, description and timestamp.
- AC-PER-9 **Fungible** catalog items update `stock` (count per zone = max over the window) instead of creating
  instances. *(Contract addition: `/ingest/window` accepts `stock_updates`.)*
- AC-PER-10 **Events**: omni `events` map to inventory events for the matched entity, with the actor descriptor from
  `people[ref].appearance` and evidence = the frame at `frame_index` stored to S3. A transcript statement like
  "putting the drill in node 2" yields a `moved` event with source `speech` when it matches a catalog item and a
  location.
- AC-PER-11 **Location**: the camera's default location, overridden by `location_hint` when it maps to a known
  location id. Windows from `no_capture` locations are summarised only: no frames or crops stored, and no reviews.
- AC-PER-12 **Decision client** calls `/v1/systemone` (choice) or chat fallback. It returns
  `{choice, probabilities, latency_ms, model}` and logs every call into `decisions` of the ingest payload. On timeout
  or error it returns `NEW` with `probabilities={}` and marks the item for review.
- AC-PER-13 **Storage**: frames, crops and the raw model response go to S3 under
  `frames/<camera>/<date>/<ts>.jpg`, `crops/...`, `raw/...`. Uploads are skipped in no-capture.
- AC-PER-14 **End to end** (fake omni + fake detector + fake decision + test DB via inventory-api): a scripted
  20-window scenario with 3 objects (one seen in 10 windows, one moved, one unknown) yields exactly 3 non-merged
  entities, 1 `moved` event and 1 `unknown_object` review.
- AC-PER-15 **ASR path** (research/03): audio chunks are also sent to a dedicated ASR endpoint (`ASR_URL`,
  OpenAI-compatible `/v1/audio/transcriptions`; Qwen3-ASR-1.7B on vLLM). It passes a context prompt built from catalog
  names and module codes, to bias toward ISS vocabulary. The ASR transcript is the stored record (window.transcript);
  the omni transcript is used only as a fallback when ASR is unavailable.
- AC-PER-16 **Speech claims**: an utterance is classified as `statement` / `question` / `other`, by the decision model
  with a rule fallback (question marks, interrogatives, modal "should I"). Only statements produce `speech` events.
  Speech-only events get `confidence ≤ 0.6` and the entity is flagged `needs_review` until a visual sighting in the
  claimed location within 10 min confirms it, after which the flag clears.
- AC-PER-17 Silence handling: windows whose audio RMS is below the VAD threshold skip ASR. Empty or hallucinated
  transcripts (the same phrase repeated more than 3 times, or text on silence) are discarded.
- AC-PER-18 **Public replay interface** (used by eval and smoke tests):
  `perception.replay.run(source, *, camera_id, start_time, api: InventoryClient, omni, detector, embedder, decision,
  asr, storage, config) -> ReplayStats`, plus the CLI `python -m perception replay --source PATH --camera ID`.
  - `source` may be a video path or an iterable of `(timestamp, frame_ndarray, audio_chunk|None)`, so synthetic eval
    scenes can be fed without encoding video.
  - All collaborators are injectable interfaces with fakes in `perception.fakes` (FakeOmni driven by a scripted
    observation list, FakeDetector, FakeEmbedder that is deterministic per object id, FakeDecision, FakeASR,
    MemoryStorage).
