# SPDX-License-Identifier: AGPL-3.0-only
"""#8152 — coût d'un exécuteur au repos au-delà de 45 agents (manaty/ameesh#6379).

Constat IMA (1.61.0, 128 agents au repos) : 0,4 cœur et 80 à 100 requêtes/s
sans tour. Au-delà de 45 agents, le pas plafonné à 45 s refait croître les
passes avec le nombre d'agents (2,8 passes/s à 128) ; chaque passe relisait
`schema_non_additive_migrations` et appelait `ameesh_resolve_agent_name`
quatre fois pour le même nom. Banc local (10 agents, pilote psql) : 19,7
requêtes par passe avant ce lot, 14,6 après.

Témoins :

* l'hôte règle le débit de repos (`AMEESH_IDLE_PASSES_PER_S`), à toute
  taille : ni plafond de 45 s ni tiers du bail, sauf `AMEESH_IDLE_POLL_MAX` ;
  sans réglage, le pas est inchangé ;
* la passe garde les noms primaires lus du registre : `resolve` ne refait pas
  la requête dans la passe, et l'oublie après ;
* une passe au repos réelle (base de test) ne relit ni le marqueur de schéma
  (partagé 5 s, #5980) ni la résolution du nom.
"""
from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import agent_names, config as config_mod, db as db_mod, registry
from ameesh.runner import AgentWorker, Runner

from . import EMPTY_CONFIG
from .support import PgTestCase


def _runner(agents: int, *, rate: float = 0.0, cap: float = 0.0,
            poll: float = 5.0) -> Runner:
    runner = Runner.__new__(Runner)
    runner.poll = poll
    runner.lock = threading.Lock()
    runner.workers = {"a%03d" % i: object() for i in range(agents)}
    runner.listener = object()
    runner.cfg = types.SimpleNamespace(idle_passes_per_s=rate, idle_poll_max=cap)
    return runner


class DebitDeRepos(unittest.TestCase):
    def test_sans_reglage_le_pas_est_inchange(self):
        self.assertEqual(_runner(30).idle_poll(), 30.0)
        self.assertEqual(_runner(128).idle_poll(), Runner.IDLE_POLL_MAX_S)
        self.assertFalse(_runner(128).idle_pace_set())

    def test_le_debit_de_l_hote_vaut_au_dela_de_45_agents(self):
        runner = _runner(128, rate=0.2)
        self.assertTrue(runner.idle_pace_set())
        self.assertAlmostEqual(runner.idle_poll(), 640.0)
        # l'exécuteur entier : au plus le débit réglé
        self.assertAlmostEqual(128 / runner.idle_poll(), 0.2)

    def test_plafond_de_l_hote(self):
        self.assertEqual(_runner(128, rate=0.2, cap=120.0).idle_poll(), 120.0)
        self.assertEqual(_runner(128, cap=30.0).idle_poll(), 30.0)

    def test_poll_reste_le_plancher(self):
        self.assertEqual(_runner(2, rate=10.0).idle_poll(), 5.0)

    def test_la_config_relue_s_applique(self):
        runner = _runner(128)
        runner.cfg = types.SimpleNamespace(idle_passes_per_s=1.0, idle_poll_max=0.0)
        self.assertEqual(runner.idle_poll(), 128.0)

    def _worker(self, runner, ttl: float) -> AgentWorker:
        worker = AgentWorker.__new__(AgentWorker)
        worker.runner = runner
        patcher = mock.patch.object(AgentWorker, "lease_ttl",
                                    new_callable=mock.PropertyMock, return_value=ttl)
        patcher.start()
        self.addCleanup(patcher.stop)
        return worker

    def test_tiers_du_bail_leve_seulement_par_le_reglage(self):
        self.assertEqual(self._worker(_runner(128), 180)._idle_wait(), 45.0)

    def test_tiers_du_bail_court(self):
        self.assertEqual(self._worker(_runner(128), 90)._idle_wait(), 30.0)

    def test_debit_regle_sans_tiers_du_bail(self):
        # le battement renouvelle seul (#5806) : le débit réglé n'est pas
        # ramené au tiers du bail
        self.assertAlmostEqual(self._worker(_runner(128, rate=0.2), 180)._idle_wait(), 640.0)

    def test_doublure_sans_reglage(self):
        worker = self._worker(types.SimpleNamespace(poll=3.0), 180)
        self.assertEqual(worker._idle_wait(), 3.0)


class AttenteMalgreLesRenouvellements(unittest.TestCase):
    """B impl-11 : le pas réglé est tenu même s'il dépasse le bail."""

    TTL = 0.3

    def _worker(self):
        worker = AgentWorker.__new__(AgentWorker)
        worker.runner = types.SimpleNamespace(stop=threading.Event(),
                                              draining=threading.Event())
        worker.wake, worker.stopping = threading.Event(), threading.Event()
        worker.lease_lost = threading.Event()
        worker.lease_deadline = time.time() + self.TTL
        patcher = mock.patch.object(AgentWorker, "lease_ttl",
                                    new_callable=mock.PropertyMock, return_value=self.TTL)
        patcher.start()
        self.addCleanup(patcher.stop)
        return worker

    def _renew(self, worker, stop):
        # le battement : repousse l'échéance sans toucher l'attente du worker
        def battement():
            while not stop.wait(self.TTL / 3):
                worker.lease_deadline = time.time() + self.TTL
        thread = threading.Thread(target=battement, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)

    def test_le_pas_entier_malgre_un_bail_plus_court(self):
        worker, stop = self._worker(), threading.Event()
        self._renew(worker, stop)
        debut = time.monotonic()
        self.assertFalse(worker._idle_sleep(1.5))
        # ni coupé à l'échéance du premier bail (0,3 s), ni rejoué
        self.assertGreaterEqual(time.monotonic() - debut, 1.45)

    def test_un_reveil_interrompt(self):
        worker, stop = self._worker(), threading.Event()
        self._renew(worker, stop)
        threading.Timer(0.2, worker.wake.set).start()
        debut = time.monotonic()
        self.assertTrue(worker._idle_sleep(5.0))
        self.assertLess(time.monotonic() - debut, 1.0)

    def test_bail_echu_sans_battement(self):
        worker = self._worker()  # personne ne renouvelle
        debut = time.monotonic()
        self.assertFalse(worker._idle_sleep(5.0))
        self.assertLess(time.monotonic() - debut, 1.0)

    def test_arret_drainage_perte_de_bail(self):
        for drapeau in ("stop", "draining", "lease_lost", "stopping"):
            worker, stop = self._worker(), threading.Event()
            self._renew(worker, stop)
            event = (getattr(worker.runner, drapeau) if drapeau in ("stop", "draining")
                     else getattr(worker, drapeau))
            threading.Timer(0.1, event.set).start()
            debut = time.monotonic()
            self.assertFalse(worker._idle_sleep(5.0), drapeau)
            # vu à la fin de la tranche en cours (bornée par le bail)
            self.assertLess(time.monotonic() - debut, 2.0, drapeau)
            stop.set()


class ConfigDuDebit(unittest.TestCase):
    def _load(self, **env):
        return config_mod.load(env=dict(env, AMEESH_CONFIG=EMPTY_CONFIG))

    def test_defaut(self):
        cfg = self._load()
        self.assertEqual((cfg.idle_passes_per_s, cfg.idle_poll_max), (0.0, 0.0))

    def test_reglage(self):
        cfg = self._load(AMEESH_IDLE_PASSES_PER_S="0.25", AMEESH_IDLE_POLL_MAX="300")
        self.assertEqual((cfg.idle_passes_per_s, cfg.idle_poll_max), (0.25, 300.0))

    def test_valeur_illisible_ou_nulle_garde_le_defaut(self):
        for value in ("abc", "0", "-1", "nan", "inf"):
            cfg = self._load(AMEESH_IDLE_PASSES_PER_S=value, AMEESH_IDLE_POLL_MAX=value)
            self.assertEqual((cfg.idle_passes_per_s, cfg.idle_poll_max), (0.0, 0.0), value)


class ResolutionDansLaPasse(unittest.TestCase):
    def _db(self):
        agents = mock.Mock()
        agents.resolve_name.side_effect = lambda name, operation: name
        return agents

    def test_le_primaire_lu_sert_la_passe(self):
        agents = self._db()
        with mock.patch.object(agent_names.storage, "of",
                               return_value=types.SimpleNamespace(agents=agents)):
            with agent_names.pass_scope():
                agent_names.note_row({"name": "orch"})
                for _ in range(4):
                    self.assertEqual(agent_names.resolve("orch", db=object()), "orch")
                self.assertEqual(agents.resolve_name.call_count, 0)
                # un autre nom n'est pas confirmé
                agent_names.resolve("autre", db=object())
                self.assertEqual(agents.resolve_name.call_count, 1)
            # hors passe : la requête revient (un renommage est vu)
            agent_names.resolve("orch", db=object())
            self.assertEqual(agents.resolve_name.call_count, 2)

    def test_hors_passe_note_row_ne_retient_rien(self):
        agent_names.note_row({"name": "orch"})
        self.assertIsNone(agent_names._PASS_PRIMARIES.get())

    def test_propre_au_fil(self):
        seen = []
        with agent_names.pass_scope():
            agent_names.note_row({"name": "orch"})
            thread = threading.Thread(
                target=lambda: seen.append(agent_names._PASS_PRIMARIES.get()))
            thread.start()
            thread.join()
        self.assertEqual(seen, [None])

    def test_registry_get_confirme_le_primaire(self):
        store = types.SimpleNamespace(agents=mock.Mock())
        store.agents.get.return_value = {"name": "orch"}
        with mock.patch.object(registry.storage, "of", return_value=store):
            with agent_names.pass_scope():
                registry.get(object(), "ancien-nom")
                self.assertEqual(agent_names._PASS_PRIMARIES.get(), {"orch"})


_RESOLVE = re.compile(r"SELECT ameesh_resolve_agent_name\(\S+, \S+\) AS name")


class PasseAuReposReelle(PgTestCase):
    """Une passe au repos réelle : ni marqueur de schéma ni résolution du nom."""

    def test_passe_au_repos(self):
        name = "repos8152"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        runner = Runner(self.cfg, self.db)
        runner.poll = 1.0  # sans écoute : une passe par seconde
        lease = registry.claim(self.db, name, runner.runner_id, 180)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        runner.workers[name] = worker
        passes = []
        enough = threading.Event()
        statements = []
        original_pick = AgentWorker._pick_for_turn
        original_trip = db_mod.TRACE.trip

        def pick(self_):
            passes.append(1)
            if len(passes) >= 4:
                enough.set()
            return original_pick(self_)

        def trip(sql=""):
            original_trip(sql)
            if threading.current_thread() is worker:
                statements.append((len(passes), " ".join(str(sql).split())))

        with mock.patch.object(AgentWorker, "_pick_for_turn", pick), \
                mock.patch.object(db_mod.TRACE, "trip", trip):
            worker.start()
            try:
                self.assertTrue(enough.wait(60), "four idle passes expected")
            finally:
                runner.stop.set()
                worker.stopping.set()
                worker.wake.set()
                worker.join(30)
        # après la première passe (admission initiale), sur les passes 2 à 4
        steady = [sql for n, sql in statements if 1 <= n < 4]
        self.assertTrue(steady)
        self.assertLessEqual(
            sum("schema_non_additive_migrations" in sql for sql in steady), 1,
            "the idle pass shares the runner's schema admission (5 s)")
        self.assertEqual([sql for sql in steady if _RESOLVE.search(sql)], [])

    def test_debit_regle_tenu_par_la_boucle_reelle(self):
        """B impl-11 : bail de 3 s renouvelé par le vrai battement, pas réglé à
        100 s : une seule passe en 8 s (avant : une passe par bail au moins)."""
        name = "debit8152"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        cfg = dataclasses.replace(self.cfg, idle_passes_per_s=0.01, lease_ttl=3.0)
        runner = Runner(cfg, self.db)
        runner.poll = 1.0
        runner.listener = object()  # écoute établie : le pas réglé s'applique
        lease = registry.claim(self.db, name, runner.runner_id, 3)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        runner.workers[name] = worker
        passes = []
        original_pick = AgentWorker._pick_for_turn

        def pick(self_):
            passes.append(time.monotonic())
            return original_pick(self_)

        with mock.patch.object(AgentWorker, "_pick_for_turn", pick):
            worker.start()
            try:
                time.sleep(8)
                vivant = registry.get(self.db, name)
            finally:
                runner.stop.set()
                worker.stopping.set()
                worker.wake.set()
                worker.join(30)
                runner.listener = None
        self.assertEqual(len(passes), 1, passes)
        # le bail a bien été renouvelé pendant l'attente (battement)
        self.assertEqual(vivant.get("lease_owner"), runner.runner_id)


if __name__ == "__main__":
    unittest.main()
