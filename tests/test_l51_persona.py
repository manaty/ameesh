# SPDX-License-Identifier: AGPL-3.0-only
"""L51 (0029, 0032, 0033) : fiche `Persona`, lue comme `Agent` ; suppléants et
supérieurs des humains."""
from __future__ import annotations

import unittest

from ameesh import canon

from .test_canon import _TmpMixin, codes, fiche, write


class PersonaTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def charger(self):
        loaded = canon.load(self.root, untrusted=True)
        return loaded, canon.validate(loaded)

    def test_persona_lue_comme_un_agent(self):
        write(self.root, "agents/verificateur.md", fiche(
            type="Persona", title="verificateur", responsible="human:alice",
            capabilities=["read", "propose"], harnesses=["deepseek", "claude"],
            roles=["relecteur", "referent"]))
        loaded, findings = self.charger()
        self.assertEqual(codes(findings, canon.ERROR), set())
        persona = loaded.agent("verificateur")
        self.assertTrue(persona.is_persona)
        self.assertEqual(persona.harness, "deepseek")          # le premier admis
        self.assertEqual(persona.harnesses, ["deepseek", "claude"])
        self.assertEqual(persona.roles, ["relecteur", "referent"])
        self.assertFalse(loaded.agent("ouvrier").is_persona)   # les fiches Agent restent lues

    def test_harnais_inconnu_dans_la_liste(self):
        write(self.root, "agents/verificateur.md", fiche(
            type="Persona", title="verificateur", responsible="human:alice",
            harnesses=["deepseek", "inexistant"]))
        _, findings = self.charger()
        self.assertIn("agent-harness-unknown", codes(findings, canon.ERROR))

    def test_harness_hors_de_la_liste(self):
        write(self.root, "agents/verificateur.md", fiche(
            type="Persona", title="verificateur", responsible="human:alice",
            harness="codex", harnesses=["deepseek", "claude"]))
        loaded, findings = self.charger()
        self.assertEqual(loaded.agent("verificateur").harness, "codex")
        self.assertIn("agent-harness-outside-list", codes(findings, canon.WARNING))

    def test_persona_et_agent_du_meme_nom(self):
        write(self.root, "agents/ouvrier-persona.md", fiche(
            type="Persona", title="ouvrier", responsible="human:alice", harness="claude"))
        _, findings = self.charger()
        self.assertIn("agent-duplicate", codes(findings, canon.ERROR))

    def test_persona_ne_peut_pas_approuver(self):
        write(self.root, "agents/verificateur.md", fiche(
            type="Persona", title="verificateur", responsible="human:alice",
            harness="claude", capabilities=["read", "approve"]))
        _, findings = self.charger()
        self.assertIn("agent-approve-capability", codes(findings, canon.ERROR))


class SuppleantsTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def membre(self, **champs):
        write(self.root, "membres/alice.md", fiche(type="Member", title="alice", **champs))
        loaded = canon.load(self.root, untrusted=True)
        return loaded, canon.validate(loaded)

    def test_suppleants_et_superieurs_lus(self):
        loaded, findings = self.membre(deputies=["human:bruno"], superiors=["human:bruno"])
        self.assertEqual(codes(findings, canon.ERROR), set())
        alice = [m for m in loaded.members if m.title == "alice"][0]
        self.assertEqual(alice.deputies, ["human:bruno"])
        self.assertEqual(alice.superiors, ["human:bruno"])

    def test_suppleant_inconnu(self):
        _, findings = self.membre(deputies=["human:personne"])
        self.assertIn("member-deputies-unresolved", codes(findings, canon.ERROR))

    def test_superieur_sans_prefixe(self):
        _, findings = self.membre(superiors=["bruno"])
        self.assertIn("member-superiors-unresolved", codes(findings, canon.ERROR))

    def test_pas_son_propre_suppleant(self):
        _, findings = self.membre(deputies=["human:alice"])
        self.assertIn("member-deputies-self", codes(findings, canon.ERROR))


if __name__ == "__main__":
    unittest.main()
