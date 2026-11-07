# SPDX-License-Identifier: AGPL-3.0-only
"""Content and scope witnesses for #6184; no database or execution required."""
import copy
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ameesh import test_attestation as a


class AttestationTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 7, 14, 10, tzinfo=timezone.utc)
        self.payload = dict(
            schema=a.SCHEMA, source_sha="a" * 40, base_sha="b" * 40,
            tree="c" * 40, change_id="packet-1", lots=[dict(id="6184", sha="a" * 40)],
            scope=dict(plan={"schema": "test-plan/1", "modules": ["contract"]},
                       selection_digest="d" * 64, tests=["contract"],
                       canon_digest="e" * 64, registry_digest="f" * 64,
                       runner_digest="0" * 64, profile="guest", drivers={"db": "psycopg"},
                       database_schema="42", fixtures={"db": "1"}, exclusions={}),
            results={"contract": "passed"}, timestamp="2026-11-07T14:00:00Z",
            links=["artifact://run/1"], patch_id="index-only")

    def reuse(self, envelope=None, **kwargs):
        args = dict(candidate_sha="1" * 40, tree=self.payload["tree"],
                    scope=self.payload["scope"], now=self.now)
        args.update(kwargs)
        return a.reuse(envelope or a.seal(self.payload), **args)

    def test_same_tree_new_commit_retains_provenance(self):
        result = self.reuse()
        self.assertTrue(result.reusable)
        self.assertEqual((result.source_sha, result.candidate_sha), ("a" * 40, "1" * 40))

    def test_patch_index_cannot_authorize_other_tree(self):
        self.assertEqual(self.reuse(tree="2" * 40).causes, ("tree-changed",))

    def test_revert_preserves_ancestry_but_changes_tree(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                return subprocess.check_output(["git", "-C", root, *args], text=True).strip()
            git("init", "-q")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.invalid")
            file = Path(root) / "content"
            file.write_text("base")
            git("add", ".")
            git("commit", "-qm", "base")
            file.write_text("feature")
            git("commit", "-qam", "feature")
            source = git("rev-parse", "HEAD")
            self.payload.update(source_sha=source, tree=git("rev-parse", "HEAD^{tree}"))
            envelope = a.seal(self.payload)
            git("revert", "--no-edit", "HEAD")
            git("merge-base", "--is-ancestor", source, "HEAD")
            result = self.reuse(envelope, candidate_sha=git("rev-parse", "HEAD"),
                                tree=git("rev-parse", "HEAD^{tree}"))
            self.assertEqual(result.causes, ("tree-changed",))

    def test_each_scope_change_is_named(self):
        for field in a.SCOPE_FIELDS:
            with self.subTest(field=field):
                scope = copy.deepcopy(self.payload["scope"])
                value = scope[field]
                if isinstance(value, dict):
                    value["extra"] = "changed"
                elif isinstance(value, list):
                    value.append("extra")
                elif field.endswith("digest"):
                    scope[field] = "9" * 64
                else:
                    scope[field] += "-changed"
                self.assertIn(field.replace("_", "-") + "-changed",
                              self.reuse(scope=scope).causes)

    def test_altered_artifact_is_rejected(self):
        envelope = a.seal(self.payload)
        envelope["payload"]["results"]["contract"] = "failed"
        with self.assertRaisesRegex(a.AttestationError, "artifact-altered"):
            self.reuse(envelope)

    def test_all_nonpassing_results_never_green(self):
        for status in ("failed", "skipped", "inconclusive"):
            self.payload["results"]["contract"] = status
            self.assertEqual(self.reuse().causes, ("tests-not-green",))

    def test_excluded_modules_never_green(self):
        self.payload["scope"]["exclusions"] = {"host-test": "device absent"}
        self.assertEqual(self.reuse().causes, ("tests-excluded",))

    def test_missing_unexpected_or_unknown_results_refused(self):
        for results in ({}, {"extra": "passed"}, {"contract": "INCOMPLETE"}):
            self.payload["results"] = results
            with self.assertRaisesRegex(a.AttestationError, "invalid-results"):
                a.seal(self.payload)

    def test_policy_age_and_future(self):
        self.assertTrue(self.reuse(max_age_seconds=600).reusable)
        self.assertEqual(self.reuse(max_age_seconds=599).causes, ("expired",))
        self.payload["timestamp"] = "2026-11-07T15:00:00+01:00"
        self.assertTrue(self.reuse().reusable)
        self.payload["timestamp"] = "2026-11-07T15:00:00Z"
        self.assertEqual(self.reuse().causes, ("timestamp-in-future",))
        with self.assertRaisesRegex(a.AttestationError, "invalid-max-age"):
            self.reuse(max_age_seconds=float("nan"))

    def test_canonical_objects_order_arrays_and_detached_payload(self):
        self.assertEqual(a.digest({"b": 2, "a": 1}), a.digest({"a": 1, "b": 2}))
        self.assertNotEqual(a.digest([1, 2]), a.digest([2, 1]))
        envelope = a.seal(self.payload)
        decoded = a.validate(envelope)
        decoded["scope"]["profile"] = "changed"
        self.assertEqual(a.validate(envelope), self.payload)
        self.payload["links"].append("other")
        self.assertNotEqual(envelope["payload"], self.payload)

    def test_invalid_shapes_and_non_json_values(self):
        for field, value in (("schema", "future/2"), ("tree", "short"),
                             ("timestamp", "2026-11-07T14:00:00"),
                             ("lots", [{"id": "x", "sha": "a" * 40}] * 2)):
            payload = copy.deepcopy(self.payload)
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(a.AttestationError):
                a.seal(payload)
        for value in (float("nan"), {1: "key"}, ("tuple",)):
            with self.assertRaises(a.AttestationError):
                a.canonical_json(value)

    def test_isolated_surrogates_are_named_errors(self):
        for value in ("\ud800", "\udfff", {"\ud800": "value"},
                      {"nested": ["\udfff"]}):
            with self.subTest(value=ascii(value)):
                with self.assertRaisesRegex(a.AttestationError, "^invalid-unicode$"):
                    a.canonical_json(value)
        envelope = a.seal(self.payload)
        envelope["payload"]["links"] = ["\ud800"]
        with self.assertRaisesRegex(a.AttestationError, "^invalid-unicode$"):
            a.validate(envelope)
        self.assertEqual(a.canonical_json({"é": "😀"}), '{"é":"😀"}')

    def test_now_requires_timezone_aware_datetime(self):
        for now in (None, "2026-11-07T14:10:00Z", 42, object(),
                    datetime(2026, 11, 7, 14, 10)):
            with self.subTest(now=repr(now)):
                with self.assertRaisesRegex(a.AttestationError, "^invalid-now$"):
                    self.reuse(now=now)
