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

    def test_duration_variable_against_a_subsecond_threshold(self):
        """#5302 : la mesure a quitté l'assertion, H1 ne la voit plus."""
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    debut = time.monotonic()\n"
            "    wake_s = time.monotonic() - debut\n"
            "    self.assertLess(wake_s, 0.05)\n"), ["H4"])

    def test_threshold_first_and_chained_durations(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    debut = time.time()\n"
            "    elapsed = time.time() - debut\n"
            "    reste = elapsed\n"
            "    self.assertGreater(0.2, reste)\n"), ["H4"])

    def test_duration_against_a_second_or_more_is_not_h4(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    debut = time.monotonic()\n"
            "    elapsed = time.monotonic() - debut\n"
            "    self.assertLess(elapsed, 5)\n"), [])

    def test_zero_is_a_logic_bound_not_a_latency(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    debut = time.monotonic()\n"
            "    reste = debut - time.monotonic()\n"
            "    self.assertLess(reste, 0)\n"), [])

    def test_a_non_clock_value_is_not_a_duration(self):
        self.assertEqual(self.rules(
            "def test_x(self):\n"
            "    elapsed = compute()\n"
            "    self.assertLess(elapsed, 0.05)\n"), [])

    def test_self_attribute_duration(self):
        self.assertEqual(self.rules(
            "import time\n"
            "class T:\n"
            "    def setUp(self):\n"
            "        self.elapsed = time.monotonic() - 0\n"
            "    def test_x(self):\n"
            "        self.assertLess(self.elapsed, 0.4)\n"), ["H4"])

    def test_named_subsecond_threshold(self):
        self.assertEqual(self.rules(
            "import time\n"
            "BUDGET = 0.05\n"
            "def test_x(self):\n"
            "    debut = time.monotonic()\n"
            "    wake_s = time.monotonic() - debut\n"
            "    self.assertLess(wake_s, BUDGET)\n"), ["H4"])

    def test_disk_usage_is_h5(self):
        self.assertEqual(self.rules(
            "import shutil\n"
            "def test_x(self):\n"
            "    libre = shutil.disk_usage('/').free\n"), ["H5"])

    def test_patched_disk_usage_is_not_a_read(self):
        self.assertEqual(self.rules(
            "from unittest import mock\n"
            "def test_x(self):\n"
            "    with mock.patch.object(shutil, 'disk_usage'):\n"
            "        pass\n"), [])

    def test_disk_usage_exemption(self):
        self.assertEqual(self.rules(
            "import shutil\n"
            "def test_x(self):\n"
            "    # hermeticity: allow — disque simulé par le banc\n"
            "    shutil.disk_usage('/')\n"), [])

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

    def test_punctuation_only_reason_does_not_exempt(self):
        for reason in ("—", "-", ".", "... !?", "_", "( )"):
            with self.subTest(reason=reason):
                self.assertEqual(self.rules(
                    "import time\n"
                    "def test_x(self):\n"
                    f"    # hermeticity: allow — {reason}\n"
                    "    self.assertLess(time.time() - debut, 5)\n"), ["H1"])

    def test_exemption_ascii_separator_and_unicode_reason(self):
        for reason in ("échéance contrôlée", "(échéance contrôlée)", "... 5 s"):
            with self.subTest(reason=reason):
                self.assertEqual(self.rules(
                    "import time\n"
                    "def test_x(self):\n"
                    f"    # hermeticity: allow - {reason}\n"
                    "    self.assertLess(time.time() - debut, 5)\n"), [])

    def test_exemption_inside_a_multiline_assertion(self):
        self.assertEqual(self.rules(
            "import time\n"
            "def test_x(self):\n"
            "    self.assertTrue(\n"
            "        # hermeticity: allow - le banc mesure la garde de 5 s\n"
            "        time.time() - debut < 5)\n"), [])

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

    def test_rules_filter_keeps_only_the_requested_ones(self):
        path = self.file(
            "import shutil, time\n"
            "def test_x(self):\n"
            "    shutil.disk_usage('/')\n"
            "    self.assertLess(time.time() - debut, 5)\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hermeticity.main([str(path), "--rules", "H5"])
        self.assertEqual(rc, 1)
        self.assertIn("H5", out.getvalue())
        self.assertNotIn("H1 horloge", out.getvalue())

    def test_unknown_rule_is_a_usage_error(self):
        path = self.file("def test_x(self):\n    pass\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = hermeticity.main([str(path), "--rules", "H9"])
        self.assertEqual(rc, 2)
        self.assertIn("H9", err.getvalue())

    def test_max_findings_truncates_the_text_not_the_judgement(self):
        path = self.file(
            "import time\n"
            "def test_x(self):\n"
            "    self.assertLess(time.time() - debut, 5)\n"
            "    self.assertLess(time.time() - debut, 6)\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hermeticity.main([str(path), "--max-findings", "1"])
        self.assertEqual(rc, 1)
        self.assertIn("de plus", out.getvalue())
        self.assertIn("2 constat(s)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
