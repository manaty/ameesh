echo "== timers liés"; systemctl list-timers --all --no-pager --plain 2>/dev/null | grep -iE "pg_stat|postgres|export|metric" || echo "aucun"
echo "== cron"; ls /etc/cron.d 2>/dev/null; grep -rils pg_stat_statements /etc/cron* /var/spool/cron 2>/dev/null || echo "aucune mention dans cron"
echo "== conteneurs"; docker ps --format '{{.Names}} {{.Image}}'
echo "== unités memoria-*export*"; for u in $(systemctl list-units --all --plain --no-legend 'memoria-*export*' 2>/dev/null | awk '{print $1}'); do echo "$u: $(systemctl show -p ExecStart --value $u | grep -o 'path=[^ ;]*' | head -1)"; done
echo "== mention pg_stat_statements dans les scripts memoria"; grep -rlis pg_stat_statements /opt /usr/local/bin /usr/local/sbin /srv 2>/dev/null | head || true; echo "(fin)"
