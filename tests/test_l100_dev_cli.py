# SPDX-License-Identifier: AGPL-3.0-only
"""L100 T5 (#4386) — `ameesh module dev …` and its aliases without a removal date.

`ameesh module dev sync-merges | sync-github | project-github | merge-pr |
review-class` is served by the `dev` module's `main`, found through the module
registry (capability `cli`). The old commands (`ameesh work sync-merges…`,
`ameesh review-class`) stay aliases (L104 keeps them): each new command gives
the same output, in `--json` too, and the same exit code as its alias.
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import support
from ameesh import main as main_mod, mesh_cli, modules
from ameesh.modules import dev, registry

SRC = str(Path(__file__).resolve().parents[1] / "src")

#: new command → its alias, with arguments the parser accepts
WORK_CASES = {
    "sync-merges": ["--git-dir", "/nonexistent", "--dry-run", "--json"],
    "sync-github": ["--repo", "manaty/x", "--dry-run", "--json"],
    "project-github": ["--app", "acme", "--dry-run", "--json"],
    "merge-pr": ["1", "--head", "abc123", "--lot", "4386",
                 "--dry-run", "--json"],
}


def _run(argv):
    """`ameesh <argv>` in process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = isolated_entrypoint(main_mod.main, list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _seen(args):
    """What a handler received: the parsed namespace, without bound functions."""
    return {key: value for key, value in sorted(vars(args).items()) if key != "func"}


class RegistryTest(unittest.TestCase):
    def test_dev_serves_the_cli_capability_through_the_registry(self):
        found = registry.discover().get("dev")
        self.assertIn("cli", found.capabilities)
        self.assertEqual(found.cli, "ameesh module dev")
        self.assertIs(modules.installed()["dev"].main, dev.main)
        self.assertEqual(sorted(dev.COMMANDS),
                         ["merge-pr", "project-github", "review-class",
                          "sync-github", "sync-merges"])


class WorkAliasTest(unittest.TestCase):
    """Same parser, same handler, same output and exit code as `work …`."""

    def _both(self, command, arguments, *, code=0):
        calls = []

        def handler(*args):
            namespace = args[-1]
            calls.append(_seen(namespace))
            print(json.dumps({"command": command, "seen": calls[-1]}, default=str))
            return code

        db = mock.Mock()
        with mock.patch.object(mesh_cli, "_open", return_value=db), \
                mock.patch.object(mesh_cli, "_work_actor"), \
                mock.patch("ameesh.plan_cli.run", side_effect=handler), \
                mock.patch("ameesh.modules.dev.merge_pr.run", side_effect=handler), \
                mock.patch("ameesh.db.connect",
                           side_effect=AssertionError("module dev opened a database")):
            old = _run(["work", command, *arguments])
            new = _run(["module", "dev", command, *arguments])
        self.assertEqual(len(calls), 2, (old, new))
        return old, new

    def test_each_work_command_equals_its_alias(self):
        for command, arguments in WORK_CASES.items():
            with self.subTest(command=command):
                old, new = self._both(command, arguments)
                self.assertEqual(new, old)
                self.assertEqual(old[0], 0)
                self.assertEqual(json.loads(new[1])["command"], command)

    def test_exit_code_is_the_alias_exit_code(self):
        for command, arguments in WORK_CASES.items():
            with self.subTest(command=command):
                old, new = self._both(command, arguments, code=3)
                self.assertEqual((new[0], new[1]), (old[0], old[1]))
                self.assertEqual(new[0], 3)

    def test_usage_error_is_the_alias_usage_error(self):
        with mock.patch.object(mesh_cli, "_open"):
            old = _run(["work", "sync-merges", "--no-such-option"])
            new = _run(["module", "dev", "sync-merges", "--no-such-option"])
        self.assertEqual((new[0], new[1]), (old[0], old[1]))
        self.assertEqual(new[0], 2)


class ModuleDevUsageTest(unittest.TestCase):
    def test_help_and_unknown_command(self):
        code, out, _ = _run(["module", "dev", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("review-class", out)
        self.assertIn("L104", out)
        code, out, err = _run(["module", "dev"])
        self.assertEqual(code, 2)
        code, out, err = _run(["module", "dev", "fire"])
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)


class ReviewClassAliasTest(unittest.TestCase):
    """`review-class` reads no database: real runs, compared byte for byte."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name, "canon")
        root.mkdir()
        (root / "federation.yaml").write_text(
            "id: test\nroot: test\nmembers: []\nreview_policies:\n"
            "  risk_classes:\n    default: normal\n    rules:\n"
            "      - paths: ['**/*.sql']\n        class: sensitive\n"
            "      - paths: ['docs/**']\n        class: light\n", encoding="utf-8")
        self.work = Path(self.tmp.name, "work")
        self.work.mkdir()
        self.env = support.child_env(
            AMEESH_CANON=str(root), AMEESH_CANON_UNTRUSTED="1",
            AMEESH_STATE=str(Path(self.tmp.name, "state")),
            PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))

    def mesh(self, *argv):
        proc = subprocess.run([sys.executable, "-m", "ameesh", *argv], cwd=self.work,
                              env=self.env, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout

    def test_review_class_equals_its_alias(self):
        cases = (
            ["db/x.sql", "docs/a.md"],
            ["--json", "docs/a.md"],
            ["--json", "db/x.sql", "docs/a.md"],
            [],                          # neither files nor --diff: usage, 2
            ["--diff", "HEAD"],          # outside a git repository: 1
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                old = self.mesh("review-class", *arguments)
                new = self.mesh("module", "dev", "review-class", *arguments)
                self.assertEqual(new, old)
        self.assertEqual(json.loads(self.mesh("module", "dev", "review-class",
                                              "--json", "docs/a.md")[1])["class"], "light")
        self.assertEqual(self.mesh("module", "dev", "review-class")[0], 2)


if __name__ == "__main__":
    unittest.main()
