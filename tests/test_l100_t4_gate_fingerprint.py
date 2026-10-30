# SPDX-License-Identifier: AGPL-3.0-only
"""L100 T4: the gate cache follows moved code, including classification."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import merge_queue


class MovedGateFingerprintTest(unittest.TestCase):
    def test_each_moved_implementation_changes_the_gate_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in merge_queue._GATE_CODE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"original")
            with mock.patch.object(merge_queue, "__file__", str(root / "merge_queue.py")):
                for name in ("lot_class", "review", "review_target"):
                    with self.subTest(name=name):
                        path = root / "modules" / "dev" / (name + ".py")
                        with mock.patch.object(merge_queue, "_GATE_CODE_FINGERPRINT", None):
                            before = merge_queue._gate_code_version()
                        path.write_bytes(b"changed implementation")
                        with mock.patch.object(merge_queue, "_GATE_CODE_FINGERPRINT", None):
                            after = merge_queue._gate_code_version()
                        self.assertNotEqual(before, after)
                        path.write_bytes(b"original")
