# SPDX-License-Identifier: AGPL-3.0-only
"""Flux d'événements du serveur de l'exécuteur médié (L108).

Le serveur tient UNE écoute `LISTEN` sur les trois canaux de
`evenements.CHANNELS` (`wakeups.subscribe` du pilote Postgres, sur une
connexion dédiée). Chaque NOTIFY reçu entre dans un tampon circulaire de
`RING_SIZE` événements, numérotés dans un flux propre au processus
(`<flux>:<n>`, `evenements.cursor`). Les lecteurs (SSE ou attente longue)
lisent ce tampon après leur curseur, filtré par hôte :

* `agent_mail` : si `to` est un agent admis sur l'hôte de l'exécuteur ;
* `agent_lease` : si l'agent (`name`) est admis. Le déclencheur de 0001
  écrit `agent` ; le serveur le renomme `name` à la réception (contrat
  1.1, `wire_payload`), seule forme qui sort sur le fil ;
* `ameesh_budget` et `reset` : à tous.

Un curseur d'un autre flux, mal formé, ou sorti du tampon donne un seul
événement `reset` (le client relit tout en base). Une coupure de l'écoute
publie aussi un `reset` à la reprise : des NOTIFY ont pu se perdre.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Iterable, Optional

from .evenements import CHANNELS, RESET, RING_SIZE, Event, cursor, parse_cursor

log = logging.getLogger("ameesh.exec")

#: attente avant de rouvrir une écoute tombée (secondes)
RELISTEN_S = 2.0


def visible(event: Event, admitted: Iterable[str]) -> bool:
    """L'événement peut-il être montré à un exécuteur dont les agents admis
    sont `admitted` ?"""
    if event.channel in (RESET, "ameesh_budget"):
        return True
    data = event.data or {}
    if event.channel == "agent_mail":
        who = data.get("to")
    elif event.channel == "agent_lease":
        who = data.get("name")
    else:
        return False
    return isinstance(who, str) and who in admitted


class EventHub:
    """Tampon circulaire alimenté par LISTEN/NOTIFY.

    `listen` : fabrique d'abonnement (`wakeups.subscribe(CHANNELS)` sur une
    connexion dédiée) ; elle peut rendre None (pilote sans écoute) : le hub
    réessaie, et les exécuteurs gardent leur sondage."""

    def __init__(self, listen: Optional[Callable[[], Any]] = None, *,
                 ring_size: int = RING_SIZE, stream: Optional[str] = None):
        self.stream = stream or os.urandom(2).hex()
        self._listen = listen
        self._ring: collections.deque = collections.deque(maxlen=ring_size)
        self._n = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.listening = False

    # -- cycle de vie ---------------------------------------------------------
    def start(self) -> None:
        if self._listen is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ameesh-exec-listen",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        dropped = False
        while not self._stop.is_set():
            sub = None
            try:
                sub = self._listen()
            except Exception as exc:
                log.warning("écoute LISTEN impossible : %s", str(exc)[:200])
            if sub is None:
                self.listening = False
                self._stop.wait(RELISTEN_S)
                dropped = True
                continue
            self.listening = True
            if dropped:
                self.publish(RESET, {"reason": "listen"})
            try:
                while not self._stop.is_set():
                    signal = sub.wait(1.0)
                    if signal is None:
                        continue
                    if signal.get("event") == "down":
                        log.warning("écoute LISTEN tombée : %s", signal.get("error", "")[:200])
                        break
                    self.publish(signal.get("channel", ""), _payload(signal.get("payload")))
            finally:
                self.listening = False
                dropped = True
                try:
                    sub.close()
                except Exception:
                    pass
            self._stop.wait(RELISTEN_S)

    # -- écriture ---------------------------------------------------------------
    def publish(self, channel: str, data: dict) -> Event:
        data = wire_payload(channel, data)
        with self._cond:
            self._n += 1
            event = Event(cursor(self.stream, self._n), channel, data)
            self._ring.append((self._n, event))
            self._cond.notify_all()
        return event

    # -- lecture ------------------------------------------------------------------
    def current(self) -> str:
        with self._cond:
            return cursor(self.stream, self._n)

    def _read(self, after_n: Optional[int], admitted) -> tuple[list[Event], str, int]:
        """Sous le verrou : (événements visibles après `after_n`, curseur
        courant, n courant). `after_n` None : trou de reprise."""
        now = cursor(self.stream, self._n)
        if after_n is None:
            return [Event(now, RESET, {"reason": "gap"})], now, self._n
        events = [e for n, e in self._ring if n > after_n and visible(e, admitted)]
        return events, now, self._n

    def _after(self, after: Optional[str]) -> Optional[int]:
        """`n` de reprise ; None si le curseur ouvre un trou (autre flux, mal
        formé, sorti du tampon, venu du futur). Sans curseur : maintenant."""
        if not after:
            return self._n
        try:
            stream, n = parse_cursor(after)
        except ValueError:
            return None
        if stream != self.stream or n > self._n or n < 0:
            return None
        oldest = self._ring[0][0] if self._ring else self._n + 1
        if n < oldest - 1:
            return None
        return n

    def poll(self, after: Optional[str], admitted: Callable[[], Iterable[str]],
             wait: float) -> tuple[list[Event], str]:
        """Attente longue : rend dès qu'un événement visible existe après
        `after`, sinon une liste vide au bout de `wait` secondes."""
        deadline = time.monotonic() + max(0.0, wait)
        allowed = frozenset(admitted())   # lu hors du verrou (base, cache)
        with self._cond:
            after_n = self._after(after)
            while True:
                events, now, n = self._read(after_n, allowed)
                if events:
                    return events, now
                after_n = n
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return [], now
                self._cond.wait(remaining)


def wire_payload(channel: str, data: dict) -> dict:
    """Le payload tel qu'il sort sur le fil (contrat 1.1) : pour
    `agent_lease`, la clé `agent` du déclencheur de 0001 devient `name`
    (`{"name", "owner", "epoch", "status"}`) ; les autres canaux passent
    tels quels."""
    data = dict(data or {})
    if channel == "agent_lease" and "agent" in data and "name" not in data:
        data["name"] = data.pop("agent")
    return data


def _payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"payload": str(raw)[:1000]}
    return value if isinstance(value, dict) else {"payload": value}


__all__ = ["CHANNELS", "EventHub", "visible", "wire_payload"]
