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
