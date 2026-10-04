# SPDX-License-Identifier: AGPL-3.0-only
"""ECDSA sur P-256 avec SHA-256 (ES256) — **vérification seulement**.

Les passkeys WebAuthn et les clés d'appareil (Android Keystore, Secure
Enclave) signent en ES256. ameesh doit vérifier ces signatures sans
dépendance obligatoire, comme `signing.py` le fait pour Ed25519 :

* **pure** (toujours disponible) : arithmétique sur la courbe NIST P-256
  (SEC 2, FIPS 186-4), coordonnées jacobiennes, astuce de Shamir pour
  u1·G + u2·Q. Aucune génération de clé ni signature ici : ce module ne
  manipule que des données publiques (clé publique, message, signature), le
  temps constant n'est donc pas requis ;
* **cryptography** quand il est importable.

Le choix se force par `AMEESH_ECDSA_BACKEND` (`pure`, `cryptography`, `auto`) ;
à défaut, `AMEESH_SIGNING_BACKEND` / `AGENT_MESH_SIGNING_BACKEND` valant `pure`
ou `cryptography` s'appliquent aussi (une seule variable pour forcer tout le
chemin pur dans les tests).

Les deux backends voient exactement les mêmes entrées : la signature DER est
décodée **strictement** ici (encodage minimal, aucun octet en trop), r et s
sont bornés dans [1, n-1] et le point public est validé (sur la courbe,
coordonnées < p) avant d'appeler l'un ou l'autre. Une signature acceptée par
un backend l'est donc par l'autre.
"""
from __future__ import annotations

import hashlib
import os

# Paramètres de secp256r1 (SEC 2 v2 §2.4.2)
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

_INF = (1, 1, 0)  # point à l'infini en jacobien (Z = 0)


class P256Error(ValueError):
    """Clé ou signature P-256 mal formée."""


# --------------------------------------------------------------------------
# points
# --------------------------------------------------------------------------

def is_on_curve(x: int, y: int) -> bool:
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


def decode_point(data: bytes) -> tuple[int, int]:
    """Point SEC1 (non compressé 04‖X‖Y ou compressé 02/03‖X), validé."""
    data = bytes(data)
    if len(data) == 65 and data[0] == 0x04:
        x = int.from_bytes(data[1:33], "big")
        y = int.from_bytes(data[33:], "big")
    elif len(data) == 33 and data[0] in (0x02, 0x03):
        x = int.from_bytes(data[1:], "big")
        if x >= P:
            raise P256Error("abscisse hors du corps")
        rhs = (x * x * x + A * x + B) % P
        y = pow(rhs, (P + 1) // 4, P)  # p ≡ 3 (mod 4)
        if y * y % P != rhs:
            raise P256Error("abscisse sans point sur la courbe")
        if (y & 1) != (data[0] & 1):
            y = P - y
    else:
        raise P256Error("point SEC1 attendu (65 octets 04… ou 33 octets 02/03…)")
    if not is_on_curve(x, y):
        raise P256Error("point hors de la courbe P-256")
    return x, y


def encode_point(x: int, y: int) -> bytes:
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _double(point: tuple) -> tuple:
    """Doublement jacobien, a = -3 (dbl-2001-b)."""
    x1, y1, z1 = point
    if z1 == 0 or y1 == 0:
        return _INF
    delta = z1 * z1 % P
    gamma = y1 * y1 % P
    beta = x1 * gamma % P
    alpha = 3 * (x1 - delta) * (x1 + delta) % P
    x3 = (alpha * alpha - 8 * beta) % P
    z3 = ((y1 + z1) * (y1 + z1) - gamma - delta) % P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % P
    return (x3, y3, z3)


def _add(p1: tuple, p2: tuple) -> tuple:
    """Addition jacobienne générale (add-2007-bl), cas particuliers compris."""
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    if z1 == 0:
        return p2
    if z2 == 0:
        return p1
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    h = (u2 - u1) % P
    r = 2 * (s2 - s1) % P
    if h == 0:
        if r == 0:
            return _double(p1)
        return _INF
    i = 4 * h * h % P
    j = h * i % P
    v = u1 * i % P
    x3 = (r * r - j - 2 * v) % P
    y3 = (r * (v - x3) - 2 * s1 * j) % P
    z3 = ((z1 + z2) * (z1 + z2) - z1z1 - z2z2) * h % P
    return (x3, y3, z3)


def _affine(point: tuple) -> tuple[int, int] | None:
    x, y, z = point
    if z == 0:
        return None
    zi = pow(z, -1, P)
    zi2 = zi * zi % P
    return x * zi2 % P, y * zi2 * zi % P


def scalar_mult(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    """k·point (affine) — utilisé par les tests et pour dériver une clé publique."""
    acc = _INF
    addend = (point[0], point[1], 1)
    for bit in bin(k % N)[2:]:
        acc = _double(acc)
        if bit == "1":
            acc = _add(acc, addend)
    return _affine(acc)


def _shamir(u1: int, u2: int, q: tuple[int, int]) -> tuple[int, int] | None:
    """u1·G + u2·Q en une seule passe."""
    g = (GX, GY, 1)
    qj = (q[0], q[1], 1)
    gq = _add(g, qj)
    acc = _INF
    for i in range(max(u1.bit_length(), u2.bit_length()) - 1, -1, -1):
        acc = _double(acc)
        b1 = (u1 >> i) & 1
        b2 = (u2 >> i) & 1
        if b1 and b2:
            acc = _add(acc, gq)
        elif b1:
            acc = _add(acc, g)
        elif b2:
            acc = _add(acc, qj)
    return _affine(acc)


# --------------------------------------------------------------------------
# signatures DER
# --------------------------------------------------------------------------

def _der_integer(data: bytes, index: int) -> tuple[int, int]:
    if index + 2 > len(data) or data[index] != 0x02:
        raise P256Error("DER : INTEGER attendu")
    length = data[index + 1]
    if length == 0 or length & 0x80 or length > 33:
        raise P256Error("DER : longueur d'entier invalide")
    body = data[index + 2:index + 2 + length]
    if len(body) != length:
        raise P256Error("DER : entier tronqué")
    if body[0] & 0x80:
        raise P256Error("DER : entier négatif")
    if length > 1 and body[0] == 0 and not body[1] & 0x80:
        raise P256Error("DER : entier non minimal")
    return int.from_bytes(body, "big"), index + 2 + length


def decode_der_signature(data: bytes) -> tuple[int, int]:
    """(r, s) d'une signature ECDSA DER, décodée strictement (DER, pas BER)."""
    data = bytes(data)
    if len(data) < 8 or len(data) > 72 or data[0] != 0x30:
        raise P256Error("DER : SEQUENCE attendue")
    if data[1] & 0x80 or data[1] != len(data) - 2:
        raise P256Error("DER : longueur de SEQUENCE invalide")
    r, index = _der_integer(data, 2)
    s, index = _der_integer(data, index)
    if index != len(data):
        raise P256Error("DER : octets en trop")
    return r, s


def encode_der_signature(r: int, s: int) -> bytes:
    def integer(value: int) -> bytes:
        body = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
        if body[0] & 0x80:
            body = b"\x00" + body
        return b"\x02" + bytes([len(body)]) + body
    content = integer(r) + integer(s)
    return b"\x30" + bytes([len(content)]) + content


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

def _verify_pure(point: tuple[int, int], message: bytes, r: int, s: int) -> bool:
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")  # |n| = 256 : pas de troncature
    w = pow(s, -1, N)
    result = _shamir(e * w % N, r * w % N, point)
    if result is None:
        return False
    return result[0] % N == r


class PureP256:
    """Vérification ES256 de référence, sans dépendance."""

    name = "pure"

    @staticmethod
    def verify(point: tuple[int, int], message: bytes, r: int, s: int) -> bool:
        return _verify_pure(point, message, r, s)


class CryptographyP256:
    name = "cryptography"

    def __init__(self):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        self._ec = ec
        self._hashes = hashes
        self._utils = utils
        self._invalid = InvalidSignature

    def verify(self, point: tuple[int, int], message: bytes, r: int, s: int) -> bool:
        try:
            key = self._ec.EllipticCurvePublicNumbers(
                point[0], point[1], self._ec.SECP256R1()).public_key()
            key.verify(self._utils.encode_dss_signature(r, s), message,
                       self._ec.ECDSA(self._hashes.SHA256()))
            return True
        except (self._invalid, ValueError):
            return False


_BACKENDS = {"pure": PureP256, "cryptography": CryptographyP256}
_CACHE: dict[str, object] = {}


def _wanted(name: str | None) -> str:
    if name:
        return name
    explicit = os.environ.get("AMEESH_ECDSA_BACKEND")
    if explicit:
        return explicit
    shared = (os.environ.get("AMEESH_SIGNING_BACKEND")
              or os.environ.get("AGENT_MESH_SIGNING_BACKEND") or "")
    return shared if shared in _BACKENDS else "auto"


def backend(name: str | None = None):
    """Backend demandé (`AMEESH_ECDSA_BACKEND`) ou le meilleur disponible."""
    wanted = _wanted(name)
    if wanted in _CACHE:
        return _CACHE[wanted]
    if wanted == "auto":
        try:
            instance = CryptographyP256()
        except ImportError:
            instance = PureP256()
    elif wanted == "pure":
        instance = PureP256()
    else:
        try:
            instance = _BACKENDS[wanted]()
        except KeyError:
            raise ValueError("backend ECDSA inconnu : %r (pure, cryptography)" % wanted)
        except ImportError as exc:
            raise ValueError("backend ECDSA %s indisponible : %s" % (wanted, exc))
    _CACHE[wanted] = instance
    return instance


def available_backends() -> list[str]:
    found = ["pure"]
    try:
        CryptographyP256()
        found.append("cryptography")
    except ImportError:
        pass
    return found


def verify(point: tuple[int, int], message: bytes, der_signature: bytes,
           backend_name: str | None = None) -> bool:
    """Vrai si `der_signature` est une signature ES256 de `message` par `point`.

    `message` est le message brut : le SHA-256 fait partie d'ES256.
    """
    try:
        r, s = decode_der_signature(der_signature)
    except P256Error:
        return False
    if not (1 <= r < N and 1 <= s < N):
        return False
    if not is_on_curve(point[0], point[1]):
        return False
    return backend(backend_name).verify(point, message, r, s)
