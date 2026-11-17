#!/usr/bin/env bash
# Clear observation-derived data (entities, sightings, windows, events, reviews, stock, decisions, agent memory)
# while keeping the seed (locations, cameras, catalog) and the audit log.
#
# Destructive: prints the current counts and does nothing unless --yes is given.
# Object-store frames and crops are left in place; orphaned keys are harmless for the POC.
#
#   scripts/reset-observations.sh            dry run
#   scripts/reset-observations.sh --yes      truncate
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../deploy/scripts/lib.sh
. "$ROOT/deploy/scripts/lib.sh"

PG=inventory-db-postgres
$PODMAN container exists "$PG" || die "$PG is not running (scripts/stack.sh up, or PODMAN=\"sudo podman\")"

# entity last: the others reference it.
TABLES="review_item, event, sighting, entity_exemplar, stock, observation_window, decision_log, agent_memory, entity"

$PODMAN exec "$PG" psql -U postgres -d inventory -c \
  "select 'entity' t, count(*) from entity
   union all select 'sighting', count(*) from sighting
   union all select 'review_item', count(*) from review_item
   union all select 'observation_window', count(*) from observation_window"

[ "${1:-}" = "--yes" ] || { echo "dry run: re-run with --yes to TRUNCATE: $TABLES"; exit 0; }

$PODMAN exec "$PG" psql -U postgres -d inventory -v ON_ERROR_STOP=1 \
  -c "TRUNCATE $TABLES RESTART IDENTITY CASCADE"
log "observation data cleared (seed + audit_log kept)"
