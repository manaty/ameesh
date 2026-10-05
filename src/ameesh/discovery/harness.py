# SPDX-License-Identifier: AGPL-3.0-only
"""Source `harness` : ce que les harnais du poste exposent SANS appel payant.

`adapters.SPECS` dit comment chaque harnais accepte un modèle et un effort
(`--model`, `-m`, `model_reasoning_effort=…`). Aucun harnais ne publie la liste
des modèles qu'il accepte sans être appelé, et un appel serait payant : cette
source ne prétend donc **pas** lister de modèles.

Conséquence voulue, et c'est la règle de retrait : une source qui ne liste pas ne
retire jamais rien. Elle rend `complete=True` parce qu'elle a fini son travail,
pas parce qu'elle aurait vu une liste vide.
"""
from __future__ import annotations

from .. import adapters
from .base import SourceResult


def listing() -> SourceResult:
    """Les harnais connus et leurs drapeaux, sans aucun modèle prétendu."""
    lines = []
    for name in sorted(adapters.SPECS):
        spec = adapters.SPECS[name]
        lines.append(f"{name}: model_flags={list(spec.model_flags)} effort_flags={list(spec.effort_flags)}")
    raw = "\n".join(lines).encode("utf-8")
    return SourceResult(
        source="harness",
        complete=True,
        models=(),
        raw=raw,
        kind="local",
        detail="les harnais exposent des drapeaux, pas des listes de modèles : rien à retirer",
    )
