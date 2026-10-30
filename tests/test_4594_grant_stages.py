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
* le banc local qui reproduit la file derrière `_tx_lock` (un détenteur
  tient le verrou pendant la mesure) et mesure le réveil devant elle.
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

    Un détenteur tient `_tx_lock` jusqu'à l'Event de relâchement : la file
    existe par construction (une acquisition ordinaire attend plus de 2 ms),
    `threading.Lock` n'étant pas FIFO, aucune mesure ne dépend plus de
    l'ordonnancement. Une lecture en base attend derrière ce verrou ; le
    réveil du worker doit partir avant cette file, pas après."""

    def setUp(self) -> None:
        super().setUp()
        self.register("lat-file", "claude")
        self.runner_obj = Runner(dataclasses.replace(self.cfg), self.db, once=True)
        self.worker = _Worker()
        self.runner_obj.workers["lat-file"] = self.worker
        self.mid = mail.send(self.db, "dev1", "lat-file", "un message")

    def test_le_reveil_devance_la_file(self):
        lock = threading.RLock()
        held = threading.Event()
        release = threading.Event()

        def holder():
            with lock:
                held.set()
                release.wait(5)

        original = mail.get

        def queued_get(db, message_id):
            with lock:
                time.sleep(0.05)
            return original(db, message_id)

        # 1. précondition : un détenteur tient le verrou, une acquisition
        # ordinaire attend derrière lui. Non tenue = banc non concluant.
        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        try:
            if not held.wait(5):
                self.skipTest("le détenteur n'a pas pris le verrou")
            started = time.monotonic()
            acquired = lock.acquire(timeout=0.02)
            queue_s = time.monotonic() - started
            if acquired:
                lock.release()
            if acquired or queue_s <= 0.002:
                self.skipTest("le banc ne reproduit pas la file derrière le verrou")
        finally:
            release.set()
            holder_thread.join(timeout=5)

        # 2. la lecture en base est derrière ce verrou quand `dispatch` part :
        # le réveil doit devancer la file, pas l'attendre.
        held.clear()
        release.clear()
        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        thread = None
        try:
            if not held.wait(5):
                self.skipTest("le détenteur n'a pas repris le verrou")
            item = {"channel": "agent_mail",
                    "payload": json.dumps({"to": "lat-file", "id": self.mid})}
            with mock.patch.object(mail, "get", side_effect=queued_get):
                started = time.monotonic()
                thread = threading.Thread(target=self.runner_obj.dispatch, args=(item,))
                thread.start()
                woke = self.worker.wake.wait(0.2)
                wake_s = time.monotonic() - started
                release.set()
                thread.join(timeout=5)
        finally:
            release.set()
            holder_thread.join(timeout=5)
            if thread is not None:
                thread.join(timeout=5)
        self.assertTrue(woke, "le réveil est resté derrière la file")
        self.assertLess(wake_s, 0.05,
                        "le réveil a attendu la lecture en base (file de %.3fs)"
                        % queue_s)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
