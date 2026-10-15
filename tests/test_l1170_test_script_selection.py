# SPDX-License-Identifier: AGPL-3.0-only
"""#1170: exercise the real shell script with two isolated unittest modules.

Only interpreter dependency checks are stubbed; unittest actually loads and
runs the requested tests. No database, package installation or repo discovery.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from . import support  # isolate the host's agent environment

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test.sh"
# #1207 : chaque temoin lance de vrais interpretes Python sous le script. La
# R1 #1170 a signale le litteral de 15 s comme fragile sous charge CI, sans
# aucun echec constate. Il reste un garde-fou anti-blocage, mais nomme et
# mesure : 0,2 a 0,4 s par temoin au repos (module 10/10 en 2,2 s psycopg et
# 3,9 s psql, #1207), donc 120 s gardent une marge >250x tout en restant tres
# en dessous du plafond du job ou le module tourne (45 min psql, 30 min psycopg).
WITNESS_TIMEOUT_S = 120


class TestScriptSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("scripts", "tests", "fakebin", ".venv/bin"):
            (self.root / directory).mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "scripts/test.sh")
        (self.root / "tests/__init__.py").touch()
        for name in ("alpha", "beta"):
            (self.root / f"tests/test_{name}.py").write_text(
                "import os, unittest\n"
                "class Witness(unittest.TestCase):\n"
                f"    def test_{name}(self):\n"
                "        with open(os.environ['SELECTION_EVENTS'], 'a') as out:\n"
                f"            out.write(os.environ['AMEESH_DRIVER'] + ':{name}\\n')\n"
            )
        # Use the running Python for both drivers, with no external dependencies.
        proxy = (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "if sys.argv[1:2] == ['-c']:\n"
            "    sys.exit(0 if os.path.exists('deps-ready') else 1)\n"
            "with open(os.environ['SELECTION_CALLS'], 'a') as out:\n"
            "    out.write(json.dumps([os.environ['AMEESH_DRIVER'], sys.argv[1:]]) + '\\n')\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        )
        for path in ("fakebin/python3", ".venv/bin/python"):
            target = self.root / path
            target.write_text(proxy)
            target.chmod(0o755)
        (self.root / "deps-ready").touch()
        pip = self.root / ".venv/bin/pip"
        pip.write_text("#!/usr/bin/env bash\ntouch deps-ready\n")
        pip.chmod(0o755)
        self.env = dict(os.environ, AMEESH_PG_UP="0", AMEESH_TEST_VENV=".venv",
                        PYTHONPATH="", SELECTION_EVENTS=str(self.root / "events"),
                        SELECTION_CALLS=str(self.root / "calls"))
        self.env["PATH"] = str(self.root / "fakebin") + os.pathsep + os.environ["PATH"]

    def run_selection(self, args, expected, *, named=False):
        done = subprocess.run(["bash", str(self.root / "scripts/test.sh"), *args],
                              cwd=self.root, env=self.env, text=True,
                              capture_output=True, timeout=WITNESS_TIMEOUT_S)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        events = (self.root / "events").read_text().splitlines()
        self.assertCountEqual(events, [f"{driver}:{test}" for driver in
                                      ("psql", "psycopg") for test in expected])
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        prefix = ["-m", "unittest", "-v"] if named else [
            "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
        self.assertEqual(calls, [[driver, prefix + args] for driver in ("psql", "psycopg")])

    def test_one_module_excludes_foreign_witness_on_both_drivers(self):
        self.run_selection(["tests.test_alpha"], ["alpha"], named=True)

    def test_multiple_modules_on_both_drivers(self):
        self.run_selection(["tests.test_alpha", "tests.test_beta"],
                           ["alpha", "beta"], named=True)

    def test_qualified_test_name(self):
        self.run_selection(["tests.test_beta.Witness.test_beta"], ["beta"], named=True)

    def test_no_arguments_keeps_discovery_command(self):
        self.run_selection([], ["alpha", "beta"])

    def test_discovery_filename_filter(self):
        self.run_selection(["-p", "test_alpha*.py"], ["alpha"])

    def test_long_discovery_filename_filter(self):
        self.run_selection(["--pattern=test_beta*.py"], ["beta"])

    def test_discovery_test_name_filter(self):
        self.run_selection(["-k", "alpha"], ["alpha"])

    def test_options_before_module_names(self):
        self.run_selection(["-k", "alpha", "tests.test_alpha", "tests.test_beta"],
                           ["alpha"], named=True)

    def test_psycopg_installation_branch_uses_same_selection(self):
        (self.root / "deps-ready").unlink()
        self.run_selection(["tests.test_alpha"], ["alpha"], named=True)
        self.assertTrue((self.root / "deps-ready").exists())

    def test_discovery_options_with_names_fail_instead_of_running_suite(self):
        done = subprocess.run(["bash", str(self.root / "scripts/test.sh"),
                               "-p", "test_*.py", "tests.test_alpha"],
                              cwd=self.root, env=self.env, text=True,
                              capture_output=True, timeout=WITNESS_TIMEOUT_S)
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((self.root / "events").exists())
