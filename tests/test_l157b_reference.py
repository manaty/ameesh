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

from ameesh import (alert_briefs, estimates, exploitation, notify, registry,
                    storage, work)

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
        listing = [{"name": "dev1", "team": "app"}, {"name": "orch", "team": "app"},
                   {"name": "auditeur", "team": "app"}]
        out = alert_briefs.briefs(alert, listing, {}, ["auditeur"])
        self.assertEqual(set(out), {"dev1", "orch", "auditeur"})
        self.assertIn("overestimates", out["dev1"])
        self.assertIn("phases", out["orch"])
        self.assertIn("estimate_bias", exploitation.ALERT_TYPES)

    def test_estimateur_sans_equipe_ne_reveille_pas_tous_les_orchestrateurs(self):
        # #3502 : un estimateur hors registre (`claude3`, `bureau`, `inconnu`,
        # `human:smichea` dans la table du 25/10) n'a pas d'équipe ; le repli
        # « sans équipe, tous les orchestrateurs » réveillait les pilotes des
        # autres projets (l'orchestrateur Nexlink recevait les estimateurs
        # ameesh). L'auditeur reste destinataire — c'est son geste.
        listing = [{"name": "mesh-design", "team": "ameesh"},
                   {"name": "orchestrateur", "team": "nexlink"},
                   {"name": "auditeur", "team": "ameesh"}]
        alert = {"type": "estimate_bias", "agent": "claude3", "value": 2.9,
                 "detail": "estimates of claude3: median ×2.9",
                 "orchestrators": ["mesh-design", "orchestrateur"]}
        out = alert_briefs.briefs(alert, listing, {}, ["auditeur"])
        self.assertEqual(set(out), {"auditeur"})
        # un estimateur au registre mais sans projet reçoit SON brief, pas
        # celui de tous les orchestrateurs.
        alert["agent"] = "bureau"
        listing.append({"name": "bureau"})
        out = alert_briefs.briefs(alert, listing, {}, ["auditeur"])
        self.assertEqual(set(out), {"bureau", "auditeur"})


class BiasEpisodeTest(unittest.TestCase):
    """#3502 : l'alerte est un épisode (sens, palier) daté à l'entrée en
    biais, pas une publication ; republier ne la relance pas."""

    def _body(self, generated, ratio):
        return {"schema": estimates.REFERENCE_SCHEMA, "generated_ts": generated,
                "bias_range": [0.5, 2.0],
                "bias": [{"estimator": "dev1", "median_ratio": ratio,
                          "p80_ratio": ratio * 1.5, "lots": 4, "biased": True}]}

    def _alert(self, ref):
        with mock.patch.object(estimates, "current_reference", return_value=ref):
            return exploitation._estimate_bias(
                types.SimpleNamespace(), object(), [], [])[0]

    def test_sens_et_paliers(self):
        self.assertEqual(estimates.bias_side(3), "under")
        self.assertEqual(estimates.bias_side(0.3), "over")
        self.assertIsNone(estimates.bias_side(1))
        self.assertEqual([estimates.bias_level(r) for r in (3, 4, 8, 0.3, 0.25, 0.1)],
                         [1, 2, 3, 1, 2, 3])
        self.assertIsNone(estimates.bias_level(1))
        # un ratio nul ne boucle pas
        self.assertEqual(estimates.bias_level(0.0), estimates.BIAS_MAX_LEVEL)

    def test_date_d_episode_survit_a_une_publication(self):
        first = self._body(1000.0, 3.0)
        estimates._date_bias(first, None)
        entry = first["bias"][0]
        self.assertEqual((entry["biased_since"], entry["tier_since"]), (1000.0, 1000.0))
        # même sens, même palier : l'épisode est conservé
        second = self._body(2000.0, 3.2)
        estimates._date_bias(second, {"published_ts": 1500.0, "body": first})
        entry = second["bias"][0]
        self.assertEqual((entry["biased_since"], entry["tier_since"]), (1000.0, 1000.0))
        # même sens, palier supérieur : le début du palier change, pas le biais
        third = self._body(3000.0, 4.2)
        estimates._date_bias(third, {"published_ts": 2500.0, "body": second})
        entry = third["bias"][0]
        self.assertEqual((entry["biased_since"], entry["tier_since"]), (1000.0, 3000.0))
        # sens inverse : nouvel épisode
        fourth = self._body(4000.0, 0.3)
        estimates._date_bias(fourth, {"published_ts": 3500.0, "body": third})
        entry = fourth["bias"][0]
        self.assertEqual((entry["biased_since"], entry["tier_since"]), (4000.0, 4000.0))

    def test_republication_ne_change_pas_la_cle(self):
        body = self._body(1000.0, 3.0)
        estimates._date_bias(body, None)
        first = self._alert({"id": 12, "published_ts": 1100.0, "body": body})
        same = self._alert({"id": 13, "published_ts": 1200.0,
                            "body": dict(body, generated_ts=2000.0)})
        self.assertEqual(notify.key_of(first), notify.key_of(same))
        self.assertEqual(first["since"], same["since"])
        self.assertEqual(first["bias_level"], 1)
        # un palier supérieur est un nouvel épisode
        higher = self._body(3000.0, 4.2)
        estimates._date_bias(higher, {"published_ts": 2500.0, "body": body})
        tier = self._alert({"id": 14, "published_ts": 3100.0, "body": higher})
        self.assertNotEqual(notify.key_of(first), notify.key_of(tier))
        self.assertTrue(tier["reason"].startswith("bias-level-2"))
        # le sens inverse aussi
        over = self._body(4000.0, 0.3)
        estimates._date_bias(over, None)
        inverted = self._alert({"id": 15, "published_ts": 4100.0, "body": over})
        self.assertNotEqual(notify.key_of(first), notify.key_of(inverted))

    def test_retour_a_un_palier_deja_vu_est_un_nouvel_episode(self):
        # #3502 : le registre `notified` retient 30 jours ; un retour au
        # palier 2 après un passage au palier 1 doit donc produire une clé
        # neuve, sinon la seconde montée ne serait jamais renvoyée.
        first = self._body(1000.0, 3.0)
        estimates._date_bias(first, None)
        up = self._body(2000.0, 4.2)
        estimates._date_bias(up, {"published_ts": 1500.0, "body": first})
        down = self._body(3000.0, 3.2)
        estimates._date_bias(down, {"published_ts": 2500.0, "body": up})
        up2 = self._body(4000.0, 4.2)
        estimates._date_bias(up2, {"published_ts": 3500.0, "body": down})
        keys = [notify.key_of(self._alert({"id": i, "published_ts": ts, "body": body}))
                for i, (body, ts) in enumerate(
                    ((first, 1100.0), (up, 2100.0), (down, 3100.0), (up2, 4100.0)),
                    start=12)]
        self.assertEqual(len(set(keys)), 4)
        # republier le dernier état ne change pas la clé du dernier épisode
        again = self._body(5000.0, 4.2)
        estimates._date_bias(again, {"published_ts": 4500.0, "body": up2})
        self.assertEqual(notify.key_of(self._alert(
            {"id": 16, "published_ts": 5100.0, "body": again})), keys[-1])

    def test_table_ancienne_garde_la_cle_du_niveau_1(self):
        # une table publiée avant le correctif n'a pas de dates d'épisode : le
        # repli `published_ts` et l'absence de `reason` gardent la clé déjà
        # posée dans `notify` (pas de rafale au déploiement).
        alert = self._alert({"id": 13, "published_ts": NOW,
                             "body": self._body(NOW, 3.0)})
        self.assertEqual(alert["since"], NOW)
        self.assertIsNone(alert["reason"])
        tier = self._alert({"id": 13, "published_ts": NOW,
                            "body": self._body(NOW, 4.2)})
        self.assertEqual(tier["reason"], "bias-level-2")

    def test_contenu_identique_ignore_horodatage_et_dates(self):
        first = self._body(1000.0, 3.0)
        estimates._date_bias(first, None)
        second = self._body(2000.0, 3.0)
        self.assertEqual(estimates._content(first), estimates._content(second))
        second["bias"][0]["median_ratio"] = 4.0
        self.assertNotEqual(estimates._content(first), estimates._content(second))

    def test_compte_rendu_publication_inchangee(self):
        row = {"id": 13, "unchanged": True,
               "body": {"delivered": 1, "measured": 1, "groups": [],
                        "estimators": []}}
        text = estimates.format_published(row)
        self.assertIn("reference table #13 unchanged", text)
        self.assertIn("no new row, no new estimate_bias alert", text)


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

    def _three_biased_lots(self):
        for minutes in (20, 30, 40):
            lot = work.add(self.db, title="lot %d" % minutes, actor="orch",
                           estimate=minutes * 10, size="m")
            self._deliver(lot["id"], started_min_ago=minutes)

    def test_deux_publications_identiques_une_seule_ligne(self):
        # #3502 : republier la même table n'écrit rien et ne relance aucune
        # alerte (le contenu est identique hors horodatage de calcul).
        self._three_biased_lots()
        first = estimates.publish(self.db, actor="auditeur")
        second = estimates.publish(self.db, actor="auditeur")
        self.assertFalse(first["unchanged"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(
            self.db.query("SELECT count(*) AS n FROM estimate_references")[0]["n"], 1)
        self.assertEqual(storage.of(self.db).work.reference()["id"], first["id"])
        proc = self.mesh("work", "estimates", "--publish", "--actor", "auditeur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reference table #%s unchanged" % first["id"], proc.stdout)

    def test_publication_modifiee_garde_l_episode(self):
        # une table au contenu différent crée une ligne, mais un biais qui
        # reste dans le même sens garde sa date d'entrée (pas de nouvel
        # épisode, donc pas de nouvel envoi).
        self._three_biased_lots()
        first = estimates.publish(self.db, actor="auditeur")
        since = first["body"]["bias"][0]["biased_since"]
        lot = work.add(self.db, title="lot 50", actor="orch", estimate=500, size="m")
        self._deliver(lot["id"], started_min_ago=50)
        second = estimates.publish(self.db, actor="auditeur")
        self.assertFalse(second["unchanged"])
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["body"]["bias"][0]["biased_since"], since)


if __name__ == "__main__":
    unittest.main()
