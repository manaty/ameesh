# SPDX-License-Identifier: AGPL-3.0-only
"""#2584 : `backlog add` respecte le canon §2 l.129 — un défaut non bloquant
sous le seuil de lot du canon devient une ligne du panier de son chantier, un
défaut trouvé en relisant une suite d'avis ne crée rien, et un élément sous le
seuil ne se confie pas tant qu'un lot de roadmap du projet est prenable.

Le seuil vient de `processus/mecanique.yaml` (`backlog.lot_min_value`), lu au
commit exact ; sans bloc, la route historique (un élément = un lot) reste."""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ameesh import backlog, canon, mechanics, registry, review_lot, storage, work

from .support import PgTestCase
from .test_l1616_mechanics import MECHANICS, _Repo

HEURE = 3600.0
SEUIL = backlog.Rules(lot_min_value=30, timezone="Europe/Paris", commit="c" * 40)
BLOC = "backlog:\n  lot_min_value: 30\n"


def _paris(day: int, hour: int = 10) -> float:
    return datetime(2026, 10, day, hour, tzinfo=ZoneInfo("Europe/Paris")).timestamp()


class MechanicsBlockTest(unittest.TestCase):
    """Le bloc `backlog` du canon : optionnel, strict quand il est là."""

    def test_absent_block_is_none_and_not_rendered(self):
        document = mechanics.compile_document(MECHANICS)
        self.assertIsNone(document.backlog_lot_min_value)
        self.assertNotIn("backlog", document.to_dict())

    def test_declared_threshold_is_compiled(self):
        document = mechanics.compile_document(MECHANICS + BLOC)
        self.assertEqual(document.backlog_lot_min_value, 30)
        self.assertEqual(document.to_dict()["backlog"], {"lot_min_value": 30})

    def test_malformed_block_refuses(self):
        for block in ("backlog:\n  lot_min_value: 0\n",
                      "backlog:\n  lot_min_value: 101\n",
                      "backlog:\n  lot_min_value: \"30\"\n",
                      "backlog:\n  lot_min_value: true\n",
                      "backlog:\n  lot_min_value: 30\n  basket: daily\n",
                      "backlog: {}\n"):
            with self.subTest(block=block), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(MECHANICS + block)


class RulesTest(unittest.TestCase):
    """`backlog.rules` lit le seuil au commit exact du canon configuré."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        backlog._RULES.clear()
        self.addCleanup(backlog._RULES.clear)

    def _rules(self, text, project="ameesh"):
        repo = _Repo(self.tmp.name, mechanics_text=text)
        commit = repo.git("rev-parse", "HEAD")
        source = SimpleNamespace(mode="git", repo=repo.root, commit=commit)
        cfg = SimpleNamespace(canon=repo.root)
        with patch.object(canon, "load_configured",
                          return_value=[SimpleNamespace(sources=[source])]):
            return backlog.rules(cfg, project), commit

    def test_threshold_and_timezone_come_from_the_canon(self):
        rule, commit = self._rules(MECHANICS + BLOC)
        self.assertEqual(rule, backlog.Rules(30, "Europe/Paris", commit))
        self.assertEqual(mechanics.load(self.tmp.name, commit,
                                        "ameesh").backlog_lot_min_value, 30)

    def test_no_block_no_threshold(self):
        rule, _commit = self._rules(MECHANICS)
        self.assertIsNone(rule)

    def test_undeclared_project_or_no_canon_no_threshold(self):
        rule, _commit = self._rules(MECHANICS + BLOC, project="inconnu")
        self.assertIsNone(rule)
        self.assertIsNone(backlog.rules(SimpleNamespace(canon=""), "ameesh"))
        self.assertIsNone(backlog.rules(None, "ameesh"))


class ReviewOriginTest(unittest.TestCase):
    """L'origine d'un élément issu d'une relecture, lue sur son texte."""

    def test_sources_and_titles_of_the_study(self):
        cases = [
            ({"source": "relecture #313 (mty-amh-qa-01, N1)"}, 313),
            ({"source": "verdict ameesh-dev4 2026-10-19 sur #2577 93f49eff2 (N2)"}, 2577),
            ({"source": "R2 #1300 ameesh-dev1 + claude2"}, 1300),
            ({"source": "N relecture auditeur #2198 @e91a0254, 19/10"}, 2198),
            ({"source": "relecture #2532 (résiduel #2525)"}, 2532),
            ({"source": "review-of:#42 relecture"}, 42),
            ({"title": "#2464 N (dev7) : la classe d'une remise", "source": "finding"}, 2464),
            ({"title": "account_sharing relus à chaud (#313 N1)"}, 313),
            # un constat, une issue GitHub, une source sans relecture : rien
            ({"source": "#2577 dry-run complet du delta, 2026-10-19"}, None),
            ({"source": "relecture manaty/nexlink#1669"}, None),
            ({"source": "constat de l'auditeur", "title": "Dette #12 à rembourser"}, None),
        ]
        for kw, expected in cases:
            with self.subTest(**kw):
                self.assertEqual(backlog.review_origin(**kw), expected)


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.now = _paris(19)
        self.rules = patch.object(backlog, "rules", side_effect=self._rules)
        self.rules.start()
        self.addCleanup(self.rules.stop)
        self.threshold = SEUIL

    def _rules(self, cfg, project):
        return self.threshold if project else None

    def element(self, title, score, **kw):
        kw.setdefault("expected_value", "gain mesurable pour %s" % title)
        kw.setdefault("app", "ameesh")
        kw.setdefault("now", self.now)
        kw.setdefault("actor", "relecteur")
        return backlog.add(self.db, title=title, value_score=score, **kw)

    def lot(self, title, **kw):
        kw.setdefault("app", "ameesh")
        return work.add(self.db, title=title, **kw)

    def improvements(self):
        return [row["id"] for row in self.db.query(
            "SELECT id FROM work_items WHERE type = 'improvement' ORDER BY id")]

    def agent(self, name, harness="deepseek"):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', team = 'ameesh',"
            " status_since = now() - interval '2 hours',"
            " last_turn_at = now() - interval '2 hours' WHERE name = %s",
            ("runner-%s@h" % name, name))


class BasketTest(_Base):
    def test_two_minor_defects_of_a_lot_share_one_basket(self):
        source = self.lot("Ajouter l'export des comptes")
        first = self.element("N1 : nommer la colonne", 10, review_of=source["id"],
                             source="relecture R1", body="détail\nsur deux lignes")
        second = self.element("N2 : ordre du tri", 20, review_of=source["id"],
                              priority=1, required_capabilities=["sensible"])
        self.assertTrue(first["basket"] and second["basket"])
        self.assertTrue(first["basket_opened"])
        self.assertFalse(second["basket_opened"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.improvements(), [first["id"]])
        basket = work.get(self.db, first["id"])
        self.assertEqual(basket["type"], "improvement")
        self.assertEqual((basket["state"], basket["assignee"]), ("intake", None))
        self.assertEqual(basket["source"],
                         "backlog-basket:ameesh:#%d:2026-10-19" % source["id"])
        self.assertIn("#%d" % source["id"], basket["title"])
        self.assertEqual(second["basket_lines"], 2)
        # valeur : la plus haute ; priorité : la plus haute ; capacités : l'union
        self.assertEqual((basket["value_score"], basket["priority"]), (20, 1))
        self.assertEqual(basket["required_capabilities"], ["sensible"])
        self.assertIn("N1 : nommer la colonne", basket["body"])
        self.assertIn("  sur deux lignes", basket["body"])
        self.assertIn("N2 : ordre du tri", basket["body"])
        self.assertIn("review-of:#%d relecture R1" % source["id"], basket["body"])
        # le journal porte une ligne par défaut ; l'auteur du panier est le premier
        notes = [e["note"] for e in work.events(self.db, basket["id"])]
        self.assertEqual(sum(n.startswith(backlog.BASKET_LINE_NOTE) for n in notes), 2)
        # un panier est une suite d'avis
        self.assertTrue(backlog.is_review_followup(basket))

    def test_at_the_threshold_the_defect_keeps_its_lot(self):
        source = self.lot("Ajouter l'export des comptes")
        item = self.element("N3 : perte silencieuse d'une ligne", 30, review_of=source["id"],
                            source="relecture R1")
        self.assertNotIn("basket", item)
        self.assertEqual(item["source"], "review-of:#%d relecture R1" % source["id"])
        self.assertFalse(item["source"].startswith(backlog.BASKET_PREFIX))
        # né d'une relecture, il est lui-même une suite d'avis
        self.assertTrue(backlog.is_review_followup(item))

    def test_workstream_day_and_taken_basket(self):
        one = self.lot("Lot un")
        two = self.lot("Lot deux")
        a = self.element("N : a", 5, review_of=one["id"])
        b = self.element("N : b", 5, review_of=two["id"])
        self.assertNotEqual(a["id"], b["id"])          # un panier par lot relu
        c = self.element("N : c", 5, review_of=one["id"], chantier="Ménage /tmp")
        d = self.element("N : d", 5, review_of=two["id"], chantier="  ménage   /TMP ")
        self.assertEqual(c["id"], d["id"])             # --chantier regroupe
        self.assertNotIn(c["id"], (a["id"], b["id"]))
        e = self.element("N : e", 5)                   # sans origine : le projet
        self.assertEqual(work.get(self.db, e["id"])["source"],
                         "backlog-basket:ameesh:ameesh:2026-10-19")
        f = self.element("N : f", 5, review_of=one["id"], now=_paris(20))
        self.assertNotEqual(f["id"], a["id"])          # un panier par jour
        # un panier pris est fermé : la ligne suivante en ouvre un autre
        work.assign(self.db, a["id"], "human:proprio")
        g = self.element("N : g", 5, review_of=one["id"])
        self.assertTrue(g["basket_opened"])
        self.assertNotEqual(g["id"], a["id"])
        self.assertNotIn("N : g", work.get(self.db, a["id"])["body"])

    def test_package_is_the_workstream(self):
        storage.of(self.db).packages.upsert({"id": "WP-EXPORT", "kind": "lot",
                                             "title": "Export", "team": "ameesh",
                                             "canon_ref": "test"})
        source = self.lot("Export CSV", package="WP-EXPORT")
        line = self.element("N : en-tête", 8, review_of=source["id"])
        basket = work.get(self.db, line["id"])
        self.assertEqual(basket["source"], "backlog-basket:ameesh:wp-export:2026-10-19")
        self.assertEqual(basket["package_id"], "WP-EXPORT")

    def test_without_canon_threshold_the_historical_route_stays(self):
        self.threshold = None
        source = self.lot("Ajouter l'export des comptes")
        first = self.element("N1 : nommer la colonne", 10, review_of=source["id"])
        second = self.element("N2 : ordre du tri", 10, review_of=source["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotIn("basket", first)
        # sans seuil, une suite d'avis n'est pas refusée non plus
        self.element("N : sur la suite", 5, review_of=first["id"])


class ReviewFollowupRefusalTest(_Base):
    def test_a_defect_found_reviewing_a_basket_creates_nothing(self):
        source = self.lot("Ajouter l'export des comptes")
        basket = self.element("N1 : nommer la colonne", 10, review_of=source["id"])
        before = self.improvements()
        with self.assertRaises(backlog.BacklogError) as ctx:
            self.element("N : encore", 5, review_of=basket["id"])
        self.assertIn("creates nothing", str(ctx.exception))
        self.assertIn("#%d" % basket["id"], str(ctx.exception))
        # au seuil non plus : une suite de suite n'est jamais un lot
        with self.assertRaises(backlog.BacklogError):
            self.element("N : grave mais non bloquant", 60, review_of=basket["id"])
        self.assertEqual(self.improvements(), before)

    def test_follow_up_lots_and_inferred_origin(self):
        suite = self.lot("#2452 N2 : train_reviews hors des classes")
        with self.assertRaises(backlog.BacklogError):
            self.element("N : libellé", 5, review_of=suite["id"])
        # origine lue sur la source de relecture, sans --review-of
        with self.assertRaises(backlog.BacklogError):
            self.element("N : libellé", 5, source="relecture #%d (dev7, N1)" % suite["id"])
        item = self.element("constat neuf", 40, source="relecture #%d (R1)"
                            % self.lot("Roadmap : export")["id"])
        with self.assertRaises(backlog.BacklogError):
            self.element("N : sur le constat relu", 5,
                         source="verdict dev7 sur #%d (N1)" % item["id"])

    def test_after_merge_review_points_to_its_lot(self):
        suite = self.lot("#2452 N2 : train_reviews hors des classes")
        revue = self.lot("REVUE #%d : relecture après fusion" % suite["id"],
                         issue_ref=review_lot.ref_of(suite["id"]))
        with self.assertRaises(backlog.BacklogError):
            self.element("N : libellé", 5, review_of=revue["id"])
        roadmap = self.lot("Roadmap : export")
        revue2 = self.lot("REVUE #%d : relecture après fusion" % roadmap["id"],
                          issue_ref=review_lot.ref_of(roadmap["id"]))
        line = self.element("N : libellé", 5, review_of=revue2["id"])
        self.assertEqual(work.get(self.db, line["id"])["source"],
                         "backlog-basket:ameesh:#%d:2026-10-19" % roadmap["id"])

    def test_unknown_review_of_is_refused(self):
        with self.assertRaises(backlog.BacklogError):
            self.element("N : x", 5, review_of=999999)
        with self.assertRaises(backlog.BacklogError):
            self.element("N : x", 5, review_of="abc")


class AssignTest(_Base):
    def test_basket_waits_for_the_roadmap_lot(self):
        self.agent("dev1")
        roadmap = self.lot("Roadmap : export des comptes")
        basket = self.element("N : libellé", 10)
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, basket["id"], "dev1", actor="orch", cfg=self.cfg)
        self.assertIn("#%d" % roadmap["id"], str(ctx.exception))
        self.assertIsNone(work.get(self.db, basket["id"])["assignee"])
        # un humain peut le prendre ; un élément au seuil se confie
        big = self.element("dette réelle", 30)
        work.assign(self.db, big["id"], "dev1", actor="orch", cfg=self.cfg)
        # la roadmap confiée, le panier se confie
        self.agent("dev2")
        work.assign(self.db, roadmap["id"], "dev2", actor="orch", cfg=self.cfg)
        self.agent("dev3")
        work.assign(self.db, basket["id"], "dev3", actor="orch", cfg=self.cfg)
        self.assertEqual(work.get(self.db, basket["id"])["assignee"], "dev3")

    def test_human_and_followup_lots_do_not_hold_the_basket(self):
        self.agent("dev1")
        # une suite d'avis ou une relecture après fusion n'est pas de la roadmap
        self.lot("#2452 N2 : train_reviews hors des classes")
        self.lot("REVUE #1 : relecture après fusion", issue_ref=review_lot.ref_of(1))
        # un lot d'un autre projet non plus
        self.lot("Roadmap Nexlink", app="nexlink")
        basket = self.element("N : libellé", 10)
        self.assertEqual(backlog.assign_refusal(self.db, work.get(self.db, basket["id"]),
                                                cfg=self.cfg), "")
        self.lot("Roadmap : export des comptes")
        work.assign(self.db, basket["id"], "human:proprio")
        self.assertEqual(work.get(self.db, basket["id"])["assignee"], "human:proprio")

    def test_without_threshold_nothing_waits(self):
        self.agent("dev1")
        self.lot("Roadmap : export des comptes")
        self.threshold = None
        item = self.element("N : libellé", 10)
        work.assign(self.db, item["id"], "dev1", actor="orch", cfg=self.cfg)


class AutoTakeTest(_Base):
    def test_idle_take_leaves_the_basket_while_roadmap_waits(self):
        self.agent("dev1")
        roadmap = self.lot("Roadmap : export des comptes")
        basket = self.element("N : libellé", 10)
        taken = backlog.auto_take(self.cfg, self.db, now=time.time(), paid=("deepseek",),
                                  budget_check=lambda agent: "", lot_idle_s=0,
                                  review_max_per_hour=0)
        self.assertNotIn(basket["id"], [t["item"]["id"] for t in taken])
        self.assertIsNone(work.get(self.db, basket["id"])["assignee"])
        self.assertIsNone(work.get(self.db, roadmap["id"])["assignee"])


if __name__ == "__main__":
    unittest.main()
