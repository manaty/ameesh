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
  réserve le montant sur le grant) et crée la tentative — échéances du reçu
  ou du grant recontrôlées en SQL après le dernier verrou, à l'heure réelle
  de la base (règle de 0011_receipts.sql) ; ENSUITE seulement
  appelle le connecteur, clé d'idempotence = `action_id` ; enregistre
  `confirmed | failed | unknown`. Exception, délai dépassé, perte de
  connexion → `unknown`. Si l'issue ne peut pas être enregistrée, l'action
  reste `launched` et sera traitée comme inconnue (`recover`). Un appel
  abandonné après son délai qui rend plus tard une issue CERTAINE l'inscrit
  par une transition conditionnelle (`unknown` → issue, même tentative),
  jamais par-dessus un état déjà tranché ;
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
de chaque transition. Chaque transition (proposée, approuvée, lancée,
confirmée, échouée, inconnue, annulée, remplacée) écrit aussi, APRÈS coup,
une entrée lisible dans le fil du projet / du lot de l'action (§7.1, C5 :
`_publish`, par `fil.record` qui ne lève jamais) : sans secret, arguments
résumés.

Le SQL est dans le stockage (`storage.of(db).actions`, spec §10) : les
transitions qui accordent une autorité y restent des fonctions PL/pgSQL
(`ameesh_action_launch`, `ameesh_action_settle`, `ameesh_action_replace`).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from . import connectors as connectors_mod
from . import db as db_mod
from . import authority, fil, jcs, receipts, storage, work
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
#: un appel abandonné après son délai qui rend la main plus tard attend au plus
#: ce délai que `unknown` soit enregistré avant d'y inscrire son issue tardive
LATE_WAIT = 60.0
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

# codes de refus (stables) — en plus de ceux des reçus (receipts.*). Un refus
# dû à l'état de l'action est `state` (approuver, exécuter, annuler, réconcilier
# hors de l'état voulu ; exécuter une action d'issue inconnue compris) ; seuls
# `launched` (tentative en cours : course entre deux exécutions) et
# `receipt_required` (action pas encore approuvée) le précisent.
UNKNOWN_ACTION = "unknown_action"
STATE = "state"
LAUNCHED = "launched"
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

def get(db: Db, action_id: str, *, with_receipts: bool = False) -> dict | None:
    if not isinstance(action_id, str) or not _ACTION_ID_RE.fullmatch(action_id):
        return None
    return storage.of(db).actions.get(action_id, with_receipts=with_receipts)


def _require(db: Db, action_id: str, *, with_receipts: bool = False) -> dict:
    action = get(db, action_id, with_receipts=with_receipts)
    if action is None:
        raise ActionError(UNKNOWN_ACTION, "action %s introuvable" % (action_id,))
    return action


def list_actions(db: Db, *, state: str | None = None, project: str | None = None,
                 limit: int = 50) -> list[dict]:
    if state and state not in STATES:
        raise ActionError(INVALID, "état inconnu : %r (%s)" % (state, ", ".join(STATES)))
    return storage.of(db).actions.recent(state=state, project=project, limit=limit)


def attempts(db: Db, action_id: str) -> list[dict]:
    return storage.of(db).actions.attempts(action_id)


def events(db: Db, action_id: str) -> list[dict]:
    return storage.of(db).actions.events(action_id)


def _event(db: Db, action: dict, event: str, actor: str, note: str) -> None:
    """Événement informatif (refus, réconciliation sans résultat) : sans transition."""
    try:
        storage.of(db).actions.log_event(
            action["action_id"], int(action.get("attempts") or 0), event, action.get("state"),
            action.get("state"), actor or "", note or "")
    except DbError:
        pass  # le refus lui-même est rendu à l'appelant ; le journal est un plus


# --------------------------------------------------------------------------
# fil lisible (§7.1, C5) : une entrée par transition
# --------------------------------------------------------------------------

#: transitions écrites dans le fil, et leur libellé
THREAD_EVENTS = {
    "proposed": "proposée", "approved": "approuvée", "launched": "lancée",
    "confirmed": "confirmée", "failed": "échouée", "unknown": "— issue INCONNUE",
    "cancelled": "annulée", "replaced": "remplacée",
}
#: une clé d'argument qui ressemble à un secret : valeur jamais recopiée
_SECRET_KEY_RE = re.compile(r"secret|token|passw|key|auth|credential|cookie|signature|bearer",
                            re.I)
#: une valeur qui contient une longue suite base64/hex (jeton, clé…) : masquée
_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
_THREAD_DETAIL_MAX = 300


def args_summary(args, limit: int = 200) -> str:
    """Arguments résumés pour le fil : clés, valeurs courtes ; jamais un secret.

    Valeur d'une clé qui évoque un secret, ou qui contient une longue suite
    opaque (jeton, clé, signature) : masquée ; objet ou liste : sa taille
    seulement ; texte : 40 caractères au plus."""
    if not isinstance(args, dict) or not args:
        return "aucun"
    parts = []
    for key in sorted(args):
        value = args[key]
        if _SECRET_KEY_RE.search(str(key)):
            shown = "<masqué>"
        elif isinstance(value, dict):
            shown = "{%d clé(s)}" % len(value)
        elif isinstance(value, list):
            shown = "[%d élément(s)]" % len(value)
        elif isinstance(value, str):
            text = " ".join(value.split())
            if _OPAQUE_RE.search(text):
                shown = "<masqué, %d caractères>" % len(text)
            else:
                shown = text if len(text) <= 40 else text[:39] + "…"
        else:
            shown = jcs.dumps(value)
        parts.append("%s=%s" % (str(key)[:40], shown))
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one(text, limit: int = _THREAD_DETAIL_MAX) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def transition_text(action: dict, event: str, *, detail: str = "") -> str:
    """Texte lisible (autosuffisant) d'une transition, pour le fil."""
    action_id = action["action_id"]
    what = "%s %s sur %s" % (action["connector"], action["operation"],
                             action.get("target") or "—")
    attempt = int(action.get("attempts") or 0)
    label = THREAD_EVENTS.get(event, event)
    if event == "proposed":
        money = ("" if action.get("amount") is None
                 else ", montant %s %s" % (action["amount"], action.get("currency")))
        approvers = action.get("approvers") or []
        text = "Action %s proposée par %s : %s (projet %s, classe %s%s) — %s%s." % (
            action_id, action["proposed_by"], what, action["project"], action["class"], money,
            "approbation humaine (reçu) requise" if action["requires_receipt"] else "sans reçu",
            " ; approbateurs : %s" % ", ".join(approvers) if approvers else "")
        if action.get("replaces"):
            text += " Elle remplace %s (doublon assumé par %s)." % (
                action["replaces"], action.get("replace_approver") or "?")
        text += " Arguments : %s. Empreinte %s." % (args_summary(action.get("args")),
                                                   action["digest"])
        return text
    head = "Action %s %s (%s" % (action_id, label, what)
    head += ", tentative %d)" % attempt if attempt and event != "approved" else ")"
    if event == "approved":
        tail = _one(detail or action.get("last_note")) or "approuvée"
        return "%s : %s. Exécution : ameesh action execute %s." % (head, tail, action_id)
    if event == "launched":
        return "%s : appel du connecteur, clé d'idempotence %s." % (head, action_id)
    if event == "confirmed":
        ref = action.get("external_ref")
        return "%s%s%s." % (head, " : réf. %s" % ref if ref else "",
                            " — %s" % _one(detail) if detail else "")
    if event == "failed":
        return "%s : %s. Nouvelle tentative : ameesh action retry %s (nouveau reçu)." % (
            head, _one(detail or action.get("last_error")) or "échec certain", action_id)
    if event == "unknown":
        return ("%s : %s. Aucune nouvelle tentative automatique : ameesh action reconcile %s, "
                "puis décision humaine (ameesh decisions)." % (
                    head, _one(detail or action.get("last_error")) or "issue inconnue",
                    action_id))
    if event == "cancelled":
        return "%s : %s." % (head, _one(detail or action.get("last_note")) or "annulée")
    if event == "replaced":
        return "%s : remplacée par %s (doublon assumé par %s), à approuver." % (
            head, action.get("replaced_by") or "?", action.get("replace_approver")
            or _one(detail) or "?")
    return "%s : %s." % (head, _one(detail or action.get("last_note")))


def _author(actor: str) -> str:
    """Auteur d'une entrée : `human:`/`agent:` tel quel, sinon ameesh (l'outil)."""
    actor = (actor or "").strip()
    if _MEMBER_RE.fullmatch(actor):
        return actor
    return "ameesh:%s" % (_one(actor, 64) or "action")


def _publish(db: Db, action_id: str, event: str, actor: str, *, detail: str = "") -> None:
    """Écrit la transition dans le fil du projet / du lot de l'action (§7.1).

    Ne lève JAMAIS (`fil.record` non plus) : la transition est déjà faite en
    base ; un fil inaccessible se dit sur stderr. Aucun secret : ni reçu, ni
    nonce, ni jeton ; les arguments sont résumés (`args_summary`)."""
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        return
    try:
        action = get(db, action_id)
        if action is None:
            return
        fil.record(
            cfg, db, sender=_author(actor),
            recipients=list(action.get("approvers") or []) or ["all"],
            text=transition_text(action, event, detail=detail),
            project=action["project"], lot=action.get("work_item"),
            meta={"action_id": action_id, "event": event, "state": action["state"],
                  "attempt": int(action.get("attempts") or 0) or None})
    except Exception as exc:  # noqa: BLE001 - le fil est un plus, jamais un obstacle
        fil.warn("transition %s de %s non écrite dans le fil (%s)"
                 % (event, action_id, _one(exc, 200)))


def _close_lot_on_merge(db: Db, action_id: str, by: str) -> None:
    """Fusion confirmée par la porte (`git-merge`) : le lot lié passe `merged`
    et son jalon de fusion est posé (L29). Idempotent ; un lot fermé n'est
    jamais rouvert (le constat est écrit au fil). Ne lève JAMAIS : l'issue de
    l'action est déjà enregistrée, la fermeture du lot est une conséquence."""
    try:
        action = get(db, action_id)
        if action is None or action.get("connector") != "git-merge" \
                or action.get("state") != "confirmed" or not action.get("work_item"):
            return
        from . import work as work_mod
        done = work_mod.close_merged(
            db, int(action["work_item"]), sha=action.get("external_ref") or "",
            actor=action.get("auth_approver") or by or "git-merge",
            source="la porte (action %s)" % action_id, pr_ref=action.get("target"))
        if done["result"] == "refused":
            fil.warn("lot #%s : %s" % (action["work_item"], done["detail"]))
    except Exception as exc:  # noqa: BLE001 - conséquence, jamais un obstacle
        fil.warn("lot de l'action %s non fermé après la fusion (%s) : "
                 "ameesh work sync-github le rattrapera" % (action_id, _one(exc, 200)))


# --------------------------------------------------------------------------
# demandes d'approbation déposées auprès d'ameesh-approve (§9)
# --------------------------------------------------------------------------

#: événement du journal d'une demande déposée (sans transition)
APPROVAL_REQUESTED = "approval_requested"
_REQUEST_ID_RE = re.compile(r"\breq_[0-9a-z]{26}\b")


def record_approval_request(db: Db, action: dict, reply: dict, *, by: str) -> None:
    """Trace une demande déposée auprès d'ameesh-approve : un événement du
    journal (son identifiant, pour `fetch-receipt`) et une entrée du fil avec
    le LIEN à ouvrir sur le téléphone. Jamais le jeton de service (ameesh ne
    le connaît que pour l'en-tête de la requête). Ne lève pas."""
    request_id = str(reply.get("request_id") or "")
    approver = str(reply.get("approver") or "?")
    duplicate = reply.get("assume_duplicate") is True
    until = reply.get("link_exp") or reply.get("exp")
    moment = time.strftime("%H:%M", time.localtime(float(until))) if until else "?"
    note = "%s envoyée à %s (ameesh-approve)%s, lien valable jusqu'à %s" % (
        request_id, approver, " — DOUBLON À ASSUMER" if duplicate else "", moment)
    _event(db, action, APPROVAL_REQUESTED, by, note)
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        return
    what = "%s %s sur %s" % (action["connector"], action["operation"],
                             action.get("target") or "—")
    if duplicate:
        ask = ("Décision demandée à %s : ASSUMER LE DOUBLON de l'action %s (%s, issue "
               "inconnue) et la remplacer" % (approver, action["action_id"], what))
    else:
        ask = "Demande d'approbation de l'action %s (%s) envoyée à %s" % (
            action["action_id"], what, approver)
    text = ("%s : ouvrir %s sur son téléphone (lien à usage unique, valable jusqu'à %s ; "
            "empreinte signée %s). Reçu : ameesh action fetch-receipt %s." % (
                ask, reply.get("link") or "?", moment, reply.get("digest") or "?",
                action["action_id"]))
    try:
        fil.record(cfg, db, sender=_author(by), recipients=[approver], text=text,
                   project=action["project"], lot=action.get("work_item"),
                   meta={"action_id": action["action_id"], "event": APPROVAL_REQUESTED,
                         "request_id": request_id})
    except Exception as exc:  # noqa: BLE001 - fil.record ne lève pas ; prudence
        fil.warn("demande %s non écrite dans le fil (%s)" % (request_id, _one(exc, 200)))


def last_approval_request(db: Db, action_id: str) -> str | None:
    """Identifiant de la dernière demande déposée pour l'action (journal)."""
    note = storage.of(db).actions.last_event_note(action_id, APPROVAL_REQUESTED)
    match = _REQUEST_ID_RE.search(note) if note is not None else None
    return match.group(0) if match else None


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
    storage.of(db).actions.propose(
        action_id=action_id, project=project, work_item=work_item, proposed_by=proposed_by,
        connector=connector.name, operation=operation, target=target,
        args_json=canonical_args, action_class=chosen, amount=amount, currency=currency,
        policy_version=policy_version, digest=action_digest, dedupe=connector.dedupe,
        requires_receipt=requires, approvers_json=jcs.dumps(approvers),
        note="proposée (%s, %s)" % (chosen, "reçu requis" if requires else "sans reçu"))
    action = _require(db, action_id)
    if digest(action) != action_digest:  # l'aller-retour jsonb ne doit rien changer
        raise ActionError(INVALID, "empreinte instable après enregistrement (%s)" % action_id)
    _publish(db, action_id, "proposed", proposed_by)
    return action


# --------------------------------------------------------------------------
# autorisation : reçu, grant, ou rien (classes non gardées)
# --------------------------------------------------------------------------

def _covering_grant(db: Db, action: dict) -> int | None:
    """Un grant qui couvre l'action (lecture seule, sans verrou : simple
    pré-filtre ; la réservation se fait au lancement, atomiquement, et y
    recontrôle tout sous verrou). Une réservation vivante de l'action passe en
    premier."""
    amount = int(action.get("amount") or 0)
    rows = storage.of(db).actions.covering_grants(
        action["action_id"], amount, action["connector"], action["operation"],
        action["class"], action.get("currency"))
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
    bound = storage.of(db).actions.bind(action["action_id"], from_states=from_states,
                                        digest=digest(action), auth=auth, by=by)
    if not bound:
        current = _require(db, action["action_id"])
        raise ActionError(STATE, "action %s modifiée entre-temps (état %s)"
                          % (action["action_id"], current["state"]))
    _publish(db, action["action_id"], "approved", by)
    return _require(db, action["action_id"])


def approve(db: Db, action_id: str, receipt=None, *, standing: bool = False, policy=None,
            by: str = "", now: float | None = None) -> dict:
    """`proposed → approved` (ou nouvelle liaison si déjà `approved`).

    Le reçu est vérifié contre l'empreinte recalculée et l'`action_id`, son
    nonce est seulement CONTRÔLÉ (consommé au lancement). Un reçu `deny`
    valide annule l'action, une fois son nonce consommé (`consume_nonce` :
    échéances recontrôlées en SQL, à l'heure réelle) ; non consommé (échu,
    rejoué, authentificateur révoqué entre-temps), il est refusé et l'action
    ne change pas. Sans reçu : un grant qui couvre l'action
    (`standing=True`), ou rien pour une classe non gardée.
    """
    action = _require(db, action_id)
    if action["state"] not in ("proposed", "approved"):
        hint = {"failed": " : utiliser retry (nouveau reçu, même action)",
                "unknown": " : issue inconnue, voir reconcile / retry / replace"}
        raise ActionError(STATE, "action %s en %s, approbation impossible%s"
                          % (action_id, action["state"], hint.get(action["state"], "")))
    policy = policy if policy is not None else policy_from_env()
    kind, auth = _authorize(db, action, receipt, standing=standing, policy=policy, now=now,
                            allow_deny=True)
    if kind == "deny":
        verdict = auth["verdict"]
        try:
            consumed = receipts.consume_nonce(
                db, verdict.approver, verdict.nonce,
                by="%s (refus de %s)" % (by or "porte", action_id), exp=verdict.exp,
                iat=verdict.iat, clock_skew=policy.clock_skew, challenge=verdict.challenge,
                authenticator_id=verdict.authenticator_id)
        except receipts.DeadlineError as exc:
            raise ActionError(exc.code, "refus signé non consommé : %s" % exc) from exc
        if not consumed:
            state = authority.nonce_state(db, verdict.approver, verdict.nonce)
            if state:
                raise ActionError(receipts.REPLAY, "refus signé : nonce déjà consommé (%s)" % state)
            raise ActionError(receipts.REVOKED, "refus signé : authentificateur révoqué avant "
                              "la consommation du nonce")
        return cancel(db, action_id, by=by, note="refusée par %s (reçu deny)" % verdict.approver)
    return _bind(db, action, auth, from_states=("proposed", "approved"), by=by)


# --------------------------------------------------------------------------
# exécution
# --------------------------------------------------------------------------

def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300] or type(exc).__name__


def _guarded(func, timeout: float, label: str, late=None):
    """Appelle `func` dans un fil, borné par `timeout`.

    Renvoie (terminé, valeur, exception). Un appel qui ne rend pas la main
    est abandonné (fil démon) : son issue est inconnue. S'il la rend plus
    tard, `late(valeur, exception)` est appelé dans ce fil. Sous verrou :
    le résultat va à l'appelant OU à `late`, jamais aux deux, jamais perdu.
    """
    box: dict = {}
    lock = threading.Lock()

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - toute exception = issue inconnue
            box["error"] = exc
        with lock:
            box["done"] = True
            abandoned = box.get("abandoned", False)
        if abandoned and late is not None:
            try:
                late(box.get("value"), box.get("error"))
            except Exception:  # noqa: BLE001 - fil démon : rien à qui remonter
                pass

    thread = threading.Thread(target=run, name=label, daemon=True)
    thread.start()
    thread.join(timeout)
    with lock:
        if not box.get("done"):
            box["abandoned"] = True
            return False, None, None
    return True, box.get("value"), box.get("error")


def _call(connector, action: Action, timeout: float, late=None) -> Outcome:
    done, value, error = _guarded(lambda: connector.execute(action, action.action_id),
                                  timeout, "ameesh-action-%s" % action.action_id, late=late)
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
    row = storage.of(db).actions.settle(
        action_id, attempt, from_state=from_state, state=outcome.state,
        external_ref=outcome.external_ref, error=error, by=by, note=note,
        settled_by=settled_by, stale_after=stale_after)
    return row if row is not None else {"result": STATE, "detail": "aucune réponse"}


def _note(outcome: Outcome, prefix: str) -> str:
    text = "%s : %s" % (prefix, outcome.state)
    if outcome.external_ref:
        text += " (%s)" % outcome.external_ref
    if outcome.detail:
        text += " — " + outcome.detail[:300]
    return text


def _late_db(db: Db):
    """Connexion propre au fil tardif : celle de l'appelant peut être fermée
    entre-temps, ou prise dans une autre transaction. À défaut, la sienne."""
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        return db
    try:
        return db_mod.connect(cfg, driver=getattr(db, "name", None))
    except DbError:
        return db


def _record_late(db: Db, action_id: str, attempt: int, value, error, *, by: str) -> None:
    """Issue rendue APRÈS le délai par l'appel abandonné (dans son fil).

    Certaine (`confirmed` | `failed`) : elle règle l'inconnue par une
    transition conditionnelle (`ameesh_action_settle`, `unknown` → issue :
    action ET tentative `attempt` encore `unknown`), avec la référence
    externe. Jamais d'écrasement : si une réconciliation, une nouvelle
    tentative ou un humain a tranché entre-temps, l'issue tardive est
    seulement journalisée (`late_ignored`). Incertaine : journalisée
    (`late_unknown`), rien ne change.
    """
    if error is not None:
        outcome = Outcome.unknown("%s après le délai : %s" % (type(error).__name__,
                                                               _one_line(error)))
    elif not isinstance(value, Outcome):
        outcome = Outcome.unknown("réponse tardive du connecteur illisible : %r" % (value,))
    else:
        outcome = value
    note = _note(outcome, "tentative %d, issue tardive (après le délai)" % attempt)
    target = _late_db(db)
    try:
        event = "late_unknown"
        if outcome.state in ("confirmed", "failed"):
            settled = _settle(target, action_id, attempt, "unknown", outcome, by=by,
                              settled_by="connector-late", note=note)
            if settled.get("result") == "ok":
                _publish(target, action_id, outcome.state, by or "connector-late",
                         detail="issue tardive, après le délai%s" % (
                             " — %s" % outcome.detail if outcome.detail else ""))
                if outcome.state == "confirmed":
                    _close_lot_on_merge(target, action_id, by or "connector-late")
                return
            event = "late_ignored"
        current = get(target, action_id)
        if current is None:
            return
        if event == "late_ignored":
            note += " — ignorée : action déjà %s (tentative %s), rien n'est écrasé" % (
                current["state"], current["attempts"])
        _event(target, dict(current, attempts=attempt), event, by, note)
    except DbError:
        pass  # l'action reste `unknown` : la réconciliation tranchera
    finally:
        if target is not db:
            target.close()


def _refuse_state(action: dict) -> ActionError:
    state = action["state"]
    action_id = action["action_id"]
    if state == "launched":
        return ActionError(LAUNCHED, "action %s déjà lancée (tentative %s) : jamais de second "
                           "appel ; attendre l'issue, ou l'échéance puis reconcile"
                           % (action_id, action["attempts"]))
    if state == "unknown":
        return ActionError(STATE, "action %s : issue inconnue, aucune nouvelle "
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
       (`ameesh_receipt_consume`) ou réservation sur le grant
       (`ameesh_standing_reserve`) + tentative, en une transaction qui prend
       ses verrous puis recontrôle les échéances (exp et iat signés, ou until
       du grant) à l'heure réelle de la base, avant d'écrire et après ;
       échue → refus `expired` (ou `iat_future`), rien n'est écrit ; deux
       exécutions concurrentes : une seule passe ;
    3. appel du connecteur, clé d'idempotence = action_id, borné par son délai ;
    4. issue enregistrée (`ameesh_action_settle`). Si l'enregistrement échoue,
       l'action reste `launched` : `recover` la traitera comme inconnue.
       Délai dépassé : `unknown` ; si l'appel abandonné rend la main plus tard
       avec une issue certaine, son fil l'inscrit (`_record_late`).
    """
    connectors_mod.check(connector)
    policy = policy if policy is not None else policy_from_env()
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
    launch = storage.of(db).actions.launch(
        action_id, digest=expected, auth_kind=action["auth_kind"],
        auth_nonce=action["auth_nonce"], auth_grant_id=action["auth_grant_id"], by=by,
        timeout=timeout, clock_skew=policy.clock_skew)
    if launch is None:
        launch = {"result": STATE, "detail": "aucune réponse"}
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
    _publish(db, action_id, "launched", by)
    recorded = threading.Event()

    def late(value, error) -> None:
        # l'appel abandonné a fini par rendre la main : attendre que `unknown`
        # soit enregistré ci-dessous, puis y inscrire l'issue si elle est certaine
        recorded.wait(LATE_WAIT)
        _record_late(db, action_id, attempt, value, error, by=by)

    # --- l'état `launched` est validé en base : l'appel externe peut partir ---
    outcome = _call(connector, to_action(action, attempt), timeout, late=late)
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
    finally:
        recorded.set()
    if settled.get("result") != "ok":
        raise ActionError(UNRECORDED, "action %s : issue %s non enregistrée (%s)"
                          % (action_id, outcome.state, settled.get("detail")))
    _publish(db, action_id, outcome.state, by, detail=outcome.detail or "")
    if outcome.state == "confirmed":
        _close_lot_on_merge(db, action_id, by)
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
    moved = []
    for row in storage.of(db).actions.launched(action_id=action_id, grace=grace, force=force):
        outcome = Outcome.unknown("interrompue après le lancement (processus arrêté ?) : "
                                  "issue inconnue, aucun nouvel appel automatique")
        settled = _settle(db, row["action_id"], int(row["attempts"]), "launched", outcome,
                          by=by, settled_by="recover", note=outcome.detail,
                          stale_after=None if force else float(grace))
        if settled.get("result") == "ok":
            moved.append(row["action_id"])
            _publish(db, row["action_id"], "unknown", by, detail=outcome.detail)
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
    _publish(db, action_id, found.state, by or "reconcile",
             detail="réconciliation%s" % (" — %s" % found.detail if found.detail else ""))
    if found.state == "confirmed":
        _close_lot_on_merge(db, action_id, by or "reconcile")
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
    `assume_duplicate_digest`, voir le module). Consommation du nonce
    (`ameesh_receipt_consume`), liaison et création en une transaction, qui
    verrouille l'action remplacée puis recontrôle l'échéance de la décision
    à l'heure réelle de la base, avant d'écrire et après (échue entre-temps
    → refus `expired`, rien n'est créé, nonce intact). La nouvelle action
    naît `proposed` : elle exige sa propre approbation.
    """
    if receipt is None:
        raise ActionError(DECISION_REQUIRED, "remplacer %s exige une décision humaine signée "
                          "qui assume le doublon" % action_id)
    policy = policy if policy is not None else policy_from_env()
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
    result = storage.of(db).actions.replace(
        action_id, digest=digest(action), new_id=new_id, new_digest=new_digest,
        receipt_json=jcs.dumps(document), approver=verdict.approver, nonce=verdict.nonce,
        challenge=verdict.challenge, authenticator_id=verdict.authenticator_id, by=by,
        clock_skew=policy.clock_skew)
    if result is None:
        result = {"result": STATE, "detail": "aucune réponse"}
    if result.get("result") != "ok":
        raise ActionError(result.get("result") or STATE, "remplacement de %s refusé : %s"
                          % (action_id, result.get("detail")))
    _publish(db, action_id, "replaced", by, detail=verdict.approver)
    _publish(db, new_id, "proposed", by)
    return _require(db, new_id)


def cancel(db: Db, action_id: str, *, by: str = "", note: str = "") -> dict:
    """`proposed | approved | failed → cancelled`. Jamais après le lancement
    d'une tentative dont l'issue n'est pas certaine."""
    cancelled = storage.of(db).actions.cancel(action_id, by=by, note=note)
    if not cancelled:
        action = _require(db, action_id)
        if action["state"] in ("launched", "unknown"):
            raise ActionError(STATE, "action %s en %s : l'effet a peut-être eu lieu, "
                              "annulation impossible (reconcile)" % (action_id, action["state"]))
        raise ActionError(STATE, "action %s en %s : annulation impossible"
                          % (action_id, action["state"]))
    _publish(db, action_id, "cancelled", by, detail=note or "annulée")
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
    approvals, unknown, interrupted = storage.of(db).actions.decision_queues()
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
