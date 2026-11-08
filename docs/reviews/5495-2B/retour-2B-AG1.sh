#!/bin/bash
# Retour arrière 2B : retire la seule ligne ameesh-staging-L2-AG1-* de chaque cible, puis efface la clé d'AG1.
set -euo pipefail
for ip in 78.232.56.22 78.232.60.1 51.15.137.245; do
  ssh -F none -o BatchMode=yes root@$ip 'set -eu; f=/root/.ssh/authorized_keys; cp -p $f $f.avant-retour-2B-$(date -u +%Y%m%dT%H%MZ); grep -v "ameesh-staging-L2-AG1-" $f > $f.tmp || true; chmod 600 $f.tmp; mv $f.tmp $f; ! grep -q "ameesh-staging-L2-AG1-" $f'
done
ssh -F none -o BatchMode=yes root@78.232.56.22 'shred -u /home/ameesh-staging/.ssh/id_ed25519; rm -f /home/ameesh-staging/.ssh/id_ed25519.pub'
echo "retour 2B fait"
