#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# #3444 — preuve, sur l'hôte, que le garde-fou du drop-in `cles-owner.conf`
# est effectif : un processus d'unité utilisateur ne peut plus lire ~/.ssh.
#
# À lancer par le propriétaire du compte des exécuteurs (smichea sur le
# poste), depuis un checkout du dépôt :
#
#   deploy/poste/preuve-isolation-cles-owner.sh
#   deploy/poste/preuve-isolation-cles-owner.sh --cible ~/.ssh --helper CHEMIN
#
# Elle démarre une unité UTILISATEUR transitoire avec les mêmes propriétés que
# le drop-in (InaccessiblePaths=, SSH_AUTH_SOCK retiré), y exécute le témoin
# root `/usr/local/libexec/ameesh/verifie-cles-owner`, et sort en 0 seulement
# si le mount namespace a réellement été posé. C'est le critère de recette du
# lot : avant le prérequis AppArmor (deploy/poste/ameesh-systemd-executor),
# la preuve ÉCHOUE (InaccessiblePaths= silencieusement ignoré) ; après, elle
# passe. Aucun secret n'est lu ni affiché : le témoin dit seulement si le
# chemin est encore listable.
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
  --property="InaccessiblePaths=-$CIBLE" \
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
  echo "PASS : $CIBLE est hors de l'exécuteur (mount ns posé, agent SSH absent)"
else
  echo "FAIL : isolation absente — prérequis AppArmor non posé ?" >&2
  echo "       voir deploy/poste/ameesh-systemd-executor.apparmor, docs/EXPLOITATION.md" >&2
fi
exit "$rc"
