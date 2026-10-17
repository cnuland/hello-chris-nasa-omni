# TDD log: dashboard (`dashboard/`)

Spec: `docs/spec/40-dashboard.md` (AC-UI-1 to AC-UI-10). Contracts: `contracts/openapi/inventory-api.yaml`, plus the
agent chat shape from `docs/spec/30-agent.md` (AC-AGT-1/5).

## Red phase (2026-10-17, tester)

### Skeleton

- Stack: React 18, react-router-dom 6, Vite 8, TypeScript 7, vitest 5 (jsdom 29), @testing-library/react 16 and
  user-event 14, msw 3, dom-accessibility-api. npm resolved the latest majors. The lock file is `package-lock.json`.
- `vite.config.ts`:
  - dev proxy `/api` -> `http://localhost:8080` and `/agent` -> `http://localhost:8090`, both with the prefix
    stripped;
  - the targets can be overridden with `API_PROXY_TARGET` / `AGENT_PROXY_TARGET`;
  - the vitest config is in the same file.
- `Containerfile`: node:24 build stage, then `nginxinc/nginx-unprivileged` on port 8080 (works with OpenShift's
  arbitrary UID). `nginx/default.conf.template` proxies `/api/` -> `${API_UPSTREAM}/` and `/agent/` ->
  `${AGENT_UPSTREAM}/` (with `proxy_buffering off` for SSE), and has an SPA fallback. The defaults are
  `http://inventory-api:8080` and `http://agent:8090`.
- Stubs. They raise `NotImplementedError` or render `null`; no behaviour is implemented.
  - `src/App.tsx` (renders null)
  - `src/pages/*.tsx`
  - `src/api/client.ts`
  - `src/api/types.ts`: complete types from the OpenAPI contract.
  - `src/lib/filters.ts` (`filtersFromSearch`, `filtersToSearch`, `filtersToItemsQuery`)
  - `src/lib/format.ts`
  - `src/lib/diff.ts` (`changedFields`)
  - `src/lib/locations.ts` (`flattenLocationTree`)
  - `src/lib/sse.ts` (`SSEParser`)
  - `src/main.tsx` wraps `App` in a `BrowserRouter`.
- Test infrastructure (`tests/`):
  - `setup.ts`:
    - jest-dom matchers, msw lifecycle (`onUnhandledFrame: 'error'`), and `TZ=UTC`;
    - a **fetch shim** that resolves relative URLs against `window.location`, and pre-encodes `FormData` bodies as
      multipart bytes. Under vitest's jsdom realm, undici's own FormData serialisation hangs and its parser fails
      realm assertions. Both problems were verified with probes.
  - `multipart.ts`: a small multipart encoder/decoder. The mock server records multipart requests as `MultipartBody`.
  - `server.ts`:
    - default msw handlers for every endpoint;
    - a request log (`requests`, `requestsTo`, `lastItemsQuery`). The body is parsed before any handler runs, and
      the logger is kept in front of `server.use()` overrides;
    - SSE helpers. `controllableSSE()` lets a test push frames one by one. Its first chunk is an SSE comment,
      because msw resolves `fetch()` only once a chunk exists.
  - `render.tsx`:
    - `renderApp(path)` renders `<MemoryRouter><App/></MemoryRouter>` and a URL probe (`current`, `searchParams()`);
    - `findToast`, `findItem`, `findReviewCard`, `freezeDate`, `fakeTimers`.
  - `fixtures.ts`: OpenAPI-shaped items, detail, history, locations, catalog, reviews, stats and events.
    `NOW = 2026-10-17T12:05:00Z`.

### UI contract the tests pin down (for the developer)

Where the spec is silent, the tests take the decisions below. The tests use accessible queries wherever possible
(this also serves AC-UI-10).

| Area | Contract |
|---|---|
| Routes | `/` inventory; `/items/:id` detail (drawer or page); `/items/new` add form; `/review`; `/chat`; `/overview`. `App` renders inside a Router supplied by the caller. |
| Shell | `<nav>` with links Inventory (`/`), Overview, Review (shows the open count, e.g. "Review 3"), Chat. Content in `<main>`. |
| Toasts | `role="region"` `aria-label="Notifications"`. Error toasts use `role="alert"`, show the API `detail`, and have a Dismiss/Close button. Nothing modal. |
| Media | API paths `/media/...` are rendered as `/api/media/...`. A null snapshot renders no `<img>` with a bogus src. |
| Inventory row/card | `data-testid="item-<entity_id>"`:<br>- img alt = label;<br>- label is a link to `/items/:id`;<br>- catalog name; status text;<br>- `role="progressbar"` named Confidence, with `aria-valuenow` as a 0-100 percent;<br>- location name + code;<br>- relative time ("5 min ago", "3 h ago", "1 d ago") whose `title` holds the UTC time;<br>- last moved by;<br>- a "Needs review" flag only when the item needs review. |
| Filters | Labels:<br>- Search: 300 ms debounce.<br>- Kind: select, `''` = any.<br>- Status: a `role="group"` named Status with one checkbox per enum value.<br>- Location: select with depth-first hierarchy, value = location_id.<br>- Min confidence: range, 0..1.<br>- Seen after / Seen before: datetime-local, sent as ISO UTC.<br>- Needs review: checkbox; sends `needs_review=true`, nothing when off.<br><br>The URL query uses the same names as `/items`: status is repeated, empty values are omitted, invalid values are dropped. Changing a filter or the sort resets the offset to 0. |
| Sort/paging | Sort select labelled Sort, with the 6 enum values; default `-last_seen_at`. `limit=50`; the URL carries `offset`. Shows "N items" and "Page X of Y". Previous/Next buttons are disabled at the ends. Server order is kept. |
| Detail | Heading = label. Large snapshot. Attribute keys and values. `role="list"` named History/Timeline with one listitem per entry (summary, kind, evidence img). Lines read "Moved by <actor\|unknown> at <UTC>". 404 shows "not found". Empty history shows "No history". |
| Edit | An "Edit" button opens `<form aria-label="Edit item">` with fields Label, Description, Catalog (select, `''` = none), Location (select), Status (confirmed/provisional/unknown/dismissed), Operator, Note, and a Save button. The PATCH contains only the changed fields: location becomes `last_location`; a cleared catalog becomes `catalog_id: null`; operator/note are sent only alongside a real change; no change means no PATCH. Success shows a "Saved/updated" toast; failure shows an error toast and keeps the input. |
| Add | `<form aria-label="Add item">` with fields Image (file, `accept` image/*), Label, Location, Catalog, Description. The submit button (Add item/Create/Submit/Save) is disabled, and the "Image is required" message is shown, until an image is chosen. A non-image file does not count. Label and location are required. The POST is multipart and omits empty optionals. The inventory page links to it with "Add item". |
| Review | Page-level Operator input (required; otherwise "operator is required"). Card `data-testid="review-<id>"` shows: image, description, UTC time, location, model guess, kind. Actions:<br>- Search catalog input + Catalog select + Classify (`assign_catalog`).<br>- New catalog item: Catalog id (validated against the contract pattern), Class name, Display name, then Create (`new_catalog`).<br>- `role="list"` named Candidates with images, and buttons "Merge into <label>" (`merge_into`).<br>- Dismiss.<br><br>The card is removed on success and kept on error. The nav badge updates. |
| Chat | Textbox labelled Message, Send button (and Enter), `role="log"`. Disabled while streaming. Sends the full history. `role="list"` named Citations: entity citations are links to `/items/:id`; window citations are shown but not linked; the list is omitted when there are no citations. Proposals show "Proposed update", the fields, and a Confirm button. Confirm sends the PATCH once and then shows "applied/confirmed/updated"; on error the button stays. |
| Overview | `role="region"` tiles: Items = sum(by_status) excluding merged and dismissed; Unknowns = `open_reviews_by_kind.unknown_object`; Possible duplicates = `open_reviews_by_kind.possible_duplicate`; People = `total` of `GET /items?kind=person&seen_after=now-1h`. `role="list"` named Recent events (from `GET /events`), polled every 10 s; the interval is cleared on unmount. |
| Theme | `src/styles/theme.css` (imported by `main.tsx`) with `color-scheme: dark`, a hex `--bg` (luminance < 0.05) and a hex `--fg` (contrast >= 7:1). No CSS width/min-width > 1280 px. |

### Cross-component contract assumptions (not edits — for reviewer/agent team)

1. **Agent streaming format** (AC-AGT-1 only says "SSE tokens"). The dashboard expects
   `POST /agent/chat/stream {messages, session_id?}` to return `text/event-stream` with:
   - `event: token` / `data: {"text": "..."}`
   - `event: done` / `data: {reply, citations, tool_calls, proposals?}`
   - `event: error` / `data: {"detail": "..."}`
   
   Comment lines must be ignored.
2. **Proposals in the chat response** (AC-AGT-5 says the agent "returns a proposal object"). The dashboard expects
   `proposals: [{entity_id, fields: <PATCH body>, summary?}]` on the `done` payload (and on the `/chat` JSON). The
   agent spec should adopt this shape.
3. `/stats` has no schema in the OpenAPI file. The dashboard uses the shape the inventory-api tests pinned
   (`by_status`, `by_kind`, `by_module`, `open_reviews_by_kind`). Adding it to the OpenAPI is recommended.

### Test command

```
cd dashboard
npm install
npx vitest run          # or: npm test
```

### Result

**157 tests in 11 files: 150 failed, 7 passed, 0 errors** (about 18 s). `npx tsc --noEmit -p .` is clean.

The 7 passes are static checks on skeleton files this phase creates on purpose:
- 5 in `packaging.test.ts`: vite proxy, Containerfile, nginx template, no secrets;
- AC-UI-10 "index.html declares a responsive viewport";
- AC-UI-10 "no paid UI kits in dependencies".

AC coverage map:

| AC | File | Tests |
|---|---|---|
| AC-UI-1 | `ac_ui_01_inventory.test.tsx` | 12 |
| AC-UI-2 | `ac_ui_02_filters.test.tsx` (UI + `lib/filters`, `lib/locations`) | 19 |
| AC-UI-3 | `ac_ui_03_sort_pagination.test.tsx` (incl. 5 parametrised sort cases) | 16 |
| AC-UI-4 | `ac_ui_04_item_detail.test.tsx` | 10 |
| AC-UI-5 | `ac_ui_05_edit_item.test.tsx` (UI + `lib/diff`) | 16 |
| AC-UI-6 | `ac_ui_06_add_item.test.tsx` | 13 |
| AC-UI-7 | `ac_ui_07_review.test.tsx` | 17 |
| AC-UI-8 | `ac_ui_08_chat.test.tsx` (UI + `lib/sse`) | 22 |
| AC-UI-9 | `ac_ui_09_overview.test.tsx` | 9 |
| AC-UI-10 | `ac_ui_10_a11y_theme_toasts.test.tsx` (incl. 1280/1920 cases) | 18 |
| packaging | `packaging.test.ts` | 5 |

Failure-reason sample (from the `vitest run` output, most frequent first):

| Reason | Count |
|---|---|
| `Unable to find an element by: [data-testid="item-1111…"]` (App renders null) | 24 |
| `Unable to find role="heading" and name /pistol-grip drill/i` | 20 |
| `Unable to find a label with the text of: /message/i` (chat) | 17 |
| `Unable to find an element by: [data-testid="review-aaaa…"]` | 15 |
| `Unable to find role="form" and name /add item/i` | 13 |
| `Unable to find role="region" and name /notifications/i` | 7 |
| `NotImplementedError: changedFields` / `SSEParser.push` / `filtersFromSearch` / `flattenLocationTree` | 5 / 4 / 3 / 2 |
| `AssertionError: expected 0 to be greater than 0` (no `/items` request made) | 3 |
| `AssertionError: --bg hex colour: expected undefined to be defined` (no theme.css) | 1 |

None of the failures are syntax, import or infrastructure errors. Before the red step was closed, the infrastructure
was checked with throwaway probes, since deleted: multipart upload through the shim, SSE incremental reads,
debounce and 10 s polling under fake timers, and logging of `server.use()` overrides.

## Green phase (2026-10-17, developer)

### Result

**157 tests in 11 files: 157 passed, 0 failed, 0 skipped** (`npx vitest run`, about 2 s; three repeated runs were
all green, with no React `act()` or console warnings). `npx tsc --noEmit -p .` is clean, and `npm run build`
(`tsc -b && vite build`) produces `dist/` (about 206 kB JS, 65 kB gzip).

No tests were changed. The "Test changes" section is not needed.

### Implementation map

| Module | Responsibility |
|---|---|
| `src/lib/filters.ts` | URL <-> `Filters` <-> `GET /items` query. Invalid enum/range values are dropped. The default sort and `offset=0` are omitted from the URL but always sent to the API. |
| `src/lib/format.ts` | Relative time, UTC time (`YYYY-MM-DD HH:MM:SS UTC`), `/media` -> `/api/media`, datetime-local <-> ISO (UTC). |
| `src/lib/locations.ts` | Depth-first location tree. Siblings keep the API order. Orphans and cycles are kept as roots. |
| `src/lib/diff.ts` | `changedFields`: trimmed comparison; a cleared catalog becomes `null`; operator/note are sent only with a real change; `merged` is never PATCHed. |
| `src/lib/sse.ts` | Incremental SSE parser: CRLF handling, comments, frames split across chunks; malformed JSON becomes an `error` event. |
| `src/lib/log.ts` | Structured JSON console logger (quiet under vitest except for errors). |
| `src/api/client.ts` | fetch client. Non-2xx responses become `ApiError(status, detail)` (FastAPI `detail` string or list). A network failure becomes `ApiError(0, "network error (...)")`. `streamChat` reads the SSE body incrementally. |
| `src/state/toasts.tsx` | Notifications region. Errors use `role=alert` and stay until dismissed; success/info toasts auto-dismiss after 6 s. Identical visible messages are de-duplicated. |
| `src/state/reviewCount.tsx` | Nav badge = sum of `/stats` `open_reviews_by_kind`. Refreshed on mount and after each resolve. Failures are silent, because the overview reports its own `/stats` errors. |
| `src/state/reference.ts` | `useLocations` / `useCatalog` hooks. |
| `src/pages/*` | Inventory, ItemDetail (+ `components/EditItemForm`), AddItem, Review, Chat, Overview. |
| `src/styles/theme.css` | Dark mission-control theme: `--bg #0a0e14`, `--fg #e6edf3` (contrast about 16:1). Fluid layout with no fixed widths above 1280 px. |

### Decisions within the tester's contract

- **Item detail** is a page, not a drawer. "Moved by <actor|unknown> at <UTC>" lines come from `detail.events`
  (`event_type = moved`) and appear in a separate "Movements" list, so the History list keeps exactly one listitem
  per timeline entry.
- **Edit item:** saving updates the page from the PATCH response, closes the form and reloads the history.
- **Add item:** on success it shows an "Item added" toast and navigates to the new item.
- **Inventory count line:** it reads "N items", or "No items" when the total is 0. The empty-table text is
  deliberately different ("Nothing matches the current filters.") so that only one element matches `/no items/`.
- **Datetime-local filter inputs** are interpreted as UTC. They are labelled "Seen after (UTC)" / "Seen before (UTC)"
  to fit mission time (GMT).
- **Review operator:** the operator name persists in `localStorage` (`nasa-omni.operator`). All review actions are
  disabled, and "Operator is required…" is shown, while it is empty.
- **Catalog search** matches the display name, class name, id and synonyms.
- **Chat:**
  - Send is disabled while a reply streams, and `send()` is guarded. The textarea stays editable (`aria-busy`) so
    the operator can draft the next question. The tests only pin the button.
  - Confirm on a proposal is guarded by an in-flight ref, so a double click sends exactly one PATCH.
  - A failed confirm re-enables the button.
- **Overview:** stats, the people count and events all refresh every 10 s. The interval is cleared on unmount.

### Lint/format

There is no ESLint/Prettier config in the skeleton (the stack spec asks for `tsc --noEmit` plus the vitest suite).
Both are clean.

### Cross-component notes (unchanged from red phase)

The agent SSE/proposal shape and the missing `/stats` schema in the OpenAPI file (assumptions 1–3 above) still need
to be adopted by the agent spec and the contract owner. The dashboard implements exactly those shapes.

## Review round 1 (2026-10-17, developer)

### Result

**187 tests in 12 files: 187 passed, 0 failed, 0 skipped** (`npx vitest run`, three repeated runs all green). The
previous count was 157; 30 tests were added, plus 1 new file (`tests/error_boundary.test.tsx`). `npx tsc --noEmit -p .`
is clean and `npm run build` succeeds. There is no ESLint/Prettier config in this component (unchanged).

Every new or strengthened test was run red before the fix (the red run had 26 failing tests, each failing on the
behaviour under test). One test passed from the start: the lone-trailing-CR SSE test, which is kept as a regression
guard.

### Findings and resolutions

| # | Sev | Finding | Test first (red) | Fix |
|---|---|---|---|---|
| 1 | high | Chat confirm PATCHes model `fields` verbatim: no operator, no allow-list | `ac_ui_08`: Confirm is disabled until an operator is entered; PATCH body `toEqual({fields…, operator, note: "via agent proposal <id>"})`; model `operator`/`note`/unknown keys/bad types/`status: merged` are dropped; a proposal with no valid fields has no Confirm. The existing proposal tests now seed the operator through localStorage. | New `lib/agent.ts`: `sanitizeProposalFields` (allow-list of label, description, catalog_id, last_location, status enum, attributes object, needs_review boolean, with type and length checks, mirroring the agent's `ProposalFields`) and `proposalPatch(p, operator)`. New `state/operator.ts`: `useOperator`, a localStorage-backed hook (`nasa-omni.operator`) that keeps every user in sync. Chat has an "Operator" field; confirm is blocked without it. |
| 2 | med | Proposal card names the target only through the optional `summary`, which the real agent never sends | `ac_ui_08`: the fixture is exactly what the harness forwards (no summary). The card links to `/items/{id}` by label and shows `last_location: LAB1S2 → NOD2D4`. If GET fails, the link falls back to the id. | `ProposalCard` loads GET /items/{id} (and uses `current` if the agent ever forwards it) and renders a per-field current → proposed list. The model's free-text `summary` is deliberately **not** shown: the operator approves the verified diff, not prose that might disagree with the PATCH. |
| 3 | med | Overview Items tile counts person tracks (`by_status` covers all entities); the fixture was inconsistent | `ac_ui_09`: fixture `by_kind` changed to `{object: 37, person: 5}` (37 + 5 = 42 = live `by_status`). Asserts Items = 37 and neither 42 nor 45. | Items tile = `stats.by_kind.object` (the API already limits it to live entities). |
| 4 | med | Review queue capped at the API default of 50; badge only refreshed on mount and resolve | `ac_ui_07`: an API-like stub (default 50, max 500) with 60 items expects all 60 cards and `50 < limit ≤ 500`. "Showing 60 of 80" appears when the badge is larger. The badge refreshes when the review page opens and every 30 s (fake timers). | `listReview(status, limit = 500)` (the API maximum; GET /review has no offset). A "Showing N of M open review items" notice appears when the badge total exceeds the loaded cards. `ReviewCountProvider` polls every 30 s (`REVIEW_COUNT_REFRESH_MS`) and refreshes on review page mount. A sequence guard means only the latest response sets the count. |
| 5 | med | No ErrorBoundary; `done` payload cast without checks | `ac_ui_08`: `citations: "nope"` gives an error toast and the shell survives. Proposals with a numeric entity_id, missing or non-object `fields`, or non-uuid citations are dropped without a crash. Parser tests cover non-object and non-string reply payloads. `error_boundary.test.tsx`: a page that throws (vi.mock) keeps the nav and toasts and shows an alert panel, and navigating away recovers. | `validateChatResponse` in `lib/agent.ts` is called from `parseFrame`. Structural problems (not an object, non-string reply, non-array citations or proposals) become an `error` event. Individual malformed citations (entity_id must be a uuid) and proposals are dropped. New `components/ErrorBoundary.tsx` wraps `<Routes>` and is keyed by pathname, so it resets on navigation. |
| 6 | low | Inventory keeps the previous query's rows after a failed fetch | `ac_ui_02`: after a filter change, a 500 must leave no drill row and no "N items" line, and must show "Inventory unavailable". | `setPage(null)` on error. |
| 7 | low | Operator attribution inconsistent (edit and add forms) | `ac_ui_05`: the edit form operator is prefilled from the shared operator and sent. `ac_ui_06`: the add form has an Operator field, prefilled and posted as multipart `operator`, and omitted when empty. | Both forms use `useOperator`. Typing in either form updates the shared operator. **Not done:** making operator *required* for PATCH. AC-UI-5 says only "an operator/note field", and the existing AC tests (field-only PATCH bodies) pin it as optional. Since the field is prefilled, writes are attributed in practice. |
| 8 | low | A CRLF split across chunks creates a false frame boundary | `ac_ui_08` parser: `"event: token\r"` + `"\ndata: …\r\n\r\n"` yields exactly one token. | `SSEParser` holds back a trailing `\r` until the next push. The end-of-stream `\n\n` flush resolves it. |
| 9 | low | A 409 on resolve leaves an unresolvable card | `ac_ui_07`: the 409 test now expects the card to be removed and an info (non-alert) toast. A new 422 test keeps the card, shows an error toast and leaves the buttons enabled. | 409 removes the card, pushes an info toast and refreshes the badge. Other errors are unchanged. |
| 10 | low | Overview polling can stack requests, and stale responses can overwrite newer ones | `ac_ui_09`: the first /events request hangs and the second returns FRESH. Releasing the first afterwards must not show STALE, and no error toast may appear. | One `AbortController` per tick: each tick aborts the previous one, and responses are applied only if their signal is not aborted. `getStats`/`listEvents` accept a signal. |
| 11 | low | Viewport tests pass trivially in jsdom; forcing TZ=UTC hid a time-zone bug in URL date parsing | `ac_ui_02`: under `TZ=America/New_York` (the test asserts the switch took effect), `seen_after=2026-10-17T10:00` must parse to `10:00Z`. Explicit offsets are still honoured. | `filtersFromSearch` appends `Z` to offset-less date-times. The 1280/1920 tests are renamed "smoke", with a comment that the CSS width scan is the real check (a Playwright layout check could come later). |
| 12 | low | Clearing the label gives no message; Movements stale after a save | `ac_ui_05`: an empty label shows "Label is required", sets `aria-invalid` and keeps Save disabled even when another field changed. After saving a location, Movements and "last moved by" show the new move from a second GET /items/{id}. | `labelError()` in `lib/diff.ts`; the form blocks submit while the label is invalid. After a save, `ItemDetailPage` reloads GET /items/{id}. Fields just PATCHed (plus their `*_name` display fields) keep the PATCH response values; everything else comes from the fresh GET. |
| 13 | low | nginx has no security headers and no cache policy | `packaging.test.ts`: a CSP with `default-src 'self'`, `img-src 'self' blob: data:`, `connect-src 'self'`, `frame-ancestors 'none'` and no unsafe-* or `*`, plus nosniff, `X-Frame-Options: DENY` and Referrer-Policy. `location = /index.html` has no-cache and `/assets/` is immutable. Every location that uses `add_header` repeats the security set (nginx does not inherit it). | Template updated. `server_tokens off`. |

### Cross-component follow-ups (not edited here)

- **Agent harness** (`services/agent/nasa_agent/harness.py`): forward `current` (already computed by
  `_propose_update`) with each proposal. The dashboard uses it when present and otherwise falls back to
  GET /items/{id}.
- **Agent contract:** the dashboard sets `operator` (human) and `note: "via agent proposal <proposal_id>"` on confirm and
  drops any model-supplied operator/note. This matches `ProposalFields`' docstring. No contract change is needed.
