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
