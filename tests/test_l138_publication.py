# SPDX-License-Identifier: AGPL-3.0-only
"""L138 — `poste.sh publier` / `rattraper` (#309 review r1).

A local bare repository stands for GitHub's git side, a fake `gh` on PATH
for its API (state in a JSON file). No network, no real GitHub.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "mise-a-jour", "poste.sh")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_GH_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
    json.dump(state, open(path, "w"))
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if args[:2] == ["release", "view"]:
    save(); sys.exit(0 if args[2] in state["releases"] else 1)
if args[:2] == ["release", "create"]:
    latest = [a for a in args if a.startswith("--latest=")][0].split("=", 1)[1]
    files = [a for a in args[3:] if os.path.isfile(a)]
    state["releases"][args[2]] = {"notes": opt("--notes"), "latest": latest,
                                  "assets": {os.path.basename(f): open(f).read()
                                             for f in files}}
    save(); sys.exit(0)
if args[:2] == ["pr", "view"]:
    save()
    pr = state["prs"].get(args[2])
    if pr is None:
        sys.exit(1)
    if opt("--json") == "body":
        if pr.get("body_fails"):
            sys.exit(1)
        print(pr["body"]); sys.exit(0)
    print("%s %s" % (pr["state"], pr["merge"])); sys.exit(0)
if args[:1] == ["api"]:
    save()
    sha = args[1].split("/commits/")[1].split("/")[0]
    for n in state["pulls"].get(sha, []):
        print(n)
    sys.exit(0)
save(); sys.exit(2)
'''


def _git(cwd, *args) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l138-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "distant.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "remote", "add", "origin", self.remote)
        self.v100 = self.version("1.0.0", "ameesh 1.0.0 (#5)")
        self.v110 = self.version("1.1.0", "ameesh 1.1.0 (#6)")
        self.push()
        os.makedirs(os.path.join(self.repo, "deploy", "mise-a-jour"))
        shutil.copy(SCRIPT, os.path.join(self.repo, "deploy", "mise-a-jour", "poste.sh"))
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "gh"), "w", encoding="utf-8") as fh:
            fh.write(FAKE_GH)
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.state = os.path.join(self.tmp, "gh.json")
        self.gh({"releases": {}, "prs": {}, "pulls": {}})
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"],
                        FAKE_GH_STATE=self.state, HOME=self.tmp, GIT_CONFIG_GLOBAL="/dev/null",
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def version(self, version, subject) -> str:
        self.write("pyproject.toml", '[project]\nversion = "%s"\n' % version)
        _git(self.repo, "add", "pyproject.toml")
        _git(self.repo, "commit", "-q", "-m", subject)
        return _git(self.repo, "rev-parse", "HEAD")

    def push(self):
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "fetch", "-q", "origin")

    def gh(self, state=None) -> dict:
        if state is not None:
            with open(self.state, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def pr(self, number, merge, body="Notes of the PR.", **extra):
        state = self.gh()
        state["prs"][str(number)] = dict(state="MERGED", merge=merge, body=body, **extra)
        self.gh(state)

    def run_script(self, *args, blank=False):
        env = dict(self.env, A_BLANC="1") if blank else self.env
        return subprocess.run(["bash", os.path.join(self.repo, "deploy", "mise-a-jour",
                                                    "poste.sh"), *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def remote_tags(self) -> dict:
        out = _git(self.repo, "ls-remote", "--tags", "origin")
        return {ref.split("/")[-1]: sha for sha, ref in
                (line.split() for line in out.splitlines()) if not ref.endswith("^{}")}

    def test_r1_1_dry_run_writes_nothing_not_even_a_fetch(self):
        self.pr(5, self.v100)
        self.pr(6, self.v110)
        # a tag pushed by someone else: the dry run sees it without fetching it
        other = os.path.join(self.tmp, "autre")
        subprocess.run(["git", "clone", "-q", self.remote, other], check=True,
                       env=self.env)
        _git(other, "tag", "-a", "v1.0.0", "-m", "x", self.v100)
        _git(other, "push", "-q", "origin", "v1.0.0")
        refs = _git(self.repo, "for-each-ref")
        fetch_head = os.path.join(self.repo, ".git", "FETCH_HEAD")
        stamp = os.stat(fetch_head).st_mtime_ns
        done = self.run_script("rattraper", blank=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(_git(self.repo, "for-each-ref"), refs)
        self.assertEqual(os.stat(fetch_head).st_mtime_ns, stamp)
        self.assertIn("étiquette : déjà posée", done.stdout)
        self.assertEqual(done.stdout.count("à blanc : git"), 2)     # v1.1.0: tag + push
        self.assertEqual(self.gh()["releases"], {})

    def test_r1_2_tag_made_here_but_not_pushed_is_pushed_on_rerun(self):
        self.pr(5, self.v100)
        _git(self.repo, "tag", "-a", "v1.0.0", "-m", "ameesh 1.0.0", self.v100)
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("posée ici, absente du distant", done.stdout)
        self.assertEqual(self.remote_tags()["v1.0.0"],
                         _git(self.repo, "rev-parse", "refs/tags/v1.0.0"))
        self.assertIn("v1.0.0", self.gh()["releases"])
        # a local tag on another commit, absent from the remote: refused
        _git(self.repo, "tag", "-a", "v1.1.0", "-m", "x", self.v100)
        done = self.run_script("publier", self.v110)
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("v1.1.0", self.remote_tags())

    def test_r1_3_existing_tag_on_a_commit_of_another_version_is_refused(self):
        self.pr(5, self.v100)
        self.pr(6, self.v110)
        _git(self.repo, "tag", "-a", "v1.0.0", "-m", "x", self.v110)
        _git(self.repo, "push", "-q", "origin", "v1.0.0")
        done = self.run_script("rattraper")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("porte la version 1.1.0, pas 1.0.0", done.stderr)
        self.assertEqual(self.gh()["releases"], {})

    def test_r1_4_notes_read_from_main_not_the_working_tree(self):
        self.write("docs/versions/1.0.0.md", "Reviewed notes.\n")
        _git(self.repo, "add", "docs")
        _git(self.repo, "commit", "-q", "-m", "notes")
        self.push()
        self.write("docs/versions/1.0.0.md", "Unreviewed edit.\n")
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.gh()["releases"]["v1.0.0"]["notes"].strip(), "Reviewed notes.")

    def test_r1_5_pr_must_have_merged_the_commit(self):
        # (#5) in the subject, but PR 5 merged another commit; the API names PR 7
        self.pr(5, self.v110, body="Wrong PR.")
        self.pr(7, self.v100, body="Right PR.")
        state = self.gh()
        state["pulls"][self.v100] = [7]
        self.gh(state)
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        notes = self.gh()["releases"]["v1.0.0"]["notes"]
        self.assertIn("Right PR.", notes)
        self.assertIn("PR : #7", notes)
        # no PR that merged it: no release
        done = self.run_script("publier", self.v110)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("notes introuvables pour 1.1.0", done.stderr)
        self.assertNotIn("v1.1.0", self.gh()["releases"])

    def test_r1_6_unreadable_pr_body_fails(self):
        self.pr(5, self.v100, body_fails=True)
        done = self.run_script("publier", self.v100)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("illisible", done.stderr)
        self.assertEqual(self.gh()["releases"], {})

    def test_p3_body_with_only_the_tool_footer_fails_before_any_tag(self):
        # #309 r2 P3, #348 r1: notes must remain once the tool's footer is
        # removed, and that is checked before the tag is made or pushed
        tool = "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)"
        for body in ("", "  \n", tool, "\n\n%s\n" % tool, "---\n%s" % tool,
                     "%s\n\nCo-Authored-By: Claude <noreply@anthropic.com>" % tool,
                     "\U0001F916\uFE0F Generated with [Claude Code](x)",
                     "   %s   " % tool):
            self.pr(5, self.v100, body=body)
            done = self.run_script("publier", self.v100)
            self.assertNotEqual(done.returncode, 0, repr(body))
            self.assertIn("sans notes", done.stderr)
            self.assertEqual(self.gh()["releases"], {})
            self.assertNotIn("v1.0.0", self.remote_tags())
            self.assertEqual(_git(self.repo, "tag", "-l", "v1.0.0"), "")
        self.pr(5, self.v100, body="Real notes.\n\n---\n\n%s\n" % tool)
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.gh()["releases"]["v1.0.0"]["notes"], "Real notes.\n\nPR : #5")

    def test_348_r2_rejected_notes_leave_local_tags_untouched(self):
        # #348 r2 P3: notes reduced to the tool footer are refused before any
        # tag is fetched; the release's own tag comes only once they pass
        tool = "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)"
        other = os.path.join(self.tmp, "autre")
        subprocess.run(["git", "clone", "-q", self.remote, other], check=True,
                       env=self.env)
        _git(other, "tag", "-a", "v1.0.0", "-m", "x", self.v100)
        _git(other, "tag", "-a", "v0.9.0", "-m", "x", self.v100)
        _git(other, "push", "-q", "origin", "v1.0.0", "v0.9.0")
        tags = _git(self.repo, "for-each-ref", "refs/tags")
        self.pr(5, self.v100, body=tool)
        done = self.run_script("publier", self.v100)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("sans notes", done.stderr)
        self.assertEqual(_git(self.repo, "for-each-ref", "refs/tags"), tags)
        self.pr(5, self.v100, body="Real notes.")
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("étiquette : déjà posée", done.stdout)
        self.assertEqual(_git(self.repo, "tag", "-l"), "v1.0.0")    # its own tag only

    def test_p3_a_note_about_generation_is_kept(self):
        note = "\U0001F916 Generated with now displays the selected tool."
        self.pr(5, self.v100, body=note)
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.gh()["releases"]["v1.0.0"]["notes"], note + "\n\nPR : #5")

    def test_latest_only_on_the_highest_version(self):
        self.pr(5, self.v100)
        self.pr(6, self.v110)
        done = self.run_script("rattraper")
        self.assertEqual(done.returncode, 0, done.stderr)
        releases = self.gh()["releases"]
        self.assertEqual({t: r["latest"] for t, r in releases.items()},
                         {"v1.0.0": "false", "v1.1.0": "true"})
        self.assertEqual(set(self.remote_tags()), {"v1.0.0", "v1.1.0"})


    # #4786 : la provenance du runtime publiée en actifs de la release
    FAKE_BUILD = r'''#!/usr/bin/env python3
import hashlib, os, subprocess, sys
args = sys.argv[1:]
opt = lambda n: args[args.index(n) + 1]
if os.environ.get("FAKE_BUILD_FAILS"):
    sys.exit(2)
assert "--assets" in args
out = opt("--output")
# construction-runtime.py refuses a group/world-writable ancestor (#4786 B1)
up = os.path.dirname(os.path.abspath(out))
while up.startswith(os.environ["HOME"]):
    if os.stat(up).st_mode & 0o022:
        sys.exit("output parent must not be group/world writable")
    up = os.path.dirname(up)
os.mkdir(out)
files = {"ameesh-1.2.0-py3-none-any.whl": "wheel of " + opt("--sha")}
for name in ("runtime-pins.txt", "runtime-build-pins.txt"):
    files[name] = subprocess.run(["git", "-C", opt("--clone"), "show",
                                  opt("--sha") + ":deploy/mise-a-jour/" + name],
                                 check=True, capture_output=True, text=True).stdout
sums = ""
for name in sorted(files):
    with open(os.path.join(out, name), "w") as fh:
        fh.write(files[name])
    digest = hashlib.sha256(files[name].encode()).hexdigest()
    if os.environ.get("FAKE_BUILD_BAD_SUMS") and name == "runtime-pins.txt":
        digest = "0" * 64
    sums += "%s  %s\n" % (digest, name)
with open(os.path.join(out, "SHA256SUMS"), "w") as fh:
    fh.write(sums)
'''

    def with_pins(self):
        builder = os.path.join(self.repo, "deploy", "mise-a-jour", "construction-runtime.py")
        with open(builder, "w", encoding="utf-8") as fh:
            fh.write(self.FAKE_BUILD)
        self.write("deploy/mise-a-jour/runtime-pins.txt", "psutil==1 --hash=sha256:%s\n" % ("a" * 64))
        self.write("deploy/mise-a-jour/runtime-build-pins.txt", "setuptools==1\n")
        _git(self.repo, "add", "deploy/mise-a-jour/runtime-pins.txt",
             "deploy/mise-a-jour/runtime-build-pins.txt")
        sha = self.version("1.2.0", "ameesh 1.2.0 (#8)")
        self.push()
        self.pr(8, sha)
        # the pins of the commit, not of the working tree
        self.write("deploy/mise-a-jour/runtime-pins.txt", "unreviewed\n")
        return sha

    def test_4786_assets_of_the_commit_are_attached(self):
        sha = self.with_pins()
        done = self.run_script("publier", sha)
        self.assertEqual(done.returncode, 0, done.stderr)
        assets = self.gh()["releases"]["v1.2.0"]["assets"]
        self.assertEqual(sorted(assets), ["SHA256SUMS", "ameesh-1.2.0-py3-none-any.whl",
                                          "runtime-build-pins.txt", "runtime-pins.txt"])
        self.assertEqual(assets["ameesh-1.2.0-py3-none-any.whl"], "wheel of " + sha)
        self.assertIn("psutil==1", assets["runtime-pins.txt"])
        self.assertEqual(len(assets["SHA256SUMS"].splitlines()), 3)
        # a version before #4778 (no runtime-pins.txt) has no asset
        self.pr(5, self.v100)
        done = self.run_script("publier", self.v100)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.gh()["releases"]["v1.0.0"]["assets"], {})

    def test_4786_a_group_writable_cache_does_not_stop_the_build(self):
        # umask 002 : ~/.cache 0775 sur un hôte standard (relecture #27073, B1)
        cache = os.path.join(self.tmp, ".cache")
        os.makedirs(cache)
        os.chmod(cache, 0o775)
        sha = self.with_pins()
        done = self.run_script("publier", sha)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.gh()["releases"]["v1.2.0"]["assets"]), 4)
        self.assertEqual([n for n in os.listdir(self.tmp) if "actifs" in n], [])

    def test_4786_dry_run_names_the_assets_without_building(self):
        sha = self.with_pins()
        env_fail = dict(self.env, FAKE_BUILD_FAILS="1")
        done = subprocess.run(["bash", os.path.join(self.repo, "deploy", "mise-a-jour", "poste.sh"),
                               "publier", sha], capture_output=True, text=True,
                              env=dict(env_fail, A_BLANC="1"), timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("actifs : roue ameesh", done.stdout)
        self.assertEqual(self.gh()["releases"], {})

    def test_4786_failed_or_unverified_assets_stop_before_the_tag(self):
        sha = self.with_pins()
        for flag, why in (("FAKE_BUILD_FAILS", "non construits"),
                          ("FAKE_BUILD_BAD_SUMS", "ne vérifie pas")):
            done = subprocess.run(["bash", os.path.join(self.repo, "deploy", "mise-a-jour",
                                                        "poste.sh"), "publier", sha],
                                  capture_output=True, text=True,
                                  env=dict(self.env, **{flag: "1"}), timeout=120)
            self.assertNotEqual(done.returncode, 0, flag)
            self.assertIn(why, done.stderr)
            self.assertNotIn("v1.2.0", self.remote_tags())
            self.assertEqual(_git(self.repo, "tag", "-l", "v1.2.0"), "")
            self.assertEqual(self.gh()["releases"], {})


if __name__ == "__main__":
    unittest.main()
