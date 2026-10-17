# Spec: agent (services/agent) — NASA chatbot + memory

- AC-AGT-1 `POST /chat {messages:[{role,content}], session_id?}` returns `{reply, citations:[{entity_id|window_id,
  at}], tool_calls:[...]}`. A streaming variant `/chat/stream` returns SSE tokens.
- AC-AGT-2 Tools (typed, read-only), each calling inventory-api:
  - `search_items(q, filters)`
  - `get_item(entity_id)`
  - `item_history(entity_id)`
  - `where_is(name)`: resolves the catalog/label, returns last location and time
  - `who_moved(name, since?)`
  - `list_unknowns(location?)`
  - `inventory_stats()`
  - `search_observations(q)`: full-text over windows
  - `recall_memory(scope, key)`
- AC-AGT-3 Runs a tool-calling loop against an OpenAI-compatible LLM (`AGENT_LLM_URL`, `tools`, `tool_choice=auto`)
  with at most 6 steps. Tool arguments are validated with pydantic; invalid args are fed back as a tool error once.
- AC-AGT-4 **Grounding**: answers that state a location, time or actor must come from tool results in this turn. The
  harness appends citations from the tools used, and if no tool returned data it answers "I don't have a record of
  that" rather than guessing. Tested with a scripted fake LLM.
- AC-AGT-5 **Write proposals**: the agent may propose an edit (`propose_update(entity_id, fields)`), which returns a
  proposal object. It never calls PATCH itself; the dashboard confirms.
- AC-AGT-6 **Dreaming** (`POST /memory/dream` + a periodic job) builds per-entity, per-location and global summaries
  from the last N hours of observation windows, events and current inventory into `agent_memory` (upsert). Inventory
  facts (current location, status, unresolved reviews) are always included first, and chatter is truncated to fit the
  token budget. It is idempotent.
- AC-AGT-7 The context builder for each chat turn includes the global memory summary plus the system prompt (crew
  privacy rules: describe people by role/appearance only), within a configurable token budget, with oldest chat turns
  dropped first.
- AC-AGT-8 Each chat request gets a uuid. Logs record tool calls and latency, not raw images.
- AC-AGT-9 **MCP server** (`services/agent/mcp_server.py`, Model Context Protocol over streamable HTTP) exposes the
  same read tools plus `propose_update`, so OpenClaw (MIT; Red Hat `claw-operator`) or any MCP client can use the
  inventory. There is no raw-SQL tool (research/05: the reference Postgres MCP server is archived and has an injection
  bypass).
- AC-AGT-10 **Inventory-first memory** (research/05): every chat turn renders a budgeted, SQL-derived inventory block
  (open reviews, recently moved items, items in the asked-about module), which is never summarised away.
  Dreaming summaries are an auxiliary layer only.
