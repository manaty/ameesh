# SPDX-License-Identifier: AGPL-3.0-only
"""Réveil de l'exécuteur médié : `GET /api/exec/v1/events` à la place de
LISTEN/NOTIFY (lot L109).

`RemoteSubscription` tient le contrat de `storage.interface.Subscription` :
`wait(timeout)` rend `{"channel", "payload"}` (payload NOTIFY d'origine, en
texte JSON), None au délai, ou `{"event": "down", "error"}` quand le flux est
rompu (l'exécuteur ferme et se réabonne, avec l'attente croissante de L72).

* Un fil lit le flux **SSE** ; si le serveur ne le sert pas, il passe à
  l'**attente longue** (`?wait=25&after=`) et s'y tient pour la connexion.
* Le curseur (`Last-Event-ID` / `after`) est gardé sur la connexion : un
  nouvel abonnement reprend là où le précédent s'est arrêté. Un trou de
  reprise arrive comme un événement `reset` : tout relire.
* Un réveil ne fait jamais foi : l'exécuteur relit en base et garde son
  sondage.
"""
from __future__ import annotations

import queue
import threading
from typing import Optional, Sequence

from ... import db as db_mod
from ...executeur_mediee import contrat as C
from ...executeur_mediee import evenements as E


class RemoteSubscription:
    """Abonnement au flux d'événements du serveur, pour ces canaux."""

    def __init__(self, db, channels: Sequence[str], *, start: bool = True):
        self.db = db
        self.channels = frozenset(channels or E.CHANNELS)
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name="flux-exec")
        if start:
            self._thread.start()

    # -- fil de lecture ------------------------------------------------------
    def _push(self, event: E.Event) -> None:
        if event.id:
            self.db.events_cursor = event.id
        if event.channel == E.RESET or event.channel in self.channels:
            self._queue.put(event.as_signal())

    def _down(self, exc: BaseException) -> None:
        self._queue.put({"event": "down", "error": db_mod.explain(exc)})

    def _read(self) -> None:
        transport = self.db.transport
        try:
            while not self._closed.is_set():
                if not self.db.events_long_poll:
                    try:
                        for event in transport.stream_events(self.db.events_cursor):
                            if self._closed.is_set():
                                return
                            self._push(event)
                        # un flux qui se termine sans erreur est une coupure
                        raise db_mod.Unavailable("flux d'événements terminé")
                    except C.NotSupportedRemotely:
                        self.db.events_long_poll = True
                        continue
                events, last = transport.poll_events(self.db.events_cursor, E.MAX_WAIT_S)
                if self._closed.is_set():
                    return
                for event in events:
                    self._push(event)
                if last:
                    self.db.events_cursor = last
        except db_mod.DbError as exc:
            if not self._closed.is_set():
                self._down(exc)
        except Exception as exc:  # jamais un fil mort en silence : l'exécuteur se réabonne
            if not self._closed.is_set():
                self._down(exc)

    # -- Subscription ----------------------------------------------------------
    def wait(self, timeout: float) -> Optional[dict]:
        if self._closed.is_set():
            return None
        try:
            return self._queue.get(timeout=max(0.0, float(timeout)))
        except queue.Empty:
            return None

    def close(self) -> None:
        """Idempotent, ne lève pas. Le fil de lecture s'arrête au prochain
        événement ou délai de lecture (c'est un fil démon)."""
        self._closed.set()
