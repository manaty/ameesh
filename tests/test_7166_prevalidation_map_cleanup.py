# SPDX-License-Identifier: AGPL-3.0-only
"""#7166: a map downloaded at the deadline is removed before returning."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ameesh import mechanics, prevalidation as pv, prevalidation_calls as calls
from ameesh.deadline import Deadline


class MapCleanupTests(unittest.TestCase):
    def test_expiry_after_download_removes_map_without_starting_heavy_work(self):
        now = [100.0]
        deadline = Deadline(5.0, clock=lambda: now[0], wall=160.0)
        params = calls.Parameters(params=mechanics.Prevalidation(
            classes=("P0", "P1"), contracts=(), hosts=("vm1",),
            timeout_seconds=60, parts_script="scripts/test-parts.py"),
            repo="/canon", commit="c" * 40, project="ameesh")
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            download = Path(root) / "download"
            download.mkdir()
            carte = download / "carte.json.gz"
            carte.write_bytes(b"downloaded map")

            def map_for(*args, **kwargs):
                now[0] += 6.0
                return str(carte), str(download)

            with mock.patch.object(calls, "parameters", return_value=params), \
                    mock.patch.object(calls, "tooling"), \
                    mock.patch.object(calls, "map_for", side_effect=map_for), \
                    mock.patch.object(calls, "heavy_queue") as queue, \
                    mock.patch.object(calls, "prepare") as prepare, \
                    mock.patch.object(calls, "_run_hosts") as hosts:
                with self.assertRaises(pv.PrevalidationError) as caught:
                    calls.run(None, None, 7166, repo="/repo", head="a" * 40,
                              base="b" * 40, project="ameesh", record=False,
                              deadline=deadline)
            self.assertEqual(caught.exception.cause, "run-incomplete")
            self.assertIn("avant la file heavy", caught.exception.why)
            self.assertFalse(download.exists())
            queue.assert_not_called()
            prepare.assert_not_called()
            hosts.assert_not_called()
