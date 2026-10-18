#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Paquet git pour un commit absent du depot destinataire (#1155, avis R1 B3).
#
# Le staging vise un ancetre de origin/main ; exclure origin/main du paquet
# revient a exclure la cible elle-meme et git refuse le paquet vide (rc 128).
# Les exclusions se calculent donc sur les objets reellement presents chez le
# destinataire, jamais sur une reference du depot source.
#
#   paquet-git.sh DEPOT REF SHA SORTIE [BASE_DESTINATAIRE]
#
#   DEPOT  : depot source (le poste), qui contient SHA
#   REF    : reference temporaire a creer dans DEPOT pour le paquet
#   SHA    : commit a transferer
#   SORTIE : fichier .bundle a ecrire
#   BASE   : commit deja present chez le destinataire, exclu du paquet
#            (optionnel ; ignore s'il est inconnu du depot source ou egal a SHA)
#
# Muet en cas de succes ; la reference temporaire est retiree dans tous les cas.
set -euo pipefail

[ $# -ge 4 ] || { echo "usage : paquet-git.sh DEPOT REF SHA SORTIE [BASE]" >&2; exit 2; }
depot=$1
ref=$2
sha=$3
sortie=$4
base=${5:-}

git -C "$depot" update-ref "$ref" "$sha"
trap 'git -C "$depot" update-ref -d "$ref" 2>/dev/null || true' EXIT
exclusions=()
if [ -n "$base" ] && [ "$base" != "$sha" ] \
   && git -C "$depot" cat-file -e "$base^{commit}" 2>/dev/null; then
  exclusions+=("^$base")
fi
rm -f "$sortie"
git -C "$depot" bundle create -q "$sortie" "$ref" ${exclusions[@]+"${exclusions[@]}"}
