# SPDX-License-Identifier: AGPL-3.0-only
"""Job identity, degraded waits and descriptor exhaustion (#476 r6b)."""
import errno
import os
import signal
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import background, platform, suite_cap
from ameesh.platform.signals import ProcessHandle


class JobReapingTest(unittest.TestCase):
    def tracker(self):
        runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                                 launch_lock=threading.Lock(), container_runtime=lambda: None)
        return background.Tracker(runner, log=lambda _: None)

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "fork"), "Linux children")
    def test_start_without_handles_reaps_attributed_job(self):
        self._job_without_handle()

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "fork"), "Linux children")
    def test_identity_mismatch_preserves_unknown_zombie(self):
        self._job_without_handle(mismatch=True)

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "fork"), "Linux children")
    def test_job_checks_ignore_unrelated_zombie_children(self):
        unrelated = os.fork()
        if unrelated == 0:
            os._exit(0)
        try:
            until = time.monotonic() + 5
            while os.waitid(os.P_PID, unrelated, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            for mismatch in (False, True):
                with self.subTest(mismatch=mismatch):
                    self._job_without_handle(mismatch=mismatch)
            self.assertIsNotNone(os.waitid(os.P_PID, unrelated, os.WEXITED | os.WNOHANG | os.WNOWAIT))
        finally:
            os.waitpid(unrelated, 0)

    def _job_without_handle(self, mismatch=False):
        tracker = self.tracker()
        pid = os.fork()
        if pid == 0:
            os.setsid()
            time.sleep(60)
            os._exit(0)
        try:
            until = time.monotonic() + 5
            while os.getpgid(pid) != pid:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            row = next(p for p in platform.process_table() if p["pid"] == pid)
            with mock.patch.object(background.storage, "of"):
                entry = tracker.start(agent="test", turn_id="no-handle", pgid=pid,
                                      members=[row], containers=[], grace_s=10, now=0,
                                      handles={})
                self.assertEqual(entry.jobs[pid].start, row["start"])
                self.assertNotIn(pid, tracker.observed_handles)
                os.kill(pid, signal.SIGKILL)
                until = time.monotonic() + 5
                while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
                start = mock.patch.object(platform, "start_time", return_value=row["start"] + 1) \
                    if mismatch else mock.patch.object(platform, "start_time", wraps=platform.start_time)
                with start as identity, mock.patch.object(platform, "reap", wraps=platform.reap) as reap:
                    snapshot = platform.process_table()
                    tracker._refresh(entry, snapshot, now=1)
                identity.assert_called_with(pid)
                self.assertEqual(entry.jobs[pid].code, None if mismatch else -signal.SIGKILL)
                # The host may also contain zombie children from other tests.
                # Count only this fixture; preserve its real identity/wait checks.
                if mismatch:
                    reap.assert_not_called()
                    # Only this test's child: earlier modules of the same shard
                    # leave zombies of the test process (#417 CI).
                    tracker._reap_strays(
                        [p for p in platform.process_table() if p["pid"] == pid], [])
                    self.assertEqual(tracker.unknown_zombies, 1)
                    self.assertIsNotNone(os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
                else:
                    reap.assert_called_once_with(pid)
                    self.assertNotIn(pid, [p["pid"] for p in snapshot])
                    # Our child is reaped: the shard's inherited zombies stay
                    # out of the count, as in the mismatch case above.
                    tracker._reap_strays(
                        [p for p in platform.process_table() if p["pid"] == pid], [])
                    self.assertEqual(tracker.unknown_zombies, 0)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass

    def test_replacement_observation_is_not_reaped_for_old_job(self):
        for path in ("refresh", "close", "members"):
            with self.subTest(path=path):
                tracker = self.tracker()
                replacement = mock.Mock()
                replacement.reap.return_value = 42
                tracker.observed_children[7001] = 2.0
                tracker.observed_handles[7001] = replacement
                job = background.Job(7001, "old job", start=1.0)
                entry = background.Entry(agent="test", turn_id="old", pgid=7000,
                                         ended=0, deadline=10, grace_s=10, jobs={7001: job})
                with mock.patch.object(background.storage, "of"), \
                        mock.patch.object(platform, "reap") as reap:
                    if path == "refresh":
                        tracker._refresh(entry, [], now=1)
                    elif path == "close":
                        tracker._close(entry, "done", 1, survives=False)
                    else:
                        tracker._members(7000, [{"pid": 7001, "pgid": 7000,
                                                 "start": 2.0, "zombie": True}], entry)
                replacement.reap.assert_not_called()
                replacement.close.assert_not_called()
                reap.assert_not_called()
                self.assertIsNone(job.code)

    def test_missing_or_unreadable_job_identity_never_uses_pid_wait(self):
        tracker = self.tracker()
        with mock.patch.object(platform, "reap") as reap, \
                mock.patch.object(platform, "start_time", side_effect=platform.NotAvailable("start", "refused")):
            self.assertIsNone(tracker._reap_job(background.Job(7001, "unknown")))
            self.assertIsNone(tracker._reap_job(background.Job(7001, "unreadable", start=1)))
        reap.assert_not_called()

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "pidfd_open"), "Linux pidfds")
    def test_emfile_during_observation_does_not_abort_start_or_close(self):
        tracker = self.tracker()
        pid = os.fork()
        if pid == 0:
            os.setsid()
            time.sleep(60)
            os._exit(0)
        handle = None
        try:
            until = time.monotonic() + 5
            while os.getpgid(pid) != pid:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            row = next(p for p in platform.process_table() if p["pid"] == pid)
            handle = ProcessHandle.open(pid, row["start"])
            self.assertIsNotNone(handle)
            with mock.patch.object(handle, "duplicate", side_effect=OSError(errno.EMFILE, "too many files")), \
                    mock.patch.object(background.storage, "of") as store:
                entry = tracker.start(agent="test", turn_id="emfile", pgid=pid,
                                      members=[row], containers=[], grace_s=10, now=0,
                                      handles={pid: handle})
                self.assertNotIn(pid, tracker.observed_handles)
                self.assertNotIn(pid, tracker.observed_children)
                self.assertTrue(handle.live())
                tracker._refresh(entry, [row], now=.5)
                self.assertNotIn(pid, tracker.observed_handles)
                self.assertNotIn(pid, tracker.observed_children)
                self.assertTrue(handle.live())
                os.kill(pid, signal.SIGKILL)
                until = time.monotonic() + 5
                while handle.live():
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
                tracker._close(entry, "done", 1, survives=False)
                self.assertEqual(entry.jobs[pid].code, -signal.SIGKILL)
                store.return_value.turn_resources.close_turn.assert_called_once()
        finally:
            if handle is not None:
                handle.close()
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass

    def test_suite_cap_waits_during_local_freeze(self):
        tracker = self.tracker()
        cap = suite_cap.SuiteCap(tracker.runner)
        attempted, signalled = threading.Event(), threading.Event()
        target = mock.Mock(pid=7001)
        target.live.return_value = True
        target.location.return_value = (7000, 7000)
        target.state.side_effect = ["S", "T"]
        cap_handle = mock.Mock()
        cap_handle.send.side_effect = lambda _: signalled.set()
        item = suite_cap.Capped(None, "test", 0, {7001: cap_handle})
        entry = background.Entry(agent="test", turn_id="local", pgid=7000,
                                 ended=0, deadline=10, grace_s=10, handles={7001: target})
        def cap_thread():
            attempted.set()
            cap._term(item, [])
        worker = threading.Thread(target=cap_thread)
        def freeze(timeout):
            worker.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(signalled.wait(.1))
            return True
        target.freeze.side_effect = freeze
        try:
            with mock.patch.object(suite_cap, "gather"):
                tracker._signal_handles(entry, signal.SIGTERM)
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(signalled.is_set())
        finally:
            if worker.ident is not None:
                worker.join(timeout=2)
