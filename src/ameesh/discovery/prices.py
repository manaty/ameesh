# SPDX-License-Identifier: AGPL-3.0-only
"""Source `prices` : le barème local (`$AMEESH_PRICES`, sinon `DEFAULT_PRICES`).

C'est la seule source dont le vocabulaire est **local** : le barème est un
dictionnaire modèle → triplet, sans fournisseur. On le rattache au fournisseur
quand le préfixe le dit (`PRICE_FAMILY`), et à `local` sinon — un modèle inconnu
n'est pas une erreur ici, c'est une information manquante.

Cette source ne retire jamais rien : elle ne liste pas des modèles, elle donne
des prix.
"""
from __future__ import annotations

from .. import cost
from .base import ModelSeen, SourceResult

PROVIDERS_BY_PREFIX = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("codex-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("deepseek-", "deepseek"),
)


def provider_for(model_id: str) -> str:
    """Le fournisseur que le préfixe du modèle annonce, `local` s'il ne dit rien."""
    lowered = model_id.lower()
    for prefix, provider in PROVIDERS_BY_PREFIX:
        if lowered.startswith(prefix):
            return provider
    return "local"


def listing(path: str | None = None) -> SourceResult:
    """Le contenu du barème, un `ModelSeen` par entrée."""
    prices = cost.load_prices(path)
    models = []
    for model_id, triplet in sorted(prices.items()):
        if not isinstance(triplet, (list, tuple)) or len(triplet) < 3:
            continue
        models.append(
            ModelSeen(
                provider=provider_for(model_id),
                model_id=model_id,
                price_input=float(triplet[0]),
                price_cached=float(triplet[1]),
                price_output=float(triplet[2]),
                source="prices",
            )
        )
    raw = repr(sorted(prices.items())).encode("utf-8")
    return SourceResult(source="prices", complete=True, models=tuple(models), raw=raw, kind="local")
