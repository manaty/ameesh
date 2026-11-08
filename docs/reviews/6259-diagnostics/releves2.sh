set -eu
Q() { runuser -u postgres -- psql -d ameesh -XA -F' | ' -c "$1"; }
echo "== pg_default_acl (schémas)"; Q "select d.defaclobjtype, coalesce(d.defaclnamespace::regnamespace::text,'(base)'), d.defaclrole::regrole, case when x.grantee=0 then 'PUBLIC' else x.grantee::regrole::text end, x.privilege_type from pg_default_acl d cross join lateral aclexplode(d.defaclacl) x where d.defaclobjtype='n'"
echo "== pg_default_acl (tout)"; Q "select count(*) from pg_default_acl"
echo "== bornes"; Q "select (select stats_reset from public.pg_stat_statements_info) as stats_reset, pg_postmaster_start_time() as demarrage"
echo "== template1"; runuser -u postgres -- psql -d template1 -XAtc "select coalesce((select n.nspname from pg_extension e join pg_namespace n on n.oid=e.extnamespace where extname='pg_stat_statements'),'-')"
echo "== timers/cron/conteneurs"; systemctl list-timers --all --no-pager --plain 2>/dev/null | grep -icE "pg_stat|postgres|export|metric" ; ls /etc/cron.d 2>/dev/null; crontab -l 2>/dev/null | grep -ic pg_stat || true; docker ps --format '{{.Names}}'
echo "== unités memoria-*export* (ExecStart)"; for u in $(systemctl list-units --all --plain --no-legend 'memoria-*export*' 2>/dev/null | awk '{print $1}'); do echo "$u: $(systemctl show -p ExecStart --value $u | grep -o 'path=[^ ;]*' | head -1)"; done
