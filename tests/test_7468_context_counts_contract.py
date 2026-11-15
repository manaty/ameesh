# SPDX-License-Identifier: AGPL-3.0-only
"""#7468 — contrat des compteurs de contexte.

Deux défauts non bloquants de la relecture de #7463 :

* un harnais hors des trois conventions ne perd plus la taille de consigne
  mesurée par le runner (`context_first_chars`) en même temps que les tailles
  de flux, qu'il ne publie pas ;
* `codex app-server` alimente le normalisateur avec l'événement que son driver
  a déjà analysé (`item/completed`, `thread/tokenUsage/updated`) : les
  compteurs de ce harnais résident ne sont plus de faux zéros.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from ameesh import adapters, context_metrics, harnesses, resident

from .test_l75_resident import codex_doc


class OutsideTheThreeTest(unittest.TestCase):
    def test_prompt_size_is_kept_when_the_stream_sizes_are_unknown(self):
        counts = context_metrics.TurnNormalizer(
            "acp", context_first_chars=42).finish()["counts"]
        self.assertEqual(counts["context_first_chars"], 42)
        for key in ("tool_calls", "tool_output_chars", "tool_output_max_chars",
                    "assistant_chars", "steps"):
            self.assertIsNone(counts[key], key)
        self.assertIsNone(counts["tool_output_chars_by_tool"])

    def test_no_prompt_size_stays_null(self):
        counts = context_metrics.TurnNormalizer("acp").finish()["counts"]
        self.assertIsNone(counts["context_first_chars"])

    def test_a_known_harness_still_starts_at_zero(self):
        counts = context_metrics.TurnNormalizer(
            "claude", context_first_chars=7).finish()["counts"]
        self.assertEqual(counts["context_first_chars"], 7)
        self.assertEqual(counts["tool_calls"], 0)
        self.assertEqual(counts["tool_output_chars"], 0)
        self.assertEqual(counts["tool_output_chars_by_tool"], {})


def codex_driver() -> tuple[resident.CodexAppServer, list[tuple[dict, dict]]]:
    """Un driver `codex app-server` sans processus : il ne fait qu'analyser."""
    path = os.path.join(tempfile.mkdtemp(), "codex.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(codex_doc(), fh)
    descriptor = harnesses.load(path, source="test")
    adapter = adapters.HarnessAdapter(descriptor, sys.executable, resolve=False)
    events: list[tuple[dict, dict]] = []
    driver = resident.CodexAppServer(
        adapter, cwd=tempfile.mkdtemp(),
        on_event=lambda raw, parsed: events.append((raw, parsed)))
    driver.session_id = "thr-1"
    return driver, events


class CodexAppServerCountsTest(unittest.TestCase):
    def _driver(self):
        return codex_driver()

    def test_completed_agent_message_is_reduced_without_content(self):
        driver, events = self._driver()
        driver._on_notification("item/completed", {
            "threadId": "thr-1",
            "item": {"type": "agentMessage", "id": "m1", "text": "bonjour"}})
        parsed = events[-1][1]
        self.assertEqual(parsed["content_counts"]["step"], 1)
        self.assertEqual(parsed["content_counts"]["assistants"], [("m1", 7)])
        self.assertNotIn("bonjour", json.dumps(parsed["content_counts"]))

    def test_completed_command_output_is_counted(self):
        driver, events = self._driver()
        driver._on_notification("item/completed", {
            "threadId": "thr-1",
            "item": {"type": "commandExecution", "id": "c1",
                     "aggregatedOutput": "x" * 12}})
        counts = events[-1][1]["content_counts"]
        self.assertEqual(counts["step"], 1)
        self.assertEqual(counts["starts"], [("c1", "commandExecution")])
        self.assertEqual(counts["results"], [("c1", 12)])
        self.assertNotIn("x" * 12, json.dumps(counts))

    def test_a_compaction_item_is_not_a_content_step(self):
        driver, events = self._driver()
        driver._on_notification("item/completed", {
            "threadId": "thr-1",
            "item": {"type": "contextCompaction", "id": "k1"}})
        self.assertNotIn("content_counts", events[-1][1])

    def test_a_real_call_publishes_usage(self):
        driver, events = self._driver()
        driver._on_notification("thread/tokenUsage/updated", {
            "threadId": "thr-1",
            "tokenUsage": {
                "last": {"inputTokens": 300, "cachedInputTokens": 200,
                         "outputTokens": 9},
                "total": {"inputTokens": 300, "cachedInputTokens": 200,
                          "outputTokens": 9}}})
        parsed = events[-1][1]
        self.assertEqual(parsed["call_usage"], {
            "input_tokens": 300, "cached_input_tokens": 200,
            "output_tokens": 9})
        self.assertEqual(parsed["usage"], {
            "input_tokens": 300, "cached_input_tokens": 200,
            "output_tokens": 9})

    def test_a_total_only_update_is_not_published_as_a_call(self):
        # Un relevé `total` sans `last` ne doit pas être lu comme un cumul de
        # fin de tour par le normalisateur Codex.
        driver, events = self._driver()
        driver._on_notification("thread/tokenUsage/updated", {
            "threadId": "thr-1",
            "tokenUsage": {"total": {"inputTokens": 600, "cachedInputTokens": 0,
                                     "outputTokens": 18}}})
        parsed = events[-1][1]
        self.assertNotIn("usage", parsed)
        self.assertNotIn("call_usage", parsed)


class ObservationTest(unittest.TestCase):
    def test_the_driver_event_feeds_calls_steps_and_prompt_size(self):
        driver, events = codex_driver()
        driver._on_notification("item/completed", {
            "threadId": "thr-1",
            "item": {"type": "agentMessage", "id": "m1", "text": "bonjour"}})
        driver._on_notification("thread/tokenUsage/updated", {
            "threadId": "thr-1",
            "tokenUsage": {"last": {"inputTokens": 300, "cachedInputTokens": 200,
                                    "outputTokens": 9}}})
        normalizer = context_metrics.TurnNormalizer(
            "codex", context_first_chars=7)
        for _raw, parsed in events:
            normalizer.observe(parsed)
        document = normalizer.finish()
        counts = document["counts"]
        self.assertEqual(counts["calls"], 1)
        self.assertEqual(counts["steps"], 1)
        self.assertEqual(counts["assistant_chars"], 7)
        self.assertEqual(counts["context_first_chars"], 7)
        self.assertEqual(document["measures"]["C_last"]["value"], 300)
        self.assertEqual(document["measures"]["R_turn"]["value"], 300)
        self.assertEqual(document["series"]["steps"], 1)
