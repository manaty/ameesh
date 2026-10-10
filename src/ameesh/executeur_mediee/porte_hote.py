# SPDX-License-Identifier: AGPL-3.0-only
"""Porte d'hôte, côté exécuteur (L112) : `FileGate`, `SocketGate` et le
contrôleur qui fait obéir l'exécuteur à l'état d'inactivité de l'hôte.

Un appareil prêté (VM Nexlink Compute) ne doit travailler que lorsque sa
machine est inutilisée. Le runner Compute écrit l'état (`ameesh-host-state/1`,
`porte.py`) ; l'exécuteur :

* `available` : réclame normalement, dans la limite `caps.max_concurrent` ;
* `draining` : ne réclame plus ; chaque tour en cours s'arrête au prochain
  **point sûr** (fin de l'appel d'outil en cours, `safe_point` des lecteurs de
  flux), au plus tard à l'échéance (`drain_deadline_ts`, sinon maintenant +
  `host_gate_drain`, 90 s) ; la consigne repart en attente, la session est
  gardée, le bail est rendu ; puis acquittement `drained: true` ;
* `stopped` : aucun tour — le tour en cours est arrêté tout de suite, même
  suite que ci-dessus (écritures faites au mieux : si le réseau est déjà
  coupé, le bail échoit seul) ;
* retour à `available` : reprise sans geste humain (la passe suivante
  réclame de nouveau).

État illisible ou absent : `stopped` pour un hôte médié, `available` pour un
hôte classique (`fallback`) ; rien ne change pour un hôte sans porte
configurée (`from_config` rend None).

Configuration (fichier de l'hôte ou environnement, `docs/EXPLOITATION.md`) :
`host_gate` (`file` | `socket`), `host_gate_path`, `host_gate_ack`,
`host_gate_fallback`, `host_mediated`, `host_gate_drain`.
"""
from __future__ import annotations

import abc
import ctypes
import ctypes.util
import dataclasses
import json
import os
import select
import socket
import threading
import time
from typing import Any, Callable, Optional

from .porte import (ACK_FILE, DEFAULT_DRAIN_S, FILE_POLL_S, SOCKET_PATH, STATE_FILE,
                    STATES, GateAck, GateState, HostGate, availability_body)

GATE_KINDS = ("file", "socket")
#: bail d'un hôte volatil (fiche Host `policy.volatile`), en secondes
VOLATILE_LEASE_TTL_S = 90.0
#: au-delà de l'échéance du retrait, délai avant de dire le retrait en retard
OVERDUE_GRACE_S = 30.0
#: connexion à la socket perdue : délai avant de la dire illisible
SOCKET_LOST_GRACE_S = FILE_POLL_S
#: taille maximale lue d'un état (un fichier énorme est illisible)
MAX_STATE_BYTES = 65536


def _number_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def parse_state(raw: Any) -> GateState:
    """`GateState.from_json`, plus les contrôles qu'il ne fait pas : instants
    numériques (ou nuls), `caps.max_concurrent` entier positif. `ValueError`
    sinon : l'appelant applique son état de repli."""
    state = GateState.from_json(raw)
    if not (_number_or_none(state.until_ts) and _number_or_none(state.drain_deadline_ts)):
        raise ValueError("until_ts, drain_deadline_ts : nombre ou null attendu")
    cap = state.caps.get("max_concurrent")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0):
        raise ValueError("caps.max_concurrent : entier positif attendu")
    return state


def fallback_state(fallback: str, seq: int = -1) -> GateState:
    """L'état retenu quand la porte est illisible ou absente."""
    return GateState(state=fallback, seq=seq, reason="unknown")


def _key(state: GateState) -> tuple:
    return (state.state, state.seq, state.reason, state.drain_deadline_ts,
            tuple(sorted(state.caps.items())))


def _atomic_write(path: str, data: dict) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# inotify (ctypes) : facultatif, sondage sinon
# --------------------------------------------------------------------------

_IN_EVENTS = (0x2 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)


class _Inotify:
    """Surveillance d'un dossier ; `None` à la construction si indisponible."""

    def __init__(self, directory: str):
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        wd = libc.inotify_add_watch(fd, os.fsencode(directory), _IN_EVENTS)
        if wd < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, "inotify_add_watch %s" % directory)
        self.fd = fd

    @classmethod
    def maybe(cls, directory: str) -> "Optional[_Inotify]":
        try:
            return cls(directory)
        except (OSError, AttributeError):
            return None

    def drain(self) -> None:
        while True:
            try:
                if not os.read(self.fd, 4096):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class _GateBase(HostGate, abc.ABC):
    """Socle commun : état de repli, dernier état rendu, réveil à la fermeture."""

    def __init__(self, *, fallback: str, log: Callable[[str], None] | None = None):
        if fallback not in STATES:
            raise ValueError("état de repli inconnu : %r" % fallback)
        self.fallback = fallback
        self._log = log or (lambda _m: None)
        self._closed = threading.Event()
        self._rendered: tuple | None = None
        self._last_seq = -1
        self._last_error = ""

    def _note_error(self, message: str) -> None:
        if message != self._last_error:
            self._last_error = message
            self._log("porte d'hôte : %s — état retenu : %s" % (message, self.fallback))

    def _fallback(self) -> GateState:
        return fallback_state(self.fallback, self._last_seq)

    def _accept(self, state: GateState) -> GateState:
        self._last_seq = state.seq
        if self._last_error:
            self._log("porte d'hôte de nouveau lisible : %s (seq %d)" % (state.state, state.seq))
        self._last_error = ""
        return state


class FileGate(_GateBase):
    """L'état dans un fichier (`state.json`), écrit par le runner Compute par
    fichier temporaire puis `rename` ; relu sur inotify, sinon toutes les
    `poll` secondes ; acquittement écrit de la même façon (`ack.json`)."""

    def __init__(self, path: str = STATE_FILE, ack_path: str | None = None, *,
                 fallback: str = "stopped", poll: float = FILE_POLL_S,
                 use_inotify: bool = True, log: Callable[[str], None] | None = None):
        super().__init__(fallback=fallback, log=log)
        self.path = path
        self.ack_path = ack_path or os.path.join(os.path.dirname(path) or ".",
                                                 os.path.basename(ACK_FILE))
        self.poll = max(0.05, float(poll))
        self._use_inotify = use_inotify
        self._inotify: _Inotify | None = None
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)

    # -- lecture ---------------------------------------------------------
    def _read(self) -> GateState:
        try:
            with open(self.path, "rb") as fh:
                data = fh.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            self._note_error("état absent (%s)" % self.path)
            return self._fallback()
        except OSError as exc:
            self._note_error("état illisible (%s)" % exc)
            return self._fallback()
        try:
            if len(data) > MAX_STATE_BYTES:
                raise ValueError("état trop grand")
            return self._accept(parse_state(json.loads(data.decode("utf-8"))))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self._note_error("état illisible (%s)" % exc)
            return self._fallback()

    def state(self) -> GateState:
        return self._read()

    def _watch(self) -> None:
        if self._use_inotify and self._inotify is None:
            directory = os.path.dirname(self.path) or "."
            if os.path.isdir(directory):
                self._inotify = _Inotify.maybe(directory)

    def _sleep(self, seconds: float) -> None:
        self._watch()
        fds = [self._wake_r] + ([self._inotify.fd] if self._inotify else [])
        try:
            ready, _w, _x = select.select(fds, [], [], max(0.0, seconds))
        except (OSError, ValueError):
            self._closed.wait(seconds)
            return
        if self._inotify is not None and self._inotify.fd in ready:
            self._inotify.drain()
            # le runner écrit puis renomme : on laisse le rename aboutir
            time.sleep(0.01)

    def wait_change(self, timeout: float) -> GateState:
        fin = time.monotonic() + max(0.0, timeout)
        while True:
            current = self._read()
            if _key(current) != self._rendered:
                self._rendered = _key(current)
                return current
            reste = fin - time.monotonic()
            if reste <= 0 or self._closed.is_set():
                return current
            self._sleep(min(reste, self.poll))

    def acknowledge(self, ack: GateAck) -> None:
        body = dataclasses.replace(ack, ts=ack.ts or time.time()).to_json()
        try:
            _atomic_write(self.ack_path, body)
        except OSError as exc:
            self._log("porte d'hôte : acquittement non écrit (%s)" % exc)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None


class SocketGate(_GateBase):
    """L'état par une socket Unix (`gate.sock`) ouverte par le runner Compute :
    l'exécuteur s'y connecte, lit un état JSON par ligne et répond par un
    acquittement par ligne. Connexion perdue : nouvel essai toutes les
    0,5 s ; au-delà de `lost_grace` secondes sans connexion (et avant la
    première ligne), l'état vaut `fallback`."""

    def __init__(self, path: str = SOCKET_PATH, *, fallback: str = "stopped",
                 lost_grace: float = SOCKET_LOST_GRACE_S, retry: float = 0.5,
                 log: Callable[[str], None] | None = None):
        super().__init__(fallback=fallback, log=log)
        self.path = path
        self.lost_grace = max(0.0, float(lost_grace))
        self.retry = max(0.05, float(retry))
        self._cond = threading.Condition()
        self._state: GateState | None = None
        self._connected = False
        self._live = False
        self._lost_at = time.monotonic()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._pending: bytes | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="porte-socket")
        self._thread.start()

    def _current(self) -> GateState:
        with self._cond:
            # `_live` : une ligne reçue sur la connexion courante ; une
            # reconnexion sans ligne ne prolonge pas l'ancien état
            if self._state is not None and (
                    self._live or time.monotonic() - self._lost_at < self.lost_grace):
                return self._state
            return self._fallback()

    def state(self) -> GateState:
        return self._current()

    def _set(self, state: GateState | None) -> None:
        with self._cond:
            self._state = state
            self._live = True
            self._cond.notify_all()

    def _loop(self) -> None:
        while not self._closed.is_set():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.path)
            except OSError as exc:
                sock.close()
                self._note_error("socket injoignable (%s)" % exc)
                with self._cond:
                    self._cond.notify_all()
                self._closed.wait(self.retry)
                continue
            with self._cond:
                self._sock = sock
                self._connected = True
            with self._send_lock:
                pending, self._pending = self._pending, None
            if pending:
                self._send(pending)
            try:
                for line in sock.makefile("rb"):
                    if self._closed.is_set():
                        break
                    if not line.strip():
                        continue
                    try:
                        state = self._accept(parse_state(json.loads(line.decode("utf-8"))))
                    except (ValueError, TypeError, UnicodeDecodeError) as exc:
                        self._note_error("état illisible (%s)" % exc)
                        state = self._fallback()
                    self._set(state)
            except OSError as exc:
                self._note_error("socket coupée (%s)" % exc)
            finally:
                with self._cond:
                    self._sock = None
                    self._connected = False
                    if self._live:
                        self._lost_at = time.monotonic()
                    self._live = False
                    self._cond.notify_all()
                try:
                    sock.close()
                except OSError:
                    pass
            if not self._closed.is_set():
                self._note_error("socket fermée par le runner")
                self._closed.wait(self.retry)

    def wait_change(self, timeout: float) -> GateState:
        fin = time.monotonic() + max(0.0, timeout)
        while True:
            current = self._current()
            if _key(current) != self._rendered:
                self._rendered = _key(current)
                return current
            reste = fin - time.monotonic()
            if reste <= 0 or self._closed.is_set():
                return current
            with self._cond:
                # réveil borné : l'échéance de `lost_grace` n'émet aucun signal
                self._cond.wait(min(reste, 0.25))

    def _send(self, data: bytes) -> bool:
        with self._cond:
            sock = self._sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                sock.sendall(data)
            return True
        except OSError as exc:
            self._log("porte d'hôte : acquittement non envoyé (%s)" % exc)
            return False

    def acknowledge(self, ack: GateAck) -> None:
        body = dataclasses.replace(ack, ts=ack.ts or time.time()).to_json()
        data = (json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if not self._send(data):
            with self._send_lock:
                self._pending = data  # le dernier seulement, envoyé à la reconnexion

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._cond:
            sock = self._sock
            self._cond.notify_all()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def is_mediated(cfg) -> bool:
    """Hôte médié : déclaré (`host_mediated`), ou exécuteur sans base (URL
    http(s) du serveur du mesh à la place du DSN, L109)."""
    if bool(getattr(cfg, "host_mediated", False)):
        return True
    if str(getattr(cfg, "backend", "") or "") == "mediated":
        return True  # L109 : `backend: mediated`
    dsn = str(getattr(cfg, "dsn", "") or "")
    return dsn.startswith(("http://", "https://"))


def default_fallback(cfg) -> str:
    choisi = str(getattr(cfg, "host_gate_fallback", "") or "").strip()
    if choisi:
        return choisi
    return "stopped" if is_mediated(cfg) else "available"


def from_config(cfg, *, log: Callable[[str], None] | None = None) -> Optional[HostGate]:
    """La porte configurée, ou None (hôte classique sans porte : rien ne
    change, `AlwaysAvailable` implicite)."""
    kind = str(getattr(cfg, "host_gate", "") or "").strip().lower()
    if kind in ("", "none", "aucune"):
        return None
    fallback = default_fallback(cfg)
    path = str(getattr(cfg, "host_gate_path", "") or "")
    if kind == "file":
        return FileGate(path or STATE_FILE, getattr(cfg, "host_gate_ack", "") or None,
                        fallback=fallback, log=log)
    if kind == "socket":
        return SocketGate(path or SOCKET_PATH, fallback=fallback, log=log)
    raise ValueError("host_gate : %r inconnu (%s)" % (kind, " | ".join(GATE_KINDS)))


# --------------------------------------------------------------------------
# bail d'un hôte volatil
# --------------------------------------------------------------------------

def lease_ttl_for(canons, host: str, default: float) -> float:
    """Durée du bail des agents de `host` : `policy.lease_ttl` de sa fiche
    Host (le plus court de tous les canons), sinon 90 s si une fiche le dit
    volatil (`policy.volatile`), sinon `default` (configuration de l'hôte).
    Le serveur (L108, `GET /host`) applique la même règle."""
    declared: list[float] = []
    volatile = False
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        policy = getattr(fiche, "policy", None)
        if policy is None:
            continue
        if getattr(policy, "lease_ttl", None):
            declared.append(float(policy.lease_ttl))
        volatile = volatile or bool(getattr(policy, "volatile", False))
    if declared:
        return min(declared)
    if volatile:
        return VOLATILE_LEASE_TTL_S
    return float(default)


# --------------------------------------------------------------------------
# rapport de disponibilité (vers le serveur)
# --------------------------------------------------------------------------

class AvailabilitySink(abc.ABC):
    """Où l'exécuteur relaie l'état de sa porte. L109 branche
    `TransportSink` (`PUT /host/availability`) ; un hôte classique écrit le
    registre local (`disponibilite.FileRegistry`)."""

    @abc.abstractmethod
    def report(self, state: GateState, ack: GateAck | None) -> None:
        """Appelé à chaque changement d'état et d'acquittement ; ne lève pas."""


class TransportSink(AvailabilitySink):
    """Vers le serveur, par le transport de L109 : le corps figé
    `ameesh-exec-availability/1`, une fois par changement d'état."""

    #: nouvel essai d'un rapport en échec (secondes)
    RETRY_S = 5.0

    def __init__(self, transport, log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self._log = log or (lambda _m: None)
        self._clock = clock
        self._sent: tuple | None = None
        self._failed_at: float | None = None
        self._failures = 0

    def pending(self, state: GateState) -> bool:
        """Un rapport reste-t-il à envoyer (état non reçu par le serveur, et
        dernier échec assez ancien) ? Le contrôleur rappelle `report`."""
        if _key(state) == self._sent:
            return False
        return self._failed_at is None or self._clock() - self._failed_at >= self.RETRY_S

    def report(self, state: GateState, ack: GateAck | None) -> None:
        if _key(state) == self._sent:
            return
        try:
            self.transport.put_availability(state)
            self._sent = _key(state)
            self._failed_at, self._failures = None, 0
        except Exception as exc:  # serveur injoignable : renvoyé toutes les RETRY_S
            self._failed_at = self._clock()
            self._failures += 1
            if self._failures in (1, 10) or self._failures % 100 == 0:
                self._log("disponibilité non relayée au serveur (%s), essai %d"
                          % (exc, self._failures))


class RegistrySink(AvailabilitySink):
    """Vers un registre de disponibilité (`disponibilite`), avec l'acquittement."""

    def __init__(self, registry, host: str, executor_id: str,
                 log: Callable[[str], None] | None = None):
        self.registry = registry
        self.host = host
        self.executor_id = executor_id
        self._log = log or (lambda _m: None)

    def report(self, state: GateState, ack: GateAck | None) -> None:
        try:
            self.registry.put(self.host, availability_body(state),
                              executor_id=self.executor_id, gate=state.to_json(),
                              ack=ack.to_json() if ack is not None else None)
        except Exception as exc:
            self._log("disponibilité non enregistrée (%s)" % exc)


# --------------------------------------------------------------------------
# contrôleur (dans l'exécuteur)
# --------------------------------------------------------------------------

class GateController:
    """Fait obéir l'exécuteur à sa porte. Lu par `Runner.sweep` (réclamer ?),
    par chaque worker (nouveau tour ? point sûr ?) ; son fil applique les
    transitions, arrête les tours à l'échéance et acquitte."""

    TICK = 0.25

    def __init__(self, runner, gate: HostGate, *, drain_s: float = DEFAULT_DRAIN_S,
                 sink: AvailabilitySink | None = None,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.runner = runner
        self.gate = gate
        self.drain_s = max(0.0, float(drain_s))
        self.sink = sink
        self._log = log or (lambda _m: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: GateState = gate.state()
        self._applied: tuple | None = None
        self.deadline: float | None = None
        self._interrupted = False
        self._overdue = False
        self._ack_key: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.acks: list[GateAck] = []  # derniers acquittements (essais, `show`)
        #: L114b : retrait demandé par SIGTERM (état forcé, la porte ne peut
        #: que l'aggraver en `stopped`)
        self._terminating: GateState | None = None

    # -- lectures (tous fils) ---------------------------------------------
    @property
    def current(self) -> GateState:
        with self._lock:
            if self._applied is None:
                # rien d'appliqué encore (mode `--once`) : l'état du moment
                self._state = self.gate.state()
            return self._state

    def may_claim(self) -> bool:
        return self.current.may_claim

    def holds_turns(self) -> bool:
        """Aucun nouveau tour (draining, stopped)."""
        return not self.current.may_claim

    def max_concurrent(self) -> int:
        """`caps.max_concurrent` de la porte, sinon `DEFAULT_MAX_CONCURRENT`
        (contrat 1.1)."""
        return self.current.max_concurrent

    def describe(self) -> str:
        st = self.current
        return "%s (%s, seq %d)" % (st.state, st.reason, st.seq)

    # -- boucle ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.apply(self.gate.state())
        self._thread = threading.Thread(target=self._loop, daemon=True, name="porte-hote")
        self._thread.start()

    def terminate(self, drain_s: float | None = None) -> GateState:
        """L114b : SIGTERM reçu par un exécuteur médié. Retrait local, comme
        un `draining` de la porte : plus de réclamation, chaque tour finit au
        point sûr, préemption à l'échéance (`drain_s`, défaut celui du
        contrôleur, 90 s), puis baux rendus. La porte ne peut plus rouvrir
        la réclamation ; un `stopped` de sa part reste appliqué."""
        now = self._clock()
        current = self.current
        delay = self.drain_s if drain_s is None else max(0.0, float(drain_s))
        forced = GateState(state="draining", seq=current.seq, until_ts=None,
                           drain_deadline_ts=now + delay, caps=dict(current.caps),
                           reason="sigterm")
        self._terminating = forced
        if current.state != "stopped":
            self.apply(forced)
        return forced

    @property
    def terminating(self) -> bool:
        return self._terminating is not None

    def _effective(self, state: GateState) -> GateState:
        forced = self._terminating
        if forced is None or state.state == "stopped":
            return state
        return forced

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.gate.wait_change(self.TICK)
                if self._stop.is_set():
                    return
                self.apply(self._effective(state))
                self.tick()
            except Exception as exc:  # la porte ne tue jamais l'exécuteur
                self._log("porte d'hôte : erreur du contrôleur (%s)" % exc)
                self._stop.wait(self.TICK)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.gate.close()
        finally:
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=2.0)

    def _workers(self) -> list:
        lock = getattr(self.runner, "lock", None)
        workers = getattr(self.runner, "workers", {}) or {}
        if lock is None:
            return list(workers.values())
        with lock:
            return list(workers.values())

    # -- transitions -------------------------------------------------------
    def apply(self, state: GateState) -> bool:
        """Applique un état ; rend True s'il a changé."""
        now = self._clock()
        with self._lock:
            if _key(state) == self._applied:
                return False
            previous = self._state if self._applied is not None else None
            self._applied = _key(state)
            self._state = state
            if state.may_claim:
                self.deadline = None
                self._interrupted = self._overdue = False
            else:
                if state.state == "stopped":
                    deadline = now
                elif isinstance(state.drain_deadline_ts, (int, float)):
                    deadline = float(state.drain_deadline_ts)
                else:
                    deadline = now + self.drain_s
                if previous is not None and not previous.may_claim and self.deadline is not None:
                    deadline = min(deadline, self.deadline)  # jamais repoussée
                else:
                    self._interrupted = self._overdue = False
                self.deadline = deadline
            self._ack_key = None  # nouvel acquittement à publier
        self._announce(previous, state, now)
        if state.may_claim:
            wake = getattr(self.runner, "wake_all", None)
            if wake is not None:
                wake.set()  # reprise immédiate, sans attendre la passe suivante
        else:
            for worker in self._workers():
                worker.wake.set()  # un worker au repos rend son bail tout de suite
        self._report(None)
        return True

    def _announce(self, previous: GateState | None, state: GateState, now: float) -> None:
        avant = previous.state if previous is not None else "démarrage"
        if state.may_claim:
            self._log("porte d'hôte : %s → available (%s, seq %d) : réclamation ouverte"
                      % (avant, state.reason, state.seq))
        elif state.state == "draining":
            self._log("porte d'hôte : %s → draining (%s, seq %d) : plus de réclamation, "
                      "tours arrêtés au point sûr, au plus tard dans %ds"
                      % (avant, state.reason, state.seq,
                         max(0, int((self.deadline or now) - now))))
        else:
            self._log("porte d'hôte : %s → stopped (%s, seq %d) : aucun tour"
                      % (avant, state.reason, state.seq))

    def _report(self, ack: GateAck | None) -> None:
        if self.sink is None:
            return
        try:
            self.sink.report(self.current, ack)
        except Exception as exc:
            self._log("porte d'hôte : rapport impossible (%s)" % exc)

    def tick(self) -> GateAck:
        """Échéance du retrait, retard, acquittement ; rend l'acquittement."""
        now = self._clock()
        state = self.current
        workers = [w for w in self._workers() if w.is_alive()]
        if not state.may_claim and self.deadline is not None and now >= self.deadline:
            # à chaque passage : un tour publié juste après l'échéance est
            # rattrapé au suivant
            for worker in workers:
                if getattr(worker, "proc", None) is not None \
                        and getattr(worker, "host_yield", None) is None:
                    self._interrupted = True
                    worker.yield_to_host(
                        "échéance du retrait de l'hôte" if state.state == "draining"
                        else "hôte arrêté")
        in_turn = sorted(w.name for w in workers if getattr(w, "proc", None) is not None)
        held = sorted(w.name for w in workers)
        drained = (not state.may_claim) and not held
        if not state.may_claim and held and self.deadline is not None \
                and now >= self.deadline + OVERDUE_GRACE_S and not self._overdue:
            self._overdue = True
            self._log("porte d'hôte : retrait en retard de %ds, baux encore détenus : %s"
                      % (int(now - self.deadline), ", ".join(held)))
        pending = getattr(self.sink, "pending", None)
        if pending is not None and pending(state):
            self._report(None)  # rapport au serveur encore en échec : nouvel essai
        ack = GateAck(seq=state.seq, state=state.state, in_turn=tuple(in_turn),
                      held=tuple(held), drained=drained, ts=now)
        key = (ack.seq, ack.state, ack.in_turn, ack.held, ack.drained)
        if key != self._ack_key:
            self._ack_key = key
            self.gate.acknowledge(ack)
            self.acks = (self.acks + [ack])[-20:]
            if drained:
                self._log("porte d'hôte : retrait terminé (seq %d), aucun bail détenu"
                          % state.seq)
            self._report(ack)
        return ack


def controller_from_config(runner, *, log: Callable[[str], None] | None = None,
                           sink: AvailabilitySink | None = None) -> Optional[GateController]:
    """Le contrôleur de l'exécuteur, ou None sans porte configurée."""
    cfg = runner.cfg
    gate = from_config(cfg, log=log)
    if gate is None:
        return None
    if sink is None and not is_mediated(cfg):
        from . import disponibilite
        sink = RegistrySink(disponibilite.FileRegistry(disponibilite.default_path(cfg)),
                            cfg.host, runner.runner_id, log=log)
    return GateController(runner, gate, drain_s=getattr(cfg, "host_gate_drain", DEFAULT_DRAIN_S),
                          sink=sink, log=log)
