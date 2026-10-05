# SPDX-License-Identifier: AGPL-3.0-only
"""Une source de découverte : ce qu'elle a vu, et si elle a tout vu.

La règle qui compte est dans `SourceResult.complete` : un catalogue ne peut
retirer un modèle que sur une liste **complète et réussie**. Une réponse
partielle (pagination non suivie), une erreur réseau ou une réponse illisible
donnent `complete=False` — elles ne rafraîchissent pas, elles ne retirent pas.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json


@dataclasses.dataclass(frozen=True)
class ModelSeen:
    """Un modèle tel qu'une source le décrit, avant toute écriture en base."""

    provider: str
    model_id: str
    context_window: int | None = None
    price_input: float | None = None
    price_cached: float | None = None
    price_output: float | None = None
    source: str = ""


@dataclasses.dataclass(frozen=True)
class HarnessSeen:
    """Un couple modèle × harnais, avec les efforts que le harnais accepte."""

    provider: str
    model_id: str
    harness: str
    efforts: tuple[str, ...] = ()
    source: str = "harness"


@dataclasses.dataclass(frozen=True)
class SourceResult:
    """Ce qu'une source rapporte, et si son rapport est complet.

    `models` et `harnesses` sont ce qu'elle a vu ; `complete` est la seule chose
    qui autorise un retrait. `detail` sert au message d'événement, jamais à un
    secret : une source n'écrit ni clé, ni en-tête d'authentification.
    """

    source: str
    complete: bool
    models: tuple[ModelSeen, ...] = ()
    harnesses: tuple[HarnessSeen, ...] = ()
    raw: bytes = b""
    detail: str = ""
    #: `presence` : la source dit quels modèles existent CHEZ ELLE (les trois
    #: fournisseurs). `local` : elle apporte des prix ou des options, elle ne dit
    #: rien de la présence — un barème rafraîchi n'est pas une réapparition, et il
    #: ne retire jamais rien (B2).
    kind: str = "presence"

    @property
    def raw_digest(self) -> str:
        """L'empreinte de la réponse brute — la réponse elle-même n'est pas gardée."""
        return hashlib.sha256(self.raw).hexdigest() if self.raw else ""


def digest_of(payload: object) -> str:
    """Empreinte stable d'une charge utile déjà analysée (tests et sources locales)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
