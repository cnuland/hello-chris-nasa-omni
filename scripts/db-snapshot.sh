#!/usr/bin/env bash
# Save or restore the inventory database, for example to keep a demo baseline between experiments.
#
#   scripts/db-snapshot.sh save <file.dump>        custom-format pg_dump
#   scripts/db-snapshot.sh restore <file.dump>     REPLACES all data, then re-owns it to the inventory role
#
# Against a Quadlet host, run it with the root store:  PODMAN="sudo podman" scripts/db-snapshot.sh save x.dump

# Ubuntu's /bin/sh is dash, which has no `set -o pipefail`: `sh thisscript.sh` would die with
# "Illegal option -o pipefail". Re-exec under bash so the script behaves the same however it is invoked
# (including when the executable bit was lost copying the bundle to a host).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../deploy/scripts/lib.sh
. "$ROOT/deploy/scripts/lib.sh"

PG=inventory-db-postgres
$PODMAN container exists "$PG" || die "$PG is not running (scripts/stack.sh up, or PODMAN=\"sudo podman\")"

case "${1:-}" in
  save)
    $PODMAN exec "$PG" pg_dump -U postgres -d inventory -Fc > "${2:?usage: $0 save <file.dump>}"
    log "saved $2 ($(wc -c <"$2" | tr -d ' ') bytes)"
    ;;
  restore)
    [ -f "${2:-}" ] || die "usage: $0 restore <file.dump>"
    $PODMAN exec -i "$PG" pg_restore -U postgres -d inventory --clean --if-exists --no-owner < "$2"
    nasa_db_reown
    log "restored $2"
    ;;
  *) echo "usage: $0 save|restore <file.dump>" >&2; exit 2;;
esac
