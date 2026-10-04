# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : réveil par LISTEN/NOTIFY.

Les signaux `agent_mail` et `agent_lease` sont émis par des triggers
(migration 0001). `subscribe` ouvre l'écouteur de `ameesh.db` (`db.listener` :
psycopg, connexion dédiée en LISTEN ; psql, session interactive sur un pty
avec battement de 0,5 s) et le rend tel quel : mêmes canaux, même `wait`,
même événement `down` ; None si le pilote ne sait pas écouter (repli de
l'appelant sur le sondage). `notify` est le `pg_notify` du diagnostic
`doctor`, SQL déplacé tel quel depuis `ameesh.cli` (lot L1, phase 2).
"""
from __future__ import annotations

from ... import db as db_mod
from .. import interface


class Wakeups(interface.Wakeups):

    def subscribe(self, channels) -> interface.Subscription | None:
        return db_mod.listener(self.db, channels)

    def notify(self, channel, payload) -> None:
        self.db.execute("SELECT pg_notify(%s, %s)", (channel, payload))
