# SPDX-License-Identifier: AGPL-3.0-only
"""Journal des pannes serveur par empreinte (canon §16, lot #3889).

Un client ameesh n'affiche jamais une panne serveur à un humain : la panne
est gardée ici, regroupée par empreinte, pour qu'un lot s'ouvre tout seul
(primitive `ameesh work fingerprint open` du lot #3885).

Une empreinte = (application, route, exception, révision). La route est le
GABARIT (`POST /a/<jeton>`), jamais le chemin reçu : un jeton de lien est un
secret. L'exception est son nom de classe (avec son `code` ou son SQLSTATE,
et la classe de sa cause), jamais son message, qui peut porter une donnée
personnelle. Les seuls champs écrits sont ceux du contrat de #3885 :

    {empreinte, route, exception, revision, first_seen, last_seen,
     occurrences, users}

`users` compte les sujets distincts touchés (un identifiant opaque donné par
l'appelant, par exemple un `request_id`, gardé sous forme de condensat).

Le journal ne lève jamais : une panne du journal ne doit pas devenir une
nouvelle erreur affichée. Fichier JSON 0600, réécrit atomiquement sous verrou.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

SCHEMA = "ameesh-server-faults/1"
#: champs d'une entrée : exactement le contrat de #3885
FIELDS = ("empreinte", "route", "exception", "revision", "first_seen", "last_seen",
          "occurrences", "users")
#: condensats de sujets gardés par empreinte (borne mémoire et disque)
MAX_SUBJECTS = 500


def describe(exc: BaseException) -> str:
    """Le nom stable d'une exception, sans son message : classe, `code` ou
    SQLSTATE s'il y en a, puis la classe de sa cause directe."""
    def one(error: BaseException) -> str:
        name = type(error).__name__
        code = getattr(error, "sqlstate", None) or getattr(error, "code", None)
        if isinstance(code, str) and code and len(code) <= 40 and code.isprintable():
            name += ":" + code
        return name
    text = one(exc)
    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None:
        text += "<-" + one(cause)
    return text


def fingerprint(app: str, route: str, exception: str, revision: str) -> str:
    raw = "\x00".join(("ameesh-fault/1", app, route, exception, revision))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _subject(subject: str) -> str:
    return hashlib.sha256(("ameesh-fault-subject/1\x00" + subject).encode("utf-8")
                          ).hexdigest()[:16]


class Journal:
    """Le journal d'une application, dans `path` (un fichier JSON)."""

    def __init__(self, path: str, app: str, revision: str | None = None,
                 clock=time.time):
        self.path = path
        self.app = app
        if revision is None:
            from . import version
            revision = version()
        self.revision = revision
        self.clock = clock
        self._lock = threading.Lock()

    def record(self, route: str, exc: BaseException, *, subject: str | None = None) -> str:
        """Compte une panne ; rend son empreinte (vide si le journal a échoué)."""
        exception = describe(exc)
        key = fingerprint(self.app, route, exception, self.revision)
        try:
            with self._lock, self._locked():
                data = self._load()
                now = int(self.clock())
                entry = data["faults"].get(key)
                if entry is None:
                    entry = {"empreinte": key, "route": route, "exception": exception,
                             "revision": self.revision, "first_seen": now,
                             "occurrences": 0, "subjects": []}
                    data["faults"][key] = entry
                entry["last_seen"] = now
                entry["occurrences"] += 1
                if subject:
                    digest = _subject(subject)
                    if digest not in entry["subjects"] and len(entry["subjects"]) < MAX_SUBJECTS:
                        entry["subjects"].append(digest)
                self._save(data)
        except Exception as error:  # le journal ne lève jamais
            log.warning("server fault journal %s: %s", self.path, type(error).__name__)
            return ""
        log.error("server fault %s: %s on %s (%s)", key, exception, route, self.revision)
        return key

    def entries(self) -> list[dict]:
        """Les entrées au contrat de #3885, la plus récente d'abord."""
        try:
            with self._lock, self._locked():
                data = self._load()
        except OSError:
            return []
        out = []
        for entry in data["faults"].values():
            row = {name: entry.get(name) for name in FIELDS if name != "users"}
            row["users"] = len(entry.get("subjects") or [])
            out.append(row)
        return sorted(out, key=lambda row: row["last_seen"] or 0, reverse=True)

    # -- fichier -------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = None
        except ValueError:
            log.warning("server fault journal %s unreadable: started again", self.path)
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("faults"), dict):
            data = {"schema": SCHEMA, "app": self.app, "faults": {}}
        return data

    def _save(self, data: dict) -> None:
        tmp = "%s.tmp-%d" % (self.path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
