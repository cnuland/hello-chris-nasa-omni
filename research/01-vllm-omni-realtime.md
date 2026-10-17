# 01 - vLLM-Omni and the `/v1/realtime` API for an ISS Video + Audio Tracking POC

Research date: 2026-10-17. Source snapshot: `vllm-project/vllm-omni` `main` at commit `37c78bd6d478` (2026-10-18 01:24 +0800), shallow-cloned and read directly. Every file path below is relative to that checkout unless it is a full URL.

Labels used in this report:
- **[V]** Verified: read in source, docs, PR/issue text, or a primary web page.
- **[I]** Inference: my reasoning from verified facts. Not tested on hardware.

---

## 0. TL;DR

1. **[V]** PR #8896 (merged 2026-10-14, merge commit `c2ba8e71`) is small. It removes a hard `ValueError("Image input is not supported")` from the turn-based OpenAI Realtime handler and maps `conversation.item.create` → `input_image` parts into Chat-Completions `image_url` parts (`vllm_omni/entrypoints/openai/realtime/connection.py`). It does **not** add a video-frame stream, any frame-rate control, or incremental KV reuse. Images are conversation items, and a response still has to be triggered by `response.create` or by a server-VAD audio commit.
2. **[V]** vLLM-Omni ships **three** WebSocket ways to get live camera + mic data into a model:
   - `/v1/realtime?duplex=0`: OpenAI-Realtime turn-based. Images plus audio, with text or audio out. This is what PR #8896 touched.
   - `/v1/realtime?duplex=1` (alias `/v1/duplex`): native full-duplex. Camera frames ride on `input_audio_buffer.append.video_frames` in 1 s units. Only **MiniCPM-o 4.5** consumes them.
   - `/v1/video/chat/stream`: a purpose-built streaming-video endpoint. It has `video.frame` + `audio.chunk` + `video.query`, near-duplicate frame filtering (EVS), and **incremental prefill** through thinker prefix caching. It targets Qwen3-Omni.

   For a "watch continuously, summarize, track" workload, `/v1/video/chat/stream` or a windowed `/v1/chat/completions` loop fits better than `/v1/realtime?duplex=0`.
3. **[V]** Supported omni-input models in vLLM-Omni are Qwen3-Omni-30B-A3B, Qwen2.5-Omni-3B/7B, MiniCPM-o 4.5, Ming-flash-omni-2.0, and others. **Gemma 4 is not a vLLM-Omni model.** It runs in upstream vLLM, which supports text, image and audio, with video only as frames. In vLLM-Omni, **NVIDIA Cosmos3** is a generation/world/policy model, not a video-understanding one.
4. **[V+I]** On **4× T4 16 GB (SM 7.5, no BF16)**, Qwen3-Omni-30B-A3B is not realistic. Qwen2.5-Omni-3B thinker-only in FP16 on one T4 per replica is the most plausible omni option. **[V]** A user on 4× T4 hit a TP≥2 failure with an AWQ Qwen3-Omni checkpoint (issue #3714, unresolved). The **DGX Spark (128 GB unified)** can hold Qwen3-Omni-30B-A3B in BF16. Multi-arch (arm64) nightly images exist.
5. **[V]** The OpenAI-compatible chat path in vLLM-Omni does **not** forward `response_format`/JSON-schema into sampling. The serving code only copies a whitelist of sampling fields. The one place structured decoding is wired is **tool calling on `/v1/realtime`**, which uses xgrammar structural tags. Plan on tool calls or client-side schema validation for Postgres writes.
6. **[I]** A VLM is not a multi-object tracker. Use a classical detector/tracker (e.g. YOLO + ByteTrack) for per-frame IDs and boxes. Use the omni model at about 0.5–1 Hz for semantic summarization, identity reasoning and audio understanding.

---

## 1. What vLLM-Omni is (vs. vLLM)

**[V]** From `README.md`: vLLM-Omni "extends [vLLM's] support for omni-modality model inference and serving". It covers text, image, audio, video and action data. It supports non-autoregressive architectures (DiT and other parallel generators) and heterogeneous outputs. It adds:
- pipelined stage execution with overlap
- "fully disaggregation based on OmniConnector and dynamic resource allocation across stages"
- TP/PP/DP/EP
- an OpenAI-compatible server
- full-duplex realtime serving

It tracks vLLM releases: the source targets vLLM 0.31.0, and the latest stable is vLLM-Omni 0.30.0 on vLLM 0.30.0 (`docs/getting_started/installation/gpu/cuda.inc.md`; `gh release list`: v0.30.0 is 2026-09-25, v0.31.0rc1 is 2026-10-06). Install it with `uv pip install vllm==0.30.0 && uv pip install vllm-omni==0.30.0` and serve with `vllm serve <model> --omni`. Papers: technical report arXiv:2610.09307 (2026-10-07) and the original disaggregated-serving paper arXiv:2602.02204.

### 1.1 Stage model

**[V]** A model is a graph of stages. Each stage is a vLLM engine (AR) or a diffusion/generation engine, configured by a deploy YAML (`vllm_omni/deploy/*.yaml`, passed with `--deploy-config`). For Qwen3-Omni (`vllm_omni/deploy/qwen3_omni_moe.yaml`):

| Stage | Role | Notes |
|---|---|---|
| 0 Thinker | Multimodal LLM (audio and vision encoders + 30B-A3B MoE) that produces text | `max_num_seqs: 64`, `gpu_memory_utilization: 0.9`, `moe_backend: triton`, `devices: "0"` |
| 1 Talker | Generates audio codec tokens | `devices: "1"`, fed through `SharedMemoryConnector` |
| 2 Code2Wav | Codec → waveform | `devices: "1"`, `gpu_memory_utilization: 0.1` |

The comment says it was "verified on 2x H100". `async_chunk: true` streams codec chunks between stages. Red Hat's write-up describes the same Thinker (~30B MoE) / Talker (~3B MoE) / Code2Wav split. It explains that the OmniConnector data plane uses `/dev/shm` on one node and RDMA/TCP across nodes, and notes that "for text-only requests, the Talker and Code2Wav still initialize and hold memory" ([Red Hat Developers, 2026-07-01](https://developers.redhat.com/articles/2026/07/01/inside-vllm-omni-architecture-serving-qwen3-omni)).

**[V] Thinker-only mode** is the right mode for this POC because we need text, not speech. `vllm_omni/deploy/qwen3_omni_moe_thinking.yaml` sets `pipeline: qwen3_omni_moe_thinker_only`, `enable_prefix_caching: true`, TP=2, and is "verified on 2x H100-80G". Per `docs/user_guide/examples/online_serving/qwen3_omni.md`, this YAML also works for Instruct weights and loads no talker or code2wav. `docs/configuration/stage_configs.md` documents the `pipeline` override for `qwen2_5_omni_thinker_only` / `qwen3_omni_moe_thinker_only`.

### 1.2 Models relevant to "video + audio in, text out"

**[V]** From `docs/models/supported_models.md`:

| Model | HF id | NVIDIA | Notes |
|---|---|---|---|
| Qwen3-Omni | `Qwen/Qwen3-Omni-30B-A3B-Instruct` | ✅ | Primary target of the realtime and video-stream endpoints |
| Qwen2.5-Omni | `Qwen/Qwen2.5-Omni-7B`, `-3B` | ✅ (also AMD/NPU/XPU) | Deploy YAML: `max_num_seqs: 1`, `enforce_eager: true` on CUDA |
| MiniCPM-o 4.5 | `openbmb/MiniCPM-o-4_5` | ✅ | Only model whose duplex Stage 0 consumes camera frames |
| Ming-flash-omni-2.0 | `Jonathan1909/Ming-flash-omni-2.0` | ✅ | Large MoE; has a thinker-only YAML |
| JoyAI-VL-Interaction | (recipe `recipes/JD/JoyAI-VL-Interaction.md`) | — | Has a "camera observation once per second" realtime profile |
| Cosmos3 | `nvidia/Cosmos3-Nano/Super` | ✅ | **Generation / world model / action policy**, not understanding |

**[V] Gemma 4.** There is no Gemma 4 understanding model in `vllm_omni/model_executor/models` (grep only finds Gemma as text encoders inside diffusion/TTS pipelines). Upstream vLLM serves `google/gemma-4-E4B-it` with native text, image and audio. The model "does not have a native video tower", so video is split into timestamped frames (up to 32 frames × 70 soft tokens) ([vLLM recipe](https://recipes.vllm.ai/Google/gemma-4-E4B-it), [vLLM gemma4_mm docs](https://docs.vllm.ai/en/v0.24.0/api/vllm/model_executor/models/gemma4_mm/), [vLLM blog](https://vllm.ai/blog/gemma4)). The recipe says it "fits on a single 24 GB+ GPU", requires vLLM ≥ 0.19.1, and uses example flags `--limit-mm-per-prompt '{"image": 4, "audio": 1}'`.

**[V] Qwen3.5-Omni** appears to be API-only, not open weights ([search summary](https://www.datalearner.com/en/ai-models/pretrained-models/qwen3-5-omni-plus)). That leaves Qwen3-Omni as the current open Qwen omni model.

### 1.3 Hardware, memory, quantization, parallelism

- **[V]** CUDA requirement: "compute capability 7.5 or higher (e.g., T4 ...)" (`docs/getting_started/installation/gpu/cuda.inc.md`). The T4 is the minimum.
- **[V]** Qwen3-Omni-30B-A3B memory (HF model card, Transformers + FA2, BF16 "theoretical minimum"): Instruct needs 78.85 GB for 15 s of video, 88.52 GB for 30 s, 107.74 GB for 60 s and 144.81 GB for 120 s. Thinking (thinker only) needs 68.74 / 77.79 / 95.76 / 131.65 GB ([HF card](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct)). The weights are about 60 GB in BF16 ([Red Hat](https://developers.redhat.com/articles/2026/07/01/inside-vllm-omni-architecture-serving-qwen3-omni)).
- **[V]** Quantization (`docs/user_guide/quantization/overview.md`). For multi-stage omni models, quantization is scoped to the **thinker LM only**. Audio and vision encoders, talker and Code2Wav stay BF16. Validated paths are ModelOpt (FP8/NVFP4 thinker) and **AutoRound W4A16**, with checkpoints `Intel/Qwen2.5-Omni-7B-int4-AutoRound` and `Intel/Qwen3-Omni-30B-A3B-Instruct-int4-AutoRound` (`docs/user_guide/quantization/autoround.md`). The hardware tables start at **SM 80 (Ampere)**, so **T4/SM 75 is not listed for any method**. BitsAndBytes requires SM 75+ (`bitsandbytes.md`) but is "not validated" for omni stages.
- **[V]** Issue #3714: a user with **4× T4** running `cyankiwi/Qwen3-Omni-30B-A3B-Instruct-AWQ-4bit` with `--dtype half --kv-cache-dtype fp8` hit `ExllamaLinearKernel cannot implement ... Group size (32) does not evenly divide ... (4304)` with TP 2 or 4 under vllm-omni. They reported it works in plain vLLM. The issue was closed for inactivity, and no fix is recorded.
- **[V]** Issue #4838 is a T4 BF16/Half dtype crash (Voxtral TTS). It is evidence that Turing FP16 paths are under-tested.
- **[V]** TP is set per stage (`tensor_parallel_size` in the stage YAML). Examples include the thinker-only TP=2, the MiniCPM-o 8×4090 layout with thinker TP=4, and NPU Qwen3-Omni TP=2.
- **[V] DGX Spark / unified memory.** Multi-arch images exist. Docker Hub `vllm/vllm-omni` tags `v0.30.0`, `nightly`, `nightly-aarch64` and `nightly-<sha>` are `amd64`+`arm64`. Issue #8550 (open) says that on unified-memory GPUs (Thor, Spark, GH200) `request_memory_tolerant()` can give a stage *all* free host memory and hang the box; it was observed with Qwen2.5-Omni-3B. Issue #1121 ("dgxspark aarch64 not support") is still open; a commenter got Qwen3-Omni working via a custom Spark Docker build. Spark specs: 128 GB LPDDR5x unified, 273 GB/s ([NVIDIA docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)). GB10 is SM 12.1, and the repo's attention docs repeatedly mark SM120/121 as special cases (`docs/user_guide/diffusion/attention_backends.md`).

---

## 2. `/v1/realtime` in detail

### 2.1 Routing and `duplex` semantics (verified in `vllm_omni/entrypoints/openai/api_server.py`)

```python
@router.websocket("/v1/realtime")
async def realtime_websocket(websocket):
    duplex_handler = websocket.app.state.openai_serving_duplex
    duplex_query = websocket.query_params.get("duplex")
    use_duplex_realtime = duplex_handler is not None and (
        duplex_query is None or duplex_query.lower() in {"1","true","on"})
    if use_duplex_realtime: ... duplex_handler.handle_realtime_session(websocket)
    if duplex_query in {"1","true","on"}:  # requested but not a duplex deployment
        -> error "VAD realtime is not enabled", close 1008
    realtime_handler = websocket.app.state.openai_realtime_handler  # turn-based
    await realtime_handler.handle_websocket(websocket)
```

- **duplex=0 / turn-based** (`OpenAIRealtimeHandler` → `OpenAIFullDuplexConnection` in `vllm_omni/entrypoints/openai/realtime/connection.py`). It is mounted on ordinary (non-duplex) deployments. It rejects diffusion engines and requires a chat service (`vllm_omni/entrypoints/duplex/openai.py`). The docs say "currently only supported for Qwen3-Omni" (`docs/serving/realtime_openai_duplex_api.md`). The code gates only on "non-diffusion + chat service", so other omni/VLM chat models may work for text output **[I]**.
- **duplex=1** requires a deploy YAML with `session_mode: duplex` and a model `duplex_plugin`: MiniCPM-o 4.5, PersonaPlex, AURA, JoyAI, and Qwen3-Omni via `vllm_omni/deploy/qwen3_omni_duplex.yaml`. A duplex server mounts *only* `/v1/realtime`, `/v1/duplex`, `/v1/chat/completions` (if the plugin allows it), `/v1/models` and `/health`. `?duplex=0` on a duplex server is rejected with "Realtime API is not available" (`docs/serving/realtime_duplex_api.md`).

### 2.2 duplex=0 protocol (verified from `connection.py`)

**Client → server events handled** (`_dispatch_event`): `session.update`, `input_audio_buffer.append`, `.commit`, `.clear`, `response.create`, `response.cancel`, `conversation.item.create`, `.delete`, `.retrieve`, `.truncate`. Anything else returns `error` "Unknown event type". Events are parsed with the official `openai.types.realtime` Pydantic models (`TypeAdapter(types.RealtimeClientEvent)`), so OpenAI GA Realtime shapes apply.

**Key constraints from source:**
- **Audio input must be mono PCM16 at 24 kHz.** `SAMPLE_RATE_HZ = 24000` and `_is_pcm16_24khz_format` rejects other rates with "Only 24 kHz PCM16 input audio is supported". The engine resamples to Qwen's 16 kHz (`examples/online_serving/realtime_web/README.md`). `docs/serving/realtime_openai_duplex_api.md` says "16 kHz" for the Qwen3-Omni example, which conflicts with the code. Trust the code (24 kHz).
- Caps: `MAX_AUDIO_APPEND_BYTES = 15 MiB` per append, `MAX_INPUT_AUDIO_BUFFER_BYTES = 64 MiB` per pending turn, and session history `MAX_HISTORY_BYTES = 64 MiB` (`realtime/session.py`).
- **Default `output_modalities` is `["audio"]`** and `max_output_tokens: "inf"` (`session.py` L55–56). For text you must send `session.update` with `output_modalities: ["text"]`, or override per `response.create`. Text then arrives as `response.output_text.delta` / `.done`. In audio mode the text arrives as `response.output_audio_transcript.delta`.
- `turn_detection` supports only `server_vad` (Silero; defaults threshold 0.5, prefix 300 ms, silence 500 ms). `input_audio_transcription` and `noise_reduction` are accepted but nulled out. MCP tools are stripped.
- `response.create` runs a preflight that binary-searches truncation of old items against `max_model_len`. With `truncation: auto` it triggers at 80% of the limit and trims to 50% (`AUTO_TRUNCATION_*`). **Every `response.create` re-renders the full prompt (all retained images and audio) through the chat renderer.**
- Server event sequence per response: `response.created → response.output_item.added → response.content_part.added → (*.delta)* → ... → response.done`. Tool calls emit `response.function_call_arguments.delta/.done` on a separate `function_call` item.

**Image input, as added by PR #8896 (diff verified with `gh pr diff 8896`):**

```python
# _validate_input_item
if isinstance(item, types.RealtimeConversationItemUserMessage):
    for part in item.content:
        if part.type == "input_image" and not part.image_url:
            raise ValueError("input_image requires an image_url")
...
# _build_full_prompt
elif part.type == "input_image" and part.image_url:
    image_url = ImageURL(url=part.image_url)
    if part.detail is not None:
        image_url["detail"] = part.detail
    content.append(ChatCompletionContentPartImageParam(type="image_url", image_url=image_url))
```

The docs added in the PR (`docs/serving/realtime_openai_duplex_api.md`) say: "Images do not start a response; send `response.create` after adding the item. The `input_image.image_url` value is passed through the model's chat multimodal renderer." The test `test_input_images_map_to_chat_multimodal_prompt` shows one user item carrying text + PNG + JPEG data URLs (with `detail: "low"`) becoming one chat user message with three parts. The PR author tested it "with a demo livekit frontend" (PR comment).

**Caveats the review bot raised on PR #8896 (verified in the PR thread):**
- No server-side image budget is enforced in turn mode. The `realtime_web` README promises "eight images and 4 MiB; external image URLs are not supported", but that applies to the duplex plugin. In duplex=0, any number or size of data URL, and even `http(s)` URLs, reach the renderer. **Our client must prune old image items with `conversation.item.delete`.**
- Stored images are re-rendered on every response and on truncation probes (`skip_mm_cache=True`).

**Minimal verified-shape duplex=0 session for our use case:**

```jsonc
// 1. configure: text out, no VAD (we drive turns), system prompt
{"type":"session.update","session":{"type":"realtime","output_modalities":["text"],
  "instructions":"You are an ISS cabin observer...","audio":{"input":{"format":{"type":"audio/pcm","rate":24000},"turn_detection":null}}}}
// 2. stream mic audio continuously (24 kHz PCM16 base64)
{"type":"input_audio_buffer.append","audio":"<b64>"}
// 3. every N seconds: add the latest keyframe(s)
{"type":"conversation.item.create","item":{"id":"cam_000123","type":"message","role":"user",
  "content":[{"type":"input_text","text":"t=12:03:05Z camera=Node2-fwd"},
             {"type":"input_image","image_url":"data:image/jpeg;base64,...","detail":"low"}]}}
// 4. seal the audio window and ask
{"type":"input_audio_buffer.commit"}
{"type":"response.create","response":{"output_modalities":["text"],"max_output_tokens":400}}
// 5. prune old frames to bound the prompt
{"type":"conversation.item.delete","item_id":"cam_000115"}
```

### 2.3 duplex=1 (native full-duplex): the only true "frames ride with audio" path

**[V]** `docs/serving/realtime_duplex_api.md`: `input_audio_buffer.append` can carry `video_frames` (bare base64 JPEG/PNG, no `data:` prefix) "rid[ing] the append that closes a 1 s model unit". Only "a model whose Stage 0 interleaves a frame track at unit boundaries" accepts it. Per the capability table that is **MiniCPM-o 4.5 only** (`chunk_period_ms` 1000). Qwen3-Omni rejects `video_frames` because "it answers whole turns". The duplex Qwen plugin keeps "at most four audio payloads ... 8 MiB ... at most eight prompt images", and older turns' images are dropped with their audio. The protocol is about 70% OpenAI names, with Tier 2/3 extensions such as `response.listen/speak`, `playback.ack`, `session.resume` and `server_event_seq`. Python client: `vllm_omni.clients.duplex.DuplexClient` with `stream_pcm(video_frames=...)`. Open bug #8637: "MiniCPM-o 4.5 AV duplex ends with incomplete responses and post-commit drain timeout on a 180s stream".

**[I]** duplex=1 is designed for *conversational* agents, where the model decides when to speak. Our workload is a passive observer that writes structured records, so the duplex machinery (barge-in, playback acks) is overhead.

### 2.4 `/v1/video/chat/stream`: the purpose-built streaming-video endpoint

**[V]** `docs/serving/video_stream_api.md`, `vllm_omni/entrypoints/openai/serving_video_stream.py`, `video_stream_base.py`:

| Dir | Event | Fields |
|---|---|---|
| C→S | `session.config` (first) | `modalities` (default `["text","audio"]`), `num_frames` (1–128, default 4), `max_frames` (1–256, default 50), `system_prompt`, `use_audio_in_video` (default true), `sampling_params_list`, `enable_frame_filter` (EVS, default true), `frame_filter_threshold` (0.95) |
| C→S | `video.frame` | `data` = base64 JPEG/PNG, optional `frame_id` |
| C→S | `audio.chunk` | `data` = base64 **PCM16 16 kHz mono** |
| C→S | `video.query` | `text` |
| C→S | `video.done` | — |
| S→C | `response.start`, `response.text.delta`, `response.text.done`, `video.frames.consumed` (`frame_ids`, `latest_pts_ms`), `response.output_audio.*`, `session.done`, `error` | |

Hard limits in source: `_MAX_FRAME_SIZE = 10 MB`, `_MAX_BUFFER_FRAMES = 64`, `_MAX_AUDIO_BUFFER_BYTES = 4 MiB` (about 131 s of 16 kHz PCM16), `_MAX_MSG_QUEUE = 200`, idle timeout 60 s.

**Incremental prefill** applies when stage-0 `enable_prefix_caching: true` and `modalities: ["text"]`. As frames arrive, the server "warmup" prefills `history + frames` with `max_tokens=1`. A `video.query` then pays only for the audio + question suffix. Once `max_frames` is reached, the oldest half is dropped in one step. After each turn, history compresses to the last two text messages. Open PR #7813 adds `max_history_turns`. Frames are fed as individual images (`image_pil`), not as one video tensor. The doc says outright that this "does not accelerate `/v1/realtime` camera input". Known issue: "back-to-back short replies can still expose an engine-layer scheduler race ... workaround of at least 200 ms idle between turns."

Reference client: `examples/online_serving/qwen3_omni/streaming_video_client.py`. It samples video at 2 fps, uses JPEG quality 80, and sends 1 s audio chunks of 32 000 bytes.

**[I]** For "frames + audio continuously, ask every few seconds", this is the closest fit, and it is the only path with prefix-cache-backed incremental prefill. Downsides: it is Qwen3-Omni-specific (the prompt builder is "Qwen-Omni"), and it is a vLLM-Omni-proprietary protocol.

---

## 3. Other endpoints: chat completions, audio-in-video, structured output

- **[V]** `/v1/chat/completions` accepts text, image, audio and video parts, with `modalities: ["text"]` for text-only output (`docs/serving/chat_completions_api.md`). There is also `POST /v1/chat/completions/batch` (no streaming or tools). Qwen-specific inputs: `image_url`, `video_url`, `input_audio` (`{"data": b64wav, "format":"wav"}`), and `mm_processor_kwargs: {"use_audio_in_video": true}`. The Qwen card warns to keep `use_audio_in_video` consistent across a multi-turn conversation ([HF card](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct)).
- **[V] Sampling overrides.** `serving_chat.py::_apply_request_overrides` copies only `temperature, top_p, top_k, max_tokens, min_tokens, seed, ignore_eos, stop, stop_token_ids, frequency_penalty, presence_penalty` onto the thinker's deploy-YAML defaults. `max_tokens` otherwise comes from the YAML. Per-stage `sampling_params_list` can be passed in `extra_body`, and each entry is built as `SamplingParams(**dict)`.
- **[V] Structured output.** Grep across `vllm_omni/entrypoints` shows `StructuredOutputsParams` is used **only** in the realtime tool-call path (`connection.py` L1076–1077: `sp.structured_outputs = StructuredOutputsParams(structural_tag=...)`). The HTTP chat path never converts `response_format`. The test `tests/dfx/reliability/invalid_param_test/test_invalid_omni_chat.py::invalid_response_format_json_schema` only checks that a *malformed* schema is rejected (PR #6290). The scheduler does have grammar support (`vllm_omni/core/sched/omni_ar_scheduler.py`). The closed PR #7799 reports "7/7 valid identical JSON outputs" for Qwen3-VL and "28/28" for MiniCPM-o in JSON-schema E2E runs, but it was never merged and does not show the HTTP wiring. **Conclusion: do not depend on `response_format: json_schema` against vLLM-Omni.** Options:
  1. Realtime tool calling (`tools` in `session.update`), which requires server flags `--enable-auto-tool-choice --tool-call-parser <parser>` (`connection.py` raises "Function tools require ..." otherwise). The parser emits a structural tag, so arguments are grammar-constrained **[V]**. The correct parser for Qwen-Omni (likely `hermes`) is **[I]**/unverified.
  2. Offline `Omni.generate` with `SamplingParams(structured_outputs=StructuredOutputsParams(json=...))` **[I]**.
  3. Prompt for JSON, then validate and retry client-side.

  Upstream vLLM (e.g. for Gemma 4) supports `response_format` normally.

---

## 4. Deployment

- **[V] Images.** `vllm/vllm-omni:v0.30.0` (amd64 + arm64) and `vllm/vllm-omni:nightly-<sha>`. **PR #8896 is not in v0.30.0 or v0.31.0rc1** (rc1 was cut 2026-10-06, before the 2026-10-14 merge). I checked with the GitHub compare API: `nightly-6bc3eabd26e6...` (2026-10-17) contains merge commit `c2ba8e71` (70 commits ahead). Pin a nightly SHA until 0.31.0 final. `docker/Dockerfile.cuda` is `FROM vllm/vllm-openai:v0.31.0` + `uv pip install .`.
- **[V] Helm.** `examples/online_serving/chart-helm/` (Deployment, Service, Ingress, HPA, PVC model cache, `omniArgs.extraArgs`). Its README lists `Qwen/Qwen2.5-Omni-7B` at 2 GPUs. It also lists a "`Qwen/Qwen3-Omni-7B-Chat`", which does not exist on HF, so treat the chart docs as loosely maintained.
- **[V] Red Hat.** There is a Red Hat Developers deep-dive on vLLM-Omni (single B200; demo image `jitigges/vllm-omni-demo`). I found **no** evidence that vLLM-Omni is a supported Red Hat AI / OpenShift AI ServingRuntime ([search](https://developers.redhat.com/articles/2026/07/01/inside-vllm-omni-architecture-serving-qwen3-omni)). **[I]** You can wrap it in a custom KServe `ServingRuntime` (container `vllm/vllm-omni:nightly-<sha>`, args `vllm serve /mnt/models --omni --deploy-config ...`). Things to plan for:
  - (a) KServe/Knative and the OpenShift Route/Ingress must allow **WebSocket upgrade** and long idle timeouts (`haproxy.router.openshift.io/timeout`).
  - (b) Multi-stage engines spawn processes that use `/dev/shm` (SharedMemoryConnector), so mount an `emptyDir: {medium: Memory}` at `/dev/shm`.
  - (c) The `/health` 200 does not mean warmup or admission is ready in duplex mode (`realtime_duplex_api.md`).
- **[V] Known issues worth tracking:**
  - #8637: MiniCPM-o AV duplex fails on long streams.
  - #4383: per-request Python preprocess dominates the decode step at high concurrency, "a frame-cadence blocker for realtime/duplex".
  - #8550: unified-memory over-allocation.
  - #3714: T4 AWQ TP failure.
  - #5554: Qwen3-Omni stage-0 deadlock on 8×L4 (closed).
  - Video-stream scheduler race (200 ms idle workaround).

---

## 5. Best practices for streaming-video LLM inference

**Verified anchors:**
- Qwen3-Omni evaluation uses video at **fps=2** (HF card).
- vLLM-Omni's own UI sends **1 JPEG/s, longest side 448 px** for Qwen VAD camera input, and keeps **8** camera items (`realtime_web/README.md`).
- The JoyAI profile commits "latest frame once per second while idle".
- The streaming client uses 2 fps and JPEG q80.
- MiniCPM-o aligns frames to 1 s units.
- EVS near-duplicate filtering has a default similarity threshold of 0.95.
- vLLM pre-allocates memory per `limit_mm_per_prompt`, so lower it to avoid OOM (HF card).
- Research: StreamingVLM keeps attention-sink tokens + a short recent-vision window + a longer text window, and evicts old vision tokens first. It runs at up to 8 FPS on one H100 ([arXiv 2510.09608](https://arxiv.org/abs/2510.09608)). Related KV work: LiveVLM (2505.15269), InfiniPot-V (2506.15745), HERMES (2601.14724), DSCache (2605.01858).

**Recommendations [I]:**
1. **Decouple perception rate from reasoning rate.** Run detection/tracking at 5–15 fps on the edge (or on one T4). Send the omni model **keyframes at 0.5–1 fps**, triggered by tracker events (new ID, ID lost, zone entry) plus a periodic heartbeat. EVS-style dedup (perceptual hash or SSIM ≥ 0.95) avoids paying for static ISS modules.
2. **Resolution and token budget.** Qwen-VL-family vision encoders tokenize by 28×28 patches, so 448×448 ≈ 256 visual tokens per image (448/28 = 16, so 16² patches before merge) **[I, approximate]**. Eight frames plus 10–30 s of audio (Qwen audio ≈ 25 tokens/s **[I]**) plus prompt fits comfortably in 8–16 k context. Use `detail: "low"` and downscale. Send crops of tracked objects instead of full frames when asking identity questions.
3. **Window, don't accumulate.** Keep a sliding window (e.g. the last 8 keyframes + the last 20 s of audio). Delete old items (duplex=0), rely on `max_frames` halving (video-stream), or simply send stateless windows (chat completions). Carry the long-term state as *text* (the previous summary + the current inventory snapshot from Postgres) rather than old pixels.
4. **Prefix caching.** Put a stable system prompt and the inventory schema first. Turn on stage-0 `enable_prefix_caching` (the thinker-only YAML already does). The video-stream endpoint exploits it for incremental prefill.
5. **Batching.** Multiple camera feeds work better as independent requests to a pool of small replicas, since the Qwen2.5-Omni YAML is `max_num_seqs: 1`. Qwen3-Omni stage 0 allows 64.
6. **Timestamps.** Neither endpoint gives precise A/V alignment ("no precise video/audio timestamp alignment", `realtime_web/README.md`). Put wall-clock timestamps and camera IDs in `input_text` parts next to each image, and keep your own alignment in the client.

---

## 6. Recommendations for the POC

### 6.1 Model + hardware

| Target | Recommended model | Serving | Rationale |
|---|---|---|---|
| **ARO, 4× T4 16 GB** | **Qwen2.5-Omni-3B, thinker-only, `--dtype half`**, one replica per T4 (up to 4 streams), `max_model_len` ~8–16k, `limit_mm_per_prompt` image≤8/audio≤1 | vLLM-Omni nightly (with #8896) | **[I]** About 4.5–5 B params incl. encoders ≈ 10 GB FP16, which leaves KV headroom on 16 GB. Qwen3-Omni-30B-A3B (60 GB BF16, int4 thinker still ~16+ GB plus BF16 encoders) does not fit, and a 4× T4 user hit a TP bug (#3714). Turing has no BF16, so watch for FP16 overflow (Qwen is trained in BF16). |
| ARO fallback | `google/gemma-4-E2B/E4B-it` on **upstream vLLM** (image + audio, frames as images, real `response_format` JSON schema) | `vllm/vllm-openai` | **[I]** Upstream vLLM has better-trodden T4 paths and structured outputs. E4B recommends 24 GB+, so E2B or int4 is likely needed on a T4. No `/v1/realtime` image path, so use chat completions windows. |
| **DGX Spark (dev)** | **Qwen3-Omni-30B-A3B-Instruct, thinker-only (`qwen3_omni_moe_thinking.yaml` adapted to TP=1)** in BF16, or the Intel AutoRound int4 thinker for speed | `vllm/vllm-omni:nightly-aarch64` pinned SHA | **[V]** Weights about 60 GB fit in 128 GB. **[I]** Set explicit `gpu_memory_utilization` (≤0.7) because of #8550. The 273 GB/s bandwidth limits decode, but A3B active params make it workable. Expect SM121 kernel friction. |

Also try **MiniCPM-o 4.5** if you want true AV-interleaved duplex later. It is the only model with frames in 1 s units. **[I]** Its ~9 B thinker likely needs TP=2 on T4s.

### 6.2 Client architecture

```
camera(s) ──► edge ingest ──► detector+tracker (YOLO/ByteTrack, 5–15 fps) ──► track events
   mic ─────► VAD/segmenter (16k/24k PCM16)                                     │
                         │                                                       ▼
                         └──────► Omni gateway (one WS session per feed) ◄── keyframe selector (EVS + events, ≤1 fps, 448px)
                                     │  path A: /v1/video/chat/stream (Qwen3-Omni, text-only, prefix cache)
                                     │  path B: /v1/realtime?duplex=0 (item.create input_image + audio commit + response.create, tools)
                                     │  path C: /v1/chat/completions stateless windows (simplest; works on any backend)
                                     ▼
                         JSON validator (pydantic) ─► reconciler (match LLM entities ↔ track IDs) ─► Postgres (inventory, sightings, events)
```

- **Start with path C**: windowed, stateless chat completions every 2–5 s, with 4–8 keyframes + an audio segment + the current inventory snapshot. It is portable across vLLM-Omni and upstream vLLM, easy to load-test, and robust to the realtime churn visible in the open PRs (#7743, #7996, #8828, #8493).
- **Path A** is the upgrade on Qwen3-Omni/Spark for lower latency through incremental prefill.
- **Path B** applies once you need OpenAI-Realtime compatibility (e.g. LiveKit front ends) or voice interaction with crew. In that case set `output_modalities:["text"]`, use 24 kHz PCM, prune images to ≤8, and use `tools` (e.g. `upsert_object`, `log_person_sighting`) for grammar-constrained arguments.
- Keep the omni model off the per-frame critical path. Postgres writes come from the reconciler, never directly from model text.

---

## 7. Open questions

1. Does Qwen2.5-Omni(-3B) thinker-only run correctly in **FP16 on T4** in vLLM-Omni (overflow/NaNs, attention backend on SM75, `enforce_eager` cost)? It needs a smoke test. No repo CI covers Turing.
2. Does `/v1/realtime?duplex=0` work with **Qwen2.5-Omni** or thinker-only Qwen3-Omni for text output? The docs claim Qwen3-Omni only, while the code gates only on "chat service present".
3. Does `/v1/video/chat/stream` work with **Qwen2.5-Omni**? Its handler is named "Qwen-Omni" and documented for Qwen3-Omni.
4. Which `--tool-call-parser` is correct for Qwen-Omni realtime tool calls, and is grammar-constrained argument generation reliable with images in context? (Open PRs #5555 and #8493 are still changing this area.)
5. Will `response_format: json_schema` be wired into the omni chat path (follow-up to closed PR #7799)?
6. Is Intel AutoRound W4A16 usable on **SM75** (it is listed only for SM80+)? Would `Intel/Qwen2.5-Omni-7B-int4-AutoRound` fit on a single T4?
7. What are the real latency and throughput on DGX Spark for Qwen3-Omni thinker-only with 8×448 px frames + 20 s audio (prefill dominates)? Does issue #8550 bite with the stock image?
8. Is Red Hat planning a supported vLLM-Omni ServingRuntime for OpenShift AI, and how do KServe/Knative handle long-lived WebSocket sessions (timeouts, autoscaling on concurrent sessions)?
9. Do ISS operations need on-prem/air-gapped weights and licensing review (Qwen: Apache-2.0; Gemma: Gemma terms)?
10. Will the scheduler race in the video-stream endpoint ("≥200 ms idle between turns") affect a 1–2 s query cadence?
