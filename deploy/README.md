# Podman deployment

The POC runs as Podman containers. On a POC host they are managed by systemd through **Quadlet**, so the stack
comes back after a reboot; nothing replaces the host OS. Target hosts are **Ubuntu 22.04/24.04** (NVIDIA's
standard GB10 software stack) and **RHEL 9/10**. The same manifests run with plain `podman kube play` on a
developer machine — see [Local development](#local-development-macos-or-linux-same-command).

| Tier | How it runs | Files |
|---|---|---|
| Data: Postgres 16 + pgvector, RustFS (S3) | `podman kube play`, one pod per service | `kube/data.yaml`, `quadlet/nasa-data.kube` |
| Application: inventory API, agent + MCP server, dashboard | same | `kube/app.yaml`, `quadlet/nasa-app.kube` |
| TLS front door (:443, the only public port) | same | `kube/tls.yaml`, `quadlet/nasa-tls.kube` |
| Inference: gemma4, dgemma (+ decision server), asr, agent-llm | Quadlet `.container` units with CDI GPUs | `quadlet/*.container`, `config/vllm-start.sh` |
| Perception | Quadlet container unit, installed but not started | `quadlet/perception.container` |
| Memory compression ("dreaming") | systemd timer, every 6 h | `quadlet/agent-dreaming.{service,timer}` |

Everything joins one Podman network (`nasa`) and finds everything else by name through aardvark-dns.

## Conventions

These are the rules the manifests follow. They are what make the same files work rootless on a laptop, as root
under Quadlet on a POC host, and under `sudo podman` for an operator debugging the running system.

1. **The kube YAML is the single source of truth for topology.** The Quadlet `.kube` units point at the same
   files that `podman kube play` uses, so there is no second copy of the layout to keep in step.
2. **No `hostPath`.** State is in PVCs, which podman backs with named volumes (`nasa-pgdata`, `nasa-objects`,
   `nasa-media`). A host path is used only where an operator genuinely needs filesystem access: the model
   weight cache at `/var/lib/nasa/models`, which is tens of gigabytes.
3. **Config travels in the manifests.** The Postgres init script and the TLS proxy config are ConfigMaps inside
   `kube/*.yaml`, so they are version-controlled and nothing has to be staged on the host first. The one
   exception is `nasa-endpoints`, passed with `--configmap`, because the model URLs differ per host.
4. **Secrets live only in podman's secret store.** `deploy/scripts/install-app.sh` generates them and pipes them into
   `podman kube play -` on stdin; no plaintext or base64 file is ever written. The TLS key is a secret volume,
   not a mounted host file.
5. **Every container has a `livenessProbe`,** which podman installs as the container healthcheck, so
   `podman ps` tells you the truth about what is actually serving.
6. **Scripts take `$PODMAN`,** so one code path covers the rootless store (`podman`) and the root store that
   systemd units run from (`sudo podman`).
7. **Runtime images are pinned**: RustFS by digest, the rest by explicit tag. The one gap is the Containerfile
   base image `ubi9/python-311:latest`, which still floats; pin it to a dated tag before any flight use.
8. **Every image is multi-arch.** The GB10 is Arm, and so is an Apple Silicon laptop, so an amd64-only image
   breaks the deployment rather than merely slowing it down. When pinning by digest, pin the **manifest list**,
   not an architecture's image: a per-arch digest is immutable but unrunnable everywhere else.

Verified with `podman manifest inspect` (2026-11-18):

| Image | Architectures |
|---|---|
| `pgvector/pgvector:pg16` | amd64, arm64 |
| `rustfs/rustfs@sha256:9ca95595…` (1.0.3 list) | amd64, arm64 |
| `nginxinc/nginx-unprivileged:1.27-alpine` | 386, amd64, arm, arm64, ppc64le, riscv64, s390x |
| `vllm/vllm-openai:v0.31.0` | amd64, arm64 |
| `ubi9/python-311` (our service images) | amd64, arm64, ppc64le, s390x |
| `node:24-alpine` (dashboard build) | amd64, arm64, s390x |
| `busybox:stable`, `ubuntu:24.04` (helpers) | amd64, arm64, + others |

## Install on a POC host

All commands are run from the repository root.

Three scripts, in order:

```bash
# 1. podman, NVIDIA driver + toolkit, CDI spec. Reboot and re-run if it installs the driver.
deploy/scripts/host-setup.sh

# 2. Model servers. The hardware profile comes from the GPU count; --profile gb10|t4 overrides it.
deploy/scripts/install-inference.sh gemma4 dgemma dgemma-decision asr

# 3. Data + application + TLS tiers. Builds any missing service images into the root store first.
deploy/scripts/install-app.sh
```

`install-app.sh` builds `inventory-api`, `agent` and `dashboard` itself if they are not already in root's image
store, so there is no separate build step. `--no-build` skips it, `--build` forces a rebuild, and
`--with-perception` also builds the perception image (slow: a CUDA torch wheel). Perception is installed but
not started either way, so its image can wait until a camera feed is attached.

`install-app.sh` is idempotent and takes `--restore-db inventory.dump --restore-objects objects.tgz` to come up
on an existing demo baseline. On the GB10 the chat endpoint defaults to the `gemma4` server (one Gemma 4 server
answers both perception and chat), so `agent-llm` is not installed.

### Upgrading a host that ran the first Podman bundle

The first version of this bundle kept state in host directories and the credentials in a base64 file. Both have
moved, so an existing host needs one migration step before `install-app.sh`:

```bash
# 1. with the old stack still running, take a baseline
PODMAN="sudo podman" scripts/db-snapshot.sh save pre-upgrade.dump
sudo tar -czf pre-upgrade-objects.tgz -C /var/lib/nasa/objects .

# 2. stop the old units, then install and restore into the new volumes
sudo systemctl stop nasa-app nasa-data
deploy/scripts/install-app.sh --restore-db pre-upgrade.dump --restore-objects pre-upgrade-objects.tgz
```

`install-app.sh` deletes the old `/etc/nasa/secrets.yaml`, `/etc/nasa/db-init` and `/etc/nasa/tls-proxy.conf`,
because the credentials now live only in podman's secret store and the config is in the manifests. The old
`/var/lib/nasa/postgres` and `/var/lib/nasa/objects` directories are left untouched; delete them once the
restored stack looks right.

### Day-to-day

```bash
sudo systemctl status nasa-data nasa-app nasa-tls
sudo journalctl -fu nasa-app
sudo systemctl start perception                             # once a camera feed is attached
PODMAN="sudo podman" scripts/load-sample-video.sh tour.mp4
PODMAN="sudo podman" scripts/db-snapshot.sh save baseline.dump
PODMAN="sudo podman" scripts/reset-observations.sh --yes
```

### TLS

`install-app.sh` generates a self-signed certificate into the `nasa-tls` podman secret. Replace it with a real
one and restart the front door:

```bash
sudo podman secret rm nasa-tls
printf 'apiVersion: v1\nkind: Secret\nmetadata: {name: nasa-tls}\ndata:\n  tls.crt: %s\n  tls.key: %s\n' \
  "$(base64 -w0 your.crt)" "$(base64 -w0 your.key)" | sudo podman kube play -
sudo systemctl restart nasa-tls
```

nginx resolves the dashboard pod once at start-up, so `nasa-tls` is `PartOf=nasa-app`: restarting the
application tier restarts the front door and the address is re-resolved.

## GB10 (production target) — UNTESTED

One Dell GB10: Blackwell (sm_121) with bf16/FP8/NVFP4, 128 GB unified LPDDR5x at 273 GB/s, 20 Arm cores. Every
model server shares the single GPU, so each gets a memory fraction rather than a whole device. This profile is
written from `research/07-us-models.md` §5b and **has not been run on GB10 hardware** — verify every flag on the
box first.

| | T4 development hosts (tested) | GB10 (this profile) |
|---|---|---|
| Perception omni | Gemma 4 12B **fp32** (no bf16 on T4), TP 4 | **FP8** (`RedHatAI/gemma-4-12B-it-FP8-Dynamic`), TP 1 |
| Chat | separate Gemma 4 12B QAT W4A16 on its own T4 | **the same gemma4 server** (saves ~13 GB, one decode engine) |
| Decision | DiffusionGemma INT8 TP 4 + sm75 patches, `--no-constrained` | INT8 **TP 1**, no patches, constrained label reads allowed |
| OWLv2 + DINOv2 | own T4 | in the perception container, same GPU |
| GPU sharing | whole GPUs per unit | one GPU, split by `--gpu-memory-utilization` |
| Sampling stride | 30 s | start at 12 s and tune |

Memory budget as fractions of 128 GB (vLLM total ≤ 0.85, leaving room for the OS, Postgres and the app tier):
`gemma4` 0.24 (~31 GB) · `dgemma` 0.30 (~38 GB) · `asr` 0.05 (~6 GB) · perception torch ~3 GB ·
OS + Postgres + RustFS + app ~20 GB → **~98 GB, about 30 GB headroom**.

No hand-editing is needed: `quadlet/gb10/*.container` holds these settings and
`deploy/scripts/install-inference.sh` picks them automatically when it sees one GPU (override with
`--profile gb10|t4`). Installing the T4 units on a GB10 fails four ways at once - tensor-parallel 4 on one
GPU, `asr` pinned to a GPU 3 that does not exist, `gemma4` and `dgemma` both claiming host port 8000, and
memory fractions summing past the box - which is why this is a profile rather than a note to remember.

The full verification checklist, and the OpenShift/MicroShift variant of the same profile, are in
[`../k8s/gb10/README.md`](../k8s/gb10/README.md).

## Local development (macOS or Linux, same command)

No systemd, no root, no GPU. The data and application tiers run from the same `kube/*.yaml` the POC host
uses. `stack.sh up` runs a preflight that checks the podman version and, on macOS, starts the podman VM,
so the command is identical on an Apple Silicon laptop, an Intel Mac, Ubuntu and RHEL:

```bash
scripts/stack.sh up --build
scripts/stack.sh status
```

The dashboard is on <http://localhost:8080/>. Inventory, review queue, add/edit and the overview all work. Chat
needs a model server: point the stack at one with `scripts/stack.sh up --endpoints my-endpoints.yaml`.

`scripts/stack.sh down --volumes` removes the database and object store as well as the pods.

## Why the GPU servers are not kube YAML

`podman kube play` on Podman 4.9 (Ubuntu 24.04) cannot attach CDI devices: neither `nvidia.com/gpu` limits nor
CDI annotations reach the container. Quadlet `.container` units can, with `AddDevice=`. The model servers are
also the one tier with no reason to be a pod — each is a single container with its own GPU budget.

## Podman version

The manifests and units are written for **Podman 4.9** (what Ubuntu 24.04 ships) and run unchanged on Podman 5.
Two places where that costs something, both marked in the files:

- `.container` units set the entrypoint with `PodmanArgs=--entrypoint=...`; the Quadlet `Entrypoint=` key needs
  Podman 5.3.
- Units do not use `Notify=healthy` (Podman 5.0+) for start-up ordering, so `install-app.sh` polls for
  readiness between tiers instead.

## Issues found on real hardware (fixed in these files)

Found on the GB10 (Ubuntu, Arm):

- **`pinging container registry localhost: connection refused`** when starting `nasa-app`. Rootless podman and
  root podman have separate image stores. Quadlet units run as root, so images built with a plain
  `scripts/build-images.sh` are invisible to them; podman then treats `localhost` as a registry hostname and
  reports what looks like a network fault. The locally built images now carry `imagePullPolicy: Never`, so the
  error at least says "image not known", and `install-app.sh` checks the root store up front and tells you to
  build with `PODMAN="sudo podman"`.

- **`set: Illegal option -o pipefail`.** Ubuntu's `/bin/sh` is dash, so `sh somescript.sh` ignores the shebang
  and the strictness line fails immediately. Every script now re-execs itself under bash
  (`[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"`), so it behaves the same however it is invoked —
  including when the executable bit was lost copying the bundle over.
- **`Error occurred resolving path "/usr/share/containers/systemd"`.** A bare `quadlet -dryrun` scans the
  distro search paths and exits 1 if any is missing; Ubuntu's podman package ships neither
  `/usr/share/containers/systemd` nor `/run/containers/systemd`, so the pre-flight check aborted the install
  on a perfectly good set of units. The installers now run the check hermetically with `QUADLET_UNIT_DIRS`
  against a staging directory, which skips the distro paths and validates only the units being installed.
- **Silent "Unit not found" later on.** The installers now call `nasa_quadlet_verify` after `daemon-reload` to
  confirm the generator really produced the units, with the remedy in the error message.
- **An amd64-only image on an Arm box.** RustFS was pinned to a per-architecture digest; see conventions 8.

Found on the T4 development hosts:

- `ubuntu-drivers install --gpgpu` installs no `nvidia-smi`: `host-setup.sh` adds `nvidia-utils-<ver>-server`.
- NVIDIA Container Toolkit 1.20 writes `additionalGids` into the CDI spec, which Podman 4.9 rejects
  ("unresolvable CDI devices"): `host-setup.sh` strips it.
- Podman 4.9 Quadlet has no `Entrypoint=` key: units use `PodmanArgs=--entrypoint=...`.
- The RustFS image runs as uid 10001. With the object store on a named volume, podman sets the ownership on
  first use; only a restored tarball still needs an explicit `chown`, which `install-app.sh` does.
- Whisper and the 12B chat model do not fit on one 16 GB T4 together: Whisper moved to the decision host's GPU 3.

## The T4 development layout used for the demo recording (2026-10-21)

T4s have no bf16, so Gemma 4 runs in fp32 and needs a whole 4x T4 VM. Three Ubuntu 24.04 VMs stood in for one GB10:

| Host | GPUs | Units |
|---|---|---|
| omni (NC64as_T4_v3) | 4x T4 | `gemma4` (TP 4, fp32) |
| decision (NC64as_T4_v3) | 4x T4 | `dgemma` (TP 4) + `dgemma-decision`; `asr` on GPU 3 (port 8001) |
| app (NC16as_T4_v3) | 1x T4 | data + app tiers, `agent-llm` (Gemma 4 QAT W4A16) |

```bash
deploy/scripts/install-app.sh --omni http://<omni>:8000 --decision http://<decision>:8011 --asr http://<decision>:8001 \
  --chat http://agent-llm:8000/v1 --chat-model agent
```

The `.container` files are the T4 variants (fp32, tensor-parallel 4, sm75 start-up patches). For the GB10,
switch `gemma4` to `RedHatAI/gemma-4-12B-it-FP8-Dynamic` with TP 1 and the memory fractions in
[`../k8s/gb10/README.md`](../k8s/gb10/README.md), and give every unit `AddDevice=nvidia.com/gpu=all`.
