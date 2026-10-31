# SPDX-License-Identifier: AGPL-3.0-only
"""#339: a turn row whose process group is gone is settled without waiting
for a host reboot (L147's rule, a liveness check in place of the boot time)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from ameesh import containers, exploitation, platform, resources, runner as runner_mod, storage
from ameesh.runner import Runner

from .support import PgTestCase


class SettleDeadTurnsTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")
        self.turns = storage.of(self.db).turn_resources
        # a live group (a sleeping child, leader of its session) and a dead one
        self.vivant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                       start_new_session=True)
        self.addCleanup(lambda: (self.vivant.kill(), self.vivant.wait()))
        mort = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        mort.wait()
        self.mort = mort.pid

    def _row(self, prefix, *, pgid, status="running", age_s=0.0, host=None, containers=None):
        turn = "%s-%s" % (prefix, uuid.uuid4().hex[:6])
        self.turns.open_turn(turn, "a1", host or self.cfg.host, pgid=pgid, label="l",
                             containers=containers)
        if status == "orphan":
            self.turns.mark_orphan(turn)
        if age_s:
            self.db.execute("UPDATE turn_resources SET started_at = now() - make_interval("
                            "secs => %s), ended_at = CASE WHEN ended_at IS NULL THEN NULL"
                            " ELSE now() - make_interval(secs => %s) END WHERE turn_id = %s",
                            (age_s, age_s, turn))
        return turn

    def _review(self, runtime=None, uptime=86400.0, scan=None, engines=None) -> list:
        runner = Runner(self.cfg, self.db, once=True)
        if engines is None:
            engines = ([runtime] if runtime is not None else [], [])
        if scan is None:
            scan = {"values": {}, "unread": []}
        with mock.patch.object(resources, "boot_time",
                               return_value=uptime and time.time() - uptime), \
                mock.patch.object(containers, "all_runtimes", return_value=engines), \
                mock.patch.object(platform, "env_values",
                                  **({"side_effect": scan} if callable(scan) or
                                     isinstance(scan, Exception) else {"return_value": scan})), \
                mock.patch.object(runner, "_user_manager_state", return_value="running"), \
                mock.patch.object(runner_mod, "log"):
            return [r["turn_id"] for r in runner.settle_dead_turns()]

    @staticmethod
    def _engine(listed, binary="docker", context=""):
        moteur = mock.Mock()
        moteur.binary, moteur.context = binary, context
        moteur.list_all.return_value = listed
        return moteur

    def test_dead_group_settled_live_one_kept(self):
        orphelin_mort = self._row("om", pgid=self.mort, status="orphan")
        orphelin_vivant = self._row("ov", pgid=self.vivant.pid, status="orphan")
        laisse = self._row("rl", pgid=self.mort, age_s=2 * runner_mod.LIVE_REVIEW_IDLE_S)
        recent = self._row("rr", pgid=self.mort)          # a turn closing right now
        sans_groupe = self._row("sg", pgid=None, status="orphan")
        ailleurs = self._row("ai", pgid=self.mort, status="orphan", host="autre-hote")
        soldes = self._review()
        self.assertIn(orphelin_mort, soldes)
        self.assertIn(laisse, soldes)
        for turn in (orphelin_vivant, recent, sans_groupe, ailleurs):
            self.assertNotIn(turn, soldes)
        status = {t: self.turns.get(t)["status"] for t in
                  (orphelin_mort, orphelin_vivant, laisse, recent, sans_groupe, ailleurs)}
        self.assertEqual(status, {orphelin_mort: "done", orphelin_vivant: "orphan",
                                  laisse: "done", recent: "running",
                                  sans_groupe: "orphan", ailleurs: "orphan"})
        alertes = [a["turn"] for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "orphan_resource"]
        self.assertNotIn(orphelin_mort, alertes)
        self.assertIn(orphelin_vivant, alertes)

    def test_a_running_container_keeps_the_row(self):
        avec = self._row("ac", pgid=self.mort, status="orphan")
        sans = self._row("sc", pgid=self.mort, status="orphan")
        moteur = self._engine([{"id": "c1", "name": "pg", "image": "postgres",
                                "created": "", "labels": {"ameesh.turn": avec}}])
        soldes = self._review(moteur)
        self.assertIn(sans, soldes)
        self.assertNotIn(avec, soldes)
        self.assertEqual(self.turns.get(avec)["status"], "orphan")

    def test_silent_engine_settles_nothing(self):
        turn = self._row("me", pgid=self.mort, status="orphan")
        self.assertNotIn(turn, self._review(self._engine(None)))
        self.assertEqual(self.turns.get(turn)["status"], "orphan")

    def test_not_before_the_boot_gate(self):
        # a container its restart policy relaunches may not be there yet
        turn = self._row("bt", pgid=self.mort, status="orphan")
        self.assertEqual(self._review(uptime=60.0), [])
        self.assertEqual(self.turns.get(turn)["status"], "orphan")

    def test_paged_review_resumes_without_waiting(self):
        turns = [self._row("pg%d" % i, pgid=self.mort, status="orphan") for i in range(3)]
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch.object(runner_mod, "BOOT_REVIEW_MAX_ROWS", 2), \
                mock.patch.object(runner_mod, "BOOT_ORPHAN_PAGE", 2), \
                mock.patch.object(resources, "boot_time", return_value=time.time() - 86400), \
                mock.patch.object(containers, "all_runtimes", return_value=([], [])), \
                mock.patch.object(platform, "env_values",
                                  return_value={"values": {}, "unread": []}), \
                mock.patch.object(runner_mod, "log"):
            premier = runner.settle_dead_turns()
            self.assertLessEqual(runner.live_review_at, time.monotonic())  # the rest now
            second = runner.settle_dead_turns()
        self.assertEqual(len(premier), 2)
        self.assertEqual({r["turn_id"] for r in premier + second}, set(turns))
        for turn in turns:
            self.assertEqual(self.turns.get(turn)["status"], "done")

    # #339 r2 (codex r1)

    def test_a_setsid_survivor_keeps_the_row(self):
        # a child that left the turn's group (setsid) still carries its turn id
        turn = self._row("ss", pgid=self.mort, status="orphan")
        autre = self._row("so", pgid=self.mort, status="orphan")
        survivant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     start_new_session=True,
                                     env=dict(os.environ, AMEESH_TURN_ID=turn))
        self.addCleanup(lambda: (survivant.kill(), survivant.wait()))
        reel = platform.env_values
        # the real scan; this shared host's unreadable processes are another test
        soldes = self._review(scan=lambda name, **kwargs: dict(reel(name), unread=[]))
        self.assertNotIn(turn, soldes)
        self.assertIn(autre, soldes)
        self.assertEqual(self.turns.get(turn)["status"], "orphan")

    def test_an_unreadable_process_keeps_only_later_rows(self):
        turn = self._row("ur", pgid=self.mort, status="orphan")
        self.assertEqual(self._review(scan={"values": {}, "unread": [time.time()]}), [])
        self.assertEqual(self._review(scan={"values": {}, "unread": [None]}), [])
        self.assertEqual(self.turns.get(turn)["status"], "orphan")
        avant = time.time() - 2 * runner_mod.LIVE_REVIEW_START_SLACK_S
        self.assertEqual(self._review(scan={"values": {}, "unread": [avant]}), [turn])

    def test_the_executor_itself_is_not_an_unread_survivor(self):
        """#5029: the non-dumpable executor never carries a turn id."""
        seen = {}

        def scan(name, **kwargs):
            seen.update(kwargs)
            return {"values": {}, "unread": []}

        turn = self._row("ex", pgid=self.mort, status="orphan")
        self.assertEqual(self._review(scan=scan), [turn])
        self.assertFalse(seen.get("allow"))

    def test_no_process_table_settles_nothing(self):
        turn = self._row("pt", pgid=self.mort, status="orphan")
        self.assertEqual(self._review(scan=platform.NotAvailable("process environment", "x")),
                         [])
        self.assertEqual(self.turns.get(turn)["status"], "orphan")

    def test_every_engine_is_asked(self):
        turn = self._row("en", pgid=self.mort, status="orphan")
        vide = self._engine([])
        ailleurs = self._engine([{"id": "c2", "name": "pg", "image": "postgres", "created": "",
                                  "labels": {"ameesh.turn": turn}}], "podman", "machine")
        self.assertEqual(self._review(engines=([vide, ailleurs], [])), [])
        self.assertEqual(self._review(engines=([vide, self._engine(None, "podman")], [])), [])
        self.assertEqual(self._review(engines=([vide], ["/usr/bin/podman"])), [])
        self.assertEqual(self.turns.get(turn)["status"], "orphan")
        self.assertEqual(self._review(engines=([vide, self._engine([], "podman")], [])), [turn])

    def test_unknown_boot_time_settles_nothing(self):
        turn = self._row("bn", pgid=self.mort, status="orphan")
        self.assertEqual(self._review(uptime=None), [])
        self.assertEqual(self.turns.get(turn)["status"], "orphan")

    def test_a_row_changed_after_the_read_is_kept(self):
        # the turn closes (containers left: `orphan`) while the review runs
        ferme = self._row("cf", pgid=self.mort, age_s=2 * runner_mod.LIVE_REVIEW_IDLE_S)
        stable = self._row("cs", pgid=self.mort, age_s=2 * runner_mod.LIVE_REVIEW_IDLE_S)

        def pendant(name, **kwargs):
            self.turns.mark_orphan(ferme, ["c9"])
            return {"values": {}, "unread": []}
        self.assertEqual(self._review(scan=pendant), [stable])
        self.assertEqual(self.turns.get(ferme)["status"], "orphan")
        self.assertEqual(self.turns.get(stable)["status"], "done")

    def test_settle_unchanged_compares_what_was_read(self):
        turns = [self._row("su%d" % i, pgid=self.mort, status="orphan") for i in range(3)]
        lus = [r for r in self.turns.to_review(self.cfg.host, 0) if r["turn_id"] in turns]
        self.db.execute("UPDATE turn_resources SET ended_at = ended_at + interval '1 second'"
                        " WHERE turn_id = %s", (turns[1],))
        self.turns.mark_orphan(turns[2], ["c3"])
        self.assertEqual(self.turns.settle_unchanged(lus), [turns[0]])


class AllRuntimesTest(unittest.TestCase):
    """#339 r2: every engine a turn's shims cover, with its contexts."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.cfg = mock.Mock(container_runtime="auto", container_timeout=5.0)

    def _binary(self, name, text="#!/bin/sh\n"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)
        return path

    def _run(self, contexts):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            out = contexts.get(os.path.basename(argv[0]))
            return mock.Mock(returncode=0 if out is not None else 1,
                             stdout="\n".join(out or []))
        return run, calls

    def test_each_engine_and_context(self):
        docker, podman = self._binary("docker"), self._binary("podman")
        run, _ = self._run({"docker": ["default", "remote"], "podman": ["machine"]})
        with mock.patch.dict(os.environ, {"PATH": self.dir}):
            engines, silent = containers.all_runtimes(self.cfg, run=run)
        self.assertEqual(silent, [])
        self.assertEqual([e._argv("ps") for e in engines],
                         [[docker, "ps"],               # DOCKER_HOST, codex r2
                          [docker, "--context", "default", "ps"],
                          [docker, "--context", "remote", "ps"],
                          [podman, "ps"],
                          [podman, "--connection", "machine", "ps"]])

    def test_an_engine_without_contexts_is_silent(self):
        self._binary("docker")
        podman = self._binary("podman")
        run, _ = self._run({"docker": ["default"]})
        with mock.patch.dict(os.environ, {"PATH": self.dir}):
            engines, silent = containers.all_runtimes(self.cfg, run=run)
        self.assertEqual([e.context for e in engines], ["", "default"])
        self.assertEqual(silent, [podman])

    def test_shims_skipped_disabled_engine_none(self):
        shims = os.path.join(self.dir, "shims")
        os.makedirs(shims)
        with open(os.path.join(shims, "docker"), "w", encoding="utf-8") as fh:
            fh.write(containers.SHIM)
        os.chmod(os.path.join(shims, "docker"), 0o755)
        docker = self._binary("docker")
        run, _ = self._run({"docker": ["default"]})
        with mock.patch.dict(os.environ, {"PATH": shims + os.pathsep + self.dir}):
            engines, _ = containers.all_runtimes(self.cfg, run=run)
            self.assertEqual({e.binary for e in engines}, {docker})
            self.cfg.container_runtime = "none"
            self.assertEqual(containers.all_runtimes(self.cfg, run=run), ([], []))
