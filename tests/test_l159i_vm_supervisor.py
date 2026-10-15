# SPDX-License-Identifier: AGPL-3.0-only
"""VM supervisor lifecycle with fake Podman; no VM or network access."""
import json
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest.mock import patch

from ameesh.mediated_executor import vm_supervisor as V


class Process:
    def __init__(self, rc, running=False):
        self.rc = rc
        self.returncode = rc
        self.running = running

    def poll(self):
        if self.running:
            self.running = False
            return None
        return self.rc

    def wait(self, **kwargs):
        return self.rc


@unittest.skipUnless(sys.platform == 'linux', 'VM supervision uses Linux rootless Podman')
class VMSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        example = Path(__file__).resolve().parents[1] / 'deploy/guest/mediated-recipe.example.json'
        self.recipe = json.loads(example.read_text())
        for key in ('volume', 'state_dir', 'ack_dir', 'enroll_dir'):
            p = self.root / key
            p.mkdir()
            self.recipe[key] = str(p)
        self.state = self.root / 'state_dir/state.json'
        self.state.write_text(json.dumps({'schema': 'ameesh-host-state/1', 'state': 'available',
                                        'seq': 1, 'until_ts': 200, 'expires_ts': 190}))
        self.code = self.root / 'enroll_dir/code'
        self.code.write_text('private-code')
        self.uid = patch.object(V.os, 'geteuid', return_value=10001)
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def supervise(self, launch, stop=None, **kwargs):
        return V.supervise(self.recipe, stop or threading.Event(), launch=launch,
                           now=lambda: 100, log=lambda _: None, **kwargs)

    def test_exit_codes_preserve_volume_except_revocation_and_clear_code(self):
        marker = self.root / 'volume/executor.json'
        for rc in (0, 2, 3, 4, 5, 6, 125, 126, 127):
            with self.subTest(rc=rc):
                marker.write_text('enrolled')
                self.code.write_text('private-code')
                self.assertEqual(self.supervise(lambda _: Process(rc)), rc)
                self.assertFalse(self.code.exists())
                self.assertEqual(marker.exists(), rc != 6)

    def test_only_exit_one_retries_with_bounded_exponential_delay(self):
        class Stop:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return False
            def wait(self, delay):
                self.delays.append(delay)
                return len(self.delays) == 7
        stop = Stop()
        self.assertEqual(self.supervise(lambda _: Process(1), stop), 0)
        self.assertEqual(stop.delays, [30, 60, 120, 240, 480, 600, 600])

    def test_old_ack_does_not_clear_current_enrollment_code(self):
        ack = self.root / 'ack_dir/ack.json'
        ack.write_text('{"schema":"ameesh-host-ack/1"}')
        def launch(_):
            self.assertTrue(self.code.exists())
            self.assertFalse(ack.exists())
            return Process(0)
        self.supervise(launch)

    def test_current_ack_clears_code_before_process_exits(self):
        class Stop:
            def is_set(inner):
                return False
            def wait(inner, _):
                self.assertFalse(self.code.exists())
                return False
        def launch(_):
            (self.root / 'ack_dir/ack.json').write_text('{"schema":"ameesh-host-ack/1"}')
            return Process(0, running=True)
        self.supervise(launch, Stop())

    def test_missing_expired_malformed_or_draining_gate_never_launches(self):
        for raw in (None, {}, {'schema': 'ameesh-host-state/1', 'state': 'available',
                              'seq': 1, 'until_ts': 99},
                    {'schema': 'ameesh-host-state/1', 'state': 'draining',
                     'seq': 1, 'until_ts': 200, 'expires_ts': 190}):
            with self.subTest(raw=raw):
                if raw is None:
                    self.state.unlink(missing_ok=True)
                else:
                    self.state.write_text(json.dumps(raw))
                stop = threading.Event()
                def waited(_):
                    stop.set()
                    return True
                with patch.object(stop, 'wait', side_effect=waited):
                    self.assertEqual(self.supervise(lambda _: self.fail('launched'), stop), 0)

    def test_signal_stops_container_with_hundred_seconds_grace(self):
        stop = threading.Event()
        def launch(_):
            stop.set()
            return Process(0, running=True)
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return Process(0)
        self.assertEqual(self.supervise(launch, stop, run=run), 0)
        self.assertEqual(calls[0][0], ['podman', 'stop', '--ignore', '--time=100', V.NAME])
        self.assertEqual(calls[0][1]['timeout'], 110)

    def test_stale_gate_stops_existing_container_and_preserves_volume(self):
        def launch(_):
            self.state.unlink()
            return Process(0, running=True)
        calls = []
        self.assertEqual(self.supervise(launch, run=lambda argv, **_: calls.append(argv) or Process(0)), 0)
        self.assertIn('--time=100', calls[0])

    def test_dead_guard_during_long_window_is_stopped(self):
        raw = {'schema': 'ameesh-host-state/1', 'state': 'available', 'seq': 1,
               'until_ts': 9000, 'expires_ts': 99}
        self.state.write_text(json.dumps(raw))
        self.assertEqual(V.gate_state(str(self.state), 100), 'stopped')
        raw['expires_ts'] = 190
        self.state.write_text(json.dumps(raw))
        def launch(_):
            raw['expires_ts'] = 99
            self.state.write_text(json.dumps(raw))
            return Process(0, running=True)
        calls = []
        self.assertEqual(self.supervise(launch, run=lambda argv, **_: calls.append(argv) or Process(0)), 0)
        self.assertIn('--time=100', calls[0])

    def test_fresh_guard_with_no_window_launches(self):
        self.state.write_text(json.dumps({'schema': 'ameesh-host-state/1', 'state': 'available',
                                         'seq': 1, 'until_ts': None, 'expires_ts': 190}))
        calls = []
        self.assertEqual(self.supervise(lambda argv: calls.append(argv) or Process(0)), 0)
        self.assertEqual(len(calls), 1)

    def test_image_file_gate_requires_fresh_heartbeat_even_without_supervisor(self):
        from ameesh.mediated_executor.host_gate import FileGate, from_config
        from ameesh.config import Config
        import dataclasses
        cfg = dataclasses.replace(Config(), host_gate='file', host_gate_lending=True,
                                  host_gate_path=str(self.state), host_gate_fallback='available')
        gate = from_config(cfg)
        self.addCleanup(gate.close)
        with patch('ameesh.mediated_executor.host_gate.time.time', return_value=100):
            for expiry, expected in ((99, 'stopped'), (None, 'stopped'), (190, 'available'),
                                     (10000, 'stopped')):
                self.state.write_text(json.dumps({'schema': 'ameesh-host-state/1',
                    'state': 'available', 'seq': 1, 'until_ts': None, 'expires_ts': expiry}))
                self.assertEqual(gate.state().state, expected)

    def test_legacy_file_gate_stale_heartbeat_falls_back_and_absent_one_is_obeyed(self):
        """N6 : sur le chemin legacy (`lending=False`), un `expires_ts` périmé
        retombe sur le repli, un état sans `expires_ts` reste obéi."""
        from ameesh.mediated_executor.host_gate import from_config
        from ameesh.config import Config
        import dataclasses
        cfg = dataclasses.replace(Config(), host_gate='file', host_gate_lending=False,
                                  host_gate_path=str(self.state), host_gate_fallback='available')
        gate = from_config(cfg)
        self.addCleanup(gate.close)
        with patch('ameesh.mediated_executor.host_gate.time.time', return_value=100):
            self.state.write_text(json.dumps({'schema': 'ameesh-host-state/1',
                'state': 'stopped', 'seq': 3, 'until_ts': None, 'expires_ts': 99}))
            self.assertEqual(gate.state().state, 'available')
            self.state.write_text(json.dumps({'schema': 'ameesh-host-state/1',
                'state': 'stopped', 'seq': 4, 'until_ts': None}))
            self.assertEqual(gate.state().state, 'stopped')

    def test_unsupported_python_refused_before_any_launch(self):
        """N3 : le wipe de révocation exige `shutil.rmtree(dir_fd=)` (3.11) ;
        sur 3.9/3.10 `supervise` refuse avant de lancer, au lieu de lever une
        TypeError au moment d'effacer le volume."""
        with patch.object(V.sys, 'version_info', (3, 10, 14)):
            with self.assertRaisesRegex(ValueError, r'requires Python >= 3\.11'):
                self.supervise(lambda _: self.fail('launched'))
        with patch.object(V.sys, 'version_info', (3, 11, 0)):
            self.assertEqual(self.supervise(lambda _: Process(0)), 0)

    def test_require_supported_python_bounds(self):
        """N3 : la borne se contrôle sans dépendre du Python qui exécute la
        suite."""
        V.require_supported_python((3, 11, 0))
        V.require_supported_python((3, 12, 4))
        with self.assertRaisesRegex(ValueError, r'requires Python >= 3\.11.*found 3\.9'):
            V.require_supported_python((3, 9, 18))

    def test_wipe_does_not_follow_guest_symlink(self):
        protected = self.root / 'protected'
        protected.mkdir()
        (protected / 'keep').write_text('audit')
        (self.root / 'volume/link').symlink_to(protected)
        self.assertEqual(self.supervise(lambda _: Process(6)), 6)
        self.assertTrue((protected / 'keep').exists())
        self.assertEqual(list((self.root / 'volume').iterdir()), [])

    def test_parent_symlink_and_nonregular_lock_refused(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'volume')
        with self.assertRaises(OSError):
            V.directory(str(alias))
        lock = self.root / 'enroll_dir/vm-supervisor.lock'
        V.os.mkfifo(lock)
        with self.assertRaises(ValueError):
            self.supervise(lambda _: self.fail('launched'))

    def test_duplicate_supervisor_refused_before_launch(self):
        fd = V.directory(self.recipe['enroll_dir'])
        try:
            lock = V.os.open('vm-supervisor.lock', V.os.O_CREAT | V.os.O_RDWR,
                             0o600, dir_fd=fd)
            try:
                V.fcntl.flock(lock, V.fcntl.LOCK_EX | V.fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    self.supervise(lambda _: self.fail('launched'))
            finally:
                V.os.close(lock)
        finally:
            V.os.close(fd)

    def test_code_cleanup_failure_still_stops_live_container(self):
        stop = threading.Event()
        class LiveProcess:
            alive = True
            def poll(inner):
                return None if inner.alive else 0
            def wait(inner, **_):
                return 0
        process = LiveProcess()
        def launch(_):
            stop.set()
            return process
        calls = []
        def run(argv, **_):
            calls.append(argv)
            if argv[1] == 'kill':
                process.alive = False
            return Process(0)
        with patch.object(V, 'remove_code', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                self.supervise(launch, stop, run=run)
        self.assertIn(['podman', 'kill', V.NAME], calls)
        self.assertFalse(process.alive)


if __name__ == '__main__':
    unittest.main()
