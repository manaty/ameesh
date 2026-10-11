# SPDX-License-Identifier: AGPL-3.0-only
"""L223 — permission mode of interactive Claude sessions (`ameesh attach`).

Turns under the runner pass `--dangerously-skip-permissions`; an interactive
session (attach, chat) passed no mode and stopped on permission prompts that
the owner, on mobile, cannot approve. It now passes `--permission-mode auto`
(never `bypassPermissions` by default), adjustable through
`AMEESH_CLAUDE_PERMISSION_MODE` and per agent (`ameesh set <agent>
permission_mode=…`), falling back to `acceptEdits` when the binary's help does
not know the mode.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from ameesh import adapters, harnesses, registry
from ameesh.runner import run_attach

from .support import FAKEBIN, PgTestCase

VARIABLE = "AMEESH_CLAUDE_PERMISSION_MODE"


def _without_variable() -> dict:
    return {k: v for k, v in os.environ.items() if k != VARIABLE}


class PermissionModeTest(unittest.TestCase):
    def setUp(self):
        adapters._HELP_CACHE.clear()
        self.addCleanup(adapters._HELP_CACHE.clear)

    def _claude(self, **env):
        base = dict(_without_variable(), AMEESH_BIN_DIR=FAKEBIN, **env)
        patch = mock.patch.dict(os.environ, base, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        return adapters.adapter_for("claude")

    def test_attach_passes_auto_by_default(self):
        claude = self._claude()
        self.assertEqual(claude.interactive_command("s1"),
                         [claude.binary, "--permission-mode", "auto", "--resume", "s1"])
        self.assertEqual(claude.interactive_command(),
                         [claude.binary, "--permission-mode", "auto"])

    def test_never_bypass_by_default(self):
        rule = harnesses.get("claude").interactive_permission
        self.assertEqual(rule["default"], "auto")
        self.assertNotIn("bypassPermissions", (rule["default"], rule["fallback"]))
        # turns under the runner keep their own, separate mode
        self.assertIn("--dangerously-skip-permissions", harnesses.get("claude").command)

    def test_variable_overrides_the_mode(self):
        claude = self._claude(**{VARIABLE: "acceptEdits"})
        self.assertEqual(claude.interactive_command("s1")[1:3],
                         ["--permission-mode", "acceptEdits"])

    def test_agent_setting_wins_over_variable(self):
        claude = self._claude(**{VARIABLE: "acceptEdits"})
        argv = claude.interactive_command("s1", permission_mode="plan")
        self.assertEqual(argv[1:3], ["--permission-mode", "plan"])

    def test_malformed_mode_ignored(self):
        claude = self._claude(**{VARIABLE: "auto --dangerously-skip-permissions"})
        argv = claude.interactive_command(None, permission_mode="x;rm")
        self.assertEqual(argv[1:], ["--permission-mode", "auto"])

    def test_older_version_falls_back_to_accept_edits(self):
        claude = self._claude(AMEESH_TEST_CLAUDE_HELP=(
            '  --permission-mode <mode>  (choices: "acceptEdits", '
            '"bypassPermissions", "default", "plan")'))
        self.assertEqual(claude.interactive_command()[1:], ["--permission-mode", "acceptEdits"])

    def test_mode_allowlist(self):
        # codex verdict #182 (1): a well-formed but unknown mode is dropped,
        # even when the help is unreadable; bypassPermissions only from the
        # explicit agent setting
        claude = self._claude(**{VARIABLE: "notAMode"})
        self.assertEqual(claude.interactive_command()[1:], ["--permission-mode", "auto"])
        adapters._HELP_CACHE.clear()
        with mock.patch.object(adapters.subprocess, "run", side_effect=OSError("absent")):
            self.assertEqual(claude.permission_mode(), "acceptEdits")
            self.assertEqual(claude.permission_mode("notAMode"), "acceptEdits")
        adapters._HELP_CACHE.clear()
        claude = self._claude(**{VARIABLE: "bypassPermissions"})
        self.assertEqual(claude.permission_mode(), "auto")
        self.assertEqual(claude.permission_mode("bypassPermissions"), "bypassPermissions")

    def test_auto_looked_up_in_the_option_choices_only(self):
        # codex verdict #182 (2): "auto" elsewhere in the help does not count
        claude = self._claude(AMEESH_TEST_CLAUDE_HELP=(
            '  --auto-update             Enable auto updates\n'
            '  --permission-mode <mode>  Permission mode\n'
            '                            (choices: "acceptEdits", "plan")\n'
            '  --model <model>           e.g. "auto"'))
        self.assertEqual(claude.permission_mode(), "acceptEdits")
        adapters._HELP_CACHE.clear()
        claude = self._claude(AMEESH_TEST_CLAUDE_HELP=(
            '  --permission-mode <mode>  Permission mode to use\n'
            '                            (choices: "acceptEdits", "auto",\n'
            '                            "plan")\n'
            '  --plugin-dir <path>'))
        self.assertEqual(claude.permission_mode(), "auto")

    def test_help_with_invalid_bytes_or_timeout(self):
        # codex verdict #182 (3): neither UnicodeDecodeError nor a timeout
        # propagates; a timed-out help yields the fallback
        claude = self._claude()
        output = subprocess.CompletedProcess(
            [], 0, b'Usage: claude \xff\xfe\n  --permission-mode <mode> (choices: "auto", "plan") \xc3\n',
            b"\xe9")
        with mock.patch.object(adapters.subprocess, "run", return_value=output):
            self.assertEqual(claude.permission_mode(), "auto")
        adapters._HELP_CACHE.clear()
        timeout = subprocess.TimeoutExpired(["claude", "--help"], 10)
        with mock.patch.object(adapters.subprocess, "run", side_effect=timeout):
            self.assertEqual(claude.permission_mode(), "acceptEdits")
        adapters._HELP_CACHE.clear()
        with mock.patch.object(adapters, "_option_choices", side_effect=RuntimeError("x")):
            self.assertEqual(claude.permission_mode(), "acceptEdits")

    def test_codex_and_deepseek_unchanged(self):
        self._claude()
        codex = adapters.adapter_for("codex")
        self.assertEqual(codex.permission_mode(), "")
        self.assertEqual(codex.interactive_command("t1"), [codex.binary, "resume", "t1"])
        dsh = adapters.adapter_for("deepseek")
        self.assertEqual(dsh.permission_mode("auto"), "")

    def test_descriptor_valid(self):
        with open(harnesses.get("claude").path, encoding="utf-8") as fh:
            document = json.load(fh)
        errors = [f for f in harnesses.validate(document) if f.severity == harnesses.ERROR]
        self.assertEqual(errors, [])
        self.assertNotIn("harness-key-unknown", [f.code for f in harnesses.validate(document)])
        document["ameesh"]["interactive_permission"] = {"flag": ["--permission-mode"],
                                                        "default": "auto"}
        codes = [f.code for f in harnesses.validate(document)]
        self.assertIn("harness-permission-mode-invalid", codes)


class AttachPermissionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        adapters._HELP_CACHE.clear()
        self.addCleanup(adapters._HELP_CACHE.clear)
        cwd = os.path.join(self.tmp, "work", "perm")
        os.makedirs(cwd, exist_ok=True)
        self.register("perm", "claude", cwd=cwd)

    def _attach(self, **env) -> list[str]:
        base = dict(_without_variable(), AMEESH_BIN_DIR=FAKEBIN,
                    AMEESH_TEST_LOG=self.turns_log, **env)
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertEqual(run_attach(self.cfg, self.db, "perm", ttl=30), 0)
        return self.turns()[-1]["argv"]

    def test_attach_runs_claude_in_auto_mode(self):
        argv = self._attach()
        self.assertEqual(argv[:2], ["--permission-mode", "auto"])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_attach_environment_variable(self):
        self.assertEqual(self._attach(**{VARIABLE: "acceptEdits"})[:2],
                         ["--permission-mode", "acceptEdits"])

    def test_attach_agent_setting(self):
        proc = self.mesh("set", "perm", "permission_mode=plan")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("interactive permission mode (attach): plan", proc.stdout)
        self.assertEqual(self._attach(**{VARIABLE: "acceptEdits"})[:2],
                         ["--permission-mode", "plan"])
        proc = self.mesh("set", "perm", "permission_mode=")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._attach()[:2], ["--permission-mode", "auto"])
        self.assertEqual(self.mesh("set", "perm", "permission_mode=a b").returncode, 2)
        self.assertIsNotNone(registry.get(self.db, "perm"))

    def test_attach_unreadable_setting_ignored(self):
        # codex verdict #182 (P2): byte 0xff in the agent setting
        state_dir = self.cfg.agent_dir("perm")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "permission_mode"), "wb") as fh:
            fh.write(b"\xffplan")
        self.assertEqual(self._attach()[:2], ["--permission-mode", "auto"])
        self.assertIsNone(registry.get(self.db, "perm")["lease_owner"])

    def test_attach_ctrl_c_while_preparing_releases_the_lease(self):
        # codex verdict #182 (P2): Ctrl-C during a slow `claude --help`
        base = dict(_without_variable(), AMEESH_BIN_DIR=FAKEBIN, AMEESH_TEST_LOG=self.turns_log)
        real_run = subprocess.run

        def run(argv, *a, **k):
            if argv[-1:] == ["--help"]:
                raise KeyboardInterrupt
            return real_run(argv, *a, **k)
        with mock.patch.dict(os.environ, base, clear=True), \
                mock.patch.object(adapters.subprocess, "run", side_effect=run):
            self.assertEqual(run_attach(self.cfg, self.db, "perm", ttl=30), 130)
        self.assertIsNone(registry.get(self.db, "perm")["lease_owner"])
        # the interrupted help is not cached as unreadable
        self.assertEqual(self._attach()[:2], ["--permission-mode", "auto"])

    def test_attach_survives_a_failing_help(self):
        # codex verdict #182 (3): an error while reading the help must neither
        # abort the attach nor keep the lease
        with mock.patch.object(adapters, "_option_choices", side_effect=UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte")):
            self.assertEqual(self._attach()[:2], ["--permission-mode", "acceptEdits"])
        self.assertIsNone(registry.get(self.db, "perm")["lease_owner"])


if __name__ == "__main__":
    unittest.main()
