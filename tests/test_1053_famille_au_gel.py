# SPDX-License-Identifier: AGPL-3.0-only
"""#1053 (suite #1037 N1) : la famille de l'auteur d'un lot est celle du gel,
pas celle de la session courante à la fusion.

Le gel est le geste de livraison : son acteur est l'auteur du lot produit, et
les tours comptés de cet acteur (`turn_costs`, 0014) jusqu'à l'instant du gel
sont la trace du harnais qui a RÉELLEMENT tourné. Entre le gel et la fusion,
une session peut être remplacée ou changer de harnais (rotation, `adopt`,
L242b) : la porte §2 ne doit pas désigner un relecteur de la famille du travail
produit. Témoins :

* un changement de session entre le gel et la fusion ne change pas la famille
  du lot antérieur (témoin du lot) ;
* #1777 : le tour qui a posé le gel est celui dont la ligne `turn_costs` suit
  le gel (elle est écrite à la fin du tour), dans la fenêtre d'un tour ; la
  ligne d'une session suivante, plus tard, ne compte pas ;
* l'auteur est l'acteur du gel, même si le lot a été réassigné depuis ;
* #1777 : un gel reposé par un tiers garde l'acteur du premier gel, même si
  le tiers a des tours tracés ; sans tour tracé, on retombe sur l'assigné,
  producteur du lot ;
* sans tour tracé, la famille reste inconnue : le registre courant n'est jamais
  une preuve historique.
"""
from __future__ import annotations

import unittest

from ameesh import registry, review_lot, work

from .support import PgTestCase


class FamilleAuGelTest(PgTestCase):
    """La porte §2 relit les tours du gel, pas le registre à la fusion."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        registry.upsert(self.db, "a1", harness="deepseek", host=self.cfg.host)
        self.lot = work.add(self.db, title="un chemin léger", app="ameesh",
                            assignee="a1")["id"]
        self.sha = "a" * 40

    def turn(self, agent, harness, *, offset, session=None):
        """Une ligne du grand livre, à `offset` minutes de maintenant."""
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES (%s, %s, %s, now() + make_interval(mins => %s))",
            (agent, harness, session, int(offset)))

    def freeze(self, actor="a1", sha=None):
        work.milestone(self.db, self.lot, "frozen", sha=sha or self.sha, actor=actor)

    def switch_session(self, harness):
        """La session VIVANTE de a1 bascule (rotation, adopt) après le gel."""
        self.db.execute(
            "UPDATE agent_registry SET harness = %s, session_id = %s,"
            " session_origin = %s, session_harness = %s WHERE name = 'a1'",
            (harness, "session-" + harness, "session-" + harness, harness))

    def body(self):
        out = review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy of ameesh"},
            merged_sha="b" * 40, pr=476)
        self.assertTrue(out["created"], out)
        return work.get(self.db, out["id"])["body"]

    def test_la_bascule_de_session_entre_gel_et_fusion_ne_change_pas_la_famille(self):
        """Témoin du lot : la production est deepseek, et la seule ligne
        postérieure au gel est celle d'une session SUIVANTE (codex, bien
        après la fenêtre du tour du gel) — le relecteur désigné reste codex
        (l'autre famille que deepseek), pas claude (la famille de la nouvelle
        session)."""
        self.turn("a1", "deepseek", offset=-5, session="s-deepseek")
        self.freeze()
        # session suivante, au-delà de la fenêtre d'un tour (30 min)
        self.turn("a1", "codex", offset=45, session="s-codex")
        self.switch_session("codex")
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais deepseek)", body)
        self.assertIn("— codex (règle du pilote du 13/10)", body)
        self.assertNotIn("codex ou claude", body)

    def test_le_tour_du_gel_est_lu_apres_l_instant_du_gel(self):
        """#1777 N1 : la ligne d'un tour est écrite à sa FIN ; le tour qui a
        posé le gel (première session codex) est donc la première ligne APRÈS
        le gel, et c'est sa famille qui compte — pas celle du tour précédent."""
        self.turn("a1", "deepseek", offset=-5, session="s-deepseek")
        self.freeze()
        self.turn("a1", "codex", offset=2, session="s-codex")   # le tour du gel
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais codex)", body)
        self.assertIn("— claude (règle du pilote du 13/10)", body)

    def test_un_gel_par_un_tiers_sans_tour_retombe_sur_l_assigne(self):
        """#1777 N2 : le pilote repose le gel ; il n'a aucun tour tracé, donc
        sa famille n'est pas déduite — l'assigné porte la production."""
        self.turn("a1", "deepseek", offset=-5, session="s-deepseek")
        self.freeze(actor="mesh-design")
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais deepseek)", body)
        self.assertIn("— codex (règle du pilote du 13/10)", body)

    def test_un_gel_par_un_tiers_ne_lit_pas_le_tour_suivant_de_l_assigne(self):
        """#1777 N2 : le gel d'un tiers porte sur un travail déjà produit ;
        le tour que l'assigné ouvre APRÈS ce gel ne le lui attribue pas."""
        self.turn("a1", "deepseek", offset=-5, session="s-deepseek")
        self.freeze(actor="mesh-design")
        self.turn("a1", "codex", offset=2, session="s-codex")
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais deepseek)", body)

    def test_un_gel_repose_par_un_tiers_garde_l_acteur_du_premier_gel(self):
        """#1777 N2 : le pilote repose le gel sur une nouvelle tête ; même avec
        des tours tracés à lui, l'auteur reste l'acteur du premier gel."""
        self.turn("a1", "deepseek", offset=-5, session="s-deepseek")
        self.freeze()                                   # livraison par l'auteur
        self.turn("mesh-design", "claude", offset=-2, session="s-pilote")
        self.freeze(actor="mesh-design", sha="c" * 40)  # gel reposé, autre tête
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais deepseek)", body)
        self.assertIn("— codex (règle du pilote du 13/10)", body)
        self.assertNotIn("mesh-design", body)

    def test_l_auteur_est_l_acteur_du_gel_meme_reassigne_depuis(self):
        """La relecture réassigne le lot après le gel : la famille reste celle
        de l'acteur du gel, pas celle du nouvel assigné."""
        self.turn("a1", "deepseek", offset=-5)
        self.freeze()
        registry.upsert(self.db, "a2", harness="codex", host=self.cfg.host)
        work.assign(self.db, self.lot, "a2", actor="mesh-design")
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais deepseek)", body)
        self.assertIn("— codex (règle du pilote du 13/10)", body)

    def test_un_gel_sans_acteur_lit_les_tours_de_l_assigne(self):
        """Libellé sans acteur (gel ancien ou déclaré à la main) : l'assigné
        porte la trace ; le harnais reste celui des tours."""
        self.turn("a1", "claude", offset=-5)
        self.freeze(actor="")
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais claude)", body)
        self.assertIn("— codex (règle du pilote du 13/10)", body)

    def test_sans_tour_trace_la_famille_reste_inconnue(self):
        """Aucun tour avant le gel : le registre courant (codex) ne prouve
        rien, la famille n'est pas déduite de lui."""
        self.freeze()
        self.switch_session("codex")
        body = self.body()
        self.assertIn("harnais non tracé au gel", body)
        self.assertIn("codex ou claude", body)
        self.assertNotIn("— claude (règle du pilote du 13/10)", body)


if __name__ == "__main__":
    unittest.main()
