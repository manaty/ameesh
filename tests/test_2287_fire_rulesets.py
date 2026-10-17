# SPDX-License-Identifier: AGPL-3.0-only
"""#2287 : le feu lit les rulesets de la branche cible avec sa protection.

Constat non bloquant de la relecture de #2271 (auditeur, 17/10) :
`fire._protection_required` ne lisait que `branches/<cible>/protection`. Un
check requis déclaré par un ruleset du dépôt ou de l'organisation
(`rules/branches/<cible>`, règle `required_status_checks`) échappait à la
porte du feu : la PR pouvait être tenue pour prête sans lui, et GitHub
refusait ensuite la fusion — ou un ruleset contourné par le jeton la laissait
passer. Le feu lit désormais les deux sources et en fait l'union ;
`integration_id` est l'application imposée, comme `checks[].app_id`.
"""
from __future__ import annotations

import unittest

from ameesh import fire, land, radar
from tests.test_2271_fire_required_checks import (FakeGh, SHA, _check, _project, _run,
                                                   _status)


def _rule(*checks, kind="required_status_checks"):
    """Une règle active telle que la rend `rules/branches/<cible>`."""
    return {"type": kind, "ruleset_source_type": "Repository",
            "ruleset_source": "manaty/ameesh", "ruleset_id": 7,
            "parameters": {"strict_required_status_checks_policy": False,
                           "required_status_checks": list(checks)}}


NO_PROTECTION = "gh: Branch not protected (HTTP 404)"


class RulesetTest(unittest.TestCase):
    def _required(self, gh, *, project=None, target="main"):
        return fire.required_checks("manaty/ameesh", target, project, gh=gh)

    def test_a_ruleset_check_is_required_without_classic_protection(self):
        # Témoin rouge : sans la lecture des rulesets, ce dépôt n'exigeait rien.
        gh = FakeGh(error=NO_PROTECTION,
                    rules=[_rule({"context": "tests", "integration_id": 15368})])
        self.assertEqual(self._required(gh), (("tests", 15368),))
        self.assertTrue(any("rules/branches/main" in arg for call in gh.calls
                            for arg in call))

    def test_a_ruleset_check_without_integration_imposes_no_app(self):
        gh = FakeGh(error=NO_PROTECTION, rules=[_rule({"context": "lint"})])
        self.assertEqual(self._required(gh), (("lint", None),))

    def test_rulesets_complete_the_classic_protection(self):
        gh = FakeGh(rules=[_rule({"context": "tests", "integration_id": 15368},
                                 {"context": "lint", "integration_id": 15368})])
        self.assertEqual(self._required(gh), (("tests", 15368), ("lint", 15368)))

    def test_other_rule_types_are_ignored(self):
        gh = FakeGh(error=NO_PROTECTION,
                    rules=[{"type": "deletion", "ruleset_id": 7},
                           {"type": "pull_request", "parameters": {
                               "required_approving_review_count": 1}}])
        self.assertEqual(self._required(gh), ())

    def test_a_ruleset_context_keeps_its_significant_spaces(self):
        gh = FakeGh(error=NO_PROTECTION, rules=[_rule({"context": "tests "})])
        self.assertEqual(self._required(gh), (("tests ", None),))

    def test_a_branch_name_with_a_slash_is_url_encoded(self):
        gh = FakeGh(error=NO_PROTECTION)
        self._required(gh, target="release/1.11.0")
        self.assertTrue(any("rules/branches/release%2F1.11.0" in arg
                            for call in gh.calls for arg in call))

    def test_unreadable_rulesets_fail_closed_without_a_declaration(self):
        gh = FakeGh(rules_error="gh: Not Found (HTTP 404)")
        with self.assertRaises(land.LandError):
            self._required(gh)

    def test_a_declaration_covers_unreadable_rulesets(self):
        gh = FakeGh(rules_error="gh: Resource not accessible by integration (HTTP 403)")
        project = _project(required_checks=("coverage",))
        self.assertEqual(self._required(gh, project=project), (("coverage", None),))

    def test_malformed_rules_are_refused(self):
        for bad in ([{"no": "type"}],
                    ["required_status_checks"],
                    [{"type": "required_status_checks"}],
                    [{"type": "required_status_checks", "parameters": {
                        "required_status_checks": "tests"}}],
                    [_rule({"integration_id": 15368})],
                    [_rule({"context": " "})],
                    [_rule({"context": "tests", "integration_id": "15368"})],
                    [_rule({"context": "tests", "integration_id": True})],
                    [_rule({"context": "tests", "integration_id": 0})]):
            with self.subTest(bad=bad), self.assertRaises(land.LandError):
                fire._ruleset_required("manaty/ameesh", "main", gh=FakeGh(rules=bad))

    def test_a_missing_reader_fails_closed(self):
        with self.assertRaises(land.LandError):
            fire._ruleset_required("manaty/ameesh", "main", gh=None)
        with self.assertRaises(land.LandError):
            fire._ruleset_required(None, "main", gh=FakeGh())


class ReadyPrRulesetTest(unittest.TestCase):
    def _ready(self, rollup, gh):
        return fire.ready_pr(_status(rollup), target="main", project=None,
                             frozen=SHA, slug="manaty/ameesh", gh=gh)

    def test_a_missing_ruleset_check_is_not_ready(self):
        gh = FakeGh(check_runs=[_run("tests")],
                    rules=[_rule({"context": "lint", "integration_id": 15368})])
        ok, why = self._ready([_check("tests")], gh)
        self.assertFalse(ok)
        self.assertIn("lint (absent)", why)

    def test_a_ruleset_check_from_another_app_is_not_ready(self):
        gh = FakeGh(error=NO_PROTECTION, check_runs=[_run("lint", app_id=99999)],
                    rules=[_rule({"context": "lint", "integration_id": 15368})])
        ok, why = self._ready([_check("lint")], gh)
        self.assertFalse(ok)
        self.assertIn("expected 15368", why)

    def test_all_required_checks_green_is_ready(self):
        gh = FakeGh(check_runs=[_run("tests"), _run("lint")],
                    rules=[_rule({"context": "lint", "integration_id": 15368})])
        ok, why = self._ready([_check("tests"), _check("lint")], gh)
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
