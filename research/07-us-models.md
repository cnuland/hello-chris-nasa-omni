# 07 - US-origin model lineup: Gemma 4, NVIDIA Cosmos / Nemotron, US ASR and tool-calling LLMs (T4 test cluster now, Dell GB10 production)

Research date: 2026-10-17. This uses the same conventions as `06-decision-models-vlms.md`:

- **[V]**: checked against a primary source on this date (model card, HF API/config, vLLM source/PR, vendor docs).
- **[I]**: my inference or estimate, not measured on our hardware.
- **UNVERIFIED**: I looked and could not confirm it.

Vendor speed numbers are labelled as vendor- or community-reported.

---

## 0. Executive summary (decision-ready)

1. **Gemma 4 has a variant that is a near drop-in for Qwen2.5-Omni: `google/gemma-4-12B-it`, the "12B Unified" model.**
   - It is dense, 11.95B parameters, encoder-free, with 256K context and Apache-2.0 licence.
   - It takes **images, audio and video-as-frames natively**, which keeps native audio.
   - It emits **JSON boxes natively** (`{"box_2d": [...], "label": ...}` on a 0-1000 grid).
   - It does **tool calling**.
   - vLLM serves it as `Gemma4UnifiedForConditionalGeneration` (merged 2026-06-03, in v0.31.0, the image we already run).
   - It runs on T4 in fp16 **only with the same sm75 Triton patch we already carry for DiffusionGemma**, because Gemma 4 has 512-wide global-attention heads (vLLM issue #38918 is still open).
   - Audio is capped at **30 s per clip**. Our 6 s windows are fine. **[V]**
2. **Gemma 4 E2B/E4B also take audio**, through a conformer audio tower. **26B-A4B and 31B do not take audio.** **[V]**
3. **vLLM-Omni has no Gemma 4 or Nemotron pipeline.** Its only NVIDIA entry is the Cosmos 3 *generation* pipeline. **Plain vLLM `/v1/chat/completions` handles multi-image plus audio for Gemma 4**, so we do not need vLLM-Omni for perception any more. **[V]**
4. **NVIDIA Cosmos Reason 1/2, and the Cosmos 3 reasoner, are Qwen-derived.** **[V]**
   - Cosmos-Reason1-7B is built on Qwen2.5-VL-7B.
   - Cosmos-Reason2-2B/8B/32B are post-trained from Qwen3-VL-2B/8B/32B-Instruct. The HF `base_model` field says so.
   - The Cosmos3-Nano reasoner's config instantiates `qwen3_vl_text` from `Qwen3-VL-8B-Instruct.json` and loads the Qwen tokenizer.
   - **If "US-origin" means weight lineage, Cosmos Reason does not qualify.** None of these take audio. Treat this as a compliance flag for NASA to rule on.
5. **The NVIDIA model that does fit is Nemotron 3 Nano Omni (`nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-*`, released 2026-04-28).** **[V]**
   - Architecture: NVIDIA Nemotron-3-Nano hybrid Mamba-Transformer MoE (31B total / about 3B active), with a C-RADIOv4-H vision encoder and a Parakeet-TDT-0.6B-v2 audio encoder. All three are NVIDIA components.
   - Inputs: video, audio (up to 1 hour), images and text.
   - It supports tool calling, has BF16/FP8/NVFP4 checkpoints and an official **DGX Spark** recipe, and is in core vLLM (≥0.20).
   - Caveat: Qwen and gpt-oss models were used to generate synthetic training data.
   - **It is not practical on T4.** NVFP4/FP8 need Ada/Blackwell, and BF16 is 62 GB.
6. **Recommendation:**
   - **T4 now:** Gemma 4 12B Unified for perception, Gemma 4 12B QAT-W4A16 for chat, and Whisper-large-v3-turbo or Granite-Speech for ASR. Keep OWLv2 (Google) and DINOv2 (Meta) until Gemma's boxes pass our eval.
   - **GB10:** Gemma 4 12B, or a Nemotron 3 Nano Omni bake-off. On a 273 GB/s box Nemotron's 3B-active MoE decodes roughly 4-6x faster than a dense 12B. Details are in §5.

---

## 1. Gemma 4 (Google)

### 1.1 Variants [V]

Source: HF model cards and HF blog (`huggingface.co/google/gemma-4-E4B-it`, `.../gemma-4-12B-it`, `huggingface.co/blog/gemma4`).

| Variant (HF id, `-it` = instruct) | Params | Modalities in | Context | Audio |
|---|---|---|---|---|
| `google/gemma-4-E2B-it` | 2.3B effective (5.1B with embeddings) | text, image, video-as-frames, audio | 128K | yes (conformer tower) |
| `google/gemma-4-E4B-it` | 4.5B effective (8B with embeddings) | text, image, video-as-frames, audio | 128K | yes (conformer tower) |
| `google/gemma-4-12B-it` ("12B Unified", created 2026-05-23) | 11.95B dense, 48 layers, encoder-free | text, image, video-as-frames, audio | 256K | yes (raw 16 kHz waveform, projected) |
| `google/gemma-4-26B-A4B-it` | 25.2B MoE / 3.8B active | text, image, video-as-frames | 256K | **no** |
| `google/gemma-4-31B-it` | 30.7B dense | text, image, video-as-frames | 256K | **no** |

DiffusionGemma-26B-A4B-it, our decision model, is a separate text+image diffusion LM.

**Licence:** Apache-2.0 for the whole family (HF API `cardData.license=apache-2.0`).

**Limits from the card:**

- "Audio supports a maximum length of 30 seconds."
- "Video supports a maximum of 60 seconds assuming ... one frame per second."
- Configurable image token budgets: 70, 140, 280, 560 or 1120 tokens per image.
- Prompt order: put images before the text and audio after it.
- Audio training covered **speech only**: "Music and non-speech sounds were not part of the training data."
- The models are "not explicitly post-trained on videos".

**Video in vLLM:** no native video tower. vLLM splits a video into up to **32 timestamped frames at 70 soft tokens each** (`gemma4_mm.py`, `_VIDEO_MAX_FRAMES = 32`). For our 6 frames per 6 s, **send 6 images, not a video**. That keeps per-image budgets high (280-560 tokens) for grounding and lets the model cite a `frame_index`. **[V source, I recommendation]**

12B text config (from `config.json`): `head_dim 256`, **`global_head_dim 512`**, 16 heads, 8 KV heads (1 global KV head), sliding window 1024, vocab 262144, final logit softcap 30. **[V]**

### 1.2 Grounding and bounding boxes [V and UNVERIFIED parts]

- The model card lists "object detection" and "pointing" as image capabilities.
- The HF launch blog says Gemma 4 returns **native JSON** for detection with no special prompt or grammar. The keys are `box_2d` (4 ints) and `label`, and "the coordinates refer to an image size of 1000x1000". Its example is `[171, 75, 245, 308]`.
- **The coordinate order is not documented.** The Gemini convention that Gemma follows is `[ymin, xmin, ymax, xmax]`. **UNVERIFIED for Gemma 4: calibrate on a known image.**
- Quality evidence is qualitative only. Community tests: the 26B-A4B demo works; E2B boxes are "not exactly over the object". I found no RefCOCO or COCO-mAP numbers for Gemma 4. **UNVERIFIED.**
- **Implication:** Gemma 4 can plausibly replace OWLv2 boxes, but only after it passes `eval/` IoU on ISS frames.
  - Ask for boxes **per frame**, and give each image its own label such as "Frame 3:".
  - Use 560-token image budgets on the frames where you ask for boxes.
  - Constrain the output with a JSON schema through vLLM structured outputs (xgrammar). Add `box_2d` to our schema and convert it to pixels server-side.

### 1.3 vLLM support [V]

- **Classes:**
  - `Gemma4ForConditionalGeneration` (`gemma4_mm.py`; E2B/E4B/26B/31B, with SigLIP vision tower and optional audio tower).
  - **`Gemma4UnifiedForConditionalGeneration`** (`gemma4_unified.py`, PR #44429, merged 2026-06-03, so in releases ≥ v0.23.0 and in **v0.31.0**).
  - Both are registered in `vllm/model_executor/models/registry.py`.
- **Stale doc:** the supported-models table footnote still says "audio input is only supported by gemma-4-E2B and E4B". That predates the 12B. The `gemma4_unified.py` docstring says "encoder-free image + audio + video", and the vLLM Gemma 4 recipe says the 12B takes raw 16 kHz waveform frames.
- **Recipe flags** (docs.vllm.ai/projects/recipes/.../Google/Gemma4.html):
  - `--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4`, plus `--chat-template examples/tool_chat_template_gemma4.jinja` (from the vLLM repo).
  - `--limit-mm-per-prompt '{"image": 4, "audio": 1}'`.
  - Audio needs `vllm[audio]` extras. The stock image may lack them; a community repo notes `vllm/vllm-openai:gemma4` shipped without audio deps. **Check the v0.31.0 image for `soundfile`/`librosa`.**
- **vLLM-Omni:** the supported-models list (docs.vllm.ai/projects/vllm-omni/.../supported_models) has **no Gemma entry**. **Plain vLLM `/v1/chat/completions` with `image_url` × N plus `input_audio`/`audio_url` content parts is the serving path.** It also has structured outputs, which our omni service already relies on.
- **Speculative decoding:** `google/gemma-4-*-it-assistant` MTP drafters exist (`--speculative-config '{"model": "google/gemma-4-31B-it-assistant", "num_speculative_tokens": 4}'`). They are worth trying on GB10 for decode speed. **[I]**

### 1.4 Checkpoints [V, HF API]

| Format | IDs |
|---|---|
| BF16 | `google/gemma-4-{E2B,E4B,12B,26B-A4B,31B}-it` |
| Google QAT W4A16 (compressed-tensors; no `--quantization` flag) | `google/gemma-4-{E2B,E4B,12B,31B}-it-qat-w4a16-ct` |
| Google QAT GGUF | `google/gemma-4-*-it-qat-q4_0-gguf` |
| FP8 (Red Hat) | `RedHatAI/gemma-4-12B-it-FP8-Dynamic`, `RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic`, `RedHatAI/gemma-4-31B-it-FP8-{dynamic,block}` |
| NVFP4 | `RedHatAI/gemma-4-12B-it-NVFP4`, `nvidia/Gemma-4-26B-A4B-NVFP4`, `nvidia/Gemma-4-31B-IT-NVFP4`, `RedHatAI/gemma-4-{26B-A4B,31B}-it-NVFP4` |
| AWQ / GPTQ | no official ones; community only |

- The recipe says 4-bit on 26B-A4B loses too much quality and suggests `--quantization int8_per_channel_weight_only` instead.
- FP8 and NVFP4 are **useless on T4** (no FP8 tensor cores on sm75). They are for the GB10.

### 1.5 T4 (sm75) feasibility [V for issues, I for sizing]

- **Blocking bug: vLLM issue #38918** ("Gemma4 on Turing GPUs (SM 7.5): all attention backends hit shared memory limits"). It is **still open**; reports say it is unfixed in 0.29/0.30.
  - The 512-wide global heads make Triton unified attention request 96 KB of shared memory against Turing's 64 KB.
  - FlashAttention needs sm80 and FlashInfer rejects head size 512.
  - The fix is the community patch: 16-token tiles and 1 pipeline stage on pre-sm80, or the unmerged PR #39018.
  - **We already carry exactly this patch** in `/Users/cnuland/hello-chris-dd-kuberay/k8s/aro/dgemma-t4.yaml` and `deploy/inference/dgemma.yaml` (`triton_unified_attention.py`: `launch_num_stages = 1`, `TILE_SIZE_{PREFILL,DECODE} = 16`). The same file also carries the PyTorch top-k/top-p sampler patch.
  - **Reuse that init script verbatim for every Gemma 4 pod on T4.**
- **Measured on a T4** (AWS write-up, vLLM 0.30.0, fp16, patched Triton attention, text-only QAT W4A16 builds):

  | Model | Decode (1 stream) | Weights | KV cache |
  |---|---|---|---|
  | E2B | 108.5 tok/s | 2.9 GiB | |
  | E4B | 65.6 tok/s | 4.5 GiB | |
  | 12B | 28.5 tok/s | 7.4 GiB | 20K tokens |

  - Answers were identical to L4.
  - 26B-A4B OOMed on a single T4.
  - **Multimodal (image/audio) on T4 was not tested there. UNVERIFIED.** Our own DiffusionGemma pod does pass images through the Gemma 4 vision path on T4 in fp16, which is encouraging. **[V, local]**
- **fp16 risk:**
  - Gemma is trained in bf16. Gemma 3 on T4 fp16 produced empty or NaN output (vLLM forum).
  - Unsloth reports that the Gemma 4 **conformer audio tower** overflows in fp16: `masked_fill(-1e9)` exceeds the fp16 max on T4.
  - vLLM builds that tower from the **HF implementation** (`AutoModel.from_config`), so **E2B/E4B audio on T4 fp16 is at risk**. **[I]**
  - The **12B Unified has no audio tower** (audio is a linear projection of waveform frames), so it sidesteps that specific bug. **[V architecture, I consequence]**
- **Sizing:**
  - 12B BF16 → fp16 is about 24 GB of weights.
  - TP=2 on 2×T4 leaves only about 1-2 GB/GPU for KV and activations. That is tight but possible at `--max-model-len 8192`.
  - **TP=4 on a new 4×T4 node is comfortable.**
  - The QAT W4A16 12B (`google/gemma-4-12B-it-qat-w4a16-ct`, about 7.4 GiB) fits one T4. W4A16 on sm75 goes through vLLM's Exllama/AWQ-class kernels, not Marlin, which is sm80+.
  - A Marlin shape-mismatch bug on this exact checkpoint (#44796, v0.22) is closed. **[V]**
  - Whether the QAT checkpoint's vision and audio embedders behave well is **UNVERIFIED**. PR #44571 excludes the vision embedder from quantization.

---

## 2. NVIDIA Cosmos and Nemotron

### 2.1 Cosmos Reason / Cosmos 3: provenance and fit [V]

| Model | Base (provenance) | Sizes | Inputs | Grounding | Licence | vLLM |
|---|---|---|---|---|---|---|
| `nvidia/Cosmos-Reason1-7B` | **Qwen2.5-VL-7B-Instruct** | 7B | image, video, text | limited | NVIDIA Open Model License | yes (Qwen2.5-VL class) |
| `nvidia/Cosmos-Reason2-{2B,8B,32B}` (Dec 2025; 8B updated Mar 2026) | **Qwen3-VL-{2B,8B,32B}-Instruct**: "post-trained based on Qwen3-VL-8B-Instruct and follows the same model architecture"; HF `base_model: Qwen/Qwen3-VL-8B-Instruct` | 2B / 8B / 32B | image, video (fps=4 recommended), text; **no audio** | "2D/3D point localization and bounding box coordinates"; repo `2d_grounding` prompt returns JSON `bbox_2d` (Qwen3-VL style) | NVIDIA Open Model License (gated, auto-approve) | yes, `Qwen3VLForConditionalGeneration`, vLLM ≥0.11, `--reasoning-parser qwen3` |
| `nvidia/Cosmos3-Nano` (16B = 8B reasoner + 8B generator, 2026-05-31) / `Cosmos3-Super` (64B) | Card does not name a base, **but `config.json` builds the reasoner as `qwen3_vl_text` from `Qwen3-VL-8B-Instruct.json`, with tokenizer `Qwen/Qwen3-VL-8B-Instruct` and vocab 151936** | 16B / 64B | text, image, video (audio only muxed in MP4, ≤0.5 s) | bboxes and points in reasoning output | **OpenMDW-1.1** | vLLM 0.19.1/0.21 + `vllm-cosmos3` plugin (reasoner); vLLM-Omni `vllm/vllm-omni:cosmos3` (generation) |

Notes:

- The Cosmos-Reason2 repo is "no longer under active development" and points to Cosmos 3.
- Cosmos-Reason2 needs 24 GB (2B) or 32 GB (8B) per the repo, supports Ampere/Hopper/Blackwell, and is tested only in BF16.
- Cosmos 3 says "only BF16 precision is tested ... FP16 ... not officially supported".
- **None of these is a good T4 citizen, none takes speech audio, and all are Qwen-architecture or Qwen-weight derivatives.**

**"Cosmos Nemotron"** was NVIDIA's early-2025 brand for its VLMs. The current equivalents are the **Nemotron Nano VL** line: `nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-{BF16,FP8,NVFP4-QAD}`, which takes text, image and video and has no audio. It is a Nemotron-H backbone with a RADIO encoder. I did not open that card to confirm, so its backbone provenance is **UNVERIFIED**. The omni successor is below.

### 2.2 Nemotron 3 Nano Omni: the NVIDIA model that fits [V]

`nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-{BF16,FP8,NVFP4}`, released 2026-04-28, arXiv 2604.24954.

- **Architecture and provenance:**
  - LLM: Nemotron 3 Nano 30B-A3B (NVIDIA hybrid Mamba2-Transformer MoE, 31B total / about 3B active).
  - Vision: **C-RADIOv4-H** (NVIDIA).
  - Audio: **Parakeet-TDT-0.6B-v2** (NVIDIA).
  - No Qwen component in the architecture. The card lists Qwen3-VL-30B-A3B, Qwen3.5-122B/397B, Qwen2.5-VL-72B and gpt-oss-120b as models used to *improve* it (synthetic data). Disclose that to NASA.
- **Inputs:**
  - Video: MP4 ≤2 min, up to 2 fps / 256 frames at 720p.
  - Audio: WAV/MP3 ≤1 h, ≥8 kHz.
  - Images and text. English only.
- **Context:** 256K (recipes use 131072).
- **Grounding:** CVBench2D 83.95, and visual grounding is an RL task. **No box format is documented. UNVERIFIED.**
- **ASR quality (card, non-reasoning mode):** HF-ASR WER 5.95. Video-MME 72.2.
- **Tool calling:** yes, with `--enable-auto-tool-choice --tool-call-parser qwen3_coder`. That is the parser *name*; it is a format, not a Qwen model dependency. Reasoning parser: `--reasoning-parser nemotron_v3`. Disable thinking per request with `chat_template_kwargs: {"enable_thinking": false}`.
- **vLLM:** core class `NemotronH_Nano_Omni_Reasoning_V3`. Requires vLLM ≥0.20.0 and `pip install "vllm[audio]"` in the container.
- **Sizes:** NVFP4 21 GB, FP8 33 GB, BF16 62 GB.
- **Hardware:** **DGX Spark is explicitly supported**, with a dedicated recipe. Turing is not listed.
- **Licence:** NVIDIA Open Model Agreement (commercial use OK; NASA has accepted NVIDIA licences).
- **T4:** BF16 does not fit 4×T4 (62 GB against 64 GB), and FP8/NVFP4 need sm89+. Community AWQ/W4A16 repos exist (`mattbucci/...-AWQ`, `drawais/...-W4A16`), but Mamba2 Triton kernels, MoE int4 kernels and RADIO on sm75 are all **UNVERIFIED**. Our DGemma pod already needed `--moe-backend humming` because Triton's INT8 fused-MoE could not lower on sm75. **Test it on the GB10, or on a temporary Ampere+ node, not on T4.**

---

## 3. US-origin ASR [V]

| Option | Origin / licence | vLLM | T4 | Notes |
|---|---|---|---|---|
| `openai/whisper-large-v3-turbo` (809M), `openai/whisper-small`, `-medium`, `-large-v3` | OpenAI / MIT | **Yes**: `WhisperForConditionalGeneration` on `/v1/audio/transcriptions` (listed in vLLM supported models) | Model fits easily (about 1.6 GB fp16). vLLM encoder-decoder attention on sm75 is **UNVERIFIED** | Turbo = large-v3 encoder + 4-layer decoder. The `prompt` parameter biases vocabulary (module names). |
| `ibm-granite/granite-speech-4.1-2b` / `-2b-plus` | IBM (US) / Apache-2.0 | **Yes**: `GraniteSpeech(Plus)ForConditionalGeneration`; vLLM ≥0.23 | Same decoder-LM path as our working Qwen3-ASR; likely fine in fp16 **[I]** | **Keyword-list biasing in the prompt** (Gigaspeech keyword F1 74→84). The "plus" variant does speaker-attributed ASR. Open-ASR avg WER 5.3-5.7. EN/FR/DE/ES/PT. Built on Granite-4.0-1B. |
| `nvidia/parakeet-tdt-0.6b-v2` (EN) / `-v3` (25 langs) | NVIDIA / CC-BY-4.0 | **No core vLLM** (not in the transcription table) | NeMo PyTorch runs on T4 **[I]**. **Riva/NIM ASR needs CC ≥8.0 (no T4)**, and on DGX Spark only Parakeet-1.1B CTC/RNNT are supported | Serve with a small NeMo FastAPI wrapper. Best accuracy/latency ratio, but a new serving stack. |
| `nvidia/canary-1b-v2` | NVIDIA / CC-BY-4.0 | No | NeMo | Avoid **`nvidia/canary-qwen-2.5b`**: its `base_model` includes `Qwen/Qwen3-1.7B`. |
| Nemotron 3.5 ASR streaming 0.6B | NVIDIA | Out-of-tree plugin only (`Nemotron3_5AsrForRNNT`, github.com/Sohaib-Ahmed21/vllm-nemotron-asr-plugin) | UNVERIFIED | Streaming, 40 locales |

**Recommendation:**

- **Granite-Speech-4.1-2B** (keyword biasing for "Destiny / Harmony / Unity / Kibo / Columbus / Tranquility / Zvezda" etc.) or **Whisper-large-v3-turbo** on vLLM v0.31.0 on T4 GPU 3, replacing Qwen3-ASR. Try Whisper first because it is simplest. Fall back to Granite if Whisper's encoder-decoder path misbehaves on sm75.
- On GB10, Nemotron Omni's built-in Parakeet encoder can do ASR itself.

---

## 4. US-origin tool-calling chatbot LLM [V unless noted]

| Model | Licence | vLLM tool parser | T4 (16 GB) | GB10 |
|---|---|---|---|---|
| **`google/gemma-4-12B-it-qat-w4a16-ct`** | Apache-2.0 | `--tool-call-parser gemma4 --reasoning-parser gemma4 --enable-auto-tool-choice` (+ `tool_chat_template_gemma4.jinja` if needed) | **Yes, with the sm75 patch**: 28.5 tok/s, about 20K KV tokens (AWS) | yes |
| `google/gemma-4-E4B-it(-qat-w4a16-ct)` | Apache-2.0 | `gemma4` | Yes with the patch (65 tok/s) | yes |
| `nvidia/Gemma-4-26B-A4B-NVFP4` | Apache-2.0 | `gemma4` | no | **yes**: 52 tok/s vLLM on GB10 (community, needs `--moe-backend marlin` on sm121) |
| `openai/gpt-oss-20b` (MXFP4, about 13 GB) | Apache-2.0 | `--tool-call-parser openai --enable-auto-tool-choice` (only `tool_choice="auto"`) | **No**: MXFP4 Marlin MoE needs sm80; community T4 patch attempts failed to build | **yes**: 50-70 tok/s on DGX Spark (Ollama/SGLang reports) |
| `nvidia/NVIDIA-Nemotron-3-Nano-4B-BF16` / Nemotron 3 Nano 30B-A3B | NVIDIA Open Model | `--tool-call-parser qwen3_coder` + `nano_v3` reasoning plugin | Mamba on sm75 UNVERIFIED | yes (DGX Spark in recipe; about 75 tok/s W4A16 30B-A3B, community) |
| `meta-llama/Llama-3.1-8B-Instruct` (AWQ community) | Llama 3.1 Community Licence (**NASA has not accepted it**) | `llama3_json` | yes, no patch needed (AWQ) | yes |
| Llama 4 Scout 17B-16E | Llama 4 licence | `llama4_pythonic` | no | too big alongside the stack |

**Pick:** Gemma 4 12B QAT-W4A16 on T4 GPU 2, replacing Qwen3-8B-AWQ (hermes → gemma4 parser). Keep `enable_thinking` off by default, as now. On GB10, either reuse the perception model or run `nvidia/Gemma-4-26B-A4B-NVFP4` / `gpt-oss-20b`.

---

## 5. Recommendation

### 5a. T4 test cluster (now)

All pods use `docker.io/vllm/vllm-openai:v0.31.0`. Every Gemma 4 pod gets the **sm75 init script from `deploy/inference/dgemma.yaml`** (Triton 1-stage / 16-token tiles + PyTorch sampler) and `CUDA_MODULE_LOADING=EAGER`. Add `pip install "vllm[audio]"` (or bake a derived image) wherever audio is used.

| Node / GPU | Role | Model (HF id) | Key flags |
|---|---|---|---|
| **New node 4×T4** (preferred) or node 1 GPU 0-1 | Perception (replaces Qwen2.5-Omni + vLLM-Omni) | `google/gemma-4-12B-it` | `--dtype half --tensor-parallel-size 4` (or 2) `--max-model-len 8192 --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image":8,"audio":1,"video":0}' --mm-processor-kwargs '{"max_soft_tokens":280}'` (raise to 560 for box frames). Requests use `response_format` json_schema. Fallback on 2 GPUs: `google/gemma-4-12B-it-qat-w4a16-ct` TP=2. |
| Node 1 GPU 2 | Chatbot / agent | `google/gemma-4-12B-it-qat-w4a16-ct` | `--dtype half --max-model-len 16384 --gpu-memory-utilization 0.90 --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 --default-chat-template-kwargs '{"enable_thinking": false}'` |
| Node 1 GPU 3 | ASR | `openai/whisper-large-v3-turbo` (fallback `ibm-granite/granite-speech-4.1-2b`) | `--dtype half --gpu-memory-utilization 0.85`; endpoint `/v1/audio/transcriptions` |
| Node 2 4×T4 | Decision | DiffusionGemma-26B (unchanged; Google, Apache-2.0) | as today |
| Node 3 1×T4 | Boxes + re-ID | OWLv2 (Google) + DINOv2 (Meta) | keep until Gemma `box_2d` passes `eval/` IoU on ISS clips |

Notes:

- If perception goes to a new node, node 1 GPU 0-1 are freed, for example to run a second Gemma 12B replica or Granite-Speech next to Whisper.
- **Go/no-go test before committing**, on one patched pod:
  1. 12B Unified with 6 images plus 6 s of 16 kHz audio in fp16 on T4.
  2. Check that the output isn't NaN or empty.
  3. Check that the transcript is sane.
  4. Check that `box_2d` order is `[ymin,xmin,ymax,xmax]` on a calibration image.
- If fp16 audio fails, drop audio from the perception prompt and feed the Whisper transcript as text (mitigation below).

### 5b. Dell GB10 production (single node)

**Hardware:** NVIDIA DGX Spark spec: Blackwell GPU (sm_121), 20-core Arm (10× X925 + 10× A725), **128 GB LPDDR5x coherent unified memory, 273 GB/s**, up to 1 PFLOP FP4 (sparse), ConnectX-7 200 Gb, 140 W GB10 TDP. Dell Pro Max GB10 listings match: 128 GB LPDDR5x-8533 on a 256-bit bus = 273 GB/s. **[V]**

**Image:** `vllm/vllm-openai:v0.31.0` (multi-arch, arm64 present). Separate `-cu129` tags exist, so the default tag is presumably the CUDA 13 build that sm_121 needs; **verify with `nvidia-smi` and the vLLM startup log**. The vLLM DGX Spark blog uses `cu130-nightly` and says to pin a digest.

**Known GB10 quirks** (community):

- NVFP4 MoE through the CUTLASS FP4 path produced garbage on sm121. Use `--moe-backend marlin`.
- Leave `--quantization` unset for pre-quantized NVFP4 (vLLM blog).
- `--gpu-memory-utilization` is a fraction of the shared 128 GB, so **the sum across all vLLM processes must stay ≤ about 0.85** to leave room for the OS, Postgres and the app. **[I]**

**Decode estimate method:**

- Single stream: tok/s ≈ 0.6-0.7 × 273 GB/s ÷ bytes read per token. Bytes per token = dense weights, or active-expert plus shared weights for MoE, plus the tied LM head.
- Concurrent models **share** the 273 GB/s. **[I]**

**Option A: Gemma-first (matches NASA's stated interest)**

| Role | Model | Weights | KV / act. budget | Est. decode, 1 stream |
|---|---|---|---|---|
| Perception | `RedHatAI/gemma-4-12B-it-FP8-Dynamic` (or `google/gemma-4-12B-it` BF16) | about 13 GB (24 GB BF16) | 8 GB | about 12-15 tok/s FP8 (about 7-9 BF16) **[I]**; `RedHatAI/gemma-4-12B-it-NVFP4` about 20-25 tok/s, NVFP4 quality and sm121 kernels UNVERIFIED |
| Chat | `nvidia/Gemma-4-26B-A4B-NVFP4` (or share the perception model) | 16.5 GB | 8 GB | about 50 tok/s (community-measured 51.6) |
| ASR | `openai/whisper-large-v3-turbo` | 1.6 GB | 2 GB | n/a (well under real time) |
| Decision | DiffusionGemma-26B-A4B (INT8 or NVFP4) | about 26 GB INT8 | 5 GB | prefill-bound (one pass per decision) |
| Boxes / re-ID | OWLv2 + DINOv2 | about 2 GB | 1 GB | n/a |
| OS, app, Postgres, dashboard | | | about 15 GB | |
| **Total** | | | | **about 98 GB FP8 (about 109 GB BF16)**: fits, but is tight with BF16 |

Throughput check: a strict-JSON window is about 400-800 output tokens. At about 13 tok/s that is **30-60 s of compute per 6 s window**. That is fine because real time isn't needed, but either sample every 5th-10th window or trim the JSON. To keep up, Gemma 12B MTP speculative decoding (`google/gemma-4-12B-it-assistant`) and NVFP4 are the levers. **[I]**

**Option B: Nemotron Omni (fastest; one model covers perception, ASR and chat)**

| Role | Model | Weights | KV | Est. decode |
|---|---|---|---|---|
| Perception + ASR + chat | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4` | 21 GB | 15-20 GB | about 50-75 tok/s **[I]** (text Nemotron 3 Nano 30B-A3B W4A16 is about 75 tok/s on GB10, community-reported) |
| Decision | DiffusionGemma-26B | about 26 GB | 5 GB | as above |
| Boxes / re-ID | OWLv2 + DINOv2 | 2 GB | 1 GB | |
| **Total** | | | | **about 75 GB**, leaving lots of headroom |

Official DGX Spark command (model card), adapted:

```
vllm serve nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4 --trust-remote-code \
  --max-model-len 131072 --max-num-seqs 8 --gpu-memory-utilization 0.35 \
  --limit-mm-per-prompt '{"video":1,"image":8,"audio":1}' \
  --media-io-kwargs '{"video":{"fps":2,"num_frames":256}}' --enable-prefix-caching \
  --max-num-batched-tokens 32768 --kv-cache-dtype fp8 \
  --reasoning-parser nemotron_v3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

- The card's Spark recipe uses `vllm/vllm-openai:v0.20.0` with `pip install vllm[audio]`, and `--gpu-memory-utilization 0.8` when it runs alone.
- Use `chat_template_kwargs: {"enable_thinking": false}`, temperature 0.2 and top_k 1 for perception JSON.
- Its box format is undocumented, so keep OWLv2.

**Recommended GB10 path:** run **A vs B as a bake-off on `eval/`**, scoring JSON validity, object recall, event accuracy, location_hint and box IoU. Gemma 4 12B is the default because NASA asked for it and it has documented box JSON. Nemotron 3 Nano Omni is the speed and long-audio option and is equally US-origin (NVIDIA).

### 5c. What we lose versus Qwen2.5-Omni, and mitigations

| Loss | Affected | Mitigation |
|---|---|---|
| Native audio, if we pick a vision-only model (Gemma 26B/31B, Cosmos, Nemotron Nano VL) | those models | Use Gemma 4 12B / E4B or Nemotron Omni, which keep audio. Otherwise run **Whisper/Granite ASR → transcript text in the prompt** (we already run separate ASR, so this costs nothing new). |
| Audio length capped at **30 s** (Gemma 4) | Gemma | Our windows are 6 s, so this is a non-issue. Long-form speech goes through ASR. |
| Non-speech audio understanding (alarms, fans, tool sounds); Gemma audio is speech-only | Gemma | Accept. Optionally add a small audio-event classifier later. |
| vLLM-Omni streaming/talker pipeline | all | We only use text out, so plain vLLM is simpler. |
| Native video tower / timestamps | Gemma | Send 6 frames as images with "Frame i (t=…s)" labels. Gemma's vLLM video path adds `mm:ss` stamps if we use video. |
| Turnkey T4 support | Gemma | Carry the sm75 Triton patch until #38918 / #39018 lands. |
| Qwen-based Cosmos Reason models | compliance | Don't use them if lineage matters. Nemotron Omni is the NVIDIA alternative. |

---

## Sources

**Gemma 4**

- Model cards: https://huggingface.co/google/gemma-4-E4B-it , https://huggingface.co/google/gemma-4-12B-it
- HF launch blog (box_2d, audio, function calling): https://huggingface.co/blog/gemma4
- vLLM recipe (flags, parsers, QAT IDs, image tags): https://docs.vllm.ai/projects/recipes/en/latest/Google/Gemma4.html
- vLLM model code: https://docs.vllm.ai/en/latest/api/vllm/model_executor/models/gemma4_mm/ , https://github.com/vllm-project/vllm/blob/main/vllm/model_executor/models/gemma4_unified.py , registry.py; PR #44429 (Gemma4 Unified), #44571, issue #44796
- Turing bug: https://github.com/vllm-project/vllm/issues/38918
- T4 measurements: https://builder.aws.com/content/3K3ZnLrNaDhh6xnnUAsP2LbJZVQ/gemma-4-on-amazon-sagemaker-the-nvidia-t4-decodes-at-08x-of-the-l4-with-the-same-answers
- fp16 audio overflow: https://unsloth.ai/docs/models/gemma-4/train
- Gemma 3 T4 fp16: https://discuss.vllm.ai/t/gemma3-on-a-t4-gpu/643
- Detection demos: https://pcuenq-gemma-4-object-detection.hf.space/ , https://debuggercafe.com/gemma-4-inference-architecture-and-practical-insights/
- vLLM blog: https://vllm.ai/blog/2026-04-02-gemma4
- GB10 Gemma numbers: https://ai-muninn.com/en/blog/dgx-spark-gemma4-26b-nvfp4-52-toks
- Audio deps note: https://github.com/nishchaysinha/gemma4-audio-vllm-docker

**vLLM**

- Supported models: https://docs.vllm.ai/en/latest/models/supported_models/
- vLLM-Omni supported models: https://docs.vllm.ai/projects/vllm-omni/en/latest/models/supported_models/
- DGX Spark: https://vllm.ai/blog/2026-06-01-vllm-dgx-spark

**Cosmos**

- https://huggingface.co/nvidia/Cosmos-Reason2-8B , https://huggingface.co/nvidia/Cosmos-Reason2-32B , https://github.com/nvidia-cosmos/cosmos-reason2 , https://huggingface.co/nvidia/Cosmos-Reason1-7B
- https://huggingface.co/nvidia/Cosmos3-Nano (and its `config.json`), https://huggingface.co/blog/nvidia/cosmos-3-for-physical-ai , https://docs.nvidia.com/cosmos/latest/cosmos3/index.html , https://github.com/NVIDIA/cosmos/blob/main/README.md
- Grounding prompt write-up: https://dev.classmethod.jp/articles/dgx-spark-cosmos-reason2-structured-reasoning/
- Licence: https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license

**Nemotron**

- https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16 , https://vllm.ai/blog/2026-04-28-nemotron-omni , https://developer.nvidia.com/blog/nvidia-nemotron-3-nano-omni-powers-multimodal-agent-reasoning-in-a-single-efficient-open-model/
- https://recipes.vllm.ai/nvidia/NVIDIA-Nemotron-3-Nano-4B-BF16
- Licence: https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-agreement/

**ASR**

- https://huggingface.co/openai/whisper-large-v3-turbo , https://huggingface.co/ibm-granite/granite-speech-4.1-2b-plus , https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3 , https://huggingface.co/nvidia/canary-qwen-2.5b
- https://docs.nvidia.com/nim/speech/26.02.0/reference/support-matrix/asr.html , https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b

**Chat LLMs**

- https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html , https://discuss.vllm.ai/t/build-issues-when-serving-gpt-oss-20b-on-tesla-t4-gpus-with-vllm/1673 , https://lmsys.org/blog/2025-11-03-gpt-oss-on-nvidia-dgx-spark , https://registry.ollama.ai/blog/nvidia-spark-performance

**GB10 / DGX Spark**

- https://www.nvidia.com/en-us/products/workstations/dgx-spark/ , https://www.shi.com/product/51157109/Dell-Pro-Max-with-GB10-FCM1253

**Local**

- `/Users/cnuland/hello-chris-dd-kuberay/k8s/aro/dgemma-t4.yaml`, `deploy/inference/{dgemma,agent-llm,omni}.yaml`
