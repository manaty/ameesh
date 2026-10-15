# SPDX-License-Identifier: AGPL-3.0-only
"""Autorité du propriétaire : signatures Ed25519 liées au contenu et à l'échéance.

Règle de base (R4, spec §5 point 3) : **l'autorité ne se déduit jamais du
texte**. Un message ne porte l'autorité du propriétaire que si

1. l'expéditeur a une clé **publique** enregistrée dans `agent_registry` avec
   le rôle `owner`, non révoquée ;
2. la signature Ed25519 vérifie sur un payload canonique qui **est exactement**
   le contenu stocké (expéditeur, destinataire, texte, horodatage, nonce) ;
3. l'échéance n'est pas dépassée.

Une clé de rôle `agent` prouve la **provenance** (le message est bien de cet
agent, contenu et échéance couverts) mais ne confère **aucune** autorité
propriétaire et ne peut pas approuver. Tout le reste — y compris un message
signé « le propriétaire » — est un message d'agent ordinaire. Une signature
invalide ou expirée est signalée comme telle, jamais ignorée.

Les approbations (`mesh_approvals`) suivent le même modèle, avec en plus une
action, l'empreinte d'un artefact et une consommation unique.

Le SQL est dans le stockage (`storage.of(db).keys` / `.approvals` /
`.nonces`, spec §10).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass

from . import signing, storage
from .db import Db, DbError

DOMAIN = "agent-mesh/v1"
#: rôles de clé : seul 'owner' porte l'autorité du propriétaire
KEY_ROLES = ("owner", "agent")
DEFAULT_KEY_ROLE = "agent"
#: nom d'un détenteur de clé (même règle que agent_registry.name)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# a refusal of the key_holders guard (0364), as psql reports it
_GUARD_RE = re.compile(r"[^\n:]*(?:key|holder)[^\n]*own gesture[^\n]*")
DEFAULT_TTL = 24 * 3600.0
MAX_TTL = 30 * 24 * 3600.0
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)
_LOCAL_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?\s*$")


class AuthorityError(RuntimeError):
    """Signature, clé ou approbation refusée — toujours avec une raison lisible."""


@dataclass(frozen=True)
class Verdict:
    """Résultat d'une vérification.

    `ok` : la signature est valide, couvre ce contenu, la clé est connue, non
    révoquée, et l'échéance n'est pas dépassée. `role` distingue ensuite
    l'autorité du propriétaire (`owner`) de la simple authenticité d'un agent.
    """

    ok: bool
    reason: str
    fingerprint: str = ""
    expires_ts: float = 0.0
    role: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def authority(self) -> bool:
        """Autorité du propriétaire : signature valide d'une clé `owner`."""
        return self.ok and self.role == "owner"

    @property
    def authentic(self) -> bool:
        """Signature valide, propriétaire ou agent (provenance prouvée)."""
        return self.ok

    def short(self) -> str:
        if self.authority:
            return "signed by the owner"
        if self.ok:
            return "signed by an agent"
        return self.reason


# --------------------------------------------------------------------------
# temps, payloads canoniques
# --------------------------------------------------------------------------

def now_us() -> int:
    return time.time_ns() // 1000


def new_nonce() -> str:
    return secrets.token_hex(16)


def parse_ttl(text: str | None, default: float = DEFAULT_TTL) -> float:
    """« 30m », « 48h », « 7d », « 3600 » → secondes. Borné par MAX_TTL."""
    if text is None:
        return default
    match = _TTL_RE.match(text)
    if not match:
        raise AuthorityError("unreadable expiry: %r (expected 30m, 48h, 7d or seconds)" % text)
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds <= 0:
        raise AuthorityError("zero or negative expiry")
    if seconds > MAX_TTL:
        raise AuthorityError("expiry too far away (maximum %d days)" % (MAX_TTL // 86400))
    return seconds


def parse_expiry(text: str | None) -> int:
    """Échéance absolue : « 2026-10-04 08:00 » (heure locale) → microsecondes epoch."""
    if not text:
        raise AuthorityError("missing absolute expiry")
    match = _LOCAL_RE.match(text)
    if not match:
        raise AuthorityError("unreadable absolute expiry: %r (expected 2026-10-04 08:00)" % text)
    moment = time.mktime(time.strptime(
        "%s %s:%s" % (match.group(1), match.group(2), match.group(3) or "00"), "%Y-%m-%d %H:%M:%S"))
    return int(moment * 1_000_000)


def canonical(kind: str, fields: dict) -> bytes:
    """Payload signé : JSON canonique (clés triées, sans espaces) + domaine + type."""
    body = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ("%s|%s|%s" % (DOMAIN, kind, body)).encode("utf-8")


def message_fields(*, sender: str, recipient: str, body: str, ts_us: int,
                   expires_us: int, nonce: str) -> dict:
    return {
        "v": 1, "kind": "message", "from": sender, "to": recipient, "text": body,
        "ts": int(ts_us), "expires": int(expires_us), "nonce": nonce,
    }


def approval_fields(*, approver: str, action: str, artifact_kind: str, artifact_hash: str,
                    decision: str, ts_us: int, expires_us: int, nonce: str) -> dict:
    return {
        "v": 1, "kind": "approval", "approver": approver, "action": action,
        "artifact_kind": artifact_kind, "artifact_hash": artifact_hash,
        "decision": decision, "ts": int(ts_us), "expires": int(expires_us), "nonce": nonce,
    }


def _us(epoch_seconds) -> int:
    return int(round(float(epoch_seconds or 0) * 1_000_000))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise AuthorityError("unreadable base64: %s" % exc) from exc


# --------------------------------------------------------------------------
# clés enregistrées
# --------------------------------------------------------------------------

def key_info(db: Db, agent: str) -> dict | None:
    from .agent_names import resolve as resolve_agent_name
    agent = resolve_agent_name(agent, db=db, operation="authority.key_info")
    return storage.of(db).keys.info(agent)


def _refused(exc: DbError) -> None:
    """A refusal of the key guards of 0364 is an AuthorityError; any other
    database error goes up as is."""
    if "key_holders_one_owner" in str(exc):
        raise AuthorityError(
            "an owner key holder already exists: one at a time — replace its key "
            "(key register --human <its id> --replace, with the owner's database "
            "role)") from exc
    refused = _GUARD_RE.search(str(exc))
    if not refused:
        raise exc
    raise AuthorityError(refused.group(0).strip()) from exc


def register_key(db: Db, agent: str, public: bytes, *, role: str = DEFAULT_KEY_ROLE,
                 note: str | None = None) -> dict:
    """Enregistre la clé PUBLIQUE d'un agent.

    `role='owner'` est un acte du propriétaire (sa clé privée ne circule pas) ;
    `role='agent'` (défaut) n'authentifie qu'un agent et ne donne aucune
    autorité. Enregistrer une clé propriétaire pour un agent revient à lui
    déléguer l'autorité du propriétaire : à ne faire qu'en connaissance de cause.
    C'est le geste du propriétaire (#326 r2) : la base (0364) le refuse hors
    de son rôle de base, comme toute pose, tout changement ou tout retour
    d'une clé owner sur un agent ; une clé `agent` reste libre."""
    from .agent_names import resolve as resolve_agent_name
    agent = resolve_agent_name(agent, db=db, operation="authority.register_key")
    if len(public) != 32:
        raise AuthorityError("Ed25519 public key: 32 bytes expected")
    if role not in KEY_ROLES:
        raise AuthorityError("unknown key role: %r (%s)" % (role, ", ".join(KEY_ROLES)))
    fingerprint = signing.fingerprint(public)
    keys = storage.of(db).keys
    if (keys.info(agent) or {}).get("holder") == "human":
        raise AuthorityError(
            "%s holds a human's key: ameesh key register --human %s --public-key … "
            "--role %s" % (agent, agent, role))
    try:
        found = keys.register(agent, b64(public), fingerprint, role, note)
    except DbError as exc:
        _refused(exc)
    if not found:
        raise AuthorityError(
            "agent %s not in the registry: declare it before registering its key; "
            "a human's key (the owner's) is registered without any agent: "
            "ameesh key register --human %s --public-key <file.pub> --role owner"
            % (agent, agent))
    return {"agent": agent, "fingerprint": fingerprint, "role": role}


def register_human_key(db: Db, human: str, public: bytes, *,
                       role: str = DEFAULT_KEY_ROLE, replace: bool = False) -> dict:
    """Enregistre la clé PUBLIQUE d'un humain (#330) : `human` est
    `human:<id>` ou `<id>` ; la clé est rangée sous `<id>` dans `key_holders`,
    une identité qui ne détient qu'une clé — jamais un agent (ni exécutable,
    ni réveillable, ni dans un projet, une alerte ou une prise L165). C'est le
    nom que `decide --key` prend par défaut (`human:<id>` → `<id>`). Refusé si
    un agent porte ce nom : la clé du propriétaire ne s'attache jamais à
    l'identité d'un agent.

    Une clé existante n'est jamais écrasée (#326 r1) : seule une première clé
    s'enregistre, et une première clé owner seulement s'il n'en existe aucune
    (alerte `owner_key_registered`). Remplacer ou faire tourner une clé, même
    révoquée, est le geste du propriétaire : `replace`, avec le rôle de base
    du propriétaire — la base (0364) refuse tout autre rôle. `status` vaut
    `created` ou `replaced`."""
    name = human.split(":", 1)[1] if human.startswith("human:") else human
    if len(public) != 32:
        raise AuthorityError("Ed25519 public key: 32 bytes expected")
    if role not in KEY_ROLES:
        raise AuthorityError("unknown key role: %r (%s)" % (role, ", ".join(KEY_ROLES)))
    if not _NAME_RE.match(name):
        raise AuthorityError("invalid name for a key holder: %r" % name)
    fingerprint = signing.fingerprint(public)
    try:
        status = storage.of(db).keys.register_human(
            name, "human:" + name, b64(public), fingerprint, role, replace=replace)
    except DbError as exc:
        _refused(exc)
    if status == "agent":
        raise AuthorityError(
            "an agent is named %s: a human's key never sits on an agent's identity "
            "(choose another name, then decide … --as <name>)" % name)
    if status == "exists":
        raise AuthorityError(
            "%s already holds a key: it is never overwritten; replacing or rotating it "
            "is the owner's own gesture (key register --human %s --replace, with the "
            "owner's database role)" % (name, name))
    return {"agent": name, "human": "human:" + name, "fingerprint": fingerprint,
            "role": role, "status": status}


def revoke_key(db: Db, agent: str) -> bool:
    from .agent_names import resolve as resolve_agent_name
    agent = resolve_agent_name(agent, db=db, operation="authority.revoke_key")
    return storage.of(db).keys.revoke(agent)


def confirm_key(db: Db, name: str) -> int:
    """Le propriétaire confirme les enregistrements de la clé de `name`
    (#326 r3) : l'alerte `owner_key_registered` d'une clé owner posée sans son
    geste s'éteint. Geste du propriétaire, refusé par la base hors de son
    rôle ; rend l'id de l'événement `confirmed`."""
    try:
        event = storage.of(db).keys.confirm(name)
    except DbError as exc:
        if "key_events is written" not in str(exc):
            raise
        raise AuthorityError(
            "confirming a key is the owner's own gesture: run it with the owner's "
            "database role") from exc
    if not event:
        raise AuthorityError("no key registered for %s" % name)
    return event


def _valid_key(db: Db, agent: str, expected_fingerprint: str | None) -> tuple[dict | None, Verdict]:
    from .agent_names import resolve as resolve_agent_name
    agent = resolve_agent_name(agent, db=db, operation="authority._valid_key")
    info = key_info(db, agent)
    if not info or not info.get("public_key"):
        return None, Verdict(False, "no public key registered for %s" % agent)
    if info.get("key_revoked_ts"):
        return None, Verdict(False, "key of %s revoked" % agent)
    fingerprint = info.get("public_key_fingerprint")
    role = info.get("key_role") or DEFAULT_KEY_ROLE
    if expected_fingerprint and fingerprint != expected_fingerprint:
        return None, Verdict(
            False, "unknown key (fingerprint %s, registry %s)"
            % (expected_fingerprint[:12], (fingerprint or "?")[:12]), role=role)
    return info, Verdict(True, "known key", fingerprint or "", role=role)


# --------------------------------------------------------------------------
# vérification d'un message
# --------------------------------------------------------------------------

def verify_message(db: Db, row: dict) -> Verdict:
    """Vérifie un message normalisé (clés from/to/text/ts/signature/…)."""
    signature_text = row.get("signature")
    payload_text = row.get("signed_payload")
    if not signature_text:
        return Verdict(False, "unsigned")
    if not payload_text:
        return Verdict(False, "signature without payload")
    sender = row.get("from") or row.get("sender") or ""
    info, verdict = _valid_key(db, sender, row.get("signature_key"))
    if not info:
        return verdict

    expires_ts = row.get("signature_expires_ts") or 0.0
    # Alias input may have been signed before transport normalized it. Keep
    # the signed spellings byte-for-byte, but accept them only if both still
    # resolve to the identities stored on this message. All other fields are
    # rebuilt from the row as before; a changed body/date/nonce still fails.
    signed_sender = sender
    signed_recipient = row.get("to") or row.get("recipient") or ""
    try:
        prefix, kind, fields_text = payload_text.split("|", 2)
        fields = json.loads(fields_text)
        from .agent_names import resolve
        if (prefix == DOMAIN and kind == "message" and isinstance(fields, dict)
                and isinstance(fields.get("from"), str) and isinstance(fields.get("to"), str)
                and resolve(fields["from"], db=db, operation="authority.signature.sender")
                    == resolve(sender, db=db, operation="authority.signature.row_sender")
                and resolve(fields["to"], db=db, operation="authority.signature.recipient")
                    == resolve(signed_recipient, db=db, operation="authority.signature.row_recipient")):
            signed_sender = fields["from"]
            signed_recipient = fields["to"]
    except (ValueError, TypeError):
        pass
    expected = canonical("message", message_fields(
        sender=signed_sender,
        recipient=signed_recipient,
        body=row.get("text") or row.get("body") or "",
        ts_us=_us(row.get("ts")),
        expires_us=_us(expires_ts),
        nonce=row.get("nonce") or "",
    ))
    if expected.decode("utf-8") != payload_text:
        return Verdict(False, "the signature does not cover this content")
    public = unb64(info["public_key"])
    try:
        signature = unb64(signature_text)
    except AuthorityError as exc:
        return Verdict(False, str(exc))
    if not signing.verify(public, payload_text.encode("utf-8"), signature):
        return Verdict(False, "invalid Ed25519 signature")
    if expires_ts and expires_ts < time.time():
        return Verdict(
            False, "signature expired (%s)" % time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_ts)))
    role = info.get("key_role") or DEFAULT_KEY_ROLE
    fingerprint = info.get("public_key_fingerprint") or ""
    if role == "owner":
        return Verdict(True, "owner authority proven", fingerprint, expires_ts
                       if False else expires_ts, role)
    return Verdict(
        True, "authentic agent signature (no owner authority)",
        fingerprint, expires_ts, role)


def sign_message(seed: bytes, *, sender: str, recipient: str, body: str,
                 ttl: float = DEFAULT_TTL) -> dict:
    """Prépare un message signé : champs prêts pour `mail.send`."""
    backend = signing.backend()
    public = backend.public_from_seed(seed)
    ts_us = now_us()
    expires_us = ts_us + int(ttl * 1_000_000)
    nonce = new_nonce()
    payload = canonical("message", message_fields(
        sender=sender, recipient=recipient, body=body,
        ts_us=ts_us, expires_us=expires_us, nonce=nonce,
    ))
    return {
        "signed_payload": payload.decode("utf-8"),
        "signature": b64(backend.sign(seed, payload)),
        "signature_key": signing.fingerprint(public),
        "nonce": nonce,
        "created_us": ts_us,
        "expires_us": expires_us,
    }


# --------------------------------------------------------------------------
# approbations (porte de gouvernance)
# --------------------------------------------------------------------------

def hash_artifact(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def build_approval(seed: bytes, *, approver: str, action: str, artifact_kind: str,
                   artifact_hash: str, decision: str = "approved",
                   ttl: float = DEFAULT_TTL) -> dict:
    if not HASH_RE.match((artifact_hash or "").lower()):
        raise AuthorityError("artifact hash expected as sha256 hex (64 characters)")
    if decision not in ("approved", "rejected"):
        raise AuthorityError("unknown decision: %r" % decision)
    backend = signing.backend()
    public = backend.public_from_seed(seed)
    ts_us = now_us()
    expires_us = ts_us + int(ttl * 1_000_000)
    nonce = new_nonce()
    fields = approval_fields(
        approver=approver, action=action, artifact_kind=artifact_kind,
        artifact_hash=artifact_hash.lower(), decision=decision,
        ts_us=ts_us, expires_us=expires_us, nonce=nonce,
    )
    payload = canonical("approval", fields)
    return {
        "approver": approver,
        "action": action,
        "artifact_kind": artifact_kind,
        "artifact_hash": artifact_hash.lower(),
        "decision": decision,
        "nonce": nonce,
        "signed_payload": payload.decode("utf-8"),
        "signature": b64(backend.sign(seed, payload)),
        "signature_key": signing.fingerprint(public),
        "created_us": ts_us,
        "expires_us": expires_us,
    }


def create_approval(db: Db, seed: bytes, *, approver: str, action: str, artifact_kind: str,
                    artifact_hash: str, decision: str = "approved",
                    ttl: float = DEFAULT_TTL, meta: dict | None = None) -> dict:
    """Signe une approbation et l'enregistre — après contrôle de la clé enregistrée."""
    built = build_approval(
        seed, approver=approver, action=action, artifact_kind=artifact_kind,
        artifact_hash=artifact_hash, decision=decision, ttl=ttl,
    )
    info, verdict = _valid_key(db, approver, built["signature_key"])
    if not info:
        raise AuthorityError(
            "%s — register the public key of %s before signing an approval"
            % (verdict.reason, approver))
    if info.get("key_role") != "owner":
        raise AuthorityError(
            "the key of %s has role %r: an approval requires an owner key "
            "(\"agent-mesh key register %s --public-key … --role owner\")"
            % (approver, info.get("key_role") or DEFAULT_KEY_ROLE, approver))
    built["id"] = storage.of(db).approvals.create(
        approver=built["approver"], action=built["action"],
        artifact_kind=built["artifact_kind"], artifact_hash=built["artifact_hash"],
        decision=built["decision"], nonce=built["nonce"],
        signed_payload=built["signed_payload"], signature=built["signature"],
        signature_key=built["signature_key"], created_us=built["created_us"],
        expires_us=built["expires_us"], meta=meta)
    return built


def verify_approval(db: Db, row: dict) -> Verdict:
    payload_text = row.get("signed_payload")
    if not payload_text or not row.get("signature"):
        return Verdict(False, "unsigned approval")
    info, verdict = _valid_key(db, row.get("approver") or "", row.get("signature_key"))
    if not info:
        return verdict
    if info.get("key_role") != "owner":
        return Verdict(
            False, "key with role %r: only an owner key can approve"
            % (info.get("key_role") or DEFAULT_KEY_ROLE),
            info.get("public_key_fingerprint") or "", 0.0,
            info.get("key_role") or DEFAULT_KEY_ROLE)

    # Le payload est la référence : « agent-mesh/v1|approval|{json canonique} ».
    parts = payload_text.split("|", 2)
    if len(parts) != 3 or parts[0] != DOMAIN or parts[1] != "approval":
        return Verdict(False, "unreadable approval payload")
    try:
        stored = json.loads(parts[2])
    except ValueError:
        return Verdict(False, "unreadable approval payload")
    for key in ("approver", "action", "artifact_kind", "artifact_hash", "decision", "nonce"):
        if str(stored.get(key, "")) != str(row.get(key) or ""):
            return Verdict(False, "the signed payload does not match the approval (%s)" % key)

    public = unb64(info["public_key"])
    if not signing.verify(public, payload_text.encode("utf-8"), unb64(row["signature"])):
        return Verdict(False, "invalid Ed25519 signature")
    if row.get("consumed_ts") or row.get("consumed_at"):
        return Verdict(False, "approval already consumed")

    # L'échéance et l'horodatage de la ligne doivent être EXACTEMENT ceux qui
    # ont été signés : modifier expires_at en base ne ressuscite pas un reçu.
    if "expires" in stored and row.get("expires_ts") is not None:
        if _us(row["expires_ts"]) != int(stored["expires"]):
            return Verdict(False, "the signed expiry does not match the row "
                                  "(expires_at modified)")
    if "ts" in stored and row.get("created_ts") is not None:
        if _us(row["created_ts"]) != int(stored["ts"]):
            return Verdict(False, "the signed timestamp does not match the row "
                                  "(created_at modified)")
    expiry_state = nonce_state(db, row.get("approver") or "", row.get("nonce") or "")
    if expiry_state is not None:
        return Verdict(False, "nonce already consumed (%s)" % expiry_state,
                       info.get("public_key_fingerprint") or "")

    expires_ts = row.get("expires_ts")
    if expires_ts is None:
        # On ne sait pas la dater : on refuse (une approbation sans échéance
        # n'est pas une décision bornée).
        return Verdict(False, "unknown expiry")
    if float(expires_ts) < time.time():
        return Verdict(False, "approval expired")
    if row.get("unexpired") is False:
        return Verdict(False, "approval expired")
    return Verdict(True, "valid approval", info.get("public_key_fingerprint") or "",
                   float(expires_ts))


def list_approvals(db: Db, *, action: str | None = None, artifact_hash: str | None = None,
                   limit: int = 50) -> list[dict]:
    return storage.of(db).approvals.recent(action=action, artifact_hash=artifact_hash,
                                           limit=limit)


def find_approval(db: Db, action: str, artifact_hash: str, *, decision: str = "approved",
                  consume_by: str | None = None) -> tuple[dict | None, Verdict]:
    """La dernière approbation valide pour (action, hash), consommée si demandé."""
    rows = storage.of(db).approvals.candidates(action, artifact_hash, decision)
    last = Verdict(False, "no approval for %s %s" % (action, artifact_hash[:12]))
    for row in rows:
        if row.get("consumed_ts"):
            last = Verdict(False, "approval #%s already consumed" % row["id"])
            continue
        verdict = verify_approval(db, row)
        if verdict.ok:
            if consume_by:
                if not consume_approval(db, int(row["id"]), consume_by):
                    last = Verdict(False, "approval #%s consumed by someone else" % row["id"])
                    continue
                row["consumed_by"] = consume_by
                row["consumed_ts"] = time.time()
            return row, verdict
        last = verdict
    return None, last


def nonce_state(db: Db, approver: str, nonce: str) -> str | None:
    """Raison si ce nonce a déjà été consommé, sinon None."""
    if not approver or not nonce:
        return None
    row = storage.of(db).nonces.state(approver, nonce)
    if row is None:
        return None
    return "by %s on %s" % (
        row.get("consumed_by") or "?",
        time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("consumed_ts") or 0)))


def consume_approval(db: Db, approval_id: int, by: str) -> bool:
    """Consomme une approbation une seule fois, par (approver, nonce).

    Le reçu de consommation est une ligne de `mesh_consumed_nonces` : réinsérer
    l'approbation sous un nouvel id ne permet pas de la rejouer, et supprimer la
    ligne d'approbation ne supprime pas la preuve de consommation.
    """
    # L'INSERT ... ON CONFLICT est la décision atomique : s'il n'insère rien,
    # le nonce était déjà consommé. La mise à jour de la ligne n'est qu'un
    # affichage (la vérité est dans mesh_consumed_nonces, qui survit à la
    # suppression de l'approbation).
    return storage.of(db).approvals.consume(approval_id, by)
