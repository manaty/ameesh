#!/bin/bash
# #6002 v4.2 — pose des ACL de traversée (pilote #200777 O1), exécuté SUR AG3 en root par
# pose-AG3-vm-operator-v42.sh, et tel quel par temoin-acl-v42.sh en conteneur.
#   acl-pose-v42.sh <dossier de reçus> <login> [<racine>]   (racine : /home/ameesh par défaut)
# u:<login>:--x (traversée seule, sans r, sans ACL par défaut) sur la racine, .local, .local/share,
# .local/share/ameesh ; états avant/après gardés en root 0600 dans les reçus (base du retour borné).
set -euo pipefail; umask 077
OPS=$1; L=$2; B=${3:-/home/ameesh}
P="$B $B/.local $B/.local/share $B/.local/share/ameesh"
stat -c '%a %U:%G %n' $P > "$OPS/acl-parents.mode.avant"
if ! command -v setfacl >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends acl >/dev/null; echo acl > "$OPS/paquet-acl-pose"; fi
getfacl -p $P > "$OPS/acl-parents.avant"
for d in $P; do getfacl -p "$d" | grep -q "^user:$L:" && { echo "ACL $L déjà présente sur $d"; exit 1; }; done
for d in $P; do setfacl -m "u:$L:--x" "$d"; done
for d in $P; do
  getfacl -p "$d" | grep -qx "user:$L:--x" || { echo "ACL absente sur $d"; exit 1; }
  ! getfacl -p "$d" | grep -q '^default:' || { echo "ACL par défaut sur $d"; exit 1; }
done
getfacl -p $P > "$OPS/acl-parents.apres"
chmod 600 "$OPS"/acl-parents.*
echo "ACL posées : u:$L:--x sur 4 parents"
