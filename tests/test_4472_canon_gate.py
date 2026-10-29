# SPDX-License-Identifier: AGPL-3.0-only
"""A complete canon sync releases claims before slow review designation work."""
import threading
import types
import unittest
from unittest import mock

from ameesh.runner import Runner


class CanonGateTest(unittest.TestCase):
    def runner(self):
        runner = object.__new__(Runner)
        runner.cfg = types.SimpleNamespace(canon="/canon")
        runner._gate_settled = threading.Event()
        runner.stop = threading.Event()
        runner.canon_thread = None
        runner.canon_sync_interval = 300
        runner.once = False
        runner._canon_sync_counts = (1, 1)
        return runner

    def test_complete_sync_opens_gate_while_designations_are_still_running(self):
        for mode in ("periodic", "once", "zero-interval"):
            with self.subTest(mode=mode):
                runner = self.runner()
                runner.once = mode == "once"
                if mode == "zero-interval":
                    runner.canon_sync_interval = 0
                entered = threading.Event()
                release = threading.Event()

                def sync():
                    runner._canon_complete = True
                    return True

                def retry():
                    entered.set()
                    release.wait(5)

                with mock.patch.object(runner, "canon_sync_once", side_effect=sync), \
                        mock.patch.object(runner, "retry_review_designations", side_effect=retry), \
                        mock.patch("ameesh.runner.log_async"):
                    starter = threading.Thread(target=runner.start_canon_sync)
                    starter.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        self.assertTrue(runner._gate_settled.is_set())
                    finally:
                        runner.stop.set()
                        release.set()
                        starter.join(3)
                        if runner.canon_thread:
                            runner.canon_thread.join(3)
                    self.assertFalse(starter.is_alive())

    def test_incomplete_sync_keeps_gate_closed_and_reports_pass(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                runner = self.runner()
                runner._canon_complete = False
                runner._canon_sync_counts = (1, 2)
                with mock.patch.object(runner, "canon_sync_once", return_value=ok), \
                        mock.patch.object(runner, "retry_review_designations") as retry, \
                        mock.patch("ameesh.runner.log_async") as log:
                    self.assertFalse(runner._canon_sync_pass())
                self.assertFalse(runner._gate_settled.is_set())
                retry.assert_not_called()
                messages = [call.args[0] for call in log.call_args_list]
                self.assertTrue(any("canons 1/2" in message and
                                    ("incomplete" if ok else "failed") in message
                                    for message in messages))
