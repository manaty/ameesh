# SPDX-License-Identifier: AGPL-3.0-only
"""CTX-03 (#4871) : fenêtre du modèle effectif et suivi du compactage natif.

Témoins du découpage #4865 (contrat ``ameesh-context/1`` §7.3) : ``modelUsage``
avec auxiliaire en premier, alias, bascule de modèle, ``post_tokens`` absent,
fenêtre ``null``. Observation seule : le chemin de décision legacy garde sa
valeur, et une inconnue reste ``null`` (jamais ``0`` ni une supposition).
"""
from __future__ import annotations

import json
import unittest

from ameesh import context_profile, resident, resident_runner

from tests.test_l75_resident import DriverCase, acp_doc, codex_doc

AUX = {"contextWindow": 200_000}
MAIN = {"contextWindow": 1_000_000}


class ModelUsageTest(unittest.TestCase):
    def test_auxiliary_first_never_gives_the_window(self):
        observed = context_profile.from_model_usage(
            {"claude-haiku-4-5": AUX, "claude-opus-5-5": MAIN},
            announced="claude-opus-5-5")
        self.assertEqual(observed.model_effective, "claude-opus-5-5")
        self.assertEqual(observed.model_quality, "measured")
        self.assertEqual(observed.window_tokens, 1_000_000)
        self.assertEqual(observed.window_quality, "measured")
        self.assertEqual(observed.window_source,
                         context_profile.SOURCE_MODEL_USAGE)
        self.assertEqual(observed.auxiliary_models, ("claude-haiku-4-5",))

    def test_no_announced_model_keeps_the_profile_unknown(self):
        # Une entrée unique peut être un auxiliaire : sans annonce du modèle
        # principal, le contrat interdit de la prendre pour le fil principal.
        observed = context_profile.from_model_usage(
            {"claude-haiku-4-5": AUX}, announced=None)
        self.assertIsNone(observed.model_effective)
        self.assertIsNone(observed.window_tokens)
        self.assertIn(context_profile.REASON_MODEL_UNKNOWN, observed.reasons)

        # Deux entrées sans annonce : le principal est ambigu, pas deviné.
        ambiguous = context_profile.from_model_usage(
            {"claude-haiku-4-5": AUX, "claude-opus-5-5": MAIN})
        self.assertIsNone(ambiguous.model_effective)
        self.assertIsNone(ambiguous.window_tokens)
        self.assertIn(context_profile.REASON_MODEL_AMBIGUOUS, ambiguous.reasons)

    def test_alias_is_resolved_only_by_a_versioned_table(self):
        table = context_profile.AliasTable(
            version=1, entries={"opus": ("claude-opus-5-5",)})
        observed = context_profile.from_model_usage(
            {"claude-haiku-4-5": AUX, "claude-opus-5-5": MAIN},
            announced="opus", aliases=table)
        self.assertEqual(observed.model_effective, "claude-opus-5-5")
        self.assertEqual(observed.model_quality, "derived")
        self.assertEqual(observed.window_tokens, 1_000_000)

    def test_unknown_or_ambiguous_alias_stays_null(self):
        usage = {"claude-opus-5-5": MAIN}
        unknown = context_profile.from_model_usage(usage, announced="opus")
        self.assertIsNone(unknown.model_effective)
        self.assertIsNone(unknown.window_tokens)
        self.assertIn(context_profile.REASON_ALIAS_UNKNOWN, unknown.reasons)

        ambiguous = context_profile.from_model_usage(
            {"a": MAIN, "b": MAIN}, announced="opus",
            aliases=context_profile.AliasTable(
                version=1, entries={"opus": ("a", "b")}))
        self.assertIsNone(ambiguous.model_effective)
        self.assertIsNone(ambiguous.window_tokens)
        self.assertIn(context_profile.REASON_ALIAS_AMBIGUOUS, ambiguous.reasons)

    def test_a_missing_window_is_null_never_zero(self):
        for entry in ({}, {"contextWindow": 0}, {"contextWindow": None},
                      {"contextWindow": "1000000"}):
            observed = context_profile.from_model_usage(
                {"m": entry}, announced="m")
            self.assertIsNone(observed.window_tokens, entry)
            self.assertEqual(observed.window_quality, "unknown", entry)
            self.assertIn(context_profile.REASON_WINDOW_UNKNOWN,
                          observed.reasons, entry)

    def test_announced_model_without_model_usage_keeps_the_window_unknown(self):
        observed = context_profile.from_model_usage(None, announced="m")
        self.assertEqual(observed.model_effective, "m")
        self.assertIsNone(observed.window_tokens)
        self.assertEqual(observed.window_quality, "unknown")


class AdvanceTest(unittest.TestCase):
    def test_same_model_without_metadata_keeps_the_measured_window(self):
        first = context_profile.from_model_usage(
            {"m": MAIN}, announced="m")
        profile = context_profile.advance(None, first)
        again = context_profile.from_model_usage(None, announced="m")
        profile = context_profile.advance(profile, again)
        self.assertEqual(profile.model_effective, "m")
        self.assertEqual(profile.window_tokens, 1_000_000)

    def test_model_switch_invalidates_the_previous_window(self):
        profile = context_profile.advance(
            None, context_profile.from_model_usage({"m": MAIN}, announced="m"))
        switched = context_profile.from_model_usage(None, announced="other")
        profile = context_profile.advance(profile, switched)
        self.assertEqual(profile.model_effective, "other")
        self.assertIsNone(profile.window_tokens)   # never the old model's
        self.assertEqual(profile.window_quality, "unknown")

    def test_new_model_with_a_new_window_uses_the_new_one(self):
        profile = context_profile.advance(
            None, context_profile.from_model_usage({"m": MAIN}, announced="m"))
        switched = context_profile.from_model_usage(
            {"other": {"contextWindow": 400_000}}, announced="other")
        profile = context_profile.advance(profile, switched)
        self.assertEqual(profile.model_effective, "other")
        self.assertEqual(profile.window_tokens, 400_000)


class CompactionTest(unittest.TestCase):
    def test_post_absent_keeps_the_before_and_marks_partial(self):
        got = context_profile.compaction(
            {"compact_metadata": {"trigger": "manual", "pre_tokens": 150_000}})
        self.assertEqual(got.pre_tokens, 150_000)
        self.assertIsNone(got.post_tokens)
        self.assertEqual(got.quality, "partial")
        self.assertEqual(got.context_tokens, 150_000)
        self.assertEqual(got.trigger, "manual")
        self.assertIn(context_profile.REASON_POST_UNKNOWN, got.reasons)

    def test_complete_compaction_is_measured(self):
        got = context_profile.compaction(
            {"compact_metadata": {"pre_tokens": 150_000, "post_tokens": 30_000}})
        self.assertEqual((got.pre_tokens, got.post_tokens), (150_000, 30_000))
        self.assertEqual(got.quality, "measured")
        self.assertEqual(got.context_tokens, 30_000)
        self.assertEqual(got.reasons, ())

    def test_empty_boundary_is_unknown(self):
        got = context_profile.compaction({})
        self.assertIsNone(got.pre_tokens)
        self.assertIsNone(got.post_tokens)
        self.assertIsNone(got.context_tokens)
        self.assertEqual(got.quality, "unknown")

    def test_root_tokens_are_read_too(self):
        got = context_profile.compaction({"preTokens": 12, "postTokens": 3})
        self.assertEqual((got.pre_tokens, got.post_tokens), (12, 3))


class ResidentObservationTest(DriverCase):
    """L'observation du driver, sans changement du chemin de décision legacy."""

    def test_auxiliary_first_is_observed_and_the_legacy_window_is_untouched(self):
        usage = json.dumps({"fake-aux": {"contextWindow": 200_000},
                            "fake-model": {"contextWindow": 1_000_000}})
        driver = self.driver(env={"FAKE_CLAUDE_MODEL_USAGE": usage})
        driver.start()
        result = self.wait(driver.send("hello", input_id="in-1"))
        self.assertEqual(result.profile["model_effective"], "fake-model")
        self.assertEqual(result.profile["window_tokens"], 1_000_000)
        self.assertEqual(result.profile["auxiliary_models"], ["fake-aux"])
        self.assertEqual(result.profile["window_source"],
                         context_profile.SOURCE_MODEL_USAGE)
        # CTX-03 observe : la valeur que lit encore la décision legacy ne
        # change pas (CTX-04 est le lot qui la basculera).
        self.assertEqual(result.usage["context_window"], 200_000)

    def test_codex_window_is_tied_to_the_announced_model(self):
        driver = self.driver(codex_doc(), model="gpt-x")
        driver.start()
        result = self.wait(driver.send("hello", input_id="in-1"))
        self.assertEqual(result.profile["model_effective"], "gpt-x")
        self.assertEqual(result.profile["window_tokens"], 258_400)
        self.assertEqual(result.profile["window_source"],
                         context_profile.SOURCE_CODEX)

    def test_acp_window_is_tied_to_the_announced_model(self):
        driver = self.driver(acp_doc(), model="modele-1")
        driver.start()
        result = self.wait(driver.send("hello", input_id="in-1"))
        self.assertEqual(result.profile["model_effective"], "modele-1")
        self.assertEqual(result.profile["window_tokens"], 200_000)
        self.assertEqual(result.profile["window_source"],
                         context_profile.SOURCE_ACP)

    def test_compaction_without_post_is_partial(self):
        driver = self.driver(env={"FAKE_CLAUDE_CONTEXT": "150000",
                                  "FAKE_CLAUDE_WINDOW": "1000000",
                                  "FAKE_COMPACT_NO_POST": "1"})
        driver.start()
        result = self.wait(driver.send("/compact", input_id="in-1"))
        self.assertEqual(result.compact["pre"], 150_000)
        self.assertIsNone(result.compact["post"])
        self.assertEqual(result.compact["quality"], "partial")
        self.assertIn(context_profile.REASON_POST_UNKNOWN,
                      result.compact["reasons"])
        self.assertEqual(result.usage["context_tokens"], 150_000)
        self.assertEqual(result.profile["window_tokens"], 1_000_000)


class UsageEventTest(unittest.TestCase):
    def test_profile_and_compact_are_published_next_to_usage(self):
        result = resident.TurnResult(
            status=resident.COMPLETED, session="s1",
            usage={"input_tokens": 3},
            profile={"window_tokens": 1_000_000, "model_effective": "m"},
            compact={"pre": 150_000, "post": None, "quality": "partial"})
        event = resident_runner.usage_event("acp", result, turn="t1")
        self.assertEqual(event["usage"], {"input_tokens": 3})
        self.assertEqual(event["profile"]["window_tokens"], 1_000_000)
        self.assertEqual(event["compact"]["quality"], "partial")


if __name__ == "__main__":
    unittest.main()
