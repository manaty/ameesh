# SPDX-License-Identifier: AGPL-3.0-only
"""Content-free counters on the three harness streams, lot #7463."""
import json
import unittest

from ameesh import adapters, context_metrics, context_corpus
from .test_ctx05a_corpus import payload


class ContentCountsTest(unittest.TestCase):
    def normalize(self, harness, reader, stream, interrupted=False):
        normalizer = context_metrics.TurnNormalizer(harness, context_first_chars=42)
        for event in stream:
            normalizer.observe(reader.parse(json.dumps(event)))
        return normalizer.finish(interrupted=interrupted)

    def test_claude_big_dump_and_repeated_message(self):
        assistant = {"type": "assistant", "message": {
            "id": "m1", "usage": {"input_tokens": 10}, "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "PRIVATE"}}]}}
        result = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "x" * 200000},
                {"type": "image", "data": "PRIVATE"}]}]}}
        doc = self.normalize("claude", adapters.ClaudeStream(), [
            assistant, assistant, result, result,
            {"type": "result", "result": "hello"}])
        counts = doc["counts"]
        self.assertEqual(counts["tool_calls"], 1)
        self.assertEqual(counts["tool_output_chars"], 200000)
        self.assertEqual(counts["tool_output_max_chars"], 200000)
        self.assertEqual(counts["tool_output_chars_by_tool"], {"Bash": 200000})
        self.assertEqual(counts["assistant_chars"], 5)
        self.assertEqual(counts["steps"], 1)
        self.assertNotIn("PRIVATE", json.dumps(doc))
        self.assertLess(len(json.dumps(doc)), 10000)

    def test_claude_distinct_blocks_sharing_message_id(self):
        stream = [{"type": "assistant", "message": {"id": "m", "content": [
            {"type": "text", "text": text}]}} for text in ("hello", "world", "hello")]
        counts = self.normalize("claude", adapters.ClaudeStream(), stream)["counts"]
        self.assertEqual(counts["assistant_chars"], 10)

    def test_codex_completed_items(self):
        doc = self.normalize("codex", adapters.CodexStream(), [
            {"type": "item.completed", "item": {"id": "1", "type": "command_execution",
                                                  "aggregated_output": "abcd"}},
            {"type": "item.completed", "item": {"id": "2", "type": "mcp_tool_call",
                "tool": "lookup", "result": {"content": [{"type": "text", "text": "xyz"}]}}},
            {"type": "item.completed", "item": {"id": "3", "type": "agent_message", "text": "ok"}}])
        counts = doc["counts"]
        self.assertEqual(counts["steps"], 3)
        self.assertEqual(counts["tool_calls"], 2)
        self.assertEqual(counts["tool_output_chars"], 7)
        self.assertEqual(counts["tool_output_chars_by_tool"], {"command_execution": 4, "lookup": 3})
        self.assertEqual(counts["assistant_chars"], 2)
        self.assertIsNone(doc["series"]["steps"])

    def test_dsh_interrupted(self):
        doc = self.normalize("deepseek", adapters.DshStream(), [
            {"type": "tool_call", "callId": "t", "tool": "exec", "input": "PRIVATE"},
            {"type": "tool_result", "callId": "t", "result": "abc"},
            {"type": "text", "text": "ok"},
            {"type": "status", "phase": "step_end"}], interrupted=True)
        counts = doc["counts"]
        self.assertEqual(counts["tool_output_chars_by_tool"], {"exec": 3})
        self.assertEqual(counts["assistant_chars"], 2)
        self.assertEqual(counts["steps"], 1)
        self.assertIn("turn_interrupted", doc["reasons"])
        self.assertNotIn("PRIVATE", json.dumps(doc))

    def test_no_tool_output_and_initial_prompt(self):
        for harness, reader in [("claude", adapters.ClaudeStream()),
                                ("codex", adapters.CodexStream()),
                                ("deepseek", adapters.DshStream())]:
            counts = self.normalize(harness, reader, [])["counts"]
            self.assertEqual(counts["tool_calls"], 0)
            self.assertEqual(counts["tool_output_chars"], 0)
            self.assertEqual(counts["tool_output_max_chars"], 0)
            self.assertEqual(counts["tool_output_chars_by_tool"], {})
            self.assertEqual(counts["context_first_chars"], 42)

    def test_interrupted_before_tool_result(self):
        doc = self.normalize("deepseek", adapters.DshStream(), [
            {"type": "tool_call", "callId": "t", "tool": "exec"}], interrupted=True)
        self.assertEqual(doc["counts"]["tool_calls"], 1)
        self.assertEqual(doc["counts"]["tool_output_chars"], 0)

    def test_corpus_whitelist_and_legacy(self):
        body = payload()
        body["context"]["counts"] = {
            "tool_calls": 1, "assistant_chars": "PRIVATE", "prompt": "PRIVATE",
            "tool_output_chars_by_tool": {"Bash": 200000, "private value": 9, "exec": "PRIVATE"}}
        line = context_corpus.corpus_line(body)
        self.assertEqual(line["counts"]["tool_calls"], 1)
        self.assertIsNone(line["counts"]["assistant_chars"])
        self.assertEqual(line["counts"]["tool_output_chars_by_tool"], {"Bash": 200000})
        self.assertNotIn("PRIVATE", json.dumps(line))
        context_corpus.validate_line(line)
        line["counts"]["prompt"] = "PRIVATE"
        with self.assertRaises(context_corpus.CorpusError):
            context_corpus.validate_line(line)
        del body["context"]["counts"]
        self.assertIsNone(context_corpus.corpus_line(body)["counts"]["tool_calls"])
