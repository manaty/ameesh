# SPDX-License-Identifier: AGPL-3.0-only
"""E1a public CLI contract; real disposable Git, no database or IMA code."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import canon, canon_snapshot

ROOT = Path(__file__).resolve().parents[1]


class SnapshotContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="7669-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "home"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "AMEESH_", "AGENT_MESH_"))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="snapshot-test", GIT_COMMITTER_NAME="snapshot-test",
                        GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git("init", "-q", "-b", "main")
        self.write("federation.yaml", "id: public-test\nroot: home\nmembers:\n"
                   "  - id: home\n    ref: main\n    bundle: .\n    workspace_path: .\n"
                   "extensions:\n  ameesh:\n    scope: canon\n")
        self.write("canon/membres/alice.md", "---\ntype: Member\ntitle: alice\n"
                   "roles: [reviewer]\nauthenticators: []\n"
                   "directory: {login: alice, display_name: Alice}\n"
                   "private_token: MUST_NOT_EXPORT\n---\nPrivate body MUST_NOT_EXPORT\n")
        self.write("canon/agents/helper.md", "---\ntype: Agent\ntitle: helper\n"
                   "responsible: human:alice\nteam: acme\ncapabilities: [read]\n"
                   "harness: codex\nmodel: codex\nprovider: openai\n"
                   "credential_mode: api-key\ndirectory: {purpose: assistance}\n---\n")
        self.commit = self.commit_all()
        self.cfg = self.base / "config.json"
        self.cfg.write_text("{}")
        self.env.update(AMEESH_CONFIG=str(self.cfg), AMEESH_CANON=str(self.repo),
                        AMEESH_CANON_REF="origin/main", PYTHONPATH=str(ROOT / "src"))

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit_all(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "snapshot fixture")
        value = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", value)
        return value

    def cli(self, *args, expected=0, env=None):
        result = subprocess.run(
            [sys.executable, "-m", "ameesh", "canon", "snapshot", "--json",
             "--expect-commit", self.commit, *args], env=env or self.env,
            cwd=str(self.base), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def build(self, **kwargs):
        loaded = canon.load(str(self.repo), ref="origin/main")
        return canon_snapshot.build(loaded, expected_commit=self.commit, **kwargs)

    def codes(self, doc):
        self.assertFalse(doc["ok"])
        self.assertIsNone(doc["snapshot"])
        self.assertIsNone(doc["snapshot_id"])
        self.assertFalse(doc["freshness"]["refs_verified"])
        return {row["code"] for row in doc["refusals"]}

    def test_installed_style_cli_directory_pin_scope_and_no_database(self):
        env = dict(self.env, AMEESH_DSN="postgresql://invalid.invalid/unreachable")
        doc = self.cli("--expect-scope", "canon", "--single-source", env=env)
        self.assertEqual(doc["schema"], "ameesh-canon-snapshot/1")
        self.assertTrue(doc["ok"], doc)
        payload = doc["snapshot"]
        self.assertEqual(payload["root_commit"], self.commit)
        self.assertEqual(payload["sources"][0]["scope"], ["canon"])
        self.assertEqual(payload["sources"][0]["trust"], "git-pinned")
        self.assertEqual(payload["members"][0]["directory"]["login"], "alice")
        self.assertEqual(payload["agents"][0]["name"], "helper")
        self.assertEqual(payload["agents"][0]["directory"], {"purpose": "assistance"})
        self.assertTrue(payload["members"][0]["ref"].endswith("@" + self.commit))
        self.assertNotIn("MUST_NOT_EXPORT", json.dumps(doc))
        self.assertNotIn("authenticators", payload["members"][0])
        self.assertTrue(doc["freshness"]["refs_verified"])
        self.assertEqual(doc["freshness"]["scope"], "local-refs")
        self.assertIsNone(doc["freshness"]["remote_current"])

    def test_content_id_is_stable_and_matches_exported_json(self):
        first, second = self.cli(), self.cli()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        encoded = json.dumps(first["snapshot"], sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(first["snapshot_id"], "sha256:" + hashlib.sha256(encoded).hexdigest())
        self.write("canon/membres/bob.md", "---\ntype: Member\ntitle: bob\n"
                   "roles: []\nauthenticators: []\n---\n")
        self.commit = self.commit_all()
        newer = self.cli("--previous-commit", first["snapshot"]["root_commit"])
        self.assertNotEqual(first["snapshot_id"], newer["snapshot_id"])
        self.assertEqual(len(first["snapshot"]["members"]), 1)

    def test_stale_pin_does_not_export_directory(self):
        self.write("note.txt", "new source")
        self.commit_all()
        self.assertIn("expected-commit-mismatch", self.codes(self.cli(expected=1)))

    def test_rewind_and_unknown_previous_are_refused(self):
        old = self.commit
        self.write("note.txt", "new")
        newer = self.commit_all()
        self.git("reset", "--hard", old)
        self.git("update-ref", "refs/remotes/origin/main", old)
        self.assertIn("non-monotonic-source",
                      self.codes(self.cli("--previous-commit", newer, expected=1)))
        self.assertIn("non-monotonic-source",
                      self.codes(self.cli("--previous-commit", "a" * 40, expected=1)))

    def test_symbolic_pin_and_invalid_previous_are_refused(self):
        self.assertIn("invalid-expected-commit",
                      self.codes(self.cli("--expect-commit", "HEAD", expected=1)))
        self.assertIn("invalid-previous-commit",
                      self.codes(self.cli("--previous-commit", "main", expected=1)))

    def test_wrong_or_broadened_scope_is_refused(self):
        self.assertIn("expected-scope-mismatch",
                      self.codes(self.cli("--expect-scope", "other", expected=1)))
        self.write("federation.yaml", (self.repo / "federation.yaml").read_text().replace(
            "scope: canon", "scope: ."))
        self.commit = self.commit_all()
        self.assertIn("expected-scope-mismatch",
                      self.codes(self.cli("--expect-scope", "canon", expected=1)))

    def test_dirty_or_divergent_checkout_is_refused(self):
        self.write("canon/membres/alice.md", "dirty MUST_NOT_EXPORT")
        self.assertIn("source-worktree-dirty", self.codes(self.cli(expected=1)))
        self.commit_all()
        self.git("update-ref", "refs/remotes/origin/main", self.commit)
        self.assertIn("source-head-diverged", self.codes(self.cli(expected=1)))

    def test_invalid_canon_has_codes_without_private_messages(self):
        self.write("canon/agents/helper.md", "---\ntype: Agent\ntitle: helper\n"
                   "responsible: human:MUST_NOT_EXPORT\n---\n")
        self.commit = self.commit_all()
        doc = self.cli(expected=1)
        self.codes(doc)
        self.assertTrue(doc["findings"])
        self.assertNotIn("MUST_NOT_EXPORT", json.dumps(doc))
        self.assertTrue(all("message" not in f for f in doc["findings"]))

    def test_ref_moves_during_projection(self):
        loaded = canon.load(str(self.repo), ref="origin/main")
        validate = canon.validate

        def move(value):
            findings = validate(value)
            self.write("note.txt", "concurrent advance")
            self.commit_all()
            return findings

        with mock.patch.object(canon, "validate", side_effect=move):
            doc = canon_snapshot.build(loaded, expected_commit=self.commit)
        self.assertIn("source-moved", self.codes(doc))

    def test_dirty_tree_created_during_projection(self):
        loaded = canon.load(str(self.repo), ref="origin/main")
        validate = canon.validate

        def dirty(value):
            findings = validate(value)
            self.write("untracked.txt", "racing change")
            return findings

        with mock.patch.object(canon, "validate", side_effect=dirty):
            doc = canon_snapshot.build(loaded, expected_commit=self.commit)
        self.assertIn("source-worktree-dirty", self.codes(doc))

    def test_untrusted_source_is_never_exported(self):
        loaded = canon.load(str(self.repo), ref="origin/main")
        loaded.sources[0].mode = "untrusted"
        self.assertIn("untrusted-source", self.codes(
            canon_snapshot.build(loaded, expected_commit=self.commit)))

    def test_failed_fetch_does_not_authorize_cached_refs(self):
        self.git("remote", "add", "origin", str(self.base / "missing-remote.git"))
        doc = self.cli("--fetch", expected=1)
        self.assertIn("canon-fetch-failed", self.codes(doc))
        self.assertEqual(doc["freshness"]["scope"], "fetched-refs")

    def test_successful_explicit_fetch_keeps_observation_separate(self):
        remote = self.base / "remote.git"
        subprocess.run(["git", "clone", "--bare", str(self.repo), str(remote)],
                       env=self.env, capture_output=True, check=True, timeout=15)
        self.git("remote", "add", "origin", str(remote))
        doc = self.cli("--fetch")
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["freshness"]["scope"], "fetched-refs")
        self.assertIsNone(doc["freshness"]["remote_current"])

    def test_missing_canon_returns_a_machine_refusal(self):
        doc = self.cli("--canon", str(self.base / "missing"), expected=1)
        self.assertIn("canon-unreadable", self.codes(doc))
        self.assertNotIn(str(self.base), json.dumps(doc))

    def test_invalid_directory_metadata_fails_closed(self):
        loaded = canon.load(str(self.repo), ref="origin/main")
        for invalid in [{"bad": float("nan")}, {2: "numeric-key", 10: "other"},
                        {"nested": [{"bad": object()}]}]:
            with self.subTest(invalid=type(invalid)):
                loaded.members[0].fiche.data["directory"] = invalid
                doc = canon_snapshot.build(loaded, expected_commit=self.commit)
                self.assertIn("snapshot-data-invalid", self.codes(doc))

    def test_single_source_guard_and_every_federation_ref_are_checked(self):
        extra = self.base / "extra"
        extra.mkdir()
        for args in [("init", "-q", "-b", "main"),
                     ("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                      "commit", "--allow-empty", "-qm", "extra")]:
            subprocess.run(["git", "-C", str(extra), *args], env=self.env,
                           capture_output=True, check=True, timeout=15)
        commit = subprocess.check_output(["git", "-C", str(extra), "rev-parse", "HEAD"],
                                         env=self.env, text=True).strip()
        subprocess.run(["git", "-C", str(extra), "update-ref",
                        "refs/remotes/origin/main", commit], env=self.env, check=True)
        loaded = canon.load(str(self.repo), ref="origin/main")
        loaded.sources.append(canon.Source(member="extra", directory=str(extra),
                                           repo=str(extra), rev="origin/main", commit=commit))
        doc = canon_snapshot.build(loaded, expected_commit=self.commit, single_source=True)
        self.assertIn("unexpected-source-count", self.codes(doc))
        # A movement of a non-root source also invalidates the whole snapshot.
        subprocess.run(["git", "-C", str(extra), "update-ref", "-d",
                        "refs/remotes/origin/main"], env=self.env, check=True)
        doc = canon_snapshot.build(loaded, expected_commit=self.commit)
        self.assertIn("source-moved", self.codes(doc))
        self.assertIn({"code": "source-moved", "member": "extra"}, doc["refusals"])

    def test_repository_mismatch_warning_is_not_authority(self):
        loaded = canon.load(str(self.repo), ref="origin/main")
        loaded.load_findings.append(canon.Finding(
            code="member-repository-mismatch", severity=canon.WARNING,
            message="credential=MUST_NOT_EXPORT", member="home"))
        doc = canon_snapshot.build(loaded, expected_commit=self.commit)
        self.assertIn("member-repository-mismatch", self.codes(doc))
        self.assertNotIn("MUST_NOT_EXPORT", json.dumps(doc))


if __name__ == "__main__":
    unittest.main()
