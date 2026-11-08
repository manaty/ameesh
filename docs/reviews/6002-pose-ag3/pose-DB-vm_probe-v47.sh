#!/bin/bash
# #6002 v4.7 — transport C robuste : la commande destinée à mesh-1 voyage en base64 (alphabet sans
#   métacaractère) et n'est interprétée qu'une fois, par bash -c sur mesh-1 ; v4.6 perdait les quotes au saut
#   AG2 (PGOPTIONS='-c search_path=public' → « invalid command-line argument : -c », STOP du 17:19Z). PGOPTIONS
#   écrit sans espace (-csearch_path=public). Résidu exact de la tentative 20261108T171938Z retiré au départ.
# #6002 v4.6 — rôle pris à M' = 242ced700d6e (#6329 : vm_probe_receipt, plus aucun SELECT d'agent_mailbox
#   pour vm_probe) ; vérificateur SCRAM RÉUTILISÉ depuis les reçus de la pose AG3 (non régénéré) et gardé
#   sur AG3 tant que ALTER ROLE n'a pas réussi (une relance reste possible) ; audit seul relancé une fois en
#   cas de refus (course connue #6364). v4.2 — trap posé AVANT l'installation du rôle (N impl-03 #200830). v4.1 — (traps : aucun vérificateur résiduel sur mesh-1 ni AG3 en cas d'échec) ; v4 — étape 2 (rôle DB) : deploy/sql/role-vm-probe.sql à M (97285a07) sur la base centrale, puis
# audit deploy/sql/_contrat-vm-probe.sql (refuse tout écart). Route root : BG → AG2 → runuser
# ameesh-staging ssh root@10.77.0.1 (go owner 08/11). Exécution en superutilisateur postgres (peer),
# search_path du mesh = public. Aucun secret ici : seul le VÉRIFICATEUR SCRAM produit sur AG3 par
# pose-AG3-vm-operator-v4.sh (étape 6) transite (AG3 → BG → mesh-1, par tube, jamais affiché),
# puis ALTER ROLE vm_probe PASSWORD :'v' (lu par \set depuis un fichier root:postgres 0640 détruit après).
#   ./pose-DB-vm_probe.sh [--apply] <TS de la pose AG3>
set -euo pipefail
AG2=78.232.60.1 AG3=51.15.137.245; M=242ced700d6ec1a63fc9d72643bc9faad175f72f
ROLE_SHA=98cac000079d8949; CONTRAT_SHA=cf371e77ddc207c7   # 16 premiers hexa, git show M':<fichier> | sha256sum (#201411)
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
POSE=${1:?TS de la pose AG3 (dossier /root/ameesh-operator-6002-AG3-<TS>)}
# v4.7 : « $* » est encodé en base64 ; le saut AG2 (bash) et le saut ssh ne voient qu'un mot sans quote ni
# espace ; seul bash -c sur mesh-1 l'interprète, tel qu'écrit ici. L'entrée standard (données, scripts)
# traverse inchangée.
C() { local b; b=$(printf '%s' "$*" | base64 -w0)
  ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG2" \
    "runuser -u ameesh-staging -- ssh -o BatchMode=yes root@10.77.0.1 'bash -c \"\$(echo $b | base64 -d)\"'"; }
RESIDU=/var/tmp/ameesh-6002-vm-probe-20261108T171938Z-2469696   # tentative v4.6 du 08/11 17:19Z : 2 .sql publics
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
# v4.7 : résidu exact de la tentative v4.6 retiré s'il est conforme (root:postgres, seulement les 2 .sql publics) ;
# tout autre contenu → STOP sans rien toucher.
C "if [ -e $RESIDU ]; then [ \"\$(stat -c %U:%G $RESIDU)\" = root:postgres ] && [ \"\$(ls -A $RESIDU | LC_ALL=C sort | tr '\\n' ' ')\" = '_contrat-vm-probe.sql role-vm-probe.sql ' ] && rm -f $RESIDU/role-vm-probe.sql $RESIDU/_contrat-vm-probe.sql && rmdir $RESIDU && echo 'résidu de la tentative 20261108T171938Z retiré'; else echo 'pas de résidu'; fi" \
  || stop "résidu $RESIDU non conforme : examiner sans rien effacer"
TS=$(date -u +%Y%m%dT%H%M%SZ)-$$; D=/var/tmp/ameesh-6002-vm-probe-$TS
# v4.2 : dès ici, toute sortie (échec compris) détruit le vérificateur sur mesh-1 et sur AG3.
# v4.6 : à toute sortie, la copie de mesh-1 est détruite. Celle d'AG3 (root 0600 dans les reçus de la pose)
# n'est détruite qu'APRÈS un ALTER ROLE réussi : sinon le passfile vm-operator ne correspondrait plus à aucun
# vérificateur et la relance exigerait un nouveau secret (retour AG3 puis repose).
# v4.7 (#201452) : le trap nettoie TOUT le dossier de cette exécution sur mesh-1 (vérificateur détruit, .sql
# publics retirés, dossier retiré), succès comme échec.
trap 'C "if [ -d $D ]; then shred -u $D/v 2>/dev/null; rm -f $D/role-vm-probe.sql $D/_contrat-vm-probe.sql; rmdir $D; fi; true" || true' EXIT

# 1. pièces SQL de M dans un dossier root:postgres 0750 (postgres doit les lire, \ir relatif)
C "install -d -m 750 -o root -g postgres $D"
for f in role-vm-probe.sql _contrat-vm-probe.sql; do
  git -C "$SRC" show "$M:deploy/sql/$f" | C "sh -c 'cat > $D/$f && chown root:postgres $D/$f && chmod 640 $D/$f && sha256sum $D/$f'"
done
# 2. installation transactionnelle (inclut l'audit du contrat) ; ON_ERROR_STOP
out=$(C "runuser -u postgres -- env PGOPTIONS=-csearch_path=public psql -d ameesh -X -v ON_ERROR_STOP=1 -f $D/role-vm-probe.sql" 2>&1) || { echo "$out"; stop "installation refusée (transaction annulée)"; }
echo "$out"; printf '%s\n' "$out" | grep -q 'vm_probe contract held' || stop "contrat non confirmé"
# 3. secret : vérificateur SCRAM d'AG3 → fichier 0640 root:postgres → ALTER ROLE ; fichier détruit
ssh -F none -o BatchMode=yes "root@$AG3" "cat $OPS/vm_probe.scram" | C "sh -c 'umask 077; cat > $D/v && chown root:postgres $D/v && chmod 640 $D/v'"
C 'bash -s' "$D" <<'EOS'
set -euo pipefail; D=$1
trap 'shred -u "$D/v" 2>/dev/null || true' EXIT
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
audit() { C "runuser -u postgres -- env PGOPTIONS=-csearch_path=public psql -d ameesh -X -v ON_ERROR_STOP=1 -f $D/_contrat-vm-probe.sql" 2>&1; }
# Course connue du contrat (#6364, DROP concurrent d'une fonction definer) : l'audit SEUL est relancé une fois.
if ! out=$(audit) || ! printf '%s\n' "$out" | grep -q 'vm_probe contract held'; then
  echo "$out"; echo "audit refusé : relance unique de l'audit seul (course #6364 possible)"; sleep 5
  out=$(audit) || { echo "$out"; stop "contrat refusé deux fois après mot de passe : retour-DB-vm_probe.sh"; }
fi
echo "$out"; printf '%s\n' "$out" | grep -q 'vm_probe contract held' || stop "contrat non confirmé après mot de passe"
C "runuser -u postgres -- psql -d ameesh -X -At -c \"select rolname, rolcanlogin, left(rolpassword,14) from pg_authid where rolname in ('vm_probe','vm_probe_writer') order by 1\"; rm -f $D/role-vm-probe.sql $D/_contrat-vm-probe.sql; rmdir $D"
echo "rôle vm_probe posé et audité ($TS). Suite : tests-AG3-vm-operator-v46.sh B."
