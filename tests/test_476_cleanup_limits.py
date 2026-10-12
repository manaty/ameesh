# SPDX-License-Identifier: AGPL-3.0-only
"""Cleanup budgets, unsupported pidfd waits and bounded alerts (#476 r6c)."""
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


class CleanupLimitsTest(unittest.TestCase):
    def tracker(self):
        runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                                 launch_lock=threading.Lock(), container_runtime=lambda: None)
        return background.Tracker(runner, log=lambda _: None)

    def entry(self, handle):
        return background.Entry(agent="test", turn_id="limits", pgid=7000,
                                ended=0, deadline=10, grace_s=10, handles={7001: handle})

    def target(self):
        target = mock.Mock(pid=7001)
        target.live.return_value = True
        target.location.return_value = (7000, 7000)
        target.state.side_effect = ["S", "T"]
        return target

    def test_freeze_uses_remaining_section_budget_and_expiry_refuses_term(self):
        tracker, target = self.tracker(), self.target()
        entry = self.entry(target)
        clock = [0, .1, .2, 1.8, 2.1] + [2.1] * 10
        with mock.patch.object(background.time, "perf_counter", side_effect=clock):
            tracker._signal_handles(entry, signal.SIGTERM)
        self.assertAlmostEqual(target.freeze.call_args.kwargs["timeout"], .2)
        self.assertNotIn(mock.call(signal.SIGTERM), target.send.call_args_list)
        self.assertIn(mock.call(signal.SIGCONT), target.send.call_args_list)
        self.assertTrue(entry.unverified)

    def test_blocked_registry_does_not_delay_suite_cap_term(self):
        tracker, target = self.tracker(), self.target()
        entered, release, term = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def registry(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("registry not released")
            return set()
        def cleanup():
            try:
                tracker._signal_handles(self.entry(target), signal.SIGTERM)
            except BaseException as exc:
                failures.append(exc)
        cap = suite_cap.SuiteCap(tracker.runner)
        cap_handle = mock.Mock()
        cap_handle.send.side_effect = lambda sig: term.set() if sig == signal.SIGTERM else None
        item = suite_cap.Capped(None, "suite", 0, {7001: cap_handle})
        worker = threading.Thread(target=cleanup)
        cap_worker = threading.Thread(target=lambda: cap._term(item, []))
        with mock.patch.object(tracker, "_foreign_groups", side_effect=registry), \
                mock.patch.object(suite_cap, "gather"):
            try:
                worker.start()
                self.assertTrue(entered.wait(2))
                cap_worker.start()
                self.assertTrue(term.wait(1), "suite-cap TERM was blocked by registry I/O")
            finally:
                release.set()
                worker.join(timeout=2)
                if cap_worker.ident is not None:
                    cap_worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(cap_worker.is_alive())
        self.assertEqual(failures, [])

    def test_zombie_alert_lists_only_twenty_pids(self):
        tracker = self.tracker()
        notes = []
        tracker.log = notes.append
        rows = [{"pid": 900000+i, "ppid": os.getpid(), "pgid": os.getpgrp(),
                 "start": 1.0, "zombie": True} for i in range(10000)]
        tracker._reap_strays(rows, [])
        self.assertEqual(tracker.unknown_zombies, 10000)
        self.assertEqual(len(notes), 1)
        self.assertLess(len(notes[0]), 512)
        self.assertIn("900019", notes[0])
        self.assertNotIn("900020", notes[0])
        self.assertIn("… et 9980 autres", notes[0])

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "pidfd_open")
                         and hasattr(os, "P_PIDFD"), "Linux pidfd waits")
    def test_unsupported_wait_is_cached_and_identity_guarded_fallback_reaps(self):
        for error in (errno.EINVAL, errno.ENOSYS):
            for mismatch in (False, True):
                with self.subTest(error=error, mismatch=mismatch):
                    self._unsupported_wait(error, mismatch)

    def _unsupported_wait(self, error, mismatch):
        tracker = self.tracker()
        tracker.UNKNOWN_ZOMBIES_WARN = 1
        notes = []
        tracker.log = notes.append
        pids = []
        entry = background.Entry(agent="test", turn_id="old-kernel", pgid=None,
                                 ended=0, deadline=10, grace_s=10)
        try:
            for _ in range(2):
                pid = os.fork()
                if pid == 0:
                    os.setsid()
                    time.sleep(60)
                    os._exit(0)
                pids.append(pid)
                until = time.monotonic() + 5
                while os.getpgid(pid) != pid:
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
            rows = [p for p in platform.process_table() if p["pid"] in pids]
            tracker._capture(entry, rows)
            self.assertEqual(len(tracker.observed_handles), 2)
            for pid in pids:
                os.kill(pid, signal.SIGKILL)
                until = time.monotonic() + 5
                while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
            real_start = platform.start_time
            def identity(pid):
                start = real_start(pid)
                return start + 1 if mismatch and pid == pids[1] else start
            with mock.patch.object(ProcessHandle, "_wait_supported", None), \
                    mock.patch.object(os, "waitid", side_effect=OSError(error, "unsupported")) as wait, \
                    mock.patch.object(platform, "start_time", side_effect=identity), \
                    mock.patch.object(platform, "reap", wraps=platform.reap) as reap:
                tracker._reap_strays(platform.process_table(), [])
                self.assertFalse(ProcessHandle._wait_supported)
                self.assertEqual(wait.call_count, 1)
                self.assertEqual(wait.call_args.args[0], os.P_PIDFD)
                self.assertEqual({call.args[0] for call in reap.call_args_list},
                                 {pids[0]} if mismatch else set(pids))
                self.assertEqual(tracker.unknown_zombies, int(mismatch))
                if mismatch:
                    self.assertEqual(len(notes), 1)
                    self.assertIn(str(pids[1]), notes[0])
            if mismatch:
                self.assertIsNotNone(os.waitid(os.P_PID, pids[1], os.WEXITED | os.WNOHANG | os.WNOWAIT))
        finally:
            tracker._drop_handles(entry)
            for handle in tracker.observed_handles.values():
                handle.close()
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ChildProcessError, ProcessLookupError):
                    pass
