# SPDX-License-Identifier: AGPL-3.0-only
"""#7323: generated-only deltas carry only after frozen generation checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess

from ameesh import verdict_carry
from .test_2442_verdict_carry import _git


class GeneratedCarryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="7323-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        _git(self.root, "init", "-q", "-b", "main")
        self.write("source.txt", "base\n")
        self.write("generated.txt", "base\n")
        self.write("check.py", "from pathlib import Path\nimport sys\n"
                   "sys.exit(0 if Path('generated.txt').read_bytes() == "
                   "Path('source.txt').read_bytes() else 1)\n")
        self.rule = {"check": [sys.executable, "check.py"]}
        self.declare()
        self.tip = self.commit()
        self.write("source.txt", "reviewed\n")
        self.reviewed = self.commit()

    def write(self, path, text):
        full = Path(self.root) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")

    def declare(self):
        self.write(verdict_carry.GENERATED_FILES, json.dumps({
            "version": 1, "files": {"generated.txt": self.rule}}))

    def commit(self):
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "fixture")
        return _git(self.root, "rev-parse", "HEAD")

    def check(self, frozen):
        return verdict_carry._carriable(self.root, self.reviewed, frozen, self.tip)

    def test_generated_only_green_check_carries_as_adaptation(self):
        self.write("generated.txt", "reviewed\n")
        ok, why, adaptation = self.check(self.commit())
        self.assertTrue(ok, why)
        self.assertTrue(adaptation)
        self.assertIn("frozen generation checks passed", why)

    def test_red_check_refuses(self):
        self.write("generated.txt", "invented\n")
        self.assertFalse(self.check(self.commit())[0])

    def test_mixed_code_refuses(self):
        self.write("generated.txt", "reviewed\n")
        self.write("source.txt", "unreviewed\n")
        self.assertFalse(self.check(self.commit())[0])

    def test_unlisted_output_refuses(self):
        self.write("generated.txt", "reviewed\n")
        self.write("another.txt", "generated\n")
        self.assertFalse(self.check(self.commit())[0])

    def test_declaration_cannot_grant_itself_an_exemption(self):
        self.rule["check"] = [sys.executable, "-c", "pass"]
        self.declare()
        self.write("generated.txt", "invented\n")
        self.assertFalse(self.check(self.commit())[0])

    def test_dirty_checkout_is_not_the_proof(self):
        self.write("generated.txt", "invented\n")
        frozen = self.commit()
        self.write("generated.txt", "reviewed\n")
        self.assertFalse(self.check(frozen)[0])

    def test_hidden_code_change_then_revert_refuses(self):
        self.write("source.txt", "hidden\n")
        self.commit()
        self.write("source.txt", "reviewed\n")
        self.write("generated.txt", "reviewed\n")
        self.assertFalse(self.check(self.commit())[0])

    def test_missing_command_and_timeout_are_inconclusive(self):
        self.write("generated.txt", "reviewed\n")
        frozen = self.commit()
        original_run = subprocess.run
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("checker", 60)):
            def run(argv, *args, **kwargs):
                if argv[0] == sys.executable:
                    raise exc
                return original_run(argv, *args, **kwargs)
            with self.subTest(exc=type(exc).__name__), mock.patch.object(
                    verdict_carry.subprocess, "run", side_effect=run):
                self.assertFalse(verdict_carry.generated_adapted(
                    self.root, self.reviewed, frozen)[0])

    def test_symlink_output_refuses(self):
        (Path(self.root) / "generated.txt").unlink()
        os.symlink("source.txt", Path(self.root) / "generated.txt")
        self.assertFalse(self.check(self.commit())[0])

    def test_checker_cannot_fix_a_stale_file_and_claim_success(self):
        self.write("check.py", "from pathlib import Path\n"
                   "Path('generated.txt').write_bytes(Path('source.txt').read_bytes())\n")
        self.reviewed = self.commit()
        self.write("generated.txt", "invented\n")
        self.assertFalse(self.check(self.commit())[0])

    def test_old_generated_refusal_is_retried_on_the_same_freeze(self):
        old = (verdict_carry.NOT_CARRIED + " " + self.reviewed[:12] + " → "
               + self.tip[:12] + ": generated.txt differs outside a test or a conflict")
        self.assertFalse(verdict_carry._decided([old], self.reviewed, self.tip))
        current = old + "; generated check #7323: failed"
        self.assertTrue(verdict_carry._decided([current], self.reviewed, self.tip))

    def test_manifest_absent_from_trusted_target_refuses(self):
        _git(self.root, "rm", verdict_carry.GENERATED_FILES)
        self.tip = self.commit()
        self.write("generated.txt", "reviewed\n")
        self.assertFalse(self.check(self.commit())[0])

    def runtime_fixture(self):
        self.rule.update(runtime="tools", locks=["lock.txt"])
        self.declare()
        self.write(".gitignore", "tools/\n")
        self.write("lock.txt", "locked\n")
        self.write("check.py", "from pathlib import Path\nimport sys\n"
                   "sys.exit(0 if Path('tools/marker').read_text() == 'installed' "
                   "and Path('generated.txt').read_bytes() == "
                   "Path('source.txt').read_bytes() else 1)\n")
        self.reviewed = self.commit()
        self.tip = self.reviewed
        self.write("generated.txt", "reviewed\n")
        return self.commit()

    def test_runtime_is_reused_when_locks_match(self):
        frozen = self.runtime_fixture()
        self.write("tools/marker", "installed")
        ok, why, adaptation = self.check(frozen)
        self.assertTrue(ok, why)
        self.assertTrue(adaptation)

    def test_missing_runtime_is_inconclusive(self):
        frozen = self.runtime_fixture()
        ok, why, _ = self.check(frozen)
        self.assertFalse(ok)
        self.assertIn("runtime unavailable", why)

    def test_runtime_with_different_lock_is_refused(self):
        frozen = self.runtime_fixture()
        self.write("tools/marker", "installed")
        self.write("lock.txt", "different\n")
        ok, why, _ = self.check(frozen)
        self.assertFalse(ok)
        self.assertIn("runtime lock differs", why)
