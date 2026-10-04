# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : index des fils lisibles (`thread_index`, 0009).

SQL déplacé tel quel depuis `ameesh.fil` (lot L1). L'index et la trace
dans la boîte (`agent_mailbox.meta`) sont UNE requête : un CTE qui modifie
s'exécute même s'il n'est pas lu.
"""
from __future__ import annotations

import json

from .. import interface

_INDEX_UPSERT = """
INSERT INTO thread_index
    (project, lot, transport, host, location, last_entry_id, last_mailbox_id,
     last_author, last_excerpt, last_at, entries)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), 1)
ON CONFLICT (project, lot, transport, host) DO UPDATE SET
    location        = excluded.location,
    entries         = thread_index.entries + 1,
    last_entry_id   = CASE WHEN thread_index.last_at IS NULL
                             OR excluded.last_at >= thread_index.last_at
                           THEN excluded.last_entry_id ELSE thread_index.last_entry_id END,
    last_mailbox_id = CASE WHEN thread_index.last_at IS NULL
                             OR excluded.last_at >= thread_index.last_at
                           THEN excluded.last_mailbox_id ELSE thread_index.last_mailbox_id END,
    last_author     = CASE WHEN thread_index.last_at IS NULL
                             OR excluded.last_at >= thread_index.last_at
                           THEN excluded.last_author ELSE thread_index.last_author END,
    last_excerpt    = CASE WHEN thread_index.last_at IS NULL
                             OR excluded.last_at >= thread_index.last_at
                           THEN excluded.last_excerpt ELSE thread_index.last_excerpt END,
    last_at         = greatest(thread_index.last_at, excluded.last_at),
    updated_at      = now()
"""


class Threads(interface.Threads):

    def index(self, *, project, lot, transport, host, location, entry_id, mailbox_ids,
              author, excerpt, ts, trace) -> None:
        # ids entiers seulement : ils entrent tels quels dans le texte SQL
        ids = sorted({int(i) for i in mailbox_ids})
        params: tuple = (
            project, lot, transport, host, location, entry_id, ids[-1] if ids else None,
            author, excerpt, ts,
        )
        sql = _INDEX_UPSERT
        if ids:
            # Un CTE qui modifie s'exécute même s'il n'est pas lu : une seule
            # requête pour l'index et la trace dans la boîte.
            sql = ("WITH trace AS (UPDATE agent_mailbox SET meta = meta || %%s::jsonb "
                   "WHERE id IN (%s)) " % ", ".join(str(i) for i in ids)) + _INDEX_UPSERT
            params = (json.dumps(trace, ensure_ascii=False),) + params
        self.db.execute(sql, params)

    def indexed(self) -> list[dict]:
        return self.db.query(
            """
            SELECT project, lot, transport, host, location, last_entry_id,
                   last_author, last_excerpt, entries,
                   extract(epoch from last_at)::float8 AS last_ts
              FROM thread_index
             ORDER BY last_at DESC NULLS LAST, project, lot
            """)
