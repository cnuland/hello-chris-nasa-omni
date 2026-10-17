# 02 - Detection, Tracking, Entity Resolution (Dedup), Localization, and Evaluation for an ISS Object/Crew Inventory

*Research date: 2026-10-17. Scope: open-source-only components for a POC that watches ISS interior video (plus audio), detects and tracks objects and crew, and writes a deduplicated inventory (identity, last-seen location, who moved it, confidence, snapshot) to Postgres, with VLMs served by vLLM / vLLM-Omni.*

**How to read this document.** Statements marked **[V]** were checked against a primary source during this research (paper abstract, repo README, LICENSE file, Hugging Face model card, or GitHub API metadata pulled on 2026-10-17). Statements marked **[I]** are my inference or engineering judgment. Benchmark numbers are as reported by the authors unless stated otherwise; none were reproduced.

---

## 0. TL;DR

- **The dedup problem is the core of this project, not detection.** On a closed-world station with a known catalog, the right abstraction is *track -> catalog entity* assignment (a constrained matching problem), not "insert a row per detection and cluster later". Most duplicates come from (a) track fragmentation (the same object re-acquired as a new track after occlusion / camera move), (b) cross-camera re-sightings, and (c) VLM naming drift ("laptop" vs "ThinkPad" vs "computer"). Each needs a different fix.
- **Detection:** use a fast open-vocabulary detector driven by the *catalog vocabulary* plus *visual exemplars* (closed-world prompts), and fine-tune a closed-set detector once you have labels. Licenses matter a lot here: YOLOE is AGPL-3.0, YOLO-World is GPL-3.0, SAM 3 / SAM 3.1 and DINOv3 are under custom Meta licenses (not OSI open source, and SAM's license explicitly excludes ITAR-subject uses). Apache/MIT options: Grounding DINO (original), MM-Grounding-DINO, LLMDet, OWLv2, Florence-2, RF-DETR / D-FINE / RT-DETR, SAM 2 / 2.1, Molmo 2, Qwen3-VL, Gemma 4.
- **Tracking:** ByteTrack / OC-SORT / BoT-SORT (MIT) are adequate inside one camera; add a SAM 2 (Apache-2.0) mask propagator only for objects in hand / under heavy motion. Use appearance embeddings (DINOv2 Apache-2.0 or SigLIP 2 Apache-2.0) for re-acquisition.
- **VLMs:** use them for *slow-path* reasoning - naming/verification of crops against the catalog, event adjudication ("who moved it"), and descriptions - not for per-frame detection. Qwen3-VL (Apache-2.0) and Gemma 4 (Apache-2.0) both emit grounded boxes; Qwen3-Omni (Apache-2.0 per card) on vLLM-Omni has a streaming video+audio WebSocket API.
- **Dedup algorithm (recommended):** two-stage. Stage 1: tracklets are built within each camera. Stage 2: each closed tracklet is assigned to a catalog entity by Hungarian matching over a cost combining (catalog-class compatibility) x (appearance similarity to the entity's exemplar gallery) x (spatio-temporal plausibility given last-seen zone and time) with a "new entity" dummy column whose cost is a calibrated threshold. Middle-confidence decisions go to a human merge/split queue. pgvector HNSW stores the exemplar galleries.
- **Evaluation:** TrackEval (MIT) for HOTA/IDF1/MOTA; pairwise and B-cubed precision/recall on entity clusters for the dedup metric that matters most ("duplicate rate"); event-level F1 with a time tolerance for "moved" events; a small hand-labelled set from public ISS footage plus synthetic sequences.

---

## 1. Open-vocabulary detection and segmentation

### 1.1 Landscape and licenses (pulled from GitHub API / HF on 2026-10-17)

| Model | Paper | Code / weights license | Notes |
|---|---|---|---|
| Grounding DINO (original) | arXiv 2303.05499 | Apache-2.0 **[V]** (github.com/IDEA-Research/GroundingDINO) | Text-prompted boxes; slow-ish (Swin backbones). |
| Grounding DINO 1.5 / 1.6 Pro, DINO-X | arXiv 2405.10300 (1.5) | API repo is Apache-2.0, but models are served only through IDEA's hosted DeepDataSpace API with a token **[V]**; no weights found **[I]** | 1.5 Pro reports 54.3 AP COCO zero-shot / 55.7 AP LVIS-minival **[V]**. **Not usable** under an open-source-only rule. |
| MM-Grounding-DINO | arXiv 2401.02361 | Apache-2.0 (MMDetection) **[V]** | Fully open reproduction incl. training code. |
| LLMDet (CVPR 2025) | arXiv 2501.18954 | Apache-2.0 **[V]** (github.com/iSEE-Laboratory/LLMDet) | Fine-tuned from MM-GDINO with LLM caption supervision; Swin-T 44.7 vs 41.4 AP LVIS-minival for MM-GDINO **[V]**. Good open "Grounding DINO-class" choice. |
| OWLv2 | arXiv 2306.09683 (NeurIPS 2023) | Apache-2.0 (HF `google/owlv2-*`, big_vision) **[V]** | Supports *image-conditioned* (exemplar) queries; few-shot by averaging exemplar embeddings (mechanism from OWL-ViT, arXiv 2205.06230) **[V]**. Very attractive for a closed catalog of specific ISS items. |
| YOLO-World | arXiv 2401.17270 | **GPL-3.0** **[V]** | 35.4 AP LVIS at 52 FPS on V100 (paper) **[V]**. |
| YOLOE (ICCV 2025) | arXiv 2503.07465 | **AGPL-3.0** **[V]** | Text, visual and prompt-free modes; YOLOE-v8-L 35.9 AP LVIS-minival at ~102 FPS on T4 TensorRT (model card) **[V]**. Network-service copyleft - legal review needed. |
| Florence-2 | arXiv 2311.06242 | MIT (HF) **[V]** | 0.23B/0.77B; OD, phrase grounding, dense region captions; small enough to run per-crop. |
| SAM 2 / 2.1 | arXiv 2408.00714 | Apache-2.0 **[V]** | Promptable video segmentation with memory; no text prompt. |
| SAM 3 / SAM 3.1 | arXiv 2511.16719 | **"SAM License"** (custom, Nov 19 2025) **[V]** | Text/exemplar "promptable concept segmentation" + tracking, 848M params **[V]**; ~30 ms/image with 100+ objects on H200 (secondary sources) ; SAM 3.1 "Object Multiplex" (2026-03-27) ~7x faster at 128 objects on H100 **[V]**. License is royalty-free and allows derivatives, but it is not OSI open source and **clause 1.b.v forbids "any activities subject to the International Traffic in Arms Regulations (ITAR)"** **[V]**. |
| Molmo / Molmo 2 | arXiv 2409.17146; 2601.10611 | Apache-2.0 (code + HF weights) **[V]** | Pointing in images; Molmo 2 (Dec 2025) adds *video* pointing and tracking; reports 56.2 J&F video tracking vs 41.1 for Gemini 3 Pro (authors' numbers) **[V]**. |
| Rex-Omni (CVPR 2026) | IDEA | **IDEA License 1.0 + Qwen Research License** **[V]** | 3B MLLM detector via next-point prediction. Not usable under the rule. |
| RF-DETR / D-FINE / RT-DETR | - | Apache-2.0 **[V]** | Closed-set real-time detectors to fine-tune once the catalog is labelled. |

### 1.2 VLMs that output boxes (for slow-path verification)

- **Qwen3-VL** (arXiv 2511.21631; Apache-2.0 weights on HF **[V]**): 2D grounding with relative coordinates, JSON `bbox_2d` output, 3D grounding, and "text-timestamp" interleaving for temporal grounding **[V]**.
- **Cosmos-Reason2** (NVIDIA; post-trained from Qwen3-VL-2B/8B/32B): 2D/3D point and box localization, timestamp recognition, recommended 4 FPS video input, >= 32 GB GPU **[V]**. Code repo Apache-2.0 but **weights under NVIDIA Open Model License** **[V]** - permissive for commercial use but not an OSI license; check whether "open source only" admits it.
- **Gemma 4** (released 2026-04-02; E2B, E4B, 12B, 26B-A4B MoE, 31B; Apache-2.0 on HF **[V]**): object detection listed in Google's vision docs; community demos return JSON boxes; NVIDIA NIM card cites video up to 60 s at 1 FPS **[V, secondary]**. Box format and accuracy unbenchmarked publicly **[V]**.
- **Molmo 2** (above): best open option for *pointing* at an object across video frames.

**[I] Practical assessment.** VLM grounding is good enough to *verify* or *name* a candidate box (is this crop the "Ziploc of wipes" from the catalog?), but too slow and too variable for per-frame detection of dozens of objects. Use a detector for recall and a VLM for precision/naming on a sampled subset (track births, track ends, low-confidence classes).

### 1.3 Closed-world strategy [I]

1. Start with an Apache/MIT open-vocab detector (LLMDet or OWLv2) prompted with the catalog's class names **plus** image exemplars per item (OWLv2 image-conditioned queries; SAM 3 also supports exemplars but has the license issue).
2. Harvest crops and VLM-verified labels from the POC footage; after a few hundred examples per class, fine-tune RF-DETR or D-FINE (Apache-2.0) as the closed-set fast path. Keep the open-vocab detector as a low-rate "novelty" pass to catch uncatalogued objects.
3. Microgravity: objects appear at arbitrary orientations and float mid-frame without a supporting surface. COCO/LVIS-trained detectors carry a "gravity prior" (objects rest on surfaces, upright). No public dataset of floating tools in a spacecraft cabin was found **[V - negative search]**. Mitigate with rotation augmentation (full 360 degrees plus out-of-plane) during fine-tuning and by collecting exemplars of each catalog item in multiple poses. The Astrobee semantic-masking paper fine-tuned a box detector on ISS imagery with eight ISS-specific classes (cargo bags, laptops, etc.), showing in-domain fine-tuning is feasible (arXiv 2411.01804) **[V]**.

---

## 2. Multi-object tracking and re-identification

### 2.1 Tracking-by-detection baselines

| Tracker | Paper | License | Comment |
|---|---|---|---|
| SORT / DeepSORT | arXiv 1602.00763 / 1703.07402 | GPL-3.0 (original DeepSORT repo) **[I - verify]** | DeepSORT adds appearance; dated. |
| ByteTrack | arXiv 2110.06864 | MIT **[V]** | Associates low-score boxes too - helps for partially occluded / motion-blurred items. |
| OC-SORT | arXiv 2203.14360 | MIT **[V]** | Observation-centric re-update; robust to non-linear motion and occlusion - relevant to floating/tumbling items and handheld cameras. |
| BoT-SORT | arXiv 2206.14651 | MIT **[V]** | Adds **camera-motion compensation** (global motion via sparse optical flow / ECC) and ReID - the most useful property for handheld/free-flyer cameras. |
| StrongSORT | arXiv 2202.13514 | GPL-3.0 **[I - verify]** | |
| BoxMOT (collection) | github.com/mikel-brostrom/boxmot | **AGPL-3.0** **[V]** | Convenient, but copyleft; reimplement or use the MIT originals. |
| roboflow/supervision | MIT **[V]** | Ships ByteTrack and utilities (zones, annotators). Convenient Apache/MIT glue. |

### 2.2 Learned / segmentation-based trackers

- **MOTIP** (CVPR 2025, arXiv 2403.16848, Apache-2.0 **[V]**): association as in-context ID prediction; 67.5 HOTA / 72.2 IDF1 on DanceTrack vs ByteTrack 47.7 HOTA **[V]**. Needs training on in-domain tracks; not a day-1 option.
- **MASA** (CVPR 2024, arXiv 2406.04221, Apache-2.0 **[V]**): learns instance-association embeddings from unlabelled images via SAM regions; a "universal adapter" that lets any detector track anything zero-shot **[V]**. Very relevant: class-agnostic association features for objects that are not people.
- **SAMURAI** (arXiv 2411.11922, Apache-2.0 **[V]**): SAM 2 + motion-aware memory for zero-shot single-object tracking.
- **SAM2MOT** (arXiv 2504.04519, AAAI 2026): tracking-by-segmentation with SAM 2, trajectory manager and cross-object interaction; +2.1 HOTA / +4.5 IDF1 on DanceTrack **[V]**.
- **SAM 3 / 3.1** video: detector + SAM 2-style tracker sharing a backbone; Object Multiplex for many objects **[V]**. Technically the most complete "detect-and-track by text" stack, but see the license note.

**[I] Recommendation for the POC.** Detector -> BoT-SORT (for camera-motion compensation) per camera stream, with an appearance embedding (DINOv2/SigLIP 2 or MASA features) for re-acquisition after occlusion. Trigger SAM 2.1 mask propagation only for "interesting" tracks (object in a crew member's hand, object near a stowage location) to get a clean snapshot mask and a stable trajectory during manipulation.

### 2.3 Crew identification and privacy

- The crew is a **closed, enrolled set of ~7 people** **[I]**, which turns open-world person re-ID into closed-set classification with a reject option. That is much easier and lets you avoid storing biometric templates for anyone not enrolled.
- Appearance-based person re-ID (Torchreid, MIT **[V]**; reid-strong-baseline, MIT **[V]**) is trained on clothing appearance, which is stable within a day but **changes across days** - cloth-changing re-ID is an open research area (e.g., COCAS arXiv 2005.07862) **[V]**. Plan for daily re-enrollment or use multiple cues.
- Face recognition: InsightFace code is MIT, but **its pretrained models and training data are non-commercial research only** **[V]**. If face ID is used at all, it needs a properly licensed model and explicit crew consent; face blurring hurts re-ID only slightly on standard benchmarks (Dietlmeier et al., arXiv 2010.06307) **[V]**.
- **[I] Privacy-preserving design:** (1) enroll crew with consent using a small gallery of body crops per day (clothing + name tag/patch region), (2) store only embeddings and a role-level ID (e.g., `CREW_03`) in the inventory DB, with the name mapping in a separate access-controlled table, (3) blur faces in stored snapshots by default, (4) keep person-tracking retention short (e.g., only "who moved item X" events persist), (5) never run face ID on audio/video of non-enrolled people (ground calls on screens). Audio diarization (Qwen3-Omni or a separate speaker model) can corroborate identity ("I'm putting the drill in the CTB") but should also be enrollment-based.

### 2.4 Moving/handheld cameras and microgravity [I unless noted]

- ISS imagery comes from fixed module cameras, crew-handled cameras, and free-flyers: JAXA's Int-Ball2 camera robot in Kibo (visual SLAM + IMU, ground-controlled shooting) **[V]** and NASA's Astrobee (Astrobee ISS dataset: monocular + IMU, 6-DoF ground truth poses, four modules, 3D CAD models; Kang et al., RA-L 2024) **[V]**.
- Camera motion breaks Kalman-filter motion models - use BoT-SORT's CMC or a homography from sparse features. Floating objects drift slowly and rotate, so IoU-based association remains OK between adjacent frames, but appearance changes with rotation: use multi-view exemplar galleries (Section 4).
- "No up": avoid gravity-based heuristics (e.g., "object on table"). Location must be expressed relative to module/rack/stowage features, not floor/ceiling.

---

## 3. VLM/LLM video understanding and streaming

### 3.1 Streaming video LLMs (verified summaries)

- **VideoLLM-online** (CVPR 2024, arXiv 2406.11816, Apache-2.0 **[V]**): LIVE framework; learns when to speak during a stream.
- **Flash-VStream** (ICCV 2025, arXiv 2506.23825; earlier 2406.08085, Apache-2.0 **[V]**): memory-based real-time long-video understanding.
- **Dispider** (CVPR 2025, arXiv 2501.03218, Apache-2.0 **[V]**): disentangles perception, decision (when to respond) and reaction.
- **VideoChat-Online / OVBench** (CVPR 2025, arXiv 2501.00584): Pyramid Memory Bank; OVBench covers past/present/future tasks **[V]**.
- **StreamingVLM** (MIT Han Lab / NVIDIA, arXiv 2510.09608, ICLR 2026, MIT license **[V]**): KV cache with attention sinks + short vision window (~16 s) + longer text window, contiguous RoPE; up to 8 FPS on one H100 over 2+ hour streams **[V]**.
- **SimpleStream** (arXiv 2604.02317, Apr 2026): a plain sliding window of the last 4 frames on an off-the-shelf VLM (Qwen3-VL-8B) reaches 67.7% on OVO-Bench and 80.59% on StreamingBench, beating memory-heavy streaming models, and argues extra memory can hurt real-time perception **[V, authors' claims]**. OASIS (CVPR 2026) shows hierarchical event memory helps mostly on hour-long horizons **[V, secondary]**.

**[I] Implication for this project.** The *memory* for an inventory system should be the **structured database**, not a VLM KV cache. A VLM needs only a short window (a few seconds of frames around an event) plus retrieved context from Postgres ("last known location of entity 42 was Node 1 port-forward bag"). This matches the SimpleStream finding and keeps behaviour auditable.

### 3.2 Serving: vLLM-Omni

vLLM-Omni (Apache-2.0 **[V]**) exposes a WebSocket `/v1/video/chat/stream` endpoint for Qwen3-Omni: client sends `session.config`, then base64 JPEG/PNG frames and optional audio chunks, then `query` messages that are answered over the buffered session **[V]**. Documented caveats: audio-buffer overflow clears buffered audio; back-to-back short replies can hit a scheduler race (workaround: >= 200 ms idle between turns) **[V]**. Qwen3-Omni-30B-A3B HF card lists Apache-2.0 **[V]**. **[I]** Use it for event adjudication with audio (crew narration is a strong "who moved it" signal), and run Qwen3-VL / Gemma 4 on plain vLLM for crop verification.

### 3.3 Event detection: "person moved item"

- No off-the-shelf open model directly outputs "agent X moved object Y" **[V - negative search]**. Building blocks: the 100DOH hand-object detector (hand location, side, contact state, contacted-object box; CVPR 2020, arXiv 2006.06669; MIT license **[V]**), Ego4D's object-state-change tasks, EgoHOS contact segmentation.
- **[I] Rule-first event model** (cheap, explainable), with VLM confirmation:
  1. `PICKUP`: an entity's track overlaps a crew member's hand region (hand-object contact = true) and then the entity's position departs its rest zone.
  2. `CARRY`: entity track co-moves with a person track (velocity correlation over k frames).
  3. `PLACE`: contact ends and entity becomes stationary (or disappears into a known container/bag zone).
  4. `DISAPPEAR` / `REAPPEAR`: track ends inside a container region or leaves the frame; later re-acquired by the entity matcher (Section 4).
  5. On each candidate event, send a 3-6 s clip (+ audio) and the candidate entity/crew IDs to the VLM with a constrained JSON schema (`{event, entity_id, crew_id, confidence, evidence}`); accept only if the VLM confirms and the rule score passes.

---

## 4. Entity resolution / deduplication for a persistent inventory (core section)

### 4.1 Where duplicates come from [I]

| Source | Example | Fix |
|---|---|---|
| Track fragmentation (same camera) | drill occluded by arm, comes back with new track ID | Short-gap re-association (appearance + motion gate), tracklet stitching |
| Cross-camera / cross-session sightings | drill seen in Node 1 cam, later in Destiny cam | Entity matcher against catalog galleries with spatio-temporal plausibility |
| Viewpoint/rotation change | floating object seen from a different side | Multi-view exemplar gallery per entity (k diverse crops) |
| Label drift from VLM/open-vocab detector | "laptop" / "computer" / "HP ZBook" | Map every label to a **catalog class ID** via text-embedding nearest neighbour + synonyms; never key entities on free text |
| Identical instances | 4 identical CTBs, 10 identical drink pouches | Do *not* force instance identity - model as count-per-location or use distinguishing marks (labels/barcodes via OCR) |
| Detector false positives | reflections, screen content (ground video on a laptop) | min-detections-per-track, mask out screen regions, VLM verify |

### 4.2 What the literature does

- **ConceptGraphs** (arXiv 2309.16650; MIT **[V]**): incremental 3D object map; each detection is matched to existing objects by a combination of geometric similarity (3D overlap) and semantic similarity (CLIP feature); default config `semantic_threshold: 0.5`, `physical_threshold: 0.5`, objects need `obj_min_detections: 3`, and a periodic post-merge pass merges objects with overlap >= 0.7 **and** visual similarity >= 0.7 **and** caption text similarity >= 0.7 (`base_mapping.yaml`) **[V]**. This "separate thresholds on geometry and semantics + periodic merge" pattern transfers directly.
- **HOV-SG** (arXiv 2403.17846, RSS 2024, MIT **[V]**): hierarchical floor/room/object scene graph - the room layer is analogous to ISS modules.
- **FARM - Find Anything using Relational Spatial Memory** (arXiv 2606.15476, Berkeley/Stanford, 2026): object-level memory at 5-10 Hz; each entity stores a 3D Gaussian, appearance feature, up to k viewpoint-diverse crops, caption, and retrieval embeddings (SigLIP2 image, Qwen3 text, Qwen3-VL); association = appearance-feature prefilter then Hellinger distance between Gaussians; **union-find** resolves detections that bridge two entities (that's how duplicates get merged); merge embedding DINOv3 by default (YOLOE features weaker in ablation); detector YOLOE; captioning offloaded asynchronously to vLLM workers **[V]**. Thresholds not published in the text I read. This is the closest published analogue to this POC's architecture.
- **Khronos** (arXiv 2402.13817, RSS 2024, BSD-3-Clause **[V]**): separates short-term dynamics from long-term changes; reports ~90% object detection F1 and ~94% change-detection F1 on real robot data **[V]** - the "object appeared / disappeared since last visit" logic is directly relevant for stowage areas.
- **DovSG** (arXiv 2410.11989, RA-L 2025): local scene-graph updates on change; notes the failure mode of one object split into multiple nodes **[V]** - i.e., duplicates are a known weakness of open-vocabulary scene graphs.
- **ReMEmbR** (NVIDIA, arXiv 2409.13682): VLM captions + position + time into a vector DB (Milvus) and an LLM agent queries it **[V]**. Code license is **NVIDIA License, non-commercial / research or evaluation only** **[V]**. A later paper criticises its "redundant caption-only memory under revisits" **[V, secondary]** - i.e., caption-level memory duplicates; use it as a design reference only.
- **OCC4M** (arXiv 2609.28798, 2026): object-centric 4D memory with persistent tracks and containment relations queried by a VLM **[V, abstract]** - containment ("drill is inside CTB-3") is exactly the ISS stowage problem.

### 4.3 Embeddings for appearance matching

- **DINOv2** (arXiv 2304.07193; Apache-2.0 **[V]**): best-known instance-level retrieval features; recommended default.
- **DINOv3** (arXiv 2508.10104): stronger, but **custom "DINOv3 License"**, gated on HF **[V]** - not open source by the strict rule.
- **SigLIP 2** (arXiv 2502.14786; Apache-2.0 **[V]**): image-text aligned; use for text queries ("find the red bag") and for label->catalog mapping, but category-level more than instance-level **[I]**.
- **MASA** features: trained explicitly for instance association **[V]**.
- **[I]** Store two vectors per crop: `dino` (instance identity) and `siglip` (semantic/text retrieval). Use `halfvec` in pgvector to halve storage.

### 4.4 Recommended dedup algorithm (closed-world catalog) [I]

**Data model (Postgres).**

```
catalog_item(item_id, class_id, display_name, synonyms[], is_fungible bool,
             expected_count int, home_zone_id, barcode/label_text)
entity(entity_id, item_id NULL, class_id, status {confirmed, provisional, merged_into},
       last_zone_id, last_seen_at, last_track_id, confidence, snapshot_uri)
entity_exemplar(entity_id, emb_dino halfvec(768), emb_siglip halfvec(1152),
                view_cluster int, quality real, frame_ref, created_at)   -- HNSW indexes
tracklet(track_id, camera_id, zone_id, t_start, t_end, class_hist jsonb,
         mean_emb halfvec, best_crop_uri, n_dets, assigned_entity_id NULL, assign_cost real)
sighting(entity_id, track_id, zone_id, t, bbox, conf)
event(event_id, type, entity_id, crew_id, zone_from, zone_to, t, confidence, clip_uri, verified_by)
merge_log(op {merge,split,relabel}, src_ids[], dst_id, actor {auto,human}, reason, t)
```

**Stage 0 - normalize labels.** Map detector/VLM free-text labels to `class_id` by nearest neighbour in a SigLIP/Qwen3 text-embedding table of catalog names + synonyms; below a similarity floor -> `class_id = UNKNOWN` (novelty queue). Never create entities keyed on free text.

**Stage 1 - within-camera tracklets.** BoT-SORT/ByteTrack with CMC. A tracklet is only eligible for the inventory when it has >= N detections (ConceptGraphs uses 3; suggest 5 at 5-10 FPS) and a mean detector confidence above a floor. Short-gap stitching: if a tracklet ends and another of a compatible class begins in the same camera within Δt (e.g., 3 s) with cosine(DINO) >= τ_stitch and spatial gap consistent with max drift speed, concatenate them before Stage 2. This kills the most common duplicate source cheaply.

**Stage 2 - tracklet-to-entity assignment (batched every few seconds per camera, and at tracklet close).** For the set of open/closed tracklets T and candidate entities E (retrieved by pgvector top-k on `emb_dino`, filtered by `class_id`, plus all entities last seen in the same/adjacent zones), compute

```
cost(t,e) = w_a * (1 - max_cos(t.mean_emb, exemplars(e)))      -- appearance (best view)
          + w_c * class_incompat(t.class_hist, e.class_id)      -- 0 if same class, large if incompatible
          + w_s * spatial_cost(t.zone, e.last_zone, Δt)         -- 0 same zone; grows with module hops / Δt too short
          + w_x * exclusivity_penalty(e)                        -- e is concurrently visible elsewhere -> +inf
          + w_l * label_mismatch(OCR text, e.label_text)        -- strong evidence when available
```

Add one dummy "new entity" column per tracklet with cost `θ_new`. Solve with Hungarian (`scipy.optimize.linear_sum_assignment`), which enforces one-to-one in a time window (two tracklets visible *simultaneously* cannot be the same entity - the "cannot-link" constraint; tracklets that are temporally disjoint may both map to the same entity across successive solves). Hard gates (set cost = +inf): incompatible class; physically impossible transit (entity seen in Kibo 2 s ago, tracklet in Zvezda); concurrent visibility.

**Stage 3 - decide with calibrated confidence.** Convert the margin between best and second-best cost (and vs `θ_new`) into a probability with a logistic/isotonic calibrator fitted on the labelled set. Three bands:
- `p >= 0.9`: auto-assign; append exemplar if it adds a new view (cos to existing exemplars < 0.85) - keeps galleries multi-view without bloat (cap k ~ 10-20 per entity, prune by quality).
- `0.5 <= p < 0.9`: assign provisionally, write to a **human review queue** with side-by-side snapshots ("same item?"); human merge/split writes `merge_log` and becomes training data for the calibrator.
- `p < 0.5` and no candidate: create `provisional` entity. Provisional entities that are never re-seen or never confirmed are garbage-collected; provisional entities with `item_id` resolvable to a catalog item where the catalog's `expected_count` is already met are flagged as **likely duplicates** (closed-world prior: if the catalog says there is one Fluke multimeter, two live entities is a contradiction to resolve, not a new item).

**Stage 4 - periodic consolidation (every N minutes / nightly).** Agglomerative pass over entities of the same class: merge pairs where (max exemplar cosine >= τ_merge) AND (never concurrently visible) AND (spatio-temporal history is consistent), using union-find (as in FARM) so chains of evidence merge transitively; record every merge so it is reversible (split = undo merge). Thresholds τ are set from the labelled set to target a chosen pairwise precision (e.g., 0.98), because a false merge (two items become one, one "disappears") is worse for an inventory than a duplicate that a human can merge.

**Fungible items (food pouches, wipes, identical CTBs) [I].** Do not attempt instance identity. Represent as `(item_id, zone_id, estimated_count, last_seen_at)` and track counts with a per-zone counter (detections deduplicated within a frame, max over a window). Upgrade to instances only when a distinguishing barcode/label is legible (OCR via Florence-2 or the VLM). ISS already relies on barcodes and RFID (IMS; REALM RFID with ~1.5 m mean location error in instrumented modules) **[V]** - joining vision sightings to IMS barcodes/RFID would be the strongest dedup signal if any of that data is available.

**Why this design [I].** It mirrors the classic Fellegi-Sunter / MOT assignment literature: hard gates + soft cost + a "new" option + human review in the uncertain band. It exploits the two strongest closed-world priors (catalog class and expected count) and the physical "one object, one place at a time" constraint, which purely embedding-based dedup ignores.

### 4.5 pgvector specifics

- pgvector (PostgreSQL license **[I - repo shows NOASSERTION; upstream is the PostgreSQL License]**) supports HNSW and IVFFlat; v0.8.0 (2024-10-30) added **iterative index scans** (`hnsw.iterative_scan = strict_order|relaxed_order`, `hnsw.max_scan_tuples`) to fix over-filtering when combining ANN with `WHERE class_id = ...` **[V]**. `halfvec` (float16) is supported (added in 0.7.0) **[I - verify in CHANGELOG]**.
- **[I]** Collection is small (hundreds-thousands of entities x <= 20 exemplars) - an exact scan per `class_id` partition is fine; HNSW matters only for the raw sighting archive.

---

## 5. Location without GPS

- **Topology (verified public knowledge, [I] on usage):** USOS pressurized modules: Node 1 Unity, Node 2 Harmony, Node 3 Tranquility (+ Cupola), Destiny (US Lab), Columbus (ESA), Kibo JEM-PM and JEM-ELM-PS, PMM Leonardo, Airlock Quest, BEAM; Russian segment: Zvezda, Zarya, Poisk, Rassvet, Nauka, Prichal. Within modules, rack locations follow a standard naming of deck/overhead/port/starboard x position (e.g., `LAB1S2`) **[I - verify against NASA ops nomenclature]**.
- **Location hierarchy for the DB [I]:** `module -> face (forward/aft/port/starboard/deck/overhead) -> rack/bay -> container (bag/CTB/drawer)`. Zones are polygons in each fixed camera's image plane (supervision's `PolygonZone` makes this trivial), mapped to the hierarchy in a `camera_zone` table. Containment ("in CTB-12") is an entity-to-entity relation (OCC4M/ConceptGraphs-style) rather than a coordinate.
- **Fixed cameras:** `camera_id -> module` is configuration; zone polygons per camera are a one-time annotation. Highest reliability, recommended for the POC.
- **Handheld / free-flyer cameras:** need visual place recognition (VPR) to map a frame to a module/zone. Open options: **MegaLoc** (arXiv 2502.17237, MIT **[V]**) - single retrieval model, SOTA on many VPR benchmarks **[V]**; **AnyLoc** (arXiv 2308.00688, BSD-3 **[V]**) - DINOv2 features + VLAD, designed for unstructured/indoor domains; **gmberton/VPR-methods-evaluation** (MIT) wraps 10+ methods **[V]**; SALAD is GPL-3.0 **[V]**. Build a reference database of geotagged frames per zone (from fixed-camera footage, Astrobee dataset imagery, and a walkthrough), retrieve top-k, vote over a short window, and optionally ask the VLM to confirm the module from visible signage (module hatches have labels). The Astrobee semantic-masking paper notes that ISS clutter (bags, cables, laptops) moves and corrupts map matching, so masking movable objects before VPR helps **[V]**.
- **Astrobee ISS dataset** (astrobee-iss-dataset.github.io): calibrated imagery, 6-DoF GT, CAD models over four modules **[V]** - the best public substrate for building and evaluating zone classification.

---

## 6. Evaluation

### 6.1 Metrics

- **Detection:** COCO-style mAP@[.5:.95] and per-class AP on the catalog classes; report recall at a fixed precision since misses are worse than false alarms for an inventory (false alarms get filtered downstream) **[I]**.
- **MOT:** **TrackEval** (github.com/JonathonLuiten/TrackEval, MIT **[V]**) computes HOTA (arXiv 2009.07736), CLEAR MOTA, IDF1 (Ristani et al., arXiv 1609.01775), and identity switches. Report **HOTA (DetA/AssA split)** and **IDF1**; IDF1/AssA track identity consistency, which is what drives duplicates. MOTA is dominated by detection errors and is less informative here.
- **Entity resolution (most important for "reduce duplicates"):** treat each ground-truth physical object as a cluster and each predicted entity as a cluster over the set of sightings/tracklets:
  - **Pairwise precision/recall/F1** over pairs of tracklets (same-entity predicted vs gold).
  - **B-cubed precision/recall** (Bagga & Baldwin 1998; Amigó et al., "A comparison of extrinsic clustering evaluation metrics", Information Retrieval 2009) - per-item averaging, less dominated by big clusters.
  - **Operational metrics [I]:** *duplicate rate* = (#predicted entities - #gold objects matched) / #gold objects; *false-merge rate* = fraction of gold objects sharing an entity with another object; *inventory accuracy* = fraction of gold objects whose `last_zone` is correct at audit time T; *human-review load* = items in queue per hour.
- **Events ("who moved what"):** event-level precision/recall/F1 where a prediction matches a gold event if type, entity, and crew agree and |Δt| <= tolerance (e.g., 2 s); additionally report attribution accuracy (correct crew_id | event detected) and temporal IoU for durations.
- **Location:** zone-level top-1 accuracy (module and sub-zone), confusion matrix across adjacent modules.
- **Descriptions / free text:** LLM-as-judge with decomposed QA rather than holistic scoring - VDCscore (AuroraCap, arXiv 2410.03051) decomposes gold captions into QA pairs **[V]**; ARGUS (ICCV 2025) reports hallucination and omission rates separately **[V]**; CAPability (NeurIPS 2025, arXiv 2502.14914) scores correctness and thoroughness **[V]**. **[I]** Use an open judge model different from the generator, fix the rubric, and spot-check judge agreement with humans (Cohen's kappa) on ~50 items.

### 6.2 Building test sets [I]

1. **Public ISS footage** (NASA video archives, ISS live-stream recordings): select 20-40 clips (1-5 min) covering fixed cameras, handheld, and free-flyer views; annotate boxes + track IDs + global object IDs + events in CVAT (MIT) or Label Studio (Apache-2.0). Global IDs across clips are what make the entity-resolution metrics possible.
2. **Astrobee ISS dataset** for location/VPR ground truth (6-DoF poses -> module labels).
3. **Synthetic "mock ISS"**: a lab mock-up (racks, Velcro, bags, floating via fishing line or handheld motion) or a rendered scene from the ISS CAD/3D models (NASA publishes ISS 3D models) with domain randomization; inject scripted events (crew moves item A from zone 1 to zone 3) so event ground truth is free.
4. **Dedup stress suites:** scripted sequences specifically designed to cause duplicates - occlude-and-reveal, carry an item between two cameras, rotate a floating item 180 degrees, place 3 identical items, show a laptop displaying a video of another object.
5. Keep a frozen **dev/test split** and version it; log every pipeline config with its metrics.

---

## 7. Recommended pipeline for the POC

All components below are Apache-2.0 / MIT / BSD unless flagged.

1. **Ingest:** per-camera RTSP/HLS -> frame sampler at 5-10 FPS for detection/tracking, 1-4 FPS ring buffer for VLM clips; audio chunks to the same ring buffer. Each frame tagged `(camera_id, t)`.
2. **Fast path (per frame, GPU):**
   - **Detector:** LLMDet or OWLv2 (Apache-2.0) prompted with catalog names + per-item image exemplars; replace with a fine-tuned **RF-DETR / D-FINE** (Apache-2.0) closed-set model once ~200+ labelled crops/class exist. Person + hand boxes from the same model (or the 100DOH hand-object detector, MIT, for contact state).
   - **Tracker:** **BoT-SORT** (MIT) for camera-motion compensation (ByteTrack via supervision is fine for fixed cameras). Appearance features from **DINOv2** (Apache-2.0) crops (or MASA embeddings).
   - **Masks on demand:** **SAM 2.1** (Apache-2.0) for snapshot masks and for tracks in contact with hands. Evaluate **SAM 3.1** only if legal clears the SAM License (non-OSI, ITAR clause).
3. **Location:** fixed-camera zone polygons -> module/face/rack hierarchy; for moving cameras, **MegaLoc** (MIT) or **AnyLoc** (BSD-3) retrieval against a zone-labelled reference DB, smoothed over a time window, optionally VLM-confirmed from signage.
4. **Entity resolution service** (Python, Postgres + **pgvector**): label normalization -> tracklet stitching -> Hungarian tracklet-to-entity assignment with hard gates and a "new" column -> calibrated three-band decision -> nightly union-find consolidation; closed-world `expected_count` contradictions flagged; fungible items tracked as counts. Every merge/split logged and reversible.
5. **Slow path (VLM, async workers on vLLM):**
   - **Qwen3-VL-8B** (Apache-2.0) or **Gemma 4 12B / 26B-A4B** (Apache-2.0): verify/name crops at tracklet birth and for low-confidence assignments; read labels/barcodes; produce short descriptions.
   - **Qwen3-Omni-30B-A3B on vLLM-Omni** (Apache-2.0, streaming WebSocket API): adjudicate candidate events with a 3-6 s clip + audio and return constrained JSON (`event, entity_id, crew_id, confidence, evidence`).
   - **Molmo 2** (Apache-2.0) as an alternative for point-based verification across frames.
   - Cosmos-Reason2 is technically strong for physical/temporal reasoning, but weights are under the NVIDIA Open Model License - include only if that license is acceptable.
6. **Crew identity:** consent-based daily enrollment gallery, body-appearance embeddings (Torchreid, MIT), closed-set with reject; IDs pseudonymized; faces blurred in stored snapshots; no InsightFace pretrained weights (non-commercial).
7. **Human-in-the-loop dashboard:** review queue (merge/split/relabel with side-by-side snapshots), inventory view by module, event timeline with clips. Human decisions feed the calibrator and the fine-tuning set.
8. **Evaluation harness:** TrackEval for HOTA/IDF1; custom scripts for pairwise/B-cubed, duplicate rate, false-merge rate, event F1 with tolerance, zone accuracy; LLM-as-judge (decomposed QA) for descriptions. Run on every config change.

**Why this shape [I].** It keeps the VLM off the per-frame critical path (cost and latency), makes the database the system's memory (auditable, queryable, and consistent with evidence that simple short windows beat memory-heavy streaming VLMs), puts deduplication into an explicit, testable, reversible algorithm with closed-world constraints, and stays within licenses a NASA POC can actually ship.

---

## 8. Open questions

1. **Licensing policy definition.** Does "open source only" mean OSI-approved licenses for code *and* weights? That decides SAM 3/3.1 (SAM License with an ITAR exclusion), DINOv3 (custom), Cosmos-Reason2 (NVIDIA Open Model License), and the AGPL/GPL detectors (YOLOE, YOLO-World, BoxMOT). Legal should rule on the SAM License ITAR clause specifically, given ISS hardware context.
2. **Data access.** Which feeds are available (fixed module cameras vs crew camcorders vs Int-Ball2/Astrobee), at what resolution/latency, and is audio included? Can we join to IMS barcode or REALM RFID data? That join would be the strongest dedup and location signal.
3. **Catalog source.** Is there an authoritative item list (IMS export) with photos, part numbers, and expected counts per module? The dedup algorithm's best prior depends on it.
4. **Crew privacy/consent.** Is crew identification permitted at all? If so, by what modality (body appearance, name patches, voice), retention, and who can see the name mapping?
5. **Fungible vs unique.** Which catalog items need instance-level tracking vs per-zone counts?
6. **Ground-truth effort.** Who annotates global object IDs and events for the test set, and how many hours of footage are realistic?
7. **Compute target.** Ground-side GPUs only, or eventually on-board (edge) inference? That changes the detector size and whether VLM verification is feasible in real time.
8. **Unverified numbers to re-check before relying on them:** SAM 3's 30 ms/H200 claim (secondary sources), Gemma 4 grounding format/accuracy, original DeepSORT/StrongSORT repo licenses, pgvector `halfvec` version, ISS rack naming convention.

---

## References (primary sources consulted)

Detection/segmentation: Grounding DINO arXiv 2303.05499 (github.com/IDEA-Research/GroundingDINO); Grounding DINO 1.5 arXiv 2405.10300 (github.com/IDEA-Research/Grounding-DINO-1.5-API); MM-Grounding-DINO arXiv 2401.02361; LLMDet arXiv 2501.18954 (github.com/iSEE-Laboratory/LLMDet); OWL-ViT arXiv 2205.06230; OWLv2 arXiv 2306.09683; YOLO-World arXiv 2401.17270 (github.com/AILab-CVC/YOLO-World); YOLOE arXiv 2503.07465 (github.com/THU-MIG/yoloe); Florence-2 arXiv 2311.06242; SAM 2 arXiv 2408.00714 (github.com/facebookresearch/sam2); SAM 3 arXiv 2511.16719 (github.com/facebookresearch/sam3, LICENSE, RELEASE_SAM3p1.md); Molmo arXiv 2409.17146; Molmo 2 arXiv 2601.10611 (github.com/allenai/molmo2); Rex-Omni (github.com/IDEA-Research/Rex-Omni); RF-DETR (github.com/roboflow/rf-detr); D-FINE (github.com/Peterande/D-FINE).

VLMs/streaming: Qwen3-VL arXiv 2511.21631 (github.com/QwenLM/Qwen3-VL); Qwen3-Omni arXiv 2509.17765; vLLM-Omni streaming video API (docs.vllm.ai/projects/vllm-omni/en/latest/serving/video_stream_api/); Cosmos-Reason2 (huggingface.co/nvidia/Cosmos-Reason2-8B); Gemma 4 (huggingface.co/google/gemma-4-31B-it; ai.google.dev/gemma/docs/capabilities/vision); VideoLLM-online arXiv 2406.11816; Flash-VStream arXiv 2506.23825; Dispider arXiv 2501.03218; VideoChat-Online arXiv 2501.00584; StreamingVLM arXiv 2510.09608; SimpleStream arXiv 2604.02317.

Tracking/ReID: ByteTrack arXiv 2110.06864; OC-SORT arXiv 2203.14360; BoT-SORT arXiv 2206.14651; DeepSORT arXiv 1703.07402; StrongSORT arXiv 2202.13514; MOTIP arXiv 2403.16848; MASA arXiv 2406.04221; SAMURAI arXiv 2411.11922; SAM2MOT arXiv 2504.04519; Torchreid (github.com/KaiyangZhou/deep-person-reid); COCAS arXiv 2005.07862; faces in ReID arXiv 2010.06307; InsightFace license (github.com/deepinsight/insightface); 100DOH arXiv 2006.06669 (github.com/ddshan/hand_object_detector).

Memory/scene graphs: ConceptGraphs arXiv 2309.16650 (github.com/concept-graphs/concept-graphs, hydra_configs/base_mapping.yaml); HOV-SG arXiv 2403.17846; Khronos arXiv 2402.13817; DovSG arXiv 2410.11989; ReMEmbR arXiv 2409.13682 (github.com/NVIDIA-AI-IOT/remembr, LICENSE.md); FARM arXiv 2606.15476; OCC4M arXiv 2609.28798; DINOv2 arXiv 2304.07193; DINOv3 arXiv 2508.10104; SigLIP 2 arXiv 2502.14786; pgvector 0.8.0 release (postgresql.org/about/news/pgvector-080-released-2952).

Location/ISS: MegaLoc arXiv 2502.17237 (github.com/gmberton/MegaLoc); AnyLoc arXiv 2308.00688; Astrobee semantic masking arXiv 2411.01804; Astrobee ISS dataset (Kang et al., RA-L 2024, astrobee-iss-dataset.github.io); Int-Ball2 (IEEE RAM, Sept 2025); ISS RFID/REALM (ntrs.nasa.gov/citations/20070020337 and related NTRS papers).

Evaluation: HOTA arXiv 2009.07736; IDF1 arXiv 1609.01775; TrackEval (github.com/JonathonLuiten/TrackEval); Amigó et al. 2009 (B-cubed); AuroraCap/VDCscore arXiv 2410.03051; CAPability arXiv 2502.14914; ARGUS (ICCV 2025).
