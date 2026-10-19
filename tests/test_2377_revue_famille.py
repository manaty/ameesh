# SPDX-License-Identifier: AGPL-3.0-only
"""#2377 — un lot de relecture après fusion n'est pris que par l'autre famille.

Constat (#2372) : le lot REVUE #2340, dont l'auteur était un agent DeepSeek,
a été pris au repos par un autre agent DeepSeek. La famille écrite au corps
n'était qu'une consigne ; `backlog.auto_take` ne regardait que les capacités
et la classe déclarée.

La prise relit la même preuve que l'ouverture du lot (`review_lot`) : l'auteur
au gel et les tours tracés (`turn_costs`), jamais le registre courant. Trois
cas : même famille refusée, autre famille prise, famille d'auteur inconnue
refusée (doute -> non pris, #1374).
"""
from __future__ import annotations

import time
import unittest

from ameesh import backlog, registry, review_lot, work

from .support import PgTestCase

HEURE = 3600.0


class RevueFamilleTest(PgTestCase):
    """La garde branchée sur la prise automatique, sur une base migrée."""

    def setUp(self):
        super().setUp()
        self.now = time.time()

    def agent(self, name, harness, *, team="ameesh"):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', responsible = %s,"
            " team = %s WHERE name = %s",
            ("runner-%s@h" % name, "human:proprio", team, name))
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(secs => %s),"
            " last_turn_at = now() - make_interval(secs => %s) WHERE name = %s",
            (2 * HEURE, 2 * HEURE, name))

    def produced(self, actor, harness=""):
        """Un lot d'origine gelé par `actor` ; `harness` trace son tour.

        `harness` vide : le gel existe mais aucun tour n'est tracé — la famille
        de l'auteur reste inconnue (#1053)."""
        registry.upsert(self.db, actor, harness=harness or "other", host=self.cfg.host)
        lot = work.add(self.db, title="lot produit", app="ameesh", assignee=actor)
        if harness:
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, recorded_at)"
                " VALUES (%s, %s, now() - interval '1 minute')", (actor, harness))
        work.milestone(self.db, lot["id"], "frozen", sha="a" * 40, actor=actor)
        return lot["id"]

    def revue(self, lot):
        """Ouvre le lot de relecture du lot `lot` et le rend apte à la prise."""
        out = review_lot.open_after_merge(
            self.db, lot=lot, klass={"class": "light", "why": "policy of ameesh"},
            merged_sha="b" * 40)
        self.assertTrue(out["created"], out)
        self.db.execute(
            "UPDATE work_items SET created_at = created_at - make_interval(secs => 600)"
            " WHERE id = %s", (out["id"],))
        return out["id"]

    def take(self, **kw):
        kw.setdefault("now", self.now)
        kw.setdefault("paid", ("deepseek",))
        kw.setdefault("budget_check", lambda agent: "")
        kw.setdefault("orchestras", [])
        return backlog.auto_take(self.cfg, self.db, **kw)

    def test_meme_famille_refusee_puis_autre_famille_prend(self):
        lot = self.produced("auteur-ds", "deepseek")     # relecture = codex
        revue = self.revue(lot)
        self.agent("ds1", "deepseek")
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, revue)["assignee"])
        self.agent("cx1", "codex")
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in self.take()],
                         [("cx1", revue)])

    def test_autre_famille_prise_et_meme_famille_refusee_dans_l_autre_sens(self):
        lot = self.produced("auteur-cx", "codex")        # relecture = claude
        revue = self.revue(lot)
        self.agent("cx1", "codex")
        self.assertEqual(self.take(), [])
        self.agent("cl1", "claude")
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in self.take()],
                         [("cl1", revue)])

    def test_famille_d_auteur_inconnue_refusee(self):
        lot = self.produced("auteur-sans-tour")          # gel sans tour tracé
        revue = self.revue(lot)
        self.agent("cx1", "codex")
        self.agent("cl1", "claude")
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, revue)["assignee"])

    def test_hors_lot_de_relecture_aucune_garde(self):
        """La garde ne mord que sur `issue_ref = review-after-merge:#…`."""
        item = {"lot_team": "ameesh"}
        self.assertTrue(review_lot.may_take(self.db, item,
                                            {"name": "ds1", "harness": "deepseek"}))
        self.assertIsNone(review_lot.required_family(self.db, item))
        # une référence de relecture introuvable/illisible ne se devine pas
        self.assertEqual(review_lot.required_family(
            self.db, {"issue_ref": "review-after-merge:#99999999"}), "")
        self.assertFalse(review_lot.may_take(
            self.db, {"issue_ref": "review-after-merge:#99999999"},
            {"name": "ds1", "harness": "deepseek"}))


class FamilleDuPreneurTest(unittest.TestCase):
    """La session vivante fait foi (#1037, L242b), pas l'étiquette déclarée."""

    def test_famille_de_la_session_vivante(self):
        from unittest import mock

        with mock.patch.object(review_lot, "_required_family", return_value="codex"), \
                mock.patch.object(review_lot, "compiled_families",
                                  return_value=("claude", "codex", "deepseek")):
            # déclaré deepseek mais tourne sous codex : famille codex, accepté
            self.assertTrue(review_lot.may_take(
                None, {"issue_ref": "review-after-merge:#1"},
                {"name": "x", "harness": "deepseek",
                 "session_harness": "codex"}))
            # déclaré codex mais tourne sous deepseek : refusé
            self.assertFalse(review_lot.may_take(
                None, {"issue_ref": "review-after-merge:#1"},
                {"name": "x", "harness": "codex",
                 "session_harness": "deepseek"}))
            # un nom de harnais porte sa famille (`family_of`, #1374)
            self.assertTrue(review_lot.may_take(
                None, {"issue_ref": "review-after-merge:#1"},
                {"name": "x", "harness": "codex-mini"}))


if __name__ == "__main__":
    unittest.main()
