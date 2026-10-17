# NASA Omni Inventory POC — Architecture

Status: v0.1 (2026-10-17). Owner: hello-chris-nasa-omni. Research basis: `research/01..06`.

## 1. Goal

Experimental proof-of-concept for NASA: ingest a **live video feed and audio feed at the same time** from the
International Space Station (ISS) into **vLLM-Omni**, identify and track **objects and people**, and keep a
**Postgres inventory** that mission control can browse, search, correct and query in natural language.

The ISS is a **closed world**: most objects are in a known catalog, so unknowns are rare. Duplicate entries must be
minimised.

## 2. Requirements (traceable IDs)

| ID | Requirement |
|---|---|
| R1 | Video and audio are ingested simultaneously from a file or a live stream; frames and audio are time-aligned. |
| R2 | An omni model (vLLM-Omni) describes what is seen and heard, as structured observations. |
| R3 | Observations become inventory **entities** (one row per physical item or person-track) with last-seen location, time, confidence, description and snapshot frame. |
| R4 | **Duplicate reduction**: a new observation is matched to an existing entity before anything is created. |
| R5 | Events (moved / added / removed / used) are recorded with actor ("crew member in blue shirt"), time and evidence frame. |
| R6 | **Unknown objects** go to a review queue with image, description and timestamp; NASA classifies them (assign to catalog item, create new catalog item, merge into existing entity, or dismiss). |
| R7 | Dashboard: list items with filters (class, location, status, confidence, time range), search, sort by timestamp, item detail with history and frames, edit items changed off-screen, add unseen items (**image required**). |
| R8 | Chatbot: NASA can ask about objects and people ("where is the drill?", "who moved the camera?", "what is unknown in Node 2?"). Answers are grounded in the database and cite entity IDs and timestamps. |
| R9 | Postgres is the **source of truth**. Frame descriptions and audio transcripts go to long-term storage (RustFS). An agentic memory layer ("dreaming") compresses context, prioritising inventory state. |
| R10 | Evaluation system for the identification pipeline: detection, classification, deduplication, location, events, chatbot QA. It runs in CI as regression gates. |
| R11 | Open-source components only. Model licenses are recorded per model (see `docs/QUESTIONS.md`). |
| R12 | Runs on OpenShift with Open Data Hub (the upstream of Red Hat OpenShift AI). Inference runs in a dedicated namespace. |

## 3. System overview

```
             ┌──────────────── nasa-app ──────────────────────────────────────────────┐
 video feed ─┤ perception (Python)                                                    │
 audio feed ─┤  ingest(ffmpeg) → keyframes(1/s) + audio chunks(5 s)                   │
             │  → detector (OWLv2) + crop embeddings (DINOv2)        [fast path]      │
             │  → omni window: frames + audio → Observation JSON     [vLLM-Omni]      │
             │  → reconciler: label→catalog, candidate retrieval (pgvector), gating,  │
             │    decide (auto / DiffusionGemma adjudication / review queue)          │
             │  → inventory-api (POST observations / events / unknowns)               │
             │                                                                        │
             │ inventory-api (FastAPI) ── Postgres (CNPG + pgvector)  [source of truth]│
             │        │                └─ RustFS (frames, crops, transcripts)  [long-term]
             │ agent (FastAPI): chat + tools over inventory-api; "dreaming" memory job │
             │ dashboard (React/Vite): Inventory · Item · Unknowns · Manage · Chat     │
             └────────────────────────────────────────────────────────────────────────┘
             ┌──────────────── nasa-inference ────────────────────────────────────────┐
             │ omni:     vLLM-Omni nightly, Qwen2.5-Omni-7B thinker (A10)             │
             │ agent-llm: vLLM, Qwen3-8B, tool calling + JSON schema (A10)            │
             │ decision: vLLM, DiffusionGemma-26B INT8 TP4, /v1/systemone (4×T4)      │
             └────────────────────────────────────────────────────────────────────────┘
             nasa-data: CNPG Postgres cluster, RustFS     odh: Open Data Hub (KServe, etc.)
```

## 4. Key design decisions (ADRs in `docs/adr/`)

1. **Two perception paths** (research 01, 02). A cheap detector and embedding path runs on every keyframe. The omni
   model runs on short windows (4–8 frames + about 5 s of audio, every 2–5 s) and returns **structured observations**.
   Model text never writes to Postgres directly; the reconciler validates everything first (pydantic + JSON schema).
2. **Windowed stateless chat-completions first** (research 01, "path C"). It is portable across vLLM-Omni and upstream
   vLLM, easy to load-test, and avoids churn in the realtime endpoint. The `/v1/realtime?duplex=0` image path from
   PR #8896 is implemented behind a flag as the streaming upgrade.
3. **Closed-world dedup** (research 02 §4.4):
   - map labels to catalog classes;
   - retrieve candidate entities (pgvector on crop embeddings, same class, plus zone neighbours);
   - apply hard gates (class mismatch, impossible transit, concurrent visibility);
   - compute a weighted cost and decide in three bands: ≥0.9 auto-assign, 0.5–0.9 DiffusionGemma `choice` among
     candidates + NEW, below that a provisional entity. If the catalog `expected_count` is exceeded, flag it as a
     likely duplicate.
   - Fungible items (food packs, wipes) are tracked as **counts per zone**, not instances.
   - Every merge and split is logged and reversible.
4. **Decision model = DiffusionGemma** (research 06), via `/v1/systemone` choice questions: same-or-new, event type
   and zone. Each decision stores its probabilities, and low confidence goes to the review queue. There is a rule
   fallback when the service is down.
5. **Location model:** `module → zone` (camera-configured), stored hierarchically. In the POC, camera ID maps to
   module; a VLM hint ("visible signage") can refine the zone.
6. **People:** role/appearance descriptors and person tracks only. There is no biometric face database (privacy;
   `QUESTIONS.md` #7).
7. **Memory tiers** (diagram):
   - (1) Postgres: source of truth.
   - (2) RustFS + Postgres full-text index: long-term frame descriptions and transcripts.
   - (3) Agent memory "dreaming": a periodic job compresses recent observations into per-entity and per-zone
     summaries. Inventory facts get priority over chatter, and summaries are regenerated, never edited by hand.
8. **Agent harness:** tool-using agent with typed tools over inventory-api. It is read-only by default; any write is
   proposed and needs operator confirmation in the dashboard. It is model-agnostic (OpenAI-compatible). It sits
   behind a harness interface so it can be swapped for "Red Hat OpenClaw Enterprise" if that product becomes
   available (research 05).

## 5. Development process (spec + strict TDD + agentic pipeline)

- **Specs first:** `docs/spec/<component>.md` with numbered acceptance criteria (`AC-<COMP>-<n>`). Every test
  references the AC it proves.
- **Contracts first:** `contracts/` (SQL migrations, OpenAPI, JSON schemas) is the shared source of truth between
  components.
- **Strict TDD:** for each component, a *tester* agent writes failing tests from the spec (red is verified). A
  *developer* agent makes them pass (green). A *reviewer* agent adversarially reviews code + tests against the spec,
  and a fix round follows. Changes to tests after the red step require a reviewer note.
- **Eval pipeline** (`eval/`): gold datasets plus metrics, with thresholds in `eval/thresholds.yaml` as regression
  gates.
