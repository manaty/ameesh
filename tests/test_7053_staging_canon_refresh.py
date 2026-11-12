# SPDX-License-Identifier: AGPL-3.0-only
"""#7053 : la copie du canon de l'exécuteur de staging suit la tête vérifiée.

Après chaque passe `completed`, l'exécuteur récupère la tête distante de son
canon dans une référence candidate, la valide (erreurs de `canon check`), puis
avance sa référence de lecture d'un seul `update-ref` à comparaison. Un refus
(check rouge, fetch en échec, historique réécrit) laisse la copie intacte et
fait partir `staging_canon_stale` avec sa raison. Témoins sur de vrais dépôts
git (amont et copie) et sur la passe de l'exécuteur du #4880.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import staging_executor, staging_watch
from tests import test_4880_staging_executor as fixture

DAY = 86400.0
T0 = 1_800_000_000
REAL_RUN = subprocess.run


def _git(repo, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_NOSYSTEM="1", HOME=repo)
    if when is not None:
        env.update(GIT_AUTHOR_DATE="@%d +0000" % when,
                   GIT_COMMITTER_DATE="@%d +0000" % when)
    return REAL_RUN(["git", "-C", repo, *args], env=env, check=True,
                    capture_output=True, text=True).stdout.strip()


def _commit(repo, when, *, write=None, remove=None):
    if write:
        with open(os.path.join(repo, write), "w") as fh:
            fh.write("---\ntype: index\n---\n# canon %d\n" % when)
        _git(repo, "add", write)
    if remove:
        _git(repo, "rm", "-q", remove)
    _git(repo, "commit", "-q", "--allow-empty", "-m", str(when), when=when)
    return _git(repo, "rev-parse", "HEAD")


class Copies(unittest.TestCase):
    """Un amont (le dépôt du canon) et la copie de l'exécuteur, clonée."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="7053-canon-")
        self.addCleanup(tmp.cleanup)
        self.upstream = os.path.join(tmp.name, "home")
        self.copy = os.path.join(tmp.name, "copy")
        os.mkdir(self.upstream)
        _git(self.upstream, "init", "-q", "-b", "main")
        self.old = _commit(self.upstream, T0, write="index.md")
        _git(tmp.name, "clone", "-q", self.upstream, self.copy)
        self.cfg = types.SimpleNamespace(canon=self.copy, canon_ref="")

    def read(self):
        return _git(self.copy, "rev-parse", "refs/remotes/origin/main")


class RefreshTest(Copies):
    def test_green_head_is_installed(self):
        new = _commit(self.upstream, T0 + 2 * DAY, write="index.md")
        result = staging_executor.refresh_canon(self.cfg)
        self.assertEqual(result, {"state": "installed", "from": self.old, "to": new})
        self.assertEqual(self.read(), new)
        self.assertEqual(staging_executor.canon_commit(self.cfg), new)

    def test_red_check_keeps_the_copy(self):
        # The fetched head has no readable bundle: `canon check` errors.
        bad = _commit(self.upstream, T0 + 2 * DAY, remove="index.md")
        result = staging_executor.refresh_canon(self.cfg)
        self.assertEqual(result["state"], "refused")
        self.assertEqual(result["to"], bad)
        self.assertIn("canon check", result["reason"])
        self.assertEqual(self.read(), self.old)

    def test_up_to_date_copy_is_current(self):
        self.assertEqual(staging_executor.refresh_canon(self.cfg),
                         {"state": "current", "from": self.old})

    def test_rewritten_history_is_refused(self):
        _git(self.upstream, "checkout", "-q", "--orphan", "other")
        _commit(self.upstream, T0 + DAY, write="index.md")
        _git(self.upstream, "branch", "-q", "-M", "other", "main")
        result = staging_executor.refresh_canon(self.cfg)
        self.assertEqual((result["state"], result["reason"]),
                         ("refused", "not a fast-forward"))
        self.assertEqual(self.read(), self.old)

    def test_unreachable_remote_is_refused(self):
        _git(self.copy, "remote", "set-url", "origin", self.upstream + "-missing")
        result = staging_executor.refresh_canon(self.cfg)
        self.assertEqual(result["state"], "refused")
        self.assertTrue(result["reason"].startswith("fetch: "))
        self.assertEqual(self.read(), self.old)

    def test_pinned_or_absent_canon_is_left_alone(self):
        _commit(self.upstream, T0 + DAY, write="index.md")
        pinned = types.SimpleNamespace(canon=self.copy, canon_ref=self.old)
        self.assertEqual(staging_executor.refresh_canon(pinned)["state"], "pinned")
        self.assertEqual(self.read(), self.old)
        self.assertEqual(staging_executor.refresh_canon(
            types.SimpleNamespace(canon="", canon_ref="")), {"state": "absent"})


class CompletedPassTest(Copies):
    """La passe de l'exécuteur, puis l'alerte calculée sur son enregistrement."""

    def tick(self):
        case = fixture.ExecutorTest("test_attested_foreign_run_allows_next_attempt")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.foreign = []
        case.attested = []
        case.cfg.canon, case.cfg.canon_ref = self.copy, ""
        # The fixture stubs subprocess.run; the canon's own git calls (the
        # hardened `canon._git`) run for real.
        staging_executor.subprocess.run.side_effect = (
            lambda cmd, *a, **k: REAL_RUN(cmd, *a, **k)
            if "--no-replace-objects" in cmd else mock.DEFAULT)
        return staging_executor.tick(case.db, case.cfg, case.options)

    def alerts(self, data, head, now):
        return staging_watch.canon_drift_findings(
            [("mesh", data)], head=head,
            first_ahead=staging_watch._first_ahead(self.upstream), now=now)

    def test_drift_over_a_day_is_refreshed_and_the_alert_silent(self):
        head = _commit(self.upstream, T0 + DAY, write="index.md")
        # Before the pass, the copy is a day behind: the alert fires.
        self.assertEqual(len(self.alerts(dict(canon_ref=self.old), head,
                                         T0 + 2 * DAY)), 1)
        result = self.tick()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["canon_refresh"]["state"], "installed")
        self.assertEqual((result["canon_ref"], self.read()), (head, head))
        self.assertEqual(self.alerts(result, head, T0 + 2 * DAY), [])

    def test_red_check_keeps_the_copy_and_alerts_with_the_reason(self):
        head = _commit(self.upstream, T0 + DAY, remove="index.md")
        result = self.tick()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["canon_refresh"]["state"], "refused")
        self.assertEqual((result["canon_ref"], self.read()), (self.old, self.old))
        # Refused: the alert does not wait for the one-day threshold.
        alert, = self.alerts(result, head, T0 + DAY + 60)
        self.assertIn("refused (canon check", alert["detail"])
        self.assertEqual(alert["canon_ref"], self.old)

    def test_failed_pass_does_not_touch_the_copy(self):
        _commit(self.upstream, T0 + DAY, write="index.md")
        with mock.patch.object(fixture.ExecutorTest, "run_child",
                               lambda self, argv, env, *a, **k: (1, False)):
            result = self.tick()
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("canon_refresh", result)
        self.assertEqual(self.read(), self.old)


class LaterPassTest(unittest.TestCase):
    """Un refus reste lu malgré une passe `failed` plus récente."""

    def canons(self, rows):
        events = types.SimpleNamespace(
            latest_type=lambda *a: [dict(object="staging-request:mesh")],
            recent_type=lambda kind, days, limit, object=None: rows)
        store = types.SimpleNamespace(events=events)
        with mock.patch.object(staging_watch.storage, "of", return_value=store):
            return staging_watch.executor_canons(object())

    def test_failed_pass_keeps_the_last_refusal(self):
        refused = dict(state="refused", reason="canon check")
        (key, data), = self.canons([
            dict(data=dict(state="failed", canon_ref="c" * 40)),
            dict(data=dict(state="completed", canon_ref="c" * 40,
                           canon_refresh=refused))])
        self.assertEqual((key, data["state"], data["canon_refresh"]),
                         ("mesh", "failed", refused))

    def test_refusal_on_another_copy_is_not_carried(self):
        (_, data), = self.canons([
            dict(data=dict(state="failed", canon_ref="d" * 40)),
            dict(data=dict(state="completed", canon_ref="c" * 40,
                           canon_refresh=dict(state="refused")))])
        self.assertNotIn("canon_refresh", data)


if __name__ == "__main__":
    unittest.main()
