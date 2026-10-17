# 06 - Decision models (DiffusionGemma and alternatives) and VLM/omni model selection for the ISS inventory POC

Research date: 2026-10-17. Scope: open-weight models and components only. The pipeline is: live video and audio, then perception (vLLM-Omni / VLM), then decisions, then a Postgres inventory and dashboard.

**Conventions.** Statements marked **[V]** were checked against a primary source on 2026-10-17: a model card, a license text, vLLM source or PRs, or a paper abstract. Statements marked **[I]** are my inferences or estimates and have not been measured on our hardware. Vendor speed and benchmark numbers are labeled as vendor-reported.

---

## 0. Executive summary

1. **The "decision model" now has a standard serving path in vLLM, and DiffusionGemma is one of several backends.** vLLM main now includes `POST /v1/systemone`, the typed `choice` / `noul` (yes/no) / `score` endpoint the team used as a sidecar. It also has an OpenAI-compatible `POST /v1/decisions`. Both are on by default ([PR #59299](https://github.com/vllm-project/vllm/pull/59299), [#60677](https://github.com/vllm-project/vllm/pull/60677), [#60465](https://github.com/vllm-project/vllm/pull/60465), [#60466 DiffusionGemma read strategy](https://github.com/vllm-project/vllm/pull/60466), [#60696 noul/score](https://github.com/vllm-project/vllm/pull/60696)) **[V]**. These merged on 2026-10-08 to 10-10, after **v0.31.0 shipped on 2026-10-05**, so a nightly or main build is required until the next release **[V]**.
2. **For single-label decisions, diffusion is not where the speed comes from.** vLLM reads a DiffusionGemma decision as one read-only denoising step over a 16-token canvas, which is essentially one forward pass ([PR #60466](https://github.com/vllm-project/vllm/pull/60466)) **[V]**. An autoregressive (AR) model reads the same decision as one next-token logprob read. The cost is therefore dominated by prefill (the state and images) and active parameters, not by the decoding scheme **[I]**. DiffusionGemma's actual strengths here are its scale (25.2B total, 3.8B active MoE, Gemma 4 knowledge) and its benchmarked accuracy: JevBench "Intelligence" about 80, against 47 to 53 for Qwen3-4B on the same harness ([#60696](https://github.com/vllm-project/vllm/pull/60696)) **[V]**.
3. **Purpose-built open decision models appeared in September and October 2026 and cover audio and video, which DiffusionGemma does not.**
   - **Cloudflare Clef-Omni** (Apache-2.0, Qwen3-Omni-30B-A3B backbone plus a joint schema head) scores all options of all questions in one forward pass over text, image, audio and video.
   - **vLLM Semantic Router "Decision 3.0" (d3, d3-flash/mini/nano/lite/edge)** (Apache-2.0, Qwen3.5 backbones) handles text, image and video.

   Both are served natively on `/v1/systemone` in vLLM ([docs](https://github.com/vllm-project/vllm/blob/main/docs/models/structured_decisions_models.md)) **[V]**. For an omni (audio and video) inventory pipeline these fit better than DiffusionGemma for decisions that look at media. DiffusionGemma stays a good fit for decisions over text and JSON state, such as dedup adjudication over candidate records.
4. **Guided JSON decoding (xgrammar) is not supported for diffusion LMs in vLLM.** Requests are rejected with a 400. A DiffusionGemma author called canvas-aware constraints "an open problem" ([#45572](https://github.com/vllm-project/vllm/issues/45572)) **[V]**. Any free-form structured output (bounding boxes, inventory JSON) should come from an AR model with xgrammar, not from DiffusionGemma.
5. **Licensing has become simpler.**
   - Gemma 4 and DiffusionGemma are **Apache-2.0** **[V]**. Gemma 3/3n remain under the restrictive Gemma Terms of Use.
   - The Qwen3.5/3.6 and Qwen3-VL lines, Clef and d3 are Apache-2.0.
   - **Cosmos 3** (current NVIDIA line) is **OpenMDW-1.1**, a permissive model license. **Cosmos-Reason1/2 and Cosmos-Embed1** remain under the **NVIDIA Open Model License**, which has guardrail and termination clauses and is not OSI-style open source.
   - Liquid **d1-3B** is under the LFM Open License, which caps commercial use at $10M annual revenue.
6. **Hardware is a hard constraint.**
   - T4 (Turing, sm75) has no BF16 or FP8. The team's existing DiffusionGemma-on-4xT4 deployment needed INT8 weights, `--dtype=half`, and two runtime patches to vLLM's Triton kernels. It consumes all four T4s with tensor parallelism of 4 (`/Users/cnuland/hello-chris-dd-kuberay/k8s/aro/dgemma-t4.yaml`) **[V, local]**.
   - Cosmos-Reason2 and Cosmos 3 list only Ampere, Hopper and Blackwell as supported **[V]**.
   - The DGX Spark (GB10, 128 GB unified memory) is the realistic host for the omni perception model. The T4 node is better used for a small decision and grounding tier **[I]**.

---

## 1. DiffusionGemma and diffusion language models

### 1.1 What DiffusionGemma is [V]

Sources: [model card](https://ai.google.dev/gemma/docs/diffusiongemma/model_card), [HF repo](https://huggingface.co/google/diffusiongemma-26B-A4B-it), [GIGAZINE launch coverage](https://gigazine.net/gsc_news/en/20260611-google-ai-diffusiongemma/).

| Property | Value |
|---|---|
| Developer / release | Google DeepMind, 2026-06-10 (HF repo created 2026-06-09). Described as the first open-weight Google text-diffusion LM |
| License | **Apache-2.0** |
| Base architecture | Gemma 4 26B A4B MoE: **25.2B total, 3.8B active**, 30 layers, 128 experts (8 active + 1 shared), 1024-token sliding window, 262K vocabulary |
| Vision | ~550M-parameter vision encoder; variable-resolution images with 70/140/280/560/1120 token budgets (low budgets for classification and video) |
| Inputs | Text and image, interleaved. Video is accepted as frames, up to 60 s at 1 fps. The card's spec table lists only "Text, Image", while the overview mentions video. **No audio input** |
| Output | Text only. Optional thinking mode (`<|think|>`) |
| Context | 256K tokens |
| Canvas | 256 tokens by default (vLLM can serve a narrower canvas, e.g. `--diffusion-config={"canvas_length":64}`) |
| Speed claim (vendor) | Commits about 15 to 20 tokens per forward pass; more than 1,100 tok/s per user at low batch on H100 FP8; "up to 4x faster than Gemma 4 26B A4B" |
| Checkpoints | `google/...` BF16 (51.7 GB), `RedHatAI/...-FP8-dynamic`, `nvidia/...-NVFP4` (14.4 GB), and the team's `aidendle94/...-INT8-dynamic` (W8A8; **its HF tag says `license:gemma`, which conflicts with the Apache-2.0 base. Check before redistributing**) |

**Architecture.** An AR **encoder** prefills the prompt into a KV cache. A **decoder** applies bidirectional attention over the generation canvas and reads that cache through cross-attention. When a canvas is finished, the encoder ingests it and appends it to the cache before the next canvas starts. This is a block-diffusion design: AR across canvases, diffusion within a canvas.

**Sampling.** The model card describes "Entropy-Bounded Denoising with Adaptive Stopping":
- at most 48 steps;
- temperature decays linearly from 0.8 to 0.4;
- each step commits the lowest-entropy tokens under an entropy bound of 0.1 and fully renoises the rest;
- sampling stops early when mean canvas entropy falls below 0.005 and the argmax is stable for two steps.

**Quality versus its AR sibling** (model card, DiffusionGemma vs Gemma 4 26B A4B):

| Benchmark | DiffusionGemma | Gemma 4 26B A4B |
|---|---|---|
| MMLU Pro | 77.6 | 82.6 |
| GPQA Diamond | 73.2 | 82.3 |
| MMMU Pro (vision) | **54.3** | **73.8** |
| MATH-Vision | 70.5 | 82.4 |
| Tau2 (agentic) | 56.2 | 68.2 |
| BBEH | 47.6 | 64.8 |

The vision gap (MMMU Pro, minus 19.5 points) is the most relevant number for us. **DiffusionGemma is clearly the weaker of the two at visual understanding.** Google itself recommends Gemma 4 when quality matters ([digitalapplied guide](https://www.digitalapplied.com/blog/google-diffusiongemma-open-weight-text-diffusion-model-guide)).

**How it actually commits tokens.** [arXiv:2606.14620](https://arxiv.org/abs/2606.14620) ("Neither Parallel Nor Sequential", 686 prompts) instrumented the sampler and found:
- decoding is neither truly parallel nor block-AR;
- tokens commit in large late bursts;
- **structured JSON commits in nearly arbitrary order**;
- commit confidence tracks correctness for math but **not for factual recall**.

The last point is a caution against treating a DiffusionGemma confidence as calibrated without our own evaluation **[V for paper claims; I for implication]**.

### 1.2 How diffusion decoding works, in brief

- **Masked or discrete diffusion LMs** (LLaDA, [arXiv:2502.09992](https://arxiv.org/abs/2502.09992); Dream 7B, [arXiv:2508.15487](https://arxiv.org/abs/2508.15487)) start from a fully masked or noised sequence and iteratively predict all positions in parallel with bidirectional attention. Each step commits some positions (by confidence or entropy) and remasks the rest.
- **Block diffusion** (BD3-LM, [arXiv:2503.09573](https://arxiv.org/abs/2503.09573)) generates block by block. It is AR over blocks, which allows KV caching and variable length, and uses diffusion within a block. DiffusionGemma's "canvas" is this block.
- **Acceleration work** includes Fast-dLLM ([arXiv:2505.22618](https://arxiv.org/abs/2505.22618), approximate KV cache plus confidence-thresholded parallel decoding) and Fast-dLLM v2 ([arXiv:2509.26328](https://arxiv.org/abs/2509.26328), converts AR models to block diffusion with about 1B tokens, reporting up to 2.5x speedup) **[V abstracts]**.
- **Latency profile [I].** A DLM's per-step cost is a full forward over the canvas. It wins when many tokens can be committed per step: long, low-entropy outputs at low batch. It loses its edge at high batch, where AR decoding is already compute-efficient, and on very short outputs, where both need about one forward. Memory per sequence is higher because the sampler holds canvas-by-vocabulary state. vLLM issue [#50699](https://github.com/vllm-project/vllm/issues/50699) reports OOM from fp32 canvas-by-vocabulary temporaries under concurrency, and the team's T4 deployment caps `--max-num-seqs=2`.

### 1.3 Other DLMs for comparison

| Model | Open? | Notes |
|---|---|---|
| LLaDA 8B / LLaDA 2.0 | Open weights | Masked diffusion; LLaDA 8B is reported comparable to LLaMA3-8B. LLaDA 2.0-flash reportedly reaches 100B (secondary source) |
| Dream 7B | Open (arXiv:2508.15487) | Diffusion LM initialized from an AR model |
| **Nemotron-Labs-Diffusion 3B/8B/14B** | Open weights, NVIDIA Nemotron Open Model License | "Tri-mode": one set of weights does AR, diffusion, or self-speculation. Vendor reports 4x throughput versus Qwen3-8B on GB200 ([NVIDIA research](https://research.nvidia.com/publication/2026-05_nemotron-labs-diffusion-tri-mode-language-model-unifying-autoregressive)). vLLM support is in progress ([#58387](https://github.com/vllm-project/vllm/pull/58387), [#60283](https://github.com/vllm-project/vllm/issues/60283)), including single-forward "decision reads" ([#61734](https://github.com/vllm-project/vllm/pull/61734)) |
| Mercury / Mercury Coder (Inception) | **Closed** API | Over 1,000 tok/s on H100 (vendor, [arXiv:2506.17298](https://arxiv.org/abs/2506.17298)) |
| Gemini Diffusion (Google) | **Closed** demo | About 1,479 tok/s reported (secondary source) |

### 1.4 vLLM support status for DiffusionGemma (as of 2026-10-17) [V]

- **Model support:** `DiffusionGemmaForBlockDiffusion` is listed in vLLM supported models as T + I<sup>+</sup> + V<sup>+</sup>, and `vllm serve google/diffusiongemma-26B-A4B-it` works per the HF card.
- **Maturity:** about 20 open DiffusionGemma PRs and issues remain. They cover:
  - concurrency OOM ([#50699](https://github.com/vllm-project/vllm/issues/50699));
  - shape mismatch under concurrent decode ([#58035](https://github.com/vllm-project/vllm/issues/58035));
  - image-input EngineCore crash ([#56712](https://github.com/vllm-project/vllm/issues/56712), fix PR [#54309](https://github.com/vllm-project/vllm/pull/54309) open; [#57589](https://github.com/vllm-project/vllm/pull/57589) "fix multimodal support" merged 2026-09-18);
  - sampler recompiles ([#61669](https://github.com/vllm-project/vllm/pull/61669));
  - pipeline parallelism ([#52015](https://github.com/vllm-project/vllm/pull/52015)).

  Treat it as **preview quality**. Red Hat likewise describes decision mode as unsupported preview, with a Developer Preview planned for RH AI Inference 3.6 ([Red Hat article](https://developers.redhat.com/articles/2026/09/28/run-decision-model-vllm-and-red-hat-ai)).
- **Structured or guided outputs: not supported.** A guard PR (#45468) returns HTTP 400 for `response_format.json_schema` or `structured_outputs` on diffusion models. Feature request [#45572](https://github.com/vllm-project/vllm/issues/45572) proposes an opt-in "validate-only" first slice: generate, then run one non-advancing xgrammar full-match check. A DiffusionGemma author commented that guided decoding for text diffusion "is an open problem for which we don't have a good answer yet."
- **Decision ("Jev-like") path:**
  - [#57250](https://github.com/vllm-project/vllm/pull/57250) (merged 2026-09-22) added seed canvases, read-only requests, `diffusion_max_steps`, and an *example* `/v1/systemone` server. This is the sidecar the team used.
  - [#59299](https://github.com/vllm-project/vllm/pull/59299) (merged 2026-10-08) made `/v1/systemone` a first-class endpoint.
  - [#60466](https://github.com/vllm-project/vllm/pull/60466) (merged 2026-10-10) added `DiffusionGemmaCanvasStrategy`.
  - [#60696](https://github.com/vllm-project/vllm/pull/60696) added `noul` and `score` question types.
  - [#60465](https://github.com/vllm-project/vllm/pull/60465) added the OpenAI `/v1/decisions` format.
  - None of these is in v0.31.0 (2026-10-05) **[V]**.
- **How the DiffusionGemma read works** (source: `vllm/entrypoints/openai/decisions/strategies.py`):
  - Each question becomes one request.
  - The prompt ends with the empty thought block. The 16-token canvas is `[noise, <turn|>, pad...]`, with `diffusion_max_steps=1` and `diffusion_read_only=True`.
  - The answer is the softmax of the label-token logprobs at the slot.
  - Labels are A to Z for choice (26 options maximum), yes/no for noul, and 0 to 9 for score. There are at most 64 questions per request.
  - The server must run with `--logprobs-mode raw_logprobs|processed_logprobs`.
  - Noise is seeded from a hash of the prompt for repeatability, but results can still vary across batch sizes. Set `VLLM_BATCH_INVARIANT=1` for exact repeatability, at a throughput cost.
- **Measured (PR author, DGX Spark, NVFP4, canvas 64):** about **100 ms per read**; `label_mass` 0.99 to 1.00; JevBench Intelligence 78.4 to 80.1 over four runs.
- **Red Hat measurements:** 8.7 req/s at 0.12 s single-stream on one DGX Spark, and 54 req/s (about 162 decisions/s) at 32 concurrent requests.
- **Images on `/v1/systemone` with DiffusionGemma: not yet.** The docs table lists T+I+V for this architecture, but in main `DiffusionGemmaCanvasStrategy` inherits `NextTokenStrategy.modalities = frozenset()`, so media are rejected. PRs [#62181](https://github.com/vllm-project/vllm/pull/62181) (opened today) and [#60745](https://github.com/vllm-project/vllm/pull/60745) would add image support for next-token strategies and for `/v1/decisions`. **Workaround [I]:** use `/v1/chat/completions` with `logprobs` and `logprob_token_ids` over the single-letter labels, which is how the team's earlier sidecar worked.

### 1.5 When a DLM is the right decision engine [I]

- **Good fit:** a moderate number of typed questions over a **text or JSON state** that needs world knowledge or reasoning. Examples: "is observation O the same entity as candidate record X?", "which zone label fits this description?". You also get calibrated-looking per-option probabilities and abstention via `label_mass` and confidence.
- **Poor fit:**
  - free-form structured output (no grammar support);
  - questions whose evidence is in **audio** (no audio encoder);
  - fine visual detail (weak MMMU Pro);
  - very high concurrency on small GPUs (canvas-by-vocabulary memory).
- **Versus a small AR model on `/v1/systemone`:** same API and same read cost structure (one forward). AR models get prefix caching of the shared state across questions, which DiffusionGemma does too through its AR encoder. Choose by accuracy on our own evaluation set.

---

## 2. Gemma 4 (and Gemma 3n / 3)

Sources: [HF Gemma 4 E4B card](https://huggingface.co/google/gemma-4-E4B-it), [Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4), [HF launch blog](https://huggingface.co/blog/gemma4), [tech report arXiv:2607.02770](https://arxiv.org/abs/2607.02770), [vLLM launch blog](https://vllm.ai/blog/2026-04-02-gemma4).

| Model | Params | Context | Inputs |
|---|---|---|---|
| E2B | 2.3B effective (5.1B with embeddings) | 128K | Text, image, **audio** (30 s maximum), video as frames |
| E4B | 4.5B effective (8B with embeddings) | 128K | Text, image, **audio**, video |
| 12B Unified (released 2026-06-03) | 11.95B dense, **encoder-free** (raw pixels and audio) | 256K | Text, image, audio, video |
| 26B A4B | 25.2B / 3.8B active MoE | 256K | Text, image, video (no audio) |
| 31B | 30.7B dense | 256K | Text, image, video (no audio) |

- **Release and license [V]:** released 2026-04-02 under **Apache-2.0** ("truly open with Apache 2 licenses", per the HF blog). This is a real change from Gemma 1/2/3/3n, which use the **Gemma Terms of Use**: a custom license with a prohibited-use policy that flows down to derivatives and a "Notice" file requirement, and that is not OSI open source ([Gemma ToU](https://ai.google.dev/gemma/terms)). `google/gemma-3n-E4B-it` is tagged `license:gemma` on HF **[V]**. **Recommendation:** use Gemma 4 and not 3n for an "open-source only" project.
- **Video [V]:** video is processed as frames, up to 60 s at 1 fps. Small models can take video with its soundtrack (`load_audio_from_video=True`); 26B and 31B take video without audio.
- **Grounding [V]:** Gemma 4 emits bounding boxes natively as JSON `[{"box_2d":[y0,x0,y1,x1],"label":...}]` on a 0 to 1000 normalized grid when prompted (HF blog). **No RefCOCO, ODinW or Video-MME numbers were found** in the model card or blog.
- **Function calling:** native, including from multimodal inputs.
- **Published benchmarks (model card):**

  | Benchmark | 31B | 26B A4B | 12B | E4B | E2B |
  |---|---|---|---|---|---|
  | MMMU Pro | 76.9 | 73.8 | 69.1 | 52.6 | 44.2 |

  The 12B reports FLEURS WER 0.069 and CoVoST 38.5 for audio.
- **vLLM [V]:** day-0 support. `Gemma4ForConditionalGeneration` covers T + I + V + A, and `Gemma4UnifiedForConditionalGeneration` covers the 12B. Gemma 4 global-attention layers use 512-wide heads. On T4 (64 KB shared memory per block) the Triton attention kernel needs smaller tiles: open PR [#39018](https://github.com/vllm-project/vllm/pull/39018), and the same issue the team patched for DiffusionGemma on T4 **[V local + I]**.
- **Gemma 3n [V, secondary]:** E2B/E4B (June 2025), MobileNet-V5 vision and USM audio encoders, Gemma ToU license. Superseded by Gemma 4 E2B/E4B.

---

## 3. NVIDIA Cosmos

Sources: [NVIDIA Cosmos LLM info page](https://www.nvidia.com/en-us/ai/cosmos/llm-info/), [Cosmos3-Edge card](https://huggingface.co/nvidia/Cosmos3-Edge), [Cosmos-Reason2-8B card](https://huggingface.co/nvidia/Cosmos-Reason2-8B), [cosmos-reason2 repo](https://github.com/nvidia-cosmos/cosmos-reason2), [Cosmos-Embed1-448p card](https://huggingface.co/nvidia/Cosmos-Embed1-448p).

**Status [V]:** NVIDIA directs new development to **Cosmos 3**. The Cosmos-Reason2 repo says it "will receive only limited maintenance updates." This matters for a POC that is meant to last.

| Model | Size | What it does | License | Hardware (card) |
|---|---|---|---|---|
| Cosmos-Reason1-7B | 7B (Qwen2.5-VL-based) | Physical-AI reasoning VLM, chain of thought | NVIDIA Open Model License | Hopper/Blackwell |
| **Cosmos-Reason2-2B / 8B / 32B** | 2.4B / 8.8B / 32B; **post-trained from Qwen3-VL** | Video and image reasoning with long chain of thought; timestamps overlaid on frames for temporal localization; 2D/3D points and boxes in text; 256K context; 4 fps video recommended | NVIDIA Open Model License (gated on HF) | **Hopper and Blackwell only**; BF16 tested; 2B needs 24 GB and 8B needs 32 GB GPU memory per the repo; DGX Spark listed for inference; vLLM >= 0.11 |
| **Cosmos 3** (Edge 4B, Nano 16B, Super 64B; released 2026-05-31 / 07-20) | Mixture-of-Transformers: an AR "Reasoner" tower plus a diffusion "Generator" tower | Reasoner takes text, image and video (4 fps recommended, 256K context) and outputs text with CoT, 2D/3D points and bboxes. Generator handles video, world simulation and actions | **OpenMDW-1.1** | Ampere/Hopper/Blackwell; **BF16 only tested**. vLLM serves the understanding tower (`Cosmos3EdgeForConditionalGeneration`, `Cosmos3ForConditionalGeneration`); vLLM-Omni serves the generators (`Cosmos3OmniDiffusersPipeline`) |
| Cosmos-Embed1 (224p/336p/448p) | about 1B, QFormer-based | **Joint video-text embedder**, 8 frames at 1 to 2 fps, 768-d (448p). Tasks: text-to-video retrieval, semantic dedup, zero-shot/kNN classification | NVIDIA Open Model License | Ampere/Hopper/Blackwell |

- **Cosmos-Reason2-8B versus its Qwen3-VL-8B base** (card category averages): General 73.7 vs 72.0; Robotics 56.9 vs 53.1; Self-driving 67.9 vs 46.4; **Smart Spaces 70.0 vs 42.7**. "Smart spaces" (indoor monitoring) is the closest analogue to an ISS module **[V numbers; I relevance]**.
- **Cosmos3-Edge Reasoner latency in vLLM [V]:** on an RTX PRO 6000 Blackwell, TTFT is about 142 ms for a 50-token prompt with a 1-fps clip and 1 output token, at concurrency 1. A 100-token answer takes about 0.5 s. At concurrency 64 throughput is about 18 req/s for one-token outputs. Note that a one-token output from a VLM is effectively a decision read **[I]**.
- **Licenses.**
  - **NVIDIA Open Model License [V]:** commercial use and derivatives are permitted and NVIDIA claims no ownership of outputs. However, rights terminate if you bypass guardrails without an equivalent, or if you bring infringement litigation. A "Built on NVIDIA Cosmos" attribution is required and NVIDIA's Trustworthy AI terms apply. It is source-available or "open weights", **not OSI open source**.
  - **OpenMDW-1.1 [V]:** grants permission "without restriction" across copyright, patent, database and trade-secret rights. It requires notice retention on redistribution, has a patent/copyright-litigation termination clause, and places no restrictions on outputs. It is permissive, Apache-like in spirit. I believe OpenMDW is the Linux Foundation's open model license family **[I, publisher not stated on the license page]**.
- **Turing/T4 [V/I]:** none of the Cosmos cards list Turing. Running them on T4 would mean FP16 conversion, untested **[I]**.

---

## 4. Qwen omni and VL models

Sources: [Qwen3-Omni-30B-A3B-Instruct card](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct), [Qwen3.5-9B card](https://huggingface.co/Qwen/Qwen3.5-9B), [Qwen3-VL tech report arXiv:2511.21631](https://arxiv.org/abs/2511.21631), [vLLM-Omni supported models](https://github.com/vllm-project/vllm-omni/blob/main/docs/models/supported_models.md).

| Model | Open weights / license | Inputs | Notes |
|---|---|---|---|
| Qwen2.5-Omni-3B / 7B | Yes (7B Apache-2.0; 3B under the Qwen Research license **[I, check]**) | T/I/V/A in; text and speech out | vLLM-Omni on NVIDIA, AMD, Ascend and Intel |
| **Qwen3-Omni-30B-A3B** (Instruct / Thinking / Captioner) | Yes, **Apache-2.0** (HF tag `license: other`, `license_name: apache-2.0`) | T/I/V/A in; Instruct also produces speech via talker | vLLM (thinker only, in `vllm serve`) and **vLLM-Omni** (full thinker plus talker). BF16 minimum memory per card: Instruct 78.9 GB for a 15 s video, 107.7 GB for 60 s; Thinking 68.7 GB for 15 s. `disable_talker()` saves about 10 GB. Community AWQ-4bit, AWQ-8bit, FP8 and NVFP4 builds exist |
| Qwen3.5-Omni (Plus/Flash/Light, 2026-03-30) | **No open weights found** on the Qwen HF org as of today; launch-era press says API-only ([the-decoder](https://the-decoder.com/?p=33841)) | | Do not plan on it |
| Qwen3-VL (2B/4B/8B/30B-A3B/...) | Apache-2.0 | T/I/V | Strong 2D grounding (RefCOCO/+/g, ODinW-13, CountBench in tech report Section 5.5). The base for Cosmos-Reason2 |
| **Qwen3.5** (0.8B/2B/4B/9B/27B/35B-A3B/122B/397B, Feb 2026) | **Apache-2.0** | Natively multimodal: T/I/V | vLLM `Qwen3_5ForConditionalGeneration`. Thinking on by default. Successors are Qwen3.6 (Apr) and Qwen3.8-27B (Aug, Apache-2.0) |
| Qwen3-ASR-0.6B / 1.7B (2026-06) | Apache-2.0 | Audio | Dedicated ASR, useful for the audio channel |

**Benchmarks from the Qwen3.5-9B card [V]:**

| Benchmark | Qwen3-VL-30B-A3B | Qwen3.5-9B | Qwen3.5-4B |
|---|---|---|---|
| RefCOCO (avg) | 89.3 | 89.7 | 88.1 |
| CountBench | 90.0 | 97.2 | 96.3 |
| VideoMME (w/o subtitles) | 73.3 | 78.4 | 76.9 |
| MVBench | 72.0 | 74.4 | 71.2 |
| MLVU | 78.9 | 84.4 | 82.8 |

**Qwen3-Omni-30B-A3B-Instruct [V]:** Video-MME 70.5, MLVU 75.2, LVBench 50.2, WorldSense (audio-visual) 54.0, MMAU (audio reasoning) about 77.5.

**Grounding format [V, secondary]:** Qwen2.5-VL and later emit absolute-pixel `bbox_2d` JSON when prompted. Qwen3-VL normalizes coordinates to a 0 to 1000 grid. Verify per model before writing parsers **[I]**.

**vLLM-Omni versus vLLM [V].** vLLM-Omni (Apache-2.0, v0.30.0 on 2026-09-25, v0.31.0rc1) targets models with **non-text outputs or multi-stage pipelines**: Qwen2.5/3-Omni talkers, MiniCPM-o 4.5, TTS, image/video generation including the Cosmos 3 generators, and VLAs. Understanding-only VLMs such as Gemma 4, Qwen3.5, Qwen3-VL, Cosmos-Reason2 and the Cosmos 3 Reasoner run in plain vLLM. **Implication [I]:** vLLM-Omni is only needed if we use the Qwen3-Omni talker (spoken output) or the full Qwen3-Omni/MiniCPM-o pipeline. Text-out audio-visual perception can run as the Qwen3-Omni thinker on plain vLLM.

---

## 5. Purpose-built open decision models (new since September 2026)

These implement the same System One / Jev request contract (`state` plus typed `questions`, returning a probability per option). TypeSafe's Jev itself is a closed API ([OpenRouter](https://openrouter.ai/docs/guides/community/jev)).

| Model | Backbone / size | Inputs | Mechanism | Speed (vendor) | License |
|---|---|---|---|---|---|
| **Cloudflare/clef-omni** ([card](https://huggingface.co/Cloudflare/clef-omni), [blog](https://blog.cloudflare.com/clef-faster-cheaper-multimodal)) | Qwen3-Omni-30B-A3B thinker plus joint schema head; about 64 GB BF16 | **Text, JSON, image, audio, video with soundtrack** | One forward pass scores **all options of all questions** jointly; no output tokens. LoRA on a frozen backbone, Brier-score calibration | Median about 130 ms text, 150 ms image, a few hundred ms audio, **about 1.5 s for a 21 s video with sound** | Apache-2.0 |
| Cloudflare/clef-flash | Qwen3.5-9B plus head | T, I, V (no audio) | Same | n/a | Apache-2.0 |
| **vllm-sr/d3 family** ([d3](https://huggingface.co/vllm-sr/d3), [d3-mini](https://huggingface.co/vllm-sr/d3-mini)) | Qwen3.8-27B (d3), Qwen3.5 9B/4B/2B/0.8B/0.6B variants | T, JSON, **multiple images and videos** (2 fps, up to 32 frames) | Answer-code readout, one forward per question, up to 255 options | d3: 54 ms text, 290 ms image, 500 ms for a 10 s video (MI325X). **d3-mini (4.5B): 18 ms text, 112 ms image, 336 ms for a 10 s video; about 20 GiB peak** | Apache-2.0 |
| LiquidAI/d1-3B ([card](https://huggingface.co/LiquidAI/d1-3B)) | LFM2.5-VL-3B | T, I | First-token read | 8 ms per decision on RTX 4090 | **LFM Open License v1.0**: commercial use only below $10M annual revenue. Not OSI |
| DiffusionGemma 26B-A4B | see Section 1 | T (I pending) | 1-step canvas read | about 100 ms on Spark | Apache-2.0 |
| Any `Qwen3ForCausalLM` / Qwen3.5 model | | T (+I via [#62181](https://github.com/vllm-project/vllm/pull/62181)) | First-token read | | Apache-2.0 |

**Benchmark claims (vendor, not independently verified):**
- d3 scores 66.3 on the Jev Decision Index 0.3.2 (first on the text board); d3-mini scores 52.6; Perception Test (video multiple-choice QA) is 77.4 for d3 and 70.3 for d3-mini.
- DiffusionGemma scores about 79 to 81 on the *public JevBench items* in [#60696](https://github.com/vllm-project/vllm/pull/60696). That is a different harness and suite version, so it is **not comparable** to the d3 numbers.

All of these models are served by stock vLLM main on `/v1/systemone` ([vLLM structured decisions docs](https://github.com/vllm-project/vllm/blob/main/docs/models/structured_decisions_models.md)). Clef-Omni and d3 are served as pooling or classification models (`label_mass` = 1, zero output tokens).

---

## 6. Where a decision model fits in the perception-to-inventory pipeline

### 6.1 Reference flow [I]

```
camera/mic --> frame sampler + VAD
   |
   +--> [cheap gate] embedding/motion change detector ---------------- (d) route
   |         |
   |         v (only "interesting" windows)
   +--> [perception] omni/VLM: Qwen3-Omni thinker or Qwen3.5/Gemma 4 or Cosmos3 Reasoner
   |         output: JSON observations (object, bbox, attributes, action, speaker utterance) via xgrammar
   v
[candidate retrieval] pgvector over item embeddings (Cosmos-Embed1 / SigLIP / text embeddings) + last-known zone
   v
[decision tier] /v1/systemone: (a) same-or-new, (b) event type, (c) zone, (e) needs-clarification
   v
Postgres inventory (transactional upsert, event log, confidence, provenance) --> dashboard
```

### 6.2 The five decisions

| # | Decision | State given to the decider | Question type | Best engine [I] | Why |
|---|---|---|---|---|---|
| a | **Same entity or new?** (dedup adjudication) | New observation JSON plus the top-k (at most 25) candidate records from pgvector: name, attributes, last zone, last-seen time, thumbnail caption | `choice` over `{cand_1 ... cand_k, "new_item"}` | **DiffusionGemma or d3-flash/d3 on text/JSON state**; add images with d3, Clef or Qwen3.5 when captions are ambiguous | Needs reasoning over attributes and world knowledge. The option count fits A to Z labels. Embedding similarity alone gives the candidates, not the verdict |
| b | Event classification (added / moved / removed / used / no-change) | Before and after observation records, the actor's utterance transcript, short clip | `choice` plus `noul` ("was the item handled by crew?") | **Clef-Omni** (it can hear "I'm stowing the wrench in locker 3") or d3 with video; DiffusionGemma if the omni model already produced a text summary | Evidence is often audio-visual and temporal |
| c | Location / zone assignment | Detected bbox and scene descriptor plus the module/rack/locker taxonomy (often more than 26 zones, so make it hierarchical) | `choice`, hierarchical (module, then rack, then slot) | d3 (255 options per choice) or a DiffusionGemma two-stage choice | The taxonomy is a closed set and must never be free text |
| d | Routing: does this window need the expensive omni pass? | Motion and embedding deltas, cheap caption, audio VAD flag | `noul` / `score` | **Smallest tier**: d3-edge/d3-lite (0.6/0.8B), or a fine-tuned embedding classifier; no LLM needed in the hot path | Runs at frame rate, so latency budget is about 10 to 30 ms |
| e | Clarification-needed flag | Decision outputs and confidences from (a) to (c), plus conflicts | `noul` with criteria, plus **abstention from confidence and `label_mass`** | Rule: flag if `confidence` is below a threshold or `label_mass` is low; optionally ask the decider "is the evidence sufficient?" | The probabilities are the main value of a decision model |

### 6.3 Comparison of decision mechanisms

| Mechanism | Latency per decision (indicative) | Accuracy profile | Structured output | Calibration / abstain | Ops notes |
|---|---|---|---|---|---|
| **DLM single-step read** (DiffusionGemma on `/v1/systemone`) | about 100 ms (Spark NVFP4); about 35 to 60 ms on Cloud Run per Red Hat | Strong reasoning over text/JSON; weaker vision; no audio | Labels only | Per-option probabilities, `label_mass`; reads not bit-repeatable | Preview quality, many open bugs; memory-heavy under concurrency; T4 needs patches and all 4 GPUs |
| **AR first-token read** (Qwen3.5, Gemma 4 via `/v1/systemone` or chat logprobs) | One prefill plus one token; for small models similar to or lower than the DLM [I] | Scales with model; can include images | Labels only | Same | Mature; prefix caching shares state across questions |
| **Joint-schema / answer-code heads** (Clef-Omni, d3, d1) | 8 to 500 ms depending on size and media | Trained specifically for decisions; Clef-Omni is the only open one that uses **audio** | Labels only | Trained calibration (Brier) | Pooling or classify task in vLLM; prompt format fixed by checkpoint (`instructions` field rejected) |
| **AR plus guided decoding** (xgrammar JSON schema with an enum field) | Prefill plus N tokens | Same model quality | **Full JSON** (multiple fields, IDs, bboxes) | Needs logprobs post-processing for confidence | Mature in vLLM; the right tool for perception output, not for a single label |
| **Embedding classifier / kNN** (Cosmos-Embed1, SigLIP, text embeddings plus logistic regression or threshold) | 1 to 10 ms | Good for near-duplicate and closed-set recognition; brittle for reasoning ("same model of wrench but different serial?") | n/a | Distance or margin thresholds | Must be trained or tuned on ISS imagery; ideal as the stage-1 candidate generator and router |

**Cascades and routing.**
- **[V] vLLM Semantic Router** (v0.4.0, 2026-09-27; "an open, programmable decision layer") now ships the Decision 1.0, 2.0 and 3.0 models and the "Vela 2.0" routing models. It can route a request between backends by policy (quality, latency, cost).
- **[V/I] llm-d** (Kubernetes-native, prefix-cache-aware scheduling) is useful once several replicas of the decider share the same `state` prefix. It is overkill for a 1-node POC.
- **[I] Suggested cascade:**
  1. Embedding gate (ms).
  2. Small decision model (d3-edge/lite) for "worth looking at?".
  3. Omni perception on selected windows.
  4. DiffusionGemma or d3 for dedup, event and zone decisions.
  5. Low-confidence items go to (e), the dashboard clarification queue. Optionally re-ask with a larger model or the omni model with thinking before flagging.

---

## 7. Benchmarks and GPU memory

### 7.1 Video / grounding / audio benchmarks (from model cards; harness differences make cross-row comparison approximate)

| Model | Video-MME (w/o subtitles) | MVBench | MLVU | RefCOCO avg | Audio | Source |
|---|---|---|---|---|---|---|
| Qwen3.5-9B | 78.4 | 74.4 | 84.4 | 89.7 | (none) | Qwen3.5-9B card |
| Qwen3.5-4B | 76.9 | 71.2 | 82.8 | 88.1 | (none) | same |
| Qwen3-VL-30B-A3B | 73.3 | 72.0 | 78.9 | 89.3 | (none) | same |
| Qwen3-Omni-30B-A3B-Instruct | 70.5 | n/a | 75.2 | n/a | MMAU about 77.5; WorldSense 54.0 | Qwen3-Omni card |
| Gemma 4 (all sizes) | not published | not published | | not published | FLEURS WER 0.069 (12B) | Gemma 4 card |
| Cosmos-Reason2-8B | n/a (reports physical-AI suites; Smart Spaces 70.0) | | | | | Cosmos-Reason2 card |
| d3 / d3-mini | Perception Test 77.4 / 70.3 | | | | | d3 cards |

ODinW-13 numbers for Qwen3-VL are in Section 5.5 of arXiv:2511.21631. I did not extract a verified figure.

### 7.2 Hardware facts

**4x T4 node [V/I].** Each T4 is 16 GB GDDR6, about 320 GB/s, Turing sm75. It has FP16 and INT8 tensor cores but **no BF16, FP8 or FP4**, and 64 KB shared memory per block. FlashAttention-2 and many Triton kernels assume sm80 or later.

**Team's proven config [V, local]:** DiffusionGemma W8A8 INT8 (27 GB), TP=4, `--dtype=half`, `--moe-backend=humming`, `--max-num-seqs=2`, canvas 64, `--gpu-memory-utilization=0.80`. It also patched vLLM's `triton_unified_attention` (1 stage, 16-token tiles) and the top-k/top-p sampler (PyTorch path on sm75).

**DGX Spark [V/I].** GB10 Grace-Blackwell, 128 GB unified LPDDR5x (about 273 GB/s), supports FP8 and NVFP4. Memory bandwidth, not capacity, limits decode speed. The vLLM decision PRs were validated on Spark, and Cosmos-Reason2 lists Spark for inference.

**Estimated weight footprints [I]** (KV cache, vision encoder activations and video tokens come on top):

| Model | BF16/FP16 | 8-bit | 4-bit | Fits 1x T4? | Fits Spark? |
|---|---|---|---|---|---|
| DiffusionGemma 26B-A4B | 51.7 GB | 27 GB (INT8/FP8) | 14.4 GB (NVFP4; Blackwell only) | No (needs TP4 INT8) | Yes (NVFP4 or FP8) |
| Gemma 4 E4B | about 16 GB (8B with embeddings) | about 8 GB | about 5 GB | Yes at 8-bit/4-bit | Yes |
| Gemma 4 26B A4B / 31B | 51 / 62 GB | 26 / 31 GB | about 15 / 18 GB | TP2 to TP4 only | Yes |
| Qwen3-Omni-30B-A3B thinker | about 60+ GB (card: 69 to 108 GB with video) | about 32 GB (FP8; no FP8 on T4) | about 18 to 20 GB (AWQ) | TP2 with AWQ, untested on sm75 | Yes (FP8 or AWQ) |
| Clef-Omni | about 64 GB (card) | (none published) | (none published) | No | Yes (BF16; would crowd out other models) |
| Qwen3.5-9B / d3-flash | about 19 GB | about 10 GB | about 6 GB | 8-bit yes | Yes |
| Qwen3.5-4B / d3-mini | about 9 GB (about 20 GiB peak with media, per card) | about 5 GB | about 3 GB | Yes | Yes |
| d3-edge / d3-lite | 1.2 / 1.6 GB | | | Yes | Yes |
| Cosmos-Reason2-2B / 8B | 24 / 32 GB minimum (repo) | | | Unsupported arch | Yes |
| Cosmos3-Edge (4B) | about 8 GB, BF16 only tested | | | Unsupported arch | Yes |
| Cosmos-Embed1-448p | about 2.4 GB | | | Likely (FP32 or FP16) | Yes |

**Caution [I]:** Qwen3.5 uses hybrid linear attention (Gated DeltaNet). Its kernels (flash-linear-attention / Triton) are not guaranteed on sm75, so validate Qwen3.5 and d3 on T4 before committing.

---

## 8. Licensing summary

| Component | License | "Open source"? |
|---|---|---|
| DiffusionGemma, Gemma 4 | Apache-2.0 | Yes (OSI license on weights) |
| Gemma 3 / 3n | Gemma Terms of Use | **No**: custom, use-restricted |
| Qwen3-Omni, Qwen3-VL, Qwen3.5/3.6, Qwen3.8-27B, Qwen3-ASR | Apache-2.0 | Yes. Note that Qwen3.8-Flash-Next is tagged `other` |
| Clef / Clef-Flash / Clef-Omni, d3 family | Apache-2.0 | Yes. Inherit their Apache-2.0 bases |
| Cosmos 3 (Edge/Nano/Super) | OpenMDW-1.1 | Permissive open-model license; not OSI-listed as far as I found |
| Cosmos-Reason1/2, Cosmos-Embed1, Nemotron-Labs-Diffusion | NVIDIA Open Model / Nemotron Open Model License | **No**: guardrail and litigation termination clauses, attribution requirements |
| LiquidAI d1-3B | LFM Open License v1.0 | **No**: revenue threshold |
| vLLM, vLLM-Omni, vLLM Semantic Router, xgrammar | Apache-2.0 | Yes |
| `aidendle94/diffusiongemma-...-INT8-dynamic` | HF tag `gemma` (inconsistent with Apache-2.0 base) | Clarify, or re-quantize ourselves with llm-compressor |

Flag for NASA and legal review: "open weights" and "open source" are not the same. If the requirement means OSI-compatible licenses, exclude Cosmos-Reason2, Cosmos-Embed1 and d1-3B, or get a ruling. Note also that all of these licenses disclaim warranty, which matters for any safety-relevant inventory function.

---

## 9. Recommended model lineup for the POC

The design principle [I]: **perception produces structured observations (AR plus xgrammar); decisions are typed reads with probabilities (`/v1/systemone`); every decision stores its probabilities in Postgres; low confidence goes to the dashboard.**

| Role | Primary pick | Alternative | Hardware | Notes |
|---|---|---|---|---|
| Audio-visual perception (the "omni" box in the diagram) | **Qwen3-Omni-30B-A3B-Instruct thinker** (Apache-2.0) on vLLM, or vLLM-Omni if spoken replies are wanted; FP8 or AWQ-4bit | **Gemma 4 E4B/12B** (Apache-2.0, has audio) for a lighter, Google-aligned option; **Cosmos3-Edge/Nano Reasoner** (OpenMDW) for physical-AI and temporal grounding | **DGX Spark** | Use xgrammar JSON schema for observations (object, bbox, attributes, utterance) |
| Grounding / bbox specialist (optional second pass on keyframes) | **Qwen3.5-4B or 9B** (RefCOCO 88 to 90) | Gemma 4 (`box_2d` JSON); Cosmos-Reason2-8B (NVIDIA OML; if the Smart Spaces benchmarks matter) | Spark; 1x T4 at 8-bit only after sm75 validation | |
| Dedup adjudication (a), zone (c), text-state event typing (b) | **DiffusionGemma 26B-A4B** on vLLM main `/v1/systemone` (NVFP4 on Spark; INT8 TP4 on T4 as today) | **d3-flash/d3-mini** (Apache-2.0; T+I+V; up to 255 options) | Spark (NVFP4, 14.4 GB) for dev; **4x T4 (existing manifest)** for the ARO decision tier | Continuity with team experience; strong text reasoning. Upgrade the sidecar to the built-in endpoint (nightly until v0.32) |
| Audio-visual event confirmation (b) | **Clef-Omni** (Apache-2.0) | d3 with video (no audio); Qwen3-Omni first-token read | Spark only (about 64 GB BF16) | Use when the evidence is speech or sound, e.g. "stowing X in Y". Co-hosting with Qwen3-Omni BF16 exceeds 128 GB, so quantize one of them or time-share |
| Routing gate (d) | **Cosmos-Embed1** or SigLIP-class embeddings plus thresholds (if OSI license is required, use an Apache/MIT embedder) | **d3-edge/d3-lite** (0.6B/0.8B, Apache-2.0) on `/v1/systemone` | 1 T4 or Spark | Cheap; runs every window |
| Candidate retrieval for (a) | pgvector over item image and text embeddings | (none) | Postgres | Produces at most 25 candidates for the `choice` question |
| Clarification flag (e) | Rule over `confidence` and `label_mass`, plus optional `noul` "evidence sufficient?" | (none) | (n/a) | Calibrate thresholds on a labeled ISS-like eval set |
| ASR (if transcripts are needed separately) | **Qwen3-ASR-1.7B** (Apache-2.0) | Qwen3-Omni itself | T4 or Spark | |

**Hardware split [I]:**
- **DGX Spark (128 GB):** Qwen3-Omni FP8 (about 35 GB including encoders and KV), DiffusionGemma NVFP4 (about 20 GB with KV), d3-mini (about 20 GB), embeddings (about 3 GB). This totals about 80 GB and leaves headroom. Swap in Clef-Omni for the d3 tier when testing audio decisions.
- **ARO 4x T4:** keep the proven DiffusionGemma INT8 TP4 decision service. Alternatively, repurpose it as 1x d3-mini, 1x Qwen3.5-4B grounding and 2x spare, once Qwen3.5 kernels are validated on sm75.

## 10. Open questions

1. **License bar:** does "only open-source" mean OSI-approved licenses (which excludes Cosmos-Reason2, Cosmos-Embed1, d1, Nemotron) or "open weights"? Is OpenMDW-1.1 (Cosmos 3) acceptable?
2. **Diagram alignment:** "Gemma4 / NVIDIA Cosmos" are named as the omni models. Gemma 4 has audio only in E2B/E4B/12B, and Cosmos has **no audio input**. Is Qwen3-Omni (the only mature open audio+video+image model on vLLM/vLLM-Omni) acceptable, or must audio go through a separate ASR path?
3. **Release timing:** `/v1/systemone` with the DiffusionGemma strategy is only in vLLM main and nightly. Do we pin a nightly digest, or wait for v0.32? Does Red Hat AI Inference 3.6 ship it?
4. **DiffusionGemma images on `/v1/systemone`:** track [#62181](https://github.com/vllm-project/vllm/pull/62181) and [#60745](https://github.com/vllm-project/vllm/pull/60745), or keep the chat-completions-with-logprobs workaround.
5. **Turing viability:** do Qwen3.5/d3 (Gated DeltaNet kernels), Qwen3-Omni AWQ and Cosmos-Embed1 run on sm75 in vLLM 0.31+? Is Azure policy still limited to T4, or could A10/L4 be approved?
6. **Accuracy:** we have no ISS-domain evaluation. We need a labeled set (same-or-new pairs, event clips with audio, zone labels) to compare DiffusionGemma vs d3 vs Clef-Omni vs a Qwen3.5 first-token read, and to calibrate the `confidence` and `label_mass` thresholds. Note that [arXiv:2606.14620](https://arxiv.org/abs/2606.14620) found DiffusionGemma's confidence does not track factual-recall correctness.
7. **Determinism and audit:** decision reads vary across batch composition. Does NASA traceability require `VLLM_BATCH_INVARIANT=1` (lower throughput) plus logging the model digest, prompt and probabilities per inventory change?
8. **Flight-hardware path:** if this ever moves on-orbit, the target accelerator class matters. Jetson Thor numbers exist for Cosmos3-Edge; d1/d3-edge are sub-1B and 3B. Should the decision tier be designed for an edge-sized model from the start?
9. **Joint versus per-question reads:** DiffusionGemma "en-banc" multi-question canvases are a planned opt-in follow-up (#60466). Clef-Omni already scores all questions in one pass. Is that worth the dependency for latency?
10. **INT8 checkpoint provenance:** `aidendle94/...INT8-dynamic` is a third-party quant with a `gemma` license tag. Should we re-quantize DiffusionGemma ourselves (llm-compressor W8A8) for a clean chain of custody?
