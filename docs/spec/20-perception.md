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

## Amendments: real-video dedup failure (cluster integration 2026-10-17)

Real NASA ISS tour footage through Qwen2.5-Omni-7B produced 10 `headset`, 7 `tablet`, 7 `camera` and 6 `laptop`
entities after about 6 windows, nearly all of them the same one or two physical objects. AC-PER-19 to AC-PER-23
fix the four measured causes. AC-PER-2, AC-PER-3, AC-PER-8 and AC-PER-11 are amended as stated below.

- AC-PER-19 **Within-window consolidation** (amends AC-PER-8). Before linking, the omni objects of one window are
  consolidated. Two objects are the same instance when all of these hold:
  - same key: the same catalog class, or the same normalised label for `UNKNOWN`;
  - not fungible (fungible classes keep per-frame counting, AC-PER-9);
  - their omni boxes overlap with IoU ≥ `consolidate_iou` (default 0.5, `PERCEPTION_CONSOLIDATE_IOU`), **or** at
    least one of them has no box and they are not listed for the same `frame_index` (a model listing two boxless
    laptops in the same frame means two laptops);
  - *exception (review round):* two listings for the same frame whose boxes overlap stay separate when the detector
    found two distinct same-class boxes there, each best-matching one listing (stacked laptops).
  Clustering is leader-based, not transitive. Objects are visited by omni confidence (high first, then window
  order), and each joins the first cluster whose leader is compatible with it, so a chain of overlapping boxes
  cannot glue two side-by-side objects together. Two same-class objects with non-overlapping boxes stay separate,
  even in different frames, because a model following AC-PER-20 lists two laptops with different `frame_index`
  values.
  Each cluster becomes ONE observed instance. It is linked by trying every member's frame and box and keeping the
  best: detector-linked first, then a link whose box agrees with omni's (IoU ≥ `link_iou`), then the higher
  detector score, then the higher omni confidence, then the later frame (the most recent state). The kept member
  supplies the description, frame and box. `held_by` is taken from any member that has it.
- AC-PER-20 **Prompt** (amends AC-PER-3). The system prompt says: "List each distinct physical object ONCE per
  window, even if it is visible in several frames; frame_index = the frame where it is clearest, and bbox is on
  that frame." `location_hint` is defined as the module or zone *where the camera is*, from signage or speech. It
  is never a destination mentioned in speech.
- AC-PER-21 **Full-resolution keyframes for detection and crops** (amends AC-PER-2). The window builder keeps two
  images per keyframe:
  - `image`: downscaled to `long_side` (448), for omni only;
  - `detect_image`: the original, capped at `detect_long_side` (default 1280, `PERCEPTION_DETECT_LONG_SIDE`; 0 means
    use the omni image, values above 4096 are rejected; never upscaled, and never smaller than the omni image).
  `Keyframe.hires` is `detect_image` when set, else `image`. When no resize is needed, no copy is made. The
  detector, exemplar and review crops, and stored evidence frames all use `hires`. Boxes are normalised to 0..1,
  so a box from omni (448 image) or from the detector (hires image) is cut from `hires` at
  `x * W_hires, y * H_hires`. Both images are aspect-preserving downscales of the same frame (no letterbox or
  padding; OWLv2's square padding is undone inside the detector), so normalised coordinates agree to within one
  pixel of rounding. Memory bound: at most `window_frames` hires frames are retained by the builder, each at most
  `detect_long_side² × 3` bytes (1280: ≤ 4.9 MB, 6 frames ≤ 30 MB).
- AC-PER-22 **Exemplar quality gate** (amends AC-PER-8). An observed instance gets an embedding, an exemplar and a
  stored crop only when both hold:
  - it is linked to a detector box (omni boxes are coarse and do not localise per frame);
  - the crop's short side is ≥ `min_crop_px` (default 32, `PERCEPTION_MIN_CROP_PX`) pixels of the hires frame.
  Otherwise the sighting is still recorded, with `exemplar: null` and no crop, and reviews use the frame image.
  Without an appearance vector, the instance is matched by **class and place**: the most recently seen live
  entity of the same catalog class is taken (for `UNKNOWN`: an unclassified entity with the same normalised label)
  when it meets all of these:
  - it was last seen in the same module as the observation (or the same location when that location is not inside
    a module, for example `USOS`);
  - it was last seen within `no_exemplar_match_s` (default 600 s, `PERCEPTION_NO_EXEMPLAR_MATCH_S`);
  - no other instance of this window has taken it.
  Otherwise a new provisional entity (unknown + `unknown_object` review for `UNKNOWN`) is created.
  *Adoption (review round):* an entity created from exemplar-less sightings has no exemplar, so it is never an
  appearance candidate. A detector-linked instance that matched no candidate and raised no review therefore also
  tries the class-and-place match. It excludes every entity that came back as a candidate, because those were
  judged by appearance. Its exemplar becomes that entity's first, so intermittent detector boxes do not produce a
  duplicate when the detector finally localises the object. A coarse
  sighting carries no identity information, so attaching it to the nearest same-class entity is the most likely
  correct choice, while a new entity is a certain duplicate. With two same-class entities in one module, a
  gated sighting may refresh the wrong one; it never creates a duplicate.
- AC-PER-23 **Location hints** (amends AC-PER-11).
  - *Resolution.* Exact alias matching is tried first, as before. Then a relaxed match ignores filler words ("the",
    "module", "laboratory", "iss", "international space station", "of", "in", "inside", "aboard", "on"), on both
    the hint and the aliases, case-insensitively. "laboratory" is also tried as "lab". A relaxed key shared by two
    locations is ambiguous and resolves to nothing (a wrong location is worse than none). Example: "Columbus
    Laboratory Module" resolves to COL1.
  - *Precedence.* A camera whose configured location is not inside a module (a segment, for example `cam-handheld`
    → `USOS`) is **roaming**, and a resolvable hint sets the window location. A **fixed** camera (module or zone)
    also takes a resolvable hint, as AC-PER-11 and AC-PER-14 require (the camera may look through a hatch, and tour
    footage may be replayed on a fixed camera id). But when the hinted location is in a different module, the
    window is flagged `hint_conflict`: every sighting's confidence is capped at `hint_conflict_confidence`
    (default 0.5, `PERCEPTION_HINT_CONFLICT_CONFIDENCE`) and a warning is logged. A hint that only names an
    ancestor of the camera location (for example "US segment" for a LAB1 camera) never coarsens it: the camera
    location is kept.
  - *Privacy (never less private).* The window is `no_capture` when any of these is true:
    - the camera is unknown or private (as before);
    - the resolved hint (exact or relaxed) is private;
    - the hint text names a private place.
    A hint names a private place when it contains a private-zone term:
    - the core name of any `no_capture` location (its display name without the parenthetical qualifier and without
      its module's name words, e.g. "crew quarters", "whc");
    - a curated term: "crew quarters", "sleep station", "toilet", "waste and hygiene", "hygiene compartment",
      "bathroom", "lavatory", "restroom".
    A private hint on a fixed camera, even one naming another module, still makes the window private.
    *Review round:*
    - A relaxed key that is ambiguous (it resolves to nothing) still makes the hint private if any location
      claiming it is private.
    - The window is also `no_capture` when omni's `scene_summary` names a private place (same term list, plus the
      ids of `no_capture` locations). This covers a hint that names only the parent module, e.g. "Node 2" from
      inside crew quarters. Data from a lab window whose summary mentions "toilet" is lost; that over-privacy is
      accepted.
