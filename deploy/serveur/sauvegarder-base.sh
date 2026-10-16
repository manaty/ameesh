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
#
# Les dumps ne sont lisibles que par ameesh : répertoire 0700, fichiers 0600,
# quel que soit le umask de l'appelant (#1305) ou une ACL POSIX par
# défaut du répertoire (#1309) : le helper publie un fichier déjà 0600
# (#1484). Les surcharges
# AMEESH_SAUVEGARDES_DIR / AMEESH_PYTHON / AMEESH_SAUVEGARDER_PRIVE /
# AMEESH_RETENTION_PRIVEE ne servent qu'aux témoins : le service ne les
# positionne pas et les défauts installés ne changent pas.
set -eu
umask 077
# Défauts installés (le service ne positionne aucune surcharge) ; les surcharges
# explicites, plus bas, ne servent qu'aux témoins de bouchonnage (#1305).
d=/home/ameesh/sauvegardes
python=/opt/ameesh/venv/bin/python
helper=/usr/local/sbin/ameesh-sauvegarder-prive.py
retention=/usr/local/sbin/ameesh-retention-privee.py
d=${AMEESH_SAUVEGARDES_DIR:-$d}
python=${AMEESH_PYTHON:-$python}
helper=${AMEESH_SAUVEGARDER_PRIVE:-$helper}
retention=${AMEESH_RETENTION_PRIVEE:-$retention}
mkdir -p "$d"
chmod 700 "$d"
TMPDIR="$d/.travail"
export TMPDIR
f="$d/ameesh-$(hostname)-$(date +%F-%H%M%S).dump"
# pg_dump réutilise un fichier existant sans changer son mode : un dump laissé
# en 0644 par une version antérieure serait réécrit tel quel. Le rendre privé
# avant l'écriture (sans toucher à son contenu).
if [ -e "$f" ]; then chmod 600 "$f"; fi
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
"$python" "$retention" --root "$d" expire-unregistered \
    --now "$(date +%s)" --max-age 1209600 || expiry_status=$?
if [ -r /etc/ameesh/retention-provider.json ]; then
    "$python" "$retention" --root "$d" prune \
        --provider-enrollment /etc/ameesh/retention-provider.json --now "$(date +%s)"
else
    "$python" "$retention" --root "$d" prune --now "$(date +%s)"
fi
# même normalisation des dumps plus anciens, sans les réécrire. Après la
# rétention : un dump public est d'abord refusé par le catalogue (il ne doit
# pas expirer sans preuve), puis normalisé ici pour que le passage suivant le
# voie privé. Seulement les dumps de l'appelant, et seulement ceux à un seul
# lien : un dump partagé (plusieurs liens durs) est refusé par le catalogue,
# et changer son mode changerait aussi celui des autres chemins du même inode.
find "$d" -type f -name 'ameesh-*.dump' -user "$(id -un)" -links 1 ! -perm 0600 \
    -exec chmod 600 {} +
if [ "$expiry_status" -ne 0 ]; then
    echo "rétention privée : expire-unregistered a refusé un candidat ($expiry_status) ; prune a tourné" >&2
    exit "$expiry_status"
fi
