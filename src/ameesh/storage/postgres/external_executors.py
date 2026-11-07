# SPDX-License-Identifier: AGPL-3.0-only
"""#6091 : déclarations durables des exécutants externes non-agents.

Un système `system:<nom>` n'est pas un agent : la table ne porte que son
responsable humain et la trace de la déclaration. Le SQL reste ici, comme le
reste du stockage ; le métier (préfixe, responsable, autorité du geste) est
dans `ameesh.work`.
"""
from __future__ import annotations

from ..external_executors import ExternalExecutors as Contract

_COLUMNS = ("name, responsible, note, declared_by, declared_at,"
            " retired_at, retired_by")


class ExternalExecutors(Contract):
    def __init__(self, db):
        self.db = db

    def declare(self, name, responsible, *, note="", actor=""):
        # Une seule instruction : deux déclarations concurrentes du même nom
        # ne peuvent pas laisser un responsable différent de celui écrit. La
        # condition du DO UPDATE est relue sous le verrou de la ligne, donc
        # elle ne perd pas la course contre un retrait.
        rows = self.db.query(
            "INSERT INTO external_executors(name, responsible, note, declared_by)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET note = excluded.note"
            "  WHERE external_executors.responsible = excluded.responsible"
            "    AND external_executors.retired_at IS NULL"
            " RETURNING " + _COLUMNS,
            (name, responsible, note, actor))
        return rows[0] if rows else None

    def get(self, name):
        rows = self.db.query(
            "SELECT " + _COLUMNS + " FROM external_executors WHERE name = %s",
            (name,))
        return rows[0] if rows else None

    def rows(self, *, include_retired=False):
        sql = "SELECT " + _COLUMNS + " FROM external_executors"
        if not include_retired:
            sql += " WHERE retired_at IS NULL"
        return self.db.query(sql + " ORDER BY name")

    def retire(self, name, *, actor=""):
        rows = self.db.query(
            "UPDATE external_executors"
            "   SET retired_at = now(), retired_by = %s"
            " WHERE name = %s AND retired_at IS NULL"
            " RETURNING " + _COLUMNS,
            (actor, name))
        if rows:
            return rows[0]
        return self.get(name)
