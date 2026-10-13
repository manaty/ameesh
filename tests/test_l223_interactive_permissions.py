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
import contextlib
import dataclasses
import io
import unittest
from unittest import mock

from ameesh import adapters, harness_cli, harnesses, registry
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

    def test_bypass_reserved_without_explicit_only(self):
        # codex r4 (#182) P1: a descriptor omitting `explicit_only` does not
        # make bypassPermissions a default, a fallback nor a variable value
        for key in ("default", "fallback"):
            raw = {"flag": ["--permission-mode", "{mode}"], "default": "auto",
                   "fallback": "acceptEdits", key: "bypassPermissions"}
            messages = [f.message for f in harnesses._check_interactive_permission(raw, "")]
            self.assertTrue(any("reserved" in m and key in m for m in messages), messages)
            normalized = harnesses._interactive_permission(raw)
            self.assertEqual(normalized[key], "")
            self.assertIn("bypassPermissions", normalized["explicit_only"])
        claude = self._claude()
        rule = {k: v for k, v in harnesses.get("claude").interactive_permission.items()
                if k != "explicit_only"}
        for key in ("default", "fallback"):
            adapters._HELP_CACHE.clear()
            raw = dict(rule, **{key: "bypassPermissions"})
            claude.descriptor = dataclasses.replace(claude.descriptor,
                                                    interactive_permission=raw)
            with mock.patch.object(adapters.subprocess, "run", side_effect=OSError("no help")):
                self.assertNotEqual(claude.permission_mode(), "bypassPermissions", key)
                self.assertNotIn("bypassPermissions", claude.interactive_command("s1"), key)
        claude.descriptor = dataclasses.replace(claude.descriptor, interactive_permission=rule)
        adapters._HELP_CACHE.clear()
        with mock.patch.dict(os.environ, {VARIABLE: "bypassPermissions"}):
            self.assertNotEqual(claude.permission_mode(), "bypassPermissions")
        adapters._HELP_CACHE.clear()
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

    def test_deepseek_unchanged(self):
        self._claude()
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

    def test_reserved_mode_never_default_nor_fallback(self):
        # codex r3 (#182) P1: refused by validation, dropped by normalization
        # and refused again at resolution
        for key in ("default", "fallback"):
            raw = {"flag": ["--permission-mode", "{mode}"], "default": "auto",
                   "fallback": "acceptEdits", "explicit_only": ["bypassPermissions"],
                   key: "bypassPermissions"}
            messages = [f.message for f in harnesses._check_interactive_permission(raw, "")]
            self.assertTrue(any("reserved" in m and key in m for m in messages), messages)
            self.assertEqual(harnesses._interactive_permission(raw)[key], "")
        claude = self._claude()
        rule = dict(harnesses.get("claude").interactive_permission)
        # the fallback case with the help unavailable, as codex reproduced it
        for key in ("default", "fallback"):
            adapters._HELP_CACHE.clear()
            raw = dict(rule, **{key: "bypassPermissions"})
            claude.descriptor = dataclasses.replace(claude.descriptor,
                                                    interactive_permission=raw)
            with mock.patch.object(adapters.subprocess, "run", side_effect=OSError("no help")):
                self.assertNotEqual(claude.permission_mode(), "bypassPermissions", key)
                self.assertNotIn("bypassPermissions", claude.interactive_command("s1"), key)
        # the agent setting still may
        claude.descriptor = dataclasses.replace(claude.descriptor, interactive_permission=rule)
        adapters._HELP_CACHE.clear()
        self.assertEqual(claude.permission_mode("bypassPermissions"), "bypassPermissions")


class CodexPermissionTest(unittest.TestCase):
    """L685: `ameesh attach <codex>` — bypass only from the agent setting, and
    no shared background daemon (the owner is never asked to choose)."""

    def setUp(self):
        adapters._HELP_CACHE.clear()
        self.addCleanup(adapters._HELP_CACHE.clear)
        base = dict(_without_variable(), AMEESH_BIN_DIR=FAKEBIN)
        patch = mock.patch.dict(os.environ, base, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.codex = adapters.adapter_for("codex")

    def test_attach_defaults_unchanged(self):
        # (3) mode not set: no bypass flag, codex keeps its permission prompts;
        # --no-daemon (4) keeps the session off the shared background server
        self.assertEqual(self.codex.permission_mode(), "")
        argv = self.codex.interactive_command("t1")
        self.assertEqual(argv, [self.codex.binary, "--no-daemon", "resume", "t1"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_explicit_bypass_adds_the_flag(self):
        # (1)(2) the flag carries no `{mode}`: bypassPermissions selects
        # `--dangerously-bypass-approvals-and-sandbox`
        argv = self.codex.interactive_command("t1", permission_mode="bypassPermissions")
        self.assertEqual(argv, [self.codex.binary, "--no-daemon",
                                "--dangerously-bypass-approvals-and-sandbox",
                                "resume", "t1"])

    def test_bypass_never_from_env_nor_default(self):
        # like claude: the mode is explicit only, no variable, no default
        rule = harnesses.get("codex").interactive_permission
        self.assertEqual(rule["env"], ())
        self.assertEqual(rule["default"], "")
        self.assertEqual(rule["choices"], ("bypassPermissions",))
        self.assertEqual(rule["explicit_only"], ("bypassPermissions",))
        # a descriptor that would make the bypass a default is refused and
        # normalized back to an empty default
        raw = {"modes": {"bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"]},
               "default": "bypassPermissions"}
        messages = [f.message for f in harnesses._check_interactive_permission(raw, "")]
        self.assertTrue(any("reserved" in m and "default" in m for m in messages), messages)
        self.assertEqual(harnesses._interactive_permission(raw)["default"], "")

    def test_modes_deliver_every_choice_and_default(self):
        # L685: without a `{mode}` template, a choice the mapping does not
        # deliver would be silently ignored: validation refuses it
        for key, value in (("choices", ["bypassPermissions", "plan"]), ("default", "plan")):
            raw = {"modes": {"bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"]},
                   key: value}
            messages = [f.message for f in harnesses._check_interactive_permission(raw, "")]
            self.assertTrue(any("plan" in m and "delivered" in m for m in messages), messages)

    def test_modes_empty_table_delivers_nothing(self):
        # L685 review r1 P2: `{"modes": {}, "choices": ["plan"], "default": "plan"}`
        # passed validation (the delivery check was guarded by a non-empty table)
        # and then normalized to {} — the choices were silently lost
        raw = {"modes": {}, "choices": ["plan"], "default": "plan"}
        messages = [f.message for f in harnesses._check_interactive_permission(raw, "")]
        self.assertTrue(any("plan" in m and "delivered" in m for m in messages), messages)
        self.assertEqual(harnesses._interactive_permission(raw), {})
        # a table whose only entries are invalid delivers nothing either
        broken = {"modes": {"plan": ["--permission-mode", "{mode}"]}, "choices": ["plan"]}
        messages = [f.message for f in harnesses._check_interactive_permission(broken, "")]
        self.assertTrue(any("delivered" in m for m in messages), messages)

    def test_modes_mapping_validation(self):
        good = {"modes": {"bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"]}}
        self.assertEqual([f for f in harnesses._check_interactive_permission(good, "")
                          if f.severity == harnesses.ERROR], [])
        self.assertEqual(harnesses._interactive_permission(good)["modes"],
                         (("bypassPermissions",
                           ("--dangerously-bypass-approvals-and-sandbox",)),))
        for bad in ({"modes": ["--flag"]},
                    {"modes": {"not a mode": ["--flag"]}},
                    {"modes": {"plan": "--flag"}},
                    {"modes": {"plan": []}},
                    {"modes": {"plan": ["--permission-mode", "{mode}"]}},
                    {}):
            with self.subTest(raw=bad):
                codes = [f.code for f in harnesses._check_interactive_permission(bad, "")]
                self.assertIn("harness-permission-mode-invalid", codes)
                self.assertEqual(harnesses._interactive_permission(bad), {})

    def test_modes_only_delivery_is_never_probed(self):
        # codex's flag carries no mode: `--help` is not read (the fake codex
        # prints nothing) and the mode is still delivered
        with mock.patch.object(adapters.subprocess, "run",
                               side_effect=AssertionError("no --help probe")) as run:
            self.assertEqual(self.codex.permission_mode("bypassPermissions"),
                             "bypassPermissions")
        run.assert_not_called()

    def test_descriptor_valid(self):
        path = harnesses.get("codex").path
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
        findings = harnesses.validate(document)
        self.assertEqual([f for f in findings if f.severity == harnesses.ERROR], [])
        self.assertNotIn("harness-key-unknown", [f.code for f in findings])
        self.assertEqual(document["ameesh"]["interactive"], ["--no-daemon"])
        # harness show renders the mapping
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness_cli._show_text(harnesses.get("codex"))
        self.assertIn("bypassPermissions", out.getvalue())
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", out.getvalue())


class AttachPermissionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        adapters._HELP_CACHE.clear()
        self.addCleanup(adapters._HELP_CACHE.clear)
        cwd = os.path.join(self.tmp, "work", "perm")
        os.makedirs(cwd, exist_ok=True)
        self.register("perm", "claude", cwd=cwd)

    def _attach(self, name: str = "perm", **env) -> list[str]:
        base = dict(_without_variable(), AMEESH_BIN_DIR=FAKEBIN,
                    AMEESH_TEST_LOG=self.turns_log, **env)
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertEqual(run_attach(self.cfg, self.db, name, ttl=30), 0)
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

    def test_set_permission_mode_checked_against_choices(self):
        # codex r3 (#182) P2: an unknown mode is refused and not stored
        proc = self.mesh("set", "perm", "permission_mode=notAMode")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("invalid permission_mode", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.agent_dir("perm"),
                                                     "permission_mode")))
        proc = self.mesh("set", "perm", "permission_mode=bypassPermissions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # a harness without an interactive permission mode refuses the setting
        cwd = os.path.join(self.tmp, "work", "dsh")
        os.makedirs(cwd, exist_ok=True)
        self.register("dsh", "deepseek", cwd=cwd)
        proc = self.mesh("set", "dsh", "permission_mode=auto")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("declares no interactive permission mode", proc.stderr)

    def test_attach_codex_bypass_explicit_and_without_daemon(self):
        # L685: codex attach — the bypass flag only through the agent setting,
        # and the session never joins the shared background daemon (the owner
        # is not asked to choose on a settings conflict)
        cwd = os.path.join(self.tmp, "work", "cx685")
        os.makedirs(cwd, exist_ok=True)
        self.register("cx685", "codex", cwd=cwd)
        argv = self._attach("cx685")
        # no session yet: a fresh session, still without the daemon
        self.assertEqual(argv[:1], ["--no-daemon"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        proc = self.mesh("set", "cx685", "permission_mode=auto")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("invalid permission_mode", proc.stderr)
        proc = self.mesh("set", "cx685", "permission_mode=bypassPermissions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("interactive permission mode (attach): bypassPermissions", proc.stdout)
        argv = self._attach("cx685")
        self.assertEqual(argv[:2], ["--no-daemon",
                                    "--dangerously-bypass-approvals-and-sandbox"])
        self.assertIsNone(registry.get(self.db, "cx685")["lease_owner"])

    def test_attach_unreadable_setting_ignored(self):
        # codex verdict #182 (P2): byte 0xff in the agent setting
        state_dir = self.cfg.agent_dir("perm")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "permission_mode"), "wb") as fh:
            fh.write(b"\xffplan")
        self.assertEqual(self._attach()[:2], ["--permission-mode", "auto"])
        self.assertIsNone(registry.get(self.db, "perm")["lease_owner"])

    def _attach_capturing_stderr(self, name: str = "perm") -> tuple[list[str], str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            argv = self._attach(name)
        return argv, err.getvalue()

    def test_permission_mode_is_bound_to_the_harness(self):
        # L685 review r1 P1: a mode set while the agent runs Claude is not
        # inherited after a switch to Codex (offline reproduction: attach added
        # --dangerously-bypass-approvals-and-sandbox with no fresh codex setting)
        proc = self.mesh("set", "perm", "permission_mode=bypassPermissions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = os.path.join(self.cfg.agent_dir("perm"), "permission_mode")
        with open(state, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["harness"], "claude")
        self.assertEqual(stored["mode"], "bypassPermissions")
        # r2 P1: the registration identity is part of the binding
        self.assertEqual(stored["incarnation"],
                         "%.6f" % registry.get(self.db, "perm")["created_ts"])
        # under Claude the explicit authorization still applies
        self.assertIn("bypassPermissions", self._attach())
        registry.upsert(self.db, "perm", harness="codex")
        argv, err = self._attach_capturing_stderr()
        self.assertEqual(argv[:1], ["--no-daemon"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertIn("was set for harness 'claude'", err)
        # back under Claude, the authorization it received applies again
        registry.upsert(self.db, "perm", harness="claude")
        self.assertIn("bypassPermissions", self._attach())

    def test_permission_mode_is_bound_to_the_registration(self):
        # L685 review r2 P1: deleting the agent and recreating it under the same
        # name, with its state directory kept, must not inherit the bypass
        proc = self.mesh("set", "perm", "permission_mode=bypassPermissions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bypassPermissions", self._attach())
        cwd = os.path.join(self.tmp, "work", "perm")
        self.db.execute("delete from agent_registry where name = %s", ("perm",))
        self.register("perm", "claude", cwd=cwd)
        argv, err = self._attach_capturing_stderr()
        self.assertEqual(argv[:2], ["--permission-mode", "auto"])
        self.assertNotIn("bypassPermissions", argv)
        self.assertIn("another registration", err)

    def test_revocation_failure_is_visible_and_inert(self):
        # L685 review r2 P2: with a non-writable state directory the unlink
        # fails; the command must fail visibly and leave nothing active
        proc = self.mesh("set", "perm", "permission_mode=bypassPermissions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state_dir = self.cfg.agent_dir("perm")
        os.chmod(state_dir, 0o500)
        try:
            proc = self.mesh("set", "perm", "permission_mode=")
        finally:
            os.chmod(state_dir, 0o700)
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        self.assertIn("neutralised in place", proc.stderr)
        argv, err = self._attach_capturing_stderr()
        self.assertEqual(argv[:2], ["--permission-mode", "auto"])
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("malformed", err)

    def test_setting_failure_is_visible(self):
        # a mode that cannot be written is not silently dropped
        state_dir = self.cfg.agent_dir("perm")
        os.makedirs(state_dir, exist_ok=True)
        os.chmod(state_dir, 0o500)
        try:
            proc = self.mesh("set", "perm", "permission_mode=bypassPermissions")
        finally:
            os.chmod(state_dir, 0o700)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("cannot write", proc.stderr)
        argv, _ = self._attach_capturing_stderr()
        self.assertNotIn("bypassPermissions", argv)

    def test_permission_mode_does_not_leak_back_to_claude(self):
        # the binding is symmetric: a codex bypass authorization is not inherited
        # when the agent moves to Claude (where bypassPermissions is also a
        # descriptor choice, so it would silently be applied)
        registry.upsert(self.db, "perm", harness="codex")
        proc = self.mesh("set", "perm", "permission_mode=bypassPermissions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        registry.upsert(self.db, "perm", harness="claude")
        argv, err = self._attach_capturing_stderr()
        self.assertNotIn("bypassPermissions", argv)
        self.assertEqual(argv[:2], ["--permission-mode", "auto"])
        self.assertIn("was set for harness 'codex'", err)

    def test_attach_legacy_unbound_setting_ignored(self):
        # a plain-text file written before L685 carries no harness: fail-closed,
        # the owner must re-run `ameesh set` to authorize a mode
        state_dir = self.cfg.agent_dir("perm")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "permission_mode"), "w", encoding="utf-8") as fh:
            fh.write("bypassPermissions\n")
        argv, err = self._attach_capturing_stderr()
        self.assertEqual(argv[:2], ["--permission-mode", "auto"])
        self.assertNotIn("bypassPermissions", argv)
        self.assertIn("not bound to a harness", err)
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
