#!/usr/bin/env bash
# Save or restore the inventory DB (custom-format pg_dump), e.g. to keep a demo baseline between experiments.
#   db-snapshot.sh save <file.dump>      |      db-snapshot.sh restore <file.dump>   (restore replaces all data)
set -euo pipefail
NS=nasa-data; P="$(oc get pod -n $NS -l cnpg.io/cluster=inventory-db,cnpg.io/instanceRole=primary -o name | head -1)"
case "${1:-}" in
  save)    oc exec -n $NS "$P" -c postgres -- pg_dump -U postgres -d inventory -Fc > "${2:?file}";;
  restore) oc exec -i -n $NS "$P" -c postgres -- pg_restore -U postgres -d inventory --clean --if-exists --no-owner < "${2:?file}"
           # --no-owner leaves the restored objects owned by postgres; the API connects as the 'inventory' role.
           oc exec -n $NS "$P" -c postgres -- psql -U postgres -d inventory -q -v ON_ERROR_STOP=1 -c "
DO \$\$ DECLARE r record; BEGIN
 FOR r IN SELECT format('%I.%I', schemaname, tablename) t FROM pg_tables WHERE schemaname='public' LOOP EXECUTE 'ALTER TABLE '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT format('%I.%I', sequence_schema, sequence_name) t FROM information_schema.sequences WHERE sequence_schema='public' LOOP EXECUTE 'ALTER SEQUENCE '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT format('%I.%I', schemaname, viewname) t FROM pg_views WHERE schemaname='public' LOOP EXECUTE 'ALTER VIEW '||r.t||' OWNER TO inventory'; END LOOP;
 FOR r IN SELECT p.oid::regprocedure::text t FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=p.oid AND d.deptype='e') LOOP EXECUTE 'ALTER FUNCTION '||r.t||' OWNER TO inventory'; END LOOP;
END \$\$;";;
  *) echo "usage: $0 save|restore <file.dump>"; exit 2;;
esac
