#!/usr/bin/env bash
# Open Data Hub 3.x (the upstream of Red Hat OpenShift AI) from its public quay.io catalog: no registry.redhat.io pull
# secret needed (research/05 §6.2). Components: KServe (RawDeployment), dashboard, TrustyAI, model registry.
# cert-manager comes from its upstream Helm chart, because the Red Hat catalog operator is not reachable.
set -euo pipefail
ODH_CATALOG_TAG="${ODH_CATALOG_TAG:-v3.3.0}"   # latest stable tag on quay (v3.5.0 was deleted, 2026-10-17)
log() { printf '\n==> %s %s\n' "$(date +%H:%M:%S)" "$*"; }
log "cert-manager (upstream Helm)"
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true; helm repo update >/dev/null
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 10m
log "disable default Red Hat catalog sources (they fail without a valid pull secret)"
oc patch operatorhub cluster --type merge -p '{"spec":{"disableAllDefaultSources":true}}'
log "ODH catalog + subscription"
oc apply -f - <<YAML
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata: {name: odh-catalog, namespace: openshift-marketplace}
spec: {sourceType: grpc, image: quay.io/opendatahub/opendatahub-operator-catalog:${ODH_CATALOG_TAG}, displayName: Open Data Hub, publisher: ODH}
YAML
for _ in $(seq 1 60); do oc get packagemanifest opendatahub-operator -n openshift-marketplace >/dev/null 2>&1 && break; sleep 10; done
oc get packagemanifest opendatahub-operator -n openshift-marketplace >/dev/null || { echo "ODH package never appeared (catalog pod pull?)"; exit 1; }
CH="$(oc get packagemanifest opendatahub-operator -n openshift-marketplace -o jsonpath='{.status.defaultChannel}')"
echo "channel: $CH"
oc apply -f - <<YAML
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata: {name: opendatahub-operator, namespace: openshift-operators}
spec: {channel: "$CH", name: opendatahub-operator, source: odh-catalog, sourceNamespace: openshift-marketplace, installPlanApproval: Automatic}
YAML
oc apply -f "$(dirname "$0")/odh-rbac-fix.yaml"
log "waiting for the ODH operator CSV"
for _ in $(seq 1 60); do
  CSV="$(oc get subscription opendatahub-operator -n openshift-operators -o jsonpath='{.status.installedCSV}' 2>/dev/null || true)"
  [ -n "$CSV" ] && [ "$(oc get csv "$CSV" -n openshift-operators -o jsonpath='{.status.phase}' 2>/dev/null)" = "Succeeded" ] && break; sleep 10
done
echo "installed: ${CSV:-none}"
oc rollout status deployment/opendatahub-operator-controller-manager -n openshift-operators --timeout=10m
log "DSCInitialization + DataScienceCluster"
oc apply -f "$(dirname "$0")/odh-dsc.yaml"
for _ in $(seq 1 60); do P="$(oc get datasciencecluster default-dsc -o jsonpath='{.status.phase}' 2>/dev/null)"; echo "  dsc: ${P:-?}"; [ "$P" = "Ready" ] && break; sleep 20; done
