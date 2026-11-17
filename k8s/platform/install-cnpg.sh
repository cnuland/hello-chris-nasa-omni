#!/usr/bin/env bash
# CloudNativePG operator (Apache-2.0) from the upstream release manifest; images on ghcr.io (no Red Hat catalog).
set -euo pipefail
VER="${CNPG_VERSION:-1.30.1}"
oc apply --server-side -f "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-${VER%.*}/releases/cnpg-${VER}.yaml"
oc adm policy add-scc-to-user nonroot-v2 -z cnpg-manager -n cnpg-system >/dev/null 2>&1 || true
oc rollout status deployment/cnpg-controller-manager -n cnpg-system --timeout=5m
