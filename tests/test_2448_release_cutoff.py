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


class ReleaseFixture(unittest.TestCase):
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

    def test_red_landing_next_cutoff_uses_the_published_green_content(self):
        """B1: a red landing must not make the next cutoff replay v1.11.8."""
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
        self.assertEqual(second["result"], "released", second)
        self.assertEqual(second["version"], "1.11.9")
        self.assertEqual(self.git(self.clone, "rev-parse", "%s^" % second["release_sha"]),
                         first["release_sha"])
        self.assertEqual(self.git(self.clone, "rev-parse", "origin/main"),
                         second["landing_sha"])

    def test_seven_red_cutoffs_keep_the_release_chain_green(self):
        """B1: the green chain is durable across >=7 successive red cutoffs."""
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
        versions = []
        with mock.patch.object(release_cutoff, "ci_reading", side_effect=reading):
            for slot in slots:
                report = release_cutoff.execute(self.cfg, None, self.project, now=slot)
                self.assertEqual(report["result"], "released", report)
                versions.append(report["version"])
        self.assertEqual(versions, ["1.11.8", "1.11.9", "1.11.10", "1.11.11",
                                    "1.11.12", "1.11.13", "1.11.14"])

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
