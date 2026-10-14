#!/usr/bin/env bash
# #759 — tests ciblés de l'auteur sur Y3 = 494e7916007ded9be406af65c54728967e684af7
# (refusion contrôlée de origin/main 0532faed dans la tête b35a3af1), pilote psql.
set -uo pipefail
cd /home/ameesh/work/ds2-759-refusion

HEAD=$(git rev-parse HEAD)
[ "$HEAD" = "494e7916007ded9be406af65c54728967e684af7" ] || { echo "MAUVAISE TETE: $HEAD"; exit 2; }

CFG=$(mktemp "${TMPDIR:-/tmp}/ameesh-config-vide-XXXXXX")
printf '{}\n' > "$CFG"
export AMEESH_CONFIG="$CFG"
export AMEESH_TEST_DSN="postgresql://agent_mesh@127.0.0.1:55432/agent_mesh"
export AMEESH_REQUIRE_DB=1
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export AMEESH_BALANCE_INTERVAL=0
unset DEEPSEEK_API_KEY AMEESH_DEEPSEEK_API_BASE

echo "tete=$HEAD"
echo "debut=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
AMEESH_DRIVER=psql python3 -m unittest discover -s tests -t . -v \
  -k test_plan \
  -k test_l126_issues \
  -k test_merged_work_item_tracking \
  -k test_work \
  -k test_l63_platform \
  -k test_1060_alert_types
rc=$?
echo "fin=$(date -u +%Y-%m-%dT%H:%M:%SZ) rc=$rc"
exit $rc
