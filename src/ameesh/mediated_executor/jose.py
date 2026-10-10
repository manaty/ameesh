# SPDX-License-Identifier: AGPL-3.0-only
"""Briques ES256 partagées par l'appareil et le serveur (lot L110).

Formats retenus (étude de l'exécuteur médié §3 ; le contrat L107 ne fixait
pas l'encodage des signatures) :

* **clé publique de l'exécuteur** : JWK `{"kty": "EC", "crv": "P-256", "x",
  "y"}` (base64url sans remplissage, 32 octets chacun) ; son empreinte est
  celle de la RFC 7638 (SHA-256 du JSON canonique de `crv`, `kty`, `x`, `y`),
  en base64url : c'est le `kid` des assertions ;
* **signatures** (`proof`, assertion JWS) : ES256 au sens de JOSE (RFC 7518
  §3.4), c'est-à-dire `r‖s` brut de 64 octets en base64url — jamais du DER ;
* **assertion** : JWS compact `en-tête.charge.signature` ;
* **liaison facultative à la clé d'appareil Nexlink** (`device_attestation`) :
  aux formats de Nexlink remote-help (05 §2) — clé publique en SPKI DER
  base64url, signature `r‖s` de 64 octets base64url, transcription ASCII
  ligne à ligne terminée par `\\n` (voir `attestation_transcript`).

La vérification passe par `ameesh.p256.verify` (pur ou `cryptography`).
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping

from .. import jcs, p256

SCHEMA_ATTESTATION = "ameesh-device-attestation/1"

#: préfixe DER de la SPKI d'une clé P-256 non compressée (65 octets suivent)
SPKI_P256_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


class JoseError(ValueError):
    """Clé, signature ou JWS mal formé."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    if not isinstance(text, str) or not text or "=" in text:
        raise JoseError("base64url attendu (sans remplissage)")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise JoseError("base64url illisible") from exc
    if b64u(raw) != text:  # encodage canonique seulement
        raise JoseError("base64url non canonique")
    return raw


# --------------------------------------------------------------------------
# clés
# --------------------------------------------------------------------------

def jwk_from_point(point: tuple[int, int]) -> dict:
    x, y = point
    return {"kty": "EC", "crv": "P-256",
            "x": b64u(x.to_bytes(32, "big")), "y": b64u(y.to_bytes(32, "big"))}


def point_from_jwk(jwk: Any) -> tuple[int, int]:
    """Point P-256 validé d'une JWK publique ; refuse toute clé privée (`d`)."""
    if not isinstance(jwk, Mapping):
        raise JoseError("JWK : objet attendu")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise JoseError("JWK : EC P-256 attendue")
    if "d" in jwk:
        raise JoseError("JWK : une clé PRIVÉE n'a rien à faire ici")
    try:
        xb, yb = unb64u(jwk.get("x")), unb64u(jwk.get("y"))
    except JoseError as exc:
        raise JoseError("JWK : x ou y illisible") from exc
    if len(xb) != 32 or len(yb) != 32:
        raise JoseError("JWK : x et y font 32 octets")
    x, y = int.from_bytes(xb, "big"), int.from_bytes(yb, "big")
    if not p256.is_on_curve(x, y):
        raise JoseError("JWK : point hors de la courbe P-256")
    return x, y


def public_jwk(jwk: Mapping) -> dict:
    """La JWK réduite à ses membres requis, validée."""
    point_from_jwk(jwk)
    return {k: jwk[k] for k in ("kty", "crv", "x", "y")}


def thumbprint(jwk: Mapping) -> str:
    """Empreinte RFC 7638 (SHA-256) d'une JWK EC, en base64url."""
    pub = public_jwk(jwk)
    canonical = json.dumps({"crv": pub["crv"], "kty": pub["kty"], "x": pub["x"], "y": pub["y"]},
                           separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canonical.encode("ascii")).digest())


def spki_from_point(point: tuple[int, int]) -> bytes:
    return SPKI_P256_PREFIX + p256.encode_point(*point)


def point_from_spki(der: bytes) -> tuple[int, int]:
    der = bytes(der)
    if len(der) != len(SPKI_P256_PREFIX) + 65 or not der.startswith(SPKI_P256_PREFIX):
        raise JoseError("SPKI : clé P-256 non compressée attendue")
    try:
        return p256.decode_point(der[len(SPKI_P256_PREFIX):])
    except p256.P256Error as exc:
        raise JoseError("SPKI : %s" % exc) from exc


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------

def raw_to_der(sig: bytes) -> bytes:
    if len(sig) != 64:
        raise JoseError("signature ES256 : 64 octets r‖s attendus")
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (1 <= r < p256.N and 1 <= s < p256.N):
        raise JoseError("signature ES256 : r ou s hors bornes")
    return p256.encode_der_signature(r, s)


def verify_raw(point: tuple[int, int], message: bytes, sig: bytes) -> bool:
    """Signature ES256 `r‖s` (64 octets) de `message` par `point` ?"""
    try:
        der = raw_to_der(sig)
    except JoseError:
        return False
    return p256.verify(point, message, der)


def verify_b64(jwk_or_point, message: bytes, sig_b64: str) -> bool:
    point = jwk_or_point if isinstance(jwk_or_point, tuple) else point_from_jwk(jwk_or_point)
    try:
        sig = unb64u(sig_b64)
    except JoseError:
        return False
    return verify_raw(point, message, sig)


# --------------------------------------------------------------------------
# messages signés
# --------------------------------------------------------------------------

def normalize_server_url(url: str) -> str:
    """L'URL du serveur telle qu'elle est signée : sans `/` final."""
    text = (url or "").strip()
    if not (text.startswith("https://") or text.startswith("http://")):
        raise JoseError("URL du serveur : http(s):// attendu")
    return text.rstrip("/")


def normalize_code(code: str) -> str:
    """Code d'enrôlement normalisé (Crockford : O→0, I/L→1, tirets et blancs
    ignorés), le même à l'appareil et au serveur."""
    text = "".join((code or "").split()).replace("-", "").upper()
    return text.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))


def enroll_proof_message(code: str, public_key: Mapping, server_url: str) -> bytes:
    """Ce que `proof` signe : JCS({code, public_key, server_url})."""
    return jcs.canonicalize({"code": code, "public_key": public_jwk(public_key),
                             "server_url": normalize_server_url(server_url)})


def attestation_transcript(*, server_url: str, code_sha256: str,
                           executor_thumbprint: str) -> bytes:
    """Le défi que la clé d'appareil Nexlink signe pour lier l'exécuteur à
    l'appareil : ASCII, une ligne par champ, `\\n` final (Nexlink 05 §2)."""
    lines = [SCHEMA_ATTESTATION,
             "server_url=%s" % normalize_server_url(server_url),
             "code_sha256=%s" % code_sha256,
             "executor_thumbprint=%s" % executor_thumbprint]
    text = "".join(line + "\n" for line in lines)
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise JoseError("transcription : ASCII seulement") from exc


def jws_signing_input(header: Mapping, payload: Mapping) -> bytes:
    return ("%s.%s" % (b64u(json.dumps(dict(header), separators=(",", ":")).encode()),
                       b64u(json.dumps(dict(payload), separators=(",", ":")).encode()))
            ).encode("ascii")


def jws_parse(token: str) -> tuple[dict, dict, bytes, bytes]:
    """(en-tête, charge, entrée signée, signature) d'un JWS compact, sans
    vérifier la signature."""
    if not isinstance(token, str) or len(token) > 4096:
        raise JoseError("assertion : texte de 4 096 caractères au plus attendu")
    parts = token.split(".")
    if len(parts) != 3:
        raise JoseError("assertion : JWS compact à trois parties attendu")
    try:
        header = json.loads(unb64u(parts[0]))
        payload = json.loads(unb64u(parts[1]))
    except (ValueError, JoseError) as exc:
        raise JoseError("assertion : en-tête ou charge illisible") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise JoseError("assertion : objets JSON attendus")
    return header, payload, ("%s.%s" % (parts[0], parts[1])).encode("ascii"), unb64u(parts[2])
