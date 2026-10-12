# SPDX-License-Identifier: AGPL-3.0-only
"""Defences shared by ameesh's stdlib HTTP servers (L84).

ameesh-approve (`approve.server`) and the read API (`serve`) are two
processes, two origins; what protects them both lives here, once:

* `RateLimiter`: token buckets per key (IP, token);
* `BoundedHTTPServer`: a `ThreadingHTTPServer` with a cap on simultaneous
  connections (beyond it a connection is closed at once), daemon threads,
  and errors logged in one line, never with the request;
* private files: `check_private` (owner = effective uid, nothing for the group
  nor the others), `ensure_private_dir`, and the 0600 token files
  `read_token_file` / `write_token_file` (no link, no overwrite, ASCII,
  bounded size);
* `is_loopback`, `bearer_ok` (constant-time comparison).

Every function that refuses raises the error class its caller gives
(`error=`), so that each service keeps its own (`ApproveConfigError`…).
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
from http.server import ThreadingHTTPServer

#: headers of every response, whatever the service (each adds its CSP)
BASE_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
#: a token file: at least this many characters, at most this many bytes
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_FILE = 4096
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bearer_ok(header: str | None, token: str) -> bool:
    """`Authorization: Bearer <token>`, compared in constant time."""
    scheme, _, value = (header or "").strip().partition(" ")
    value = value.strip()
    return (scheme.lower() == "bearer" and bool(value) and bool(token)
            and hmac.compare_digest(value.encode(), token.encode()))


class RateLimiter:
    """Seaux à jetons : `per_minute` requêtes par minute, rafale = per_minute."""

    MAX_KEYS = 10000

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > self.MAX_KEYS:
                # les seaux pleins n'apprennent rien : on les oublie
                for stale in [k for k, (t, l) in self._buckets.items()
                              if t + (now - l) * self.rate >= self.capacity]:
                    del self._buckets[stale]
                if len(self._buckets) > self.MAX_KEYS:
                    self._buckets.clear()
            return allowed


class BoundedHTTPServer(ThreadingHTTPServer):
    """At most `max_connections` connections served at once; the others are
    closed at once (never queued without limit)."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    #: where `handle_error` and the refusals are logged
    log = logging.getLogger("ameesh.http")

    def __init__(self, address, handler, *, max_connections: int = 64):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.log.warning("too many simultaneous connections: connection closed")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        # pas de trace complète sur stderr : une ligne, sans donnée de requête
        self.log.warning("connection interrupted: %s", sys.exc_info()[0].__name__)


# --------------------------------------------------------------------------
# private files
# --------------------------------------------------------------------------

def check_private(st: os.stat_result, what: str, path: str, *, error=ValueError) -> None:
    if st.st_uid != os.geteuid():
        raise error("%s %s: not owned by the service user" % (what, path))
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise error(
            "%s %s: permissions too open (%04o); chmod %s"
            % (what, path, stat.S_IMODE(st.st_mode), "700" if stat.S_ISDIR(st.st_mode) else "600"))


def ensure_private_dir(path: str, *, error=ValueError) -> str:
    """Crée (0700) ou contrôle un dossier privé : ni lien, ni ouvert aux autres."""
    path = expand(path)
    if not os.path.lexists(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise error("directory %s: real directory expected (not a link)" % path)
    check_private(st, "directory", path, error=error)
    return path


def read_token_file(path: str, *, what: str = "service token", hint: str = "",
                    error=ValueError) -> str:
    """Lit un jeton ; refuse un lien, un fichier lisible par d'autres."""
    path = expand(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise error("%s not found: %s%s" % (what, path, hint and " (%s)" % hint)) from exc
    except OSError as exc:
        raise error("unreadable %s %s: %s" % (what, path, exc)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise error("%s %s: regular file expected" % (what, path))
        check_private(st, what, path, error=error)
        if st.st_size > MAX_TOKEN_FILE:
            raise error("%s %s: file too large" % (what, path))
        raw = os.read(fd, MAX_TOKEN_FILE + 1)
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise error("%s %s: ASCII expected" % (what, path)) from exc
    if len(token) < MIN_TOKEN_LENGTH or not _TOKEN_RE.fullmatch(token):
        raise error("%s %s: at least %d printable characters, "
                    "no spaces" % (what, path, MIN_TOKEN_LENGTH))
    return token


def write_token_file(path: str, *, error=ValueError) -> str:
    """Crée un jeton neuf (256 bits) en 0600 ; refuse d'écraser."""
    path = expand(path)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise error("%s already exists: a token is never overwritten" % path) from exc
    try:
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return path


__all__ = ["BASE_HEADERS", "BoundedHTTPServer", "RateLimiter", "bearer_ok", "check_private",
           "ensure_private_dir", "expand", "is_loopback", "read_token_file",
           "write_token_file"]
