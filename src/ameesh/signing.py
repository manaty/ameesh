# SPDX-License-Identifier: AGPL-3.0-only
"""Signatures Ed25519 — l'autorité du propriétaire (R4, spec §5 point 3).

Trois implémentations, une seule interface :

* **pure** (par défaut, toujours disponible) : Ed25519 selon la RFC 8032, en
  Python et `hashlib` seulement. C'est ce qui permet à `agent-mesh` de vérifier
  une signature sans aucune dépendance installée ;
* **cryptography** et **nacl** quand ils sont importables : plus rapides, et
  utilisés comme contre-vérification croisée dans les tests.

Quel que soit le backend, une clé publique de petit ordre ou non canonique est
refusée (`public_key_error`), comme une signature dont R est de petit ordre ou
non canonique, ou dont S n'est pas réduit (`signature_error`) : sans ces
contrôles, la clé neutre 01 00…00 et la signature (neutre, 0) vérifieraient
pour tout message, avec le backend pur comme avec OpenSSL.

Rien ici ne connaît la base : ce module signe et vérifie des octets. Le lien
avec le registre (`agent_registry.public_key`), le contenu (payload canonique)
et l'échéance est dans `authority.py`.

Format des clés sur disque (PEM-like, lisible et sans ambiguïté) :

    -----BEGIN AGENT-MESH PRIVATE KEY-----
    <base64 du germe de 32 octets>
    -----END AGENT-MESH PRIVATE KEY-----

La clé privée n'est jamais enregistrée en base, jamais journalisée, et
`key generate` est un acte du propriétaire : aucun agent ne doit l'exécuter.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

PUBLIC_HEADER = "-----BEGIN AGENT-MESH PUBLIC KEY-----"
PUBLIC_FOOTER = "-----END AGENT-MESH PUBLIC KEY-----"
PRIVATE_HEADER = "-----BEGIN AGENT-MESH PRIVATE KEY-----"
PRIVATE_FOOTER = "-----END AGENT-MESH PRIVATE KEY-----"

# --------------------------------------------------------------------------
# Ed25519 de référence (RFC 8032) — arithmétique sur la courbe twisted Edwards
# --------------------------------------------------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493  # ordre du point de base
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)  # racine carrée de -1


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _recover_x(y: int, sign: int) -> int | None:
    """x tel que (x, y) soit sur la courbe, du signe demandé (None si aucun)."""
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _point_add(p1: tuple, p2: tuple) -> tuple:
    """Addition en coordonnées homogènes étendues (RFC 8032 §5.1.4)."""
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    d = z1 * 2 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)  # neutre
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_neg(point: tuple) -> tuple:
    x, y, z, t = point
    return (-x % _P, y, z, -t % _P)


def _point_compress(point: tuple) -> bytes:
    x, y, z, _t = point
    zi = pow(z, _P - 2, _P)
    x = x * zi % _P
    y = y * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _point_decompress(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0)
_BASE = (_BX, _BY, 1, _BX * _BY % _P)


def expand_seed(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("germe Ed25519 : 32 octets attendus, %d reçus" % len(seed))
    hashed = _sha512(seed)
    scalar = int.from_bytes(hashed[:32], "little")
    # Élagage RFC 8032 §5.1.5 : bas 3 bits à zéro, bit 255 à zéro, bit 254 à un.
    scalar &= ~0b111
    scalar &= (1 << 255) - 1
    scalar |= 1 << 254
    return scalar, hashed[32:]


def public_from_seed(seed: bytes) -> bytes:
    scalar, _prefix = expand_seed(seed)
    return _point_compress(_point_mul(scalar, _BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = expand_seed(seed)
    public = _point_compress(_point_mul(scalar, _BASE))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    encoded_r = _point_compress(_point_mul(r, _BASE))
    k = int.from_bytes(_sha512(encoded_r + public + message), "little") % _L
    s = (r + k * scalar) % _L
    return encoded_r + s.to_bytes(32, "little")


def _is_small_order(point: tuple) -> bool:
    """[8]P = neutre : P est l'un des 8 points du sous-groupe de torsion."""
    x, y, z, _t = _point_mul(8, point)
    return x % _P == 0 and (y - z) % _P == 0


def public_key_error(public: bytes) -> str | None:
    """Raison de refuser une clé publique Ed25519, ou None si elle est admissible.

    Refusées : longueur autre que 32 octets, encodage non canonique (y non
    réduit mod p, ou bit de signe posé sur x = 0), point hors de la courbe, et
    les 8 points de PETIT ORDRE (dont le neutre 01 00…00) : avec une telle clé,
    la signature constante (R = neutre, S = 0) vérifie pour TOUT message.
    Une clé honnête (a·B) n'est jamais concernée.
    """
    if not isinstance(public, (bytes, bytearray)) or len(public) != 32:
        return "32 octets attendus"
    point = _point_decompress(bytes(public))
    if point is None:
        return "encodage non canonique ou point hors de la courbe"
    if _is_small_order(point):
        return "point de petit ordre (sous-groupe de torsion)"
    return None


def check_public_key(public: bytes) -> bytes:
    """Clé publique Ed25519 admissible, ou ValueError (voir `public_key_error`)."""
    error = public_key_error(public)
    if error:
        raise ValueError("clé publique Ed25519 refusée : %s" % error)
    return bytes(public)


def signature_error(public: bytes, signature: bytes) -> str | None:
    """Contrôles communs à TOUS les backends, avant la vérification elle-même.

    La clé doit être admissible (`public_key_error`) ; R doit être l'encodage
    canonique d'un point de la courbe qui n'est pas de petit ordre ; S doit
    être réduit (S < L). Les backends n'appliquent pas tous ces règles (OpenSSL
    accepte une clé de petit ordre) : elles sont donc imposées ici.
    """
    error = public_key_error(public)
    if error:
        return "clé publique : %s" % error
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
        return "signature : 64 octets attendus"
    signature = bytes(signature)
    r_point = _point_decompress(signature[:32])
    if r_point is None:
        return "R : encodage non canonique ou point hors de la courbe"
    if _is_small_order(r_point):
        return "R de petit ordre"
    if int.from_bytes(signature[32:], "little") >= _L:
        return "S non réduit (S >= L)"
    return None


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if signature_error(public, signature) is not None:
        return False
    public, signature = bytes(public), bytes(signature)
    point = _point_decompress(public)
    encoded_r = signature[:32]
    s = int.from_bytes(signature[32:], "little")
    k = int.from_bytes(_sha512(encoded_r + public + message), "little") % _L
    # [S]B - [k]A doit valoir R (vérification sans cofacteur)
    computed = _point_add(_point_mul(s, _BASE), _point_neg(_point_mul(k, point)))
    return _point_compress(computed) == encoded_r


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

class PureEd25519:
    """Implémentation de référence RFC 8032, sans dépendance."""

    name = "pure"
    reference = True

    @staticmethod
    def generate_seed() -> bytes:
        return secrets.token_bytes(32)

    @staticmethod
    def public_from_seed(seed: bytes) -> bytes:
        return public_from_seed(seed)

    @staticmethod
    def sign(seed: bytes, message: bytes) -> bytes:
        return sign(seed, message)

    @staticmethod
    def verify(public: bytes, message: bytes, signature: bytes) -> bool:
        return verify(public, message, signature)


class CryptographyEd25519:
    name = "cryptography"
    reference = False

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey,
        )
        self._private = Ed25519PrivateKey
        self._public = Ed25519PublicKey

    @staticmethod
    def generate_seed() -> bytes:
        return secrets.token_bytes(32)

    def public_from_seed(self, seed: bytes) -> bytes:
        from cryptography.hazmat.primitives import serialization
        key = self._private.from_private_bytes(seed)
        return key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, seed: bytes, message: bytes) -> bytes:
        return self._private.from_private_bytes(seed).sign(message)

    def verify(self, public: bytes, message: bytes, signature: bytes) -> bool:
        if signature_error(public, signature) is not None:
            return False
        try:
            self._public.from_public_bytes(public).verify(signature, message)
            return True
        except Exception:
            return False


class NaclEd25519:
    name = "nacl"
    reference = False

    def __init__(self):
        import nacl.signing
        self._signing = nacl.signing

    @staticmethod
    def generate_seed() -> bytes:
        return secrets.token_bytes(32)

    def public_from_seed(self, seed: bytes) -> bytes:
        return bytes(self._signing.SigningKey(seed).verify_key)

    def sign(self, seed: bytes, message: bytes) -> bytes:
        return self._signing.SigningKey(seed).sign(message).signature

    def verify(self, public: bytes, message: bytes, signature: bytes) -> bool:
        if signature_error(public, signature) is not None:
            return False
        try:
            self._signing.VerifyKey(public).verify(message, signature)
            return True
        except Exception:
            return False


_BACKENDS = {"pure": PureEd25519, "cryptography": CryptographyEd25519, "nacl": NaclEd25519}
_CACHE: dict[str, object] = {}


def backend(name: str | None = None):
    """Backend demandé (`AGENT_MESH_SIGNING_BACKEND`) ou le meilleur disponible."""
    wanted = (name or os.environ.get("AMEESH_SIGNING_BACKEND")
              or os.environ.get("AGENT_MESH_SIGNING_BACKEND") or "auto")
    if wanted in _CACHE:
        return _CACHE[wanted]
    if wanted == "auto":
        for candidate in ("cryptography", "nacl"):
            try:
                instance = _BACKENDS[candidate]()
                _CACHE[wanted] = instance
                return instance
            except ImportError:
                continue
        instance = PureEd25519()
    elif wanted == "pure":
        instance = PureEd25519()
    else:
        try:
            instance = _BACKENDS[wanted]()
        except KeyError:
            raise ValueError("backend de signature inconnu : %r (pure, cryptography, nacl)" % wanted)
        except ImportError as exc:
            raise ValueError("backend %s indisponible : %s" % (wanted, exc))
    _CACHE[wanted] = instance
    return instance


def available_backends() -> list[str]:
    found = ["pure"]
    for name in ("cryptography", "nacl"):
        try:
            _BACKENDS[name]()
            found.append(name)
        except ImportError:
            pass
    return found


# --------------------------------------------------------------------------
# clés : empreinte, fichiers
# --------------------------------------------------------------------------

def fingerprint(public: bytes) -> str:
    """Empreinte affichable et comparable : sha256 de la clé brute, en hex."""
    return hashlib.sha256(public).hexdigest()


def _pem(body: str, header: str, footer: str) -> str:
    return "%s\n%s\n%s\n" % (header, body, footer)


def encode_public(public: bytes) -> str:
    return _pem(base64.b64encode(public).decode("ascii"), PUBLIC_HEADER, PUBLIC_FOOTER)


def encode_private(seed: bytes) -> str:
    return _pem(base64.b64encode(seed).decode("ascii"), PRIVATE_HEADER, PRIVATE_FOOTER)


def _decode(text: str, header: str, footer: str, what: str) -> bytes:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 3 or lines[0] != header or lines[2] != footer:
        raise ValueError("%s : format attendu « %s … %s »" % (what, header, footer))
    try:
        raw = base64.b64decode(lines[1], validate=True)
    except Exception as exc:
        raise ValueError("%s : base64 illisible (%s)" % (what, exc)) from exc
    if len(raw) != 32:
        raise ValueError("%s : 32 octets attendus, %d reçus" % (what, len(raw)))
    return raw


def decode_public(text: str) -> bytes:
    return check_public_key(_decode(text, PUBLIC_HEADER, PUBLIC_FOOTER, "clé publique"))


def decode_private(text: str) -> bytes:
    return _decode(text, PRIVATE_HEADER, PRIVATE_FOOTER, "clé privée")


def read_public(path: str) -> bytes:
    with open(path, encoding="utf-8") as fh:
        return decode_public(fh.read())


def read_private(path: str) -> bytes:
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            "clé privée %s lisible par d'autres (mode %o) : chmod 600" % (path, mode))
    with open(path, encoding="utf-8") as fh:
        return decode_private(fh.read())


def write_keypair(directory: str, name: str = "owner") -> tuple[str, str, str]:
    """Écrit une paire de clés (0600/0644). Acte du propriétaire, pas d'un agent."""
    backend_instance = backend()
    seed = backend_instance.generate_seed()
    public = backend_instance.public_from_seed(seed)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    private_path = os.path.join(directory, name + ".key")
    public_path = os.path.join(directory, name + ".pub")
    for path in (private_path, public_path):
        if os.path.exists(path):
            raise FileExistsError("refus d'écraser %s" % path)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(encode_private(seed))
    with open(public_path, "w", encoding="utf-8") as fh:
        fh.write(encode_public(public))
    os.chmod(public_path, 0o644)
    return private_path, public_path, fingerprint(public)
