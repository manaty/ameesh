# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — preuves exactes d'origine (commit, jalon) : contrat de stockage."""
from __future__ import annotations

import abc
from typing import Optional, Sequence


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
        les faits recopiés du jalon —, les champs de la preuve et
        `same_identity` : les deux acteurs désignent la même fiche du registre
        (nom ou alias, #4299 : un renommage réécrit le jalon, pas la preuve)."""

    @abc.abstractmethod
    def commits(self, actor: str, *, since_ts: Optional[float] = None,
                until_ts: Optional[float] = None,
                limit: Optional[int] = None) -> list[dict]:
        """Les preuves de commits écrites par `actor`, les plus récentes d'abord.

        `actor` vaut pour toute orthographe de sa fiche du registre (nom ou
        alias, #4299 : un renommage réécrit le jalon, pas la preuve).

        Un reçu de commit ne porte pas toujours le lot (`work_item_id` est
        NULL quand le hook post-commit ne le connaît pas). Cette lecture rend
        les SHA que l'acteur a réellement créés, pour que la désignation
        retrouve un correctif de relecteur **retiré** de la tête (#4457) sans
        se fier à une note. `since_ts`/`until_ts` bornent par l'instant
        d'écriture du reçu (epoch). **Sans `limit`**, la fenêtre est lue
        exhaustivement : une troncature silencieuse ferait disparaître le
        correctif sous des reçus plus récents (B/P2 cx3). Une ligne par
        `subject`, sans `recorded`."""

    @abc.abstractmethod
    def origins(self, commits: Sequence[str]) -> list[int]:
        """Les lots que des jalons de gel/verdict rattachent à ces commits.

        Lecture exacte, sans deviner : le reçu doit nommer l'un de ces commits
        (sha complet) et son fait `work_item_milestones` doit exister ; le lot
        rendu est celui qui a déclaré le jalon. Identifiants distincts et
        triés ; un reçu sans fait n'est pas une origine."""
