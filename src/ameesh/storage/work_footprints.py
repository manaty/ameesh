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

    # #4818 (manaty/ameesh#3643) : plafond, politiques et analyses d'impact

    @abc.abstractmethod
    def cap_reached(self, item_id: int) -> dict | None:
        """`{cap, in_flight}` si le dépôt du lot a atteint son plafond de lots
        en vol (le lot lui-même non compté), sinon None."""

    @abc.abstractmethod
    def policies(self, apps: Sequence[str] | None = None) -> dict:
        """`{app: {app, unknown_policy, max_in_flight, shared_paths,
        updated_by, updated_ts}}` des dépôts qui ont une politique posée."""

    @abc.abstractmethod
    def set_policy(self, app: str, *, actor: str, unknown_policy: str | None = None,
                   max_in_flight: int | None = None, clear_cap: bool = False,
                   shared_paths: Sequence[str] | None = None) -> dict:
        """Pose la politique du dépôt sous le verrou de la garde ; un champ
        absent garde sa valeur. Rend `{app, before, after, changed}`."""

    @abc.abstractmethod
    def record_analysis(self, item_id: int, analysis: dict, *, actor: str) -> dict | None:
        """Écrit la dernière analyse d'impact du lot et sa ligne de journal
        (`analysis["note"]`) dans une transaction. None si le lot n'existe pas."""

    @abc.abstractmethod
    def analyses(self, item_ids: Sequence[int]) -> dict:
        """`{lot: analyse}` (status, reason, paths, weak, policy, elapsed_ms,
        git_calls, attempts, analyzed_by, analyzed_ts)."""

    @abc.abstractmethod
    def claim_unknown(self, *, retry_after_s: float, limit: int,
                      apps: Sequence[str] | None = None) -> list[dict]:
        """Prend (en réécrivant `analyzed_at`) au plus `limit` lots `intake`
        dont la dernière analyse est `unknown` depuis `retry_after_s` : deux
        exécuteurs ne les réanalysent pas ensemble. Rend `[{work_item_id}]`."""

    @abc.abstractmethod
    def unknown_apps(self) -> list[str]:
        """Les dépôts qui ont un lot `intake` à l'analyse inconnue."""

    @abc.abstractmethod
    def refresh_candidates(self, *, limit: int,
                           assignees: Sequence[str] | None = None) -> list[dict]:
        """Les lots occupants qui portent une branche (de ces assignés, avec
        `assignees`) : `id, app, branch, branch_target, assignee, state,
        diff_head, has_diff`."""
