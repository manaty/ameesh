# SPDX-License-Identifier: AGPL-3.0-only
"""CBOR minimal et clés publiques COSE (RFC 8949, RFC 9052/9053).

Une passkey WebAuthn livre sa clé publique en COSE_Key (une table CBOR) ; le
canon la recopie telle quelle. Ce module n'en lit que ce dont ameesh a
besoin :

* CBOR : entiers, chaînes d'octets et de texte, tableaux, tables, `false`,
  `true`, `null` — longueurs définies seulement. Refus des étiquettes, des
  flottants, des longueurs indéfinies, des clés de table en double, et de
  toute longueur qui dépasse les octets disponibles (pas d'allocation sur la
  foi d'un en-tête) ;
* COSE_Key : EC2 / P-256 / ES256 (kty 2, crv 1, alg -7) et OKP / Ed25519 /
  EdDSA (kty 1, crv 6, alg -8). Une clé qui porte une partie privée (`d`) est
  refusée bruyamment : elle n'a rien à faire dans le canon. Une clé Ed25519 de
  petit ordre ou non canonique est refusée aussi (falsification universelle).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import p256, signing

MAX_DEPTH = 16

# étiquettes COSE (RFC 9052 §7, RFC 9053 §7)
KTY, ALG, CRV, X, Y, D = 1, 3, -1, -2, -3, -4
KTY_OKP, KTY_EC2 = 1, 2
ALG_ES256, ALG_EDDSA = -7, -8
CRV_P256, CRV_ED25519 = 1, 6


class CborError(ValueError):
    """CBOR mal formé ou hors du sous-ensemble accepté."""


def _argument(data: bytes, index: int, info: int) -> tuple[int, int]:
    if info < 24:
        return info, index
    sizes = {24: 1, 25: 2, 26: 4, 27: 8}
    size = sizes.get(info)
    if size is None:
        raise CborError("CBOR : longueur indéfinie ou réservée (info %d)" % info)
    if index + size > len(data):
        raise CborError("CBOR : en-tête tronqué")
    return int.from_bytes(data[index:index + size], "big"), index + size


def _item(data: bytes, index: int, depth: int):
    if depth > MAX_DEPTH:
        raise CborError("CBOR : imbrication trop profonde")
    if index >= len(data):
        raise CborError("CBOR : donnée tronquée")
    initial = data[index]
    major, info = initial >> 5, initial & 0x1F
    index += 1
    if major == 7:
        simple = {20: False, 21: True, 22: None}
        if info not in simple:
            raise CborError("CBOR : valeur simple ou flottant non pris en charge (%d)" % info)
        return simple[info], index
    if major == 6:
        raise CborError("CBOR : étiquettes non prises en charge")
    arg, index = _argument(data, index, info)
    remaining = len(data) - index
    if major == 0:
        return arg, index
    if major == 1:
        return -1 - arg, index
    if major in (2, 3):
        if arg > remaining:
            raise CborError("CBOR : chaîne plus longue que les données")
        raw = bytes(data[index:index + arg])
        index += arg
        if major == 2:
            return raw, index
        try:
            return raw.decode("utf-8"), index
        except UnicodeDecodeError as exc:
            raise CborError("CBOR : texte non UTF-8") from exc
    if major == 4:
        if arg > remaining:
            raise CborError("CBOR : tableau plus long que les données")
        items = []
        for _ in range(arg):
            value, index = _item(data, index, depth + 1)
            items.append(value)
        return items, index
    # major == 5 : table
    if arg * 2 > remaining:
        raise CborError("CBOR : table plus longue que les données")
    table: dict = {}
    for _ in range(arg):
        key, index = _item(data, index, depth + 1)
        if not isinstance(key, (int, str)) or isinstance(key, bool):
            raise CborError("CBOR : clé de table ni entière ni textuelle")
        if key in table:
            raise CborError("CBOR : clé de table en double : %r" % (key,))
        value, index = _item(data, index, depth + 1)
        table[key] = value
    return table, index


def decode(data: bytes, index: int = 0, *, allow_trailing: bool = False):
    """Décode un élément CBOR ; refuse les octets en trop sauf demande contraire.

    Renvoie (valeur, index de fin).
    """
    value, end = _item(bytes(data), index, 0)
    if not allow_trailing and end != len(data):
        raise CborError("CBOR : %d octet(s) en trop" % (len(data) - end))
    return value, end


# --------------------------------------------------------------------------
# clés publiques
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PublicKey:
    """Clé publique prête à vérifier : `ES256` (point P-256) ou `EdDSA` (Ed25519)."""

    alg: str
    point: tuple = ()      # (x, y) pour ES256
    raw: bytes = b""       # 32 octets pour EdDSA

    def verify(self, message: bytes, signature: bytes) -> bool:
        if self.alg == "ES256":
            return p256.verify(self.point, message, signature)
        if self.alg == "EdDSA":
            # contrôles communs (clé et R hors petit ordre, S réduit) avant
            # tout backend — redondant avec `ed25519_key`, voulu
            if signing.signature_error(self.raw, signature) is not None:
                return False
            return bool(signing.backend().verify(self.raw, message, signature))
        return False


def es256_key(point: tuple[int, int]) -> PublicKey:
    if not p256.is_on_curve(point[0], point[1]):
        raise CborError("clé ES256 : point hors de la courbe P-256")
    return PublicKey("ES256", point=(point[0], point[1]))


def ed25519_key(raw: bytes) -> PublicKey:
    """Clé Ed25519 brute (32 octets) → PublicKey.

    Refus d'un encodage non canonique et des 8 points de petit ordre (dont le
    neutre) : une telle clé accepterait une signature constante pour tout
    message (`signing.public_key_error`).
    """
    raw = bytes(raw)
    error = signing.public_key_error(raw)
    if error:
        raise CborError("clé Ed25519 refusée : %s" % error)
    return PublicKey("EdDSA", raw=raw)


def parse_cose_key(blob: bytes) -> PublicKey:
    """COSE_Key → PublicKey. Seuls ES256/P-256 et EdDSA/Ed25519 sont admis."""
    try:
        table, _end = decode(blob)
    except CborError as exc:
        raise CborError("clé COSE illisible : %s" % exc) from exc
    if not isinstance(table, dict):
        raise CborError("clé COSE : table CBOR attendue")
    if D in table:
        raise CborError("clé COSE contenant une partie PRIVÉE (d) : refus")
    kty, alg, crv = table.get(KTY), table.get(ALG), table.get(CRV)
    if kty == KTY_EC2:
        if alg != ALG_ES256 or crv != CRV_P256:
            raise CborError("clé COSE EC2 : seul ES256 sur P-256 est admis (alg %r, crv %r)"
                            % (alg, crv))
        x, y = table.get(X), table.get(Y)
        if not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
            raise CborError("clé COSE EC2 : x et y de 32 octets attendus (point non compressé)")
        return es256_key((int.from_bytes(x, "big"), int.from_bytes(y, "big")))
    if kty == KTY_OKP:
        if alg != ALG_EDDSA or crv != CRV_ED25519:
            raise CborError("clé COSE OKP : seul EdDSA sur Ed25519 est admis (alg %r, crv %r)"
                            % (alg, crv))
        x = table.get(X)
        if not isinstance(x, bytes):
            raise CborError("clé COSE OKP : x attendu")
        return ed25519_key(x)
    raise CborError("clé COSE : type de clé non pris en charge (kty %r)" % (kty,))
