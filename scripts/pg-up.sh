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
# montage est comparé par sa source sur l'hôte (.Source : chemin d'un bind
# mount, ou _data d'un volume nommé) ; toute inspection en échec refuse
refus() { echo "$1 — refus de $ACTION $NAME" >&2; exit 1; }
sources() { docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' "$1"; }
# forme canonique d'une source (`/srv/pg-data/` ou un lien vers ce dossier
# désignent le même répertoire) ; telle quelle si realpath échoue
canon() {
  local src
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    realpath -m -- "$src" 2>/dev/null || printf '%s\n' "$src"
  done <<<"$1"
}
# refus si un autre conteneur (ceux que liste `docker ps $1`) monte l'une des
# sources de $2
garde_sources() {
  local conteneurs autre leurs src miennes
  miennes=$(canon "$2")
  conteneurs=$(docker ps $1 --format '{{.Names}}') || refus "docker ps en échec"
  while IFS= read -r autre; do
    [ -n "$autre" ] && [ "$autre" != "$NAME" ] || continue
    leurs=$(sources "$autre") || refus "inspection des montages de $autre en échec"
    leurs=$(canon "$leurs")
    while IFS= read -r src; do
      [ -n "$src" ] || continue
      if grep -qxF -- "$src" <<<"$leurs"; then
        refus "$src (données de $NAME) est monté par le conteneur $autre"
      fi
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
        source_voulue=$(docker volume inspect -f '{{.Mountpoint}}' "$VOLUME") \
          || refus "inspection du volume $VOLUME en échec"
      fi ;;
  esac
  if [ -n "$source_voulue" ]; then
    garde_sources -a "$source_voulue"
  fi
  echo "création du conteneur $NAME ($IMAGE, 127.0.0.1:$PORT, volume $VOLUME)"
  docker run -d --name "$NAME" \
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
