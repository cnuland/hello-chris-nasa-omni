# Spec: deploy (deploy/) — OpenShift + Open Data Hub

- AC-DEP-1 Kustomize overlays create the namespaces `nasa-inference`, `nasa-data`, `nasa-app` and `opendatahub` (ODH
  operator from upstream manifests on quay.io; no Red Hat pull secret needed).
- AC-DEP-2 `nasa-data`: CloudNativePG cluster (Postgres 16 with pgvector) and a migrations Job applying
  `contracts/db/*.sql`; RustFS StatefulSet + bucket bootstrap Job. Credentials come from Secrets created by the
  bootstrap script, never committed.
- AC-DEP-3 `nasa-inference`:
  - **omni**: vLLM-Omni nightly (pinned digest ≥ PR #8896), Qwen2.5-Omni-7B, 1 GPU;
  - **agent-llm**: vLLM, Qwen3-8B with tool parser, 1 GPU;
  - **decision**: vLLM DiffusionGemma INT8, TP4 on 4×T4, with `CUDA_MODULE_LOADING=EAGER` and the sm75 patches.
  Each is served through a KServe RawDeployment InferenceService with ServingRuntimes where the runtime supports it,
  otherwise a plain Deployment + Service. Readiness probes are on `/health`.
- AC-DEP-4 `nasa-app`: inventory-api, perception (Deployment per camera feed), agent (+ dreaming CronJob), dashboard
  (nginx serving the build, proxying `/api` and `/agent`), and Routes for the dashboard. Images are built in-cluster
  (BuildConfigs).
- AC-DEP-5 `scripts/bootstrap.sh` is idempotent: creates secrets, applies overlays, waits for readiness, seeds the
  DB, and prints the dashboard URL. `scripts/smoke.sh` sends a 30 s sample clip and asserts that ≥ 1 entity appears.
