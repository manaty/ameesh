# SPDX-License-Identifier: AGPL-3.0-only
"""#339: a turn row whose process group is gone is settled without waiting
for a host reboot (L147's rule, a liveness check in place of the boot time)."""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from unittest import mock

from ameesh import exploitation, resources, runner as runner_mod, storage
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

    def _review(self, runtime=None, uptime=86400.0) -> list:
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch.object(resources, "boot_time", return_value=time.time() - uptime), \
                mock.patch.object(runner, "container_runtime", return_value=runtime), \
                mock.patch.object(runner, "_user_manager_state", return_value="running"), \
                mock.patch.object(runner_mod, "log"):
            return [r["turn_id"] for r in runner.settle_dead_turns()]

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
        moteur = mock.Mock()
        moteur.list_all.return_value = [{"id": "c1", "name": "pg", "image": "postgres",
                                         "created": "", "labels": {"ameesh.turn": avec}}]
        soldes = self._review(moteur)
        self.assertIn(sans, soldes)
        self.assertNotIn(avec, soldes)
        self.assertEqual(self.turns.get(avec)["status"], "orphan")

    def test_silent_engine_settles_nothing(self):
        turn = self._row("me", pgid=self.mort, status="orphan")
        moteur = mock.Mock()
        moteur.list_all.return_value = None
        self.assertNotIn(turn, self._review(moteur))
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
                mock.patch.object(runner, "container_runtime", return_value=None), \
                mock.patch.object(runner_mod, "log"):
            premier = runner.settle_dead_turns()
            self.assertLessEqual(runner.live_review_at, time.monotonic())  # the rest now
            second = runner.settle_dead_turns()
        self.assertEqual(len(premier), 2)
        self.assertEqual({r["turn_id"] for r in premier + second}, set(turns))
        for turn in turns:
            self.assertEqual(self.turns.get(turn)["status"], "done")
