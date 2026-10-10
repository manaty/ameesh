# SPDX-License-Identifier: AGPL-3.0-only
"""L72 : un exécuteur survit à une panne passagère de la base.

Constat du 2026-10-10 : délai de connexion dépassé vers une base distante, et
chaque exécuteur plantait (fil du worker ou passe principale), tuant les tours
en cours et laissant des agents `blocked` sous un message trompeur (« ameesh
migrate » ?). Ici la panne est simulée par une enveloppe de la connexion de
test qui lève `Unavailable` tant qu'elle est « coupée ».
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from ameesh import account_turn, accounts, db as db_mod, registry
from ameesh.runner import AgentWorker, Reprise, Runner

from .support import PgTestCase

TIMEOUT = ('psql : psql: error: connection to server at "10.0.0.1", port 5432 '
           'failed: timeout expired')


class PanneDb:
    """Connexion de test coupable : `coupe` lève `Unavailable` sur chaque
    appel (ou sur ceux dont le SQL contient `motif`)."""

    def __init__(self, db):
        self._db = db
        self.cfg = db.cfg
        self.name = db.name
        self.coupe = False
        self.motif: str | None = None
        self.refus = 0

    def _garde(self, sql: str = "") -> None:
        if self.coupe and (self.motif is None or self.motif in sql):
            self.refus += 1
            raise db_mod.Unavailable(TIMEOUT)

    def query(self, sql, params=()):
        self._garde(sql)
        return self._db.query(sql, params)

    def execute(self, sql, params=()):
        self._garde(sql)
        return self._db.execute(sql, params)

    def script(self, sql):
        self._garde(sql)
        return self._db.script(sql)

    def transaction(self):
        self._garde()
        return self._db.transaction()

    def listen(self, channels):
        self._garde()
        return self._db.listen(channels)

    def ping(self):
        self._garde()
        return self._db.ping()

    def close(self):
        pass


def attendre(cond, delai=10.0, pas=0.02) -> bool:
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(pas)
    return cond()


class PanneDeBaseTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(Reprise, "MINIMUM", 0.05)
        patch.start()
        self.addCleanup(patch.stop)

    def _worker(self, name="panne", prompt=None, ttl=3600.0, **cfg):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd, **({"prompt": prompt} if prompt else {}))
        self.pdb = PanneDb(self.db)
        conf = dataclasses.replace(self.cfg, budget_usd_per_hour=0.0, poll=1.0,
                                   db_retry_max=0.2, **cfg)
        runner = Runner(conf, self.pdb, once=False)
        runner.lease_ttl = ttl
        lease = registry.claim(self.db, name, runner.runner_id, ttl)
        self.assertTrue(lease)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    def _lance(self, worker) -> threading.Thread:
        self.addCleanup(lambda: (worker.stopping.set(), worker.wake.set(),
                                 worker.join(timeout=5)))
        worker.start()
        return worker

    # -- panne pendant l'attente --------------------------------------------
    def test_panne_pendant_l_attente_le_worker_survit_et_reprend(self):
        worker = self._worker()
        self.pdb.coupe = True
        self._lance(worker)
        self.assertTrue(attendre(lambda: worker._reprise.echecs >= 3),
                        "le worker doit réessayer, avec attente croissante")
        self.assertTrue(worker.is_alive(), "une panne de base a tué le worker")
        self.assertFalse(worker.lease_lost.is_set())
        self.pdb.coupe = False
        worker.wake.set()
        self.assertTrue(attendre(lambda: not worker._reprise.en_panne),
                        "la base revenue, le worker doit reprendre de lui-même")
        self.assertTrue(worker.is_alive())

    def test_statut_base_injoignable_pose_puis_leve_au_retour(self):
        worker = self._worker()
        # panne partielle : le choix du tour échoue, l'écriture du statut passe
        self.pdb.motif = "agent_mailbox"
        self.pdb.coupe = True
        self._lance(worker)
        self.assertTrue(attendre(
            lambda: registry.get(self.db, "panne")["status_text"] == "base injoignable"))
        row = registry.get(self.db, "panne")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("délai de connexion dépassé", row["last_error"])
        self.assertNotIn("migrate", row["last_error"])
        self.pdb.coupe = False
        worker.wake.set()
        self.assertTrue(attendre(
            lambda: registry.get(self.db, "panne")["status"] != "blocked"),
            "le blocage « base injoignable » doit se lever sans reprise manuelle")
        self.assertEqual(registry.get(self.db, "panne")["status_text"], "")

    def test_statut_herite_d_un_executeur_precedent_est_leve(self):
        worker = self._worker()
        registry.set_marked_block(self.db, "panne", worker.runner.runner_id, worker.epoch,
                                  "base injoignable", "base injoignable : ancien", "base injoignable")
        self.assertIsNone(worker.pick())
        self.assertNotEqual(registry.get(self.db, "panne")["status"], "blocked")

    def test_consigne_consommee_puis_panne_est_remise_en_attente(self):
        worker = self._worker(prompt="fais le lot")
        appels = []

        def tour(spec):
            appels.append(spec)
            if len(appels) == 1:
                raise db_mod.Unavailable(TIMEOUT)  # panne avant le lancement
            worker.stopping.set()
            return True

        with mock.patch.object(worker, "run_turn", side_effect=tour):
            self._lance(worker)
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(appels), 2, "la consigne remise doit être reprise")
        self.assertEqual(appels[1]["prompt"], "fais le lot")
        self.assertEqual(worker.fast_failures, 0, "une panne n'est pas un échec de tour")

    # -- panne pendant le renouvellement ------------------------------------
    def test_panne_pendant_le_renouvellement_ne_tue_pas_le_tour(self):
        worker = self._worker()
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        worker.proc, worker.pgid = proc, proc.pid
        self.pdb.coupe = True
        for _ in range(AgentWorker.MAX_RENEW_FAILURES + 2):
            self.assertTrue(worker.renew(), "échec non définitif : le bail court encore")
        self.assertIsNone(proc.poll(), "le harnais a été tué pendant une panne courte")
        self.assertFalse(worker.lease_lost.is_set())
        avant = worker.lease_deadline
        self.pdb.coupe = False
        time.sleep(0.01)
        self.assertTrue(worker.renew())
        self.assertEqual(worker.renew_failures, 0)
        self.assertGreater(worker.lease_deadline, avant)
        self.assertIsNone(proc.poll())

    def test_bail_echu_pendant_la_panne_arrete_le_harnais(self):
        """L'invariant de bail tient : sans renouvellement avant l'échéance, le
        harnais est arrêté, comme avant L72."""
        worker = self._worker()
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        worker.proc, worker.pgid = proc, proc.pid
        self.pdb.coupe = True
        self.assertTrue(worker.renew())
        worker.lease_deadline = time.time() - 0.01
        self.assertFalse(worker.renew())
        self.assertTrue(worker.lease_lost.is_set())
        self.assertTrue(attendre(lambda: proc.poll() is not None, 5))

    # -- panne pendant la garde de budget -----------------------------------
    def test_panne_pendant_la_garde_de_budget_n_est_pas_une_pause(self):
        worker = self._worker()
        worker.runner.budget_usd_per_hour = 10.0
        profil = mock.Mock(name="profil")
        with mock.patch.object(accounts, "profiles", return_value=[profil]), \
                mock.patch.object(accounts, "choose",
                                  side_effect=db_mod.Unavailable(TIMEOUT)):
            with self.assertRaises(db_mod.Unavailable):
                worker.budget_ok()
        row = registry.get(self.db, "panne")
        self.assertFalse((row["status_text"] or "").startswith("budget"))

    def test_erreur_sql_des_comptes_sans_migrate_trompeur(self):
        worker = self._worker()
        profil = mock.Mock(name="profil")
        with mock.patch.object(accounts, "profiles", return_value=[profil]), \
                mock.patch.object(accounts, "choose",
                                  side_effect=db_mod.DbError("psql : ERROR: deadlock detected")):
            raison = account_turn.choose(worker, mock.Mock())
        self.assertIn("état des comptes indisponible", raison)
        self.assertNotIn("migrate", raison)
        with mock.patch.object(accounts, "profiles", return_value=[profil]), \
                mock.patch.object(accounts, "choose", side_effect=db_mod.DbError(
                    'psql : ERROR: relation "account_state" does not exist')):
            self.assertIn("ameesh migrate", account_turn.choose(worker, mock.Mock()))

    def test_pause_budget_levee_quand_la_garde_laisse_passer(self):
        worker = self._worker()
        worker.runner.budget_usd_per_hour = 10.0
        registry.set_status(self.db, "panne", "blocked",
                            status_text="budget : état des comptes indisponible (x)")
        worker.agent = registry.get(self.db, "panne")
        with mock.patch.object(worker, "budget_reason", return_value=""):
            self.assertTrue(worker.budget_ok())
        row = registry.get(self.db, "panne")
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["status_text"], "")

    def test_comptabilite_illisible_a_la_creation_ne_suspend_pas_pour_toujours(self):
        self.register("compta", "claude", cwd=self.tmp)
        pdb = PanneDb(self.db)
        runner = Runner(dataclasses.replace(self.cfg, budget_usd_per_hour=10.0), pdb)
        lease = registry.claim(self.db, "compta", runner.runner_id, 3600)
        pdb.coupe = True
        worker = AgentWorker(runner, registry.get(self.db, "compta"), lease)
        self.assertTrue(worker._compta_en_echec)  # doute : suspension
        pdb.coupe = False
        worker._compta_repare()
        self.assertFalse(worker._compta_en_echec, "aucun marqueur : la suspension se lève")

    # -- passe principale et écoute -----------------------------------------
    def test_passe_principale_survit_a_la_panne(self):
        self.pdb = PanneDb(self.db)
        runner = Runner(dataclasses.replace(self.cfg, db_retry_max=0.2), self.pdb)
        passes = []

        def passe():
            passes.append(1)
            if len(passes) <= 3:
                raise db_mod.Unavailable(TIMEOUT)
            runner.stop.set()

        with mock.patch.object(runner, "sweep", side_effect=passe), \
                mock.patch.object(runner, "_listen_loop"), \
                mock.patch.object(runner, "start_canon_sync"), \
                mock.patch.object(runner, "start_balance_poll"), \
                mock.patch.object(runner, "start_resource_poll"):
            self.assertEqual(runner.run(), 0)
        self.assertEqual(len(passes), 4)

    def test_ecoute_reessaie_tant_que_la_base_ne_repond_pas(self):
        self.pdb = PanneDb(self.db)
        runner = Runner(dataclasses.replace(self.cfg, db_retry_max=0.2), self.pdb)
        pings = []

        def ping():
            pings.append(1)
            if len(pings) < 3:
                raise db_mod.Unavailable(TIMEOUT)

        with mock.patch("ameesh.storage.postgres.wakeups.db_mod.listener",
                        return_value=None), \
                mock.patch.object(self.pdb, "ping", side_effect=ping):
            runner._listen_loop()  # rend la main : pilote sans écoute
        self.assertEqual(len(pings), 3)

    def test_explain_delai_de_connexion(self):
        texte = db_mod.explain(db_mod.Unavailable(TIMEOUT))
        self.assertIn("délai de connexion dépassé", texte)
        self.assertIn("AMEESH_CONNECT_TIMEOUT", texte)
        self.assertNotIn("migrate", texte)


class ReconnexionPsycopgTest(PgTestCase):
    def test_connexion_cassee_rouverte(self):
        try:
            drv = db_mod.connect(self.cfg, driver="psycopg")
        except db_mod.Unavailable:
            raise unittest.SkipTest("psycopg absent")
        try:
            drv.conn.close()  # comme après une coupure réseau
            self.assertEqual(drv.query("SELECT 1 AS un")[0]["un"], 1)
        finally:
            drv.close()


if __name__ == "__main__":
    unittest.main()
