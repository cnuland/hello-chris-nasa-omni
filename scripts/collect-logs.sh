#!/usr/bin/env bash
# Collect everything needed to diagnose a failing or crash-looping unit into one shareable file.
#
#   scripts/collect-logs.sh                      # every POC unit found on this host
#   scripts/collect-logs.sh gemma4               # just one
#   scripts/collect-logs.sh --stop gemma4        # stop it first, so the restart loop stops churning
#   scripts/collect-logs.sh -o /tmp/gb10.txt gemma4 dgemma
#
# Why this exists: Quadlet runs containers with --rm, so a container that crashes on start deletes itself and
# `podman logs <name>` answers "no such container" - it looks as though nothing was logged. It was: podman's
# journald log driver already sent the container's stdout and stderr to the systemd journal under the unit.
# This script reads it from there, together with the generated unit (the exact podman command that ran) and
# enough host context to tell a configuration problem from a hardware one.

# Ubuntu's /bin/sh is dash, which has no `set -o pipefail`: `sh thisscript.sh` would die with
# "Illegal option -o pipefail". Re-exec under bash so the script behaves the same however it is invoked
# (including when the executable bit was lost copying the bundle to a host).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

OUT="/tmp/nasa-logs-$(date +%Y%m%d-%H%M%S).txt"
SINCE="" STOP=0
UNITS=()
while [ $# -gt 0 ]; do case "$1" in
  -o) OUT="${2:?-o needs a file}"; shift 2;;
  --since) SINCE="${2:?--since needs a time}"; shift 2;;
  --stop) STOP=1; shift;;
  -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  -*) echo "unknown option $1" >&2; exit 2;;
  *) UNITS+=("$1"); shift;;
esac; done

command -v systemctl >/dev/null || { echo "this host has no systemd; use 'podman logs <container>' instead" >&2; exit 2; }

# Default to whatever POC units are actually installed.
if [ ${#UNITS[@]} -eq 0 ]; then
  for u in nasa-data nasa-app nasa-tls perception gemma4 dgemma dgemma-decision asr agent-llm; do
    systemctl cat "$u.service" >/dev/null 2>&1 && UNITS+=("$u")
  done
fi
[ ${#UNITS[@]} -gt 0 ] || { echo "no POC units found on this host" >&2; exit 1; }

JARGS=(--no-pager --no-hostname)
[ -n "$SINCE" ] && JARGS+=(--since "$SINCE") || JARGS+=(--boot)

say() { printf '\n========== %s ==========\n' "$*"; }

{
  say "host"
  (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || true
  echo "kernel:  $(uname -srm)"
  echo "podman:  $(podman --version 2>&1)"
  echo "units:   ${UNITS[*]}"

  say "GPUs"
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv 2>&1 || echo "nvidia-smi unavailable"
  echo "--- CDI devices ---"
  nvidia-ctk cdi list 2>&1 | head -20 || echo "nvidia-ctk unavailable"

  say "memory"
  free -h 2>&1 || true

  say "images in the root store"
  sudo podman images --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' 2>&1 | head -20

  say "containers"
  sudo podman ps --all --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>&1 | head -30

  for u in "${UNITS[@]}"; do
    if [ "$STOP" = 1 ]; then
      say "stopping $u (so the restart loop stops churning)"
      sudo systemctl stop "$u.service" 2>&1 || true
    fi
    say "systemctl status $u"
    systemctl status "$u.service" --no-pager -l 2>&1 | head -30 || true
    say "generated unit for $u (the exact podman command)"
    systemctl cat "$u.service" 2>&1 || true
    say "journal for $u (container stdout and stderr land here, not in podman logs)"
    sudo journalctl -u "$u.service" "${JARGS[@]}" 2>&1 || true
  done
} > "$OUT" 2>&1

echo "wrote $OUT  ($(wc -l <"$OUT" | tr -d ' ') lines, $(du -h "$OUT" | cut -f1))"
echo
echo "Most recent errors:"
grep -iE 'error|traceback|exception|failed|cannot|no such|out of memory|assert' "$OUT" | tail -15 | sed 's/^/  /'
