# SPDX-License-Identifier: AGPL-3.0-only
"""Les sources de découverte de L14 (décision 0020).

Chaque source est un petit module remplaçable : `prices` et `harness` lisent ce
que le poste sait déjà (barème, options des harnais), les trois autres lisent les
**endpoints de liste** des fournisseurs — jamais une complétion, donc aucun coût
en tokens. Les tests n'appellent pas le réseau : ils donnent à chaque analyseur
une réponse ENREGISTRÉE.
"""
from __future__ import annotations

from .base import HarnessSeen, ModelSeen, SourceResult, digest_of

#: Le vocabulaire exposé par `--source` (validé par mesh-design).
SOURCES = ("anthropic", "openai", "deepseek", "harness", "prices")

__all__ = ["HarnessSeen", "ModelSeen", "SourceResult", "digest_of", "SOURCES"]
