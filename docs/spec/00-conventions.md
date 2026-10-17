# Spec conventions

- Each component spec lists acceptance criteria `AC-<COMP>-<n>`. Every test names the AC it proves in its test name
  or docstring (e.g. `test_ac_api_07_add_item_requires_image`).
- Contracts in `contracts/` are authoritative: `db/*.sql`, `openapi/inventory-api.yaml`, `schemas/*.json`. A spec
  that needs a contract change must say so explicitly, and the reviewer must approve it.
- Strict TDD order per component: (1) tests written from the spec; (2) run, and confirm they fail for the right
  reason (not import/syntax errors); (3) implement; (4) all green; (5) adversarial review; (6) fixes. Tests are not
  weakened to pass. Any test change after the red step needs a written justification in `docs/tdd-log/<comp>.md`.
- Python 3.11, pytest, ruff. Services are FastAPI + psycopg 3. JS: React 18 + Vite + TypeScript, vitest +
  @testing-library.
- Local Postgres for tests: `postgresql://postgres:test@localhost:55432/<db>` (pgvector/pgvector:pg16). Each test
  session creates a fresh database from `contracts/db/*.sql`.
- No secrets in code: configuration comes from environment variables (`DATABASE_URL`, `S3_*`, `OMNI_URL`,
  `DECISION_URL`, `AGENT_LLM_URL`).
- Model outputs are untrusted: always validate them against the JSON schemas before acting.
