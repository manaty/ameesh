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
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), __COUNT__)
ON CONFLICT (project, lot, transport, host) DO UPDATE SET
    location        = excluded.location,
    entries         = thread_index.entries + __COUNT__,
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

    def lock(self, *, project, lot, transport, host, location) -> int | None:
        self.db.execute(
            "INSERT INTO thread_index (project, lot, transport, host, location)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (project, lot, transport, host, location))
        rows = self.db.query(
            "SELECT counted_until FROM thread_index WHERE project = %s AND lot = %s"
            " AND transport = %s AND host = %s FOR UPDATE", (project, lot, transport, host))
        counted = rows[0]["counted_until"] if rows else None
        return None if counted is None else int(counted)

    def index(self, *, project, lot, transport, host, location, entry_id, mailbox_ids,
              author, excerpt, ts, trace, count=True) -> None:
        # ids entiers seulement : ils entrent tels quels dans le texte SQL
        ids = sorted({int(i) for i in mailbox_ids})
        params: tuple = (
            project, lot, transport, host, location, entry_id, ids[-1] if ids else None,
            author, excerpt, ts,
        )
        sql = _INDEX_UPSERT.replace("__COUNT__", "1" if count else "0")
        if ids:
            # Un CTE qui modifie s'exécute même s'il n'est pas lu : une seule
            # requête pour l'index et la trace dans la boîte.
            sql = ("WITH trace AS (UPDATE agent_mailbox SET meta = meta || %%s::jsonb "
                   "WHERE id IN (%s)) " % ", ".join(str(i) for i in ids)) + sql
            params = (json.dumps(trace, ensure_ascii=False),) + params
        self.db.execute(sql, params)

    def recount(self, *, project, lot, transport, host, location, kept, last, end) -> int:
        """L361b : une seule opération d'index pour un hôte médié — verrou de
        la ligne, recomptage depuis le census du fichier, expiration. Le
        fichier a déjà été réécrit par l'hôte, sous son propre verrou ; un
        recomptage plus récent (`counted_until` au-delà de `end`) fait foi.
        Un `counted_until` égal à `end` est repris : à fin logique constante,
        seule une expiration change le fil."""
        counted = self.lock(project=project, lot=lot, transport=transport, host=host,
                            location=location)
        end = int(end)
        if counted is not None and counted > end:
            return counted
        self.expired(project=project, lot=lot, transport=transport, host=host,
                     kept=int(kept), last=last, end=end)
        return end

    def indexed(self) -> list[dict]:
        return self.db.query(
            """
            SELECT project, lot, transport, host, location, last_entry_id,
                   last_author, last_excerpt, entries,
                   extract(epoch from last_at)::float8 AS last_ts
              FROM thread_index
             ORDER BY last_at DESC NULLS LAST, project, lot
            """)

    def expired(self, *, project, lot, transport, host, kept, last, end) -> None:
        last = last or {}
        self.db.execute(
            """
            UPDATE thread_index
               SET entries         = %s,
                   last_entry_id   = %s,
                   last_mailbox_id = %s,
                   last_author     = coalesce(%s, last_author),
                   last_excerpt    = %s,
                   last_at         = coalesce(to_timestamp(%s), last_at),
                   counted_until   = %s,
                   updated_at      = now()
             WHERE project = %s AND lot = %s AND transport = %s AND host = %s
            """, (int(kept), last.get("entry_id"), last.get("mailbox_id"), last.get("author"),
                  last.get("excerpt") or "", last.get("ts"), int(end),
                  project, lot, transport, host))
