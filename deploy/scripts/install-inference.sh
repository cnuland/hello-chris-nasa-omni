#!/usr/bin/env bash
# Install GPU model servers on this host as Quadlet units. Usage: install-inference.sh <unit> [<unit>...]
#
#   single host (GB10)   install-inference.sh gemma4 dgemma dgemma-decision asr
#   split T4 hosts       omni host:      install-inference.sh gemma4
#                        decision host:  install-inference.sh dgemma dgemma-decision asr
#                        app host:       install-inference.sh agent-llm
#
# Model weights are cached under /var/lib/nasa/models, a host directory rather than a volume: it holds tens of
# gigabytes, operators need to see and size it, and it must survive `podman system reset`.

# Ubuntu's /bin/sh is dash, which has no `set -o pipefail`: `sh thisscript.sh` would die with
# "Illegal option -o pipefail". Re-exec under bash so the script behaves the same however it is invoked
# (including when the executable bit was lost copying the bundle to a host).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/scripts/lib.sh"

[ $# -gt 0 ] || { echo "usage: $0 <unit> [<unit>...]   (gemma4 dgemma dgemma-decision asr agent-llm)" >&2; exit 2; }
for u in "$@"; do
  [ -f "$HERE/quadlet/$u.container" ] || die "no such unit: $u.container"
done

# Validate the units in a staging directory first, so a syntax error costs nothing on the host.
log "checking unit files"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE/quadlet/nasa.network" "$STAGE/"
for u in "$@"; do cp "$HERE/quadlet/$u.container" "$STAGE/"; done
nasa_quadlet_check "$STAGE"

log "installing units"
sudo install -d -m 0755 /etc/containers/systemd /etc/nasa /var/lib/nasa/models
sudo install -m 0755 "$HERE/config/vllm-start.sh" /etc/nasa/vllm-start.sh
sudo install -m 0644 "$HERE/quadlet/nasa.network" /etc/containers/systemd/
for u in "$@"; do sudo install -m 0644 "$HERE/quadlet/$u.container" /etc/containers/systemd/; done

sudo systemctl daemon-reload
# Confirm the generator actually ran, rather than discovering it did not from `systemctl start`.
UNITS=""
for u in "$@"; do UNITS="$UNITS $u.service"; done
# shellcheck disable=SC2086
nasa_quadlet_verify $UNITS

log "starting"
for u in "$@"; do sudo systemctl start "$u.service"; done

echo
echo "started: $*"
echo "Model weights download on first start and vLLM warm-up can take 15-40 minutes; the units report healthy"
echo "only when /health answers. Watch with:  journalctl -fu ${1}.service"
