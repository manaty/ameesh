# SPDX-License-Identifier: AGPL-3.0-only
"""The pilot check flags native guest-gate use without printing secrets."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from . import support

spec = importlib.util.spec_from_file_location(
    "native_gate_check", Path(support.REPO) / "deploy/guest/check-native-gates.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class NativeGateCheckTest(unittest.TestCase):
    def test_default_none_explicit_file_guest_and_live_part(self):
        for ident, choice, part, expected in (
                ("runner-a@host", "", False, 0),
                ("runner-a@host", "none", False, 0),
                ("runner-a@host", "file", False, 1),
                ("guest-a@host", "file", False, 0),
                ("runner-a@host", "", True, 1)):
            with self.subTest(ident=ident, choice=choice, part=part), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proc = root / "100"
                proc.mkdir()
                (proc / "cmdline").write_bytes(
                    ("python\0/venv/bin/ameesh\0run\0--runner-id\0" + ident + "\0").encode())
                (proc / "status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
                (proc / "environ").write_bytes(
                    ("HOME=" + tmp + "\0AMEESH_HOST_GATE=" + choice + "\0AMEESH_DSN=secret\0").encode())
                (proc / "fd").mkdir()
                if part:
                    (proc / "fd/9").symlink_to("/run/ameesh-gate/ack/parts/part.lock")
                output = []
                with mock.patch.object(check.pwd, "getpwuid", return_value=SimpleNamespace(
                        pw_name="ameesh", pw_dir=tmp)):
                    self.assertEqual(check.check(root, output.append), expected)
                self.assertNotIn("secret", "\n".join(output))
