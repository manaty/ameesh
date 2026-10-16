# SPDX-License-Identifier: AGPL-3.0-only
"""L157b : table de référence des durées, publiée par l'auditeur
(`work estimates --publish`) et seulement LUE à la création d'un lot ;
biais de chaque estimateur et alerte `estimate_bias`.

Tests sans base (groupes, repli vers un groupe plus large, biais, libellé,
courrier de l'alerte), puis sur Postgres réel (publication, `work add
--size`, `work show`, `ameesh alerts`).
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import alert_briefs, estimates, exploitation, registry, storage, work

from .support import PgTestCase

NOW = 1_800_000_000.0


def _row(i, type_, size, assignee, actual, *, by="orch", est=None, late=False):
    merged = NOW + i
    return {"id": i, "type": type_, "change_size": size, "assignee": assignee,
            "started_ts": merged - actual * 60, "merged_ts": merged,
            "estimate_minutes": est, "estimate_by": by, "late": late}


AGENTS = {"dev1": {"host": "vm1", "harness": "claude"},
          "dev2": {"host": "vm2", "harness": "codex"}}


class BuildReferenceTest(unittest.TestCase):

    def setUp(self) -> None:
        rows = [_row(1, "evolution", "m", "dev1", 20, est=200),
                _row(2, "evolution", "m", "dev1", 30, est=300),
                _row(3, "evolution", "m", "dev1", 40, est=400),
                _row(4, "evolution", "m", "dev2", 90, by="dev2", est=60),
                _row(5, "evolution", None, "dev2", 100, by="dev2", est=60),
                _row(6, "bug", "s", None, 10),
                # pas de début mesuré : hors table
                dict(_row(7, "bug", "s", "dev1", 10), started_ts=None)]
        self.body = estimates.build_reference(rows, AGENTS, now=NOW)

    def test_groupes_par_niveau(self):
        b = self.body
        self.assertEqual((b["schema"], b["delivered"], b["measured"]),
                         ("ameesh-estimate-reference/1", 7, 6))
        fin = [g for g in b["groups"] if g["level"] == "type,size,host,harness"]
        self.assertEqual({(g["type"], g["size"], g["host"], g["harness"], g["lots"])
                          for g in fin},
                         {("evolution", "m", "vm1", "claude", 3),
                          ("evolution", "m", "vm2", "codex", 1)})
        ts = {(g["type"], g["size"]): g for g in b["groups"] if g["level"] == "type,size"}
        self.assertEqual(ts[("evolution", "m")]["lots"], 4)
        self.assertEqual(ts[("evolution", "m")]["median_minutes"], 35.0)
        # taille inconnue : dans le groupe du type, pas dans un groupe « ? »
        self.assertNotIn(("evolution", "?"), ts)
        t = {g["type"]: g for g in b["groups"] if g["level"] == "type"}
        self.assertEqual((t["evolution"]["lots"], t["bug"]["lots"]), (5, 1))
        tous = next(g for g in b["groups"] if g["level"] == "*")
        self.assertEqual(tous["lots"], 6)

    def test_biais_par_estimateur(self):
        by = {e["estimator"]: e for e in self.body["estimators"]}
        self.assertEqual(by["orch"]["median_ratio"], 0.1)
        self.assertTrue(by["orch"]["biased"])            # ×0,1 sur 3 lots
        self.assertFalse(by["dev2"]["biased"])           # ×1,58, 2 lots : trop peu
        self.assertEqual([e["estimator"] for e in self.body["bias"]], ["orch"])

    def test_lecture_et_repli(self):
        b = self.body
        g = estimates.lookup(b, type="evolution", size="m", host="vm1", harness="claude")
        self.assertEqual((g["level"], g["lots"], g["median_minutes"]),
                         ("type,size,host,harness", 3, 30.0))
        # un seul lot sur vm2/codex : on remonte au type et à la taille
        g = estimates.lookup(b, type="evolution", size="m", host="vm2", harness="codex")
        self.assertEqual(g["level"], "type,size")
        # sans taille ni assigné : le type
        self.assertEqual(estimates.lookup(b, type="evolution")["level"], "type")
        # le type bug n'a qu'un lot : l'ensemble
        self.assertEqual(estimates.lookup(b, type="bug", size="s")["level"], "*")
        self.assertIsNone(estimates.lookup({"groups": [], "min_lots": 3}, type="bug"))

    def test_libelle(self):
        g = estimates.lookup(self.body, type="evolution", size="m", host="vm1",
                             harness="claude")
        text = estimates.proposal_label(dict(g, published_ts=NOW, published_by="auditeur"))
        self.assertIn("median 30 min · p80 36 min", text)
        self.assertIn("evolution, medium, vm1, claude, 3 lots", text)
        self.assertIn("by auditeur", text)
        self.assertIn("none", estimates.proposal_label(None))


class BriefTest(unittest.TestCase):

    def test_bias_ne_reveille_pas_un_ancien_assigneur(self):
        # #1980 : une réaffectation ancienne ne donne aucun mandat pour
        # corriger les estimations d'un tiers, même dans la même équipe.
        listing = [{"name": name, "canon": "legacy", "chantier": "app"}
                   for name in ("dev1", "docs", "orch", "pilot", "auditeur")]
        canon = types.SimpleNamespace(
            agents=[types.SimpleNamespace(title="pilot", roles=["orchestrateur"])],
            owns=lambda name: name == "home")
        listing[3]["canon"] = "home"
        ref = {"id": 5, "published_ts": NOW,
               "body": {"bias": [{"estimator": "dev1", "median_ratio": 3,
                                  "p80_ratio": 5, "lots": 4}]}}
        ops = types.SimpleNamespace(assigners=lambda since_s: ["docs"])
        with mock.patch.object(estimates, "current_reference", return_value=ref), \
                mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                mock.patch("ameesh.storage.of",
                           return_value=types.SimpleNamespace(operations=ops)):
            alerts = exploitation._estimate_bias(
                types.SimpleNamespace(), object(), listing, None, declared=["orch"])
        self.assertEqual(len(alerts), 1)
        out = alert_briefs.briefs(alerts[0], listing, auditor_names=["auditeur"])
        self.assertEqual(set(out), {"dev1", "orch", "pilot", "auditeur"})

    def test_estimateur_et_orchestrateurs(self):
        alert = {"type": "estimate_bias", "agent": "dev1", "value": 0.1,
                 "detail": "estimations de dev1 : réel/estimé médian ×0,1",
                 "orchestrators": ["orch"]}
        listing = [{"name": "dev1"}, {"name": "orch"}, {"name": "auditeur"}]
        out = alert_briefs.briefs(alert, listing, {}, ["auditeur"])
        self.assertEqual(set(out), {"dev1", "orch", "auditeur"})
        self.assertIn("overestimates", out["dev1"])
        self.assertIn("phases", out["orch"])
        self.assertIn("estimate_bias", exploitation.ALERT_TYPES)


class ReferencePgTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def _deliver(self, lot_id, *, started_min_ago):
        work.move(self.db, lot_id, "build")
        work.close_merged(self.db, lot_id, sha="abcdef1")
        # un seul instant pour les trois dates : avec le pilote psql, chaque
        # requête a sa transaction, donc son propre now()
        at = float(self.db.query("SELECT extract(epoch from now())::float8 AS at_epoch")[0]["at_epoch"])
        self.db.execute(
            "UPDATE work_items SET started_at = to_timestamp(%s) - make_interval(mins => %s)"
            " WHERE id = %s", (at, int(started_min_ago), int(lot_id)))
        self.db.execute(
            "UPDATE work_item_estimates SET at = to_timestamp(%s) - make_interval(mins => %s)"
            " WHERE work_item_id = %s", (at, int(started_min_ago) + 5, int(lot_id)))
        self.db.execute(
            "UPDATE work_item_milestones SET at = to_timestamp(%s) WHERE work_item_id = %s"
            " AND kind = 'merged'", (at, int(lot_id)))

    def test_publication_lecture_et_alerte(self):
        # sans table publiée : pas de référence, pas d'alerte
        self.assertIsNone(estimates.current_reference(self.db))
        proc = self.mesh("work", "add", "--title", "avant", "--actor", "orch",
                         "--estimate", "1h", "--size", "m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("none (no published table", proc.stdout)
        work.close(self.db, work.list_items(self.db)[0]["id"], abandoned=True)

        # trois lots estimés 10 fois trop par orch
        for minutes in (20, 30, 40):
            lot = work.add(self.db, title="lot %d" % minutes, actor="orch",
                           estimate=minutes * 10, size="m")
            self._deliver(lot["id"], started_min_ago=minutes)
        # la création seule ne calcule rien : toujours pas de référence
        self.assertIsNone(estimates.proposal(self.db, type="evolution", size="m"))

        proc = self.mesh("work", "estimates", "--publish", "--actor", "auditeur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BIAS (estimate_bias)", proc.stdout)
        ref = estimates.current_reference(self.db)
        self.assertEqual((ref["published_by"], ref["body"]["measured"]), ("auditeur", 3))
        self.assertEqual([e["estimator"] for e in ref["body"]["bias"]], ["orch"])

        proc = self.mesh("work", "add", "--title", "après", "--actor", "orch",
                         "--estimate", "30m", "--size", "m",
                         "--estimate-source", "référence ×1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("median 30 min · p80 36 min (evolution, medium, 3 lots",
                      proc.stdout)
        new = [i for i in work.list_items(self.db) if i["title"] == "après"][0]
        self.assertEqual(new["change_size"], "m")
        proc = self.mesh("work", "show", str(new["id"]), "--json")
        shown = json.loads(proc.stdout)
        self.assertEqual(shown["estimate_reference"]["median_minutes"], 30.0)
        proc = self.mesh("work", "show", str(new["id"]))
        self.assertIn(": median 30 min", proc.stdout)
        self.assertIn(" medium", proc.stdout)

        bias = [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "estimate_bias"]
        self.assertEqual([(a["agent"], a["value"], a["lot_count"]) for a in bias],
                         [("orch", 0.1, 3)])
        # --publish couvre tous les projets
        proc = self.mesh("work", "estimates", "--publish", "--app", "p")
        self.assertEqual(proc.returncode, 2)
        # une taille inconnue est refusée
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="x", size="xl")
        self.assertEqual(storage.of(self.db).work.reference()["id"], ref["id"])


if __name__ == "__main__":
    unittest.main()
