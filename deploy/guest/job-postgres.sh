#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# A throwaway Postgres for one offloaded job (lot L159b), in rootless Podman:
# never the application's database, nor the mesh's.
#
#   job-postgres up     starts it; prints the job's variables (K=V lines,
#                       read by ameesh-offload-shell as `before` output)
#   job-postgres down   removes it and its data
#
# Called from a profile hook, with OFFLOAD_JOB set. The port is published on
# the loopback only, on a free port; the password is random, per job.
set -eu
: "${OFFLOAD_JOB:?OFFLOAD_JOB unset (run from a profile hook)}"
NAME="ameesh-job-$OFFLOAD_JOB-pg"
IMAGE=${JOB_PG_IMAGE:-docker.io/library/postgres:17}
USER=${JOB_PG_USER:-agent_mesh}
DB=${JOB_PG_DB:-agent_mesh}

case "${1:-}" in
  up)
    password=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    podman run -d --rm --name "$NAME" --label ameesh-offload-job="$OFFLOAD_JOB" \
      -e POSTGRES_USER="$USER" -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB="$DB" \
      -p 127.0.0.1::5432 "$IMAGE" \
      -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null
    port=$(podman port "$NAME" 5432/tcp | sed -n '1s/.*://p')
    # TCP inside the container: the init-time server listens on the socket only
    i=0
    until podman exec "$NAME" pg_isready -q -h 127.0.0.1 -U "$USER" -d "$DB"; do
      i=$((i + 1))
      if [ "$i" -ge 60 ]; then
        podman logs --tail 20 "$NAME" >&2 || true
        echo "job-postgres: $NAME not ready after 60 s" >&2
        exit 1
      fi
      sleep 1
    done
    echo "job-postgres: $NAME ready on 127.0.0.1:$port" >&2
    echo "PGPASSWORD=$password"
    echo "AMEESH_TEST_DSN=postgresql://$USER@127.0.0.1:$port/$DB"
    echo "DATABASE_URL=postgresql://$USER:$password@127.0.0.1:$port/$DB"
    ;;
  down)
    podman rm -f -t 2 "$NAME" >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: job-postgres up|down" >&2
    exit 2
    ;;
esac
