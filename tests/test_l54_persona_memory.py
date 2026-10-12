# SPDX-License-Identifier: AGPL-3.0-only
"""L54: persona memory in a neutral Git repository."""
from __future__ import annotations

import os
import sys
import dataclasses
import io
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import cli, config as config_mod, db as db_mod, persona_memory as pm
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

    def test_cloned_index_symlink_cannot_inject_credentials(self):
        seed = os.path.join(self.tmp, "seed")
        secret = os.path.join(self.tmp, "credential")
        with open(secret, "w") as fh:
            fh.write("PRIVATE_CREDENTIAL_VALUE")
        os.unlink(os.path.join(seed, "MEMORY.md"))
        os.symlink(secret, os.path.join(seed, "MEMORY.md"))
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "malicious index")
        git(seed, "push", "-q")
        with self.assertRaisesRegex(pm.MemoryError, "symlink"):
            self.open_copy()

    def test_journal_and_index_links_refused_before_read_or_write(self):
        path = self.open_copy()
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(path, "journal"))
        with self.assertRaisesRegex(pm.MemoryError, "symlink"):
            pm.journal_path(path, "s1")
        with self.assertRaisesRegex(pm.MemoryError, "symlink"):
            pm.sync(path, "unsafe")
        os.unlink(os.path.join(path, "journal"))
        os.unlink(os.path.join(path, "MEMORY.md"))
        os.symlink(os.path.join(self.tmp, "seed", "MEMORY.md"), os.path.join(path, "MEMORY.md"))
        with self.assertRaisesRegex(pm.MemoryError, "symlink"):
            pm.index_text(path)

    def test_whole_copy_and_archive_deletions_refused(self):
        path = self.open_copy()
        os.unlink(os.path.join(path, "MEMORY.md"))
        self.assertEqual(pm.sync(path, "wipe")["status"], "destructive")
        self.assertIn("MEMORY.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
        git(path, "restore", "MEMORY.md")
        os.makedirs(os.path.join(path, "journal"))
        journal = pm.journal_path(path, "s1")
        with open(journal, "w") as fh:
            fh.write("archived fact")
        self.assertEqual(pm.sync(path, "archive")["status"], "pushed")
        os.unlink(journal)
        self.assertEqual(pm.sync(path, "erase archive")["status"], "destructive")
        self.assertIn("journal/s1.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))

    def test_session_committed_secrets_scanned_even_when_later_removed(self):
        path = self.open_copy()
        filename = os.path.join(path, "secret.md")
        with open(filename, "w") as fh:
            fh.write("sk-abcdefghijklmnopqrstuvwxyz0123")
        git(path, "add", "-A")
        git(path, "commit", "-qm", "session secret")
        os.unlink(filename)
        git(path, "add", "-A")
        git(path, "commit", "-qm", "remove secret")
        before = git(self.bare, "rev-parse", "main")
        self.assertEqual(pm.sync(path, "clean index")["status"], "secret")
        self.assertEqual(git(self.bare, "rev-parse", "main"), before)
        self.assertEqual(pm.close(path)["status"], "kept")

    def test_session_committed_wipe_refused(self):
        path = self.open_copy()
        os.unlink(os.path.join(path, "MEMORY.md"))
        git(path, "add", "-A")
        git(path, "commit", "-qm", "wipe")
        before = git(self.bare, "rev-parse", "main")
        self.assertEqual(pm.sync(path, "clean index")["status"], "destructive")
        self.assertEqual(git(self.bare, "rev-parse", "main"), before)

    def test_sync_checks_configured_repository_before_staging(self):
        path = self.open_copy()
        with open(os.path.join(path, "new.md"), "w") as fh:
            fh.write("durable fact")
        with self.assertRaisesRegex(pm.MemoryError, "different memory repository"):
            pm.sync(path, "wrong repository", repository=os.path.join(self.tmp, "other.git"))
        self.assertNotIn("new.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(git(path, "diff", "--cached", "--name-only"), "")

    def test_activation_discards_inactive_peer_edits_and_commits(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                path = self.open_copy()
                with open(os.path.join(path, "MEMORY.md"), "w") as fh:
                    fh.write("forged inactive persona memory")
                with open(os.path.join(path, "forged.md"), "w") as fh:
                    fh.write("untracked forged note")
                if committed:
                    git(path, "add", "-A")
                    git(path, "commit", "-qm", "peer forgery")
                with mock.patch.object(pm.logging, "getLogger") as logger:
                    self.assertEqual(self.open_copy(), path)
                self.assertNotIn("forged", pm.index_text(path))
                self.assertFalse(os.path.exists(os.path.join(path, "forged.md")))
                logger.return_value.warning.assert_called_once()
                self.assertIn("discarded local state", logger.return_value.warning.call_args.args[0])
                self.assertEqual(pm.sync(path, "resume")["status"], "unchanged")
                self.assertEqual(git(path, "rev-parse", "HEAD"), git(self.bare, "rev-parse", "main"))

    def test_inactive_fsmonitor_never_runs_or_poison_staging(self):
        path = self.open_copy()
        marker = os.path.join(self.tmp, "fsmonitor-ran")
        hook = os.path.join(self.tmp, "poison-fsmonitor")
        with open(hook, "w") as stream:
            stream.write("#!" + sys.executable + "\nimport glob, os, tempfile\n")
            stream.write("open(%r, 'w').write('executed')\n" % marker)
            stream.write("for p in glob.glob(os.path.join(tempfile.gettempdir(), "
                         "'ameesh-memory-activation-*', 'copy', 'MEMORY.md')):\n"
                         "    open(p, 'w').write('forged by inactive fsmonitor')\n")
            stream.write("print('token')\n")
        os.chmod(hook, 0o755)
        git(path, "config", "core.fsmonitor", hook)
        with mock.patch.object(pm, "_git", wraps=pm._git) as calls:
            self.open_copy()
        self.assertFalse(any(call.args[0] == path for call in calls.call_args_list))
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn("forged", pm.index_text(path))
        self.assertEqual(pm.sync(path, "resume", repository=self.bare)["status"], "unchanged")
        self.assertNotIn("forged", git(self.bare, "show", "main:MEMORY.md"))

    def test_active_git_commands_disable_local_fsmonitor_and_commit_hooks(self):
        path = self.open_copy()
        marker = os.path.join(self.tmp, "hook-ran")
        hooks = os.path.join(self.tmp, "hooks")
        os.makedirs(hooks)
        hook = os.path.join(hooks, "pre-commit")
        with open(hook, "w") as stream:
            stream.write("#!" + sys.executable + "\nopen(%r, 'w').write('executed')\n" % marker)
        os.chmod(hook, 0o755)
        git(path, "config", "core.fsmonitor", hook)
        git(path, "config", "core.hooksPath", hooks)
        with open(os.path.join(path, "note.md"), "w") as stream:
            stream.write("durable fact")
        self.assertEqual(pm.sync(path, "turn", repository=self.bare)["status"], "pushed")
        self.assertFalse(os.path.exists(marker))

    def test_push_instead_of_local_and_global_cannot_redirect_transport(self):
        for mode in ("local-push", "local-fetch", "global"):
            with self.subTest(mode=mode):
                other = os.path.join(self.tmp, "other-" + mode + ".git")
                subprocess.run(["git", "clone", "-q", "--bare", self.bare, other], check=True)
                before = git(other, "rev-parse", "main")
                path = pm.open_copy(self.bare, self.state + "-" + mode, "verif-a")
                env = {}
                rule = "url." + other + (".insteadOf" if mode == "local-fetch" else ".pushInsteadOf")
                if mode == "global":
                    global_config = os.path.join(self.tmp, "global-git-config")
                    subprocess.run(["git", "config", "--file", global_config, rule, self.bare], check=True)
                    env["GIT_CONFIG_GLOBAL"] = global_config
                else:
                    git(path, "config", rule, self.bare)
                with open(os.path.join(path, "note-" + mode + ".md"), "w") as stream:
                    stream.write("durable fact")
                with mock.patch.dict(os.environ, env), mock.patch.object(pm, "_git", wraps=pm._git) as calls:
                    self.assertEqual(pm.sync(path, "turn", repository=self.bare)["status"], "pushed")
                self.assertEqual(git(other, "rev-parse", "main"), before)
                self.assertIn("note-" + mode + ".md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
                self.assertFalse(any(call.args[0] == path and call.args[1] in ("push", "fetch")
                                     for call in calls.call_args_list))

    def test_push_ignores_pushurl_and_pushremote(self):
        path = self.open_copy()
        other = os.path.join(self.tmp, "other.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.bare, other], check=True)
        before = git(other, "rev-parse", "main")
        git(path, "remote", "add", "elsewhere", other)
        git(path, "config", "remote.origin.pushurl", other)
        git(path, "config", "branch.main.pushRemote", "elsewhere")
        git(path, "config", "remote.pushDefault", "elsewhere")
        with open(os.path.join(path, "note.md"), "w") as fh:
            fh.write("durable persona note")
        self.assertEqual(pm.sync(path, "turn", repository=self.bare)["status"], "pushed")
        self.assertEqual(git(other, "rev-parse", "main"), before)
        self.assertIn("note.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(pm.close(path, repository=self.bare)["status"], "removed")

    def test_repository_change_does_not_reuse_old_persona_memory(self):
        path = self.open_copy()
        other = os.path.join(self.tmp, "other.git")
        subprocess.run(["git", "init", "-q", "--bare", other], check=True)
        with self.assertRaisesRegex(pm.MemoryError, "different memory repository"):
            pm.open_copy(other, self.state, "verif-a")
        self.assertTrue(os.path.isdir(path))



class IsolationPolicyTest(_Repository):
    def test_only_approved_host_setting_enables_exception_and_doctor_displays_it(self):
        seed = os.path.join(self.tmp, "seed")
        cfg = config_mod.Config(canon=seed, canon_ref="main", host="pc")
        self.assertEqual(pm.isolation_policy(cfg), "required")
        os.makedirs(os.path.join(seed, "hosts"))
        card = os.path.join(seed, "hosts", "pc.md")
        with open(card, "w") as fh:
            fh.write("---\ntype: Host\ntitle: pc\nmemory_isolation: shared-uid-accepted\n---\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "owner exception")
        self.assertEqual(pm.isolation_policy(cfg), "required")
        git(seed, "tag", "origin/main")
        self.assertEqual(pm.isolation_policy(cfg), "required")
        git(seed, "tag", "-d", "origin/main")
        git(seed, "push", "-q")
        git(seed, "fetch", "-q")
        self.assertEqual(pm.isolation_policy(cfg), "shared-uid-accepted")
        unapproved = os.path.join(self.tmp, "unapproved-canon")
        shutil.copytree(seed, unapproved, ignore=shutil.ignore_patterns(".git"))
        self.assertEqual(pm.isolation_policy(dataclasses.replace(
            cfg, canon=unapproved, canon_untrusted=True)), "required")
        with mock.patch.object(cli.db_mod, "connect", side_effect=db_mod.Unavailable("test offline")), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(cli.cmd_doctor(cfg, notify_test=False), 1)
        self.assertIn("shared-uid-accepted", out.getvalue())
        self.assertIn("no UID isolation", out.getvalue())


class ExecutorTest(_Repository):
    def test_sync_at_turn_end(self):
        path = self.open_copy()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("- a durable fact\n")
        logs = []
        worker = types.SimpleNamespace(
            _memory_activated=True, name="verif-a", agent={"memory_repository": self.bare},
            runner=types.SimpleNamespace(dry_run=False), db=mock.Mock(),
            cfg=types.SimpleNamespace(state_dir=self.state, persona_memory={}))
        import ameesh.runner as runner_mod
        previous = runner_mod.log_async
        runner_mod.log_async = logs.append
        self.addCleanup(setattr, runner_mod, "log_async", previous)
        with mock.patch.object(pm, "require_isolation"):
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
