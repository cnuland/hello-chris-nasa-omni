#!/usr/bin/env bash
# Put a sample feed on the perception media volume.
#
#   scripts/load-sample-video.sh <local.mp4> [name-in-volume]        default name: sample.mp4
#   PODMAN="sudo podman" scripts/load-sample-video.sh tour.mp4       on a Quadlet host (root store)
#
# Default clip: NASA "Step Inside the International Space Station" (public domain, 2022, 15 min, with audio):
#   http://images-assets.nasa.gov/video/Step_Inside_the_Station-4K/Step_Inside_the_Station-4K~medium.mp4
#
# perception is stopped while the file is written, because it reads VIDEO_SOURCE continuously and would see a
# half-copied file. On a Quadlet host it is restarted afterwards only if it was running.

# Ubuntu's /bin/sh is dash, which has no `set -o pipefail`: `sh thisscript.sh` would die with
# "Illegal option -o pipefail". Re-exec under bash so the script behaves the same however it is invoked
# (including when the executable bit was lost copying the bundle to a host).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../deploy/scripts/lib.sh
. "$ROOT/deploy/scripts/lib.sh"

SRC="${1:?usage: load-sample-video.sh <local.mp4> [name-in-volume]}"
NAME="${2:-sample.mp4}"
VOLUME="${NASA_MEDIA_VOLUME:-nasa-media}"
[ -f "$SRC" ] || die "no such file: $SRC"

WAS_RUNNING=0
if $PODMAN container exists perception && [ "$($PODMAN inspect perception --format '{{.State.Running}}')" = true ]; then
  WAS_RUNNING=1
  log "stopping perception"
  systemctl is-active --quiet perception.service 2>/dev/null && sudo systemctl stop perception.service || $PODMAN stop perception
fi

log "copying $(basename "$SRC") into volume $VOLUME as $NAME"
# Streamed through a throwaway container: it needs no tar on either side and works rootless and rootful alike.
$PODMAN run --rm -i --user 0 -v "$VOLUME:/media" docker.io/library/busybox:stable \
  sh -c "cat > /media/$NAME && ls -la /media" < "$SRC"

if [ "$WAS_RUNNING" = 1 ]; then
  log "starting perception"
  systemctl is-enabled --quiet perception.service 2>/dev/null && sudo systemctl start perception.service || $PODMAN start perception
fi
