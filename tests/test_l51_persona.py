# SPDX-License-Identifier: AGPL-3.0-only
"""L51 (0029, 0032, 0033): read `Persona` like `Agent`; human deputies and
superiors."""
from __future__ import annotations

import unittest
from dataclasses import replace

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
        shown = {a["title"]: a for a in loaded.to_dict()["agents"]}
        self.assertEqual(shown["checker"]["type"], "Persona")
        self.assertEqual(shown["checker"]["harnesses"], ["deepseek", "claude"])
        self.assertEqual(shown["checker"]["roles"], ["reviewer", "domain-expert"])
        self.assertEqual(shown["ouvrier"]["type"], "Agent")

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
        shown = {m["title"]: m for m in loaded.to_dict()["members"]}
        self.assertEqual(shown["alice"]["deputies"], ["human:bruno"])
        self.assertEqual(shown["alice"]["superiors"], ["human:bruno"])

    def test_unknown_deputy(self):
        _, findings = self.member(deputies=["human:nobody"])
        self.assertIn("member-deputies-unresolved", codes(findings, canon.ERROR))

    def test_superior_without_prefix(self):
        _, findings = self.member(superiors=["bruno"])
        self.assertIn("member-superiors-unresolved", codes(findings, canon.ERROR))

    def test_cannot_be_own_deputy(self):
        _, findings = self.member(deputies=["human:alice"])
        self.assertIn("member-deputies-self", codes(findings, canon.ERROR))

    def relationships(self, entries):
        for name, fields in entries.items():
            write(self.root, "membres/%s.md" % name, fiche(type="Member", title=name, **fields))
        loaded = canon.load(self.root, untrusted=True)
        return loaded, canon.validate(loaded)

    def test_reciprocal_relationships_are_cycles_in_each_graph(self):
        for key in ("deputies", "superiors"):
            with self.subTest(key=key):
                _, findings = self.relationships({
                    "alice": {key: ["human:bruno"]}, "bruno": {key: ["human:alice"]}})
                cycle = [f for f in findings if f.code == "member-%s-cycle" % key]
                self.assertEqual(len(cycle), 1)
                self.assertEqual(cycle[0].severity, canon.ERROR)
                self.assertIn("human:alice", cycle[0].message)
                self.assertIn("human:bruno", cycle[0].message)

    def test_longer_cycle_after_an_acyclic_prefix_is_detected(self):
        for key in ("deputies", "superiors"):
            with self.subTest(key=key):
                _, findings = self.relationships({
                    "alice": {key: ["human:bruno"]}, "bruno": {key: ["human:carlo"]},
                    "carlo": {key: ["human:dora"]}, "dora": {key: ["human:bruno"]}})
                self.assertIn("member-%s-cycle" % key, codes(findings, canon.ERROR))

    def test_shared_descendants_are_acyclic(self):
        for key in ("deputies", "superiors"):
            with self.subTest(key=key):
                _, findings = self.relationships({
                    "alice": {key: ["human:bruno", "human:carlo"]},
                    "bruno": {key: ["human:dora"]}, "carlo": {key: ["human:dora"]}, "dora": {}})
                self.assertEqual(codes(findings, canon.ERROR), set())

    def test_deputy_and_superior_edges_do_not_form_a_combined_graph(self):
        _, findings = self.relationships({
            "alice": {"deputies": ["human:bruno"]}, "bruno": {"superiors": ["human:alice"]}})
        self.assertEqual(codes(findings, canon.ERROR), set())

    def test_deep_member_chain_does_not_exhaust_python_recursion(self):
        loaded, _ = self.member()
        template = loaded.members[0]
        loaded.members = [replace(template, title="m%d" % i,
                                  deputies=["human:m%d" % (i + 1)] if i < 1100 else [])
                          for i in range(1101)]
        self.assertNotIn("member-deputies-cycle", codes(canon.validate(loaded), canon.ERROR))
        loaded.members[-1].deputies = ["human:m500"]
        self.assertIn("member-deputies-cycle", codes(canon.validate(loaded), canon.ERROR))


if __name__ == "__main__":
    unittest.main()
