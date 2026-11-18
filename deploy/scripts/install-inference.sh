#!/usr/bin/env bash
# Install GPU model servers on this host as Quadlet units.
#
#   install-inference.sh [--profile gb10|t4] <unit> [<unit>...]
#
#   single host (GB10)   install-inference.sh gemma4 dgemma dgemma-decision asr
#   split T4 hosts       omni host:      install-inference.sh gemma4
#                        decision host:  install-inference.sh dgemma dgemma-decision asr
#                        app host:       install-inference.sh agent-llm
#
# The profile decides which variant of each unit is installed, and defaults to counting the GPUs on this host:
#
#   gb10  one Blackwell GPU, 128 GB unified memory. FP8 / INT8 weights, tensor-parallel 1, each server given a
#         fraction of the single GPU, host ports on loopback. quadlet/gb10/*.container
#   t4    four T4s per host, split across machines. fp32 (T4 has no bf16), tensor-parallel 4, whole GPUs,
#         sm75 start-up patches. quadlet/*.container
#
# Installing the t4 units on a GB10 fails in several ways at once: tensor-parallel 4 with one GPU, asr pinned
# to a GPU 3 that does not exist, gemma4 and dgemma both claiming host port 8000, and memory fractions that
# add up to more than the box has. Hence the profile rather than a note in the README.
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

usage() { echo "usage: $0 [--profile gb10|t4] <unit> [<unit>...]   (gemma4 dgemma dgemma-decision asr agent-llm)" >&2; exit 2; }

PROFILE=auto
UNITS_IN=""
while [ $# -gt 0 ]; do case "$1" in
  --profile) PROFILE="${2:?--profile needs gb10 or t4}"; shift 2;;
  -h|--help) usage;;
  -*) die "unknown option $1";;
  *) UNITS_IN="$UNITS_IN $1"; shift;;
esac; done
# shellcheck disable=SC2086
set -- $UNITS_IN
[ $# -gt 0 ] || usage

if [ "$PROFILE" = auto ]; then
  GPUS="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)"
  GPUS="${GPUS:-0}"
  if [ "$GPUS" = 1 ]; then PROFILE=gb10; else PROFILE=t4; fi
  log "detected ${GPUS} GPU(s) -> '${PROFILE}' profile (override with --profile)"
else
  case "$PROFILE" in gb10|t4) ;; *) die "unknown profile '$PROFILE' (gb10 or t4)";; esac
  log "using the '${PROFILE}' profile"
fi

# unit_file <name> - the profile's variant if it has one, otherwise the shared default.
unit_file() {
  if [ -f "$HERE/quadlet/$PROFILE/$1.container" ]; then
    printf %s "$HERE/quadlet/$PROFILE/$1.container"
  else
    printf %s "$HERE/quadlet/$1.container"
  fi
}

for u in "$@"; do
  [ -f "$(unit_file "$u")" ] || die "no such unit: $u.container"
done
if [ "$PROFILE" = gb10 ]; then
  case " $* " in *" agent-llm "*)
    echo "    note: agent-llm is not needed on the GB10 - the gemma4 server answers chat as well." >&2;;
  esac
fi

# Validate the units in a staging directory first, so a syntax error costs nothing on the host.
log "checking unit files"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE/quadlet/nasa.network" "$STAGE/"
for u in "$@"; do cp "$(unit_file "$u")" "$STAGE/$u.container"; done
nasa_quadlet_check "$STAGE"

log "installing units"
sudo install -d -m 0755 /etc/containers/systemd /etc/nasa /var/lib/nasa/models
sudo install -m 0755 "$HERE/config/vllm-start.sh" /etc/nasa/vllm-start.sh
sudo install -m 0644 "$HERE/quadlet/nasa.network" /etc/containers/systemd/
for u in "$@"; do
  src="$(unit_file "$u")"
  echo "    $u  <- ${src#"$HERE"/}"
  sudo install -m 0644 "$src" "/etc/containers/systemd/$u.container"
done

sudo systemctl daemon-reload
# Confirm the generator actually ran, rather than discovering it did not from `systemctl start`.
UNITS=""
for u in "$@"; do UNITS="$UNITS $u.service"; done
# shellcheck disable=SC2086
nasa_quadlet_verify $UNITS

log "starting"
for u in "$@"; do sudo systemctl start "$u.service"; done

echo
echo "started ($PROFILE profile): $*"
echo "Weight download and vLLM warm-up take 15-40 minutes on a first start; a unit reports healthy only once"
echo "/health answers. Watch with:   journalctl -fu ${1}.service"
echo "Check health with:             sudo podman ps --format '{{.Names}}\t{{.Status}}'"
