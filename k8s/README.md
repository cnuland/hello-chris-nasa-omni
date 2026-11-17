# Kubernetes / OpenShift manifests — superseded, kept for reference

**This is not the deployment path for the POC.** The POC runs on **Podman**: see [`deploy/`](../deploy/) and the
[top-level README](../README.md).

These manifests are the v0.1 deployment, which targeted OpenShift with Open Data Hub (the upstream of Red Hat
OpenShift AI). They are what the prototype actually ran on, on an ARO cluster with T4 GPUs, and every measurement
quoted in the top-level README was taken against them. On 2026-10-21 the target changed: NASA's Dell GB10 runs
NVIDIA's standard Ubuntu-based software stack and NASA will run the POC on its own GB10, so the deployment moved to
Podman containers managed by systemd on the existing OS, with nothing replacing the host platform.

They are kept because single-node OpenShift, MicroShift, or an on-prem Red Hat AI Enterprise environment on Dell
servers may come back as a future discussion. The background is in
[`docs/poc-design/archive/openshift-v0.1/`](../docs/poc-design/archive/openshift-v0.1/).

## What is here

| Path | What it is |
|---|---|
| `platform/` | Cluster prerequisites: GPU machinesets and the NVIDIA GPU operator, Open Data Hub, CloudNativePG |
| `data/` | `nasa-data`: CloudNativePG Postgres 16 + pgvector, a migrations Job over `contracts/db/*.sql`, RustFS |
| `inference/` | `nasa-inference`: gemma4, dgemma (+ decision server), agent-llm, and the retired Qwen omni server |
| `app/` | `nasa-app`: inventory-api, perception, agent (+ MCP, + dreaming CronJob), dashboard, BuildConfigs, Routes |
| `gb10/` | Untested single-node OpenShift / MicroShift profile for a GB10, with GPU time-slicing and a memory budget |
| `scripts/` | `bootstrap.sh` (whole-cluster bring-up), `build-images.sh` (in-cluster binary builds), and the `oc` versions of the snapshot / reset / load-video helpers |

## Running them

Needs `oc` logged in as cluster-admin, plus `helm` and `jq`. All secrets are generated on the cluster; none are
stored in this repo.

```bash
export KUBECONFIG=~/path/to/dedicated/kubeconfig
./k8s/scripts/bootstrap.sh                       # gpu, odh, cnpg, data, inference, app (idempotent)
ONLY="data app" ./k8s/scripts/bootstrap.sh       # or individual steps
```

## Differences from the Podman deployment

These are the parts that had to be replaced rather than translated, and they are the reason the two trees do not
share manifests:

| Cluster concept | Podman equivalent (`deploy/`) |
|---|---|
| Three namespaces | One container network, `nasa`; names resolve across the whole host |
| CloudNativePG operator, 2 instances, failover | A single `pgvector/pgvector:pg16` pod; HA is out of scope for one box |
| Migrations Job | inventory-api applies `contracts/db/*.sql` itself at start-up |
| KServe `InferenceService` + ServingRuntime | Quadlet `.container` units running `vllm/vllm-openai` directly |
| GPU via `resources.limits: nvidia.com/gpu` | CDI: `AddDevice=nvidia.com/gpu=all` in the Quadlet unit |
| In-cluster BuildConfigs / ImageStreams | `podman build` on the host (`scripts/build-images.sh`) |
| PersistentVolumeClaims backed by a storage class | PVCs in the kube YAML, which podman backs with named volumes |
| `Route` + the router's certificate | An nginx pod on :443 with a certificate from a podman secret |
| Dreaming `CronJob` | A systemd timer (`agent-dreaming.timer`) |
