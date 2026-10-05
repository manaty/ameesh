# SPDX-License-Identifier: AGPL-3.0-only
"""Sources `anthropic`, `openai`, `deepseek` : les endpoints de LISTE seulement.

Aucun de ces modules n'appelle une complétion : ce sont des listes de modèles,
donc un coût nul en tokens (décision 0020). L'analyseur est séparé du transport
pour que les tests lui donnent une réponse ENREGISTRÉE, sans réseau.

Deux choses se perdent facilement et sont donc explicites :

* **la pagination** : une première page qui annonce une suite n'est pas une liste
  complète, donc `complete=False` — et une liste incomplète ne retire rien ;
* **l'erreur** : un statut non 2xx ou un corps illisible donnent `complete=False`
  aussi, jamais une liste vide qu'on prendrait pour « tout a disparu ».
"""
from __future__ import annotations

import json
from typing import Any

from .base import ModelSeen, SourceResult

PROVIDER = ""
LIST_PATH = ""
#: Les clés qui annoncent une page suivante. `has_more` est LE signal des API qui
#: le publient ; `next_page`/`next` en tiennent lieu ailleurs. **`last_id` n'en est
#: pas un** : c'est un curseur, et une dernière page peut le porter sans qu'il reste
#: quoi que ce soit — la revue a vu une vraie dernière page jugée incomplète.
NEXT_KEYS = ("has_more", "next_page", "next")


def _entries(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", payload.get("models", []))
    elif isinstance(payload, list):
        data = payload
    else:
        data = []
    return [entry for entry in data if isinstance(entry, dict)]


def _has_more(payload: Any) -> bool:
    """Vrai si la réponse dit qu'il reste des pages (donc liste incomplète)."""
    if not isinstance(payload, dict):
        return False
    for key in NEXT_KEYS:
        if key == "has_more" and payload.get(key) is True:
            return True
        if key in ("next_page", "next", "last_id") and payload.get(key):
            return True
    return False


def parse_listing(
    payload: Any,
    *,
    provider: str = "",
    status_ok: bool = True,
    pagination_followed: bool = False,
) -> SourceResult:
    """Analyse une réponse enregistrée (ou réelle) d'un endpoint de liste.

    `provider` est un **paramètre**, pas un état de module : un parseur partagé qui
    ne connaît pas son fournisseur écrirait des lignes sans fournisseur dans le
    catalogue, ce qu'un test a attrapé ici avant la revue.
    """
    raw = json.dumps(payload, sort_keys=True).encode("utf-8") if payload is not None else b""
    if not status_ok:
        return SourceResult(
            source=provider,
            complete=False,
            raw=raw,
            detail="réponse en erreur : rien n'est retiré",
        )
    models = []
    for entry in _entries(payload):
        model_id = entry.get("id") or entry.get("name") or entry.get("model")
        if not model_id:
            continue
        context = entry.get("context_window") or entry.get("max_input_tokens") or entry.get("context_length")
        models.append(
            ModelSeen(
                provider=provider,
                model_id=str(model_id),
                context_window=int(context) if isinstance(context, (int, float)) else None,
                source=provider,
            )
        )
    complete = True
    detail = ""
    if _has_more(payload) and not pagination_followed:
        complete = False
        detail = "pagination non suivie : liste partielle, rien n'est retiré"
    return SourceResult(
        source=provider, complete=complete, models=tuple(models), raw=raw, detail=detail
    )
