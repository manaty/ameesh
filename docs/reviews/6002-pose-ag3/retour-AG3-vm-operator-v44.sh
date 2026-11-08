#!/bin/bash
# #6002 v4.4 — acl-retour-v44.sh (lectures getfacl exigées réussies). v4.3 — reçu ACL absent : STOP sauf --sans-acl prouvé (aucune entrée u:vm-operator) ; mode vérifié
#   avant restauration (acl-retour-v44.sh). v4.2 — retour arrière. ACL : contrôle de non-dérive AVANT toute mutation (acl-retour-v42.sh
#   verifier ; dérive = STOP, rien effacé), puis restauration bornée setfacl --restore depuis le reçu
#   root 0600 (avis B impl-11 #200824, impl-03 #200830 : setfacl -x laissait mask::) ; dossier
#   /etc/ssh/authorized_keys retiré s'il a été créé par la pose et qu'il est vide.
# #6002 v4 — retour arrière en miroir de la pose, depuis bureau-goal. Ordre : AG3 d'abord (plus aucun
# consommateur du rôle), puis retour-DB-vm_probe.sh (rôle retiré). À blanc par défaut.
#   ./retour-AG3-vm-operator-v4.sh [--apply] <TS de la pose AG3>
set -euo pipefail
AG3=51.15.137.245
APPLY=0 SANS_ACL=0
while [ $# -gt 1 ]; do case $1 in --apply) APPLY=1; shift;; --sans-acl) SANS_ACL=1; shift;; *) break;; esac; done
POSE=${1:?TS de la pose AG3}; OPS=/root/ameesh-operator-6002-AG3-$POSE
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" bash -s -- "$@"; }
R "$OPS" <<'EOS'
set -u; OPS=$1; echo "état avant retour :"
test -d "$OPS" && echo "  reçus $OPS" || echo "  reçus absents (retour sans reçus : ACL non restaurée)"
for f in /home/ameesh/.config/systemd/user/ameesh-vm-health.timer /var/lib/ameesh-vm-health /etc/ameesh-vm-adapter \
         /var/lib/ameesh-vm-remote /etc/sudoers.d/ameesh-vm-operator /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf \
         /etc/ssh/authorized_keys/vm-operator /opt/ameesh-vm-adapter; do [ -e "$f" ] && echo "  présent $f"; done
getent passwd vm-operator >/dev/null && echo "  compte vm-operator"; true
EOS
# Contrôle de non-dérive des ACL, AVANT toute mutation (lecture seule ; aussi à blanc).
# Contrôle exécuté depuis les octets relus de BG (entrée standard), même si les reçus sont absents.
ICI=$(cd "$(dirname "$0")" && pwd)
ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" "SANS_ACL=$SANS_ACL bash -s -- verifier $OPS vm-operator" < "$ICI/acl-retour-v44.sh" \
  || { echo "STOP : ACL en dérive, reçu ACL absent ou illisible : aucun retour lancé, rien effacé" >&2; exit 1; }
[ $APPLY = 1 ] || { echo "(à blanc, aucun effet)"; exit 0; }
R "$OPS" <<'EOS'
set -eu; OPS=$1
# 7. minuteur et unités utilisateur d'ameesh
# v4.1 (N impl-03) : minuteur ET service traités séparément (l'un peut exister sans l'autre).
U=/home/ameesh/.config/systemd/user
if [ -e $U/ameesh-vm-health.timer ]; then systemctl --user -M ameesh@ disable --now ameesh-vm-health.timer; fi
if [ -e $U/ameesh-vm-health.service ]; then systemctl --user -M ameesh@ stop ameesh-vm-health.service || true; fi
rm -f $U/ameesh-vm-health.timer $U/ameesh-vm-health.service
systemctl --user -M ameesh@ daemon-reload
systemctl --user -M ameesh@ reset-failed ameesh-vm-health.service ameesh-vm-health.timer 2>/dev/null || true
# 6 bis. ACL : restauration bornée (non-dérive revérifiée, setfacl --restore, getfacl = reçu à l'octet)
if [ -f "$OPS/acl-parents.avant" ]; then bash "$OPS/acl-retour-v44.sh" restaurer "$OPS" vm-operator; fi
if [ -f "$OPS/paquet-acl-pose" ]; then DEBIAN_FRONTEND=noninteractive apt-get remove -y -q acl >/dev/null; fi
# 6. cartes, passfile (détruit), vérificateur éventuel ; 5. sudoers ; 4. sshd + clé ; 1-3. compte et états
[ -f /var/lib/ameesh-vm-remote/vm_probe.pgpass ] && shred -u /var/lib/ameesh-vm-remote/vm_probe.pgpass
[ -f "$OPS/vm_probe.scram" ] && shred -u "$OPS/vm_probe.scram"
rm -rf /etc/ameesh-vm-adapter /var/lib/ameesh-vm-health
rm -f /etc/sudoers.d/ameesh-vm-operator; visudo -c >/dev/null
rm -f /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf /etc/ssh/authorized_keys/vm-operator; sshd -t; systemctl reload ssh
if [ -f "$OPS/authorized_keys-dir-cree" ]; then rmdir /etc/ssh/authorized_keys 2>/dev/null || echo "/etc/ssh/authorized_keys non vide : gardé"; fi
if getent passwd vm-operator >/dev/null; then pkill -u vm-operator || true; userdel vm-operator; fi
rm -rf /var/lib/ameesh-vm-remote /opt/ameesh-vm-adapter
! getent passwd vm-operator && [ ! -e /etc/ameesh-vm-adapter ] && [ ! -e /opt/ameesh-vm-adapter ] && echo "retour AG3 ok"
EOS
ssh -F none -o BatchMode=yes "root@$AG3" true && echo "root@AG3 OK après retour"
echo "Suite : retour-DB-vm_probe.sh --apply (rôle retiré)."
