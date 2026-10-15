#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# Sauvegarde de la base d'un mesh (lot L55) : copie locale gardée 14 jours, puis
# copie hors de la VM si /etc/ameesh/sauvegarde.env existe (stockage d'objets
# S3 ; une clé en ÉCRITURE SEULE suffit et est recommandée).
#   SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_BUCKET, SCW_REGION
#
# Les ensembles de récupération inscrits au catalogue de rétention (par le
# contrôleur de release) ne sont jamais expirés ici : seule une copie
# quotidienne sans inscription garde la fenêtre locale déclarée.
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
# Expiry only follows completed backup/upload. A recovery set registered in
# the private catalog (by the release controller) is never expired here; a
# daily dump no record names keeps the declared local window. A set with a
# remote member is only expired through the enrolled provider document;
# without it that set stays protected instead of being guessed. A refused
# candidate (non-private, shared or foreign-owned dump) never expires: its
# status is kept and re-raised after prune, which must still run, so the
# daily timer cannot stay green over a file it will never delete.
expiry_status=0
"$python" /usr/local/sbin/ameesh-retention-privee.py --root "$d" expire-unregistered \
    --now "$(date +%s)" --max-age 1209600 || expiry_status=$?
if [ -r /etc/ameesh/retention-provider.json ]; then
    "$python" /usr/local/sbin/ameesh-retention-privee.py --root "$d" prune \
        --provider-enrollment /etc/ameesh/retention-provider.json --now "$(date +%s)"
else
    "$python" /usr/local/sbin/ameesh-retention-privee.py --root "$d" prune --now "$(date +%s)"
fi
if [ "$expiry_status" -ne 0 ]; then
    echo "rétention privée : expire-unregistered a refusé un candidat ($expiry_status) ; prune a tourné" >&2
    exit "$expiry_status"
fi
