#!/usr/bin/env bash
# Save or restore the inventory DB (custom-format pg_dump), e.g. to keep a demo baseline between experiments.
#   db-snapshot.sh save <file.dump>      |      db-snapshot.sh restore <file.dump>   (restore replaces all data)
set -euo pipefail
NS=nasa-data; P="$(oc get pod -n $NS -l cnpg.io/cluster=inventory-db,cnpg.io/instanceRole=primary -o name | head -1)"
case "${1:-}" in
  save)    oc exec -n $NS "$P" -c postgres -- pg_dump -U postgres -d inventory -Fc > "${2:?file}";;
  restore) oc exec -i -n $NS "$P" -c postgres -- pg_restore -U postgres -d inventory --clean --if-exists --no-owner < "${2:?file}";;
  *) echo "usage: $0 save|restore <file.dump>"; exit 2;;
esac
