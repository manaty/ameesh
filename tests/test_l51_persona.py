# SPDX-License-Identifier: AGPL-3.0-only
"""L51 (0029, 0032, 0033): read `Persona` like `Agent`; human deputies and
superiors."""
from __future__ import annotations

import unittest

from ameesh import canon

from .test_canon import _TmpMixin, codes, fiche, write


class PersonaTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load_canon(self):
        loaded = canon.load(self.root, untrusted=True)
        return loaded, canon.validate(loaded)

    def test_persona_read_as_agent(self):
        write(self.root, "agents/checker.md", fiche(
            type="Persona", title="checker", responsible="human:alice",
            capabilities=["read", "propose"], harnesses=["deepseek", "claude"],
            roles=["reviewer", "domain-expert"]))
        loaded, findings = self.load_canon()
        self.assertEqual(codes(findings, canon.ERROR), set())
        persona = loaded.agent("checker")
        self.assertTrue(persona.is_persona)
        self.assertEqual(persona.harness, "deepseek")          # first admitted harness
        self.assertEqual(persona.harnesses, ["deepseek", "claude"])
        self.assertEqual(persona.roles, ["reviewer", "domain-expert"])
        self.assertFalse(loaded.agent("ouvrier").is_persona)   # legacy Agent cards remain readable

    def test_unknown_harness_in_list(self):
        write(self.root, "agents/checker.md", fiche(
            type="Persona", title="checker", responsible="human:alice",
            harnesses=["deepseek", "missing"]))
        _, findings = self.load_canon()
        self.assertIn("agent-harness-unknown", codes(findings, canon.ERROR))

    def test_harness_outside_list(self):
        write(self.root, "agents/checker.md", fiche(
            type="Persona", title="checker", responsible="human:alice",
            harness="codex", harnesses=["deepseek", "claude"]))
        loaded, findings = self.load_canon()
        self.assertEqual(loaded.agent("checker").harness, "codex")
        self.assertIn("agent-harness-outside-list", codes(findings, canon.WARNING))

    def test_persona_and_agent_with_same_name(self):
        write(self.root, "agents/worker-persona.md", fiche(
            type="Persona", title="ouvrier", responsible="human:alice", harness="claude"))
        _, findings = self.load_canon()
        self.assertIn("agent-duplicate", codes(findings, canon.ERROR))

    def test_persona_cannot_approve(self):
        write(self.root, "agents/checker.md", fiche(
            type="Persona", title="checker", responsible="human:alice",
            harness="claude", capabilities=["read", "approve"]))
        _, findings = self.load_canon()
        self.assertIn("agent-approve-capability", codes(findings, canon.ERROR))


class DeputiesTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def member(self, **fields):
        write(self.root, "membres/alice.md", fiche(type="Member", title="alice", **fields))
        loaded = canon.load(self.root, untrusted=True)
        return loaded, canon.validate(loaded)

    def test_deputies_and_superiors_read(self):
        loaded, findings = self.member(deputies=["human:bruno"], superiors=["human:bruno"])
        self.assertEqual(codes(findings, canon.ERROR), set())
        alice = [m for m in loaded.members if m.title == "alice"][0]
        self.assertEqual(alice.deputies, ["human:bruno"])
        self.assertEqual(alice.superiors, ["human:bruno"])

    def test_unknown_deputy(self):
        _, findings = self.member(deputies=["human:nobody"])
        self.assertIn("member-deputies-unresolved", codes(findings, canon.ERROR))

    def test_superior_without_prefix(self):
        _, findings = self.member(superiors=["bruno"])
        self.assertIn("member-superiors-unresolved", codes(findings, canon.ERROR))

    def test_cannot_be_own_deputy(self):
        _, findings = self.member(deputies=["human:alice"])
        self.assertIn("member-deputies-self", codes(findings, canon.ERROR))


if __name__ == "__main__":
    unittest.main()
