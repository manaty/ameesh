# SPDX-License-Identifier: AGPL-3.0-only
"""Actions et porte (spec §7, R5, C6) — et la file des décisions (C10).

Une action est un effet sur le monde extérieur, classé `read`, `reversible`,
`irreversible` ou `costly`. Son identité (`action_id`, « act_ » + ULID) est
attribuée à la proposition et reste la même à travers toutes ses tentatives :
c'est la clé d'idempotence transmise au connecteur. Le nonce d'un reçu, lui,
n'autorise qu'UNE tentative.

    proposée → approuvée → lancée → confirmée | échouée | inconnue

* `propose` : args canonisés (JCS), classe (celle du canon prime ; une classe
  demandée ne peut que RELEVER celle du connecteur), empreinte ;
* `approve` : vérifie le reçu (L6) contre l'empreinte RECALCULÉE depuis
  l'action et le lie à l'action, sans le consommer — ou lie un grant
  (approbation permanente bornée) qui la couvre ; les classes `read` et
  `reversible` n'exigent rien (paramétrable à la proposition) ;
* `execute` : revérifie le reçu, puis en UNE transaction (fonction
  `ameesh_action_launch`) passe `approved → launched`, consomme le nonce (ou
  réserve le montant sur le grant) et crée la tentative ; ENSUITE seulement
  appelle le connecteur, clé d'idempotence = `action_id` ; enregistre
  `confirmed | failed | unknown`. Exception, délai dépassé, perte de
  connexion → `unknown`. Si l'issue ne peut pas être enregistrée, l'action
  reste `launched` et sera traitée comme inconnue (`recover`) ;
* `unknown` : aucune nouvelle tentative automatique. `retry` (nouveau reçu,
  MÊME `action_id`) n'est permis qu'après `failed`, ou après `unknown` si le
  connecteur déclare `dedupe = guaranteed` ; sinon `reconcile`, et si
  l'issue reste introuvable, une décision humaine : `replace`.

Décision « assumer le doublon » (`replace`) — le reçu est un reçu d'action
ordinaire (`ameesh-receipt/1`, décision `approve`) dont `action_id` est
l'action INCONNUE remplacée et dont `digest` est :

    SHA-256("ameesh-assume-duplicate/1\\0" ‖ JCS({action_digest,
            decision: "assume-duplicate", replaces: action_id}))

Domaine distinct : un reçu d'approbation de l'action (nouvelle tentative) ne
vaut jamais décision d'assomption, et inversement. La nouvelle action
(nouvel `action_id`, `replaces` = l'ancienne, même contenu) naît `proposed`
et suit le protocole complet : elle exige sa propre approbation.

Le journal (`action_events`) est écrit par déclencheur, dans la transaction
de chaque transition. L'entrée de fil (C5, lot L4) n'est pas encore écrite :
le journal est la source qu'indexera le fil.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from . import connectors as connectors_mod
from . import jcs, receipts, work
from .connectors import ACTION_CLASSES, SEVERITY, Action, ConnectorError, Outcome
from .db import Db, DbError

STATES = ("proposed", "approved", "launched", "confirmed", "failed", "unknown", "cancelled")
TERMINAL = ("confirmed", "cancelled")
#: classes qui exigent TOUJOURS un reçu ou un grant (contrainte en base aussi)
GATED_CLASSES = frozenset({"irreversible", "costly"})
DEFAULT_RECEIPT_CLASSES = GATED_CLASSES
#: une action `launched` dont l'échéance est dépassée depuis plus que ce délai
#: est réputée interrompue (processus mort) : `recover` la passe en `unknown`
RECOVER_GRACE = 60.0
DEFAULT_REQUEST_TTL = 600
ASSUME_DUPLICATE_DOMAIN = b"ameesh-assume-duplicate/1\x00"
SUMMARY_DOMAIN = b"ameesh-summary/1\x00"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ACTION_ID_RE = re.compile(r"^act_[0-9A-Za-z]{26}$")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_MEMBER_RE = re.compile(r"^(human|agent):%s$" % _NAME)
_HUMAN_RE = re.compile(r"^human:%s$" % _NAME)
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# codes de refus (stables) — en plus de ceux des reçus (receipts.*)
UNKNOWN_ACTION = "unknown_action"
STATE = "state"
LAUNCHED = "launched"
UNKNOWN_OUTCOME = "unknown"
DEDUPE = "dedupe"
REPLACED = "replaced"
RECEIPT_REQUIRED = "receipt_required"
DECISION_REQUIRED = "decision_required"
NO_COVER = "no_cover"
CONNECTOR = "connector"
CLASS = "class"
APPROVER = "approver_not_allowed"
INVALID = "invalid"
UNRECORDED = "unrecorded"


class ActionError(RuntimeError):
    """Action refusée ou introuvable — toujours avec un code stable et une raison."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def __str__(self) -> str:
        return "[%s] %s" % (self.code, self.reason)


# --------------------------------------------------------------------------
# identité, empreintes, demandes d'approbation
# --------------------------------------------------------------------------

def new_action_id(now: float | None = None) -> str:
    """« act_ » + ULID (48 bits de millisecondes, 80 bits aléatoires), base32 Crockford."""
    millis = int((time.time() if now is None else now) * 1000) & ((1 << 48) - 1)
    value = (millis << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "act_" + "".join(reversed(chars))


def _field(action, name: str, default=None):
    if isinstance(action, Action):
        mapping = {"class": "action_class"}
        return getattr(action, mapping.get(name, name), default)
    return action.get(name, default)


def digest_fields(action) -> dict:
    """Les neuf champs signés (spec §7.1), lus sur une ligne ou une `Action`."""
    return {
        "action_id": _field(action, "action_id"),
        "project": _field(action, "project"),
        "connector": _field(action, "connector"),
        "operation": _field(action, "operation"),
        "target": _field(action, "target") or "",
        "args": _field(action, "args"),
        "amount": _field(action, "amount"),
        "currency": _field(action, "currency"),
        "policy_version": _field(action, "policy_version"),
    }


def digest(action) -> str:
    """Empreinte d'une action (spec §7.1), par `receipts.action_digest` (L6)."""
    return receipts.action_digest(digest_fields(action))


def assume_duplicate_digest(action) -> str:
    """Empreinte de la décision « assumer le doublon » qui remplace `action`."""
    payload = {"action_digest": digest(action), "decision": "assume-duplicate",
               "replaces": _field(action, "action_id")}
    return "sha256:" + hashlib.sha256(ASSUME_DUPLICATE_DOMAIN
                                      + jcs.canonicalize(payload)).hexdigest()


def summary(action, *, assume_duplicate: bool = False) -> str:
    """Résumé lisible, recalculé depuis l'action (jamais depuis le texte d'un agent)."""
    amount = _field(action, "amount")
    money = "" if amount is None else " — montant %s %s" % (amount, _field(action, "currency"))
    text = "%s %s %s sur %s (classe %s, projet %s)%s — args %s" % (
        "ASSUMER LE DOUBLON et remplacer" if assume_duplicate else "approuver",
        _field(action, "connector"), _field(action, "operation"),
        _field(action, "target") or "—", _field(action, "class"), _field(action, "project"),
        money, jcs.dumps(_field(action, "args") or {}))
    return text


def summary_digest(action, *, assume_duplicate: bool = False) -> str:
    text = summary(action, assume_duplicate=assume_duplicate)
    return "sha256:" + hashlib.sha256(SUMMARY_DOMAIN + text.encode("utf-8")).hexdigest()


def approval_request(action, approver: str, *, decision: str = "approve",
                     assume_duplicate: bool = False, requested_by: str | None = None,
                     ttl: int = DEFAULT_REQUEST_TTL, now: int | None = None,
                     nonce: str | None = None) -> dict:
    """La demande à signer (spec §8.1) pour approuver `action` — ou, avec
    `assume_duplicate`, pour assumer le doublon et la remplacer."""
    if not _HUMAN_RE.fullmatch(approver or ""):
        raise ActionError(INVALID, "approbateur « human:<id> » attendu, pas %r" % (approver,))
    moment = int(time.time()) if now is None else int(now)
    request = {
        "v": receipts.REQUEST_VERSION,
        "approver": approver,
        "action_id": _field(action, "action_id"),
        "digest": assume_duplicate_digest(action) if assume_duplicate else digest(action),
        "decision": decision,
        "summary_digest": summary_digest(action, assume_duplicate=assume_duplicate),
        "requested_by": requested_by or _field(action, "proposed_by"),
        "nonce": nonce or receipts.b64u_encode(os.urandom(16)),
        "iat": moment,
        "exp": moment + int(ttl),
    }
    try:
        receipts.check_request(request)
    except receipts.ReceiptError as exc:
        raise ActionError(INVALID, "demande d'approbation invalide : %s" % exc) from exc
    return request


def policy_from_env(env: dict | None = None) -> receipts.Policy:
    """Politique de vérification : AMEESH_APPROVE_RP_ID, AMEESH_APPROVE_ORIGINS."""
    env = os.environ if env is None else env
    origins = tuple(o.strip() for o in (env.get("AMEESH_APPROVE_ORIGINS") or "").split(",")
                    if o.strip())
    return receipts.Policy(rp_id=env.get("AMEESH_APPROVE_RP_ID", ""), origins=origins)


# --------------------------------------------------------------------------
# lecture
# --------------------------------------------------------------------------

_COLUMNS = """
    action_id, project, work_item, proposed_by, connector, operation, target, args,
    class, amount, currency, policy_version, digest, dedupe, requires_receipt, approvers,
    state, replaces, replaced_by, replace_approver, attempts, auth_kind, auth_approver,
    auth_nonce, auth_challenge, auth_authenticator_id, auth_grant_id, auth_by,
    external_ref, last_error, last_actor, last_note,
    extract(epoch from auth_expires_at)::float8 AS auth_expires_ts,
    extract(epoch from created_at)::float8      AS created_ts,
    extract(epoch from updated_at)::float8      AS updated_ts,
    extract(epoch from approved_at)::float8     AS approved_ts,
    extract(epoch from launched_at)::float8     AS launched_ts,
    extract(epoch from launch_deadline)::float8 AS launch_deadline_ts,
    extract(epoch from finished_at)::float8     AS finished_ts,
    (state = 'launched' AND launch_deadline < now()) AS stale
"""


def get(db: Db, action_id: str, *, with_receipts: bool = False) -> dict | None:
    if not isinstance(action_id, str) or not _ACTION_ID_RE.fullmatch(action_id):
        return None
    columns = _COLUMNS + (", auth_receipt, replace_receipt" if with_receipts else "")
    rows = db.query("SELECT %s FROM actions WHERE action_id = %%s" % columns, (action_id,))
    return rows[0] if rows else None


def _require(db: Db, action_id: str, *, with_receipts: bool = False) -> dict:
    action = get(db, action_id, with_receipts=with_receipts)
    if action is None:
        raise ActionError(UNKNOWN_ACTION, "action %s introuvable" % (action_id,))
    return action


def list_actions(db: Db, *, state: str | None = None, project: str | None = None,
                 limit: int = 50) -> list[dict]:
    sql = "SELECT %s FROM actions WHERE true" % _COLUMNS
    params: list = []
    if state:
        if state not in STATES:
            raise ActionError(INVALID, "état inconnu : %r (%s)" % (state, ", ".join(STATES)))
        sql += " AND state = %s"
        params.append(state)
    if project:
        sql += " AND project = %s"
        params.append(project)
    sql += " ORDER BY created_at DESC, action_id DESC LIMIT %s"
    params.append(int(limit))
    return db.query(sql, tuple(params))


def attempts(db: Db, action_id: str) -> list[dict]:
    return db.query(
        """
        SELECT action_id, attempt_no, auth_kind, receipt_id, approver, nonce, grant_id,
               reservation_id, state, external_ref, error, launched_by, settled_by,
               extract(epoch from launched_at)::float8 AS launched_ts,
               extract(epoch from deadline_at)::float8 AS deadline_ts,
               extract(epoch from finished_at)::float8 AS finished_ts
          FROM action_attempts WHERE action_id = %s ORDER BY attempt_no
        """, (action_id,))


def events(db: Db, action_id: str) -> list[dict]:
    return db.query(
        """
        SELECT id, action_id, attempt_no, event, from_state, to_state, actor, note,
               extract(epoch from created_at)::float8 AS created_ts
          FROM action_events WHERE action_id = %s ORDER BY id
        """, (action_id,))


def _event(db: Db, action: dict, event: str, actor: str, note: str) -> None:
    """Événement informatif (refus, réconciliation sans résultat) : sans transition."""
    try:
        db.query(
            "INSERT INTO action_events (action_id, attempt_no, event, from_state, to_state, "
            "actor, note) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (action["action_id"], int(action.get("attempts") or 0), event, action.get("state"),
             action.get("state"), actor or "", note or ""))
    except DbError:
        pass  # le refus lui-même est rendu à l'appelant ; le journal est un plus


def to_action(row: dict, attempt: int | None = None) -> Action:
    return Action(
        action_id=row["action_id"], project=row["project"], connector=row["connector"],
        operation=row["operation"], target=row.get("target") or "", args=dict(row["args"] or {}),
        action_class=row["class"], amount=row.get("amount"), currency=row.get("currency"),
        policy_version=row.get("policy_version") or "1",
        attempt=int(row.get("attempts") or 0) if attempt is None else int(attempt),
        work_item=row.get("work_item"),
    )


# --------------------------------------------------------------------------
# proposition
# --------------------------------------------------------------------------

def propose(db: Db, connector, *, project: str, operation: str, target: str,
            args: dict | None = None, proposed_by: str, action_class: str | None = None,
            canon_class: str | None = None, amount: int | None = None,
            currency: str | None = None, work_item: int | None = None,
            approvers=(), policy_version: str = "1",
            receipt_classes=DEFAULT_RECEIPT_CLASSES) -> dict:
    """Enregistre une action `proposed` ; renvoie la ligne.

    `canon_class` : la classe déclarée par le canon (prime sur
    `connector.classify`). `action_class` : la classe demandée par le
    proposant ; elle peut relever la classe du connecteur, jamais l'abaisser.
    `receipt_classes` : classes qui exigent un reçu (irreversible et costly
    toujours). `approvers` : humains habilités à approuver (vide = tout
    approbateur du registre).
    """
    connectors_mod.check(connector)
    if not isinstance(project, str) or not _IDENT_RE.fullmatch(project):
        raise ActionError(INVALID, "projet invalide : %r" % (project,))
    if not isinstance(operation, str) or not _IDENT_RE.fullmatch(operation):
        raise ActionError(INVALID, "opération invalide : %r" % (operation,))
    if not isinstance(target, str) or not 1 <= len(target) <= 1024:
        raise ActionError(INVALID, "cible (compte ou objet visé) : texte de 1 à 1024 "
                          "caractères attendu")
    if not isinstance(proposed_by, str) or not _MEMBER_RE.fullmatch(proposed_by):
        raise ActionError(INVALID, "proposant « agent:<id> » ou « human:<id> » attendu, pas %r"
                          % (proposed_by,))
    approvers = list(approvers or ())
    for approver in approvers:
        if not isinstance(approver, str) or not _HUMAN_RE.fullmatch(approver):
            raise ActionError(INVALID, "approbateur « human:<id> » attendu, pas %r" % (approver,))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ActionError(INVALID, "args : objet JSON attendu")
    try:
        canonical_args = jcs.dumps(args)
        args = jcs.loads(canonical_args)
    except jcs.JcsError as exc:
        raise ActionError(INVALID, "args non canonisables (JCS) : %s" % exc) from exc
    if amount is not None:
        if type(amount) is not int or amount < 0 or amount > jcs.MAX_SAFE_INTEGER:
            raise ActionError(INVALID, "montant : entier >= 0 en unités mineures attendu")
        if currency is None:
            raise ActionError(INVALID, "un montant exige une devise")
    if currency is not None and (not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency)):
        raise ActionError(INVALID, "devise : code ISO 4217 (3 majuscules) attendu")
    if not isinstance(policy_version, str) or not 1 <= len(policy_version) <= 128:
        raise ActionError(INVALID, "policy_version : texte de 1 à 128 caractères")

    default = connector.classify(operation, args)
    if default not in ACTION_CLASSES:
        raise ActionError(CLASS, "connecteur %s : classe %r inconnue" % (connector.name, default))
    if canon_class is not None:
        if canon_class not in ACTION_CLASSES:
            raise ActionError(CLASS, "classe du canon inconnue : %r" % (canon_class,))
        chosen = canon_class
    elif action_class is not None:
        if action_class not in ACTION_CLASSES:
            raise ActionError(CLASS, "classe inconnue : %r (%s)" % (action_class, ", ".join(ACTION_CLASSES)))
        if SEVERITY[action_class] < SEVERITY[default]:
            raise ActionError(CLASS, "classe %s refusée : %s %s est classée %s par le connecteur ; "
                              "seul le canon peut l'abaisser" % (action_class, connector.name,
                                                                 operation, default))
        chosen = action_class
    else:
        chosen = default
    unknown = set(receipt_classes or ()) - set(ACTION_CLASSES)
    if unknown:
        raise ActionError(CLASS, "classes inconnues : %s" % sorted(unknown))
    requires = chosen in (set(receipt_classes or ()) | GATED_CLASSES)

    action_id = new_action_id()
    fields = {"action_id": action_id, "project": project, "connector": connector.name,
              "operation": operation, "target": target, "args": args, "amount": amount,
              "currency": currency, "policy_version": policy_version}
    try:
        action_digest = receipts.action_digest(fields)
    except receipts.ReceiptError as exc:
        raise ActionError(INVALID, str(exc)) from exc
    db.query(
        """
        INSERT INTO actions
            (action_id, project, work_item, proposed_by, connector, operation, target, args,
             class, amount, currency, policy_version, digest, dedupe, requires_receipt,
             approvers, last_actor, last_note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s)
        RETURNING action_id
        """,
        (action_id, project, work_item, proposed_by, connector.name, operation, target,
         canonical_args, chosen, amount, currency, policy_version, action_digest,
         connector.dedupe, requires, jcs.dumps(approvers), proposed_by,
         "proposée (%s, %s)" % (chosen, "reçu requis" if requires else "sans reçu")),
    )
    action = _require(db, action_id)
    if digest(action) != action_digest:  # l'aller-retour jsonb ne doit rien changer
        raise ActionError(INVALID, "empreinte instable après enregistrement (%s)" % action_id)
    return action


# --------------------------------------------------------------------------
# autorisation : reçu, grant, ou rien (classes non gardées)
# --------------------------------------------------------------------------

def _covering_grant(db: Db, action: dict) -> int | None:
    """Un grant qui couvre l'action (lecture seule ; la réservation se fait au
    lancement, atomiquement). Une réservation vivante de l'action passe en premier."""
    amount = int(action.get("amount") or 0)
    rows = db.query(
        """
        SELECT s.id, s.approver,
               EXISTS (SELECT 1 FROM standing_reservations r
                        WHERE r.grant_id = s.id AND r.action_id = %s
                          AND r.released_at IS NULL AND r.amount = %s::bigint) AS live
          FROM standing_approvals s
         WHERE s.connector = %s AND s.operations @> jsonb_build_array(%s::text)
           AND s.action_class = %s AND s.revoked_at IS NULL AND s.valid_until > now()
           AND (%s::bigint = 0 OR s.currency = %s)
           AND EXISTS (SELECT 1 FROM authenticators a
                        WHERE a.id = s.authenticator_id AND a.revoked_at IS NULL)
         ORDER BY s.valid_until, s.id
        """,
        (action["action_id"], amount, action["connector"], action["operation"],
         action["class"], amount, action.get("currency")),
    )
    allowed = action.get("approvers") or []
    usable = []
    for row in rows:
        if allowed and row["approver"] not in allowed:
            continue
        usable.append(row)
    for row in usable:
        if row.get("live"):
            return int(row["id"])
    for row in usable:
        grant = receipts.get_standing(db, int(row["id"]))
        if grant and int(grant["consumed_amount"]) + amount <= int(grant["max_amount"]):
            return int(row["id"])
    return None


def _verify(db: Db, action: dict, receipt, policy, *, expected_digest: str,
            expect_decision: str | None, now) -> receipts.ReceiptVerdict:
    verdict = receipts.verify_receipt(
        db, receipt, policy if policy is not None else policy_from_env(), kind="action",
        expected_digest=expected_digest, expected_action_id=action["action_id"],
        expect_decision=expect_decision, consume_by=None, now=now)
    if not verdict.ok:
        raise ActionError(verdict.code, "reçu refusé : %s" % verdict.reason)
    allowed = action.get("approvers") or []
    if allowed and verdict.approver not in allowed:
        raise ActionError(APPROVER, "%s n'est pas habilité à approuver %s (habilités : %s)"
                          % (verdict.approver, action["action_id"], ", ".join(allowed)))
    return verdict


def _authorize(db: Db, action: dict, receipt, *, standing: bool, policy, now,
               allow_deny: bool) -> tuple[str, dict]:
    """("approve", champs à lier) ou ("deny", verdict)."""
    if receipt is not None:
        verdict = _verify(db, action, receipt, policy, expected_digest=digest(action),
                          expect_decision=None if allow_deny else "approve", now=now)
        if verdict.decision == "deny":
            return "deny", {"verdict": verdict}
        document = receipts.parse_receipt(receipt).document
        return "approve", {
            "auth_kind": "receipt", "auth_receipt": jcs.dumps(document),
            "auth_approver": verdict.approver, "auth_nonce": verdict.nonce,
            "auth_challenge": verdict.challenge,
            "auth_authenticator_id": verdict.authenticator_id, "auth_expires": verdict.exp,
            "auth_grant_id": None,
            "note": "approuvée par %s (%s, reçu %s…)" % (
                verdict.approver, verdict.facade, verdict.challenge[:12]),
        }
    if standing:
        grant = _covering_grant(db, action)
        if grant is None:
            raise ActionError(NO_COVER, "aucune approbation permanente ne couvre %s (%s %s, "
                              "classe %s, montant %s)" % (
                                  action["action_id"], action["connector"], action["operation"],
                                  action["class"], action.get("amount")))
        return "approve", {
            "auth_kind": "standing", "auth_receipt": None, "auth_approver": None,
            "auth_nonce": None, "auth_challenge": None, "auth_authenticator_id": None,
            "auth_expires": None, "auth_grant_id": grant,
            "note": "couverte par le grant #%d (réservation au lancement)" % grant,
        }
    if not action["requires_receipt"]:
        return "approve", {
            "auth_kind": "none", "auth_receipt": None, "auth_approver": None,
            "auth_nonce": None, "auth_challenge": None, "auth_authenticator_id": None,
            "auth_expires": None, "auth_grant_id": None,
            "note": "classe %s : sans reçu" % action["class"],
        }
    raise ActionError(RECEIPT_REQUIRED, "action %s (classe %s) : reçu d'approbation humaine "
                      "ou approbation permanente requis" % (action["action_id"], action["class"]))


def _bind(db: Db, action: dict, auth: dict, *, from_states: tuple, by: str) -> dict:
    placeholders = ", ".join(["%s"] * len(from_states))
    rows = db.query(
        """
        UPDATE actions
           SET state = 'approved', auth_kind = %s, auth_receipt = %s, auth_approver = %s,
               auth_nonce = %s, auth_challenge = %s, auth_authenticator_id = %s::bigint,
               auth_expires_at = to_timestamp(%s::double precision), auth_grant_id = %s::bigint,
               auth_by = %s, approved_at = now(), last_actor = %s, last_note = %s
         WHERE action_id = %s AND state IN (__FROM__) AND replaced_by IS NULL AND digest = %s
        RETURNING action_id
        """.replace("__FROM__", placeholders),
        (auth["auth_kind"], auth["auth_receipt"], auth["auth_approver"], auth["auth_nonce"],
         auth["auth_challenge"], auth["auth_authenticator_id"], auth["auth_expires"],
         auth["auth_grant_id"], by or "", by or "", auth["note"], action["action_id"],
         *from_states, digest(action)),
    )
    if not rows:
        current = _require(db, action["action_id"])
        raise ActionError(STATE, "action %s modifiée entre-temps (état %s)"
                          % (action["action_id"], current["state"]))
    return _require(db, action["action_id"])


def approve(db: Db, action_id: str, receipt=None, *, standing: bool = False, policy=None,
            by: str = "", now: float | None = None) -> dict:
    """`proposed → approved` (ou nouvelle liaison si déjà `approved`).

    Le reçu est vérifié contre l'empreinte recalculée et l'`action_id`, son
    nonce est seulement CONTRÔLÉ (consommé au lancement). Un reçu `deny`
    valide annule l'action. Sans reçu : un grant qui couvre l'action
    (`standing=True`), ou rien pour une classe non gardée.
    """
    action = _require(db, action_id)
    if action["state"] not in ("proposed", "approved"):
        hint = {"failed": " : utiliser retry (nouveau reçu, même action)",
                "unknown": " : issue inconnue, voir reconcile / retry / replace"}
        raise ActionError(STATE, "action %s en %s, approbation impossible%s"
                          % (action_id, action["state"], hint.get(action["state"], "")))
    kind, auth = _authorize(db, action, receipt, standing=standing, policy=policy, now=now,
                            allow_deny=True)
    if kind == "deny":
        verdict = auth["verdict"]
        receipts.consume_nonce(db, verdict.approver, verdict.nonce,
                               by="%s (refus de %s)" % (by or "porte", action_id),
                               challenge=verdict.challenge,
                               authenticator_id=verdict.authenticator_id)
        return cancel(db, action_id, by=by, note="refusée par %s (reçu deny)" % verdict.approver)
    return _bind(db, action, auth, from_states=("proposed", "approved"), by=by)


# --------------------------------------------------------------------------
# exécution
# --------------------------------------------------------------------------

def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300] or type(exc).__name__


def _guarded(func, timeout: float, label: str):
    """Appelle `func` dans un fil, borné par `timeout`.

    Renvoie (terminé, valeur, exception). Un appel qui ne rend pas la main
    est abandonné (fil démon) : son issue est inconnue.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - toute exception = issue inconnue
            box["error"] = exc

    thread = threading.Thread(target=run, name=label, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None, None
    return True, box.get("value"), box.get("error")


def _call(connector, action: Action, timeout: float) -> Outcome:
    done, value, error = _guarded(lambda: connector.execute(action, action.action_id),
                                  timeout, "ameesh-action-%s" % action.action_id)
    if not done:
        return Outcome.unknown("délai dépassé (%gs) : issue inconnue" % timeout)
    if error is not None:
        return Outcome.unknown("%s pendant l'appel : %s" % (type(error).__name__, _one_line(error)))
    if not isinstance(value, Outcome):
        return Outcome.unknown("réponse du connecteur illisible : %r" % (value,))
    return value


def _settle(db: Db, action_id: str, attempt: int, from_state: str, outcome: Outcome, *,
            by: str, settled_by: str, note: str, stale_after: float | None = None) -> dict:
    error = None if outcome.state == "confirmed" else (outcome.detail or None)
    rows = db.query(
        "SELECT * FROM ameesh_action_settle(%s::text, %s::integer, %s::text, %s::text, "
        "%s::text, %s::text, %s::text, %s::text, %s::text, %s::double precision)",
        (action_id, int(attempt), from_state, outcome.state, outcome.external_ref, error,
         by or "", note, settled_by, stale_after),
    )
    return rows[0] if rows else {"result": STATE, "detail": "aucune réponse"}


def _note(outcome: Outcome, prefix: str) -> str:
    text = "%s : %s" % (prefix, outcome.state)
    if outcome.external_ref:
        text += " (%s)" % outcome.external_ref
    if outcome.detail:
        text += " — " + outcome.detail[:300]
    return text


def _refuse_state(action: dict) -> ActionError:
    state = action["state"]
    action_id = action["action_id"]
    if state == "launched":
        return ActionError(LAUNCHED, "action %s déjà lancée (tentative %s) : jamais de second "
                           "appel ; attendre l'issue, ou l'échéance puis reconcile"
                           % (action_id, action["attempts"]))
    if state == "unknown":
        return ActionError(UNKNOWN_OUTCOME, "action %s : issue inconnue, aucune nouvelle "
                           "tentative automatique (reconcile, retry si déduplication garantie, "
                           "ou décision humaine)" % action_id)
    if state == "proposed":
        return ActionError(RECEIPT_REQUIRED, "action %s non approuvée (classe %s) : reçu requis"
                           % (action_id, action["class"]))
    if state == "failed":
        return ActionError(STATE, "action %s en échec : retry avec un nouveau reçu" % action_id)
    return ActionError(STATE, "action %s en %s : rien à exécuter" % (action_id, state))


def execute(db: Db, action_id: str, connector, *, policy=None, by: str = "",
            now: float | None = None) -> dict:
    """Lance UNE tentative de l'action approuvée ; renvoie son issue.

    1. revérifie l'autorisation liée (reçu : forme, échéance, registre,
       signature, nonce encore libre, empreinte RECALCULÉE depuis la ligne) ;
    2. `ameesh_action_launch` : approved → launched + consommation du nonce
       (ou réservation sur le grant) + tentative, en une transaction ;
       deux exécutions concurrentes : une seule passe ;
    3. appel du connecteur, clé d'idempotence = action_id, borné par son délai ;
    4. issue enregistrée (`ameesh_action_settle`). Si l'enregistrement échoue,
       l'action reste `launched` : `recover` la traitera comme inconnue.
    """
    connectors_mod.check(connector)
    recover(db, action_id=action_id)
    action = _require(db, action_id, with_receipts=True)
    if action["connector"] != connector.name:
        raise ActionError(CONNECTOR, "action %s : connecteur %s, pas %s"
                          % (action_id, action["connector"], connector.name))
    if action["state"] == "proposed" and not action["requires_receipt"]:
        try:
            approve(db, action_id, None, by=by)
        except ActionError as exc:
            current = _require(db, action_id)
            if current["state"] not in ("proposed", "approved"):
                raise _refuse_state(current) from exc
            raise
        action = _require(db, action_id, with_receipts=True)
    if action["state"] != "approved":
        raise _refuse_state(action)
    expected = digest(action)
    if action["auth_kind"] == "receipt":
        try:
            verdict = _verify(db, action, action["auth_receipt"], policy,
                              expected_digest=expected, expect_decision="approve", now=now)
        except ActionError as exc:
            current = get(db, action_id)
            if current is not None and current["state"] != "approved":
                # une exécution concurrente a lancé (et consommé le reçu) entre-temps
                raise _refuse_state(current) from exc
            _event(db, action, "refused", by, "lancement refusé : %s" % exc)
            raise
        if (verdict.nonce, verdict.approver) != (action["auth_nonce"], action["auth_approver"]):
            raise ActionError(STATE, "action %s : reçu lié incohérent" % action_id)
    timeout = connectors_mod.timeout_of(connector)
    rows = db.query(
        "SELECT * FROM ameesh_action_launch(%s::text, %s::text, %s::text, %s::text, "
        "%s::bigint, %s::text, %s::double precision)",
        (action_id, expected, action["auth_kind"], action["auth_nonce"],
         action["auth_grant_id"], by or "", timeout),
    )
    launch = rows[0] if rows else {"result": STATE, "detail": "aucune réponse"}
    if launch.get("result") != "ok":
        code = launch.get("result") or STATE
        reason = launch.get("detail") or "lancement refusé"
        if code == STATE:
            current = get(db, action_id)
            if current is not None and current["state"] != "approved":
                raise _refuse_state(current)
        _event(db, action, "refused", by, "lancement refusé [%s] : %s" % (code, reason))
        raise ActionError(code, "action %s : %s" % (action_id, reason))
    attempt = int(launch["attempt"])

    # --- l'état `launched` est validé en base : l'appel externe peut partir ---
    outcome = _call(connector, to_action(action, attempt), timeout)
    note = _note(outcome, "tentative %d" % attempt)
    try:
        settled = _settle(db, action_id, attempt, "launched", outcome, by=by,
                          settled_by="connector", note=note)
        if settled.get("result") != "ok" and outcome.state != "unknown":
            # `recover` est passé entre-temps (échéance dépassée) : l'issue apprise
            # du connecteur reste certaine, elle règle l'inconnue.
            settled = _settle(db, action_id, attempt, "unknown", outcome, by=by,
                              settled_by="connector-late", note=note + " (tardive)")
    except DbError as exc:
        raise ActionError(UNRECORDED, "action %s : issue %s NON enregistrée (%s) ; l'action "
                          "reste launched et sera traitée comme inconnue"
                          % (action_id, outcome.state, _one_line(exc))) from exc
    if settled.get("result") != "ok":
        raise ActionError(UNRECORDED, "action %s : issue %s non enregistrée (%s)"
                          % (action_id, outcome.state, settled.get("detail")))
    return {"action_id": action_id, "attempt": attempt, "state": outcome.state,
            "external_ref": outcome.external_ref, "detail": outcome.detail,
            "released": settled.get("released")}


# --------------------------------------------------------------------------
# issue inconnue : reprise, réconciliation, nouvelle tentative, remplacement
# --------------------------------------------------------------------------

def recover(db: Db, *, action_id: str | None = None, grace: float = RECOVER_GRACE,
            force: bool = False, by: str = "recover") -> list[str]:
    """`launched` interrompue → `unknown` (aucun nouvel appel automatique).

    Une action reste `launched` si le processus est mort entre le lancement
    et l'enregistrement de l'issue. Passé son échéance (+ `grace`), elle est
    réputée interrompue. `force` ignore l'échéance (opérateur qui sait le
    processus mort).
    """
    sql = ("SELECT action_id, attempts FROM actions WHERE state = 'launched'")
    params: list = []
    if action_id is not None:
        sql += " AND action_id = %s"
        params.append(action_id)
    if not force:
        sql += " AND launch_deadline + make_interval(secs => %s::double precision) < now()"
        params.append(float(grace))
    moved = []
    for row in db.query(sql, tuple(params)):
        outcome = Outcome.unknown("interrompue après le lancement (processus arrêté ?) : "
                                  "issue inconnue, aucun nouvel appel automatique")
        settled = _settle(db, row["action_id"], int(row["attempts"]), "launched", outcome,
                          by=by, settled_by="recover", note=outcome.detail,
                          stale_after=None if force else float(grace))
        if settled.get("result") == "ok":
            moved.append(row["action_id"])
    return moved


def reconcile(db: Db, action_id: str, connector, *, by: str = "", force: bool = False) -> dict:
    """Retrouve l'issue d'une action `unknown` par une lecture du connecteur.

    Trouvée → `confirmed` ou `failed` (une réservation de grant est libérée
    sur `failed`). Introuvable → l'action reste `unknown` : décision humaine.
    """
    connectors_mod.check(connector)
    action = _require(db, action_id)
    if action["connector"] != connector.name:
        raise ActionError(CONNECTOR, "action %s : connecteur %s, pas %s"
                          % (action_id, action["connector"], connector.name))
    if action["state"] == "launched":
        recover(db, action_id=action_id, force=force, by=by or "reconcile")
        action = _require(db, action_id)
        if action["state"] == "launched":
            raise ActionError(LAUNCHED, "action %s lancée, échéance non atteinte : attendre "
                              "(ou --force si le processus est mort)" % action_id)
    if action["state"] != "unknown":
        raise ActionError(STATE, "action %s en %s : rien à réconcilier"
                          % (action_id, action["state"]))
    timeout = connectors_mod.timeout_of(connector)
    done, value, error = _guarded(lambda: connector.reconcile(to_action(action)), timeout,
                                  "ameesh-reconcile-%s" % action_id)
    found = value if (done and error is None and isinstance(value, Outcome)
                      and value.state in ("confirmed", "failed")) else None
    if found is None:
        why = ("délai dépassé" if not done else
               "%s : %s" % (type(error).__name__, _one_line(error)) if error is not None else
               value.detail if isinstance(value, Outcome) and value.detail else
               "introuvable")
        _event(db, action, "reconcile_none", by,
               "réconciliation sans résultat (%s) : décision humaine requise" % why)
        return {"action_id": action_id, "state": "unknown", "found": False, "detail": why,
                "external_ref": None}
    settled = _settle(db, action_id, int(action["attempts"]), "unknown", found, by=by,
                      settled_by="reconcile", note=_note(found, "réconciliation"))
    if settled.get("result") != "ok":
        current = _require(db, action_id)
        raise ActionError(STATE, "action %s réglée entre-temps (état %s)"
                          % (action_id, current["state"]))
    return {"action_id": action_id, "state": found.state, "found": True,
            "detail": found.detail, "external_ref": found.external_ref,
            "released": settled.get("released")}


def retry(db: Db, action_id: str, receipt=None, *, connector=None, standing: bool = False,
          policy=None, by: str = "", now: float | None = None) -> dict:
    """Autorise une nouvelle tentative de la MÊME action (même `action_id`).

    Permis après `failed` (échec certain), ou après `unknown` si le connecteur
    garantit la déduplication. Exige un NOUVEAU reçu (le précédent est
    consommé) — ou un grant, ou rien pour une classe non gardée. L'exécution
    suit par `execute`.
    """
    recover(db, action_id=action_id)
    action = _require(db, action_id)
    if connector is not None:
        connectors_mod.check(connector)
        if action["connector"] != connector.name:
            raise ActionError(CONNECTOR, "action %s : connecteur %s, pas %s"
                              % (action_id, action["connector"], connector.name))
    if action.get("replaced_by"):
        raise ActionError(REPLACED, "action %s remplacée par %s : plus de tentative"
                          % (action_id, action["replaced_by"]))
    state = action["state"]
    if state == "unknown":
        declared = action["dedupe"] == "guaranteed" and (
            connector is None or connector.dedupe == "guaranteed")
        if not declared:
            raise ActionError(DEDUPE, "action %s : issue inconnue et déduplication non garantie "
                              "par %s — reconcile, puis décision humaine qui assume le doublon "
                              "(replace)" % (action_id, action["connector"]))
    elif state == "launched":
        raise _refuse_state(action)
    elif state != "failed":
        raise ActionError(STATE, "action %s en %s : retry seulement après failed, ou unknown "
                          "avec déduplication garantie" % (action_id, state))
    _kind, auth = _authorize(db, action, receipt, standing=standing, policy=policy, now=now,
                             allow_deny=False)
    auth["note"] = "nouvelle tentative autorisée après %s — %s" % (state, auth["note"])
    return _bind(db, action, auth, from_states=(state,), by=by)


def replace(db: Db, action_id: str, receipt, *, policy=None, by: str = "",
            now: float | None = None) -> dict:
    """Crée une NOUVELLE action qui remplace une action `unknown`.

    Seulement avec un reçu humain signé qui assume le doublon (empreinte
    `assume_duplicate_digest`, voir le module). Consommation du nonce,
    liaison et création en une transaction. La nouvelle action naît
    `proposed` : elle exige sa propre approbation.
    """
    if receipt is None:
        raise ActionError(DECISION_REQUIRED, "remplacer %s exige une décision humaine signée "
                          "qui assume le doublon" % action_id)
    recover(db, action_id=action_id)
    action = _require(db, action_id)
    if action.get("replaced_by"):
        raise ActionError(REPLACED, "action %s déjà remplacée par %s"
                          % (action_id, action["replaced_by"]))
    if action["state"] != "unknown":
        raise ActionError(STATE, "action %s en %s : seul une action d'issue inconnue se "
                          "remplace" % (action_id, action["state"]))
    verdict = _verify(db, action, receipt, policy,
                      expected_digest=assume_duplicate_digest(action),
                      expect_decision="approve", now=now)
    document = receipts.parse_receipt(receipt).document
    new_id = new_action_id()
    new_digest = receipts.action_digest(dict(digest_fields(action), action_id=new_id))
    rows = db.query(
        "SELECT * FROM ameesh_action_replace(%s::text, %s::text, %s::text, %s::text, %s::text, "
        "%s::text, %s::text, %s::text, %s::bigint, %s::text)",
        (action_id, digest(action), new_id, new_digest, jcs.dumps(document), verdict.approver,
         verdict.nonce, verdict.challenge, verdict.authenticator_id, by or ""),
    )
    result = rows[0] if rows else {"result": STATE, "detail": "aucune réponse"}
    if result.get("result") != "ok":
        raise ActionError(result.get("result") or STATE, "remplacement de %s refusé : %s"
                          % (action_id, result.get("detail")))
    return _require(db, new_id)


def cancel(db: Db, action_id: str, *, by: str = "", note: str = "") -> dict:
    """`proposed | approved | failed → cancelled`. Jamais après le lancement
    d'une tentative dont l'issue n'est pas certaine."""
    rows = db.query(
        """
        UPDATE actions SET state = 'cancelled', finished_at = now(), last_actor = %s,
               last_note = %s
         WHERE action_id = %s AND state IN ('proposed', 'approved', 'failed')
        RETURNING action_id
        """, (by or "", note or "annulée", action_id))
    if not rows:
        action = _require(db, action_id)
        if action["state"] in ("launched", "unknown"):
            raise ActionError(STATE, "action %s en %s : l'effet a peut-être eu lieu, "
                              "annulation impossible (reconcile)" % (action_id, action["state"]))
        raise ActionError(STATE, "action %s en %s : annulation impossible"
                          % (action_id, action["state"]))
    return _require(db, action_id)


# --------------------------------------------------------------------------
# file des décisions humaines (C10, R17)
# --------------------------------------------------------------------------

def _for(rows: list[dict], human: str | None) -> list[dict]:
    if not human:
        return rows
    return [row for row in rows if not row.get("approvers") or human in row["approvers"]]


def decisions(db: Db, *, human: str | None = None) -> dict:
    """Ce qui attend un humain : approbations, issues inconnues, lots en attente.

    `human` (« human:<id> ») restreint aux actions qu'il peut approuver
    (liste `approvers` vide ou qui le contient) et aux lots qui lui sont
    assignés ou sans assignation.
    """
    if human is not None and not _HUMAN_RE.fullmatch(human):
        raise ActionError(INVALID, "« human:<id> » attendu, pas %r" % (human,))
    approvals = db.query(
        "SELECT %s FROM actions WHERE state = 'proposed' AND requires_receipt "
        "ORDER BY created_at" % _COLUMNS)
    unknown = db.query(
        "SELECT %s FROM actions WHERE state = 'unknown' AND replaced_by IS NULL "
        "ORDER BY updated_at" % _COLUMNS)
    interrupted = db.query(
        "SELECT %s FROM actions WHERE state = 'launched' AND launch_deadline < now() "
        "ORDER BY launched_at" % _COLUMNS)
    for row in unknown + interrupted:
        if row["state"] == "launched":
            row["hint"] = "interrompue : sera traitée comme inconnue (reconcile)"
        elif row["dedupe"] == "guaranteed":
            row["hint"] = "déduplication garantie : retry (nouveau reçu, même action) ou reconcile"
        else:
            row["hint"] = "reconcile ; introuvable → décision qui assume le doublon (replace)"
    waiting = work.list_items(db, state="waiting_human", limit=500)
    if human:
        short = human.split(":", 1)[1]
        waiting = [item for item in waiting
                   if not item.get("assignee") or item["assignee"] in (human, short)]
    return {
        "for": human,
        "approvals": _for(approvals, human),
        "unknown": _for(unknown, human),
        "interrupted": _for(interrupted, human),
        "waiting_human": waiting,
    }
