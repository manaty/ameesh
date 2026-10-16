# SPDX-License-Identifier: AGPL-3.0-only
"""Logique d'ameesh-approve, indépendante de HTTP (spec §9, C8).

* `create_request` : ameesh demande une approbation pour `action_id` ; le
  service relit l'action (ActionSource), recalcule son empreinte, rend SON
  résumé, construit la `request` (nonce 128 bits, iat, exp ≤ 15 min,
  summary_digest) et crée un lien à usage unique (jeton de 256 bits, stocké
  haché). Tout résumé ou texte fourni par le demandeur est ignoré. Avec
  `assume_duplicate: true`, la demande n'est pas une approbation ordinaire :
  c'est la décision qui ASSUME LE DOUBLON d'une action `unknown` (empreinte
  de duplicate.py, résumé et page distincts) ;
* `link_view` / `submit` : la page du lien, puis l'assertion WebAuthn ; le
  reçu `ameesh-receipt/1` est construit, VÉRIFIÉ par receipts.py (sans
  consommer le nonce : ameesh le consomme à l'exécution), puis enregistré —
  l'enregistrement consomme le lien.
* `receipt` : ameesh récupère le reçu et le vérifie lui-même.
* `create_login_request` / `login_view` / `login_submit` / `login_receipt`
  (L232d, lot #366) : DÉFI DE CONNEXION, genre distinct. La demande signable
  ne porte que l'hôte, le fournisseur, le compte, la portée et l'échéance ;
  l'URL d'autorisation et le code à usage unique vivent dans l'état privé
  (0600) et ne sortent qu'après une assertion WebAuthn (`view`). Le reçu
  `done` | `cancel` ne contient ni code ni condensat, et il ne prouve pas le
  grant fournisseur : la santé se vérifie séparément.
* L44 (0031) : l'action relue porte son canon (`actions.canon`) ; les
  credentials proposés et la vérification du reçu se limitent aux
  authentificateurs de CE canon — une passkey enregistrée par un autre canon
  ne vaut jamais pour elle. Un défi de connexion, lui, n'a pas de canon
  d'action : il utilise le canon par défaut.
* `create_enroll_link` / `enroll_view` / `enroll_submit` : cérémonie de
  création de passkey → fichier de PROPOSITION de canon ; jamais d'écriture
  dans `authenticators`.
"""
from __future__ import annotations

import base64
import dataclasses
import hmac
import hashlib
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from .. import jcs, receipts
from ..db import DbError
from . import duplicate, enroll, render
from .config import ApproveConfig
from .sources import ACTION_ID_RE, REQUESTABLE_STATES, ActionSource, ActionSourceError
from .store import Store, new_login_id, new_request_id, new_token

log = logging.getLogger("ameesh.approve")

_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MEMBER_RE = re.compile(r"^(human|agent):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]{1,8192}$")
REQUEST_FIELDS = ("action_id", "approver", "requested_by")
#: version du document de `GET /health`
HEALTH_VERSION = 1
#: facultatif : `assume_duplicate` (booléen) — décision qui assume le doublon
REQUEST_OPTIONAL = ("assume_duplicate",)
_ASSERTION_FIELDS = frozenset({"decision", "credential_id", "authenticatorData",
                               "clientDataJSON", "signature"})
_ASSERTION_OPTIONAL = frozenset({"userHandle"})
_ENROLL_FIELDS = frozenset({"credential_id", "clientDataJSON", "attestationObject"})
_ENROLL_OPTIONAL = frozenset({"transports"})

# -- défis de connexion (L232d, lot #366) ---------------------------------
#: fournisseurs dont un défi peut être créé ; l'URL doit pointer un hôte
#: officiel du fournisseur (défense en profondeur : l'adaptateur l'a déjà
#: extraite de la sortie épinglée du CLI)
LOGIN_PROVIDERS = ("claude", "codex", "gh")
LOGIN_URL_DOMAINS = {
    "claude": ("claude.ai", "claude.com", "anthropic.com"),
    "codex": ("chatgpt.com", "openai.com"),
    "gh": ("github.com",),
}
#: échéance maximale d'un défi (le fournisseur peut l'écourter)
LOGIN_MAX_TTL = 900
LOGIN_REQUEST_FIELDS = ("host", "provider", "account", "approver", "requested_by",
                        "url", "code")
LOGIN_REQUEST_OPTIONAL = ("scope", "provider_exp")
#: décisions qui closent le défi (le reçu correspondant est signé)
LOGIN_FINAL_DECISIONS = ("done", "cancel")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$")
#: URL d'autorisation ou code à usage unique, bornés (jamais journalisés)
_LOGIN_URL_MAX = 4096
_LOGIN_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+/-]{0,511}$")


class ApproveError(Exception):
    """Refus avec un statut HTTP, un code stable et un message lisible."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ApproveService:
    def __init__(self, cfg: ApproveConfig, db, source: ActionSource, *, service_token: str,
                 store: Store | None = None, clock=time.time, db_lock=None):
        self.cfg = cfg
        self.db = db
        self.source = source
        self.store = store or Store(cfg.state_dir)
        self.clock = clock
        self.db_lock = db_lock or threading.RLock()
        self._token_digest = hashlib.sha256(service_token.encode("utf-8")).digest()
        self.policy = receipts.Policy(rp_id=cfg.rp_id, origins=cfg.origins,
                                      allow_facades=frozenset({"webauthn"}), level=cfg.level)

    # ------------------------------------------------------------------
    # jeton de service
    # ------------------------------------------------------------------
    def check_service_token(self, presented: str | None) -> bool:
        """Comparaison à temps constant (sur les SHA-256 : longueur fixe)."""
        presented_digest = hashlib.sha256((presented or "").encode("utf-8")).digest()
        return hmac.compare_digest(presented_digest, self._token_digest) and bool(presented)

    # ------------------------------------------------------------------
    # GET /health : la politique publiée (lot L27), sans aucun secret
    # ------------------------------------------------------------------
    def health(self, *, tls: bool = False) -> dict:
        """Ce que les vérificateurs doivent partager avec le service (RP ID,
        origines) et le profil ; ni jeton, ni chemin, ni état, ni base."""
        return {
            "service": "ameesh-approve",
            "health": HEALTH_VERSION,
            "rp_id": self.cfg.rp_id,
            "origins": list(self.cfg.origins),
            "public_url": self.cfg.base_url,
            "profile": self.cfg.profile,
            "level": self.cfg.level,
            "api_via_public": bool(self.cfg.api_via_public),
            "tls": bool(tls),
        }

    # ------------------------------------------------------------------
    # outils
    # ------------------------------------------------------------------
    def _action(self, action_id: str) -> dict:
        try:
            action = self.source.get_action(action_id)
        except ActionSourceError as exc:
            log.warning("action source: %s", exc)
            raise ApproveError(503, "action_source", str(exc)) from exc
        except DbError as exc:
            log.warning("action source unreachable: %s", type(exc).__name__)
            raise ApproveError(503, "action_source", "action source unreachable") from exc
        if action is None:
            raise ApproveError(404, "unknown_action", "unknown action: %s" % action_id)
        if action["action_id"] != action_id:
            raise ApproveError(503, "action_source", "the source returned another action")
        stored = action.get("digest")
        if stored and not hmac.compare_digest(stored.encode(), action["computed_digest"].encode()):
            raise ApproveError(409, "digest_mismatch",
                               "the stored digest of the action does not match its content")
        return action

    @staticmethod
    def _signed_digest(action: dict, assume_duplicate: bool) -> str:
        """L'empreinte que signe la demande : celle de l'action, ou celle de la
        décision qui assume son doublon (domaine distinct)."""
        if assume_duplicate:
            return duplicate.assume_duplicate_digest(action)
        return action["computed_digest"]

    @staticmethod
    def _check_state(action: dict, assume_duplicate: bool, *, later: bool = False) -> None:
        """L'état de l'action admet-il cette demande ? (mêmes règles que le lot L5 :
        `actions.retry` pour une approbation, `actions.replace` pour un doublon)."""
        state = action["state"]
        if action.get("replaced_by"):
            raise ApproveError(409, "action_replaced", "action replaced by %s: nothing left "
                               "to approve" % action["replaced_by"])
        if assume_duplicate:
            if state not in duplicate.ASSUMABLE_STATES:
                raise ApproveError(409, "action_state",
                                   "action in state %s: only an action with an unknown outcome "
                                   "(unknown) is replaced by assuming the duplicate" % state)
            return
        if state not in REQUESTABLE_STATES:
            if later:
                raise ApproveError(409, "action_state", "the action moved to state %s: "
                                   "nothing left to approve" % state)
            raise ApproveError(409, "action_state", "action in state %s: nothing to approve"
                               % state)
        if state == "unknown" and action.get("dedupe") not in (None, "guaranteed"):
            # nouvelle tentative impossible : l'effet a peut-être eu lieu
            raise ApproveError(409, "dedupe",
                               "unknown outcome and deduplication not guaranteed by %s: no "
                               "new attempt; reconcile, then a decision that assumes the "
                               "duplicate (assume_duplicate)" % action["connector"])

    def _credentials(self, approver: str, canon: str = "") -> list[str]:
        """Credentials WebAuthn actifs de l'approbateur, au niveau exigé,
        déclarés par `canon` (celui de l'action, L44)."""
        with self.db_lock:
            try:
                rows = receipts.list_authenticators(self.db, approver=approver, canon=canon)
            except DbError as exc:
                raise ApproveError(503, "registry", "trust registry unreachable") from exc
        return [row["credential_id"] for row in rows
                if row["facade"] == "webauthn"
                and (self.cfg.level != "eleve" or row["level"] == "eleve")]

    def _link(self, kind: str, token: str) -> str:
        return "%s/%s/%s" % (self.cfg.base_url, kind, token)

    # ------------------------------------------------------------------
    # POST /requests
    # ------------------------------------------------------------------
    def create_request(self, body) -> dict:
        if not isinstance(body, dict):
            raise ApproveError(400, "format", "JSON object expected")
        missing = [name for name in REQUEST_FIELDS if name not in body]
        if missing:
            raise ApproveError(400, "format", "missing fields: %s" % ", ".join(missing))
        ignored = sorted(set(body) - set(REQUEST_FIELDS) - set(REQUEST_OPTIONAL))
        if ignored:
            # résumé, texte, montant… fournis par le demandeur : ignorés (le
            # service rend son propre résumé depuis l'action)
            log.info("request: ignored fields %s", ", ".join(
                re.sub(r"[^A-Za-z0-9_-]", "?", name)[:32] for name in ignored[:10]))
        action_id, approver, requested_by = (body["action_id"], body["approver"],
                                             body["requested_by"])
        if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
            raise ApproveError(400, "format", "action_id: \"act_\" + 26 characters expected")
        if not isinstance(approver, str) or not _HUMAN_RE.fullmatch(approver):
            raise ApproveError(400, "format", "approver: \"human:<id>\" expected")
        if not isinstance(requested_by, str) or not _MEMBER_RE.fullmatch(requested_by):
            raise ApproveError(400, "format", "requested_by: \"agent:<id>\" or \"human:<id>\"")
        assume_duplicate = body.get("assume_duplicate", False)
        if not isinstance(assume_duplicate, bool):
            raise ApproveError(400, "format", "assume_duplicate: boolean expected")

        action = self._action(action_id)
        self._check_state(action, assume_duplicate)
        if not self._credentials(approver, action.get("canon") or ""):
            raise ApproveError(409, "no_authenticator",
                               "%s has no active WebAuthn authenticator at level %s "
                               "in the %s of the action (enrollment, then canon PR)"
                               % (approver, self.cfg.level,
                                  receipts.canon_label(action.get("canon"))))
        summary = render.render_summary(action, approver, requested_by,
                                        assume_duplicate=assume_duplicate)
        if len(summary.encode("utf-8")) > render.MAX_SUMMARY:
            raise ApproveError(413, "summary_too_long",
                               "action too large to be reviewed by a human")

        now = int(self.clock())
        request = {
            "v": receipts.REQUEST_VERSION,
            "approver": approver,
            "action_id": action_id,
            "digest": self._signed_digest(action, assume_duplicate),
            "summary_digest": render.summary_digest(summary),
            "requested_by": requested_by,
            "nonce": _b64u(os.urandom(16)),
            "iat": now,
            "exp": now + int(self.cfg.request_ttl),
        }
        try:
            receipts.check_request(dict(request, decision="approve"))
        except receipts.ReceiptError as exc:
            raise ApproveError(400, "format", str(exc)) from exc
        request_id = new_request_id()
        token = new_token()
        record = {
            "request_id": request_id,
            "request": request,
            "summary": summary,
            "link_exp": now + int(self.cfg.link_ttl),
            "exp": request["exp"],
            "created": now,
            "assume_duplicate": assume_duplicate,
        }
        self.store.save_request(record, token)
        self.store.prune(now)
        log.info("request %s: %saction %s, approver %s, requested by %s",
                 request_id, "DUPLICATE TO ASSUME of the " if assume_duplicate else "",
                 action_id, approver, requested_by)
        return {
            "request_id": request_id,
            "link": self._link("a", token),
            "link_exp": record["link_exp"],
            "exp": request["exp"],
            "action_id": action_id,
            "approver": approver,
            "assume_duplicate": assume_duplicate,
            "digest": request["digest"],
            "action_digest": action["computed_digest"],
            "summary_digest": request["summary_digest"],
        }

    # ------------------------------------------------------------------
    # GET /a/<jeton>
    # ------------------------------------------------------------------
    def _live_record(self, token: str) -> dict:
        record = self.store.request_by_token(token)
        if record is None:
            raise ApproveError(404, "unknown_link", "unknown link")
        if self.store.has_receipt(record["request_id"]):
            raise ApproveError(410, "used", "this link has already been used")
        now = self.clock()
        if now >= record["link_exp"] or now >= record["request"]["exp"]:
            raise ApproveError(410, "expired", "this link has expired")
        return record

    def _check_unchanged(self, record: dict) -> dict:
        """L'action relue maintenant est-elle encore celle de la demande ?"""
        request = record["request"]
        assume_duplicate = record.get("assume_duplicate") is True
        action = self._action(request["action_id"])
        if not hmac.compare_digest(self._signed_digest(action, assume_duplicate).encode(),
                                   request["digest"].encode()):
            raise ApproveError(409, "action_changed",
                               "the action changed since the request: request obsolete")
        self._check_state(action, assume_duplicate, later=True)
        return action

    def link_view(self, token: str) -> dict:
        record = self._live_record(token)
        action = self._check_unchanged(record)
        request = record["request"]
        credentials = self._credentials(request["approver"], action.get("canon") or "")
        if not credentials:
            raise ApproveError(409, "no_authenticator", "no active authenticator")
        return {
            "request_id": record["request_id"],
            "request": request,
            "action": action,
            "assume_duplicate": record.get("assume_duplicate") is True,
            "summary": record["summary"],
            "link_exp": record["link_exp"],
            "rp_id": self.cfg.rp_id,
            "level": self.cfg.level,
            "credentials": credentials,
            "challenge_approve": _b64u(receipts.challenge(dict(request, decision="approve"))),
            "challenge_deny": _b64u(receipts.challenge(dict(request, decision="deny"))),
        }

    # ------------------------------------------------------------------
    # POST /a/<jeton>
    # ------------------------------------------------------------------
    def _assertion(self, body, decisions) -> str:
        """Valide la forme d'un corps d'assertion ; rend la décision."""
        if not isinstance(body, dict):
            raise ApproveError(400, "format", "JSON object expected")
        keys = set(body)
        if not _ASSERTION_FIELDS <= keys or keys - _ASSERTION_FIELDS - _ASSERTION_OPTIONAL:
            raise ApproveError(400, "format", "expected fields: %s"
                               % ", ".join(sorted(_ASSERTION_FIELDS)))
        decision = body["decision"]
        if decision not in decisions:
            raise ApproveError(400, "format", "decision: %s" % " or ".join(decisions))
        for name in _ASSERTION_FIELDS - {"decision"}:
            if not isinstance(body[name], str) or not _B64U_RE.fullmatch(body[name]):
                raise ApproveError(400, "format", "%s: base64url expected" % name)
        return decision

    def submit(self, token: str, body) -> dict:
        decision = self._assertion(body, receipts.DECISIONS)

        record = self._live_record(token)
        action = self._check_unchanged(record)
        request = dict(record["request"], decision=decision)
        # L44 (0031) : vérifié contre les authentificateurs du canon de l'action
        policy = dataclasses.replace(self.policy, canon=action.get("canon") or "")
        receipt = {
            "v": receipts.RECEIPT_VERSION,
            "request": request,
            "facade": "webauthn",
            "credential_id": body["credential_id"],
            "proof": {name: body[name]
                      for name in ("authenticatorData", "clientDataJSON", "signature")},
        }
        with self.db_lock:
            try:
                verdict = receipts.verify_receipt(
                    self.db, receipt, policy, kind="action",
                    expected_digest=request["digest"], expected_action_id=request["action_id"],
                    expect_decision=decision, consume_by=None, now=self.clock())
            except DbError as exc:
                raise ApproveError(503, "registry", "trust registry unreachable") from exc
        if not verdict.ok:
            log.info("request %s: assertion refused [%s]", record["request_id"], verdict.code)
            raise ApproveError(403, verdict.code, verdict.reason)
        if not self.store.save_receipt_once(record["request_id"], receipt):
            raise ApproveError(410, "used", "this link has already been used")
        log.info("request %s: receipt %s%s signed by %s", record["request_id"], decision,
                 " (duplicate assumed)" if record.get("assume_duplicate") is True
                 and decision == "approve" else "", verdict.approver)
        return {"request_id": record["request_id"], "decision": decision}

    # ------------------------------------------------------------------
    # défis de connexion (L232d, lot #366)
    #
    # Un défi de connexion n'est PAS une action L5 : aucune empreinte
    # d'action, aucune garde d'état d'action (les gardes L5 restent
    # inchangées, sur leur propre chemin). La demande signable ne porte
    # jamais l'URL ni le code : ils vivent dans `login-private` (0600) et ne
    # sortent qu'après une assertion WebAuthn (`view`), donc jamais dans le
    # fil, le courrier, un journal ou un reçu. Le reçu `done` ne prouve pas
    # le grant fournisseur : la santé et le compte attendu sont vérifiés par
    # l'appelant.
    # ------------------------------------------------------------------
    @staticmethod
    def _login_url(provider: str, url) -> str:
        if not isinstance(url, str) or not 0 < len(url) <= _LOGIN_URL_MAX:
            raise ApproveError(400, "format", "url: https URL expected")
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            parts.port  # noqa: B018 - lève ValueError sur un port illisible
        except ValueError as exc:
            raise ApproveError(400, "format", "url unreadable: %s" % exc) from exc
        if parts.scheme != "https" or not host or parts.username is not None \
                or parts.password is not None:
            raise ApproveError(400, "format", "url: https URL without credentials expected")
        domains = LOGIN_URL_DOMAINS[provider]
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            raise ApproveError(400, "format", "url: %r is not an official %s host"
                               % (host, provider))
        return url

    def create_login_request(self, body) -> dict:
        if not isinstance(body, dict):
            raise ApproveError(400, "format", "JSON object expected")
        missing = [name for name in LOGIN_REQUEST_FIELDS if name not in body]
        if missing:
            raise ApproveError(400, "format", "missing fields: %s" % ", ".join(missing))
        ignored = sorted(set(body) - set(LOGIN_REQUEST_FIELDS) - set(LOGIN_REQUEST_OPTIONAL))
        if ignored:
            log.info("login request: ignored fields %s", ", ".join(
                re.sub(r"[^A-Za-z0-9_-]", "?", name)[:32] for name in ignored[:10]))
        host, provider, account = body["host"], body["provider"], body["account"]
        approver, requested_by = body["approver"], body["requested_by"]
        if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
            raise ApproveError(400, "format", "host: host name expected")
        if provider not in LOGIN_PROVIDERS:
            raise ApproveError(400, "format", "provider: %s" % " or ".join(LOGIN_PROVIDERS))
        if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
            raise ApproveError(400, "format", "account: account name expected")
        if not isinstance(approver, str) or not _HUMAN_RE.fullmatch(approver):
            raise ApproveError(400, "format", "approver: \"human:<id>\" expected")
        if not isinstance(requested_by, str) or not _MEMBER_RE.fullmatch(requested_by):
            raise ApproveError(400, "format", "requested_by: \"agent:<id>\" or \"human:<id>\"")
        scope = body.get("scope", "")
        if not isinstance(scope, str) or (scope and not _SCOPE_RE.fullmatch(scope)):
            raise ApproveError(400, "format", "scope: short text expected (empty allowed)")
        code = body["code"]
        if not isinstance(code, str) or not _LOGIN_CODE_RE.fullmatch(code):
            raise ApproveError(400, "format", "code: one-time code expected")
        url = self._login_url(provider, body["url"])

        now = int(self.clock())
        ttl = LOGIN_MAX_TTL
        provider_exp = body.get("provider_exp")
        if provider_exp is not None:
            if type(provider_exp) is not int or provider_exp <= now:
                raise ApproveError(400, "format", "provider_exp: future epoch expected")
            ttl = min(ttl, provider_exp - now)
        if not self._credentials(approver):
            raise ApproveError(409, "no_authenticator",
                               "%s has no active WebAuthn authenticator" % approver)

        request = {
            "v": receipts.REQUEST_VERSION,
            "approver": approver,
            "login_id": "",
            "host": host,
            "provider": provider,
            "account": account,
            "scope": scope,
            "summary_digest": "",
            "requested_by": requested_by,
            "nonce": _b64u(os.urandom(16)),
            "iat": now,
            "exp": now + int(ttl),
        }
        summary = render.render_login_summary(request)
        request["summary_digest"] = render.summary_digest(summary)
        login_id = new_login_id()
        request["login_id"] = login_id
        try:
            receipts.check_request(dict(request, decision="view"))
        except receipts.ReceiptError as exc:
            raise ApproveError(400, "format", str(exc)) from exc
        token = new_token()
        record = {
            "login_id": login_id,
            "request": request,
            "summary": summary,
            "link_exp": request["exp"],     # le lien vit exactement le défi
            "exp": request["exp"],
            "created": now,
        }
        private = {"url": url, "code": code, "provider_exp": provider_exp}
        self.store.save_login(record, token, private)
        self.store.prune(now)
        log.info("login challenge %s: %s/%s on %s, approver %s, requested by %s, %d s",
                 login_id, provider, account or "(default)", host, approver, requested_by,
                 request["exp"] - now)
        return {
            "login_id": login_id,
            "link": self._link("login", token),
            "link_exp": record["link_exp"],
            "exp": request["exp"],
            "host": host,
            "provider": provider,
            "account": account,
            "scope": scope,
        }

    def _live_login(self, token: str) -> dict:
        record = self.store.login_by_token(token)
        if record is None:
            raise ApproveError(404, "unknown_link", "unknown link")
        if self.store.has_login_receipt(record["login_id"]):
            raise ApproveError(410, "used", "this challenge is already settled")
        if self.clock() >= record["link_exp"] or self.clock() >= record["request"]["exp"]:
            raise ApproveError(410, "expired", "this challenge has expired")
        return record

    def login_view(self, token: str) -> dict:
        """La page PUBLIQUE du défi : hôte, fournisseur, compte, portée,
        échéance et résumé — jamais l'URL ni le code (assertion d'abord)."""
        record = self._live_login(token)
        request = record["request"]
        credentials = self._credentials(request["approver"])
        if not credentials:
            raise ApproveError(409, "no_authenticator", "no active authenticator")
        return {
            "login_id": record["login_id"],
            "request": request,
            "summary": record["summary"],
            "host": request["host"],
            "provider": request["provider"],
            "account": request["account"],
            "scope": request["scope"],
            "exp": request["exp"],
            "link_exp": record["link_exp"],
            "rp_id": self.cfg.rp_id,
            "level": self.cfg.level,
            "credentials": credentials,
            "viewed": self.store.login_viewed(record["login_id"]),
            "challenge_view": _b64u(receipts.challenge(dict(request, decision="view"))),
            "challenge_cancel": _b64u(receipts.challenge(dict(request, decision="cancel"))),
        }

    def _login_receipt(self, record: dict, body, decision: str):
        """Construit et VÉRIFIE le reçu d'un défi (sans consommer le nonce :
        c'est ameesh qui le consommera en acceptant le reçu)."""
        request = dict(record["request"], decision=decision)
        receipt = {
            "v": receipts.RECEIPT_VERSION,
            "request": request,
            "facade": "webauthn",
            "credential_id": body["credential_id"],
            "proof": {name: body[name]
                      for name in ("authenticatorData", "clientDataJSON", "signature")},
        }
        expected = {"login_id": request["login_id"], "host": request["host"],
                    "provider": request["provider"], "account": request["account"],
                    "exp": request["exp"]}
        with self.db_lock:
            try:
                verdict = receipts.verify_receipt(
                    self.db, receipt, self.policy, kind="login", expected_login=expected,
                    expect_decision=decision, consume_by=None, now=self.clock())
            except DbError as exc:
                raise ApproveError(503, "registry", "trust registry unreachable") from exc
        return receipt, verdict

    def login_submit(self, token: str, body) -> dict:
        """`POST /login/<jeton>` : `view` ouvre la vue privée (assertion
        requise AVANT tout affichage) ; `done` / `cancel` signent le reçu."""
        decision = self._assertion(body, ("view",) + LOGIN_FINAL_DECISIONS)
        record = self._live_login(token)
        if decision == "done" and not self.store.login_viewed(record["login_id"]):
            raise ApproveError(409, "not_viewed",
                               "open the private view before signing done")
        receipt, verdict = self._login_receipt(record, body, decision)
        if not verdict.ok:
            log.info("login %s: assertion refused [%s]", record["login_id"], verdict.code)
            raise ApproveError(403, verdict.code, verdict.reason)
        if decision == "view":
            self.store.mark_login_viewed(record["login_id"])
            private = self.store.login_private(record["login_id"])
            if private is None:
                raise ApproveError(410, "gone", "the private challenge is no longer available")
            request = record["request"]
            log.info("login %s: private view opened by %s", record["login_id"], verdict.approver)
            return {
                "login_id": record["login_id"],
                "url": private["url"],
                "code": private["code"],
                "provider_exp": private.get("provider_exp"),
                "exp": request["exp"],
                "challenge_done": _b64u(receipts.challenge(dict(request, decision="done"))),
                "challenge_cancel": _b64u(receipts.challenge(dict(request, decision="cancel"))),
            }
        if not self.store.save_login_receipt_once(record["login_id"], receipt):
            raise ApproveError(410, "used", "this challenge is already settled")
        self.store.drop_login_private(record["login_id"])
        log.info("login %s: receipt %s signed by %s", record["login_id"], decision,
                 verdict.approver)
        return {"login_id": record["login_id"], "decision": decision}

    def login_receipt(self, login_id: str) -> tuple[int, bytes | dict]:
        """`GET /login-receipts/<id>` : le reçu (200), en attente (202), échu (410)."""
        raw = self.store.login_receipt_bytes(login_id)
        if raw is not None:
            return 200, raw
        record = self.store.get_login(login_id)
        if record is None:
            raise ApproveError(404, "unknown_request", "unknown challenge")
        if self.clock() >= record["link_exp"] or self.clock() >= record["request"]["exp"]:
            return 410, {"status": "expired", "login_id": login_id}
        return 202, {"status": "pending", "login_id": login_id,
                     "link_exp": record["link_exp"]}

    # ------------------------------------------------------------------
    # GET /receipts/<id>
    # ------------------------------------------------------------------
    def receipt(self, request_id: str) -> tuple[int, bytes | dict]:
        raw = self.store.receipt_bytes(request_id)
        if raw is not None:
            return 200, raw
        record = self.store.get_request(request_id)
        if record is None:
            raise ApproveError(404, "unknown_request", "unknown request")
        if self.clock() >= record["link_exp"]:
            return 410, {"status": "expired", "request_id": request_id}
        return 202, {"status": "pending", "request_id": request_id,
                     "link_exp": record["link_exp"]}

    # ------------------------------------------------------------------
    # enrôlement
    # ------------------------------------------------------------------
    def create_enroll_link(self, approver: str, ttl: int | None = None) -> dict:
        if not isinstance(approver, str) or not _HUMAN_RE.fullmatch(approver):
            raise ApproveError(400, "format", "approver: \"human:<id>\" expected")
        ttl = int(ttl or self.cfg.enroll_ttl)
        if not 60 <= ttl <= 86400:
            raise ApproveError(400, "format", "enrollment duration between 60 s and 24 h")
        now = int(self.clock())
        token = new_token()
        record = {"approver": approver, "challenge": _b64u(os.urandom(32)),
                  "created": now, "exp": now + ttl}
        self.store.save_enroll(record, token)
        log.info("enrollment link created for %s", approver)
        return {"link": self._link("enroll", token), "exp": record["exp"], "approver": approver}

    def _live_enroll(self, token: str) -> dict:
        record = self.store.enroll_by_token(token)
        if record is None:
            raise ApproveError(404, "unknown_link", "unknown link")
        if self.store.enroll_used(record):
            raise ApproveError(410, "used", "this link has already been used")
        if self.clock() >= record["exp"]:
            raise ApproveError(410, "expired", "this link has expired")
        return record

    def enroll_view(self, token: str) -> dict:
        record = self._live_enroll(token)
        approver = record["approver"]
        with self.db_lock:
            try:
                rows = receipts.list_authenticators(self.db, approver=approver)
            except DbError:
                rows = []
        return {
            "approver": approver,
            "rp_id": self.cfg.rp_id,
            "rp_name": self.cfg.rp_name,
            "challenge": record["challenge"],
            "user_id": _b64u(enroll.user_handle(approver)),
            "exclude": [row["credential_id"] for row in rows if row["facade"] == "webauthn"],
            "exp": record["exp"],
        }

    def enroll_submit(self, token: str, body, *, origin: str) -> dict:
        if not isinstance(body, dict):
            raise ApproveError(400, "format", "JSON object expected")
        keys = set(body)
        if not _ENROLL_FIELDS <= keys or keys - _ENROLL_FIELDS - _ENROLL_OPTIONAL:
            raise ApproveError(400, "format", "expected fields: %s"
                               % ", ".join(sorted(_ENROLL_FIELDS)))
        try:
            decoded = {name: receipts.b64u_decode(body[name], name) for name in _ENROLL_FIELDS}
        except receipts.ReceiptError as exc:
            raise ApproveError(400, "format", str(exc)) from exc
        record = self._live_enroll(token)
        try:
            entry = enroll.parse_registration(
                attestation_object=decoded["attestationObject"],
                client_data_json=decoded["clientDataJSON"],
                raw_id=decoded["credential_id"],
                challenge=receipts.b64u_decode(record["challenge"], "challenge"),
                rp_id=self.cfg.rp_id, origins=self.cfg.origins)
        except enroll.EnrollError as exc:
            log.info("enrollment of %s refused [%s]", record["approver"], exc.code)
            raise ApproveError(403, exc.code, str(exc)) from exc
        if not self.store.claim_enroll(record):
            raise ApproveError(410, "used", "this link has already been used")
        name = enroll.write_proposal(self.cfg.proposals_path, approver=record["approver"],
                                     entry=entry, rp_id=self.cfg.rp_id, origin=origin,
                                     now=self.clock())
        log.info("authenticator proposal for %s: %s", record["approver"], name)
        return {"proposal": name, "credential_id": entry["credential_id"],
                "key_fingerprint": entry["key_fingerprint"],
                "backup_eligible": entry["backup_eligible"], "level": entry["level"]}


def load_json_body(raw: bytes):
    try:
        return jcs.loads(raw)
    except jcs.JcsError as exc:
        raise ApproveError(400, "format", "unreadable JSON") from exc
