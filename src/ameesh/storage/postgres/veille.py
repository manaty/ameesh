# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : le relevé précédent de la veille (migration 0512).

Deux opérations seulement : ce que la veille a déjà vu pour un harnais, et
l'enregistrement du relevé courant. Le catalogue partagé n'est pas lu ici :
`models discover` y écrit avant la veille, et s'en servir comme relevé
perdrait le modèle nouveau (B de l'auditeur, #4291).
"""
from __future__ import annotations

from typing import Sequence

from .. import interface


class Veille(interface.Veille):

    def readings(self, harness: str, provider: str) -> list[str]:
        rows = self.db.query(
            "select model_id from veille_readings"
            " where harness = %s and provider = %s"
            " order by model_id", (harness, provider))
        return [str(row["model_id"]) for row in rows]

    def record_readings(self, harness: str, provider: str,
                        model_ids: Sequence[str]) -> int:
        count = 0
        for model_id in model_ids:
            self.db.execute(
                "insert into veille_readings (harness, provider, model_id)"
                " values (%s, %s, %s)"
                " on conflict (harness, provider, model_id)"
                " do update set last_seen = now()",
                (harness, provider, model_id))
            count += 1
        return count
