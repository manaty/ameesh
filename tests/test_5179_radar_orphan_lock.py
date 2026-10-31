# SPDX-License-Identifier: AGPL-3.0-only
"""#5179 : un verrou git orphelin du clone radar ne bloque plus la file.

Le 31/10, `.git/index.lock` (vide, 04:16Z, aucun git vivant) a fait échouer tout
atterrissage nexlink pendant 16 h, repris sans alerte : chaque réessai rafraîchit
`updated_ts`, que lit `merge_blocked`. Témoins : orphelin retiré et atterrissage
réussi ; verrou récent, clone tenu ou état illisible laissés intacts ; alerte
nommant le fichier après K échecs.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import exploitation, land, radar, resolutions

from .test_l160b_radar import _git
from .test_l160e2_land import _Land


class _Clone(_Land):
    def clone(self) -> str:
        path = radar.clone_dir(self.cfg, "p")
        resolutions.Replayer(self.repo, path).ensure()
        return path

    @staticmethod
    def lock(clone: str, name: str = "index.lock", age: float = 3600.0) -> str:
        path = os.path.join(clone, ".git", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8"):
            pass
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path


class SweepTest(_Clone):
    def test_each_kind_of_orphan_lock_is_removed_and_journaled(self):
        clone = self.clone()
        locks = [self.lock(clone, name) for name in (
            "index.lock", "HEAD.lock", "packed-refs.lock", "refs/heads/a/b.lock")]
        out = resolutions.sweep_orphan_locks(clone)
        self.assertEqual(sorted(r["path"] for r in out), sorted(locks))
        self.assertFalse([p for p in locks if os.path.exists(p)])
        self.assertGreaterEqual(out[0]["age_s"], 3599)
        self.assertEqual(out[0]["size"], 0)
        with open(clone + ".git-locks.log", encoding="utf-8") as handle:
            self.assertEqual(len(handle.readlines()), 4)

    def test_the_event_is_recorded_when_a_base_is_given(self):
        clone = self.clone()
        path = self.lock(clone)
        with mock.patch("ameesh.events.record") as record:
            resolutions.sweep_orphan_locks(clone, db="db")
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1], "git.lock_removed")
        self.assertEqual(record.call_args.kwargs["path"], path)

    def test_a_recent_lock_is_left_intact(self):
        clone = self.clone()
        path = self.lock(clone, age=60.0)
        self.assertEqual(resolutions.sweep_orphan_locks(clone), [])
        self.assertTrue(os.path.exists(path))

    def test_a_live_git_process_keeps_the_lock(self):
        clone = self.clone()
        path = self.lock(clone)
        git = subprocess.Popen(["git", "cat-file", "--batch"], cwd=clone,
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        try:
            self.assertIs(resolutions.git_process_in(clone), True)
            self.assertEqual(resolutions.sweep_orphan_locks(clone), [])
            self.assertTrue(os.path.exists(path))
        finally:
            git.communicate()

    def test_unreadable_proc_keeps_the_lock(self):
        clone = self.clone()
        path = self.lock(clone)
        missing = os.path.join(self.tmp, "no-proc")
        self.assertEqual(resolutions.sweep_orphan_locks(clone, proc_root=missing), [])
        self.assertTrue(os.path.exists(path))

    def test_a_git_process_with_an_unreadable_cwd_keeps_the_lock(self):
        clone = self.clone()
        path = self.lock(clone)
        proc = os.path.join(self.tmp, "proc", "42")
        os.makedirs(proc)
        with open(os.path.join(proc, "cmdline"), "wb") as handle:
            handle.write(b"git\0status\0")
        with mock.patch.object(os, "readlink", side_effect=PermissionError("denied")):
            self.assertEqual(resolutions.sweep_orphan_locks(
                clone, proc_root=os.path.dirname(proc)), [])
        self.assertTrue(os.path.exists(path))

    def test_unreadable_refs_keep_every_lock(self):
        clone = self.clone()
        path = self.lock(clone)
        with mock.patch.object(resolutions, "git_locks", side_effect=PermissionError("denied")):
            self.assertEqual(resolutions.sweep_orphan_locks(clone), [])
        self.assertTrue(os.path.exists(path))

    def test_a_symlink_is_not_a_git_lock(self):
        clone = self.clone()
        path = os.path.join(clone, ".git", "index.lock")
        os.symlink(os.path.join(self.tmp, "elsewhere"), path)
        self.assertEqual(resolutions.sweep_orphan_locks(clone, now=time.time() + 86400), [])
        self.assertTrue(os.path.lexists(path))

    def test_a_clone_held_by_the_mesh_is_not_swept(self):
        clone = self.clone()
        path = self.lock(clone)
        # un autre processus du mesh : le flock du clone, sur sa propre description
        with open(clone + ".worktree.lock", "a+", encoding="utf-8") as other:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX)
            with self.assertRaises(resolutions.CloneBusy):
                with resolutions.Replayer(self.repo, clone).locked(wait=0.05):
                    self.fail("the lock was held")
        self.assertTrue(os.path.exists(path))

    def test_only_a_radar_clone_is_swept(self):
        other = os.path.join(self.tmp, "checkout")
        _git(self.tmp, "clone", "-q", self.repo, other)
        path = self.lock(other)
        with resolutions.Replayer(self.repo, other).locked():
            pass
        self.assertTrue(os.path.exists(path))

    def test_a_nested_hold_does_not_sweep_under_a_running_block(self):
        clone = self.clone()
        replayer = resolutions.Replayer(self.repo, clone)
        with replayer.locked():
            path = self.lock(clone)
            with replayer.locked():
                self.assertTrue(os.path.exists(path))
        with replayer.locked():
            self.assertFalse(os.path.exists(path))


class LandTest(_Clone):
    def test_an_orphan_lock_no_longer_stops_the_landing(self):
        self.queued(4)
        path = self.lock(radar.clone_dir(self.cfg, "p"))
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertFalse(os.path.exists(path))

    def test_a_kept_lock_fails_and_notes_the_named_file(self):
        self.queued(4)
        path = self.lock(radar.clone_dir(self.cfg, "p"), age=30.0)
        with mock.patch.object(self.fake, "queue_update",
                               wraps=self.fake.queue_update) as update:
            out = self.land(4)
        self.assertEqual(out["result"], "error", out)
        self.assertIn("index.lock", out["error"])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        notes = [c.kwargs.get("note") for c in update.call_args_list]
        self.assertTrue([n for n in notes if n and n.startswith(land.FAILURE_NOTE + "lock ")
                         and n.endswith(":" + path)], notes)


class AlertTest(unittest.TestCase):
    ROW = {"work_item_id": 7, "state": "queued", "queued_ts": 1.0, "target": "develop",
           "assignee": "orchestrateur", "title": "t", "updated_ts": 999.0}
    LOCK = land.FAILURE_NOTE + "lock agents-2:/r/nexlink.clone/.git/index.lock"

    def alerts(self, notes, now=1000.0):
        ops = mock.Mock()
        ops.queue.return_value = [dict(self.ROW)]
        if isinstance(notes, Exception):
            ops.events_noted.side_effect = notes
        else:
            ops.events_noted.return_value = [
                {"note": note, "created_ts": 990.0} for note in notes]
        with mock.patch("ameesh.storage.of", return_value=mock.Mock(work=ops)):
            return exploitation._merge_lock_stuck(None, now)

    def test_raised_after_k_failures_and_names_the_file(self):
        self.assertEqual(self.alerts([self.LOCK] * (land.GIT_LOCK_ALERT_AFTER - 1)), [])
        alert = self.alerts([self.LOCK] * land.GIT_LOCK_ALERT_AFTER)[0]
        self.assertEqual(alert["type"], "merge_lock_stuck")
        self.assertEqual(alert["lot"], 7)
        self.assertIn("agents-2:/r/nexlink.clone/.git/index.lock", alert["detail"])

    def test_another_cause_breaks_the_run(self):
        other = land.FAILURE_NOTE + "other"
        self.assertEqual(self.alerts([self.LOCK] * 3 + [other]), [])
        self.assertEqual(self.alerts([self.LOCK] * 3 + [other, self.LOCK]), [])

    def test_an_old_failure_is_not_current(self):
        self.assertEqual(self.alerts([self.LOCK] * 3,
                                     now=990.0 + land.GIT_LOCK_ALERT_FRESH_S + 1), [])

    def test_an_unreadable_journal_concludes_nothing(self):
        self.assertEqual(self.alerts(RuntimeError("down")), [])

    def test_the_git_message_gives_the_path(self):
        note = land._failure_note(
            "GitError: merge: fatal: Unable to create '/r/x.clone/.git/index.lock': File exists.")
        self.assertTrue(note.endswith(":/r/x.clone/.git/index.lock"), note)
        self.assertEqual(land._failure_note("merge: boom"), land.FAILURE_NOTE + "other")


if __name__ == "__main__":
    unittest.main()
