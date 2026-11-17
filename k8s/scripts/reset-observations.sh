#!/usr/bin/env bash
# Clear observation-derived data (entities, sightings, windows, events, reviews, stock, decisions, agent memory) while
# keeping the seed (locations, cameras, catalog) and the audit log. Destructive: prints counts and requires --yes.
# Object-store frames/crops are left in place (orphaned keys are harmless for the POC).
set -euo pipefail
NS=nasa-data; POD="$(oc get pod -n $NS -l cnpg.io/cluster=inventory-db,cnpg.io/instanceRole=primary -o name | head -1)"
TABLES="review_item, event, sighting, entity_exemplar, stock, observation_window, decision_log, agent_memory, entity"
oc exec -n $NS "$POD" -c postgres -- psql -U postgres -d inventory -c \
  "select 'entity' t, count(*) from entity union all select 'sighting', count(*) from sighting union all select 'review_item', count(*) from review_item union all select 'observation_window', count(*) from observation_window"
[ "${1:-}" = "--yes" ] || { echo "dry run: re-run with --yes to TRUNCATE: $TABLES"; exit 0; }
oc exec -n $NS "$POD" -c postgres -- psql -U postgres -d inventory -v ON_ERROR_STOP=1 -c "TRUNCATE $TABLES RESTART IDENTITY CASCADE"
echo "observation data cleared (seed + audit_log kept)"
