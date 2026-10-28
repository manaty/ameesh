# SPDX-License-Identifier: AGPL-3.0-only
"""#4117 : `work backlog add` et les paniers passent par `estimates.check`.

L157 rend l'estimation obligatoire pour un lot créé par un agent. La file
d'amélioration et la création automatique d'un panier (`backlog.py`, #2584)
l'ignoraient : 183 lots sur 1 000 sans estimation (analyse de l'auditeur du
28/10, table #16).

Témoins : une création de lot par un agent sans estimation est refusée ; un
panier ouvert sans estimation prend la référence publiée par l'auditeur
(type et taille, source « référence table #N ») ; une estimation explicite
l'emporte ; un panier existant et les lots existants ne sont pas réestimés.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import backlog, estimates, registry, storage, work

from .support import PgTestCase
from .test_l1616_mechanics import FEDERATION, MECHANICS, _Repo

SEUIL = backlog.Rules(lot_min_value=30, timezone="Europe/Paris", commit="c" * 40)
BLOC = "backlog:\n  lot_min_value: 30\n"


def _reference(db, *, size=None, median=25.0, p80=40.0, type_median=35.0):
    """Publie une table de référence minimale (auditeur) et rend sa ligne."""
    groups = []
    if size:
        groups.append({"level": "type,size", "type": backlog.ITEM_TYPE, "size": size,
                       "lots": 4, "median_minutes": median, "p80_minutes": p80})
    groups.append({"level": "type", "type": backlog.ITEM_TYPE, "lots": 7,
                   "median_minutes": type_median, "p80_minutes": type_median + 15})
    body = {"schema": estimates.REFERENCE_SCHEMA, "generated_ts": 1.0,
            "delivered": 7, "measured": 7, "min_lots": 3,
            "bias_range": [0.5, 2.0], "groups": groups, "estimators": [], "bias": []}
    return storage.of(db).work.publish_reference(body, actor="auditeur")


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev1", harness="deepseek", host=self.cfg.host,
                        chantier="ameesh")
        self.rules = mock.patch.object(backlog, "rules", return_value=SEUIL)
        self.rules.start()
        self.addCleanup(self.rules.stop)

    def element(self, title, score, **kw):
        kw.setdefault("expected_value", "gain mesurable pour %s" % title)
        kw.setdefault("actor", "dev1")
        return backlog.add(self.db, title=title, value_score=score, **kw)

    def titles(self):
        return [row["title"] for row in work.list_items(self.db)]


class AgentCreationTest(_Base):
    def test_a_lot_created_by_an_agent_without_estimate_is_refused(self):
        with self.assertRaises(estimates.EstimateError) as caught:
            self.element("lot sans durée", 40)
        self.assertIn("estimate required", str(caught.exception))
        self.assertIn("--estimate 2h", str(caught.exception))
        self.assertEqual(work.list_items(self.db), [])

    def test_the_estimate_and_the_size_are_stored_on_the_lot(self):
        item = self.element("lot estimé", 40, estimate="90m",
                            estimate_source="conception", size="s")
        self.assertEqual(item["type"], backlog.ITEM_TYPE)
        self.assertEqual((item["estimate_minutes"], item["estimate_source"],
                          item["estimate_by"], item["change_size"]),
                         (90, "conception", "dev1", "s"))
        history = storage.of(self.db).work.estimates(item["id"])
        self.assertEqual([(h["minutes"], h["source"], h["estimated_by"]) for h in history],
                         [(90, "conception", "dev1")])

    def test_a_human_still_creates_a_lot_without_estimate(self):
        item = self.element("lot d'un humain", 40, actor="human:proprio")
        self.assertIsNone(item["estimate_minutes"])

    def test_an_unknown_size_is_refused_before_creation(self):
        with self.assertRaises(backlog.BacklogError) as caught:
            self.element("taille inconnue", 40, estimate="30m", size="xl")
        self.assertIn("unknown size", str(caught.exception))
        self.assertEqual(work.list_items(self.db), [])


class ReferenceTest(_Base):
    def test_a_basket_takes_the_type_and_size_reference(self):
        ref = _reference(self.db, size="s", median=25.0)
        item = self.element("N : en-tête", 5, size="s", chantier="taille-s")
        self.assertTrue(item["basket"])
        self.assertTrue(item["basket_opened"])
        self.assertEqual(item["estimate_minutes"], 25)
        self.assertEqual(item["estimate_source"], backlog.REFERENCE_SOURCE % ref["id"])
        self.assertEqual(item["estimate_by"], estimates.REFERENCE_ESTIMATOR)
        self.assertEqual(item["change_size"], "s")
        history = storage.of(self.db).work.estimates(item["id"])
        self.assertEqual([(h["minutes"], h["source"], h["estimated_by"]) for h in history],
                         [(25, backlog.REFERENCE_SOURCE % ref["id"],
                           estimates.REFERENCE_ESTIMATOR)])
        # la proposition porte l'identifiant de la table, pas seulement sa date
        proposal = estimates.proposal(self.db, type=backlog.ITEM_TYPE, size="s")
        self.assertEqual((proposal["id"], proposal["median_minutes"]), (ref["id"], 25.0))

    def test_an_unknown_size_falls_back_to_the_type_group(self):
        _reference(self.db, size="s", median=25.0, type_median=35.0)
        item = self.element("N : ordre du tri", 5, size="m", chantier="taille-m")
        self.assertEqual(item["estimate_minutes"], 35)
        self.assertEqual(item["estimate_source"].split("#")[0], "référence table ")

    def test_an_explicit_estimate_wins_over_the_reference(self):
        _reference(self.db, size="s", median=25.0)
        item = self.element("N : nommer la colonne", 5, size="s",
                            estimate="45m", estimate_source="conception",
                            chantier="explicite")
        self.assertEqual((item["estimate_minutes"], item["estimate_source"],
                          item["estimate_by"]),
                         (45, "conception", "dev1"))

    def test_without_a_reference_an_agent_is_refused(self):
        with self.assertRaises(estimates.EstimateError):
            self.element("N : sans référence", 5, chantier="sans-reference")
        self.assertEqual(work.list_items(self.db), [])

    def test_a_human_may_open_a_basket_without_a_reference(self):
        item = self.element("N : humain", 5, actor="human:proprio",
                            chantier="humain")
        self.assertTrue(item["basket"])
        self.assertIsNone(item["estimate_minutes"])

    def test_an_open_basket_is_not_reestimated(self):
        first = self.element("N1 : une", 5, estimate="45m", chantier="panier-existant")
        self.assertEqual(first["estimate_minutes"], 45)
        _reference(self.db, size="s", median=25.0)
        second = self.element("N2 : deux", 5, size="s", chantier="panier-existant")
        self.assertEqual(second["id"], first["id"])
        self.assertFalse(second["basket_opened"])
        basket = work.get(self.db, first["id"])
        self.assertEqual((basket["estimate_minutes"], basket["estimate_source"]),
                         (45, None))

    def test_a_line_added_to_an_open_basket_needs_no_estimate(self):
        # B/P2 de la relecture cx2 : sans table publiée, l'agent ajoute une
        # ligne à un panier déjà ouvert (estimé par sa première ligne) ; aucun
        # lot n'est créé, donc aucune estimation n'est exigée.
        first = self.element("N1 : une", 5, estimate="45m", chantier="panier-sans-table")
        self.assertEqual(first["estimate_minutes"], 45)
        second = self.element("N2 : deux", 5, chantier="panier-sans-table")
        self.assertEqual(second["id"], first["id"])
        self.assertFalse(second["basket_opened"])
        basket = work.get(self.db, first["id"])
        self.assertEqual((basket["estimate_minutes"], basket["estimate_source"]),
                         (45, None))

    def test_existing_lots_are_not_backfilled(self):
        legacy = work.add(self.db, title="lot existant", app="ameesh")
        _reference(self.db, size="s", median=25.0)
        self.element("N : nouveau", 5, size="s", chantier="neuf")
        self.assertIsNone(work.get(self.db, legacy["id"])["estimate_minutes"])


class CliTest(PgTestCase):
    """La commande, de bout en bout, avec un canon qui déclare le seuil."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev1", harness="deepseek", host=self.cfg.host,
                        chantier="ameesh")
        self.canon = os.path.join(self.tmp, "canon")
        os.makedirs(self.canon, exist_ok=True)
        _Repo(self.canon, mechanics_text=MECHANICS + BLOC,
              federation_text=FEDERATION + "members: []\n")
        self.env_canon = dict(AMEESH_CANON=self.canon, AMEESH_CANON_REF="HEAD")

    def mesh(self, *args, **kw):
        kw.setdefault("env", self.env(**self.env_canon))
        return super().mesh(*args, **kw)

    def test_a_lot_from_an_agent_without_estimate_is_refused(self):
        proc = self.mesh("work", "backlog", "add", "--title", "sans durée",
                         "--value", "valeur", "--score", "40", "--actor", "dev1")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("estimate required", proc.stderr)
        self.assertIn("--estimate 2h", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])

    def test_a_lot_with_estimate_displays_it_and_the_reference(self):
        _reference(self.db, size="s", median=25.0)
        proc = self.mesh("work", "backlog", "add", "--title", "estimé",
                         "--value", "valeur", "--score", "40", "--actor", "dev1",
                         "--estimate", "1h30", "--estimate-source", "conception",
                         "--size", "s")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("estimated 1 h 30", proc.stdout)
        self.assertIn("reference:", proc.stdout)
        item = work.list_items(self.db)[0]
        self.assertEqual((item["estimate_minutes"], item["estimate_source"],
                          item["change_size"]), (90, "conception", "s"))
        # la référence reste une lecture : elle n'écrase pas l'estimation explicite
        self.assertIn("median 25 min", proc.stdout)

    def test_an_auto_basket_takes_the_reference_and_says_so(self):
        ref = _reference(self.db, size="s", median=25.0, p80=40.0)
        proc = self.mesh("work", "backlog", "add", "--title", "N : ligne",
                         "--value", "valeur", "--score", "5", "--actor", "dev1",
                         "--size", "s")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("line added to basket", proc.stdout)
        self.assertIn("estimated 25 min", proc.stdout)
        self.assertIn("reference: median 25 min", proc.stdout)
        item = work.list_items(self.db)[0]
        self.assertEqual((item["estimate_minutes"], item["estimate_source"]),
                         (25, backlog.REFERENCE_SOURCE % ref["id"]))
        proc = self.mesh("work", "show", str(item["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("estimated 25 min", proc.stdout)
        self.assertIn("référence table #%s" % ref["id"], proc.stdout)

    def test_a_human_is_warned_not_refused(self):
        proc = self.mesh("work", "backlog", "add", "--title", "par un humain",
                         "--value", "valeur", "--score", "40", "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot without an estimated duration", proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["estimate_minutes"], None)

    def test_an_unknown_size_is_refused_by_the_parser(self):
        proc = self.mesh("work", "backlog", "add", "--title", "taille",
                         "--value", "valeur", "--score", "40", "--actor", "dev1",
                         "--estimate", "30m", "--size", "xl")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid choice", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])


if __name__ == "__main__":
    unittest.main()
