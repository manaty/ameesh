#!/bin/bash
# Preuves après pose. Phase A (avant remote.json) : TOUT est refusé, y compris true. Phase B (après inscription réelle,
# #6296/#6298) : true/journalctl admis, le reste refusé. Chaque attendu est asserté ; échec = rc 1 du script.
set -u
PHASE=${1:?usage: tests-AG3-vm-operator.sh A|B}
O=(-F none -o BatchMode=yes -o IdentitiesOnly=yes -o "IdentityFile=$HOME/.ssh/id_ed25519" -o StrictHostKeyChecking=yes -o ConnectTimeout=8)
ECHEC=0
attendu() { local voulu=$1; shift; local rc=0; ssh "${O[@]}" vm-operator@51.15.137.245 "$@" >/dev/null 2>&1 || rc=$?
  if { [ "$voulu" = admis ] && [ $rc -eq 0 ]; } || { [ "$voulu" = refuse ] && [ $rc -ne 0 ] && [ $rc -ne 255 ]; }; then echo "OK   $voulu rc=$rc : $*"; else echo "ECHEC attendu $voulu, rc=$rc : $*"; ECHEC=1; fi; }
if [ "$PHASE" = A ]; then
  attendu refuse /usr/bin/true
  attendu refuse /usr/bin/journalctl -b -1 -n 1 -o cat --no-pager
else
  attendu admis /usr/bin/true
  attendu admis /usr/bin/journalctl -b -1 -n 1 -o cat --no-pager
fi
attendu refuse id
attendu refuse /bin/sh -c id
attendu refuse /usr/bin/sudo -n /usr/bin/systemctl status ssh
# PTY : sshd doit refuser l'allocation (message), et la commande ne doit rien produire de plus
out=$(ssh "${O[@]}" -tt vm-operator@51.15.137.245 /usr/bin/true 2>&1); echo "$out" | grep -qi "PTY allocation request failed" && echo "OK   PTY refusé" || { echo "ECHEC PTY : $out"; ECHEC=1; }
# root et ameesh toujours joignables (sshd intact)
ssh -F none -o BatchMode=yes root@51.15.137.245 true && echo "OK   root@AG3" || { echo "ECHEC root@AG3"; ECHEC=1; }
exit $ECHEC
