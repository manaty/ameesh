# SPDX-License-Identifier: AGPL-3.0-only
"""Accès Postgres pour agent-mesh.

Deux pilotes, une seule interface :

* **psycopg** (v3) s'il est importable : le chemin nominal, LISTEN/NOTIFY natif ;
* **psql** sinon : le binaire libpq, présent partout où Postgres est utilisé.
  Les requêtes sont alors exécutées par un sous-processus `psql`, les SELECT
  encapsulés en `json_agg` et les notifications lues sur un `psql` persistant
  qui écoute (`LISTEN` + `SELECT pg_sleep(...)`) — vérifié sur le conteneur.

Règles communes :

* les requêtes sont écrites avec des marqueurs `%s` (style psycopg) ; le pilote
  psql les remplace par des littéraux SQL échappés (`'` doublé, jamais de
  concaténation de chaîne non contrôlée) ;
* `query()` renvoie une liste de dicts, `execute()` un nombre de lignes (0 avec
  psql : utiliser `query()` avec `RETURNING` quand le compte importe) ;
* `script()` applique un script multi-instructions dans une transaction ;
* `transaction()` ouvre une transaction explicite (BEGIN … COMMIT, ROLLBACK
  sur exception) dont l'objet rendu a la même interface `query()` /
  `execute()` : lectures, contrôles et écritures entre les deux voient le
  même état, et les verrous pris (`pg_advisory_xact_lock`, `FOR UPDATE`)
  tiennent jusqu'au COMMIT. psycopg : `Connection.transaction()` ; psql :
  une session `psql` persistante sur des tubes (`_PsqlTransaction`).
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import pty
import queue
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from decimal import Decimal
from typing import Any, Iterable, Sequence, Union
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .config import Config, mask_dsn

#: sonde de connexion ET de schéma (L61) : une seule requête pour les deux
_SCHEMA_PROBE = "SELECT to_regclass('agent_registry') IS NOT NULL AS ok"
_SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_NOTIFY_RE = re.compile(
    r'^Asynchronous notification "([^"]+)" with payload "(.*)" received from server process'
)
_LISTEN_SLEEP = 31536000  # un an : le processus meurt avec l'exécuteur
_CONNECT_HINTS = (
    "could not connect",
    "connection refused",
    "connection to server",
    "no such file or directory",
    "timeout expired",
    "server closed the connection",
    # coupure en cours de requête ou de session (L72) : réseau, VPN, serveur
    # redémarré — même famille que l'échec de connexion
    "could not receive data",
    "could not send data",
    "connection timed out",
    "no route to host",
    "network is unreachable",
    "ssl syscall",
    "ssl connection has been closed",
    "terminating connection",
    "the database system is starting up",
    "the database system is shutting down",
    "connection is lost",
    "connection is closed",
    "connection already closed",
    "consuming input failed",
    "password authentication failed",
    "database \"",
    "role \"",
    "n'a pas pu",
)


class DbError(RuntimeError):
    """Erreur SQL ou de protocole."""


class Unavailable(DbError):
    """La base n'est pas joignable (ou le pilote n'existe pas).

    Panne passagère pour l'exécuteur (L72) : il réessaie avec une attente
    croissante au lieu de s'arrêter."""


class SchemaMissing(DbError):
    """La base répond mais les tables agent-mesh ne sont pas migrées."""


# --------------------------------------------------------------------------
# compteur d'allers-retours (L61)
# --------------------------------------------------------------------------

class Trace:
    """Compte les connexions et les allers-retours d'un processus.

    Un aller-retour, c'est un échange client → serveur → client : une requête,
    une instruction, un lot en pipeline (`query_batch`), un sous-processus
    `psql`. Les connexions sont comptées à part (TCP + TLS + authentification :
    plusieurs allers-retours réseau à elles seules).

    `AMEESH_DB_TRACE=FICHIER` (ou `stderr`) : à la sortie du processus, une
    ligne JSON `{"connects", "round_trips", "statements"}` y est ajoutée — c'est
    ce que lisent les tests qui bornent le nombre de requêtes d'une commande.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.connects = 0
        self.round_trips = 0
        self.statements: list[str] = []

    def connect(self) -> None:
        self.connects += 1

    def trip(self, sql: str = "") -> None:
        self.round_trips += 1
        self.statements.append(" ".join(str(sql).split())[:160])

    def snapshot(self) -> dict:
        return {"connects": self.connects, "round_trips": self.round_trips,
                "statements": list(self.statements)}


TRACE = Trace()


def _trace_dump() -> None:
    target = os.environ.get("AMEESH_DB_TRACE", "")
    if not target:
        return
    line = json.dumps(dict(TRACE.snapshot(), argv=sys.argv[1:]),
                      ensure_ascii=False)
    try:
        if target in ("1", "stderr"):
            sys.stderr.write(line + "\n")
        else:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


if os.environ.get("AMEESH_DB_TRACE"):
    import atexit
    atexit.register(_trace_dump)


def startup_options(cfg: Config, existing: str = "") -> str:
    """Les réglages de session passés DANS le paquet de démarrage (`options`
    libpq) plutôt qu'en `SET` après connexion : zéro aller-retour de plus.

    `existing` : les options déjà voulues par l'utilisateur (paramètre
    `options` du DSN ou PGOPTIONS), conservées devant les nôtres.
    """
    parts = [existing.strip()] if existing and existing.strip() else []
    if cfg.schema != "public":
        # Schéma autoportant : pas de repli sur public, sinon un
        # `CREATE TABLE IF NOT EXISTS` verrait la table de public et ne
        # créerait rien (isolation des tests et des chantiers).
        parts.append("-c search_path=%s" % quote_ident(cfg.schema))
    if cfg.statement_timeout_ms and cfg.statement_timeout_ms > 0:
        # Sans lui, une requête qui pend (réseau, verrou) bloquerait le
        # battement de bail et laisserait un harnais tourner sans bail.
        parts.append("-c statement_timeout=%d" % int(cfg.statement_timeout_ms))
    return " ".join(parts)


def dsn_has_options(dsn: str) -> bool:
    """Le DSN fixe-t-il déjà `options` (il primerait sur PGOPTIONS) ?"""
    if "://" in dsn:
        return bool(re.search(r"[?&]options=", dsn))
    return bool(re.search(r"(?:^|\s)options\s*=", dsn))


def is_remote(cfg: Config) -> bool:
    """La base est-elle hors de cette machine (ni socket Unix, ni boucle locale) ?"""
    dsn = cfg.dsn or ""
    host = ""
    if "://" in dsn:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        query = dict(kv.split("=", 1) for kv in parts.query.split("&") if "=" in kv)
        host = unquote(query.get("host", "")) or host
    else:
        match = re.search(r"(?:^|\s)host\s*=\s*(\S+)", dsn)
        host = match.group(1).strip("'\"") if match else ""
    host = host or os.environ.get("PGHOST", "")
    if not host or host.startswith("/") or host.startswith("@"):
        return False
    return host not in ("localhost", "127.0.0.1", "::1") and not host.startswith("127.")


# --------------------------------------------------------------------------
# échappement
# --------------------------------------------------------------------------

def quote_ident(name: str) -> str:
    """Identifiant SQL sûr (noms de schéma, de canal) : refus sinon."""
    if not _SQL_IDENT.match(name or ""):
        raise DbError("identifiant SQL invalide : %r" % (name,))
    return '"%s"' % name


def sql_literal(value: Any) -> str:
    """Littéral SQL échappé. Utilisé uniquement par le pilote psql."""
    if value is None:
        return "NULL"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "'\\x%s'::bytea" % bytes(value).hex()
    text = value if isinstance(value, str) else str(value)
    if "\x00" in text:
        raise DbError("caractère NUL interdit dans un paramètre SQL")
    return "'" + text.replace("'", "''") + "'"


def bind(sql: str, params: Sequence[Any]) -> str:
    """Remplace les `%s` par des littéraux échappés (pilote psql).

    Le SQL de ce paquet ne contient jamais `%s` dans une chaîne littérale :
    c'est une contrainte volontaire, vérifiée par les tests.
    """
    chunks = sql.split("%s")
    if len(chunks) - 1 != len(params):
        raise DbError("SQL : %d marqueurs pour %d paramètres" % (len(chunks) - 1, len(params)))
    out: list[str] = []
    for chunk, param in zip(chunks, params):
        out.append(chunk)
        out.append(sql_literal(param))
    out.append(chunks[-1])
    return "".join(out)


_DML_RE = re.compile(r"\b(insert|update|delete)\b", re.I)


def _json_query(sql: str) -> str:
    """Encapsule un SELECT (ou un DML avec RETURNING) en un seul texte JSON.

    Un DML est reconnu soit directement (`UPDATE … RETURNING`), soit derrière un
    `WITH` : dans les deux cas il est imbriqué dans un CTE, parce qu'un DML ne
    peut pas être mis en sous-requête du FROM.
    """
    direct = re.match(r"\s*(insert|update|delete)\b", sql, re.I)
    modifying = bool(direct) or (
        re.match(r"\s*with\b", sql, re.I) and bool(_DML_RE.search(sql))
    )
    if modifying:
        if not re.search(r"\breturning\b", sql, re.I):
            raise DbError("query() sur un DML sans RETURNING : %.60s" % sql)
        return (
            "WITH __dml AS (\n%s\n)\n"
            "SELECT coalesce(json_agg(row_to_json(__dml)), '[]'::json)::text FROM __dml" % sql
        )
    return (
        "SELECT coalesce(json_agg(row_to_json(t)), '[]'::json)::text\n"
        "FROM (\n%s\n) t" % sql
    )


def _normalize(value: Any) -> Any:
    import datetime
    import uuid

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, memoryview):
        return bytes(value).hex()
    return value


def _normalize_row(row: dict) -> dict:
    return {key: _normalize(value) for key, value in row.items()}


def _parse_json_rows(text: str) -> list[dict]:
    """Le texte JSON d'un `_json_query` (pilote psql) → liste de dicts."""
    if not text:
        return []
    try:
        rows = json.loads(text)
    except ValueError as exc:
        raise DbError("réponse JSON illisible de psql : %.120s" % text) from exc
    if isinstance(rows, dict):
        rows = [rows]
    return [_normalize_row(row) for row in rows]


def _batch_item(item) -> tuple[str, Sequence[Any]]:
    """`query_batch` accepte `sql` seul ou `(sql, params)`."""
    if isinstance(item, str):
        return item, ()
    sql, params = item
    return sql, tuple(params or ())


# --------------------------------------------------------------------------
# DSN
# --------------------------------------------------------------------------

def split_password(dsn: str) -> tuple[str, str]:
    """(mot de passe, DSN sans mot de passe) — le secret ne traîne pas en argv."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        password = ""
        if parts.netloc and "@" in parts.netloc:
            creds, host = parts.netloc.rsplit("@", 1)
            user, _, pwd = creds.partition(":")
            password = unquote(pwd)
            netloc = "%s@%s" % (quote(user), host)
            dsn = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    else:
        match = re.search(r"(?:^|\s)password=(\S+)", dsn)
        password = match.group(1).strip("'\"") if match else ""
        if match:
            dsn = (dsn[: match.start()] + " " + dsn[match.end():]).strip()
    return password, dsn


# --------------------------------------------------------------------------
# pilote psql
# --------------------------------------------------------------------------

class PsqlDriver:
    """Pilote par sous-processus `psql` (aucune dépendance Python)."""

    name = "psql"

    def __init__(self, cfg: Config):
        binary = shutil.which("psql")
        if not binary:
            raise Unavailable("psql introuvable dans le PATH")
        self.binary = binary
        self.cfg = cfg
        #: délai client : si le serveur ne répond plus (réseau coupé, requête
        #: qui pend), on ne bloque pas le battement de bail indéfiniment.
        self._client_timeout: float | None = (
            cfg.statement_timeout_ms / 1000.0 + 5.0
            if cfg.statement_timeout_ms and cfg.statement_timeout_ms > 0 else None
        )
        self.password, base = split_password(cfg.dsn)
        self.env = os.environ.copy()
        self.env["PGCONNECT_TIMEOUT"] = str(max(1, int(cfg.connect_timeout)))
        if self.password:
            self.env["PGPASSWORD"] = self.password
        base_args = [
            binary, "-X", "-q", "-A", "-t", "-P", "pager=off",
            "-v", "ON_ERROR_STOP=1", "-d", base,
        ]
        self._args = list(base_args)
        # Un `psql -c` ne lit jamais stdin : l'écouteur interactif a donc sa
        # propre ligne de commande, et reçoit le search_path par son amorce.
        self._interactive_args = list(base_args)
        self._bootstrap = ""
        if not dsn_has_options(base):
            # L61 : search_path et statement_timeout dans le paquet de
            # démarrage (PGOPTIONS) — aucun `SET` à envoyer avant la requête.
            options = startup_options(cfg, self.env.get("PGOPTIONS", ""))
            if options:
                self.env["PGOPTIONS"] = options
        else:
            # le DSN fixe `options` (il primerait sur PGOPTIONS) : réglages en SET
            if cfg.schema != "public":
                schema = "SET search_path TO %s;" % quote_ident(cfg.schema)
                self._args += ["-c", schema[:-1]]
                self._bootstrap = schema + "\n"
            if cfg.statement_timeout_ms and cfg.statement_timeout_ms > 0:
                timeout = "SET statement_timeout = %d;" % int(cfg.statement_timeout_ms)
                self._args += ["-c", timeout[:-1]]
                self._bootstrap = timeout + "\n" + self._bootstrap
        #: résultat de la vérification de schéma, faite avec la sonde de
        #: connexion (L61 : un seul sous-processus pour les deux)
        self._schema_ok: bool | None = None
        self._check()

    # -- interne -----------------------------------------------------------
    def _run(self, extra: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        # un sous-processus = une connexion et un échange
        TRACE.connect()
        TRACE.trip(" ".join(extra[1:]) if extra[:1] == ["-c"] else " ".join(extra))
        try:
            return subprocess.run(
                self._args + extra, input=stdin, capture_output=True, text=True,
                env=self.env, timeout=self._client_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Le serveur annule lui-même une requête trop longue
            # (statement_timeout, 5 s plus tôt) : un délai CLIENT dépassé dit
            # donc que le serveur ne répond plus — réseau ou base (L72).
            raise Unavailable(
                "psql : délai client dépassé (%ss) — requête abandonnée"
                % self._client_timeout) from exc
        except OSError as exc:
            raise Unavailable("psql n'a pas pu démarrer : %s" % exc) from exc

    def _check(self) -> None:
        # La sonde de connexion dit aussi si le schéma est migré : la
        # vérification de `require_schema` ne coûte pas un psql de plus.
        proc = self._run(["-c", _SCHEMA_PROBE])
        if proc.returncode == 0:
            self._schema_ok = proc.stdout.strip() in ("t", "true")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            message = detail[-1] if detail else "code %d" % proc.returncode
            low = (proc.stderr or "").lower()
            if proc.returncode == 2 or any(hint in low for hint in _CONNECT_HINTS):
                raise Unavailable("psql : %s" % message)
            raise DbError("psql : %s" % message)

    def _fail(self, proc: subprocess.CompletedProcess) -> DbError:
        lines = [l for l in (proc.stderr or "").strip().splitlines() if l.strip()]
        message = lines[-1] if lines else "code %d" % proc.returncode
        for line in reversed(lines):
            if "ERROR" in line or "ERREUR" in line:
                message = line.strip()
                break
        low = (proc.stderr or "").lower()
        if proc.returncode == 2 or any(hint in low for hint in _CONNECT_HINTS):
            return Unavailable("psql : %s" % message)
        return DbError("psql : %s" % message)

    # -- interface ---------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        proc = self._run(["-c", _json_query(bind(sql, params))])
        if proc.returncode != 0:
            raise self._fail(proc)
        return _parse_json_rows(proc.stdout.strip())

    def query_batch(self, items: Sequence[tuple]) -> list[list[dict]]:
        """Plusieurs SELECT indépendants en UN sous-processus psql et UNE
        instruction : chaque requête devient une sous-requête agrégée en JSON,
        réunies par `json_build_array` (psql n'enverrait sinon ses `-c` qu'un
        par un, un aller-retour chacun). Même contrat que
        `PsycopgDriver.query_batch` : des lectures, pas de DML."""
        items = [_batch_item(item) for item in items]
        if not items:
            return []
        if len(items) == 1:
            return [self.query(*items[0])]
        if any(not pure_read(sql) or ";" in sql.strip().rstrip(";") for sql, _ in items):
            return [self.query(sql, params) for sql, params in items]
        out: list[list[dict]] = []
        for start in range(0, len(items), 90):  # json_build_array : 100 arguments au plus
            chunk = items[start:start + 90]
            parts = ["(SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM (\n%s\n) t)"
                     % bind(sql.strip().rstrip(";"), params) for sql, params in chunk]
            proc = self._run(["-c", "SELECT json_build_array(%s)::text" % ",\n".join(parts)])
            if proc.returncode != 0:
                raise self._fail(proc)
            text = proc.stdout.strip()
            try:
                results = json.loads(text)
            except ValueError as exc:
                raise DbError("réponse JSON illisible de psql : %.120s" % text) from exc
            if not isinstance(results, list) or len(results) != len(chunk):
                raise DbError("psql : réponse de lot inattendue")
            out += [[_normalize_row(row) for row in (rows or [])] for rows in results]
        return out

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        proc = self._run(["-c", bind(sql, params)])
        if proc.returncode != 0:
            raise self._fail(proc)
        return 0

    def script(self, sql: str) -> None:
        proc = self._run(["-1", "-f", "-"], stdin=sql)
        if proc.returncode != 0:
            raise self._fail(proc)

    def transaction(self) -> "_PsqlTransaction":
        """Transaction explicite sur une session psql persistante (voir
        `_PsqlTransaction`) ; à utiliser en `with db.transaction() as tx:`."""
        return _PsqlTransaction(self)

    def listen(self, channels: Iterable[str]) -> "Listener":
        return _PsqlListener(self, list(channels))

    def close(self) -> None:
        pass

    def ping(self) -> None:
        self._check()


class _PsqlTransaction:
    """Transaction explicite avec le pilote psql (aucune dépendance Python).

    Un `psql` persistant lit ses instructions sur un tube : `BEGIN` à l'entrée
    du bloc `with`, `COMMIT` à sa sortie normale ; sur exception, la session
    est abandonnée (ROLLBACK, puis fin du processus : la connexion fermée
    annule de toute façon la transaction). Chaque instruction est suivie d'un
    `SELECT` témoin propre à la session : psql vide sa sortie après chaque
    résultat de requête (pas après `\\echo`, vérifié sur le conteneur), la
    réponse d'une instruction est donc tout ce qui précède son témoin.
    `ON_ERROR_STOP` : la première erreur termine psql (code 3) ; elle est lue
    sur stderr (fichier temporaire : un NOTICE ne bloque jamais le tube) et
    levée en DbError, la transaction étant annulée par la fermeture.

    Même interface que le pilote (`query`, `execute`, `cfg`, `name`) :
    le code qui reçoit `db` s'exécute tel quel dans la transaction. Un
    `transaction()` imbriqué rend la même session (pas de sous-transaction).
    """

    name = "psql"

    def __init__(self, driver: PsqlDriver):
        self.driver = driver
        self.cfg = driver.cfg
        self.proc: subprocess.Popen | None = None
        self._err = None
        self._buf = b""
        self._depth = 0
        self._seq = 0
        self._token = "__ameesh_tx_%s" % os.urandom(8).hex()

    # -- cycle de vie ------------------------------------------------------
    def __enter__(self) -> "_PsqlTransaction":
        if self.proc is not None:
            self._depth += 1
            return self
        self._err = tempfile.TemporaryFile()
        TRACE.connect()
        try:
            self.proc = subprocess.Popen(
                self.driver._interactive_args, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self._err, env=self.driver.env)
        except OSError as exc:
            self._err.close()
            raise Unavailable("psql n'a pas pu démarrer : %s" % exc) from exc
        try:
            for line in self.driver._bootstrap.splitlines():
                if line.strip():
                    self._run(line)
            self._run("BEGIN")
        except BaseException:
            self._close(abort=True)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._depth:
            self._depth -= 1
            return False
        try:
            if exc_type is None:
                self._run("COMMIT")
        except BaseException:
            self._close(abort=True)
            raise
        self._close(abort=exc_type is not None)
        return False

    def transaction(self) -> "_PsqlTransaction":
        return self

    def _close(self, abort: bool) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    if abort:
                        proc.stdin.write(b"ROLLBACK;\n")
                    proc.stdin.close()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2 if abort else 10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        finally:
            if self._err is not None:
                self._err.close()
                self._err = None

    # -- échanges ----------------------------------------------------------
    def _failure(self) -> DbError:
        proc = self.proc
        code = proc.wait() if proc is not None else -1
        text = ""
        if self._err is not None:
            self._err.seek(0)
            text = self._err.read().decode("utf-8", "replace")
        self._close(abort=True)
        return self.driver._fail(subprocess.CompletedProcess([], code or 3, "", text))

    def _run(self, sql: str) -> str:
        """Envoie une instruction, rend sa sortie (tout ce qui précède le témoin)."""
        if self.proc is None:
            raise DbError("psql : transaction fermée")
        self._seq += 1
        TRACE.trip(sql)
        marker = ("%s_%d__" % (self._token, self._seq)).encode()
        payload = sql.rstrip().rstrip(";") + ";\nSELECT '%s';\n" % marker.decode()
        try:
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
        except OSError:
            raise self._failure() from None
        timeout = self.driver._client_timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        fd = self.proc.stdout.fileno()
        while True:
            head, sep, rest = self._buf.partition(marker + b"\n")
            if sep:
                self._buf = rest
                return head.decode("utf-8", "replace").rstrip("\n")
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                self._close(abort=True)
                raise Unavailable("psql : délai client dépassé (%ss) — transaction annulée"
                                  % timeout)
            chunk = os.read(fd, 65536)
            if not chunk:
                raise self._failure()
            self._buf += chunk

    # -- interface ---------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return _parse_json_rows(self._run(_json_query(bind(sql, params))).strip())

    def query_batch(self, items: Sequence[tuple]) -> list[list[dict]]:
        """Dans une transaction ouverte : séquentiel, sur la même session."""
        return [self.query(sql, params) for sql, params in map(_batch_item, items)]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._run(bind(sql, params))
        return 0


class _PsqlListener:
    """Écoute LISTEN/NOTIFY avec `psql` (sans psycopg).

    Deux contraintes de psql, vérifiées sur le conteneur :

    * il ne vide son stdout (bloqué en `pg_sleep`) qu'à la fin de la requête ;
    * il n'imprime les notifications qu'en mode interactif, au tour de boucle
      suivant.

    D'où ce montage : `psql` tourne sur un pty (stdin+stdout), on lui envoie un
    battement `SELECT 1` deux fois par seconde, et on filtre la sortie sur la
    ligne « Asynchronous notification … ». La latence de réveil est donc de
    ~0,5 s, contre 5 s pour le sondage de repli.
    """

    HEARTBEAT = 0.5

    def __init__(self, driver: PsqlDriver, channels: list[str]):
        self.channels = channels
        self._queue: queue.Queue = queue.Queue()
        self._lines: list[str] = []
        self._down = threading.Event()
        self._stop = threading.Event()
        self._buf = ""
        self.master, slave = pty.openpty()
        try:
            self.proc = subprocess.Popen(
                driver._interactive_args, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, env=driver.env,
            )
        except OSError as exc:
            os.close(self.master)
            os.close(slave)
            raise Unavailable("psql n'a pas pu démarrer : %s" % exc) from exc
        os.close(slave)
        threading.Thread(target=self._read_pty, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        self._write(
            "\\set PROMPT1 ''\n\\set PROMPT2 ''\n"
            + driver._bootstrap
            + "".join("LISTEN %s;\n" % quote_ident(c) for c in channels)
        )

    def _write(self, text: str) -> None:
        try:
            os.write(self.master, text.encode("utf-8"))
        except OSError:
            self._down.set()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.HEARTBEAT):
            if self.proc.poll() is not None:
                break
            self._write("SELECT 1;\n")

    def _read_pty(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self._buf += data.decode("utf-8", "replace")
                *complete, self._buf = self._buf.split("\n")
                for raw in complete:
                    line = raw.strip()
                    if not line:
                        continue
                    match = _NOTIFY_RE.match(line)
                    if match:
                        self._queue.put({"channel": match.group(1), "payload": match.group(2)})
                    else:
                        self._lines.append(line)
                        del self._lines[:-20]
        finally:
            self._down.set()
            self._queue.put({"event": "down", "error": self.error})

    @property
    def error(self) -> str:
        return self._lines[-1] if self._lines else ""

    def wait(self, timeout: float) -> dict | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._down.is_set():
                return {"event": "down", "error": self.error}
            return None

    def close(self) -> None:
        self._stop.set()
        self._write("\\q\n")
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass


# --------------------------------------------------------------------------
# pilote psycopg
# --------------------------------------------------------------------------

class PsycopgDriver:
    """Pilote psycopg 3 : connexion directe, LISTEN/NOTIFY natif.

    L61 — chaque aller-retour compte quand la base est loin :

    * search_path et statement_timeout partent dans le paquet de démarrage
      (`options`), pas en `SET` ; la connexion établie suffit comme sonde ;
    * `require_schema(db, defer=True)` ne coûte rien sur le moment : la
      vérification part en pipeline AVEC la requête suivante ;
    * `query_batch` envoie plusieurs SELECT indépendants en un seul échange.
    """

    name = "psycopg"

    def __init__(self, cfg: Config):
        try:
            import psycopg  # noqa: F401
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise Unavailable("psycopg (v3) absent : %s" % exc) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.cfg = cfg
        #: reconnexion après une coupure (L72) : un seul fil rouvre la connexion
        self._reconnect_lock = threading.Lock()
        #: transactions explicites ouvertes : jamais de reconnexion au milieu
        self._tx_depth = 0
        self._schema_ok: bool | None = None
        #: vérification de schéma différée, jointe à la prochaine requête
        self._schema_pending = False
        #: la vérification différée a trouvé le schéma absent (voir _schema_from)
        self._schema_missing = False
        self.conn = self._connect()

    def _connect(self):
        kwargs: dict = dict(autocommit=True,
                            connect_timeout=max(1, int(self.cfg.connect_timeout)),
                            row_factory=self._dict_row)
        try:
            existing = self._psycopg.conninfo.conninfo_to_dict(self.cfg.dsn).get("options")
        except Exception:
            existing = None
        if existing is None:
            # un `options=` explicite écraserait PGOPTIONS : on le reprend
            existing = os.environ.get("PGOPTIONS", "")
        options = startup_options(self.cfg, str(existing or ""))
        if options:
            kwargs["options"] = options
        TRACE.connect()
        try:
            return self._psycopg.connect(self.cfg.dsn, **kwargs)
        except Exception as exc:  # psycopg.OperationalError et cie
            raise Unavailable("psycopg : %s" % _one_line(exc)) from exc

    def _check(self) -> None:
        TRACE.trip("SELECT 1")
        try:
            if self.cfg.schema != "public":
                self.conn.execute("SET search_path TO %s" % quote_ident(self.cfg.schema))
            if self.cfg.statement_timeout_ms and self.cfg.statement_timeout_ms > 0:
                self.conn.execute("SET statement_timeout = %d"
                                  % int(self.cfg.statement_timeout_ms))
            self.conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            raise Unavailable("psycopg : %s" % _one_line(exc)) from exc

    def _live(self) -> None:
        """Rouvre la connexion si une coupure l'a cassée (L72).

        psycopg marque la connexion `broken` (ou `closed`) après une erreur
        réseau, et ne se reconnecte jamais seul : sans ceci, une panne de base
        de quelques secondes rendait le pilote inutilisable jusqu'au
        redémarrage du processus. Jamais au milieu d'une transaction
        explicite : la suite de ses instructions passerait hors transaction ;
        on lève `Unavailable`, la transaction est perdue (annulée par la
        coupure) et l'appelant la rejoue en entier."""
        conn = self.conn
        if not (getattr(conn, "closed", False) or getattr(conn, "broken", False)):
            return
        if self._tx_depth:
            raise Unavailable("psycopg : connexion perdue pendant une transaction")
        with self._reconnect_lock:
            if self.conn is not conn:
                return  # un autre fil a déjà rouvert
            try:
                conn.close()
            except Exception:
                pass
            self.conn = self._connect()
            self._check()

    # -- vérification de schéma différée ------------------------------------
    def _pipeline_ok(self) -> bool:
        try:
            return bool(self._psycopg.Pipeline.is_supported())
        except Exception:
            return False

    def _settle_schema(self) -> None:
        """Vérifie maintenant (une requête) le schéma laissé en attente."""
        self._schema_pending = False
        require_schema(self)

    def _run_batch(self, items: list, *, probe: bool, fetch: bool) -> list:
        """Un seul échange (pipeline) : la sonde de schéma éventuelle, puis
        les requêtes. Rend, par requête, ses lignes (`fetch`) ou son rowcount."""
        TRACE.trip(" ;; ".join(sql for sql, _ in items))
        statements = ([(_SCHEMA_PROBE, ())] if probe else []) + list(items)
        cursors = []
        try:
            with self.conn.pipeline():
                for sql, params in statements:
                    cur = self.conn.cursor()
                    if params:
                        cur.execute(sql, tuple(params))
                    else:
                        cur.execute(sql)
                    cursors.append(cur)
            out = []
            for cur in cursors[1 if probe else 0:]:
                if fetch:
                    out.append([_normalize_row(dict(row)) for row in cur.fetchall()])
                else:
                    out.append(cur.rowcount)
        except Exception as exc:
            if probe:
                self._schema_from(cursors)
            raise _map_error(exc) from exc
        if probe:
            self._schema_from(cursors)
        return out

    def _schema_from(self, cursors: list) -> None:
        """Lit la sonde jointe ; lève SchemaMissing si le schéma manque."""
        try:
            row = cursors[0].fetchone() if cursors else None
        except Exception:
            row = None
        if row is None:
            return  # sonde sans réponse : l'erreur de la requête dira le reste
        self._schema_ok = bool(dict(row).get("ok"))
        if not self._schema_ok:
            # définitif pour cette connexion : un appelant qui avale l'erreur
            # (« liaison illisible → non lié ») ne doit pas continuer comme si
            # de rien n'était — la requête suivante redit de migrer
            self._schema_missing = True
            raise SchemaMissing(_schema_message(self))

    def _deferred(self, sql: str) -> bool:
        """La vérification en attente peut-elle voyager avec `sql` ?"""
        if self._schema_missing:
            raise SchemaMissing(_schema_message(self))
        if not self._schema_pending:
            return False
        if (";" in sql.strip().rstrip(";") or not self._pipeline_ok()
                or self.conn.info.transaction_status != self._psycopg.pq.TransactionStatus.IDLE):
            # plusieurs instructions (interdit en pipeline), ou transaction
            # ouverte : la sonde passe seule, d'abord
            self._settle_schema()
            return False
        self._schema_pending = False
        return True

    # -- interface ---------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        self._live()
        if self._deferred(sql):
            return self._run_batch([(sql, params)], probe=True, fetch=True)[0]
        TRACE.trip(sql)
        try:
            with self.conn.cursor() as cur:
                if params:
                    cur.execute(sql, tuple(params))
                else:
                    cur.execute(sql)
                return [_normalize_row(dict(row)) for row in cur.fetchall()]
        except Exception as exc:
            raise _map_error(exc) from exc

    def query_batch(self, items: Sequence[tuple]) -> list[list[dict]]:
        """Plusieurs SELECT indépendants en UN aller-retour (pipeline psycopg).

        `items` : des `(sql, params)` (ou `sql` seul). Rend une liste de
        résultats dans le même ordre. Sans pipeline (libpq < 14) : séquentiel.
        Pas de DML ici : une erreur annule le lot entier.
        """
        items = [_batch_item(item) for item in items]
        if not items:
            return []
        self._live()
        if self._schema_missing:
            raise SchemaMissing(_schema_message(self))
        probe = False
        if self._schema_pending:
            if any(";" in sql.strip().rstrip(";") for sql, _ in items):
                self._settle_schema()
            else:
                probe = self._deferred(items[0][0])
        if len(items) == 1 and not probe:
            return [self.query(*items[0])]
        if not self._pipeline_ok() or any(";" in sql.strip().rstrip(";") for sql, _ in items):
            if probe:
                self._schema_pending = True
                self._settle_schema()
            return [self.query(sql, params) for sql, params in items]
        return self._run_batch(items, probe=probe, fetch=True)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._live()
        if self._deferred(sql):
            return self._run_batch([(sql, params)], probe=True, fetch=False)[0]
        TRACE.trip(sql)
        try:
            with self.conn.cursor() as cur:
                if params:
                    cur.execute(sql, tuple(params))
                else:
                    cur.execute(sql)
                return cur.rowcount
        except Exception as exc:
            raise _map_error(exc) from exc

    def script(self, sql: str) -> None:
        self._live()
        if self._schema_missing:
            raise SchemaMissing(_schema_message(self))
        if self._schema_pending:
            self._settle_schema()
        TRACE.trip(sql)
        try:
            with self.conn.transaction():
                self.conn.execute(sql)
        except Exception as exc:
            raise _map_error(exc) from exc

    @contextlib.contextmanager
    def transaction(self):
        """Transaction explicite sur la connexion (BEGIN … COMMIT, ROLLBACK sur
        exception) ; rend le pilote lui-même, dont les requêtes passent par
        cette connexion. Imbriquée : un point de sauvegarde."""
        self._live()
        if self._schema_missing:
            raise SchemaMissing(_schema_message(self))
        if self._schema_pending:
            self._settle_schema()
        self._tx_depth += 1
        try:
            with self.conn.transaction():
                yield self
        except self._psycopg.Error as exc:
            raise _map_error(exc) from exc
        finally:
            self._tx_depth -= 1

    def listen(self, channels: Iterable[str]) -> "Listener":
        try:
            conn = self._connect()
            for channel in channels:
                TRACE.trip("LISTEN %s" % channel)
                conn.execute("LISTEN %s" % quote_ident(channel))
        except AttributeError as exc:  # psycopg trop ancien
            raise Unavailable("psycopg sans support LISTEN : %s" % exc) from exc
        return _PsycopgListener(conn, list(channels))

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def ping(self) -> None:
        self._live()
        self._check()


class _PsycopgListener:
    """Écoute LISTEN/NOTIFY avec psycopg 3.

    `Connection.notifies(stop_after=1)` jette les notifications non consommées
    à la fermeture du générateur : on draine donc la connexion entière dans un
    tampon (`notifies(timeout=0)`), puis on rend les notifications une à une.
    """

    def __init__(self, conn, channels: list[str]):
        self.conn = conn
        self.channels = channels
        self._pending: deque = deque()

    def wait(self, timeout: float) -> dict | None:
        import select

        attente = timeout
        while True:
            try:
                if not self._pending:
                    self._pending.extend(self.conn.notifies(timeout=0))
            except Exception as exc:
                return {"event": "down", "error": _one_line(exc)}
            if self._pending:
                notify = self._pending.popleft()
                return {"channel": notify.channel, "payload": notify.payload}
            try:
                pret, _, _ = select.select([self.conn], [], [], attente)
            except Exception as exc:
                return {"event": "down", "error": _one_line(exc)}
            if not pret:
                return None
            attente = 0  # réveillé : ne pas re-bloquer

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


def _map_error(exc: Exception) -> DbError:
    text = _one_line(exc).lower()
    if any(hint in text for hint in _CONNECT_HINTS):
        return Unavailable(_one_line(exc))
    if "does not exist" in text and ("relation" in text or "table" in text):
        return SchemaMissing(_one_line(exc))
    return DbError(_one_line(exc))


def is_unavailable(exc: BaseException) -> bool:
    """La base est-elle injoignable (panne passagère, L72) plutôt qu'en erreur ?"""
    return isinstance(exc, Unavailable)


def explain(exc: BaseException, limit: int = 160) -> str:
    """Cause lisible d'une erreur de base, sur une ligne, pour un statut ou un
    journal (L72). Un délai de connexion dépassé n'est pas un problème de
    migration : « ameesh migrate » n'est suggéré que si une table manque."""
    text = _one_line(exc)[:limit]
    low = text.lower()
    if "timeout expired" in low or "délai client dépassé" in low or "timed out" in low:
        return ("délai de connexion dépassé, base injoignable (réseau, VPN ou serveur ; "
                "délai réglable par AMEESH_CONNECT_TIMEOUT) : %s" % text)
    if isinstance(exc, Unavailable):
        return "base injoignable : %s" % text
    if isinstance(exc, SchemaMissing) or (
            "does not exist" in low and ("relation" in low or "table" in low
                                         or "column" in low or "colonne" in low)) \
            or "n'existe pas" in low:
        return "%s : « ameesh migrate » ?" % text
    return text


# --------------------------------------------------------------------------
# fabrique
# --------------------------------------------------------------------------

def connect(cfg: Config, driver: str | None = None) -> PsqlDriver | PsycopgDriver:
    """Ouvre une connexion. Lève `Unavailable` si la base (ou le pilote) manque.

    L109 : `backend: mediated` (exécuteur médié, session du harnais dans la
    VM) n'a pas de base : la « connexion » est le client de `/api/exec/v1`
    (`storage.remote.RemoteDb`), que `storage.of()` reconnaît."""
    if getattr(cfg, "backend", "") == "mediated":
        from .storage import remote
        from .mediated_executor import device
        # L110 dans L109 : jeton d'accès par l'identité de l'appareil enrôlé
        device.install_token_source()
        return remote.connect(cfg)  # type: ignore[return-value]
    wanted = driver or cfg.driver
    errors: list[str] = []
    if wanted in ("auto", "psycopg"):
        try:
            return PsycopgDriver(cfg)
        except Unavailable as exc:
            errors.append(str(exc))
            if wanted == "psycopg":
                raise
    if wanted in ("auto", "psql"):
        try:
            return PsqlDriver(cfg)
        except Unavailable as exc:
            errors.append(str(exc))
    raise Unavailable(" ; ".join(errors) or "aucun pilote Postgres disponible")


def _schema_message(db) -> str:
    return ("schéma agent-mesh absent (base %s, schéma %s) : lancez « agent-mesh migrate »"
            % (mask_dsn(db.cfg.dsn), db.cfg.schema))


def require_schema(db: PsqlDriver | PsycopgDriver, *, defer: bool = False) -> None:
    """Vérifie que les migrations sont passées, avec un message actionnable.

    L61 : la réponse est gardée sur la connexion (pilote psql : elle vient de
    la sonde de connexion, sans requête de plus). `defer=True` (psycopg) :
    rien n'est envoyé maintenant, la vérification part avec la prochaine
    requête et `SchemaMissing` est levée à ce moment-là.
    """
    if getattr(db, "_schema_ok", None) is True:
        return
    if defer and hasattr(db, "_schema_pending"):
        db._schema_pending = True
        return
    ok = bool(db.query(_SCHEMA_PROBE)[0].get("ok"))
    if hasattr(db, "_schema_ok"):
        db._schema_ok = ok
    if not ok:
        raise SchemaMissing(_schema_message(db))


def open_db(cfg: Config) -> PsqlDriver | PsycopgDriver:
    """Connexion + schéma vérifié, au moindre coût (L61) : le chemin des
    commandes. Avec psycopg, la vérification voyage avec la première requête
    (`SchemaMissing` peut donc venir d'elle) ; avec psql, de la sonde."""
    db = connect(cfg)
    try:
        require_schema(db, defer=True)
    except BaseException:
        db.close()
        raise
    return db


# --------------------------------------------------------------------------
# lecture groupée (L61)
# --------------------------------------------------------------------------
#
# Une commande de lecture (`work list`, `progress`, `alerts`…) enchaîne des
# requêtes indépendantes, éparpillées dans les modules métier : sur une base
# lointaine, chacune coûte un aller-retour. `batched(db, fn)` les regroupe
# SANS toucher au SQL des opérations de stockage (règle de `storage`) :
#
# 1. `fn` est d'abord jouée « à blanc » sur un enregistreur : chaque SELECT
#    inconnu est noté et reçoit une liste vide ;
# 2. les SELECT notés partent en UN aller-retour (`query_batch`) ;
# 3. on rejoue à blanc avec ces réponses : les requêtes qui dépendaient des
#    premières (`WHERE id IN (…)`) apparaissent, et partent au tour suivant ;
# 4. quand plus rien de neuf n'apparaît, `fn` est jouée pour de vrai : ses
#    lectures sont servies par les réponses gardées, toute autre requête va
#    à la base. Le résultat est donc toujours celui d'une vraie exécution —
#    au pire, une requête imprévue coûte son aller-retour, comme avant.
#
# Seules les lectures pures sont regroupées (SELECT/WITH sans DML, sans verrou,
# sans fonction à effet) ; une écriture ou une transaction arrête la passe à
# blanc, et, dans la vraie passe, coupe le cache (lecture de ses écritures).
# `fn` ne doit rien afficher ni écrire hors de la base : elle est rejouée.

_READ_RE = re.compile(r"^\s*\(?\s*(select|with|values)\b", re.I)
_UNSAFE_RE = re.compile(
    r"\b(insert|update|delete|merge|truncate|for\s+(no\s+key\s+)?update|for\s+(key\s+)?share"
    r"|pg_advisory\w*|nextval|setval|pg_notify|set_config|pg_sleep|txid_current\w*"
    r"|pg_current_xact_id\w*|lo_\w+|dblink\w*)\b", re.I)
#: écart toléré entre deux « maintenant » passés en paramètre (époque, en s)
_NOW_SLACK = 120.0
#: historiques en ajout seul, qu'aucune vue ne relit : un INSERT dans l'une
#: d'elles (relevé de jauges fait en passant par un affichage) ne retire du
#: cache que les réponses qui la lisent, au lieu de tout couper
APPEND_ONLY = ("quota_gauge_readings",)
_INSERT_RE = re.compile(r"^\s*insert\s+into\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.I)


class _Abort(Exception):
    """Une passe à blanc rencontre une écriture : elle s'arrête là."""


def pure_read(sql: str) -> bool:
    """SELECT sans effet : peut être regroupé et rejoué depuis le cache."""
    return bool(_READ_RE.match(sql)) and not _UNSAFE_RE.search(sql)


def _same_params(left: tuple, right: tuple) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a == b and type(a) is type(b):
            continue
        # un horodatage « maintenant » recalculé entre deux passes
        if (isinstance(a, float) and isinstance(b, float) and a > 1e9 and b > 1e9
                and abs(a - b) <= _NOW_SLACK):
            continue
        return False
    return True


class _Answers:
    """Réponses gardées, par SQL puis paramètres (horodatages tolérés)."""

    def __init__(self) -> None:
        self._by_sql: dict[str, list[tuple[tuple, list[dict]]]] = {}
        #: requêtes refusées par la base pendant le préchargement : jamais
        #: redemandées par les passes à blanc suivantes
        self.refused: list[tuple[str, tuple]] = []

    def get(self, sql: str, params: tuple):
        for known, rows in self._by_sql.get(sql, ()):
            if _same_params(known, params):
                return copy.deepcopy(rows)
        return None

    def forget(self, table: str) -> None:
        """Oublie les réponses des requêtes qui lisent `table`."""
        pattern = re.compile(r"\b%s\b" % re.escape(table), re.I)
        for sql in [sql for sql in self._by_sql if pattern.search(sql)]:
            del self._by_sql[sql]

    def put(self, sql: str, params: tuple, rows: list[dict]) -> None:
        self._by_sql.setdefault(sql, []).append((params, rows))


class _Recorder:
    """La connexion vue par une passe à blanc : aucune requête n'est envoyée."""

    def __init__(self, db, answers: _Answers):
        self._db = db
        self._answers = answers
        self.cfg = db.cfg
        self.name = db.name
        self.misses: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        value = getattr(self._db, name)
        if callable(value):
            raise _Abort(name)  # une méthode inconnue : on ne devine pas
        return value

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        params = tuple(params or ())
        rows = self._answers.get(sql, params)
        if rows is not None:
            return rows
        if not pure_read(sql):
            raise _Abort(sql)
        if sql.count("%s") != len(params) or (sql, params) in self._answers.refused:
            # requête bâtie sur une réponse vide de la passe à blanc : elle
            # serait refusée ; la vraie passe la construira correctement
            return []
        if not any(s == sql and _same_params(p, params) for s, p in self.misses):
            self.misses.append((sql, params))
        return []

    def query_batch(self, items: Sequence[tuple]) -> list[list[dict]]:
        return [self.query(sql, params) for sql, params in map(_batch_item, items)]

    def execute(self, *_a, **_k):
        raise _Abort("execute")

    def script(self, *_a, **_k):
        raise _Abort("script")

    def transaction(self):
        raise _Abort("transaction")

    def listen(self, *_a, **_k):
        raise _Abort("listen")

    def close(self) -> None:
        pass


class _Replay:
    """La connexion vue par la vraie passe : lectures servies par les réponses
    gardées tant qu'aucune écriture n'a eu lieu, le reste va à la base."""

    def __init__(self, db, answers: _Answers, *, owns: bool = False):
        self._db = db
        self._answers: _Answers | None = answers
        #: `prefetch` : la vue remplace la connexion, sa fermeture la ferme
        self._owns = owns

    def __getattr__(self, name: str):
        return getattr(self._db, name)

    def _cut(self, sql: str = "") -> None:
        match = _INSERT_RE.match(sql or "")
        if match and self._answers is not None and match.group(1).lower() in APPEND_ONLY:
            self._answers.forget(match.group(1).lower())
            return
        self._answers = None

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        if self._answers is not None:
            rows = self._answers.get(sql, tuple(params or ()))
            if rows is not None:
                return rows
        if not pure_read(sql):
            self._cut(sql)
        return self._db.query(sql, params)

    def query_batch(self, items: Sequence[tuple]) -> list[list[dict]]:
        items = [_batch_item(item) for item in items]
        out: list = [None] * len(items)
        todo = []
        for index, (sql, params) in enumerate(items):
            rows = self._answers.get(sql, params) if self._answers is not None else None
            if rows is None:
                todo.append(index)
            else:
                out[index] = rows
        if todo:
            for index, rows in zip(todo, self._db.query_batch([items[i] for i in todo])):
                out[index] = rows
        return out

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._cut(sql)
        return self._db.execute(sql, params)

    def script(self, sql: str) -> None:
        self._cut()
        return self._db.script(sql)

    def transaction(self):
        self._cut()
        return self._db.transaction()

    def close(self) -> None:
        if self._owns:
            self._db.close()
        # sinon la connexion appartient à l'appelant de `batched`


def batched(db, fn, *, levels: int = 4):
    """Exécute `fn(db)` en regroupant ses lectures (voir plus haut).

    `levels` borne le nombre d'allers-retours de préchargement (une requête
    qui dépend d'une autre en demande un de plus). Rend la valeur de `fn`.
    """
    if isinstance(db, (_Recorder, _Replay)) or not hasattr(db, "query_batch"):
        return fn(db)
    return fn(_Replay(db, _preload(db, fn, levels)))


def prefetch(db, fn, *, levels: int = 4):
    """Précharge les lectures de `fn` (passes à blanc, voir plus haut) et rend
    une connexion qui les sert : le code qui suit, inchangé, lit depuis ces
    réponses ; toute autre requête va à la base. La fermer ferme `db`.

    Pour une commande dont le code de lecture est déjà écrit en ligne :
    `db = db_mod.prefetch(db, lambda d: (registry.overview(d), …))`."""
    if isinstance(db, (_Recorder, _Replay)) or not hasattr(db, "query_batch"):
        return db
    return _Replay(db, _preload(db, fn, levels), owns=True)


def _preload(db, fn, levels: int) -> _Answers:
    answers = _Answers()
    for _level in range(levels):
        recorder = _Recorder(db, answers)
        try:
            fn(recorder)
        except Exception:
            pass  # réponses vides ou écriture : on garde ce qui a été noté
        if not recorder.misses:
            break
        try:
            results = db.query_batch(recorder.misses)
        except (SchemaMissing, Unavailable):
            raise
        except DbError:
            # une requête de la passe à blanc est invalide (bâtie sur une
            # réponse vide) : le lot échoue en entier. Repli : une à une, les
            # fautives écartées — la vraie passe fera les bonnes.
            results = []
            for sql, params in recorder.misses:
                try:
                    results.append(db.query(sql, params))
                except (SchemaMissing, Unavailable):
                    raise
                except DbError:
                    results.append(None)
        for (sql, params), rows in zip(recorder.misses, results):
            if rows is None:
                answers.refused.append((sql, params))
            else:
                answers.put(sql, params, rows)
    return answers


def listener(db: PsqlDriver | PsycopgDriver, channels: Iterable[str]) -> "Listener | None":
    """Ouvre un écouteur, ou None si le pilote ne sait pas écouter."""
    try:
        return db.listen(channels)
    except (Unavailable, DbError, NotImplementedError):
        return None


#: type commun aux deux pilotes (et à une transaction psql ouverte, même interface)
Db = Union[PsqlDriver, PsycopgDriver, _PsqlTransaction]
Listener = Union[_PsqlListener, _PsycopgListener]
