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

## Amendments: omni output budget and truncation salvage (cluster integration 2026-10-17)

Measured on the cluster (vLLM-Omni, Qwen2.5-Omni-7B fp16 TP2 on T4, real NASA ISS tour video, 53 window
requests): about 2,200 prompt tokens and about **1,000 generated tokens** per request at about 28 tok/s, so about
36 s per window against a 4 s stride. **18 of 53 (34 %) finished with `length`** at `max_tokens` = 1500: the JSON
was cut off, failed validation, was retried (another ~55 s) and then dropped. AC-PER-24 to AC-PER-28 amend AC-PER-3
(and AC-PER-4 for the shared prompt and reply handling).

- AC-PER-24 **Output budget in the prompt** (amends AC-PER-3, AC-PER-20). The system prompt (shared by the chat and
  realtime clients) states an explicit budget:
  - at most `omni_max_objects` objects (default 12), the most salient or inventory-relevant first;
  - each `description` 12 words or fewer; `attributes` only when they add identifying information (label text,
    serial, colour that tells two items apart), otherwise omitted;
  - at most 8 people and at most 8 events; `scene_summary` one short sentence;
  - `transcript`: when the deployment has an ASR endpoint (`ASR_URL`; ASR is the transcript of record, see
    replay), the prompt says `transcript` must be `""`; without ASR it asks for the intelligible speech verbatim
    and `""` when nothing intelligible is said (never a placeholder such as "No speech detected");
  - the reply is ONE compact JSON object on a single line (no indentation, no line breaks, no code fences) with no
    prose before or after it;
  - keys in schema order `scene_summary, location_hint, objects, people, events, transcript, audio_events`, and
    `location_hint` always present (`null` when unknown), so a cut-off reply still carries the location first
    (AC-PER-26 relies on this).
- AC-PER-25 **Config** (env, validated like the other numbers; ConfigError otherwise):
  - `OMNI_MAX_TOKENS` (default 700, integer 1..32768) is the request's `max_tokens`;
  - `OMNI_MAX_OBJECTS` (default 12, integer 1..40, the schema's `maxItems`) is the prompt cap and the client cap
    (AC-PER-27);
  - `OMNI_REPETITION_PENALTY` (default 1.05, > 0 and <= 2; temperature 0 loops on repeated objects) is sent as
    `repetition_penalty` only when it is not 1.0;
  - `OMNI_TRUNCATED_CONFIDENCE` (default 0.5, <= 1) caps the sighting confidence of a salvaged window.
  `create_omni_client` passes these to the chat client (and the prompt settings to the realtime client).
- AC-PER-26 **Truncation salvage.** When a reply's top-level JSON object is not complete (finish_reason `length`,
  or the JSON stops parsing part-way), the client recovers an observation from the complete prefix:
  - parsing uses the JSON decoder itself, value by value, so braces, brackets, escaped quotes and `\u` escapes
    inside strings never count as structure;
  - `objects`, `people`, `events` and `audio_events` keep only array elements that are fully closed; a partial
    element is dropped whole (a cut-off `moved` event is never reported);
  - top-level strings (`scene_summary`, `location_hint`, `transcript`) are kept only when closed; a cut-off
    `transcript` becomes `""`;
  - missing required fields are filled with schema-valid empties;
  - *privacy (fail closed):* salvage requires a complete `scene_summary` **and** a complete `location_hint` (string
    or null). A reply cut before or inside `location_hint` might have named crew quarters or the WHC, so it is not
    salvaged;
  - the result must validate against `observation.schema.json` and must carry something (a non-empty
    `scene_summary`, or at least one object, person or event); otherwise the existing invalid path applies
    (repair retry, then `invalid`);
  - prose before the reply that looks like a key (`{"a" is ...`) is skipped: the next `{"` is tried, and a
    complete non-observation object before the reply is stepped over;
  - a salvaged reply is **not retried**. `OmniResult.truncated` is True (a sidecar flag; the schema-validated
    observation is unchanged, its schema has `additionalProperties: false`).
  A complete JSON object that is schema-invalid is not salvaged (repair retry, as before). The reconciler logs a
  truncated window, caps its sighting confidence at `omni_truncated_confidence`, and writes no `stock_updates`
  for it (AC-PER-9 counts from an incomplete list would overwrite the stock with an undercount).
- AC-PER-27 **Client-side object cap.** After parsing (full or salvaged), at most `omni_max_objects` objects are
  kept, in reply order. Selection priority:
  1. the best listing of each distinct instance that an event names (`events[].object_label`) or a person holds
     (`held_by`): the reconciler drops an event whose object is not observed, so capping it away would lose a real
     movement or hand-over;
  2. the highest-confidence listing of each other distinct instance. Two listings are one instance when they have
     the same normalised label and boxes at IoU >= 0.5, or one of them has no box (as AC-PER-19 consolidates
     them);
  3. the remaining listings (per-frame repeats) by confidence.
  Per-frame repeats are dropped first. The only listing of anything (an unknown object for NASA review, or a second
  laptop) is dropped only when the window has more distinct instances than `omni_max_objects`. *Choice:* unknown
  labels get no priority over catalog labels. The client cannot run the AC-PER-7 mapping, and non-catalog labels
  are often clutter ("handrail", "cable"), which the prompt already deprioritises. "Anything you cannot identify"
  is named among the salient objects in the prompt instead.
  The number dropped is `OmniResult.dropped_objects`. A window with dropped objects writes no `stock_updates`, for
  the same reason as a truncated one. The cap is applied before schema validation, so a reply with more than 40
  objects is capped rather than rejected.
- AC-PER-28 **Usage logging.** Every chat-completions omni request logs one `omni usage` record with `window`, `attempt`,
  `prompt_tokens`, `completion_tokens`, `finish_reason`, `latency_ms`, `truncated` and `dropped_objects`
  (`usage` from the OpenAI-compatible response; null when the server sends none). `OmniResult` carries
  `finish_reason`, `prompt_tokens` and `completion_tokens` (summed over attempts).

### Review round (AC-PER-24..28): residual risks accepted

- *Fungible counts.* Stock is overwritten with `est_count`. Truncated or capped windows write no stock update, but a
  model that obeys the prompt's object budget in a cluttered scene (for example 20 food packages) can under-list
  without any visible cap. Detector counts still feed the max (AC-PER-9). Fungible classes are therefore updated
  reliably only by windows with at most `omni_max_objects` listings.
- *ASR fallback.* With `ASR_URL` set, omni is told to leave `transcript` empty. If ASR fails for a window, replay's
  fallback to the omni transcript now finds "" and that window's speech is lost. ASR is the transcript of record,
  and the saving (the transcript echo was a large share of the output) is worth it.
- *Raw control characters.* A string with a raw newline is invalid JSON for the decoder. A complete reply with one
  goes to repair, as before. In a cut-off reply, salvage stops at that element.

### AC-PER-10 amendment: within-window event repeats (cluster integration 2026-10-17)
- Inside one window, events with the same target (entity_id or entity_ref), event_type, to_location and actor_desc
  collapse to the single most confident event, keeping the order of first appearance. Events with a different actor,
  type or destination stay separate. This complements the cross-window `event_dedup_s` check. Tests:
  tests/test_ac_per_10_within_window_event_dedup.py.

## Amendments: revisit dedup (cluster integration 2026-10-17)

Measured on the cluster (docs/QUESTIONS.md #29): the full NASA ISS tour video ran in a loop, 101 windows of 6 frames
every 12 s across 8 modules. New entities per 10-window bucket were 19, 22, 16 (windows 0–29), then 7, 6, 9, 5, 10
(30–79), then **22** in windows 80–89, right after the video looped back to Columbus about 15 min later, then 2, 3.
Cause (verified in the code): a sighting without an exemplar (AC-PER-22) attached only to a same-class entity last
seen in the module within `no_exemplar_match_s` (600 s), so every omni-only object of a module revisited after more
than 10 min became a new entity. Expected counts only flagged the duplicates afterwards (35 `possible_duplicate`
reviews). The detector-linked path was verified and kept: `/ingest/candidates` searches all live exemplars of the
class with no time or place filter; the impossible-transit gate (AC-PER-8 step 2) rejects only the *candidate* last
seen in another module within `min_transit_s`. It never blocks the other candidates, for example the same object last
seen in this module 15 min ago. AC-PER-29 to AC-PER-32 amend AC-PER-8, AC-PER-19 and AC-PER-22.

- AC-PER-29 **Revisit dedup for sightings without an exemplar** (amends AC-PER-22).
  - *Candidates.* Live entities of the observation's catalog class (for `UNKNOWN`: unclassified entities with the
    same label key, AC-PER-31) whose last location is the observation's module or one of its zones, that no other
    instance of this window has taken (and, for adoption, that were not already compared by appearance). One
    `GET /items?location=<module>&sort=-last_seen_at` query per (class, module) per window (`catalog_id=<class>`,
    limit 50; for `UNKNOWN` no catalog filter and no free-text `q`, limit 200), cached for the window.
  - *Recent.* If any candidate was last seen within `no_exemplar_match_s`, AC-PER-22 applies, with one change from
    the review round: among the recent candidates, one last seen at the observation's own location (zone) is
    taken first, else the most recently seen. In the second window of a revisit, both same-class objects were just
    attached and are "recent"; the zone-blind rule swapped them.
  - *Revisit, one candidate.* Otherwise, after any gap, a single candidate is attached. There is no time limit.
  - *Revisit, several candidates.* The decision model is asked `same_or_new` (systemone choice). Options are up to 4
    candidates (those last seen in the observation's own zone first, then most recent first) plus `NEW`. The state
    carries the observation (label, description, location, time, held) and, for every candidate, its label,
    description, last location, last-seen time and `seen_ago_s`. The call is logged in `decisions`. The subject
    holds ids, locations and times, never descriptions.
    - A candidate chosen with probability ≥ `decision_accept` is attached.
    - `NEW` chosen with probability ≥ `decision_accept` creates a new entity. The exception is a non-fungible class
      whose live count has already reached `expected_count`: there a new entity would be a certain duplicate, so the
      fallback applies.
    - In every other case (model unavailable or erroring, `needs_review`, low probability, a choice that was not
      offered), the **fallback** applies: attach the most recently seen candidate last seen at the observation's own
      location (zone), else the most recently seen candidate, and add a `low_confidence` review naming the
      candidates. No new entity is created.
  - *Coarse places.* When the observation's location is not inside a module (a segment such as `USOS`, or none),
    AC-PER-22 applies unchanged: the same exact location, within `no_exemplar_match_s`. A segment is too coarse to
    identify an object after an arbitrary gap.
  - *Privacy.* In a `no_capture` window the attachment rules are the same, but no review is created (AC-PER-11) and
    nothing is uploaded.
- AC-PER-30 **Revisit context for detector-linked sightings** (amends AC-PER-8 step 4). The similarity bands are
  unchanged: p ≥ `assign_p` assigns, p < `ask_p` creates a new entity. A revisit in the ask band goes to the decision
  model like any other ask, never straight to `NEW`, with the same context as AC-PER-29: each candidate's label, last
  location, last-seen time, `seen_ago_s` and, when inventory has one, its description. `/ingest/candidates` returns
  no description, so descriptions come from one `GET /items?catalog_id=<class>` query per class per window, made
  only when an ask happens.
- AC-PER-31 **UNKNOWN label key** (amends AC-PER-19, AC-PER-22, AC-PER-10; docs/QUESTIONS.md #27).
  `label_key(text)` is `normalise_label(text)` with all spaces removed, so "spacesuit", "space suit" and
  "Space-Suit" are one key. The key is used only where one UNKNOWN label is compared with another: within-window
  consolidation, class-and-place matching, and matching an omni event's object label to an observed UNKNOWN object.
  `normalise_label` itself (display and search) is unchanged.
- AC-PER-32 **Eval gate** (eval/, AC-EVAL-5/6). `nasa_eval run` replays the dataset a second time, shifted by the
  scene duration plus `REVISIT_GAP_S` = 900 s, after scoring the first pass. In the local target, the gold detector
  misses every other catalog object on the revisit (OWLv2 is intermittent on real footage), so both the exemplar path
  and the class-and-place path are exercised. `revisit_new_entity_rate` = entities created during the revisit / gold
  objects of the scene, gated at ≤ 0.05 in eval/thresholds.yaml. The committed reference snapshot records a correct
  revisit (no new entity).

### Review round (AC-PER-29..32): residual risks accepted

- *Wrong merge after a long gap.* A coarse sighting carries no identity. When the only same-class entity of a module
  has left and a different one of the same class is shown, the newcomer is attached to the old entity. A detector-linked
  sighting is protected, because appearance decides and entities that came back as candidates are excluded from
  adoption. AC-PER-22 already accepted this risk within 10 min; AC-PER-29 accepts it for any gap. The alternative
  measured on the cluster, a certain duplicate on every revisit, was worse.
- *Adoption after a long gap.* A detector-linked instance that matched no exemplar may adopt a stale exemplar-less
  entity (AC-PER-22 adoption, now without a time limit). If that adoption is wrong, the entity's first exemplar is
  the wrong object's, and the real object later gets a fresh entity (it is excluded as an appearance candidate).
  The history is mixed, but no duplicate is created on the revisit.
- *Zone-blind single candidate.* One stale candidate is attached even when it was last seen in another zone of the
  module (it may have been moved during the gap). With zones configured, a never-seen second object of the class
  can be merged into it.
- *UNKNOWN listing cap.* UNKNOWN candidates come from the 200 most recently seen entities of the module (no class
  filter, and free-text `q` would not match "spacesuit" to "space suit"). A module with more live entities can miss
  an old unknown.
- *Detector-linked ask band with the decision model down* keeps AC-PER-8: a new provisional entity plus a
  `possible_duplicate` review. Appearance was ambiguous there, so the "prefer attaching" rule is not applied.
- *Fallback attaches are reviewed.* With several candidates and no confident decision, the attached entity may be
  the wrong one of the candidates. A `low_confidence` review is opened (not in `no_capture` windows), so NASA can
  correct it.

### AC-PER-33 Box sanity (Gemma 4 A/B, 2026-10-18)
- Well-formed object boxes (4 numbers) are normalised before validation: inverted coordinates are swapped, values
  clamped to [0, 1], and boxes with a side under 2% of the frame become null (the object is kept). Malformed boxes
  (wrong length, non-numbers) are left for schema validation to reject (AC-PER-3).
  Seen live: Gemma 4 returned "cabling" [0.58,0.1,0.2,0.91] and "hatch" [0.35,0.55,0.64,0.54].
  Tests: tests/test_ac_per_33_box_sanity.py.

### AC-PER-34 bbox provenance (UI review round 2, finding UI2-07)
- Every assignment's `sighting` carries `bbox_source`, which says where `sighting.bbox` came from:
  - `"detector"` when the omni object is linked to a detector box (by IoU or by label, AC-PER-8). The bbox is then the
    detector's box.
  - `"omni"` when there is no linked detector box and the bbox is the omni model's own (coarse) box.
  - `null` when the sighting has no bbox.
- inventory-api stores it on the sighting (`sighting.bbox_source`, migration 004, AC-API-37) and returns it with review
  images (`image_bbox_source`), item sightings and item snapshots. The dashboard draws detector boxes solid and omni
  boxes dashed ("approximate").
- The field is optional in the ingest contract, so an older perception build keeps working (its sightings get null).
  Tests: `tests/test_ac_per_08_reconciler.py::test_ac_per_08_bbox_source_*`.

## Amendments: person tracks from `people[]` and sleep-cue privacy (Gemma 4 run, 2026-10-18)

Measured live (docs/QUESTIONS.md #33): with Gemma 4 12B as the omni model, people are reported only in the
observation's `people` array (`{ref, appearance, activity, is_speaking}`), never as objects labelled "person". The
reconciler built person entities only from person *objects* (Qwen2.5-Omni listed both), so the Gemma run created 0
person tracks against 8–10 with Qwen. The same run described "dark hair, sleeping in a blue sleeping bag" in Harmony
(crew quarters NOD2S5/D5/P5/O5 are `no_capture`) and the window was stored. AC-PER-35 to AC-PER-38 amend AC-PER-8,
AC-PER-10, AC-PER-11, AC-PER-22, AC-PER-23 and AC-PER-29. People are tracked by appearance text only, never by
biometrics (docs/QUESTIONS.md #7).

- AC-PER-35 **Person tracks from `people[]`** (amends AC-PER-8, AC-PER-22, AC-PER-29).
  - *Person class.* The catalog class with `category = person` (seed: `crew-member`). Without one, `people[]` is
    ignored (logged). An omni object is a *person object* when its label maps to that class.
  - *Sightings.* Every `people[]` entry (first entry per `ref`, at most 10 per the schema) is one person sighting of
    the window, at the window location, stamped at the last frame, with `description` = the sanitised appearance
    (control characters removed, whitespace collapsed, at most 200 characters). Entries with distinct refs are
    distinct people: two entries of one window never share an entity, whatever their appearance.
  - *Linking with person objects (no double create).* Person objects and `people[]` entries of one window are paired
    by appearance similarity (below), greedily, best first, pairs with similarity ≥ `person_match_floor`. If exactly
    one person object and one entry are left, they are paired too. A pair is one sighting: the object's box (and
    detector link) plus the entry's appearance as the description. If several of each are left they are paired in
    order, **without** a box (a box could belong to the other person). Unpaired person objects and entries are
    sightings of their own.
  - *Matching (one path for every person sighting, reusing AC-PER-29).* Person sightings never get an exemplar
    (AC-PER-36) and are matched by class, place and appearance text. Candidates: live person-class entities last
    seen in the observation's module or one of its zones (one cached `GET /items?catalog_id=<class>&location=<module>`
    per window, as AC-PER-29), not taken by another sighting of this window. A candidate is eligible when the
    appearance similarity between its `description` and the observation is ≥ `person_match_floor`. Recent
    candidates (last seen within `no_exemplar_match_s`) and revisits (any gap, modules only) use the same floor;
    outside modules (a segment) only recent candidates at the exact location are eligible (AC-PER-22). Pairs are
    assigned globally, best first: higher similarity, then recent before stale, then the observation's own zone,
    then most recently seen. An observation whose appearance carries no descriptor (empty, or only generic words
    such as "astronaut") attaches to the most recently seen recent candidate, else starts a new track.
  - *Appearance similarity* (`perception.people.appearance_similarity`, deterministic, in [0, 1]). Text is
    normalised (lower case, punctuation removed, "t-shirt"/"tee" → shirt, "trousers" → pants, "gray" → grey,
    "flight suit"/"jumpsuit"/"coveralls" → suit). Descriptors are colour + garment pairs (colours immediately
    before a garment, a non-colour word in between allowed: "blue flight suit" → blue suit), uncoloured garments
    and remaining distinctive words (e.g. "flag", "patch", "headset"); generic words ("crew", "member", "person",
    "astronaut", articles, prepositions) are dropped. If both texts name the same garment with disjoint specific
    colours ("blue shirt" vs "red shirt"; "dark"/"light" alone are colour families, see the review round), the
    similarity is 0 (different people). Otherwise it is the overlap coefficient |A ∩ B| / min(|A|, |B|) of the
    descriptor sets, with hair weighing half.
  - *New track.* `new_entity` = {`catalog_id`: person class, `kind`: `person`, `status`: `provisional`, `label`:
    the class name, `description`: the appearance}.
  - *Events.* `events[].actor_ref` (P1, P2, …) still resolves to `people[ref].appearance` for `actor_desc`
    (AC-PER-10), sanitised like the description. The ingest contract has no person-entity link for events
    (`event.actor_entity` is not accepted by `/ingest/window`), so the actor stays a descriptor.
  - *Config.* `person_match_floor` (`PERCEPTION_PERSON_MATCH_FLOOR`, default 0.5, ≤ 1).
  - *Accepted.* Matching is approximate. Two crew members with similar outfits ("dark hair, blue shirt" twice) are
    merged into one track across windows, and a crew member who changes clothes starts a new track; a person's
    entity description is the first appearance seen. Both are accepted: the alternative is identity from faces.
- AC-PER-36 **Person privacy** (amends AC-PER-8, AC-PER-11, AC-PER-13, AC-PER-22). Person sightings (from objects or
  `people[]`) never store a crop and never get an exemplar embedding: a crop of a person shows the face, and an
  appearance embedding of it is a re-identification vector (biometric-like). Before this amendment a detector-linked
  person object with a crop ≥ `min_crop_px` was embedded and its crop uploaded in capture windows. Frames are still
  stored in capture windows, as for every window, and the sighting keeps the box. In a `no_capture` window no person
  sighting is recorded at all (where a crew member is inside a private zone is not tracked), and events get
  `actor_desc` = null (no description of people in private places).
- AC-PER-37 **Sleep cues make a window private** (amends AC-PER-11, AC-PER-23). A window is `no_capture` when any of
  `people[].appearance`, `people[].activity`, `objects[].label`, `objects[].description` or `scene_summary`
  contains a sleep cue, matched on whole words after normalisation: "sleeping", "asleep", "sleeping bag(s)",
  "sleep station(s)", "crew quarters", "bunk(s)". A sleeping person or a sleeping bag in use is almost always in, or
  seen into, a crew quarter (NOD2S5/D5/P5/O5). The window then follows the existing `no_capture` path exactly: no
  frames, crops or raw reply are uploaded, no review is created, the transcript is not stored, unknown objects and
  person sightings are skipped, and known objects are sighted without image URIs. The reason is logged at INFO
  ("sleep cue; window treated as no_capture", with the cue and the field, e.g. `people[1].appearance`, never the
  text) and recorded in the payload as `window.privacy = {"no_capture": true, "reason": "sleep_cue", "cue": ...,
  "field": ...}` (inventory-api ignores unknown window fields). The other private reasons (camera, hint, scene
  summary naming a private place) are recorded the same way with their own `reason`.
  *Accepted.* False positives (a stowed, unused sleeping bag in Harmony; "sleeping bag" as a catalog label) lose
  that window's images. Over-privacy is the design choice. The `scene_summary` is still stored, as for every
  `no_capture` window (AC-PER-11 "summarised only").
- AC-PER-38 **Eval gates** (eval/). `GoldOmni` reports people only in `people[]`, as Gemma does. A new profile
  `gemma_people` (now the default) lists every gold actor in every window, with Gemma-like varying appearance text
  (the exact gold text in the window of that actor's event), on top of `repeat_per_frame` objects. In every profile,
  one *quiet* window (the first, in time order, with no gold event, no unknown object and no scripted transcript line
  within 6 s, so no other metric depends on it) also lists a sleeping crew member with the live Gemma text ("dark
  hair, sleeping in a blue sleeping bag") and has the neutral scene summary "Crew member resting in the module"
  (no sleep cue, so only the `people[]` path can catch it). The scorer finds that window by its stored summary
  (summaries are stored for private windows too, AC-PER-11). Metrics (eval/thresholds.yaml):
  - `person_track_recall` = gold actors with a person track whose description contains the actor's descriptor
    words / gold actors, gated ≥ 0.99;
  - `person_tracks_per_person` = person entities / gold actors, gated ≤ 1.5 (catches over-splitting);
  - `sleep_window_media` = stored raw replies + sighting frames of the sleep-cue window, gated ≤ 0 (None, i.e. a
    failure, when no such window was stored).
  - `sleep_window_person_sightings` = person sightings recorded in the sleep-cue window, gated ≤ 0 (review round).
  *Integration finding:* the first draft put the sleeping crew member in the window at the scene start. That window
  also holds the transcript line "Starting the tool inventory survey", which tests/e2e searches for, and a private
  window stores no transcript. The quiet-window rule keeps the sleep probe independent of the other checks.

### Review round (AC-PER-35..38): fixes and residual risks accepted

Fixes (each test-first, tests/test_ac_per_35_38_people_privacy.py `test_review_*`, eval `test_review_*`):
- *Sleep-cue bypasses.* `sanitise_text` deletes invisible format characters (soft hyphen, zero-width space, bidi
  overrides) instead of turning them into spaces, which split "slee\u00adping" into two words. Added cues:
  "sleepingbag(s)", "sleep pod(s)", "sleep rack", "sleep restraint", "sleeps", "slumber(ing)", "napping", "dozing",
  "dozes".
- *Privacy hold.* Windows overlap (6 frames every 4 s), so the next window can show the sleeper without a cue. Every
  window of the camera that starts within `privacy_hold_s` (`PERCEPTION_PRIVACY_HOLD_S`, default 30 s, 0 = off) of
  the end of a private window is private too (`window.privacy.reason = "privacy_hold"`). Held windows do not extend
  the hold.
- *Colour families.* A modifier without a specific colour stands for a family: "dark" = black, blue, brown, grey,
  green, red, purple; "light"/"pale" = white, grey, blue, yellow, khaki, pink, green, blonde, orange. So "dark hair" vs
  "blonde hair" clashes, while "dark t-shirt" vs "black t-shirt" does not. Specific colours win over the modifier
  ("light grey" = grey). Several colours of one garment are all allowed ("white and blue shirt"): "and"/"or" no longer
  reset the colours, only "with"/"plus" and punctuation do. A colour attaches to a garment at most one word later
  ("blue NASA polo"); "haired" is hair; a polo is a shirt.
- *Ties.* Candidates with the same overlap similarity are ranked by the two-sided (Dice) similarity before recency.
  Otherwise "blue shirt, headset" took the entity "headset" (another person's, seen more recently) from its exact
  match, and the other person got a new track.
- *Pairing.* A leftover person object and people entry are never paired when their descriptions clash in colour.
- *Sanitising.* Person objects' descriptions are sanitised like people[] appearances.

Residual risks accepted:
- *Lenient overlap.* The overlap coefficient divides by the shorter description, and an entity keeps its first
  description. A track first described as "headset" or "shirt" absorbs any later person with a headset or a shirt of
  any colour, as long as no garment clashes in colour. This is the documented over-merge, preferred to splitting one
  person whose description gets more detailed ("blue flight suit" → "blue flight suit with American flag patch").
- *Greedy, not optimal, assignment.* Assignment is greedy over (similarity, closeness, recency), not a maximum
  matching, so a three-way tie can still swap tracks.
- *Summary text.* A private window keeps its `scene_summary` (AC-PER-11), which may describe the sleeping person.
- *Names.* The model may name a crew member in `appearance`. Perception does not strip names; the omni prompt asks for
  appearance only.
- *Eval purity.* `person_track_recall` checks that each actor has a track; it does not detect a track that also
  absorbed the other actor's sightings. `person_tracks_per_person` ≤ 1.5 only bounds over-splitting.

## Amendments: ISS module resolution (real-video eval, 2026-10-18)

Measured (docs/spec/50-eval.md AC-EVAL-10, live Gemma 4 run on the NASA tour): `window_location_acc` 0.391 (9 of 23
scored windows). No window was given the wrong module; all 14 misses stayed at segment level (`USOS`). Causes, from
the stored raw replies (tests/fixtures/real_tour_run_2026-10-18.json):
- official names do not resolve: `Japanese Experiment Module "Kibo"`, `Nauka Multipurpose Laboratory Module`;
- the hint is often null while `scene_summary` names the module;
- the handheld camera stays in a module for 30–60 s, but only the window that names it gets it.

AC-PER-39 to AC-PER-42 amend AC-PER-11 and AC-PER-23. AC-PER-16 (generic words never resolve) still holds.

- AC-PER-39 **Module names** (amends AC-PER-23). Exact aliases, added to the seed's ids and display names (only for
  location ids that exist in the seed; no codes are invented: the seed has no Quest, Poisk, Rassvet or PMA):
  | id | aliases (normalised: case, quotes, apostrophes and punctuation ignored) |
  |---|---|
  | JPM1 | Kibo, JEM, JPM, Japanese Experiment Module, Kibo/JEM Pressurized Module |
  | JLP1 | JLP, Kibo/JEM Logistics Module, Experiment Logistics Module |
  | ZARYA | Zarya, FGB, Functional Cargo Block |
  | NAUKA | Nauka, MLM, Multipurpose Laboratory Module |
  | ZVEZDA | Zvezda, Service Module, SM |
  | CUP1 | Cupola |
  | COL1 | Columbus, Columbus Laboratory, Columbus Lab, European Laboratory, European Lab |
  | LAB1 | Destiny, US Lab, U.S. Lab, US Laboratory, U.S. Laboratory, Destiny Lab(oratory) (plus the existing "lab") |
  | NOD1 / NOD2 / NOD3 | Unity / Harmony / Tranquility (Tranquillity), Node 1 / Node 2 / Node 3 |
  | PMM1 | Leonardo, PMM, Permanent Multipurpose Module |
  - *Relaxed keys* (AC-PER-23) that are only a generic word are never registered: "multipurpose", "service",
    "experiment", "japanese experiment", "logistics", "pressurized", "european", "us", "u s". "Service Module"
    alone is ZVEZDA (exact alias); "service" alone, "laboratory", "the laboratory module" resolve to nothing.
  - *Mention scan* (`LocationResolver.scan_modules(text)`). When the exact and relaxed matches fail, the text is
    scanned for module **mentions**: whole-word phrases from a curated mention table (the names above without the
    generic ones: "lab", "the lab", "SM", "node" alone and every relaxed key are excluded), longest phrase first,
    non-overlapping. Common English words only count when capitalised in the original text ("Unity", "Harmony",
    "Destiny", "Tranquility"; "US" in "US Lab(oratory)"; "BEAM", "Quest", "Bishop"). Each mention is marked:
    - *in*: preceded (skipping "the"/"a"/"an", and an adjacent mention of the same module) by "in", "inside",
      "within" or "aboard";
    - *directional*: preceded the same way by "to", "into", "toward(s)", "from", "onto", "leaving", "leaves",
      "exits", "exiting" (signage such as "To FGB", "a view from Unity into Harmony");
    - otherwise *plain*.
    Modules the seed lacks are mentions too, of an **unseeded** module (Quest / Joint Airlock, PMA / Pressurized
    Mating Adapter, Poisk / Mini-Research Module, Rassvet, Pirs, Prichal, BEAM, Bishop); they never resolve.
    Decision over the distinct modules mentioned: one module → that module, unless all its mentions are
    directional (then nothing); two or more → the single module with an *in* mention, else nothing. A decision
    for an unseeded module yields no location but reports **elsewhere** (AC-PER-41).
  - *Hints.* `resolve_hint(hint)` = exact, then relaxed, then the mention scan. `resolve()` (speech claims,
    event destinations) is unchanged apart from the new exact aliases. A hint that names a private place is not
    scanned (it keeps the AC-PER-23 result and stays private).
- AC-PER-40 **Scene-summary fallback** (amends AC-PER-11). For a **roaming** camera (configured location not inside
  a module: `cam-handheld` → `USOS`), when the hint gives no module (null, unresolvable, or a segment), the module
  comes from the mention scan of `scene_summary`, with the same rules (two modules: the one the camera is *in*, else
  none). A summary module outside a segment named by the hint is ignored. Fixed cameras do not use the summary (it
  describes what is seen, which for a fixed camera is often another module through a hatch).
  Privacy is unchanged and fails closed: a summary naming a private place, a sleep cue, a private hint or a
  no_capture summary module makes the window `no_capture` (AC-PER-23/37); the summary never makes a window less
  private. `WindowScope.source` is `hint`, `summary` or `camera`.
- AC-PER-41 **Module carry for a roaming camera** (amends AC-PER-11). A handheld camera moves slowly between
  modules, so a window with no module of its own inherits the module of the last window that had one:
  - *Anchor*: the last window of this camera (reconciler) that resolved a module itself (hint or summary) and is
    not `no_capture`. Carried windows do not move the anchor, so a carry lasts at most `module_carry_s` after the
    last real evidence (no unbounded chain through a long unnamed stretch).
  - *Carry*: a capturable window of a roaming camera whose location is not inside a module inherits the anchor's
    module when 0 ≤ `t_start` − anchor `t_end` ≤ `module_carry_s` (`PERCEPTION_MODULE_CARRY_S`, default 45, 0 =
    off, at most 600; a window overlapping the anchor counts as gap 0) and the module lies inside the window's location when
    the hint named a segment.
  - *Never across privacy*: a `no_capture` window (any reason, including the privacy hold) is never given a carried
    module and clears the anchor, so nothing carries out of it either.
  - *Elsewhere*: a window whose hint or summary decides on an unseeded module (e.g. "Poisk Research Module")
    clears the anchor and is not carried into: the camera has left the anchored module.
  - *Resolved wins*: a window that resolves a module itself always uses it and becomes the anchor.
  - *Marking*: the window payload carries `window.location = {"location_id", "source": "carried", "carried_from"
    (anchor t_end), "gap_s"}` (inventory-api ignores unknown window fields; `source` is `hint`/`summary` for
    resolved windows), an INFO log, and every sighting's confidence is capped at `module_carry_confidence`
    (`PERCEPTION_MODULE_CARRY_CONFIDENCE`, default 0.5, ≤ 1).
  - Fixed cameras never carry (they have their own location rules).
- AC-PER-42 **Real-data regression** (tests/test_ac_per_39_42_module_resolution.py). The stored raw replies of the
  live run are fixtures; each must resolve to its caption module. The whole run (51 windows: hint, summary, privacy,
  caption truth) replays through `window_policy` + `ModuleCarry`; the predicted modules and the resulting
  `window_location_acc` are asserted, and no window may be given a module other than its caption module when the
  caption names a seeded module.

### Review round (AC-PER-39..42): fixes and residual risks accepted

Fixes (each test-first, tests/test_ac_per_39_42_module_resolution.py `test_review_*`):
- *Punctuation.* A mention never spans a comma, semicolon, colon, `!`, `?`, brackets, a dash or a sentence-ending
  period; an abbreviation dot ("U.S. Lab") is not a barrier. Number words become digits only right after a
  capitalised "Node" ("Node two"), never in "the node, one crew member" or "the node one crew member".
- *JLP names.* A Kibo/JEM mention directly followed by a logistics-module mention ("JEM Experiment Logistics
  Module", "Kibo's logistics module") is part of the JLP1 name; "ELM-PS" is a JLP1 mention.
- *Fixed cameras* do not use the mention scan for hints either (exact and relaxed matches only, as AC-PER-23).
- *Not-inside markers.* Also *dir*: "outside", "near", "behind", "beyond", "past", "across", "opposite",
  "between", "in front of", "out of", and a mention followed by "hatch"/"hatchway". A mention after "and"/"or"
  directly after another mention takes that mention's marker ("between Harmony and Kibo": both *dir*).
- *Common words need module context.* "Unity", "Harmony", "Destiny", "Tranquility", "Columbus" count only when the
  next word is module/node/lab/laboratory/PMM, or the previous word is an *in* / *dir* word, "of", "and" or "or";
  "Leonardo" only before a module word ("Unity is important", "Columbus Day", "Leonardo da Vinci" are nothing). The
  case rule applies to one-word phrases and "US …" only ("the quest airlock" is still Quest).
- *Carry.* `WindowScope.mentions` lists every module the hint or summary mentions (any marker, unseeded included);
  a window that mentions any module other than the anchor's is not carried into ("A view of Kibo from the Cupola"
  after Columbus). The anchor is re-checked against the current resolver (a catalog refresh that removes the module
  or makes it no_capture ends the carry). A non-finite `module_carry_s` (only possible programmatically) is off.
- *Private terms* (AC-PER-23 curated list) gain "cq", "cqs", "quarters", "hygiene area": a hint such as "Harmony CQ"
  now resolves to NOD2, so it must stay private.

Residual risks accepted:
- "through" is not a not-inside word ("photos through a Cupola window" is in the Cupola; "through Harmony into
  Kibo" decides nothing because "into" marks Kibo and Harmony is plain, so two modules without *in*).
- Non-ISS uses of distinctive names ("the service module of the car", "SM" in a spoken "in the SM bag") resolve.
  The video is ISS footage; "SM" was requested as a Zvezda alias.
- Gemma's signage-like hints "NOD 3" (with a space) and "To COL" are not resolved ("NoD3" matches the id). "NOD 3"
  was seen in two Cupola-caption windows; resolving it would have scored them wrong.
- At the live cadence (5 s windows every 30 s) a carry reaches one window (25 s gap; the next is 55 s after the
  anchor). Zarya windows in the run carry no evidence at all (no hint, no name in the summary) and stay unresolved.

## Amendment: private-window text redaction (UI review round 3, finding UI3-02)

The round-3 UI review found that `no_capture` windows stored no media but still stored their `scene_summary`, which
was full-text searchable (`/api/search?q=sleeping` returned "person is sleeping in a sleeping bag inside the Harmony
Module"). AC-PER-11's "summarised only" is withdrawn: a private window keeps **no descriptive text**. AC-PER-43
amends AC-PER-11, AC-PER-36 and AC-PER-37 (inventory-api side: AC-API-39).

- AC-PER-43 **Private windows carry no text.** For a window that the reconciler treats as `no_capture` (any reason:
  camera/hint/scene-summary private place, sleep cue, privacy hold) the ingest payload's `window` has:
  - `private: true` and `privacy_reason` = one coarse category: `"private_zone"` (camera, hint or summary names a
    no_capture place; was `location` / `scene_summary`), `"sleep_cue"` or `"privacy_hold"`. A capturable window has
    `private: false` and `privacy_reason: null`.
  - `scene_summary: null`, `transcript: null` and `audio_events: []` (the contract allows null for both texts;
    audio event labels are model text too).
  - `privacy = {"no_capture": true, "reason": <the same category>}` plus `field` for a sleep cue (e.g.
    `people[1].appearance`, a field path). The matched cue word (`cue`, e.g. "sleeping bag") is **no longer sent**: it
    is itself a description of the private scene. The INFO log keeps the cue word (logs are not searchable through
    the API, and they never contain the model text).
  - Sightings (known objects only; people are never sighted, AC-PER-36) are location-only facts:
    `sighting.description = null` and, for a new entity, `new_entity.description = null` (its label is the catalog
    class name, because unknown objects are skipped). An object description such as "laptop next to the sleeping
    crew member" would otherwise describe the private scene.
  - Decision-log subjects name the catalog class (`label` = class name), never the omni label text; speech-act
    subjects already omit the utterance.
  - Events keep `actor_desc = null` (AC-PER-36) and carry no text field.
  *Accepted.* A private window's new object entity has no description, so a later same-or-new decision for it
  has only the class, place and time (it was created from a private window; describing it from that window is
  exactly what is withheld). Speech claims from a private window's transcript still become `moved` events (a
  location fact; the words are not stored).
- AC-PER-44 **A person's own movement has no actor** (amends AC-PER-35; orchestrator request, round 3). When the
  entity of an omni event is a person track itself (the event's object label maps to the person class), the event
  gets `actor_desc = null`: the person moved, nobody moved them. `actor_desc` is set only when a person handles an
  object. Live example fixed: "person 87dbd193 moved by person (spacesuit, dark visor)".
  *Eval:* AC-PER-38's scorer found the sleep-cue window by its stored summary; it now also matches the stored
  `privacy_reason = "sleep_cue"` (the summary is null).

## Amendments: cross-module person continuity (live Gemma 4 run, 2026-10-18)

Measured (live Gemma 4 run on the NASA tour, one handheld camera through about 9 modules, 7 to 11 crew aboard):
**25 person tracks** (USOS 17, NOD1 2, NOD3 2, ZVEZDA 2, COL1 1, CUP1 1). Two causes:
- AC-PER-35 matches a person sighting only against tracks last seen in the **same module**. On the ISS the crew float
  between modules and the handheld camera follows them, so every module starts a new track. Segment-level windows
  (`USOS`) match only tracks at that exact location seen recently.
- The appearance normaliser loses evidence on real strings. "grey long-sleeve shirt" loses its colour (two words
  between colour and garment) and then matches any shirt; "white spacesuit", "orange helmet", "dark visor", "watch"
  and "harness" are not descriptors; "US flag" is not "American flag".

The raw replies of the run (rustfs `raw/cam-handheld/2026-10-18/*.878740.json`, read-only) are saved as
`tests/fixtures/gemma4_tour_people_2026-10-18.json`: 99 stored windows, 03:02:48 to 03:57:48 (the tour loops about
every 15 minutes), people[] verbatim. AC-PER-45 to AC-PER-48 amend AC-PER-35. The safety rules stay: two entries of
one window never share a track, a colour clash is never a match, private windows track no one (AC-PER-36), and people
are tracked by appearance text only, never by biometrics.

- AC-PER-45 **Cross-module person continuity** (amends AC-PER-35 *Matching*). The candidates of a person sighting
  are:
  - *same place* (AC-PER-35, unchanged): live person-class entities last seen in the observation's module or one of
    its zones (outside modules: the exact location, recent only);
  - *cross-module*: any other live person-class entity, last seen **anywhere** within
    `person_cross_module_s` (`PERCEPTION_PERSON_CROSS_MODULE_S`, default 600 s, 0 = off) of the sighting's time. One
    cached `GET /items?catalog_id=<class>&sort=-last_seen_at&limit=50` per window (the AC-PER-30 class listing).
  A cross-module candidate is eligible only when the observation has appearance descriptors and the similarity is
  ≥ `person_match_floor`; a featureless observation ("astronaut", "hand visible") matches same-place candidates only,
  as before. Beyond `person_cross_module_s`, only same-place candidates are eligible, as before. Assignment stays
  global and best first: higher similarity, then closeness (Dice), then **same place before cross-module** (same-module
  matches win ties), then recent before stale, own zone, most recently seen.
  A candidate whose description clashes in colour with the observation is never eligible, whatever the floor (also
  for a floor of 0).
- AC-PER-46 **Appearance normalisation, real-string round** (amends AC-PER-35 *Appearance similarity*). Measured on
  the 72 distinct appearance strings of the fixture (docs/tdd-log/perception.md):
  - *Sleeve length is not a word between colour and garment.* "long sleeve(d)", "short sleeve(d)" and "sleeveless"
    are dropped before parsing, so "grey long-sleeve shirt" is grey shirt (9 of 72 strings lost their shirt colour).
  - *Garment synonyms.* "tank top" is a shirt; "spacesuit", "space suit", "EVA suit", "EMU" are `spacesuit` (a
    garment of its own, not the flight suit `suit`, and not a sleep cue). Already: t-shirt / tee / polo → shirt,
    flight suit / jumpsuit / coveralls → suit.
  - *Accessories are descriptors:* helmet, visor, watch, harness (with a colour when one is named: "orange helmet").
  - *Insignia.* "US flag" / "USA flag" / "U.S. flag" is "American flag".
  - *Colour families.* Unchanged: navy, teal → blue; "dark blue" is blue (a specific colour wins over the modifier);
    "dark" alone is the dark family.
  - *Pose and activity words* (floating, looking, holding, working, standing, reaching, ...) are never descriptors:
    only the clothing / colour / insignia vocabulary counts (unchanged, now tested on the real strings).
  - *Weights.* Hair 0.5 (generic: most crew have dark hair), garments 1, **distinctive accessories 2**: gloves,
    headset, visor, helmet, glasses, watch, harness, spacesuit, lanyard, badge. Insignia words (flag, patch, American,
    NASA, ...) weigh 1 each; a flag patch is already two or three of them. A distinctive accessory on one side only
    lowers the similarity ("blue suit, orange gloves" vs "blue flight suit with American flag patch": 0.5 → 0.33).
- AC-PER-47 **Soft cap at the crew size** (amends AC-PER-35 *New track*). When the person class has an
  `expected_count` (seed: `crew-member` 7) and the live person-class entity count at the start of the window
  (`count_live_entities`, cached per window) is already ≥ it, a person sighting that found no candidate under
  AC-PER-45 may attach to **any** live person-class entity (any place, any gap), still only with appearance
  descriptors, similarity ≥ `person_match_floor` and no colour clash, best first as AC-PER-45, never two sightings of a
  window on one entity. Otherwise it starts a new track, as before: the cap prefers attaching but never forces it
  (more crew than the seed's count, e.g. during a handover, still get tracks). `person_soft_cap`
  (`PERCEPTION_PERSON_SOFT_CAP`, default true). The crew size is not hard-coded: it is the catalog's `expected_count`.
- AC-PER-48 **Eval and real-data regression.**
  - *Eval* (eval/, perception AC-PER-38 extended): after the revisit pass the local runner replays a **crew transit**
    pass: the scene's frames again, `TRANSIT_GAP_S` (60 s) after the revisit, through the roaming camera
    `cam-handheld` with GoldOmni profile `crew_transit`: no objects, events or transcript, every gold actor in
    `people[]` of every window with the Gemma-like varying appearance text, and a `location_hint` naming one of three
    modules (Unity, Columbus, Kibo) by thirds of the scene. Each actor is thus seen in LAB1 (first pass and revisit)
    and then in 3 more modules within 5 minutes. `person_tracks_per_person` is measured on the final database (all
    passes); its gate tightens from ≤ 1.5 to **≤ 1.3**. The transit pass runs only for the `local` target with the
    default replay (an injected replay measures none; the cluster's omni cannot be scripted).
  - *Real data* (tests/test_ac_per_45_48_person_continuity.py): the fixture windows replayed through the reconciler
    (cam-handheld, FakeInventory, the live 30 s cadence) produce at most 60 % of the person tracks of the AC-PER-35
    matching (measured counts in docs/tdd-log/perception.md), and the replay never puts two entries of one window on
    one track.

### Review round (AC-PER-45..48): fixes and residual risks accepted

Measured with the new normaliser in every run, so only the matching differs: cross-module matching raised the
colour-clashing pairs inside tracks from 3 to 28 on the fixture. A track keeps its first, often short, description
("blue shirt, dark hair"), and the overlap coefficient divides by the shorter side. Fixes (each test-first,
`test_review_*` in tests/test_ac_per_45_48_person_continuity.py and eval/tests/test_ac_eval_09_crew_transit.py):
- *Weak tiers need two-sided, specific evidence.* A cross-module or soft-cap candidate also needs closeness (Dice)
  ≥ `person_match_floor` and a specific shared trait (`people.appearance_evidence`): a garment both texts colour
  compatibly (hair excluded), or a shared distinctive accessory or insignia. Colourless garments ("astronaut in a
  shirt") and a watch are not evidence. Same-place matching keeps the lenient AC-PER-35 rule.
- *Latest-sightings clash.* The reconciler remembers the appearance texts of each person track's last
  `PERSON_RECENT_DESCRIPTIONS` (5) recorded sightings, in memory, in this process only. The same text is stored as
  the sighting description, and private windows record none. A candidate whose first description **or** any of these
  texts clashes in colour with the observation is not eligible, in every tier. The memory is lost on restart, which
  falls back to the first description only.
- *Soft-cap entry as specified.* Only sightings that had **no** candidate go to the cap pass. A sighting that lost its
  candidate to another person of the same window starts a new track; it does not take a stale look-alike.
- *Over-broad text is featureless* (injection). More than 8 descriptor keys, or one garment in more than 3 specific
  colours, gives no descriptors. A crafted "every colour, every garment" appearance matched every crew member at 1.0.
- *Normaliser.* "long sleeves" (plural), "flags"/"logos" (plural); "watch" weighs 1 (common, not distinctive).
- *Eval.* `person_track_recall` is also re-measured after the transit pass: merged actors lose recall, while
  `person_tracks_per_person` is a maximum and would pass a merge.
- *Logs.* The class-listing failure warning names its uses (decision context, person cross-module and cap candidates).

Residual risks accepted:
- *Blue-polo over-merge.* Crew in the same coloured shirt whose descriptions never clash (no trousers named, or the
  same trousers) still merge, now station-wide within 600 s, and at any gap once the cap is reached. On the fixture
  the track "blue shirt, dark hair" takes 20 sightings in 7 places, probably 2 to 4 people. Without faces this is the
  price of continuity; the alternative is a track per module.
- *Under-merge.* A terse text in another module ("blue flight suit" against "... with American flag patch", Dice
  0.4) starts a new track. Gemma's varying trouser colours for one person ("grey"/"black pants") can split a track by
  the latest-sightings rule.
- *Listing limits and failures.* Both listings return at most 50 rows: the cap's "any live entity" means the 50 most
  recently seen. If the class listing fails, cross-module and cap matching are silently off for that window (the
  warning is logged). If the module listing fails, same-module tracks compete as cross-module ones.
- *Two cameras.* The same person seen by two cameras in two modules at once can be two tracks, and two similar people
  seen that way can be one. No transit-time rule: the handheld camera follows crew through hatches within seconds.
- *Eval coverage.* The synthetic actors' shirts always clash and there are fewer of them than `expected_count`, so
  the eval exercises cross-module continuity but neither the cap nor look-alike merges. Those are covered by the
  unit tests and the real-fixture purity check (≤ 5 clashing pairs).

## Amendments: crop privacy, structure filter, event places (UI review round 4, 2026-10-18)

Round 4 (docs/ui-review/round4) found, live:
- **UI4-01 (P1).** An unknown "window" object whose omni box was misplaced was linked by IoU to a detector box over a
  crew member, so its 915×258 crop (top of a 1080×608 frame) showed the person's face and upper body. The crop was
  stored as the item snapshot, embedded as an exemplar and shown as the review image (purged by hand). Pass 1 of
  linking accepted a detector box of **any** label, including the detector's own `person` box.
- **UI4-05.** All 6 open unknowns were station structure (window, hatch, hatchway, handle, fabric cover, a spacecraft
  outside the window): nothing an operator can classify.
- **UI4-06.** "Person … left USOS" while the track's next sightings were still in USOS; "Person … seen" with no place.

AC-PER-49 to AC-PER-52 amend AC-PER-5, AC-PER-8, AC-PER-10, AC-PER-13, AC-PER-22, AC-PER-24 and AC-PER-36.

- AC-PER-49 **No crop ever overlaps a person** (amends AC-PER-8, AC-PER-13, AC-PER-22, AC-PER-36).
  - *Detector labels.* The replay's detector query labels are the catalog class names plus `person` and `face`
    (added when missing). A `person`/`face` detection is never linked to, stored as, or counted as an object.
  - *Body labels* (`perception.privacy.is_body_label`, matched with the AC-PER-50 head-noun rule on the `label_key`
    normalisation): face, head, hand(s), arm(s), finger(s), leg(s), foot/feet, torso, body, human, man, woman, people.
    An omni object with a body label is never an instance (no sighting, entity, review or event target). Its box is a
    person region.
  - *Person regions of a frame:* every detector box of that frame whose label maps to the person class (catalog
    `category = person`) or is a body label; every omni object box whose label maps to the person class or is a body
    label, on its `frame_index` (an omni box without a `frame_index` counts for every frame of the window). `people[]`
    entries carry no box in the observation schema (contract), so they add no region; a person seen only in `people[]`
    and missed by the detector cannot be localised (residual risk below).
  - *Linking.* A person-class detection links only to a person instance; a face/body detection links to nothing
    (both passes: IoU and label). A non-person instance can therefore never take a person's box.
  - *Overlap rule* (`perception.privacy.overlaps_person(crop_box, regions)`, normalised coordinates; ratios are
    invariant under the frame's aspect ratio). A crop box C overlaps a person region P when **any** of:
    - IoU(C, P) > 0.05;
    - area(C ∩ P) > 10 % of area(C);
    - C covers ≥ 25 % of P's *head zone*: the top third of P (the whole box for a face/head region).
  - *Dropping.* Before an exemplar crop is embedded, it is checked against every person region of its frame. When it
    overlaps one, the instance gets **no embedding, no exemplar and no crop**; nothing is uploaded for it. The
    sighting is still recorded (with its box) and carries `person_overlap: true`; the instance is matched like any
    exemplar-less sighting (AC-PER-22/29); its reviews use the whole frame (`image_uri` = frame), never a crop.
  - *Defence in depth.* Every sighting of a capturable window whose frame has person regions carries
    `person_boxes` (at most 10, rounded to 6 decimals), so inventory-api checks the crop itself (AC-API-41).
  - *Statistic.* `window.person_crops_dropped` = the number of crops dropped (inventory-api ignores unknown window
    fields).
  - *Accepted.* A person the detector misses and omni boxes nowhere has no region, so a crop over them is not
    caught. OWLv2 queries `person` and `face` on every frame at full resolution (AC-PER-21), and the misplaced-box case
    seen live is closed by the linking rule (the crop is always the linked detector box, never omni's box).
- AC-PER-50 **Station structure is not inventory** (amends AC-PER-7, AC-PER-8).
  - *Denylist* (`perception.labels.is_structure_label`). The label is normalised (`normalise_label`), cut before the
    first preposition or participle ("of", "with", "on", "in", "showing", "visible", "seen", "leading", ...), and
    matched with `label_key`: the whole cut phrase, its last two words or its last word (the head noun) against the
    structure keys: hatch, hatchway, hatch handle, window, porthole, viewport, handle, handrail, handhold, wall, panel,
    rack, cable(s), cabling, wire(s), wiring, cover, fabric cover, light(s), lamp, vent, grille, duct, fan, ceiling,
    floor, deck, bulkhead, hull, module, node, station, space station, ISS, structure, station structure, airlock,
    cupola, opening, port, docking port, frame, bracket, mount, rail, interior, background, view, spacecraft, vehicle,
    capsule, Dragon, Soyuz, Progress, Cygnus, solar array, truss, radiator, Earth, planet, space, stars.
  - *Catalog first.* Only objects whose label maps to `UNKNOWN` (AC-PER-7) are checked, so a catalogued item is kept
    however structural it sounds (a cargo transfer bag, a `cable` of the seed's power-cable class).
  - *Effect.* A denylisted object is dropped before consolidation and linking: no entity, no sighting, no review, no
    exemplar, and its detector box stays available to other objects. Omni events naming it are dropped as events for
    an unobserved object. `window.structure_filtered` counts them (a window statistic only), and `ReplayStats`
    sums them.
  - *Measured* on the real Gemma 4 replies (tests/fixtures/gemma4_tour/ and gemma4_tour_people_2026-10-18.json): 17 of
    the 19 unknown listings (7 of 8 distinct labels) are structure; the two kept are `watch` (a wristwatch). No
    listing that maps to a catalog class is filtered.
  - *Prompt* (amends AC-PER-24): the budget line says to skip station structure (walls, panels, racks, handrails,
    handles, hatches, windows, lights, vents, covers, cables) and anything outside the station (Earth, spacecraft),
    and that people go in `people`, never in `objects`.
- AC-PER-51 **Event places** (amends AC-PER-10, AC-PER-44).
  - An omni event other than `moved` whose destination hint does not resolve gets `to_location` = the target's
    sighting location in this window: where it happened ("seen"/"used" always name a place when the window has one).
    `from_location` is unchanged (the entity's previous place). `removed` and `consumed` are not filled: the object
    leaves the inventory there, so a destination would be false (the eval scores them as out of scope).
  - A `moved` event whose destination hint does not resolve gets `to_location` = the target's sighting location
    when that place differs from `from_location` (not the same location, not one inside the other, not the same
    module).
  - *Person tracks.* A `moved` event of a person track (the target maps to the person class) is emitted only when the
    track has a known previous place (`from_location`) and its destination (the resolved hint, else this window's
    sighting) is a different place by the rule above. Otherwise it is dropped (logged): "left USOS" while the track's
    sighting in the same window is still in USOS is the contradiction seen live. A new track (no previous place) keeps
    the event with `to_location` = its sighting ("arrived in").
- AC-PER-52 **Tests** (tests/test_ac_per_49_52_crop_privacy_structure.py). Real geometry: the live 915×258 crop at the
  top of a 1080×608 frame over a person box; partial overlaps (each rule alone), several people, a face box, an omni
  person box, a crop clear of everyone (kept). The denylist on the real fixtures (fraction removed, no catalog match
  removed). Event places on the live "left USOS" case.

### Review round (AC-PER-49..52): fixes and residual risks accepted

Fixes (each test-first, `test_review_*` in tests/test_ac_per_49_52_crop_privacy_structure.py):
- *Body labels vs the catalog (P1).* "head" fuzzy-maps to the `headset` class, so a head box became a headset instance
  and its crop was uploaded. A body label is now kept as an item only when it is an exact/synonym catalog name of a
  non-person class (e.g. a catalogued "robot hand"), and never for face/head labels; otherwise it is a person region
  and not an instance.
- *Determiners (P1).* `label_heads` drops leading determiners ("the man", "a woman", "the head"); `figure` and
  `silhouette` are body heads.
- *Face kind for the API.* Sightings send `person_box_kinds` (parallel to `person_boxes`, "person" | "face") so
  inventory-api applies the whole-box head zone to face boxes too. Boxes are sent ordered (x1 <= x2, y1 <= y2), the
  geometry orders inverted boxes itself, and a crop with no area fails closed (as in the API).
- *Box cap.* Before the 10-box cap, the regions that overlap the object come first.
- *Structure false positives.* `module`, `fan`, `mount`, `frame`, `cover` are structure only as the whole label
  ("memory module", "portable fan", "camera mount", "laptop cover", "picture frame" are kept; "fabric cover" stays a
  two-word key). `robot arm` joins `robotic arm` and Canadarm.
- *Event dedup.* Filled destinations vary with the zone of the box centre, so the cross-window event dedup key uses the
  destination's module (the place itself outside modules).

Residual risks accepted:
- *Detector misses.* A person neither detected nor boxed by omni has no region (people[] has no box).
- *Recall cost.* Any object held in front of a crew member loses its exemplar (matched by class and place instead).
- *Broad heads that remain* (handle, panel, rack, light, port): an unknown "bag handle" or "rack of tools" is dropped
  silently as structure.

### AC-PER-53 Unknown objects stay visible (final Gemma run, 2026-10-18)
- The omni prompt uses catalog names only when an item *clearly* matches. It asks explicitly for loose non-catalog
  items with a short plain label, saying that NASA must classify them, and forbids forcing an item into a catalog
  class. The structure rule (AC-PER-50) is scoped to fixed station parts, not loose items.
- **Measured before:** 99 object listings, 2 non-catalog labels (both structure), 0 unknowns in the review queue.
  Tests: tests/test_ac_per_53_unknowns_prompt.py.

## Amendments: detector boxes for unknown objects (live Gemma 4 run, 2026-10-18)

Measured live after AC-PER-53: three unknowns came back ("orange tool" COL1, "orange sphere" and "white wrapped
object" JPM1), each with `image_bbox` and `image_bbox_source` null. The review image was the whole 1080×608 frame
with no box. Causes, read in the code and the raw replies:
- OWLv2 is queried with the catalog class names plus `person`/`face` only (AC-PER-49), so it never looks for
  "orange sphere", and a label link (AC-PER-8 pass 2) for an UNKNOWN label cannot happen.
- Gemma's own boxes are absent: this run's replies give boxes on a 0..1000 scale, which AC-PER-33 clamps to a
  degenerate box and nulls (see the residual risks below).

AC-PER-54 to AC-PER-57 amend AC-PER-5, AC-PER-8, AC-PER-13, AC-PER-22 and AC-PER-34.

- AC-PER-54 **Detector boxes for unknowns** (amends AC-PER-5, AC-PER-8).
  - *When.* After linking (AC-PER-8, both passes), each instance that maps to `UNKNOWN`, is not station structure
    (AC-PER-50), is not a body label (AC-PER-49) and has **no linked detector box** is a *query candidate*. A window
    that is `no_capture` (any reason) runs no queries: its unknowns are not instances anyway.
  - *Query text.* `perception.labels.noun_phrase(label)`: `normalise_label`, leading determiners dropped, cut before
    the first preposition/participle (as AC-PER-50), at most the last 6 words ("orange tool on the wall" -> "orange
    tool"). No descriptor phrase: OWLv2 does best on short noun phrases (its text queries are capped at 16 tokens;
    the detector also truncates), and a description can name a person. Empty labels give no query.
  - *Never a person query* (AC-PER-49). No query is made from a label that is a body label, maps to the person class,
    or contains a person word anywhere (`perception.privacy.mentions_person`: the body heads and whole labels of
    AC-PER-49 plus person, persons, astronaut(s), crew, crewmember(s), cosmonaut(s), member(s), man, men, woman,
    women, human(s), people, face(s), head, hand(s), crewman/crewmen, spacewalker(s), kid(s), child(ren), boy(s),
    girl(s), guy(s), someone, somebody, hair, eye(s), neck, shoulder(s), skin, beard, wrist(s), thumb(s), mouth, nose,
    lips), e.g. "astronaut's bag", "crew member glove", "crewman's pouch".
  - *Frame.* The instance's linked frame (`frame_index` of its best listing; the latest frame when omni gave none).
  - *Budget.* At most `config.unknown_queries_max` distinct query texts per window (`PERCEPTION_UNKNOWN_QUERIES_MAX`,
    default 6, 0 = off, max 40), chosen by omni confidence (then window order). Instances sharing a query text in one
    frame share it. Queries are grouped by frame: **one extra detector call per frame** that has queries, with only
    the query texts (never the catalog labels again, never `person`/`face`), on the full-resolution keyframe
    (AC-PER-21). Extra calls per window = the number of frames that have queries (<= window frames). A failing call is
    logged and skipped (the window is still written, without query boxes; its texts are not counted).
  - *Link.* For each query instance (omni confidence order), the detections of its frame whose label is its query
    text are ranked by score. Boxes that belong to another object are skipped (IoU >= `config.link_iou`, same
    frame): every detection of the main call (any label, linked or not) and every box linked to another instance. The **first remaining** box is the candidate; it is linked only when its score >=
    `config.unknown_query_min_score` (`PERCEPTION_UNKNOWN_QUERY_MIN_SCORE`, default 0.3, <= 1; calibrated below) and it does not overlap
    a person region (AC-PER-55). A rejected candidate is not replaced by a lower box. The linked box becomes the
    sighting `bbox` with `bbox_source = "detector"` (AC-PER-34), and the instance goes through the exemplar gate
    (AC-PER-22) and the person-overlap guard (AC-PER-49) like any detector-linked instance.
  - *Catalog objects are unaffected.* Instances that map to a catalog class never query, and query boxes are never
    offered to them (no IoU or label link, no consolidation input, no stock count, no person region).
  - *Statistics.* `window.unknown_queries` (query texts whose call succeeded), `window.unknown_boxes_linked`,
    `window.unknown_query_person_rejected` and `window.unknown_query_ms` (wall time of the extra calls); inventory-api
    ignores unknown window fields. One log line per window with queries ("unknown-object queries"): queries, frames,
    calls, linked, ms.
  - *Cost.* OWLv2's cost is the image encoder, so each extra call costs about one detection (≈ 0.35 s per frame on the
    T4). Typical window: one or two unknowns → one or two extra calls; worst case `min(budget, frames)` calls.
- AC-PER-55 **Query boxes and people** (amends AC-PER-49).
  - *Fail closed when the detector saw nobody.* When omni reports people (`people[]` entries, or an object labelled
    as the person class or a body part) but no frame of the window has a person region, no query box is linked
    (counted as rejected): `people[]` carries no box, so the crop guard would have nothing to check. One region in
    any frame is enough (live Kibo window: the crew member is boxed on frame 4, 0.32, the sphere is on frame 0).
  - A query candidate that overlaps a person region of its frame (the AC-PER-49 rule and regions: detector
    `person`/`face`/body boxes and omni person/body boxes) is **never linked**: the instance keeps omni's box (or
    none), gets no exemplar and no crop, and its review uses the whole frame. Counted in
    `window.unknown_query_person_rejected`, logged without the label.
  - The AC-PER-49 crop guard still runs on every linked box before any crop is embedded or uploaded (defence in
    depth); `person_boxes` are sent as before.
- AC-PER-56 **Review image of an unknown** (amends AC-PER-13).
  - Every review of an UNKNOWN instance uses the whole evidence frame (`image_uri` = `frame_uri`), never the crop,
    so inventory-api returns the sighting's box as `image_bbox` (with `image_bbox_source`) and the dashboard draws it
    over the frame: more context for NASA than a tight crop. The crop is still stored as the exemplar's `crop_uri`.
    Without a frame (none stored) the crop is used, as before. Reviews of catalog instances are unchanged.
- AC-PER-57 **Tests** (tests/test_ac_per_54_57_unknown_queries.py): a fake detector that returns a box only for the
  new query (linked, `bbox_source = detector`, crop, exemplar, review = frame + box); threshold (below / at); budget
  (at most N texts, highest confidence first, one call per frame, 0 = off); no queries for catalog, structure, body,
  person-word labels or private windows; the person guard (an "orange sphere" whose best query box overlaps a person
  gets no crop, no link, and a lower box is not used); catalog-matched objects unchanged (same payload with and
  without the query detector); a replay over the live Gemma 4 reply with the unknown "orange sphere"
  (tests/fixtures/gemma4_unknowns_2026-10-18.json); a model-marked check of the real OWLv2 on the live Kibo frame.

- *Threshold calibration* (real OWLv2 base-patch16-ensemble on CPU, the three live frames, 1080×608):
  - "orange sphere" (Kibo, video t ≈ 55 s): best 0.604, px [565, 253, 604, 290], exactly the sphere; next 0.148.
  - "orange tool" (COL1): best 0.277 is the *wrong* object (the orange sphere seen through the far hatch, px
    [561, 165, 581, 185]); the real tool scores 0.142.
  - "white wrapped object": best 0.058 (nothing usable).
  - A threshold of 0.3 links the sphere and rejects the wrong tool box; 0.2 would have linked it. Default 0.3.
  - Latency: one call takes ≈ 0.50 s on the laptop CPU with 1 query and ≈ 0.52 s with 6 (the image encoder
    dominates), so the cost is per extra call (frame), not per query: ≈ 0.35 s per call on the T4.

### Residual risks (AC-PER-54..57)
- *Wrong box.* OWLv2 may return a confident box on a different orange object; the threshold, the frame restriction
  and the person guard bound this, and the operator sees the box on the whole frame.
- *Detector misses, widened.* Before AC-PER-54 an unknown without a detector box never got a crop. Now a free-text
  query box can land on a person whom OWLv2 missed on that frame while boxing them (or someone else) elsewhere in the
  window. Bounded by the fail-closed rule, the AC-PER-49 guard and the person-word filter.
- *Repeated cost.* A persistent unknown is queried again in every window (one extra call per frame with queries).
- *Omni's box ignored.* When omni has a valid box, a query box elsewhere on the frame still wins; with Gemma's boxes
  nulled (below) there is nothing to compare with. Follow-up once the box scale is fixed: prefer query boxes that
  overlap omni's box.
- *Eval.* The eval's fake detector never returns unknown objects, so `make eval` exercises the calls (it reports
  `unknown_queries`) but cannot score the links; the unit and replay tests cover them.
- *Gemma 0..1000 boxes.* The live Gemma 4 replies give `bbox` on a 0..1000 scale in `[y1, x1, y2, x2]` order
  (Gemma's native box format), not the `[x1, y1, x2, y2]` 0..1 the prompt asks for, so AC-PER-33 nulls every omni box.
  Query boxes do not depend on omni's box; fixing the scale is a separate change (it affects every object's linking). **Fixed by AC-PER-58.**

## Amendments: box format and UI review round 5 (live Gemma 4 run, 2026-10-18)

Measured on the live run (cam-handheld replaying tour-medium.mp4 in a loop, 34 raw replies fetched read-only, now
tests/fixtures/gemma4_boxes_2026-10-18.json) and on docs/ui-review/round5 (UI5-02, -03, -04, -05, -09).
AC-PER-58 to AC-PER-63 amend AC-PER-3, 8, 10, 24, 33, 41, 44, 50, 51 and 54.

- AC-PER-58 **Omni box format** (amends AC-PER-3, AC-PER-24, AC-PER-33).
  - *Measured.* All 142 boxes of the 34 replies are integers in 0..1000. Drawn on the real 1080x608 video frames,
    `[y1,x1,y2,x2]/1000` lands on the object and `[x1,y1,x2,y2]/1000` does not (treadmill belt, laptop, orange sphere,
    wristwatch, camera): Gemma's native box_2d. Earlier runs sometimes gave 0..1. The old clamp kept 1 of 142 boxes
    (a whole-frame box, also wrong).
  - *Order per model.* `OMNI_BOX_ORDER` = `auto` (default) | `xyxy` | `yxyx`; `auto` = `yxyx` when `OMNI_MODEL`
    contains "gemma", else `xyxy` (Qwen and the fakes keep today's behaviour). Chat and realtime clients use it.
  - *Prompt.* For `yxyx` the prompt asks for Gemma's native format ("bbox is [y1,x1,y2,x2] as integers on a 0..1000
    grid"), and the schema shown in the prompt has its bbox entry rewritten to match. Asking a model for the format it
    was trained on is more robust than asking for another and converting what comes back. Validation still uses the
    contract schema (xyxy 0..1) after conversion.
  - *Scale per reply.* When any coordinate of any well-formed box in the reply is > 1.5, every box of the reply is on
    the 0..1000 grid (divided by 1000), else 0..1. Per reply, not per box: a corner box `[0, 0, 1, 1]` in a grid reply
    is a 1/1000 box, not the whole frame. Then the order is applied and the AC-PER-33 rules (swap, clamp, degenerate
    under 2%) run. Boxes leave the client as canonical `[x1, y1, x2, y2]` in 0..1.
  - *Effect on the real replies:* 141 of 142 boxes survive (1 is 1.7% wide, degenerate by AC-PER-33).
  - *IoU link classes* (amends AC-PER-8 pass 1). Measured with the real OWLv2 (CPU) on the real frames: with boxes
    restored, 29 of 39 IoU links took a detection of another class (laptop -> "cable" 0.34, camera -> "cargo transfer
    bag" 0.61, food package -> "laptop" 0.48). Now a catalog instance links by IoU only to a detection of its own
    class (IoU >= `link_iou`); an UNKNOWN instance may take another class's box only at IoU >=
    `link_iou_cross_class` (`PERCEPTION_LINK_IOU_CROSS_CLASS`, default 0.5, <= 1); a same-class box is preferred
    over a better-overlapping other-class box. On the real run: before 37 of 139 instances linked (1 by IoU, the
    wrong whole-frame box), after 42 of 141 (15 by IoU: 11 same class, 4 unknowns to another class's box).
  - Tests: tests/test_ac_per_58_box_format.py.
- AC-PER-59 **Unknown queue flood** (UI5-02; amends AC-PER-50, AC-PER-54).
  - *Structure keys added:* latch, latches, hatch frame, insulation, foil, foil insulation, thermal insulation,
    treadmill, exercise equipment/machine/device, ergometer, cycle ergometer, ARED. `cover` is structure as a head noun
    when every word before it is a modifier (colour, material, size, shape: "white cloth cover", "protective
    cover"); after an item noun it is the item's cover ("laptop cover", "lens cover" are kept).
  - *Fixed in place:* an UNKNOWN whose label or description says "mounted/fixed/bolted/built/installed on|to|into
    (the) wall/rack/hatch/panel/ceiling/floor/bulkhead/frame", "attached to (the) wall/hatch/bulkhead/frame" or "on
    the hatch frame" is structure. "attached to a rail/rack" is not: in the live run 16 listings are pouches and bags
    velcroed to a rail or rack, i.e. stowed loose items.
  - *Generic colour+noun labels* (`labels.is_generic_label`): the noun phrase is modifier words plus a generic head
    (object, item, thing, cylinder, container, device, box, piece, unit, component, equipment, part, ...) and the
    description has nothing distinguishing (text, label, logo, serial, barcode, sticker, markings, screen, display,
    a digit or a quote). Such an UNKNOWN is kept only when the AC-PER-54 query of its own label linked a detector box;
    otherwise it is dropped (no entity, sighting or review) and counted in `window.generic_unknowns_skipped`. An IoU
    link to another class's box does not count.
  - *Caps:* at most `unknowns_per_frame_max` (`PERCEPTION_UNKNOWNS_PER_FRAME`, default 2) UNKNOWN instances per
    frame and `unknowns_per_window_max` (`PERCEPTION_UNKNOWNS_PER_WINDOW`, default 4) per window; 0 = no cap; max 40.
    Detector-linked first, then omni confidence, then window order. Dropped ones are counted in
    `window.unknowns_capped`. Catalog instances are never affected.
  - *Measured on the round-5 labels:* yellow latch, silver foil insulation, silver cylinder (mounted on the wall),
    white cloth cover and treadmill are structure; "grey object" (the hatch) is generic. Kept: orange sphere,
    orange tool, white wrapped object, pen, wristwatch. On all 35 UNKNOWN listings of the run (seed catalog): before,
    8 were structure and 27 could reach the queue; after, 13 are structure, 6 are generic (held for a box) and 16
    can reach the queue, then capped.
  - Tests: tests/test_ac_per_59_unknown_flood.py.
- AC-PER-60 **Re-identification across modules** (UI5-03; amends AC-PER-8, AC-PER-10).
  - *Found live:* laptop 8535788a went JPM1 -> USOS -> ZVEZDA with no move. The decision log shows the Zvezda match
    was the decision model choosing it at 0.83 with appearance similarity 0.61 (p 0.63, ask band), and the USOS
    sighting before it was matched the same way at similarity 0.47.
  - *Generic classes:* `generic_classes` (`PERCEPTION_GENERIC_CLASSES`, comma-separated class names or catalog ids,
    `none` = no class; default laptop, tablet, camera, camcorder, cargo transfer bag, m-bag). For such an instance, an
    embedded candidate whose last place is in another module (both places in a known module, the modules differ; a
    segment-level place is not "another module") counts only with p >= `assign_p` (the auto band). Below it the
    candidate is not offered to the decision model. When nothing else matches, the instance becomes a new provisional
    entity with a `possible_duplicate` review naming the held candidates. Exemplar-less matching (AC-PER-22/29)
    already never crosses modules. A detector link alone does not qualify: every exemplar match is detector-linked,
    including the live Zvezda one.
  - *Module change is visible:* when an object instance (not a person track) is matched to an entity whose previous
    place is in another module, a `moved` event (previous place -> this sighting, source `pipeline`, no actor,
    confidence = the sighting's, evidence = the frame) is added, unless omni reported a `moved` event for that
    instance in this window (omni's wins), or the same move was recorded within `event_dedup_s`.
  - Tests: tests/test_ac_per_60_63_reid_carry_events.py.
- AC-PER-61 **The module carry ends on a scene change** (UI5-04; amends AC-PER-41).
  - *Found live:* window 063138 (hint "NOD 3", read from a "To NOD 3" sign; the frames show a node, not the Cupola)
    inherited CUP1 from the Cupola window 30 s before.
  - *Rule:* a window whose `location_hint` is non-empty but resolves to no location (`caption_scene_change`) is
    evidence the camera left the anchored module: it does not inherit, and the anchor is cleared. The window metadata
    says `location.carry_ended = "caption"`. A summary naming another module already ends the carry (AC-PER-41
    `mentions`).
  - *Rejected after measurement:* (a) a summary transit rule ("... a hatch into another module"): on the 51 labelled
    windows of tests/fixtures/real_tour_run_2026-10-18.json it lost one correct Kibo carry (a view from inside Kibo
    through its hatch) and gained nothing; (b) a frame-embedding (DINOv2) distance between windows: not implemented,
    the caption rule covers the live case and there is no labelled data to calibrate a threshold.
  - *Fixture correction:* the two "NOD 3" windows of the labelled run (video t 237-240 s) were labelled CUP1 (the last
    caption before them); the frames show a node with "To NOD 3" signs and the next caption is "Quest Airlock" (on
    Node 1). Their truth is now NOD1. The replay is 17/30 correct with 0 wrong; without the caption rule the carry
    puts both in CUP1 (2 wrong).
  - *Person attach (checked):* the CUP1 sighting of track 395bae46 was not an attach. It created the track. Gemma
    described the person as "grey long-sleeve shirt, blue shorts", while the frame shows a blue shirt and grey
    trousers (garments swapped). The AC-PER-45..48 clash rule holds: "blue shirt" vs "grey long-sleeve shirt" clash.
    The CUP1 sighting of 28513d12 at 06:31:38 is the carried Node 1 window that this rule fixes.
- AC-PER-62 **Segment sighting keeps a module location** (UI5-05). Perception never writes `last_location`;
  inventory-api's ingest does, so the rule is AC-API-45 (`LOCATION_DOWNGRADE_HOLD_S`, default 600 s). Perception
  reads the kept module back as the entity's `last_location` (matching, events' `from_location`, AC-PER-60's module
  test). The sighting itself stays at segment level.
- AC-PER-63 **Moved events** (UI5-09; amends AC-PER-10, AC-PER-44, AC-PER-51).
  - An object `moved` event whose target has no previous place (a first sighting, from_location null) is dropped
    (logged). The new entity already records where it was first seen.
  - A person track's own `moved` event is never "moved". When it has a destination (the resolved hint, else its
    sighting) that is a change of place, or the track is new, it becomes a `seen` event there (an arrival, no actor).
    Otherwise it is dropped (AC-PER-51).
  - Speech `moved` claims (AC-PER-10/16) are unchanged.

### Review round (AC-PER-58..63): fixes and residual risks accepted
Fixes (each test-first, tests/test_ac_per_58_63_review_round.py):
- *Held review flood (P2).* `api.candidates` returns the nearest same-class entities however dissimilar, so a
  cross-module `possible_duplicate` review was raised for almost every new generic-class sighting. Only ask-band
  (`ask_p <= p < assign_p`) held candidates raise it.
- *Held review blocked adoption (P2).* The review was added before the exemplar-less same-place adoption ran, which
  then skipped the instance and created a duplicate. The review is now added after adoption, only when still new.
- *No synthesized move from inferred places.* No AC-PER-60 `moved` event in a carried, truncated or hint-conflict
  window.
- *Placeholder hints* ("unknown", "N/A", "ISS", "station", "interior", "Node", "module") are not captions and do not
  end the carry.
- *Regex false positives.* A negation within 3 words ("not attached to the wall", "no text or markings") cancels a
  fixed-in-place or distinguishing cue; quotes count only at word boundaries (not possessives); digits count only as
  identifiers (A12, SN-2231), not sizes or counts.
- *IoU link order.* Same-class IoU links are made for every instance before unknowns may take another class's box.
- *Non-finite coordinates.* An infinite coordinate is clamped (it used to invalidate the whole reply); a NaN makes the
  box null (NaN passes JSON parsing and the schema's bounds).
- A warning is logged when a 0..1000 grid reply reaches an `xyxy` client (a Gemma served under an alias without
  "gemma" in the name: set `OMNI_BOX_ORDER=yxyx`).

Residual risks accepted:
- *Model alias.* `auto` detects Gemma by name; an alias needs `OMNI_BOX_ORDER`. A non-Gemma model that answers in
  pixel coordinates now gets plausible but wrong boxes (they used to be dropped).
- *Mixed replies.* A reply mixing 0..1 and 0..1000 boxes nulls its 0..1 boxes (by design, per reply).
- *Cable boxes.* 2 of the 4 real cross-class unknown links are OWLv2 `cable` boxes (IoU 0.58, 0.59).
- *Label links ignore omni's box.* Pass 2 still takes the best same-class detection anywhere on the frame.
- *Generic modifiers.* "storage", "protective", "padded", "soft" are modifiers, so "white storage box" is generic
  (held for a query box).
- *Garment swaps.* Gemma can swap garment colours ("grey shirt, blue shorts" for a blue shirt and grey trousers),
  which starts a wrong track; the clash rule cannot see it.
- *Recall.* A truly moved generic item (laptop carried to another module) becomes a new entity plus an ask-band
  review unless its appearance reaches the auto band.
- `FakeOmni` parses scripted replies as `xyxy` (test fake only).
