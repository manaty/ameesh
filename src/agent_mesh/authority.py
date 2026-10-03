"""Autorité du propriétaire : signatures Ed25519 liées au contenu et à l'échéance.

Règle de base (R4, spec §5 point 3) : **l'autorité ne se déduit jamais du
texte**. Un message ne porte l'autorité du propriétaire que si

1. l'expéditeur a une clé **publique** enregistrée dans `agent_registry`, non
   révoquée ;
2. la signature Ed25519 vérifie sur un payload canonique qui **est exactement**
   le contenu stocké (expéditeur, destinataire, texte, horodatage, nonce) ;
3. l'échéance n'est pas dépassée.

Tout le reste — y compris un message signé « le propriétaire » — est un message
d'agent ordinaire. Une signature invalide ou expirée est signalée comme telle,
jamais ignorée.

Les approbations (`mesh_approvals`) suivent le même modèle, avec en plus une
action, l'empreinte d'un artefact et une consommation unique.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass

from . import signing
from .db import Db

DOMAIN = "agent-mesh/v1"
DEFAULT_TTL = 24 * 3600.0
MAX_TTL = 30 * 24 * 3600.0
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)
_LOCAL_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?\s*$")


class AuthorityError(RuntimeError):
    """Signature, clé ou approbation refusée — toujours avec une raison lisible."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    fingerprint: str = ""
    expires_ts: float = 0.0

    def __bool__(self) -> bool:
        return self.ok

    def short(self) -> str:
        if self.ok:
            return "signé %s" % self.fingerprint[:12]
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
        raise AuthorityError("échéance illisible : %r (attendu 30m, 48h, 7d ou des secondes)" % text)
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds <= 0:
        raise AuthorityError("échéance nulle ou négative")
    if seconds > MAX_TTL:
        raise AuthorityError("échéance trop lointaine (maximum %d jours)" % (MAX_TTL // 86400))
    return seconds


def parse_expiry(text: str | None) -> int:
    """Échéance absolue : « 2026-10-04 08:00 » (heure locale) → microsecondes epoch."""
    if not text:
        raise AuthorityError("échéance absolue manquante")
    match = _LOCAL_RE.match(text)
    if not match:
        raise AuthorityError("échéance absolue illisible : %r (attendu 2026-10-04 08:00)" % text)
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
        raise AuthorityError("base64 illisible : %s" % exc) from exc


# --------------------------------------------------------------------------
# clés enregistrées
# --------------------------------------------------------------------------

def key_info(db: Db, agent: str) -> dict | None:
    rows = db.query(
        """
        SELECT name, public_key, public_key_fingerprint,
               extract(epoch from key_updated_at)::float8 as key_updated_ts,
               extract(epoch from key_revoked_at)::float8 as key_revoked_ts
          FROM agent_registry WHERE name = %s
        """,
        (agent,),
    )
    return rows[0] if rows else None


def register_key(db: Db, agent: str, public: bytes, *, note: str | None = None) -> dict:
    """Enregistre la clé PUBLIQUE d'un agent (acte du propriétaire)."""
    if len(public) != 32:
        raise AuthorityError("clé publique Ed25519 : 32 octets attendus")
    fingerprint = signing.fingerprint(public)
    rows = db.query(
        """
        UPDATE agent_registry
           SET public_key = %s,
               public_key_fingerprint = %s,
               key_updated_at = now(),
               key_revoked_at = NULL,
               status_text = coalesce(%s, status_text),
               updated_at = now()
         WHERE name = %s
        RETURNING name
        """,
        (b64(public), fingerprint, note, agent),
    )
    if not rows:
        raise AuthorityError(
            "agent %s inconnu du registre : déclarez-le avant d'enregistrer sa clé" % agent)
    return {"agent": agent, "fingerprint": fingerprint}


def revoke_key(db: Db, agent: str) -> bool:
    rows = db.query(
        """
        UPDATE agent_registry
           SET key_revoked_at = now(), updated_at = now()
         WHERE name = %s AND public_key IS NOT NULL AND key_revoked_at IS NULL
        RETURNING name
        """,
        (agent,),
    )
    return bool(rows)


def _valid_key(db: Db, agent: str, expected_fingerprint: str | None) -> tuple[dict | None, Verdict]:
    info = key_info(db, agent)
    if not info or not info.get("public_key"):
        return None, Verdict(False, "aucune clé publique enregistrée pour %s" % agent)
    if info.get("key_revoked_ts"):
        return None, Verdict(False, "clé de %s révoquée" % agent)
    fingerprint = info.get("public_key_fingerprint")
    if expected_fingerprint and fingerprint != expected_fingerprint:
        return None, Verdict(
            False, "clé inconnue (empreinte %s, registre %s)"
            % (expected_fingerprint[:12], (fingerprint or "?")[:12]))
    return info, Verdict(True, "clé connue", fingerprint or "")


# --------------------------------------------------------------------------
# vérification d'un message
# --------------------------------------------------------------------------

def verify_message(db: Db, row: dict) -> Verdict:
    """Vérifie un message normalisé (clés from/to/text/ts/signature/…)."""
    signature_text = row.get("signature")
    payload_text = row.get("signed_payload")
    if not signature_text:
        return Verdict(False, "non signé")
    if not payload_text:
        return Verdict(False, "signature sans payload")
    sender = row.get("from") or row.get("sender") or ""
    info, verdict = _valid_key(db, sender, row.get("signature_key"))
    if not info:
        return verdict

    expires_ts = row.get("signature_expires_ts") or 0.0
    expected = canonical("message", message_fields(
        sender=sender,
        recipient=row.get("to") or row.get("recipient") or "",
        body=row.get("text") or row.get("body") or "",
        ts_us=_us(row.get("ts")),
        expires_us=_us(expires_ts),
        nonce=row.get("nonce") or "",
    ))
    if expected.decode("utf-8") != payload_text:
        return Verdict(False, "la signature ne couvre pas ce contenu")
    public = unb64(info["public_key"])
    try:
        signature = unb64(signature_text)
    except AuthorityError as exc:
        return Verdict(False, str(exc))
    if not signing.verify(public, payload_text.encode("utf-8"), signature):
        return Verdict(False, "signature Ed25519 invalide")
    if expires_ts and expires_ts < time.time():
        return Verdict(
            False, "signature expirée (%s)" % time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_ts)))
    return Verdict(True, "autorité du propriétaire prouvée",
                   info.get("public_key_fingerprint") or "", expires_ts)


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
        raise AuthorityError("empreinte d'artefact attendue en sha256 hex (64 caractères)")
    if decision not in ("approved", "rejected"):
        raise AuthorityError("décision inconnue : %r" % decision)
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
            "%s — enregistrez la clé publique de %s avant de signer une approbation"
            % (verdict.reason, approver))
    rows = db.query(
        """
        INSERT INTO mesh_approvals
            (approver, action, artifact_kind, artifact_hash, decision, nonce,
             signed_payload, signature, signature_key, expires_at, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                to_timestamp(%s::bigint / 1000000.0), %s::jsonb)
        RETURNING id
        """,
        (built["approver"], built["action"], built["artifact_kind"], built["artifact_hash"],
         built["decision"], built["nonce"], built["signed_payload"], built["signature"],
         built["signature_key"], built["expires_us"],
         json.dumps(meta or {}, ensure_ascii=False)),
    )
    built["id"] = int(rows[0]["id"])
    return built


def verify_approval(db: Db, row: dict) -> Verdict:
    payload_text = row.get("signed_payload")
    if not payload_text or not row.get("signature"):
        return Verdict(False, "approbation non signée")
    info, verdict = _valid_key(db, row.get("approver") or "", row.get("signature_key"))
    if not info:
        return verdict

    # Le payload est la référence : « agent-mesh/v1|approval|{json canonique} ».
    parts = payload_text.split("|", 2)
    if len(parts) != 3 or parts[0] != DOMAIN or parts[1] != "approval":
        return Verdict(False, "payload d'approbation illisible")
    try:
        stored = json.loads(parts[2])
    except ValueError:
        return Verdict(False, "payload d'approbation illisible")
    for key in ("approver", "action", "artifact_kind", "artifact_hash", "decision", "nonce"):
        if str(stored.get(key, "")) != str(row.get(key) or ""):
            return Verdict(False, "le payload signé ne correspond pas à l'approbation (%s)" % key)

    public = unb64(info["public_key"])
    if not signing.verify(public, payload_text.encode("utf-8"), unb64(row["signature"])):
        return Verdict(False, "signature Ed25519 invalide")
    if row.get("consumed_ts") or row.get("consumed_at"):
        return Verdict(False, "approbation déjà consommée")

    expires_ts = row.get("expires_ts")
    if expires_ts is None:
        # On ne sait pas la dater : on refuse (une approbation sans échéance
        # n'est pas une décision bornée).
        return Verdict(False, "échéance inconnue")
    if float(expires_ts) < time.time():
        return Verdict(False, "approbation expirée")
    if row.get("unexpired") is False:
        return Verdict(False, "approbation expirée")
    return Verdict(True, "approbation valide", info.get("public_key_fingerprint") or "",
                   float(expires_ts))


def list_approvals(db: Db, *, action: str | None = None, artifact_hash: str | None = None,
                   limit: int = 50) -> list[dict]:
    sql = """
        SELECT id, approver, action, artifact_kind, artifact_hash, decision, nonce,
               signed_payload, signature, signature_key,
               extract(epoch from created_at)::float8 as created_ts,
               extract(epoch from expires_at)::float8 as expires_ts,
               extract(epoch from consumed_at)::float8 as consumed_ts,
               consumed_by, unconsumed, unexpired, key_known, key_live, key_matches
          FROM mesh_approvals_status
         WHERE true
    """
    params: list = []
    if action:
        sql += " AND action = %s"
        params.append(action)
    if artifact_hash:
        sql += " AND artifact_hash = %s"
        params.append(artifact_hash.lower())
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))
    return db.query(sql, tuple(params))


def find_approval(db: Db, action: str, artifact_hash: str, *, decision: str = "approved",
                  consume_by: str | None = None) -> tuple[dict | None, Verdict]:
    """La dernière approbation valide pour (action, hash), consommée si demandé."""
    rows = db.query(
        """
        SELECT id, approver, action, artifact_kind, artifact_hash, decision, nonce,
               signed_payload, signature, signature_key,
               extract(epoch from expires_at)::float8 as expires_ts,
               extract(epoch from consumed_at)::float8 as consumed_ts,
               consumed_by, unconsumed, unexpired, key_known, key_live, key_matches
          FROM mesh_approvals_status
         WHERE action = %s AND artifact_hash = %s AND decision = %s
         ORDER BY id DESC LIMIT 20
        """,
        (action, artifact_hash.lower(), decision),
    )
    last = Verdict(False, "aucune approbation pour %s %s" % (action, artifact_hash[:12]))
    for row in rows:
        if row.get("consumed_ts"):
            last = Verdict(False, "approbation #%s déjà consommée" % row["id"])
            continue
        verdict = verify_approval(db, row)
        if verdict.ok:
            if consume_by:
                if not consume_approval(db, int(row["id"]), consume_by):
                    last = Verdict(False, "approbation #%s consommée par un autre" % row["id"])
                    continue
                row["consumed_by"] = consume_by
                row["consumed_ts"] = time.time()
            return row, verdict
        last = verdict
    return None, last


def consume_approval(db: Db, approval_id: int, by: str) -> bool:
    rows = db.query(
        """
        UPDATE mesh_approvals
           SET consumed_at = now(), consumed_by = %s
         WHERE id = %s AND consumed_at IS NULL
        RETURNING id
        """,
        (by, int(approval_id)),
    )
    return bool(rows)
