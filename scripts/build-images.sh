#!/usr/bin/env bash
# Build the service images with Podman. Tags match deploy/kube/*.yaml: localhost/nasa/<service>:poc.
#
#   scripts/build-images.sh                       # inventory-api, agent, dashboard (minutes)
#   scripts/build-images.sh perception            # just perception (slow: a torch wheel, GB-sized)
#   TORCH_INDEX=cu128 scripts/build-images.sh perception     # CUDA build for a GPU host
#   PODMAN="sudo podman" scripts/build-images.sh             # into the root store a Quadlet host runs from
#
# perception is not in the default set on purpose: it pulls torch + torchvision and takes far longer than the
# other three, and it is the one service that cannot run without a GPU and the model servers anyway.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PODMAN="${PODMAN:-podman}"
TAG="${TAG:-poc}"
TORCH_INDEX="${TORCH_INDEX:-cpu}"

build() {
  local name=$1 context=$2 containerfile=$3; shift 3
  echo "==> build localhost/nasa/${name}:${TAG}"
  $PODMAN build --tag "localhost/nasa/${name}:${TAG}" --file "$containerfile" "$@" "$context"
}

SERVICES=(inventory-api agent dashboard)
[ $# -eq 0 ] || SERVICES=("$@")
for svc in "${SERVICES[@]}"; do
  case "$svc" in
    # Built from the repo root: the Containerfile bundles contracts/db (migrations) and contracts/schemas.
    inventory-api) build inventory-api "$ROOT" "$ROOT/services/inventory-api/Containerfile";;
    perception)    build perception    "$ROOT" "$ROOT/services/perception/Containerfile" --build-arg "TORCH_INDEX=$TORCH_INDEX";;
    agent)         build agent         "$ROOT/services/agent" "$ROOT/services/agent/Containerfile";;
    dashboard)     build dashboard     "$ROOT/dashboard" "$ROOT/dashboard/Containerfile";;
    *) echo "unknown service: $svc (inventory-api|perception|agent|dashboard)" >&2; exit 2;;
  esac
done
$PODMAN images --filter reference='localhost/nasa/*' --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'
