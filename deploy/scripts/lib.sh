#!/usr/bin/env bash
# Shared helpers for the Podman deployment scripts. Sourced, never executed.
#
# $PODMAN lets every script run against either store without a second code path:
#   podman          rootless, the developer's own containers (the default)
#   sudo podman     the root store that Quadlet / systemd units run from
# shellcheck shell=bash

PODMAN="${PODMAN:-podman}"

log()  { printf '\n==> %s %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
rand() { openssl rand -hex "${1:-24}"; }

# base64 without line wrapping, on both GNU coreutils and BSD/macOS (`-w0` exists only on GNU).
b64()     { printf %s "$1" | base64 | tr -d '\n'; }
# Files go through base64 directly: $(cat file) would strip the trailing newline of a PEM.
b64file() { base64 < "$1" | tr -d '\n'; }

# ------------------------------------------------------------------------------------------------ secrets
# Secrets live only in podman's secret store. They are generated here and piped into `podman kube play -` on
# stdin, so no plaintext or base64 copy is ever written to a file. Reading them back later uses podman itself.

# nasa_secret_get <secret> <key> - one value from a Secret created by `podman kube play`, which stores the whole
# Secret manifest as the secret's data.
nasa_secret_get() {
  $PODMAN secret inspect --showsecret "$1" \
    | jq -r '.[0].SecretData' \
    | awk -v k="$2:" '$1 == k { print $2; found = 1 } END { exit !found }' \
    | base64 -d
}

# nasa_secrets_ensure - create `nasa-secrets` with fresh random credentials if it does not exist yet, then
# export the values the callers need. Idempotent: an existing secret is read back, never regenerated, because
# the Postgres data volume and the stored S3 objects are keyed to the first set.
nasa_secrets_ensure() {
  if ! $PODMAN secret exists nasa-secrets; then
    log "generating nasa-secrets"
    local pg inv s3a s3s mcp
    pg=$(rand) inv=$(rand) s3a="nasa$(rand 6)" s3s=$(rand) mcp=$(rand)
    $PODMAN kube play - >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata: {name: nasa-secrets}
data:
  postgres-password: $(b64 "$pg")
  inventory-db-password: $(b64 "$inv")
  database-url: $(b64 "postgresql://inventory:$inv@inventory-db:5432/inventory")
  s3-access-key: $(b64 "$s3a")
  s3-secret-key: $(b64 "$s3s")
  mcp-token: $(b64 "$mcp")
YAML
  fi
  S3_ACCESS_KEY=$(nasa_secret_get nasa-secrets s3-access-key)
  S3_SECRET_KEY=$(nasa_secret_get nasa-secrets s3-secret-key)
  export S3_ACCESS_KEY S3_SECRET_KEY
}

# nasa_tls_ensure <common-name> - self-signed certificate for the TLS front door, also kept as a podman secret.
# Replace it with a real certificate by deleting the secret and re-creating it from the issued files:
#   podman secret rm nasa-tls
#   printf 'apiVersion: v1\nkind: Secret\nmetadata: {name: nasa-tls}\ndata:\n  tls.crt: %s\n  tls.key: %s\n' \
#     "$(base64 -w0 your.crt)" "$(base64 -w0 your.key)" | podman kube play -
nasa_tls_ensure() {
  if $PODMAN secret exists nasa-tls; then return 0; fi
  log "generating a self-signed TLS certificate for ${1}"
  local dir; dir=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=${1}" \
    -addext "subjectAltName=DNS:${1}" -keyout "$dir/tls.key" -out "$dir/tls.crt" 2>/dev/null
  $PODMAN kube play - >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata: {name: nasa-tls}
data:
  tls.crt: $(b64file "$dir/tls.crt")
  tls.key: $(b64file "$dir/tls.key")
YAML
  rm -rf "$dir"
}

# ------------------------------------------------------------------------------------------------ waiting
# nasa_wait <seconds> <description> <command...> - poll until the command succeeds.
nasa_wait() {
  local timeout=$1 what=$2; shift 2
  local deadline=$(( $(date +%s) + timeout ))
  printf '    waiting for %s ' "$what"
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || { printf ' timeout\n'; return 1; }
    printf '.'; sleep 2
  done
  printf ' ok\n'
}

nasa_wait_db()  { nasa_wait "${1:-180}" "Postgres" $PODMAN exec inventory-db-postgres pg_isready -U postgres -d inventory; }
nasa_wait_api() { nasa_wait "${1:-180}" "inventory-api" $PODMAN exec inventory-api-api \
                    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)"; }

# ------------------------------------------------------------------------------------------------ database
# nasa_db_reown - hand every object in the public schema back to the `inventory` role. pg_restore runs with
# --no-owner (the dump's owner does not exist on a fresh host), which leaves everything owned by postgres,
# while the API connects as `inventory`.
nasa_db_reown() {
  $PODMAN exec inventory-db-postgres psql -U postgres -d inventory -q -v ON_ERROR_STOP=1 -c "
DO \$\$ DECLARE r record; BEGIN
 FOR r IN SELECT format('%I.%I', schemaname, tablename) t FROM pg_tables WHERE schemaname='public' LOOP EXECUTE 'ALTER TABLE '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT format('%I.%I', sequence_schema, sequence_name) t FROM information_schema.sequences WHERE sequence_schema='public' LOOP EXECUTE 'ALTER SEQUENCE '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT format('%I.%I', schemaname, viewname) t FROM pg_views WHERE schemaname='public' LOOP EXECUTE 'ALTER VIEW '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT p.oid::regprocedure::text t FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=p.oid AND d.deptype='e') LOOP EXECUTE 'ALTER FUNCTION '||r.t||' OWNER TO inventory'; END LOOP;
END \$\$;"
}

# ------------------------------------------------------------------------------------------------ object store
# nasa_bucket_ensure - create the object-store bucket for frames, crops, review images and operator photos.
# Run from inside inventory-api, the one container that already has boto3 and the credentials.
nasa_bucket_ensure() {
  $PODMAN exec inventory-api-api python -c '
import os, boto3
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["S3_ACCESS_KEY"],
                  aws_secret_access_key=os.environ["S3_SECRET_KEY"], region_name="us-east-1")
b = os.environ.get("S3_BUCKET", "inventory")
if b not in [x["Name"] for x in s3.list_buckets().get("Buckets", [])]:
    s3.create_bucket(Bucket=b)
print("    bucket ready:", b)'
}
