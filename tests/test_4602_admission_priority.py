# SPDX-License-Identifier: AGPL-3.0-only
"""#4602 — admission DB équitable, priorité aux workers réveillés.

Un exécuteur sert tous ses agents sur une seule connexion psycopg, sérialisée
par ``PsycopgDriver._tx_lock`` (L1469). Chaque agent au repos refait sa passe
toutes les ``poll`` secondes ; un worker réveillé par un courrier attend
derrière elles. Le verrou d'admission reçoit deux classes :

* ``CONTROL`` : le fil d'écoute et l'agent qu'une notification vient de
  réveiller ; la passe qui suit le réveil garde cette classe ;
* ``NORMAL`` : les passes de sondage au repos, le travail de fond, les tours.

FIFO dans chaque classe, avec vieillissement (une place NORMAL qui attend
depuis ``AGING_S`` passe devant les CONTROL plus récents) et rafale (après
``BOOST_BURST`` acquisitions CONTROL, une NORMAL passe) : un sondeur n'est
jamais affamé. Ces témoins couvrent l'ordre, la réentrance, l'expiration, le
vieillissement, la rafale, la non-famine et le passage de la classe au worker.
"""
from __future__ import annotations

import json
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import admission_lock
from ameesh.runner import AgentWorker, Runner


def _wait_queued(lock: admission_lock.PriorityRLock, count: int,
                 kind: int = admission_lock.NORMAL, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lock._condition:
            if len(lock._queues[kind]) >= count:
                return
        time.sleep(0.002)
    raise AssertionError("lock queue never reached %d/%d" % (count, kind))


class OrdreTest(unittest.TestCase):
    def setUp(self) -> None:
        admission_lock.reset()

    def tearDown(self) -> None:
        admission_lock.reset()

    def _order(self, first_kind: int, second_kind: int):
        lock = admission_lock.PriorityRLock()
        lock.acquire()
        order: list[str] = []

        def waiter(kind: int, label: str):
            admission_lock.set_priority(kind)
            with lock:
                order.append(label)

        first = threading.Thread(target=waiter, args=(first_kind, "first"))
        first.start()
        _wait_queued(lock, 1, first_kind)
        second = threading.Thread(target=waiter, args=(second_kind, "second"))
        second.start()
        _wait_queued(lock, 1, second_kind)
        lock.release()
        first.join(timeout=3)
        second.join(timeout=3)
        self.assertFalse(first.is_alive() or second.is_alive())
        return order

    def test_control_passe_avant_normal(self):
        self.assertEqual(self._order(admission_lock.NORMAL, admission_lock.CONTROL),
                         ["second", "first"])

    def test_fifo_dans_une_classe(self):
        lock = admission_lock.PriorityRLock()
        lock.acquire()
        order: list[int] = []
        threads = []
        for index in range(3):
            thread = threading.Thread(
                target=lambda n=index: (admission_lock.set_priority(admission_lock.NORMAL),
                                        lock.acquire(), order.append(n), lock.release()))
            thread.start()
            _wait_queued(lock, index + 1)
            threads.append(thread)
        lock.release()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(order, [0, 1, 2])

    def test_reentrant(self):
        lock = admission_lock.PriorityRLock()
        with lock:
            with lock:
                self.assertTrue(lock.locked())
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())
        lock.acquire()
        errors: list[BaseException] = []

        def wrong_release():
            try:
                lock.release()
            except BaseException as exc:  # noqa: BLE001 - on vérifie le refus
                errors.append(exc)

        thread = threading.Thread(target=wrong_release)
        thread.start()
        thread.join(timeout=3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        lock.release()

    def test_expiration(self):
        lock = admission_lock.PriorityRLock()
        lock.acquire()
        result: list[bool] = []

        def contend():
            started = time.monotonic()
            result.append(lock.acquire(timeout=0.05))
            result.append(time.monotonic() - started >= 0.04)

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=3)
        lock.release()
        self.assertEqual(result, [False, True])

    def test_rafale_laisse_passer_un_normal(self):
        lock = admission_lock.PriorityRLock()
        control = admission_lock._Ticket(admission_lock.CONTROL, 100.0, 1)
        normal = admission_lock._Ticket(admission_lock.NORMAL, 100.0, 2)
        lock._queues[0].append(control)
        lock._queues[1].append(normal)
        lock._burst = admission_lock.BOOST_BURST
        self.assertIs(lock._next(100.0), normal)
        lock._burst = admission_lock.BOOST_BURST - 1
        self.assertIs(lock._next(100.0), control)

    def test_vieillissement(self):
        lock = admission_lock.PriorityRLock()
        now = 100.0
        control = admission_lock._Ticket(admission_lock.CONTROL, now, 1)
        normal = admission_lock._Ticket(admission_lock.NORMAL,
                                        now - admission_lock.AGING_S - 1, 2)
        lock._queues[0].append(control)
        lock._queues[1].append(normal)
        self.assertIs(lock._next(now), normal)
        # un contrôle plus ancien garde la tête
        control.since = now - admission_lock.AGING_S - 2
        self.assertIs(lock._next(now), control)

    def test_pas_de_reveil_perdu_entre_deux_ages(self):
        """B1 dev1 : deux attentes ne doivent pas se renvoyer la sélection.

        La passation est faite une fois par `release()` ; avec une horloge
        simulée, une NORMAL qui vieillit pendant que la CONTROL attend ne peut
        pas laisser le verrou libre sans réveiller personne."""
        clock = [100.0]
        lock = admission_lock.PriorityRLock(clock=lambda: clock[0])
        lock.acquire()
        order: list[str] = []

        def waiter(kind: int, label: str):
            admission_lock.set_priority(kind)
            with lock:
                order.append(label)

        normal = threading.Thread(target=waiter, args=(admission_lock.NORMAL, "normal"))
        normal.start()
        _wait_queued(lock, 1, admission_lock.NORMAL)
        clock[0] = 100.2
        control = threading.Thread(target=waiter, args=(admission_lock.CONTROL, "control"))
        control.start()
        _wait_queued(lock, 1, admission_lock.CONTROL)
        clock[0] = 102.2  # la NORMAL est vieillie (2,2 s), la CONTROL non (2,0 s)
        lock.release()
        normal.join(timeout=2)
        control.join(timeout=2)
        self.assertFalse(normal.is_alive() or control.is_alive(),
                         "réveil perdu : le verrou est resté libre avec des attentes")
        self.assertEqual(order, ["normal", "control"])

    def test_bascule_de_secours(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"AMEESH_DB_ADMISSION_PRIORITY": "0"}):
            self.assertFalse(admission_lock.enabled())
        with mock.patch.dict(os.environ, {"AMEESH_DB_ADMISSION_PRIORITY": "1"}):
            self.assertTrue(admission_lock.enabled())

    def test_pas_de_famine(self):
        lock = admission_lock.PriorityRLock()
        progress = {"normal": 0}
        done = threading.Event()

        def normal():
            admission_lock.set_priority(admission_lock.NORMAL)
            for _ in range(20):
                with lock:
                    progress["normal"] += 1

        def control():
            admission_lock.set_priority(admission_lock.CONTROL)
            for _ in range(40):
                with lock:
                    time.sleep(0.001)
            done.set()

        threads = [threading.Thread(target=normal) for _ in range(3)]
        for thread in threads:
            thread.start()
        controller = threading.Thread(target=control)
        controller.start()
        controller.join(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(done.is_set())
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertGreater(progress["normal"], 0)


class _Worker:
    """Worker minimal : ce que `dispatch` touche, rien de plus."""

    def __init__(self) -> None:
        self.wake = threading.Event()
        self._priority_wake = False
        self.preempted: list[dict] = []

    def interrupt_allowed(self, _row) -> bool:
        return False

    def request_preempt(self, row) -> bool:
        self.preempted.append(row)
        return True

    def request_restart(self) -> None:
        pass


class PrioriteWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        admission_lock.reset()

    def tearDown(self) -> None:
        admission_lock.reset()

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_dispatch_marque_la_passe(self):
        worker = _Worker()
        fake = types.SimpleNamespace(lock=threading.RLock(),
                                     workers={"x": worker},
                                     wake_all=threading.Event(), db=None)
        Runner.dispatch(fake, {"channel": "agent_mail",
                               "payload": json.dumps({"to": "x"})})
        self.assertTrue(worker._priority_wake)
        self.assertTrue(worker.wake.is_set())

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_sans_cible_ne_met_pas_tout_en_control(self):
        worker = _Worker()
        fake = types.SimpleNamespace(lock=threading.RLock(),
                                     workers={"x": worker},
                                     wake_all=threading.Event(), db=None)
        Runner.dispatch(fake, {"channel": "agent_mail",
                               "payload": json.dumps({})})
        self.assertTrue(worker.wake.is_set())
        self.assertFalse(worker._priority_wake)

    def test_le_worker_consomme_sa_priorite(self):
        partial = AgentWorker.__new__(AgentWorker)
        partial._priority_wake = True
        AgentWorker._admission_priority(partial)
        self.assertEqual(admission_lock.priority(), admission_lock.CONTROL)
        self.assertFalse(partial._priority_wake)
        AgentWorker._admission_priority(partial)
        self.assertEqual(admission_lock.priority(), admission_lock.NORMAL)

    def test_la_passe_reveillee_reste_control_jusqu_au_choix(self):
        """B2 dev1 : le second appel ne redescend jamais la passe en NORMAL."""
        partial = AgentWorker.__new__(AgentWorker)
        partial._priority_wake = True
        AgentWorker._admission_priority(partial)
        self.assertEqual(admission_lock.priority(), admission_lock.CONTROL)
        AgentWorker._admission_control(partial)  # avant _pick_for_turn
        self.assertEqual(admission_lock.priority(), admission_lock.CONTROL)
        self.assertFalse(partial._priority_wake)


class BancAdmissionTest(unittest.TestCase):
    """Mesure avant/après sur la file : un contrôle devant des sondeurs."""

    def setUp(self) -> None:
        admission_lock.reset()

    def tearDown(self) -> None:
        admission_lock.reset()

    def test_le_controle_devance_les_sondeurs(self):
        lock = admission_lock.PriorityRLock()
        stop = threading.Event()

        def poller():
            admission_lock.set_priority(admission_lock.NORMAL)
            while not stop.is_set():
                with lock:
                    time.sleep(0.002)
                time.sleep(0.008)

        threads = [threading.Thread(target=poller, daemon=True) for _ in range(20)]
        for thread in threads:
            thread.start()
        time.sleep(0.05)
        normal_waits: list[float] = []
        control_waits: list[float] = []
        try:
            for _ in range(5):
                started = time.monotonic()
                with lock:
                    pass
                normal_waits.append(time.monotonic() - started)
                started = time.monotonic()
                admission_lock.set_priority(admission_lock.CONTROL)
                with lock:
                    pass
                control_waits.append(time.monotonic() - started)
                admission_lock.reset()
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertGreater(min(normal_waits), 0.0)
        self.assertLess(max(control_waits), min(normal_waits),
                        "le contrôle n'est pas passé devant les sondeurs "
                        "(normal %s, contrôle %s)" % (normal_waits, control_waits))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
