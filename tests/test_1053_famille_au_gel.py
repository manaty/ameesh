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
* un tour postérieur au gel ne compte pas ;
* l'auteur est l'acteur du gel, même si le lot a été réassigné depuis ;
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

    def turn(self, agent, harness, *, offset):
        """Une ligne du grand livre, à `offset` minutes de maintenant."""
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, recorded_at)"
            " VALUES (%s, %s, now() + make_interval(mins => %s))",
            (agent, harness, int(offset)))

    def freeze(self, actor="a1"):
        work.milestone(self.db, self.lot, "frozen", sha=self.sha, actor=actor)

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
        """Témoin du lot : la production est deepseek, la session courante
        devient codex après le gel — le relecteur désigné reste codex (l'autre
        famille que deepseek), pas claude (la famille de la nouvelle session)."""
        self.turn("a1", "deepseek", offset=-5)
        self.freeze()
        self.turn("a1", "codex", offset=5)      # postérieur au gel : ignoré
        self.switch_session("codex")
        body = self.body()
        self.assertIn("- Auteur : a1 (harnais deepseek)", body)
        self.assertIn("— codex (règle du pilote du 13/10)", body)
        self.assertNotIn("codex ou claude", body)

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
