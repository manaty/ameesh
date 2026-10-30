# SPDX-License-Identifier: AGPL-3.0-only
"""#4602 — priorité d'admission de la connexion base partagée.

Un exécuteur sert tous ses agents sur **une** connexion psycopg, sérialisée
par ``PsycopgDriver._tx_lock`` (L1469) : une instruction d'un fil attend la
fin du bloc d'un autre. Sur un hôte qui tient des dizaines d'agents — un
runtime de rôles en tient soixante-dix —, chaque agent au repos refait sa
passe complète toutes les ``poll`` secondes : renouvellement du bail, lecture
du registre, courrier, lots, budget. La file d'accès à la connexion devient
le chemin critique : un agent **réveillé par un courrier** attend derrière
tous les agents qui dorment, puis derrière les suivants, et le grant de son
tour arrive une minute plus tard alors que la base et l'hôte sont au repos.

Ce module donne deux classes d'admission à ce verrou :

* ``CONTROL`` — le fil qui vient de recevoir un signal de travail (le
  listener de NOTIFY et l'agent qu'il vient de réveiller) ;
* ``NORMAL`` — tout le reste, dont les passes de sondage des agents au repos.

Les files sont FIFO dans chaque classe, avec deux garde-fous :

* **vieillissement** : une place NORMAL qui attend depuis ``AGING_S`` passe
  devant les CONTROL plus récents ; un sondage ne peut donc pas être affamé
  par un flux de réveils ;
* **rafale** : après ``BOOST_BURST`` acquisitions CONTROL consécutives, une
  place NORMAL passe, même sans vieillissement (le verrou reste réentrant et
  ne peut pas être pris par un autre fil tant que son propriétaire le tient).

Le verrou ne change **aucune** règle métier : mêmes transactions, mêmes
verrous SQL, même ordre. Seul l'ordre d'attente des fils change. Il est
désactivable par ``AMEESH_DB_ADMISSION_PRIORITY=0`` (le pilote retombe alors
sur ``threading.RLock``, comportement d'avant).

Un fil qui attend plus de ``WARN_S`` secondes est signalé une fois (observateur
posé par l'exécuteur) : ``[agent] deferred: shared database admission
(queued Xs)``. C'est l'étage « reserve → deliver → harness start » que le
rapport #3513 demande de rendre visible.
"""
from __future__ import annotations

import collections
import contextlib
import os
import threading
import time

#: classe de service : signal de travail reçu, listener, geste de contrôle
CONTROL = 0
#: classe ordinaire : passes de sondage, travail de fond, tours en cours
NORMAL = 1

#: au plus autant d'acquisitions CONTROL consécutives avant de laisser passer
#: une place NORMAL (le verrou reste réentrant : ce compteur ne s'applique
#: qu'à l'acquisition initiale d'un fil, jamais aux ré-entrées)
BOOST_BURST = 8

#: âge au-delà duquel une place NORMAL passe devant les CONTROL plus récents
AGING_S = 2.0

#: attente au-delà de laquelle l'observateur est appelé
WARN_S = 1.0

#: seuil de journalisation réglable sans redémarrage logique (diagnostic seul)
def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 and value == value else default


AGING_S = _env_seconds("AMEESH_DB_AGING_S", AGING_S)
WARN_S = _env_seconds("AMEESH_DB_WAIT_WARN_S", WARN_S)

#: priorité du fil courant ; un fil qui n'a rien déclaré est NORMAL
_local = threading.local()

#: observateur d'attente longue, posé par l'exécuteur
_observer = None
_observer_lock = threading.Lock()


def enabled() -> bool:
    """Le pilote psycopg doit-il utiliser le verrou prioritaire ?

    Activé par défaut ; ``AMEESH_DB_ADMISSION_PRIORITY=0|off|false|no``
    restaure le ``threading.RLock`` d'avant (bascule de secours, pas un
    réglage de performance)."""
    return (os.environ.get("AMEESH_DB_ADMISSION_PRIORITY") or "1").strip().lower() \
        not in ("0", "off", "false", "no")


def set_priority(value: int) -> None:
    """Déclare la classe du fil courant (``CONTROL`` ou ``NORMAL``)."""
    _local.priority = CONTROL if int(value) <= CONTROL else NORMAL


def priority() -> int:
    """La classe du fil courant (``NORMAL`` par défaut)."""
    return getattr(_local, "priority", NORMAL)


def reset() -> None:
    """Rend le fil courant à la classe ordinaire."""
    _local.priority = NORMAL


@contextlib.contextmanager
def priority_for(value: int):
    """Classe ``value`` pour la durée du bloc, puis classe précédente."""
    previous = priority()
    set_priority(value)
    try:
        yield
    finally:
        set_priority(previous)


def set_wait_observer(observer) -> None:
    """Pose l'observateur ``observer(thread_name, waited_s)`` des attentes
    longues ; ``None`` le retire. Il est appelé hors verrou de condition et
    doit être non bloquant (l'exécuteur y met un ``log_async``)."""
    global _observer
    with _observer_lock:
        _observer = observer


def _notify(waited: float) -> None:
    observer = _observer
    if observer is None or waited < WARN_S:
        return
    try:
        observer(threading.current_thread().name, waited)
    except Exception:  # un diagnostic ne casse jamais l'admission
        pass


class _Ticket:
    __slots__ = ("priority", "since", "ident", "granted", "wakeup")

    def __init__(self, priority: int, since: float, ident: int,
                 wakeup: threading.Condition | None = None):
        self.priority = priority
        self.since = since
        self.ident = ident
        self.granted = False
        #: #4515 : condition propre au ticket, sur le verrou du PriorityRLock
        self.wakeup = wakeup


class PriorityRLock:
    """Verrou réentrant à deux classes, FIFO par classe, avec vieillissement.

    La **passation** est faite une seule fois, par ``release()``, sous la
    condition : il choisit un ticket, le marque ``granted`` et pose son
    propriétaire, puis réveille les attentes. Une attente ne recalcule jamais
    la sélection à son réveil : deux attentes qui évalueraient ``_next`` à des
    instants différents pourraient chacune choisir l'autre et s'endormir
    ensemble, laissant le verrou libre sans personne pour le notifier (#4602,
    B1 de la relecture dev1).

    #4515 : la passation ne réveille que le fil choisi (une condition par
    ticket, sur le même verrou). Un ``notify_all`` réveillait toutes les
    attentes à chaque passation : avec vingt agents au repos, chaque fil
    était réveillé ~200 fois/s pour se rendormir aussitôt, et se disputait
    le GIL pour le faire.

    L'API est celle de ``threading.RLock`` pour les usages du pilote
    (``with``, ``acquire``, ``release``, ``locked``). ``clock`` est injectable
    par les tests de vieillissement.
    """

    def __init__(self, clock=None):
        self._mutex = threading.Lock()
        self._condition = threading.Condition(self._mutex)
        self._owner: int | None = None
        self._depth = 0
        self._queues = (collections.deque(), collections.deque())
        self._burst = 0
        self._clock = clock or time.monotonic

    # -- état (diagnostic et tests) ---------------------------------------
    def locked(self) -> bool:
        with self._condition:
            return self._owner is not None

    def _is_owned(self) -> bool:
        with self._condition:
            return self._owner == threading.get_ident()

    def queued(self) -> tuple[int, int]:
        """``(control, normal)`` en attente ; lecture de diagnostic."""
        with self._condition:
            return len(self._queues[0]), len(self._queues[1])

    # -- sélection --------------------------------------------------------
    def _next(self, now: float) -> _Ticket | None:
        control, normal = self._queues
        aged = bool(normal) and (now - normal[0].since) >= AGING_S
        if control and not aged:
            if normal and self._burst >= BOOST_BURST:
                return normal[0]
            return control[0]
        if aged:
            if control and control[0].since < normal[0].since:
                return control[0]
            return normal[0]
        return normal[0] if normal else None

    def _grant_next(self, now: float) -> None:
        """Choisit UN ticket et le passe propriétaire ; sous la condition."""
        ticket = self._next(now)
        if ticket is None:
            self._owner = None
            return
        self._queues[ticket.priority].remove(ticket)
        ticket.granted = True
        self._owner = ticket.ident
        self._depth = 1
        self._burst = self._burst + 1 if ticket.priority == CONTROL else 0
        ticket.wakeup.notify()

    # -- acquisition ------------------------------------------------------
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        ident = threading.get_ident()
        want = priority()
        with self._condition:
            if self._owner == ident:
                self._depth += 1
                return True
            if self._owner is None and not self._queues[0] and not self._queues[1]:
                # verrou libre, personne n'attend : acquisition immédiate
                self._owner, self._depth = ident, 1
                self._burst = self._burst + 1 if want == CONTROL else 0
                return True
            if not blocking:
                return False
            ticket = _Ticket(want, self._clock(), ident,
                             threading.Condition(self._mutex))
            self._queues[want].append(ticket)
            if self._owner is None:
                # ne jamais laisser un verrou libre avec des attentes : la
                # passation part immédiatement (une attente plus ancienne
                # peut être choisie, la nôtre attendra son tour)
                self._grant_next(self._clock())
            deadline = None if timeout is None or timeout < 0 \
                else time.monotonic() + float(timeout)
            waited = 0.0
            try:
                while not ticket.granted:
                    if deadline is None:
                        ticket.wakeup.wait()
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if ticket.granted:
                            break  # la passation a gagné la course
                        # un ticket non choisi ne change ni le propriétaire
                        # ni le choix suivant : personne à réveiller
                        self._queues[want].remove(ticket)
                        return False
                    ticket.wakeup.wait(remaining)
                waited = self._clock() - ticket.since
            except BaseException:
                if ticket.granted:
                    # passation reçue mais acquisition interrompue : la rendre
                    self._depth -= 1
                    if not self._depth:
                        self._grant_next(self._clock())
                else:
                    try:
                        self._queues[want].remove(ticket)
                    except ValueError:
                        pass
                raise
        _notify(waited)
        return True

    def release(self) -> None:
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError("cannot release an un-acquired lock")
            self._depth -= 1
            if self._depth:
                return
            self._owner = None
            self._grant_next(self._clock())

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
