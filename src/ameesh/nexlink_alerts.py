# SPDX-License-Identifier: AGPL-3.0-only
"""Canal `nexlink` : les alertes du mesh poussées dans l'app de l'owner (#3536).

Spec owner du 25/10/2026 (décision #138440, option a) : le canal remplace
ntfy à terme, le hub Nexlink ne voit jamais le clair, et l'app déchiffre avec
la clé *key-wrap* X25519 du compte owner (génération 1, enregistrée en prod
par MP-9b). Ce module ne dépend ni de `notify` ni de la base : il porte le
format et la cryptographie, testables seuls.

**Enveloppe** (claire, c'est tout ce que le hub stocke) ::

    {"v": 1, "alert_id": <sha256 hex>, "event": "raised"|"resolved",
     "severity": "urgent"|"normal", "occurred_at": "…Z",
     "ciphertext": <base64 ct||tag>, "nonce": <base64 12 o>,
     "wraps": [{"key_id": …, "gen": 1, "wrap": <base64>}]}

Sans clé active : ``{"content_free": true}`` et ni chiffré ni enveloppe.

**Contenu** : JSON UTF-8 `{title, body, kind, event, lot, decision, host,
since}` chiffré en AES-256-GCM sous une CEK aléatoire de 32 octets, avec
``AAD = "1|<alert_id>|<occurred_at>"`` (le ``v`` de l'enveloppe, l'identité de
l'alerte et son instant : un chiffré ne se rejoue pas sous une autre alerte).

**Enveloppe de clé** : le format de `wrapToX25519` de Nexlink
(``shared/message-crypto/src/x25519-wrap.ts``), reproduit ici à l'octet :

* ``eph`` = paire X25519 fraîche ; ``shared = X25519(eph_priv, recipient_pub)``
  (refus si tout à zéro) ;
* ``kek = HKDF-SHA-256(ikm=shared, salt="nexlink/x25519-wrap/v1",
  info=eph_pub ‖ recipient_pub, L=32)`` ;
* ``aad = "nexlink/x25519-wrap/v1" ‖ recipient_account_id(16) ‖
  recipient_gen(4, BE) ‖ eph_pub(32) ‖ subject_kind(1) ‖
  subject_actor_id(16) ‖ subject_gen(4, BE)`` ;
* ``wrap = 0x01 ‖ eph_pub(32) ‖ nonce(12) ‖ tag(16) ‖ ct``.

Le sujet est ``meshAlert = 5`` (le 4 est `oprfBackup`), avec l'UUID fixe
``255d5ab8-0090-5f3d-a246-aa6209fd879c`` (UUIDv5 NAMESPACE_URL de
``ameesh:nexlink-mesh-alert:v1``) et la génération 1, figés avec #3537.

**Auth du transport** : HMAC-SHA-256 sur
``key_id "\\n" timestamp "\\n" body``, en-têtes ``X-Mesh-Key-Id``,
``X-Mesh-Timestamp``, ``X-Mesh-Signature: v1=<hex>``. Le secret est nommé
(`token_env` / `token_file`), jamais écrit en configuration.

Aucune clé privée ici : le mesh ne fait qu'emballer pour la clé publique.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from typing import NamedTuple

#: libellé et version du wrap (Nexlink `x25519-wrap.ts`).
WRAP_LABEL = b"nexlink/x25519-wrap/v1"
WRAP_VERSION = 0x01
WRAP_NONCE_BYTES = 12
WRAP_TAG_BYTES = 16
WRAP_OVERHEAD = 1 + 32 + WRAP_NONCE_BYTES + WRAP_TAG_BYTES
WRAP_MAX_PAYLOAD = 1024

#: sujet « alerte du mesh » : 5, jamais 4 (`oprfBackup`).
MESH_ALERT_SUBJECT = 5
MESH_ALERT_ACTOR_ID = "255d5ab8-0090-5f3d-a246-aa6209fd879c"
MESH_ALERT_SUBJECT_GEN = 1
#: identité de l'émetteur pour le HMAC (un seul émetteur : le mesh) ; le
#: déploiement peut la surcharger par `AMEESH_NEXLINK_KEY_ID` des deux côtés.
MESH_ALERT_KEY_ID = "ameesh-v1"
#: algorithme annoncé par `GET /mesh/alerts/keys` (une seule valeur en v1).
KEY_ALG = "x25519-hkdf-sha256-aes-256-gcm"

#: version du format d'enveloppe et du contenu.
ENVELOPE_V = 1
CONTENT_KEY_BYTES = 32
GCM_TAG_BYTES = 16
NONCE_BYTES = 12


class CryptoUnavailable(RuntimeError):
    """La bibliothèque `cryptography` (extra `ameesh[crypto]`) manque."""


def crypto_available() -> bool:
    """Vrai si l'extra `cryptography` est importable (jamais d'exception)."""
    try:
        _crypto()
    except CryptoUnavailable:
        return False
    return True


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: F401
            X25519PrivateKey, X25519PublicKey)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
        from cryptography.hazmat.primitives.serialization import (  # noqa: F401
            Encoding, PublicFormat)
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise CryptoUnavailable(
            "cryptography is not installed (install the `ameesh[crypto]` extra)") from exc
    return True


# --------------------------------------------------------------------------
# encodages
# --------------------------------------------------------------------------

def uuid_bytes(value) -> bytes:
    """Les 16 octets d'un UUID (refus d'un UUID nul, comme Nexlink)."""
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invalid UUID: %r" % (value,)) from None
    raw = parsed.bytes
    if not any(raw):
        raise ValueError("nil UUID")
    return raw


def u32be(value) -> bytes:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid generation: %r" % (value,)) from None
    if not 0 < number < 2 ** 32:
        raise ValueError("invalid generation: %r" % (value,))
    return struct.pack(">I", number)


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text) -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except Exception:
        raise ValueError("invalid base64") from None


# --------------------------------------------------------------------------
# wrap X25519 (compatible Nexlink)
# --------------------------------------------------------------------------

class WrapContext(NamedTuple):
    """Ce qu'un wrap nomme : le destinataire et le sujet emballé."""

    recipient_account_id: str
    recipient_gen: int
    subject_kind: int = MESH_ALERT_SUBJECT
    subject_actor_id: str = MESH_ALERT_ACTOR_ID
    subject_gen: int = MESH_ALERT_SUBJECT_GEN


def wrap_aad(context: WrapContext, ephemeral_public: bytes) -> bytes:
    """L'AAD exacte de `x25519WrapAad` (Nexlink), à l'octet."""
    if context.subject_kind != MESH_ALERT_SUBJECT:
        raise ValueError("unknown wrap subject: %r" % (context.subject_kind,))
    if len(ephemeral_public) != 32:
        raise ValueError("an X25519 public key is 32 bytes")
    return b"".join((WRAP_LABEL,
                     uuid_bytes(context.recipient_account_id),
                     u32be(context.recipient_gen),
                     bytes(ephemeral_public),
                     bytes([context.subject_kind]),
                     uuid_bytes(context.subject_actor_id),
                     u32be(context.subject_gen)))


def wrap_to_x25519(recipient_public: bytes, context: WrapContext, payload: bytes, *,
                   ephemeral_private: bytes | None = None,
                   nonce: bytes | None = None) -> bytes:
    """Emballer ``payload`` pour la clé key-wrap X25519 du compte owner.

    ``ephemeral_private`` et ``nonce`` ne servent qu'aux vecteurs de test :
    en production les deux sont tirés au hasard. Rend le blob
    ``0x01 ‖ eph_pub ‖ nonce ‖ tag ‖ ct`` (celui de `wrapToX25519`).
    """
    _crypto()
    if not isinstance(recipient_public, (bytes, bytearray)) or len(recipient_public) != 32:
        raise ValueError("an X25519 public key is 32 bytes")
    if not isinstance(payload, (bytes, bytearray)) or not 1 <= len(payload) <= WRAP_MAX_PAYLOAD:
        raise ValueError("a wrap payload is 1 to 1024 bytes")
    private = os.urandom(32) if ephemeral_private is None else bytes(ephemeral_private)
    if len(private) != 32:
        raise ValueError("an X25519 private key is 32 bytes")
    nonce = os.urandom(WRAP_NONCE_BYTES) if nonce is None else bytes(nonce)
    if len(nonce) != WRAP_NONCE_BYTES:
        raise ValueError("a GCM nonce is 12 bytes")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                                  X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    ephemeral = X25519PrivateKey.from_private_bytes(private)
    ephemeral_public = ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(bytes(recipient_public)))
    if not any(shared):
        raise ValueError("the recipient key is not a usable X25519 public key")
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=WRAP_LABEL,
               info=bytes(ephemeral_public) + bytes(recipient_public)).derive(shared)
    sealed = AESGCM(kek).encrypt(nonce, bytes(payload), wrap_aad(context, ephemeral_public))
    ciphertext, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return b"".join((bytes([WRAP_VERSION]), ephemeral_public, nonce, tag, ciphertext))


def unwrap_from_x25519(private_key: bytes, context: WrapContext, wrapped: bytes) -> bytes:
    """L'inverse, pour les tests et la vérification croisée (jamais en prod)."""
    _crypto()
    if len(wrapped) <= WRAP_OVERHEAD:
        raise ValueError("an X25519 wrap is longer than 61 bytes")
    if wrapped[0] != WRAP_VERSION:
        raise ValueError("unknown X25519 wrap version")
    ephemeral_public = wrapped[1:33]
    nonce = wrapped[33:33 + WRAP_NONCE_BYTES]
    tag = wrapped[33 + WRAP_NONCE_BYTES:WRAP_OVERHEAD]
    ciphertext = wrapped[WRAP_OVERHEAD:]
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                                  X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    recipient = X25519PrivateKey.from_private_bytes(bytes(private_key))
    recipient_public = recipient.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = recipient.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    if not any(shared):
        raise ValueError("the wrap carries an unusable ephemeral key")
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=WRAP_LABEL,
               info=bytes(ephemeral_public) + recipient_public).derive(shared)
    return AESGCM(kek).decrypt(nonce, ciphertext + tag,
                               wrap_aad(context, ephemeral_public))


# --------------------------------------------------------------------------
# contenu (AES-256-GCM sous CEK)
# --------------------------------------------------------------------------

def new_cek() -> bytes:
    return os.urandom(CONTENT_KEY_BYTES)


def content_aad(alert_id: str, occurred_at: str, version: int = ENVELOPE_V) -> bytes:
    """``v|alert_id|occurred_at`` (spec owner) : lie le chiffré à l'alerte."""
    return ("%d|%s|%s" % (int(version), alert_id, occurred_at)).encode("utf-8")


def content_plaintext(title: str, body: str, *, kind: str | None = None,
                      event: str | None = None, context: dict | None = None) -> bytes:
    """Le JSON UTF-8 que l'app déchiffre (jamais vu par le hub).

    `context` porte les champs structurés (lot, décision, hôte, `since`…),
    jamais le `detail` brut de l'alerte : le texte est déjà dans `body`.
    """
    payload = {"title": str(title), "body": str(body)}
    if kind is not None:
        payload["kind"] = str(kind)
    if event is not None:
        payload["event"] = str(event)
    for key, value in (context or {}).items():
        if value is not None:
            payload[str(key)] = value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def seal_content(cek: bytes, plaintext: bytes, aad: bytes, *,
                 nonce: bytes | None = None) -> tuple[bytes, bytes]:
    """AES-256-GCM ; rend ``(nonce, ct ‖ tag)`` (l'ordre des plateformes)."""
    _crypto()
    if len(cek) != CONTENT_KEY_BYTES:
        raise ValueError("a content key is 32 bytes")
    nonce = os.urandom(NONCE_BYTES) if nonce is None else bytes(nonce)
    if len(nonce) != NONCE_BYTES:
        raise ValueError("a GCM nonce is 12 bytes")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return nonce, AESGCM(bytes(cek)).encrypt(nonce, bytes(plaintext), bytes(aad))


def open_content(cek: bytes, nonce: bytes, sealed: bytes, aad: bytes) -> bytes:
    """L'inverse, pour les tests et la vérification croisée."""
    _crypto()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(bytes(cek)).decrypt(bytes(nonce), bytes(sealed), bytes(aad))


# --------------------------------------------------------------------------
# transport : signature et en-têtes HMAC
# --------------------------------------------------------------------------

def mesh_signature(secret, key_id: str, timestamp: str, body: bytes) -> str:
    """``v1=hex(HMAC-SHA-256(secret, key_id "\\n" timestamp "\\n" body))``."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    signed = b"\n".join((str(key_id).encode("ascii"),
                         str(timestamp).encode("ascii"),
                         bytes(body or b"")))
    return "v1=" + hmac.new(bytes(secret), signed, hashlib.sha256).hexdigest()


def mesh_headers(secret, body: bytes, *, key_id: str = MESH_ALERT_KEY_ID,
                 timestamp: str | None = None) -> dict:
    """Les trois en-têtes signés d'une requête (POST ou GET)."""
    timestamp = str(int(timestamp if timestamp is not None else time.time()))
    return {"X-Mesh-Key-Id": str(key_id),
            "X-Mesh-Timestamp": timestamp,
            "X-Mesh-Signature": mesh_signature(secret, key_id, timestamp, body)}


def alert_identity_hash(identity: str) -> str:
    """`alert_id` : SHA-256 de l'identité de registre `notified` (#2395)."""
    return hashlib.sha256(str(identity).encode("utf-8")).hexdigest()


def delivery_key(event: str, alert_id: str) -> str:
    """Clé d'idempotence d'un envoi : l'alerte, et l'événement s'il en faut.

    `raised` garde l'`alert_id` du contrat (#3537) ; un `resolved` reçoit
    ``<alert_id>:resolved`` — la ligne durable de l'app a la même identité,
    mais l'envoi n'est pas un doublon du `raised`.
    """
    event = str(event or "raised")
    return str(alert_id) if event == "raised" else "%s:%s" % (alert_id, event)
