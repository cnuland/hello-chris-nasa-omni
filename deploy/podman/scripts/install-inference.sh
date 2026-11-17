#!/usr/bin/env bash
# Install the GPU model servers on this host as Quadlet units. Usage: install-inference.sh <unit> [<unit>...]
# e.g. omni host: gemma4    decision host: dgemma dgemma-decision    app host (dev only): asr agent-llm
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
sudo install -d /etc/containers/systemd /etc/nasa /var/lib/nasa/models
sudo install -m 0755 "$HERE/config/vllm-start.sh" /etc/nasa/vllm-start.sh
sudo install -m 0644 "$HERE/quadlet/nasa.network" /etc/containers/systemd/
for u in "$@"; do sudo install -m 0644 "$HERE/quadlet/$u.container" /etc/containers/systemd/; done
sudo /usr/libexec/podman/quadlet -dryrun >/dev/null       # fail early on unit syntax errors
sudo systemctl daemon-reload
for u in "$@"; do sudo systemctl start "$u.service"; done
