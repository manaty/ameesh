# SPDX-License-Identifier: AGPL-3.0-only
"""#4594 — étages du grant et réveil avant la lecture en base.

Le rapport #3513 demande de nommer les étages « reserve → deliver → harness
start → pending → grant ». Amesh en mesure trois (`reserve_s`, `deliver_s`,
`harness start` = `spawn` + `harness_start`) ; `pending` et `grant` sont côté
consommateur, après `first_output_ts`. Les témoins de ce module couvrent :

* les deux étages nommés dans le reçu `turn.stages` (voir aussi
  `test_3218_grant_latency`) ;
* le réveil du worker **avant toute lecture en base** dans `dispatch` : un
  `mail.get` ou un `registry.get` qui attend la connexion partagée ne retarde
  plus le signal ;
* le banc local qui reproduit la file derrière `_tx_lock` (plusieurs fils
  dans une section critique courte) et mesure le réveil devant elle.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
import unittest
from unittest import mock

from ameesh import mail, registry
from ameesh.runner import Runner

from .support import PgTestCase


class _Worker:
    """Worker minimal : ce que `dispatch` touche, rien de plus."""

    def __init__(self) -> None:
        self.wake = threading.Event()
        self.preempted: list[dict] = []

    def interrupt_allowed(self, _row) -> bool:
        return False

    def request_preempt(self, row) -> bool:
        self.preempted.append(row)
        return True

    def request_restart(self) -> None:
        pass


class ReveilAvantLectureTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("lat-disp", "claude")
        self.runner_obj = Runner(dataclasses.replace(self.cfg), self.db, once=True)
        self.worker = _Worker()
        self.runner_obj.workers["lat-disp"] = self.worker
        self.mid = mail.send(self.db, "dev1", "lat-disp", "un message")

    def test_le_reveil_part_avant_la_lecture_en_base(self):
        original = mail.get
        gate = threading.Event()

        def slow_get(db, message_id):
            gate.wait(2.0)  # simule une lecture qui attend la connexion
            return original(db, message_id)

        item = {"channel": "agent_mail",
                "payload": json.dumps({"to": "lat-disp", "id": self.mid})}
        with mock.patch.object(mail, "get", side_effect=slow_get):
            started = time.monotonic()
            thread = threading.Thread(target=self.runner_obj.dispatch, args=(item,))
            thread.start()
            woke = self.worker.wake.wait(0.5)
            elapsed = time.monotonic() - started
            gate.set()
            thread.join(timeout=5)
        self.assertTrue(woke, "dispatch a attendu la lecture en base avant le réveil")
        self.assertLess(elapsed, 0.4)
        self.assertFalse(thread.is_alive())

    def test_le_reveil_part_avant_la_lecture_du_restart(self):
        gate = threading.Event()

        def slow_get(_db, _name):
            gate.wait(2.0)
            return {}

        item = {"channel": "agent_mail",
                "payload": json.dumps({"to": "lat-disp", "restart": True})}
        with mock.patch.object(registry, "get", side_effect=slow_get):
            thread = threading.Thread(target=self.runner_obj.dispatch, args=(item,))
            thread.start()
            woke = self.worker.wake.wait(0.5)
            gate.set()
            thread.join(timeout=5)
        self.assertTrue(woke, "dispatch restart a attendu la lecture en base")
        self.assertFalse(thread.is_alive())


class BancFileTxLockTest(PgTestCase):
    """Le banc local de la file derrière la connexion partagée.

    Vingt fils tiennent `_tx_lock` 2 ms toutes les 10 ms (une passe de
    sondage) ; une lecture en base attend derrière eux. Le réveil du worker
    doit partir avant cette file, pas après."""

    def setUp(self) -> None:
        super().setUp()
        self.register("lat-file", "claude")
        self.runner_obj = Runner(dataclasses.replace(self.cfg), self.db, once=True)
        self.worker = _Worker()
        self.runner_obj.workers["lat-file"] = self.worker
        self.mid = mail.send(self.db, "dev1", "lat-file", "un message")

    def test_le_reveil_devance_la_file(self):
        lock = threading.RLock()
        stop = threading.Event()

        def poller():
            while not stop.is_set():
                with lock:
                    time.sleep(0.002)
                time.sleep(0.008)

        pollers = [threading.Thread(target=poller, daemon=True) for _ in range(20)]
        for thread in pollers:
            thread.start()
        time.sleep(0.05)
        # mesure de la file : une acquisition ordinaire attend les sondeurs
        started = time.monotonic()
        with lock:
            pass
        queue_s = time.monotonic() - started

        original = mail.get

        def queued_get(db, message_id):
            with lock:
                time.sleep(0.05)
            return original(db, message_id)

        item = {"channel": "agent_mail",
                "payload": json.dumps({"to": "lat-file", "id": self.mid})}
        try:
            with mock.patch.object(mail, "get", side_effect=queued_get):
                started = time.monotonic()
                thread = threading.Thread(target=self.runner_obj.dispatch, args=(item,))
                thread.start()
                woke = self.worker.wake.wait(0.2)
                wake_s = time.monotonic() - started
                thread.join(timeout=5)
        finally:
            stop.set()
            for poller_thread in pollers:
                poller_thread.join(timeout=2)
        self.assertGreater(queue_s, 0.002,
                           "le banc ne reproduit pas la file derrière le verrou")
        self.assertTrue(woke, "le réveil est resté derrière la file")
        self.assertLess(wake_s, 0.05,
                        "le réveil a attendu la lecture en base (file de %.3fs)"
                        % queue_s)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
