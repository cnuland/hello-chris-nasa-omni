#!/usr/bin/env bash
# Build the service images in-cluster (OpenShift binary Docker builds) from a clean copy of the working tree.
# Podman equivalent, and the supported path: scripts/build-images.sh.
# --from-dir (oc makes the archive) instead of a macOS bsdtar tarball: bsdtar's xattr/AppleDouble entries made the
# builder's extractor fail ("unable to extract binary build input").

# Ubuntu's /bin/sh is dash, which has no `set -o pipefail`: `sh thisscript.sh` would die with
# "Illegal option -o pipefail". Re-exec under bash so the script behaves the same however it is invoked
# (including when the executable bit was lost copying the bundle to a host).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root: the build context is the whole tree
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
EXCL=(--exclude=.venv --exclude=node_modules --exclude=__pycache__ --exclude='*.egg-info' --exclude=.pytest_cache
      --exclude=.git --exclude=research --exclude=dist --exclude='*.mp4' --exclude='eval/runs' --exclude='.DS_Store')
rsync -a "${EXCL[@]}" "$ROOT/" "$TMP/root/"
for b in ${BUILDS:-inventory-api perception agent dashboard}; do
  case "$b" in agent) D="$TMP/root/services/agent";; dashboard) D="$TMP/root/dashboard";; *) D="$TMP/root";; esac
  echo "==> build $b"
  oc start-build "$b" -n nasa-app --from-dir="$D" --wait
done
