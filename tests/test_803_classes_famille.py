# SPDX-License-Identifier: AGPL-3.0-only
"""#803 : la prise automatique honore la classe déclarée du lot.

Un lot qui exige une capacité de classe (sensible/production/decision) n'est
tiré que si le registre porte une famille de harnais autre que celle du
preneur — garde **structurelle** du canon §2, jamais la disponibilité d'un
relecteur. La classe d'un lot non gelé se lit sur ce marqueur déclaré ; le
doute du canon §8 (diff non gelé) se tranche à la porte de fusion, pas à la
prise, sinon tout lot `intake` serait sensible.
"""
from __future__ import annotations

import time
import unittest

from ameesh import backlog, registry

from .support import PgTestCase

HEURE = 3600.0

AUTRES = (
    {"name": "ds1", "harness": "deepseek"},
    {"name": "cx1", "harness": "codex"},
    {"name": "cl1", "harness": "claude"},
)


class MarqueurTest(unittest.TestCase):
    """Lecture du marqueur et de la famille, sans base."""

    def test_marqueur_de_classe(self):
        for cap in ("sensible", "sensitive", "production", "decision"):
            self.assertTrue(backlog.declared_class({"required_capabilities": [cap]}), cap)
        self.assertFalse(backlog.declared_class({"required_capabilities": ["read"]}))
        self.assertFalse(backlog.declared_class({}))

    def test_une_autre_famille_au_registre(self):
        ds = {"name": "ds1", "harness": "deepseek"}
        # une autre famille suffit, quelle qu'elle soit (lecture structurelle)
        self.assertTrue(backlog.other_family_available(ds, AUTRES))
        # mono-famille : impossibilité de principe -> refus
        self.assertFalse(backlog.other_family_available(ds, [AUTRES[0]]))
        # le preneur ne se compte pas lui-même
        self.assertFalse(backlog.other_family_available(ds, [dict(ds)]))
        # registre illisible, ou harnais du preneur inconnu : refus
        self.assertFalse(backlog.other_family_available(ds, None))
        self.assertFalse(backlog.other_family_available({"name": "x", "harness": ""},
                                                        AUTRES))

    def test_fits_refuse_un_lot_marque_sans_autre_famille(self):
        item = {"team": "web", "required_capabilities": ["read", "sensible"]}
        agent = {"name": "ds1", "team": "web", "harness": "deepseek",
                 "capabilities": ["read", "sensible"]}
        self.assertTrue(backlog.fits(item, agent, others=AUTRES))
        self.assertFalse(backlog.fits(item, agent,
                                      others=[{"name": "ds2", "harness": "deepseek"}]))
        self.assertFalse(backlog.fits(item, agent))     # registre non fourni
        # un lot sans marqueur n'est pas concerné par la garde
        self.assertTrue(backlog.fits({"team": "web",
                                      "required_capabilities": ["read"]}, agent))
        # la capacité de classe reste exigée du preneur
        self.assertFalse(backlog.fits(item, dict(agent, capabilities=["read"]),
                                      others=AUTRES))

    def test_lot_fits_refuse_un_lot_marque_sans_autre_famille(self):
        item = {"lot_team": "web", "required_capabilities": ["sensible"]}
        agent = {"name": "ds1", "team": "web", "harness": "deepseek",
                 "capabilities": ["sensible"]}
        self.assertTrue(backlog.lot_fits(item, agent, others=AUTRES))
        self.assertFalse(backlog.lot_fits(
            item, agent, others=[{"name": "ds2", "harness": "deepseek"}]))
        self.assertFalse(backlog.lot_fits(item, agent))     # registre non fourni
        self.assertTrue(backlog.lot_fits({"lot_team": "web"}, agent))


class PriseTest(PgTestCase):
    """La garde branchée sur la prise automatique, sur une base migrée."""

    def setUp(self):
        super().setUp()
        self.now = time.time()

    def agent(self, name, harness, *, capabilities=(), since_s=2 * HEURE):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', responsible = %s"
            " WHERE name = %s", ("runner-%s@h" % name, "human:proprio", name))
        self.db.execute("UPDATE agent_registry SET capabilities = %s::text[]"
                        " WHERE name = %s", ("{" + ",".join(capabilities) + "}", name))
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(secs => %s),"
            " last_turn_at = now() - make_interval(secs => %s) WHERE name = %s",
            (since_s, since_s, name))

    def element(self, title, score, **kw):
        kw.setdefault("expected_value", "valeur mesurable pour %s" % title)
        return backlog.add(self.db, title=title, value_score=score, **kw)

    def take(self, **kw):
        kw.setdefault("now", self.now)
        kw.setdefault("paid", ("deepseek",))
        kw.setdefault("budget_check", lambda agent: "")
        return backlog.auto_take(self.cfg, self.db, **kw)

    def test_lot_marque_pris_avec_une_autre_famille(self):
        self.agent("ds1", "deepseek", capabilities=["read", "sensible"])
        self.agent("cx1", "codex", capabilities=["read"])
        item = self.element("sensible", 50, required_capabilities=["sensible"])
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("ds1", item["id"])])

    def test_lot_marque_refuse_en_mesh_mono_famille(self):
        self.agent("ds1", "deepseek", capabilities=["read", "sensible"])
        self.agent("ds2", "deepseek", capabilities=["read", "sensible"])
        self.element("sensible", 50, required_capabilities=["sensible"])
        self.assertEqual(self.take(), [])
        # dans le même mesh mono-famille, un lot sans marqueur reste pris
        item = self.element("leger", 40)
        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [item["id"]])


if __name__ == "__main__":
    unittest.main()
