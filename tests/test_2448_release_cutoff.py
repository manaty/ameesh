# SPDX-License-Identifier: AGPL-3.0-only
"""#2448: release at the compiled canon cutoffs, without an agent.

The Git fixture is a real bare origin and a real clone; CI and GitHub are
explicit fakes, and the staging script only writes an attestation under a
temporary HOME. No network, no host, no database.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest

from ameesh import platform
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
        # The fixture changes HOME: a python3 shim (mise, pyenv) would then
        # resolve a different installation or wait for a network download.
        # Run the test interpreter, as the parent process already does.
        (self.seed / "deploy/mise-a-jour/staging.sh").write_text(
            STAGING.replace("\npython3 -", "\n%s -" % shlex.quote(sys.executable)))
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

    def test_reported_poste_attestation_is_complete_but_reruns_staging(self):
        """#3848 : trois VM + poste reporte = attestation complete, et une
        release ne relance le staging que sur le poste revenu, pour qu'il
        rattrape le commit actif (relecture dev1, #3853)."""
        sha = self.git(self.clone, "rev-parse", "origin/main")
        folder = self.home / ".local/state/ameesh/mise-a-jour"
        folder.mkdir(parents=True)
        path = folder / ("actif-%s.json" % sha[:12])
        data = {"sha": sha, "date": "2026-10-18T09:00:00Z",
                "hotes": {hote: {"actif": sha} for hote in release_cutoff.STAGING_VMS},
                "reportes": {"pc-smichea": {"raison": "poste absent",
                                            "mise_a_jour": "a son retour"}}}
        path.write_text(json.dumps(data))
        self.assertEqual(release_cutoff.read_attestation(sha)["reportes"],
                         data["reportes"])
        # depuis une VM, le poste serait encore reporte : pas de relance a vide
        with mock.patch.dict(os.environ, {"HOTE_LOCAL": "ameesh-agents-1"}), \
                mock.patch.object(release_cutoff, "run_staging") as stage:
            out = release_cutoff.maybe_staging(self.project, sha, [], self.cfg.state_dir)
        stage.assert_not_called()
        self.assertEqual(out["skipped"], "already attested")
        # le poste revenu (la sequence tourne sur lui) : relance pour le rattraper
        with mock.patch.dict(os.environ, {"HOTE_LOCAL": "pc-smichea"}), \
                mock.patch.object(release_cutoff, "run_staging",
                                  return_value={"exit": 0}) as stage:
            out = release_cutoff.maybe_staging(self.project, sha, [], self.cfg.state_dir)
        stage.assert_called_once()
        self.assertEqual(out["exit"], 0)
        # les quatre hotes prouves : plus de relance
        data["hotes"]["pc-smichea"] = {"actif": sha}
        data.pop("reportes")
        path.write_text(json.dumps(data))
        with mock.patch.object(release_cutoff, "run_staging") as stage:
            out = release_cutoff.maybe_staging(self.project, sha, [], self.cfg.state_dir)
        stage.assert_not_called()
        self.assertEqual(out["skipped"], "already attested")

    def test_reported_server_attestation_is_complete(self):
        """#147558 : sans acces root, le serveur peut etre reporte ; les deux
        VM d'agents restent requises et le poste prouve fait sauter la relance."""
        sha = self.git(self.clone, "rev-parse", "origin/main")
        folder = self.home / ".local/state/ameesh/mise-a-jour"
        folder.mkdir(parents=True)
        path = folder / ("actif-%s.json" % sha[:12])
        data = {"sha": sha, "date": "2026-10-18T09:00:00Z",
                "hotes": {hote: {"actif": sha} for hote in (
                    *release_cutoff.STAGING_AGENTS, release_cutoff.STAGING_POSTE)},
                "reportes": {"manaty-mesh-1": {"raison": "sans-acces root"}}}
        path.write_text(json.dumps(data))
        self.assertIsNotNone(release_cutoff.read_attestation(sha))
        with mock.patch.object(release_cutoff, "run_staging") as stage:
            out = release_cutoff.maybe_staging(self.project, sha, [], self.cfg.state_dir)
        stage.assert_not_called()
        self.assertEqual(out["skipped"], "already attested")
        # poste reporte en plus : la relance reste due pour son retour
        data["reportes"]["pc-smichea"] = {"raison": "poste absent"}
        data["hotes"].pop("pc-smichea")
        path.write_text(json.dumps(data))
        self.assertIsNotNone(release_cutoff.read_attestation(sha))
        with mock.patch.dict(os.environ, {"HOTE_LOCAL": "pc-smichea"}), \
                mock.patch.object(release_cutoff, "run_staging",
                                  return_value={"exit": 0}) as stage:
            release_cutoff.maybe_staging(self.project, sha, [], self.cfg.state_dir)
        stage.assert_called_once()

    def test_staging_local_host_follows_staging_sh(self):
        """Meme ordre que staging.sh : HOTE_LOCAL, AMEESH_HOST, config, nom court."""
        config = self.root / "config-hote.json"
        config.write_text(json.dumps({"host": "PC-Smichea"}))
        propre = {k: v for k, v in os.environ.items()
                  if k not in ("HOTE_LOCAL", "AMEESH_HOST", "AGENT_MESH_HOST",
                               "AGENT_MESH_CONFIG")}
        cas = [({"HOTE_LOCAL": "ameesh-agents-1", "AMEESH_HOST": "pc-smichea"},
                "ameesh-agents-1"),
               ({"AMEESH_HOST": "Ameesh-Agents-2"}, "ameesh-agents-2"),
               ({"AMEESH_CONFIG": str(config)}, "pc-smichea"),
               ({"AMEESH_CONFIG": str(config) + ".absent"},
                os.uname().nodename.split(".")[0].lower())]
        for env, attendu in cas:
            with self.subTest(env), mock.patch.dict(os.environ, dict(propre, **env),
                                                    clear=True):
                self.assertEqual(release_cutoff.staging_local_host(), attendu)

    def test_reported_poste_attestation_must_be_exact(self):
        sha = self.git(self.clone, "rev-parse", "origin/main")
        folder = self.home / ".local/state/ameesh/mise-a-jour"
        folder.mkdir(parents=True)
        path = folder / ("actif-%s.json" % sha[:12])
        vms = {hote: {"actif": sha} for hote in release_cutoff.STAGING_VMS}
        cas = {
            'trois VM sans report': {"sha": sha, "hotes": vms},
            'deux VM seulement': {"sha": sha,
                                  "hotes": {k: vms[k] for k in list(vms)[:2]},
                                  "reportes": {"pc-smichea": {"raison": "absent"}}},
            'hote reporte inconnu': {"sha": sha, "hotes": vms,
                                     "reportes": {"ailleurs": {"raison": "absent"}}},
            'raison vide': {"sha": sha, "hotes": vms,
                            "reportes": {"pc-smichea": {"raison": ""}}},
            'actif sans rapport': {"sha": sha,
                                   "hotes": {**vms, "pc-smichea": {"actif": "b" * 40}},
                                   "reportes": {"pc-smichea": {"raison": "absent"}}},
        }
        for nom, contenu in cas.items():
            with self.subTest(nom):
                path.write_text(json.dumps(contenu))
                self.assertIsNone(release_cutoff.read_attestation(sha))

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

    def test_equal_tree_with_a_red_target_inherits(self):
        """Witness 2, #3633 (décision owner #141353 a) : arbre égal + paquet
        vert + main rouge → vert. Avant, un rouge conclu restait rouge."""
        base, _second, head, merge = self.second_packet()
        plan = self.plan(self.ci(green=(base, head), failed=(merge,)),
                         self.suites(base, head))
        self.assertTrue(plan.ci.get("failed"), plan.ci)
        self.assertTrue(plan.green)
        self.assertEqual(plan.base, merge)
        self.assertEqual(plan.last_green, merge)
        self.assertEqual(plan.tree_green["head"], head)
        self.assertEqual(plan.tree_green["reference"], base)
        self.assertIn("tree of %s" % merge[:12], plan.notes)

    def test_equal_tree_with_a_target_without_ci_inherits(self):
        """#3633 : une tête sans aucune lecture de CI (annulée, jamais lancée)
        hérite de la même façon."""
        base, _second, head, merge = self.second_packet()

        def reading(project, sha):
            if sha in (base, head):
                return {"green": True}
            return {"green": False, "pending": [], "failed": [],
                    "missing": ["tests"]}

        plan = self.plan(reading, self.suites(base, head))
        self.assertTrue(plan.green)
        self.assertEqual(plan.base, merge)
        self.assertEqual(plan.tree_green["head"], head)

    def test_a_red_target_with_a_red_packet_head_stays_red(self):
        """#3633 : l'héritage exige une tête de paquet entièrement verte."""
        base, _second, head, merge = self.second_packet()
        plan = self.plan(self.ci(green=(base,), failed=(merge, head)),
                         self.suites(base, head))
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, base)

    def test_a_red_target_with_a_different_tree_stays_red(self):
        """#3633 : une résolution de conflit rend l'arbre différent ; le rouge
        de la cible reste alors rouge."""
        base, second, head, merge = self.second_packet(conflict=True)
        plan = self.plan(self.ci(green=(base, second, head), failed=(merge,)),
                         self.suites(second, head))
        self.assertFalse(plan.green)
        self.assertIsNone(plan.tree_green)
        self.assertEqual(plan.base, second)
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

    def test_next_cutoff_starts_from_the_release_on_a_green_tree(self):
        """#3641 : V, release sur M rouge à arbre vert, est le dernier vert.

        La coupe suivante doit repartir de V (déjà taguée), pas du vert B
        antérieur : sinon `staging_pending.regressions(B)` voit V actif et
        retient le paquet suivant (`kept_active` vide de release).
        """
        base, _second, head, merge = self.second_packet()
        reading = self.ci(green=(base, head), failed=(merge,))
        suites = self.suites(base, head)
        planned = self.plan(reading, suites)
        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading), \
             mock.patch.object(release_cutoff, "suite_reading", side_effect=suites):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "released", report)
        release_sha = report["release_sha"]
        self.assertEqual(self.git(self.clone, "rev-parse", "%s^1" % release_sha), merge)
        # #3641 : la tête est rouge, pas « en cours » ; la preuve d'arbre
        # reste citée à part dans le rapport et dans l'écart.
        self.assertTrue(any("CI rouge" in text for text in report["deviations"]),
                        report["deviations"])
        body = release_cutoff._report_body(
            self.project, planned, result="released", version=planned.version,
            tag="v1.11.8", release_sha=release_sha, staging=None,
            deviations=report["deviations"], previous=planned.previous,
            cutoff_at=planned.cutoff_at, cutoff_label=planned.cutoff_label,
            rules=types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris")))
        self.assertIn("CI tête: rouge", body)

        plan = self.plan(reading, suites)

        self.assertEqual(plan.head, release_sha)
        self.assertEqual(plan.base, release_sha)
        self.assertTrue(plan.empty, plan)
        self.assertEqual(plan.previous["tag"], "v1.11.8")
        self.assertFalse(plan.kept_active, plan.kept_active)

    def test_third_packet_on_a_release_on_a_green_tree_is_cut(self):
        """#3641, B P1 de la revue : un 3e paquet fusionné sur V est coupé.

        V (release sur M rouge à arbre vert) reste la base de coupe, mais
        `tree_green` ne peut pas prouver la suite contre V, qui n'a pas de
        suite propre. La preuve se rejoue contre B, le vert qui a validé
        l'arbre de M : sans cela le plan est vide, « nothing new since
        v1.11.8 », et le paquet P3 n'est jamais coupé.
        """
        base, _second, head, merge = self.second_packet()
        reading = self.ci(green=(base, head), failed=(merge,))
        suites = self.suites(base, head)
        with mock.patch.dict(os.environ,
                             {"GIT_COMMITTER_DATE": "2026-10-18T11:04:00+0200"}), \
             mock.patch.object(release_cutoff, "ci_reading", side_effect=reading), \
             mock.patch.object(release_cutoff, "suite_reading", side_effect=suites):
            report = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(11, 5))
        self.assertEqual(report["result"], "released", report)
        release_sha = report["release_sha"]

        # 3e paquet : tête verte fusionnée --no-ff sur V (la cible est rouge,
        # son arbre est celui de la tête du paquet).
        self.git(self.seed, "fetch", "-q", "origin")
        self.git(self.seed, "checkout", "-q", "-B", "main", "origin/main")
        self.git(self.seed, "checkout", "-qb", "integration/paquet-h-3")
        self.write("packet-3.txt", "packet 3\n")
        head3 = self.commit("packet head 3")
        self.git(self.seed, "push", "-q", "origin", "integration/paquet-h-3")
        self.git(self.seed, "checkout", "-q", "main")
        self.git(self.seed, "merge", "--no-ff", "-q", "integration/paquet-h-3",
                 "-m", "Merge pull request #102 from manaty/integration/paquet-h-3")
        merge3 = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "push", "-q", "origin", "main")

        def reading3(project, sha):
            if sha in (base, head, head3):
                return {"green": True}
            if sha in (merge, merge3):
                return {"green": False, "pending": [],
                        "failed": ["tests [main]"], "missing": []}
            return {"green": False}

        def suites3(project, sha):
            if sha in (head, head3):
                return {"head": sha, "runs": self.packet_runs()}
            if sha == base:
                return {"head": sha, "runs": [self.reference_run()]}
            return {"head": sha, "runs": []}

        plan = self.plan(reading3, suites3)
        self.assertTrue(plan.green, plan)
        self.assertIsNotNone(plan.tree_green, plan)
        self.assertEqual(plan.tree_green["head"], head3)
        self.assertEqual(plan.tree_green["reference"], base)
        self.assertEqual(plan.base, merge3)
        self.assertFalse(plan.empty, plan)
        self.assertEqual(plan.last_green, merge3)
        self.assertIn("tree of %s" % merge3[:12], plan.notes)
        self.assertIn(head3[:12], plan.notes)


class HeadCiStateTextTest(unittest.TestCase):
    """#3641 : le rapport dit l'état lu de la CI de la tête."""

    def test_red_running_cancelled_and_absent_are_named(self):
        self.assertEqual(release_cutoff.ci_state_text({"green": True}), "verte")
        self.assertEqual(release_cutoff.ci_state_text(
            {"green": False, "failed": ["tests [main]"]}), "rouge")
        self.assertEqual(release_cutoff.ci_state_text(
            {"green": False, "negative": ["tests"]}), "rouge")
        self.assertEqual(release_cutoff.ci_state_text(
            {"green": False, "pending": ["tests [main]"]}), "en cours")
        self.assertEqual(release_cutoff.ci_state_text(
            {"green": False, "unconcluded": [{"check": "tests"}]}), "non conclue")
        self.assertEqual(release_cutoff.ci_state_text(
            {"green": False, "inconclusive": ["tests"], "failed": ["tests"]}),
            "non concluante")
        self.assertEqual(release_cutoff.ci_state_text(
            {"green": False, "missing": ["tests"]}), "absente")
        self.assertEqual(release_cutoff.ci_state_text({"green": False, "runs": 0}),
                         "absente")
        self.assertEqual(release_cutoff.ci_state_text(None), "absente")


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
                # as Popen: a retried communicate returns the whole output
                return "partial output\nrest\n", ""

        proc = FakeProc()
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=str(Path(tmp.name) / "repo"), target="main",
            staging="deploy/mise-a-jour/staging.sh", github="manaty/ameesh")
        with mock.patch.object(release_cutoff, "_worktree",
                               return_value=(tmp.name, str(worktree))), \
             mock.patch.object(release_cutoff, "_drop_worktree"), \
             mock.patch.object(release_cutoff.subprocess, "Popen",
                               return_value=proc) as popen, \
             mock.patch.object(release_cutoff, "_poste_units", return_value=[]), \
             mock.patch.object(release_cutoff, "_kill_group") as kill:
            with self.assertRaises(release_cutoff.TransientReleaseError):
                release_cutoff.run_staging(project, "a" * 40, [],
                                           state_dir=tmp.name, timeout_s=1)
        self.assertGreaterEqual(kill.call_count, 1)
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))
        log = next((Path(tmp.name) / "release").glob("staging-*.log"))
        self.assertIn("partial output", log.read_text())



# Fake `systemctl --user`: the transient units live in $UNITES_ETAT ("name
# state pid" lines), alive while their pid is (a zombie is not); `stop` kills them unless
# STOP_SANS_EFFET=1. LIST_KO=1 makes every read fail, LIST_KO_APRES_STOP=1
# every read after a stop. Every call is appended to $UNITES_ETAT.calls.
FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
etat=$UNITES_ETAT
shift
cmd=$1; shift
echo "$cmd $*" >> "$etat.calls"
case $cmd in
  list-units)
    [ "${LIST_KO:-0}" = 1 ] && exit 1
    [ "${LIST_KO_APRES_STOP:-0}" = 1 ] && [ -e "$etat.stopped" ] && exit 1
    pat=${@: -1}
    while read -r u st pid; do
      etat_pid=$(ps -o stat= -p "$pid" 2>/dev/null) || continue
      [[ $u == $pat && $etat_pid != Z* ]] && echo "$u loaded $st running [systemd-run] poste.sh"
    done < "$etat" ;;
  stop)
    : > "$etat.stopped"
    if [ "${STOP_SANS_EFFET:-0}" != 1 ]; then
      while read -r u st pid; do kill "$pid" 2>/dev/null; done < "$etat"
    fi ;;
esac
exit 0
"""

# A staging run that hands the workstation restart to a detached writer (as
# systemd-run --pipe does, the "unit" keeps the staging stdout), then outlives
# its timeout.
SLOW_STAGING = """#!/usr/bin/env bash
setsid sleep "$DUREE_UNITE" &
echo "$!" >> "$UNITES_ETAT.pids"
echo "poste-maj-staging-$$-redemarrer.service active $!" > "$UNITES_ETAT"
echo "restart handed to poste-maj-staging-$$-redemarrer"
sleep 30
"""


@unittest.skipUnless(platform.is_linux(), "unités systemd et état /proc : Linux")
class StagingTimeoutUnitsTest(unittest.TestCase):
    """#3049: a timed-out staging never drops the worktree under a live unit."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ameesh-3049-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.worktree = self.root / "worktree"
        script = self.worktree / "deploy/mise-a-jour/staging.sh"
        script.parent.mkdir(parents=True)
        script.write_text(SLOW_STAGING)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
        os.chmod(bin_dir / "systemctl", 0o755)
        self.state = self.root / "units"
        self.state.write_text("")
        self.env = {"PATH": "%s:%s" % (bin_dir, os.environ.get("PATH", "")),
                    "UNITES_ETAT": str(self.state), "DUREE_UNITE": "30"}
        self.addCleanup(self.kill_units)

    def kill_units(self):
        pids = Path(str(self.state) + ".pids")
        for pid in (pids.read_text().split() if pids.exists() else ()):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run_timeout(self, grace=0.6, **env):
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=str(self.root / "repo"), target="main",
            staging="deploy/mise-a-jour/staging.sh", github="manaty/ameesh")
        at_drop = []
        started = time.monotonic()
        with mock.patch.dict(os.environ, {**self.env, **env}), \
             mock.patch.object(release_cutoff, "STAGING_UNIT_GRACE_S", grace), \
             mock.patch.object(release_cutoff, "STAGING_UNIT_POLL_S", 0.05), \
             mock.patch.object(release_cutoff, "STAGING_KILL_GRACE_S", 0.2), \
             mock.patch.object(release_cutoff, "STAGING_DRAIN_S", 0.3), \
             mock.patch.object(release_cutoff, "_worktree",
                               return_value=(str(self.root), str(self.worktree))), \
             mock.patch.object(release_cutoff, "_drop_worktree",
                               side_effect=lambda *a: at_drop.append(self.live())):
            with self.assertRaises(release_cutoff.TransientReleaseError) as caught:
                release_cutoff.run_staging(project, "a" * 40, [],
                                           state_dir=str(self.root), timeout_s=0.5)
        # bounded: neither the held pipe nor the 30 s unit is waited for
        self.assertLess(time.monotonic() - started, 10)
        calls = Path(str(self.state) + ".calls").read_text().splitlines()
        log = next((self.root / "release").glob("staging-*.log")).read_text()
        self.assertIn("restart handed to", log)
        return str(caught.exception), at_drop, calls

    def live(self):
        """pids of the fake units still alive"""
        pids = Path(str(self.state) + ".pids")
        alive = []
        for pid in pids.read_text().split():
            try:
                stat = Path("/proc/%s/stat" % pid).read_text()
            except OSError:
                continue
            if stat.rsplit(")", 1)[1].split()[0] != "Z":
                alive.append(pid)
        return alive

    def stops(self, calls):
        return [call for call in calls if call.startswith("stop")]

    def test_the_worktree_is_dropped_once_the_unit_ended(self):
        message, at_drop, calls = self.run_timeout(grace=5, DUREE_UNITE="2")
        self.assertIn("timed out", message)
        # seen alive while it held stdout, not after it ended
        self.assertIn("ended before the worktree was dropped", message)
        self.assertRegex(message, r"poste-maj-staging-\d+-redemarrer\.service")
        self.assertEqual(at_drop, [[]])
        self.assertEqual(self.stops(calls), [])

    def test_a_unit_past_its_grace_is_stopped_before_the_drop(self):
        message, at_drop, calls = self.run_timeout()
        self.assertIn("stopped: the workstation may be half restarted", message)
        self.assertEqual(at_drop, [[]])
        self.assertRegex(self.stops(calls)[0], r"^stop poste-maj-staging-\d+-\*$")

    def test_a_unit_that_survives_its_stop_keeps_the_worktree(self):
        message, at_drop, _ = self.run_timeout(STOP_SANS_EFFET="1")
        self.assertIn("still running", message)
        self.assertIn("worktree kept", message)
        self.assertEqual(at_drop, [])

    def test_an_unreadable_state_keeps_the_worktree(self):
        message, at_drop, calls = self.run_timeout(LIST_KO="1")
        self.assertIn("unreadable state", message)
        self.assertIn("worktree kept", message)
        self.assertEqual(at_drop, [])
        self.assertEqual(len(self.stops(calls)), 1, calls)

    def test_an_unprovable_stop_keeps_the_worktree(self):
        message, at_drop, _ = self.run_timeout(LIST_KO_APRES_STOP="1")
        self.assertNotIn("stopped:", message)
        self.assertIn("unreadable state", message)
        self.assertEqual(at_drop, [])

    def test_without_systemctl_there_is_no_unit(self):
        with mock.patch.object(release_cutoff.shutil, "which", return_value=None):
            self.assertEqual(release_cutoff._poste_units(1), [])
            self.assertEqual(release_cutoff._settle_poste_units(1, time.sleep), ("", True))

    def test_the_status_bullet_and_dead_units_are_not_live(self):
        out = ("● poste-maj-staging-7-retour.service loaded failed failed x\n"
               "poste-maj-staging-7-redemarrer.service loaded active running x\n"
               "poste-maj-staging-7-vieux.service loaded inactive dead x\n")
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(release_cutoff.shutil, "which", return_value="systemctl"), \
             mock.patch.object(release_cutoff, "_systemctl", return_value=done) as call:
            self.assertEqual(release_cutoff._poste_units(7),
                             ["poste-maj-staging-7-redemarrer.service"])
        self.assertEqual(call.call_args.args[-1], "poste-maj-staging-7-*")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="no bus")
        with mock.patch.object(release_cutoff.shutil, "which", return_value="systemctl"), \
             mock.patch.object(release_cutoff, "_systemctl", return_value=failed):
            self.assertIsNone(release_cutoff._poste_units(7))


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
