# SPDX-License-Identifier: AGPL-3.0-only
"""Policy reads use immutable Git objects and the complete lot diff."""
import copy
import json
import os
import subprocess
import tempfile
import unittest

from ameesh import train, train_policy


class VersionedPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.canon = os.path.join(self.tmp.name, "canon")
        self.repo = os.path.join(self.tmp.name, "repo")
        for path in (self.canon, self.repo):
            os.mkdir(path)
            self.git(path, "init", "-q")
            self.git(path, "config", "user.name", "fixture")
            self.git(path, "config", "user.email", "fixture@example.invalid")
        self.document = dict(review_policies=dict(
            risk_classes=dict(default="light", projects=dict(example=dict(default="light", rules=[
                dict(paths=["db/**", "private/**", "queue/**", "missing/**"], **{"class": "sensitive"})]))),
            train_reviews=dict(schema="ameesh-train-reviews-policy/1", projects=dict(example=dict(rules=[
                dict(paths=["db/**", "private/**", "**/*.sql"], reviews=2),
                dict(paths=["queue/**"], reviews=1)])))))
        self.version = self.save_policy()
        self.base = self.change("readme.md", "base")
        self.item = dict(id=7, app="example", type="evolution", branch_target="main")
        self.row = dict(work_item_id=7, frozen_sha=self.base, target="main")

    def git(self, path, *args):
        return subprocess.check_output(["git", "-C", path, *args], text=True,
                                       stderr=subprocess.PIPE).strip()

    def write(self, root, path, content):
        destination = os.path.join(root, path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w") as fh:
            fh.write(content)

    def commit(self, root):
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "fixture")
        return self.git(root, "rev-parse", "HEAD")

    def save_policy(self):
        self.write(self.canon, "federation.yaml", json.dumps(self.document))
        return self.commit(self.canon)

    def change(self, path, content="content"):
        self.write(self.repo, path, content)
        return self.commit(self.repo)

    def provider(self, version=None, project="example"):
        return train_policy.Versioned(self.canon, version or self.version, project)

    def qualify(self, path, *, provider=None, content="content"):
        self.row["frozen_sha"] = self.change(path, content)
        return (provider or self.provider())(self.item, self.row, self.base, self.repo)

    def test_light_and_one_or_two_reviews_are_bound_to_exact_subject_and_policy(self):
        for path, risk, count in (("docs/readme.md", "light", 0), ("queue/lock.py", "sensitive", 1),
                                  ("private/keys.py", "sensitive", 2)):
            # Each subsequent diff contains the preceding changes as well.
            result = self.qualify(path)
            self.assertEqual((result.risk, result.reviews), (risk, count))
            self.assertEqual((result.lot, result.base, result.head, result.policy_commit),
                             (7, self.base, self.row["frozen_sha"], self.version))

    def test_sensitive_path_without_a_count_is_refused_even_beside_covered_path(self):
        self.change("private/key.py")
        with self.assertRaisesRegex(train.TrainError, "review count unknown.*missing/"):
            self.qualify("missing/secret.py")

    def test_positive_rule_can_escalate_a_path_missing_from_risk_classification(self):
        result = self.qualify("new/deep/schema.sql")
        self.assertEqual((result.risk, result.reviews), ("sensitive", 2))

    def test_maximum_overlapping_count_wins_independent_of_rule_order(self):
        rules = self.document["review_policies"]["train_reviews"]["projects"]["example"]["rules"]
        rules.append(dict(paths=["db/**"], reviews=1))
        version = self.save_policy()
        self.assertEqual(self.qualify("db/migrate.py", provider=self.provider(version)).reviews, 2)

    def test_dirty_checkout_and_new_policy_commit_do_not_change_pinned_rules(self):
        self.document["review_policies"]["train_reviews"]["projects"]["example"]["rules"] = []
        lowered = self.save_policy()
        self.write(self.canon, "federation.yaml", "broken: [")
        result = self.qualify("private/keys.py")
        self.assertEqual((result.reviews, result.policy_commit), (2, self.version))
        with self.assertRaisesRegex(train.TrainError, "review count unknown"):
            self.provider(lowered)(self.item, self.row, self.base, self.repo)

    def test_rename_and_deletion_keep_the_old_sensitive_path_in_scope(self):
        self.base = self.change("private/secret.py")
        self.git(self.repo, "mv", "private/secret.py", "public.py")
        self.row["frozen_sha"] = self.commit(self.repo)
        result = self.provider()(self.item, self.row, self.base, self.repo)
        self.assertEqual(result.reviews, 2)
        os.unlink(os.path.join(self.repo, "public.py"))
        self.row["frozen_sha"] = self.commit(self.repo)
        self.assertEqual(self.provider()(self.item, self.row, self.base, self.repo).reviews, 2)

    def test_newline_in_a_path_cannot_escape_two_review_scope(self):
        result = self.qualify("db/line\nbreak/migration.sql")
        self.assertEqual(result.reviews, 2)

    def test_path_normalization_cannot_change_identity_before_classification(self):
        with self.assertRaisesRegex(train.TrainError, "changing its identity"):
            self.qualify(" private/keys.py")

    def test_project_declaration_required_despite_global_fallback(self):
        with self.assertRaisesRegex(train.TrainError, "explicit versioned project"):
            self.provider(project="other")
        self.document["review_policies"].pop("train_reviews")
        with self.assertRaisesRegex(train.TrainError, "explicit versioned project"):
            self.provider(self.save_policy())

    def test_malformed_rules_never_supply_a_requirement(self):
        original = copy.deepcopy(self.document)
        for rule in (dict(paths=["**"], reviews=True), dict(paths=["**"], reviews=0),
                     dict(paths=[], reviews=2), dict(paths=["**"], reviews=3),
                     dict(paths=["**"], reviews=1, typo=2), dict(paths="**", reviews=2)):
            self.document = copy.deepcopy(original)
            self.document["review_policies"]["train_reviews"]["projects"]["example"]["rules"] = [rule]
            with self.subTest(rule=rule), self.assertRaises(train.TrainError):
                self.provider(self.save_policy())

    def test_incomplete_or_invalid_classification_and_unknown_schema_refuse(self):
        original = copy.deepcopy(self.document)
        for change in ("default", "class", "schema"):
            self.document = copy.deepcopy(original)
            policies = self.document["review_policies"]
            if change == "default":
                del policies["risk_classes"]["projects"]["example"]["default"]
            elif change == "class":
                policies["risk_classes"]["projects"]["example"]["default"] = "unknown"
            else:
                policies["train_reviews"]["schema"] = "future-schema"
            with self.subTest(change=change), self.assertRaises(train.TrainError):
                self.provider(self.save_policy())

    def test_full_commit_required_and_symlink_policy_refused(self):
        for ref in ("HEAD", self.version[:12], "0" * 40,
                    self.git(self.canon, "rev-parse", "HEAD:federation.yaml")):
            with self.subTest(ref=ref), self.assertRaises(train.TrainError):
                self.provider(ref)
        os.unlink(os.path.join(self.canon, "federation.yaml"))
        os.symlink("elsewhere.yaml", os.path.join(self.canon, "federation.yaml"))
        with self.assertRaisesRegex(train.TrainError, "regular versioned"):
            self.provider(self.commit(self.canon))

    def test_wrong_lot_project_and_release_refuse(self):
        self.row["frozen_sha"] = self.change("docs/readme.md")
        for item, row in ((dict(self.item, id=8), self.row),
                          (dict(self.item, app="other"), self.row),
                          (dict(self.item, type="release"), self.row),
                          (self.item, dict(self.row, target="origin/release/1.0"))):
            with self.subTest(item=item, row=row), self.assertRaises(train.TrainError):
                self.provider()(item, row, self.base, self.repo)

    def test_shallow_clone_and_unknown_or_abbreviated_base_refuse(self):
        self.row["frozen_sha"] = self.change("private/keys.py")
        for base in ("0" * 40, self.base[:12]):
            with self.subTest(base=base), self.assertRaises(train.TrainError):
                self.provider()(self.item, self.row, base, self.repo)
        clone = os.path.join(self.tmp.name, "shallow")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + self.repo, clone],
                       check=True, capture_output=True)
        with self.assertRaisesRegex(train.TrainError, "complete Git history"):
            self.provider()(self.item, self.row, self.base, clone)
