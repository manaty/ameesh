# SPDX-License-Identifier: AGPL-3.0-only
"""#2448: release at the compiled canon cutoffs, without an agent.

The Git fixture is a real bare origin and a real clone; CI and GitHub are
explicit fakes, and the staging script only writes an attestation under a
temporary HOME. No network, no host, no database.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import config, release_cutoff

FEDERATION = """federation: "0.1"
id: manaty
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules:
          - paths: ["src/ameesh/migrations/**", "**/*.sql"]
            class: sensitive
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules:
          - paths: ["src/ameesh/migrations/**", "**/*.sql"]
            reviews: 2
"""

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""

STAGING = """#!/usr/bin/env bash
set -euo pipefail
for a in "$@"; do last="$a"; done
root="$(cd "$(dirname "$0")/../.." && pwd)"
git -C "$root" merge-base --is-ancestor "$last" origin/main
mkdir -p "$HOME/.local/state/ameesh/mise-a-jour"
python3 - "$last" <<'EOF'
import json, os, sys
sha = sys.argv[1]
path = os.path.expanduser("~/.local/state/ameesh/mise-a-jour/actif-%s.json" % sha[:12])
json.dump({"sha": sha, "hotes": {name: {"actif": sha} for name in
          ("ameesh-agents-1", "ameesh-agents-2", "manaty-mesh-1", "pc-smichea")}},
          open(path, "w"))
EOF
echo "staged $last"
"""

MANIFEST = """# Paquet integration/paquet-h-test — composition

## Composition (2 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2000 | `aaaa` | `bbbb` | ok |
| 2 | #2002, #2003 | `cccc` | `dddd` | stacked |

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2001 | avis manquant |
| #2212 | déjà `merged` |
| #2004 | lot bien composé, non retiré |
"""


def _paris(hour: int, minute: int = 0, day: int = 18) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Europe/Paris")).timestamp()


class FakeGh:
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    def run(self, argv, stdin=None):
        FakeGh.calls.append(argv)
        if argv[:2] == ["release", "view"]:
            return 1, "", "not found"
        return 0, "", ""


class ReleaseFixtureMixin:
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-2448-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_DATE": "2026-10-18T10:00:00+0200",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.clone = self.root / "clone"
        self.canon = self.root / "canon"
        for folder in (self.seed, self.canon):
            folder.mkdir()
        self.git(self.root, "init", "--bare", "-q", str(self.origin))
        self.git(self.seed, "init", "-q", "-b", "main")
        self.write("pyproject.toml", '[project]\nname = "x"\nversion = "1.11.7"\n')
        (self.seed / "docs/versions").mkdir(parents=True)
        (self.seed / "docs/versions/1.11.7.md").write_text("old\n")
        (self.seed / "deploy/mise-a-jour").mkdir(parents=True)
        (self.seed / "deploy/mise-a-jour/staging.sh").write_text(STAGING)
        (self.seed / "scripts").mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/version-level.py",
                        self.seed / "scripts/version-level.py")
        os.chmod(self.seed / "deploy/mise-a-jour/staging.sh", 0o755)
        self.commit("base")
        self.git(self.seed, "tag", "-a", "v1.11.7", "-m", "ameesh 1.11.7")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "-q", "origin", "main", "--tags")
        self.git(self.seed, "checkout", "-qb", "integration/paquet-h-test")
        (self.seed / "reviews").mkdir()
        (self.seed / "reviews/integration-paquet-h-test.md").write_text(MANIFEST)
        (self.seed / "change.txt").write_text("new\n")
        self.commit("paquet")
        self.git(self.seed, "push", "-q", "origin", "integration/paquet-h-test")
        self.git(self.seed, "checkout", "-q", "main")
        self.git(self.seed, "merge", "--no-ff", "-q", "integration/paquet-h-test",
                 "-m", "Merge pull request #100 from manaty/integration/paquet-h-test\n\n"
                       "Paquet h-test — 2 unités (#2000, #2002)")
        self.head = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "push", "-q", "origin", "main")
        (self.canon / "processus").mkdir()
        (self.canon / "processus/mecanique.yaml").write_text(MECHANICS)
        (self.canon / "federation.yaml").write_text(FEDERATION)
        self.git(self.canon, "init", "-q")
        self.canon_commit = self.commit("canon", repo=self.canon)
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.root, "clone", "-q", str(self.origin), str(self.clone))
        fake_canon = types.SimpleNamespace(sources=[types.SimpleNamespace(
            mode="git", repo=str(self.canon), commit=self.canon_commit)])
        self.load_canon = mock.patch.object(
            release_cutoff.canon_mod, "load_configured", return_value=[fake_canon])
        self.load_canon.start()
        self.addCleanup(self.load_canon.stop)
        self.project = release_cutoff.ProjectRelease(
            name="ameesh", repo=str(self.clone), target="main", report="smichea",
            github="manaty/ameesh")
        self.cfg = config.Config(
            host="pc-smichea", humans="smichea", state_dir=str(self.state),
            canon=str(self.canon), canon_ref="HEAD",
            release={"host": "pc-smichea",
                     "projects": {"ameesh": {"repo": str(self.clone), "target": "main",
                                             "github": "manaty/ameesh"}}})
        FakeGh.calls = []
        self.gh = mock.patch.object(release_cutoff.plan_github, "Gh", FakeGh)
        self.gh.start()
        self.addCleanup(self.gh.stop)

    def git(self, repo, *args):
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def commit(self, message, repo=None):
        repo = self.seed if repo is None else repo
        self.git(repo, "add", "-A")
        self.git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-qm", message)
        return self.git(repo, "rev-parse", "HEAD")

    def write(self, path, text):
        target = self.seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def execute(self, *, now=None, ci=True, green=None, force=False):
        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": ci}), \
             mock.patch.object(release_cutoff, "last_green",
                               return_value=green or self.head):
            return release_cutoff.execute(self.cfg, None, self.project,
                                          now=now or _paris(11, 5), force=force)

class ReleaseFixture(ReleaseFixtureMixin, unittest.TestCase):

    def test_release_tags_green_head_generates_notes_and_runs_staging(self):
        report = self.execute()
        self.assertEqual(report["result"], "released", report)
        self.assertEqual(report["version"], "1.11.8")
        self.assertEqual(report["tag"], "v1.11.8")
        release_sha = report["release_sha"]
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"), release_sha)
        self.assertEqual(self.git(self.clone, "rev-parse", "v1.11.8^{commit}"), release_sha)
        self.assertEqual(release_cutoff.remote_tag(self.clone, "origin", "v1.11.8"),
                         release_sha)
        notes = self.git(self.clone, "show", "origin/main:docs/versions/1.11.8.md")
        self.assertIn("#2000", notes)
        self.assertIn("#2002", notes)
        self.assertIn("#2003", notes)
        self.assertIn("#2001", notes)
        self.assertNotIn("#2212", notes)
        self.assertNotIn("#2004", notes)
        self.assertEqual(report["staging"]["exit"], 0)
        attestation = self.home / ".local/state/ameesh/mise-a-jour" / (
            "actif-%s.json" % release_sha[:12])
        self.assertTrue(attestation.exists(), report)
        self.assertTrue(Path(report["report_path"]).exists())
        state = release_cutoff.load_state(self.cfg)["projects"]["ameesh"]
        self.assertEqual(state["last_result"], "released")
        self.assertEqual(state["last_cutoff"], _paris(11))
        self.assertTrue(any(call[:2] == ["release", "create"] for call in FakeGh.calls))

    def test_second_tick_is_already_recorded(self):
        first = self.execute()
        second = self.execute(now=_paris(11, 6))
        self.assertEqual(first["result"], "released")
        self.assertEqual(second["result"], "already-recorded")
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         first["release_sha"])

    def test_tag_recent_is_reconciled_without_second_version(self):
        first = self.execute()
        release_cutoff.save_state(self.cfg, {"schema": release_cutoff.SCHEMA,
                                             "projects": {}})
        # The annotated tag carries the test's real committer date; move the
        # simulated cutoff close to it so the durable recent-tag guard applies.
        tag_at = float(self.git(self.clone, "for-each-ref", "--format=%(taggerdate:unix)",
                                "refs/tags/v1.11.8"))
        second = self.execute(now=tag_at + 10)
        self.assertEqual(second["result"], "reconciled", second)
        self.assertEqual(second["version"], "1.11.8")
        self.assertEqual(second["release_sha"], first["release_sha"])
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         first["release_sha"])
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.8"), "v1.11.8")

    def test_published_tag_after_the_window_is_reconciled_not_missed(self):
        """Staging busy (75) after the push, window over: staged late, not "missed"."""
        with mock.patch.object(release_cutoff, "maybe_staging",
                               return_value={"exit": 75}):
            with self.assertRaises(release_cutoff.ReleaseBusy):
                self.execute()
        later = self.execute(now=_paris(11, 45))
        self.assertEqual(later["result"], "reconciled", later)
        self.assertEqual(later["version"], "1.11.8")
        self.assertEqual(later["staging"]["exit"], 0)

    def _red_cutoff(self, green):
        """A green B, a red commit on main, then the 11:00 cutoff: V and M."""
        self.write("red.txt", "red\n")
        self.commit("red change")
        self.git(self.seed, "push", "-q", "origin", "main")

        def reading(project, sha):
            return {"green": sha == green}

        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "released", report)
        self.assertNotEqual(report["landing_sha"], report["release_sha"])
        return report

    def test_green_landing_merge_is_green_content(self):
        """B1: CI green on the landing M itself must be trusted, not skipped."""
        green = self.head
        first = self._red_cutoff(green)
        landing = first["landing_sha"]
        self.assertTrue(release_cutoff.commit_is_green(
            self.clone, self.project, landing, reading={"green": True}))

        def reading(project, sha):
            return {"green": sha in (green, landing)}

        with mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            plan = release_cutoff.build_plan(self.cfg, self.project, now=_paris(23, 5))
        self.assertTrue(plan.ci["green"], plan.ci)
        self.assertTrue(plan.green)
        self.assertEqual(plan.base, landing)
        self.assertEqual(plan.last_green, landing)
        self.assertEqual(plan.version, "1.11.9")
        self.assertEqual(self.git(self.clone, "show", "%s:red.txt" % plan.base), "red")
        # The tagged content follows the validated landing: red.txt is in it.
        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T23:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            second = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(23, 5))
        self.assertEqual(second["result"], "released", second)
        self.assertEqual(self.git(self.clone, "rev-parse", "%s^" % second["release_sha"]),
                         landing)
        self.assertEqual(self.git(self.clone, "show",
                                  "%s:red.txt" % second["release_sha"]), "red")

    def test_red_child_of_a_green_landing_keeps_the_landing_as_base(self):
        """B1: a red child of a green landing M still finds M as last green."""
        green = self.head
        first = self._red_cutoff(green)
        landing = first["landing_sha"]
        self.git(self.seed, "pull", "-q", "--ff-only", "origin", "main")
        self.write("later.txt", "later\n")
        self.commit("later red change")
        self.git(self.seed, "push", "-q", "origin", "main")
        with mock.patch.object(release_cutoff, "ci_reading",
                               side_effect=lambda project, sha:
                               {"green": sha in (green, landing)}):
            plan = release_cutoff.build_plan(self.cfg, self.project, now=_paris(23, 5))
        self.assertFalse(plan.ci["green"], plan.ci)
        self.assertFalse(plan.green)
        self.assertEqual(plan.last_green, landing)
        self.assertEqual(plan.base, landing)

    def test_red_head_releases_last_green_with_a_landing_merge(self):
        green = self.head
        self.write("red.txt", "red\n")
        self.commit("red change")
        red = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "push", "-q", "origin", "main")

        def reading(project, sha):
            return {"green": sha == green}

        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "released", report)
        self.assertEqual(report["target_head"], red)
        self.assertNotEqual(report["release_sha"], report["landing_sha"])
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         report["landing_sha"])
        self.assertEqual(self.git(self.clone, "rev-parse", "v1.11.8^{commit}"),
                         report["release_sha"])
        # The tag names the green content and is reachable from main through M.
        self.git(self.clone, "merge-base", "--is-ancestor", report["release_sha"],
                 "origin/main")
        notes = self.git(self.clone, "show", "origin/main:docs/versions/1.11.8.md")
        self.assertIn("#2000", notes)
        self.assertTrue(any("non verte" in text for text in report["deviations"]))
        attestation = self.home / ".local/state/ameesh/mise-a-jour" / (
            "actif-%s.json" % report["release_sha"][:12])
        self.assertTrue(attestation.exists(), report)

    def test_red_landing_next_cutoff_is_a_no_op(self):
        """B1/N4: a red landing neither replays v1.11.8 nor tags an empty 1.11.9."""
        green = self.head
        self.write("red.txt", "red\n")
        self.commit("red change")
        self.git(self.seed, "push", "-q", "origin", "main")

        def reading(project, sha):
            return {"green": sha == green}

        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            first = release_cutoff.execute(self.cfg, None, self.project,
                                           now=_paris(11, 5))
        self.assertEqual(first["result"], "released", first)
        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T23:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            second = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(23, 5))
        self.assertEqual(second["result"], "nothing-new", second)
        self.assertEqual(second["version"], "1.11.8")
        self.assertEqual(second["base"], first["release_sha"])
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.9"), "")
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         first["landing_sha"])
        state = release_cutoff.load_state(self.cfg)["projects"]["ameesh"]
        self.assertEqual(state["last_result"], "nothing-new")
        self.assertEqual(state["last_cutoff"], _paris(23))

    def test_seven_red_cutoffs_release_once_then_no_op(self):
        """B1/N4: a main that stays red is released once, then each cutoff is a no-op."""
        green = self.head
        self.write("red.txt", "red\n")
        self.commit("red change")
        self.git(self.seed, "push", "-q", "origin", "main")

        def reading(project, sha):
            return {"green": sha == green}

        slots = [_paris(11, day=18), _paris(23, day=18),
                 _paris(11, day=19), _paris(23, day=19),
                 _paris(11, day=20), _paris(23, day=20),
                 _paris(11, day=21)]
        results = []
        with mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            for slot in slots:
                report = release_cutoff.execute(self.cfg, None, self.project, now=slot)
                results.append((report["result"], report["version"]))
        self.assertEqual(results, [("released", "1.11.8")] + [("nothing-new", "1.11.8")] * 6)
        self.assertEqual(sorted(self.git(self.clone, "tag", "--list", "v1.11.*").split()),
                         ["v1.11.7", "v1.11.8"])

    def test_green_head_already_tagged_is_a_no_op(self):
        """N4: nothing merged since the last release: no empty release."""
        first = self.execute()
        self.assertEqual(first["result"], "released", first)
        second = self.execute(now=_paris(23, 5))
        self.assertEqual(second["result"], "nothing-new", second)
        self.assertEqual(second["tag"], "v1.11.8")
        self.assertIsNone(second.get("staging"))
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.9"), "")
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         first["release_sha"])

    def test_last_green_returns_none_without_any_green(self):
        """N5: no green in the walk means refuse, never fall back silently."""
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": False}):
            self.assertIsNone(release_cutoff.last_green(
                self.clone, self.head, self.project))

    def test_red_head_without_green_refuses(self):
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": False}), \
             mock.patch.object(release_cutoff, "last_green", return_value=None):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "refused-no-green", report)
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.8"), "")
        self.assertTrue(Path(report["report_path"]).exists())

    def test_red_history_without_green_refuses_end_to_end(self):
        """N5: the real last_green walk finds no green and never uses v1.11.7."""
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": False}):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "refused-no-green", report)
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.8"), "")
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"), self.head)

    def test_explicit_version_below_the_floor_is_refused(self):
        """N6: `--version` below the content's minor floor, without a bump on main."""
        self.write("src/ameesh/migrations/0499_test.sql",
                   "alter table example add column extra integer;\n")
        self.commit("new migration")
        self.git(self.seed, "push", "-q", "origin", "main")
        head = self.git(self.seed, "rev-parse", "HEAD")
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": True}):
            with self.assertRaisesRegex(release_cutoff.ReleaseError, "minor floor"):
                release_cutoff.execute(self.cfg, None, self.project, now=_paris(11, 5),
                                       version="1.11.8")
            plan = release_cutoff.build_plan(self.cfg, self.project, now=_paris(11, 5),
                                             version="1.12.0")
        self.assertEqual(plan.version, "1.12.0")
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.8"), "")
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"), head)

    def test_version_level_minor_is_used(self):
        self.write("src/ameesh/migrations/0499_test.sql",
                   "alter table example add column extra integer;\n")
        self.commit("new migration")
        self.git(self.seed, "push", "-q", "origin", "main")
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": True}):
            plan = release_cutoff.build_plan(self.cfg, self.project, now=_paris(11, 5))
        self.assertEqual(plan.floor, "minor", plan.floor_reason)
        self.assertEqual(plan.version, "1.12.0")

    def test_untagged_patch_is_raised_to_the_minor_floor(self):
        """B4: an untagged version bump cannot bypass the content's floor."""
        self.write("src/ameesh/migrations/0499_test.sql",
                   "alter table example add column extra integer;\n")
        self.write("pyproject.toml", '[project]\nname = "x"\nversion = "1.11.8"\n')
        self.commit("patch bump with a migration")
        self.git(self.seed, "push", "-q", "origin", "main")
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": True}):
            plan = release_cutoff.build_plan(self.cfg, self.project, now=_paris(11, 5))
            self.assertEqual(plan.floor, "minor", plan.floor_reason)
            self.assertEqual(plan.version, "1.12.0")
            self.assertIn("follows that floor", plan.notes)
            self.assertNotIn("patch by the build/beta rhythm", plan.notes)
            with self.assertRaises(release_cutoff.ReleaseError):
                release_cutoff.build_plan(self.cfg, self.project, now=_paris(11, 5),
                                          version="1.11.8")
            with self.assertRaises(release_cutoff.ReleaseError):
                release_cutoff.build_plan(self.cfg, self.project, now=_paris(11, 5),
                                          version="2.0.0")

    def test_patch_override_still_refuses_major_and_unknown(self):
        """B4: the override never authorises a major or an unknown floor."""
        project = dataclasses.replace(self.project, version_bump="patch")
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": True}):
            with self.assertRaises(release_cutoff.ReleaseError):
                release_cutoff.build_plan(self.cfg, project, now=_paris(11, 5),
                                          version="2.0.0")
        with self.assertRaises(release_cutoff.ReleaseError):
            release_cutoff.next_version("1.11.7", bump="patch", floor="major")
        with self.assertRaises(release_cutoff.ReleaseError):
            release_cutoff.next_version("1.11.7", bump="patch", floor="unknown")

    def test_patch_override_refuses_an_untagged_major_bump(self):
        """B4: an untagged major bump is refused even under a patch override."""
        project = dataclasses.replace(self.project, version_bump="patch")
        self.write("pyproject.toml", '[project]\nname = "x"\nversion = "2.0.0"\n')
        self.commit("major bump")
        self.git(self.seed, "push", "-q", "origin", "main")
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": True}):
            with self.assertRaises(release_cutoff.ReleaseError):
                release_cutoff.build_plan(self.cfg, project, now=_paris(11, 5))

    def test_manifest_parser_keeps_composition_and_drops_non_exclusions(self):
        parsed = release_cutoff.parse_manifest(MANIFEST)
        self.assertEqual(parsed["included"], [2000, 2002, 2003])
        self.assertEqual([lot for lot, _ in parsed["excluded"]], [2001])

    def test_notes_are_generated_from_the_merged_packet(self):
        with mock.patch.object(release_cutoff, "ci_reading",
                               return_value={"green": True}):
            plan = release_cutoff.build_plan(self.cfg, self.project, now=_paris(11, 5))
        self.assertEqual(plan.version, "1.11.8")
        self.assertIn("#2000", plan.notes)
        self.assertIn("#2001", plan.notes)
        self.assertNotIn("#2212", plan.notes)
        self.assertEqual(plan.packets[0].package, "paquet-h-test")


def _job(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _run(run_id, jobs, *, event="push", workflow="tests", conclusion="success"):
    return {"id": run_id,
            "url": "https://github.com/manaty/ameesh/actions/runs/%d" % run_id,
            "workflow": workflow, "event": event, "conclusion": conclusion,
            "jobs": jobs}


class PacketTreeGreenTest(ReleaseFixtureMixin, unittest.TestCase):
    """#2692: a packet merge whose tree is the green packet head is green.

    The target's CI is still in progress; the packet head's full suite was
    validated before the merge, under the same jobs. These are the three
    witnesses of the lot, plus the two guards (red target, red packet head)
    and the evidence cited in the release notes.
    """

    def reference_run(self):
        return _run(500, [
            _job("plan"),
            _job("psycopg 1/8"),
            _job("psql 1/16"),
            _job("plateforme macOS"),
            _job("bilan des impactés"),
            _job("tests"),
            _job("version number", "skipped"),
        ])

    def packet_runs(self, *, missing=None):
        """Two runs: the PR run (matrix) and the packet gate (flow checks)."""
        jobs = [
            _job("plan"),
            _job("psycopg 1/8"),
            _job("psql 1/16"),
            _job("plateforme macOS"),
            _job("bilan des impactés"),
            _job("tests"),
        ]
        if missing:
            jobs = [job for job in jobs if job["name"] != missing]
        flows = [
            _job("tests (flux) psql 1/16"),
            _job("tests (flux) psycopg 1/8"),
        ]
        if missing:
            flows = [job for job in flows if not job["name"].endswith(" " + missing)]
        return [
            _run(501, jobs, event="pull_request"),
            _run(502, [
                _job("couverture (PR ouverte sur cette tête)"),
                _job("suite complète du paquet", "skipped"),
                *flows,
            ], workflow="paquet"),
        ]

    def second_packet(self, *, conflict=False, subject=(
            "Merge pull request #101 from manaty/integration/paquet-h-2692")):
        """A green main commit, then a packet head merged as a packet merge."""
        self.write("main-2.txt", "main\n")
        base = self.commit("main green base")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.seed, "checkout", "-qb", "integration/paquet-h-2692")
        self.write("packet-2.txt", "packet\n")
        self.write("main-2.txt", "packet\n")
        head = self.commit("packet head")
        self.git(self.seed, "push", "-q", "origin", "integration/paquet-h-2692")
        self.git(self.seed, "checkout", "-q", "main")
        if not conflict:
            self.git(self.seed, "merge", "--no-ff", "-q",
                     "integration/paquet-h-2692", "-m", subject)
            merge = self.git(self.seed, "rev-parse", "HEAD")
            self.git(self.seed, "push", "-q", "origin", "main")
            return base, base, head, merge
        self.write("main-2.txt", "main edited\n")
        second = self.commit("main green second")
        proc = subprocess.run(["git", "-C", str(self.seed), "merge", "--no-ff",
                               "integration/paquet-h-2692"],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "a conflict is expected")
        self.write("main-2.txt", "resolved\n")
        self.commit(subject)
        merge = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "push", "-q", "origin", "main")
        return base, second, head, merge

    def ci(self, *, green=(), pending=(), failed=()):
        def reading(project, sha):
            if sha in failed:
                return {"green": False, "pending": [], "failed": ["tests [main]"],
                        "missing": []}
            if sha in pending:
                return {"green": False, "pending": ["tests [main]"], "failed": [],
                        "missing": []}
            return {"green": sha in green}
        return reading

    def suites(self, reference, head, *, missing=None):
        def reading(project, sha):
            if sha == head:
                return {"head": sha, "runs": self.packet_runs(missing=missing)}
            if sha == reference:
                return {"head": sha, "runs": [self.reference_run()]}
            return {"head": sha, "runs": []}
        return reading

    def plan(self, reading, suites):
        with mock.patch.object(release_cutoff, "ci_reading", side_effect=reading), \
             mock.patch.object(release_cutoff, "suite_reading", side_effect=suites):
            return release_cutoff.build_plan(self.cfg, self.project,
                                             now=_paris(11, 5))

    def test_equal_tree_with_a_green_packet_and_running_target_is_green(self):
        """Witness 1: arbre égal + paquet vert + main en cours → vert."""
        base, _second, head, merge = self.second_packet()
        plan = self.plan(self.ci(green=(base, head), pending=(merge,)),
                         self.suites(base, head))
        self.assertTrue(plan.ci.get("pending"), plan.ci)
        self.assertFalse(plan.ci.get("green"))
        self.assertTrue(plan.green)
        self.assertEqual(plan.base, merge)
        self.assertEqual(plan.last_green, merge)
        self.assertEqual(plan.tree_green["head"], head)
        self.assertEqual(plan.tree_green["reference"], base)
        self.assertEqual(plan.tree_green["tree"],
                         self.git(self.clone, "rev-parse", "%s^{tree}" % merge))
        self.assertIn("tree of %s" % merge[:12], plan.notes)
        self.assertIn(head[:12], plan.notes)
        self.assertIn("actions/runs/501", plan.notes)
        self.assertIn("reference run", plan.notes)
        self.assertIn("#2000", plan.notes)

    def test_equal_tree_with_a_red_target_never_inherits(self):
        """Witness 2: arbre égal + main rouge → pas vert."""
        base, _second, head, merge = self.second_packet()
        plan = self.plan(self.ci(green=(base, head), failed=(merge,)),
                         self.suites(base, head))
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)
        self.assertNotIn("tree of %s" % merge[:12], plan.notes)

    def test_different_tree_never_inherits(self):
        """Witness 3: arbre différent (résolution de conflit) → pas d'héritage."""
        base, second, head, merge = self.second_packet(conflict=True)
        self.assertFalse(release_cutoff.same_tree(
            self.clone, merge, head))
        plan = self.plan(self.ci(green=(base, second, head), pending=(merge,)),
                         self.suites(second, head))
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, second)

    def test_a_red_packet_head_never_inherits(self):
        """Guard: a red on main stays red only while the packet head is green."""
        base, _second, head, merge = self.second_packet()
        plan = self.plan(self.ci(green=(base,), pending=(merge,), failed=(head,)),
                         self.suites(base, head))
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)

    def test_a_different_job_set_never_inherits(self):
        """Same tree is not enough: the packet's jobs must be the target's."""
        base, _second, head, merge = self.second_packet()
        plan = self.plan(self.ci(green=(base, head), pending=(merge,)),
                         self.suites(base, head, missing="psql 1/16"))
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)

    def test_a_smaller_ancestor_suite_cannot_replace_the_last_green_suite(self):
        base, _second, head, merge = self.second_packet()
        older = self.git(self.seed, "rev-parse", base + "^1")

        def suites(project, sha):
            if sha == head:
                return {"head": sha, "runs": self.packet_runs(missing="psql 1/16")}
            run = self.reference_run()
            if sha == older:
                run["jobs"] = [job for job in run["jobs"]
                               if job["name"] != "psql 1/16"]
            return {"head": sha, "runs": [run]}

        plan = self.plan(self.ci(green=(base, head), pending=(merge,)), suites)
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)

    def test_a_failed_required_job_never_inherits(self):
        """A green run cannot hide a required job that actually failed."""
        base, _second, head, merge = self.second_packet()

        def suites(project, sha):
            runs = self.packet_runs()
            if sha == head:
                runs[0]["jobs"][1]["conclusion"] = "failure"
                return {"head": sha, "runs": runs}
            return {"head": sha, "runs": [self.reference_run()]}

        plan = self.plan(self.ci(green=(base, head), pending=(merge,)), suites)
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)

    def test_a_similarly_named_job_never_stands_in_for_a_required_one(self):
        """Only an exact job, a caller prefix or a flow run proves it."""
        base, _second, head, merge = self.second_packet()

        def suites(project, sha):
            runs = self.packet_runs()
            if sha == head:
                runs[0]["jobs"] = [job for job in runs[0]["jobs"]
                                   if job["name"] != "tests"]
                runs[0]["jobs"].append(_job("sommaire des tests"))
                return {"head": sha, "runs": runs}
            return {"head": sha, "runs": [self.reference_run()]}

        plan = self.plan(self.ci(green=(base, head), pending=(merge,)), suites)
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)

    def test_execute_releases_the_packet_tree_and_cites_the_runs(self):
        """The release tags the packet merge's content, not the previous green."""
        base, _second, head, merge = self.second_packet()
        reading = self.ci(green=(base, head), pending=(merge,))
        suites = self.suites(base, head)
        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading), \
             mock.patch.object(release_cutoff, "suite_reading", side_effect=suites):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "released", report)
        self.assertEqual(report["version"], "1.11.8")
        self.assertEqual(report["target_head"], merge)
        self.assertEqual(report["release_sha"], report["landing_sha"])
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         report["release_sha"])
        self.assertEqual(self.git(self.clone, "rev-parse", "v1.11.8^{commit}"),
                         report["release_sha"])
        self.assertEqual(report["tree_green"]["head"], head)
        notes = self.git(self.clone, "show", "origin/main:docs/versions/1.11.8.md")
        self.assertIn("tree of %s" % merge[:12], notes)
        self.assertIn(head[:12], notes)
        self.assertIn("actions/runs/501", notes)
        self.assertTrue(any("arbre identique" in text for text in report["deviations"]),
                        report["deviations"])


class StagingTimeoutTest(unittest.TestCase):
    def test_timeout_accepts_bytes_and_kills_the_process_group(self):
        """B2: partial bytes output must not break the report, children die."""
        tmp = tempfile.TemporaryDirectory(prefix="ameesh-2448-timeout-")
        self.addCleanup(tmp.cleanup)
        worktree = Path(tmp.name) / "worktree"
        script = worktree / "deploy/mise-a-jour/staging.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env bash\n")

        class FakeProc:
            pid = 424242
            returncode = -15

            def __init__(self):
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("bash", timeout,
                                                    output=b"partial output\n")
                return "", ""

        proc = FakeProc()
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=str(Path(tmp.name) / "repo"), target="main",
            staging="deploy/mise-a-jour/staging.sh", github="manaty/ameesh")
        with mock.patch.object(release_cutoff, "_worktree",
                               return_value=(tmp.name, str(worktree))), \
             mock.patch.object(release_cutoff, "_drop_worktree"), \
             mock.patch.object(release_cutoff.subprocess, "Popen",
                               return_value=proc) as popen, \
             mock.patch.object(release_cutoff, "_kill_group") as kill:
            with self.assertRaises(release_cutoff.TransientReleaseError):
                release_cutoff.run_staging(project, "a" * 40, [],
                                           state_dir=tmp.name, timeout_s=1)
        self.assertGreaterEqual(kill.call_count, 1)
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))
        log = next((Path(tmp.name) / "release").glob("staging-*.log"))
        self.assertIn("partial output", log.read_text())


class PureReleaseTest(unittest.TestCase):
    def test_a_rerun_of_the_same_suite_is_not_ambiguous(self):
        same = {"runs": [_run(2, [_job("plan"), _job("tests")]),
                         _run(1, [_job("plan"), _job("tests")])]}
        self.assertIsNotNone(release_cutoff._full_suite_run(same))
        other = {"runs": [_run(2, [_job("plan")]), _run(1, [_job("tests")])]}
        self.assertIsNone(release_cutoff._full_suite_run(other))
        empty = {"runs": [_run(3, [])]}
        self.assertIsNone(release_cutoff._full_suite_run(empty))

    def test_version_bumps(self):
        self.assertEqual(release_cutoff.next_version("1.11.7"), "1.11.8")
        self.assertEqual(release_cutoff.next_version("1.11.7", bump="level", floor="minor"),
                         "1.12.0")
        with self.assertRaises(release_cutoff.ReleaseError):
            release_cutoff.next_version("1.11.7", bump="level", floor="major")
        with self.assertRaises(release_cutoff.ReleaseError):
            release_cutoff.version_tuple("1.11")

    def test_due_window_and_recorded_cutoff(self):
        class Rules:
            timezone = "Europe/Paris"
            cutoffs = ("11:00", "23:00")

            def zone(self):
                return ZoneInfo(self.timezone)

            def cutoff(self, now):
                return _paris(11) if now >= _paris(11) else _paris(23, day=17)

        self.assertEqual(release_cutoff.due(Rules(), _paris(11, 5), {})["label"], "11:00")
        self.assertIsNone(release_cutoff.due(Rules(), _paris(11, 5),
                                             {"last_cutoff": _paris(11)}))
        self.assertTrue(release_cutoff.due(Rules(), _paris(12, 0), {})["missed"])
        self.assertIsNone(release_cutoff.due(Rules(), _paris(10, 59), {}))
        self.assertIsNone(release_cutoff.due(Rules(), _paris(18, 0), {}))

    def test_cutoff_slot_uses_the_compiled_label(self):
        class Rules:
            timezone = "Europe/Paris"
            cutoffs = ("11:00", "23:00")

            def zone(self):
                return ZoneInfo(self.timezone)

            def cutoff(self, now):
                return _paris(11)

        at, label = release_cutoff.cutoff_slot(Rules(), _paris(12))
        self.assertEqual((at, label), (_paris(11), "11:00"))


if __name__ == "__main__":
    unittest.main()
