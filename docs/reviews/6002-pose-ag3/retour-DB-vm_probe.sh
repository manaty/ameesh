#!/bin/bash
# #6002 v4 — retour du rôle DB (miroir de pose-DB-vm_probe.sh), route BG → AG2 → root@10.77.0.1.
# Sessions vm_probe terminées, objets de vm_probe_writer (la seule fonction vm_probe_send) et droits
# retirés par DROP OWNED, puis les deux rôles supprimés. À blanc par défaut.
#   ./retour-DB-vm_probe.sh [--apply]
set -euo pipefail
AG2=78.232.60.1
APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
C() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG2" "runuser -u ameesh-staging -- ssh -o BatchMode=yes root@10.77.0.1 bash -s"; }
C <<'EOS'
runuser -u postgres -- psql -d ameesh -X -At -c "select 'rôles', string_agg(rolname, ',') from pg_roles where rolname in ('vm_probe','vm_probe_writer')" \
  -c "select 'fonction', count(*) from pg_proc where proname='vm_probe_send'" -c "select 'sessions', count(*) from pg_stat_activity where usename='vm_probe'"
EOS
[ $APPLY = 1 ] || { echo "(à blanc, aucun effet)"; exit 0; }
C <<'EOS'
set -euo pipefail
runuser -u postgres -- psql -d ameesh -X -q -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'vm_probe';
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname IN ('vm_probe','vm_probe_writer')) THEN
    EXECUTE 'DROP OWNED BY ' || (SELECT string_agg(quote_ident(rolname), ',') FROM pg_roles WHERE rolname IN ('vm_probe','vm_probe_writer'));
  END IF; END $$;
DROP ROLE IF EXISTS vm_probe;
DROP ROLE IF EXISTS vm_probe_writer;
COMMIT;
SQL
[ "$(runuser -u postgres -- psql -d ameesh -X -At -c "select count(*) from pg_roles where rolname in ('vm_probe','vm_probe_writer')")" = 0 ] \
  && [ "$(runuser -u postgres -- psql -d ameesh -X -At -c "select count(*) from pg_proc where proname='vm_probe_send'")" = 0 ] && echo "retour DB ok"
EOS
