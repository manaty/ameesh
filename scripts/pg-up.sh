#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# agent-mesh-pg — le Postgres local du banc (Docker, postgres:17).
#
# Le port n'est publié que sur la boucle locale : la base n'est pas exposée au
# réseau. Le volume nommé garde les données entre deux démarrages.
# Aucun mot de passe n'est codé ici : AMEESH_PG_PASSWORD le fixe à la création
# du conteneur ; ensuite les clients le lisent dans PGPASSWORD ou ~/.pgpass.
set -euo pipefail

# AMEESH_PG_* est le nom courant ; AGENT_MESH_PG_* reste accepté (alias).
NAME=${AMEESH_PG_NAME:-${AGENT_MESH_PG_NAME:-ameesh-pg}}
PORT=${AMEESH_PG_PORT:-${AGENT_MESH_PG_PORT:-55432}}
IMAGE=${AMEESH_PG_IMAGE:-${AGENT_MESH_PG_IMAGE:-postgres:17}}
USER=${AMEESH_PG_USER:-${AGENT_MESH_PG_USER:-agent_mesh}}
PASSWORD=${AMEESH_PG_PASSWORD:-${AGENT_MESH_PG_PASSWORD:-}}
DB=${AMEESH_PG_DB:-${AGENT_MESH_PG_DB:-agent_mesh}}
#: volume : le banc historique vit dans agent-mesh-pg-data (le conteneur
#: renommé le garde) ; une machine neuve crée ameesh-pg-data. Un autre nom de
#: conteneur a son propre volume, ${NAME}-data (L159f2, #278) : deux agents du
#: même hôte ne partagent jamais un répertoire de données.
if [ "$NAME" = ameesh-pg ]; then
  VOLUME=${AMEESH_PG_VOLUME:-ameesh-pg-data}
else
  VOLUME=${AMEESH_PG_VOLUME:-$NAME-data}
fi

# deux postgres sur un même répertoire de données le corrompent : chaque
# montage est comparé par le répertoire qu'il désigne sur l'hôte ; toute
# inspection en échec refuse
refus() { echo "$1 — refus de $ACTION $NAME" >&2; exit 1; }
# le répertoire d'un volume nommé ($1, monté en $2) : son `device` quand le
# pilote local le monte en bind (`-o o=bind,device=/srv/…`), car .Mountpoint
# n'en dit que le _data ; sinon $2
source_du_volume() {
  local opts o device
  opts=$(docker volume inspect -f '{{index .Options "o"}}{{"\x1f"}}{{index .Options "device"}}' "$1") \
    || return 1
  IFS=$'\x1f' read -r o device <<<"$opts"
  case ",$o," in
    *,bind,*|*,rbind,*) [ -z "$device" ] || { printf '%s\n' "$device"; return 0; } ;;
  esac
  printf '%s\n' "$2"
}
# les répertoires montés par un conteneur, un par ligne : la source d'un bind
# mount, celle d'un volume nommé résolue par `source_du_volume`
sources() {
  local lignes type nom src
  lignes=$(docker inspect -f '{{range .Mounts}}{{.Type}}{{"\x1f"}}{{.Name}}{{"\x1f"}}{{.Source}}{{"\n"}}{{end}}' "$1") \
    || return 1
  while IFS=$'\x1f' read -r type nom src; do
    [ -n "$src" ] || continue
    if [ "$type" = volume ] && [ -n "$nom" ]; then
      src=$(source_du_volume "$nom" "$src") || return 1
    fi
    printf '%s\n' "$src"
  done <<<"$lignes"
}
# deux répertoires se recouvrent : le même, ou l'un contient l'autre
# (/srv et /srv/pg-data), dans un sens comme dans l'autre
recouvre() {
  [ "$1" = "$2" ] || [ "$1" = / ] || [ "$2" = / ] \
    || [ "${1#"$2"/}" != "$1" ] || [ "${2#"$1"/}" != "$2" ]
}
# forme canonique d'une source (`/srv/pg-data/` ou un lien vers ce dossier
# désignent le même répertoire) ; telle quelle si realpath échoue
canon() {
  local src
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    realpath -m -- "$src" 2>/dev/null || printf '%s\n' "$src"
  done <<<"$1"
}
# refus si un autre conteneur (ceux que liste `docker ps $1`) monte un
# répertoire qui recouvre l'une des sources de $2
garde_sources() {
  local conteneurs autre leurs src miennes leur
  miennes=$(canon "$2")
  conteneurs=$(docker ps $1 --format '{{.Names}}') || refus "docker ps en échec"
  while IFS= read -r autre; do
    [ -n "$autre" ] && [ "$autre" != "$NAME" ] || continue
    leurs=$(sources "$autre") || refus "inspection des montages de $autre en échec"
    leurs=$(canon "$leurs")
    while IFS= read -r src; do
      [ -n "$src" ] || continue
      while IFS= read -r leur; do
        [ -n "$leur" ] || continue
        if recouvre "$src" "$leur"; then
          refus "$src (données de $NAME) recouvre $leur, monté par le conteneur $autre"
        fi
      done <<<"$leurs"
    done <<<"$miennes"
  done <<<"$conteneurs"
}

if docker inspect "$NAME" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
    # un conteneur créé avant ce garde peut partager ses données avec un
    # autre : refus de le démarrer si un autre conteneur en marche monte une
    # même source
    ACTION=démarrer
    miennes=$(sources "$NAME") || refus "inspection des montages de $NAME en échec"
    garde_sources "" "$miennes"
    echo "démarrage du conteneur $NAME"
    docker start "$NAME" >/dev/null
  fi
else
  if [ -z "$PASSWORD" ]; then
    echo "AMEESH_PG_PASSWORD requis pour créer le banc : aucun mot de passe n'est codé dans le dépôt." >&2
    exit 1
  fi
  ACTION=créer
  # refus si un autre conteneur, même arrêté, monte déjà ce volume nommé
  autres=$(docker ps -a --filter "volume=$VOLUME" --format '{{.Names}}') \
    || refus "docker ps en échec"
  autres=$(tr '\n' ' ' <<<"$autres")
  if [ -n "${autres// /}" ]; then
    echo "le volume $VOLUME est déjà monté par : $autres— choisissez AMEESH_PG_VOLUME" >&2
    exit 1
  fi
  # le filtre `volume=` ne voit pas la source d'un bind mount (L159f3, #307) :
  # la source que prendra le conteneur (le chemin d'un bind mount, le _data
  # d'un volume nommé qui existe déjà) est comparée à celles de tous les
  # conteneurs, même arrêtés
  case "$VOLUME" in
    /*) source_voulue=$VOLUME ;;
    *)
      volumes=$(docker volume ls -q) || refus "docker volume ls en échec"
      source_voulue=
      if grep -qxF -- "$VOLUME" <<<"$volumes"; then
        point=$(docker volume inspect -f '{{.Mountpoint}}' "$VOLUME") \
          && source_voulue=$(source_du_volume "$VOLUME" "$point") \
          || refus "inspection du volume $VOLUME en échec"
      fi ;;
  esac
  if [ -n "$source_voulue" ]; then
    garde_sources -a "$source_voulue"
  fi
  # #2003 : un volume nommé créé ici porte l'étiquette du banc — le ménage
  # d'ameesh ne récolte que les volumes qu'il peut attribuer (banc étiqueté,
  # ou volume anonyme d'un tour), après `docker_volume_idle` sans conteneur.
  # Un volume existant garde ses étiquettes : Docker ne les modifie pas, et
  # le ménage ne supprime jamais un volume nommé sans étiquette.
  if [ "${VOLUME#/}" = "$VOLUME" ] && ! grep -qxF -- "$VOLUME" <<<"${volumes:-}"; then
    docker volume create --label ameesh.bench=postgres "$VOLUME" >/dev/null \
      || { echo "création du volume $VOLUME en échec" >&2; exit 1; }
  fi
  echo "création du conteneur $NAME ($IMAGE, 127.0.0.1:$PORT, volume $VOLUME)"
  # #1411 : le banc est partagé par tous les agents de l'hôte ; l'étiquette
  # d'un tour (posée par le shim docker depuis AMEESH_CONTAINER_LABELS) en
  # ferait la ressource d'un tour et d'un lot, gardée jusqu'à la fin du lot
  # (L73) puis récoltée à tort. `env -u` : le shim ne voit plus l'étiquette.
  env -u AMEESH_CONTAINER_LABELS docker run -d --name "$NAME" \
    -e POSTGRES_USER="$USER" -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB="$DB" \
    -p "127.0.0.1:$PORT:5432" \
    -v "$VOLUME":/var/lib/postgresql/data \
    --health-cmd "pg_isready -U $USER -d $DB" \
    --health-interval 2s --health-timeout 3s --health-retries 30 \
    "$IMAGE" >/dev/null
fi

for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo unknown)
  [ "$status" = healthy ] && { echo "$NAME prêt sur 127.0.0.1:$PORT"; exit 0; }
  sleep 1
done
echo "le conteneur $NAME n'est pas devenu sain" >&2
docker logs --tail 20 "$NAME" >&2 || true
exit 1
