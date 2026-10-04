# SPDX-License-Identifier: AGPL-3.0-only
"""Client de l'API de service d'ameesh-approve (spec §9), côté ameesh.

    POST /requests        dépose une demande d'approbation ; rend le lien à
                          usage unique à ouvrir sur le téléphone
    GET  /receipts/<id>   le reçu signé (200), en attente (202), échu (410)

Configuration : `AMEESH_APPROVE_URL` (`approve_url`) et
`AMEESH_APPROVE_TOKEN_FILE` (`approve_token_file`). Le jeton de service
permet de DEMANDER une approbation, jamais d'en signer une ; il est lu dans
un fichier régulier de l'utilisateur, en `0600` (même contrôle que le
service : `approve.config.read_service_token`).

Le jeton ne quitte ce module que dans l'en-tête `Authorization` d'une
requête vers l'URL configurée — jamais affiché, journalisé, écrit dans un
fil ni dans un message d'erreur. Pour qu'il n'aille nulle part ailleurs :

* l'URL est `https://…`, ou `http://` sur la boucle locale seulement
  (127.0.0.1, ::1, localhost : ameesh-approve n'écoute que là) ; ni
  identifiants, ni requête, ni fragment ;
* aucun mandataire (les variables `*_proxy` sont ignorées) ;
* aucune redirection suivie (elle emporterait l'en-tête vers une autre
  adresse) : une réponse 3xx est une erreur.

Le reçu récupéré n'est pas cru sur parole : l'appelant le VÉRIFIE lui-même
(`receipts.verify_receipt`) avant de l'attacher à l'action.
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .config import Config

REQUEST_ID_RE = re.compile(r"^req_[0-9a-z]{26}$")
TIMEOUT = 15.0
MAX_RESPONSE = 1024 * 1024
_LOOPBACK_NAMES = ("localhost",)


class ApproveClientError(RuntimeError):
    """Refus ou panne d'ameesh-approve — code stable, message lisible, sans secret."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint(url: str) -> str:
    """L'URL de base de l'API, contrôlée ; sans `/` final."""
    text = (url or "").strip()
    if not text:
        raise ApproveClientError(
            "config", "ameesh-approve non configuré : AMEESH_APPROVE_URL (et "
            "AMEESH_APPROVE_TOKEN_FILE, le jeton de service en 0600)")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - lève ValueError sur un port illisible
    except ValueError as exc:
        raise ApproveClientError("config", "AMEESH_APPROVE_URL illisible : %s" % exc) from exc
    if parts.scheme not in ("https", "http") or not host:
        raise ApproveClientError("config", "AMEESH_APPROVE_URL : https://hôte[:port] attendu")
    if parts.username is not None or parts.password is not None or parts.query \
            or parts.fragment:
        raise ApproveClientError("config", "AMEESH_APPROVE_URL : ni identifiants, ni requête, "
                                           "ni fragment")
    if parts.scheme == "http" and not _loopback(host):
        raise ApproveClientError(
            "config", "AMEESH_APPROVE_URL : http seulement sur la boucle locale (127.0.0.1, "
                      "::1, localhost) ; sinon https — le jeton de service ne circule jamais "
                      "en clair")
    return text.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None     # 3xx → HTTPError : l'en-tête Authorization ne suit jamais


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _token(cfg: Config) -> str:
    from .approve.config import ApproveConfigError, read_service_token
    if not cfg.approve_token_file:
        raise ApproveClientError("config", "AMEESH_APPROVE_TOKEN_FILE non configuré (fichier "
                                           "0600 du jeton de service d'ameesh-approve)")
    try:
        return read_service_token(cfg.approve_token_file)
    except ApproveConfigError as exc:
        raise ApproveClientError("config", str(exc)) from exc


def _call(cfg: Config, method: str, path: str, body: dict | None = None
          ) -> tuple[int, bytes]:
    url = endpoint(cfg.approve_url) + path
    token = _token(cfg)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener().open(request, timeout=TIMEOUT) as response:
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise ApproveClientError("too_large", "réponse d'ameesh-approve trop grosse",
                                         response.status)
            return response.status, payload
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_RESPONSE)
        except OSError:
            payload = b""
        finally:
            exc.close()
        if 300 <= exc.code < 400:
            raise ApproveClientError("redirect", "ameesh-approve répond par une redirection "
                                                 "(HTTP %d) : refusée" % exc.code, exc.code)
        return exc.code, payload
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ApproveClientError("unreachable", "ameesh-approve injoignable (%s)"
                                 % " ".join(str(reason).split())[:200]) from exc


def _json(payload: bytes) -> dict:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _refusal(status: int, payload: bytes) -> ApproveClientError:
    data = _json(payload)
    code = str(data.get("error") or "http_%d" % status)[:64]
    message = " ".join(str(data.get("message") or "HTTP %d" % status).split())[:300]
    return ApproveClientError(code, message, status)


def create_request(cfg: Config, *, action_id: str, approver: str, requested_by: str,
                   assume_duplicate: bool = False) -> dict:
    """`POST /requests` : la réponse du service (request_id, link, digest…)."""
    status, payload = _call(cfg, "POST", "/requests", {
        "action_id": action_id, "approver": approver, "requested_by": requested_by,
        "assume_duplicate": bool(assume_duplicate)})
    if status != 201:
        raise _refusal(status, payload)
    data = _json(payload)
    if not REQUEST_ID_RE.fullmatch(str(data.get("request_id") or "")) \
            or not isinstance(data.get("link"), str):
        raise ApproveClientError("format", "réponse d'ameesh-approve illisible", status)
    return data


def fetch_receipt(cfg: Config, request_id: str) -> tuple[str, bytes | dict]:
    """`GET /receipts/<id>` : ("signed", octets du reçu) | ("pending", infos) |
    ("expired", infos). Toute autre réponse : ApproveClientError."""
    if not REQUEST_ID_RE.fullmatch(request_id or ""):
        raise ApproveClientError("format", "identifiant de demande « req_… » attendu")
    status, payload = _call(cfg, "GET", "/receipts/" + request_id)
    if status == 200:
        return "signed", payload
    if status == 202:
        return "pending", _json(payload)
    if status == 410:
        return "expired", _json(payload)
    raise _refusal(status, payload)
