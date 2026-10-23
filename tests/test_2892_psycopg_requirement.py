# SPDX-License-Identifier: AGPL-3.0-only
"""The installer must provide Connection.notifies(timeout=), added in 3.2."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PsycopgRequirementTest(unittest.TestCase):
    def test_all_installation_paths_require_timeout_support(self):
        # These TOML string arrays are also Python literals; keep this test
        # runnable on Python 3.9 without adding a TOML/packaging dependency.
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for key, count in (("dependencies", 2), ("psycopg", 1)):
            with self.subTest(installation=key):
                match = re.search(r"^" + key + r"\s*=\s*(\[.*?\])[^\S\n]*$", text,
                                  re.MULTILINE | re.DOTALL)
                self.assertIsNotNone(match)
                requirements = [r for r in ast.literal_eval(match.group(1))
                                if r.startswith("psycopg")]
                self.assertEqual(len(requirements), count)
                for requirement in requirements:
                    with self.subTest(requirement=requirement):
                        bound = re.search(r">=([0-9]+)\.([0-9]+)", requirement)
                        self.assertIsNotNone(bound, requirement)
                        self.assertGreaterEqual(
                            tuple(map(int, bound.groups())), (3, 2), requirement)


if __name__ == "__main__":
    unittest.main()
