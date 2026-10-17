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
