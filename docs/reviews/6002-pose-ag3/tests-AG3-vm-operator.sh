#!/bin/bash
# Preuves après pose (depuis bureau-goal, clé opérateur existante, compte vm-operator)
O="-F none -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityFile=$HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=yes -o ConnectTimeout=8"
t() { echo "== $*"; ssh $O vm-operator@51.15.137.245 "$@"; echo "rc=$?"; }
t /usr/bin/true                                         # admis : rc 0
t /usr/bin/journalctl -b -1 -n 1 -o cat --no-pager      # admis (lecture groupe systemd-journal)
t id                                                    # refusé par le dispatch
t /bin/sh -c id                                         # refusé
t /usr/bin/sudo -n /usr/bin/systemctl status ssh        # refusé (hors liste)
t /opt/ameesh-vm-adapter/remote/quiesce                 # refus fermé tant que remote.json absent (#6296/#6298)
ssh $O -tt vm-operator@51.15.137.245 /usr/bin/true; echo "pty rc=$?"   # PermitTTY no
