# SPDX-License-Identifier: AGPL-3.0-only
"""Fin de tour : le travail de fond garde un délai de grâce (2026-10-11).

Constat du 2026-10-10 : la fin d'un tour tuait le travail lancé en fond
pendant le tour (groupe de processus du tour tué, conteneurs du tour
supprimés) — une suite de tests coupée deux fois, un job relancé. Désormais :
délai de grâce (`turn_grace_seconds`, 20 min par défaut, par hôte et par
agent), l'agent apprend au tour suivant ce qui tourne encore ou ce qui a fini
(code de sortie), le nettoyage habituel vient à l'échéance — ou tout de suite
sur un arrêt d'urgence ; la borne du courrier ne demande pas de conclure
pendant un travail du tour, et passe à 20 messages.
Postgres réel ; faux harnais `claude` du banc.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from ameesh import background, config as config_mod, mail, platform, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


def _p(pid, ppid, name, pgid=100, zombie=False) -> dict:
    return {"pid": pid, "ppid": ppid, "pgid": pgid, "name": name, "start": 0.0,
            "zombie": zombie}


class JobsInTurnTest(unittest.TestCase):
    """Pendant le tour, vu du hook de courrier : ce qui est un travail de fond."""

    TABLE = [
        _p(100, 50, "claude"),                     # le harnais, chef du groupe
        _p(101, 100, "node"),                      # serveur MCP : pas un travail
        _p(102, 100, "bash"), _p(103, 102, "pytest"),  # tâche de fond du harnais
        _p(109, 102, "sh"), _p(110, 109, "sleep"),     # shell imbriqué : un par arbre
        _p(104, 100, "sh"), _p(105, 104, "python3"),   # le hook et son shell
        _p(106, 1, "make"), _p(107, 106, "cc1"),       # détaché (`nohup … &`)
        _p(108, 100, "bash"),                      # shell inactif : rien n'y tourne
        _p(111, 100, "bash", zombie=True),
        _p(200, 1, "autre", pgid=200),             # un autre groupe
    ]

    def test_travail_de_fond_pendant_le_tour(self):
        jobs = background.jobs_in_turn(self.TABLE, 100, exclude={105, 104, 100, 50})
        self.assertEqual([p["pid"] for p in jobs], [102, 106])

    def test_harnais_disparu_rien(self):
        table = [p for p in self.TABLE if p["pid"] != 100]
        self.assertEqual(background.jobs_in_turn(table, 100), [])


class NoteTest(unittest.TestCase):
    """Ce que l'agent apprend en tête de son tour suivant."""

    def _tracker(self) -> background.Tracker:
        runner = mock.Mock()
        runner.container_runtime.return_value = None
        return background.Tracker(runner, log=lambda *_a: None)

    @staticmethod
    def _entry(turn, state, ended, job=None, **kw) -> background.Entry:
        entry = background.Entry(agent=kw.pop("agent", "a"), turn_id=turn * 4, pgid=None,
                                 ended=ended, deadline=ended + 1200, grace_s=1200,
                                 state=state, **kw)
        if job is not None:
            entry.jobs[job.pid] = job
        return entry

    def test_en_cours_fini_arrete_puis_dit_une_fois(self):
        tracker, now = self._tracker(), 10_000.0
        en_cours = self._entry("aaaa", "running", now - 360,
                               background.Job(5, 'pid 5 "pytest -q"'))
        fini = self._entry("bbbb", "done", now - 900,
                           background.Job(6, 'pid 6 "make test"', code=0, ended=now - 600),
                           closed=now - 600)
        tue = self._entry("cccc", "expired", now - 1500,
                          background.Job(7, 'pid 7 "sleep 9999"', code=-15, ended=now - 300),
                          closed=now - 300)
        autre = self._entry("dddd", "done", now - 60, background.Job(8, "x", code=1),
                            agent="b", closed=now - 30)
        tracker.entries = [en_cours, fini, tue, autre]
        texte, dits = tracker.note_for("a", now=now)
        self.assertTrue(texte.startswith("[ameesh] Background work from your previous turns"))
        self.assertIn('pytest -q": running; stopped in 14 min', texte)
        self.assertIn("finished 5 min after the turn ended", texte)
        self.assertIn('make test": exit code 0', texte)
        self.assertIn("stopped when the grace period (20 min) ran out", texte)
        self.assertIn("killed by signal 15", texte)
        self.assertNotIn("dddd", texte)  # un autre agent
        self.assertEqual(dits, [tue.turn_id, fini.turn_id])
        tracker.mark_reported(dits)
        texte, dits = tracker.note_for("a", now=now)
        self.assertIn("pytest", texte)        # toujours en cours : redit
        self.assertNotIn("make test", texte)  # fini et déjà dit : plus jamais
        self.assertEqual(dits, [])
        tracker.tick(now=now)                 # les entrées dites partent
        self.assertNotIn(fini, tracker.entries)

    def test_note_bornee(self):
        tracker, now = self._tracker(), 10_000.0
        tracker.entries = [self._entry("%04d" % i, "done", now - 100,
                                       background.Job(i, 'pid %d "%s"' % (i, "x" * 70),
                                                      code=0), closed=now)
                           for i in range(40)]
        texte, dits = tracker.note_for("a", now=now)
        self.assertLessEqual(len(texte.encode("utf-8")), background.NOTE_MAX_BYTES + 10)
        self.assertIn("- …", texte)
        # seuls les tours cités sont notés « dits » : les autres viendront après
        self.assertTrue(0 < len(dits) < 40)
        self.assertTrue(all(t[:8] in texte for t in dits))


@unittest.skipUnless(hasattr(os, "fork") and sys.platform.startswith("linux"), "Linux")
class ZombieTest(unittest.TestCase):
    """#458 : un zombie du groupe dont le parent n'est pas l'exécuteur (un
    sous-moissonneur au-dessus de lui, ex. l'exécuteur qui fait tourner
    l'agent qui lance ces tests) ne garde pas la ressource « orpheline »."""

    ENFANT = ("import os, sys, time\n"
              "pid = os.fork()\n"
              "if pid == 0:\n"
              "    os.setpgid(0, 0)\n"
              "    os._exit(4)\n"
              "print(pid, flush=True)\n"
              "time.sleep(30)\n")

    def test_zombie_d_un_autre_parent_ferme_done(self):
        # parent qui ne moissonne pas : son fils, seul de son groupe, reste zombie
        parent = subprocess.Popen([sys.executable, "-c", self.ENFANT],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(parent.wait)
        self.addCleanup(parent.kill)
        pid = int(parent.stdout.readline())
        fin = time.monotonic() + 10
        while not any(p["pid"] == pid and p["zombie"] for p in platform.process_table()):
            self.assertLess(time.monotonic(), fin, "pas de zombie")
            time.sleep(0.02)
        self.assertTrue(background._group_alive(pid))  # signal 0 : le zombie répond
        runner = mock.Mock()
        runner.container_runtime.return_value = None
        tracker = background.Tracker(runner, log=lambda *_a: None)
        now = time.time()
        entry = background.Entry(agent="a", turn_id="t" * 32, pgid=pid, ended=now,
                                 deadline=now + 1200, grace_s=1200, lineage={pid})
        entry.jobs[pid] = background.Job(pid, 'pid %d "sh"' % pid)
        tracker.entries = [entry]
        with mock.patch.object(background.storage, "of") as of:
            tracker.tick(now=now)
        self.assertEqual(entry.state, "done")
        of.return_value.turn_resources.close_turn.assert_called_once_with(
            entry.turn_id, orphan=False, containers=None)


@unittest.skipUnless(hasattr(os, "WNOHANG"), "POSIX")
class PgidRecycleTest(unittest.TestCase):
    """#458 r2 (test de mty-amh-qa-01) : le passage de suivi voit le groupe du
    tour vide, puis son numéro est recyclé avant la fermeture de la
    ressource. Le verdict du passage fait foi : la ressource se ferme `done`
    (orphan=False), rien n'est signalé au groupe étranger. Sans Postgres."""

    PGID = 4242       # pid du harnais du tour == pgid de son groupe
    JOB = 4243        # tâche de fond du tour, vue dans la lignée

    @staticmethod
    def _p(pid, ppid, pgid, name="x", zombie=False) -> dict:
        return {"pid": pid, "ppid": ppid, "pgid": pgid, "name": name,
                "start": 0.0, "zombie": zombie}

    def _tick_with_recycled_group(self, now: float, recycled: list):
        runner = mock.Mock()
        runner.container_runtime.return_value = None
        tracker = background.Tracker(runner, log=lambda *a: None)
        entry = background.Entry(agent="a", turn_id="t" * 32, pgid=self.PGID,
                                 ended=now - 30.0, deadline=now + 1200.0,
                                 grace_s=1200, lineage={self.PGID, self.JOB})
        entry.jobs[self.JOB] = background.Job(self.JOB, 'pid %d "pytest"' % self.JOB)
        tracker.entries = [entry]
        # 1re lecture (le passage) : groupe vide ; ensuite : un groupe étranger
        reads = [[]]
        signaux: list = []
        with mock.patch.object(background.platform, "process_table",
                               side_effect=lambda: reads.pop(0) if reads else list(recycled)), \
                mock.patch.object(background, "_group_alive", side_effect=bool), \
                mock.patch.object(background.os, "killpg",
                                  side_effect=lambda p, s: signaux.append((p, s))), \
                mock.patch.object(background.storage, "of") as of:
            tracker.tick(now=now)
        self.assertEqual(entry.state, "done")
        of.return_value.turn_resources.close_turn.assert_called_once_with(
            entry.turn_id, orphan=False, containers=None)
        self.assertEqual(signaux, [], "nothing may be signalled")

    def test_numero_recycle_ferme_done(self):
        """Un membre étranger du groupe recyclé (pid != pgid, hors lignée)."""
        self._tick_with_recycled_group(10000.0, [self._p(99999, 1, self.PGID, "unrelated")])

    def test_numero_recycle_chef_du_nouveau_groupe(self):
        """Le chef du nouveau groupe a pour pid l'ancien pgid, donc un pid de
        la lignée apprise : seul le verdict du passage tranche."""
        self._tick_with_recycled_group(10000.0, [self._p(self.PGID, 1, self.PGID, "unrelated")])


class KillVerdictTest(unittest.TestCase):
    """#458 r3 (codex) : un signal refusé (EPERM) n'est pas un groupe
    disparu ; le survivant garde la ressource orpheline. Sans Postgres."""

    def _expire(self, error):
        runner = mock.Mock()
        runner.container_runtime.return_value = None
        tracker = background.Tracker(runner, log=lambda *a: None)
        entry = background.Entry(agent="a", turn_id="t" * 32, pgid=4242, ended=0.0,
                                 deadline=1.0, grace_s=1, lineage={4242})
        tracker.entries = [entry]
        vivant = [{"pid": 4242, "ppid": 1, "pgid": 4242, "name": "x", "start": 0.0,
                   "zombie": False}]
        with mock.patch.object(background.platform, "process_table", return_value=vivant), \
                mock.patch.object(background.os, "killpg", side_effect=error), \
                mock.patch.object(background.storage, "of") as of:
            tracker.tick(now=10.0)
        self.assertEqual(entry.state, "expired")
        return of.return_value.turn_resources.close_turn

    def test_signal_refuse_le_groupe_vivant_reste_orphelin(self):
        self._expire(PermissionError(1, "EPERM")).assert_called_once_with(
            "t" * 32, orphan=True, containers=None)

    def test_groupe_disparu_ferme_non_orphelin(self):
        self._expire(ProcessLookupError(3, "ESRCH")).assert_called_once_with(
            "t" * 32, orphan=False, containers=None)


class PlatformTest(unittest.TestCase):
    def test_moisson_d_un_enfant_et_table_des_processus(self):
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
        code, fin = None, time.monotonic() + 15
        while code is None and time.monotonic() < fin:
            code = platform.reap(proc.pid)
            time.sleep(0.02)
        proc.returncode = code  # moissonné ici : Popen ne l'attend plus
        self.assertEqual(code, 3)
        self.assertIsNone(platform.reap(proc.pid))  # plus notre enfant
        moi = [p for p in platform.process_table() if p["pid"] == os.getpid()]
        self.assertEqual(moi[0]["pgid"], os.getpgid(0))
        self.assertFalse(moi[0]["zombie"])


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.db.execute("DELETE FROM turn_resources")
        self.pids: list[int] = []

    def tearDown(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        super().tearDown()

    def _worker(self, name: str, **reglages):
        # mode service (sans `run`) : le délai de grâce s'applique ; le suivi
        # se fait à la main (`tick`)
        runner = Runner(self.cfg, self.db)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        if reglages:
            storage.of(self.db).operations.set_settings(
                name, {k: str(v) for k, v in reglages.items()})
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _env(self, **extra):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def _pid(self, path: str) -> int:
        def lu() -> str:
            try:
                with open(path, encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                return ""
        pid = int(self.wait_for(lu, timeout=10))
        self.pids.append(pid)
        return pid

    def _ressources(self, agent: str) -> list[dict]:
        return self.db.query(
            "SELECT status, extract(epoch from ended_at)::float8 AS ended_ts"
            " FROM turn_resources WHERE agent = %s ORDER BY id", (agent,))

    def _tour(self, worker, prompt: str = "x", **env) -> str:
        self.clear_turns()
        with self._env(**env):
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": prompt, "ids": []}))
        return self.turns()[-1]["argv"][-1]


class GraceTest(_Base):
    def test_la_fin_du_tour_laisse_finir_le_travail_de_fond(self):
        runner, worker = self._worker("fond-ok")
        pidfile, stop = os.path.join(self.tmp, "fond.pid"), os.path.join(self.tmp, "fond.stop")
        self._tour(worker, "lance", AMEESH_TEST_BACKGROUND_PID=pidfile,
                   AMEESH_TEST_BACKGROUND="while [ ! -e %s ]; do sleep 0.1; done; exit 4" % stop)
        pid = self._pid(pidfile)
        time.sleep(0.3)
        self.assertTrue(platform.alive(pid), "la fin du tour a tué le travail de fond")
        self.assertTrue(runner.background.has_jobs("fond-ok"))
        # en grâce : la ressource reste `running`, la fin du tour est notée
        lignes = self._ressources("fond-ok")
        self.assertEqual(lignes[0]["status"], "running")
        self.assertIsNotNone(lignes[0]["ended_ts"])
        # le tour suivant l'apprend en tête de consigne
        consigne = self._tour(worker, "suite")
        self.assertIn("Background work from your previous turns", consigne)
        self.assertIn('": running', consigne)
        self.assertIn("stopped in 20 min if still running", consigne)
        self.assertTrue(consigne.endswith("suite"))
        # le travail finit : l'exécuteur le constate, ferme la ressource
        open(stop, "w").close()
        self.wait_for(lambda: not platform.alive(pid), timeout=10)
        runner.background.tick()
        self.assertFalse(runner.background.has_jobs("fond-ok"))
        self.assertEqual(self._ressources("fond-ok")[0]["status"], "done")
        consigne = self._tour(worker, "et après")
        self.assertIn("finished", consigne)
        # code lu en sous-moissonneur ; ce processus de test ne l'est pas
        self.assertRegex(consigne, r"exit code (4|unknown)")
        # dit une fois
        self.assertNotIn("Background work", self._tour(worker, "encore"))

    def test_echeance_de_la_grace_reglee_par_agent(self):
        runner, worker = self._worker("fond-long", turn_grace_seconds=1)
        self.assertEqual(worker.turn_grace_seconds(), 1)
        pidfile = os.path.join(self.tmp, "long.pid")
        self._tour(worker, AMEESH_TEST_BACKGROUND="sleep 60",
                   AMEESH_TEST_BACKGROUND_PID=pidfile)
        pid = self._pid(pidfile)
        self.assertTrue(platform.alive(pid))
        time.sleep(1.2)
        runner.background.tick()
        self.wait_for(lambda: not platform.alive(pid), timeout=10)
        self.assertFalse(runner.background.has_jobs("fond-long"))
        self.assertIn(self._ressources("fond-long")[0]["status"], ("done", "orphan"))
        self.assertIn("stopped when the grace period", self._tour(worker))

    def test_sans_grace_nettoyage_immediat(self):
        runner, worker = self._worker("fond-zero", turn_grace_seconds=0)
        pidfile = os.path.join(self.tmp, "zero.pid")
        self._tour(worker, AMEESH_TEST_BACKGROUND="sleep 60",
                   AMEESH_TEST_BACKGROUND_PID=pidfile)
        pid = self._pid(pidfile)
        self.wait_for(lambda: not platform.alive(pid), timeout=5)
        self.assertFalse(runner.background.has_jobs("fond-zero"))
        self.assertIsNotNone(self._ressources("fond-zero")[0]["ended_ts"])
        self.assertNotEqual(self._ressources("fond-zero")[0]["status"], "running")

    def test_tour_clos_au_point_sur_garde_le_travail_de_fond(self):
        runner, worker = self._worker("fond-clos", turn_max_seconds=1)
        pidfile = os.path.join(self.tmp, "clos.pid")
        appels = [{"reread": 10, "tool": True, "sleep": 1.5},
                  {"reread": 10, "tool": True, "sleep": 30}]
        debut = time.monotonic()
        self._tour(worker, AMEESH_TEST_CLAUDE_CALLS=json.dumps(appels),
                   AMEESH_TEST_BACKGROUND="sleep 60", AMEESH_TEST_BACKGROUND_PID=pidfile)
        self.assertLess(time.monotonic() - debut, 20)
        self.assertTrue(worker.last_turn_closed.startswith("maximum turn duration"))
        pid = self._pid(pidfile)
        self.assertTrue(platform.alive(pid), "la clôture au point sûr a tué le travail")
        self.assertTrue(runner.background.has_jobs("fond-clos"))
        # arrêt de l'exécuteur : le nettoyage, tout de suite
        self.assertEqual(runner.background.stop_all("test"), 1)
        self.wait_for(lambda: not platform.alive(pid), timeout=10)
        self.assertNotEqual(self._ressources("fond-clos")[0]["status"], "running")

    def test_arret_d_urgence_sans_grace(self):
        _runner, worker = self._worker("fond-urgence")
        self.assertEqual(worker._urgent_end(), "")
        for drapeau, raison in ((worker.preempting, "preemption"),
                                (worker.restarting, "restart requested"),
                                (worker.lease_lost, "lease lost")):
            drapeau.set()
            self.assertEqual(worker._urgent_end(), raison)
            drapeau.clear()
        worker.runner.stop.set()
        self.assertEqual(worker._urgent_end(), "runner shutdown")

    def test_ressource_en_grace_jugee_sur_la_fin_du_tour(self):
        tours = storage.of(self.db).turn_resources
        tours.open_turn("grace-1", "a", "pc", pgid=4242, label=None)
        self.db.execute("UPDATE turn_resources SET started_at = now() - interval '2 hours'")
        tours.begin_grace("grace-1", containers=["abc"])
        self.assertEqual(tours.stale_running(3600.0), [])  # tour fini à l'instant
        self.db.execute("UPDATE turn_resources SET ended_at = now() - interval '2 hours'")
        self.assertEqual([r["turn_id"] for r in tours.stale_running(3600.0)], ["grace-1"])
        self.assertEqual(tours.get("grace-1")["containers"], ["abc"])


class CourrierEtTravailTest(_Base):
    """La borne du courrier ne demande pas de conclure pendant un travail du tour."""

    def _hook(self, worker, tour: str, borne: int) -> str:
        env = self.env(AGENT_MAIL_NAME=worker.name, AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch), AMEESH_TURN_ID=tour,
                       AMEESH_TURN_MAIL_MAX=str(borne))
        proc = self.cli("hook", "claude", env=env,
                        stdin=json.dumps({"hook_event_name": "PostToolUse"}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _releve_perime(self, name: str) -> None:
        from ameesh import cli
        chemin = cli.tour_path(self.cfg, name)
        with open(chemin, encoding="utf-8") as fh:
            etat = json.load(fh)
        etat["travail_ts"] = 0.0
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump(etat, fh)

    def test_borne_atteinte_pendant_un_travail_du_tour(self):
        _runner, worker = self._worker("orch-fond")
        # un « harnais » vivant, chef de son groupe, qui a lancé un travail détaché
        pidfile = os.path.join(self.tmp, "detache.pid")
        harnais = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys, time;"
             "subprocess.run(['sh', '-c', 'sleep 60 & echo $! > ' + sys.argv[1]]);"
             "time.sleep(60)", pidfile], start_new_session=True)
        self.addCleanup(lambda: (harnais.kill(), harnais.wait()))
        pid = self._pid(pidfile)
        storage.of(self.db).turn_resources.open_turn(
            "tour-fond", "orch-fond", self.cfg.host, pgid=harnais.pid, label=None)
        for i in range(4):
            mail.send(self.db, "pair", "orch-fond", "message n°%d" % i)
        sortie = self._hook(worker, "tour-fond", borne=2)
        self.assertIn("message n°1", sortie)
        self.assertIn("2 pending message(s)", sortie)
        self.assertIn("do not wrap up before it ends", sortie)
        self.assertIn("sleep 60", sortie)
        self.assertNotIn("Wrap up this turn", sortie)
        # le travail fini, l'invitation à conclure vient
        os.kill(pid, signal.SIGKILL)
        self.wait_for(lambda: not platform.alive(pid), timeout=5)
        self._releve_perime("orch-fond")
        self.assertIn("Wrap up this turn", self._hook(worker, "tour-fond", borne=2))

    def test_sans_travail_la_borne_conclut_comme_avant(self):
        _runner, worker = self._worker("orch-sans")
        for i in range(3):
            mail.send(self.db, "pair", "orch-sans", "message n°%d" % i)
        sortie = self._hook(worker, "tour-inconnu", borne=2)
        self.assertIn("Wrap up this turn", sortie)


class ReglagesTest(_Base):
    def test_defauts_et_environnement(self):
        self.assertEqual(config_mod.Config().turn_mail_max, 20)
        self.assertEqual(config_mod.Config().turn_grace_seconds, 1200)
        cfg = config_mod.load(env={"AMEESH_TURN_GRACE_SECONDS": "60",
                                   "AMEESH_CONFIG": "/nulle-part.json"})
        self.assertEqual(cfg.turn_grace_seconds, 60.0)
        cfg = config_mod.load(env={"AMEESH_TURN_GRACE_SECONDS": "-3",
                                   "AMEESH_CONFIG": "/nulle-part.json"})
        self.assertEqual(cfg.turn_grace_seconds, 1200.0)

    def test_ameesh_set_turn_grace_seconds(self):
        self._worker("grace-set")
        proc = self.mesh("set", "grace-set", "turn_grace_seconds=45m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("background_grace_s=2700", proc.stdout)
        self.assertEqual(registry.get(self.db, "grace-set")["turn_grace_seconds"], 2700)
        self.assertEqual(self.mesh("set", "grace-set", "turn_grace_seconds=bientot")
                         .returncode, 2)
        proc = self.mesh("set", "grace-set", "turn_grace_seconds=")
        self.assertIn("background_grace_s=1200 (default)", proc.stdout)


@unittest.skipUnless(platform.is_linux(), "sous-moissonneur : Linux")
class SousMoissonneurTest(_Base):
    def test_code_de_sortie_du_travail_de_fond_au_tour_suivant(self):
        cwd = os.path.join(self.tmp, "work", "fond-code")
        os.makedirs(cwd, exist_ok=True)
        self.register("fond-code", "claude", cwd=cwd, prompt="lance le travail")
        proc = self.runner_popen(
            "--agents", "fond-code", "--poll", "1",
            env=self.env(AMEESH_TEST_BACKGROUND="sleep 4; exit 3",
                         AMEESH_TURN_GRACE_SECONDS="120"))
        try:
            self.wait_for(lambda: len(self.turns()) >= 1, timeout=30)
            # fin du travail constatée par le fil `fond`, ressource fermée
            self.wait_for(lambda: [r["status"] for r in self._ressources("fond-code")][:1]
                          == ["done"], timeout=40, interval=0.5)
            mail.send(self.db, "pair", "fond-code", "et ensuite ?")
            self.wait_for(lambda: len(self.turns()) >= 2, timeout=30)
        finally:
            proc.terminate()
            sortie, _ = proc.communicate(timeout=60)
        self.assertIn("exit code 3", self.turns()[1]["argv"][-1], sortie)


if __name__ == "__main__":
    unittest.main()
