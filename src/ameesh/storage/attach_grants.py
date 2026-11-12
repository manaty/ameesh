# SPDX-License-Identifier: AGPL-3.0-only
"""#7119 (avatar D1) — journal des grants d'attach : contrat de stockage."""
from __future__ import annotations

import abc
from typing import Optional


class AttachGrants(abc.ABC):
    """La table en ajout seul `attach_grant_events` (migration 0574).

    Une ligne par issue de vérification : `granted` (le grant accepté, une
    seule fois par `attach_id`) ou `refused` (avec son code). Aucune
    opération ne met à jour ni ne supprime une ligne.
    """

    @abc.abstractmethod
    def record(self, *, outcome: str, code: str, reason: str, human: str, agent: str,
               host: str, tenant: str, device_key: str, attach_id: str, nonce: str,
               exp: Optional[int], checked_by: str) -> Optional[int]:
        """Écrit une issue ; rend son id. Pour `granted`, None si une ligne
        `granted` existe déjà pour cet `attach_id` (rien n'est écrit)."""

    @abc.abstractmethod
    def recent(self, *, agent: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Les dernières issues (toutes, ou d'un agent), de la plus récente à
        la plus ancienne ; `at_ts` en secondes Unix."""
