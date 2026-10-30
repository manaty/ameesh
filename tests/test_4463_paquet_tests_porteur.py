# SPDX-License-Identifier: AGPL-3.0-only
"""#4463 — les tests par unité s'arrêtent à la fusion du porteur, et sont bornés.

Le 29/10, le tic de 01:20Z a composé h-11 (14 unités) ; la ci-full a fusionné
la PR à 02:00:52Z, mais les tests par unité ont continué en série jusqu'à
03:02:51Z, et `ameesh-paquet-horaire.service` est resté « activating » : aucun
tic suivant. Témoins :

* PR fusionnée pendant les tests : la garde relue avant chaque série arrête
  les séries suivantes, le porteur reçoit « tests par unité interrompus :
  porteur fusionné à <sha> », la sortie finit sur `MERGED_LINE` ;
* tests plus longs que la borne totale (`--tests-budget`, canon
  `packet_tests.total_timeout_seconds`) : chaque série est bornée au temps
  restant, les séries non parties sont non concluantes et rien n'est retiré ;
* cas nominal : PR ouverte, sans borne totale, comportement inchangé.

Aucune base, aucun réseau.
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import stat
import tempfile
import time
import unittest
from unittest import mock

from ameesh import mechanics
from tests.test_4271_paquet_series import DOCUMENT, paquet_horaire
from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto

MERGE_SHA = "3c16763c9cc5" + "0" * 28


def _composition(lots=(1, 2, 3)):
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(lot, "a" * 40, ("%d" % lot) * 40) for lot in lots],
        discarded=[], head="e" * 40)


def _run(label, modules, **_kwargs):
    return green(label)


class LocalTests:
    """`check_local_tests` en série, `run_tests` remplacé et consigné."""

    def __init__(self, lots=(1, 2, 3), run=_run):
        self.composition = _composition(lots)
        self.calls: list[tuple[str, int]] = []
        self.walls: list[int | None] = []
        self.lines: list[str] = []
        self.run_fn = run

    def run(self, parallel=1, which=None, **kwargs):
        def fake(_worktree, label, modules, *, timeout, heavy=False, wall=None):
            self.calls.append((label, timeout))
            self.walls.append(wall)
            return self.run_fn(label, modules, timeout=timeout)

        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=lambda _w, unit: ["tests.test_u%d" % unit.lot]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto.shutil, "which", return_value=which), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake):
            return paquet_auto.check_local_tests(
                pathlib.Path("."), self.composition, timeout=900, parallel=parallel,
                select=lambda *_: ({}, ""), log=self.lines.append, **kwargs)


class FusionDuPorteurTest(unittest.TestCase):
    def test_la_garde_arrete_les_series_apres_la_fusion(self) -> None:
        states = iter(["OPEN", "MERGED"])
        local = LocalTests()

        def guard():
            state = next(states)
            if state != "OPEN":
                raise paquet_auto.CarrierSettled(state)

        with self.assertRaises(paquet_auto.CarrierSettled) as caught:
            local.run(guard=guard)
        self.assertEqual(caught.exception.state, "MERGED")
        # la première série a tourné ; la deuxième n'est jamais partie
        self.assertEqual([label for label, _ in local.calls], ["unité #1"])

    def test_porteur_fusionne_pendant_les_tests_ligne_au_porteur(self) -> None:
        flux = Flux(self, tests=[self._guarded], compositions=[_composition((1, 2))],
                    states=("OPEN", "MERGED"))
        out = io.StringIO()
        with mock.patch.object(paquet_auto, "pull_request_merge_sha",
                               return_value=MERGE_SHA), \
                contextlib.redirect_stdout(out):
            rc = flux.run()
        self.assertEqual(rc, 0)
        notes = [call[1][2] for call in flux.calls("note")]
        self.assertEqual(len(notes), 1)
        self.assertIn("tests par unité interrompus : porteur fusionné à "
                      "3c16763c9cc5", notes[0])
        # ni recomposition, ni retrait, ni courrier ; paquet-horaire ne publie pas
        self.assertEqual(flux.calls("mail"), [])
        self.assertEqual(flux.calls("withdraw"), [])
        self.assertEqual(len(flux.calls("push")), 1)
        last = out.getvalue().strip().splitlines()[-1]
        self.assertTrue(last.startswith(paquet_auto.MERGED_LINE), last)
        self.assertIn("interrompus", last)

    def test_porteur_clos_pendant_les_tests_dit_et_arrete(self) -> None:
        flux = Flux(self, tests=[self._guarded], compositions=[_composition((1, 2))],
                    states=("OPEN", "CLOSED"))
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(paquet_auto.PaquetAutoError):
            flux.run()
        notes = [call[1][2] for call in flux.calls("note")]
        self.assertTrue(any("interrompus : PR closed" in note for note in notes), notes)

    @staticmethod
    def _guarded(**kwargs):
        """Deux séries : la garde passe avant chacune (état lu par Flux)."""
        for _ in range(2):
            kwargs["guard"]()
        return [green()], [], []

    def test_cas_nominal_pr_ouverte_inchange(self) -> None:
        flux = Flux(self, tests=[self._guarded], compositions=[_composition((1, 2))])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flux.run(), 0)
        notes = [call[1][2] for call in flux.calls("note")]
        self.assertTrue(any("tests par unité verts" in note for note in notes), notes)
        self.assertFalse(any("interrompus" in note for note in notes))
        self.assertEqual(len(flux.calls("mail")), 1)

    def test_local_sans_poussee_ne_lit_pas_la_pr(self) -> None:
        flux = Flux(self, tests=[self._guarded], compositions=[_composition((1, 2))],
                    states=("MERGED",), local=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flux.run(), 0)
        self.assertEqual(flux.calls("state"), [])


class BorneTotaleTest(unittest.TestCase):
    def test_echeance_passee_series_non_jouees_non_concluantes(self) -> None:
        local = LocalTests()
        runs, faulty, removals = local.run(deadline=time.monotonic() - 1)
        self.assertEqual(local.calls, [])
        self.assertEqual((faulty, removals), ([], []))
        self.assertEqual([run.label for run in runs],
                         ["unité #1", "unité #2", "unité #3", "contrats"])
        self.assertTrue(all(run.inconclusive for run in runs))
        self.assertIn("borne totale", runs[0].output)

    def test_chaque_serie_bornee_au_temps_restant(self) -> None:
        clock = [1000.0]

        def slow(label, modules, *, timeout):
            clock[0] += 400  # chaque série prend 400 s
            return green(label)

        local = LocalTests(run=slow)
        with mock.patch.object(paquet_auto.time, "monotonic", side_effect=lambda: clock[0]):
            runs, faulty, _ = local.run(deadline=1000.0 + 600)
        # 1re série : min(900, 600) ; 2e : 200 s restants ; ensuite plus rien
        self.assertEqual(local.calls, [("unité #1", 600), ("unité #2", 200)])
        self.assertEqual(faulty, [])
        self.assertEqual([run.inconclusive for run in runs],
                         [False, False, True, True])

    def test_en_parallele_l_attente_du_creneau_compte_dans_l_echeance(self) -> None:
        # relecture cx3 : la borne passe en temps mural à chaque série
        local = LocalTests()
        local.run(parallel=4, which="/bin/ameesh", deadline=time.monotonic() + 600)
        self.assertEqual(len(local.walls), 4)
        self.assertTrue(all(wall is not None and 590 <= wall <= 600
                            for wall in local.walls), local.walls)

    def test_sans_borne_totale_inchange(self) -> None:
        local = LocalTests()
        runs, faulty, _ = local.run()
        self.assertEqual(local.walls, [None] * 4)
        self.assertEqual(local.calls, [("unité #1", 900), ("unité #2", 900),
                                       ("unité #3", 900), ("contrats", 900)])
        self.assertEqual(faulty, [])
        self.assertFalse(any(run.inconclusive for run in runs))

    def test_le_budget_arme_l_echeance_de_compose_packet(self) -> None:
        seen = {}

        def tests(**kwargs):
            seen["deadline"] = kwargs["deadline"]
            return [green()], [], []

        flux = Flux(self, tests=[tests], compositions=[_composition((1,))],
                    tests_budget=600)
        before = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flux.run(), 0)
        self.assertGreaterEqual(seen["deadline"], before + 600)
        self.assertLessEqual(seen["deadline"], time.monotonic() + 600)

    def test_argument_par_defaut_sans_borne(self) -> None:
        flux = Flux(self, tests=[lambda **kw: ([green()], [], []) if kw["deadline"] is None
                                 else self.fail("échéance inattendue")],
                    compositions=[_composition((1,))])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flux.run(), 0)


class CreneauxSaturesTest(unittest.TestCase):
    """Relecture cx3 : `ameesh heavy` attend un créneau sans borne et ignore
    SIGTERM tant que sa série n'est pas partie ; le temps mural la borne."""

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="paquet-4463-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        self.tmp = pathlib.Path(tmp)
        # un faux `ameesh heavy` dont tous les créneaux sont pris
        self.heavy = self.tmp / "ameesh"
        self.heavy.write_text("#!/bin/sh\ntrap '' TERM\nsleep 60 &\nwait\nsleep 60\n")
        self.heavy.chmod(self.heavy.stat().st_mode | stat.S_IEXEC)
        grace = mock.patch.object(paquet_auto, "WALL_GRACE_S", 1)
        grace.start()
        self.addCleanup(grace.stop)

    def test_run_walled_arrete_une_attente_qui_ignore_sigterm(self) -> None:
        start = time.monotonic()
        proc = paquet_auto.run_walled([str(self.heavy)], cwd=self.tmp,
                                      env=dict(os.environ), wall=1)
        self.assertIsNone(proc)
        self.assertLess(time.monotonic() - start, 10)

    def test_run_tests_heavy_borne_attente_comprise(self) -> None:
        start = time.monotonic()
        with mock.patch.object(paquet_auto.shutil, "which", return_value=str(self.heavy)), \
                mock.patch.object(paquet_auto, "project_python", return_value="python3"), \
                mock.patch.object(paquet_auto, "save_test_log", side_effect=lambda run: run):
            run = paquet_auto.run_tests(self.tmp, "unité #1", ["tests.test_x"],
                                        timeout=900, heavy=True, wall=1)
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(run.inconclusive)
        self.assertIn("attente du créneau comprise", run.output)

    def test_run_walled_rend_la_sortie_dans_la_borne(self) -> None:
        proc = paquet_auto.run_walled(["sh", "-c", "echo OK; exit 1"], cwd=self.tmp,
                                      env=dict(os.environ), wall=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"OK", proc.stdout)


#: le vrai `ameesh heavy` (`heavy.run`), un créneau, grâces raccourcies
HEAVY_DRIVER = """\
import sys, types
from ameesh import heavy
heavy.GROUP_GRACE_S = 1.0
heavy.POLL_S = 0.2
cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1,
                            heavy_max_seconds=0)
sys.exit(heavy.run(cfg, sys.argv[2:], nice=0))
"""

#: la série ignore SIGTERM ; son enfant, dans une autre session (donc un autre
#: groupe), l'ignore aussi et garde le descripteur du créneau
SERIE = """\
trap '' TERM
echo $$ > leader.pid
setsid sh -c 'echo $$ > enfant.pid; exec sleep 300' &
sleep 300 &
wait
sleep 300
"""


def _alive(pid: int) -> bool:
    deadline = time.monotonic() + 3      # le temps d'être récolté
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            stat = pathlib.Path("/proc/%d/stat" % pid).read_text()
        except (ProcessLookupError, FileNotFoundError):
            return False
        if stat.rpartition(")")[2].split()[0] == "Z":
            return False        # mort, en attente de son récolteur
        time.sleep(0.1)
    return True


def _killpg(pgid: int) -> None:
    with contextlib.suppress(OSError):
        os.killpg(pgid, 9)


class ArbreHeavyTest(unittest.TestCase):
    """Relectures cx3 (#161273) et dev3 (jalon 24994) : `ameesh heavy` place
    sa série dans son propre groupe ; tuer la session du wrapper la laissait
    vivre, avec le créneau. Créneau pris au départ (acquisition retardée),
    série et enfant de groupes distincts qui ignorent SIGTERM."""

    def setUp(self) -> None:
        from ameesh import heavy
        self.heavy = heavy
        tmp = tempfile.mkdtemp(prefix="paquet-4463-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        self.tmp = pathlib.Path(tmp)
        self.cfg = __import__("types").SimpleNamespace(state_dir=tmp, max_heavy_suites=1)
        (self.tmp / "driver.py").write_text(HEAVY_DRIVER)
        grace = mock.patch.object(paquet_auto, "WALL_GRACE_S", 8)
        grace.start()
        self.addCleanup(grace.stop)

    def pid(self, name: str) -> int:
        return int((self.tmp / name).read_text())


    def test_borne_murale_arrete_l_arbre_et_rend_le_creneau(self) -> None:
        import sys
        import threading
        held = self.heavy.acquire(self.cfg)
        self.assertIsNotNone(held)
        threading.Timer(1.0, self.heavy.release, (held[1],)).start()
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve()
                                              .parents[1] / "src"))
        start = time.monotonic()
        proc = paquet_auto.run_walled(
            [sys.executable, str(self.tmp / "driver.py"), str(self.tmp), "sh", "-c", SERIE],
            cwd=self.tmp, env=env, wall=3)
        took = time.monotonic() - start
        self.assertIsNone(proc)
        self.assertLess(took, 3 + 8 + 1)
        leader, enfant = self.pid("leader.pid"), self.pid("enfant.pid")
        for group in (leader, enfant):   # sans le correctif : rien ne survit au test
            self.addCleanup(_killpg, group)
        self.assertFalse(_alive(leader), "série vivante")
        self.assertFalse(_alive(enfant), "enfant d'un autre groupe vivant")
        self.assertEqual([row["busy"] for row in self.heavy.status(self.cfg)], [False])


#: série directe (parallel = 1, sans heavy) : son enfant, aux sorties
#: redirigées, reste dans son groupe ; l'un ignore SIGTERM, l'autre non
SERIE_DIRECTE = """\
import os, subprocess, sys
for name, trap in (("enfant.pid", "trap '' TERM; "), ("enfant2.pid", "")):
    child = subprocess.Popen(["sh", "-c", trap + "exec sleep 300"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(name, "w").write(str(child.pid))
open("leader.pid", "w").write(str(os.getpid()))
child.wait()
"""


class SerieDirecteTest(unittest.TestCase):
    """Relecture cx3 (#161913) : sans heavy, SIGTERM au seul meneur laissait
    ses enfants vivre après la borne."""

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="paquet-4463-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        self.tmp = pathlib.Path(tmp)
        (self.tmp / "serie.py").write_text(SERIE_DIRECTE)
        grace = mock.patch.object(paquet_auto, "WALL_GRACE_S", 2)
        grace.start()
        self.addCleanup(grace.stop)

    def test_borne_murale_arrete_les_enfants_de_la_serie(self) -> None:
        import sys
        start = time.monotonic()
        proc = paquet_auto.run_walled([sys.executable, "serie.py"], cwd=self.tmp,
                                      env=dict(os.environ), wall=2)
        self.assertIsNone(proc)
        self.assertLess(time.monotonic() - start, 2 + 2 + 1)
        pids = [int((self.tmp / name).read_text())
                for name in ("leader.pid", "enfant.pid", "enfant2.pid")]
        for pid in pids:
            self.addCleanup(_killpg, pid)
        for pid in pids:
            self.assertFalse(_alive(pid), "pid %d vivant" % pid)


class PrefixesBornesTest(unittest.TestCase):
    """Relectures cx2 (#162578) et docs-01 (#162602) : un voisin rouge mène au
    rejeu sur les préfixes, qui ignorait l'échéance totale et la fusion du
    porteur. Empilement réel A, B, C de #4217, rejeu simulé."""

    def setUp(self) -> None:
        from tests import test_4217_paquet_attribution_prefixes as t4217
        self.t4217 = t4217
        self.auto = t4217.paquet_auto
        fixture = t4217.PrefixReplayTest
        for name in ("git", "write", "commit", "commit_unit"):
            setattr(self, name, getattr(fixture, name).__get__(self))
        fixture.setUp(self)

    def prefixes(self, replay, **bounds):
        return self.auto.prefix_failures(
            self.repo, self.composition, [self.failure], timeout=900,
            replay=replay, log=lambda _line: None, **bounds)

    def worktrees(self) -> int:
        return self.git("worktree", "list", "--porcelain").count("worktree ")

    def test_fusion_relue_avant_chaque_prefixe(self) -> None:
        calls, guards = [], []

        def replay(_worktree, label, modules, *, timeout):
            calls.append(label)
            return self.t4217._green(label, modules)

        def guard():
            guards.append(len(calls))
            if len(calls) >= 1:
                raise self.auto.CarrierSettled("MERGED")

        with self.assertRaises(self.auto.CarrierSettled):
            self.prefixes(replay, guard=guard)
        self.assertEqual(calls, ["préfixe 1 (#1)"])
        self.assertEqual(guards, [1])
        self.assertEqual(self.worktrees(), 1)         # préfixe rendu

    def test_echeance_borne_chaque_prefixe_puis_arrete(self) -> None:
        calls = []

        def replay(_worktree, label, modules, *, timeout, wall):
            calls.append((label, timeout, wall))
            time.sleep(1.2)
            return self.t4217._green(label, modules)

        with self.assertRaises(self.auto.BudgetSpent):
            self.prefixes(replay, deadline=time.monotonic() + 2)
        self.assertEqual([label for label, _t, _w in calls], ["préfixe 1 (#1)"])
        _label, timeout, wall = calls[0]
        self.assertLessEqual(timeout, 2)
        self.assertEqual(wall, timeout)
        self.assertEqual(self.worktrees(), 1)

    def test_echeance_pendant_un_prefixe_non_concluant(self) -> None:
        # relecture cx2 (#162655) : le rejeu lancé est coupé par le temps mural
        def replay(_worktree, label, modules, *, timeout, wall):
            time.sleep(2.0)
            return self.auto.TestRun(label, list(modules), None,
                                     "temps mural atteint", float(wall),
                                     timed_out=True)

        with self.assertRaises(self.auto.BudgetSpent) as caught:
            self.prefixes(replay, deadline=time.monotonic() + 1.9)
        self.assertIn("pendant le préfixe 1", str(caught.exception))
        self.assertEqual(self.worktrees(), 1)

    def test_delai_propre_du_prefixe_reste_illisible(self) -> None:
        # sous l'échéance, un préfixe non concluant de lui-même ne change pas
        def replay(_worktree, label, modules, *, timeout, wall):
            return self.auto.TestRun(label, list(modules), None,
                                     "did not finish", 1.0, timed_out=True)

        found, why, flaky = self.prefixes(replay, deadline=time.monotonic() + 600)
        self.assertEqual(found, {})
        self.assertEqual(flaky, [])
        self.assertIn("préfixe 1 (#1) non concluant", why)

    def test_sans_borne_inchange(self) -> None:
        replay, calls = self.t4217.PrefixReplayTest.replay_at(self, {"préfixe 2 (#2)"})
        found, why, flaky = self.prefixes(replay)
        self.assertEqual(flaky, [])
        self.assertEqual((list(found), why), ([2], ""))
        self.assertEqual(len(calls), 2)


class VoisinRougePrefixesTest(unittest.TestCase):
    """Le vrai `check_local_tests` : voisin rouge, base, préfixes, sous
    l'échéance et la garde."""

    def setUp(self) -> None:
        from tests import test_4217_paquet_attribution_prefixes as t4217
        self.auto = t4217.paquet_auto
        fixture = t4217.CheckLocalTestsTest
        for name in ("git", "write", "commit", "commit_unit"):
            setattr(self, name, getattr(fixture, name).__get__(self))
        fixture.setUp(self)
        self.base_calls = []

    def check(self, prefix):
        neighbors = {3: [{"module": "tests.test_target", "reason": "carte",
                          "files": ["src/pkg/value.py"]}]}

        def on_base(_worktree, _sha, modules, **kwargs):
            self.base_calls.append(kwargs)
            return set()

        import sys
        with mock.patch.object(self.auto, "project_python", return_value=sys.executable):
            return self.auto.check_local_tests(
                self.repo, self.composition, timeout=30, base_sha=self.base,
                attribute=lambda *_a: self.fail("attribution inattendue"),
                select=lambda *_a: (neighbors, "carte simulée"),
                on_base=on_base, prefix=prefix, guard=lambda: None,
                deadline=time.monotonic() + 600, log=lambda _line: None)

    def test_echeance_aux_prefixes_non_concluant_rien_retire(self) -> None:
        seen = {}

        def prefix(*_args, **kwargs):
            seen.update(kwargs)
            raise self.auto.BudgetSpent("borne totale atteinte avant le préfixe 2 (#2)")

        runs, faulty, removals = self.check(prefix)
        self.assertEqual((faulty, removals), ([], []))
        self.assertIn("deadline", seen)
        self.assertIn("guard", seen)
        self.assertTrue(runs[-1].inconclusive)
        self.assertEqual(runs[-1].label, "préfixes")
        # la base aussi est bornée en temps mural
        self.assertEqual(self.base_calls[0]["wall"], self.base_calls[0]["timeout"])

    def test_prefixe_coupe_par_l_echeance_paquet_continue(self) -> None:
        # voisin rouge → préfixe lancé → TestRun coupé → aucun retrait, sans
        # AttributionUnreadable (relecture cx2 #162655)
        real = self.auto.prefix_failures

        def prefix(*args, **kwargs):
            def replay(_worktree, label, modules, *, timeout, wall):
                return self.auto.TestRun(label, list(modules), None,
                                         "temps mural atteint", float(wall),
                                         timed_out=True)

            # 5 s restantes au lancement, plus rien au retour du rejeu
            with mock.patch.object(self.auto, "series_timeout",
                                   side_effect=[5, None]):
                return real(*args, replay=replay, **kwargs)

        runs, faulty, removals = self.check(prefix)
        self.assertEqual((faulty, removals), ([], []))
        self.assertEqual(runs[-1].label, "préfixes")
        self.assertTrue(runs[-1].inconclusive)

    def test_fusion_aux_prefixes_remonte_telle_quelle(self) -> None:
        def prefix(*_args, **_kwargs):
            raise self.auto.CarrierSettled("MERGED")

        with self.assertRaises(self.auto.CarrierSettled):
            self.check(prefix)


class CanonTest(unittest.TestCase):
    def test_cle_optionnelle_compilee(self) -> None:
        doc = mechanics.compile_document(
            DOCUMENT + "packet_tests:\n  unit_timeout_seconds: 600\n  parallel: 2\n"
                       "  total_timeout_seconds: 1800\n")
        self.assertEqual(doc.packet_tests, mechanics.PacketTests(600, 2, 1800))
        self.assertEqual(doc.to_dict()["packet_tests"]["total_timeout_seconds"], 1800)
        doc = mechanics.compile_document(
            DOCUMENT + "packet_tests:\n  unit_timeout_seconds: 600\n  parallel: 2\n")
        self.assertIsNone(doc.packet_tests.total_timeout_seconds)
        self.assertNotIn("total_timeout_seconds", doc.to_dict()["packet_tests"])

    def test_cle_mal_formee_refusee(self) -> None:
        for value in ("0", "-5", "trente"):
            with self.subTest(value=value), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(
                    DOCUMENT + "packet_tests:\n  unit_timeout_seconds: 600\n"
                               "  parallel: 2\n  total_timeout_seconds: %s\n" % value)

    def context(self, unit_tests):
        shell = mock.Mock()
        shell.run.return_value = paquet_horaire.Result(0, "", "")
        ctx = paquet_horaire.Context(
            repo=pathlib.Path("."), target="main", project="ameesh", pilot="p",
            actor="p", shell=shell, python="python3", unit_tests=unit_tests)
        ctx.say = mock.Mock()
        return ctx, shell

    def test_paquet_horaire_transmet_la_borne_totale(self) -> None:
        ctx, shell = self.context(mechanics.PacketTests(600, 2, 1800))
        paquet_horaire.paquet_auto(ctx, "h-20261029-12", 4463)
        cmd = shell.run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--tests-budget") + 1], "1800")

    def test_borne_totale_absente_dite_sans_valeur_supposee(self) -> None:
        ctx, shell = self.context(mechanics.PacketTests(600, 2))
        paquet_horaire.paquet_auto(ctx, "h-20261029-12", 4463)
        cmd = shell.run.call_args.args[0]
        self.assertNotIn("--tests-budget", cmd)
        self.assertIn("total_timeout_seconds absent", ctx.say.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
