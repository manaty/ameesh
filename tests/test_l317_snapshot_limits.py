# SPDX-License-Identifier: AGPL-3.0-only
"""Sparse/growing files, slow I/O and aggregate snapshot storage are bounded."""
import contextlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tests import support
from ameesh.modules.it import execution, snapshot_limits
from ameesh.modules.it.proof_service import produce


class SnapshotLimitsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "store")
        self.root.mkdir()
        self.artifact = Path(folder.name, "artifact")
        self.artifact.write_bytes(b"abcd")
        self.limits = {"max_bytes": 1024, "total_bytes": 2048, "min_free_bytes": 1, "copy_seconds": 2}

    def copies(self):
        return list(self.root.glob("execution-*"))

    def snapshot(self, **options):
        return execution.snapshot(self.artifact, root=self.root, limits=self.limits, **options)

    def test_sparse_file_is_rejected_by_stat_before_copy_or_launch(self):
        with self.artifact.open("wb") as source:
            source.truncate(1024 * 1024)  # logical 1 MiB, zero allocated blocks
        self.assertLess(self.artifact.stat().st_blocks * 512, 1024 * 1024)
        with mock.patch.object(execution.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "size limit"):
                execution.execute(self.artifact, snapshot_root=self.root, snapshot_limits=self.limits,
                                  expected_sha="0" * 64)
        launch.assert_not_called()
        self.assertEqual(self.copies(), [])

    def test_file_growth_is_rejected_by_actual_byte_counter_and_cleaned(self):
        self.limits["max_bytes"] = 4
        original = execution.open_artifact

        def grow_after_stat(*args, **kwargs):
            path, stream = original(*args, **kwargs)
            # Grow after the initial descriptor stat but before the first read.
            read = stream.read
            def grow(size):
                self.artifact.write_bytes(b"abcdef")
                return read(size)
            stream.read = grow
            return path, stream

        with mock.patch.object(execution, "open_artifact", side_effect=grow_after_stat):
            with self.assertRaisesRegex(ValueError, "copied-byte limit"):
                with self.snapshot():
                    self.fail("oversized snapshot admitted")
        self.assertEqual(self.copies(), [])

    def test_copy_time_is_checked_before_writing_a_slow_read(self):
        original = execution.open_artifact
        def slow(*args, **kwargs):
            path, stream = original(*args, **kwargs)
            read = stream.read
            def wait(size):
                time.sleep(.03)
                return read(size)
            stream.read = wait
            return path, stream
        # The loop check works even outside the authoritative helper's main
        # thread; disable the signal here to exercise that independent guard.
        with mock.patch.object(execution, "open_artifact", side_effect=slow), \
                mock.patch.object(snapshot_limits, "copy_timer", side_effect=lambda deadline: contextlib.nullcontext()):
            with self.assertRaisesRegex(ValueError, "copy timed out"):
                with self.snapshot(deadline=time.monotonic() + .01):
                    self.fail("late read admitted")
        self.assertEqual(self.copies(), [])

    @unittest.skipUnless(hasattr(__import__('signal'), 'setitimer'), "POSIX interval timer required")
    def test_main_thread_timer_interrupts_blocking_copy_io(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "copy timed out"):
            with snapshot_limits.copy_timer(started + .03):
                time.sleep(1)
        self.assertLess(time.monotonic() - started, .5)

    def test_volume_reserve_refuses_before_creating_copy(self):
        with mock.patch.object(snapshot_limits.shutil, "disk_usage", return_value=mock.Mock(free=4)):
            with self.assertRaisesRegex(ValueError, "free-space reserve"):
                with self.snapshot():
                    self.fail("reserve ignored")
        self.assertEqual(self.copies(), [])

    def test_volume_pressure_during_copy_aborts_and_cleans_partial_bytes(self):
        checks = [mock.Mock(free=100), mock.Mock(free=100), mock.Mock(free=0)]
        with mock.patch.object(snapshot_limits.shutil, "disk_usage", side_effect=checks):
            with self.assertRaisesRegex(ValueError, "free-space reserve"):
                with self.snapshot():
                    self.fail("pressure ignored")
        self.assertEqual(self.copies(), [])

    def test_total_budget_counts_existing_or_crashed_snapshots(self):
        self.limits["total_bytes"] = 10
        stale = self.root / "execution-stale"
        stale.mkdir()
        (stale / "artifact").write_bytes(b"12345678")
        with self.assertRaisesRegex(ValueError, "total byte limit"):
            with self.snapshot():
                self.fail("aggregate limit ignored")
        self.assertEqual(self.copies(), [stale])
        self.assertEqual((stale / "artifact").read_bytes(), b"12345678")

    def test_live_snapshot_remains_accounted_while_other_copy_is_admitted(self):
        self.limits["total_bytes"] = 7
        with self.snapshot() as first:
            with self.assertRaisesRegex(ValueError, "total byte limit"):
                with self.snapshot():
                    self.fail("live copy ignored")
            self.assertEqual(Path(first[1]).read_bytes(), b"abcd")
        self.assertEqual(self.copies(), [])

    def test_copy_lock_wait_has_a_deadline(self):
        started = time.monotonic()
        with snapshot_limits.store_lock(self.root, started + 1):
            with self.assertRaisesRegex(ValueError, "copy timed out"):
                with snapshot_limits.store_lock(self.root, started + .03):
                    self.fail("concurrent copy admitted")
        self.assertLess(time.monotonic() - started, .5)

    def test_nonprivate_store_lock_is_refused(self):
        lock = self.root / ".snapshot.lock"
        lock.write_text("")
        lock.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private and producer-owned"):
            with self.snapshot():
                self.fail("unsafe lock admitted")
        self.assertEqual(self.copies(), [])

    def test_expired_timeout_and_bad_limits_never_create_or_run_snapshot(self):
        for limits in ({"max_bytes": 0}, {"total_bytes": -1}, {"min_free_bytes": True},
                       {"copy_seconds": float('inf')}, {"max_bytes": 1.5}, {"extra": 1}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                snapshot_limits.settings(limits)
        with self.assertRaisesRegex(ValueError, "copy timed out"):
            with self.snapshot(deadline=time.monotonic() - 1):
                self.fail("expired deadline admitted")
        self.assertEqual(self.copies(), [])

    def test_project_limits_are_private_helper_settings_not_request_fields(self):
        payload = dict(project="acme", lot=1, artifact=str(self.artifact), runner=None, arguments=[], timeout=60)
        with mock.patch("ameesh.modules.it.item", return_value={"app": "acme"}), \
                mock.patch("ameesh.modules.it.prove", return_value={"id": 1}) as prove:
            produce(mock.Mock(), payload, actor="uid:1000", execution_user="worker", snapshot_limits=self.limits)
        self.assertEqual(prove.call_args.kwargs["snapshot_limits"], self.limits)
        with self.assertRaisesRegex(ValueError, "execution inputs only"):
            produce(mock.Mock(), dict(payload, snapshot_limits={"max_bytes": 9999999999}),
                    actor="uid:1000", execution_user="worker")
