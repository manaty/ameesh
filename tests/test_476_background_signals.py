# SPDX-License-Identifier: AGPL-3.0-only
"""Background expiry never signals an empty or recycled process group."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # noqa: F401 -- isolate live agent settings
from ameesh import background, platform
from ameesh.platform.signals import ProcessHandle


def row(pid=7001, *, start=10.0, zombie=False):
    return {"pid": pid, "ppid": 6999, "pgid": 7000, "name": "tool",
            "start": start, "zombie": zombie}


class BackgroundSignalTest(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.runner = SimpleNamespace(db=None, host="test", runner_id="test",
                                      container_runtime=lambda: None)
        self.tracker = background.Tracker(self.runner, log=self.notes.append)
        self.entry = background.Entry(agent="test", turn_id="expiry-test", pgid=7000,
                                      ended=0, deadline=1, grace_s=1, lineage={7001},
                                      containers=[{"id": "remaining-container"}])

    def test_empty_group_with_container_expires_without_signalling(self):
        # The table sees no members; a later killpg would hit any group that
        # acquired the old numeric ID after that observation.
        with mock.patch.object(self.tracker, "_containers"), \
                mock.patch.object(self.tracker, "_close") as close, \
                mock.patch.object(platform, "process_table", return_value=[]), \
                mock.patch.object(os, "killpg") as killpg:
            self.tracker._refresh(self.entry, [], now=2)
        killpg.assert_not_called()
        close.assert_called_once_with(self.entry, "expired", 2)
        self.assertIsNone(self.entry.pgid)

    def test_container_only_expiry_does_not_require_a_process_scan(self):
        self.entry.pgid = None
        with mock.patch.object(platform, "process_table") as scan, \
                mock.patch.object(os, "killpg") as killpg:
            self.tracker._kill(self.entry)
        scan.assert_not_called()
        killpg.assert_not_called()

    def test_zombie_only_group_with_container_never_gets_signalled(self):
        with mock.patch.object(self.tracker, "_containers"), \
                mock.patch.object(self.tracker, "_close"), \
                mock.patch.object(os, "killpg") as killpg:
            self.tracker._refresh(self.entry, [row(zombie=True)], now=2)
        killpg.assert_not_called()
        self.assertIsNone(self.entry.pgid)

    @unittest.skipUnless(hasattr(os, "getpgrp"), "POSIX groups")
    def test_runner_and_its_group_are_never_captured(self):
        own = row(pid=os.getpid())
        own["pgid"] = os.getpgrp()
        client = row()
        client["pgid"] = os.getpgrp()
        with mock.patch.object(background.ProcessHandle, "open") as acquire:
            self.tracker._capture(self.entry, [own, client])
        acquire.assert_not_called()
        self.assertEqual(self.entry.handles, {})

    def test_recycled_historical_pid_is_not_an_ownership_anchor(self):
        # The PID appears in lineage, but its captured process has exited.
        old = mock.Mock()
        old.live.return_value = False
        self.entry.handles[7001] = old
        foreign = [row(start=99.0)]
        with mock.patch.object(self.tracker, "_containers"), \
                mock.patch.object(self.tracker, "_close"), \
                mock.patch.object(os, "killpg") as killpg:
            self.tracker._refresh(self.entry, foreign, now=2)
        killpg.assert_not_called()
        old.send.assert_not_called()
        old.close.assert_called_once()
        self.assertIsNone(self.entry.pgid)
        self.assertTrue(any("ownership unknown" in note for note in self.notes))

    def test_unreadable_scan_does_not_signal_retained_handles(self):
        handle = mock.Mock()
        self.entry.handles[7001] = handle
        with mock.patch.object(platform, "process_table", side_effect=OSError("unreadable")), \
                mock.patch.object(os, "killpg") as killpg:
            self.tracker._kill(self.entry)
        handle.send.assert_not_called()
        handle.close.assert_called_once()
        killpg.assert_not_called()
        self.assertTrue(any("process table unavailable" in note for note in self.notes))

    def test_missing_pidfd_has_no_numeric_signal_fallback(self):
        with mock.patch.object(os, "pidfd_open", side_effect=OSError("unavailable"),
                               create=True), mock.patch.object(os, "killpg") as killpg, \
                mock.patch.object(os, "kill") as kill:
            self.tracker._capture(self.entry, [row()])
            self.tracker._kill(self.entry)
        killpg.assert_not_called()
        kill.assert_not_called()
        self.assertTrue(any("no stable handle" in note for note in self.notes))
        self.assertTrue(self.entry.unverified)

    def test_unknown_handle_cleanup_keeps_resource_orphaned(self):
        self.entry.unverified = True
        with mock.patch.object(self.tracker, "_containers"), \
                mock.patch.object(self.tracker, "_close"), \
                mock.patch.object(os, "killpg") as killpg:
            self.tracker._refresh(self.entry, [row()], now=2)
        killpg.assert_not_called()
        self.assertTrue(self.entry.unverified)
        with mock.patch.object(background.storage, "of") as store:
            self.tracker._release_resource(self.entry)
        self.assertTrue(store.return_value.turn_resources.close_turn.call_args.kwargs["orphan"])

    def test_uncaptured_child_after_leader_exit_keeps_resource_orphaned(self):
        leader = mock.Mock()
        leader.live.return_value = True
        with mock.patch.object(background.ProcessHandle, "open", return_value=leader), \
                mock.patch.object(platform, "command_line", return_value="fixture"), \
                mock.patch.object(platform, "reap", return_value=None), \
                mock.patch.object(background.storage, "of") as store, \
                mock.patch.object(os, "killpg") as killpg:
            entry = self.tracker.start(agent="test", turn_id="unknown-child",
                                       pgid=7000, members=[row()], containers=[],
                                       grace_s=1, now=0)
            self.assertFalse(entry.unverified)
            self.assertEqual(entry.handles, {7001: leader})
            # The leader dies between passes; this child was never captured.
            leader.live.return_value = False
            child = row(pid=7002)
            child["ppid"] = 1
            self.tracker._refresh(entry, [child], now=2)
        killpg.assert_not_called()
        leader.send.assert_not_called()
        leader.close.assert_called_once()
        self.assertIsNone(entry.pgid)
        self.assertEqual(entry.handles, {})
        self.assertTrue(entry.unverified)
        store.return_value.turn_resources.close_turn.assert_called_once_with(
            "unknown-child", orphan=True, containers=None)

    def test_handle_rejects_snapshot_start_mismatch_and_closes_fd(self):
        with mock.patch.object(os, "pidfd_open", return_value=42, create=True), \
                mock.patch("ameesh.platform.signals.start_time", return_value=11.0), \
                mock.patch.object(os, "close") as close:
            self.assertIsNone(ProcessHandle.open(7001, 10.0))
        close.assert_called_once_with(42)

    def test_escalation_keeps_handles_when_group_number_is_recycled(self):
        old = mock.Mock()
        old.live.return_value = True
        self.entry.handles[7001] = old
        self.tracker.TERM_GRACE_S = 0
        # Initial ownership is valid. After TERM, the same number belongs
        # to a different group; it must not provide new signal targets.
        with mock.patch.object(platform, "process_table",
                               side_effect=[[row()], [row(pid=7999, start=99)]]), \
                mock.patch.object(background.ProcessHandle, "open") as acquire, \
                mock.patch.object(os, "killpg") as killpg, \
                mock.patch.object(background.time, "monotonic", side_effect=[0, 1, 2, 9]):
            self.tracker._kill(self.entry)
        acquire.assert_not_called()
        killpg.assert_not_called()
        self.assertEqual(old.send.call_args_list,
                         [mock.call(signal.SIGTERM), mock.call(signal.SIGCONT),
                          mock.call(signal.SIGKILL)])
        old.close.assert_called_once()

    def test_shutdown_cannot_close_fd_during_signalling(self):
        entered, release, closing, closed = (threading.Event() for _ in range(4))
        old = mock.Mock()
        old.live.return_value = True
        self.entry.handles[7001] = old
        self.tracker.TERM_GRACE_S = 0

        def send(sig):
            if sig == signal.SIGTERM:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release signal sender")

        def close():
            closing.set()
            self.tracker._drop_handles(self.entry)
            closed.set()

        old.send.side_effect = send
        with mock.patch.object(platform, "process_table", return_value=[row()]), \
                mock.patch.object(background.time, "monotonic", side_effect=[0, 1, 2, 9]):
            killer = threading.Thread(target=self.tracker._kill, args=(self.entry,))
            closer = threading.Thread(target=close)
            try:
                killer.start()
                self.assertTrue(entered.wait(5))
                closer.start()
                self.assertTrue(closing.wait(5))
                self.assertFalse(closed.wait(.05), "fd closed during pidfd_send_signal")
            finally:
                release.set()
                killer.join(5)
                if closer.ident is not None:
                    closer.join(5)
        self.assertFalse(killer.is_alive())
        self.assertFalse(closer.is_alive())
        old.close.assert_called_once()

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "pidfd_open")
                         and hasattr(signal, "pidfd_send_signal"), "Linux pidfds")
    def test_real_term_then_kill_reaches_reparented_ignoring_child(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-476-") as tmp:
            ready = os.path.join(tmp, "ready")
            detached = os.path.join(tmp, "detached")
            child_code = ("import os,signal,time; from pathlib import Path; "
                          "signal.signal(signal.SIGTERM,lambda *_: "
                          "(os.setsid(),Path(%r).touch())); " % detached +
                          "Path(%r).touch(); time.sleep(60)" % ready)
            parent_code = ("import subprocess,sys,time; from pathlib import Path; "
                           "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
                           "\nwhile not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
                           "print(p.pid,flush=True); time.sleep(60)")
            parent = subprocess.Popen([sys.executable, "-c", parent_code, child_code, ready],
                                      start_new_session=True, stdout=subprocess.PIPE,
                                      text=True)
            fd = None
            try:
                import select
                self.assertTrue(select.select([parent.stdout], [], [], 5)[0],
                                "synthetic parent did not start")
                child = int(parent.stdout.readline())
                fd = os.pidfd_open(child)
                members = [p for p in platform.process_table() if p["pgid"] == parent.pid]
                with mock.patch.object(background.storage, "of"):
                    entry = self.tracker.start(agent="test", turn_id="real-expiry",
                                               pgid=parent.pid, members=members,
                                               containers=[], grace_s=1, now=0)
                self.tracker.TERM_GRACE_S = .5
                with mock.patch.object(os, "killpg") as killpg:
                    self.tracker._kill(entry)
                killpg.assert_not_called()
                self.assertEqual(parent.wait(timeout=5), -signal.SIGTERM)
                self.assertTrue(select.select([fd], [], [], 5)[0],
                                "reparented child survived escalation")
                self.assertTrue(os.path.exists(detached), "child did not leave the old group")
                self.assertEqual(entry.handles, {})
            finally:
                if fd is not None:
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.close(fd)
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                parent.stdout.close()


if __name__ == "__main__":
    unittest.main()
