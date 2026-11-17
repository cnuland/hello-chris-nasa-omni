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
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -gt 0 ] || { echo "usage: $0 <unit> [<unit>...]   (gemma4 dgemma dgemma-decision asr agent-llm)" >&2; exit 2; }

for u in "$@"; do
  [ -f "$HERE/quadlet/$u.container" ] || { echo "no such unit: $u.container" >&2; exit 2; }
done

sudo install -d -m 0755 /etc/containers/systemd /etc/nasa /var/lib/nasa/models
sudo install -m 0755 "$HERE/config/vllm-start.sh" /etc/nasa/vllm-start.sh
sudo install -m 0644 "$HERE/quadlet/nasa.network" /etc/containers/systemd/
for u in "$@"; do sudo install -m 0644 "$HERE/quadlet/$u.container" /etc/containers/systemd/; done

# Fail on a unit syntax error before systemd sees it (`if`, not `&&`, which would suppress set -e).
if [ -x /usr/libexec/podman/quadlet ]; then sudo /usr/libexec/podman/quadlet -dryrun >/dev/null; fi
sudo systemctl daemon-reload
for u in "$@"; do sudo systemctl start "$u.service"; done

echo
echo "started: $*"
echo "Model weights download on first start and vLLM warm-up can take 15-40 minutes; the units report healthy"
echo "only when /health answers. Watch with:  journalctl -fu ${1}.service"
