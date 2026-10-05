# SPDX-License-Identifier: AGPL-3.0-only
"""openai : la liste des modèles publiée par le fournisseur (L14, décision 0020).

Le transport vit dans `listing` et n'est **jamais** appelé par les tests ;
l'analyse vit dans `providers.parse_listing`, que les tests nourrissent d'une
réponse ENREGISTRÉE. La clé d'API vient de l'environnement (ou du trousseau de
l'hôte) et n'est journalisée nulle part — ni dans un événement, ni dans un fil,
ni dans un commit (décision 0014).
"""
from __future__ import annotations

import os

from . import http, providers
from .base import SourceResult

PROVIDER = "openai"
LIST_PATH = "/v1/models"
DEFAULT_BASE_URL = "https://api.openai.com"
KEY_VAR = "OPENAI_API_KEY"
#: En-têtes exigés par le fournisseur, hors authentification.
EXTRA_HEADERS = {}


def listing(*, token: str | None = None, base_url: str | None = None, timeout: float = 20.0, allow_http: bool = False) -> SourceResult:
    """La liste réelle du fournisseur : réseau, donc hors des tests."""
    key = token or os.environ.get(KEY_VAR) or ""
    if not key:
        return SourceResult(
            source=PROVIDER, complete=False, detail="aucune clé disponible : rien n'est retiré"
        )
    url = (base_url or os.environ.get(f"{KEY_VAR}_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + LIST_PATH
    # `http.fetch_json` refuse les redirections, exige HTTPS et vérifie l'origine :
    # une clé ne doit jamais suivre un rebondissement vers une autre origine (B1).
    try:
        payload, failure = http.fetch_json(
        url,
        headers={"Authorization": f"Bearer {key}", **EXTRA_HEADERS},
        timeout=timeout,
            allow_http=allow_http,
        )
    except (http.RedirectRefused, http.InsecureEndpoint, http.OriginChanged) as refused:
        # Une redirection (ou une origine qui change, ou un endpoint non HTTPS) est
        # une LISTE QU'ON N'A PAS — donc `complete=False`, et surtout pas une
        # exception qui interromprait la découverte.
        return SourceResult(
            source=PROVIDER,
            complete=False,
            detail=f"{type(refused).__name__} : lecture refusée, rien n'est retiré",
        )
    if failure:
        return SourceResult(
            source=PROVIDER,
            complete=False,
            detail=f"{failure} : lecture impossible, rien n'est retiré",
        )
    return providers.parse_listing(payload, provider=PROVIDER)


def parse_listing(payload, **kwargs):
    """Le parseur partagé, avec le nom de CE fournisseur.

    Sans ce passage explicite, la source écrirait des lignes sans fournisseur : le
    test TestProviderListingTest.test_anthropic_reads_the_models_it_publishes l'a
    attrapé avant la revue.
    """
    return providers.parse_listing(payload, provider=PROVIDER, **kwargs)
