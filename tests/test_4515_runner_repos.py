# SPDX-License-Identifier: AGPL-3.0-only
"""#4515 — un exécuteur au repos ne brûle plus un cœur (manaty/ameesh#3447).

Constat IMA (1.16.0) : 1,1–1,4 vCPU hors tour, ~190 fils réveillés toutes les
5–20 ms. Banc local (psycopg, 20 agents au repos, 60 s) : 16 907 requêtes/min
en 1.16.0, 7 442 sur `main` d'avant ce lot, chaque worker réveillé ~200 fois/s.
Trois causes, trois témoins déterministes (aucune attente murale non bornée) :

* chaque worker au repos refaisait sa passe complète (~30 requêtes sur la
  connexion partagée) toutes les `poll` secondes : le pas s'allonge avec le
  nombre d'agents tant que l'écoute est établie (`Runner.idle_poll`) ;
* tout courrier du maillage pour un agent servi ailleurs réveillait tous les
  workers de l'exécuteur : seul un NOTIFY sans destinataire le fait encore ;
* le verrou d'admission (#4602) réveillait toutes les attentes à chaque
  passation : seul le fil choisi est réveillé.

Plus un vidage des piles par SIGUSR1, pour les hôtes sans ptrace.
"""
from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import admission_lock, runner as runner_mod, storage
from ameesh.runner import AgentWorker, Runner


def _runner(agents: int, listening: bool, poll: float = 5.0) -> Runner:
    runner = Runner.__new__(Runner)
    runner.poll = poll
    runner.lock = threading.Lock()
    runner.workers = {"a%02d" % i: object() for i in range(agents)}
    runner.listener = object() if listening else None
    return runner


class PasDeRepos(unittest.TestCase):
    def test_sans_ecoute_le_sondage_reste_poll(self):
        self.assertEqual(_runner(70, listening=False).idle_poll(), 5.0)

    def test_petit_executeur_inchange(self):
        # un exécuteur à un agent (hôtes du maillage ameesh) garde `poll`
        self.assertEqual(_runner(1, listening=True).idle_poll(), 5.0)
        self.assertEqual(_runner(5, listening=True).idle_poll(), 5.0)

    def test_budget_de_passes_de_l_executeur(self):
        for agents in (20, 45):
            runner = _runner(agents, listening=True)
            pas = runner.idle_poll()
            # au plus IDLE_PASSES_PER_S passes de repos par seconde, en tout
            self.assertLessEqual(agents / pas, Runner.IDLE_PASSES_PER_S + 1e-9)

    def test_plafond_de_releve(self):
        # relève élue (#4231) : 60 + 60 + 120 + pas < 300 s (canon §11)
        for agents in (70, 200):
            self.assertEqual(_runner(agents, listening=True).idle_poll(),
                             Runner.IDLE_POLL_MAX_S)
        self.assertLess(60 + 60 + 120 + Runner.IDLE_POLL_MAX_S, 300)

    def test_worker_borne_par_le_bail_et_doublure(self):
        worker = AgentWorker.__new__(AgentWorker)
        worker.runner = _runner(30, listening=True)
        self.assertEqual(worker._idle_poll(), 30.0)
        # l'appelant borne au tiers du bail : `min(_idle_poll(), ttl / 3)`
        worker.runner = types.SimpleNamespace(poll=3.0)  # doublure sans budget
        self.assertEqual(worker._idle_poll(), 3.0)

    def test_ecoute_retablie_reveille_chaque_worker_une_fois(self):
        """Un NOTIFY perdu pendant la coupure ne peut pas attendre `idle_poll`."""
        runner = _runner(0, listening=False, poll=1)
        workers = [types.SimpleNamespace(wake=threading.Event()) for _ in range(3)]
        runner.workers = {str(i): w for i, w in enumerate(workers)}
        runner.train_service = None
        runner.cfg = types.SimpleNamespace(connect_timeout=1)
        runner.db, runner.wake_all = object(), threading.Event()
        runner.stop = threading.Event()
        runner.db_retry_max = 1
        listener = mock.Mock()
        vus = []

        def wait(**_):
            vus.append([w.wake.is_set() for w in workers])
            runner.stop.set()
            return None

        listener.wait.side_effect = wait
        with mock.patch.object(storage, "of") as st:
            st.return_value.wakeups.subscribe.return_value = listener
            runner._listen_loop()
        self.assertEqual(vus, [[True, True, True]])


class PorteFermee(unittest.TestCase):
    def test_la_fermeture_reveille_les_workers_une_fois(self):
        runner = _runner(0, listening=True)
        workers = [types.SimpleNamespace(wake=threading.Event(), is_alive=lambda: True)
                   for _ in range(3)]
        runner.workers = {str(i): w for i, w in enumerate(workers)}
        runner.db, runner.host, runner.mediated = object(), "h", True
        runner.power_hold, runner.draining = threading.Event(), threading.Event()
        runner._gate_settled = threading.Event()
        runner._gate_settled.set()
        runner.host_gate = mock.Mock()
        runner.host_gate.holds_turns.return_value = True
        for nom in ("report_liveness", "role_evaluation_once", "expire_delegations_once",
                    "expire_operator_pauses_once", "lift_device_gates_once",
                    "prune_events_once", "_note_gate_wait"):
            setattr(runner, nom, mock.Mock())
        with mock.patch.object(runner_mod.registry, "reap", return_value=[]), \
                mock.patch.object(runner_mod.migrations, "require_compatible_writer"):
            runner.sweep()
            self.assertTrue(all(w.wake.is_set() for w in workers))
            for w in workers:
                w.wake.clear()
            runner.sweep()  # porte toujours fermée : pas de nouveau réveil
        self.assertFalse(any(w.wake.is_set() for w in workers))


class _Worker:
    def __init__(self):
        self.wake = threading.Event()
        self._priority_wake = False


class CourrierDuMaillage(unittest.TestCase):
    def _dispatch(self, payload: dict):
        ici = _Worker()
        fake = types.SimpleNamespace(lock=threading.RLock(), workers={"ici": ici},
                                     wake_all=threading.Event())
        fake.db = object()
        with mock.patch.object(runner_mod.migrations, "require_compatible_writer"):
            Runner.dispatch(fake, {"channel": "agent_mail", "payload": json.dumps(payload)})
        return ici, fake

    def test_courrier_pour_un_agent_servi_ailleurs(self):
        ici, fake = self._dispatch({"to": "ailleurs", "from": "x"})
        self.assertFalse(ici.wake.is_set())
        self.assertTrue(fake.wake_all.is_set())  # la passe de l'exécuteur relit

    def test_sans_destinataire_tout_le_monde_relit(self):
        ici, _fake = self._dispatch({})
        self.assertTrue(ici.wake.is_set())

    def test_destinataire_servi_ici(self):
        ici, _fake = self._dispatch({"to": "ici"})
        self.assertTrue(ici.wake.is_set())


class _CountingCondition(threading.Condition):
    """Compte les retours de `wait` : chaque retour est un réveil du fil."""
    returns = 0
    guard = threading.Lock()

    def wait(self, timeout=None):
        try:
            return super().wait(timeout)
        finally:
            with _CountingCondition.guard:
                _CountingCondition.returns += 1


def _conditions_comptees():
    """Les seules conditions d'`admission_lock` sont comptées (les `Event`
    des fils du test gardent la vraie `threading.Condition`)."""
    proxy = types.SimpleNamespace(**vars(threading))
    proxy.Condition = _CountingCondition
    return mock.patch.object(admission_lock, "threading", proxy)


class PassationCiblee(unittest.TestCase):
    def setUp(self):
        admission_lock.reset()
        _CountingCondition.returns = 0

    def tearDown(self):
        admission_lock.reset()

    def test_une_passation_ne_reveille_que_le_fil_choisi(self):
        attentes = 12
        with _conditions_comptees():
            lock = admission_lock.PriorityRLock()
            lock.acquire()
            ordre: list[int] = []

            def waiter(i: int):
                with lock:
                    ordre.append(i)

            fils = []
            for i in range(attentes):
                t = threading.Thread(target=waiter, args=(i,))
                t.start()
                fils.append(t)
                deadline = time.monotonic() + 5
                while lock.queued()[1] < i + 1:  # FIFO : départs en ordre
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.001)
            lock.release()
            for t in fils:
                t.join(5)
        self.assertEqual(ordre, list(range(attentes)))
        # un réveil par attente ; `notify_all` en faisait 12+11+…+1 = 78
        self.assertEqual(_CountingCondition.returns, attentes)

    def test_expiration_sans_reveil_des_autres(self):
        with _conditions_comptees():
            lock = admission_lock.PriorityRLock()
            lock.acquire()
            resultat = {}
            autre = threading.Thread(target=lambda: resultat.setdefault(
                "autre", lock.acquire(timeout=5)))
            autre.start()
            while lock.queued()[1] < 1:
                time.sleep(0.001)
            presse = threading.Thread(target=lambda: resultat.setdefault(
                "presse", lock.acquire(timeout=0.05)))
            presse.start()
            presse.join(5)
            self.assertFalse(resultat["presse"])
            self.assertEqual(lock.queued(), (0, 1))
            lock.release()
            autre.join(5)
        self.assertTrue(resultat["autre"])
        # l'expiration ne réveille que son propre fil ; la passation, l'autre
        self.assertEqual(_CountingCondition.returns, 2)


class PilesSansPtrace(unittest.TestCase):
    def test_sigusr1_ecrit_la_pile_de_chaque_fil(self):
        import faulthandler
        sig = signal.SIGUSR2  # le banc ne touche pas à la disposition de SIGUSR1
        arret = threading.Event()
        fil = threading.Thread(target=arret.wait, daemon=True)
        fil.start()
        with tempfile.TemporaryFile("w+") as sortie:
            self.assertTrue(runner_mod.dump_stacks_on_signal(sig, sortie))
            try:
                os.kill(os.getpid(), sig)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    sortie.seek(0)
                    texte = sortie.read()
                    if texte.count("Thread 0x") >= 2:
                        break
                    time.sleep(0.01)
            finally:
                faulthandler.unregister(sig)
                arret.set()
        self.assertGreaterEqual(texte.count("Thread 0x"), 2, texte)
        self.assertIn("test_4515_runner_repos.py", texte)

    def test_flux_sans_descripteur(self):
        import io
        self.assertFalse(runner_mod.dump_stacks_on_signal(signal.SIGUSR2, io.StringIO()))


if __name__ == "__main__":
    unittest.main()
