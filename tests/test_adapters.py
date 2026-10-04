# SPDX-License-Identifier: AGPL-3.0-only
"""Adaptateurs : lignes de commande identiques à la v0, lecture des flux JSONL."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import adapters

from .support import FAKEBIN


class AdapterTest(unittest.TestCase):
    def test_binaires_resolus_par_ameesh_bin_dir(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            for harness in ("claude", "codex", "deepseek"):
                with self.subTest(harness=harness):
                    adapter = adapters.adapter_for(harness)
                    self.assertEqual(adapter.binary, os.path.join(FAKEBIN, adapters.DEFAULT_BIN[harness]))

    def test_binaire_manquant(self):
        with tempfile.TemporaryDirectory() as vide, \
                mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": vide, "PATH": vide}):
            os.environ.pop("AMEESH_CLAUDE_BIN", None)
            with self.assertRaises(adapters.HarnessMissing):
                adapters.adapter_for("claude")

    def test_harnais_inconnu(self):
        with self.assertRaises(adapters.HarnessMissing):
            adapters.adapter_for("gemini")

    def test_lignes_de_commande_claude(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("claude")
            self.assertEqual(
                adapter.command("fais X"),
                [adapter.binary, "-p", "--output-format", "stream-json", "--verbose",
                 "--dangerously-skip-permissions", "fais X"],
            )
            self.assertEqual(
                adapter.command("fais X", "sess-1"),
                [adapter.binary, "-p", "--output-format", "stream-json", "--verbose",
                 "--dangerously-skip-permissions", "--resume", "sess-1", "fais X"],
            )
            self.assertEqual(adapter.env(), {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"})

    def test_lignes_de_commande_codex(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("codex")
            self.assertEqual(
                adapter.command("fais X"),
                [adapter.binary, "--dangerously-bypass-hook-trust", "exec",
                 "--dangerously-bypass-approvals-and-sandbox", "--json", "fais X"],
            )
            self.assertEqual(
                adapter.command("fais X", "fil-1"),
                [adapter.binary, "--dangerously-bypass-hook-trust", "exec",
                 "--dangerously-bypass-approvals-and-sandbox", "--json",
                 "resume", "fil-1", "fais X"],
            )

    def test_lignes_de_commande_deepseek(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("deepseek")
            self.assertEqual(
                adapter.command("fais X"),
                [adapter.binary, "--profile", "agent", "--json", "fais X"],
            )
            self.assertEqual(
                adapter.command("fais X", "session-1"),
                [adapter.binary, "--profile", "agent", "--json", "--session-id",
                 "session-1", "fais X"],
            )
            self.assertEqual(adapter.env(), {"DSH_PERMISSION_MODE": "danger-full-access"})

    def test_lecture_flux_claude(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("claude")
        init = adapter.parse(json.dumps(
            {"type": "system", "subtype": "init", "session_id": "s1"}))
        self.assertEqual(init["session"], "s1")
        message = adapter.parse(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "un"}, {"type": "tool_use", "name": "x"},
            {"type": "text", "text": "deux"}]}}))
        self.assertEqual(message["display"], ["un", "deux"])
        result = adapter.parse(json.dumps(
            {"type": "result", "result": "fini", "total_cost_usd": 0.5}))
        self.assertEqual(result["cost"], 0.5)
        self.assertIn("fin du tour", result["display"][0])
        self.assertEqual(adapter.parse("pas du json"), {})

    def test_lecture_flux_codex(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("codex")
        self.assertEqual(
            adapter.parse(json.dumps({"type": "thread.started", "thread_id": "t1"}))["session"],
            "t1",
        )
        item = adapter.parse(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "coucou"}}))
        self.assertEqual(item["display"], ["coucou"])
        fin = adapter.parse(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}))
        self.assertEqual(fin["display"], ["== fin du tour"])
        self.assertEqual(fin["usage"], {"input_tokens": 3})
        erreur = adapter.parse(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}))
        self.assertEqual(erreur["error"], "boom")

    def test_lecture_flux_deepseek(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("deepseek")
        self.assertEqual(
            adapter.parse(json.dumps({"type": "session", "sessionId": "d1"}))["session"], "d1")
        self.assertEqual(
            adapter.parse(json.dumps({"type": "text", "text": "salut"}))["display"], ["salut"])
        final = adapter.parse(json.dumps({"type": "final", "text": "fini", "cost_usd": 0.01}))
        self.assertEqual(final["cost"], 0.01)
        self.assertIn("fin du tour", final["display"][0])

    def test_descripteurs_de_harnais(self):
        """Les lignes de commande viennent d'une table déclarative (base L16)."""
        self.assertEqual(set(adapters.SPECS), set(adapters.DEFAULT_BIN))
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            for harness in ("claude", "codex", "deepseek"):
                with self.subTest(harness=harness):
                    adapter = adapters.adapter_for(harness)
                    self.assertEqual(adapter.spec.key, harness)
                    self.assertEqual(adapter.spec.binary, adapters.DEFAULT_BIN[harness])
                    # sans session, aucun drapeau de session ne traîne
                    self.assertNotIn(adapter.spec.session_flag[0],
                                     adapter.interactive_command())
                    # la session se place avant le texte
                    argv = adapter.command("fais X", "s-1")
                    self.assertEqual(argv[-1], "fais X")
                    self.assertEqual(argv[-2], "s-1")

            claude = adapters.adapter_for("claude")
            self.assertEqual(claude.interactive_command("s1"),
                             [claude.binary, "--resume", "s1"])
            codex = adapters.adapter_for("codex")
            self.assertEqual(codex.interactive_command("t1"), [codex.binary, "resume", "t1"])
            dsh = adapters.adapter_for("deepseek")
            self.assertEqual(dsh.interactive_command("d1"),
                             [dsh.binary, "--profile", "agent", "--session-id", "d1"])


if __name__ == "__main__":
    unittest.main()
