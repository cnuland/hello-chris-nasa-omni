# Spec: dashboard (dashboard/) — React + Vite + TS

Talks only to inventory-api (`/api` proxy) and agent (`/agent` proxy).

- AC-UI-1 **Inventory view** (default route `/`): a table or cards of items, each row showing:
  - snapshot thumbnail, label, catalog name, status badge, confidence bar;
  - last location (display name + code), last seen (absolute UTC and relative, both visible: AC-UI-11);
  - last handled (AC-UI-12; was "last moved by"), and a needs-review flag.
- AC-UI-2 **Filters**: text search (debounced 300 ms), kind, status (multi), location (hierarchical select), min
  confidence slider, time range, needs-review toggle. Filters are reflected in the URL query string (shareable) and
  map 1:1 to `/items` params.
- AC-UI-3 Sorting by last seen (default newest first), label and confidence, plus pagination (50/page) with the total
  count shown.
- AC-UI-4 **Item detail** drawer/page: large snapshot, attributes, history timeline (sightings/events/audit) with
  evidence images, and "moved by … at …" lines (amended by AC-UI-12: an "Activity" list of all events, and AC-UI-17).
- AC-UI-5 **Edit item** form (off-screen changes): label, description, catalog, location, status, and an
  operator/note field. It sends PATCH with only the changed fields and shows success/error.
- AC-UI-6 **Add item** form: an image is **required** (the submit button is disabled and a validation message shown
  until an image file is chosen), plus label, location and optional catalog/description. It posts multipart.
- AC-UI-7 **Unknowns / Review queue** (`/review`): cards with the image, description, timestamp, location, model
  guess and kind. Actions: classify as a catalog item (searchable select), create a new catalog item, merge into one
  of the candidates (shown side-by-side with images), or dismiss. The card disappears on success. The open-count
  badge shows in the nav. (Amended by AC-UI-13 to AC-UI-16.)
- AC-UI-8 **Chat** (`/chat`): message list, input, streaming reply, and clickable citations that open the item
  detail. "Proposed update" messages render a confirm button that performs the PATCH. (Amended by AC-UI-19 to AC-UI-22.)
- AC-UI-9 **Overview**: stats tiles (items, unknowns, possible duplicates, people seen in the last hour) and a recent
  events feed (auto-refresh every 10 s).
- AC-UI-10 Accessible labels on all inputs; works at 1280 px and 1920 px; dark theme (mission-control style). Errors
  from the API are shown as non-blocking toasts.

## UI-review amendments (2026-10-17, from docs/ui-review/REPORT.md)

All new API fields below are optional; every AC holds with and without them (graceful degradation).

- AC-UI-11 **Timestamps** (UI-07): lists and cards show absolute UTC at minute precision (`2026-10-17 21:42 UTC`, in a
  `<time dateTime>`) with the relative time ("5 min ago") visible beside it, never hover-only. This applies to the
  inventory "Last seen" column, the item facts, review cards, candidate tiles, the overview feed and chat citation
  chips. The full `…:SS UTC` stays in a `title`.
- AC-UI-12 **Last handled** (UI-04, UI-09): the inventory column "Moved by" becomes **"Last handled"**. It renders
  `last_handled` as verb + actor + time, e.g. "Used by astronaut in blue suit · 2026-10-17 21:42 UTC". If
  `last_handled` is absent it falls back to `last_moved_by`/`last_moved_at`. The free-text actor is phrased so it
  reads naturally after "by":
  - a person noun is kept as is ("astronaut in blue suit");
  - a qualifier gets "person" in front ("wearing a blue tank top" -> "person wearing a blue tank top");
  - a bare description is put in brackets ("Blue shirt, black shorts" -> "person (blue shirt, black shorts)");
  - a missing actor reads "an unknown person".

  The item detail shows:
  - "Last sighting" (`last_seen_at`) and "Last handled";
  - "Location set by X at T" (`location_set_by`/`location_set_at`) when present, so an operator edit is never shown as
    a sighting;
  - an **Activity** list of every event (moved, used, handed over, …), newest first. Each line reads "Verb by actor at
    UTC, from A to B". A one-sided move says "to an unknown location" (never `?`). The evidence frame is shown and
    can be enlarged.
- AC-UI-13 **Duplicate merge decision** (UI-03): a possible-duplicate card puts the subject (`subject`, or GET
  /items/{entity_id} when the API does not send it) next to the candidates. Each one shows:
  - thumbnail (`thumbnail_url`, falling back to `snapshot_url`), label linking to `/items/{id}`, short id and catalog;
  - location name + code, last seen (AC-UI-11), confidence bar and sighting count;
  - similarity (candidates only).

  Each candidate has a "Merge subject into this" button whose accessible name is unique (label, short id, location),
  and which posts `merge_into`. "Not a duplicate (keep both)" posts `assign_catalog` with the subject's catalog (or
  the catalog chosen in the select), the subject's label and a note. This closes the review and keeps both items; the
  contract has no separate keep action. With no candidates, the card says so explicitly and links to an inventory
  search. `candidates_source: suggested` is labelled as a suggestion.
- AC-UI-14 **Review images** (UI-08): when `image_bbox` (normalised `[x1,y1,x2,y2]`) is valid, an outline of the
  object is drawn on the frame. Invalid boxes are ignored. Review images are at least 300 px wide. Clicking an image
  opens an accessible lightbox:
  - `role="dialog"` with `aria-modal`;
  - focus moves to Close and is trapped inside;
  - Esc, Close or a backdrop click closes it, and focus returns to the image.

  The overlay uses CSSOM styles only (CSP `style-src 'self'`).
- AC-UI-15 **Dismiss confirm** (UI-10): "Dismiss…" opens an inline confirm step. Its text says that dismissing also
  marks the item dismissed and hides it from the default inventory; on duplicates it points to "keep both". Nothing is
  posted until "Confirm dismiss"; "Cancel" closes the step.
- AC-UI-16 **Review queue filters** (UI-11): the queue has:
  - kind tabs with counts (`/stats` `open_reviews_by_kind`);
  - a location select;
  - a debounced search;
  - a sort: created/observed, newest/oldest.

  The filters live in the URL and map 1:1 to GET /review `kind`, `location`, `q`, `sort`, `order`. `X-Total-Count`
  drives "Showing N of M". If the header is absent (an older API), the same filters and sort are applied client-side.
  Every card with an entity links to the item page ("Open item") and to its history (`/items/{id}#history`, which
  scrolls to the History section).
- AC-UI-17 **History grouping** (UI-12): consecutive audit entries with the same summary are shown as one row, e.g.
  "14 × update by pipeline". API-collapsed entries (`count`, `first_at`) are honoured and added up. A disclosure
  button lists the folded entries. Filter chips (Sightings / Events / Audit, with counts) show or hide each kind.
- AC-UI-18 **Overview feed** (UI-13): events show `entity_label` (falling back to "item <short id>") and location
  names ("Node 1 Unity (NOD1)") from `from_location_name`/`to_location_name`, falling back to codes. Actors are
  phrased as in AC-UI-12. A one-sided route reads "to X" / "from X" ("at X" for added), and the route is omitted when
  both ends are missing. Timestamps follow AC-UI-11.
- AC-UI-19 **Chat Markdown** (UI-06): assistant text is rendered with a small safe Markdown subset (paragraphs,
  lists, headings shown bold, bold/italic, inline and fenced code) built only from React elements. There is no
  `dangerouslySetInnerHTML`. Raw HTML appears as literal text, and Markdown links keep their text but never become
  anchors. Cited entity UUIDs in the text become links to the item, named by its label. This is compatible with the
  CSP: no inline scripts or styles.
- AC-UI-20 **Citation chips** (UI-06): entity citations read "label (location) · time" and link to `/items/{id}`.
  The label and location are looked up via GET /items/{id} through a small cache (60 s TTL, failures cached). They
  fall back to "Item <short id>".
- AC-UI-21 **Chat progress and Stop** (UI-05, UI-02 dashboard side): while waiting for the first token, a
  `role="status"` line shows "Thinking: looking up the inventory…", the elapsed seconds, and the tools reported so
  far. Tools come from optional SSE `status` {text?, tool?} or `tool_call`/`tool`/`tool_start` {name} events, which
  are validated. After the reply, the tools from `tool_calls` are listed ("Looked up with: …"). A Stop button aborts
  the request and keeps any partial text, marked "Stopped". In-stream `error` events and HTTP failures are shown
  inline in the conversation (as well as in a toast).
- AC-UI-22 **Chat persistence, New chat, Retry** (UI-14, UI-19, UI-01 dashboard side):
  - The conversation and a `session_id` persist in `sessionStorage` (`nasa-omni.chat.v1`), so navigating away and
    back keeps it. "New chat" clears it and starts a new session id.
  - A question that got no answer (error, stop, or leaving mid-stream) is marked failed with a Retry button.
  - The request sends only completed turns (no error bubbles or stopped/failed turns). It is capped at 40 messages
    and 60 000 characters, newest kept and starting with a user turn, plus `session_id`.
- AC-UI-23 **Polish** (UI-15, UI-17, UI-18, UI-20, UI-21):
  - "Seen after/before (UTC)" are 24 h `YYYY-MM-DD HH:MM` text inputs with format errors, plus quick ranges (last
    1 h / 24 h).
  - Snapshots use `object-fit: contain`.
  - A malformed or unknown id (422/404) shows "Item not found." without a request.
  - The add form shows "Label is required" / "Location is required" once touched, with `aria-invalid`, plus a line
    next to the disabled submit that lists what is missing.
  - The confidence slider has `aria-valuetext`, there is a "Reset filters" button, and a "Skip to content" link.

### AC-UI-24 Citation labels from the agent (integration 2026-10-18)
- `validateChatResponse` keeps the optional `label`, `location` and `location_name` (plain strings of 200 characters
  or fewer, else dropped) and an item citation's `catalog_id`, as sent by the agent (AC-AGT-17).
- Citation chips prefer these fields and fall back to the cached item lookup. Catalog chips read "Catalog: <label>".
  Tests: tests/ac_ui_24_citation_labels.test.ts.

## UI review round 2 amendments (2026-10-18, from docs/ui-review/round2/)

Every new API field below is optional; each AC holds with and without it (tested both ways).

- AC-UI-25 **Merge confirm, undo and merged items** (UI2-05):
  - "Merge subject into this" opens an inline confirm step ("Confirm merge" group): both thumbnails, and
    "Merge <subject> into <target> at <location>? <subject> is marked merged, and its sightings and history move to
    <target>. You can undo this…". Nothing is posted until "Confirm merge"; "Cancel" closes it. Focus moves to Confirm.
  - After the merge, a toast reads "Merged <subject> into <target>." with a link "Open <target>" and an "Undo merge"
    button (shown for 20 s), which calls `POST /api/items/{subject}/unmerge {operator}` and reloads the queue.
  - A merged item (`status: merged` or `merged_into`) shows a banner "Merged into <label> at <UTC>" linking to the
    target ("merged into another item" without `merged_into`), with an Operator field and an "Unmerge" button that is
    disabled until an operator name is entered. A target lists "Merged from" (`merged_from`) with links and times.
  - A 409 from unmerge reads "This merge can't be undone safely: <detail>. Nothing was changed." (inline on the item
    page, as an error toast from Undo).
- AC-UI-26 **Ranked candidates** (UI2-06): candidates are sorted by `score` (highest first; unscored keep the API
  order, after the scored ones). Each tile shows "Match score" and `match_reasons` chips ("Why it matched"; strings
  only, at most 6, 60 characters each). The heading reads "ranked by match score"; suggested lists without score or
  similarity read "Other items of this class (suggested, unranked)".
- AC-UI-27 **Box sources** (UI2-07, UI-08): `image_bbox_source` / `snapshot_bbox_source` / sighting `bbox_source`.
  Detector boxes are solid. Omni boxes are dashed, padded by 15 % per side, tagged "approximate" (CSS generated
  content), and the enlarge button name ends "(approximate box)". Unknown source: solid (older APIs). With no box
  there is no overlay and the caption says "Whole frame". Boxes and captions appear on review images, candidate tiles
  (the snapshot is used when `snapshot_bbox` is set), the item hero image and history sighting frames.
- AC-UI-28 **Event wording** (UI2-09, UI2-10, UI-04, UI-13, amends AC-UI-12/18): one shared sentence for Activity,
  History event rows and the overview feed: "Used by <actor> at Node 1 Unity (NOD1)", "Moved by <actor> from A to B",
  "Added by … at X". `location_role` decides ("at" vs "from_to"); without it moves (or two different ends) are routes
  and everything else is "at". Only a move with no destination says "to an unknown location". `actor_kind: operator`
  (or, without it, `source` operator/api; for Last handled, an actor equal to `location_set_by`) reads
  "operator <id>" ("an operator" when empty), never "person (…)". History event rows are matched to `events` by id;
  otherwise the API summary ("used USOS -> ? by …") is rewritten. Activity lines read "<sentence> · <UTC>".
- AC-UI-29 **History label** (UI2-11): a server-collapsed summary that starts with its count ("9 pipeline updates
  (…)") gets the group total in place of that number, never "9 × 9 …"; other folded runs keep "N × summary".
- AC-UI-30 **Markdown lists** (UI2-12, amends AC-UI-19): indented items nest inside the previous item, so numbering
  continues across nested bullets (one `<ol>`); an ordered list keeps its first number (`<ol start>`); `_em_` is
  italic when not inside a word (snake_case stays literal).
- AC-UI-31 **Review queue polish** (UI2-13, UI2-14, UI2-15):
  - A filtered queue shows "Showing x of y open review items." (x = matches, y = all open), plus "Tab counts are totals
    for the whole queue"; a truncated list shows "Showing n of x matching review items.".
  - Subject / candidate landmarks are named "Subject: <label> <id>, review <id>" and "Possible matches for …" (unique).
  - Result toasts describe the operator's action: "Kept both items: <subject> stays a separate item.",
    "Classified as <catalog name>.", "Created catalog item <name> and classified the item as it.", "Dismissed …".
- AC-UI-32 **Layout** (UI2-17, UI-21, 1024 px duplicate cards):
  - The filter grid is `auto-fill` 14rem columns with fields top-aligned (date hints no longer lift the inputs).
    Search spans 2 columns, the Sort select is at least 14rem, and "Needs review" sits in the Status group on its own
    full-width row.
  - Duplicate cards show at most 4 candidates until "Show N more candidates". Their actions (catalog select, buttons,
    merge/dismiss confirm) sit in a footer that sticks to the bottom of the viewport while the card is on screen.
  - Checked with Playwright at 1024 and 1440 px (scripts/ui_check.py) and axe (0 violations).
