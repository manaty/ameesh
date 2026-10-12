# SPDX-License-Identifier: AGPL-3.0-only
"""Ownership must hold at signalling, and reaping must preserve other callers."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import background, platform
from ameesh.platform.signals import ProcessHandle


@unittest.skipUnless(platform.is_linux() and hasattr(os, "pidfd_open")
                     and hasattr(signal, "pidfd_send_signal"), "Linux pidfds")
class OwnershipRaceTest(unittest.TestCase):
    def tracker(self):
        runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                                 launch_lock=threading.Lock(), container_runtime=lambda: None)
        return background.Tracker(runner, log=lambda _: None)

    def wait_for(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.01)
        self.fail("process condition was not reached")

    def test_migration_after_first_location_does_not_receive_term(self):
        tracker = self.tracker()
        with tempfile.TemporaryDirectory(prefix="ameesh-ownership-race-") as tmp:
            ready, resumed = Path(tmp)/"ready", Path(tmp)/"resumed"
            code = ("import os,signal,time; from pathlib import Path\n"
                    "pid=os.fork()\n"
                    "if not pid:\n"
                    " signal.signal(signal.SIGUSR1,lambda *_:os.setsid())\n"
                    " signal.signal(signal.SIGUSR2,lambda *_:Path(%r).touch())\n"
                    " Path(%r).write_text(str(os.getpid()))\n"
                    "time.sleep(60)\n") % (str(resumed), str(ready))
            parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            child, handle = None, None
            try:
                self.wait_for(ready.exists)
                child = int(ready.read_text())
                row = next(p for p in platform.process_table() if p["pid"] == child)
                handle = ProcessHandle.open(child, row["start"])
                self.assertIsNotNone(handle)
                tracker.runner.harness_pids = {child}
                entry = background.Entry(agent="test", turn_id="race", pgid=parent.pid,
                                         ended=0, deadline=1, grace_s=1, handles={child: handle})
                location = handle.location
                first = True
                def interleaving():
                    nonlocal first
                    old = location()
                    if first:
                        first = False
                        os.kill(child, signal.SIGUSR1)
                        self.wait_for(lambda: os.getpgid(child) == child)
                    return old
                sends = []
                send = handle.send
                def record(signum):
                    sends.append(signum)
                    return send(signum)
                with mock.patch.object(handle, "location", side_effect=interleaving), \
                        mock.patch.object(handle, "send", side_effect=record):
                    tracker._signal_handles(entry, signal.SIGTERM)
                self.assertNotIn(signal.SIGTERM, sends)
                self.assertTrue(platform.alive(child))
                self.assertTrue(entry.unverified)
                os.kill(child, signal.SIGUSR2)
                self.wait_for(resumed.exists)
            finally:
                if handle is not None:
                    handle.send(signal.SIGKILL)
                    handle.close()
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                if child is not None:
                    try:
                        os.waitpid(child, 0)
                    except ChildProcessError:
                        pass

    def test_untracked_popen_keeps_its_exit_status(self):
        tracker = self.tracker()
        child = subprocess.Popen([sys.executable, "-c", "import os; os._exit(37)"],
                                 start_new_session=True)
        try:
            self.wait_for(lambda: os.waitid(os.P_PID, child.pid,
                          os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)
            tracker._reap_strays(platform.process_table(), [])
            self.assertEqual(child.wait(timeout=5), 37)
        finally:
            child.wait(timeout=5)

    def test_unknown_zombies_in_runner_group_are_counted_and_warned(self):
        tracker = self.tracker()
        notes = []
        tracker.log = notes.append
        rows = [{"pid": 900000+i, "ppid": os.getpid(), "pgid": os.getpgrp(),
                 "start": 1.0, "zombie": True} for i in range(32)]
        with mock.patch.object(platform, "reap") as reap:
            tracker._reap_strays(rows, [])
            tracker._reap_strays(rows, [])
        reap.assert_not_called()
        self.assertEqual(tracker.unknown_zombies, 32)
        self.assertEqual(len(notes), 1)
        self.assertIn("alert", notes[0])

    def test_observed_descendant_is_peeked_before_reaping_in_runner_group(self):
        tracker = self.tracker()
        tracker.observed_children[900000] = 1.0
        row = {"pid": 900000, "ppid": os.getpid(), "pgid": os.getpgrp(),
               "start": 1.0, "zombie": True}
        with mock.patch.object(platform, "exited_child", return_value=True) as peek, \
                mock.patch.object(platform, "reap") as reap:
            parent = mock.Mock()
            parent.attach_mock(peek, "peek")
            parent.attach_mock(reap, "reap")
            tracker._reap_strays([row], [])
        self.assertEqual(parent.mock_calls, [mock.call.peek(900000), mock.call.reap(900000)])
        self.assertEqual(tracker.unknown_zombies, 0)

    def test_recycled_observation_is_not_reaped(self):
        tracker = self.tracker()
        tracker.observed_children[900000] = 1.0
        row = {"pid": 900000, "ppid": os.getpid(), "pgid": os.getpgrp(),
               "start": 2.0, "zombie": True}
        with mock.patch.object(platform, "exited_child") as peek, \
                mock.patch.object(platform, "reap") as reap:
            tracker._reap_strays([row], [])
        peek.assert_not_called()
        reap.assert_not_called()
        self.assertEqual(tracker.observed_children, {})
