# SPDX-License-Identifier: AGPL-3.0-only
"""#4786: the cutoff's GitHub release carries the assets of its commit.

The release commit builds its own wheel, pin files and SHA256SUMS
(`construction-runtime.py --assets`, faked here), verified before
`gh release create`; a failed build is a deviation, never a held release.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import release_cutoff
from tests.test_2448_release_cutoff import ReleaseFixtureMixin

FAKE_BUILD = r'''import hashlib, os, sys
args = sys.argv[1:]
opt = lambda n: args[args.index(n) + 1]
mode = os.environ.get("FAKE_ASSETS", "")
if mode == "fail":
    sys.exit("construction-runtime: wheel build failed")
out = opt("--output")
up = os.path.dirname(os.path.abspath(out))
while up.startswith(os.environ["HOME"]):
    if os.stat(up).st_mode & 0o022:
        sys.exit("construction-runtime: output parent must not be group/world writable")
    up = os.path.dirname(up)
os.mkdir(out)
files = {"ameesh-1.11.8-py3-none-any.whl": "wheel of " + opt("--sha"),
         "runtime-pins.txt": "pins\n", "runtime-build-pins.txt": "build pins\n"}
sums = ""
for name in sorted(files):
    with open(os.path.join(out, name), "w") as fh:
        fh.write(files[name])
    os.chmod(os.path.join(out, name), 0o444)
    digest = hashlib.sha256(files[name].encode()).hexdigest()
    sums += "%s  %s\n" % ("0" * 64 if mode == "bad" and name == "runtime-pins.txt" else digest, name)
with open(os.path.join(out, "SHA256SUMS"), "w") as fh:
    fh.write(sums)
'''


class ReleaseAssets(ReleaseFixtureMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        # a HOME is private; its subdirectories follow the umask
        os.chmod(self.home, 0o700)
        self.write("deploy/mise-a-jour/construction-runtime.py", FAKE_BUILD)
        self.write("deploy/mise-a-jour/runtime-release.py", "# helper\n")
        self.write("deploy/mise-a-jour/runtime-pins.txt", "pins\n")
        self.write("deploy/mise-a-jour/runtime-build-pins.txt", "build pins\n")
        self.head = self.commit("chore: actifs de release (#4786)")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.clone, "pull", "-q", "--ff-only")
        self.seen = {}
        original = release_cutoff.plan_github.Gh.run

        def run(gh, argv, stdin=None):
            if argv[:2] == ["release", "create"]:
                # read at creation: the files are removed afterwards
                for path in argv[argv.index("--notes-file") + 2:]:
                    with open(path, encoding="utf-8") as stream:
                        self.seen[os.path.basename(path)] = stream.read()
            return original(gh, argv, stdin)
        patcher = mock.patch.object(release_cutoff.plan_github.Gh, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_call(self):
        calls = [c for c in release_cutoff.plan_github.Gh.calls if c[:2] == ["release", "create"]]
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def test_the_release_of_the_cutoff_attaches_the_verified_assets(self):
        # umask 002 : ~/.cache 0775 must not matter (B1 of review #27073)
        (self.home / ".cache").mkdir(mode=0o775)
        os.chmod(self.home / ".cache", 0o775)
        report = self.execute()
        self.assertEqual(report["result"], "released", report)
        self.create_call()
        self.assertEqual(sorted(self.seen), ["SHA256SUMS", "ameesh-1.11.8-py3-none-any.whl",
                                             "runtime-build-pins.txt", "runtime-pins.txt"])
        self.assertEqual(self.seen["ameesh-1.11.8-py3-none-any.whl"],
                         "wheel of " + report["release_sha"])
        self.assertFalse([d for d in report["deviations"] if "actifs" in d], report)
        self.assertEqual([n for n in os.listdir(self.home) if "actifs" in n], [])

    def test_a_failed_or_unverified_build_releases_without_assets(self):
        for mode, why in (("fail", "wheel build failed"), ("bad", "SHA256SUMS")):
            with self.subTest(mode=mode), \
                    mock.patch.dict(os.environ, {"FAKE_ASSETS": mode}):
                release_cutoff.plan_github.Gh.calls = []
                self.seen.clear()
                deviations = []
                self.assertTrue(release_cutoff.ensure_github_release(
                    self.project, "v1.11.8", "1.11.8", "notes", self.head, deviations))
                self.assertEqual(self.seen, {})
                self.assertEqual(len(deviations), 1, deviations)
                self.assertIn("sans actifs", deviations[0])
                self.assertIn(why, deviations[0])
                self.assertEqual([n for n in os.listdir(self.home) if "actifs" in n], [])

    def test_a_commit_without_the_asset_build_publishes_none(self):
        before = self.git(self.seed, "rev-parse", "HEAD~1")
        deviations = []
        self.assertTrue(release_cutoff.ensure_github_release(
            self.project, "v1.11.7", "1.11.7", "notes", before, deviations))
        self.assertEqual(self.create_call()[-2:], ["--notes-file", self.create_call()[-1]])
        self.assertEqual((self.seen, deviations), ({}, []))


if __name__ == "__main__":
    unittest.main()
