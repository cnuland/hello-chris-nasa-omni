# UI/UX review: ISS Inventory dashboard (live)

- **Reviewed:** 2026-10-18 00:00-00:20 UTC, https://dashboard-nasa-app.apps.bgrr25lx.eastus.aroapp.io
- **Method:** Playwright with Chromium 156, 1440x1000 (plus 1024 and 1920 checks), dark color scheme. axe-core 4.x was injected with a CSP bypass, because the site's CSP `script-src 'self'` blocks CDN injection. API results were cross-checked with `curl /api/...`.
- **Data at review time:** 125 items (115 objects, 10 people) and 69 open reviews (32 unknown, 37 possible duplicates). All captures date from 22:41-23:01 UTC on 2026-10-17.
- **Scripts:** `/tmp/pw-review/*.py` (not in the repo).

## Summary verdict

**PARTIAL PASS. The inventory and review mechanics are solid. The chatbot and the "who moved it" story are not ready for operators.**

The CRUD parts are well built: inventory, filters, edit, add with a required image, classify and dismiss.
- Every inventory filter produced exactly the API's totals: laptop 13, person 10, NOD1 18, confidence ≥0.6 32, unknown 32, needs-review 68, seen-after 57.
- Filters round-trip through the URL. Pagination is 50 per page with the total shown.
- All thumbnails loaded.
- PATCH sends only the changed fields and the change shows up in the audit and history.
- Review resolutions remove the card and update the nav badge.
- axe-core found **0 violations** on all 6 routes. Focus rings are visible, there were no console errors and no 4xx/5xx in normal use, and pages load in about 0.9-1.3 s.
- The dark theme and its contrast suit a dark room.

These requirements are only partly met:
- **R8 chat:** the first question works. Follow-up questions in the same conversation return "I don't have a record of that." Actor and time questions ("who used", "what changed in the last hour") are wrong or fail. Streaming is not visible: up to 64 s of "…" and then everything at once.
- **Diagram "Person with blue shirt moved item at TIME":** only `moved` events appear, the actor text is a pasted fragment, and 2 of 125 rows fill "Moved by".
- **AC-UI-7 merge side by side:** 23 of 37 duplicate reviews have no candidates. The rest show identical, undifferentiated candidate tiles.
- **Image usefulness:** about 60% of unknown-object images are whole frames with no box around the object.

**Counts:** P1 = 3, P2 = 11, P3 = 8 (22 findings).

## Requirement-by-requirement

| Requirement | Verdict | Evidence | Notes |
|---|---|---|---|
| Diagram: Items last seen, confidence | PASS | 01-inventory-default | ConfidenceBar with % on every row and on the detail page |
| Diagram: Items last seen, last-seen location | PASS | 01, 04-filter-location-nod1 | Display name plus code (e.g. "Node 1 Unity / NOD1") |
| Diagram: Items last seen, "Person with blue shirt moved item at TIME" | PARTIAL | 22, 20, 23 | Shown only for `moved` events. Actor fragments ("Moved by wearing a blue tank top…"); `used` and `handed_over` hidden (UI-04) |
| Diagram: Items last seen, saved frame | PARTIAL | 20, 22, 40 | Frames saved and shown per sighting, but no bbox, no enlarge, and the hero is cropped (UI-08, UI-17) |
| Diagram: Items last seen, filter / timestamps / search | PASS (timestamps PARTIAL) | 02-13 | All filters correct against the API. Last-seen is relative only and UTC is hover-only (UI-07) |
| Diagram: Agent Dashboard | PARTIAL | 61-69 | Chat exists with clickable citations. Follow-ups fail, actor and time answers are wrong, no visible streaming (UI-01, 02, 05, 06) |
| Diagram: View Inventory, search + sort by timestamp | PASS | 02, 11 | Newest/oldest sort works. Label sort is case-sensitive (UI-16) |
| Diagram: Manage Inventory, edit item changed off-screen | PASS | 30-33 | Real edit made. PATCH carried only changed fields. Audit and move event recorded. Side effect: last_seen bumped (UI-09) |
| Diagram: Manage Inventory, add unseen item (REQUIRES IMAGE) | PASS | 50-54 | Submit disabled with message until an image is chosen; non-image rejected; multipart 201 |
| User req: list items with filters | PASS | 02-13 | |
| User req: unknown objects with image + description + timestamp for NASA to classify | PASS (image quality PARTIAL) | 40, 45, 46 | Card shows image, description, UTC time, location and model guess. Classify works |
| User req: chat about objects | PARTIAL | 61-69 | See UI-01, UI-02 |
| AC-UI-1 Inventory row content | PASS | 01 | Thumb, label, catalog, status, confidence, location name + code, relative time with UTC title, moved by, needs-review flag |
| AC-UI-2 Filters (debounce, URL, 1:1 params) | PASS | 02-10, 13 | Shared URL `?location=NOD1&status=unknown` restores state; result 0 = API 0 |
| AC-UI-3 Sort + 50/page + total | PASS | 11, 12 | "Page 3 of 3", 25 rows, "125 items" |
| AC-UI-4 Item detail (snapshot, attrs, timeline w/ evidence, moved-by lines) | PARTIAL | 20-24 | All present. Moved-by limited and audit noise (UI-04, UI-12) |
| AC-UI-5 Edit form | PASS | 30-34 | Label validation, Save disabled until dirty, success toast |
| AC-UI-6 Add item, image required | PASS | 50-53 | Validation message for missing label/location is absent (UI-20) |
| AC-UI-7 Review queue (actions, side-by-side merge, badge) | PARTIAL | 40-48 | Classify, new-catalog validation, dismiss and badge all work (69 → 67). Merge comparison inadequate (UI-03); no confirm (UI-10); no filters (UI-11) |
| AC-UI-8 Chat (streaming, clickable citations, proposals) | PARTIAL | 61-69, 83 | Citations clickable. Streaming not perceptible. Proposals not exercised (no proposal returned) |
| AC-UI-9 Overview tiles + feed, 10 s refresh | PASS | 70 | 4 tiles. Refresh confirmed (3 API calls per 10 s). Feed shows ids, not labels (UI-13) |
| AC-UI-10 a11y labels, 1280/1920, dark, toasts | PASS | 14, 15, 80-82 | axe 0 violations; errors appear as non-blocking toasts (82, 83); no horizontal scroll at 1024 |
| R6 Unknowns review queue | PASS (PARTIAL on image usefulness) | 40-47 | |
| R7 Dashboard list/filter/sort/detail/edit/add | PASS | as above | |
| R8 Chatbot grounded, cites ids + timestamps | PARTIAL | 61-69 | Q1 grounded with 5 citations. The rest fail (UI-01, UI-02) |

## Chat transcript summary

| # | Question | Result |
|---|---|---|
| 1 | Where is the laptop right now? | 23.5 s. Five laptop entities with modules and UTC times, 5 clickable citations. Raw markdown. Sensible "most recent sighting NOD1" conclusion |
| 2 | What unknown objects are in Node 1? | "I don't have a record of that." (Technically 0 open unknowns in NOD1, but the reply never says so; tool called with "Node 1" rather than NOD1) |
| 3 | Who used the drill? | In-conversation: "no record". Fresh session: after 57 s, a list of drill entities that never says who |
| 4 | Where is the multimeter usually kept? | In-conversation: "no record". Fresh session: correct, LAB1 (Destiny), with catalog citation, 2 s |
| 5 | What changed in the last hour? | In-conversation: "no record". Fresh session: 64 s, lists unknown objects seen about 70 min earlier and misses the 2 real changes. One direct call returned `llm failure` |

## Cross-cutting checks

- **Console and network:** 0 console errors and 0 failed requests across all flows. The only errors were deliberate ones: the 422 on a malformed id, and the simulated 503 and 502.
- **Load time:** document about 300 ms; network-idle 0.9-1.3 s per route; item detail 0.7-1.1 s.
- **Empty and error states:** "Nothing matches the current filters." (09); "Inventory unavailable." plus an error toast (82); chat failure toast (83); review "All clear" state exists in code but was not reachable.
- **Accessibility:** axe-core passed on all 6 routes (31-45 rules each, 0 violations). Every input has a label. Focus outline is a visible 2 px accent. Logical tab order. Enter on a row link opens the item. Gaps: no skip link, hover-only UTC, aria-hidden slider value (UI-07, UI-21).
- **UTC consistency:** absolute times everywhere use `YYYY-MM-DD HH:MM:SS UTC`. Exceptions: the relative-only last-seen (UI-07), the locale 12-hour date pickers (UI-15), and chat prose using ISO `…Z`.
- **Visual design:** a coherent mission-control dark theme with good hierarchy, status badges and monospace timestamps. Weak spots: small thumbnails, long review grid, opaque ids in feeds.

## Real mutations made during the review (operator `ui-review`)

| # | Type | Id | Change |
|---|---|---|---|
| 1 | PATCH item | `af8031d3-443f-4844-90ff-30e903cf26fb` ("text", an on-screen caption detected as an object) | last_location USOS → NOD1, note "UI review: off-screen location change test (USOS->NOD1)", 2026-10-18 00:06:31Z. Audit 391 and move event 3e8bf94b recorded |
| 2 | Review resolve: assign_catalog | review `c3057aa9-3e29-4993-8b40-c52b0e6de20c` (entity `c478f331-4ccb-4df2-ab71-c734b8a9f8e2`, "tool, possibly a wrench") | Classified as catalog `wrench`; the entity is now "Wrench", status confirmed |
| 3 | Review resolve: dismiss | review `eca3e3a0-a9c1-4825-8fc1-57acec9bc935` (entity `af8031d3…`, overlay text) | Dismissed; the entity status is now `dismissed` |
| 4 | Create item | `8b2949a6-6c11-4082-aff5-8db6aed79dbb` | "UI-REVIEW TEST ITEM", NOD1, 64x64 grey test PNG, description "Created by automated UI review (Playwright). Safe to delete." |

The chat questions were read-only. No proposals were confirmed.

## Prioritised findings

### UI-01 [P1] Chat follow-up questions in the same conversation return "I don't have a record of that."

- **Page:** `/chat`
- **Steps to reproduce:** Open /chat. Ask "Where is the laptop right now?" and wait for the answer. In the same conversation ask "Where is the multimeter usually kept?" (and the other questions in the review list).
- **Expected:** Each question is answered from the database. The multimeter question should say the catalog home is LAB1 (Destiny), which is what the agent answers in a fresh conversation.
- **Actual:** All 4 follow-up questions came back as "I don't have a record of that." with no citations, each within 2-5 s. Asked in a fresh conversation, the multimeter question was answered correctly with a catalog citation (69-chat-fresh-q0-done). Whenever there is earlier history, the agent stops calling tools usefully, so the chat breaks after the first question.
- **Suggested fix:** ChatPage.send() posts the whole transcript, including long markdown assistant replies full of UUIDs, as `messages`. Option 1: send only the last N turns, or send user turns plus a short summary, and/or use the agent's session_id instead of replaying prose. Option 2: fix the agent's system prompt or tool loop so that a new user turn always triggers tool calls. Add an e2e test that asks two questions in sequence.
- **Files:** `dashboard/src/pages/ChatPage.tsx`, `dashboard/src/api/client.ts (streamChat)`, `services/agent`
- **Screenshots:** `shots/61-chat-q1.png`, `shots/62-chat-q2.png`, `shots/63-chat-q3.png`, `shots/64-chat-q4.png`, `shots/65-chat-q5.png`, `shots/69-chat-fresh-q0-done.png`

### UI-02 [P1] Chatbot cannot answer actor and time questions ("Who used the drill?", "What changed in the last hour?")

- **Page:** `/chat`
- **Steps to reproduce:** In a fresh /chat session, ask "What changed in the last hour?" and then "Who used the drill?". The same questions can also be sent with POST /agent/chat/stream.
- **Expected:** R8: answers grounded in events. Expected the 2 operator events in the last hour (move of af8031d3 USOS->NOD1 at 00:06:31Z, item 8b2949a6 added at 00:07:48Z) and the people (actor_desc) linked to drill use or move events, each with citations.
- **Actual:** "What changed in the last hour?" took 64 s. It listed unknown objects last seen about 70 min earlier, left out both real changes, and had no event citations. One direct API call returned `{"detail":"llm failure"}`. "Who used the drill?" took 57 s and dumped a markdown list of drill entities, never saying who. "What unknown objects are in Node 1?" called list_unknowns with location="Node 1" (a display name, not the NOD1 code) and replied "I don't have a record of that." instead of "No open unknowns in Node 1 (NOD1)".
- **Suggested fix:** Agent side: add or use an events tool (GET /events?since=&entity=) for "changed / who moved / who used" intents, and resolve module names to location codes before tool calls. Return an explicit 'none found in <scope>' reply. Dashboard side: show the in-stream `error` event inline in the conversation, not only as a toast.
- **Files:** `services/agent`, `dashboard/src/pages/ChatPage.tsx`
- **Screenshots:** `shots/65-chat-q5.png`, `shots/69-chat-fresh-q1-done.png`, `shots/63-chat-q3.png`

### UI-03 [P1] Possible-duplicate cards don't give the operator enough to decide a merge

- **Page:** `/review`
- **Steps to reproduce:** Open /review and scroll to a POSSIBLE DUPLICATE card, e.g. review 9d1680e5 (headset) or 9507864f (camera).
- **Expected:** AC-UI-7: the subject and each candidate shown side by side with image, label, location, last-seen UTC, confidence and a link, so the operator can tell them apart and merge.
- **Actual:** Only 14 of the 37 duplicate reviews have any candidates. The other 23 (e.g. "18 live 'camera' entities would exceed the expected count of 4") show no candidates at all, so merging is impossible and only Classify or Dismiss remain. When candidates do exist, they appear as a strip of identical captions and buttons (5x "headset" / "Merge into headset") with no location, time, confidence or id, and the subject image has no caption. Nothing on the card links to the item detail.
- **Suggested fix:** ReviewCard: draw the subject in a left column and the candidates in a grid on the right. Give each candidate tile its image, catalog name, last_location_name (code), formatUtc(last_seen_at), ConfidenceBar, an id prefix and a <Link to=/items/:id>, and make the merge button label unique ("Merge into headset 163e1c50 (NOD1)"). For over-count reviews with no candidates, have the API return the N nearest same-class entities as candidates, or link to the inventory filtered by catalog_id. Add a "Not a duplicate / keep" action.
- **Files:** `dashboard/src/pages/ReviewPage.tsx`, `dashboard/src/styles/theme.css`, `services/inventory-api (review candidates)`
- **Screenshots:** `shots/41-review-duplicate-headset-candidates.png`, `shots/42-review-duplicate-laptop.png`, `shots/43-review-duplicate-no-candidates.png`

### UI-04 [P2] "Person with blue shirt moved item at TIME" is rarely shown and reads badly when it is

- **Page:** `/items/:id, /`
- **Steps to reproduce:** Open /items/ec044362-a184-449b-a016-8d011373ee10 (person) and /items/da9709e7-4ed6-4b2d-beb0-aa6c814ae764 (laptop with 'used' events). Look at the inventory 'Moved by' column.
- **Expected:** Diagram 'Items last seen': a sentence such as "Person (blue shirt, headset) moved laptop from USOS to NOD1 at 22:50:05 UTC" with the saved frame, shown for move, use and hand-over events.
- **Actual:** The Movements section only lists event_type=='moved'. The laptop's 2 'used' events ("by Blue shirt, black shorts, headset") show "No recorded movements" and "Last moved by —". The actor text is pasted in raw: "Moved by wearing a blue tank top and a headset at …". A missing destination shows as "USOS → ?". Evidence frames exist on events (evidence_url) but are not shown in Movements. Only 2 of 125 rows fill the Moved-by column.
- **Suggested fix:** ItemDetailPage: rename the section to 'Activity' and include moved, used and handed_over events. Render the actor as "person (<actor_desc>)" and link actor_entity when present. Write "to unknown location" instead of '?'. Show evidence_url as a Thumb. Fill the inventory Moved-by column from the latest move, use or hand-over event (API last_moved_by or a new last_actor field).
- **Files:** `dashboard/src/pages/ItemDetailPage.tsx`, `dashboard/src/pages/InventoryPage.tsx`, `dashboard/src/lib/format.ts`
- **Screenshots:** `shots/22-detail-person-bluetank-moved.png`, `shots/20-detail-laptop-used.png`, `shots/23-detail-moved-actor.png`

### UI-05 [P2] Chat streaming isn't perceptible: up to 64 s of '…' and then the whole answer at once

- **Page:** `/chat`
- **Steps to reproduce:** Ask "Where is the laptop right now?" or "Who used the drill?".
- **Expected:** AC-UI-8 streaming reply: tokens appear progressively, or at least a visible status ("searching inventory…", tool name) and a Stop button.
- **Actual:** The first token arrived at the same moment as the last one (23.5 s for Q1, 57 s for the drill question, 64 s for the last-hour question; measured both in the browser and with curl on /agent/chat/stream). The agent buffers the whole reply and then sends it as token events. The UI shows a bare '…' with no spinner, elapsed time or cancel button.
- **Suggested fix:** Agent: stream LLM tokens as they are generated and emit `status` or tool_call events. ChatPage: render a typing indicator with elapsed seconds and the tool currently running, and add a Stop button that calls abortRef.current.abort().
- **Files:** `dashboard/src/pages/ChatPage.tsx`, `dashboard/src/lib/sse.ts`, `services/agent`
- **Screenshots:** `shots/68-chat-fresh-q1-waiting.png`, `shots/61-chat-q1.png`

### UI-06 [P2] Chat shows raw Markdown and UUIDs, and citations are opaque ids

- **Page:** `/chat`
- **Steps to reproduce:** Ask "Where is the laptop right now?".
- **Expected:** A readable answer (bold or list rendered, no backticks). Citations show item label, location and UTC time and link to the item.
- **Actual:** The reply shows literal `**US Orbital Segment (USOS)**` and backtick-wrapped full UUIDs. The citation chips read "Item da9709e7 · 2026-10-17 22:51:03 UTC" with no label or location, so the operator can't tell the 5 laptops apart. The citations are clickable and do open the item.
- **Suggested fix:** Render assistant text with a small safe markdown renderer (no raw HTML), or tell the agent to answer in plain text. Turn entity_id mentions in the text into Links. In Citations, look up the label and last_location (getItem, cached) and render "laptop · NOD1 · 23:00:27 UTC".
- **Files:** `dashboard/src/pages/ChatPage.tsx (Citations, msg-text)`, `services/agent (prompt)`
- **Screenshots:** `shots/61-chat-q1.png`, `shots/66-chat-citation-opens-item.png`

### UI-07 [P2] Last-seen time is only relative ("1 h ago" on every row); UTC is hover-only

- **Page:** `/`
- **Steps to reproduce:** Load /. Look at the Last seen column.
- **Expected:** Trustworthy UTC timestamps visible at a glance for operators, e.g. "23:01:41 UTC · 1 h ago".
- **Actual:** All 125 rows read "1 h ago" (the data spans 22:41-23:01 UTC), so the column can't tell items apart. The exact UTC time is only in a title tooltip, which keyboard and touch users can't reach. The detail page does the same for 'Last seen'.
- **Suggested fix:** InventoryPage ItemRow and ItemDetailPage facts: render <time dateTime>{formatUtc(...)}</time> with the relative time as secondary muted text. Consider an HH:MM:SS UTC column with the date shown once per group.
- **Files:** `dashboard/src/pages/InventoryPage.tsx`, `dashboard/src/pages/ItemDetailPage.tsx`, `dashboard/src/lib/format.ts`
- **Screenshots:** `shots/01-inventory-default.png`

### UI-08 [P2] Many review and inventory images are full video frames with no box around the object

- **Page:** `/review, /`
- **Steps to reproduce:** Open /review and look at the first UNKNOWN OBJECT card ("tool: a black tool with a long handle").
- **Expected:** R6 and the diagram's saved frame: the operator can see which object the description refers to (crop, or frame with a bounding box), and can enlarge it.
- **Actual:** 18 of 30 unknown-object review images and 72 of 125 inventory snapshots are whole frames (/media/frames/...), not crops, and there is no bounding-box overlay. On a 160 px thumbnail of a cluttered module, the operator can't find "the tool". Images can't be clicked to view them full size.
- **Suggested fix:** API: return the bbox (or a crop_url) on review items and sightings. Thumb: overlay the bbox on frames and wrap the image in a button that opens a lightbox at full resolution. Make review images at least 240-320 px wide.
- **Files:** `dashboard/src/components/Thumb.tsx`, `dashboard/src/pages/ReviewPage.tsx`, `dashboard/src/styles/theme.css`, `services/inventory-api`
- **Screenshots:** `shots/40-review-top.png`, `shots/01-inventory-default.png`

### UI-09 [P2] An operator edit resets 'Last seen' to 'just now'

- **Page:** `/items/:id`
- **Steps to reproduce:** Edit item af8031d3 and change the location from USOS to NOD1 (done during this review at 00:06:31Z).
- **Expected:** 'Last seen' keeps meaning the last camera sighting (22:57:14 UTC). The operator change appears as 'Last updated by ui-review at …'.
- **Actual:** After saving, the facts show "Last seen: just now" and the item jumps to the top of the newest-first inventory. That tells mission control the camera saw it, which it did not.
- **Suggested fix:** inventory-api: don't bump last_seen_at on a PATCH, or add a separate last_sighting_at field. ItemDetailPage: show 'Last sighting' and 'Last operator update' as separate facts.
- **Files:** `services/inventory-api`, `dashboard/src/pages/ItemDetailPage.tsx`
- **Screenshots:** `shots/32-edit-saved-toast.png`, `shots/33-edit-history-after.png`

### UI-10 [P2] Dismiss resolves in one click with no confirmation or undo, and is ambiguous on duplicate cards

- **Page:** `/review`
- **Steps to reproduce:** Enter an operator on /review and click Dismiss on any card.
- **Expected:** Destructive actions confirm, or offer an Undo toast. On duplicate cards, it's clear whether Dismiss closes the review or dismisses the entity.
- **Actual:** One click POSTs action=dismiss and the card disappears. Resolving review eca3e3a0 also set entity af8031d3 to status 'dismissed', which hides it from the default inventory, and nothing says so. On a possible-duplicate card, Dismiss could remove a real item.
- **Suggested fix:** ReviewCard: use a two-step confirm (button turns into 'Confirm dismiss?') or an Undo toast with a 5-10 s delay before the POST. Label the actions per kind: 'Not an object (dismiss)' for unknowns, 'Not a duplicate' for duplicates.
- **Files:** `dashboard/src/pages/ReviewPage.tsx`, `dashboard/src/state/toasts.tsx`
- **Screenshots:** `shots/47-review-text-overlay-before-dismiss.png`, `shots/46-review-after-classify-toast.png`

### UI-11 [P2] Review queue: 67 cards in one list with no filtering, sorting or link to the item

- **Page:** `/review`
- **Steps to reproduce:** Open /review.
- **Expected:** Operators can filter by kind (unknown / duplicate), location and time, sort, and open the underlying item.
- **Actual:** All 67 open cards render in one long two-column grid. There are no filters, no sort control, no count per kind, and no link to /items/:entity_id. Each card repeats the full 24-entry catalog select.
- **Suggested fix:** Add kind tabs with counts (from stats.open_reviews_by_kind), a location select and a sort to ReviewPage. Add an 'Open item' link in each card header. Consider pagination or virtualisation.
- **Files:** `dashboard/src/pages/ReviewPage.tsx`
- **Screenshots:** `shots/48-review-full.png`, `shots/40-review-top.png`

### UI-12 [P2] History timeline is flooded with "update by pipeline" audit lines that have no detail

- **Page:** `/items/:id`
- **Steps to reproduce:** Open /items/ec044362-a184-449b-a016-8d011373ee10.
- **Expected:** The history focuses on sightings and events. Audit entries say what changed (field: old → new) and can be filtered or collapsed.
- **Actual:** The person entity's history starts with 14 consecutive "AUDIT … update by pipeline" lines before the first sighting. None of them says what changed. There is no filter by entry kind.
- **Suggested fix:** Timeline: add kind filter chips (Sightings / Events / Audit). Collapse consecutive pipeline audits into one line ("14 pipeline updates"). Have the API include the changed fields in the audit summary.
- **Files:** `dashboard/src/pages/ItemDetailPage.tsx (Timeline)`, `services/inventory-api (history)`
- **Screenshots:** `shots/22-detail-person-bluetank-moved.png`, `shots/20-detail-laptop-used.png`

### UI-13 [P2] Overview event feed shows opaque item ids and fragment actor text

- **Page:** `/overview`
- **Steps to reproduce:** Open /overview.
- **Expected:** Each event reads e.g. "MOVED laptop (SSC laptop) by person in blue shirt, USOS → NOD1, 23:01:12 UTC".
- **Actual:** Events read "item eb76b0fe" with no label. The actor shows as "by sleeping, wearing a headset". Routes show as "— → NOD1" or "NOD3 → —". Item labels are never shown.
- **Suggested fix:** Have GET /events return entity_label (join) or resolve labels client-side. Format the actor as "person (<desc>)" and drop the route when both ends are missing. Write "added at NOD1" for added events.
- **Files:** `dashboard/src/pages/OverviewPage.tsx (EventLine)`, `services/inventory-api (events)`
- **Screenshots:** `shots/70-overview.png`

### UI-14 [P2] Chat conversation is lost when you navigate away (e.g. by clicking a citation)

- **Page:** `/chat`
- **Steps to reproduce:** Ask a question, click a citation chip, then press Back.
- **Expected:** The conversation is still there.
- **Actual:** After Back there are 0 messages, because ChatPage keeps messages in local component state. Clicking a citation is the main way to drill down, so the operator loses their thread every time.
- **Suggested fix:** Lift messages into a context provider next to ReviewCountProvider, or persist them to sessionStorage. Optionally open citations in a side drawer instead of navigating away.
- **Files:** `dashboard/src/pages/ChatPage.tsx`, `dashboard/src/App.tsx`
- **Screenshots:** `shots/66-chat-citation-opens-item.png`, `shots/67-chat-after-back.png`

### UI-15 [P3] Date filters say UTC but use the browser-locale 12-hour picker

- **Page:** `/`
- **Steps to reproduce:** Look at Seen after / Seen before (UTC).
- **Expected:** 24-hour ISO UTC entry (YYYY-MM-DD HH:MM) that matches every other timestamp in the app.
- **Actual:** The inputs show 'mm/dd/yyyy, --:-- --' with AM/PM (en-US locale) while the rest of the UI uses 2026-10-17 23:01:41 UTC. There are no quick ranges such as 'last 1 h / 24 h'.
- **Suggested fix:** Replace datetime-local with a text input using pattern YYYY-MM-DDTHH:MM plus a format hint, or add preset range buttons. Keep localInputToIso.
- **Files:** `dashboard/src/pages/InventoryPage.tsx`, `dashboard/src/lib/format.ts`
- **Screenshots:** `shots/06-filter-seen-after.png`, `shots/01-inventory-default.png`

### UI-16 [P3] Sorting by label is case-sensitive

- **Page:** `/`
- **Steps to reproduce:** Pick Sort: Label (A-Z).
- **Expected:** Case-insensitive alphabetical order.
- **Actual:** "Pressurized Mating Adapter", "Research Results" and "Space Station" sort before "astrobee robot".
- **Suggested fix:** inventory-api: ORDER BY lower(label). The dashboard only passes sort=label.
- **Files:** `services/inventory-api`
- **Screenshots:** `shots/11-sort-oldest.png`

### UI-17 [P3] The large snapshot uses object-fit: cover, which crops the evidence

- **Page:** `/items/:id`
- **Steps to reproduce:** Open /items/af8031d3-443f-4844-90ff-30e903cf26fb.
- **Expected:** The whole frame is visible (letterboxed if needed).
- **Actual:** The right edge of the frame is cut off (the overlay text reads "Science on al…"). Small thumbnails crop the same way.
- **Suggested fix:** theme.css: .thumb-lg { object-fit: contain; background: var(--bg-sunken) }. Use contain for the md evidence thumbs as well.
- **Files:** `dashboard/src/styles/theme.css`
- **Screenshots:** `shots/32-edit-saved-toast.png`

### UI-18 [P3] A malformed item id shows a generic error instead of 'Item not found'

- **Page:** `/items/:id`
- **Steps to reproduce:** Open /items/not-a-real-id.
- **Expected:** 'Item not found.' with a link back.
- **Actual:** The API returns 422 (invalid UUID), the page says 'This item could not be loaded.', and two console errors are logged.
- **Suggested fix:** ItemDetailPage: treat 422 like 404, or check the UUID shape before fetching.
- **Files:** `dashboard/src/pages/ItemDetailPage.tsx`
- **Screenshots:** `shots/25-detail-notfound.png`

### UI-19 [P3] A failed chat request loses the typed question and offers no retry

- **Page:** `/chat`
- **Steps to reproduce:** Make /agent/chat/stream fail (simulated 502) and send a question.
- **Expected:** The user message is marked failed and has a Retry button, or the draft is restored.
- **Actual:** A toast says 'Chat request failed: bad gateway'. The user bubble stays with no reply, and the input is empty.
- **Suggested fix:** ChatPage.send(): on failure, restore setDraft(text) or mark the message failed and add a Retry action.
- **Files:** `dashboard/src/pages/ChatPage.tsx`
- **Screenshots:** `shots/83-chat-agent-down.png`

### UI-20 [P3] Add-item form only explains the missing image, not the missing label or location

- **Page:** `/items/new`
- **Steps to reproduce:** Choose an image but leave Label empty. The Add item button stays disabled with no reason given.
- **Expected:** Inline 'Label is required' and 'Location is required' messages, linked with aria-describedby.
- **Actual:** Only the image has a message. The disabled submit is also skipped by Tab, so keyboard users get no hint at all. The file input never sets aria-invalid.
- **Suggested fix:** AddItemPage: show field-error text for label and location once they've been touched or a submit was attempted. Set aria-invalid on the invalid fields. Consider an enabled submit that focuses the first invalid field.
- **Files:** `dashboard/src/pages/AddItemPage.tsx`
- **Screenshots:** `shots/50-add-empty.png`, `shots/51-add-no-image-validation.png`, `shots/52-add-non-image-rejected.png`

### UI-21 [P3] Filter bar polish: 'Needs review' checkbox is misaligned and the confidence value is hidden from screen readers

- **Page:** `/`
- **Steps to reproduce:** Load / at 1440 px and at 1024 px.
- **Expected:** Checkboxes line up with the other filters. The slider announces its value.
- **Actual:** 'Needs review' sits centered in its own column, away from the Status group. The % read-out next to the slider is aria-hidden and the input has no aria-valuetext. There is no 'Clear filters' button and no skip-to-content link.
- **Suggested fix:** theme.css .filters: align the checkbox field to the left. InventoryPage: add aria-valuetext={`${pct}%`}, a 'Reset filters' button and a skip link in App.tsx.
- **Files:** `dashboard/src/pages/InventoryPage.tsx`, `dashboard/src/styles/theme.css`, `dashboard/src/App.tsx`
- **Screenshots:** `shots/01-inventory-default.png`, `shots/14-inventory-1024.png`

### UI-22 [P3] Data quality the UI surfaces: one 'person' track holds different crew members

- **Page:** `/items/:id`
- **Steps to reproduce:** Open /items/ec044362-a184-449b-a016-8d011373ee10.
- **Expected:** One person entity per crew member.
- **Actual:** The 14 exemplars include 'white spacesuit', 'man in gray shirt and black shorts' and 'blue shirt'. The person was also 'moved' by another person. Operators have no way in the UI to split a track.
- **Suggested fix:** Pipeline: tighten person re-identification. UI: add a 'Split / remove sighting' action in the timeline (needs an API endpoint).
- **Files:** `services/perception`, `dashboard/src/pages/ItemDetailPage.tsx`
- **Screenshots:** `shots/22-detail-person-bluetank-moved.png`
