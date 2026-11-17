#!/usr/bin/env bash
# Run the data and application tiers from deploy/kube/*.yaml with `podman kube play`. This is the development
# and demo path: rootless, no systemd, and the same manifests the POC host runs under Quadlet.
#
# For a systemd-managed POC host use deploy/scripts/install-app.sh instead: it installs this same YAML as Quadlet
# units so the stack comes back after a reboot.
#
# The GPU model servers (gemma4, dgemma, asr) are not part of this: they need CUDA and 16-100 GB of weights, so
# they only ever run as Quadlet units on a GPU host. Point this stack at them with --endpoints; without it the
# inventory, review queue and dashboard all work, and only chat answers fail.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../deploy/scripts/lib.sh
. "$ROOT/deploy/scripts/lib.sh"

KUBE="$ROOT/deploy/kube"
NETWORK="${NASA_NETWORK:-nasa}"
ENDPOINTS="$KUBE/endpoints.yaml"
BUILD=0 TLS=0 VOLUMES=0
ARGS=()

usage() {
  cat <<'USAGE'
usage: scripts/stack.sh <command> [options]

  up [--build] [--tls] [--endpoints FILE]   start the data and application tiers (idempotent)
  down [--volumes]                          stop them; --volumes also deletes the database and object store
  status                                    pods, containers and published ports
  logs <pod> [-f]                           follow one pod: inventory-db rustfs inventory-api agent dashboard tls

  --build        rebuild the service images first (scripts/build-images.sh)
  --tls          also start the HTTPS front door on :443 with a self-signed certificate
  --endpoints    ConfigMap with the model server URLs (default deploy/kube/endpoints.yaml)
USAGE
}

cmd="${1:-}"; shift || true
while [ $# -gt 0 ]; do case "$1" in
  --build)     BUILD=1; shift;;
  --tls)       TLS=1; shift;;
  --volumes)   VOLUMES=1; shift;;
  --endpoints) ENDPOINTS="${2:?--endpoints needs a file}"; shift 2;;
  -h|--help)   usage; exit 0;;
  *)           ARGS+=("$1"); shift;;
esac; done

play() { $PODMAN kube play --replace --network "$NETWORK" "$@"; }

case "$cmd" in
up)
  command -v jq >/dev/null || die "jq is required (brew install jq / apt-get install jq)"
  # `if`, not `&&`: a command run as the right-hand side of && has set -e suppressed, so a failed build
  # would be ignored and the stack would come up on stale images.
  if [ "$BUILD" = 1 ]; then "$ROOT/scripts/build-images.sh"; fi
  $PODMAN network exists "$NETWORK" || $PODMAN network create "$NETWORK"
  nasa_secrets_ensure

  log "data tier"
  play "$KUBE/data.yaml"
  nasa_wait_db

  log "application tier"
  # inventory-api applies contracts/db/*.sql at start-up, so the schema is current once it answers /healthz.
  play --configmap "$ENDPOINTS" "$KUBE/app.yaml"
  nasa_wait_api
  nasa_bucket_ensure

  if [ "$TLS" = 1 ]; then
    log "TLS front door"
    nasa_tls_ensure localhost
    play "$KUBE/tls.yaml"
  fi
  log "up"
  if [ "$TLS" = 1 ]; then
    echo "    dashboard: https://localhost/ (self-signed certificate), http://localhost:8080/"
  else
    echo "    dashboard: http://localhost:8080/"
  fi
  ;;

down)
  # Reverse order, and keep going if a tier was never started.
  for f in tls app data; do $PODMAN kube down "$KUBE/$f.yaml" >/dev/null 2>&1 || true; done
  if [ "$VOLUMES" = 1 ]; then
    log "deleting volumes and secrets (database, object store, sample media)"
    # nasa-db-init / nasa-tls-proxy / nasa-tls are the volumes podman materialises for the ConfigMap and Secret
    # mounts. `kube play --replace` refreshes their contents, so they are harmless day to day, but a full
    # teardown should leave nothing behind.
    $PODMAN volume rm --force nasa-pgdata nasa-objects nasa-media nasa-db-init nasa-tls-proxy nasa-tls >/dev/null 2>&1 || true
    $PODMAN secret rm nasa-secrets nasa-tls >/dev/null 2>&1 || true
  fi
  log "down"
  ;;

status)
  # No .Networks column: podman has no header name for it and prints "<no value>" in a table.
  $PODMAN pod ps --format 'table {{.Name}}\t{{.Status}}\t{{.NumberOfContainers}}'
  echo
  # Only this stack's containers, so an unrelated container on the host does not look like part of it.
  $PODMAN ps --all --filter pod=inventory-db --filter pod=rustfs --filter pod=inventory-api \
              --filter pod=agent --filter pod=dashboard --filter pod=tls \
              --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  ;;

logs)
  [ ${#ARGS[@]} -ge 1 ] || { usage; die "logs needs a pod name"; }
  $PODMAN pod logs "${ARGS[@]}"
  ;;

*)
  usage
  exit 2
  ;;
esac
