# SPDX-License-Identifier: AGPL-3.0-only
"""A PostgreSQL connection string without its password (#518).

`split_password(dsn)` returns `(password, dsn without it)`: the password goes
to PGPASSWORD, which libpq reads, and the rest may go on a command line, which
every process of the host can read (ps, /proc/<pid>/cmdline). It follows
libpq's own parsing (`conninfo_parse`, `conninfo_uri_parse_options` and
`conninfo_uri_parse_params` in fe-connect.c), so that the connection the rest
describes is the one the whole string described:

* a URI (`postgresql://` or `postgres://`): the password of the userinfo
  (before the first `@` that comes before any `/`) and every `password` query
  parameter are taken out, percent-decoded (a query key too, and blanks
  around a token dropped); the user and every other part are kept as
  written, never re-encoded. A password in both places is refused;
* key/value pairs: libpq's tokenizer (blanks around `=`, single-quoted values,
  backslash escapes in quoted and bare values); every `password` entry is
  taken out, the last one wins, and the other entries are written back in
  order, quoted as libpq reads them;
* anything else is a bare database name, with no password.

libpq reads the string first, then a service file, then PGPASSWORD; the
split keeps that order. An explicit empty password stays in the rest: it is
no secret, and it still overrides the service and PGPASSWORD. A password next
to a service (`service` in the string, or PGSERVICE in the client's
environment) is refused: in PGPASSWORD, the service's password would win.

A string libpq's parser would refuse is refused (`DsnError`), with a message
that never quotes the string; an unknown keyword or a bad value is left to
libpq, which refuses it. Standard library only: `deploy/mise-a-jour/poste.sh`
runs this file with the host's python3 before ameesh is installed
(`python3 dsn.py password|dsn`, the DSN on standard input, byte for byte).
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping

#: isspace() in the C locale, as libpq tests it
_BLANKS = " \t\n\r\f\v"
_URI_PREFIXES = ("postgresql://", "postgres://")
_HEX = "0123456789abcdefABCDEF"


class DsnError(ValueError):
    """A connection string libpq would refuse; the message never quotes it."""


def split_password(dsn: str, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(password, the same connection string without it); ("", dsn) without one.

    `environ`: the environment of the client that gets the rest (os.environ by
    default), for PGSERVICE."""
    if "\x00" in dsn:   # libpq stops reading at a NUL: the rest would differ
        raise DsnError("a NUL byte in the DSN")
    if dsn.startswith(_URI_PREFIXES):
        password, rest, service = _split_uri(dsn)
    elif "=" in dsn:
        password, rest, service = _split_keywords(dsn)
    else:
        return "", dsn
    if password and (service or "PGSERVICE" in (os.environ if environ is None else environ)):
        raise DsnError("a password next to a service (service= or PGSERVICE): libpq reads "
                       "the service file before PGPASSWORD; put the password in the "
                       "service file or in ~/.pgpass")
    return password, rest


# -- URI ---------------------------------------------------------------------

def _uri_decode(text: str) -> str:
    """libpq's conninfo_uri_decode: %XX only, %00 forbidden; blanks (" ")
    around the token are dropped and a blank inside it is refused, as libpq
    16 to 18 do (an older one may keep them)."""
    raw = text.encode("utf-8", "surrogateescape").strip(b" ")
    if b" " in raw:
        raise DsnError("unexpected blank inside a URI token (use %20)")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x25:   # '%'
            pair = raw[i + 1:i + 3].decode("ascii", "replace")
            if len(pair) != 2 or any(c not in _HEX for c in pair):
                raise DsnError("invalid percent-encoded token in the URI")
            if pair == "00":
                raise DsnError("forbidden value %00 in the URI")
            out.append(int(pair, 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode("utf-8", "surrogateescape")


def _split_uri(dsn: str) -> tuple[str, str, bool]:
    prefix = next(p for p in _URI_PREFIXES if dsn.startswith(p))
    rest = dsn[len(prefix):]
    in_userinfo = None
    userinfo = ""
    end = 0
    while end < len(rest) and rest[end] not in "@/":
        end += 1
    if end < len(rest) and rest[end] == "@":
        user, colon, secret = rest[:end].partition(":")
        if colon and secret:   # libpq ignores an empty one
            in_userinfo = _uri_decode(secret)
        # the "@" stays, even after an empty user: without it, a later "@"
        # would end a userinfo libpq never read
        userinfo = user + "@"
        rest = rest[end + 1:]
    base, question, query = rest.partition("?")
    in_query = None
    service = False
    kept = []
    params = query.split("&") if query else []
    if params and not params[-1]:   # libpq accepts one trailing "&"
        params.pop()
    for param in params:
        if param.count("=") != 1:
            raise DsnError("malformed query parameter in the URI")
        key, value = param.split("=")
        key = _uri_decode(key)
        if key == "password":
            in_query = _uri_decode(value)   # the last one wins
        else:
            service = service or key == "service"
            kept.append(param)
    if in_userinfo is not None and in_query is not None:
        raise DsnError("a password in both the userinfo and the query of the URI")
    password = in_query if in_query is not None else in_userinfo
    if password == "":   # explicit and empty: it still overrides PGPASSWORD
        kept.append("password=")
    if kept:
        base += "?" + "&".join(kept)
    return password or "", prefix + userinfo + base, service


# -- key/value pairs ----------------------------------------------------------

def _tokens(dsn: str) -> list[tuple[str, str]]:
    """[(keyword, value)] as libpq's conninfo_parse reads them."""
    out = []
    i, n = 0, len(dsn)
    while i < n:
        if dsn[i] in _BLANKS:
            i += 1
            continue
        start = i
        while i < n and dsn[i] != "=" and dsn[i] not in _BLANKS:
            i += 1
        key = dsn[start:i]
        while i < n and dsn[i] in _BLANKS:
            i += 1
        if i >= n or dsn[i] != "=" or not key:
            raise DsnError("missing \"=\" after a keyword, or an empty keyword")
        i += 1
        while i < n and dsn[i] in _BLANKS:
            i += 1
        value = []
        if i < n and dsn[i] == "'":
            i += 1
            while True:
                if i >= n:
                    raise DsnError("unterminated quoted string")
                c = dsn[i]
                i += 1
                if c == "\\":
                    if i < n:
                        value.append(dsn[i])
                        i += 1
                    continue
                if c == "'":
                    break
                value.append(c)
        else:
            while i < n:
                c = dsn[i]
                i += 1
                if c in _BLANKS:
                    break
                if c == "\\":
                    if i < n:
                        value.append(dsn[i])
                        i += 1
                    continue
                value.append(c)
        out.append((key, "".join(value)))
    return out


def _quote(value: str) -> str:
    """A value as libpq reads it back (psycopg's `_param_escape`)."""
    if not value:
        return "''"
    value = re.sub(r"([\\'])", r"\\\1", value)
    return "'%s'" % value if re.search(r"\s", value) else value


def _split_keywords(dsn: str) -> tuple[str, str, bool]:
    password = None
    service = False
    kept = []
    for key, value in _tokens(dsn):
        if key == "password":
            password = value   # the last one wins
        else:
            service = service or key == "service"
            kept.append("%s=%s" % (key, _quote(value)))
    if password == "":   # explicit and empty: it still overrides PGPASSWORD
        kept.append("password=''")
    return password or "", " ".join(kept), service


# -- command line (deploy/mise-a-jour/poste.sh) --------------------------------

def main(argv: list[str] | None = None) -> int:
    """`dsn.py password|dsn`, the DSN on standard input: writes the password,
    or the DSN without it, on standard output. Neither reaches argv. Both are
    read and written byte for byte: a final newline may be data (a URI's
    database name, an escaped or quoted newline)."""
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["password"], ["dsn"]):
        print("usage: dsn.py password|dsn < DSN", file=sys.stderr)
        return 2
    dsn = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    try:
        password, rest = split_password(dsn)
    except DsnError as exc:
        print("dsn.py: %s" % exc, file=sys.stderr)
        return 1
    out = password if argv == ["password"] else rest
    sys.stdout.buffer.write(out.encode("utf-8", "surrogateescape"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
