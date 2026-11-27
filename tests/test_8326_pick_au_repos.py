# SPDX-License-Identifier: AGPL-3.0-only
"""#8326 — le choix du tour d'un agent au repos ne tient pas l'admission.

Constat IMA (manaty/ameesh#6567, 1.72.0) : sur 31 agents résidents au repos,
chacun passait des minutes dans « choosing turn (accounts, budget, mail) »
alors qu'aucun n'avait de courrier ni de consigne. Le journal d'admission
nommait un détenteur `document` (44,1 s sur 48,5 s, 30 acquisitions, 6,9 s
max) : le détenteur est un tour, pas un pick au repos ; les picks au repos
faisaient la file devant lui.

Témoins de ce module :

* le choix d'un passage au repos dont le sommeil précédent s'est terminé seul
  et dont l'empreinte n'a pas changé est **sauté** : zéro requête quand la
  passe groupée #8166 est vivante, **une** lecture bornée quand elle ne l'est
  pas (exécuteur médié, passe en panne, premier passage) ;
* aucun réveil n'est perdu : NOTIFY pendant le sommeil, réveil programmé dû,
  relance au repos, relance après plafond, travail local, rôle élu, attach,
  changement d'empreinte et filet `IDLE_SAFETY_S` gardent la sélection
  complète ;
* le banc à 31 agents au repos ne prend aucune acquisition d'admission (ou
  une lecture bornée par agent) même quand un détenteur `document` tient la
  connexion ; le choix complet, lui, en prend plusieurs.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import admission_lock, rate_limit as rate_limit_mod, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

#: Un seul état local pour tout le module (le worker n'y cherche qu'une
#: demande d'attach absente) : pas un répertoire par worker et par test.
_STATE = tempfile.mkdtemp(prefix="w8326-tests-")
atexit.register(shutil.rmtree, _STATE, True)


def _runner(*, alive=True, rows=None, gen=0, safety=900.0, nudge=3600.0):
    return types.SimpleNamespace(
        idle_sweep_alive=lambda: alive,
        idle_generation=lambda name: gen,
        idle_row=lambda name: (rows or {}).get(name),
        IDLE_SAFETY_S=safety,
        idle_nudge=nudge,
        stop=threading.Event(),
        draining=threading.Event(),
    )


def _worker(*, row=None, gen=0, alive=True, name="quiet"):
    worker = AgentWorker.__new__(AgentWorker)
    threading.Thread.__init__(worker, name=name)
    worker.name = name
    worker.runner = _runner(alive=alive, rows={name: row} if row is not None else {},
                            gen=gen)
    worker.db = object()
    worker.state_dir = _STATE
    worker.agent = {"releve": "", "last_turn_ts": None}
    worker.wake = threading.Event()
    worker.stopping = threading.Event()
    worker.lease_lost = threading.Event()
    worker.last_activity = time.monotonic()
    worker.nudged = True
    worker.cfg = types.SimpleNamespace(lot_wake=False, lot_wake_margin=60.0)
    worker.rate_limit = ""
    worker.mail_batch = lambda: 30.0
    worker._quiet_eligible = True
    worker._quiet_full_at = time.monotonic()
    worker._quiet_gen = gen
    worker._quiet_sig = None
    worker._quiet_probe_unsupported = False
    worker._quiet_skips = 0
    worker._quiet_logged = False
    return worker


ROW = {"sig": "x", "pending": False, "wake_ts": None}


class SautDuChoixTest(unittest.TestCase):
    def test_passe_groupee_vivante_zero_requete(self):
        worker = _worker(row=dict(ROW))
        with mock.patch.object(registry, "get",
                               side_effect=AssertionError("la base ne doit pas être lue")):
            self.assertIsNone(AgentWorker._pick(worker))
        self.assertEqual(worker._quiet_skips, 1)
        self.assertTrue(worker._quiet_logged)

    def test_sans_passe_groupee_une_seule_lecture(self):
        worker = _worker(alive=False)
        lectures = []

        def lire(names):
            lectures.append(list(names))
            return {"host": "", "agents": {worker.name: dict(ROW)}}

        leases = types.SimpleNamespace(idle_signatures=lire)
        with mock.patch("ameesh.runner.storage.of",
                        return_value=types.SimpleNamespace(leases=leases)), \
                mock.patch.object(registry, "get",
                                  side_effect=AssertionError("pas de sélection complète")):
            worker._quiet_eligible = True
            self.assertIsNone(AgentWorker._pick(worker))
            worker._quiet_eligible = True
            self.assertIsNone(AgentWorker._pick(worker))
        self.assertEqual(len(lectures), 2)  # une lecture bornée par passage
        self.assertEqual(worker._quiet_skips, 2)

    def test_sans_passe_groupee_empreinte_changee_plein_choix(self):
        worker = _worker(alive=False)
        vues = iter([dict(ROW), dict(ROW, sig="y")])
        leases = types.SimpleNamespace(
            idle_signatures=lambda names: {"host": "", "agents": {worker.name: next(vues)}})
        appels = []
        with mock.patch("ameesh.runner.storage.of",
                        return_value=types.SimpleNamespace(leases=leases)), \
                mock.patch.object(registry, "get",
                                  side_effect=lambda db, name: appels.append(name)):
            self.assertIsNone(AgentWorker._pick(worker))
            worker._quiet_eligible = True
            self.assertIsNone(AgentWorker._pick(worker))
        self.assertEqual(appels, ["quiet"])  # le changement relance la sélection
        self.assertIsNone(worker._quiet_sig)

    def test_stockage_sans_empreinte_plein_choix(self):
        from ameesh.mediated_executor.contract import NotSupportedRemotely

        def refuse(names):
            raise NotSupportedRemotely("idle_signatures")

        worker = _worker(alive=False)
        appels = []
        with mock.patch("ameesh.runner.storage.of",
                        return_value=types.SimpleNamespace(
                            leases=types.SimpleNamespace(idle_signatures=refuse))), \
                mock.patch.object(registry, "get",
                                  side_effect=lambda db, name: appels.append(name)):
            self.assertIsNone(AgentWorker._pick(worker))
        self.assertEqual(appels, ["quiet"])
        self.assertTrue(worker._quiet_probe_unsupported)


class PasDeReveilPerduTest(unittest.TestCase):
    """Chaque cas ci-dessous garde la sélection complète."""

    def _full(self, worker):
        appels = []
        with mock.patch.object(registry, "get",
                               side_effect=lambda db, name: appels.append(name)):
            self.assertIsNone(AgentWorker._pick(worker))
        self.assertEqual(appels, [worker.name])

    def test_pas_de_sommeil_precedent(self):
        worker = _worker(row=dict(ROW))
        worker._quiet_eligible = False
        self._full(worker)

    def test_notify_pose(self):
        worker = _worker(row=dict(ROW))
        worker.wake.set()
        self._full(worker)

    def test_travail_local(self):
        worker = _worker(row=dict(ROW))
        worker.suite = "plafond"
        self._full(worker)

    def test_courrier_ou_consigne_en_attente(self):
        self._full(_worker(row=dict(ROW, pending=True)))

    def test_reveil_programme_du(self):
        self._full(_worker(row=dict(ROW, wake_ts=time.time() - 1)))

    def test_empreinte_changee(self):
        worker = _worker(row=dict(ROW), gen=1)
        worker._quiet_gen = 0
        self._full(worker)

    def test_role_elu(self):
        worker = _worker(row=dict(ROW))
        worker.agent = {"releve": "election"}
        self._full(worker)

    def test_relance_au_repos_due(self):
        worker = _worker(row=dict(ROW))
        worker.nudged = False
        worker.runner.idle_nudge = 10.0
        worker.last_activity -= 60.0
        self._full(worker)

    def test_reveil_de_lot_du(self):
        worker = _worker(row=dict(ROW))
        worker.cfg = types.SimpleNamespace(lot_wake=True, lot_wake_margin=60.0)
        worker.last_activity -= 120.0
        self._full(worker)

    def test_relance_apres_plafond_due(self):
        worker = _worker(row=dict(ROW))
        worker.rate_limit = rate_limit_mod.RATE_LIMITED
        worker.retry_remaining = lambda: 0.0
        self._full(worker)

    def test_attach_demande(self):
        worker = _worker(row=dict(ROW))
        worker._attach_pending = lambda: True
        self._full(worker)

    def test_filet_du_choix_complet(self):
        worker = _worker(row=dict(ROW))
        worker._quiet_full_at -= 901.0
        self._full(worker)

    def test_passe_groupee_sans_ligne(self):
        worker = _worker(alive=True)  # aucune ligne pour ce nom
        self._full(worker)

    def test_sommeil_interrompu(self):
        """Un réveil pendant le sommeil interdit le saut du choix suivant."""
        worker = _worker(row=dict(ROW))
        worker._idle_nominal = 30.0
        worker._quiet_gated_sleep = 0.0
        worker.runner.lease_ttl = 180.0
        worker.lease_deadline = time.monotonic() + 180.0
        worker.deadline_passed = lambda: False
        worker.wait_timeout = lambda cap: cap
        resultat = []
        thread = threading.Thread(target=lambda: resultat.append(worker._idle_sleep(5.0)))
        thread.start()
        time.sleep(0.05)
        worker.wake.set()
        thread.join(timeout=5)
        self.assertEqual(resultat, [True])
        self.assertFalse(worker._quiet_eligible)
        self._full(worker)

    def test_sommeil_naturel_autorise_le_saut(self):
        worker = _worker(row=dict(ROW))
        worker._idle_nominal = 0.05
        worker._quiet_gated_sleep = 0.0
        worker.runner.lease_ttl = 180.0
        worker.lease_deadline = time.monotonic() + 180.0
        worker.deadline_passed = lambda: False
        worker.wait_timeout = lambda cap: cap
        self.assertFalse(worker._idle_sleep(0.05))
        self.assertTrue(worker._quiet_eligible)
        with mock.patch.object(registry, "get",
                               side_effect=AssertionError("le saut devait être permis")):
            self.assertIsNone(AgentWorker._pick(worker))

    def test_sommeil_raccourci_par_une_echeance(self):
        worker = _worker(row=dict(ROW))
        worker._idle_nominal = 30.0
        worker._quiet_gated_sleep = 0.0
        worker.runner.lease_ttl = 180.0
        worker.lease_deadline = time.monotonic() + 180.0
        worker.deadline_passed = lambda: False
        worker.wait_timeout = lambda cap: cap
        self.assertFalse(worker._idle_sleep(0.01))
        self.assertFalse(worker._quiet_eligible)

    def test_sommeil_raccourci_par_la_fenetre_de_courrier(self):
        # #8326 : `_until_due` a raccourci le pas (fenêtre de regroupement) ;
        # même à un écart sous la tolérance d'horloge, le choix reste complet.
        worker = _worker(row=dict(ROW))
        worker._idle_nominal = 0.01
        worker._quiet_gated_sleep = 0.0
        worker.runner.lease_ttl = 180.0
        worker.lease_deadline = time.monotonic() + 180.0
        worker.deadline_passed = lambda: False
        worker.wait_timeout = lambda cap: cap
        worker._due_at = time.time() + 0.5
        self.assertLess(worker._until_due(30.0), 30.0)
        self.assertTrue(worker._idle_due_shortened)
        self.assertFalse(worker._idle_sleep(0.01))
        self.assertFalse(worker._quiet_eligible)
        self.assertFalse(worker._idle_due_shortened)


class BancTrenteEtUnAgents(unittest.TestCase):
    """31 agents au repos, un détenteur `document` qui tient l'admission.

    Le détenteur du constat IMA est le tour d'un agent (30 acquisitions,
    44,1 s sur 48,5 s) : le banc le reproduit par 30 acquisitions de 5 ms
    sous le verrou partagé, et compte les acquisitions des choix au repos.
    """

    AGENTS = 31
    DOCUMENT = 30
    HOLD_S = 0.005

    def _banc(self, *, alive=True):
        rows = {"a%02d" % i: dict(ROW) for i in range(self.AGENTS)}
        runner = _runner(alive=alive, rows=rows, gen=0)
        workers = [_worker(row=dict(ROW), name=name, alive=alive)
                   for name in sorted(rows)]
        # les 31 workers partagent l'exécuteur (empreintes communes), pas
        # leur état local : un état par worker, comme en production, sinon la
        # demande d'attach de `try_lock` se contended entre les fils et le
        # banc mesurerait une course de test, pas le choix du tour.
        etat = os.path.join(_STATE, "banc-%s" % ("groupee" if alive else "bornee"))
        for worker in workers:
            worker.state_dir = os.path.join(etat, worker.name)
            os.makedirs(worker.state_dir, exist_ok=True)
        # les 31 workers partagent l'exécuteur (empreintes communes)
        for worker in workers:
            worker.runner = runner
        acquisitions = {"document": 0, "repos": 0}
        verrou = threading.Lock()

        def document():
            for _ in range(self.DOCUMENT):
                with verrou:
                    acquisitions["document"] += 1
                time.sleep(self.HOLD_S)

        resultats = []

        def repos(worker):
            resultats.append(AgentWorker._pick(worker))

        if not alive:
            lectures = []

            def lire(names):
                with verrou:
                    acquisitions["repos"] += 1
                lectures.append(names)
                return {"host": "", "agents": {
                    name: dict(ROW) for name in names}}

            leases = types.SimpleNamespace(idle_signatures=lire)
            patcher = mock.patch("ameesh.runner.storage.of",
                                 return_value=types.SimpleNamespace(leases=leases))
            patcher.start()
            self.addCleanup(patcher.stop)
        # `document` tient la connexion pendant que les 31 choix au repos
        # passent : aucun ne doit l'attendre.
        fil = threading.Thread(target=document)
        threads = [threading.Thread(target=repos, args=(worker,))
                   for worker in workers]
        with mock.patch.object(registry, "get",
                               side_effect=AssertionError("admission au repos")):
            fil.start()
            started = time.monotonic()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            elapsed = time.monotonic() - started
            fil.join(timeout=30)
        self.assertFalse(fil.is_alive())
        self.assertEqual(len(resultats), self.AGENTS)
        self.assertTrue(all(r is None for r in resultats))
        return acquisitions, elapsed, workers

    def test_zero_admission_avec_passe_groupee(self):
        acquisitions, elapsed, workers = self._banc(alive=True)
        self.assertEqual(acquisitions["document"], self.DOCUMENT)
        self.assertEqual(acquisitions["repos"], 0,
                         "un choix au repos a pris l'admission")
        self.assertEqual(sum(w._quiet_skips for w in workers), self.AGENTS)
        self.assertLess(elapsed, 2.0)

    def test_une_lecture_bornee_sans_passe_groupee(self):
        acquisitions, elapsed, workers = self._banc(alive=False)
        self.assertEqual(acquisitions["document"], self.DOCUMENT)
        # Une lecture bornée par agent et par passage, jamais la sélection.
        self.assertEqual(acquisitions["repos"], self.AGENTS)
        self.assertEqual(sum(w._quiet_skips for w in workers), self.AGENTS)
        self.assertLess(elapsed, 5.0)


class ChoixAuReposReel(PgTestCase):
    """Le saut sur la vraie base : zéro requête avec l'empreinte groupée,
    une seule sans elle ; la sélection complète en fait davantage."""

    NAME = "quiet8326"

    def setUp(self):
        super().setUp()
        import os
        self.cwd = os.path.join(self.tmp, "work", self.NAME)
        os.makedirs(self.cwd, exist_ok=True)
        self.register(self.NAME, "claude", cwd=self.cwd)
        self.runner = Runner(self.cfg, self.db)
        lease = registry.claim(self.db, self.NAME, self.runner.runner_id, 180)
        self.worker = AgentWorker(self.runner, registry.get(self.db, self.NAME), lease)
        self.runner.workers[self.NAME] = self.worker
        self.worker._quiet_eligible = True
        self.worker._quiet_full_at = time.monotonic()
        self.worker.agent = registry.get(self.db, self.NAME) or self.worker.agent

    def _compter(self):
        return [mock.patch.object(self.db, "query", wraps=self.db.query),
                mock.patch.object(self.db, "query_batch", wraps=self.db.query_batch),
                mock.patch.object(self.db, "execute", wraps=self.db.execute)]

    def test_zero_requete_avec_la_passe_groupee(self):
        self.runner.idle_sweep_once()
        self.worker._quiet_gen = self.runner.idle_generation(self.NAME)
        self.assertIsNotNone(self.worker._quiet_gen)
        patchers = self._compter()
        mocks = [p.start() for p in patchers]
        try:
            self.assertIsNone(AgentWorker._pick(self.worker))
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(sum(m.call_count for m in mocks), 0)
        self.assertGreaterEqual(self.worker._quiet_skips, 1)

    def test_une_seule_lecture_sans_la_passe_groupee(self):
        self.worker._quiet_gen = None
        # Comme un exécuteur en régime : la sonde de schéma de `wake_at` est
        # déjà chaude (une seule fois par processus, pas par lecture).
        from ameesh import storage
        storage.of(self.db).leases.idle_signatures([self.NAME])
        patchers = self._compter()
        mocks = [p.start() for p in patchers]
        try:
            self.assertIsNone(AgentWorker._pick(self.worker))
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(sum(m.call_count for m in mocks), 1)
        self.assertGreaterEqual(self.worker._quiet_skips, 1)


class SansRegistreNiPasse(unittest.TestCase):
    def test_executeur_medie_sans_empreinte(self):
        """Aucune régression pour une doublure sans stockage : plein choix."""
        worker = _worker(alive=False)
        worker._quiet_probe_unsupported = True
        appels = []
        with mock.patch.object(registry, "get",
                               side_effect=lambda db, name: appels.append(name)):
            self.assertIsNone(AgentWorker._pick(worker))
        self.assertEqual(appels, [worker.name])


if __name__ == "__main__":
    unittest.main()
