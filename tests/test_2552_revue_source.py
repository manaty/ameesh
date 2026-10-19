# SPDX-License-Identifier: AGPL-3.0-only
"""#2552 — la prise automatique reconnaît un lot de relecture à sa SOURCE.

#2377 garde la famille sur `issue_ref = review-after-merge:#<lot>`. Or la
référence durable d'un lot de relecture est sa `source` : c'est elle que le
corps et le journal citent, et des lots historiques la portent avec un
`issue_ref` GitHub (ou aucun) — ils échappaient donc à la garde. La prise doit
laisser ces lots à la famille opposée à l'auteur de #N, quel que soit le champ
qui porte la référence.

Témoins du 18/10 (#2549 REVUE #2514, #2550 REVUE #2503) : deux lots de
relecture d'auteur DeepSeek pris au repos par des agents DeepSeek.
"""
from __future__ import annotations

import time
import unittest

from ameesh import backlog, registry, review_lot, work

from .support import PgTestCase

HEURE = 3600.0


class RevueSourceTest(PgTestCase):
    """La garde de famille mord aussi quand seule la `source` porte la référence."""

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

    def revue(self, lot, *, issue_ref=None):
        """Ouvre le lot de relecture puis, au besoin, réécrit son `issue_ref`.

        `issue_ref=None` garde la référence écrite par l'ouverture (les deux
        champs la portent). Une chaîne la remplace : c'est la forme des lots
        historiques (#1218, #1231, #1269), où seule la `source` porte
        `review-after-merge:#<lot>`."""
        out = review_lot.open_after_merge(
            self.db, lot=lot, klass={"class": "light", "why": "policy of ameesh"},
            merged_sha="b" * 40)
        self.assertTrue(out["created"], out)
        if issue_ref is not None:
            self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                            (issue_ref, out["id"]))
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

    def test_source_seule_refuse_la_meme_famille_puis_l_autre_prend(self):
        """Forme historique : `issue_ref` GitHub, `source` = review-after-merge."""
        lot = self.produced("auteur-ds", "deepseek")     # relecture = codex
        revue = self.revue(lot, issue_ref="manaty/ameesh#1922")
        self.agent("ds1", "deepseek")
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, revue)["assignee"])
        self.agent("cx1", "codex")
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in self.take()],
                         [("cx1", revue)])

    def test_source_seule_sans_issue_ref_refuse_la_meme_famille(self):
        """Forme #1269 : `issue_ref` absent, `source` = review-after-merge."""
        lot = self.produced("auteur-ds", "deepseek")
        revue = self.revue(lot, issue_ref=None)
        self.db.execute("UPDATE work_items SET issue_ref = NULL WHERE id = %s", (revue,))
        self.agent("ds1", "deepseek")
        self.assertEqual(self.take(), [])
        self.agent("cx1", "codex")
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in self.take()],
                         [("cx1", revue)])

    def test_source_seule_famille_d_auteur_inconnue_refusee(self):
        lot = self.produced("auteur-sans-tour")          # gel sans tour tracé
        self.revue(lot, issue_ref="manaty/ameesh#1")
        self.agent("cx1", "codex")
        self.agent("cl1", "claude")
        self.assertEqual(self.take(), [])

    def test_les_deux_temoins_du_18_10_sont_refuses_a_la_meme_famille(self):
        """#2549 (auteur ameesh-dev5) et #2550 (auteur deepseek1), tous deux
        deepseek : un preneur deepseek ne les prend pas, un codex les prend."""
        for i, actor in enumerate(("auteur-2514", "auteur-2503")):
            lot = self.produced(actor, "deepseek")
            self.revue(lot)
        self.agent("ds1", "deepseek")
        self.assertEqual(self.take(), [])
        self.agent("cx1", "codex")
        prises = [(t["agent"], t["item"]["source"]) for t in self.take()]
        self.assertEqual(len(prises), 1)                 # une prise par passage
        self.assertEqual(prises[0][0], "cx1")
        self.assertTrue(prises[0][1].startswith(review_lot.PREFIX))

    def test_issue_ref_porte_la_reference_meme_si_la_source_differe(self):
        """L'autre sens : `issue_ref` = review-after-merge, `source` libre."""
        lot = self.produced("auteur-ds", "deepseek")
        revue = self.revue(lot)
        self.db.execute("UPDATE work_items SET source = 'mail' WHERE id = %s", (revue,))
        self.agent("ds1", "deepseek")
        self.assertEqual(self.take(), [])
        self.agent("cx1", "codex")
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in self.take()],
                         [("cx1", revue)])


class ReferenceRevueTest(unittest.TestCase):
    """La référence de relecture se lit sur les deux champs, `issue_ref` d'abord."""

    def test_reference_sur_l_un_ou_l_autre_champ(self):
        self.assertEqual(
            review_lot.review_ref({"source": "review-after-merge:#2549"}),
            "review-after-merge:#2549")
        self.assertEqual(
            review_lot.review_ref({"issue_ref": "review-after-merge:#7",
                                   "source": "mail"}),
            "review-after-merge:#7")
        self.assertEqual(
            review_lot.review_ref({"issue_ref": "manaty/ameesh#1",
                                   "source": "review-after-merge:#7"}),
            "review-after-merge:#7")
        self.assertEqual(review_lot.review_ref({"issue_ref": "manaty/ameesh#1"}), "")
        self.assertEqual(review_lot.review_ref({}), "")


if __name__ == "__main__":
    unittest.main()
