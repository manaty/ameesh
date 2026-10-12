# SPDX-License-Identifier: AGPL-3.0-only
"""L242c : un agent est critique par un rôle de sa fiche (manaty/home#25), pas
par son nom. Rôles critiques réglables par organisation (`critical_roles` de
federation.yaml ; défaut orchestrateur, conception, auditeur, relecture) ;
`critical_agents` reste la surcharge explicite ; dès qu'une fiche déclare des
rôles, plus de déduction « a confié des lots dans les 30 derniers jours »."""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import assignments, reprise, runner, sous_utilisation as su


class _Canon:
    """Canon minimal : possède les lignes du registre dont `canon` vaut
    `name` ; ses fiches portent les rôles donnés ; `federation` facultatif."""

    readable = True

    def __init__(self, name, cards, federation=None):
        self.name, self.federation = name, federation
        self.agents = [types.SimpleNamespace(
            title=agent, fiche=types.SimpleNamespace(data={"roles": roles}))
            for agent, roles in cards.items()]

    def owns(self, row_canon):
        return row_canon == self.name

    def agent(self, name):
        return next((a for a in self.agents if a.title == name), None)


CARTES = {"orchestrateur": ["orchestrateur"], "mesh-design": ["orchestrateur", "conception"],
          "auditeur": ["auditeur"], "codex1": ["implementation", "relecture"],
          "dev7": ["implementation"], "rev": ["reviewer"]}


def _rows(canon="A"):
    return {name: {"name": name, "canon": canon} for name in CARTES}


class RolesCritiquesTest(unittest.TestCase):
    def test_defaut_et_synonymes(self):
        roles = su.critical_roles(_Canon("A", {}))
        for role in ("orchestrateur", "orchestrator", "conception", "design",
                     "auditeur", "auditor", "relecture", "review", "reviewer"):
            self.assertIn(role, roles)
        self.assertNotIn("implementation", roles)

    def test_reglage_de_l_organisation(self):
        canon = _Canon("A", {}, federation={"critical_roles": ["Orchestrateur", "qa"]})
        self.assertEqual(su.critical_roles(canon), ("orchestrateur", "orchestrator", "qa"))
        self.assertEqual(su.critical_roles(_Canon("A", {}, {"critical_roles": []})), ())
        self.assertEqual(su.critical_roles(_Canon("A", {}, {"critical_roles": "qa"})), ("qa",))

    def test_roles_declares(self):
        self.assertFalse(su.roles_declared([_Canon("A", {"x": []})]))
        self.assertTrue(su.roles_declared([_Canon("A", {"x": []}), _Canon("B", {"y": ["qa"]})]))


class CritiqueTest(unittest.TestCase):
    def fake(self, canons, declared=()):
        return types.SimpleNamespace(
            cfg=types.SimpleNamespace(critical_agents=declared), db=None,
            _audit_canons=tuple(canons), _orchestrators_declared=frozenset())

    def critical(self, fake, name, rows):
        with mock.patch("ameesh.registry.get", lambda db, n: rows.get(n)):
            return runner.Runner.is_critical(fake, name)

    def test_par_role_au_defaut(self):
        fake, rows = self.fake([_Canon("A", CARTES)]), _rows()
        for name in ("orchestrateur", "mesh-design", "auditeur", "codex1", "rev"):
            self.assertTrue(self.critical(fake, name, rows), name)
        self.assertFalse(self.critical(fake, "dev7", rows))

    def test_par_role_de_l_organisation(self):
        canon = _Canon("A", CARTES, federation={"critical_roles": ["orchestrateur"]})
        fake, rows = self.fake([canon]), _rows()
        self.assertTrue(self.critical(fake, "mesh-design", rows))
        self.assertFalse(self.critical(fake, "codex1", rows))
        # la persona auditeur suit le réglage, elle aussi
        self.assertFalse(self.critical(fake, "auditeur", rows))

    def test_surcharge_explicite(self):
        fake, rows = self.fake([_Canon("A", CARTES)], declared=("dev7",)), _rows()
        self.assertTrue(self.critical(fake, "dev7", rows))

    def test_fiche_d_un_autre_canon_ignoree(self):
        # #324 r1 : seul le canon propriétaire de l'agent compte, avec ses
        # propres rôles critiques
        a = _Canon("A", {"dev7": ["implementation"]})
        b = _Canon("B", {"dev7": ["orchestrateur"]})
        rows = {"dev7": {"name": "dev7", "canon": "A"}}
        self.assertFalse(self.critical(self.fake([a, b]), "dev7", rows))

    def test_un_nom_hors_des_fiches_ne_lit_pas_le_registre(self):
        fake = self.fake([_Canon("A", CARTES)])
        fake.db = object()
        with mock.patch("ameesh.registry.get", side_effect=AssertionError):
            self.assertFalse(runner.Runner.is_critical(fake, "inconnu"))
            self.assertFalse(runner.Runner.is_critical(fake, "dev7", {"name": "dev7",
                                                                      "canon": "A"}))

    def test_persona_auditeur_sans_canon(self):
        self.assertTrue(self.critical(self.fake([]), "auditeur", {}))

    def test_reprise_juge_de_meme(self):
        canon = _Canon("A", CARTES)
        cfg = types.SimpleNamespace(critical_agents=(), canon="home")
        with mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(cfg, _rows()["codex1"]))
            self.assertFalse(reprise._critical(cfg, _rows()["dev7"]))
            cfg.critical_agents = ("dev7",)
            self.assertTrue(reprise._critical(cfg, _rows()["dev7"]))


class SansDeductionTest(unittest.TestCase):
    """Dès qu'une fiche déclare des rôles, avoir confié des lots ne fait plus
    d'un agent un orchestrateur."""

    def storage(self):
        ops = types.SimpleNamespace(assigners=lambda since_s: ["codex3", "agent:orch"])
        return mock.patch("ameesh.storage.of",
                          return_value=types.SimpleNamespace(operations=ops))

    def test_orchestrateurs(self):
        listing = [{"name": n} for n in ("codex3", "orch", "mesh-design")]
        sans = _Canon("A", {"mesh-design": []})
        avec = _Canon("A", {"mesh-design": ["orchestrateur"], "codex3": ["relecture"]})
        with self.storage():
            self.assertEqual(su.orchestrators(None, None, listing, canons=[sans]),
                             ["codex3", "orch"])
            self.assertEqual(su.orchestrators(None, None, listing, canons=[avec]),
                             ["mesh-design"])

    def test_is_orchestrator(self):
        avec = _Canon(None, {"mesh-design": ["orchestrateur"], "codex3": ["relecture"]})
        sans = _Canon(None, {"mesh-design": []})
        cfg = types.SimpleNamespace()
        for canon, attendu in ((sans, True), (avec, False)):
            with self.storage(), \
                    mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                    mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
                self.assertEqual(assignments.is_orchestrator(cfg, None, "codex3"), attendu)
                self.assertEqual(assignments.is_orchestrator(cfg, None, "mesh-design"),
                                 canon is avec)


if __name__ == "__main__":
    unittest.main()
