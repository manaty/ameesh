#!/bin/bash
# #6002 v4.1 — retour arrière (ACL : retire EXACTEMENT u:vm-operator sur les 4 parents posés)
# #6002 v4 — retour arrière en miroir de la pose, depuis bureau-goal. Ordre : AG3 d'abord (plus aucun
# consommateur du rôle), puis retour-DB-vm_probe.sh (rôle retiré). À blanc par défaut.
#   ./retour-AG3-vm-operator-v4.sh [--apply] <TS de la pose AG3>
set -euo pipefail
AG3=51.15.137.245
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
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
# 6 bis. ACL de traversée : exactement u:vm-operator sur les 4 parents, puis contrôle = état avant
P="/home/ameesh /home/ameesh/.local /home/ameesh/.local/share /home/ameesh/.local/share/ameesh"
if command -v setfacl >/dev/null; then
  for d in $P; do if getfacl -p "$d" | grep -q '^user:vm-operator:'; then setfacl -x u:vm-operator "$d"; fi; done
  if [ -f "$OPS/acl-parents.avant" ]; then getfacl -p $P | cmp -s - "$OPS/acl-parents.avant" || { echo "ACL ≠ état avant pose"; exit 1; }; fi
  if [ -f "$OPS/paquet-acl-pose" ]; then DEBIAN_FRONTEND=noninteractive apt-get remove -y -q acl >/dev/null; fi
fi
[ ! -f "$OPS/acl-parents.mode.avant" ] || stat -c '%a %U:%G %n' $P | cmp -s - "$OPS/acl-parents.mode.avant" || { echo "modes des parents ≠ avant"; exit 1; }
# 6. cartes, passfile (détruit), vérificateur éventuel ; 5. sudoers ; 4. sshd + clé ; 1-3. compte et états
[ -f /var/lib/ameesh-vm-remote/vm_probe.pgpass ] && shred -u /var/lib/ameesh-vm-remote/vm_probe.pgpass
[ -f "$OPS/vm_probe.scram" ] && shred -u "$OPS/vm_probe.scram"
rm -rf /etc/ameesh-vm-adapter /var/lib/ameesh-vm-health
rm -f /etc/sudoers.d/ameesh-vm-operator; visudo -c >/dev/null
rm -f /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf /etc/ssh/authorized_keys/vm-operator; sshd -t; systemctl reload ssh
if getent passwd vm-operator >/dev/null; then pkill -u vm-operator || true; userdel vm-operator; fi
rm -rf /var/lib/ameesh-vm-remote /opt/ameesh-vm-adapter
! getent passwd vm-operator && [ ! -e /etc/ameesh-vm-adapter ] && [ ! -e /opt/ameesh-vm-adapter ] && echo "retour AG3 ok"
EOS
ssh -F none -o BatchMode=yes "root@$AG3" true && echo "root@AG3 OK après retour"
echo "Suite : retour-DB-vm_probe.sh --apply (rôle retiré)."
