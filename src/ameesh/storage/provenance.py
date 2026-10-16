# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — preuves exactes d'origine (commit, jalon) : contrat de stockage."""
from __future__ import annotations

import abc
from typing import Optional


class Provenance(abc.ABC):
    """La table en ajout seul `provenance_receipts` (migration 0445).

    Aucune opération ne met à jour ni ne supprime une ligne : une preuve se
    écrit une fois, à l'instant du fait. `add` rend toujours la ligne qui fait
    foi (celle qui vient d'être écrite, ou celle déjà présente) avec
    `recorded` : le domaine compare alors l'identité et refuse une divergence.
    """

    @abc.abstractmethod
    def add(self, subject: str, sha: str, actor: str, session: str, turn: str,
            harness: str, *, work_item_id: Optional[int] = None) -> dict:
        """Écrit la preuve de `subject`, ou rend celle déjà écrite.

        `subject` : `commit:<sha complet>` ou `milestone:<id>`. Rend
        `{"recorded": bool, **ligne}` — `recorded` vaut False quand la ligne
        existait déjà (la première écriture fait foi)."""

    @abc.abstractmethod
    def get(self, subject: str) -> Optional[dict]:
        """La preuve brute de `subject`, ou None si l'origine est inconnue."""

    @abc.abstractmethod
    def milestone(self, subject: str) -> Optional[dict]:
        """La preuve d'un jalon JOINte à son fait `work_item_milestones`.

        Rend None si l'une des deux lignes manque : une preuve sans fait (ou
        l'inverse) est une origine inconnue, pas une preuve partielle. La ligne
        porte `mark_actor`, `mark_sha`, `verdict`, `milestone_id`, `at_ts` —
        les faits recopiés du jalon — et les champs de la preuve."""
