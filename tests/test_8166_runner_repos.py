# SPDX-License-Identifier: AGPL-3.0-only
"""#8166 — exécuteur au repos, tranche 2 (manaty/ameesh#6392, suite de #8152).

Constat IMA (128 agents au repos) : chaque agent publiait son aperçu toutes
les 15 s (deux appels git et une écriture), renouvelait seul son bail (une
connexion neuve, un contrôle de schéma et un UPDATE au tiers du bail), et
refaisait sa passe complète au pas de repos.

Témoins :

* aperçu : au repos, ni git ni écriture si HEAD, l'index et le journal
  d'événements n'ont pas bougé ; confirmation au plus tard à `REFRESH` ;
  `STALE_AFTER` couvre cette confirmation ; pendant un tour, git à chaque
  capture comme avant ;
* battement groupé : un seul renouvellement (une connexion, un contrôle, un
  UPDATE) pour les baux des workers au repos, avec les gardes de `renew`
  (époque, détenteur, échéance sous verrou, jamais raccourcie) ; le fil du
  worker s'efface tant qu'il est couvert et reprend au tiers du bail restant ;
* passe de repos groupée : une requête pour tous les workers ; un worker sans
  travail en attente, au rôle non élu, ne refait sa passe complète que si
  son empreinte change (registre, courrier actif, réveil programmé, cycle de
  vie des hôtes), à une échéance connue ou au filet `IDLE_SAFETY_S`.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import agent_peek as peek, config, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


def _git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo(root):
    _git(root, "init", "-b", "work")
    Path(root, "f.txt").write_text("a\n")
    _git(root, "add", "f.txt")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.org", "commit", "-m", "base")


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class ApercuAuRepos(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peek8166-")
        self.addCleanup(__import__("shutil").rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        _repo(self.work)
        self.cfg = config.Config(state_dir=self.state)
        self.active = False
        self.clock = _Clock()
        clock = mock.patch.object(peek.time, "monotonic", self.clock.monotonic)
        clock.start()
        self.addCleanup(clock.stop)

    def _publisher(self):
        publisher = peek.Publisher.__new__(peek.Publisher)
        publisher.cfg, publisher.agent = self.cfg, "w"
        publisher.cwd, publisher.reader = (lambda: self.work), (lambda: None)
        publisher.active = lambda: self.active
        publisher._workspace = publisher._workspace_mark = None
        publisher._events = publisher._events_mark = None
        return publisher

    def _captures(self, publisher, n):
        with mock.patch.object(peek, "workspace", wraps=peek.workspace) as ws:
            for _ in range(n):
                publisher._capture(self.cfg)
                self.clock.t += peek.INTERVAL
        return ws.call_count

    def test_au_repos_git_une_fois(self):
        publisher = self._publisher()
        self.assertEqual(self._captures(publisher, 10), 1)

    def test_pendant_un_tour_git_a_chaque_capture(self):
        self.active = True
        self.assertEqual(self._captures(self._publisher(), 4), 4)

    def test_index_ou_commit_relance_git(self):
        publisher = self._publisher()
        self._captures(publisher, 2)
        Path(self.work, "f.txt").write_text("b\n")
        _git(self.work, "add", "f.txt")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertIn("f.txt", publisher._workspace["diff_stat"])
        _git(self.work, "-c", "user.name=T", "-c", "user.email=t@example.org",
             "commit", "-m", "suite")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertEqual(publisher._workspace["diff_stat"], "")

    def test_changement_de_branche(self):
        publisher = self._publisher()
        self._captures(publisher, 1)
        _git(self.work, "checkout", "-q", "-b", "autre")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertEqual(publisher._workspace["branch"], "autre")

    def test_worktree(self):
        tree = os.path.join(self.root, "tree")
        _git(self.work, "worktree", "add", "-q", "-b", "lot", tree)
        self.work = tree
        publisher = self._publisher()
        self.assertEqual(self._captures(publisher, 3), 1)
        Path(tree, "f.txt").write_text("c\n")
        _git(tree, "add", "f.txt")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertIn("f.txt", publisher._workspace["diff_stat"])

    def test_au_plus_tard_a_refresh(self):
        publisher = self._publisher()
        self._captures(publisher, 1)
        self.clock.t += peek.REFRESH
        self.assertEqual(self._captures(publisher, 1), 1)

    def test_hors_depot(self):
        self.work = os.path.join(self.root, "nu")
        os.makedirs(self.work)
        publisher = self._publisher()
        self.assertEqual(self._captures(publisher, 5), 1)
        self.assertFalse(publisher._workspace["available"])

    def test_journal_relu_seulement_s_il_bouge(self):
        publisher = self._publisher()
        os.makedirs(self.cfg.agent_dir("w"), exist_ok=True)
        journal = os.path.join(self.cfg.agent_dir("w"), "events.jsonl")
        Path(journal).write_text("{}\n")
        with mock.patch.object(peek, "selected_events", return_value=[]) as lu:
            for _ in range(3):
                publisher._capture(self.cfg)
            self.assertEqual(lu.call_count, 1)
            with open(journal, "a") as stream:
                stream.write("{}\n")
            publisher._capture(self.cfg)
            self.assertEqual(lu.call_count, 2)

    def test_fraicheur_couvre_la_confirmation(self):
        self.assertGreater(peek.STALE_AFTER, peek.REFRESH + peek.INTERVAL)


class PublicationAuRepos(unittest.TestCase):
    """Une écriture seulement si la capture change, ou à `REFRESH`."""

    def _run(self, captures, *, refresh):
        published = []
        operations = mock.Mock()
        operations.publish_peek.side_effect = lambda *a: published.append(a[3]) or True
        payloads = iter(captures)
        done = threading.Event()

        def capture(self_, cfg):
            try:
                return next(payloads)
            except StopIteration:
                done.set()
                self_.stop.wait(5)
                return captures[-1]

        with mock.patch.object(peek, "INTERVAL", 0.001), \
                mock.patch.object(peek, "REFRESH", refresh), \
                mock.patch.object(peek.Publisher, "_capture", capture), \
                mock.patch.object(peek.db_mod, "connect", return_value=mock.Mock()), \
                mock.patch.object(peek.storage, "of",
                                  return_value=types.SimpleNamespace(operations=operations)):
            publisher = peek.Publisher(config.Config(), agent="w", owner="r", epoch=1,
                                       cwd=lambda: "", reader=lambda: None,
                                       active=lambda: False)
            self.assertTrue(done.wait(10))
            n = len(published)
            publisher.close()
            publisher.thread.join(timeout=10)
        return n, published

    def test_identique_non_reecrit(self):
        same = {"workspace": {}, "events": []}
        n, _ = self._run([same] * 6, refresh=3600)
        self.assertEqual(n, 1)

    def test_changement_ecrit(self):
        a, b = {"events": [1]}, {"events": [2]}
        n, published = self._run([a, a, b, b, a], refresh=3600)
        self.assertEqual(published[:n], [a, b, a])

    def test_fermeture_ecrit_la_derniere_capture(self):
        same = {"events": []}
        _, published = self._run([same] * 3, refresh=3600)
        self.assertEqual(len(published), 2)  # la première, puis celle de fermeture



class RenouvellementGroupeEnBase(PgTestCase):
    def _claim(self, name, ttl=60):
        self.register(name, "claude")
        return registry.claim(self.db, name, "runner-g", ttl)

    def _expires(self, name):
        return self.db.query("SELECT extract(epoch from lease_expires_at)::float8 AS echeance"
                             " FROM agent_registry WHERE name=%s", (name,))[0]["echeance"]

    def test_gardes_de_renew(self):
        leases = {n: self._claim(n) for n in ("g1", "g2", "g3", "g4")}
        epoch = {n: int(lease["lease_epoch"]) for n, lease in leases.items()}
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 s'"
                        " WHERE name='g3'")
        avant = self._expires("g4")
        got = storage.of(self.db).leases.renew_many("runner-g", [
            ("g1", epoch["g1"], 120.0), ("g2", epoch["g2"] + 1, 120.0),
            ("g3", epoch["g3"], 120.0), ("g4", epoch["g4"], 1.0)])
        self.assertEqual(set(got), {"g1", "g4"})
        self.assertGreater(got["g1"], avant + 30)
        # jamais raccourci : un ttl plus court garde l'échéance tenue
        self.assertGreaterEqual(got["g4"], avant)
        self.assertEqual(storage.of(self.db).leases.renew_many("autre", [
            ("g1", epoch["g1"], 120.0)]), {})
        self.assertEqual(storage.of(self.db).leases.renew_many("runner-g", []), {})


class _W:
    """Worker minimal pour `group_beat_once`."""

    def __init__(self, name, *, resting=True, reste=50.0, ttl=60.0, epoch=1):
        self.name, self._resting, self.lease_ttl, self.epoch = name, resting, ttl, epoch
        self.clock = _Clock()
        self.clock.time = lambda: self.clock.t
        self.lease_deadline = self.clock.t + reste
        self.lease_held_until = 0.0
        self.lease_lost = threading.Event()
        self._renew_lock = threading.Lock()
        self.renewals = []

    def call_budget(self):
        return 5.0

    def _held_until(self):
        return 42.0


class BattementGroupe(unittest.TestCase):
    def _runner(self, workers):
        runner = Runner.__new__(Runner)
        runner.lock = threading.Lock()
        runner.workers = {w.name: w for w in workers}
        runner.runner_id = "r"
        runner.db = types.SimpleNamespace(cfg=config.Config())
        return runner

    def _once(self, workers, answer):
        calls = []

        def renew_many(owner, leases):
            calls.append((owner, sorted(n for n, _, _ in leases)))
            return answer(leases) if callable(answer) else answer

        leases = types.SimpleNamespace(renew_many=renew_many)
        with mock.patch("ameesh.runner.db_mod.connect", return_value=mock.Mock()), \
                mock.patch("ameesh.runner.migrations.require_compatible_writer") as schema, \
                mock.patch("ameesh.runner.storage.of",
                           return_value=types.SimpleNamespace(leases=leases)):
            tour = self._runner(workers).group_beat_once()
        return tour, calls, schema.call_count

    def test_seuls_les_baux_au_repos_entames_d_un_tiers(self):
        dus = [_W("a", reste=30), _W("b", reste=39.9)]
        autres = [_W("c", reste=50), _W("d", resting=False, reste=10), _W("e", reste=10)]
        autres[2].lease_lost.set()
        tour, calls, schema = self._once(dus + autres, {})
        self.assertEqual(calls, [("r", ["a", "b"])])
        self.assertEqual(schema, 1)  # un contrôle pour tout le lot
        self.assertEqual(tour, 10.0)  # un sixième du plus court bail tenu

    def test_rien_d_echu_aucune_requete(self):
        tour, calls, _ = self._once([_W("a", reste=55), _W("b", resting=False, ttl=12)], {})
        self.assertEqual(calls, [])
        self.assertEqual(tour, 2.0)  # le bail en tour compte aussi
        self.assertEqual(self._once([], {})[0], Runner.GROUP_BEAT_IDLE_S)

    def test_application_du_retour(self):
        a, b, c, d = _W("a", reste=20), _W("b", reste=20), _W("c", reste=20), _W("d", reste=20)
        base = a.clock.t

        def answer(leases):
            c.epoch = 2  # bail repris entre l'appel et la réponse
            d.lease_lost.set()
            return {"a": base + 60, "c": base + 60, "d": base + 60, "b": None}

        b_avant = b.lease_deadline
        self._once([a, b, c, d], answer)
        self.assertEqual(a.lease_deadline, base + 60)
        self.assertEqual(a.lease_held_until, 42.0)
        self.assertEqual(b.lease_deadline, b_avant)  # absent : rien n'est déclaré perdu
        self.assertFalse(b.lease_lost.is_set())
        self.assertEqual(c.lease_deadline, base + 20)
        self.assertEqual(d.lease_deadline, base + 20)

    def test_jamais_raccourci(self):
        a = _W("a", reste=30)
        self._once([a], {"a": a.clock.t + 10})
        self.assertEqual(a.lease_deadline, a.clock.t + 30)

    def test_panne_sans_effet(self):
        a = _W("a", reste=30)

        def answer(leases):
            raise OSError("base")

        self._once([a], answer)
        self.assertEqual(a.lease_deadline, a.clock.t + 30)
        self.assertEqual(a.lease_held_until, 42.0)  # l'appel a pu valider

    def test_executeur_medie_sans_battement_groupe(self):
        runner = self._runner([])
        runner.db = types.SimpleNamespace(cfg=types.SimpleNamespace(backend="mediated"))
        with mock.patch("ameesh.runner.storage.is_remote", return_value=False), \
                mock.patch("ameesh.runner.threading.Thread") as thread:
            runner.start_group_beat()
        thread.assert_not_called()
        self.assertFalse(runner.group_beat_alive())


class FilDuWorkerCouvert(unittest.TestCase):
    TTL = 60.0

    def _worker(self, *, resting=True, reste=50.0, vivant=True):
        worker = AgentWorker.__new__(AgentWorker)
        worker.clock = _Clock()
        worker.clock.time = lambda: worker.clock.t
        worker._resting = resting
        worker.lease_deadline = worker.clock.t + reste
        worker.runner = types.SimpleNamespace(group_beat_alive=lambda: vivant)
        patcher = mock.patch.object(AgentWorker, "lease_ttl",
                                    new_callable=mock.PropertyMock, return_value=self.TTL)
        patcher.start()
        self.addCleanup(patcher.stop)
        return worker

    def test_couvert_au_repos(self):
        self.assertAlmostEqual(self._worker()._group_beat_margin(), 30.0)

    def test_reprend_au_tiers_restant(self):
        self.assertIsNone(self._worker(reste=20.0)._group_beat_margin())

    def test_pas_couvert_en_tour_ou_sans_battement(self):
        self.assertIsNone(self._worker(resting=False)._group_beat_margin())
        self.assertIsNone(self._worker(vivant=False)._group_beat_margin())
        worker = self._worker()
        worker.runner = types.SimpleNamespace()
        self.assertIsNone(worker._group_beat_margin())



class BattementGroupeReel(PgTestCase):
    """Trois workers au repos, bail de 3 s : le battement groupé les tient
    seul, sans renouvellement par worker."""

    def test_baux_tenus_par_le_battement_groupe(self):
        cfg = dataclasses.replace(self.cfg, idle_passes_per_s=0.01, lease_ttl=3.0)
        runner = Runner(cfg, self.db)
        runner.poll = 1.0
        runner.listener = object()  # écoute établie : pas de repos long
        names = ["beat8166-%d" % i for i in range(3)]
        for name in names:
            cwd = os.path.join(self.tmp, "work", name)
            os.makedirs(cwd, exist_ok=True)
            self.register(name, "claude", cwd=cwd)
        par_worker, groupes = [], []
        original_call = AgentWorker._renew_call
        original_many = type(storage.of(self.db).leases).renew_many

        def renew_call(self_):
            par_worker.append((self_.name, self_._resting))
            return original_call(self_)

        def renew_many(self_, owner, leases):
            groupes.append(len(leases))
            return original_many(self_, owner, leases)

        with mock.patch.object(AgentWorker, "_renew_call", renew_call), \
                mock.patch.object(type(storage.of(self.db).leases), "renew_many", renew_many):
            runner.start_group_beat()
            workers = []
            for name in names:  # prise et départ aussitôt : bail de 3 s
                lease = registry.claim(self.db, name, runner.runner_id, 3)
                worker = AgentWorker(runner, registry.get(self.db, name), lease)
                runner.workers[name] = worker
                workers.append(worker)
                worker.start()
            try:
                deadline = time.monotonic() + 60
                while not all(getattr(w, "_resting", False) for w in workers):
                    self.assertLess(time.monotonic(), deadline, "workers never rested")
                    time.sleep(0.05)
                debut = len(par_worker)
                time.sleep(8)  # plus de deux baux
                au_repos = par_worker[debut:]
                vivants = [registry.get(self.db, w.name) for w in workers]
            finally:
                runner.stop.set()
                for worker in workers:
                    worker.stopping.set()
                    worker.wake.set()
                for worker in workers:
                    worker.join(30)
        self.assertEqual(au_repos, [], "resting workers renew through the group beat")
        self.assertGreaterEqual(len(groupes), 3)
        for worker, row in zip(workers, vivants):
            self.assertFalse(worker.lease_lost.is_set(), worker.name)
            self.assertEqual(row.get("lease_owner"), runner.runner_id)



class _Sweep:
    """Worker minimal pour `idle_sweep_once`."""

    def __init__(self, name, *, gated=True, mark=0):
        self.name, self._idle_gated, self._idle_mark = name, gated, mark
        self.wake = threading.Event()


class PasseGroupee(unittest.TestCase):
    def _runner(self, workers, answers):
        runner = Runner.__new__(Runner)
        runner.lock = threading.Lock()
        runner.workers = {w.name: w for w in workers}
        runner.db = object()
        leases = types.SimpleNamespace(idle_signatures=mock.Mock(side_effect=answers))
        patcher = mock.patch("ameesh.runner.storage.of",
                             return_value=types.SimpleNamespace(leases=leases))
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner, leases.idle_signatures

    @staticmethod
    def _lu(host="h", **sigs):
        return {"host": host, "agents": {n: {"sig": v, "pending": False, "wake_ts": None}
                                         for n, v in sigs.items()}}

    def test_une_requete_pour_tous(self):
        workers = [_Sweep("a"), _Sweep("b"), _Sweep("c")]
        runner, query = self._runner(workers, [self._lu(a="1", b="1", c="1")])
        runner.idle_sweep_once()
        self.assertEqual(query.call_count, 1)
        self.assertEqual(sorted(query.call_args.args[0]), ["a", "b", "c"])
        self.assertEqual(runner.idle_generation("a"), 0)
        self.assertTrue(runner.idle_sweep_alive())
        self.assertFalse(any(w.wake.is_set() for w in workers))  # première lecture

    def test_reveille_seulement_ce_qui_change(self):
        a, b, c = _Sweep("a"), _Sweep("b"), _Sweep("c", gated=False)
        runner, _ = self._runner([a, b, c], [self._lu(a="1", b="1", c="1"),
                                             self._lu(a="2", b="1", c="2")])
        runner.idle_sweep_once()
        runner.idle_sweep_once()
        self.assertEqual((runner.idle_generation("a"), runner.idle_generation("b")), (1, 0))
        self.assertTrue(a.wake.is_set())
        self.assertFalse(b.wake.is_set())
        self.assertFalse(c.wake.is_set())  # en tour ou au pas ordinaire : pas concerné

    def test_cycle_de_vie_des_hotes_reveille_tout(self):
        a, b = _Sweep("a"), _Sweep("b")
        runner, _ = self._runner([a, b], [self._lu(a="1", b="1"),
                                          self._lu(host="h2", a="1", b="1")])
        runner.idle_sweep_once()
        runner.idle_sweep_once()
        self.assertTrue(a.wake.is_set() and b.wake.is_set())

    def test_changement_pendant_la_passe(self):
        # marque prise avant un changement déjà vu : réveil au balayage suivant
        a = _Sweep("a", mark=0)
        runner, _ = self._runner([a], [self._lu(a="1"), self._lu(a="2"), self._lu(a="2")])
        runner.idle_sweep_once()
        a._idle_gated = False
        runner.idle_sweep_once()  # gen 1, le worker est en passe
        a._idle_gated = True
        runner.idle_sweep_once()
        self.assertTrue(a.wake.is_set())

    def test_stockage_sans_passe_groupee(self):
        runner, _ = self._runner([_Sweep("a")], [None])
        runner.idle_sweep_once()
        self.assertFalse(runner.idle_sweep_alive())

    def test_executeur_medie(self):
        runner = Runner.__new__(Runner)
        runner.db = types.SimpleNamespace(cfg=types.SimpleNamespace(backend="mediated"))
        with mock.patch("ameesh.runner.threading.Thread") as thread:
            runner.start_idle_sweep()
        thread.assert_not_called()


class PasDuWorkerCouvert(unittest.TestCase):
    def _worker(self, *, row=None, gen=0, mark=0, alive=True, releve="", nudged=True,
                lot_wake=False):
        runner = types.SimpleNamespace(
            idle_sweep_alive=lambda: alive, idle_generation=lambda n: gen,
            idle_row=lambda n: row, IDLE_SAFETY_S=900.0, idle_nudge=3600.0)
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker, name="a")
        worker.runner, worker.agent = runner, {"releve": releve}
        worker._idle_mark, worker.nudged = mark, nudged
        worker.last_activity = time.monotonic()
        worker.cfg = types.SimpleNamespace(lot_wake=lot_wake, lot_wake_margin=60.0)
        worker.mail_batch = lambda: 30.0
        worker._idle_wait = lambda: 45.0
        return worker

    ROW = {"sig": "x", "pending": False, "wake_ts": None}

    def test_couvert_filet(self):
        worker = self._worker(row=dict(self.ROW))
        self.assertEqual(worker._idle_pace(), 900.0)
        self.assertTrue(worker._idle_gated)

    def test_pas_ordinaire(self):
        cas = {"sans passe": dict(alive=False, row=dict(self.ROW)),
               "pas lu": dict(row=None),
               "travail en attente": dict(row=dict(self.ROW, pending=True)),
               "changé pendant la passe": dict(row=dict(self.ROW), gen=1),
               "marque absente": dict(row=dict(self.ROW), mark=None),
               "rôle élu": dict(row=dict(self.ROW), releve="election")}
        for nom, kw in cas.items():
            worker = self._worker(**kw)
            self.assertEqual(worker._idle_pace(), 45.0, nom)
            self.assertFalse(getattr(worker, "_idle_gated", False), nom)

    def test_echeances_connues(self):
        worker = self._worker(row=dict(self.ROW, wake_ts=time.time() + 100))
        self.assertAlmostEqual(worker._idle_pace(), 100.0, delta=1)
        worker = self._worker(row=dict(self.ROW), nudged=False)
        worker.last_activity -= 3600 - 200
        self.assertAlmostEqual(worker._idle_pace(), 200.0, delta=1)
        worker = self._worker(row=dict(self.ROW), lot_wake=True)
        self.assertAlmostEqual(worker._idle_pace(), 90.0, delta=1)  # 30 + 60
        # échéance passée : le filet
        worker = self._worker(row=dict(self.ROW, wake_ts=time.time() - 5))
        self.assertEqual(worker._idle_pace(), 900.0)

    def test_pas_regle_plus_long_que_le_filet(self):
        worker = self._worker(row=dict(self.ROW))
        worker._idle_wait = lambda: 1200.0
        self.assertEqual(worker._idle_pace(), 1200.0)


class EmpreintesEnBase(PgTestCase):
    def test_empreintes(self):
        self.register("s1", "claude")
        self.register("s2", "claude")
        leases = storage.of(self.db).leases
        a = leases.idle_signatures(["s1", "s2", "absent"])
        self.assertEqual(set(a["agents"]), {"s1", "s2"})
        self.assertFalse(a["agents"]["s1"]["pending"])
        epoch = int(registry.claim(self.db, "s1", "r", 60)["lease_epoch"])
        b = leases.idle_signatures(["s1", "s2"])
        self.assertEqual(b["agents"]["s2"], a["agents"]["s2"])
        # le battement ne change pas l'empreinte
        leases.renew("s1", "r", epoch, 120)
        leases.renew_many("r", [("s1", epoch, 120)])
        self.assertEqual(leases.idle_signatures(["s1", "s2"]), b)
        # courrier actif : changé et en attente ; passif : rien
        from ameesh import mail
        mail.send(self.db, "s2", "s1", "info", payload={"info": True})
        self.assertEqual(leases.idle_signatures(["s1"])["agents"]["s1"], b["agents"]["s1"])
        mail.send(self.db, "s2", "s1", "à faire")
        c = leases.idle_signatures(["s1"])["agents"]["s1"]
        self.assertNotEqual(c["sig"], b["agents"]["s1"]["sig"])
        self.assertTrue(c["pending"])
        # consigne en attente
        self.db.execute("UPDATE agent_registry SET pending_prompt='go' WHERE name='s2'")
        self.assertTrue(leases.idle_signatures(["s2"])["agents"]["s2"]["pending"])
        self.assertEqual(leases.idle_signatures([]), {"host": "", "agents": {}})

    def test_cycle_de_vie_des_hotes(self):
        self.register("s1", "claude")
        lu = lambda: storage.of(self.db).leases.idle_signatures(["s1"])["host"]
        a = lu()
        self.db.execute("INSERT INTO host_lifecycle (host, mode, actor)"
                        " VALUES ('h8166', 'available', 'test')")
        b = lu()
        self.assertNotEqual(a, b)
        # une tentative de crochet ne décide rien : pas de réveil général
        self.db.execute("UPDATE host_lifecycle SET hook_attempt_at = now() WHERE host='h8166'")
        self.assertEqual(lu(), b)
        self.db.execute("UPDATE host_lifecycle SET mode = 'draining' WHERE host='h8166'")
        self.assertNotEqual(lu(), b)


class PasseGroupeeReelle(PgTestCase):
    """Un worker au repos ne refait plus sa passe ; un changement de sa ligne
    du registre la relance au balayage suivant."""

    def test_reveil_sur_changement_seulement(self):
        name = "sweep8166"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        runner = Runner(self.cfg, self.db)
        runner.poll = 1.0
        runner.listener = object()  # pas ordinaire : max(1 s, 1 agent / 1 passe/s)
        passes = []
        original_pick = AgentWorker._pick_for_turn

        def pick(self_):
            passes.append(time.monotonic())
            return original_pick(self_)

        with mock.patch.object(Runner, "IDLE_SWEEP_S", 0.5), \
                mock.patch.object(AgentWorker, "_pick_for_turn", pick):
            runner.start_idle_sweep()
            lease = registry.claim(self.db, name, runner.runner_id, 180)
            worker = AgentWorker(runner, registry.get(self.db, name), lease)
            runner.workers[name] = worker
            worker.start()
            try:
                deadline = time.monotonic() + 60
                while not worker._idle_gated:
                    self.assertLess(time.monotonic(), deadline, "never covered by the sweep")
                    time.sleep(0.05)
                avant = len(passes)
                time.sleep(4)  # au pas ordinaire : quatre passes
                calme = len(passes) - avant
                self.db.execute("UPDATE agent_registry SET effort='high' WHERE name=%s",
                                (name,))
                deadline = time.monotonic() + 10
                while len(passes) == avant + calme:
                    self.assertLess(time.monotonic(), deadline, "change not seen")
                    time.sleep(0.05)
            finally:
                runner.stop.set()
                worker.stopping.set()
                worker.wake.set()
                worker.join(30)
        self.assertEqual(calme, 0)

if __name__ == "__main__":
    unittest.main()
