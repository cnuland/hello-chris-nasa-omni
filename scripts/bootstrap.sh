#!/usr/bin/env bash
# End-to-end cluster bring-up. Idempotent. Needs KUBECONFIG pointing at the target cluster (cluster-admin).
# Secrets are generated here with random values and never written to the repo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
log() { printf '\n==> %s %s\n' "$(date +%H:%M:%S)" "$*"; }
step() { [ -z "${ONLY:-}" ] || [[ " $ONLY " == *" $1 "* ]]; }

if step gpu;   then log "GPU machinesets + operator"; "$ROOT/deploy/platform/gpu-machinesets.sh"; "$ROOT/deploy/platform/install-gpu.sh"; fi
if step odh;   then log "Open Data Hub";  "$ROOT/deploy/platform/install-odh.sh"; fi
if step cnpg;  then log "CloudNativePG";  "$ROOT/deploy/platform/install-cnpg.sh"; fi

if step data; then
  log "data tier"
  oc apply -f "$ROOT/deploy/data/namespace.yaml"
  oc get secret rustfs-creds -n nasa-data >/dev/null 2>&1 || oc create secret generic rustfs-creds -n nasa-data \
    --from-literal=access-key="nasa$(openssl rand -hex 6)" --from-literal=secret-key="$(openssl rand -hex 24)"
  oc apply -k "$ROOT/deploy/data"
  # fully qualified: on ARO the short name "cluster" resolves to clusters.aro.openshift.io
  oc wait --for=condition=Ready clusters.postgresql.cnpg.io/inventory-db -n nasa-data --timeout=15m
  oc create configmap inventory-migrations -n nasa-data --from-file="$ROOT/contracts/db" --dry-run=client -o yaml | oc apply -f -
  oc delete job inventory-migrate -n nasa-data --ignore-not-found
  oc apply -f "$ROOT/deploy/data/migrate-job.yaml"
  oc wait --for=condition=Complete job/inventory-migrate -n nasa-data --timeout=10m
fi

if step inference; then
  log "inference tier"
  # two 4xT4 nodes (Azure policy allows only T4): perception = omni TP2 + agent + ASR, decision = DiffusionGemma TP4
  T4="$(oc get nodes -l gpu-pool=t4 -o name | sort)"   # (macOS bash 3.2: no mapfile)
  [ "$(echo "$T4" | grep -c node)" -ge 2 ] || { echo "need 2 T4 nodes"; exit 1; }
  oc label "$(echo "$T4" | sed -n 1p)" nasa-role=perception --overwrite
  oc label "$(echo "$T4" | sed -n 2p)" nasa-role=decision --overwrite
  oc apply -k "$ROOT/deploy/inference"
fi

if step app; then
  log "application tier"
  oc apply -f "$ROOT/deploy/app/namespace.yaml"
  # Secrets are namespaced: copy the CNPG app credentials and RustFS keys into nasa-app (values never leave the cluster)
  for s in inventory-db-app rustfs-creds; do
    oc get secret "$s" -n nasa-data -o json | jq 'del(.metadata.namespace,.metadata.uid,.metadata.resourceVersion,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.managedFields,.metadata.labels,.metadata.annotations)' \
      | oc apply -n nasa-app -f -   # the CNPG uri host inventory-db-rw.nasa-data resolves cluster-wide
  done
  oc get secret mcp-token -n nasa-app >/dev/null 2>&1 || oc create secret generic mcp-token -n nasa-app --from-literal=token="$(openssl rand -hex 24)"
  oc apply -k "$ROOT/deploy/app"
  "$ROOT/scripts/build-images.sh"
  oc rollout status deploy/inventory-api -n nasa-app --timeout=10m
  # the object-store bucket (frames, crops, review images, operator photos); idempotent
  oc exec -n nasa-app deploy/inventory-api -- python -c '
import os, boto3
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["S3_ACCESS_KEY"],
                  aws_secret_access_key=os.environ["S3_SECRET_KEY"], region_name="us-east-1")
b = os.environ.get("S3_BUCKET", "inventory")
if b not in [x["Name"] for x in s3.list_buckets().get("Buckets", [])]:
    s3.create_bucket(Bucket=b)
print("bucket ready:", b)'
fi
log "done"
