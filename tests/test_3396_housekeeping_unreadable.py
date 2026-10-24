# SPDX-License-Identifier: AGPL-3.0-only
"""#3396 : un processus illisible n'est plus « a process is working in it ».

Le doute nomme les processus et la clé du canon dans le plan, le relevé de
l'hôte le porte (`worktrees_blocked`) et, passé six heures sur des worktrees
de lots finis, l'alerte `housekeeping_blocked` le dit ; `host_disk_soon_full`
le cite. Un détenteur CONSTATÉ reste distinct, et le fail-closed ne change
pas.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from ameesh import exploitation, housekeeping as hk, platform, registry, resources, storage, work
from ameesh.platform.process import HoldersUnreadable

from .support import PgTestCase
from .test_2022_disk_early_warning import reading
from .test_l73_housekeeping import _Cfg, _age_tree, _git, _repo

GIB = 1024 ** 3
HOUR = 3600.0
DOUBT = ("holders unreadable: 23223 gpg-agent; "
         "policy.housekeeping.unreadable_ok if it is a daemon")


class HoldersDetailTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.wt = os.path.join(self.sandbox.name, "wt")
        os.makedirs(self.wt)

    def test_doubt_names_the_processes_and_the_canon_key(self):
        doubt = HoldersUnreadable(["23223 gpg-agent"])
        with mock.patch.object(platform, "holders", side_effect=doubt):
            held, detail = hk.worktree_holders(self.wt)
            self.assertTrue(hk.worktree_in_use(self.wt))       # fail-closed
        self.assertTrue(held)
        self.assertEqual(detail, DOUBT)

    def test_a_doubt_without_a_name_keeps_its_reason(self):
        mute = platform.NotAvailable("file holders", "neither psutil nor /proc",
                                     "macos")
        with mock.patch.object(platform, "holders", side_effect=mute):
            held, detail = hk.worktree_holders(self.wt)
        self.assertTrue(held)
        self.assertEqual(detail, "holders unreadable (neither psutil nor /proc)")

    def test_an_observed_holder_is_not_a_doubt(self):
        found = [{"pid": 42, "why": "working directory", "cmd": "sleep 30"}]
        with mock.patch.object(platform, "holders", return_value=found):
            held, detail = hk.worktree_holders(self.wt)
            self.assertTrue(hk.worktree_in_use(self.wt))
        self.assertTrue(held)
        self.assertEqual(detail, "processes inside (42 sleep 30)")


class HousekeepingBlockedTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM " + table)
        sandbox = tempfile.TemporaryDirectory(dir=self.tmp)
        self.addCleanup(sandbox.cleanup)
        self.root = sandbox.name
        system_tmp = os.path.join(self.root, "system-tmp")
        os.mkdir(system_tmp)
        patch = mock.patch.dict(os.environ, AMEESH_SYSTEM_TMP=system_tmp)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = storage.of(self.db).housekeeping
        self.cfgl = _Cfg(self.state)
        self.main = _repo(self.root)
        registry.upsert(self.db, "dev1", harness="codex", host="pc")
        self.policy = hk.Policy(worktree_roots=(self.root,), artifacts=(), deps=False)

    def _ended_worktree(self):
        """A tracked worktree whose lot is closed (ended) and idle for days."""
        lot = work.add(self.db, title="ended", assignee="dev1", app="ameesh")["id"]
        path = os.path.join(self.root, "wt")
        branch = "lot/%s-x" % lot
        _git("worktree", "add", "-q", path, "-b", branch, cwd=self.main)
        work.close(self.db, lot, abandoned=True)
        self.store.register_worktree(host="pc", path=path, repo=hk.common_dir(self.main),
                                     agent="dev1", lot=str(lot), turn_id="t" * 32,
                                     branch=branch, head="")
        self.db.execute("UPDATE managed_worktrees SET created_at = now() - interval '3 days'"
                        " WHERE path = %s", (path,))
        _age_tree(path, 3 * 86400)
        return path

    def _pass(self, now):
        return hk.run_pass(self.cfgl, self.db, self.policy, host="pc", actor="test",
                           now=now, artifact_scan=False)

    def _alerts(self, kind="housekeeping_blocked"):
        return [alert for alert in exploitation._housekeeping_alerts([], self.db, readings=[])
                if alert["type"] == kind]

    def _measure(self, blocked):
        self.store.log("pc", [{"kind": "mesure", "action": "measured",
                               "path": os.path.join(self.root, "tmp"), "bytes": 0,
                               "detail": "housekeeping summary",
                               "data": {"worktrees_blocked": blocked}}], actor="test")

    def test_the_plan_names_the_doubt_and_the_measure_records_it(self):
        path = self._ended_worktree()
        with mock.patch.object(hk, "worktree_holders", return_value=(True, DOUBT)):
            report = self._pass(time.time() - HOUR)
        plan = next(row for row in report["plan"] if row["path"] == path)
        self.assertEqual(plan["action"], "attend")
        self.assertIn("lot #", plan["detail"])
        self.assertIn(DOUBT, plan["detail"])
        blocked = report["measure"]["worktrees_blocked"]
        self.assertEqual(blocked["count"], 1)
        self.assertEqual(blocked["detail"], DOUBT)
        self.assertAlmostEqual(blocked["since"], time.time() - HOUR, delta=5)
        self.assertTrue(os.path.isdir(path))              # fail-closed unchanged

    def test_the_episode_keeps_its_start_across_passes(self):
        self._ended_worktree()
        start = time.time() - HOUR
        with mock.patch.object(hk, "worktree_holders", return_value=(True, DOUBT)):
            first = self._pass(start)
            second = self._pass(time.time())
        self.assertAlmostEqual(first["measure"]["worktrees_blocked"]["since"], start, delta=5)
        self.assertEqual(second["measure"]["worktrees_blocked"]["since"],
                         first["measure"]["worktrees_blocked"]["since"])

    def test_an_observed_holder_is_not_a_housekeeping_block(self):
        path = self._ended_worktree()
        observed = "processes inside (42 sleep 30)"
        with mock.patch.object(hk, "worktree_holders", return_value=(True, observed)):
            report = self._pass(time.time())
        plan = next(row for row in report["plan"] if row["path"] == path)
        self.assertEqual(plan["action"], "attend")
        self.assertIn(observed, plan["detail"])
        self.assertNotIn("worktrees_blocked", report["measure"])

    def test_alert_only_after_six_hours_and_names_process_and_key(self):
        self._measure({"count": 3, "since": time.time() - HOUR, "detail": DOUBT})
        self.assertEqual(self._alerts(), [])
        self._measure({"count": 3, "since": time.time() - 7 * HOUR, "detail": DOUBT})
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual(alert["host"], "pc")
        self.assertEqual(alert["value"], 3)
        self.assertAlmostEqual(alert["since"], time.time() - 7 * HOUR, delta=5)
        self.assertIn("23223 gpg-agent", alert["detail"])
        self.assertIn("policy.housekeeping.unreadable_ok", alert["detail"])

    def test_a_stale_measure_no_longer_claims_an_active_block(self):
        self._measure({"count": 3, "since": time.time() - 7 * HOUR, "detail": DOUBT})
        self.db.execute("UPDATE housekeeping_log SET at = now() - interval '2 days'"
                        " WHERE kind = 'mesure'")
        self.assertEqual(self._alerts(), [])

    def test_host_disk_soon_full_cites_the_active_block(self):
        limits = resources.thresholds(None)
        low = reading(10 * GIB)
        self.assertNotIn("housekeeping", exploitation._disk_soon_full_alert(
            self.db, low, limits)["detail"])
        self._measure({"count": 493, "since": time.time() - 7 * HOUR, "detail": DOUBT})
        alert = exploitation._disk_soon_full_alert(self.db, low, limits)
        self.assertIn("housekeeping blocked on this host", alert["detail"])
        self.assertIn("493 worktree(s) of ended lots", alert["detail"])
        self.assertIn("23223 gpg-agent", alert["detail"])
