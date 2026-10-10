# SPDX-License-Identifier: AGPL-3.0-only
"""JCS — JSON Canonicalization Scheme (RFC 8785), sous-ensemble sûr.

Le challenge d'un reçu (spec §8.1) et l'empreinte d'une action (spec §7.1)
sont des SHA-256 sur la forme canonique JCS d'un objet JSON : deux
implémentations (ce module, le JavaScript de la page d'approbation, une
application native) doivent produire **exactement** les mêmes octets.

Ce qui est fait, selon la RFC 8785 :

* objets : clés triées par **unités de code UTF-16** (§3.2.3) — pas par points
  de code : « 😀 » (U+1F600, D83D DE00 en UTF-16) se range avant « דּ » (U+FB33) ;
* aucun espace, séparateurs `,` et `:` ;
* chaînes : `"` et `\\` échappés, `\\b \\t \\n \\f \\r`, les autres contrôles
  U+0000–U+001F en `\\u00xx` (hexadécimal minuscule), tout le reste tel quel,
  sans normalisation Unicode (§3.2.2.2) ; sortie UTF-8 ;
* `null`, `true`, `false`.

Ce qui est **refusé** plutôt que mal sérialisé :

* les nombres non entiers, par défaut ; un flottant entier (`1.0`) est
  accepté et écrit `1` comme le ferait ECMAScript ; `-0.0` s'écrit `0`.
  Avec `doubles=True` (empreinte d'idempotence de l'API d'exécuteur, contrat
  1.1), les doubles sont écrits comme `Number.prototype.toString`
  d'ECMAScript (RFC 8785 §3.2.2.3) : chiffres les plus courts qui relisent
  le même double, notation exponentielle hors de [1e-6, 1e21[. Les reçus et
  les empreintes d'action gardent le refus : un signataire tiers n'a pas à
  reproduire cette sérialisation ;
* les entiers hors de l'intervalle sûr ±(2^53 − 1) : au-delà, un double ne
  distingue plus deux entiers voisins (2^53 et 2^53 + 1 ont la même
  représentation), et deux signataires pourraient ne pas voir le même nombre ;
* NaN et les infinis ; les surrogates UTF-16 isolés (chaîne non I-JSON) ;
* les clés non textuelles et tout type qui n'est pas du JSON.

`loads()` lit un texte JSON en refusant les clés en double et les constantes
NaN/Infinity : un reçu lu deux fois doit donner le même objet.
"""
from __future__ import annotations

import json
import math

#: Number.MAX_SAFE_INTEGER d'ECMAScript
MAX_SAFE_INTEGER = 2 ** 53 - 1
#: profondeur maximale d'imbrication acceptée (au-delà : refus, pas de récursion folle)
MAX_DEPTH = 64

_ESCAPES = {
    0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r",
    0x22: '\\"', 0x5C: "\\\\",
}


class JcsError(ValueError):
    """Valeur que JCS ne sait pas (ou ne doit pas) canoniser ici."""


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        escaped = _ESCAPES.get(code)
        if escaped is not None:
            out.append(escaped)
        elif code < 0x20:
            out.append("\\u%04x" % code)
        elif 0xD800 <= code <= 0xDFFF:
            raise JcsError("surrogate UTF-16 isolé U+%04X : chaîne non I-JSON" % code)
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _double(value: float) -> str:
    """Un double fini non entier, comme `Number.prototype.toString`."""
    from decimal import Decimal
    sign = "-" if value < 0 else ""
    sign_, digits_t, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in digits_t)
    k = len(digits)
    n = exponent + k  # position de la virgule
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
        body = "%se%s%d" % (mantissa, "+" if e > 0 else "-", abs(e))
    return sign + body


def _number(value, doubles: bool = False) -> str:
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise JcsError("entier hors de l'intervalle sûr ±(2^53-1) : %d" % value)
        return str(value)
    if not math.isfinite(value):
        raise JcsError("nombre non fini refusé : %r" % value)
    if doubles:
        return "0" if value == 0 else _double(value)
    if not value.is_integer():
        raise JcsError("nombre non entier refusé : %r (JCS des doubles non pris en charge)" % value)
    if abs(value) > MAX_SAFE_INTEGER:
        raise JcsError("nombre hors de l'intervalle sûr ±(2^53-1) : %r" % value)
    return str(int(value))  # -0.0 → "0", 1.0 → "1"


def _utf16_key(key: str) -> bytes:
    # Comparer les octets UTF-16 big-endian revient à comparer les unités de
    # code une à une (RFC 8785 §3.2.3).
    return key.encode("utf-16-be")


def _encode(value, out: list, depth: int, doubles: bool = False) -> None:
    if depth > MAX_DEPTH:
        raise JcsError("imbrication trop profonde (> %d)" % MAX_DEPTH)
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, (int, float)):
        out.append(_number(value, doubles))
    elif isinstance(value, dict):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise JcsError("clé d'objet non textuelle : %r" % (key,))
            encoded_key = _string(key)  # valide aussi les surrogates isolés
            items.append((_utf16_key(key), encoded_key, item))
        items.sort(key=lambda entry: entry[0])
        out.append("{")
        for index, (_sort, encoded_key, item) in enumerate(items):
            if index:
                out.append(",")
            out.append(encoded_key)
            out.append(":")
            _encode(item, out, depth + 1, doubles)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out, depth + 1, doubles)
        out.append("]")
    else:
        raise JcsError("type non JSON : %s" % type(value).__name__)


def dumps(value, *, doubles: bool = False) -> str:
    """Forme canonique JCS (texte). `doubles` : voir l'en-tête."""
    out: list[str] = []
    _encode(value, out, 0, doubles)
    return "".join(out)


def canonicalize(value, *, doubles: bool = False) -> bytes:
    """Forme canonique JCS, en octets UTF-8 — ce qui est haché et signé."""
    return dumps(value, doubles=doubles).encode("utf-8")


def _no_duplicates(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise JcsError("clé en double dans un objet JSON : %r" % key)
        found[key] = value
    return found


def _no_constant(name):
    raise JcsError("constante JSON non standard refusée : %s" % name)


def loads(text: str | bytes):
    """Lit un texte JSON strict : ni clé en double, ni NaN/Infinity."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JcsError("JSON non UTF-8 : %s" % exc) from exc
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except JcsError:
        raise
    except (ValueError, RecursionError) as exc:
        raise JcsError("JSON illisible : %s" % exc) from exc
