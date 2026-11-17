# Spec: deploy (deploy/) — Podman + systemd (Quadlet)

Target hosts: Ubuntu 22.04/24.04 (NVIDIA's GB10 software stack) and RHEL 9/10, Podman 4.9 or newer. The
superseded OpenShift manifests and their acceptance criteria are in `k8s/` (see `k8s/README.md`).

- AC-DEP-1 All services join one Podman network, `nasa`, and resolve each other by pod or container name. The
  only port published off the host is the dashboard's TLS port 443; the dashboard's own 8080 is bound to
  loopback.
- AC-DEP-2 Topology is defined once, in `deploy/kube/*.yaml`, and consumed both by `podman kube play`
  (development) and by Quadlet `.kube` units (a POC host). No manifest content is duplicated between the two.
- AC-DEP-3 The manifests are host-independent: no `hostPath`, so they play rootless and as root. Persistent
  state is in PVCs that podman backs with named volumes (`nasa-pgdata`, `nasa-objects`, `nasa-media`). The only
  host directory is the model weight cache, `/var/lib/nasa/models`, used by the GPU units.
- AC-DEP-4 Configuration travels in the manifests as ConfigMaps (Postgres init, TLS proxy config). The single
  per-host value set, the model endpoints, is the `nasa-endpoints` ConfigMap supplied with `--configmap`.
- AC-DEP-5 `data`: `pgvector/pgvector:pg16` with the `vector` and `pg_trgm` extensions created on first start,
  and RustFS for frames, crops, evidence images and operator photos. inventory-api applies `contracts/db/*.sql`
  at start-up, so there is no separate migration job.
- AC-DEP-6 `app`: inventory-api, agent (chat container + MCP server container in one pod), and the dashboard
  (nginx serving the build, proxying `/api` and `/agent`). A separate `tls` pod terminates HTTPS on 443 and
  proxies to the dashboard; its certificate is a podman secret, not a file on the host.
- AC-DEP-7 `inference`: Quadlet `.container` units running `vllm/vllm-openai` for gemma4 (omni perception, and
  chat on the GB10), dgemma + its structured decision server, asr (Whisper), and agent-llm on split hosts.
  GPUs are attached with CDI (`AddDevice=nvidia.com/gpu=...`), because `podman kube play` on Podman 4.9 cannot.
  Each has a `HealthCmd` on `/health` with a `HealthStartPeriod` long enough for weight download and warm-up.
- AC-DEP-8 `perception`: one Quadlet `.container` unit per camera feed, installed but not enabled, reading its
  feed from the `nasa-media` volume.
- AC-DEP-9 Secrets are generated on the host into podman's secret store and never written to a file or
  committed. Re-running the installer reads them back rather than regenerating, because the database volume and
  stored objects are keyed to the first set.
- AC-DEP-10 Every container declares a `livenessProbe` (kube YAML) or `HealthCmd` (Quadlet), so container
  health reflects whether the service actually answers.
- AC-DEP-11 `deploy/scripts/install-app.sh` is idempotent: it installs manifests and units, generates missing
  credentials, starts each tier in dependency order waiting for readiness between them, creates the object-store
  bucket, and prints the dashboard URL. It accepts `--restore-db` and `--restore-objects` to come up on an
  existing demo baseline.
- AC-DEP-12 `scripts/stack.sh up` brings the data and application tiers up from the same manifests with plain
  `podman kube play`, rootless and without systemd, for development and demos.
- AC-DEP-13 Operational scripts (`scripts/build-images.sh`, `load-sample-video.sh`, `db-snapshot.sh`,
  `reset-observations.sh`) work against either store through `$PODMAN`, so the same script serves a developer's
  rootless stack and an operator's `sudo podman` host.
- AC-DEP-14 Images are pinned: RustFS by digest, all others by explicit tag; service images are tagged
  `localhost/nasa/<service>:poc` and built by `scripts/build-images.sh`.
