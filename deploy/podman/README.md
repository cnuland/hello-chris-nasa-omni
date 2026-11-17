# Podman deployment (GB10 target; tested on Azure T4 VMs)

NASA's Dell GB10 runs NVIDIA's standard Ubuntu-based software stack, so the POC runs as Podman containers managed by
systemd (Quadlet). Nothing replaces the host OS.

| Tier | How it runs | Files |
|---|---|---|
| Data: Postgres 16 + pgvector, RustFS (S3) | `podman kube play` of Kubernetes YAML, one pod per service | `kube/data.yaml`, `quadlet/nasa-data.kube` |
| Application: inventory API, agent + MCP server, dashboard (+ TLS) | same | `kube/app.yaml`, `quadlet/nasa-app.kube` |
| Inference: gemma4, dgemma (+ decision server), asr, agent-llm | Quadlet container units with `AddDevice=nvidia.com/gpu=...` (CDI) | `quadlet/*.container`, `config/vllm-start.sh` |
| Perception | Quadlet container unit, installed but not started | `quadlet/perception.container` |
| Memory compression ("dreaming") | systemd timer, every 6 h | `quadlet/agent-dreaming.{service,timer}` |

All services join one Podman network (`nasa`) and find each other by name. Only the dashboard's TLS port (443) is
published for users.

## Why the GPU servers are not kube YAML

`podman kube play` in Podman 4.9 (Ubuntu 24.04) cannot attach CDI devices: neither `nvidia.com/gpu` limits nor CDI
annotations reach the container. Quadlet `.container` units can (`AddDevice=`). Newer Podman may allow moving them into
kube YAML.

## Install

```bash
scripts/host-setup.sh                      # Podman, NVIDIA toolkit, CDI spec (reboot once if it installs the driver)
# single host (GB10): every model server on this host
scripts/install-inference.sh gemma4 dgemma dgemma-decision asr
scripts/install-app.sh [--restore-db inventory.dump --restore-objects objects.tgz]
```

On the GB10 the chat endpoint defaults to the `gemma4` server (one Gemma 4 server answers perception and chat), so
`agent-llm` is not installed. Secrets are generated on the host at install time: in Podman's secret store and root-only
files under `/etc/nasa`, never in the bundle.

## The T4 development layout used for the demo recording (2026-10-21)

T4s have no bf16, so Gemma 4 runs in fp32 and needs a whole 4x T4 VM. Three Ubuntu 24.04 VMs stood in for one GB10:

| Host | GPUs | Units |
|---|---|---|
| omni (NC64as_T4_v3) | 4x T4 | `gemma4` (TP 4, fp32) |
| decision (NC64as_T4_v3) | 4x T4 | `dgemma` (TP 4) + `dgemma-decision`; `asr` on GPU 3 (port 8001) |
| app (NC16as_T4_v3) | 1x T4 | data + app tiers, `agent-llm` (Gemma 4 QAT W4A16) |

```bash
scripts/install-app.sh --omni http://<omni>:8000 --decision http://<decision>:8011 --asr http://<decision>:8001 \
  --chat http://agent-llm:8000/v1 --chat-model agent
```

The `.container` files are the T4 variants (fp32, tensor-parallel 4, sm75 start-up patches). For the GB10, switch
`gemma4` to `RedHatAI/gemma-4-12B-it-FP8-Dynamic` with TP 1 and the memory fractions in `../gb10/README.md`, and give
every unit `AddDevice=nvidia.com/gpu=all`.

## Issues found on the T4 VMs (fixed in these files)

- `ubuntu-drivers install --gpgpu` installs no `nvidia-smi`: `host-setup.sh` adds `nvidia-utils-<ver>-server`.
- NVIDIA Container Toolkit 1.20 writes `additionalGids` into the CDI spec, which Podman 4.9 rejects
  ("unresolvable CDI devices"): `host-setup.sh` strips it.
- Podman 4.9 Quadlet has no `Entrypoint=` key: units use `PodmanArgs=--entrypoint=...`.
- The RustFS image runs as uid 10001: restored object data must be owned by it.
- Whisper and the 12B chat model do not fit on one 16 GB T4 together; Whisper moved to the decision host's GPU 3.
