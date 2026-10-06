# SPDX-License-Identifier: AGPL-3.0-only
"""Adaptateur ACP générique (L16, R22) : pont JSON-RPC pour un tour de l'exécuteur.

Posé par `adapters.HarnessAdapter.command()` pour tout descripteur dont
`ameesh.protocol` vaut `acp`, le pont :

1. lance l'agent ACP (stdio, messages JSON-RPC délimités par `\\n`) ;
2. `initialize` (version 1, capacités du client : lecture/écriture de fichiers
   dans le dossier du tour), `authenticate` si le descripteur nomme une méthode ;
3. `session/new` ou reprend la session (`session/resume`, sinon `session/load`,
   si l'agent les annonce ; sinon session neuve, le résumé de reprise L11 étant
   dans la consigne) ;
4. applique le modèle, l'effort et le tier par `session/set_config_option`
   quand l'agent les expose (au mieux : l'agent garde ses défauts sinon) ;
5. `session/prompt` avec la consigne, lit les `session/update` jusqu'à la fin du
   tour, répond aux `session/request_permission` selon la politique du
   descripteur — **refus par défaut**, journalisé — et aux `fs/*` dans le
   dossier de travail seulement ;
6. à l'interruption (SIGTERM/SIGINT de l'exécuteur), envoie `session/cancel`,
   répond `cancelled` aux permissions en attente, attend un délai borné puis
   tue l'agent (SIGTERM, puis SIGKILL).

Tout message illisible, trop grand, d'id inconnu ou de méthode inattendue met
fin au tour **en erreur** : jamais d'attente sans issue. Sa sortie standard est
un flux d'**événements JSONL** que `adapters.AcpStream` relit (correspondance
documentée dans `docs/design/descripteurs-de-harnais.md`) ; sa sortie d'erreur
est le journal, que l'exécuteur range dans `stderr.log`. L'agent reste dans le
même groupe de processus que le pont : l'exécuteur peut donc tuer le groupe
entier (aucune fuite), au prix d'une annulation « au mieux » si l'agent meurt
sur SIGTERM avant de lire `session/cancel`.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import __version__, harnesses
from .harnesses import HarnessDescriptor

#: version majeure du protocole ACP que ce pont parle
PROTOCOL_VERSION = 1
#: délai laissé à l'agent pour conclure après `session/cancel` (l'exécuteur
#: envoie SIGKILL au groupe au bout de `AgentWorker.TERMINATE_GRACE`, 3 s : ce
#: délai doit rester en dessous pour que la fin du tour soit écrite)
CANCEL_GRACE = 2.0
#: délais par défaut : initialisation/session, et inactivité pendant un tour
DEFAULT_INIT_TIMEOUT = 120.0
DEFAULT_IDLE_TIMEOUT = 1800.0
#: taille maximale d'un message JSON-RPC (garde-fou mémoire)
MAX_MESSAGE_CHARS = 4 * 1024 * 1024
#: taille maximale d'une écriture demandée par l'agent (fs/write_text_file)
MAX_WRITE_CHARS = 4 * 1024 * 1024
#: méthodes client que ce pont n'implémente pas : refus explicite (fail-closed)
UNSUPPORTED_CLIENT_METHODS = (
    "terminal/create", "terminal/output", "terminal/wait_for_exit",
    "terminal/kill", "terminal/release", "elicitation/create", "elicitation/complete",
)
#: catégories ACP utilisées pour retrouver une option de configuration
DEFAULT_CATEGORIES = {"model": ("model",), "effort": ("thought_level", "model_config"),
                      "tier": ()}


class AcpError(RuntimeError):
    """Le tour ACP ne peut pas aboutir (protocole, agent, délai)."""


class AcpRpcError(AcpError):
    """Une requête JSON-RPC a rendu une erreur."""

    def __init__(self, code: Any, message: str, data: Any = None):
        super().__init__("erreur JSON-RPC %s : %s" % (code, message))
        self.code = code
        self.message = message
        self.data = data


class AcpCancelled(AcpError):
    """Le tour a été interrompu (signal de l'exécuteur) : annulation propre."""


def log(message: str) -> None:
    """Journal du pont : toujours sur la sortie d'erreur."""
    try:
        sys.stderr.write("[acp] %s\n" % message)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# JSON-RPC 2.0 sur stdio
# --------------------------------------------------------------------------

@dataclass
class Pending:
    """Une requête en vol : son événement, son résultat ou son erreur."""

    id: int
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: dict | None = None

    def raise_for_error(self) -> Any:
        if self.error is not None:
            raise AcpRpcError(self.error.get("code"), self.error.get("message") or "?",
                              self.error.get("data"))
        return self.result


def _valid_rpc_id(value: Any) -> bool:
    """Les `id` JSON-RPC 2.0 permis : chaîne, entier, ou null (jamais un booléen)."""
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_message(message: dict) -> None:
    """Valide l'enveloppe JSON-RPC 2.0 d'un message reçu de l'agent.

    Lève `AcpError` : un message non conforme n'est **jamais** ignoré, il met
    fin au tour. Sans cela, un `id` non hachable faisait mourir le fil de
    lecture avant d'avoir enregistré la panne, et le tour attendait le délai.
    """
    if message.get("jsonrpc") != "2.0":
        raise AcpError('message JSON-RPC sans `jsonrpc: "2.0"`')
    if "method" in message:
        method = message["method"]
        if not isinstance(method, str) or not method:
            raise AcpError("`method` JSON-RPC invalide : %r" % (method,))
        if "id" in message and not _valid_rpc_id(message["id"]):
            raise AcpError("`id` JSON-RPC invalide : %r" % (message["id"],))
        params = message.get("params", {})
        if params is not None and not isinstance(params, dict):
            raise AcpError("`params` JSON-RPC invalide (objet attendu)")
        return
    if "id" not in message or not _valid_rpc_id(message["id"]):
        raise AcpError("réponse JSON-RPC sans `id` valide")
    if ("result" in message) == ("error" in message):
        raise AcpError("réponse JSON-RPC : exactement un de `result` ou `error` attendu")
    if "error" in message:
        error = message["error"]
        if not isinstance(error, dict) or not isinstance(error.get("message"), str) \
                or not (isinstance(error.get("code"), int)
                        and not isinstance(error.get("code"), bool)):
            raise AcpError("objet `error` JSON-RPC invalide")


class JsonRpcPeer:
    """Un agent ACP lancé en sous-processus, et le JSON-RPC qui l'accompagne.

    Le fil de lecture consomme la sortie standard en continu (aucun blocage si
    l'agent écrit beaucoup) et traite les requêtes serveur → client par un
    gestionnaire fourni. Une ligne illisible, un message trop grand, une
    réponse à une requête inconnue ou une méthode inattendue **arrêtent le
    tour en erreur** (fail-closed), sans jamais laisser une requête en attente.
    """

    def __init__(self, argv: Sequence[str], *, cwd: str | None = None,
                 env: Mapping[str, str] | None = None,
                 max_message: int = MAX_MESSAGE_CHARS):
        self.argv = list(argv)
        self.max_message = max(1024, int(max_message))
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, Pending] = {}
        self._request_handler: Callable[[str, dict], Any] | None = None
        self._notification_handler: Callable[[str, dict], None] | None = None
        self._eof = threading.Event()
        #: dernière ligne reçue de l'agent (délai d'inactivité)
        self.last_message = time.monotonic()
        #: première panne de protocole : elle prime sur toute réponse
        self.fatal: AcpError | None = None
        self.proc = subprocess.Popen(
            self.argv, cwd=cwd, env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- handlers ----------------------------------------------------------
    def set_handlers(self, request: Callable[[str, dict], Any],
                     notification: Callable[[str, dict], None]) -> None:
        self._request_handler = request
        self._notification_handler = notification

    # -- pannes ------------------------------------------------------------
    def _fail(self, exc: AcpError) -> None:
        """Enregistre la panne et débloque toutes les requêtes en vol."""
        with self._lock:
            if self.fatal is None:
                self.fatal = exc
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.error = {"code": -32000, "message": str(exc)}
            pending.event.set()

    def raise_fatal(self) -> None:
        if self.fatal is not None:
            raise self.fatal

    # -- envoi -------------------------------------------------------------
    def _send(self, message: dict) -> None:
        with self._write_lock:
            if self.proc.stdin is None or self.proc.poll() is not None:
                raise AcpError("l'agent ACP n'est plus vivant")
            try:
                self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AcpError("écriture vers l'agent ACP impossible : %s" % exc)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def send_request(self, method: str, params: dict | None = None) -> Pending:
        self.raise_fatal()
        with self._lock:
            self._next_id += 1
            pending = Pending(id=self._next_id)
            self._pending[pending.id] = pending
        self._send({"jsonrpc": "2.0", "id": pending.id, "method": method,
                    "params": params or {}})
        return pending

    def wait(self, pending: Pending, timeout: float | None = None,
             cancel: threading.Event | None = None) -> Pending:
        """Attend une réponse ; `cancel` armé, lève `AcpCancelled` sans répondre."""
        deadline = None if not timeout else time.monotonic() + timeout
        while True:
            if pending.event.wait(0.1):
                self.raise_fatal()
                return pending
            self.raise_fatal()
            if cancel is not None and cancel.is_set():
                raise AcpCancelled()
            if deadline is not None and time.monotonic() >= deadline:
                raise AcpError("délai dépassé en attendant %s" % pending.id)
            if self._eof.is_set() and not pending.event.is_set():
                raise AcpError("l'agent ACP s'est arrêté avant de répondre")

    def request(self, method: str, params: dict | None = None, *,
                timeout: float | None = None,
                cancel: threading.Event | None = None) -> Any:
        pending = self.send_request(method, params)
        self.wait(pending, timeout=timeout, cancel=cancel)
        return pending.raise_for_error()

    # -- réception ---------------------------------------------------------
    def _read_loop(self) -> None:
        """Consomme la sortie de l'agent ; **toute** panne est fatale et enregistrée.

        Un fil de lecture qui meurt en silence laisserait la requête en vol
        attendre jusqu'au délai : ici, la moindre exception est convertie en
        erreur fatale (`_fail`), qui débloque les requêtes et arrête le tour.
        """
        try:
            self._read_messages()
        except BaseException as exc:  # ultime filet : jamais un fil mort silencieux
            self._fail(AcpError("lecteur JSON-RPC interrompu : %r" % (exc,)))
        finally:
            self._eof.set()
            with self._lock:
                pendings = list(self._pending.values())
                self._pending.clear()
            for pending in pendings:
                if not pending.event.is_set():
                    pending.error = {"code": -32000,
                                     "message": "l'agent ACP s'est arrêté"}
                    pending.event.set()

    def _read_messages(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            return
        while True:
            try:
                line = stream.readline(self.max_message + 1)
                if line == "":
                    return
                self.last_message = time.monotonic()
                if len(line) > self.max_message:
                    raise AcpError("message JSON-RPC trop grand (> %d caractères)"
                                   % self.max_message)
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise AcpError("ligne illisible sur la sortie de l'agent : %s"
                                   % line[:200]) from exc
                if not isinstance(message, dict):
                    raise AcpError("message JSON-RPC non objet")
                _validate_message(message)
                self._dispatch(message)
            except AcpError as exc:
                self._fail(exc)
                return
            except Exception as exc:
                # id non hachable, type inattendu, bogue de dispatch : fatal aussi
                self._fail(AcpError("message JSON-RPC non conforme (%s) : %s"
                                    % (type(exc).__name__, exc)))
                return

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            params = message.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            if "id" in message:
                self._answer_request(message["id"], message["method"], params)
            else:
                handler = self._notification_handler
                if handler is None:
                    raise AcpError("notification ACP inattendue : %s" % message["method"])
                handler(message["method"], params)
            return
        pending_id = message["id"]
        with self._lock:
            pending = self._pending.pop(pending_id, None)
        if pending is None:
            raise AcpError("réponse à une requête inconnue (id %r)" % (pending_id,))
        if "error" in message:
            pending.error = message["error"]
        else:
            pending.result = message.get("result")
        pending.event.set()

    def _answer_request(self, request_id: Any, method: str, params: dict) -> None:
        if method in UNSUPPORTED_CLIENT_METHODS:
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601,
                                  "message": "méthode client non supportée : %s" % method}})
            raise AcpError("méthode client inattendue : %s" % method)
        handler = self._request_handler
        if handler is None:
            raise AcpError("méthode client inattendue : %s" % method)
        try:
            result = handler(method, params)
        except AcpRpcError as exc:
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": exc.code, "message": exc.message,
                                  **({"data": exc.data} if exc.data is not None else {})}})
            return
        except AcpError as exc:
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32000, "message": str(exc)}})
            raise
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    # -- arrêt -------------------------------------------------------------
    def close(self, *, grace: float = CANCEL_GRACE) -> None:
        """Ferme l'entrée puis tue l'agent (SIGTERM, puis SIGKILL après `grace`).

        L'agent est notre enfant, dans notre groupe : on ne tue pas le groupe
        (on en fait partie) ; l'exécuteur, lui, tue le groupe entier.
        """
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=max(0.0, grace))
            except subprocess.TimeoutExpired:
                log("l'agent ne répond pas au SIGTERM : SIGKILL")
                try:
                    self.proc.kill()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass


# --------------------------------------------------------------------------
# politique de permissions
# --------------------------------------------------------------------------

def _option_id(options: Sequence[dict], allow: bool) -> str | None:
    wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in wanted:
        for option in options:
            if isinstance(option, dict) and option.get("kind") == kind:
                return str(option.get("optionId"))
    return None


def permission_decision(permissions: Mapping[str, Any], tool_call: Mapping[str, Any],
                        options: Sequence[dict]) -> tuple[str, str | None, str]:
    """Politique de permission : refuse par défaut, journalise la raison.

    Renvoie (décision, optionId, raison). La décision est `allow` ou `deny` ;
    l'option choisie est la plus faible qui convient (`allow_once` /
    `reject_once`), et None si l'agent n'en propose pas d'utilisable — le pont
    répond alors `cancelled`, comme le veut l'ACP.
    """
    kind = str(tool_call.get("kind") or "other").strip().lower()
    deny_kinds = {str(k).lower() for k in (permissions.get("deny_kinds") or [])}
    allow_kinds = {str(k).lower() for k in (permissions.get("allow_kinds") or [])}
    default = permissions.get("default") or "deny"
    if kind in deny_kinds:
        decision, reason = "deny", "kind %s explicitement refusé" % kind
    elif default == "allow":
        decision, reason = "allow", "politique par défaut : allow"
    elif kind in allow_kinds:
        decision, reason = "allow", "kind %s admis par allow_kinds" % kind
    else:
        decision, reason = "deny", "kind %s non couvert (refus par défaut)" % kind
    return decision, _option_id(options, decision == "allow"), reason


def _content_text(update: Mapping[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _flatten_options(option: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for item in option.get("options") or []:
        if not isinstance(item, dict):
            continue
        if "group" in item:  # option groupée
            for value in item.get("options") or []:
                if isinstance(value, dict) and value.get("value") is not None:
                    values.append(str(value["value"]))
        elif item.get("value") is not None:
            values.append(str(item["value"]))
    return values


def _open_root(cwd: str) -> int:
    """Ouvre le dossier de travail, une fois, sans suivre aucun lien.

    Le chemin est parcouru **depuis la racine du système**, composant par
    composant, chacun en `O_NOFOLLOW` : un lien nulle part dans le chemin (même
    un ancêtre du dossier de travail) n'est suivi. Le descripteur rendu est
    épinglé pour tout le tour : les opérations `fs/*` ne rouvrent jamais la
    racine par son nom, donc un remplacement du chemin après l'acquisition ne
    déplace pas la frontière.

    Lève `AcpRpcError` (`-32002`) si le dossier n'est pas ouvrable sans suivre
    de lien, ou si l'un de ses ancêtres en est un.
    """
    racine = os.path.abspath(cwd)
    composants = [c for c in racine.split(os.sep) if c]
    try:
        fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:  # pragma: no cover : tout système POSIX a une racine
        raise AcpRpcError(-32002, "racine du système illisible : %s" % exc)
    try:
        for composant in composants:
            suivant = os.open(composant,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = suivant
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
            log("dossier de travail atteint par un lien (%r) : refusé" % cwd)
            raise AcpRpcError(-32002, "dossier de travail atteint par un lien : %s" % cwd)
        raise AcpRpcError(-32002, "dossier de travail illisible : %s" % exc)
    return fd


def _open_beneath(root_fd: int, cwd: str, path: Any, flags: int,
                  mode: int = 0o666) -> int:
    """Ouvre `path` sous le dossier de travail épinglé, sans suivre aucun lien.

    C'est la **frontière de sécurité** du client de fichiers. `root_fd` est le
    descripteur de la racine acquis par `_open_root` et gardé pour le tour ; la
    fonction ne le ferme jamais. Le chemin est contrôlé **syntaxiquement** (un
    composant `..` est refusé avant toute normalisation, un chemin absolu doit
    rester sous la racine), puis chaque composant est ouvert par `dir_fd` +
    `O_NOFOLLOW` : la course entre contrôle et ouverture est impossible, et
    **tout** lien est refusé, même un lien qui pointe à l'intérieur.

    Lève `AcpRpcError` : `-32001` pour un chemin hors dossier, un `..` ou un
    lien, `-32002` pour une ouverture impossible.
    """
    if not isinstance(path, str) or not path:
        raise AcpRpcError(-32602, "chemin attendu")
    racine = os.path.abspath(cwd)
    if os.pardir in path.split(os.sep):
        log("fs refusé : composant `..` (%r, cwd %s)" % (path, cwd))
        raise AcpRpcError(-32001, "chemin hors du dossier de travail de la session : %s"
                          % (path,))
    absolu = path if os.path.isabs(path) else os.path.join(racine, path)
    absolu = os.path.normpath(absolu)
    rel = os.path.relpath(absolu, racine)
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        log("fs refusé hors du dossier de travail (%r, cwd %s)" % (path, cwd))
        raise AcpRpcError(-32001, "chemin hors du dossier de travail de la session : %s"
                          % (path,))
    composants = [c for c in rel.split(os.sep) if c not in ("", os.curdir)]
    if not composants:
        log("fs refusé hors du dossier de travail (%r, cwd %s)" % (path, cwd))
        raise AcpRpcError(-32001, "chemin hors du dossier de travail de la session : %s"
                          % (path,))
    courant = root_fd
    try:
        for composant in composants[:-1]:
            suivant = os.open(composant,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=courant)
            if courant != root_fd:
                os.close(courant)
            courant = suivant
        ouvert = os.open(composants[-1], flags | os.O_NOFOLLOW, mode, dir_fd=courant)
    except OSError as exc:
        if courant != root_fd:
            os.close(courant)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
            log("fs refusé (lien ou composant interdit) : %r (cwd %s)" % (path, cwd))
            raise AcpRpcError(-32001, "lien ou composant interdit dans le dossier de "
                                      "travail : %s" % (path,))
        raise AcpRpcError(-32002, "ouverture impossible (%s) : %s" % (path, exc))
    if courant != root_fd:
        os.close(courant)
    return ouvert


# --------------------------------------------------------------------------
# un tour
# --------------------------------------------------------------------------

class AcpTurn:
    """Un tour ACP complet : initialisation, session, consigne, fin, annulation."""

    def __init__(self, descriptor: HarnessDescriptor, agent_binary: str, *,
                 prompt: str, session: str | None = None, model: str | None = None,
                 effort: str | None = None, tier: str | None = None,
                 cwd: str | None = None, timeout: float = 0.0,
                 init_timeout: float = DEFAULT_INIT_TIMEOUT,
                 idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 cancel_grace: float = CANCEL_GRACE, out=None):
        self.descriptor = descriptor
        self.agent_binary = agent_binary
        self.prompt = prompt
        self.session = session
        self.model = model
        self.effort = effort
        self.tier = tier
        self.cwd = cwd or os.getcwd()
        self.timeout = max(0.0, timeout)
        self.init_timeout = max(0.0, init_timeout)
        self.idle_timeout = max(0.0, idle_timeout)
        self.cancel_grace = max(0.0, cancel_grace)
        self.out = out if out is not None else sys.stdout
        self._out_lock = threading.Lock()
        self._cancel = threading.Event()
        self.peer: JsonRpcPeer | None = None
        #: descripteur épinglé du dossier de travail, ouvert au début du tour et
        #: fermé à la fin : les `fs/*` ne rouvrent jamais la racine par son nom.
        self._root_fd: int | None = None
        self.session_id: str | None = session
        self.config_options: list[dict] = []
        #: capacités annoncées par l'agent (initialize)
        self.load_session = False
        self.resume_session = False
        self.final_text = ""
        self.loading = False
        self.max_used: int | None = None
        self.size: int | None = None
        self._first_cost: float | None = None
        self._last_cost: float | None = None
        self._cost_readings = 0
        self._tools: dict[str, dict] = {}
        self._tool_status: dict[str, str] = {}

    # -- sortie ------------------------------------------------------------
    def emit(self, event: dict) -> None:
        with self._out_lock:
            try:
                self.out.write(json.dumps(event, ensure_ascii=False) + "\n")
                self.out.flush()
            except BrokenPipeError:
                # Lecteur parti (pipe fermé) : on ne veut pas d'une trace de
                # fermeture à l'arrêt ; la suite du tour continue sans bruit.
                try:
                    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                except OSError:
                    pass
            except (OSError, ValueError):
                pass

    def set_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- agent -------------------------------------------------------------
    def _agent_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.descriptor.env.items()})
        return env

    def _start(self) -> None:
        argv = [self.agent_binary, *self.descriptor.launcher, *self.descriptor.command]
        log("lancement de l'agent : %s (cwd %s)" % (" ".join(argv[:4]) + " …", self.cwd))
        try:
            self.peer = JsonRpcPeer(argv, cwd=self.cwd, env=self._agent_env())
        except OSError as exc:
            raise AcpError("agent ACP illisible : %s" % exc)
        self.peer.set_handlers(self._on_request, self._on_notification)

    def _initialize(self) -> None:
        assert self.peer is not None
        result = self.peer.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            # Le client rend la lecture/écriture de fichiers DANS le dossier de
            # travail du tour (chemins résolus, liens refusés au dehors) ; rien
            # pour le terminal ni l'élicitation, qui sont refusés.
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            "clientInfo": {"name": "ameesh", "title": "ameesh", "version": __version__},
        }, timeout=self.init_timeout or None, cancel=self._cancel)
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version != PROTOCOL_VERSION:
            raise AcpError("version ACP %r non supportée (attendu %d)"
                           % (version, PROTOCOL_VERSION))
        capabilities = (result or {}).get("agentCapabilities") or {}
        self.load_session = bool(capabilities.get("loadSession"))
        # La présence de la clé suffit : `{"resume": {}}` annonce la capacité.
        session_caps = capabilities.get("sessionCapabilities") or {}
        self.resume_session = session_caps.get("resume") is not None
        auth_methods = (result or {}).get("authMethods") or []
        auth_method = self.descriptor.acp.get("auth_method")
        if auth_method:
            self.peer.request("authenticate", {"methodId": auth_method},
                              timeout=self.init_timeout or None, cancel=self._cancel)
        elif auth_methods:
            log("l'agent annonce des méthodes d'authentification (%s) mais le descripteur "
                "n'en nomme aucune : on continue avec l'état local"
                % ", ".join(str(m.get("id")) for m in auth_methods if isinstance(m, dict)))

    def _session(self) -> None:
        assert self.peer is not None
        params = {"cwd": self.cwd, "mcpServers": []}
        if self.session:
            if self.resume_session:
                self.peer.request("session/resume", dict(params, sessionId=self.session),
                                  timeout=self.init_timeout or None, cancel=self._cancel)
                self.emit({"type": "session", "sessionId": self.session, "resumed": "resume"})
                return
            if self.load_session:
                # Le rejeu de session/load n'est pas affiché : il est déjà connu.
                self.loading = True
                try:
                    self.peer.request("session/load", dict(params, sessionId=self.session),
                                      timeout=self.init_timeout or None, cancel=self._cancel)
                finally:
                    self.loading = False
                self.emit({"type": "session", "sessionId": self.session, "resumed": "load"})
                return
            # Ni resume ni load : session neuve. Le résumé de reprise (L11) est
            # déjà dans la consigne quand l'exécuteur a tourné une session.
            log("l'agent ne sait pas reprendre la session %s : session neuve" % self.session)
        result = self.peer.request("session/new", params,
                                   timeout=self.init_timeout or None, cancel=self._cancel)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise AcpError("session/new sans sessionId")
        self.session_id = str(session_id)
        self.config_options = list((result or {}).get("configOptions") or [])
        self.emit({"type": "session", "sessionId": self.session_id})

    # -- réglages ----------------------------------------------------------
    def _find_config(self, key: str) -> dict | None:
        mapping = self.descriptor.acp.get("config") or {}
        wanted_id = mapping.get(key)
        if wanted_id:
            for option in self.config_options:
                if isinstance(option, dict) and option.get("id") == wanted_id:
                    return option
            return None
        for category in DEFAULT_CATEGORIES.get(key, ()):
            for option in self.config_options:
                if isinstance(option, dict) and option.get("category") == category:
                    return option
        return None

    def _apply_settings(self) -> None:
        assert self.peer is not None
        for key, value in (("model", self.model), ("effort", self.effort),
                           ("tier", self.tier)):
            if not value:
                continue
            option = self._find_config(key)
            if option is None:
                log("l'agent n'expose pas de réglage %s : valeur %r ignorée" % (key, value))
                continue
            options = _flatten_options(option)
            if option.get("type") != "boolean" and options and str(value) not in options:
                log("valeur %r hors des choix de l'option %s : ignorée"
                    % (value, option.get("id")))
                continue
            try:
                result = self.peer.request("session/set_config_option", {
                    "sessionId": self.session_id, "configId": option.get("id"),
                    "value": value,
                }, timeout=self.init_timeout or None, cancel=self._cancel)
            except AcpError as exc:
                log("réglage %s refusé par l'agent : %s" % (key, exc))
                continue
            if isinstance(result, dict) and isinstance(result.get("configOptions"), list):
                self.config_options = list(result["configOptions"])
                if key == "model":
                    self.emit({"type": "session", "sessionId": self.session_id,
                               "model": str(value)})

    # -- notifications -----------------------------------------------------
    def _on_notification(self, method: str, params: dict) -> None:
        if method != "session/update":
            raise AcpError("notification ACP inattendue : %s" % method)
        self._on_update(params)

    def _on_update(self, params: dict) -> None:
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if self.loading:
            return  # rejeu de session/load : rien à afficher
        if kind == "agent_message_chunk":
            text = _content_text(update)
            if text:
                self.final_text = (self.final_text + text)[-100_000:]
                self.emit({"type": "text", "text": text})
        elif kind == "agent_thought_chunk":
            text = _content_text(update)
            if text:
                self.emit({"type": "thought", "text": text})
        elif kind == "tool_call":
            call_id = str(update.get("toolCallId") or "")
            self._tools[call_id] = dict(update)
            self._tool_status[call_id] = str(update.get("status") or "pending")
            self.emit({"type": "tool", "toolCallId": call_id,
                       "title": update.get("title"), "name": update.get("name"),
                       "kind": update.get("kind"), "status": update.get("status")})
        elif kind == "tool_call_update":
            call_id = str(update.get("toolCallId") or "")
            merged = dict(self._tools.get(call_id) or {})
            merged.update({k: v for k, v in update.items() if v is not None})
            self._tools[call_id] = merged
            status = str(update.get("status") or self._tool_status.get(call_id) or "")
            changement = status != self._tool_status.get(call_id)
            self._tool_status[call_id] = status
            if changement and status in ("completed", "failed", "cancelled"):
                self.emit({"type": "tool", "toolCallId": call_id,
                           "title": merged.get("title"), "name": merged.get("name"),
                           "kind": merged.get("kind"), "status": status})
        elif kind == "usage_update":
            used = update.get("used")
            size = update.get("size")
            if isinstance(used, int) and not isinstance(used, bool):
                self.max_used = used if self.max_used is None else max(self.max_used, used)
            if isinstance(size, int) and not isinstance(size, bool):
                self.size = size
            cost = update.get("cost")
            if isinstance(cost, dict) and isinstance(cost.get("amount"), (int, float)) \
                    and not isinstance(cost.get("amount"), bool):
                amount = float(cost["amount"])
                if self._first_cost is None:
                    self._first_cost = amount
                self._last_cost = amount
                self._cost_readings += 1
        elif kind == "config_option_update":
            options = update.get("configOptions")
            if isinstance(options, list):
                self.config_options = options
                for option in options:
                    if isinstance(option, dict) and option.get("category") == "model" \
                            and option.get("currentValue") is not None:
                        self.emit({"type": "session", "sessionId": self.session_id,
                                   "model": str(option["currentValue"])})
        # plan, available_commands_update, session_info_update, current_mode_update :
        # rien à faire pour un tour d'exécuteur (le fil lisible garde la trace).

    # -- requêtes client ---------------------------------------------------
    def _on_request(self, method: str, params: dict) -> Any:
        if method == "session/request_permission":
            return self._permission(params)
        if method in ("fs/read_text_file", "fs/write_text_file"):
            return self._fs(method, params)
        raise AcpRpcError(-32601, "méthode client non supportée : %s" % method)

    def _permission(self, params: dict) -> dict:
        tool_call = params.get("toolCall")
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
        if self.cancelled:
            return {"outcome": {"outcome": "cancelled"}}
        decision, option_id, reason = permission_decision(
            self.descriptor.permissions, tool_call, options)
        self.emit({"type": "permission", "decision": decision,
                   "toolCallId": tool_call.get("toolCallId"), "title": tool_call.get("title"),
                   "kind": tool_call.get("kind"), "reason": reason})
        log("permission %s (%s) : %s" % (decision, tool_call.get("kind") or "?", reason))
        if option_id is not None:
            return {"outcome": {"outcome": "selected", "optionId": option_id}}
        return {"outcome": {"outcome": "cancelled"}}

    def _fs(self, method: str, params: dict) -> dict:
        """`fs/read_text_file` et `fs/write_text_file`, bornés au dossier du tour.

        L'ouverture passe par `_open_beneath` sur la racine **épinglée** au début
        du tour (`_root_fd`, acquise par `_open_root` : aucun ancêtre n'est
        suivi) puis chaque composant par `dir_fd` + `O_NOFOLLOW` : aucun lien
        n'est suivi, la racine n'est jamais rouverte par son nom, la course
        entre un contrôle de chemin et l'ouverture est impossible, et `..` est
        refusé avant toute normalisation. Toute demande refusée est journalisée
        et rendue en erreur JSON-RPC, jamais exécutée.
        """
        if self._root_fd is None:  # pragma: no cover : acquis avant le premier fs/*
            raise AcpRpcError(-32002, "dossier de travail non épinglé")
        path = params.get("path")
        if method == "fs/read_text_file":
            fd = _open_beneath(self._root_fd, self.cwd, path, os.O_RDONLY)
            try:
                fichier = os.fdopen(fd, encoding="utf-8")
            except (OSError, ValueError) as exc:
                os.close(fd)
                raise AcpRpcError(-32002, "lecture impossible : %s" % exc)
            try:
                with fichier:
                    contenu = fichier.read(MAX_MESSAGE_CHARS + 1)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                log("fs/read_text_file en échec (%r) : %s" % (path, exc))
                raise AcpRpcError(-32002, "lecture impossible : %s" % exc)
            if len(contenu) > MAX_MESSAGE_CHARS:
                raise AcpRpcError(-32002, "fichier trop grand : %s" % path)
            ligne = params.get("line")
            limite = params.get("limit")
            if isinstance(ligne, int) and ligne > 1:
                contenu = "".join(contenu.splitlines(keepends=True)[ligne - 1:])
            if isinstance(limite, int) and limite >= 0:
                contenu = "".join(contenu.splitlines(keepends=True)[:limite])
            return {"content": contenu}
        content = params.get("content")
        if not isinstance(content, str):
            raise AcpRpcError(-32602, "`content` texte attendu")
        if len(content) > MAX_WRITE_CHARS:
            raise AcpRpcError(-32002, "contenu trop grand (%d caractères)" % len(content))
        fd = _open_beneath(self._root_fd, self.cwd, path,
                           os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            fichier = os.fdopen(fd, "w", encoding="utf-8")
        except (OSError, ValueError) as exc:
            os.close(fd)
            raise AcpRpcError(-32002, "écriture impossible : %s" % exc)
        try:
            with fichier:
                fichier.write(content)
        except OSError as exc:
            log("fs/write_text_file en échec (%r) : %s" % (path, exc))
            raise AcpRpcError(-32002, "écriture impossible : %s" % exc)
        log("fs/write_text_file : %r (cwd %s)" % (path, self.cwd))
        return {}

    # -- tour --------------------------------------------------------------
    def _prompt(self) -> str:
        assert self.peer is not None
        pending = self.peer.send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": self.prompt}],
        })
        deadline = time.monotonic() + self.timeout if self.timeout else None
        cancelled = False
        while not pending.event.is_set():
            if self._cancel.is_set() and not cancelled:
                cancelled = True
                log("interruption : session/cancel")
                try:
                    self.peer.notify("session/cancel", {"sessionId": self.session_id})
                except AcpError as exc:
                    log("session/cancel impossible : %s" % exc)
                # Laisse à l'agent le temps de conclure sur `cancelled`.
                pending.event.wait(self.cancel_grace)
                break
            if self.idle_timeout and \
                    time.monotonic() - self.peer.last_message > self.idle_timeout:
                raise AcpError("inactivité de l'agent ACP depuis %ds" % int(self.idle_timeout))
            if deadline is not None and time.monotonic() >= deadline:
                raise AcpError("délai du tour ACP dépassé")
            pending.event.wait(0.1)
            # Une panne de protocole débloque la requête en vol : elle doit
            # arrêter le tour, jamais laisser la boucle attendre sans issue.
            self.peer.raise_fatal()
        if not pending.event.is_set():
            return "cancelled" if cancelled else "error"
        self.peer.raise_fatal()
        result = pending.raise_for_error()
        stop = (result or {}).get("stopReason") if isinstance(result, dict) else None
        return str(stop or "end_turn")

    def run(self) -> int:
        """Déroule le tour ; renvoie le code de sortie du pont."""
        exit_code = 0
        try:
            # La frontière de fichiers est fixée avant de parler à l'agent : le
            # dossier de travail est ouvert une fois, sans suivre de lien, et son
            # descripteur reste épinglé pour tout le tour (codex2).
            self._root_fd = _open_root(self.cwd)
            self._start()
            self._initialize()
            self._session()
            self._apply_settings()
            stop = self._prompt()
        except AcpCancelled:
            stop = "cancelled"
        except (AcpError, harnesses.DescriptorError) as exc:
            self.emit({"type": "error", "message": str(exc)})
            log("échec du tour : %s" % exc)
            self._close()
            return 1
        except Exception as exc:  # jamais de trace brute : un événement lisible
            self.emit({"type": "error", "message": "panne du pont ACP : %s" % exc})
            log("panne du pont ACP : %r" % exc)
            self._close()
            return 1
        usage = None
        if self.max_used is not None:
            usage = {"input_tokens": self.max_used, "output_tokens": 0,
                     "used": self.max_used}
            if self.size is not None:
                usage["size"] = self.size
        cost = None
        if self._last_cost is not None:
            # `usage_update.cost` est **cumulé** sur la session : avec au moins
            # deux relevés, la différence est le coût du tour ; avec un seul, il
            # est la seule mesure disponible (session neuve ou agent avare).
            cost = self._last_cost if self._cost_readings < 2 else max(
                self._last_cost - (self._first_cost or 0.0), 0.0)
        if usage is not None:
            # Le grand livre d'un harnais hors Claude/Codex lit l'usage
            # pas-à-pas (`status`/`step_end`, 0019 §3) : on le lui rend, avec
            # les deux conventions de clés (l'exécuteur compte `input_tokens`
            # pour la rotation de session, le grand livre lit `inputTokens`).
            self.emit({"type": "status", "phase": "step_end", "usage": {
                "inputTokens": usage["input_tokens"], "cacheReadTokens": 0,
                "outputTokens": 0, "input_tokens": usage["input_tokens"],
                "output_tokens": 0,
                **({"used": usage["used"]} if "used" in usage else {}),
                **({"size": usage["size"]} if "size" in usage else {})}})
        self.emit({"type": "final", "stopReason": stop, "text": self.final_text,
                   **({"usage": {k: v for k, v in usage.items()
                                 if k in ("used", "size")}} if usage else {}),
                   **({"costUsd": cost} if cost is not None else {})})
        if stop not in ("end_turn", "cancelled"):
            self.emit({"type": "error", "message": "fin de tour ACP : %s" % stop})
            exit_code = 1
        self._close()
        return exit_code

    def _close(self) -> None:
        if self.peer is not None:
            self.peer.close(grace=self.cancel_grace)
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh.acp", description="Pont ACP générique d'un tour d'exécuteur (L16).")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="un tour : initialize, session, prompt, fin")
    run.add_argument("--descriptor", required=True, help="descripteur du harnais (JSON)")
    run.add_argument("--descriptor-sha256", default=None,
                     help="empreinte attendue du descripteur : le pont refuse un fichier "
                          "modifié depuis la construction de la commande")
    run.add_argument("--agent", required=True, help="binaire de l'agent ACP")
    run.add_argument("--prompt", required=True, help="consigne du tour")
    run.add_argument("--session", default=None, help="session à reprendre")
    run.add_argument("--model", default=None)
    run.add_argument("--effort", default=None)
    run.add_argument("--tier", default=None)
    run.add_argument("--cwd", default=None, help="dossier de session (défaut : courant)")
    run.add_argument("--timeout", type=float, default=0.0,
                     help="délai global du tour en secondes (0 = aucun)")
    run.add_argument("--init-timeout", type=float, default=DEFAULT_INIT_TIMEOUT,
                     help="délai d'initialize et de la session (défaut %g s)"
                     % DEFAULT_INIT_TIMEOUT)
    run.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
                     help="inactivité maximale de l'agent pendant un tour (défaut %g s ; "
                     "0 = aucun)" % DEFAULT_IDLE_TIMEOUT)
    run.add_argument("--cancel-grace", type=float, default=CANCEL_GRACE,
                     help="attente après session/cancel avant SIGKILL (défaut %g s)"
                     % CANCEL_GRACE)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 2
    try:
        descriptor = harnesses.load(args.descriptor)
        log("descripteur %s : %s (%s, sha256 %s)"
            % (descriptor.id, descriptor.path, descriptor.source,
               (descriptor.sha256 or "?")[:16]))
        if args.descriptor_sha256 and descriptor.sha256 != args.descriptor_sha256:
            message = ("descripteur modifié depuis la construction de la commande "
                       "(attendu %s, lu %s)" % (args.descriptor_sha256[:16],
                                                (descriptor.sha256 or "?")[:16]))
            print(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
            log(message)
            return 2
    except harnesses.DescriptorError as exc:
        print(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False))
        log("descripteur %s : %s" % (args.descriptor, exc))
        return 2

    def _stop(_signum, _frame):
        turn.set_cancel()

    turn = AcpTurn(descriptor, args.agent, prompt=args.prompt, session=args.session,
                   model=args.model, effort=args.effort, tier=args.tier, cwd=args.cwd,
                   timeout=args.timeout, init_timeout=args.init_timeout,
                   idle_timeout=args.idle_timeout, cancel_grace=args.cancel_grace)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return turn.run()


if __name__ == "__main__":
    sys.exit(main())
