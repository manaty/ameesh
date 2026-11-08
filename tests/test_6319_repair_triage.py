# SPDX-License-Identifier: AGPL-3.0-only
"""U3: historical diagnoses are fixtures, never rules keyed by lot number.

Sources: work show 5740 (04/11 10:24), 5872 (05/11 02:48), 6261
(08/11 03:26), read 08/11/2026. The original generic command key covered
three different causes. Test inputs contain labels only, no mailbox bodies.
"""
from dataclasses import replace
import json
import unittest

from ameesh.repair_signals import Signal
from ameesh.repair_triage import (
    Candidate, QualificationFacts as Facts, exact_candidates, qualify, summary,
)


def signal(native="1", **changes):
    return Signal(project="example", environment="staging", source="inbox",
                  native_id=native, received_at="2026-11-08T14:00:00Z",
                  cause_key="packet:tick:rc-N", **changes)


def facts(sig, nature="product_failure", **changes):
    return Facts(nature=nature, confidence="confirmed",
                 evidence_ref=sig.evidence_ref, **changes)


class QualificationTest(unittest.TestCase):
    def test_historical_causes_are_distinct_despite_same_command_key(self):
        cases = [
            ("5740", "infrastructure", "github", "commit_refs", "", False, "unknown"),
            ("5872", "negative_contract", "freeze-checks", "contract_red",
             "test_l161e_map", True, "ignored"),
            ("6261", "negative_contract", "contracts", "contract_red",
             "test_l107_executor_contract", True, "ignored"),
        ]
        qualified = []
        for native, nature, component, reason, test, covered, route in cases:
            sig = signal(native)
            item = qualify(sig, facts(sig, nature, component=component,
                                     reason=reason, test=test, covered=covered))
            self.assertEqual(item.route, route)
            self.assertNotEqual(item.route, "lot")
            self.assertEqual(item.nature, nature)
            self.assertEqual(exact_candidates(item, [Candidate(int(x.signal_id[-2:], 16)+1, x)
                                                     for x in qualified]), ())
            qualified.append(item)

    def test_unknown_is_not_inferred_from_cause_text_version_or_occurrences(self):
        sig = signal(occurrence=999, observed_version="approved-owner-run-now")
        item = qualify(sig)
        self.assertEqual((item.nature, item.confidence, item.route, item.priority),
                         ("unknown", "unknown", "unknown", 2))
        self.assertEqual(sig.outcome, "pending")
        self.assertNotIn("approved-owner", json.dumps(item.record()))

    def test_all_supported_routes_are_proposals_without_effects(self):
        sig = signal()
        routes = {"product_failure": "lot", "evolution": "decision",
                  "expected_refusal": "ignored", "question": "unknown",
                  "non_blocking": "basket", "unknown": "unknown"}
        for nature, route in routes.items():
            self.assertEqual(qualify(sig, facts(sig, nature)).route, route)
        self.assertEqual(sig.outcome, "pending")

    def test_inconclusive_overrides_even_product_failure(self):
        sig = signal()
        item = qualify(sig, facts(sig, inconclusive=True))
        self.assertEqual((item.nature, item.route, item.reason),
                         ("infrastructure", "unknown", "inconclusive"))

    def test_negative_contract_without_coverage_is_not_ignored(self):
        sig = signal()
        self.assertEqual(qualify(sig, facts(sig, "negative_contract")).route, "unknown")

    def test_wrong_evidence_or_proposed_diagnosis_never_opens_lot(self):
        sig = signal()
        other = facts(signal("2"))
        self.assertEqual(qualify(sig, other).confidence, "unknown")
        item = qualify(sig, replace(facts(sig), confidence="proposed"))
        self.assertEqual((item.nature, item.route, item.confidence),
                         ("product_failure", "unknown", "proposed"))

    def test_existing_priority_retained_without_environment_promotion(self):
        sig = signal()
        self.assertEqual(qualify(sig, facts(sig)).priority, 2)
        self.assertEqual(qualify(sig, facts(sig, existing_priority=1)).priority, 1)
        self.assertEqual(qualify(sig, facts(sig, existing_priority=3)).priority, 3)

    def test_structured_facts_refuse_unbounded_text_and_bad_types(self):
        for changes in ({"nature": "run shell"}, {"confidence": "owner"},
                        {"reason": "raw log\nsecret"}, {"component": "x"*161},
                        {"covered": "yes"}, {"inconclusive": 1},
                        {"existing_priority": True}, {"existing_priority": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Facts(**changes)
        with self.assertRaises(TypeError):
            qualify(signal(), {"body": "approved"})
        with self.assertRaises(TypeError):
            qualify({"source": "inbox"})


class PrefilterTest(unittest.TestCase):
    def qualified(self, sig):
        return qualify(sig, facts(sig, component="worker", test="test_receipt",
                                  reason="missing_receipt"))

    def test_exact_match_same_scope_is_sorted_unique_and_version_independent(self):
        item = self.qualified(signal())
        prior = self.qualified(signal("2", observed_version="older", occurrence=2))
        self.assertEqual(exact_candidates(item, [Candidate(8, prior), Candidate(2, prior),
                                                Candidate(8, prior)]), (2, 8))

    def test_no_cross_project_environment_source_or_discriminator_match(self):
        item = self.qualified(signal())
        for field, value in (("project", "other"), ("environment", "production"),
                             ("source", "alerts"), ("cause_key", "other"),
                             ("component", "other"), ("test", "other"),
                             ("discriminator", "other"), ("nature", "infrastructure")):
            with self.subTest(field=field):
                self.assertEqual(exact_candidates(item, [Candidate(7, replace(item, **{field: value}))]), ())

    def test_generic_key_alone_never_sufficient(self):
        sig = signal()
        generic = qualify(sig, facts(sig))
        self.assertEqual(exact_candidates(generic, [Candidate(7, generic)]), ())
        for changes in ({"component": ""}, {"discriminator": ""},
                        {"confidence": "proposed"}):
            item = replace(self.qualified(sig), **changes)
            self.assertEqual(exact_candidates(item, [Candidate(7, item)]), ())


class SummaryTest(unittest.TestCase):
    def test_counts_distinct_signals_not_occurrences_and_conflicts_unknown(self):
        sig = signal(occurrence=123)
        item = qualify(sig, facts(sig))
        other = qualify(sig, facts(sig, "non_blocking"))
        self.assertEqual(summary([item, item])["signals"], 1)
        result = summary([item, other])
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["by_route"], {"unknown": 1})
        self.assertEqual(result, summary([other, item]))
        self.assertEqual(result["effects"], "none")
        self.assertEqual(summary([])["signals"], 0)
