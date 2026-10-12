# SPDX-License-Identifier: AGPL-3.0-only
"""L54: persona memory in a neutral Git repository."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest

from ameesh import persona_memory as pm
from ameesh.runner import AgentWorker


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class _Repository(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-l54-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bare = os.path.join(self.tmp, "memory.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        seed = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "clone", "-q", self.bare, seed], check=True, capture_output=True)
        git(seed, "config", "user.email", "t@t")
        git(seed, "config", "user.name", "t")
        with open(os.path.join(seed, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [Preferences](memories/preferences.md): short English replies\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.state = os.path.join(self.tmp, "state")

    def open_copy(self):
        return pm.open_copy(self.bare, self.state, "verif-a")


class WorkingCopyTest(_Repository):
    def test_open_and_designate(self):
        path = self.open_copy()
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o700")
        text = pm.preface(path, "verif-a", "s1")
        self.assertIn("short English replies", text)
        self.assertIn("journal/s1.md", text)
        self.assertIn("Never store secrets", text)
        self.assertEqual(self.open_copy(), path)

    def test_journal_pushed(self):
        path = self.open_copy()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("- Start the staging bench with scripts/fed-bench.sh\n")
        r = pm.sync(path, "turn end")
        self.assertEqual(r["status"], "pushed")
        self.assertIn("journal/s1.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(pm.sync(path, "none")["status"], "unchanged")

    def test_secret_rejected(self):
        path = self.open_copy()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("the key is sk-abcdefghijklmnopqrstuvwxyz0123\n")
        r = pm.sync(path, "turn end")
        self.assertEqual(r["status"], "secret")
        self.assertEqual(r["files"], ["journal/s1.md"])
        self.assertNotIn("journal/s1.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))

    def test_two_parallel_sessions(self):
        a = self.open_copy()
        b = pm.open_copy(self.bare, os.path.join(self.tmp, "state-b"), "verif-a")
        for path, session in ((a, "s1"), (b, "s2")):
            os.makedirs(os.path.join(path, "journal"), exist_ok=True)
            with open(pm.journal_path(path, session), "w", encoding="utf-8") as fh:
                fh.write("note from %s\n" % session)
        self.assertEqual(pm.sync(a, "a")["status"], "pushed")
        self.assertEqual(pm.sync(b, "b")["status"], "pushed")
        names = git(self.bare, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("journal/s1.md", names)
        self.assertIn("journal/s2.md", names)

    def test_close_removes_copy(self):
        path = self.open_copy()
        self.assertEqual(pm.close(path)["status"], "removed")
        self.assertFalse(os.path.exists(path))

    def test_unreachable_repository(self):
        with self.assertRaises(pm.MemoryError):
            pm.open_copy(os.path.join(self.tmp, "absent.git"), self.state, "x")
        self.assertFalse(os.path.exists(pm.copy_dir(self.state, "x")))

    def test_consolidation_prompt(self):
        text = pm.consolidation_prompt("/state/memory/a")
        self.assertIn("MEMORY TURN", text)
        self.assertIn("MEMORY.md", text)

    def test_staged_secret_is_checked_even_if_working_file_changed(self):
        path = self.open_copy()
        filename = os.path.join(path, "a file with spaces.md")
        with open(filename, "w", encoding="utf-8") as stream:
            stream.write("sk-abcdefghijklmnopqrstuvwxyz0123")
        git(path, "add", "-A")
        with open(filename, "w", encoding="utf-8") as stream:
            stream.write("ordinary note")
        self.assertEqual(pm.secrets_in(path), ["a file with spaces.md"])

    def test_failed_push_is_retried_without_new_changes(self):
        path = self.open_copy()
        hook = os.path.join(self.bare, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        with open(os.path.join(path, "note.md"), "w", encoding="utf-8") as stream:
            stream.write("durable note")
        self.assertEqual(pm.sync(path, "turn end")["status"], "push_rejected")
        self.assertEqual(pm.close(path)["status"], "kept")
        self.assertTrue(os.path.isdir(path))
        os.unlink(hook)
        self.assertEqual(pm.sync(path, "retry")["status"], "pushed")
        self.assertIn("note.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(pm.close(path)["status"], "removed")

    def test_repository_change_does_not_reuse_old_persona_memory(self):
        path = self.open_copy()
        other = os.path.join(self.tmp, "other.git")
        subprocess.run(["git", "init", "-q", "--bare", other], check=True)
        with self.assertRaisesRegex(pm.MemoryError, "different memory repository"):
            pm.open_copy(other, self.state, "verif-a")
        self.assertTrue(os.path.isdir(path))



class ExecutorTest(_Repository):
    def test_sync_at_turn_end(self):
        path = self.open_copy()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("- a durable fact\n")
        logs = []
        worker = types.SimpleNamespace(
            name="verif-a", agent={"memory_repository": self.bare},
            runner=types.SimpleNamespace(dry_run=False),
            cfg=types.SimpleNamespace(state_dir=self.state, persona_memory={}))
        import ameesh.runner as runner_mod
        previous = runner_mod.log_async
        runner_mod.log_async = logs.append
        self.addCleanup(setattr, runner_mod, "log_async", previous)
        AgentWorker._sync_memory(worker)
        for t in threading.enumerate():
            if t.name == "verif-a-memory":
                t.join(30)
        self.assertTrue(any("pushed" in j for j in logs), logs)

    def test_no_repository_no_work(self):
        worker = types.SimpleNamespace(name="x", agent={}, runner=types.SimpleNamespace(dry_run=False),
                                       cfg=types.SimpleNamespace(state_dir=self.state))
        AgentWorker._sync_memory(worker)


if __name__ == "__main__":
    unittest.main()
