# SPDX-License-Identifier: AGPL-3.0-only
"""Native/guest gate selection and bounded acknowledgement retries (#501)."""
import errno
import os
import tempfile
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import config, host_gate


class NativeGateTest(unittest.TestCase):
    def test_native_lender_does_not_take_guest_gate(self):
        cfg = config.Config(runner_id="native:test")
        self.assertEqual(host_gate.gate_kind(cfg, lending=True), "")
        self.assertIsNone(host_gate.from_config(cfg, lending=True))

    def test_explicit_file_override_is_preserved(self):
        cfg = config.Config(runner_id="native:test", host_gate="file")
        self.assertEqual(host_gate.gate_kind(cfg, lending=True), "file")

    def test_guest_lender_defaults_to_file(self):
        cfg = config.Config(runner_id="guest-test")
        self.assertEqual(host_gate.gate_kind(cfg, lending=True), "file")

    def test_ack_failure_backs_off_and_logs_only_error_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = host_gate.FileGate(ack_path=os.path.join(tmp, "ack.json"), use_inotify=False)
            self.addCleanup(gate.close)
            clock = [100.0]
            notes = []
            gate._log = notes.append
            ack = host_gate.GateAck(seq=1, state="draining", ts=100)
            with mock.patch.object(host_gate.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(os, "makedirs", side_effect=OSError(errno.ENOSPC, "full")) as write:
                for tick in (100, 100, 100.9, 101, 102, 103, 106, 107):
                    clock[0] = tick
                    self.assertFalse(gate.acknowledge(ack))
                self.assertEqual(write.call_count, 4)  # 100, 101, 103, 107
                self.assertEqual(len(notes), 1)
                clock[0] = 115
                write.side_effect = OSError(errno.EACCES, "denied")
                self.assertFalse(gate.acknowledge(ack))
                self.assertEqual(len(notes), 2)

    def test_ack_retry_caps_at_sixty_seconds_and_resets_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = host_gate.FileGate(ack_path=os.path.join(tmp, "ack.json"), use_inotify=False)
            self.addCleanup(gate.close)
            ack = host_gate.GateAck(seq=1, state="available", ts=100)
            clock = [100.0]
            with mock.patch.object(host_gate.time, "monotonic", side_effect=lambda: clock[0]):
                with mock.patch.object(os, "makedirs", side_effect=OSError(errno.ENOSPC, "full")):
                    for delay in (1, 2, 4, 8, 16, 32, 60, 60):
                        self.assertFalse(gate.acknowledge(ack))
                        self.assertEqual(gate._ack_retry_at - clock[0], delay)
                        clock[0] += delay
                self.assertTrue(gate.acknowledge(ack))
                with mock.patch.object(os, "makedirs", side_effect=OSError(errno.ENOSPC, "full")):
                    self.assertFalse(gate.acknowledge(ack))
                    self.assertEqual(gate._ack_retry_at - clock[0], 1)

    def test_guest_account_is_detected_without_trusting_user_environment(self):
        import pwd
        from types import SimpleNamespace
        cfg = config.Config(runner_id="native:test")
        with mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_name="ameesh-guest")):
            self.assertEqual(host_gate.gate_kind(cfg, lending=True), "file")
        with mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_name="ameesh")), \
                mock.patch.dict(os.environ, {"USER": "ameesh-guest"}):
            self.assertEqual(host_gate.gate_kind(cfg, lending=True), "")

    def test_permission_denial_detaches_native_gate_but_keeps_guest_closed(self):
        import threading
        from ameesh.runner import Runner
        for ident in ("native:test", "guest-test"):
            for error in (errno.EACCES, errno.EPERM):
                with self.subTest(ident=ident, error=error), tempfile.TemporaryDirectory() as tmp:
                    runner = Runner.__new__(Runner)
                    runner.cfg = config.Config(runner_id=ident, host_gate="file")
                    runner.runner_id, runner.host = ident, "host"
                    runner._host_gate_lock, runner.lock = threading.Lock(), threading.Lock()
                    runner.workers = {}
                    runner._gate_running = False
                    runner._host_gate_key = ("file", True, 90)
                    runner.wake_all = threading.Event()
                    gate = host_gate.FileGate(ack_path=os.path.join(tmp, "ack.json"), use_inotify=False)
                    self.addCleanup(gate.close)
                    ctl = host_gate.GateController(runner, gate)
                    runner.host_gate = ctl
                    ctl.apply(host_gate.GateState("available", 1))
                    with mock.patch.object(ctl, "_hold_slots"), \
                            mock.patch.object(os, "makedirs", side_effect=OSError(error, "denied")):
                        ctl.tick()
                    if ident.startswith("guest-"):
                        self.assertIs(runner.host_gate, ctl)
                        self.assertTrue(ctl.holds_turns())
                        self.assertFalse(ctl.may_claim())
                        ctl.apply(host_gate.GateState("available", 2))
                        self.assertFalse(ctl.may_claim())
                    else:
                        self.assertIsNone(runner.host_gate)
                        self.assertIn("not my gate", runner._host_gate_rejected)
                        self.assertTrue(ctl._stop.is_set())
                        runner._sync_host_gate(True, None)
                        self.assertIsNone(runner.host_gate)

    def test_guest_permission_failure_stays_closed_until_ack_success(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            runner = SimpleNamespace(cfg=config.Config(runner_id="guest-test"),
                                     runner_id="guest-test", workers={})
            gate = host_gate.FileGate(ack_path=os.path.join(tmp, "ack.json"), use_inotify=False)
            self.addCleanup(gate.close)
            ctl = host_gate.GateController(runner, gate)
            ctl.apply(host_gate.GateState("available", 1))
            clock = [100.0]
            with mock.patch.object(host_gate.time, "monotonic", side_effect=lambda: clock[0]):
                with mock.patch.object(os, "makedirs", side_effect=OSError(errno.EACCES, "denied")):
                    ctl.tick()
                self.assertFalse(ctl.may_claim())
                clock[0] += 1
                with mock.patch.object(os, "makedirs", side_effect=OSError(errno.ENOSPC, "full")):
                    ctl.tick()
                ctl.apply(host_gate.GateState("available", 2))
                self.assertFalse(ctl.may_claim())
                clock[0] += 2
                ctl.tick()
                ctl.apply(host_gate.GateState("available", 3))
                self.assertTrue(ctl.may_claim())

    def test_guest_first_ack_enospc_closes_until_success(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            runner = SimpleNamespace(cfg=config.Config(runner_id="guest-test"),
                                     runner_id="guest-test", workers={})
            gate = host_gate.FileGate(ack_path=os.path.join(tmp, "ack.json"), use_inotify=False)
            self.addCleanup(gate.close)
            ctl = host_gate.GateController(runner, gate)
            clock = [100.0]
            with mock.patch.object(gate, "state", return_value=host_gate.GateState("available", 1)), \
                    mock.patch.object(host_gate.time, "monotonic", side_effect=lambda: clock[0]):
                with mock.patch.object(os, "makedirs", side_effect=OSError(errno.ENOSPC, "full")):
                    ctl.refresh()
                    self.assertFalse(ctl.may_claim())
                    ctl.refresh()  # backoff must not reopen
                    self.assertFalse(ctl.may_claim())
                clock[0] += 1
                ctl.refresh()
                ctl.refresh()
                self.assertTrue(ctl.may_claim())

    def test_guest_uid_refuses_gate_override_before_runner_initialization(self):
        import pwd
        from types import SimpleNamespace
        from ameesh.runner import Runner
        for kind in ("none", "bogus"):
            with self.subTest(kind=kind), \
                    mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_name="ameesh-guest")):
                cfg = config.Config(runner_id="native:test", host_gate=kind)
                with self.assertRaisesRegex(SystemExit, "guest.*file"):
                    Runner(cfg, None)

    def test_guest_unit_identity_cannot_be_hidden_by_environment(self):
        import pwd
        from types import SimpleNamespace
        with mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_name="ameesh")), \
                mock.patch("builtins.open", mock.mock_open(read_data="0::/system.slice/ameesh-guest-runner@test.service\n")), \
                mock.patch.dict(os.environ, {"USER": "ameesh", "SYSTEMD_UNIT": "native.service"}):
            self.assertTrue(host_gate.is_guest(config.Config(runner_id="native:test")))
            with self.assertRaisesRegex(ValueError, "guest.*file"):
                host_gate.gate_kind(config.Config(host_gate="none"))

    def test_native_first_ack_enospc_keeps_explicit_gate_and_backoff(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            runner = SimpleNamespace(cfg=config.Config(runner_id="native:test", host_gate="file"),
                                     runner_id="native:test", workers={}, reject_host_gate=mock.Mock())
            gate = host_gate.FileGate(ack_path=os.path.join(tmp, "ack.json"), use_inotify=False)
            self.addCleanup(gate.close)
            ctl = host_gate.GateController(runner, gate)
            with mock.patch.object(gate, "state", return_value=host_gate.GateState("available", 1)), \
                    mock.patch.object(os, "makedirs", side_effect=OSError(errno.ENOSPC, "full")) as write:
                ctl.refresh()
                ctl.refresh()
                self.assertTrue(ctl.may_claim())
                self.assertEqual(write.call_count, 1)
                runner.reject_host_gate.assert_not_called()

    def test_guest_override_refused_even_if_database_is_unavailable(self):
        import io
        import pwd
        from types import SimpleNamespace
        from ameesh import runner
        with mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_name="ameesh-guest")), \
                mock.patch.object(runner, "load_config", return_value=config.Config(host_gate="none")), \
                mock.patch.object(runner, "_connect_at_start") as connect, \
                mock.patch.object(runner.sys, "stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(runner.main(["--once", "--runner-id", "native:test"]), 2)
            self.assertIn("guest runner requires host_gate=file", stderr.getvalue())
            connect.assert_not_called()
