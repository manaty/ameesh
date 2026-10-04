# SPDX-License-Identifier: AGPL-3.0-only
"""Authentificateurs LOGICIELS pour les tests — jamais une vraie clé.

* `SoftWebAuthn` : se comporte comme une passkey (ES256 ou EdDSA) — produit
  `authenticatorData`, `clientDataJSON` et la signature sur
  `authenticatorData ‖ SHA-256(clientDataJSON)`, avec des drapeaux et des
  champs réglables pour fabriquer chaque falsification ;
* `SoftDevice` : clé d'appareil ES256 (façade `device-es256`) ;
* `SoftEd25519` : façade `ed25519`.

La signature ECDSA est faite ici, en Python pur (k aléatoire) : la génération
de clés et la signature n'existent que dans les tests, jamais dans `ameesh`.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

from ameesh import p256, receipts, signing

RP_ID = "approve.example.test"
ORIGIN = "https://approve.example.test"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def b64u(data: bytes) -> str:
    return receipts.b64u_encode(data)


def new_action_id() -> str:
    return "act_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def new_nonce() -> str:
    return b64u(os.urandom(16))


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# CBOR (encodeur minimal, pour fabriquer des COSE_Key)
# --------------------------------------------------------------------------

def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([major << 5 | value])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if value < 1 << (8 * size):
            return bytes([major << 5 | info]) + value.to_bytes(size, "big")
    raise ValueError("entier trop grand")


def cbor(value) -> bytes:
    if isinstance(value, bool) or value is None:
        return bytes([0xF5 if value is True else 0xF4 if value is False else 0xF6])
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, list):
        return _head(4, len(value)) + b"".join(cbor(item) for item in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


# --------------------------------------------------------------------------
# clés
# --------------------------------------------------------------------------

def es256_sign(private: int, message: bytes) -> bytes:
    """Signature ECDSA P-256/SHA-256, DER. Tests seulement."""
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = secrets.randbelow(p256.N - 1) + 1
        point = p256.scalar_mult(k, (p256.GX, p256.GY))
        r = point[0] % p256.N
        if r == 0:
            continue
        s = pow(k, -1, p256.N) * (e + r * private) % p256.N
        if s == 0:
            continue
        return p256.encode_der_signature(r, s)


class SoftES256Key:
    alg = "ES256"

    def __init__(self):
        self.private = secrets.randbelow(p256.N - 1) + 1
        self.point = p256.scalar_mult(self.private, (p256.GX, p256.GY))

    def sign(self, message: bytes) -> bytes:
        return es256_sign(self.private, message)

    def cose(self) -> bytes:
        x, y = self.point
        return cbor({1: 2, 3: -7, -1: 1, -2: x.to_bytes(32, "big"), -3: y.to_bytes(32, "big")})

    def sec1(self) -> bytes:
        return p256.encode_point(*self.point)


class SoftEd25519Key:
    alg = "EdDSA"

    def __init__(self):
        self.seed = secrets.token_bytes(32)
        self.public = signing.public_from_seed(self.seed)

    def sign(self, message: bytes) -> bytes:
        return signing.sign(self.seed, message)

    def cose(self) -> bytes:
        return cbor({1: 1, 3: -8, -1: 6, -2: self.public})


# --------------------------------------------------------------------------
# demandes et reçus
# --------------------------------------------------------------------------

def make_request(approver: str, *, action_id: str | None = None, digest: str | None = None,
                 decision: str = "approve", iat: int | None = None, ttl: int = 600,
                 nonce: str | None = None, standing: dict | None = None,
                 requested_by: str = "agent:deepseek7") -> dict:
    now = int(time.time()) if iat is None else iat
    request = {
        "v": 1, "approver": approver, "decision": decision,
        "summary_digest": sha("résumé recalculé par le service"),
        "requested_by": requested_by, "nonce": nonce or new_nonce(),
        "iat": now, "exp": now + ttl,
    }
    if standing is not None:
        request["standing"] = standing
    else:
        request["action_id"] = action_id or new_action_id()
        request["digest"] = digest or sha("action %s" % request["action_id"])
    return request


def make_receipt(request: dict, facade: str, credential_id: str, proof: dict) -> dict:
    return {"v": "ameesh-receipt/1", "request": request, "facade": facade,
            "credential_id": credential_id, "proof": proof}


class SoftWebAuthn:
    """Passkey logicielle : ES256 (défaut) ou EdDSA."""

    facade = "webauthn"

    def __init__(self, alg: str = "ES256", *, rp_id: str = RP_ID, origin: str = ORIGIN,
                 backup_eligible: bool = False, backup_state: bool = False):
        self.key = SoftES256Key() if alg == "ES256" else SoftEd25519Key()
        self.rp_id = rp_id
        self.origin = origin
        self.backup_eligible = backup_eligible
        self.backup_state = backup_state
        self.credential_id = b64u(os.urandom(16))
        self.sign_count = 0

    def public_key(self) -> str:
        return b64u(self.key.cose())

    def entry(self, level: str = "standard", aaguid: str | None = None) -> dict:
        return {"facade": self.facade, "credential_id": self.credential_id,
                "public_key": self.public_key(), "aaguid": aaguid, "level": level}

    def proof(self, chal: bytes, *, type: str = "webauthn.get", origin: str | None = None,
              rp_id: str | None = None, up: bool = True, uv: bool = True,
              be: bool | None = None, bs: bool | None = None, extra_flags: int = 0,
              cross_origin=None, top_origin=None, client_extra: dict | None = None,
              auth_suffix: bytes = b"", signer=None) -> dict:
        client = {"type": type, "challenge": b64u(chal), "origin": origin or self.origin}
        if cross_origin is not None:
            client["crossOrigin"] = cross_origin
        if top_origin is not None:
            client["topOrigin"] = top_origin
        client.update(client_extra or {})
        client_json = json.dumps(client, separators=(",", ":")).encode("utf-8")
        be = self.backup_eligible if be is None else be
        bs = self.backup_state if bs is None else bs
        flags = ((receipts.FLAG_UP if up else 0) | (receipts.FLAG_UV if uv else 0)
                 | (receipts.FLAG_BE if be else 0) | (receipts.FLAG_BS if bs else 0)
                 | extra_flags)
        self.sign_count += 1
        auth = (hashlib.sha256((rp_id or self.rp_id).encode("utf-8")).digest()
                + bytes([flags]) + self.sign_count.to_bytes(4, "big") + auth_suffix)
        signature = (signer or self.key).sign(auth + hashlib.sha256(client_json).digest())
        return {"authenticatorData": b64u(auth), "clientDataJSON": b64u(client_json),
                "signature": b64u(signature)}

    def receipt(self, request: dict, **kwargs) -> dict:
        return make_receipt(request, self.facade, self.credential_id,
                            self.proof(receipts.challenge(request), **kwargs))


class SoftDevice:
    """Clé d'appareil ES256 (application native) : signe le challenge brut."""

    facade = "device-es256"

    def __init__(self, cose: bool = False):
        self.key = SoftES256Key()
        self.as_cose = cose
        self.credential_id = "device:" + b64u(os.urandom(12))

    def public_key(self) -> str:
        return b64u(self.key.cose() if self.as_cose else self.key.sec1())

    def entry(self, level: str = "standard") -> dict:
        return {"facade": self.facade, "credential_id": self.credential_id,
                "public_key": self.public_key(), "level": level}

    def receipt(self, request: dict, signer=None) -> dict:
        signature = (signer or self.key).sign(receipts.challenge(request))
        return make_receipt(request, self.facade, self.credential_id,
                            {"signature": b64u(signature)})


class SoftEd25519:
    """Façade ed25519 (outils, tests, provenance) : signe le challenge brut."""

    facade = "ed25519"

    def __init__(self):
        self.key = SoftEd25519Key()
        self.credential_id = "ed25519:" + signing.fingerprint(self.key.public)[:32]

    def public_key(self) -> str:
        return b64u(self.key.public)

    def entry(self, level: str = "standard") -> dict:
        return {"facade": self.facade, "credential_id": self.credential_id,
                "public_key": self.public_key(), "level": level}

    def receipt(self, request: dict) -> dict:
        signature = self.key.sign(receipts.challenge(request))
        return make_receipt(request, self.facade, self.credential_id,
                            {"signature": b64u(signature)})
