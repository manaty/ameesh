#!/bin/bash
# #6002 v4.1 — preuves après pose (delta v4 : ACL, nouveau venv-<sha>, carte producteur, 75 consigné).
# #6002 v4 — preuves après pose. (v4.1 : set -u respecté, reçu illisible = échec.) Lecture seule : aucune écriture tentée en base.
#   A : après pose AG3, avant le rôle DB → commandes DB refusées (rc 1), true admis.
#   B : après pose-DB-vm_probe.sh → santé #6296 réelle (version-mode, doctor rc 0, reçu frais),
#       lectures natives réelles sous vm_probe (quiesce, runners : rc 0 ou 75, jamais 1),
#       refus de lecture hors contrat (toute autre table, SELECT *, schéma diagnostics).
# Chaque attendu est asserté ; échec = rc 1.
set -u
PHASE=${1:?usage: tests-AG3-vm-operator-v4.sh A|B}; case $PHASE in A|B) ;; *) echo "phase A ou B" >&2; exit 2;; esac
AG3=51.15.137.245; P=/opt/ameesh-vm-adapter/remote
O=(-F none -o BatchMode=yes -o IdentitiesOnly=yes -o "IdentityFile=$HOME/.ssh/id_ed25519" -o StrictHostKeyChecking=yes -o ConnectTimeout=8)
ECHEC=0
attendu() { local voulu=$1; shift; local rc=0; ssh "${O[@]}" "vm-operator@$AG3" "$@" >/dev/null 2>&1 || rc=$?
  local ok=0; case $voulu in
    admis) [ $rc -eq 0 ] && ok=1;;
    refuse) [ $rc -ne 0 ] && [ $rc -ne 255 ] && ok=1;;
    erreur) [ $rc -eq 1 ] && ok=1;;
    lu) { [ $rc -eq 0 ] || [ $rc -eq 75 ]; } && ok=1;;   # lecture DB réussie : prêt (0) ou pas encore (75)
    attente) [ $rc -eq 75 ] && ok=1;;
  esac
  if [ $ok = 1 ]; then echo "OK   $voulu rc=$rc : $*"; else echo "ECHEC attendu $voulu, rc=$rc : $*"; ECHEC=1; fi; }
echo "tests #6002 v4 phase $PHASE $(date -u +%FT%TZ)"
attendu admis /usr/bin/true
attendu admis /usr/bin/journalctl -b -1 -n 1 -o cat --no-pager
if [ "$PHASE" = A ]; then
  attendu erreur $P/quiesce
else
  attendu admis $P/version-mode
  attendu admis $P/doctor
  # v4.1 (impl-03 B2 #200802, code de M inchangé) : Native.control refuse (rc 1) toute porte qui n'est pas
  # une vague fermée draining/returning, et AG3 n'a aujourd'hui AUCUNE ligne host_lifecycle. quiesce et
  # runners rendent donc 1 ; on l'atteste avec l'absence de ligne lue sous vm_probe (plus bas). La preuve
  # d'un 0/75 viendra de la première vague (O5, consignée au lot).
  attendu erreur $P/quiesce
  attendu erreur $P/runners
  # probe-receipt d'un nonce inconnu : connexion vm_probe et cartes lues, puis attente (75) sans porte.
  attendu attente $P/probe-receipt 0123456789abcdef0123456789abcdef
fi
attendu refuse id
attendu refuse /bin/sh -c id
attendu refuse /usr/bin/sudo -n /usr/bin/systemctl status ssh
attendu refuse $P/version-mode extra
out=$(ssh "${O[@]}" -tt "vm-operator@$AG3" /usr/bin/true 2>&1); echo "$out" | grep -qi "PTY allocation request failed" && echo "OK   PTY refusé" || { echo "ECHEC PTY : $out"; ECHEC=1; }
ssh -F none -o BatchMode=yes "root@$AG3" true && echo "OK   root@AG3" || { echo "ECHEC root@AG3"; ECHEC=1; }
if [ "$PHASE" = B ]; then
  # O1 (#200777) : ACL de traversée seule ; liste refusée ; python de venv-courant exécutable ; un
  # nouveau venv-<sha> en 755 créé comme par le staging (umask 022, sous ameesh) reste lisible sans
  # autre geste. Dossier d'essai retiré aussitôt.
  r=$(ssh -F none -o BatchMode=yes "root@$AG3" 'set -u
    for d in /home/ameesh /home/ameesh/.local /home/ameesh/.local/share /home/ameesh/.local/share/ameesh; do
      getfacl -p "$d" | grep -qx "user:vm-operator:--x" || echo "KO acl $d"; getfacl -p "$d" | grep -q "^default:" && echo "KO defaut $d"; done
    runuser -u vm-operator -- ls /home/ameesh >/dev/null 2>&1 && echo "KO liste"
    runuser -u vm-operator -- /home/ameesh/.local/share/ameesh/venv-courant/bin/python -I -c "pass" || echo "KO python"
    t=/home/ameesh/.local/share/ameesh/venv-essai6002-$$
    runuser -u ameesh -- sh -c "umask 022; mkdir -p $t/bin && cp /home/ameesh/.local/share/ameesh/venv-courant/pyvenv.cfg $t/ && chmod 755 $t $t/bin"
    runuser -u vm-operator -- test -r $t/pyvenv.cfg || echo "KO nouveau venv illisible"
    runuser -u vm-operator -- test -x $t/bin || echo "KO nouveau venv non traversable"
    rm -rf "$t"; echo fin')
  [ "$r" = fin ] && echo "OK   ACL --x seule, liste refusée, python de venv-courant exécuté, nouveau venv-<sha> 755 lisible" || { echo "ECHEC ACL/venv : $r"; ECHEC=1; }
  # O4 : carte producteur = unités runners actives (sinon reçu rouge : « carte producteur à mettre à jour, geste root »).
  "$(dirname "$0")/verifier-carte-producteur-AG3.sh" || ECHEC=1
  # Reçu de santé : frais (< 3 périodes), ameesh:vm-operator 0640, répertoire 2750.
  r=$(ssh -F none -o BatchMode=yes "root@$AG3" "stat -c '%a %U:%G %Y' /var/lib/ameesh-vm-health/health.json; stat -c '%a %U:%G' /var/lib/ameesh-vm-health; date +%s")
  set -- $r
  [ $# -eq 6 ] || { echo "ECHEC reçu de santé illisible : « $r »"; ECHEC=1; set -- x x x x x 0; }
  { [ "$1 $2" = "640 ameesh:vm-operator" ] && [ "$4 $5" = "2750 ameesh:vm-operator" ] && [ $(( $6 - $3 )) -lt 180 ]; } \
    && echo "OK   reçu de santé frais ($(( $6 - $3 )) s), droits conformes" || { echo "ECHEC reçu de santé : $r"; ECHEC=1; }
  # Lectures directes sous vm_probe, depuis AG3, en vm-operator, avec son passfile.
  res=$(ssh -F none -o BatchMode=yes "root@$AG3" "runuser -u vm-operator -- psql 'host=10.77.0.1 port=5432 dbname=ameesh user=vm_probe sslmode=require passfile=/var/lib/ameesh-vm-remote/vm_probe.pgpass' -X -At -v ON_ERROR_STOP=1 -f -" <<'SQL' 2>&1
SET default_transaction_read_only = on;
SELECT 'qui|' || current_user || '|' || (SELECT count(*) FROM (SELECT host, mode, claims_open FROM public.host_lifecycle) t);
SELECT 'registre|' || count(*) FROM (SELECT name, host, mode, status FROM public.agent_registry) t;
SELECT 'porte_ag3|' || count(*) FROM public.host_lifecycle WHERE host = 'ameesh-agents-3';
DO $$
DECLARE k record; ok text[] := '{}'; etoile text[] := '{}';
BEGIN
  FOR k IN SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind IN ('r','v','m','p','f') AND n.nspname NOT IN ('pg_catalog','information_schema')
             AND n.nspname !~ '^pg_' ORDER BY 1, 2 LOOP
    BEGIN EXECUTE format('SELECT 1 FROM %I.%I LIMIT 0', k.nspname, k.relname); ok := ok || (k.nspname || '.' || k.relname);
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN EXECUTE format('SELECT * FROM %I.%I LIMIT 0', k.nspname, k.relname); etoile := etoile || (k.nspname || '.' || k.relname);
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  END LOOP;
  RAISE NOTICE 'lisibles=%', array_to_string(ok, ',');
  RAISE NOTICE 'etoile=%', array_to_string(etoile, ',');
END $$;
SQL
)
  echo "$res" | sed 's/^/     /'
  attendu_lisibles="public.agent_mailbox,public.agent_registry,public.host_handoffs,public.host_lifecycle,public.host_lifecycle_agents,public.turn_resources,public.vm_adapter_requests"
  echo "$res" | grep -q '^porte_ag3|0$' && echo "OK   aucune ligne host_lifecycle pour AG3 (explique quiesce/runners rc 1)" || echo "INFO porte AG3 : $(echo "$res" | grep '^porte_ag3|') — revoir les attendus quiesce/runners"
  echo "$res" | grep -q '^qui|vm_probe|' && echo "OK   connexion vm_probe et lecture host_lifecycle" || { echo "ECHEC connexion/lecture vm_probe"; ECHEC=1; }
  echo "$res" | grep -q "lisibles=$attendu_lisibles\$" && echo "OK   seules les 7 relations du contrat sont lisibles" || { echo "ECHEC relations lisibles hors contrat"; ECHEC=1; }
  echo "$res" | grep -q 'etoile=$' && echo "OK   SELECT * refusé partout (colonnes hors contrat)" || { echo "ECHEC SELECT * admis quelque part"; ECHEC=1; }
fi
exit $ECHEC
