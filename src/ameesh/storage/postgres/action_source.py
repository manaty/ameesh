# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : la table d'actions lue par ameesh-approve.

SQL déplacé tel quel depuis `ameesh.approve.sources` (lot L1, phase 2).
Lecture seule. Le nom de la table n'entre dans le texte SQL que par
`quote_ident` (identifiant simple, refusé sinon) ; les colonnes se lisent
dans le catalogue (`pg_attribute`), sans erreur si la table manque.
"""
from __future__ import annotations

from ...db import quote_ident
from .. import interface


class ActionSource(interface.ActionSource):

    def columns(self, table) -> set:
        rows = self.db.query(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
            "AND attnum > 0 AND NOT attisdropped", (table,))
        return {row["attname"] for row in rows}

    def rows(self, table, action_id) -> list[dict]:
        return self.db.query(
            "SELECT * FROM %s WHERE action_id = %%s" % quote_ident(table), (action_id,))
