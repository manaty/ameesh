#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# Sauvegarde de la base d'un mesh (lot L55) : copie locale gardée 14 jours, puis
# copie hors de la VM si /etc/ameesh/sauvegarde.env existe (stockage d'objets
# S3 ; une clé en ÉCRITURE SEULE suffit et est recommandée).
#   SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_BUCKET, SCW_REGION
set -eu
d=/home/ameesh/sauvegardes
mkdir -p "$d"
f="$d/ameesh-$(hostname)-$(date +%F).dump"
pg_dump -Fc "postgresql://ameesh@127.0.0.1:5432/ameesh" -f "$f"
find "$d" -name 'ameesh-*.dump' -mtime +14 -delete
[ -r /etc/ameesh/sauvegarde.env ] || exit 0
. /etc/ameesh/sauvegarde.env
curl -fsS --retry 3 -T "$f" --user "$SCW_ACCESS_KEY:$SCW_SECRET_KEY" \
    --aws-sigv4 "aws:amz:$SCW_REGION:s3" \
    "https://$SCW_BUCKET.s3.$SCW_REGION.scw.cloud/$(hostname)/$(basename "$f")"
