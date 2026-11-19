# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 : les colonnes d'agent des tables du module `dev`.

`ameesh agent rename` réécrit une identité dans les colonnes qui la portent
(contrat #4299). Les tables du module `dev` ne sont pas dans le schéma noyau
après P2 (#7651) : leur nom physique est lu au registre (`schema.relation`),
donc les deux lectures (`uses`, `rewrite`) passent par ce domaine.
"""
from __future__ import annotations

from .schema import DevDomain, relation


class AgentColumns(DevDomain):
    """Lecture et réécriture d'une colonne d'identité dans une table `dev`."""

    def uses(self, table, column, is_array, name) -> int:
        """Le nombre de lignes de `table` qui portent encore `name`."""
        predicate = "%s = ANY(%s)" if is_array else "%s = %s"
        rows = self.db.query(
            "SELECT count(*) AS n FROM %s WHERE %s"
            % (relation(self.db, table), predicate % ("%s", column)),
            (name,))
        return int(rows[0]["n"])

    def rewrite(self, table, column, is_array, old, new) -> int:
        """Réécrit `old` en `new` dans `column`, et rend le nombre de lignes.

        Le comptage précède la réécriture dans la même transaction : le pilote
        psql n'expose pas le nombre de lignes d'un `execute()`, et l'UPDATE ne
        peut pas être enveloppé dans un CTE (le pilote en ajoute un).
        """
        moved = self.uses(table, column, is_array, old)
        target = relation(self.db, table)
        if is_array:
            self.db.execute(
                "UPDATE %s SET %s = array_replace(%s, %%s, %%s)"
                " WHERE %%s = ANY(%s)" % (target, column, column, column),
                (old, new, old))
        else:
            self.db.execute(
                "UPDATE %s SET %s = %%s WHERE %s = %%s" % (target, column, column),
                (new, old))
        return moved
