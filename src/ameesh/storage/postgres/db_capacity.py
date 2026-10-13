# SPDX-License-Identifier: AGPL-3.0-only
"""Read server-wide client capacity, including clients in other databases."""


def read(db):
    return db.query("""
        SELECT current_setting('max_connections')::int AS maximum,
               count(*)::int AS used,
               count(*) FILTER (WHERE state = 'idle')::int AS idle
        FROM pg_stat_activity WHERE backend_type = 'client backend'
    """)[0]
