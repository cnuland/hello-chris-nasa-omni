# 03 - The Audio Path: ASR, Diarization, Sound Events, Audio-Visual Fusion and Inventory Extraction for the ISS Omni POC

Research date: 2026-10-17. Sources: Hugging Face model cards and Hub API metadata (licenses read from `cardData`), GitHub repos and docs read with `gh`, arXiv abstracts and HTML, and NASA NTRS papers. Repo stars and activity dates are as of today.

Labels (same convention as `01-vllm-omni-realtime.md`):
- **[V]** Verified: read in a model card, repo, doc, paper, or primary web page.
- **[I]** Inference: my reasoning from verified facts. Not tested on our hardware.

Hardware context from report 01: 4x T4 16 GB (SM 7.5: no BF16, no FlashAttention-2) plus a DGX Spark (128 GB unified memory). This report treats that as the target.

---

## 0. TL;DR

1. **[V]** The open ASR field has moved past Whisper. Qwen3-ASR-1.7B/0.6B (Apache-2.0, Jan 2026), Granite-Speech-4.1-2B (Apache-2.0), Cohere-Transcribe (Apache-2.0), Voxtral-Mini-4B-Realtime (Apache-2.0), Parakeet-TDT-0.6B-v3 and Canary-Qwen-2.5B (CC-BY-4.0) all beat Whisper-large-v3 on the published benchmarks. All except the NVIDIA NeMo models are served natively by **upstream vLLM** through `/v1/audio/transcriptions`. Voxtral-Realtime and Qwen3-ASR-0.6B also stream through `/v1/realtime`.
2. **[V]** Noise matters more than leaderboard rank. On Qwen's internal "ExtremeNoise" set, Whisper-large-v3 scores **63.2% WER** and Qwen3-ASR-1.7B **16.2%**. Parakeet-v3's card shows average WER rising from 6.34% clean to **11.66% at 0 dB SNR**. **[I]** ISS modules measure **~50-65 dBA**, with a speech interference level of **~47-53 dB**, so normal-voice speech at 1-2 m from a mic will often be at **0-10 dB SNR**. Plan and test for that range.
3. **[V]** Domain vocabulary ("Node 2", "Harmony", "PGT", "CTB") is best handled by models with real **context biasing**:
   - Qwen3-ASR has a `context=` string.
   - Granite-Speech-4.1 lists keywords in the prompt. Its keyword F1 rises from 74→84 on GigaSpeech and 78→93 on LS-other.
   - VibeVoice-ASR and MOSS-Transcribe-Diarize accept hotwords.
   - NeMo has GPU-PB phrase boosting.
   - Whisper's `initial_prompt`/`hotwords` work, but weakly.
4. **[V]** Diarization: **pyannote community-1** (code MIT, weights CC-BY-4.0) is the default. **Nemotron-3-Diarization** (Sep 2026, OpenMDW-1.1, up to 8 speakers, streaming at ≥0.32 s) is the strongest permissive streaming option. Two joint models emit speaker-attributed transcripts in one pass: **MOSS-Transcribe-Diarize** (Apache-2.0, served by vLLM with `diarized_json`) and **Granite-Speech-4.1-2B-plus** (Apache-2.0). **[I]** Map anonymous speakers to crew names with enrolled ECAPA/TitaNet voiceprints plus visual active-speaker detection. Keep voiceprints on-prem and under crew consent.
5. **[V]** Qwen3-Omni-30B-A3B (Apache-2.0) does take audio and video together (`use_audio_in_video`). It aligns them with absolute-time TM-RoPE at 80 ms per position, and vLLM-Omni's `/v1/video/chat/stream` takes `audio.chunk` alongside `video.frame`. But its own report lists **multi-speaker ASR as future work**, and it was not evaluated on timestamp grounding. **[I]** Use it for **semantic** fusion, such as resolving "that's Kate's camera" against a frame, not as the system of record for who said what when. Run a dedicated ASR → diarization → extraction path in parallel and fuse late, on timestamps.
6. **[I]** Inventory events should come from an LLM extraction step with **JSON-schema-constrained decoding** (vLLM `structured_outputs`, xgrammar/llguidance backends) on upstream vLLM. The vLLM-Omni chat path does not forward `response_format` (report 01). The output is a candidate event that is reconciled against tracker state before any Postgres write.
7. **[V]** Lessons from flight: NASA's **Clarissa** (ISS, 2005) found the hard problem was not recognition. It was **telling commands apart from crew conversation in open-mic mode**. Clarissa misfired about 10% of the time and needed undo and confirmation. **[I]** The same applies here. "I'm putting the drill in Node 2" is a statement to log, but "should I put the drill in Node 2?" is not. The extractor must classify speech acts and attach a confidence. Low-confidence events go to a review queue, not straight into the inventory.

---

## 1. Speech recognition

### 1.1 Candidate models (verified licenses and capabilities)

| Model | Params | License **[V]** | vLLM support **[V]** | Notable facts **[V]** |
|---|---|---|---|---|
| `openai/whisper-large-v3` | 1.55B | Apache-2.0 | `/v1/audio/transcriptions` (encoder-decoder, beam search "highly inefficient") | 99 languages; known hallucination on non-speech |
| `openai/whisper-large-v3-turbo` | 809M | MIT | yes | decoder cut from 32→4 layers; mean English WER ~7.8 vs 7.4 for v3 (secondary sources) |
| `distil-whisper/distil-large-v3.5` | ~756M | MIT | via Whisper arch | English only |
| `Qwen/Qwen3-ASR-1.7B` / `-0.6B` + `Qwen3-ForcedAligner-0.6B` | 1.7B / 0.6B | Apache-2.0 | transcription; **0.6B also realtime** (`Qwen3ASRRealtimeGeneration` via `--hf-overrides`) | 30 languages + 22 Chinese dialects; `context=` biasing; streaming in the vLLM backend; arXiv:2601.21337 |
| `mistralai/Voxtral-Mini-4B-Realtime-2602` | 4B | Apache-2.0 | **realtime** `/v1/realtime` | natively streaming; delay 80 ms-2.4 s; 13 languages; avg 8.72% WER at 480 ms, 6.73% at 2.4 s |
| `mistralai/Voxtral-Mini-3B-2507` / `Small-24B-2507` | 3B / 24B | Apache-2.0 | transcription + chat | audio-LLM (Q&A over audio) |
| `ibm-granite/granite-speech-4.1-2b` (+ `-plus`, `-nar`) | 2B | Apache-2.0 | yes (`GraniteSpeech[Plus]ForConditionalGeneration`) | Open ASR avg 5.33 (base) / 5.71 (plus); keyword biasing; **plus** adds speaker-attributed ASR and word timestamps |
| `ibm-granite/granite-speech-5.0-470m-turboctc` | 470M | Apache-2.0 | (transformers ≥5.16) | Aug 2026; CTC, edge-oriented; IBM reports results on the **FFASR far-field/noisy leaderboard** |
| `CohereLabs/cohere-transcribe-03-2026` | ~2B | Apache-2.0 | transcription | reported #1 on Open ASR in Mar 2026 (5.42% avg, secondary source) |
| `nvidia/parakeet-tdt-0.6b-v3` | 600M | CC-BY-4.0 | NeMo (not in vLLM core) | 25 EU languages; word timestamps; published SNR table (below) |
| `nvidia/canary-qwen-2.5b` | 2.5B | CC-BY-4.0 | NeMo | English; FastConformer + Qwen3-1.7B LoRA; trained on clips ≤40 s |
| `nvidia/nemotron-speech-streaming-en-0.6b` | 600M | NVIDIA Open Model License | plugin | cache-aware streaming |
| `microsoft/VibeVoice-ASR` | ~7B | MIT | yes (`VibeVoice-ASR-HF`) | 60-min single pass; who/when/what; hotwords |
| `OpenMOSS-Team/MOSS-Transcribe-Diarize` | 0.9B | Apache-2.0 | yes, the only model with `diarized_json` | 90-min single pass; speakers + timestamps + **acoustic event tags**; hotwords; won the INTERSPEECH 2026 MLC-SLM challenge |

Sources: HF cards ([Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B), [Voxtral Realtime](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602), [Granite 4.1 plus](https://huggingface.co/ibm-granite/granite-speech-4.1-2b-plus), [Granite 5.0 TurboCTC](https://huggingface.co/ibm-granite/granite-speech-5.0-470m-turboctc), [Parakeet v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), [Canary-Qwen](https://huggingface.co/nvidia/canary-qwen-2.5b), [VibeVoice-ASR](https://huggingface.co/microsoft/VibeVoice-ASR), [MOSS-Transcribe-Diarize](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize)). The vLLM model list is in [`docs/models/supported_models.md`](https://github.com/vllm-project/vllm/blob/main/docs/models/supported_models.md) (sections "Transcription" and "Realtime Transcription"). The Open ASR Leaderboard paper is arXiv:2510.06961. Its finding: Conformer-encoder + LLM-decoder designs get the best WER, while CTC/TDT decoders get far better RTFx.

**License note [V/I]:** Apache-2.0, MIT and CC-BY-4.0 are all OSI-compatible or open-content licenses suitable for "open-source only". CC-BY-4.0 requires attribution. Several models need closer review before use:
- The NVIDIA Open Model License/Agreement (Nemotron streaming ASR, Sortformer v2.1, Nemotron-3-Nano-Omni) is not an OSI license. Treat it as "open weights", not "open source", until legal reviews it.
- `nvidia/diar_sortformer_4spk-v1` is CC-BY-**NC**-4.0. Avoid it.

### 1.2 vLLM's speech APIs (what we would actually call)

**[V]** From [`docs/serving/online_serving/speech_to_text.md`](https://github.com/vllm-project/vllm/blob/main/docs/serving/online_serving/speech_to_text.md):
- **`/v1/audio/transcriptions`** is compatible with the OpenAI API. It takes `language`, a `prompt` "to guide the transcription style", `temperature`, and `response_format` ∈ {json, text, verbose_json, diarized_json}. `verbose_json` returns segments with start/end times. The default upload limit is 25 MB (`VLLM_MAX_AUDIO_CLIP_FILESIZE_MB`).
- **`/v1/realtime`** is a WebSocket. The client sends `input_audio_buffer.append` with base64 PCM16 audio and receives incremental transcripts. The doc lists Voxtral-Realtime and Qwen3-ASR-0.6B as the realtime models. Voxtral needs `--tokenizer-mode mistral` and `mistral-common[audio]`.
- Qwen3-ASR has its own `qwen-asr` package with a vLLM backend. That backend does streaming, but streaming "does not support batch inference or returning timestamps". It uses 2-second chunks, and streaming WER on Fleurs-en goes from 3.35 to 4.02 (tech report, Table 8).

**[I]** All of these are text models, so they fit on a single T4 in FP16. A 4B model at FP16 is ~8 GB of weights. The Qwen and Granite cards recommend FlashAttention-2, which T4 cannot use, so expect lower throughput and use SDPA. Speed has not been measured on T4.

### 1.3 Noise robustness: the evidence

**[V] Published noise numbers:**

| Model | Condition | WER |
|---|---|---|
| Parakeet-TDT-0.6B-v3 | Open-ASR 8-set avg with MUSAN noise/music: clean / 10 / 5 / 0 dB SNR | 6.34 / 7.12 / 8.23 / **11.66%** (AMI: 11.3 → **24.4%**) |
| Canary-Qwen-2.5B | LibriSpeech clean + white noise: 10 / 5 / 0 / -5 dB | 2.41 / 4.08 / 9.83 / **30.6%** |
| Qwen3-ASR-1.7B vs Whisper-large-v3 | Qwen internal "ExtremeNoise" (Mandarin) | **16.17%** vs **63.17%** (Qwen3-ASR-0.6B 17.88%; GPT-4o-Transcribe 36.11%) |
| Qwen3-ASR-1.7B vs Whisper-large-v3 | "Dialog-Accented English" | 16.07% vs 21.30% |
| Voxtral Realtime | delay trade-off (Fleurs avg) | 12.6% at 160 ms → 8.72% at 480 ms → 6.73% at 2.4 s |

Sources: the [Parakeet v3 card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) "Noise Robustness" section, the [Canary-Qwen card](https://huggingface.co/nvidia/canary-qwen-2.5b), and the [Qwen3-ASR card](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) Table 4. The Qwen3-ASR report credits RL training on about 50k utterances for its noise robustness. **[V]** **FFASR** ([treble-technologies/ffasr](https://huggingface.co/spaces/treble-technologies/ffasr), launched June 2026 by Treble and Hugging Face) is the first open far-field leaderboard. It scores near-field, lab-measured, lab-simulated, high/mid/low-SNR and moving-source conditions. Its headline finding is that far-field WER at low SNR is "several times higher" than near-field WER on the same speech. I could not read the live table because the data loads from a private bucket. Room impulse responses come from the Treble10 dataset (arXiv:2510.23141).

**[V] Whisper hallucination.** "Careless Whisper" (arXiv:2402.08021) found that about 1% of Whisper API transcriptions contained whole fabricated sentences. 38% of those were harmful, and they concentrated in audio with long non-speech stretches. **[I]** An ISS cabin has continuous fan noise and long gaps between utterances, which is exactly the trigger condition. Any Whisper deployment needs VAD gating: Silero VAD (MIT) or faster-whisper's `vad_filter`, which defaults to True, plus `hallucination_silence_threshold`. A fabricated "I moved the defibrillator" is worse than a missed event.

**[I] Front-end enhancement.** Options are DeepFilterNet (MIT/Apache dual license), SGMSE (MIT), or ClearerVoice-Studio (Apache-2.0). Enhancement can help humans and some CTC models, but it often *hurts* large end-to-end ASR through artifacts. Test enhancement-on against enhancement-off on our ISS-noise test set before adopting it. The cleaner win is mic placement and beamforming: a close-talk or array mic per module, and the crew headsets that already exist on the Audio Terminal Units.

### 1.4 Accents and multilingual crews

**[V]** Qwen3-ASR covers English accents "from multiple countries and regions" and 30 languages, Russian and Japanese among them. Parakeet-v3 covers 25 EU languages, including Russian. Voxtral-Realtime covers 13 languages, also including Russian. Qwen3-Omni accepts speech input in 19 languages. **[I]** ISS crews are multinational (NASA, Roscosmos, ESA, JAXA, CSA) and switch between English and Russian, so automatic language ID per segment is required. Russian-segment speech also brings Russian module names (Zvezda, Nauka, Poisk, Rassvet, Prichal) into English sentences. Qwen3-ASR and VibeVoice explicitly handle code-switching, and VibeVoice's card claims this natively.

### 1.5 Domain vocabulary and contextual biasing

**[V] Mechanisms:**
- **Qwen3-ASR:** `model.transcribe(audio, context="...")` takes a free-text context string. During SFT the model is trained to use "context tokens inside the system prompt as background knowledge" (tech report). The report does not measure biasing accuracy.
- **Granite-Speech-4.1:** append `Keywords: …` to the prompt. The card's keyword-F1 table shows large gains: GigaSpeech 74.2→84.1, LS-other 78.2→93.0, Earnings22 68.8→81.5. Keywords that are absent from the audio are tolerated.
- **VibeVoice-ASR and MOSS-Transcribe-Diarize:** both take "customized hotwords" in the prompt.
- **NeMo (Parakeet/Canary):** GPU-PB phrase boosting (shallow fusion in greedy or beam decoding, scales to about 20k phrases; TurboBias, arXiv:2508.07014) and CTC-WS word spotting. A 2026 paper (arXiv:2605.18222) found CTC-WS beats GPU-PB in streaming. Capitalization must match the model's style.
- **Whisper:** `initial_prompt`, which is limited to the decoder's prompt window, and faster-whisper's `hotwords`. vLLM passes `prompt` through. These work weakly and can increase hallucination in silence.

**[I] ISS lexicon to build (biasing list plus the extractor's controlled vocabulary).** Include each module's formal and informal names:
- U.S. segment: Node 1/Unity, Node 2/Harmony, Node 3/Tranquility, US Lab/Destiny, Airlock/Quest, Cupola, PMM/Leonardo, BEAM.
- International partners: Columbus/COF, JEM/Kibo/JPM/JLP.
- Russian segment: Service Module/Zvezda, MLM/Nauka, MRM1/Rassvet, MRM2/Poisk, Prichal.

Also include rack and stowage location codes. These are typically module plus surface plus bay, but I could not verify the exact IMS location grammar. **Ask NASA for the official IMS location vocabulary.** Add tool names (Pistol Grip Tool/PGT, CTB/cargo transfer bag, torque wrench, crimper), crew first names and call signs, and payload names. Keep the list short per request, a few hundred tokens, and context-dependent. For example, use the module-specific stowage list for the module whose mic is active. Prompt-based biasing degrades when the list is long.

---

## 2. Speaker diarization and identification

### 2.1 Diarization options

| System | License **[V]** | Streaming | Max spk | Evidence **[V]** |
|---|---|---|---|---|
| pyannote `speaker-diarization-community-1` (pyannote.audio 4.0.x, MIT code) | weights CC-BY-4.0 (gated: contact-info click-through) | offline | unbounded | DER (no collar, overlap scored) for 3.1 → community-1: AMI-IHM 18.8→17.0, AMI-SDM 22.7→19.9, DIHARD3 21.4→20.2. Adds "exclusive" diarization for reconciling with ASR timestamps |
| pyannote `speaker-diarization-3.1` | MIT | offline | unbounded | widely deployed baseline |
| **Nemotron-3-Diarization** (NeMo) | **OpenMDW-1.1** | yes: 80 ms min, ≥0.32 s recommended; 30.4 s offline mode | **8** | released 2026-09-23; Sortformer-family; trained on ~10k h real data + 28k h simulated mixtures |
| Streaming Sortformer 4spk v2 / v2.1 | v2: CC-BY-4.0; v2.1: NVIDIA Open Model License | yes: 1.04 s low-latency config, RTF 0.093 on RTX 6000 Ada | 4 | arXiv:2507.18446 |
| DiariZen (BUT; WavLM + Conformer + VBx) | code MIT; checkpoint `diarizen-wavlm-large-s80-md` **CC-BY-NC-4.0** | offline | unbounded | 26.6% relative DER reduction vs pyannote 3 on AMI (arXiv:2409.09408) |
| MOSS-Transcribe-Diarize 0.9B | Apache-2.0 | offline (≤90 min) | many | joint ASR + diarization + events; vLLM `diarized_json` |
| Granite-Speech-4.1-2B-plus | Apache-2.0 | incremental via `prefix_text` | numbered | WDER: Fisher 0.9, CALLHOME 2.2, AMI-SDM 14.6 (vs VibeVoice 2.8/7.1/27.4) |

**[V]** Real-world egocentric diarization remains hard. The best Ego4D audio-visual diarization entries reached only **61-66% DER** (arXiv:2210.07764 and follow-ups). Independent pyannote tests show large missed-speech rates on some languages (soniqo.audio benchmark, secondary source). **[I]** The ISS setting is far-field, noisy, reverberant metal modules, with 2-7 speakers and frequent overlap. Expect DER well above the AMI numbers unless close-talk mics are used. This is the main reason for adding visual active-speaker evidence (§4.3).

### 2.2 Linking anonymous speakers to crew identities

**[V]** Speaker-embedding models:
- SpeechBrain ECAPA-TDNN `spkrec-ecapa-voxceleb`: Apache-2.0, VoxCeleb1 EER **0.80%**.
- NeMo TitaNet-Large: CC-BY-4.0, 23M params, EER **0.66%**.
- WeSpeaker toolkit: Apache-2.0.

**[I]** Recommended approach:
1. Enroll each crew member with 1-3 minutes of speech recorded *on board*, through the same mics and in the same noise. Store several centroid embeddings per person, for example one per module or mic.
2. For each diarized cluster, compute the cosine score against the enrolled centroids. Assign a name only above a calibrated threshold, chosen on held-out ISS-like data for a false-accept rate ≤1%. Otherwise label the speaker `unknown_speaker_k`.
3. Fuse with visual identity from the tracker (face or suit-patch re-ID) via active-speaker detection. Light-ASD and TalkNet are both MIT. Where audio and video disagree, flag the event for review rather than picking one.
4. Ground-to-crew audio (CAPCOM on space-to-ground loops) is a separate channel. Tag it by channel, not by voice.

### 2.3 Privacy

**[V]** 14 CFR 1214.501 says ISS crew personal information, "including all medical information, private family conference, or other private information", may not be used or disclosed without the crewmember's consent. Private medical conferences run over private audio/video links. **[I]** Voiceprints are biometric data. The POC should:
- (a) obtain explicit crew consent for enrollment;
- (b) keep embeddings and raw audio on-station or on-prem, never in third-party clouds. CIMON did route speech through IBM Cloud in Frankfurt; we should not;
- (c) store **transcript snippets plus event JSON**, not continuous raw audio, and give raw audio a short retention ring buffer;
- (d) provide a hard "privacy mode" mute, driven by schedule (PMCs, family conferences, sleep) and by voice ("privacy on");
- (e) log every identity assignment with the evidence used, for audit.

---

## 3. Audio event detection and classification

### 3.1 Models

| Model | License **[V]** | Type | Evidence **[V]** |
|---|---|---|---|
| PANNs (CNN14, Wavegram-Logmel) | MIT | AudioSet 527-class tagger + framewise SED | best mAP 0.439 (arXiv:1912.10211) |
| AST (`MIT/ast-finetuned-audioset-10-10-0.4593`) | BSD-3-Clause | tagger | 0.485 mAP ensemble; 95.6% ESC-50 (arXiv:2104.01778) |
| BEATs (microsoft/unilm) | MIT | SSL encoder + AudioSet fine-tunes | 50.6% mAP AS-2M (arXiv:2212.09058) |
| EfficientAT | MIT | mobile taggers | edge-friendly |
| YAMNet | Apache-2.0 (TF models) | MobileNet tagger, 521 classes | CPU-cheap baseline |
| LAION-CLAP (`laion/clap-htsat-unfused`) | Apache-2.0 weights (repo CC0) | **zero-shot** text-audio | ~88-89.5% zero-shot ESC-50, depending on prompt (arXiv:2211.06687 and follow-ups) |
| MS-CLAP 2023 | MIT | zero-shot | 93.9% zero-shot ESC-50 (msclap package) |
| FlexSED | (paper; code released) | **open-vocabulary SED with timestamps** via CLAP text encoder | AudioSet-Strong PSDS1A 0.448 vs Dasheng-SED 0.399 (arXiv:2509.18606) |
| Qwen3-Omni-30B-A3B-Captioner | Apache-2.0 | detailed audio captions | "low-hallucination" captioning, fine-tuned from Qwen3-Omni |
| MOSS-Transcribe-Diarize | Apache-2.0 | ASR + event tags | event awareness inside the transcript |

### 3.2 What sounds matter on the ISS (inference)

**[I]** Several Earth assumptions fail in microgravity:
- **Objects do not "drop."** A released tool floats. Impact sounds come from bumps against handrails or panels, not falls. Relevant ISS sound classes are Velcro rips (items are stowed on Velcro), zippers on CTBs, bungee snaps, drill and PGT whine, ratchets, torque-wrench clicks, cabinet and rack latches, hatch operations, and the T2 treadmill.
- **Alarms** are the highest-value class. The C&W tones (fire, rapid depressurization, toxic atmosphere) and caution tones are standardized, so a template-matching or small supervised detector trained on recordings of those tones is more reliable than any zero-shot model.
- **Fans dominate the background.** The ICES-2024 status report shows stalled-fan noise and unexplained low-frequency peaks as recurring exceedances. A background-change detector is free acoustic health monitoring. Bosch's **SoundSee** payload on Astrobee targeted exactly this: a 20-mic array, <100 Hz to 80 kHz. I could not find published SoundSee flight results.

**[I] Recommended detector:** a supervised BEATs or PANNs fine-tune on a small labeled ISS-sound set, plus CLAP zero-shot for the long tail. FlexSED is worth a trial when timestamps matter. Tool-sound events are *weak evidence*. "Drill sound at 14:02:11 near the Node 2 mic" can corroborate a visual "drill in hand" or a spoken "using the drill". It should not create an inventory event on its own.

---

## 4. Audio-visual fusion

### 4.1 Joint audio-video LLMs

**[V] Qwen3-Omni-30B-A3B** (Instruct/Thinking/Captioner, Apache-2.0; arXiv:2509.17765):
- **Audio encoder (AuT):** trained on 20M hours, 12.5 Hz tokens (80 ms each).
- **TM-RoPE:** gives audio one temporal ID per 80 ms and gives video frames IDs adjusted to their actual timestamps on the same 80 ms grid. This replaces Qwen2.5-Omni's fixed 2-s interleave chunks.
- **Inputs and benchmarks:** audio up to 40 min. WorldSense 54.0 (vs Gemini-2.5-Flash 50.9). DailyOmni 75.8 (Thinking). LibriSpeech clean/other 1.22/2.48.
- **Latency:** theoretical first-packet latency of 234 ms for audio and 547 ms for video.
- **Limitations stated by the authors:** weak long-video extrapolation. **Multi-speaker ASR is future work.** No timestamp or temporal-grounding evaluation.
- **Memory:** the card lists BF16 minimums of **78.9 GB for a 15-s video and 144.8 GB for 120 s** (transformers, Instruct with talker). It also says `use_audio_in_video` "is not available in vLLM serve; you can handle this by separately passing video and audio".

**[V] vLLM-Omni** `/v1/video/chat/stream` ([docs](https://github.com/vllm-project/vllm-omni/blob/main/docs/serving/video_stream_api.md)):
- The client sends `video.frame` (JPEG/PNG) and `audio.chunk` (PCM16 16 kHz) to separate buffers, then asks `video.query`.
- `use_audio_in_video` defaults to true.
- EVS drops near-duplicate frames.
- Incremental prefill reuses the vision prefix cache, but only in text-only sessions.
- When the audio buffer overflows, the server clears it ("Audio buffer overflow").

The Qwen card warns that `use_audio_in_video` must be kept consistent across turns.

**[V] Other open AV-LLMs:**
- video-SALMONN 2 (ByteDance, Apache-2.0 repo; arXiv:2506.15220): audio-visual captioning and QA.
- MiniCPM-o 4.5 (Apache-2.0): in vLLM-Omni full-duplex.
- Ming-flash-omni-2.0 (MIT).
- Nemotron-3-Nano-Omni-30B-A3B (NVIDIA Open Model Agreement): Parakeet audio encoder, video ≤2 min, audio ≤1 h, served by upstream vLLM with `use_audio_in_video`.
- Gemma-4 E2B/E4B (Apache-2.0): audio input in upstream vLLM.
- Qwen3.5-Omni (Mar 2026): appears **not** to have open weights. Secondary sources conflict, so treat it as unavailable.

**[V] Benchmarks:** WorldSense (ICLR 2026), Daily-Omni (cross-modal temporal reasoning, e.g. matching a voice to a gesture), and LVOmniBench (arXiv:2603.19217, 140 h of long AV video). Closest to our use case is **EgoLife** (CVPR 2025, arXiv:2503.03803): 300 h of six people living together, with an EgoLifeQA category for "locating misplaced items", built on an audio-visual model (EgoGPT) plus a retrieval memory (EgoRAG). Its architecture is a useful template: perception → timestamped memory → retrieval QA.

### 4.2 Early vs late fusion: recommendation

**[I]** Use a hybrid that is late fusion at the core:
- **Late fusion (system of record).** Separate streams, each with timestamps on the same clock:
  - visual tracker events: object ID, person ID, zone, enter/exit;
  - ASR words with word-level times (Qwen3-ForcedAligner gives ~32-43 ms average shift vs ~100+ ms for NFA or WhisperX, per the Qwen3-ASR report; Granite-plus reports similar);
  - diarization turns;
  - audio events.

  An event-fusion service joins them in a sliding window (±5-10 s) and writes candidate inventory events. This design is testable, debuggable and auditable per stream. That matters for NASA review.
- **Early fusion (on demand).** Send a 5-15 s clip (frames + audio) to Qwen3-Omni through `/v1/video/chat/stream` only for ambiguous cases. Examples: two candidate objects, an unresolved pronoun ("put *it* there"), or a disagreement between the speech and vision streams. Ask a narrow question: "Which tracked object (IDs 17, 23) is the speaker referring to when saying 'that camera'?" Overlay track IDs on the frames ("set-of-marks" prompting) so the model can answer with an ID.

Why not early fusion for everything:
- (a) Qwen3-Omni memory grows quickly with clip length, and it does not fit the T4s at all (report 01).
- (b) Its ASR on multiple overlapping speakers is acknowledged as weak.
- (c) Free-text answers from an omni model are hard to evaluate and to audit.

### 4.3 Using speech to resolve visual identity, and vice versa

**[I]** Patterns:
- **Ownership statements.** "That's Kate's camera" leads to an `ownership_assertion(object=track_23, owner=crew:kate)` with the speaker ID and timestamp. Resolve "that" by gaze, pointing or recency: the object most recently handled or in the speaker's hand within ±3 s. Escalate to Omni if there are several candidates.
- **Placement statements.** "I'm putting the drill in Node 2" leads to a candidate `move(object_class=drill, actor=speaker, dest=NOD2)`. Confirm it when the tracker sees a drill-class track leave the actor's hand in a Node 2 camera within N seconds. Otherwise keep it as an *unconfirmed* claim.
- **Active-speaker detection** (TalkNet or Light-ASD on face tracks) ties a voice to a visible person. With a single module camera plus a single mic, this is the most reliable way to name a diarized speaker who has not yet been enrolled.
- **Clock alignment.** All capture devices must share PTP/NTP time, and every frame and audio chunk must carry capture timestamps, not arrival times. The vLLM-Omni stream API reports `latest_pts_ms` for consumed frames, so pass real PTS values.

---

## 5. From transcript to structured inventory events

### 5.1 Pipeline

**[I]**
1. **Segmenter.** Group ASR words into utterances using diarization turns plus pauses.
2. **Speech-act and relevance filter.** A cheap classifier, a small LLM with `structured_outputs.choice`, labels each utterance as `{inventory_statement, question, command_to_system, plan_future, chit_chat, procedure_readback}`. Only `inventory_statement`, and `plan_future` with a lower prior, proceed. Clarissa's open-mic problem (§6.2) is exactly this.
3. **Slot filling.** An LLM with JSON-schema-constrained output fills the event. Inputs are the utterance, the previous 3-5 utterances (for coreference), the speaker identity, the current tracker context (objects near the speaker), and the controlled vocabulary.
4. **Normalization.** Map surface forms to canonical IDs, e.g. "Harmony", "Node two" and "N2" → `NOD2`, and "the big drill" → `item_class: pistol_grip_tool`. Use a lookup table plus fuzzy matching, with the LLM only as a fallback. GLiNER/GLiNER2 (Apache-2.0) are cheap span-extraction alternatives for entities.
5. **Reconciliation.** Join with vision events (§4.2). The output is `{confirmed | unconfirmed_claim | contradicted}` plus an evidence list.
6. **Write.** Insert into Postgres as an append-only event table, with the transcript span, audio offsets, speaker-ID confidence and model versions.

### 5.2 Example schema (used with vLLM `structured_outputs.json`)

```json
{
  "type": "object",
  "required": ["speech_act", "events"],
  "properties": {
    "speech_act": {"enum": ["inventory_statement","question","plan_future","command_to_system","chit_chat","procedure_readback"]},
    "events": {"type": "array", "items": {
      "type": "object",
      "required": ["action","object_mention","confidence"],
      "properties": {
        "action": {"enum": ["place","take","move","handover","lose","find","consume","assert_owner"]},
        "object_mention": {"type": "string"},
        "object_class": {"type": ["string","null"]},
        "actor": {"type": ["string","null"], "description": "crew id or 'speaker'"},
        "recipient": {"type": ["string","null"]},
        "from_location": {"type": ["string","null"], "description": "canonical location code"},
        "to_location": {"type": ["string","null"]},
        "owner": {"type": ["string","null"]},
        "tense": {"enum": ["past","present","future"]},
        "negated": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
      }}}
  }
}
```

**[V]** vLLM's structured outputs take `{"structured_outputs": {"json": schema}}` (also `choice`, `regex`, `grammar`, `structural_tag`), with xgrammar (Apache-2.0) or llguidance (MIT) as backends ([docs](https://github.com/vllm-project/vllm/blob/main/docs/features/structured_outputs.md)). The old `guided_json` field was removed in v0.12. **[V, report 01]** The vLLM-Omni chat path does not forward `response_format`. **[I]** So run extraction on a separate upstream-vLLM text model, for example a Qwen3 4-8B instruct model at FP16 or AWQ on one T4, or on the Spark. Another option is Qwen3-Omni's thinker served by upstream vLLM, where structured outputs should apply; this is unverified for Omni. Use enums for location codes when the vocabulary is closed. A schema-valid output can still be semantically wrong, so keep `negated` and `tense` fields to catch "I *didn't* put it in Node 2" and "I'll put it…".

### 5.3 Coreference

**[I]** Most errors will come from pronouns and ellipsis ("put it back", "the other one"). Mitigations:
- (a) give the extractor a rolling dialogue window plus the tracker's "objects in hand or near the speaker" list;
- (b) let the model output `object_mention="it"` with `object_class=null` rather than guess;
- (c) resolve deferred mentions in the fusion step from visual evidence;
- (d) send any remaining ambiguity to the Omni escalation path (§4.2) or the human review queue.

---

## 6. ISS and space specifics

### 6.1 Acoustic environment (verified from NASA ICES-2024-354, C. S. Allen)

**[V]** From [NTRS 20240006442](https://ntrs.nasa.gov/api/citations/20240006442/downloads/ICES-2024-354%20ISS%20Acoustics.pdf):
- **Requirements:** NC-50 for modules (vehicle only) and NC~52 including payloads. Crew quarters NC-40. Equipment NC-40, payloads NC-34.
- **Measured levels (sound level / SIL(4)):**
  - US Lab: ~60 dBA / ~53 dB.
  - JEM: 55.6 dBA / 47.6 dB, with a 63 Hz peak that raised it to NC-53.5.
  - Node 2: 57.5 dBA / 47.4 dB. Node 2 has exceeded NC-50 since April 2015, attributed to stalling IMV fans.
  - Cupola: 49.2 dBA.
  - Service Module: ~62-63 dBA.
  - Crew quarters: 49-54 dBA with fans on high.
  - MLM (Nauka): high noise from cooling pumps.
- **Dominant sources:** ventilation fans and airflow, plus pumps.

Older NASA papers report 56-69 dBA module backgrounds and 77 dBA during treadmill exercise, with hearing protection required at a 24-h crew-worn level of ≥67 dBA (NTRS 20100031889, 20110013006). **[I]** Normal conversational speech is about 60 dBA at 1 m. Against a 47-53 dB SIL, the SNR in the speech band is about 5-15 dB at 1 m and 0-10 dB at 2-3 m, and worse near the treadmill or in the SM or MLM. The fan noise is broadband and stationary, with low-frequency tonal peaks. That is easier for denoisers than babble, but continuous fan noise keeps mics active (VAD false triggers) and invites Whisper-style hallucinations. Module interiors are hard-walled and reverberant, so far-field effects (FFASR) apply.

### 6.2 Prior NASA and partner speech systems: lessons

- **Clarissa (NASA Ames, on ISS from Jan 2005) [V]:** a voice procedure navigator with a ~260-word vocabulary and ~75 commands, using the open-source Regulus grammar toolkit. Main findings:
  - the central problem was **open-mic addressee detection**: telling commands apart from crew conversation. It was solved with grammar filtering plus SVM classifiers;
  - undo, correction and confirmation were essential;
  - a press report put open-mic confusion at ~10%.

  ([ACL 2005 demo](https://preview.aclanthology.org/fix_video/P05-3008.pdf), [ICSI TR-05-005](https://www1.icsi.berkeley.edu/ftp/pub/techreports/2005/tr-05-005.pdf), [NTRS 20050240872](https://ntrs.nasa.gov/citations/20050240872)).
- **CIMON / CIMON-2 (DLR, Airbus, IBM; 2018-2020) [V]:** used Watson Speech-to-Text in IBM Cloud in Frankfurt, routed through the ground (1-2 s latency after optimization). CIMON-2 got "more sensitive microphones". CIMON-1 famously did not stop playing music when told to. **[I]** Lessons: cloud dependence and the ground round-trip are unacceptable for us, and command handling must be robust. Our POC must be fully on-prem and open source.
- **Callisto on Artemis I (Lockheed Martin, Amazon, Cisco; 2022) [V]:** a local, offline Alexa on a COTS single-board computer, integrated with Orion telemetry. It completed 21 operational sessions over the 25.5-day mission, driven by "virtual crew" in MCC because Artemis I had no crew. No independent accuracy report was found. **[I]** It shows that offline on-board voice is feasible, but it gives no evidence about noisy, multi-speaker conditions.
- **Astrobee / SoundSee (Bosch) [V]:** a 20-mic array payload for acoustic monitoring, launched Nov 2019. Flight results were not found in public sources.
- **Crew comms [V]:** crew talk to the ground through Audio Terminal Units, either fixed wall panels or tethered headsets. NASA has evaluated wireless badges (Vocera) for ISS ([NTRS 20120009451](https://ntrs.nasa.gov/citations/20120009451)). **[I]** ATU and headset audio, if NASA can tap it, is near-field and high-SNR. It is ideal for ASR but covers only loop traffic. Cabin conversation needs dedicated ambient mics.
- **Inventory context [V]:** the ISS IMS relies on manual barcode scans. One HBS case summary cites ~3,000 of ~64,000 active items as "lost". RFID handhelds and the REALM experiment reached ~1.5 m mean localization error. **[I]** Voice plus vision is complementary to RFID, not a replacement. Spoken "I put X in Y" claims could be cross-checked against RFID reads where they exist.

---

## 7. Evaluating the audio path

### 7.1 Metrics and tools

| Component | Metric | Tool (license) **[V]** |
|---|---|---|
| ASR | WER/CER, plus **domain-term recall/F1** (module, tool and crew names) | jiwer (Apache-2.0); compute keyword F1 the way the Granite card does |
| Speaker-attributed ASR | cpWER, tcpWER, WDER | meeteval (MIT) |
| Diarization | DER (no collar, overlap scored), JER | pyannote.metrics (MIT), dscore (BSD-2) |
| Speaker ID | EER, FAR at the chosen operating point, identity accuracy per event | sklearn |
| Word timing | mean absolute word-boundary shift (AAS) | custom |
| Sound events | event-based F1 (onset ±200 ms), PSDS | sed_eval (MIT), sed_scores_eval (MIT) |
| Speech-act filter | precision/recall on `inventory_statement` | custom |
| Extraction | slot-level P/R/F1 (action, object, from, to, actor); exact-event match; **end-to-end inventory-state accuracy** after replay | custom harness |
| Latency | speech end → event in Postgres, p50/p95 | Prometheus |

**[I]** The headline metric is **end-to-end inventory accuracy**: after replaying a scripted session, what fraction of objects have the correct location, holder and last-moved-by values in Postgres. Report per-stage metrics as diagnostics.

### 7.2 Building test data

**[I]** Recommended tiers:
1. **Synthetic speech.** Script hundreds of inventory utterances covering placements, handovers, negations, questions, future plans, pronouns and code-switched English/Russian, and render them with open TTS:
   - Qwen3-TTS (Apache-2.0)
   - CosyVoice3 (Apache-2.0)
   - VoxCPM2 (Apache-2.0)
   - Kokoro-82M (Apache-2.0)
   - Chatterbox (MIT)

   Several of these are served by vLLM-Omni itself (CosyVoice3, VoxCPM2, OmniVoice). Avoid F5-TTS weights (CC-BY-NC). Vary accent and speaker. Do *not* clone real crew voices without consent.
2. **Noise and room mixing.**
   - Simulate ISS-module acoustics with pyroomacoustics (MIT): small cylindrical or box rooms, hard walls, Treble10 RIRs as an alternative.
   - Add noise shaped to the measured ICES octave-band spectra: generate fan-like broadband noise matched to the NC-50/SIL ~47-53 dB curves, plus tonal peaks at 63 Hz and harmonics, plus stalled-fan and pump events.
   - Mix at SNR ∈ {15, 10, 5, 0, -5} dB with audiomentations or torch-audiomentations (MIT).
   - Use MUSAN or DNS-Challenge noise (CC-BY-4.0) for extra variety.
   - Get real ISS cabin recordings from NASA. Chris Hadfield's public ISS ambience recordings exist, but their license is unclear, so do not use them without permission.
3. **Overlap and multi-speaker.** Simulate 2-4 speaker conversations with partial overlap (lhotse, Apache-2.0; NeMo's simulator) to stress diarization.
4. **Analog recordings.** Record people acting the scripts in a mock module, a noisy server room or lab with fans at ~60 dBA, with the real cameras and mics, and with the tracker running. Ground-truth event logs come from the script plus an annotator.
5. **Sound events.** Record Velcro, zippers, drills/PGTs, ratchets, latches and C&W tone recordings, and mix them into the scenes with exact onset labels.

**[I]** Keep a fixed, versioned test set and evaluate every model swap against it. ISS-noise robustness is the main unknown, and leaderboard numbers will not predict it.

---

## 8. Recommended audio pipeline for the POC

**[I]** All components are open source. Licenses were verified above.

```
 mics (per module, PTP-timestamped) ──► VAD (Silero, MIT) ──► ring buffer (raw audio, short retention)
        │
        ├─► Streaming ASR: Voxtral-Mini-4B-Realtime @480 ms  (vLLM /v1/realtime, Apache-2.0)
        │       └─ for live captions + low-latency triggers
        │
        ├─► Segment ASR (per VAD utterance, 5–30 s): Qwen3-ASR-1.7B with context=<ISS lexicon>
        │       + Qwen3-ForcedAligner-0.6B word times  (vLLM / qwen-asr, Apache-2.0)
        │       [alt: Granite-Speech-4.1-2B keyword biasing; A/B on ISS-noise set]
        │
        ├─► Diarization: pyannote community-1 (offline windows, 30–60 s)
        │       [streaming alt: Nemotron-3-Diarization, OpenMDW] 
        │       [one-pass alt to evaluate: MOSS-Transcribe-Diarize → vLLM diarized_json]
        │     └─► Speaker ID: ECAPA (SpeechBrain) vs enrolled crew centroids + ASD from video (Light-ASD)
        │
        ├─► Audio events: BEATs/PANNs fine-tune (alarms, tools, Velcro, zippers) + CLAP zero-shot long tail
        │
        └─► Event fusion service (timestamps, ±5–10 s window)
                ├─ speech-act filter (small LLM, structured_outputs.choice)
                ├─ slot filling (LLM on upstream vLLM, structured_outputs.json + enums)
                ├─ join with tracker events (object/person/zone) → confirmed | unconfirmed | contradicted
                ├─ escalation: ambiguous cases → Qwen3-Omni /v1/video/chat/stream (frames w/ track-ID overlays + audio clip)
                └─ Postgres: append-only events + evidence (transcript span, audio offsets, confidences, model versions)
```

Placement on hardware:
- **T4 #1:** Qwen3-ASR-1.7B plus the forced aligner, FP16.
- **T4 #2:** Voxtral-Realtime. A 4B model in FP16 is a tight fit with KV cache; fall back to Qwen3-ASR-0.6B realtime if it is too large.
- **T4 #3:** pyannote, ECAPA, BEATs and CLAP. These are small and could share with #2.
- **T4 #4:** the extraction LLM (4-8B, AWQ/FP16).
- **DGX Spark:** Qwen3-Omni-30B-A3B for escalations and the vision-language work described in reports 01 and 06.

Operating rules:
- Every inventory write must carry evidence.
- Speech-only claims stay `unconfirmed` until vision corroborates them or a human approves.
- A privacy mute is honored at the mic layer.
- No audio leaves the enclave.

---

## 9. Open questions

1. **Mic topology and access.** Will NASA give us the ATU/headset audio taps, ambient cabin mics, or both? How many mics per module, and are any of them arrays? This choice changes the SNR far more than any model swap.
2. **Real ISS noise recordings.** Can NASA (the JSC Acoustics Office) supply calibrated per-module recordings for test-set mixing? We currently use synthetic noise shaped to the ICES-2024 spectra.
3. **Official vocabularies.** We need the IMS stowage location grammar, the item nomenclature, and the crew roster with name pronunciations, for biasing and normalization.
4. **License policy.** Do NVIDIA Open Model License/Agreement models (Nemotron streaming ASR, Sortformer v2.1, Nemotron-3-Nano-Omni) and OpenMDW-1.1 (Nemotron-3-Diarization) count as "open source" for this program? CC-BY-4.0 attribution is required for Parakeet, Canary and pyannote community-1.
5. **ASR on T4 throughput.** Qwen3-ASR, Granite and Voxtral cards assume FlashAttention-2 and BF16. FP16/SDPA speed and numerical stability on SM 7.5 have not been measured.
6. **Qwen3-Omni structured outputs.** Upstream vLLM serving the Qwen3-Omni thinker should accept `structured_outputs`, but this is unverified. vLLM-Omni does not forward it (report 01).
7. **Diarization in reverberant metal modules with 2-7 speakers.** Expected DER is unknown. Ego4D-style results (61-66% DER) suggest it may be poor without close-talk mics. Measure it on the analog-recording tier.
8. **Multilingual behavior.** How well do biasing and extraction handle English/Russian code-switching and Russian module names? None of the biasing evaluations cover this.
9. **Consent and retention.** What is the crew-approved policy for voiceprint enrollment, raw-audio retention, and automatic privacy windows (PMCs, family conferences)? Who can audit identity assignments?
10. **Speech-act accuracy.** Can a small LLM separate "I put it in Node 2" from "should I put it in Node 2?" and from procedure read-backs at >95% precision? Clarissa's 10% open-mic confusion is the baseline to beat.
11. **FFASR leaderboard values.** The live far-field leaderboard could not be read, so check it directly before the final ASR choice.
12. **SoundSee results.** Ask Bosch and NASA whether SoundSee's on-orbit acoustic data or findings are available. They would be the best real ISS non-speech audio corpus.
