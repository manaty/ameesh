# SPDX-License-Identifier: AGPL-3.0-only
"""#2933: refreshing a cleanup observation does not create another alert."""
from __future__ import annotations

import os
import tempfile
import time
from unittest import mock

from ameesh import exploitation, housekeeping as hk, storage

from .support import PgTestCase
from .test_l73_housekeeping import _Cfg, _git, _repo, _write, _age_tree


class CleanupAlertIdentityTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM " + table)
        sandbox = tempfile.TemporaryDirectory(dir=self.tmp)
        self.addCleanup(sandbox.cleanup)
        self.root = sandbox.name
        self.system_tmp = os.path.join(self.root, "system-tmp")
        os.mkdir(self.system_tmp)
        patch = mock.patch.dict(os.environ, AMEESH_SYSTEM_TMP=self.system_tmp)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = storage.of(self.db).housekeeping
        self.cfgl = _Cfg(self.state)

    def _alerts(self, kind):
        return [a for a in exploitation._housekeeping_alerts([], self.db, readings=[])
                if a["type"] == kind]

    def _orphan_pass(self, now):
        entries, _plan, _present = hk.orphan_pass(
            self.db, "pc", hk.Policy(), registered=(), protected=set(),
            running=set(), dry_run=False, now=now)
        self.store.log("pc", entries, actor="test")
        return entries

    def test_orphan_refresh_keeps_key_and_updates_size_until_gone(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                path = os.path.join(self.system_tmp, "legacy" if legacy else "new")
                turn = hk.Turn.begin(self.cfgl, hk.Policy(orphan_min_size=1),
                                     "dev1", None, new_session=False, base_env={})
                _write(path, 4096)
                entries = turn.end(lot=None, turn_id="test", db=self.db, host="pc")
                signal = next(e for e in entries if e["kind"] == "orphan")
                self.assertIsInstance(signal["data"]["first_seen"], float)
                if legacy:
                    del signal["data"]["first_seen"]
                self.store.log("pc", [signal], actor="test")
                # Force distinct reading timestamps without sleeping: legacy
                # alerts use this timestamp until their first refresh adopts it.
                self.db.execute("UPDATE housekeeping_log SET at = now() - interval '2 days'"
                                " WHERE path = %s", (path,))
                before = next(a for a in self._alerts("tmp_orphan") if a["path"] == path)
                now = time.time()
                for offset in (0, hk.UNATTRIBUTED_REFRESH_S + 60):
                    _write(path, 8192 + int(offset))
                    refreshed = self._orphan_pass(now + offset)
                    self.assertIn(path, [e["path"] for e in refreshed])
                    after = next(a for a in self._alerts("tmp_orphan") if a["path"] == path)
                    self.assertEqual(exploitation.alert_key(after), exploitation.alert_key(before))
                    self.assertEqual(after["since"], before["since"])
                    self.assertEqual(after["value"], hk.size_of(path))
                    row = next(r for r in self.store.latest_by_path("pc", "orphan")
                               if r["path"] == path)
                    self.assertAlmostEqual(row["data"]["first_seen"], before["since"], places=2)
                os.unlink(path)
                self._orphan_pass(now + 2 * hk.UNATTRIBUTED_REFRESH_S)
                self.assertNotIn(path, [a["path"] for a in self._alerts("tmp_orphan")])

    def test_kept_worktree_commit_keeps_key_and_new_registration_is_distinct(self):
        """#4442 : le SHA de HEAD et le nombre de fichiers sont volatils — un
        commit de plus ne change plus la raison gardée, ne réécrit pas le
        statut (`checked_at`) et ne rouvre pas l'alerte. L'alerte est unique
        par hôte ; un autre hôte est une autre alerte."""
        main = _repo(self.root)
        path = os.path.join(self.root, "copy")
        _git("worktree", "add", "-q", path, "-b", "unpublished", cwd=main)
        row = self.store.register_worktree(host="pc", path=path, repo=hk.common_dir(main),
            agent="dev1", lot=None, turn_id="test", branch="unpublished", head="")
        self.db.execute("UPDATE managed_worktrees SET created_at = now() - interval '3 days'"
                        " WHERE id = %s", (row["id"],))

        def observe():
            _age_tree(path, 3 * 86400)
            with mock.patch.object(hk, "worktree_holders", return_value=(False, "")):
                entries, _plan = hk._worktree_pass(
                    self.db, "pc", dry_run=False, protected=set(), now=time.time())
            return entries, self._alerts("worktree_kept")[0]

        _write(os.path.join(path, "work"), 100)
        _git("add", "work", cwd=path)
        _git("commit", "-q", "-m", "first", cwd=path)
        entries, before = observe()
        self.assertEqual([e["action"] for e in entries], ["kept"])
        # Make the next checked_at materially different, with no wall-clock wait.
        self.db.execute("UPDATE managed_worktrees SET checked_at = now() - interval '1 day'"
                        " WHERE id = %s", (row["id"],))
        checked_before = self.store.worktrees("pc")[0]["checked_ts"]
        _write(os.path.join(path, "work"), 200)
        _git("add", "work", cwd=path)
        _git("commit", "-q", "-m", "second", cwd=path)
        entries, after = observe()
        # only the HEAD SHA moved: same normalized reason, nothing rewritten
        self.assertEqual(entries, [])
        self.assertEqual(before["reasons"], after["reasons"])
        self.assertEqual(before["since"], after["since"])
        self.assertEqual(exploitation.alert_key(before), exploitation.alert_key(after))
        self.assertEqual(after["paths"], [path])
        tracked = self.store.worktrees("pc")[0]
        # the grace clock (`checked_at`) is not restarted by a volatile detail
        self.assertAlmostEqual(tracked["checked_ts"], checked_before, places=3)
        self.assertGreater(tracked["checked_ts"], tracked["created_ts"] + 86400)
        self.assertNotIn(_git("rev-parse", "HEAD", cwd=path).strip()[:12],
                         tracked["detail"])

        self.store.set_worktree_status(row["id"], "gone", "old observation ended")
        self.assertEqual(self._alerts("worktree_kept"), [])
        new = self.store.register_worktree(host="other-pc", path=path, repo=hk.common_dir(main),
            agent="dev1", lot=None, turn_id="new", branch="unpublished", head="")
        self.store.set_worktree_status(new["id"], "kept", "new observation")
        self.assertNotEqual(exploitation.alert_key(before),
                            exploitation.alert_key(self._alerts("worktree_kept")[0]))
