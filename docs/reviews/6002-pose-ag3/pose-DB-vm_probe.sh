#!/bin/bash
# #6002 v4 — étape 2 (rôle DB) : deploy/sql/role-vm-probe.sql à M (97285a07) sur la base centrale, puis
# audit deploy/sql/_contrat-vm-probe.sql (refuse tout écart). Route root : BG → AG2 → runuser
# ameesh-staging ssh root@10.77.0.1 (go owner 08/11). Exécution en superutilisateur postgres (peer),
# search_path du mesh = public. Aucun secret ici : seul le VÉRIFICATEUR SCRAM produit sur AG3 par
# pose-AG3-vm-operator-v4.sh (étape 6) transite (AG3 → BG → mesh-1, par tube, jamais affiché),
# puis ALTER ROLE vm_probe PASSWORD :'v' (lu par \set depuis un fichier root:postgres 0640 détruit après).
#   ./pose-DB-vm_probe.sh [--apply] <TS de la pose AG3>
set -euo pipefail
AG2=78.232.60.1 AG3=51.15.137.245; M=97285a076dee285097c412d3bd0f2649438a3257
ROLE_SHA=2a87ee04d0c4af4a; CONTRAT_SHA=4c9eecb0532b0704   # 16 premiers hexa, git show M:<fichier> | sha256sum
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
POSE=${1:?TS de la pose AG3 (dossier /root/ameesh-operator-6002-AG3-<TS>)}
C() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG2" "runuser -u ameesh-staging -- ssh -o BatchMode=yes root@10.77.0.1 $*"; }
stop() { echo "STOP: $*" >&2; exit 2; }
SRC=/home/agent/development/manaty/ameesh
for f in role-vm-probe.sql:$ROLE_SHA _contrat-vm-probe.sql:$CONTRAT_SHA; do
  [ "$(git -C "$SRC" show "$M:deploy/sql/${f%%:*}" | sha256sum | cut -c1-16)" = "${f#*:}" ] || stop "${f%%:*} ≠ M"; done
OPS=/root/ameesh-operator-6002-AG3-$POSE

# Préalables (lecture) : rôles et fonction absents, schéma public, PostgreSQL 17
C 'bash -s' <<'EOS' || stop "préalables base centrale"
set -euo pipefail
q() { runuser -u postgres -- psql -d ameesh -X -At -v ON_ERROR_STOP=1 -c "$1"; }
[ "$(q "select count(*) from pg_roles where rolname in ('vm_probe','vm_probe_writer')")" = 0 ] || { echo "rôle déjà présent"; exit 1; }
[ "$(q "select count(*) from pg_proc where proname='vm_probe_send'")" = 0 ] || { echo "fonction déjà présente"; exit 1; }
[ "$(q "show server_version_num" | cut -c1-2)" = 17 ] && [ "$(q "select count(*) from pg_namespace where nspname='public'")" = 1 ]
echo "préalables base OK"
EOS
[ $APPLY = 1 ] || { echo "(contrôle seul, aucun effet)"; exit 0; }
ssh -F none -o BatchMode=yes "root@$AG3" "test -s $OPS/vm_probe.scram && grep -q '^SCRAM-SHA-256\\\$4096:' $OPS/vm_probe.scram" || stop "vérificateur absent sur AG3 ($OPS)"
TS=$(date -u +%Y%m%dT%H%M%SZ)-$$; D=/var/tmp/ameesh-6002-vm-probe-$TS

# 1. pièces SQL de M dans un dossier root:postgres 0750 (postgres doit les lire, \ir relatif)
C "install -d -m 750 -o root -g postgres $D"
for f in role-vm-probe.sql _contrat-vm-probe.sql; do
  git -C "$SRC" show "$M:deploy/sql/$f" | C "sh -c 'cat > $D/$f && chown root:postgres $D/$f && chmod 640 $D/$f && sha256sum $D/$f'"
done
# 2. installation transactionnelle (inclut l'audit du contrat) ; ON_ERROR_STOP
out=$(C "runuser -u postgres -- env PGOPTIONS='-c search_path=public' psql -d ameesh -X -v ON_ERROR_STOP=1 -f $D/role-vm-probe.sql" 2>&1) || { echo "$out"; stop "installation refusée (transaction annulée)"; }
echo "$out"; printf '%s\n' "$out" | grep -q 'vm_probe contract held' || stop "contrat non confirmé"
# 3. secret : vérificateur SCRAM d'AG3 → fichier 0640 root:postgres → ALTER ROLE ; fichier détruit
ssh -F none -o BatchMode=yes "root@$AG3" "cat $OPS/vm_probe.scram" | C "sh -c 'umask 077; cat > $D/v && chown root:postgres $D/v && chmod 640 $D/v'"
C 'bash -s' "$D" <<'EOS'
set -euo pipefail; D=$1
grep -qc '^SCRAM-SHA-256\$4096:' "$D/v" || { shred -u "$D/v"; echo "vérificateur mal formé"; exit 1; }
runuser -u postgres -- psql -d ameesh -X -q -v ON_ERROR_STOP=1 <<SQL
\set v \`cat $D/v\`
SET password_encryption = 'scram-sha-256';
ALTER ROLE vm_probe PASSWORD :'v';
SQL
shred -u "$D/v"; echo "mot de passe posé (vérificateur), fichier détruit"
EOS
ssh -F none -o BatchMode=yes "root@$AG3" "shred -u $OPS/vm_probe.scram && echo 'vérificateur détruit sur AG3'"
# 4. audit seul du contrat, mot de passe compris (SCRAM dédié), puis ménage
out=$(C "runuser -u postgres -- env PGOPTIONS='-c search_path=public' psql -d ameesh -X -v ON_ERROR_STOP=1 -f $D/_contrat-vm-probe.sql" 2>&1) || { echo "$out"; stop "contrat refusé après mot de passe : retour-DB-vm_probe.sh"; }
echo "$out"; printf '%s\n' "$out" | grep -q 'vm_probe contract held' || stop "contrat non confirmé après mot de passe"
C "runuser -u postgres -- psql -d ameesh -X -At -c \"select rolname, rolcanlogin, left(rolpassword,14) from pg_authid where rolname in ('vm_probe','vm_probe_writer') order by 1\"; rm -f $D/role-vm-probe.sql $D/_contrat-vm-probe.sql; rmdir $D"
echo "rôle vm_probe posé et audité ($TS). Suite : tests-AG3-vm-operator-v4.sh B."
