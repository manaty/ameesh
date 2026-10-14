#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# Sauvegarde de la base d'un mesh (lot L55) : copie locale gardée 14 jours, puis
# copie hors de la VM si /etc/ameesh/sauvegarde.env existe (stockage d'objets
# S3 ; une clé en ÉCRITURE SEULE suffit et est recommandée).
#   SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_BUCKET, SCW_REGION
#
# Les dumps ne sont lisibles que par ameesh : répertoire 0700, fichiers 0600,
# quel que soit le umask de l'appelant (#1305). AMEESH_SAUVEGARDES_DIR ne sert
# qu'aux tests ; le service ne le positionne pas.
set -eu
umask 077
d=${AMEESH_SAUVEGARDES_DIR:-/home/ameesh/sauvegardes}
mkdir -p "$d"
chmod 700 "$d"
f="$d/ameesh-$(hostname)-$(date +%F).dump"
# pg_dump réutilise un fichier existant sans changer son mode : un dump du jour
# laissé en 0644 par une version antérieure serait réécrit tel quel. Le rendre
# privé avant l'écriture (sans toucher à son contenu).
if [ -e "$f" ]; then chmod 600 "$f"; fi
pg_dump -Fc "postgresql://ameesh@127.0.0.1:5432/ameesh" -f "$f"
# même normalisation des dumps plus anciens, sans les réécrire.
find "$d" -type f -name 'ameesh-*.dump' ! -perm 0600 -exec chmod 600 {} +
find "$d" -type f -name 'ameesh-*.dump' -mtime +14 -delete
[ -r /etc/ameesh/sauvegarde.env ] || exit 0
. /etc/ameesh/sauvegarde.env
curl -fsS --retry 3 -T "$f" --user "$SCW_ACCESS_KEY:$SCW_SECRET_KEY" \
    --aws-sigv4 "aws:amz:$SCW_REGION:s3" \
    "https://$SCW_BUCKET.s3.$SCW_REGION.scw.cloud/$(hostname)/$(basename "$f")"
