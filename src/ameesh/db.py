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
import json
import os
import pty
import queue
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from decimal import Decimal
from typing import Any, Iterable, Sequence, Union
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .config import Config, mask_dsn

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
        if cfg.schema != "public":
            # Schéma autoportant : pas de repli sur public, sinon un
            # `CREATE TABLE IF NOT EXISTS` verrait la table de public et ne
            # créerait rien (isolation des tests et des chantiers).
            schema = "SET search_path TO %s;" % quote_ident(cfg.schema)
            self._args += ["-c", schema[:-1]]
            self._bootstrap = schema + "\n"
        if cfg.statement_timeout_ms and cfg.statement_timeout_ms > 0:
            # Sans lui, une requête qui pend (réseau, verrou) bloquerait le
            # battement de bail et laisserait un harnais tourner sans bail.
            timeout = "SET statement_timeout = %d;" % int(cfg.statement_timeout_ms)
            self._args += ["-c", timeout[:-1]]
            self._bootstrap = timeout + "\n" + self._bootstrap
        self._check()

    # -- interne -----------------------------------------------------------
    def _run(self, extra: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
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
        proc = self._run(["-c", "SELECT 1"])
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
        text = proc.stdout.strip()
        if not text:
            return []
        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise DbError("réponse JSON illisible de psql : %.120s" % text) from exc
        if isinstance(rows, dict):
            rows = [rows]
        return [_normalize_row(row) for row in rows]

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
        text = self._run(_json_query(bind(sql, params))).strip()
        if not text:
            return []
        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise DbError("réponse JSON illisible de psql : %.120s" % text) from exc
        if isinstance(rows, dict):
            rows = [rows]
        return [_normalize_row(row) for row in rows]

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
    """Pilote psycopg 3 : connexion directe, LISTEN/NOTIFY natif."""

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
        self.conn = self._connect()
        self._check()

    def _connect(self):
        try:
            return self._psycopg.connect(
                self.cfg.dsn, autocommit=True,
                connect_timeout=max(1, int(self.cfg.connect_timeout)),
                row_factory=self._dict_row,
            )
        except Exception as exc:  # psycopg.OperationalError et cie
            raise Unavailable("psycopg : %s" % _one_line(exc)) from exc

    def _check(self) -> None:
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

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        self._live()
        try:
            with self.conn.cursor() as cur:
                if params:
                    cur.execute(sql, tuple(params))
                else:
                    cur.execute(sql)
                return [_normalize_row(dict(row)) for row in cur.fetchall()]
        except Exception as exc:
            raise _map_error(exc) from exc

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._live()
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
    """Ouvre une connexion. Lève `Unavailable` si la base (ou le pilote) manque."""
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


def require_schema(db: PsqlDriver | PsycopgDriver) -> None:
    """Vérifie que les migrations sont passées, avec un message actionnable."""
    row = db.query("SELECT to_regclass('agent_registry') IS NOT NULL AS ok")[0]
    if not row.get("ok"):
        raise SchemaMissing(
            "schéma agent-mesh absent (base %s, schéma %s) : lancez « agent-mesh migrate »"
            % (mask_dsn(db.cfg.dsn), db.cfg.schema)
        )


def listener(db: PsqlDriver | PsycopgDriver, channels: Iterable[str]) -> "Listener | None":
    """Ouvre un écouteur, ou None si le pilote ne sait pas écouter."""
    try:
        return db.listen(channels)
    except (Unavailable, DbError, NotImplementedError):
        return None


#: type commun aux deux pilotes (et à une transaction psql ouverte, même interface)
Db = Union[PsqlDriver, PsycopgDriver, _PsqlTransaction]
Listener = Union[_PsqlListener, _PsycopgListener]
