# SPDX-License-Identifier: AGPL-3.0-only
"""Release landing uses its own identity even on an unconfigured clone."""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from . import support  # isolate the runner's environment
from .test_2448_release_cutoff import ReleaseFixtureMixin


class ReleaseIdentityTest(ReleaseFixtureMixin, unittest.TestCase):
    def prepare_landing(self):
        self.write("red.txt", "red\n")
        self.red = self.commit("red change")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.clone, "config", "user.useConfigOnly", "true")

    def isolated_env(self):
        env = dict(os.environ)
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                     "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_CONFIG_PARAMETERS"):
            env.pop(name, None)
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_COUNT="0")
        return env

    def assert_release_identity(self):
        report = self.execute(ci=False)
        self.assertEqual(report["result"], "released", report)
        landing, version = report["landing_sha"], report["release_sha"]
        self.assertNotEqual(landing, version)
        self.assertEqual(self.git(self.clone, "show", "-s", "--format=%P", landing),
                         "%s %s" % (self.red, version))
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"), landing)
        self.assertEqual(self.git(self.clone, "rev-parse", report["tag"] + "^{commit}"),
                         version)
        for commit in (landing, version):
            self.assertEqual(self.git(self.clone, "show", "-s",
                                     "--format=%an|%ae|%cn|%ce", commit),
                             "ameesh release|release@ameesh.invalid|"
                             "ameesh release|release@ameesh.invalid")
        self.assertEqual(self.git(self.clone, "for-each-ref",
                                 "--format=%(taggername)|%(taggeremail)",
                                 "refs/tags/" + report["tag"]),
                         "ameesh release|<release@ameesh.invalid>")

    def test_landing_without_any_git_identity(self):
        self.prepare_landing()
        with mock.patch.dict(os.environ, self.isolated_env(), clear=True):
            # Prove the fixture would reject a Git operation needing identity.
            ident = subprocess.run(["git", "-C", str(self.clone), "var",
                                    "GIT_COMMITTER_IDENT"], capture_output=True)
            self.assertNotEqual(ident.returncode, 0)
            self.assert_release_identity()
            local = subprocess.run(["git", "-C", str(self.clone), "config", "--local",
                                    "--get-regexp", r"^user\.(name|email)$"],
                                   capture_output=True)
            self.assertEqual(local.returncode, 1)  # no persistent workaround
            self.assertNotIn("GIT_COMMITTER_NAME", os.environ)

    def test_landing_ignores_conflicting_clone_and_process_identity(self):
        self.prepare_landing()
        self.git(self.clone, "config", "user.name", "clone identity")
        self.git(self.clone, "config", "user.email", "clone@example.invalid")
        env = self.isolated_env()
        env.update(GIT_AUTHOR_NAME="process author", GIT_AUTHOR_EMAIL="author@example.invalid",
                   GIT_COMMITTER_NAME="process committer",
                   GIT_COMMITTER_EMAIL="committer@example.invalid")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assert_release_identity()
            self.assertEqual(os.environ["GIT_COMMITTER_NAME"], "process committer")
            self.assertEqual(self.git(self.clone, "config", "user.name"), "clone identity")
            self.assertEqual(self.git(self.clone, "config", "user.email"),
                             "clone@example.invalid")
