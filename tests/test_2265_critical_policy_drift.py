# SPDX-License-Identifier: AGPL-3.0-only
"""#2265 : un classement léger ne dispense pas un chemin critique d'avis."""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import canon, lot_class, merge_queue, review


class ReviewPolicyFixture:
    HEAD = "a" * 40
    FAMILIES = {"author": "deepseek", "codex": "codex", "claude": "claude",
                "codex2": "codex", "same": "deepseek"}

    def classify(self, paths, *, default="light", rules=(), canons=None):
        if canons is None:
            canons = [types.SimpleNamespace(root="fixture", sources=[], federation={
                "review_policies": {"risk_classes": {"projects": {"ameesh": {
                    "default": default, "rules": list(rules)}}}}})]
        return lot_class.class_of_paths(canons, "ameesh", paths)

    def gate(self, klass, *actors):
        marks = [{"kind": "verdict", "verdict": "ok", "sha": self.HEAD,
                  "actor": actor, "at_ts": index, "id": index}
                 for index, actor in enumerate(actors, 1)]
        with mock.patch.object(merge_queue, "actor_family",
                               side_effect=lambda _db, name: self.FAMILIES.get(name, "")):
            return merge_queue.review_gate(None, marks, self.HEAD, klass=klass,
                                           item={"assignee": "author"})


class CriticalPolicyDriftTest(ReviewPolicyFixture, unittest.TestCase):
    def test_light_class_retains_paths_for_the_review_gate(self):
        klass = self.classify(["src/ameesh/merge_pr.py"])
        self.assertEqual(klass["class"], "light")
        self.assertFalse(klass["doubt"])
        self.assertEqual([entry["path"] for entry in klass["files"]],
                         ["src/ameesh/merge_pr.py"])

    def test_critical_path_needs_two_families_despite_light_or_normal_policy(self):
        for default in ("light", "normal"):
            for path in ("src/ameesh/merge_pr.py", "src/ameesh/lot_class.py",
                         "src/ameesh/review.py", "src/ameesh/fire.py",
                         "src/ameesh/review_lot.py", "src/ameesh/registry.py"):
                with self.subTest(default=default, path=path):
                    klass = self.classify([path], default=default)
                    self.assertEqual(klass["class"], default)
                    one = self.gate(klass, "codex")
                    self.assertEqual(one["required"], 2)
                    self.assertFalse(one["ok"])
                    self.assertFalse(self.gate(klass, "codex", "codex2")["ok"])
                    self.assertFalse(self.gate(klass, "codex", "same")["ok"])
                    self.assertFalse(self.gate(klass, "codex", "unknown")["ok"])
                    two = self.gate(klass, "codex", "claude")
                    self.assertTrue(two["ok"], two["why"])

    def test_light_critical_entry_in_sensitive_lot_still_counts(self):
        klass = self.classify(["src/ameesh/merge_pr.py", "src/ameesh/budget.py"], rules=[
            {"paths": ["src/ameesh/budget.py"], "class": "sensitive"}])
        self.assertEqual(klass["class"], "sensitive")
        one = self.gate(klass, "codex")
        self.assertEqual(one["required"], 2)
        self.assertFalse(one["ok"])

    def test_unchanged_critical_path_in_grouped_rule_does_not_count(self):
        klass = self.classify(["src/ameesh/budget.py"], rules=[{
            "paths": ["src/ameesh/budget.py", "src/ameesh/merge_pr.py"],
            "class": "sensitive"}])
        one = self.gate(klass, "codex")
        self.assertEqual(one["required"], 1)
        self.assertTrue(one["ok"], one["why"])

    def test_noncritical_paths_keep_their_review_counts(self):
        for default, path, expected in (("light", "docs/guide.md", 0),
                                        ("normal", "src/ameesh/preview.py", 1),
                                        ("sensitive", "src/ameesh/runner.py", 1)):
            with self.subTest(default=default):
                klass = self.classify([path], default=default)
                one = self.gate(klass, "codex")
                self.assertEqual(one["required"], expected)
                self.assertTrue(one["ok"], one["why"])

    def test_light_markdown_is_not_caught_by_fallback_patterns(self):
        # `docs/design/decisions/` matche le motif `decisions` de sécurité :
        # une documentation légère ne demande pas deux avis pour autant.
        for default in ("light", "normal"):
            for path in ("docs/design/decisions/0042-x.md",
                         "site/docs/guides/host-ameesh-approve.md"):
                with self.subTest(default=default, path=path):
                    klass = self.classify([path], default=default)
                    self.assertEqual(merge_queue.required_verdicts(klass),
                                     0 if default == "light" else 1)
        klass = self.classify(["docs/design/decisions/0042-x.md"], rules=[{
            "paths": ["docs/design/decisions/**"], "class": "sensitive",
            "critical": True}])
        self.assertEqual(merge_queue.required_verdicts(klass), 2)
        klass = self.classify(["scripts/ci-queue-wait.py"])
        self.assertEqual(merge_queue.required_verdicts(klass), 2)


class DeclaredCriticalScopeTest(ReviewPolicyFixture, unittest.TestCase):
    """#2263 : le canon déclare les nouveaux domaines sans regex parallèle."""

    PATH = "src/ameesh/new_verifier.py"

    def canon(self, rules, name="fixture"):
        return types.SimpleNamespace(root=name, sources=[], federation={
            "review_policies": {"risk_classes": {"projects": {"ameesh": {
                "default": "light", "rules": rules}}}}})

    def test_declared_critical_path_needs_two_even_without_regex(self):
        klass = self.classify([self.PATH], rules=[{
            "paths": [self.PATH], "class": "sensitive", "critical": True}])
        self.assertEqual(klass["class"], "sensitive")
        self.assertFalse(klass["doubt"])
        self.assertIs(klass["files"][0].get("critical"), True)
        with mock.patch.object(merge_queue, "CRITICAL_SCOPE", ()):
            one = self.gate(klass, "codex")
            self.assertEqual(one["required"], 2)
            self.assertFalse(one["ok"])
            two = self.gate(klass, "codex", "claude")
            self.assertTrue(two["ok"], two["why"])

    def test_critical_flag_is_for_the_matching_path_only(self):
        rules = [{"paths": [self.PATH], "class": "sensitive", "critical": True},
                 {"paths": ["src/ameesh/budget.py"], "class": "sensitive"}]
        klass = self.classify(["src/ameesh/budget.py"], rules=rules)
        self.assertEqual(merge_queue.required_verdicts(klass), 1)
        self.assertNotIn("critical", klass["files"][0])

    def test_matching_rule_order_does_not_lose_criticality(self):
        plain = {"paths": [self.PATH], "class": "sensitive"}
        critical = {"paths": ["src/ameesh/**"], "class": "sensitive", "critical": True}
        for rules in ([plain, critical], [critical, plain]):
            with self.subTest(rules=rules):
                klass = self.classify([self.PATH], rules=rules)
                self.assertIs(klass["files"][0].get("critical"), True)
                self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_all_canons_contribute_criticality_even_at_equal_class(self):
        plain = self.canon([{"paths": [self.PATH], "class": "sensitive"}], "plain")
        critical = self.canon([{"paths": [self.PATH], "class": "sensitive",
                                "critical": True}], "critical")
        for canons in ([plain, critical], [critical, plain]):
            with self.subTest(order=[c.root for c in canons]):
                klass = self.classify([self.PATH], canons=canons)
                self.assertIs(klass["files"][0].get("critical"), True)
                self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_invalid_critical_declaration_fails_closed(self):
        for value, cls in (("true", "sensitive"), (1, "sensitive"),
                           (None, "sensitive"), (True, "light"), (True, "normal")):
            with self.subTest(value=value, cls=cls):
                raw = {"default": "light", "rules": [{"paths": [self.PATH],
                       "class": cls, "critical": value}]}
                _policy, problems = review.parse(raw)
                self.assertTrue(any(level == "error" for _code, level, _why in problems))
                klass = self.classify([self.PATH], rules=raw["rules"])
                self.assertTrue(klass["doubt"])
                self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_critical_yaml_boolean_is_preserved_by_the_canon_readers(self):
        text = ("default: light\nrules:\n  - paths: ['src/ameesh/new_verifier.py']\n"
                "    class: sensitive\n    critical: true\n")
        readers = [False] + ([True] if canon._yaml is not None else [])
        for use_pyyaml in readers:
            with self.subTest(use_pyyaml=use_pyyaml):
                raw = canon.load_yaml(text, use_pyyaml=use_pyyaml)
                klass = self.classify([self.PATH], rules=raw["rules"])
                self.assertFalse(klass["doubt"])
                self.assertEqual(merge_queue.required_verdicts(klass), 2)


if __name__ == "__main__":
    unittest.main()
