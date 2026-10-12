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
* L44 (0031) : l'action relue porte son canon (`actions.canon`) ; les
  credentials proposés et la vérification du reçu se limitent aux
  authentificateurs de CE canon — une passkey enregistrée par un autre canon
  ne vaut jamais pour elle.
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

from .. import jcs, receipts
from ..db import DbError
from . import duplicate, enroll, render
from .config import ApproveConfig
from .sources import ACTION_ID_RE, REQUESTABLE_STATES, ActionSource, ActionSourceError
from .store import Store, new_request_id, new_token

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
    def submit(self, token: str, body) -> dict:
        if not isinstance(body, dict):
            raise ApproveError(400, "format", "JSON object expected")
        keys = set(body)
        if not _ASSERTION_FIELDS <= keys or keys - _ASSERTION_FIELDS - _ASSERTION_OPTIONAL:
            raise ApproveError(400, "format", "expected fields: %s"
                               % ", ".join(sorted(_ASSERTION_FIELDS)))
        decision = body["decision"]
        if decision not in receipts.DECISIONS:
            raise ApproveError(400, "format", "decision: approve or deny")
        for name in _ASSERTION_FIELDS - {"decision"}:
            if not isinstance(body[name], str) or not _B64U_RE.fullmatch(body[name]):
                raise ApproveError(400, "format", "%s: base64url expected" % name)

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
