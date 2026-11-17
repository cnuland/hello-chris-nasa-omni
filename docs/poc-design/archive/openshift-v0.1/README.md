# Archived: OpenShift version of the POC design (v0.1, 2026-10-20)

Version 0.1 of the POC design targeted **single-node OpenShift (or MicroShift) with Red Hat OpenShift AI / Open Data
Hub** on the Dell GB10. On 2026-10-21 Red Hat's NASA account lead pointed out that NASA's GB10 runs NVIDIA's standard
Ubuntu-based software stack, and that NASA will run the POC on its own GB10. Version 0.2 therefore moved to **Podman
(Quadlet) on the existing OS**, with MicroShift as an option to test. The same review removed every non-US model
(the Qwen2.5-Omni comparison and the Cosmos/Qwen-VL note).

The OpenShift approach is kept here because single-node OpenShift, or an on-prem Red Hat AI Enterprise environment on
Dell servers, may come back as a future discussion with NASA.

## Contents

| Path | What it is |
|---|---|
| `build_poc_doc.openshift-v0.1.py` | Generator for the v0.1 document (OpenShift platform, ARO fallback, Qwen comparison table). It expects to run from `docs/poc-design/` with these figures in `img/`. |
| `img/fig1-architecture.png` | Figure 1 as used in v0.1: namespaces nasa-app / nasa-inference / nasa-data, GPU Operator, Open Data Hub / OpenShift AI |
| `img/fig2-gb10-deployment.png` | Figure 2 as used in v0.1: Deployments, Services, Route, PVCs, GPU time-slicing ConfigMap, CloudNativePG |
| `img/fig1-architecture-v5-alt.png`, `img/fig1-architecture-zip-alt.png` | Rejected Figure 1 drafts (vLLM-Omni missing; arrow errors) |
| `NASA-Omni-Inventory-POC-Demo.mp4` | First demo video (4:38): `oc` CLI and OpenShift console on ARO, then the dashboard, review queue, add item and chatbot |
| `demo-video/` | Playwright recorder and ffmpeg assembler for that video, and the console login helper (reads `KUBEADMIN_PASSWORD` from the environment) |

## Still live elsewhere in the repo

The OpenShift manifests have not moved: `deploy/{platform,data,inference,app}` and `scripts/bootstrap.sh` still deploy
the full stack to OpenShift (the prototype ran on ARO cluster `aro-cluster-zlknq`). `deploy/gb10/` is the untested
single-node OpenShift / MicroShift profile. The Podman packaging that replaced them for the GB10 is in
`deploy/podman/`.
