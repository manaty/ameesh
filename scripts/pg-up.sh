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
#: renommé le garde) ; une machine neuve crée ameesh-pg-data.
VOLUME=${AMEESH_PG_VOLUME:-ameesh-pg-data}

if docker inspect "$NAME" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
    echo "démarrage du conteneur $NAME"
    docker start "$NAME" >/dev/null
  fi
else
  if [ -z "$PASSWORD" ]; then
    echo "AMEESH_PG_PASSWORD requis pour créer le banc : aucun mot de passe n'est codé dans le dépôt." >&2
    exit 1
  fi
  echo "création du conteneur $NAME ($IMAGE, 127.0.0.1:$PORT)"
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
