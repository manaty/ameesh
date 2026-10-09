#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Isolation d'une persona sur le serveur d'un mesh (étude v2 E1) : un
# utilisateur Unix par persona. Ses sessions, sa mémoire, ses dépôts de travail
# et l'état de son exécuteur vivent dans SON dossier (0700), illisibles par les
# autres personas.
#
#   ajouter-persona.sh <persona> [--docker]
#   systemctl enable --now ameesh-persona@<persona>
#
# Ce qu'il crée, de façon idempotente :
#   - l'utilisateur `p-<persona>` (sans mot de passe, sans sudo, sans shell),
#     du groupe `ameesh-personas` ;
#   - sa configuration ameesh (même base, même hôte, mêmes canons que
#     l'utilisateur `ameesh`) et son .pgpass ;
#   - son profil `dsh` (copie de celui de `ameesh`, hook de courrier inclus).
#
# --docker l'ajoute au groupe docker : ses tests peuvent lancer des
# conteneurs, mais le groupe docker vaut un accès root à la machine, ce qui
# annule l'isolation. Ne l'utiliser qu'en connaissance de cause, en attendant
# Docker sans privilèges (rootless) par utilisateur.
#
# Limite : toutes les personas partagent le rôle Postgres des exécuteurs
# (L56) ; le cloisonnement des DONNÉES par persona est la suite du volet E.
set -euo pipefail
persona=${1:?persona}
[[ $persona =~ ^[a-z0-9][a-z0-9-]{0,28}$ ]] || { echo "nom de persona invalide : $persona" >&2; exit 2; }
user="p-$persona"
groupe=ameesh-personas

getent group "$groupe" >/dev/null || groupadd --system "$groupe"
# Les clés des harnais (/etc/ameesh/harnais.env) sont lues par systemd, en root,
# avant le passage à l'utilisateur de la persona : elle ne lit pas le fichier.

if ! id "$user" >/dev/null 2>&1; then
  useradd --create-home --home-dir "/home/$user" --shell /usr/sbin/nologin \
          --gid "$groupe" --comment "persona ameesh $persona" "$user"
fi
chmod 0700 "/home/$user"
if [[ ${2:-} == --docker ]]; then
  usermod -aG docker "$user"
  echo "ATTENTION : $user est dans le groupe docker (équivaut à root)" >&2
fi

# configuration ameesh : celle de l'utilisateur ameesh (base, hôte, canons)
install -d -m 0700 -o "$user" -g "$groupe" "/home/$user/.config" "/home/$user/.config/ameesh"
install -m 0600 -o "$user" -g "$groupe" /home/ameesh/.config/ameesh/config.json \
        "/home/$user/.config/ameesh/config.json"
pw=$(sed -n 's/^PGPASSWORD=//p' /etc/ameesh/db.env)
printf '127.0.0.1:5432:ameesh:ameesh:%s\n' "$pw" \
  | install -m 0600 -o "$user" -g "$groupe" /dev/stdin "/home/$user/.pgpass"
unset pw

# profil dsh (si l'utilisateur ameesh en a un)
if [[ -d /home/ameesh/.dsh/profiles/agent ]]; then
  install -d -m 0700 -o "$user" -g "$groupe" "/home/$user/.dsh" "/home/$user/.dsh/profiles"
  rm -rf "/home/$user/.dsh/profiles/agent"
  cp -r /home/ameesh/.dsh/profiles/agent "/home/$user/.dsh/profiles/agent"
  sed "s#/home/ameesh/#/home/$user/#g" /home/ameesh/.dsh/profiles/agent/cordis.patch.yml \
    > "/home/$user/.dsh/profiles/agent/cordis.patch.yml"
  cp /home/ameesh/.dsh/hooks.json "/home/$user/.dsh/hooks.json"
  chown -R "$user:$groupe" "/home/$user/.dsh"
fi

echo "persona $persona : utilisateur $user prêt — systemctl enable --now ameesh-persona@$persona"
