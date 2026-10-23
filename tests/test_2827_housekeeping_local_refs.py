# SPDX-License-Identifier: AGPL-3.0-only
"""A clone's tags and custom refs must not lose unpublished commits."""
import os
import tempfile
import unittest
from unittest import mock

from ameesh import housekeeping as hk, housekeeping_clones as clones
from tests.test_l73_housekeeping import _Cfg, _git, _repo, _sans_processus


class LocalRefsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="refs-2827-")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.path = _repo(self.root)
        self.cfg = _Cfg(os.path.join(self.root, "state"))
        _sans_processus(self)

    def guard_reasons(self):
        return (hk._git_copy_work(self.path, self.path),
                clones._removal_check(self.path, self.cfg, (), (),
                                      (self.root,), {}))

    def test_unpublished_commit_kept_by_any_local_ref(self):
        for kind in ("branch", "tag", "annotated-tag", "custom"):
            with self.subTest(kind=kind):
                _git("checkout", "-q", "--detach", "main", cwd=self.path)
                _git("commit", "-q", "--allow-empty", "-m", kind, cwd=self.path)
                commit = _git("rev-parse", "HEAD", cwd=self.path).strip()
                if kind == "branch":
                    ref = "refs/heads/unpublished"
                    _git("branch", "unpublished", cwd=self.path)
                elif kind == "tag":
                    ref = "refs/tags/unpublished"
                    _git("tag", "unpublished", cwd=self.path)
                elif kind == "annotated-tag":
                    ref = "refs/tags/annotated"
                    _git("tag", "-a", "annotated", "-m", "keep", cwd=self.path)
                else:
                    ref = "refs/archive/unpublished"
                    _git("update-ref", ref, commit, cwd=self.path)
                _git("checkout", "-q", "main", cwd=self.path)
                for reason in self.guard_reasons():
                    self.assertIn("local ref commit " + commit[:12], reason)
                self.assertTrue(os.path.isdir(self.path))
                _git("update-ref", "-d", ref, cwd=self.path)

    def test_refs_reachable_from_a_remote_branch_allow_removal(self):
        _git("tag", "saved", cwd=self.path)
        _git("tag", "-a", "annotated", "-m", "saved", cwd=self.path)
        _git("update-ref", "refs/archive/saved", "HEAD", cwd=self.path)
        self.assertEqual(self.guard_reasons(), ("", ""))

    def test_unreadable_refs_keep_the_clone(self):
        real_git = hk._git

        def unreadable(args, *pos, **kw):
            if args[:2] == ["log", "--all"]:
                return None
            return real_git(args, *pos, **kw)

        with mock.patch.object(hk, "_git", side_effect=unreadable):
            for reason in self.guard_reasons():
                self.assertIn("local refs unreadable", reason)

    def test_stash_is_kept_even_when_reachable_from_a_remote_branch(self):
        _git("commit", "-q", "--allow-empty", "-m", "stash witness", cwd=self.path)
        _git("push", "-q", "origin", "main", cwd=self.path)
        _git("update-ref", "refs/stash", "HEAD", cwd=self.path)
        for reason in self.guard_reasons():
            self.assertIn("has a stash", reason)
