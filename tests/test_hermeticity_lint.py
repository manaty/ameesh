# SPDX-License-Identifier: AGPL-3.0-only
"""#707 : le lint d'herméticité des tests (`scripts/test-hermeticity-lint.py`).

Sans base : chaque règle sur une source construite, l'exemption motivée, le
mode JSON et le code de sortie. Le lint lit des fichiers de test (AST), il ne
lit pas `docs/` : pas de déclaration L161a.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "hermeticity_lint", REPO / "scripts" / "test-hermeticity-lint.py")
hermeticity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hermeticity)


class RulesTest(unittest.TestCase):
    """Une règle, un cas positif et le cas voisin qui ne doit pas mordre."""

    def rules(self, source: str) -> list[str]:
        return [finding.rule for finding in hermeticity.lint_source("t.py", source)]

    def test_elapsed_time_assertion(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    debut = time.time()\n"
            "    self.assertLess(time.time() - debut, 5)\n"), ["H1"])

    def test_clock_as_input_is_not_a_measure(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    self.assertEqual(len(f(now=time.time() + 3600)), 1)\n"
            "    self.assertGreater(row['expires_ts'], time.time())\n"), [])

    def test_monotonic_is_clock_too(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    self.assertLess(time.monotonic() - debut, 5)\n"), ["H1"])

    def test_machine_state(self):
        self.assertEqual(self.rules(
            "from ameesh import platform\n"
            "def test_x(self):\n"
            "    platform.process_table()\n"), ["H2"])

    def test_proc_path_composed(self):
        self.assertEqual(self.rules(
            "def test_x(self):\n"
            "    open('/proc/%d/stat' % pid)\n"), ["H2"])

    def test_proc_in_a_string_is_not_a_read(self):
        self.assertEqual(self.rules(
            "def test_x(self):\n"
            "    \"\"\"le repli /proc/self/stat est testé ailleurs.\"\"\"\n"
            "    self.assertTrue(True)\n"), [])

    def test_calendar_fixture(self):
        self.assertEqual(self.rules(
            "import datetime\n"
            "def test_x(self):\n"
            "    jour = datetime.date.today()\n"), ["H3"])

    def test_epoch_fixture_is_not_calendar(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    maintenant = time.time()\n"), [])

    def test_calendar_in_a_measure_is_h1_only(self):
        self.assertEqual(self.rules(
            "import datetime\n"
            "def test_x(self):\n"
            "    self.assertLess(datetime.datetime.now() - debut, 5)\n"), ["H1"])

    def test_exemption_marker(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    # hermeticity: allow — le banc mesure la garde de 5 s\n"
            "    self.assertLess(time.time() - debut, 5)\n"), [])

    def test_exemption_inline_comment(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    debut = time.time()\n"
            "    self.assertLess(time.time() - debut, 5)  # hermeticity: allow — garde mesurée\n"),
            [])

    def test_exemption_without_reason_does_not_exempt(self):
        """Un commentaire vide ne supprime pas le constat (R1 #707, N1)."""
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    # hermeticity: allow —\n"
            "    self.assertLess(time.time() - debut, 5)\n"), ["H1"])
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    # hermeticity: allow\n"
            "    self.assertLess(time.time() - debut, 5)\n"), ["H1"])

    def test_marker_in_a_string_does_not_exempt(self):
        """Le marqueur doit être un commentaire, pas une chaîne de code."""
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    note = 'hermeticity: allow — dans une chaîne'\n"
            "    self.assertLess(time.time() - debut, 5)\n"), ["H1"])

    def test_exemption_on_a_multiline_assertion(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    # hermeticity: allow — le banc mesure la garde de 5 s\n"
            "    self.assertTrue(\n"
            "        time.time() - debut < 5)\n"), [])

    def test_syntax_error_is_reported(self):
        self.assertEqual(self.rules("def test_x(:\n"), ["H0"])


class CliTest(unittest.TestCase):
    """Le contrat de sortie : code 0/1/2 et schéma JSON."""

    def file(self, source: str) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "test_case.py"
        path.write_text(source, encoding="utf-8")
        return path

    def test_exit_code_and_json(self):
        path = self.file(
            "import time\n"
            "def test_x(self):\n"
            "    self.assertLess(time.time() - debut, 5)\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hermeticity.main([str(path), "--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema"], "ameesh-hermeticity-lint/1")
        self.assertEqual(payload["findings"][0]["rule"], "H1")
        self.assertEqual(payload["summary"]["findings"], 1)

    def test_clean_file_exits_zero(self):
        path = self.file("def test_x(self):\n    self.assertTrue(True)\n")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = hermeticity.main([str(path)])
        self.assertEqual(rc, 0)

    def test_files_and_diff_are_exclusive(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = hermeticity.main(["tests/x.py", "--diff", "origin/main"])
        self.assertEqual(rc, 2)
        self.assertIn("pas les deux", err.getvalue())


if __name__ == "__main__":
    unittest.main()
