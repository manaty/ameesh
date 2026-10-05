# SPDX-License-Identifier: AGPL-3.0-only
"""Le transport des sources de découverte, écrit pour ne pas perdre une clé.

Une clé d'API dans un en-tête `Authorization` est un secret qui voyage : si la
requête suit une redirection, le client la renvoie **telle quelle** à la seconde
origine. Une revue indépendante l'a reproduit sur les trois fournisseurs (302 de
127.0.0.1 vers localhost, même clé factice reçue) : c'est une fuite, pas une
hypothèse.

Trois règles, donc, et elles sont ici plutôt que dans chaque source :

1. **aucune redirection suivie** — une réponse 3xx est une erreur, jamais un
   rebondissement avec la clé ;
2. **HTTPS obligatoire** pour tout endpoint authentifié (l'`http://` est refusé
   sauf pour un test local qui le demande explicitement) ;
3. **même origine** : l'URL résolue doit garder le schéma, l'hôte et le port du
   départ, sinon la réponse est refusée même sans redirection.

Les erreurs rendues sont des **noms** de classe, jamais le texte de l'exception,
qui peut citer l'URL ou l'en-tête.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class RedirectRefused(RuntimeError):
    """La réponse était une redirection : rien n'est suivi, rien n'est renvoyé."""


class InsecureEndpoint(RuntimeError):
    """Un endpoint authentifié doit être en HTTPS (hors test local explicite)."""


class OriginChanged(RuntimeError):
    """L'URL résolue ne garde pas le schéma, l'hôte et le port du départ."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse toute redirection : `redirect_request` ne rend rien, donc 3xx final."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise RedirectRefused(f"redirection {code} refusée")


def opener() -> urllib.request.OpenerDirector:
    """Un ouvreur LOCAL qui refuse toute redirection, construit à chaque appel.

    Jamais `install_opener` ni `urllib.request._opener` : l'ancienne version
    remplaçait l'ouvreur GLOBAL le temps d'un appel puis le restaurait, ce qui
    n'est pas sûr entre fils (un fil pouvait restaurer l'ouvreur par défaut
    pendant qu'un autre l'utilisait, et suivre une redirection avec la clé —
    revue L26). Ici rien de global n'est touché : l'ouvreur est propre à
    l'appel et utilisé par `.open()` directement.
    """
    return urllib.request.build_opener(_NoRedirect)


def _same_origin(first: str, second: str) -> bool:
    a, b = urllib.parse.urlsplit(first), urllib.parse.urlsplit(second)
    return (a.scheme, a.hostname, a.port or 443) == (b.scheme, b.hostname, b.port or 443)


def fetch_json(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 20.0,
    allow_http: bool = False,
    open_with: urllib.request.OpenerDirector | None = None,
) -> tuple[object | None, str]:
    """Rend `(charge utile, "")` ou `(None, nom de l'erreur)` — jamais un secret.

    `open_with` : ouvreur injectable (tests) ; par défaut `opener()`, local à
    l'appel et sans redirection."""
    parts = urllib.parse.urlsplit(url)
    loopback = parts.hostname in ("127.0.0.1", "::1", "localhost")
    if parts.scheme != "https" and not (allow_http or loopback):
        raise InsecureEndpoint(f"endpoint non HTTPS refusé : {parts.scheme or 'sans schéma'}")
    if not parts.hostname:
        raise InsecureEndpoint("endpoint sans hôte")
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with (open_with or opener()).open(request, timeout=timeout) as answer:  # noqa: S310
            # Une réponse peut ne pas porter d'URL (les transports de test en
            # fournissent un minimal) : l'absence n'est pas un changement d'origine.
            final = getattr(answer, "geturl", None)
            final = final() if callable(final) else final
            if final and not _same_origin(url, final):
                raise OriginChanged("la réponse ne vient pas de l'origine demandée")
            body = answer.read().decode("utf-8")
    except RedirectRefused:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as error:
        return None, type(error).__name__
    try:
        return json.loads(body), ""
    except ValueError:
        return None, "JSONDecodeError"
