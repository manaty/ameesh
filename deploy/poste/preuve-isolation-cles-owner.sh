#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# #3444 — preuve, sur l'hôte, que le garde-fou du drop-in OPT-IN
# `deploy/poste/cles-owner.conf` est effectif : un processus d'unité
# utilisateur ne peut plus lire ~/.ssh. Ce n'est pas la frontière de la
# décision #133454 a (même uid = contournable) ; voir docs/EXPLOITATION.md.
#
# À lancer par le propriétaire du compte des exécuteurs (smichea sur le
# poste), depuis un checkout du dépôt :
#
#   deploy/poste/preuve-isolation-cles-owner.sh
#   deploy/poste/preuve-isolation-cles-owner.sh --cible ~/.ssh --helper CHEMIN
#
# Elle démarre une unité UTILISATEUR transitoire avec les mêmes propriétés que
# le drop-in (InaccessiblePaths= sur ~/.ssh, SSH_AUTH_SOCK retiré), y exécute
# le témoin root `/usr/local/libexec/ameesh/verifie-cles-owner` et sort en 0
# seulement si un montage inaccessible (inode mode 000) a réellement été posé
# sur la cible. C'est le critère de recette du garde-fou. Si
# kernel.apparmor_restrict_unprivileged_userns=1, le profil AppArmor
# deploy/poste/ameesh-systemd-executor.apparmor est un prérequis ; sinon il
# est inutile. La preuve reste obligatoire dans les deux cas. Aucun
# secret n'est lu ni affiché : le témoin ne regarde que le point de montage
# et son mode dans l'unité.
set -u

HELPER=/usr/local/libexec/ameesh/verifie-cles-owner
CIBLE=${HOME:?}/.ssh

while [ $# -gt 0 ]; do
  case $1 in
    --helper)
      [ $# -ge 2 ] || { echo "--helper attend un chemin" >&2; exit 2; }
      HELPER=$2; shift 2 ;;
    --cible)
      [ $# -ge 2 ] || { echo "--cible attend un chemin" >&2; exit 2; }
      CIBLE=$2; shift 2 ;;
    -h|--help)
      sed -n '2,/^set -u$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done

[ -x "$HELPER" ] || {
  echo "témoin absent ou non exécutable : $HELPER" >&2
  echo "le poser en root : install -D -m 0755 deploy/poste/verifie-cles-owner $HELPER" >&2
  exit 2
}
# Cible absente : InaccessiblePaths= ne monterait rien ; le témoin doit
# refuser, et la preuve n'aurait aucun sens.
[ -e "$CIBLE" ] || { echo "cible absente : $CIBLE" >&2; exit 2; }

echo "cible            : $CIBLE"
echo "témoin           : $HELPER"
userns=$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo "?")
echo "userns restreint : $userns (1 = restriction Ubuntu 24.04)"
if grep -qs '^ameesh-systemd-executor ' /sys/kernel/security/apparmor/profiles 2>/dev/null; then
  echo "profil AppArmor  : chargé"
else
  echo "profil AppArmor  : chargé (vérification root non lisible) ou absent"
fi

unite="test-3444-cles-$$"
systemd-run --user --unit="$unite" --wait --pipe --collect \
  --property="InaccessiblePaths=$CIBLE" \
  --property=UnsetEnvironment=SSH_AUTH_SOCK \
  --property=UnsetEnvironment=SSH_AGENT_PID \
  /bin/sh -c '
    echo "label AppArmor   : $(cat /proc/self/attr/current)"
    echo "mount ns         : $(readlink /proc/self/ns/mnt)"
    echo "SSH_AUTH_SOCK    : ${SSH_AUTH_SOCK:-absent}"
    exec "$1" "$2"
  ' sh "$HELPER" "$CIBLE"
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "PASS : $CIBLE est hors de l'exécuteur (masque mode 000, agent SSH absent)"
else
  echo "FAIL : isolation non prouvée — vérifier les diagnostics de l'unité et du témoin" >&2
  if [ "$userns" = 1 ]; then
    echo "       restriction userns active : vérifier le profil deploy/poste/ameesh-systemd-executor.apparmor" >&2
  fi
  echo "       voir docs/EXPLOITATION.md" >&2
fi
exit "$rc"
