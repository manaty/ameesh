# SPDX-License-Identifier: AGPL-3.0-only
"""État durable d'ameesh-approve : des fichiers 0600 dans un dossier 0700.

Aucune table nouvelle (pas de migration) : le service tourne sous un autre
utilisateur Unix que les agents, et son état reste chez lui.

    <état>/requests/<request_id>.json   la demande (sans le jeton)
    <état>/links/<sha256(jeton)>        → request_id
    <état>/receipts/<request_id>.json   le reçu (JCS) — sa création EST la
                                         consommation du lien
    <état>/enroll/<sha256(jeton)>.json  un jeton d'enrôlement
    <état>/enroll-used/<sha256(jeton)>  marque d'usage d'un jeton d'enrôlement

Les jetons de lien ne sont jamais écrits en clair : seul leur SHA-256 l'est
(nom de fichier), et la valeur relue est comparée à temps constant.

Usage unique : un fichier final est créé par `os.link` d'un fichier
temporaire complet — atomique, et en échec si la cible existe. Deux POST
simultanés sur le même lien ne peuvent donc pas produire deux reçus.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time

from .. import jcs
from .config import ensure_private_dir

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
REQUEST_ID_RE = re.compile(r"^req_[0-9a-z]{26}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SUBDIRS = ("requests", "links", "receipts", "enroll", "enroll-used")

#: rétention après échéance (élagage opportuniste)
KEEP_REQUESTS = 86400
KEEP_RECEIPTS = 7 * 86400


def new_token() -> str:
    """Jeton de lien : 256 bits, base64url (43 caractères)."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(("ameesh-approve-link/1\x00" + token).encode("utf-8")).hexdigest()


def new_request_id() -> str:
    return "req_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


class Store:
    def __init__(self, root: str):
        self.root = ensure_private_dir(root)
        for name in _SUBDIRS:
            ensure_private_dir(os.path.join(self.root, name))
        self._last_prune = 0.0

    # -- fichiers ----------------------------------------------------------
    def _path(self, sub: str, name: str) -> str:
        return os.path.join(self.root, sub, name)

    def _write(self, path: str, data: bytes, *, exclusive: bool) -> bool:
        """Écrit atomiquement (0600). `exclusive` : faux si la cible existe."""
        directory = os.path.dirname(path)
        tmp = os.path.join(directory, ".tmp-%s" % secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if exclusive:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    return False
            else:
                os.replace(tmp, path)
                tmp = ""
            return True
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _read(path: str) -> bytes | None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _read_json(self, path: str) -> dict | None:
        raw = self._read(path)
        if raw is None:
            return None
        try:
            data = jcs.loads(raw)
        except jcs.JcsError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _dump(data: dict) -> bytes:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")

    # -- demandes d'approbation -------------------------------------------
    def save_request(self, record: dict, token: str) -> None:
        request_id = record["request_id"]
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("request_id invalide")
        digest = token_hash(token)
        record = dict(record, token_hash=digest)
        if not self._write(self._path("requests", request_id + ".json"), self._dump(record),
                           exclusive=True):
            raise ValueError("request_id déjà utilisé")
        if not self._write(self._path("links", digest), request_id.encode("ascii"),
                           exclusive=True):
            raise ValueError("jeton déjà enregistré")

    def get_request(self, request_id: str) -> dict | None:
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            return None
        return self._read_json(self._path("requests", request_id + ".json"))

    def request_by_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return None
        digest = token_hash(token)
        raw = self._read(self._path("links", digest))
        if raw is None:
            return None
        request_id = raw.decode("ascii", "replace")
        record = self.get_request(request_id)
        if record is None or not hmac.compare_digest(
                str(record.get("token_hash", "")).encode(), digest.encode()):
            return None
        return record

    # -- reçus --------------------------------------------------------------
    def save_receipt_once(self, request_id: str, receipt: dict) -> bool:
        """Enregistre LE reçu de la demande ; faux si un reçu existe déjà."""
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("request_id invalide")
        return self._write(self._path("receipts", request_id + ".json"),
                           jcs.canonicalize(receipt), exclusive=True)

    def receipt_bytes(self, request_id: str) -> bytes | None:
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            return None
        return self._read(self._path("receipts", request_id + ".json"))

    def has_receipt(self, request_id: str) -> bool:
        return os.path.exists(self._path("receipts", request_id + ".json"))

    # -- enrôlement ----------------------------------------------------------
    def save_enroll(self, record: dict, token: str) -> None:
        digest = token_hash(token)
        record = dict(record, token_hash=digest)
        if not self._write(self._path("enroll", digest + ".json"), self._dump(record),
                           exclusive=True):
            raise ValueError("jeton d'enrôlement déjà enregistré")

    def enroll_by_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return None
        digest = token_hash(token)
        record = self._read_json(self._path("enroll", digest + ".json"))
        if record is None or not hmac.compare_digest(
                str(record.get("token_hash", "")).encode(), digest.encode()):
            return None
        return record

    def enroll_used(self, record: dict) -> bool:
        return os.path.exists(self._path("enroll-used", record["token_hash"]))

    def claim_enroll(self, record: dict) -> bool:
        """Consomme le jeton d'enrôlement, une seule fois."""
        digest = record["token_hash"]
        if not _HASH_RE.fullmatch(digest):
            return False
        return self._write(self._path("enroll-used", digest),
                           str(int(time.time())).encode("ascii"), exclusive=True)

    # -- élagage -------------------------------------------------------------
    def prune(self, now: float, *, every: float = 600.0) -> int:
        """Supprime les demandes échues depuis longtemps (au plus toutes les 10 min)."""
        if now - self._last_prune < every:
            return 0
        self._last_prune = now
        removed = 0
        for name in os.listdir(self._path("requests", "")):
            if not name.endswith(".json"):
                continue
            request_id = name[:-5]
            record = self.get_request(request_id)
            if record is None:
                continue
            exp = float(record.get("exp") or 0)
            if exp + KEEP_RECEIPTS < now:
                for path in (self._path("receipts", request_id + ".json"),):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
            if exp + KEEP_REQUESTS < now and not self.has_receipt(request_id):
                for path in (self._path("links", str(record.get("token_hash", "x"))),
                             self._path("requests", name)):
                    try:
                        os.unlink(path)
                        removed += 1
                    except (FileNotFoundError, IsADirectoryError):
                        pass
        for name in os.listdir(self._path("enroll", "")):
            if not name.endswith(".json"):
                continue
            record = self._read_json(self._path("enroll", name))
            if record and float(record.get("exp") or 0) + KEEP_REQUESTS < now:
                for path in (self._path("enroll", name), self._path("enroll-used", name[:-5])):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                removed += 1
        return removed
