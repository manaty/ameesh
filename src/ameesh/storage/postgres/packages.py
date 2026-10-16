# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : fiches WorkPackage recopiées du canon (`work_packages`, 0026, L29).

Déclaratif seulement : `canon sync` y écrit les fiches (avec `canon_ref`),
jamais l'état d'exécution, qui reste dans `work_items`.
"""
from __future__ import annotations

import json

from .. import interface

_COLUMNS = """
    id, kind, title, parent, responsible, team, scope, status, canon_ref, canon, present,
    extract(epoch from synced_at)::float8 AS synced_ts,
    to_char(start_on, 'YYYY-MM-DD') AS start_on,
    to_char(end_on, 'YYYY-MM-DD') AS end_on,
    to_char(delivery_on, 'YYYY-MM-DD') AS delivery_on,
    to_char(planned_start, 'YYYY-MM-DD') AS planned_start,
    to_char(planned_end, 'YYYY-MM-DD') AS planned_end,
    to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery,
    planned_source, planned_by
"""

#: Un seul verrou par schéma, pour TOUS les canons (L42/0031 : les identifiants
#: de `work_packages` sont globaux, le premier canon qui en déclare un le
#: garde). La clé ne dépend ni du canon ni du clone, comme celle du registre
#: des authentificateurs : deux canons ne doivent pas lire « absent » ensemble
#: puis s'écraser, et la bascule du canon par défaut écrit la même colonne.
SYNC_LOCK_SQL = ("SELECT pg_advisory_xact_lock(hashtextextended("
                 "current_schema() || ':canon-sync-packages', 0))")

#: Borne de l'attente du verrou de sync (#1747) : généreuse — un premier sync
#: d'hôte peut être long —, mais finie. Sous psycopg, `statement_timeout` levé
#: pour la prise ne borne plus l'attente : un détenteur resté « idle in
#: transaction » (par exemple `adopt_default`, qui tient le verrou du registre
#: des authentificateurs et la ligne `canon_state ''`) figeait le sync sans
#: fin. Sous psql, le délai CLIENT du pilote (`statement_timeout` de session
#: + 5 s appliqué à chaque instruction, `db._PsqlTransaction._run`) reste la
#: borne effective quand elle est plus courte.
SYNC_LOCK_TIMEOUT_MS = 600_000


class WorkPackages(interface.WorkPackages):

    def lock_sync(self) -> None:
        """Verrou transactionnel des fiches, pris AVANT leur lecture.

        L'attente d'un verrou consultatif compte dans `statement_timeout` : au
        démarrage d'un hôte, le premier sync d'un canon peut être long (un
        déploiement change beaucoup de fiches) et les suivants échoueraient en
        57014, laissant la porte d'hôte fermée jusqu'au réessai. On lève donc
        le délai le temps de la prise, puis on rétablit ceux de la session pour
        le reste de la transaction. `set_config(..., true)` (et non un
        `SET LOCAL ... %s`) : une instruction utilitaire n'accepte pas de
        paramètre lié, les deux pilotes envoient le leur tel quel.

        L'attente reste BORNÉE (#1747) : `lock_timeout` est posé le temps de la
        prise (`SYNC_LOCK_TIMEOUT_MS`), puis rétabli avec `statement_timeout`.
        Sans lui, sous psycopg, un détenteur qui ne rend pas le verrou figeait
        le sync sans fin ; sous psql, le délai client du pilote
        (`statement_timeout` de session + 5 s par instruction) borne déjà
        l'attente, et la plus courte des deux bornes s'applique.
        """
        previous = self.db.query(
            "SELECT current_setting('statement_timeout') AS statement,"
            " current_setting('lock_timeout') AS lock")[0]
        self.db.execute("SELECT set_config('statement_timeout', '0', true),"
                        " set_config('lock_timeout', %s, true)",
                        ("%dms" % SYNC_LOCK_TIMEOUT_MS,))
        self.db.execute(SYNC_LOCK_SQL)
        self.db.execute("SELECT set_config('statement_timeout', %s, true),"
                        " set_config('lock_timeout', %s, true)",
                        (previous["statement"], previous["lock"]))

    def all(self, *, include_absent: bool = False) -> list[dict]:
        sql = "SELECT %s FROM work_packages" % _COLUMNS
        if not include_absent:
            sql += " WHERE present"
        return self.db.query(sql + " ORDER BY id")

    def get(self, ident) -> dict | None:
        rows = self.db.query("SELECT %s FROM work_packages WHERE id = %%s" % _COLUMNS,
                             (ident,))
        return rows[0] if rows else None

    def upsert(self, row) -> None:
        self.db.query(
            """
            INSERT INTO work_packages
                (id, kind, title, parent, responsible, team, scope, status, canon_ref,
                 canon, start_on, end_on, delivery_on, present, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::date, %s::date,
                    %s::date, true, now())
            ON CONFLICT (id) DO UPDATE SET
                kind = excluded.kind, title = excluded.title, parent = excluded.parent,
                responsible = excluded.responsible, team = excluded.team,
                scope = excluded.scope, status = excluded.status,
                canon_ref = excluded.canon_ref, canon = excluded.canon,
                start_on = excluded.start_on, end_on = excluded.end_on,
                delivery_on = excluded.delivery_on,
                present = true, synced_at = now()
            RETURNING id
            """,
            (row["id"], row["kind"], row["title"], row.get("parent"), row.get("responsible"),
             row.get("team"),
             None if row.get("scope") is None else json.dumps(list(row["scope"])),
             row.get("status"), row["canon_ref"], row.get("canon"),
             # L96 : dates déclarées au canon (jours ISO)
             row.get("start_on"), row.get("end_on"), row.get("delivery_on")),
        )

    def retire(self, idents) -> int:
        idents = list(idents)
        if not idents:
            return 0
        rows = self.db.query(
            "UPDATE work_packages SET present = false, synced_at = now()"
            " WHERE present AND id IN (%s) RETURNING id" % ", ".join(["%s"] * len(idents)),
            tuple(idents))
        return len(rows)
