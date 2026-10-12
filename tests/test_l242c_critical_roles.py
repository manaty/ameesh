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
    `name` ; ses fiches portent les rôles donnés ; `federation` facultatif.
    `parsed` : les fiches portent aussi `roles` analysé (comme `canon`)."""

    def __init__(self, name, cards, federation=None, readable=True, parsed=None):
        self.name, self.federation, self.readable = name, federation, readable
        self.agents = []
        for agent, roles in cards.items():
            card = types.SimpleNamespace(title=agent,
                                         fiche=types.SimpleNamespace(data={"roles": roles}))
            if parsed is not None:
                card.roles = parsed.get(agent)
            self.agents.append(card)

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
    def test_defaut(self):
        self.assertEqual(su.critical_roles(_Canon("A", {})),
                         ("orchestrateur", "conception", "auditeur", "relecture"))

    def test_reglage_de_l_organisation(self):
        canon = _Canon("A", {}, federation={"critical_roles": ["Orchestrateur", "qa"]})
        self.assertEqual(su.critical_roles(canon), ("orchestrateur", "qa"))
        self.assertEqual(su.critical_roles(_Canon("A", {}, {"critical_roles": ["design"]})),
                         ("conception",))
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
        listing = [{"name": n, "canon": "A"} for n in ("codex3", "orch", "mesh-design")]
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



class Relecture424Test(unittest.TestCase):
    """#424 r1 : la P1 et les P2."""

    def fake(self, canons, configured=True):
        cfg = types.SimpleNamespace(critical_agents=(), canon="home" if configured else "")
        return types.SimpleNamespace(cfg=cfg, db=None, _audit_canons=tuple(canons),
                                     _orchestrators_declared=frozenset(),
                                     _canon_complete=True)

    def critical(self, fake, name, rows):
        with mock.patch("ameesh.registry.get", lambda db, n: rows.get(n)), \
                mock.patch("ameesh.runner.log_async"):
            return runner.Runner.is_critical(fake, name)

    def test_canon_illisible_le_verdict_connu_tient(self):
        rows = _rows()
        fake = self.fake([_Canon("A", CARTES)])
        self.assertTrue(self.critical(fake, "codex1", rows))
        self.assertFalse(self.critical(fake, "dev7", rows))
        # le canon devient illisible, puis n'est plus chargé : rien ne baisse
        for canons in ([_Canon("A", {}, readable=False)], []):
            fake._audit_canons = tuple(canons)
            self.assertTrue(self.critical(fake, "codex1", rows))
            self.assertFalse(self.critical(fake, "dev7", rows))
        # sans verdict connu : les critiques par défaut
        neuf = self.fake([_Canon("A", {}, readable=False)])
        for name in ("orchestrateur", "mesh-design", "auditeur"):
            self.assertTrue(self.critical(neuf, name, rows), name)
        self.assertFalse(self.critical(neuf, "codex1", rows))

    def test_reprise_canon_illisible_repli(self):
        cfg = types.SimpleNamespace(critical_agents=(), canon="home")
        with mock.patch("ameesh.canon.load_configured", side_effect=OSError("git")), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(cfg, {"name": "mesh-design", "canon": "A"}))
            self.assertFalse(reprise._critical(cfg, {"name": "dev7", "canon": "A"}))

    def test_alerte_canon_illisible(self):
        from ameesh import canon as canon_mod, exploitation, notify
        cfg = types.SimpleNamespace(host="h1", canon="home")
        with mock.patch.object(canon_mod, "from_config",
                               side_effect=canon_mod.CanonError("ref introuvable")):
            alertes = exploitation._critical_roles_unreadable(cfg, 100.0)
        self.assertEqual([a["type"] for a in alertes], ["critical_roles_unreadable"])
        self.assertIn("ref introuvable", alertes[0]["detail"])
        self.assertIn("mesh-design", alertes[0]["detail"])
        self.assertIn("critical_roles_unreadable", exploitation.ALERT_TYPES)
        self.assertIn("critical_roles_unreadable", notify.TYPE_LABELS)
        ok = types.SimpleNamespace(readable=True, id="manaty")
        with mock.patch.object(canon_mod, "from_config", return_value=ok):
            self.assertEqual(exploitation._critical_roles_unreadable(cfg, 100.0), [])

    def test_deduction_par_organisation(self):
        a = _Canon("A", {"mesh-design": ["orchestrateur"]})
        b = _Canon("B", {"chef-b": []})
        listing = [{"name": "chef-a", "canon": "A"}, {"name": "chef-b", "canon": "B"}]
        ops = types.SimpleNamespace(assigners=lambda since_s: ["chef-a", "chef-b"])
        with mock.patch("ameesh.storage.of",
                        return_value=types.SimpleNamespace(operations=ops)):
            self.assertEqual(su.orchestrators(None, None, listing, canons=[a, b]),
                             ["chef-b"])

    def test_roles_analyses_pas_le_frontmatter_brut(self):
        # `roles: {relecture: false}` : le canon le refuse (agent-roles-invalid)
        canon = _Canon("A", {"x": {"relecture": False}, "y": 42}, parsed={"x": None})
        rows = {"x": {"name": "x", "canon": "A"}, "y": {"name": "y", "canon": "A"}}
        fake = self.fake([canon])
        self.assertFalse(self.critical(fake, "x", rows))
        self.assertFalse(self.critical(fake, "y", rows))   # jamais de TypeError
        self.assertFalse(su.roles_declared([canon]))

    def test_synonymes_dans_les_deux_sens(self):
        canon = _Canon("A", {"r": ["relecture"], "c": ["conception"], "o": ["orchestrator"]},
                       federation={"critical_roles": ["review", "design"]})
        rows = {n: {"name": n, "canon": "A"} for n in ("r", "c", "o")}
        fake = self.fake([canon])
        self.assertTrue(self.critical(fake, "r", rows))
        self.assertTrue(self.critical(fake, "c", rows))
        self.assertFalse(self.critical(fake, "o", rows))
        self.assertEqual(su.role_orchestrators([canon]), {"o"})

    def test_homonyme_etranger_jamais_orchestrateur(self):
        a = _Canon("A", {"dev": ["implementation"]})
        b = _Canon("B", {"dev": ["orchestrateur"]})
        listing = [{"name": "dev", "canon": "A"}]
        with mock.patch("ameesh.storage.of", side_effect=AssertionError):
            self.assertEqual(su.orchestrators(None, None, listing, canons=[a, b]), [])
        with mock.patch("ameesh.canon.load_configured", return_value=[a, b]), \
                mock.patch("ameesh.registry.get", return_value={"name": "dev", "canon": "A"}), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertFalse(assignments.is_orchestrator(types.SimpleNamespace(), object(),
                                                         "dev"))


class Relecture424r2Test(unittest.TestCase):
    """#424 r2 : canon lu en partie, mémoire des verdicts, clé de l'alerte."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def fake(self, canons):
        cfg = types.SimpleNamespace(critical_agents=(), canon="home", state_dir=self.tmp)
        return types.SimpleNamespace(cfg=cfg, db=None, _audit_canons=tuple(canons),
                                     _orchestrators_declared=frozenset(),
                                     _canon_complete=True)

    def critical(self, fake, name, rows):
        with mock.patch("ameesh.registry.get", lambda db, n: rows.get(n)), \
                mock.patch("ameesh.runner.log_async"):
            return runner.Runner.is_critical(fake, name)

    def test_canon_lu_en_partie_le_verdict_tient(self):
        from ameesh.canon import Finding
        rows = _rows()
        fake = self.fake([_Canon("A", CARTES)])
        self.assertTrue(self.critical(fake, "codex1", rows))
        # une erreur de lecture fait perdre la fiche de codex1, `readable` reste vrai
        partiel = _Canon("A", {k: v for k, v in CARTES.items() if k != "codex1"})
        partiel.load_findings = [Finding("canon-git-error", "error", "reading home: EIO")]
        fake._audit_canons = (partiel,)
        self.assertTrue(self.critical(fake, "codex1", rows))
        self.assertFalse(su.fully_read(partiel))

    def test_resume_relit_le_dernier_verdict(self):
        rows = _rows()
        self.assertTrue(self.critical(self.fake([_Canon("A", CARTES)]), "codex1", rows))
        cfg = types.SimpleNamespace(critical_agents=(), canon="home", state_dir=self.tmp)
        with mock.patch("ameesh.canon.load_configured", side_effect=OSError("git")), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(cfg, rows["codex1"]))
        # un exécuteur redémarré aussi, canon illisible
        neuf = self.fake([_Canon("A", {}, readable=False)])
        self.assertTrue(self.critical(neuf, "codex1", rows))

    def test_cle_d_alerte_stable_d_un_passage_au_suivant(self):
        from ameesh import canon as canon_mod, exploitation, notify
        cfg = types.SimpleNamespace(host="h1", canon="home")
        with mock.patch.object(canon_mod, "from_config",
                               side_effect=canon_mod.CanonError("ref introuvable")):
            un = exploitation._critical_roles_unreadable(cfg, 100.0)
            deux = exploitation._critical_roles_unreadable(cfg, 400.0)
        self.assertEqual(notify.key_of(un[0]), notify.key_of(deux[0]))


if __name__ == "__main__":
    unittest.main()
