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

## UI-review fixes (2026-10-17, developer)

Input: `docs/ui-review/REPORT.md`, `findings.json` and the screenshots (live Playwright review). Spec: new
AC-UI-11 to AC-UI-23 in `docs/spec/40-dashboard.md`, with AC-UI-1/4/7/8 amended. API fields used come from the
contract in progress (`contracts/openapi/inventory-api.yaml`, inventory-api agent). All of them are optional, and
every AC is tested with and without them.

### Result

**269 tests in 16 files: 269 passed, 0 failed, 0 skipped** (`npx vitest run`, three runs in a row all green). The
previous count was 190; 79 tests were added in 4 new files:

| File | Tests |
|---|---|
| `tests/ac_ui_11_12_23_inventory_polish.test.tsx` | 20 |
| `tests/ac_ui_12_17_18_detail_overview.test.tsx` | 11 |
| `tests/ac_ui_13_16_review_ui.test.tsx` | 27 |
| `tests/ac_ui_19_22_chat_ui.test.tsx` | 21 |

`npx tsc --noEmit -p .` and `npm run typecheck` are clean. `npm run build` succeeds.

**Red evidence.** The new and amended test files were run against a clean `HEAD` checkout of `dashboard/` (temporary
git worktree, with sessionStorage cleared in setup):

- the review and chat files fail at import (`FrameImage`, `lib/markdown`, `state/chatSession` do not exist);
- the other files fail 36 of 52 tests (19/20 inventory, 11/11 detail/overview, 5 amended review tests, 1 amended
  edit test).

The one inventory test that passed is a negative guard ("no Location set fact without `location_set_*`").

**Amended existing tests** (intended behaviour changes):

- `ac_ui_07`: dismiss now goes through the confirm step (`dismissCard` helper). The merge button is named "Merge
  subject into this: <label> …".
- `ac_ui_05`: the "Movements" list is now "Activity".
- `tests/setup.ts` also clears sessionStorage and the item cache after each test.

### Findings -> fix -> tests

| Finding | Pri | Fix | Tests |
|---|---|---|---|
| UI-03 duplicate cards cannot support a merge | P1 | `DuplicateCompare` puts the subject (`subject`, or cached GET /items/{entity_id}) next to a candidate grid. Each shows: thumbnail (`thumbnail_url` → `snapshot_url`), label link, short id, catalog, location name + code, UTC + relative last seen, confidence bar, sightings, similarity. Each candidate has a uniquely named "Merge subject into this" button (`merge_into`). "Not a duplicate (keep both)" posts `assign_catalog` with the subject catalog and label: the contract has no keep action, and `dismiss` would mark the subject dismissed. The empty state says so and links to an inventory search. `candidates_source: suggested` is labelled. | `ac_ui_13_16` AC-UI-13 ×9 |
| UI-04 moved-by fragments, only `moved` shown | P2 | Column "Last handled" from `last_handled` (verb + `actorPhrase` + UTC), falling back to `last_moved_*`. Detail "Activity" lists every event type, newest first: "from A to an unknown location" (no `?`), names + codes, evidence frame with lightbox. | `ac_ui_11_12_23` AC-UI-12 ×7, `ac_ui_12_17_18` AC-UI-12 ×3 |
| UI-07 relative-only time, UTC hover-only | P2 | `Timestamp` shows `<time>` "YYYY-MM-DD HH:MM UTC" plus the relative time visibly (full seconds kept in `title`). Used in the inventory, detail facts, review cards and tiles, overview feed and citation chips. | `ac_ui_11_12_23` AC-UI-11 ×3 (+ assertions in review/overview/chat files) |
| UI-08 whole frames, no box, no enlarge | P2 | `FrameImage`: validated `image_bbox` overlay (CSSOM percentages, CSP-safe), 320 px review images, click-to-enlarge `Lightbox`. The lightbox has `role=dialog`, `aria-modal`, focus on Close, a Tab trap, Esc/backdrop/Close to close, and returns focus to the trigger. Also used for the detail hero and activity evidence. | `ac_ui_13_16` AC-UI-14 ×5 |
| UI-09 operator edit looks like a sighting | P2 (UI part) | Facts now show "Last sighting" (last_seen_at) and "Location set by X at T" (`location_set_by/at`) separately. The API no longer bumps last_seen_at on edits (inventory-api agent). | `ac_ui_11_12_23` ×2 |
| UI-10 one-click dismiss, consequence unstated | P2 | "Dismiss…" opens an inline confirm step whose text depends on the kind: it marks the item dismissed and hides it from the default inventory; on duplicates it suggests keep both; with no entity, no item is affected. Cancel closes the step. | `ac_ui_13_16` AC-UI-15 ×4, `ac_ui_07` amended |
| UI-11 no filters, sort or item link | P2 | Kind tabs with `/stats` counts (aria-pressed), location select, debounced search, created/observed × newest/oldest sort. URL-backed and 1:1 to GET /review params. `X-Total-Count` drives "Showing N of M"; without it the filters are applied client-side. "Open item" and "History" (`#history`, scrolls) links. A ref-based URL merge stops the debounced search from overwriting a newer sort. | `ac_ui_13_16` AC-UI-16 ×9 |
| UI-12 history flooded by pipeline audits | P2 | `groupTimeline` folds consecutive identical audit rows ("14 × update by pipeline"), adds up API `count`/`first_at` (the API collapses by default), and has a disclosure for the folded rows. Kind filter chips with counts. | `ac_ui_12_17_18` AC-UI-17 ×5 |
| UI-13 overview ids and fragments | P2 | `entity_label` (falling back to "item <short id>"), `*_location_name` (falling back to codes), `actorPhrase`, one-sided routes "to/from/at X", no route when both ends are missing, `Timestamp`. | `ac_ui_12_17_18` AC-UI-18 ×3 |
| UI-06 raw Markdown and opaque citations | P2 | `lib/markdown.tsx`: safe subset built from React elements only. Raw HTML stays text and Markdown links never become anchors. Only cited UUIDs become item links named by label. Citation chips read "label (location) · time" via the `state/itemCache` (60 s TTL, failures cached as null). | `ac_ui_19_22` AC-UI-19 ×7, AC-UI-20 ×3 |
| UI-05 no visible progress (UI part) | P2 | `role=status` "Thinking: looking up the inventory…" with elapsed seconds (aria-hidden, so it is not re-announced) and tools from optional SSE `status` / `tool_call` / `tool` / `tool_start` events (validated). "Looked up with: …" from `tool_calls`. Stop button aborts and keeps partial text marked "Stopped". | `ac_ui_19_22` AC-UI-21 ×4 |
| UI-02 errors only as a toast (UI part) | P1 (UI part) | In-stream `error` and HTTP failures also appear inline in the conversation ("The agent could not answer: …"). | `ac_ui_19_22` AC-UI-21 ×1 |
| UI-14 conversation lost on navigation | P2 | `state/chatSession.ts` stores messages and a `session_id` in sessionStorage (`nasa-omni.chat.v1`). An interrupted stream is restored as a failed question. "New chat" clears it and makes a new session id. | `ac_ui_19_22` AC-UI-22 ×4 |
| UI-19 failed request loses the question | P3 | The question is marked "No answer" with Retry, which drops the failed turn and resends. | `ac_ui_19_22` AC-UI-22 ×1 |
| UI-01 follow-ups fail (UI mitigation) | P1 (UI part) | Requests now carry `session_id` and only completed turns (no error, stopped or failed turns), capped at 40 messages / 60 000 characters and starting with a user turn. The root cause (agent tool loop with history) is the agent agent's. | `ac_ui_19_22` buildHistory + session tests |
| UI-15 locale 12 h date pickers | P3 | 24 h `YYYY-MM-DD HH:MM` UTC text inputs (commit when complete or cleared, inline format error) plus quick ranges "Last 1 h / 24 h". | `ac_ui_11_12_23` ×3, existing `ac_ui_02` time-range test unchanged |
| UI-17 snapshot cropped | P3 | `.thumb-lg` / `.thumb-md` use `object-fit: contain`. Frames render at their natural aspect. | CSS only (jsdom does no layout) |
| UI-18 malformed id gives a generic error | P3 | A non-UUID id shows "Item not found." without a request; 422 is treated like 404. | `ac_ui_11_12_23` ×2 |
| UI-20 add form explains only the image | P3 | "Label is required" / "Location is required" on blur, with `aria-invalid` and `aria-describedby`. The disabled submit is described by "To add the item, provide …". | `ac_ui_11_12_23` ×2 |
| UI-21 filter bar polish | P3 | `aria-valuetext` on the slider, a "Reset filters" button (keeps sort), a "Skip to content" link to `<main id="main">`, and the needs-review checkbox wrapped and aligned like the other fields. | `ac_ui_11_12_23` ×3 |

### Deferred (not dashboard-side)

- **UI-01 / UI-02 / UI-05 root causes** (agent forgets tools with history, no events tool, buffered tokens): agent
  agent. The dashboard already renders progressive tokens and any `status` events the agent starts to send.
- **UI-16** case-insensitive label sort: inventory-api (`ORDER BY lower(label)`). The dashboard passes `sort=label`.
- **UI-22** split a person track: needs a pipeline fix and a new API endpoint. There is no UI without the endpoint.

### Adversarial self-review

- **XSS:**
  - Markdown, model text, labels, descriptions and actor text are all React text nodes.
  - Tests cover `<img onerror>`, `<script>` and `javascript:` links, in the renderer and end to end.
  - A source scan asserts that no raw-HTML API is used.
  - The bbox style values are clamped numbers.
  - SSE tool names must match `^[A-Za-z0-9_.-]{1,64}$`.
- **CSP:** no inline scripts. The styles are CSSOM properties (allowed under `style-src 'self'`). The spinner uses a
  CSS keyframe in `theme.css` and respects `prefers-reduced-motion`.
- **a11y:**
  - Every new control is labelled; the existing labelled-controls tests still pass.
  - Accessible names are unique where buttons repeat (merge, open item, history), and keep the visible text first
    (label-in-name).
  - The lightbox follows the dialog pattern.
  - The status counter is aria-hidden to avoid live-region chatter.
  - The kind tabs and chips use `aria-pressed`.
- **Error states:**
  - Subject load failure: "details unavailable".
  - Review list failure: toast plus an empty state; filtered empty has its own text.
  - Corrupt sessionStorage: a fresh chat.
  - The cache stores failures, so there are no retry storms.
- **Layout at 1024 px:** no browser in this environment, so this was checked by reading the CSS only. Below 1400 px
  the review list is one column. Below 1100 px the duplicate compare stacks, the review filters use 2 columns and the
  candidate grid auto-fills 180 px tiles. The table stays in `.table-wrap` (`overflow-x: auto`). A Playwright re-run
  of the reviewer's 1024 px pass is recommended.

## AC-UI-24 citation labels (integration 2026-10-18)
- **Found by:** the agent's handoff note. `sanitizeCitation` dropped the agent's new label and location fields.
- **Red:** 2 of 3 failed (the pass-through cases); the guard for oversized or non-string labels passed.
- **Green:** the sanitizer passes the fields through and the chips prefer them. 272 tests pass; typecheck and build
  are clean.

## UI review round 2 fixes (2026-10-18, docs/ui-review/round2/)

Strict red → green this round: each test file was written first and run against the unchanged code, and the failure
reason was checked before any implementation. Baseline: 272 tests. Final: **341 tests** (69 new), `tsc --noEmit` clean,
`npm run build` clean, axe 0 violations.

### Red evidence

| Cycle | Test file | Red | Failure reason (checked) | Passed on red (why) |
|---|---|---|---|---|
| 1 | `tests/ui2_wording_markdown.test.tsx` | 21 / 22 | `eventPlace` / `eventActor` / `eventSentence` / `legacyEventSummary` / `timelineLabel` not exported; Markdown: two `<ol>` for a nested list, no `start`, `_x_` not `<em>` | XSS guard for nested list text (already safe) |
| 2 | `tests/ui2_detail_overview.test.tsx` | 16 / 19 | no "merged" region, no "Merged from" list, no `detail-media` box/caption, Activity still "Moved by person (ui-review-2) at …", history "-> ?", overview "from X" for added/used | 2 negative guards (no banner / no "Merged from" on a live item); the "9 pipeline updates" detail row (`timelineLabel` landed in cycle 1) |
| 3 | `tests/ui2_review.test.tsx` | 19 / 20 | merge posted on first click (the card disappeared, so the confirm query hit a detached node), toasts "Review resolved (assign catalog)", API order instead of score order, no chips, no `bbox-approx`, repeated region names (3 unique of 5), no "Showing x of y", 8 tiles shown, no footer | guard: unfiltered queue shows no count line |
| 4 | `tests/ui2_layout_css.test.tsx` | 5 / 5 | no `.bbox-approx` / `.review-footer` rules, `.filters` `align-items: end`, no Sort min-width, "Needs review" outside the Status group | – |
| 5 | axe follow-up (2 tests in `ui2_detail_overview`) | 2 / 2 | enlarge-button name lacked "(approximate box)"; links in the banner sentence not underlined | – |
| 6 | contract follow-up (1 test in `ui2_detail_overview`) | 1 / 1 | history entries' own `actor_kind` / `location_role` (AC-API-38) were ignored by the summary rewrite | – |

One test bug was fixed during green: the score-order test used `getAllByRole('listitem')`, which also picked up the
nested reason chips; it now reads the list's direct children. After the axe re-run, adding "(approximate box)" to the
name was not enough (axe still counted the visible "approximate" text node inside the button). The tag became CSS
generated content (`.bbox-approx::after`), and two of my own assertions were amended to match.

**Amended existing tests** (intended behaviour changes):

- `ac_ui_04`: Activity reads "Moved by <actor> from A to B · <UTC>" (was "… at <UTC>, from …").
- `ac_ui_12_17_18`: same Activity format; the overview route "A → B" is now "from A to B".
- `ac_ui_07`, `ac_ui_13_16`: merge clicks "Confirm merge". The subject region query is anchored (`/^subject/i`)
  because the candidates landmark name now contains the subject label.

### Findings → fix → tests

| Finding | Fix | Tests |
|---|---|---|
| UI2-05 merge: no confirm, no undo, dead-end source page | `MergeConfirm` group (both thumbnails, consequence, focus on Confirm, Cancel). Toast with "Open <target>" link + "Undo merge" (20 s) → `unmergeItem` → reload queue. `MergedBanner` on the item page ("Merged into <label> at <UTC>", operator-gated Unmerge, inline 409 text); `MergedFromList` on the target. Toasts gained optional `link` / `action`. | `ui2_review` UI2-05 ×6, `ui2_detail_overview` UI2-05 ×8 |
| UI2-06 (UI side) unranked candidates | `rankCandidates` (score desc, unscored after, stable), "Match score" fact, "Why it matched" chips (strings only, ≤6×60 chars), heading "ranked by match score" / "suggested, unranked". | `ui2_review` UI2-06 ×3 |
| UI2-07 / UI-08 boxes | `FrameImage` `bboxSource` + `caption`: detector solid, omni dashed + padded 15 %/side + "approximate" tag + "(approximate box)" in the button name, no box → "Whole frame". On review images, candidate tiles (snapshot + `snapshot_bbox`), hero, history sighting frames (matched by `sighting_id`). | `ui2_review` ×3, `ui2_detail_overview` ×5, `ui2_layout_css` ×1 |
| UI2-09 / UI-04 / UI-13 wording | `format.ts` `eventPlace` / `eventSentence` shared by Activity, History event rows (by event id, else `legacyEventSummary` rewrites "used USOS -> ? by …") and the overview (`eventRoute` = `eventPlace`). `location_role` honoured. "to an unknown location" only for one-sided moves. | `ui2_wording_markdown` ×9, `ui2_detail_overview` ×5 |
| UI2-10 operators shown as people | `eventActor`: `actor_kind`, else `source` operator/api, else (Last handled) actor = `location_set_by` → "operator <id>". | `ui2_wording_markdown` ×4, `ui2_detail_overview` ×3 |
| UI2-11 "9 × 9 pipeline updates" | `timelineLabel` replaces a leading count with the group total; "N ×" only for client-folded runs. | `ui2_wording_markdown` ×4, `ui2_detail_overview` ×1 |
| UI2-12 list numbering | Markdown list model with nested sub-lists, `<ol start>`, `_em_` outside words. | `ui2_wording_markdown` ×5 |
| UI2-13 no filtered count | "Showing x of y open review items." when filtered (server or client filtering), truncation line kept, "Tab counts are totals…" note. | `ui2_review` ×3 |
| UI2-14 axe landmark-unique | Section `aria-label`s "Subject: <label> <id>, review <id>" / "Possible matches for …". | `ui2_review` ×1, axe run |
| UI2-15 toast jargon | Outcome text built by the card: "Kept both items: …", "Classified as <catalog>.", "Created catalog item …", "Dismissed …", "Merged … into …". | `ui2_review` ×2 |
| UI2-17 / UI-21 filter alignment, Sort clipped | Grid `repeat(auto-fill, minmax(14rem, 1fr))`, `align-items: start`, Search spans 2, Sort `min-width: 14rem`, "Needs review" inside the Status fieldset (full-width row), Reset with a label spacer. | `ui2_layout_css` ×3 + Playwright |
| 1024 px duplicate cards (actions ~1,400 px down) | At most 4 candidates until "Show N more"; actions in a sticky `.review-footer`; compact confirm. | `ui2_review` ×2, `ui2_layout_css` ×1 + Playwright |

### Playwright measurements (vite preview, mocked routes, `dashboard/scripts/ui_check.py`)

The fixture is a duplicate card with 8 scored candidates (omni subject box, one detector candidate box), plus a merged
item, its target and an overview feed. The pages were checked at 1024×1000 and 1440×1000.

- **Filter bar**, input top y per row:
  - 1024 px: Search/Kind/Location at 157; Seen after/Seen before/Quick range at 223; Sort/Reset at 309.
  - 1440 px: Search/Kind/Location at 157; Seen after/Seen before/Quick range/Sort/Reset at 223.
  - The date inputs are no longer lifted. "Needs review" is inside the Status row. Form overflow is 0.
  - Sort select: the text "Last seen (newest first)" is 152 px wide; the select's inner width is 211 px at 1024 and
    246 px at 1440, so it is not clipped.
- **Duplicate card**:
  - From header to actions footer in the page flow: 511 px at 1024 and 576 px at 1440 (was about 1,400 px).
  - "Keep both" ends at y = 939 of 1000 on load, and stays in the viewport after scrolling 300 px into the card
    (sticky footer).
  - 4 tiles are shown. Page horizontal overflow is 0.
  - The merge confirm is visible in the footer. The toast reads "Merged camera 000000c8 into camera 00000064. Open
    camera 00000064 / Undo merge".
  - Undo answered with 409 gives "This merge can't be undone safely: … Nothing was changed."
- **axe-core** (/, /review, /review with the merge confirm open, filtered /review, the merged item, the merge target,
  /overview), at both widths: 0 violations. This includes the round-2 landmark-unique issue. The run first found 2
  new issues of mine (label-content-name-mismatch on the omni tag, link-in-text-block in the banner); both are fixed
  and covered by tests (cycle 5).
- Console: only the two mocked 409 responses.

### Deferred / not dashboard-side

- **UI2-06 ranking itself, UI2-07 box accuracy:** inventory-api / perception. The UI renders `score`,
  `match_reasons` and `*_bbox_source` as they arrive.
- **UI2-11 disclosure for server-collapsed rows:** the API sends only `audit_ids`, so there is nothing to list without
  an extra `?collapse=false` request. Not done.
- **UI2-12 ISO timestamps in chat text:** left to the agent prompt (optional in the finding).
- **Inventory list thumbnails with boxes:** not requested (only tiles and the hero were). `Thumb` is unchanged.

## UI review round 3 fixes (2026-10-18, docs/ui-review/round3/)

Strict red → green again. Each new test file was written first and run against the unchanged code, and the failure
reasons were checked before any implementation. For cycle 1, the new module `src/lib/person.ts` existed only as an
empty skeleton (`export {}`), so each test failed on its own assertion rather than the whole file failing on an import
error. Baseline: 341 tests. Final: **389 tests** (48 new), `tsc --noEmit` clean, `npm run build` clean, axe 0
violations on 28 page states.

### Red evidence

| Cycle | Test file | Red | Failure reason (checked) | Passed on red (why) |
|---|---|---|---|---|
| 1 | `tests/ui3_wording.test.ts` | 23 / 23 | `personLabel` / `personAppearance` / `appearanceChips` / `reviewReason` / `safePersonImage` / `isPersonEvent` / `personEventSentence` / `isCropUrl` / `cropDisplayWidth` / `auditText` / `hideMirroredAudits` not exported; `eventSentence` still "Moved by person (…)" for a person and "to an unknown location"; `timelineLabel` "2 × update by pipeline" | – |
| 2 | `tests/ui3_pages.test.tsx` | 20 / 20 | rows and h1 read "person"; a `/crops/` thumbnail was rendered for a person; the citation read "person (USOS)"; "MOVED person … by person (spacesuit…)"; the internal "(15 live 'person' entities…)" text was shown; no appearance chips; "Whole frame (no box)"; "Catalog: ctb-half"; no `.review-footer-row`; tile "People seen (last hour)"; crop captioned "Whole frame"; "split by …"; the mirrored pipeline row was shown; banner and toast without ids; no h1 on either not-found page | – |
| 3 | `tests/ui3_layout_css.test.tsx` | 4 / 4 | no `.review-footer-row` rule; footer padding `4px 16px 12px`; no `.keep-hint` rule; no `.frame-crop` rules | – |
| 4 | `ui3_layout_css` side-by-side test (added after the first Playwright run) | 1 / 1 | no 861–1100 px media block, so the subject stacked above the candidates | – |

Cycle 4 comes from Playwright. After cycles 1–3 the footer was 51 px and every Merge button could be scrolled into
view. But at 1024×768, with the card at the top, the first Merge button ended at y = 960, below the fold and under the
footer (top at 717). That happened because the round-2 1100 px rule stacks the subject above the candidates. The
new test went red first; the fix keeps a 200 px subject column beside the candidates between 861 and 1100 px.

Test bugs fixed before green (in my own new tests only):

- The overview person-event test first asserted that the line never contains "spacesuit". The person label's
  appearance comes from the event's `actor_desc`, which for a person's own event is that person's appearance. The
  test now asserts the label "Person: spacesuit, dark visor" and that there is no "by".
- The unmerge-toast test first expected "Merge undone: …". I chose one verb for both toasts instead: "Unmerged:
  <label> <id> is a separate item again." is used for the item-page Unmerge and for the toast Undo.

**Amended existing tests** (intended behaviour changes):

- `ui2_wording_markdown`, `ac_ui_12_17_18`: a one-sided move reads "from X (destination not seen)". A folded "update
  by pipeline" run reads "N pipeline updates"; other folded summaries keep "N × …" (a new assertion covers that).
- `ac_ui_12_17_18`: "14 pipeline updates" / "9 pipeline updates" (was "14 × update by pipeline").
- `ui2_detail_overview`: the banner reads "Merged into cargo transfer bag 44444444 at …".
- `ui2_review`: the Undo toast reads "Unmerged: headset 55555555 is a separate item again."
- `ac_ui_09_overview`: the tile is found as "Person tracks seen".
- `ac_ui_13_16_review_ui`: on a duplicate card the catalog select is behind "Choose catalog…", and the keep-both
  description points there.

### Findings → fix → tests

| Finding | Fix | Tests |
|---|---|---|
| UI3-03 "person / Crew member" rows | `lib/person.ts`: `personAppearance` (drops "person:", the internal count note, "wearing", "person (…)" and trailing punctuation; generic words are not an appearance) and `personLabel` "Person: <appearance>" (cut at 40 chars with "…"), else "Person <short id>". `displayLabel` / `displayLabelWithId` are used for inventory rows, the detail h1, review subject and candidates (subject appearance falls back to the review description), merge-confirm captions, the overview and chat citations / id links (the cached item wins over the agent's "person"). `safePersonImage` drops `/crops/`, `/exemplars/` and `/faces/` URLs; `Thumb` and `FrameImage` take a `person` flag and show the placeholder instead. | `ui3_wording` ×8, `ui3_pages` ×4 |
| UI3-04 person events / "to an unknown location" | `isPersonEvent` (`entity_catalog_id === 'crew-member'`, `entity_kind`, or the item kind) and `personEventSentence`: "seen in X", "left X", "arrived in X", "moved from A to B", with no actor. Used in the overview (a `person` badge, the person label and the sentence), Activity, history event rows (also the legacy summary) and Last handled ("Seen moving"). `eventPlace`: a move with no destination reads "from X (destination not seen)". | `ui3_wording` ×4, `ui3_pages` ×4 |
| UI3-05 person duplicate reviews | `reviewReason` turns "(N live 'X' entities would exceed the expected count of M)" into "More person tracks than the expected crew size — these may be the same person" (objects: "More X items than the expected count (M) — some may be duplicates"). The cleaned text is used in the description and alt texts, and the reason is shown on its own line. "Appearance" chips (colour/shape words + garment, at most 6) appear for the subject and each candidate. A person image with no box is captioned "Whole frame, several people may be visible". | `ui3_wording` ×5, `ui3_pages` ×3 |
| UI3-06 "People seen" tile | Label "Person tracks seen (last hour)", hint "tracks, not people: one person can have several". The API gives no sighting count. | `ui3_pages` ×1, `ac_ui_09` amended |
| UI3-07 crops | `isCropUrl` (`/crops/`) or a `crop` prop: `.frame-crop` (natural size, `max-width: 100%`, `object-fit: contain`), caption "Close-up crop". The lightbox enlarges crops to at most 2× (`cropDisplayWidth`). | `ui3_wording` ×3, `ui3_pages` ×1, `ui3_layout_css` ×1 |
| UI3-08 catalog slugs | `Catalog: <display name>` (`catalog_name`, else the /api/catalog name) with `title=<slug>`. The keep-both hint uses the name too. | `ui3_pages` ×1 |
| UI3-09 171 px footer | The duplicate footer is one flex row: "Choose catalog…" (disclosure with search + select + Classify), "New catalog item", "Not a duplicate (keep both)" and "Dismiss…". The keep-both hint is screen-reader text and the button tooltip. Padding is 8 px, `max-height: 70vh`. At 861–1100 px the subject stays in a 200 px column. | `ui3_pages` ×1, `ui3_layout_css` ×4 (incl. cycle 4) + Playwright |
| UI3-11 wording | `auditText`: "split" reads "Unmerge", "update by pipeline" reads "Pipeline update"; folded runs read "N pipeline updates". `hideMirroredAudits` drops single pipeline-update rows within 2 s of a sighting (also from the chip counts). The banner and both unmerge toasts name items as "<label> <short id>". | `ui3_wording` ×3, `ui3_pages` ×3 |
| UI3-12 no h1 on not-found | `<h1>Page not found</h1>` (App NotFound), `<h1>Item not found</h1>` / `<h1>Item unavailable</h1>` on the item page. | `ui3_pages` ×2 + axe |

### Playwright measurements (vite preview, mocked routes, `dashboard/scripts/ui_check.py`)

The round-2 fixture gained 3 person items (one whose snapshot is a crop), a person event pair and a one-sided object
move, a person duplicate review (internal reason, no box, 3 person candidates), a 220×140 crop item, mirrored and
"split" history rows, and the two not-found pages. Viewports: **1024×768** and **1440×1000**.

- **Duplicate footer:** 51 px at both sizes (was 171 px); 114 px with the catalog search open.
  - Round-2 camera card scrolled to the top: "Keep both" ends at y = 760 of 768 and 992 of 1000. It is still in the
    viewport after scrolling 300 px into the card. Page overflow-x is 0.
- **Merge reachability:** every "Merge subject into this" button (10 at each size) was scrolled to the centre, and
  `elementFromPoint` hit the button itself, not the footer: all reachable.
  - Person card at the top: the first Merge button ends at y = 609 at 1024×768 (footer top 717) and at 537 at
    1440×1000. Before cycle 4 it ended at y = 960 at 1024×768.
- **Person rows:** "Person: black shirt, grey pants", "Person: short hair, blue shorts, orange top | Seen moving
  2026-10-18 03:10 UTC", "Person 000001f6". 0 thumbnails request `/crops/`.
- **Person detail:** h1 "Person: short hair, blue shorts, orange top"; Activity "Left Node 1 Unity (NOD1) · …".
- **Crop hero:** 220×140 natural, rendered 220×140 (scale 1), `object-fit: contain`, caption "Close-up crop. Click
  to enlarge.". The lightbox scale is 1.45 (≤ 2; limited by the available width).
- **Person review:**
  - The description reads "person: grey hoodie", followed by the readable reason line.
  - Subject caption: "Whole frame, several people may be visible. Click to enlarge.".
  - Chips: "grey hoodie" for the subject, "grey hoodie / black pants" and "blue shirt / tan pants" for candidates.
  - Candidates are named "Person: grey hoodie, black pants", "Person: blue shirt, tan pants", "Person 00000200".
  - Catalog lines read "Catalog: Crew member [title=crew-member]".
- **History:** "Unmerge by ui-review-3"; the 22:30:01 pipeline update next to the 22:30:00 sighting is hidden; "9
  pipeline updates (sighting refresh: …)" is kept.
- **Overview:**
  - Tile "Person tracks seen (last hour)".
  - Person events read "PERSON Person: spacesuit, dark visor 000001f5 left Node 1 Unity (NOD1)" and "PERSON Person:
    grey t-shirt 000001f4 seen in Cupola (CUP1)".
  - The object move reads "… from US Orbital Segment (USOS) (destination not seen)".
- **Not found:** h1 "Item not found" and "Page not found".
- **axe-core:** 0 violations on 28 page states at both sizes. That covers the round-2 pages plus /?kind=person, the
  person detail, the crop detail (after the lightbox), the duplicate queue, the queue with "Choose catalog…" open,
  /items/<missing> and /nope.
- **Console:** only the two mocked 409s and the expected 404s for the missing id.

### Deferred / not dashboard-side

- **UI3-01 (people count), UI3-02 (private-window summaries), UI3-10 (chat change-list phrasing):** agent and
  inventory-api; not touched (services/agent and services/perception are owned by others).
- **UI3-05 server side:** person-region boxes on sightings (perception), appearance-based candidate scoring and
  conflicting-attribute reasons, rate-limiting person duplicate reviews (inventory-api). The dashboard draws any box
  that arrives, and shows `match_reasons` and the client-side appearance chips.
- **UI3-04 server side:** not attaching an `actor_desc` to a person's own move (or `actor_kind: self`). The dashboard
  already drops the actor clause for person subjects. `entity_kind` / `entity_description` on events are read when
  sent (optional).
- **UI3-06 sightings count:** the API has no per-hour person sighting count, so the tile is relabelled rather than
  recounted.
- **UI3-11 server fold:** sighting-refresh audit rows are hidden client-side when they mirror a sighting. Folding them
  in `/items/{id}/history` itself is left to inventory-api.

## UI review round 4 fixes (2026-10-18, docs/ui-review/round4/)

This round was strict red → green again. Each new test file was written first and run against the unchanged code, and
each failure was checked to fail for the expected reason. The suite went from 389 to **422** tests (26 files).
`npx tsc --noEmit -p .` and `npm run typecheck` are clean, and `npm run build` succeeds.

Every new API field is optional: `entity_appearance`, `entity_last_location(_name)`, `by_location_level` and
`snapshot_withheld_reason`. Each behaviour is tested with the field and without it.

### Red evidence

| Cycle | Test file | Red | Failure reason (checked) | Passed on red (why) |
|---|---|---|---|---|
| 1 | `tests/ui4_round4.test.tsx` | 19 / 24 | The overview events failed on several counts: "Person 737f632b" (`entity_appearance` and the cached description were ignored); "seen" with no place; "left US Orbital Segment (USOS)" while still in USOS; "used laptop … by person (…)" with no place; "moved camera" with nothing after it. Also: no module/segment line; "merge by …" and "split by …" not in sentence case; a 76×62 crop stayed the hero (no `.hero-frame`); no 2× fallback or "small close-up"; a bare "no image" for a private-window item; no "Not inventory" button (4 tests); the timer test saw **[7]**, the previous question's 7 s painted before the reset. | 5 guards: older events keep the round-3 wording; older stats show no line; a crop ≥ 200 px stays the hero; a null withheld reason keeps "no image"; duplicate cards have no structure action |
| 1 | `tests/ui4_layout_css.test.tsx` | 5 / 6 | No `max-width`/`min-width` on `.frame-trigger`; `.frame img` had no `max-width`; `.review-body` was `max-content 1fr`; no `min-width: 0` on `.candidate`, `.review-card`, `.review-info` or `.detail-media`; no `.hero-inset` | guard: `.frame-crop img` keeps `width: auto` (no CSS upscaling) |
| 2 | `ui4_round4` +1 (found by Playwright) | 1 / 1 | The overview borrowed a cached **object's** description as a person's appearance ("Person: black, handheld"). The mock's catch-all /items/{id} returned a camera. The fix only uses the cached description when that item is a person. | – |
| 2 | `ui4_layout_css` +2 (found by Playwright) | 2 / 2 | Two CSS problems. An appended `.frame-trigger { max-width: 100% }` beat `.frame-lg`'s 640 px cap: the hero frame was 686 px at 1440. Also, `min-width: 0` let the inset close-up shrink to 64×52 in its flex row at 1024. The generic cap now sits in the original `.frame-trigger` rule, and the inset has `.hero-inset .frame-trigger { flex: none }`. | – |

**Playwright red** (`scripts/ui_check.py` `round4`, HEAD `theme.css` with the new fixtures): `/review?kind=unknown_object`
with a 1000×280 crop had `scrollWidth` **1256** at 1024 px and **1960** at 1440 px. The review measured 1186 and 1890
with a 915 px crop. The crop rendered 1000 px wide in a 976 / 688 px card, and "New catalog item" was off screen. The
script exits 1 on this.

**Amended existing tests** (intended behaviour changes, 7 tests in 5 files):
- `ui3_wording` ×2: "Unmerged by", "Merged by"; `personEventSentence({event_type: 'moved'})` → "seen (place not
  seen)".
- `ui3_pages` ×1: "Unmerged by ui-review-3".
- `ui2_wording_markdown` ×2: "2 × Updated by …", "Edit by alice".
- `ac_ui_12_17_18` ×1 and `ui2_detail_overview` ×1: the overview reads "in X" instead of "at X" for one-place events
  (Activity and history keep "at X").

### Findings → fix → tests

| Finding | Fix | Tests |
|---|---|---|
| UI4-02 wide crops overflow the card (P2) | The original `.frame-trigger` rule gets `max-width: 100%; min-width: 0`. `.frame img` gets `max-width: 100%; height: auto`. `.review-body` becomes `minmax(0, 340px) minmax(0, 1fr)` and `.review-media` gets `max-width: 340px`. `.candidate`, `.review-card`, `.review-info` and `.detail-media` get `min-width: 0`; `.candidate` also gets `max-width: 100%`. Crops still render at natural size; the 2× limit stays in code. | `ui4_layout_css` ×8; Playwright `round4` |
| UI4-11 76×62 crop as the hero (P3) | `DetailHero` reads the natural size on load (`FrameImage onNaturalSize`). If the long side is under 200 px, the hero is `bestFrame(sightings)`: the newest frame with a valid box, else the newest frame, never a crop, and only safe scene frames for persons. The crop becomes an inset "Close-up of the object (76×62 px …)". With no frame: `upscale` → `cropDisplayWidth` (≤ 2×) and "Small close-up …". | `ui4_round4` ×4 |
| UI4-05 structure unknowns (P2, UI side) | "Not inventory (station structure)…" on unknown cards. It needs the operator and has a confirm step; confirming posts `{action: 'dismiss', note: 'not_inventory', operator}` (the contract has a free `note`). It closes the plain Dismiss step, and the reverse. The toast reads "Marked … as station structure (not inventory)". | `ui4_round4` ×5 |
| UI4-06 overview events (P2) | Person rows: `entity_appearance`, else `entity_description`, else the cached item description (only for person items), else `actor_desc`. `personEventSentence(e, {lastLocation})` always gives a place ("seen (place not seen)" as the last resort) and turns "left X" into "seen in X" while the subject is still in X; this is also used for Activity and history rows. Object rows: `objectEventSentence`, "<verb> in X by <actor>" / "<verb> (last seen in X)" / "<verb> (place not seen)". | `ui4_round4` ×8 |
| UI3-11 casing | `auditText`: `AUDIT_OPS` gives sentence-case past tense ("Merged by", "Unmerged by", "Created by pipeline", "Updated by", "Dismissed by", "Classified by"); "Pipeline update" is kept; other summaries are capitalised. | `ui4_round4` ×2 (+3 amended) |
| UI4-07 module distribution (P3) | `locationLevelsText(stats)`: "Located to a module: n · Segment only (USOS/ROS): n" (+ "· No location: n" when above 0), shown under the tiles only when `by_location_level` is sent. | `ui4_round4` ×3 |
| UI4-10 private-window "no image" (P3) | `snapshot_withheld_reason: 'private_window'` → `FrameImage emptyText`. The hero shows "No image: first seen in a private area (crew privacy)" as readable text (`.thumb-withheld`, not aria-hidden). | `ui4_round4` ×2 |
| UI4-09 stale timer (P3, UI part) | `setElapsed(0)` in `send()`, batched with `setStreaming(true)`. The old effect-only reset painted the previous value first. | `ui4_round4` ×1 (MutationObserver records every painted counter value) |

### Playwright measurements (vite preview, mocked routes, `dashboard/scripts/ui_check.py`)

New fixtures:
- a 1000×280 "window" crop on an unknown card;
- a 76×62 crop item with two sighting frames (one boxed), and one with no frames;
- a 1000×280 crop hero and a private-window item;
- 4 events with the new fields, `by_location_level`, and "merge" / "create" audit rows.

Viewports: **1024×768** and **1440×1000**.

- **UI4-02, unknown queue:** `scrollWidth` 1024 / 1440 (overflow-x 0 at both sizes). The 1000 px crop renders 338 px
  wide, inside the card (976 / 688 px). Classify, New catalog item, Not inventory and Dismiss are all on screen and are
  the `elementFromPoint` hit. At 1024 they sit in one row, x = 397–971. Overflow-x is still 0 with the structure confirm
  open.
- **Wide crop hero:** 478×134 at 1024 and 686×192 at 1440 (scale 0.48 / 0.69), inside the column, overflow-x 0.
- **UI4-11:**
  - Tiny crop with frames: the hero is the boxed frame (478×269 at 1024; 638×359 at 1440, within the 640 px cap). The
    inset crop stays 76×62 (scale 1).
  - With no frame: 152×124 (scale 2), captioned "Small close-up …".
  - Round 3's 220×140 crop hero is unchanged (scale 1).
- **UI4-10:** the hero reads "No image: first seen in a private area (crew privacy)".
- **UI3-11:** the history shows "Unmerged by ui-review-3", "Merged by ui-review-4" and "Created by pipeline".
- **UI4-06 / UI4-07:**
  - Module line: "Located to a module: 35 · Segment only (USOS/ROS): 30".
  - Person rows: "Person: blue shirt and white gloves … seen in US Orbital Segment (USOS)" (was "left USOS") and
    "Person: grey shirt, black shorts … seen in Cupola (CUP1)".
  - Object rows: "laptop … used in Node 1 Unity (NOD1) by person (grey t-shirt, black shorts)" and "camera … moved
    (place not seen)".
- **Regression checks:** every duplicate Merge button is still reachable at both sizes, and the round-2 review page
  overflow-x is 0.
- **axe-core:** 0 violations on 42 page states. The new states are the unknown queue with the wide crop, the structure
  confirm, the three crop heroes, the private item and the overview, at both sizes.

### Deferred / not dashboard-side

- **UI4-01 (P1 face crop):** purged by the orchestrator. Prevention (no object crop overlapping a person box; no
  exemplar from such a crop) is perception / inventory-api work. The dashboard already never requests crops for person
  items.
- **UI4-05 server side:** a structural-class denylist in perception, and seeding real loose unknowns for the demo. The
  `not_inventory` note is free text in the contract. A reason enum, if one is added, would be a contract change.
- **UI4-03, UI4-04, UI4-08, UI4-09 latency:** agent (services/agent). Only the timer reset is dashboard-side.
- **UI4-06 server side:** the dashboard renders "left X" as "seen in X" when `entity_last_location` (or the cached item)
  is still X. Not emitting those events at all is the reconciler's job.
- **UI4-07 inventory chip:** a per-row "module not resolved" chip in the inventory table was not added. Only the overview
  line was in scope.
- **UI4-10 sightings:** per-sighting private-window captions in the history need an `is_private` flag on sightings,
  which does not exist yet. Only the hero uses `snapshot_withheld_reason`.

## UI review round 5 fixes (2026-10-18, docs/ui-review/round5/)

Strict red → green. `tests/ui5_round5.test.tsx` was written first and run against the unchanged code, and each
failure reason was checked. Suite: **422 → 471** tests (28 files). `tsc --noEmit` and `npm run build` are clean.

### Red evidence

| Cycle | Test file | Red | Failure reason (checked) | Passed on red (why) |
|---|---|---|---|---|
| 1 | `tests/ui5_round5.test.tsx` | **37 / 47** | Failures on unchanged code:<br>- No enlarge button on boxless history sightings (plain `Thumb`).<br>- The person row read "Seen moving" and the person detail said "Last handled".<br>- `eventSentence` / `objectEventSentence` / `legacyEventSummary` gave "Moved by an unknown person to …".<br>- The overview tag said "moved", and the inventory "First seen" heuristic was missing.<br>- The person hero said "there is no box for this object".<br>- `auditText` gave "Split", "Split: restored", "Split (…)" and "Unmerge".<br>- `sentenceCase` was not exported; `displayLabel` returned "laptop"; there was no tooltip, the h1 read "m-bag" and the overview row read "laptop".<br>- There was no "No box: look for …" hint and no "Same frame as" note (2 tests).<br>- The bulk bar and checkboxes were missing (7 tests). | 10 guards: object Last handled wording; operator first placement keeps "Moved by operator"; two-sided moves; a later move keeps "Moved by"; the object hero keeps the whole-frame wording; "split by X" and "Split by X" were already mapped (round 3), so the folded raw list already read "Unmerged"; boxed unknowns keep the box caption; no checkboxes outside the unknown tab |
| 2 | same file, after the first implementation | 5 / 47 | Every bulk test failed to find the checkbox. Its accessible name was "Selectgrey object (…)": the leading space inside the `sr-only` span is dropped by accname. Fixed with an explicit `'Select '` text node. | – |
| 3 | `tests/ui5_layout_css.test.tsx` | 2 / 2 | `.review-head` had no `flex-wrap` / `gap`; `.same-frame-note` and `.bulk-list` were missing. Playwright found this: at 1440 px, the two-column cards read "UNKNOWN OBJECTObserved" and "History" wrapped under the title. **Caveat:** the CSS was written before this test. The red was recorded by stripping the round-5 CSS block, then restoring it. | – |

The deliberate behaviour changes failed **13 existing tests**. Each one is amended in place with an `Amended (UI5-xx)`
comment:
- UI5-10, sentence-case labels:
  - `ac_ui_12_17_18` overview link `/Laptop/`;
  - `ac_ui_13_16` ×2: "Headset" link / img / merge name;
  - `ui2_detail_overview` and `ui3_pages` merged banner "Cargo transfer bag 44444444";
  - `ui2_review` candidate order `['High', 'Low', 'Unscored']`;
  - `ui3_wording` `displayLabel` → "Laptop";
  - `ui4_round4` ×2: "Laptop d977201c …", "Camera cb9268bb …".
- UI5-01:
  - `ac_ui_07`: `/flashlight/` now also matches the no-box hint, so the query is anchored as `^flashlight$`;
  - `ui2_review`: an unknown with no box says "No box: look for flashlight in the frame", not "whole frame".
- UI5-09:
  - `ui2_wording_markdown`: legacy "moved ? -> NOD1" now reads "First seen in NOD1";
  - `ui3_pages`: a person row reads "Last seen in US Orbital Segment (USOS)", not "Seen moving".

### Findings → fix → tests

| Finding | Fix | Tests |
|---|---|---|
| UI5-06 boxless history frames (P3) | `TimelineRow` always renders `FrameImage`: the box when the sighting has one, else a whole-frame lightbox. Persons get the privacy caption, and crop URLs are still placeholders (`safePersonImage`). | `ui5_round5` ×2 |
| UI5-09 person "Seen moving" (P3, display) | `LastHandledCell`: persons read "Last seen in <place> · <UTC>" (from `last_location` / `last_seen_at`), else "Last seen (place not recorded)". The detail fact is labelled "Last seen" for persons. | ×4 |
| UI5-09 first sighting as a move (P3, display) | `isFirstSighting` in `format.ts`: a pipeline `moved` event with a destination, no `from_location` and no operator. It reads "First seen in X" in `eventSentence`, `objectEventSentence`, the legacy summary, and the overview tag "first seen". Inventory Last handled: a `moved` event with no actor, not from an operator, within 5 s of `first_seen_at`. | ×7 |
| UI5-10 copy (P3) | <ul><li>`PERSON_FRAME_CAPTION` on person heroes, history frames and evidence.</li><li>`auditText` maps the op when it stands alone, or is followed by " by", ":" or " (".</li><li>`sentenceCase` (first letter only; first words that already have a capital are unchanged) in `displayLabel`, overview object rows and merged-from links, with the raw label in `title`.</li></ul> | ×20 |
| UI5-01 boxless unknowns (P2, UI side) | `unknownLabel` (the "label:" prefix, else `model_guess`, else the description), `unknownNoBox` caption (card + lightbox), and `sameFrameOthers` → "Same frame as N other unknown(s) (labels)". | ×6 |
| UI5-02 structure flood (P2, UI side) | `BulkNotInventory` on `?kind=unknown_object`: <ul><li>labelled native checkboxes per card, plus Select all (indeterminate when partial) and a live count;</li><li>a confirm group lists the labels; focus moves to Confirm, and Esc / Cancel return focus;</li><li>dismiss + `not_inventory` + operator are posted in sequence; 409 counts as closed;</li><li>failures keep their cards, with an error toast.</li></ul> | ×8, `ui5_layout_css` ×2 |

### Playwright measurements (vite preview, mocked routes, `scripts/ui_check.py` `round5`)

New fixtures: three boxless unknowns sharing `/media/frames/063303.png` and a first-sighting `moved` event.
Viewports: 1024×768 and 1440×1000. Results were the same at both sizes:
- **UI5-01:**
  - The captions read "No box: look for grey object / orange bag / blue container in the frame. Click to enlarge.", and
    0 boxes are drawn.
  - The notes read "Same frame as 2 other unknowns (orange bag, blue container)" and so on.
  - Overflow-x is 0.
- **UI5-02, keyboard only:**
  - Space on two checkboxes gives "2 selected". Enter on the bulk button moves focus to "Confirm: not inventory (2)".
  - Overflow-x is 0 with the confirm open.
  - Esc closes the confirm and focus returns to the button.
  - Enter, Enter: toast "Marked 2 items as station structure (not inventory) …", and 1 of the 3 cards is left.
- **UI5-06:** the boxless history sighting opens the dialog, captioned "Whole frame: there is no box for this object.
  Press Esc to close."
- **UI5-10:**
  - The person hero reads "Whole frame (person tracks are not boxed or cropped, for crew privacy). Click to enlarge."
  - The audit rows read "Unmerged by ui-review-3", "Merged by ui-review-4" and "Created by pipeline".
  - Inventory titles read "Camera 1" (tooltip "camera 1").
- **UI5-09:** person rows read "Last seen in US Orbital Segment (USOS) · 2026-10-17 22:57 UTC", and the overview reads
  "first seen in US Orbital Segment (USOS)".
- **Regressions:** the round-4 checks (no horizontal scroll, every card button on screen) still pass.
- **axe-core:** 0 violations on 50 page states. The new states are the shared-frame queue, the bulk confirm, the
  history lightbox and the person detail, at both sizes.

### Deferred / not dashboard-side

- **UI5-01 boxes:** grounding unknown labels to a box (OWLv2 queries) is perception work, in progress. The dashboard
  only explains a missing box. An "I can't tell which object" dismiss reason was not added, because it would need a
  reason enum in the resolve contract.
- **UI5-02 data side:** the denylist additions (latch, insulation, foil, treadmill, "cover" as head noun, mounted
  qualifiers) and the per-frame cap belong to perception. The bulk action only makes cleanup faster.
- **UI5-09 data side:** perception and inventory-api are to stop emitting a `moved` event with no `from_location`. The
  inventory "First seen" rule is a heuristic (`last_handled` carries no locations). If the API adds
  `from_location` / `to_location` to `last_handled`, the rule can use them directly.
- **UI5-03, UI5-04, UI5-05** (re-ID teleport, Cupola sightings query, segment overwrite): perception, inventory-api and
  agent.
- **UI5-07, UI5-08** (chat paging, empty answers and citations): agent.
- **Label casing in chat:** citation chips and chat text keep the agent's labels. Only rows and card titles changed.
