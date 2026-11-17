# GB10 (Dell Pro Max / DGX Spark class) single-node profile: UNTESTED

> **This is the OpenShift / MicroShift variant, which is no longer the deployment path.** The GB10 runs the POC
> on Podman: see [`deploy/README.md`](../../deploy/README.md#gb10-production-target--untested). The hardware
> facts, the model-by-model comparison against the T4 hosts, the memory budget and the verification checklist
> below all still apply — only the `oc apply` section at the end is superseded.

Production target: one Dell GB10 on the ISS.
- **GPU:** Blackwell (sm_121), with bf16, FP8 and NVFP4 support.
- **Memory:** 128 GB LPDDR5x unified (shared by CPU and GPU), 273 GB/s.
- **CPU:** 20 Arm cores.

This profile is written from `research/07-us-models.md` §5b. **It has not been run on GB10 hardware.** Verify every
flag on the box first; the checklist is at the end.

## What changes versus the T4 test cluster
| | T4 cluster (tested) | GB10 (this profile) |
|---|---|---|
| Perception omni | Gemma 4 12B **fp32** (fp16 overflows, no bf16), TP4 | Gemma 4 12B **FP8** (`RedHatAI/gemma-4-12B-it-FP8-Dynamic`), TP1, native bf16 activations |
| Chat | separate Gemma 4 12B QAT W4A16 on its own T4 | **the same Gemma 4 server** (one model for perception and chat; saves about 13 GB and keeps decode bandwidth in one engine) |
| ASR | Whisper large-v3-turbo, own T4 | Whisper large-v3-turbo, small memory fraction |
| Decision | DiffusionGemma INT8 TP4 plus sm75 patches, `--no-constrained` | DiffusionGemma INT8 **TP1**, no patches; constrained label reads are allowed again at TP1 |
| OWLv2 + DINOv2 | own T4 (CUDA torch) | in the perception pod on the same GPU |
| GPU sharing | whole GPUs per pod | **one GPU**, time-sliced (device-plugin replicas), memory split with `--gpu-memory-utilization` fractions |
| Sampling stride | 30 s (about 60 s/window in fp32) | start at 12 s and tune; estimate 15-30 s/window in FP8 |

## Memory budget (fractions of 128 GB; vLLM total ≤ 0.85, leaving room for OS, Postgres and app)
| Process | Fraction | ~GB | Notes |
|---|---|---|---|
| gemma4 (perception + chat) | 0.24 | 31 | about 13 GB FP8 weights plus KV for 2-4 sequences at 16k |
| dgemma | 0.30 | 38 | about 27 GB INT8 weights plus KV at 4k |
| asr (whisper) | 0.05 | 6 | |
| perception (torch, OWLv2 + DINOv2) | n/a | about 3 | not vLLM-managed |
| OS, Postgres, RustFS, app, dashboard | n/a | about 20 | |
| **Total** | **0.59 vLLM** | **about 98** | about 30 GB headroom |

## Apply (MicroShift or single-node OpenShift on the GB10)
```bash
oc apply -f time-slicing.yaml          # after the NVIDIA GPU operator / device plugin is installed
oc apply -k .                          # data + app tiers from ../, inference from inference-gb10.yaml
```

## Verify on the box (checklist)
1. `nvidia-smi` shows the GB10. The vLLM startup log shows sm_121 and CUDA 13. Pin the image digest (the vLLM DGX
   Spark blog uses `cu130-nightly`).
2. Gemma 4 FP8 loads, and text, image and audio smoke tests pass. Reuse `/tmp`-style probes from QUESTIONS #30-31:
   - text: name three ISS modules;
   - image: one NASA tour frame;
   - audio: "Moving the drill to node one".
3. **Time-sliced pods** start together, and the `--gpu-memory-utilization` fractions sum to ≤ 0.85.
4. Run `eval/probes/omni_ab.py` on 8 tour windows, then `make eval` gates against the live stack.
5. **Measure the stride:** the omni usage logs give completion tokens and latency per window. Set
   `PERCEPTION_STRIDE_S` so one window's processing time is at most the stride.
6. **Optional bake-off:** Nemotron 3 Nano Omni NVFP4 (one model for perception, ASR and chat), as in research
   §5b option B.
