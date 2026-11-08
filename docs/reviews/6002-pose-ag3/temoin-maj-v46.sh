#!/usr/bin/env bash
# #6002 v4.6 — témoin qui EXERCE la mise à jour M → M' et son retour en conteneur jetable (image du
# constructeur, python3.12 = AG3) : l'état « pose v4.5 » est construit par le bloc de l'étape 2 extrait à
# l'octet de pose-AG3-vm-operator-v45.sh, puis maj-adaptateur-distant-v46.sh (octets du dossier) est lancé
# en verifier / appliquer / retour. systemctl est simulé (journal des appels) ; aucun réseau vers une VM.
set -u
ICI=$(cd "$(dirname "$0")" && pwd); W=$(mktemp -d)
trap 'docker run --rm -v "$W":/w ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 rm -rf /w/x >/dev/null 2>&1; rm -rf "$W"' EXIT
M=97285a076dee285097c412d3bd0f2649438a3257; N=242ced700d6ec1a63fc9d72643bc9faad175f72f; S=/home/agent/development/manaty/ameesh
mkdir -p "$W/x"; git -C $S archive --format=tar --prefix="src-${M:0:12}/" $M > "$W/x/srcM.tar"; git -C $S archive --format=tar --prefix="src-${N:0:12}/" $N > "$W/x/srcN.tar"
git -C $S show $M:deploy/vm-adapter/remote/checks/ameesh-vm-health.service > "$W/x/unitM"
awk '/^R "\$M" "\$OPS" <<.EOS.$/{k++; if(k==1){on=1; next}} on&&/^EOS$/{on=0} on' "$ICI/pose-AG3-vm-operator-v45.sh" > "$W/x/etape2.sh"
cp "$ICI/maj-adaptateur-distant-v46.sh" "$W/x/"
docker run --rm --user 0 -v "$W/x":/x:ro ameesh-l2-builder:p6-97285a0 bash -c '
set -u; ok() { echo "OK   $*"; }; ko() { echo "ECHEC $*"; E=1; }; E=0
useradd -m -d /home/ameesh ameesh; mkdir -p /usr/local/fbin; printf "#!/bin/sh\necho \"systemctl \$*\" >> /tmp/systemctl.log\n" > /usr/local/fbin/systemctl; chmod 755 /usr/local/fbin/systemctl; export PATH=/usr/local/fbin:$PATH
mkdir /ops && cp /x/srcM.tar /ops/src.tar && bash /x/etape2.sh '$M' /ops >/dev/null || { echo "ECHEC état v4.5"; exit 1; }
U=/home/ameesh/.config/systemd/user; mkdir -p $U /etc/ameesh-vm-adapter; install -m 644 -o ameesh -g ameesh /x/unitM $U/ameesh-vm-health.service
printf "{\"checks\":{\"doctor\":[\"/usr/bin/python3.12\",\"-I\",\"/opt/ameesh-vm-adapter/checks/health.py\",\"doctor\"]}}\n" > /etc/ameesh-vm-adapter/remote.json
D=/x/maj-adaptateur-distant-v46.sh
bash $D verifier '$M' '$N' /nonexistent >/dev/null && ok "verifier : pose v4.5 reconnue à M" || ko "verifier"
mkdir -m 700 /maj && cp /x/srcN.tar /maj/src.tar
bash $D appliquer '$M' '$N' /maj > /tmp/appl.out 2>&1 && ok "appliquer : rc 0" || { ko "appliquer"; cat /tmp/appl.out; }
[ "$(readlink /opt/ameesh-vm-adapter/venv)" = venv-'${N:0:12}' ] && ok "lien venv → venv-'${N:0:12}'" || ko "lien"
grep -q vm_probe_receipt /opt/ameesh-vm-adapter/remote/vm-remote.py && ! grep -q "FROM agent_mailbox" /opt/ameesh-vm-adapter/remote/vm-remote.py && ok "vm-remote.py de M'"'"' (vm_probe_receipt, plus d accès direct à agent_mailbox)" || ko "vm-remote.py"
grep -q "^StandardError=journal" $U/ameesh-vm-health.service && [ "$(stat -c %U $U/ameesh-vm-health.service)" = ameesh ] && ok "unité de santé de M'"'"' (StandardError=journal), ameesh" || ko "unité"
grep -q "daemon-reload" /tmp/systemctl.log && ok "daemon-reload du gestionnaire ameesh demandé" || ko "daemon-reload"
[ -f /maj/recu-avant.txt ] && [ -f /maj/recu-apres.txt ] && ok "reçus avant/après écrits" || ko "reçus"
bash $D retour '$M' '$N' /maj > /tmp/ret.out 2>&1 && [ "$(readlink /opt/ameesh-vm-adapter/venv)" = venv-'${M:0:12}' ] && ok "retour : lien et fichiers de M remis, M'"'"' conservé ($(ls -d /opt/ameesh-vm-adapter/src-* | wc -l) src)" || { ko "retour"; cat /tmp/ret.out; }
printf "{\"x\":\"/opt/ameesh-vm-adapter/src-'${M:0:12}'/y\"}\n" > /etc/ameesh-vm-adapter/remote.json
bash $D verifier '$M' '$N' /nonexistent >/dev/null 2>&1 && ko "carte avec src-<sha> acceptée" || ok "carte qui nommerait un src-<sha> : STOP"
exit $E'
