# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : profil de placement (C4, 0022).

Le profil d'un placement est le texte de la fonction SQL
`ameesh_placement_profile` sur les colonnes du registre qui le forment ;
`placement_profile` (le profil ÉVALUÉ, écrit avec le verdict) lui est
comparé par la condition de réclamation. Expressions déplacées telles
quelles depuis `ameesh.placement` (lot L1), avec la lecture des verdicts
écrits au registre (`Placements.recorded`).
"""
from __future__ import annotations

from .. import interface

#: colonnes du registre qui forment le profil d'un placement (0022), dans
#: l'ordre des arguments de la fonction SQL `ameesh_placement_profile`
PROFILE_COLUMNS = ("host", "harness", "provider", "model", "credential_mode")


def profile_sql(alias: str | None = None) -> str:
    """Expression SQL : profil COURANT de la ligne (`ameesh_placement_profile`
    de ses colonnes), à comparer à `placement_profile`, le profil évalué."""
    prefix = "%s." % alias if alias else ""
    return "ameesh_placement_profile(%s)" % ", ".join(prefix + c for c in PROFILE_COLUMNS)


#: profil passé en paramètres (`%s` liés, dans l'ordre de PROFILE_COLUMNS)
PROFILE_PARAMS_SQL = "ameesh_placement_profile(%s)" % ", ".join(
    ["%s::text"] * len(PROFILE_COLUMNS))


def profile_params(values: dict) -> tuple:
    """Les paramètres de PROFILE_PARAMS_SQL pour un mapping de colonnes."""
    return tuple(values.get(c) for c in PROFILE_COLUMNS)


class Placements(interface.Placements):

    def recorded(self, host) -> list[dict]:
        sql = ("SELECT name, host, canon, placement_ok, placement_diagnostic, placement_ref, "
               "placement_profile, %s AS placement_profile_current FROM agent_registry"
               % profile_sql())
        params: tuple = ()
        if host is not None:
            sql += " WHERE host = %s"
            params = (host,)
        return self.db.query(sql, params)
