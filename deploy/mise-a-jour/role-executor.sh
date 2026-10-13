#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Reapply an existing executor role after migrations, as the migration owner.
# Usage: [PGPASSWORD=…] role-executor.sh '<owner DSN>' [executor-role]
# PGOPTIONS must select the mesh schema. Never prints the DSN or creates logins.
# #518: give the DSN without its password, and the password in PGPASSWORD (or
# ~/.pgpass), which psql reads: a command line is readable by every process of
# the host. `poste.sh role` does so.
set -euo pipefail
owner_dsn=${1:?Usage: role-executor.sh '<owner DSN>' [executor-role]}
executor_role=${2:-ameesh_executor}
script_dir=$(cd "$(dirname "$0")" && pwd)
exists=$(psql -X -At "$owner_dsn" -v ON_ERROR_STOP=1 -v role="$executor_role" <<'SQL'
SELECT 1 FROM pg_roles WHERE rolname = :'role';
SQL
)
if [ "$exists" = 1 ]; then
    psql -X -q "$owner_dsn" -v ON_ERROR_STOP=1 -v role="$executor_role" \
        -f "$script_dir/../sql/role-executor.sql"
    echo 'Executor role reapplied.'
else
    echo 'Executor role absent: initial provisioning requires a separate owner decision.'
fi
