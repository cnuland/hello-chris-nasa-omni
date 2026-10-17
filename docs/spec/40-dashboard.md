# Spec: dashboard (dashboard/) — React + Vite + TS

Talks only to inventory-api (`/api` proxy) and agent (`/agent` proxy).

- AC-UI-1 **Inventory view** (default route `/`): a table or cards of items, each row showing:
  - snapshot thumbnail, label, catalog name, status badge, confidence bar;
  - last location (display name + code), last seen (relative and UTC on hover);
  - last moved by, and a needs-review flag.
- AC-UI-2 **Filters**: text search (debounced 300 ms), kind, status (multi), location (hierarchical select), min
  confidence slider, time range, needs-review toggle. Filters are reflected in the URL query string (shareable) and
  map 1:1 to `/items` params.
- AC-UI-3 Sorting by last seen (default newest first), label and confidence, plus pagination (50/page) with the total
  count shown.
- AC-UI-4 **Item detail** drawer/page: large snapshot, attributes, history timeline (sightings/events/audit) with
  evidence images, and "moved by … at …" lines.
- AC-UI-5 **Edit item** form (off-screen changes): label, description, catalog, location, status, and an
  operator/note field. It sends PATCH with only the changed fields and shows success/error.
- AC-UI-6 **Add item** form: an image is **required** (the submit button is disabled and a validation message shown
  until an image file is chosen), plus label, location and optional catalog/description. It posts multipart.
- AC-UI-7 **Unknowns / Review queue** (`/review`): cards with the image, description, timestamp, location, model
  guess and kind. Actions: classify as a catalog item (searchable select), create a new catalog item, merge into one
  of the candidates (shown side-by-side with images), or dismiss. The card disappears on success. The open-count
  badge shows in the nav.
- AC-UI-8 **Chat** (`/chat`): message list, input, streaming reply, and clickable citations that open the item
  detail. "Proposed update" messages render a confirm button that performs the PATCH.
- AC-UI-9 **Overview**: stats tiles (items, unknowns, possible duplicates, people seen in the last hour) and a recent
  events feed (auto-refresh every 10 s).
- AC-UI-10 Accessible labels on all inputs; works at 1280 px and 1920 px; dark theme (mission-control style). Errors
  from the API are shown as non-blocking toasts.
