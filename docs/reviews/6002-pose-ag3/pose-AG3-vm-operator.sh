#!/bin/bash
# #6002 — pose AG3 du compte VM non root de l'adaptateur (README #6276, préparation 1, 2 partiel, 3, 4, 5).
# Lancé depuis bureau-goal (root SSH existant vers AG3). Sans --apply : préalables seuls, aucun effet.
# remote.json et la connexion DB (étape 2 fin) ATTENDENT #6296 (contrôles réels) et #6298 (rôle vm_probe) :
# sans eux, vm-remote.py refuse tout sauf true/journalctl (échec fermé).
set -euo pipefail
AG3=51.15.137.245; SHA=41262fcde6bf74f70458387bc30ba728ebbb0699; LOGIN=vm-operator
PUB='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBw1FTmJxkxgsLmoafFW96nbNuXr9EEFenPXU6nqLKH0 vm-adapter'
FORCE='/opt/ameesh-vm-adapter/venv/bin/python -I /opt/ameesh-vm-adapter/remote/vm-remote.py dispatch'
APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 root@$AG3 "$@"; }
stop() { echo "STOP: $*" >&2; exit 2; }
# 0. Préalables (lecture seule)
R "! getent passwd $LOGIN" || stop "compte $LOGIN existe"
R "test ! -e /opt/ameesh-vm-adapter && test ! -e /var/lib/ameesh-vm-remote && test ! -e /etc/ssh/authorized_keys/$LOGIN && test ! -e /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf && test ! -e /etc/sudoers.d/ameesh-vm-operator" || stop "fichier cible déjà présent"
R "getent group systemd-journal >/dev/null && sshd -t && command -v python3.12 >/dev/null && command -v visudo >/dev/null" || stop "prérequis système"
echo "préalables OK"; [ $APPLY = 1 ] || { echo "(contrôle seul, aucun effet)"; exit 0; }
TS=$(date -u +%Y%m%dT%H%MZ); OPS=/root/ameesh-operator-6002-AG3-$TS
R "install -d -m 700 $OPS; for u in root ameesh $LOGIN; do sshd -T -C user=$u,host=x,addr=127.0.0.1 2>/dev/null | sort > $OPS/sshd-avant-$u.txt || true; done"
# 1. compte non root, sans mot de passe, groupe systemd-journal seul en plus (aucun groupe agent/sudo)
R "useradd --system --shell /bin/sh --home-dir /var/lib/ameesh-vm-remote --no-create-home --user-group $LOGIN && passwd -l $LOGIN >/dev/null && usermod -aG systemd-journal $LOGIN && id $LOGIN"
# 2 (partiel). code et venv root, non inscriptibles ; remote.json NON posé ici
git -C /home/agent/development/manaty/ameesh archive --format=tar --prefix=src-${SHA:0:12}/ $SHA | R "set -eu; install -d -m 755 -o root -g root /opt/ameesh-vm-adapter; tar -C /opt/ameesh-vm-adapter -xf -; chown -R root:root /opt/ameesh-vm-adapter; chmod -R go-w /opt/ameesh-vm-adapter
  cd /opt/ameesh-vm-adapter; python3.12 -m venv venv-${SHA:0:12}; venv-${SHA:0:12}/bin/pip install -q --no-compile src-${SHA:0:12} >/dev/null; ln -sfn venv-${SHA:0:12} venv
  install -d -m 755 remote; install -m 644 src-${SHA:0:12}/deploy/vm-adapter/remote/vm-remote.py remote/vm-remote.py
  chown -R root:root /opt/ameesh-vm-adapter; chmod -R go-w /opt/ameesh-vm-adapter
  [ -z \"\$(find /opt/ameesh-vm-adapter -xdev \( -perm -g+w -o -perm -o+w \) ! -type l)\" ] && [ -z \"\$(find /opt/ameesh-vm-adapter -xdev ! -user root)\" ]
  sha256sum remote/vm-remote.py | cut -c1-16"
# 3. état du compte VM
R "install -d -m 700 -o $LOGIN -g $LOGIN /var/lib/ameesh-vm-remote; stat -c '%a %U %n' /var/lib/ameesh-vm-remote"
# 4. clé forcée root non inscriptible + Match sshd ; sshd -t, sshd -T root/ameesh inchangés, reload sous session tenue
R "set -eu; install -d -m 755 -o root -g root /etc/ssh/authorized_keys
  printf '%s\n' 'restrict,command=\"$FORCE\" $PUB' > /etc/ssh/authorized_keys/$LOGIN; chmod 644 /etc/ssh/authorized_keys/$LOGIN
  printf '%s\n' '# #6002 : compte VM non root de l adaptateur (README #6276)' 'Match User $LOGIN' '    AuthorizedKeysFile /etc/ssh/authorized_keys/$LOGIN' '    AuthenticationMethods publickey' '    PasswordAuthentication no' '    KbdInteractiveAuthentication no' '    PermitTTY no' '    AllowTcpForwarding no' '    AllowAgentForwarding no' '    AllowStreamLocalForwarding no' '    X11Forwarding no' '    PermitTunnel no' '    PermitUserEnvironment no' '    ForceCommand $FORCE' 'Match all' > /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf
  chmod 644 /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf; sshd -t
  for u in root ameesh; do sshd -T -C user=$u,host=x,addr=127.0.0.1 | sort | diff -q $OPS/sshd-avant-$u.txt - >/dev/null || { echo ECART sshd $u; exit 3; }; done
  sshd -T -C user=$LOGIN,host=x,addr=127.0.0.1 | grep -E '^(forcecommand|permittty|allowtcpforwarding|authorizedkeysfile|permituserenvironment) '"
R "sleep 60" & HOLD=$!; sleep 2; R "systemctl reload ssh && systemctl is-active ssh"; R true && echo "root@AG3 OK après reload"; kill $HOLD 2>/dev/null || true
# 5. sudoers : seule commande, validée
R "set -eu; printf '%s\n' '# #6002 : arrêt de cette VM seulement' '$LOGIN ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block' > /tmp/ameesh-vm-operator.sudoers; visudo -cf /tmp/ameesh-vm-operator.sudoers; install -m 440 -o root -g root /tmp/ameesh-vm-operator.sudoers /etc/sudoers.d/ameesh-vm-operator; rm -f /tmp/ameesh-vm-operator.sudoers; visudo -c >/dev/null && sudo -l -U $LOGIN | tail -2"
echo "pose AG3 terminée $TS (reçus $OPS) ; tests de la clé forcée : tests-AG3-vm-operator.sh"
