# SPDX-License-Identifier: AGPL-3.0-only
"""#6091 (manaty/ameesh#4642, écart 4) : déclarations d'exécutants externes
non-agents (`system:…`) et de leur responsable humain.

Un système déclaré n'est PAS un agent : aucune ligne d'`agent_registry`, aucun
bail, aucun réveil, aucune attribution. Un lot le porte seulement comme
`external_executor` (#3447), et L165 ne le prend pas. Sa déclaration durable
porte le responsable humain, celui à qui l'attente et les alertes du lot ont
quelqu'un à qui aller.
"""
from __future__ import annotations

import abc


class ExternalExecutors(abc.ABC):
    @abc.abstractmethod
    def declare(self, name: str, responsible: str, *, note: str = "",
                actor: str = "") -> dict | None:
        """Déclare `system:<nom>` avec son `responsible` (`human:<id>`).

        Idempotent : une déclaration active du même nom et du même responsable
        est rendue telle quelle, sa note mise à jour. None si le nom est déjà
        déclaré avec un AUTRE responsable, ou s'il a été retiré : l'appelant
        relit et dit pourquoi (aucune écriture partielle)."""

    @abc.abstractmethod
    def get(self, name: str) -> dict | None:
        """La déclaration (active ou retirée), ou None si le nom est inconnu."""

    @abc.abstractmethod
    def rows(self, *, include_retired: bool = False) -> list[dict]:
        """Les déclarations, par nom ; les retirées seulement si demandé."""

    @abc.abstractmethod
    def retire(self, name: str, *, actor: str = "") -> dict | None:
        """Retire la déclaration ; idempotent (rend la ligne déjà retirée).

        None si le nom est inconnu. Aucun lot n'est modifié : une réservation
        existante garde le nom jusqu'à sa levée ou la fermeture du lot."""
