#!/usr/bin/env bash
# #6002 v4.7 — témoin qui EXERCE la vraie chaîne C de pose-DB (saut AG2 → runuser ameesh-staging → ssh
# mesh-1) avec un ssh simulé qui, comme sshd, joint ses arguments et les fait interpréter par bash -c, et un
# psql RÉEL (conteneur postgres:17, schéma minimal du mesh). Lance tels quels :
#   pose-DB-vm_probe-v46.sh --apply  → doit reproduire « invalid command-line argument … -c » ;
#   pose-DB-vm_probe-v47.sh --apply  → rôle posé, contrat tenu deux fois, résidu exact retiré.
set -u
ICI=$(cd "$(dirname "$0")" && pwd); W=$(mktemp -d); NOM=temoin-6002-v47-$$
trap 'docker rm -f $NOM >/dev/null 2>&1; rm -rf "$W"' EXIT
POSE=20261108T141112Z-2455349
python3 -I "$ICI/secret-vm-probe-v41.py" "$W/pgpass" "$W/scram" "$(id -u)" "$(id -g)" >/dev/null
cat > "$W/ssh" <<'FAUX'
#!/bin/bash
# ssh simulé : options ignorées, hôte = premier argument contenant « @ », la suite jointe par des espaces
# puis interprétée par bash -c (comportement de sshd avec un shell de connexion bash). Vers root@ depuis un
# compte non root (saut ameesh-staging → root@mesh-1), passage en root par sudo, entrée standard conservée.
while [ $# -gt 0 ]; do case $1 in -F|-o) shift 2;; -*) shift;; *@*) shift; break;; *) break;; esac; done
if [ "$(id -u)" != 0 ]; then exec sudo -n bash -c "$*"; else exec bash -c "$*"; fi
FAUX
chmod 755 "$W/ssh"
docker run -d --name $NOM -e POSTGRES_PASSWORD=x -v /home/agent/development/manaty/ameesh:/home/agent/development/manaty/ameesh:ro \
  -v "$ICI":/t:ro -v "$W":/w:ro postgres:17 >/dev/null
for i in $(seq 1 60); do docker exec $NOM pg_isready -q 2>/dev/null && break; sleep 1; done
docker exec -i $NOM bash -c '
set -u; E=0; ok() { echo "OK   $*"; }; ko() { echo "ECHEC $*"; E=1; }
apt-get update -qq >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends git sudo >/dev/null 2>&1 || { echo "git/sudo indisponibles"; exit 3; }
git config --global safe.directory "*"
useradd -m ameesh-staging; echo "ameesh-staging ALL=(root) NOPASSWD: ALL" > /etc/sudoers.d/temoin; mkdir -p /usr/local/faux && cp /w/ssh /usr/local/faux/ssh; export PATH=/usr/local/faux:$PATH
runuser -u postgres -- psql -X -q -c "create database ameesh" && runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -f /t/schema-minimal-v47.sql
OPS=/root/ameesh-operator-6002-AG3-'$POSE'; install -d -m 700 $OPS; install -m 600 /w/scram $OPS/vm_probe.scram
# Résidu exact de la tentative réelle du 17:19Z, reproduit tel quel (2 .sql publics, root:postgres)
R=/var/tmp/ameesh-6002-vm-probe-20261108T171938Z-2469696; install -d -m 750 -o root -g postgres $R
for f in role-vm-probe.sql _contrat-vm-probe.sql; do echo "-- public" > $R/$f; chown root:postgres $R/$f; chmod 640 $R/$f; done
cd /t
bash pose-DB-vm_probe-v46.sh --apply '$POSE' > /tmp/v46.out 2>&1; rc=$?
{ [ $rc != 0 ] && grep -q "invalid command-line argument for server process: -c" /tmp/v46.out; } && ok "v4.6 : défaut de quoting reproduit (rc $rc, « invalid command-line argument … -c »)" || { ko "v4.6 non reproduit (rc $rc)"; tail -5 /tmp/v46.out; }
[ "$(runuser -u postgres -- psql -d ameesh -XAtc "select count(*) from pg_roles where rolname like '"'"'vm_probe%'"'"'")" = 0 ] && ok "v4.6 : base intacte (aucun rôle vm_probe*)" || ko "v4.6 : rôle créé"
[ -f $OPS/vm_probe.scram ] && ok "v4.6 : vérificateur AG3 conservé après l échec (trap v4.6)" || ko "vérificateur détruit"
bash pose-DB-vm_probe-v47.sh --apply '$POSE' > /tmp/v47.out 2>&1; rc=$?
[ $rc = 0 ] && ok "v4.7 : pose-DB rc 0" || { ko "v4.7 rc $rc"; tail -15 /tmp/v47.out; }
grep -q "résidu de la tentative 20261108T171938Z retiré" /tmp/v47.out && [ ! -e $R ] && ok "v4.7 : résidu exact retiré" || ko "résidu"
[ "$(grep -c "vm_probe contract held" /tmp/v47.out)" -ge 2 ] && ok "v4.7 : contrat tenu à l installation ET à l audit après mot de passe" || ko "contrat"
[ "$(runuser -u postgres -- psql -d ameesh -XAtc "select left(rolpassword,14) from pg_authid where rolname='"'"'vm_probe'"'"'")" = "SCRAM-SHA-256\$" ] && ok "v4.7 : vm_probe en SCRAM (vérificateur réutilisé)" || ko "mot de passe"
[ ! -e $OPS/vm_probe.scram ] && ok "v4.7 : vérificateur AG3 détruit après ALTER ROLE réussi" || ko "vérificateur AG3 restant"
[ -z "$(ls -d /var/tmp/ameesh-6002-vm-probe-* 2>/dev/null | grep -v 20261108T171938Z | xargs -r -n1 sh -c "ls \$0/v 2>/dev/null")" ] && ok "aucune copie du vérificateur sur « mesh-1 »" || ko "copie résiduelle"
exit $E'
