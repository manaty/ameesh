# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : index des fils lisibles (`thread_index`, 0009).

SQL déplacé tel quel depuis `ameesh.fil` (lot L1). L'index et la trace
dans la boîte (`agent_mailbox.meta`) sont UNE requête quand l'entrée compte :
un CTE qui modifie s'exécute même s'il n'est pas lu. Une trace seule
(`count` faux, L361c) n'écrit que la boîte et `location` — jamais l'index,
qui n'appartient qu'au census du fichier (`recount`).
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

#: L361c — une écriture de trace (`count` faux) ne touche NI le compte NI
#: `last_*` : le census du fichier (`recount`) fait foi. Seule la boîte est
#: tracée et `location` rafraîchie (la ligne existe : `lock`/`recount` l'ont
#: créée). Un CTE qui modifie s'exécute même s'il n'est pas lu.
_TRACE_SQL = """
WITH trace AS (UPDATE agent_mailbox SET meta = meta || %s::jsonb WHERE id IN (__IDS__))
UPDATE thread_index
   SET location = %s, updated_at = now()
 WHERE project = %s AND lot = %s AND transport = %s AND host = %s
"""

_LOCATION_SQL = """
UPDATE thread_index
   SET location = %s, updated_at = now()
 WHERE project = %s AND lot = %s AND transport = %s AND host = %s
"""


class Threads(interface.Threads):

    def lock(self, *, project, lot, transport, host, location) -> int | None:
        counted, _entries = self._locked_row(project=project, lot=lot, transport=transport,
                                             host=host, location=location)
        return counted

    def _locked_row(self, *, project, lot, transport, host,
                    location) -> tuple[int | None, int]:
        """Crée la ligne au besoin, la verrouille jusqu'à la fin de la
        transaction et rend `(counted_until, entries)`."""
        self.db.execute(
            "INSERT INTO thread_index (project, lot, transport, host, location)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (project, lot, transport, host, location))
        rows = self.db.query(
            "SELECT counted_until, entries FROM thread_index WHERE project = %s AND lot = %s"
            " AND transport = %s AND host = %s FOR UPDATE", (project, lot, transport, host))
        if not rows:
            return None, 0
        counted = rows[0]["counted_until"]
        return (None if counted is None else int(counted)), int(rows[0]["entries"])

    def index(self, *, project, lot, transport, host, location, entry_id, mailbox_ids,
              author, excerpt, ts, trace, count=True) -> None:
        # ids entiers seulement : ils entrent tels quels dans le texte SQL
        ids = sorted({int(i) for i in mailbox_ids})
        if not count:
            # L361c : la trace ne compte pas et ne cite pas. `entries` et
            # `last_*` n'appartiennent qu'au census (`recount`) : une entrée
            # expirée entre-temps ne peut pas être ressuscitée par sa trace.
            if ids:
                sql = _TRACE_SQL.replace("__IDS__", ", ".join(str(i) for i in ids))
                self.db.execute(sql, (json.dumps(trace, ensure_ascii=False), location,
                                      project, lot, transport, host))
            else:
                self.db.execute(_LOCATION_SQL, (location, project, lot, transport, host))
            return
        params: tuple = (
            project, lot, transport, host, location, entry_id, ids[-1] if ids else None,
            author, excerpt, ts,
        )
        sql = _INDEX_UPSERT.replace("__COUNT__", "1")
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
        Un `counted_until` égal à `end` n'est repris que si le census ne
        rallonge pas la ligne (`kept <= entries`) : à fin logique constante,
        une expiration ne fait que raccourcir le fil, donc un census plus
        long a été lu avant elle et est périmé (L361c)."""
        counted, entries = self._locked_row(project=project, lot=lot, transport=transport,
                                            host=host, location=location)
        end = int(end)
        if counted is not None:
            if counted > end:
                return counted
            if counted == end and int(kept) > entries:
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
