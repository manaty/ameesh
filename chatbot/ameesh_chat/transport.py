# SPDX-License-Identifier: AGPL-3.0-only
"""Transport HTTPS de la fonction : aucune redirection, même origine, HTTPS.

Repris du transport des sources de découverte d'ameesh
(`src/ameesh/discovery/http.py`), sans en dépendre : la clé du fournisseur de
modèle et la signature du stockage voyagent dans les en-têtes ; une redirection
suivie les renverrait telles quelles à une autre origine.

1. **aucune redirection suivie** — une réponse 3xx est une erreur ;
2. **HTTPS obligatoire** (le bouclage local n'est admis que pour les tests) ;
3. **même origine** pour l'URL résolue ;
4. ouvreur **local** à l'appel, jamais `install_opener` ;
5. les erreurs rendues sont des **noms** de classe ou des codes, jamais le texte
   d'une exception ou d'une réponse (qui pourrait citer une question).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

#: taille maximale lue dans une réponse : rien ne justifie davantage ici
MAX_RESPONSE_BYTES = 2_000_000
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class TransportError(RuntimeError):
    """Échec de transport ; `code` est sûr à journaliser (pas de contenu)."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


class RedirectRefused(TransportError):
    """La réponse était une redirection : rien n'est suivi."""

    def __init__(self, status: int | None = None):
        super().__init__("RedirectRefused", status)


class InsecureEndpoint(TransportError):
    """Endpoint non HTTPS (hors bouclage local de test)."""

    def __init__(self):
        super().__init__("InsecureEndpoint")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise RedirectRefused(code)


def opener() -> urllib.request.OpenerDirector:
    """Ouvreur propre à l'appel, qui refuse toute redirection."""
    return urllib.request.build_opener(_NoRedirect)


def check_endpoint(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        raise InsecureEndpoint()
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOOPBACK):
        raise InsecureEndpoint()
    return parts


def _same_origin(first: str, second: str) -> bool:
    a, b = urllib.parse.urlsplit(first), urllib.parse.urlsplit(second)
    default = {"https": 443, "http": 80}
    return (a.scheme, a.hostname, a.port or default.get(a.scheme)) == (
        b.scheme, b.hostname, b.port or default.get(b.scheme))


def request(method: str, url: str, **kwargs) -> tuple[int, bytes]:
    """Rend `(statut, corps)` ; voir `request_full`."""
    status, body, _headers = request_full(method, url, **kwargs)
    return status, body


def request_full(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
    open_with: urllib.request.OpenerDirector | None = None,
) -> tuple[int, bytes, dict]:
    """Rend `(statut, corps, en-têtes en minuscules)` pour toute réponse non 3xx ;
    lève `TransportError`.

    Un statut 4xx/5xx est rendu (le corps est lu mais jamais journalisé par
    l'appelant) ; seuls les échecs de transport lèvent."""
    check_endpoint(url)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with (open_with or opener()).open(req, timeout=timeout) as answer:  # noqa: S310
            final = getattr(answer, "geturl", None)
            final = final() if callable(final) else final
            if final and not _same_origin(url, final):
                raise TransportError("OriginChanged")
            status = getattr(answer, "status", None) or answer.getcode()
            if 300 <= status < 400:
                raise RedirectRefused(status)
            headers_out = {k.lower(): v for k, v in answer.headers.items()} if getattr(answer, "headers", None) else {}
            return status, answer.read(MAX_RESPONSE_BYTES), headers_out
    except TransportError:
        raise
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise RedirectRefused(error.code) from None
        try:
            data = error.read(MAX_RESPONSE_BYTES)
        except OSError:
            data = b""
        finally:
            error.close()
        headers_out = {k.lower(): v for k, v in error.headers.items()} if error.headers else {}
        return error.code, data, headers_out
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise TransportError(type(error).__name__) from None


def post_json(url: str, payload: dict, *, headers: dict | None = None, timeout: float = 25.0,
              open_with=None) -> tuple[int, object | None]:
    """POST JSON ; rend `(statut, objet JSON ou None)`."""
    all_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    all_headers.update(headers or {})
    status, data = request("POST", url, headers=all_headers,
                           body=json.dumps(payload).encode("utf-8"),
                           timeout=timeout, open_with=open_with)
    try:
        return status, json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None
