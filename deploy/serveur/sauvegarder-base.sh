#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# Sauvegarde de la base d'un mesh (lot L55) : copie locale gardée 14 jours, puis
# copie hors de la VM si /etc/ameesh/sauvegarde.env existe (stockage d'objets
# S3 ; une clé en ÉCRITURE SEULE suffit et est recommandée).
#   SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_BUCKET, SCW_REGION
set -eu
umask 077
d=/home/ameesh/sauvegardes
mkdir -p "$d"
chmod 700 "$d"
TMPDIR="$d/.travail"
export TMPDIR
f="$d/ameesh-$(hostname)-$(date +%F-%H%M%S).dump"
python=/opt/ameesh/venv/bin/python
helper=/usr/local/sbin/ameesh-sauvegarder-prive.py
"$python" "$helper" dump --config /home/ameesh/.config/ameesh/config.json --output "$f"
if [ -r /etc/ameesh/sauvegarde.env ]; then
    . /etc/ameesh/sauvegarde.env
    export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_BUCKET SCW_REGION
    "$python" "$helper" upload --file "$f" --host "$(hostname)"
fi
# Retention follows every required backup/upload step, never a failed one.
find "$d" -maxdepth 1 -type f -name 'ameesh-*.dump' -mtime +14 -delete
