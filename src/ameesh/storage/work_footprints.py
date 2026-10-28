# SPDX-License-Identifier: AGPL-3.0-only
"""#4173 (manaty/ameesh#3185, volet 3) : l'empreinte d'un lot (fichiers ou
modules qu'il touche) et ses chevauchements dans un même dépôt."""
from __future__ import annotations

import abc
from typing import Sequence


class WorkFootprints(abc.ABC):
    @abc.abstractmethod
    def replace(self, item_id: int, source: str, paths: Sequence[str], *,
                actor: str, head: str | None = None,
                replaces: Sequence[str] = ()) -> dict | None:
        """Remplace, en une transaction, les lignes `source` (et `replaces`)
        du lot par `paths` (normalisés par l'appelant), et journalise une
        ligne si l'empreinte change. None si le lot n'existe pas.

        Rend `{paths, changed}` : l'empreinte entière (toutes sources) après
        l'écriture, et si elle a changé."""

    @abc.abstractmethod
    def for_items(self, item_ids: Sequence[int]) -> list[dict]:
        """Les lignes d'empreinte de ces lots : `work_item_id`, `source`,
        `path`, `head`, `created_by`, `created_ts`."""

    @abc.abstractmethod
    def overlapping(self, item_id: int) -> list[dict]:
        """Les lots du même dépôt (`app`) qui OCCUPENT leur empreinte (ouverts,
        assignés ou démarrés) et la chevauchent avec celle de `item_id` : une
        ligne par paire de chemins, `id`, `state`, `assignee`, `path` (le
        chemin de `item_id`), `other_path`. Même prédicat que la prise."""
