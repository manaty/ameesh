#!/bin/bash
# Retour arrière de la pose AG3 (vm-operator)
set -euo pipefail
ssh -F none -o BatchMode=yes root@51.15.137.245 'set -eu
rm -f /etc/sudoers.d/ameesh-vm-operator; visudo -c >/dev/null
rm -f /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf /etc/ssh/authorized_keys/vm-operator; sshd -t; systemctl reload ssh
pkill -u vm-operator || true; userdel vm-operator; rm -rf /var/lib/ameesh-vm-remote /opt/ameesh-vm-adapter
! getent passwd vm-operator && echo retour-ok'
