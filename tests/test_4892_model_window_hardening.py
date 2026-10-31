# SPDX-License-Identifier: AGPL-3.0-only
"""#4892 (panier #4871) : deux défauts non bloquants de la fenêtre observée.

N1 — un ``system/init`` de sous-agent (``parent_tool_use_id``) ne donne pas
le modèle effectif du fil principal : sinon ``_announced_model`` bascule sur
le modèle de l'auxiliaire et le ``result`` du fil principal résout la fenêtre
de cet auxiliaire (contrat ``ameesh-context/1`` §3.1).

N2 — une métadonnée non finie (``NaN``, ``Infinity``) ne lève jamais : la
fenêtre ou le jeton restent ``None`` avec une raison. Les valeurs viennent de
``json.loads``, qui accepte ``NaN``/``Infinity``.
"""
from __future__ import annotations

import json
import unittest

from ameesh import adapters, context_profile, resident

from tests.test_l75_resident import DriverCase

AUX = {"contextWindow": 200_000}
MAIN = {"contextWindow": 1_000_000}


class ClaudeInitModelTest(unittest.TestCase):
    """N1 au niveau du lecteur de flux : le modèle ne vient que du fil principal."""

    def setUp(self) -> None:
        self.reader = adapters.ClaudeStream()

    def test_a_subagent_init_carries_no_model(self):
        parsed = self.reader.parse(json.dumps({
            "type": "system", "subtype": "init", "session_id": "s-aux",
            "parent_tool_use_id": "toolu-1", "model": "fake-aux"}))
        self.assertNotIn("model", parsed)
        self.assertNotIn("session", parsed)

    def test_the_main_init_announces_the_model(self):
        parsed = self.reader.parse(json.dumps({
            "type": "system", "subtype": "init", "session_id": "s-main",
            "model": "fake-main"}))
        self.assertEqual(parsed["model"], "fake-main")
        self.assertEqual(parsed["session"], "s-main")

    def test_a_subagent_init_after_the_main_one_does_not_replace_it(self):
        self.reader.parse(json.dumps({
            "type": "system", "subtype": "init", "session_id": "s-main",
            "model": "fake-main"}))
        self.reader.parse(json.dumps({
            "type": "system", "subtype": "init", "session_id": "s-aux",
            "parent_tool_use_id": "toolu-1", "model": "fake-aux"}))
        parsed = self.reader.parse(json.dumps({
            "type": "result", "session_id": "s-main", "result": "done"}))
        self.assertEqual(parsed["session"], "s-main")
        self.assertNotIn("model", parsed)


class SubagentInitThenMainResultTest(DriverCase):
    """N1 de bout en bout : init de sous-agent, puis result du fil principal."""

    def test_the_window_is_the_main_model_one(self):
        usage = json.dumps({"fake-aux": AUX, "fake-model": MAIN})
        driver = self.driver(env={"FAKE_CLAUDE_MODEL_USAGE": usage,
                                  "FAKE_CLAUDE_SUBAGENT_MODEL": "fake-aux"})
        driver.start()
        result = self.wait(driver.send("hello", input_id="in-1"))
        self.assertEqual(result.profile["model_effective"], "fake-model")
        self.assertEqual(result.profile["window_tokens"], 1_000_000)
        self.assertEqual(result.profile["auxiliary_models"], ["fake-aux"])


class NonFiniteMetadataTest(unittest.TestCase):
    """N2 : NULL + raison, jamais d'exception sur une valeur non finie."""

    NON_FINITE = (float("nan"), float("inf"), float("-inf"))

    def test_a_non_finite_context_window_stays_unknown(self):
        for value in self.NON_FINITE:
            with self.subTest(value=value):
                observed = context_profile.from_model_usage(
                    {"m": {"contextWindow": value}}, announced="m")
                self.assertIsNone(observed.window_tokens)
                self.assertEqual(observed.window_quality, "unknown")
                self.assertIn(context_profile.REASON_WINDOW_UNKNOWN,
                              observed.reasons)
                self.assertIsNone(observed.to_dict()["window_tokens"])

    def test_a_non_finite_runtime_window_stays_unknown(self):
        for value in self.NON_FINITE:
            with self.subTest(value=value):
                observed = context_profile.from_runtime_window(value, model="m")
                self.assertIsNone(observed.window_tokens)
                self.assertEqual(observed.window_quality, "unknown")
                self.assertIn(context_profile.REASON_WINDOW_UNKNOWN,
                              observed.reasons)

    def test_a_non_finite_compaction_token_stays_unknown(self):
        before = context_profile.compaction(
            {"compact_metadata": {"pre_tokens": float("inf")}})
        self.assertIsNone(before.pre_tokens)
        self.assertIsNone(before.post_tokens)
        self.assertIsNone(before.context_tokens)
        self.assertIn(context_profile.REASON_PRE_UNKNOWN, before.reasons)

        after = context_profile.compaction(
            {"compact_metadata": {"pre_tokens": 10, "post_tokens": float("nan")}})
        self.assertEqual(after.pre_tokens, 10)
        self.assertIsNone(after.post_tokens)
        self.assertEqual(after.quality, "partial")
        self.assertEqual(after.context_tokens, 10)
        self.assertIn(context_profile.REASON_POST_UNKNOWN, after.reasons)

    def test_finite_values_keep_their_conversion(self):
        observed = context_profile.from_model_usage(
            {"m": {"contextWindow": 1_000_000.9}}, announced="m")
        self.assertEqual(observed.window_tokens, 1_000_000)
        compact = context_profile.compaction(
            {"compact_metadata": {"pre_tokens": 12.7, "post_tokens": -3.2}})
        self.assertEqual((compact.pre_tokens, compact.post_tokens), (12, 0))


class NonFiniteMetadataInTheDriverTest(DriverCase):
    """N2 de bout en bout : le lecteur du harnais ne meurt pas sur NaN."""

    def test_a_nan_context_window_does_not_kill_the_reader(self):
        usage = '{"fake-model": {"contextWindow": NaN}}'
        driver = self.driver(env={"FAKE_CLAUDE_MODEL_USAGE": usage})
        driver.start()
        result = self.wait(driver.send("hello", input_id="in-1"))
        self.assertEqual(result.status, resident.COMPLETED)
        self.assertEqual(result.profile["model_effective"], "fake-model")
        self.assertIsNone(result.profile["window_tokens"])
        self.assertIn(context_profile.REASON_WINDOW_UNKNOWN,
                      result.profile["reasons"])
        self.assertIsNone(result.usage.get("context_window"))


if __name__ == "__main__":
    unittest.main()
