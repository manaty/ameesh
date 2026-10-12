# SPDX-License-Identifier: AGPL-3.0-only
"""Enrôlement d'une passkey : lecture de l'attestation, PROPOSITION de canon.

Le registre de confiance vit dans le canon (spec §8.2) : un ajout est une PR
revue. Le service ne touche donc **jamais** la table `authenticators` ; il
vérifie la cérémonie `navigator.credentials.create` et écrit un fichier de
proposition (Markdown à frontmatter YAML) qu'un humain relit, confirme hors
bande avec la personne enrôlée, puis reporte dans le canon par une PR.

Vérifié ici (WebAuthn §7.1, sous-ensemble) : `clientDataJSON` (type
`webauthn.create`, challenge du jeton, origine autorisée, ni `crossOrigin` ni
`topOrigin`), `attestationObject` CBOR (`fmt`, `attStmt`, `authData`),
`rpIdHash`, drapeaux UP, UV et AT, cohérence BE/BS, identifiant de credential
égal à `rawId`, clé COSE ES256 ou EdDSA (sans partie privée). L'attestation
n'est **pas** vérifiée (format `none` attendu) : la proposition est donc au
niveau `standard` ; le niveau élevé exige une attestation vérifiée par le
relecteur (FIDO MDS), hors de ce service.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time

from .. import cose, jcs, receipts
from ..receipts import b64u_decode, b64u_encode
from .config import ensure_private_dir

FLAG_UP, FLAG_UV, FLAG_BE, FLAG_BS, FLAG_AT, FLAG_ED = (
    receipts.FLAG_UP, receipts.FLAG_UV, receipts.FLAG_BE, receipts.FLAG_BS,
    receipts.FLAG_AT, receipts.FLAG_ED)
MAX_CREDENTIAL_ID = 768   # octets ; 1024 caractères base64url (registre)
_APPROVER_RE = re.compile(r"^human:([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")


class EnrollError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def user_handle(approver: str) -> bytes:
    """Identifiant d'utilisateur WebAuthn stable, sans donnée personnelle."""
    return hashlib.sha256(b"ameesh-user/1\x00" + approver.encode("utf-8")).digest()[:16]


def check_client_data(client_json: bytes, *, kind: str, challenge: bytes, origins) -> dict:
    try:
        client = jcs.loads(client_json)
    except jcs.JcsError as exc:
        raise EnrollError("client_data", "unreadable clientDataJSON: %s" % exc) from exc
    if not isinstance(client, dict):
        raise EnrollError("client_data", "clientDataJSON: object expected")
    if client.get("type") != kind:
        raise EnrollError("client_data_type", "clientDataJSON.type: %s expected" % kind)
    try:
        signed = b64u_decode(client.get("challenge"), "clientDataJSON.challenge")
    except receipts.ReceiptError as exc:
        raise EnrollError("challenge_mismatch", str(exc)) from exc
    if not hmac.compare_digest(signed, challenge):
        raise EnrollError("challenge_mismatch", "the challenge is not the one for this link")
    if client.get("origin") not in origins:
        raise EnrollError("origin", "origin not allowed")
    if "crossOrigin" in client and client["crossOrigin"] is not False:
        raise EnrollError("cross_origin", "cross-origin context refused")
    if "topOrigin" in client:
        raise EnrollError("cross_origin", "cross-origin context refused (topOrigin)")
    return client


def parse_registration(*, attestation_object: bytes, client_data_json: bytes, raw_id: bytes,
                       challenge: bytes, rp_id: str, origins) -> dict:
    """Vérifie une création de credential ; rend l'entrée de registre proposée."""
    check_client_data(client_data_json, kind="webauthn.create", challenge=challenge,
                      origins=origins)
    try:
        attestation, _end = cose.decode(attestation_object)
    except cose.CborError as exc:
        raise EnrollError("attestation", "unreadable attestationObject: %s" % exc) from exc
    if not isinstance(attestation, dict) or set(attestation) != {"fmt", "attStmt", "authData"}:
        raise EnrollError("attestation", "attestationObject: fmt, attStmt, authData expected")
    fmt, statement, auth = attestation["fmt"], attestation["attStmt"], attestation["authData"]
    if not isinstance(fmt, str) or not re.fullmatch(r"[a-z0-9-]{1,32}", fmt):
        raise EnrollError("attestation", "unreadable attestation format")
    if not isinstance(statement, dict) or (fmt == "none" and statement):
        raise EnrollError("attestation", "attStmt inconsistent with format %s" % fmt)
    if not isinstance(auth, bytes) or len(auth) < 55:
        raise EnrollError("authenticator_data", "authData too short")
    if not hmac.compare_digest(auth[:32], hashlib.sha256(rp_id.encode("utf-8")).digest()):
        raise EnrollError("rp_id_mismatch", "rpIdHash differs from the service RP ID")
    flags = auth[32]
    if not flags & FLAG_UP:
        raise EnrollError("user_presence", "user presence not attested (UP=0)")
    if not flags & FLAG_UV:
        raise EnrollError("user_verification", "user not verified (UV=0)")
    if not flags & FLAG_AT:
        raise EnrollError("authenticator_data", "no attested credential (AT=0)")
    if flags & FLAG_BS and not flags & FLAG_BE:
        raise EnrollError("authenticator_data", "inconsistent flags: BS=1 without BE")
    aaguid = auth[37:53]
    length = int.from_bytes(auth[53:55], "big")
    if not 16 <= length <= MAX_CREDENTIAL_ID or 55 + length > len(auth):
        raise EnrollError("authenticator_data", "credential id of invalid length")
    credential = auth[55:55 + length]
    if not hmac.compare_digest(credential, raw_id):
        raise EnrollError("credential_id", "rawId differs from the attested credential")
    start = 55 + length
    try:
        _key, end = cose.decode(auth, start, allow_trailing=True)
        key_bytes = auth[start:end]
        key = cose.parse_cose_key(key_bytes)
        if flags & FLAG_ED:
            extensions, _done = cose.decode(auth, end)
            if not isinstance(extensions, dict):
                raise EnrollError("authenticator_data", "extensions: CBOR map expected")
        elif end != len(auth):
            raise EnrollError("authenticator_data", "authData: trailing bytes")
    except cose.CborError as exc:
        raise EnrollError("key", "public key refused: %s" % exc) from exc
    return {
        "facade": "webauthn",
        "credential_id": b64u_encode(credential),
        "public_key": b64u_encode(key_bytes),
        "key_fingerprint": hashlib.sha256(key_bytes).hexdigest(),
        "alg": key.alg,
        "aaguid": None if not any(aaguid) else aaguid.hex(),
        "backup_eligible": bool(flags & FLAG_BE),
        "backup_state": bool(flags & FLAG_BS),
        "sign_count": int.from_bytes(auth[33:37], "big"),
        "attestation_format": fmt,
        "level": "standard",
    }


def _q(value) -> str:
    """Scalaire YAML sûr : chaîne JSON (sous-ensemble des chaînes YAML entre guillemets)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return jcs.dumps(str(value)).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def write_proposal(directory: str, *, approver: str, entry: dict, rp_id: str, origin: str,
                   now: float | None = None) -> str:
    """Écrit la proposition (0600) ; rend le nom du fichier créé."""
    match = _APPROVER_RE.fullmatch(approver)
    if not match:
        raise EnrollError("approver", "approver \"human:<id>\" expected")
    directory = ensure_private_dir(directory)
    moment = time.time() if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(moment))
    name = "authenticator-%s-%s-%s.md" % (match.group(1), stamp, entry["key_fingerprint"][:12])
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
    canon_entry = {k: entry[k] for k in ("facade", "credential_id", "public_key", "aaguid",
                                         "level")}
    lines = [
        "---",
        "type: AuthenticatorProposal",
        "title: %s" % _q("Proposed passkey for %s" % approver),
        "status: proposed",
        "member: %s" % _q(approver),
        "created: %s" % _q(created),
        "generated: { by: \"ameesh-approve\", at: %s }" % _q(created),
        "rp_id: %s" % _q(rp_id),
        "origin: %s" % _q(origin),
        "key_fingerprint: %s" % _q(entry["key_fingerprint"]),
        "alg: %s" % _q(entry["alg"]),
        "backup_eligible: %s" % _q(entry["backup_eligible"]),
        "backup_state: %s" % _q(entry["backup_state"]),
        "attestation_format: %s" % _q(entry["attestation_format"]),
        "attestation_verified: false",
        "authenticator:",
    ]
    lines += ["  %s: %s" % (key, _q(value)) for key, value in canon_entry.items()]
    lines += [
        "---",
        "",
        "# Proposed passkey for `%s`" % approver,
        "",
        "Produced by ameesh-approve during an enrollment ceremony (single-use link",
        "created by `ameesh-approve enroll-link`). **Nothing is active**: ameesh only",
        "trusts the canon registry, at the merged revision.",
        "",
        "Before merging:",
        "",
        "1. confirm **out of band** with %s that this passkey was really created by" % approver,
        "   this person on %s (device, time): this is the defense against a fake" % created,
        "   enrollment (intercepted link, virtual authenticator);",
        "2. compare the key fingerprint `%s`;" % entry["key_fingerprint"][:16],
        "3. add the entry below to `authenticators` in the member's sheet, via a reviewed PR.",
        "",
        "Proposed level: `standard` (attestation not verified%s). The `eleve` level requires a"
        % (", syncable passkey BE=1" if entry["backup_eligible"] else ""),
        "hardware key (BE=0) whose attestation was verified by the reviewer.",
        "",
        "```yaml",
        "authenticators:",
    ]
    first = True
    for key, value in canon_entry.items():
        lines.append("%s%s: %s" % ("  - " if first else "    ", key, _q(value)))
        first = False
    lines += ["```", ""]
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        os.write(fd, "\n".join(lines).encode("utf-8"))
    finally:
        os.close(fd)
    return name
