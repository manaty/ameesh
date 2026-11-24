# SPDX-License-Identifier: AGPL-3.0-only
"""#8188: a catch-up declaration names the release from the remote's tags.

The clone of the host that runs `ameesh release catchup` may lag the release
tags (BG named v1.68.0 while v1.71.0 was active). `staging_deferred.baseline`
refreshes the tags from the project's remote before reading them, refuses
explicitly when that refresh fails, and refuses to declare against an older
release when a newer attestation already reports the host.

The fixture is a real bare origin, a real seed and real clones: no network,
no host, no database.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import release_cutoff, staging_deferred
from tests import canon_staging

HOST = canon_staging.SERVER
AGENTS = canon_staging.AGENTS
OLD_TAG = "v1.68.0"
NEW_TAG = "v1.71.0"


def attestation(sha, *, reportes=None, hotes=None, date="2026-11-24T00:00:00Z"):
    """A staging attestation: the agents always active, optional reports."""
    return {"schema": "ameesh-staging-attestation/1", "sha": sha, "date": date,
            "hotes": hotes if hotes is not None
            else {host: {"actif": sha} for host in AGENTS},
            "reportes": reportes or {}}


class GitFixture(unittest.TestCase):
    """A bare origin with v1.68.0, and clones taken at chosen moments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.git(self.root, "init", "--bare", "-q", str(self.origin))
        self.git(self.root, "init", "-q", "-b", "main", str(self.seed))
        self.git(self.seed, "config", "user.name", "t")
        self.git(self.seed, "config", "user.email", "t@example.invalid")
        self.old = self.commit("old")
        self.git(self.seed, "tag", "-a", OLD_TAG, "-m", "ameesh 1.68.0")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "-q", "origin", "main", "--tags")
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.clones = 0

    def git(self, repo, *args):
        proc = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def commit(self, name):
        (self.seed / name).write_text(name + "\n")
        self.git(self.seed, "add", name)
        self.git(self.seed, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-qm", name)
        return self.git(self.seed, "rev-parse", "HEAD")

    def release(self, name="new", tag=NEW_TAG):
        """Commit and tag a new release, then publish it on the origin."""
        sha = self.commit(name)
        self.git(self.seed, "tag", "-a", tag, "-m", "ameesh " + tag.lstrip("v"))
        self.git(self.seed, "push", "-q", "origin", "main", "--tags")
        return sha

    def push_main(self):
        self.git(self.seed, "push", "-q", "origin", "main")
        return self.git(self.seed, "rev-parse", "HEAD")

    def clone(self, *, tags=True):
        self.clones += 1
        folder = self.root / ("clone-%d" % self.clones)
        args = ["clone", "-q"] + ([] if tags else ["--no-tags"])
        args += [str(self.origin), str(folder)]
        self.git(self.root, *args)
        return folder

    def project(self, repo):
        return release_cutoff.ProjectRelease(
            name="ameesh", repo=str(repo), target="main", remote="origin",
            report="bureau", github="manaty/ameesh",
            staging_topology=canon_staging.topology())

    def baseline(self, repo, rows):
        with mock.patch.object(release_cutoff, "stored_attestations",
                               return_value=rows):
            return staging_deferred.baseline(object(), self.project(repo), HOST)


class BaselineTagsTest(GitFixture):
    def test_nominal_case_names_the_tagged_attestation(self):
        new = self.release()
        repo = self.clone()
        rows = [attestation(new, reportes={HOST: {"raison": "deferred"}}),
                attestation(self.old, reportes={HOST: {"raison": "deferred"}})]
        base = self.baseline(repo, rows)
        self.assertEqual(base["sha"], new)
        self.assertEqual(base["tag"], NEW_TAG)
        self.assertEqual(base["raison"], "deferred")

    def test_stale_local_tags_do_not_name_an_older_release(self):
        repo = self.clone()                      # knows v1.68.0 only
        new = self.release()                     # origin now has v1.71.0
        self.assertEqual(self.git(repo, "tag", "--list", NEW_TAG), "")
        rows = [attestation(new, reportes={HOST: {"raison": "deferred"}}),
                attestation(self.old, reportes={HOST: {"raison": "deferred"}})]
        base = self.baseline(repo, rows)
        self.assertEqual(base["sha"], new)
        self.assertEqual(base["tag"], NEW_TAG)

    def test_no_local_tag_still_names_the_release(self):
        new = self.release()
        repo = self.clone(tags=False)
        self.assertEqual(self.git(repo, "tag", "--list"), "")
        base = self.baseline(repo, [attestation(
            new, reportes={HOST: {"raison": "deferred"}})])
        self.assertEqual(base["sha"], new)
        self.assertEqual(base["tag"], NEW_TAG)

    def test_a_failed_tag_refresh_is_an_explicit_refusal(self):
        repo = self.clone()
        self.git(repo, "remote", "set-url", "origin",
                 str(self.root / "missing.git"))
        with self.assertRaisesRegex(release_cutoff.ReleaseError,
                                    "cannot refresh the release tags"):
            self.baseline(repo, [attestation(
                self.old, reportes={HOST: {"raison": "deferred"}})])

    def test_an_empty_history_fetches_nothing(self):
        with mock.patch.object(release_cutoff, "stored_attestations",
                               return_value=[]), \
             mock.patch.object(release_cutoff, "fetch") as fetch:
            self.assertIsNone(staging_deferred.baseline(
                object(), self.project("/nowhere"), HOST))
        fetch.assert_not_called()


class NewerReportsTest(GitFixture):
    def test_a_newer_report_refuses_the_older_baseline(self):
        repo = self.clone()
        self.commit("staged")                    # staged, not tagged yet
        newer = self.push_main()
        rows = [attestation(newer, reportes={HOST: {"raison": "deferred"}},
                            date="2026-11-24T01:00:00Z"),
                attestation(self.old, reportes={HOST: {"raison": "deferred"}})]
        with self.assertRaises(release_cutoff.ReleaseError) as caught:
            self.baseline(repo, rows)
        message = str(caught.exception)
        self.assertIn(newer[:12], message)
        self.assertIn("2026-11-24T01:00:00Z", message)
        self.assertIn(OLD_TAG, message)

    def test_a_newer_attestation_that_does_not_report_the_host_does_not_block(self):
        repo = self.clone()
        newer = self.commit("proved")            # not pushed, not tagged
        rows = [attestation(newer),               # host proved there
                attestation(self.old, reportes={HOST: {"raison": "deferred"}})]
        base = self.baseline(repo, rows)
        self.assertEqual(base["sha"], self.old)
        self.assertEqual(base["tag"], OLD_TAG)


if __name__ == "__main__":
    unittest.main()
