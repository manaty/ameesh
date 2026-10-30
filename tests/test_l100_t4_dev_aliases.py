# SPDX-License-Identifier: AGPL-3.0-only
"""L100 T4: development review aliases preserve identity and global patches."""
import importlib
import subprocess
import sys
import unittest
from unittest import mock

from tests import support  # noqa: F401


class ReviewAliasesTest(unittest.TestCase):
    def test_identity_in_both_import_orders_in_fresh_interpreters(self):
        for name in ("review", "lot_class", "review_target"):
            for old_first in (True, False):
                with self.subTest(name=name, old_first=old_first):
                    names = [f"ameesh.{name}", f"ameesh.modules.dev.{name}"]
                    if not old_first:
                        names.reverse()
                    code = (
                        "import importlib, sys; "
                        f"a = importlib.import_module({names[0]!r}); "
                        f"b = importlib.import_module({names[1]!r}); "
                        "assert a is b; "
                        f"import ameesh; assert ameesh.{name} is a"
                    )
                    result = subprocess.run([sys.executable, "-c", code],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_patches_through_legacy_paths_reach_implementations(self):
        for name, attr in (("review", "parse"), ("lot_class", "plan_git"),
                           ("review_target", "canon")):
            with self.subTest(name=name):
                implementation = importlib.import_module(f"ameesh.modules.dev.{name}")
                original = getattr(implementation, attr)
                with mock.patch(f"ameesh.{name}.{attr}") as patched:
                    self.assertIs(getattr(implementation, attr), patched)
                self.assertIs(getattr(implementation, attr), original)
