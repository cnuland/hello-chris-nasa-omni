#!/bin/bash
# Start vLLM with the pre-Ampere (T4, sm75) patches used on the development cluster. On Ampere and newer GPUs,
# including the GB10's Blackwell, the patched branches never run. Optional: VLLM_AUDIO_EXTRAS=1 installs audio decoders.
set -e
if [ "${VLLM_AUDIO_EXTRAS:-0}" = 1 ]; then pip install --quiet --no-cache-dir 'librosa>=0.10' 'soundfile>=0.12' || echo 'WARNING: audio extras install failed'; fi
F=/usr/local/lib/python3.12/dist-packages/vllm/v1/attention/ops/triton_unified_attention.py
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
anchor = "        launch_num_stages = 2\n"
patch = anchor + "    if not current_platform.has_device_capability(80):  # T4/sm75: 64KB smem per block\n        launch_num_stages = 1\n"
anchor2 = "        TILE_SIZE_DECODE = min(TILE_SIZE_DECODE, block_size)\n"
patch2 = anchor2 + "    if not current_platform.has_device_capability(80):  # T4/sm75 tile: head_size 512 x 32-token K/V tiles need 96KB\n        TILE_SIZE_PREFILL = min(TILE_SIZE_PREFILL, 16)\n        TILE_SIZE_DECODE = min(TILE_SIZE_DECODE, 16)\n"
if "T4/sm75" not in s:
    if anchor in s and anchor2 in s:
        s = s.replace(anchor, patch, 1).replace(anchor2, patch2, 1); open(p, "w").write(s)
        print("patched triton_unified_attention for sm75 (1 stage, 16-token tiles)")
    else:
        print("WARNING: sm75 patch anchors not found in this vLLM build - running unpatched")
PY
S=/usr/local/lib/python3.12/dist-packages/vllm/v1/sample/ops/topk_topp_sampler.py
python3 - "$S" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read(); a = "    if HAS_TRITON:\n        return apply_top_k_top_p_triton(logits, k, p)\n"
if a in s and "sm75 sampler" not in s:
    s = s.replace(a, "    from vllm.platforms import current_platform  # sm75 sampler: pytorch path\n"
                     "    if HAS_TRITON and current_platform.has_device_capability(80):\n        return apply_top_k_top_p_triton(logits, k, p)\n", 1)
    open(p, "w").write(s); print("patched top-k/top-p sampler: pytorch on sm75")
PY
exec vllm serve "$@"
