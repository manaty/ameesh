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
from ameesh import background, platform, suite_cap
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
                    # #843 : renommage atomique : un write_text laisse le
                    # fichier visible (exists) avant d'avoir écrit le PID,
                    # et le lecteur lit une chaîne vide (CI run 38370053962).
                    " pidfile=Path(%r); tmp=pidfile.with_name(pidfile.name+'.tmp')\n"
                    " tmp.write_text(str(os.getpid()))\n"
                    " os.replace(tmp, pidfile)\n"
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
        self.assertIn("900000", notes[0])
        self.assertIn("900019", notes[0])
        self.assertNotIn("900020", notes[0])
        self.assertIn("… et 12 autres", notes[0])

    def test_observed_descendant_uses_retained_pidfd(self):
        tracker = self.tracker()
        tracker.observed_children[900000] = 1.0
        handle = mock.Mock()
        tracker.observed_handles[900000] = handle
        row = {"pid": 900000, "ppid": os.getpid(), "pgid": os.getpgrp(),
               "start": 1.0, "zombie": True}
        with mock.patch.object(platform, "exited_child") as peek, \
                mock.patch.object(platform, "reap") as reap:
            tracker._reap_strays([row], [])
        peek.assert_not_called()
        reap.assert_not_called()
        handle.reap.assert_called_once_with()
        self.assertEqual(tracker.unknown_zombies, 0)

    def test_preexisting_stop_is_preserved_on_registry_refusal(self):
        tracker = self.tracker()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        handle = None
        try:
            row = next(p for p in platform.process_table() if p["pid"] == child.pid)
            handle = ProcessHandle.open(child.pid, row["start"])
            self.assertTrue(handle.freeze())
            entry = background.Entry(agent="test", turn_id="stop", pgid=child.pid,
                                     ended=0, deadline=1, grace_s=1, handles={child.pid: handle})
            sends = []
            send = handle.send
            def record(signum):
                sends.append(signum)
                return send(signum)
            with mock.patch.object(tracker, "_foreign_groups", return_value=None), \
                    mock.patch.object(handle, "send", side_effect=record):
                tracker._signal_handles(entry, signal.SIGTERM)
            self.assertEqual(handle.state(), "T")
            self.assertNotIn(signal.SIGCONT, sends)
            self.assertNotIn(signal.SIGTERM, sends)
            self.assertTrue(entry.unverified)
        finally:
            child.kill()
            child.wait(timeout=5)
            if handle is not None:
                handle.close()

    def test_pidfd_wait_after_another_waiter_cannot_consume_new_child(self):
        tracker = self.tracker()
        first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        handle = None
        second = None
        try:
            row = next(p for p in platform.process_table() if p["pid"] == first.pid)
            entry = background.Entry(agent="test", turn_id="wait", pgid=first.pid,
                                     ended=0, deadline=1, grace_s=1)
            tracker._capture(entry, [row])
            handle = tracker.observed_handles[first.pid]
            first.kill()
            first.wait(timeout=5)
            second = subprocess.Popen([sys.executable, "-c", "import os; os._exit(37)"],
                                      start_new_session=True)
            self.wait_for(lambda: os.waitid(os.P_PID, second.pid,
                          os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)
            calls = []
            waitid = os.waitid
            def wait(kind, identity, flags):
                calls.append((kind, identity))
                return waitid(kind, identity, flags)
            with mock.patch.object(os, "waitid", side_effect=wait):
                self.assertIsNone(handle.reap())
            self.assertEqual(calls, [(os.P_PIDFD, handle.fd)])
            self.assertEqual(second.wait(timeout=5), 37)
        finally:
            if first.poll() is None:
                first.kill()
            first.wait(timeout=5)
            if second is not None:
                second.wait(timeout=5)
            if handle is not None:
                handle.close()
            tracker._drop_handles(entry)

    def test_suite_cap_is_not_blocked_by_registry_io(self):
        tracker = self.tracker()
        cap = suite_cap.SuiteCap(tracker.runner)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        handle, worker = None, None
        attempted, signalled = threading.Event(), threading.Event()
        cap_handle = mock.Mock()
        def cap_signal(signum):
            if signum == signal.SIGCONT:
                handle.send(signum)
                signalled.set()
        cap_handle.send.side_effect = cap_signal
        item = suite_cap.Capped(None, "test", 0, {child.pid: cap_handle})
        try:
            row = next(p for p in platform.process_table() if p["pid"] == child.pid)
            handle = ProcessHandle.open(child.pid, row["start"])
            entry = background.Entry(agent="test", turn_id="cap", pgid=child.pid,
                                     ended=0, deadline=1, grace_s=1, handles={child.pid: handle})
            reads = 0
            def cap_thread():
                attempted.set()
                cap._term(item, [])
            def registry(_):
                nonlocal reads, worker
                reads += 1
                self.assertNotEqual(handle.state(), "T")
                worker = threading.Thread(target=cap_thread)
                worker.start()
                self.assertTrue(attempted.wait(2))
                self.assertTrue(signalled.wait(2))
                return None  # refuse cleanup after proving registry I/O is outside the lock
            with mock.patch.object(suite_cap, "gather"), \
                    mock.patch.object(tracker, "_foreign_groups", side_effect=registry), \
                    mock.patch.object(handle, "send", wraps=handle.send) as sends:
                tracker._signal_handles(entry, signal.SIGTERM)
                worker.join(timeout=2)
            self.assertNotIn(mock.call(signal.SIGTERM), sends.call_args_list)
            self.assertTrue(entry.unverified)
            self.assertTrue(handle.live())
            self.assertFalse(worker.is_alive())
            self.assertTrue(signalled.is_set())
            self.assertEqual(reads, 1)
            self.assertEqual(cap_handle.send.call_args_list,
                             [mock.call(signal.SIGTERM), mock.call(signal.SIGCONT)])
        finally:
            if worker is not None:
                worker.join(timeout=2)
            child.kill()
            child.wait(timeout=5)
            if handle is not None:
                handle.close()

    def test_pidfd_reap_returns_exit_and_signal_codes(self):
        for signum in (None, signal.SIGKILL):
            code = "import time,os; time.sleep(.5); os._exit(37)" if signum is None \
                else "import time; time.sleep(60)"
            child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            handle = None
            try:
                row = next(p for p in platform.process_table() if p["pid"] == child.pid)
                handle = ProcessHandle.open(child.pid, row["start"])
                if signum:
                    child.kill()
                self.wait_for(lambda: not handle.live())
                self.assertEqual(handle.reap(), -int(signum) if signum else 37)
                self.assertIsNone(handle.reap())
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                if handle is not None:
                    handle.close()

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
