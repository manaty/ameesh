# SPDX-License-Identifier: AGPL-3.0-only
"""Journal des pannes serveur par empreinte (canon §16, lot #3889).

Un client ameesh n'affiche jamais une panne serveur à un humain : la panne
est gardée ici, regroupée par empreinte, et devient un lot par la primitive
de #3885 (`ameesh.fingerprints.open_lot`, projet `ameesh`) :

- une application qui écrit dans la base du maillage (la page de
  progression) **rapporte** elle-même ses entrées nouvelles (:meth:`Journal.report`) ;
- ameesh-approve, en lecture seule dans la base (deploy/sql/role-approve.sql),
  **sert** son journal en page `nexlink-fingerprints/1` (:meth:`Journal.page`,
  `GET /fingerprints`) : la passe `fingerprints` de #3885 la lit comme toute
  source déclarée au canon.

Contrat (celui du lecteur de #3885, sans aucun autre champ) :

    {fingerprint, route, code, revision, first_seen, last_seen, occurrences, users}

- `fingerprint` : sha256 hexadécimal de (route, code), la révision hors
  empreinte (une panne qui survit à un déploiement reste le même lot) ;
- `route` : le client et le GABARIT de route (`ameesh-approve POST
  /a/<token>`), jamais le chemin reçu — un jeton de lien est un secret ;
- `code` : `SQLSTATE:<code>` si la cause est une erreur SQL, sinon
  `EXCEPTION:<classe>` (avec son `code` et la classe de sa cause), jamais le
  message, qui peut porter une donnée personnelle ;
- `first_seen`, `last_seen` : instants ISO 8601 UTC ;
- `users` : humains distincts touchés (un sujet opaque, gardé condensé).

Une entrée par (empreinte, révision). Le curseur est un numéro de mise à
jour croissant. Le journal ne lève jamais : sa panne ne doit pas devenir une
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

#: le projet des lots ouverts (`--app` de la primitive #3885)
APP = "ameesh"
#: le format du fichier
FILE_SCHEMA = "ameesh-server-faults/2"
#: le format d'une page servie : celui du lecteur de #3885
PAGE_SCHEMA = "nexlink-fingerprints/1"
#: champs d'une entrée : exactement le contrat de #3885
FIELDS = ("fingerprint", "route", "code", "revision", "first_seen", "last_seen",
          "occurrences", "users")
#: bornes du lecteur de #3885
MAX_ROUTE = 300
MAX_CODE = 120
#: condensats de sujets gardés par entrée (borne mémoire et disque)
MAX_SUBJECTS = 500
#: sujet d'une panne vue par un humain dont l'identité n'est pas connue ici
HUMAN = "human"


def code_of(exc: BaseException) -> str:
    """Le code stable d'une exception, sans son message : le SQLSTATE d'une
    erreur SQL de la chaîne, sinon `EXCEPTION:` classe (et `code`) puis la
    classe de sa cause directe."""
    chain, error = [], exc
    while error is not None and len(chain) < 5:
        chain.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    for error in chain:
        sqlstate = getattr(error, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate.isalnum() and len(sqlstate) == 5:
            return "SQLSTATE:" + sqlstate

    def one(error: BaseException) -> str:
        name = type(error).__name__
        code = getattr(error, "code", None)
        if isinstance(code, str) and code and len(code) <= 40 and code.isprintable():
            name += ":" + code
        return name
    text = "EXCEPTION:" + one(chain[0])
    if len(chain) > 1:
        text += "<-" + one(chain[1])
    return text


def fingerprint(route: str, code: str) -> str:
    raw = "ameesh-failure/1\n%s\n%s" % (route, code)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _subject(subject: str) -> str:
    return hashlib.sha256(("ameesh-fault-subject/1\x00" + subject).encode("utf-8")
                          ).hexdigest()[:16]


class Journal:
    """Le journal d'un client ameesh (`client` : `ameesh-approve`, …), dans
    `path` (un fichier JSON)."""

    def __init__(self, path: str, client: str, revision: str | None = None,
                 clock=time.time):
        self.path = path
        self.client = client
        if revision is None:
            from . import version
            revision = version()
        self.revision = revision
        self.clock = clock
        self._lock = threading.Lock()

    def record(self, route: str, exc: BaseException, *, subject: str | None = None) -> str:
        """Compte une panne de `route` (gabarit) ; rend son empreinte (vide si
        le journal a échoué). `subject` : l'humain touché (opaque, condensé)."""
        # bornes du lecteur de #3885 (route 300, code 120 caractères)
        route = ("%s %s" % (self.client, route))[:MAX_ROUTE]
        code = code_of(exc)[:MAX_CODE]
        key = fingerprint(route, code)
        try:
            with self._lock, self._locked():
                data = self._load()
                now = self.clock()
                data["seq"] += 1
                slot = "%s %s" % (key, self.revision)
                entry = data["faults"].get(slot)
                if entry is None:
                    entry = {"fingerprint": key, "route": route, "code": code,
                             "revision": self.revision, "first_seen": _iso(now),
                             "occurrences": 0, "subjects": [], "reported": 0}
                    data["faults"][slot] = entry
                entry["last_seen"] = _iso(now)
                entry["occurrences"] += 1
                entry["seq"] = data["seq"]
                if subject:
                    digest = _subject(subject)
                    if digest not in entry["subjects"] and len(entry["subjects"]) < MAX_SUBJECTS:
                        entry["subjects"].append(digest)
                self._save(data)
        except Exception as error:  # le journal ne lève jamais
            log.warning("server fault journal %s: %s", self.path, type(error).__name__)
            return ""
        log.error("server fault %s: %s on %s (%s)", key[:16], code, route, self.revision)
        return key

    # -- lecture -------------------------------------------------------------
    def _rows(self) -> list[dict]:
        with self._lock, self._locked():
            return list(self._load()["faults"].values())

    @staticmethod
    def _public(entry: dict) -> dict:
        row = {name: entry.get(name) for name in FIELDS if name != "users"}
        row["users"] = len(entry.get("subjects") or [])
        return row

    def entries(self) -> list[dict]:
        """Les entrées au contrat de #3885, la plus récente d'abord."""
        try:
            rows = self._rows()
        except OSError:
            return []
        rows.sort(key=lambda row: row.get("seq") or 0, reverse=True)
        return [self._public(row) for row in rows]

    def page(self, since: str | None = None) -> dict:
        """Une page `nexlink-fingerprints/1` : les entrées mises à jour après
        le curseur `since` (toutes sans lui) ; `cursor` est le dernier numéro
        de mise à jour servi (null : rien de nouveau)."""
        floor = int(since) if since and since.isdigit() else 0
        rows = sorted((row for row in self._rows() if (row.get("seq") or 0) > floor),
                      key=lambda row: row["seq"])
        return {"schema": PAGE_SCHEMA,
                "cursor": str(rows[-1]["seq"]) if rows else None,
                "fingerprints": [self._public(row) for row in rows]}

    # -- rapport direct (#3885) --------------------------------------------
    @property
    def pending(self) -> bool:
        """Une entrée a-t-elle des occurrences pas encore rapportées ?"""
        try:
            return any(row["occurrences"] > row.get("reported", 0) for row in self._rows())
        except (OSError, KeyError, TypeError):
            return False

    def report(self, db, open_lot=None) -> list[dict]:
        """Rapporte chaque entrée nouvelle à la primitive de #3885 (un lot par
        empreinte, noté s'il est ouvert) ; une entrée rapportée ne l'est plus
        tant qu'elle ne revient pas. Lève si la primitive échoue : le reste
        est gardé pour le rapport suivant."""
        if open_lot is None:
            from . import fingerprints
            open_lot = fingerprints.open_lot
        done = []
        for row in self._rows():
            if row["occurrences"] <= row.get("reported", 0):
                continue
            public = self._public(row)
            result = open_lot(
                db, app=APP, key=public["fingerprint"],
                title=("Panne serveur %s — %s" % (public["route"], public["code"]))[:200],
                users=public["users"], count=public["occurrences"], route=public["route"],
                code=public["code"], revision=public["revision"],
                first_seen=public["first_seen"], last_seen=public["last_seen"])
            self._mark(row["fingerprint"], row["revision"], row["occurrences"])
            done.append(result)
        return done

    def _mark(self, key: str, revision: str, occurrences: int) -> None:
        with self._lock, self._locked():
            data = self._load()
            entry = data["faults"].get("%s %s" % (key, revision))
            if entry is not None:
                entry["reported"] = max(entry.get("reported", 0), occurrences)
                self._save(data)

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
        if (not isinstance(data, dict) or data.get("schema") != FILE_SCHEMA
                or not isinstance(data.get("faults"), dict)
                or not isinstance(data.get("seq"), int)):
            data = {"schema": FILE_SCHEMA, "client": self.client, "seq": 0, "faults": {}}
        return data

    def _save(self, data: dict) -> None:
        tmp = "%s.tmp-%d-%d" % (self.path, os.getpid(), threading.get_ident())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
