#!/usr/bin/env bash
# Install the data and application tiers on this host (Podman + Quadlet).
#   install-app.sh [--omni URL] [--decision URL] [--asr URL] [--chat URL] [--chat-model NAME]
#                  [--restore-db FILE.dump] [--restore-objects FILE.tgz]
# Defaults are the single-host (GB10) layout: every model server on this host's 'nasa' network.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OMNI=http://gemma4:8000 DECISION=http://dgemma-decision:8011 ASR=http://asr:8000 CHAT=http://gemma4:8000/v1 CHAT_MODEL=gemma4
DB_DUMP="" OBJECTS=""
while [ $# -gt 0 ]; do case "$1" in
  --omni) OMNI=$2; shift 2;; --decision) DECISION=$2; shift 2;; --asr) ASR=$2; shift 2;;
  --chat) CHAT=$2; shift 2;; --chat-model) CHAT_MODEL=$2; shift 2;;
  --restore-db) DB_DUMP=$2; shift 2;; --restore-objects) OBJECTS=$2; shift 2;;
  *) echo "unknown option $1"; exit 2;; esac; done

sudo install -d -m 0755 /etc/containers/systemd /etc/nasa/kube /etc/nasa/db-init /var/lib/nasa/media /var/lib/nasa/objects
sudo install -d -m 0700 /etc/nasa/tls
sudo install -m 0644 "$HERE"/kube/*.yaml /etc/nasa/kube/
sudo install -m 0755 "$HERE"/config/db-init/*.sh /etc/nasa/db-init/
sudo install -m 0644 "$HERE"/config/tls-proxy.conf /etc/nasa/tls-proxy.conf
sudo install -m 0644 "$HERE"/quadlet/{nasa.network,nasa-data.kube,nasa-app.kube,perception.container} /etc/containers/systemd/
sudo install -m 0644 "$HERE"/quadlet/agent-dreaming.{service,timer} /etc/systemd/system/

# Secrets: generated here, stored only in Podman's secret store and root-only files. Never in the bundle.
if ! sudo podman secret exists nasa-secrets; then
  r() { openssl rand -hex 24; }
  PG=$(r) INV=$(r) S3A=$(openssl rand -hex 10) S3S=$(r) MCP=$(r)
  b() { printf %s "$1" | base64 -w0; }
  sudo tee /etc/nasa/secrets.yaml >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata: {name: nasa-secrets}
data:
  postgres-password: $(b "$PG")
  inventory-db-password: $(b "$INV")
  database-url: $(b "postgresql://inventory:$INV@inventory-db:5432/inventory")
  s3-access-key: $(b "$S3A")
  s3-secret-key: $(b "$S3S")
  mcp-token: $(b "$MCP")
YAML
  sudo chmod 600 /etc/nasa/secrets.yaml
  sudo podman kube play /etc/nasa/secrets.yaml >/dev/null
  sudo tee /etc/nasa/perception.env >/dev/null <<ENV
CAMERA_ID=cam-handheld
PERCEPTION_DEVICE=cuda
PERCEPTION_STRIDE_S=30
VIDEO_SOURCE=/media/sample.mp4
INVENTORY_API_URL=http://inventory-api:8080
OMNI_URL=$OMNI
OMNI_MODEL=gemma4
DECISION_URL=$DECISION
DECISION_MODEL=dgemma
DECISION_TIMEOUT_S=20
ASR_URL=$ASR
ASR_MODEL=asr
S3_ENDPOINT=http://rustfs:9000
S3_BUCKET=inventory
S3_ACCESS_KEY=$S3A
S3_SECRET_KEY=$S3S
ENV
  sudo chmod 600 /etc/nasa/perception.env
fi
sudo tee /etc/nasa/endpoints.yaml >/dev/null <<YAML
apiVersion: v1
kind: ConfigMap
metadata: {name: nasa-endpoints}
data: {AGENT_LLM_URL: "$CHAT", AGENT_LLM_MODEL: "$CHAT_MODEL", OMNI_URL: "$OMNI", DECISION_URL: "$DECISION", ASR_URL: "$ASR"}
YAML
[ -f /etc/nasa/tls/tls.crt ] || sudo openssl req -x509 -newkey rsa:2048 -nodes -days 180 -subj "/CN=nasa-omni-poc" \
  -keyout /etc/nasa/tls/tls.key -out /etc/nasa/tls/tls.crt 2>/dev/null

if [ -n "$OBJECTS" ] && [ -z "$(sudo ls -A /var/lib/nasa/objects)" ]; then sudo tar -xzf "$OBJECTS" -C /var/lib/nasa/objects; fi
sudo chown -R 10001:10001 /var/lib/nasa/objects     # the RustFS image runs as uid 10001
sudo /usr/libexec/podman/quadlet -dryrun >/dev/null
sudo systemctl daemon-reload
sudo systemctl start nasa-data.service
for i in $(seq 1 60); do sudo podman exec inventory-db-postgres pg_isready -U postgres -d inventory >/dev/null 2>&1 && break; sleep 2; done
if [ -n "$DB_DUMP" ]; then
  sudo podman exec -i inventory-db-postgres pg_restore -U postgres -d inventory --clean --if-exists --no-owner < "$DB_DUMP" || true
  sudo podman exec inventory-db-postgres psql -U postgres -d inventory -q -c "
DO \$\$ DECLARE r record; BEGIN
 FOR r IN SELECT format('%I.%I', schemaname, tablename) t FROM pg_tables WHERE schemaname='public' LOOP EXECUTE 'ALTER TABLE '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT format('%I.%I', sequence_schema, sequence_name) t FROM information_schema.sequences WHERE sequence_schema='public' LOOP EXECUTE 'ALTER SEQUENCE '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT format('%I.%I', schemaname, viewname) t FROM pg_views WHERE schemaname='public' LOOP EXECUTE 'ALTER VIEW '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT p.oid::regprocedure::text t FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=p.oid AND d.deptype='e') LOOP EXECUTE 'ALTER FUNCTION '||r.t||' OWNER TO inventory'; END LOOP;
END \$\$;"
fi
sudo systemctl start nasa-app.service
sudo systemctl enable --now agent-dreaming.timer >/dev/null 2>&1 || true
echo "installed: data + app tiers. Dashboard: https://$(hostname -I | awk '{print $1}')/"
