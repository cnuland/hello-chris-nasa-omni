# Open questions & decisions log (for review)

You said to decide without asking while you're away. Each entry records the question, the decision I took, and what
I need from you.

## Infrastructure

1. **Red Hat pull secret.** `~/pull-secret.txt` was revoked (registry.redhat.io returns 401).
   - **Decision:** the ARO cluster `aro-cluster-zlknq` (eastus, 3x D8s_v3 workers) was created WITHOUT a pull secret.
     The OpenShift AI operator ships from the Red Hat catalog, so without one we install **Open Data Hub**, the
     open-source upstream of OpenShift AI (same components: KServe, model registry, pipelines, TrustyAI). This fits
     your "open source only" rule.
   - **Need:** a fresh pull secret from console.redhat.com if you specifically want the Red Hat-branded OpenShift AI.
     ARO accepts a pull secret after install, so this can be swapped in later.
2. **GPU family.** The quota allows T4 (NCasT4_v3, 512 vCPU) and A10 (NVadsA10v5, 100 vCPU).
   - **Decision:** try A10 first (24 GB per GPU, better for omni models). Fall back to T4 if a policy denies it, as the
     last environment's did.
3. **Old ARO environment (jxdfq) is gone.** Its DiffusionGemma deployment and models are lost. DiffusionGemma will be
   re-deployed here with the fixes recorded in `hello-chris-dd-kuberay/k8s/aro/dgemma-t4.yaml`.

## Licensing (NASA-relevant)

4. **SAM 3 / 3.1** uses a custom Meta license that explicitly excludes ITAR-related use, so it is **not used**.
   - **Decision:** SAM 2.1 (Apache-2.0) only, if masks are needed at all.
5. **Excluded licenses** (AGPL/GPL: YOLOE, BoxMOT, YOLO-World; non-commercial: ReMEmbR, InsightFace faces; custom:
   DINOv3).
   - **Decision:** all excluded under "open source only".
6. **Model licenses that are open-weight rather than OSI open source:** Gemma 4 (Gemma terms) and NVIDIA Cosmos
   (NVIDIA Open Model License).
   - **Decision:** default to Apache-2.0 models (Qwen family) wherever they are equal or better. Gemma / Cosmos are
     kept as optional swap-ins, because your diagram names them.
   - **Need:** confirm whether NASA counts these licenses as acceptable.

## Product

7. **Crew privacy.** Tracking "people" can mean crew identity (face, voice) or just person-tracks.
   - **Decision:** the POC uses **role/appearance descriptors** ("crew member in blue shirt") plus optional operator
     labelling. No biometric face database.
   - **Need:** confirm NASA's policy.
8. **"Red Hat OpenClaw Enterprise".** Being researched (report 05). If it can't be verified as an available open-source
   product, the agent harness is built on an open-source framework with the same role, documented as a swap point.
9. **OpenClaw** (research/05) is real (MIT, OpenClaw Foundation, with Red Hat and NVIDIA). "Red Hat OpenClaw
   Enterprise" could not be verified as a product, and OpenClaw "dreaming" cannot prioritise inventory.
   - **Decision:** our own agent harness serves the NASA chatbot, with inventory-first memory. An inventory **MCP
     server** exposes the same tools, so OpenClaw can be connected later via Red Hat's `claw-operator`.
   - **Need:** confirm what stakeholders mean by "Red Hat OpenClaw Enterprise".
10. **Audio** (research/03): a dedicated ASR (Qwen3-ASR-1.7B, Apache-2.0) is the transcript of record; omni handles
    ambiguity. Speech-only movement claims stay unconfirmed until video confirms them.
    - **Need:** confirm whether crew audio capture is acceptable outside comm loops (e.g. a push-to-talk "stow call"
      convention).
11. **Privacy zones:** crew quarters (NOD2S5/D5/P5/O5) and WHC (NOD3F4) are seeded as `no_capture`: no frames,
    crops or reviews are stored there.
12. **GPU:** ASR and the agent LLM share one A10 via GPU time-slicing; omni has its own A10; DiffusionGemma runs on
    4×T4.

## Deployment decisions (2026-10-17)

13. **Default catalog sources disabled.** With no pull secret, the Red Hat OperatorHub sources fail noisily.
    `install-odh.sh` disables them and installs the ODH 3.5 quay catalog. cert-manager comes from the upstream
    jetstack chart and CloudNativePG from its upstream manifest (v1.30.1).
    - **Need:** if you add a pull secret later, re-enable the default sources (`disableAllDefaultSources:false`).
14. **ODH components.** Enabled: KServe (RawDeployment, no service mesh), dashboard, TrustyAI, model registry.
    Disabled for now: pipelines, workbenches, Ray, Kueue, trainer (they would add Red Hat-catalog dependencies).
    The models run as plain vLLM Deployments so the nightly vLLM-Omni image is used as-is; wrapping them as KServe
    ServingRuntimes is a follow-up.
15. **A10 blocked, T4 used (supersedes #2 and #12).** The RHDP policy "Deny expensive GPU VM SKUs (allow T4)" rejected
    Standard_NV36ads_A10_v5 on this subscription too. The POC therefore runs on 2× Standard_NC64as_T4_v3 (4×T4 16 GB
    each):
    - **perception node:** Qwen2.5-Omni-7B fp16 TP2 on GPUs 0-1, Qwen3-8B-AWQ (agent/chatbot) on GPU 2,
      Qwen3-ASR-1.7B on GPU 3.
    - **decision node:** DiffusionGemma-26B INT8 TP4, with the sm75 patches from the robot project.
    - **Need:** A10/L4/H100 access for production-like latency. The T4 has no bf16, and Qwen2.5-Omni in fp16 is a
      known overflow risk. If it misbehaves, the fallback is Qwen2.5-Omni-3B on one T4.
16. **Perception fast path on its own T4.** OWLv2 on CPU measured about 14 s per frame (4 threads), roughly 20×
    slower than real time. A third GPU VM was added: 1× Standard_NC16as_T4_v3 (`gpu-pool=t4cv`), which policy allows.
    The perception image now uses CUDA torch (`TORCH_INDEX=cu128`), and `PERCEPTION_DEVICE` (cpu | cuda | auto) is new
    and tested.
    - **Need:** an OK for the extra VM cost. Without it, set `PERCEPTION_DEVICE=cpu` and expect replay well below real
      time.
17. **Qwen3 "thinking" disabled for the chatbot.** On a T4 (~42 tok/s), thinking turned a one-sentence answer into
    8 s and made multi-tool turns exceed the agent's 30 s timeout. The server default is now
    `enable_thinking=false`, and the agent timeout is 120 s.
    - **Need:** decide whether you want thinking back on faster GPUs.
18. **Catalog questions in the chatbot (AC-AGT-11, new).** Integration showed that the agent could not answer "where
    is X normally kept / how many should there be" because it had no catalog tool. A `lookup_catalog` grounding tool
    is being added under TDD, with catalog citations.
19. **Integration bugs found only on the cluster** (all fixed, each with a regression test):
    - scipy was missing from the `[model]` extra;
    - transformers 5.x moved `post_process_object_detection`;
    - the MCP sidecar needed the agent's environment variables;
    - the dashboard's container default pointed the agent proxy at the MCP port (8090) instead of chat (8080).

    **Lesson:** the `model`-marked tests (real weights) must run in CI inside the built image, not only fake-backed
    unit tests.
20. **Real-video dedup failure (found 2026-10-17, fix in progress under TDD).** The first 8 windows of the NASA ISS
    tour produced 56 entities (for example 10 "headsets"), and the synthetic eval missed it. There were three causes:
    - omni lists each object once per frame (6×);
    - crops were cut from the 448 px omni frame (blurry patches, DINOv2 same-object cosine ≈0.45);
    - the camera's fixed location (LAB1) overrode a correct "Columbus" hint.

    A real omni window is now a regression fixture. **Lesson:** the eval needs real-model outputs as well as synthetic
    ones.
21. **Known issue, chatbot.** The "usual stowage" safety rewrite (AC-AGT-11) fixed a mislabelled location, but it
    dropped the rest of the answer ("how many should we have": expected_count 1). It should append the catalog
    fields it can prove instead of replacing the whole reply. Low risk; not fixed yet.
22. **DiffusionGemma memory.** It hit OOM at 0.80 GPU utilisation under perception's bursty decision traffic, so it now
    runs at 0.72. While it was down, every same-or-new decision fell back to NEW, which is the safe default
    (duplicates are flagged for review instead of wrongly merged).
23. **DiffusionGemma `--no-constrained`.** The decision sidecar's default "constrained" label-only reads require
    tensor parallel 1. On our TP4 deployment a request raised `diffusion_constrained needs tensor parallel 1`, and that
    error killed the whole vLLM EngineCore, which looks like an upstream robustness bug worth reporting. It is now
    disabled, at about 25% more GPU time per decision. Final memory settings: 0.78 utilisation, max-model-len 4096,
    max-num-seqs 1.
24. **Fixed: event dedup within a window.** One window produced three identical `used` events for the same drill and
    actor at the same timestamp, with confidence 0.7/0.5/0.4. AC-PER-10 dedups across windows (10 s), but not
    repeats inside one omni reply. Queued for perception after the omni-budget work.
25. **Dashboard polish (not blocking):** recent events show the item ID (`item 550f3b9d`) rather than its label.
    Screenshots of the live dashboard are in docs/screenshots/.
26. **Throughput and GPU sizing (measured).** On T4, omni takes 80% of the wall time: Qwen2.5-Omni-7B fp16 TP2 decodes
    at about 28 tok/s (memory-bandwidth bound), so each window costs about 20 s, even after the output budget brought
    tokens down from about 1,000 to about 550. Real time at a 4 s stride needs about 5× that, i.e. an L4/A10/L40S/H100.
    The RHDP policy blocks those GPUs here.
    - **POC setting:** `PERCEPTION_STRIDE_S=12` (a 6 s window every 12 s), so the 15 min tour takes roughly 30 min.
    - **Options:** (a) a newer GPU, the real fix; (b) Qwen2.5-Omni-3B on 1×T4, about 2× faster with lower quality;
      (c) parallel windows across 2 omni replicas.
    - **Need:** the target hardware for the ISS / DGX Spark deployment, which decides between these.
27. **Follow-up: unknown-label variants.** On the full tour, "spacesuit" and "space suit" became separate unknown
    groups. Unknown grouping compares `normalise_label` output exactly. Proposed fix: a separate grouping key without
    spaces or hyphens, used only for UNKNOWN-vs-UNKNOWN comparisons, plus a fuzzy fallback (`fuzzy_similarity` ≥ 0.9).
    It is not done yet because `normalise_label` has 12 call sites, display text included, and it deserves a reviewed
    TDD round. NASA's review queue (merge_into) covers it in the meantime.
28. **Omni still exceeds the output budget.** Over the full tour, 77% of omni replies hit `OMNI_MAX_TOKENS=700`
    (mean 662 tokens), and 42 of 44 were salvaged. The model largely ignores the prompt caps. Options: constrained JSON
    decoding if vLLM-Omni adds it to chat; a fine-tuned or smaller omni; or `OMNI_MAX_OBJECTS` 8 for faster windows.
29. **Revisit dedup after a long gap: the most important next fix.** The full tour (101 windows, with the video
    looped) created 57 entities in windows 0–29, 37 in 30–79, a **spike of 22 right after the loop restart**
    (windows 80–89, back in Columbus about 15 min later), then 5 in 90–100.
    - **Likely cause:** omni-only sightings carry no embedding (quality gate, AC-PER-22). They attach only to a
      same-class entity seen within `PERCEPTION_NO_EXEMPLAR_MATCH_S=600` s, so a module revisited after more than
      10 min gets new entities. The expected-count rule did flag them (35 possible-duplicate reviews).
    - **Proposed fix (TDD):** for omni-only sightings, attach by (class, module or zone) with no time limit when
      exactly one live candidate exists; when several exist, ask DiffusionGemma with their descriptions. Add a
      "revisit" scenario to the eval (same scene replayed after a 15 min gap, gate: new entities on the revisit ≤ 5%).

## Answers from Chris (2026-10-17)

- **Target hardware:** a Dell GB10 (DGX Spark class) running on the ISS. This supersedes the GPU-sizing "need" in
  #2, #15, #16 and #26. Production is a single GB10 node, so every model shares one GPU and 128 GB of unified memory.
- **OpenClaw:** "Red Hat OpenClaw Enterprise" is an unreleased enterprise edition of OpenClaw, announced through the
  OpenClaw Foundation. That answers #8/#9. The plan is unchanged: our harness plus the inventory MCP server, so
  OpenClaw Enterprise can attach when it ships.
- **Pull secret:** none. Upstream Open Data Hub is fine for now (#1, #13).
- **Cloud policy:** out of scope. NASA will handle it; the POC targets what NASA asked for (#15, #16, #26).
- **Licences:** Gemma and NVIDIA Cosmos are accepted, since NASA already partners with Google and NVIDIA (#6). Both
  are now open as omni or perception options.
30. **Gemma 4 12B on T4: measured (2026-10-18).**
    - **Setup:** vLLM v0.31.0 with the sm75 patches, fp16, TP4, on its own 4×T4 node. It started cleanly in 10 min.
      One T4 in the first VM failed its GSP firmware boot ("GPU is likely in a bad state"), so that Machine was
      replaced.
    - **Results by input type:**

      | Input | fp16 result |
      |---|---|
      | Text | Correct in 3.5 s |
      | Images | Degenerate ("our our our…") on every NASA window, 0 of 5 valid. The encoder-free image path overflows fp16 |
      | Audio | Answered, but described a non-existent face, so the audio likely isn't being read correctly |

    - **Action:** retry with `--dtype float32`, about 48 GB across 4×T4, slower decode.
    - **Implication for GB10:** Blackwell has bf16 and FP8, so this overflow is a T4-only problem. T4 results for
      Gemma (speed, and whether fp32 is needed) should not be read as GB10 expectations.
    - **Qwen2.5-Omni on the same windows:** valid JSON 5 of 5, but often whole-frame boxes `[0,0,1,1]` with generic
      labels ("space station interior"). This confirms omni boxes alone are too coarse, so OWLv2 stays.
31. **Decision: Gemma 4 12B replaces Qwen2.5-Omni for perception (US-origin, better quality).**
    - **fp32 on T4:** fp16 overflowed in the image path; fp32 fixes it. Same 8 NASA windows:

      | | Qwen2.5-Omni | Gemma 4 (fp32) |
      |---|---|---|
      | Valid replies | 8/8 | 8/8 |
      | Whole-frame junk boxes | 35 of 73 objects | 0 of 50 |
      | Objects named | generic | specific ISS objects (hatch, EXPRESS rack, storage bags, monitor) |
      | People | generic | "astronaut in a blue shirt floats in Kibo" |
      | Latency | 22 s/window | 60 s/window on T4 |

    - **Frame rate:** a 6-frame window every 60 s of video, per Chris ("doesn't need to be real time").
    - **Audio:** vLLM dropped `input_audio` silently because librosa/soundfile were missing from the stock image. The
      manifest now installs them at startup. ASR (/v1/audio/transcriptions) was unaffected: it has a fallback decoder,
      verified with real speech.
    - **Box sanity:** some Gemma boxes are inverted (x2<x1 or y2<y1) and need sanitising in perception.
    - **Rest of the US-origin stack, planned:** chat Qwen3-8B → Gemma 4 12B QAT-W4A16 (text-only fp16 is fine on
      T4); ASR Qwen3-ASR → Whisper large-v3-turbo (MIT). Decision (DiffusionGemma), OWLv2 (Google) and DINOv2 (Meta)
      are already US-origin.
32. **Chatbot after round 2, live on Gemma 4 chat (2026-10-18; P3 tail fixed in AC-AGT-26).** All 5 standard questions in one conversation were
    answered correctly in 10–21 s; round 2 had 3 wrong, at up to 64 s. One P3 left: the "usual stowage" rewrite plus
    list renumbering leaves a garbled tail ("…is usually kept in the following location: 1.").
33. **Gemma run findings (in progress under TDD).**
    1. **Person tracks regressed to 0.** Gemma reports people only in `people[]`, with good non-biometric
       descriptions ("blue flight suit with American flag patch"). The reconciler only built person entities from
       "person" objects, which Qwen also listed.
    2. **Privacy gap.** Gemma described "dark hair, sleeping in a blue sleeping bag" in Harmony, the module with the
       crew quarters. The window was not flagged no_capture. The fix adds sleep cues to the fail-closed privacy rule.
       The footage is NASA's public tour video, so nothing private has leaked, but the policy must catch this.
       **Need:** confirm NASA wants sleep cues treated as crew quarters (it will also catch stowed sleeping bags).
34. **Verified live: sleep-cue privacy and person tracks (Gemma run, 2026-10-18).**
    - **Privacy:** window 4 (Harmony, `people[1].appearance` "…sleeping…") and window 5 (30 s privacy hold) were
      treated as no_capture. Checked in the database and object store:
      - sightings were recorded as location-only facts, with 0 frames, 0 reviews and no raw reply;
      - RustFS holds 0 of today's 313 objects inside 03:04:43–03:05:43. Media stops at 03:04:18 (window 3) and
        resumes at 03:05:43 (window 6).
    - **People:** 12 non-biometric person tracks, e.g. "black shirt, grey pants" in COL1 and "blue shorts, grey
      long-sleeve shirt" in CUP1. Some over-splitting is expected as the crew changes clothes and modules.
35. **Module resolution (AC-PER-39..42).** On the real-video eval, replaying the saved Gemma replies, predicted
    window_location_acc went from **0.391 to 0.696**, with 0 wrong modules. The changes: aliases for every seeded
    module, a scene-summary fallback, and a handheld-camera carry.
    - **Decision for Chris: carry reach.** A carry lasts ≤45 s from the last window that *named* its module, and
      carried windows don't extend it. Chaining window to window would have carried Node 3 into the next pass's
      Columbus and Kibo windows, a wrong module, which a test demonstrates. Say if you want chaining anyway.
    - **Remaining misses:** windows whose hint and summary name no module (Zarya ×2), signage-only hints ("To COL"),
      and privacy-hold windows, which never carry.
36. **Final-tour real-video score (live, 2026-10-18 04:39 UTC).** `make eval-real` against NASA's caption timeline:
    window_location_acc **0.750** (12 of 16 scored; it was 0.391 before module resolution), module_coverage 0.889,
    **0 wrong modules**. The 4 remaining misses are unresolved: Zarya ×2, Columbus ×1, Zvezda ×1. Inventory now
    spreads across real modules, not USOS. 2 of 31 windows were private, sleep cue plus hold.
    **Open:** person tracks still over-count (21 tracks); a cross-module continuity fix is under TDD.
37. **P1 face crop (UI4-01), handled 2026-10-18 about 05:00 UTC.**
    - **What happened:** a misplaced Gemma/OWLv2 "window" box cropped a crew member's face. The crop was stored,
      embedded and shown in the review queue.
    - **Purged:** the S3 object (URL now 404), the exemplar, the snapshot and the review image (replaced with a
      privacy placeholder), with an audit row.
    - **Perception paused** until the prevention fix is deployed: object crops overlapping any person region are
      dropped, along with a structure denylist.
    - **Need:** NASA's policy on whole frames that show faces. They are stored today, as the system's evidence of
      record; crops of faces are never allowed.
38. **Unknowns restored (AC-PER-53), live 2026-10-18 06:32 UTC.** With the new prompt, the first 8 windows produced 3
    real loose unknowns and no structure: "orange tool" (COL1), "orange sphere" (JPM1, plausibly a SPHERES/Astrobee
    robot in Kibo) and "white wrapped object" (JPM1). The review images are whole frames with no faces; the objects
    are visible and NASA's caption confirms Kibo.
    **Next (under TDD):** open-vocabulary OWLv2 queries with the unknown's own label, so unknowns get real detector
    boxes and crops. Every query box goes through the person-overlap guard.
39. **Chat privacy choice (AC-AGT-38), for Chris to confirm.**
    - "What is in the crew quarters?" gets a fixed answer: nothing is recorded, plus a note that no images or
      descriptions are kept there.
    - "Who was in the crew quarters?" is answered "I don't report who was in a private area."
    - Change this if NASA wants presence (but not content) reported for private zones.
40. **Second-pass location drop is sampling phase, not code (UI6-03).**
    - **Cause:** Gemma reads module names almost only from NASA's on-screen title captions, which are up about 16 s.
      With a 30 s stride on an 896.8 s loop, each pass samples 3.2 s later and misses captions.
    - **Per-pass accuracy:** 0.73 / 0.43 / 0.39 / 0.31.
    - A conservative DINOv2 "revisit location" (AC-PER-67) recovers a few windows with 0 wrong modules.
    - **For a real ISS feed:** there are no captions, so location must come from fixed camera placement, signage or
      speech.
    - **Need:** NASA's camera inventory (fixed vs handheld, positions). That is the real location source for
      production.
41. **Structure memory and resets.** `scripts/reset-observations.sh` truncates with CASCADE, which also clears
    `structure_memory` (it references dismissed entities). Fine for demo resets. In production, operators'
    "Not inventory" knowledge should survive observation resets: decouple the FK, or exclude the table from resets.
    Follow-up.
