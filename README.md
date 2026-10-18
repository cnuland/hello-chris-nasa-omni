# NASA Omni Inventory POC

An experimental proof of concept for the International Space Station. **vLLM-Omni takes video and audio together**,
identifies and tracks objects and people, and keeps a **Postgres inventory** that mission control can browse, correct
and question in natural language. It is built only from open-source components, on OpenShift with Open Data Hub (the
upstream of Red Hat OpenShift AI).

Status: running end to end on ARO (2026-10-17) against NASA's public-domain *Step Inside the International Space
Station* tour video. This is experimental; known limits are listed below and in `docs/QUESTIONS.md`.

![Inventory](docs/screenshots/inventory.png)

## What is deployed

| Namespace | Component | What it does |
|---|---|---|
| `nasa-inference` | **Gemma 4 12B** (Google, Apache-2.0), plain vLLM, fp32 TP4 on T4 | Perception omni model. Takes 6 frames + speech audio per window and returns a structured observation: objects with boxes, people (appearance only), events, location hint |
| | **Gemma 4 12B QAT-W4A16** (vLLM, gemma4 tool parser) | Chatbot / inventory agent |
| | **Whisper large-v3-turbo** (OpenAI, MIT) | Speech transcript of record |
| | **DiffusionGemma-26B INT8** (vLLM TP4) + structured decision server | Decision model: "same object or new?" and speech-act classification, with calibrated probabilities |
| `nasa-app` | **perception** (Python, own T4) | ffmpeg ingest, then OWLv2 (Google) detection + DINOv2 (Meta) embeddings, then Gemma window, then closed-world reconciler (dedup, revisit, person tracks, privacy), then the inventory API |
| | **inventory-api** (FastAPI) | Source-of-truth API: items, review queue, merge and unmerge, ranked duplicate candidates, events, search, media |
| | **agent** (+ MCP server) | Grounded chatbot. It cites items, windows and catalog entries, works out time windows itself, and falls back to answers built only from tool data; MCP attach point for OpenClaw Enterprise |
| | **dashboard** (React + nginx) | Inventory with filters, review queue (side-by-side duplicates, merge/undo, bbox overlays), overview, add/edit, chat with live status |
| `nasa-data` | **Postgres 16 + pgvector** (CloudNativePG, 2 instances) | Inventory database, the source of truth |
| | **RustFS** (S3, Apache-2.0) | Frames, crops, evidence images, raw model replies, operator photos |
| `opendatahub` | **Open Data Hub 3.3** | KServe (RawDeployment), dashboard, TrustyAI, model registry |

**Every model is US-origin** (Google, OpenAI, Meta). Qwen2.5-Omni was the first omni model and is retired (kept at 0
replicas for A/B tests). On 8 NASA windows Gemma 4 gave 0 whole-frame junk boxes out of 50 objects, against Qwen's
35 of 73, and named specific ISS hardware. NVIDIA Cosmos Reason is post-trained from Qwen-VL; see
`research/07-us-models.md` for that and for the Nemotron 3 Nano Omni option on GB10. **Production target:** a Dell GB10
on the ISS. A single-node, untested profile with a memory budget and checklist is in [`deploy/gb10/`](deploy/gb10/).

The design, requirements (R1 to R12) and decisions are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The
research behind it (vLLM-Omni realtime, tracking and dedup, audio fusion, the ISS domain, agent memory, decision
models) is in [`research/`](research/).

## How duplicates are kept down

The ISS is a closed world, and the reconciler uses that:
1. Labels are mapped to the **catalog**, using synonyms.
2. Repeated listings from omni are **consolidated within each window**.
3. Candidates come from **pgvector** search over DINOv2 crops of full-resolution frames.
4. **Hard gates** rule out impossible matches: class, and transit between modules too fast to be real.
5. Matches fall into **three bands**:
   - **auto-merge** at ≥ 0.85;
   - **DiffusionGemma decides** between 0.5 and 0.85;
   - **new** below 0.5.
6. A **possible-duplicate review** opens when an item's `expected_count` would be exceeded.
7. Fungible items (food packs, wipes and so on) are kept as **stock counts**, not individual entities.

Unknown objects go to the **review queue** with image, description, timestamp, location and the model's guess. NASA can
classify them, create a new catalog item, merge them into an existing item, or dismiss them.

| Inventory with filters | Review queue (unknowns, with images) |
|---|---|
| ![](docs/screenshots/inventory.png) | ![](docs/screenshots/review.png) |
| **Overview / recent events** | **Add an unseen item (image required)** |
| ![](docs/screenshots/overview.png) | ![](docs/screenshots/additem.png) |

## Development process: spec first, strict TDD, eval gates

Every component was built from a spec with acceptance criteria (`docs/spec/*.md`, for example `AC-PER-22`) by a
multi-agent pipeline: **tester** (red) → **developer** (green) → **adversarial reviewer** → **fixer** → **verifier**.
The cycles are logged in [`docs/tdd-log/`](docs/tdd-log/). Each cluster-integration bug was turned into a failing
test before it was fixed (see the "cluster integration" sections of the TDD logs).

```bash
make venvs   # python venvs + dashboard node_modules
make test    # all suites; needs local Postgres: podman run -d --name nasa-pg-test -p 55432:5432 \
             #   -e POSTGRES_PASSWORD=test -e POSTGRES_DB=inventory pgvector/pgvector:pg16
make eval    # identification eval with gated thresholds (eval/thresholds.yaml)
make lint
```

| Suite | Tests |
|---|---|
| inventory-api | 450 |
| perception | 824 |
| agent | 513 |
| eval | 365 |
| dashboard | 341 |
| end-to-end | 27 |

**Product review loop.** A Playwright reviewer agent drives the live dashboard, takes screenshots and judges them
against the diagram and specs (`docs/ui-review/`). Each round's findings become spec amendments, failing tests, fixes
and redeploys.

| Round | Result |
|---|---|
| 1 | 22 findings (3 P1) |
| 2 | 14 of 22 fixed, 0 regressions, 17 new findings |
| Round-2 fixes (live check) | the five standard chat questions now answer correctly in one conversation, in 10–21 s, down from up to 64 s |

The eval gates cover:
- duplicate rate and pairwise F1;
- location accuracy;
- unknown recall and sighting recall;
- **revisit duplication** (the same scene again after 15 min);
- **person-track recall**;
- **no media from sleep-cue (crew quarters) windows**;
- chatbot QA.

They include omni profiles that copy real model behaviour seen on the cluster: objects repeated in every frame,
replies cut off at the token limit, and people reported only in `people[]`.

## Deploying

The cluster needs `oc` logged in as cluster-admin, plus `helm` and `jq`. All secrets are generated on the cluster;
none are stored in this repo.

```bash
export KUBECONFIG=~/path/to/dedicated/kubeconfig
./scripts/bootstrap.sh                       # gpu, odh, cnpg, data, inference, app (idempotent)
ONLY="data app" ./scripts/bootstrap.sh       # or individual steps
./scripts/load-sample-video.sh tour.mp4      # put a sample feed on the perception volume
./scripts/reset-observations.sh [--yes]      # clear observation data, keep the seed (dry run without --yes)
```

## Measured on the cluster (T4 GPUs; the Azure policy denied A10)

| | Value |
|---|---|
| OWLv2 detection | 0.35 s/frame on a T4, against 14 s/frame on 4 CPU threads |
| DiffusionGemma decision | ~0.4 to 0.9 s, p(same) = 0.99 on a clear same-object pair |
| Chatbot answer (tool calls + grounding) | ~9 s |
| Omni window (6 frames + audio) | ~20 s (≈550 output tokens at ~28 tok/s); 80% of perception wall time |
| Duplicates, first 8 windows of the tour | 56 entities before the real-video fixes, 16 after |
| Full tour, first pass (~75 windows, 8 modules) | 0 errors; locations followed the tour (COL1, LAB1, NOD1-3, CUP1, ZARYA, ZVEZDA); DiffusionGemma matched 41 of 44 "same or new?" questions to existing items |
| Omni replies hitting the 700-token cap | 77% (42 of 44 salvaged instead of dropped) |
| Second pass (video looped) | 22 new entities right after the loop restart, then 5 in the next 11 windows: revisit dedup after a long gap is the main open issue (QUESTIONS #29) |

Real time at a 4 s stride needs roughly 5× the omni decode speed (L4/A10-class or better). The POC runs with a
12 s stride. See `docs/QUESTIONS.md` #26.

## Known limits and open questions

`docs/QUESTIONS.md` is the decision log. The headline items:
- **Hardware.** This environment only allows T4 GPUs.
- **OpenShift AI.** No Red Hat pull secret was available, so Open Data Hub was used instead.
- **OpenClaw.** "Red Hat OpenClaw Enterprise" could not be verified as a product. An MCP server exposes the
  inventory tools so it can be attached later.
- **Crew privacy.** People are described by appearance only; there are no biometrics. Crew quarters and the WHC are
  no-capture zones, and that policy fails closed.
- **Omni hallucinations.** Omni sometimes names objects the detector does not confirm (a "drill" next to a
  cable-covered rack). Such omni-only sightings store no appearance exemplar and attach to the most recent same-class
  item in the module. Implausible ones surface through the expected-count duplicate reviews.
