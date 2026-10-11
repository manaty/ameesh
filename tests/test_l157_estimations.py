# SPDX-License-Identifier: AGPL-3.0-only
"""L157 : durée estimée de chaque lot, durée réelle mesurée, écarts pour
l'auditeur ; et la CI qui saute la suite sur une PR de version seule.

Tests sans base (lecture d'une durée, vue d'un lot, quantiles, rapport,
en-tête d'issue, détection d'une PR de version), puis sur Postgres réel
(création gardée, `work plan --estimate`, début mesuré, `work show`,
`work estimates`, `projects`, `mail send --new-lot --estimate`).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import time
import unittest

from ameesh import estimates, plan_github, projects, registry, storage, work

from .support import PgTestCase

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("version_only",
                                               REPO / "scripts" / "version-only.py")
version_only = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_only)

NOW = 1_800_000_000.0


class ParseTest(unittest.TestCase):

    def test_formes_acceptees(self):
        cas = {"90": 90, "90m": 90, "90min": 90, "2h": 120, "1h30": 90, "1h30m": 90,
               "1,5h": 90, "1.5h": 90, "2d": 2880, "2j": 2880, " 3 h ": 180, "45": 45}
        for text, minutes in cas.items():
            self.assertEqual(estimates.parse(text), minutes, text)
        self.assertEqual(estimates.parse(45), 45)

    def test_refus(self):
        for bad in ("", "0", "0h", "abc", "-2h", "2 semaines", "61d", True):
            with self.assertRaises(estimates.EstimateError, msg=repr(bad)):
                estimates.parse(bad)
        # une EstimateError est une WorkError : la CLI la rend en « erreur : … »
        self.assertTrue(issubclass(estimates.EstimateError, work.WorkError))

    def test_libelles(self):
        self.assertEqual(estimates.label(45), "45 min")
        self.assertEqual(estimates.label(120), "2 h")
        self.assertEqual(estimates.label(90), "1 h 30")
        self.assertEqual(estimates.compact(45), "45m")
        self.assertEqual(estimates.compact(90), "1h30")
        self.assertEqual(estimates.compact(120), "2h")
        self.assertEqual(estimates.compact(3 * 1440), "3j")
        self.assertEqual(estimates.ratio_text(1.333), "×1,33")
        self.assertEqual(estimates.ratio_text(2.0), "×2")


class ViewTest(unittest.TestCase):

    def test_estime_pas_commence(self):
        v = estimates.view({"estimate_minutes": 120, "state": "intake"}, now=NOW)
        self.assertEqual((v["minutes"], v["actual_minutes"], v["elapsed_minutes"], v["ratio"]),
                         (120, None, None, None))
        self.assertEqual(v["label"], "estimé 2 h")
        self.assertEqual(estimates.cell(v), "2h")

    def test_en_cours_et_depasse(self):
        item = {"estimate_minutes": 60, "started_ts": NOW - 5400, "state": "build"}
        v = estimates.view(item, now=NOW)
        self.assertEqual((v["elapsed_minutes"], v["ratio"], v["overrun"]), (90.0, 1.5, True))
        self.assertEqual(v["label"], "estimé 1 h · en cours depuis 1 h 30 (DÉPASSÉ, ×1,5)")
        self.assertEqual(estimates.cell(v), "1h30/1h!")
        v = estimates.view(dict(item, started_ts=NOW - 1800), now=NOW)
        self.assertFalse(v["overrun"])
        self.assertEqual(estimates.cell(v), "30m/1h")

    def test_livre_avec_ecart(self):
        item = {"estimate_minutes": 120, "started_ts": NOW - 9600, "state": "merged"}
        v = estimates.view(item, merged_ts=NOW, now=NOW + 999)
        self.assertEqual((v["actual_minutes"], v["gap_minutes"], v["ratio"]),
                         (160.0, 40.0, 1.333))
        self.assertIsNone(v["elapsed_minutes"])
        self.assertEqual(v["label"], "estimé 2 h · réel 2 h 40 (+40 min, ×1,33)")
        plus_vite = estimates.view(dict(item, started_ts=NOW - 3600), merged_ts=NOW)
        self.assertEqual(plus_vite["label"], "estimé 2 h · réel 1 h (−1 h, ×0,5)")

    def test_sans_estimation_ou_sans_debut(self):
        v = estimates.view({"started_ts": NOW - 600, "state": "build"}, now=NOW)
        self.assertEqual(v["label"], "sans estimation · en cours depuis 10 min")
        self.assertEqual(estimates.cell(v), "10m/?")
        self.assertEqual(estimates.cell(estimates.view({"state": "intake"})), "—")
        v = estimates.view({"estimate_minutes": 30, "state": "merged"}, merged_ts=NOW)
        self.assertEqual(v["label"], "estimé 30 min · réel inconnu (début non mesuré)")


def _row(i, type_, by, est, actual, *, late=False, started=True, revisions=1):
    merged = NOW + i
    return {"id": i, "type": type_, "app": "p", "title": "lot %d" % i,
            "started_ts": merged - actual * 60 if started else None, "merged_ts": merged,
            "estimate_minutes": est, "estimate_by": by, "estimate_source": None,
            "late": late, "revisions": revisions}


class ReportTest(unittest.TestCase):

    def test_quantiles(self):
        self.assertEqual(estimates.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(estimates.quantile([4, 1, 3, 2], 0.8), 3.4)
        self.assertEqual(estimates.quantile([2], 0.8), 2.0)
        self.assertIsNone(estimates.quantile([], 0.5))

    def test_par_type_et_par_auteur(self):
        rows = [_row(1, "evolution", "orch", 60, 60), _row(2, "evolution", "orch", 60, 120),
                _row(3, "bug", "human:o", 60, 30), _row(4, "evolution", "dev1", 60, 90),
                _row(5, "bug", "orch", 60, 600, late=True),
                _row(6, "bug", "orch", None, 60), _row(7, "bug", "orch", 60, 60, started=False),
                _row(8, "evolution", "orch", 120, 120, revisions=3)]
        rep = estimates.build(rows, app="p", now=NOW)
        self.assertEqual(rep["schema"], "ameesh-estimates/1")
        self.assertEqual((rep["delivered"], rep["overall"]["lots"]), (8, 5))
        by_type = {g["key"]: g for g in rep["by_type"]}
        self.assertEqual(by_type["evolution"]["lots"], 4)
        self.assertEqual(by_type["evolution"]["median"], 1.25)       # 1, 2, 1.5, 1
        self.assertEqual(by_type["bug"]["lots"], 1)                  # la tardive à part
        by_author = {g["key"]: g for g in rep["by_author"]}
        self.assertEqual(by_author["orch"]["lots"], 3)
        self.assertEqual(by_author["orch"]["p80"], 1.6)
        self.assertEqual(rep["excluded"]["no_estimate"], 1)
        self.assertEqual(rep["excluded"]["no_start"], 1)
        self.assertEqual(rep["revised"], 1)
        tardives = {g["key"]: g for g in rep["late"]}
        self.assertEqual(tardives["tardive"]["median"], 10.0)
        text = estimates.format_report(rep)
        for attendu in ("PAR TYPE DE LOT", "PAR AUTEUR D'ESTIMATION", "1 estimé(s) après le "
                        "début", "1 livré(s) sans estimation", "1 sans début mesuré",
                        "1 lot(s) ré-estimé(s)", "×1,25"):
            self.assertIn(attendu, text)
        self.assertIn("rien à comparer", estimates.format_report(estimates.build([])))


class IssueHeaderTest(unittest.TestCase):
    """L126 : l'en-tête de l'issue d'un lot dit la durée estimée, les dates
    prévues et, une fois livré, le réel — des nombres et des dates seulement."""

    def view(self, item, public=True):
        guard = plan_github.make_guard(plan_github.Settings())
        return plan_github.lot_view(item, public=public, guard=guard, project="p", repo="o/r")

    def test_en_tete(self):
        item = {"id": 12, "title": "Durées", "type": "evolution", "state": "build",
                "estimate_minutes": 120, "estimate_source": "conception", "estimate_by": "w1",
                "planned_start": "2026-10-12", "planned_delivery": "2026-10-15"}
        body = self.view(item).body
        self.assertIn("Durée estimée 2 h · prévu : début 12/10/2026, livraison 15/10/2026",
                      body)
        self.assertNotIn("conception", body)       # la source n'est pas publiée
        self.assertNotIn("réel", body)
        done = dict(item, state="merged", started_ts=NOW - 9600, merged_ts=NOW)
        self.assertIn("réel 2 h 40 (+40 min, ×1,33)", self.view(done).body)
        # ni estimation ni date : l'en-tête ne change pas (aucune réécriture d'issue)
        bare = {"id": 13, "title": "x", "type": "bug", "state": "build"}
        self.assertIsNone(plan_github._plan_line(bare))
        self.assertNotIn("Durée", self.view(bare, public=False).body)


class VersionOnlyTest(unittest.TestCase):
    """CI (L157) : une PR qui ne change que le numéro de version saute la suite."""

    PATCH = ('diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n'
             '+++ b/pyproject.toml\n@@ -7 +7 @@\n-version = "1.7.0"\n+version = "1.7.1"\n')

    def test_version_seule(self):
        ok, why = version_only.version_only(["pyproject.toml"], self.PATCH)
        self.assertTrue(ok, why)

    def test_autre_chose(self):
        cas = [
            (["pyproject.toml", "src/ameesh/work.py"], self.PATCH),
            (["README.md"], ""),
            (["pyproject.toml"], self.PATCH + '@@ -20 +20 @@\n-  "x"\n+  "y"\n'),
            (["pyproject.toml"], self.PATCH.replace('+version = "1.7.1"', '+name = "x"')),
            (["pyproject.toml"], self.PATCH.replace('"1.7.1"', '"1.7.0"')),
            ([], ""),
        ]
        for names, patch in cas:
            ok, why = version_only.version_only(names, patch)
            self.assertFalse(ok, (names, patch))

    def test_workflow(self):
        text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/version-only.py HEAD^1 HEAD", text)
        # les trois groupes de parts dépendent du constat ; le bilan l'accepte
        self.assertEqual(text.count("if: needs.changes.outputs.version_only != 'true'"), 3)
        self.assertIn("needs: [changes, psycopg, psql, platform-macos]", text)


class EstimatesPgTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def test_creation_gardee_agent_refuse_humain_averti(self):
        proc = self.mesh("work", "add", "--title", "sans estimation", "--actor", "orch")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("estimation obligatoire", proc.stderr)
        self.assertIn("--estimate 2h", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])
        proc = self.mesh("work", "add", "--title", "estimé", "--actor", "orch",
                         "--estimate", "1h30", "--estimate-source", "conception")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("estimé 1 h 30", proc.stdout)
        item = work.list_items(self.db)[0]
        self.assertEqual((item["estimate_minutes"], item["estimate_source"],
                          item["estimate_by"]), (90, "conception", "orch"))
        self.assertIsNotNone(item["estimate_ts"])
        hist = storage.of(self.db).work.estimates(item["id"])
        self.assertEqual([(h["minutes"], h["estimated_by"]) for h in hist], [(90, "orch")])
        self.assertIn("estimation : 1 h 30", work.events(self.db, item["id"])[0]["note"])
        # un humain : avertissement, pas refus
        proc = self.mesh("work", "add", "--title", "par un humain", "--actor", "human:o")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot sans durée estimée", proc.stderr)
        self.assertIn("ameesh work plan", proc.stderr)
        # une estimation illisible est refusée avant toute création
        proc = self.mesh("work", "add", "--title", "x", "--estimate", "bientôt")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("estimation illisible", proc.stderr)
        self.assertEqual(len(work.list_items(self.db)), 2)

    def test_plan_estimate_et_revision(self):
        lot = work.add(self.db, title="à estimer")
        proc = self.mesh("work", "plan", str(lot["id"]), "--estimate", "2h", "--source",
                         "conversation", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("estimé 2 h", proc.stdout)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["estimate_minutes"], item["estimate_source"]),
                         (120, "conversation"))
        plan = storage.of(self.db).roadmap.item_plans([lot["id"]])[0]
        self.assertIsNone(plan["planned_source"])       # aucune date touchée
        # avec des dates : les deux, en une commande
        proc = self.mesh("work", "plan", str(lot["id"]), "--estimate", "3h", "--fin",
                         "2026-10-20", "--estimate-source", "revue", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["plan"]["planned_end"], out["estimate"]["minutes"]),
                         ("2026-10-20", 180))
        hist = storage.of(self.db).work.estimates(lot["id"])
        self.assertEqual([h["minutes"] for h in hist], [120, 180])
        # sur une fiche : refusé ; lot livré : estimation figée
        proc = self.mesh("work", "plan", "fiche-x", "--estimate", "2h")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("se pose sur une tâche", proc.stderr)
        work.move(self.db, lot["id"], "build")
        work.merged(self.db, lot["id"], sha="abcdef1")
        with self.assertRaises(estimates.EstimateError):
            estimates.set_estimate(self.db, lot["id"], "1h")

    def test_debut_mesure_build_ou_premier_tour(self):
        a = work.add(self.db, title="par build", estimate="1h")
        self.assertIsNone(a["started_ts"])
        moved = work.move(self.db, a["id"], "build")
        self.assertIsNotNone(moved["started_ts"])
        work.move(self.db, a["id"], "qa")
        again = work.move(self.db, a["id"], "build")
        self.assertEqual(again["started_ts"], moved["started_ts"])     # une seule fois
        b = work.add(self.db, title="par un tour", assignee="dev1", estimate="1h")
        c = work.add(self.db, title="d'un autre", assignee="orch", estimate="1h")
        self.assertEqual(estimates.mark_started(self.db, "dev1", [str(b["id"]), c["id"], "x"]),
                         [b["id"]])
        self.assertEqual(estimates.mark_started(self.db, "dev1", [b["id"]]), [])
        self.assertIsNotNone(work.get(self.db, b["id"])["started_ts"])
        self.assertIsNone(work.get(self.db, c["id"])["started_ts"])
        self.assertIn("début mesuré : premier tour de dev1",
                      work.events(self.db, b["id"])[0]["note"])

    def _deliver(self, lot_id, *, started_min_ago, merged_min_ago=0, estimated_before=True):
        """Livre le lot, début et fusion antidatés ; l'estimation posée à la
        création est antidatée avant le début (`estimated_before`)."""
        if work.get(self.db, lot_id)["state"] == "intake":
            work.move(self.db, lot_id, "build")
        work.merged(self.db, lot_id, sha="abcdef1")
        self.db.execute(
            "UPDATE work_items SET started_at = now() - make_interval(mins => %s)"
            " WHERE id = %s", (int(started_min_ago), int(lot_id)))
        if estimated_before:
            self.db.execute(
                "UPDATE work_item_estimates SET at = now() - make_interval(mins => %s)"
                " WHERE work_item_id = %s", (int(started_min_ago) + 5, int(lot_id)))
        self.db.execute(
            "UPDATE work_item_milestones SET at = now() - make_interval(mins => %s)"
            " WHERE work_item_id = %s AND kind = 'merged'", (int(merged_min_ago), int(lot_id)))

    def test_show_et_ecarts(self):
        a = work.add(self.db, title="A", type="evolution", actor="orch", estimate="2h",
                     app="p")
        self._deliver(a["id"], started_min_ago=160)
        b = work.add(self.db, title="B", type="bug", actor="dev1", estimate="1h", app="p")
        self._deliver(b["id"], started_min_ago=30)
        # estimée après le début : à part
        c = work.add(self.db, title="C", type="bug", app="p")
        work.move(self.db, c["id"], "build")
        self.db.execute("UPDATE work_items SET started_at = now() - interval '2 hours'"
                        " WHERE id = %s", (c["id"],))
        estimates.set_estimate(self.db, c["id"], "30m", actor="orch")
        self._deliver(c["id"], started_min_ago=120, estimated_before=False)
        d = work.add(self.db, title="D", app="autre")           # sans estimation
        self._deliver(d["id"], started_min_ago=10)

        proc = self.mesh("work", "show", str(a["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("durée    : estimé 2 h · réel 2 h 40 (+40 min, ×1,33)", proc.stdout)
        proc = self.mesh("work", "show", str(a["id"]), "--json")
        shown = json.loads(proc.stdout)
        self.assertEqual((shown["estimate"]["minutes"], shown["estimate"]["actual_minutes"]),
                         (120, 160.0))

        rep = estimates.report(self.db, app="p")
        self.assertEqual((rep["delivered"], rep["overall"]["lots"]), (3, 2))
        self.assertEqual({g["key"]: g["median"] for g in rep["by_type"]},
                         {"evolution": 1.333, "bug": 0.5})
        self.assertEqual({g["key"] for g in rep["by_author"]}, {"orch", "dev1"})
        tardive = next(g for g in rep["late"] if g["key"] == "tardive")
        self.assertEqual(tardive["lots"], 1)
        proc = self.mesh("work", "estimates", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        everything = json.loads(proc.stdout)
        self.assertEqual(everything["excluded"]["no_estimate"], 1)
        proc = self.mesh("work", "estimates", "--app", "p")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PAR AUTEUR D'ESTIMATION", proc.stdout)

    def test_projects_colonne_duree(self):
        lot = work.add(self.db, title="Vue", assignee="dev1", estimate="1h")
        estimates.mark_started(self.db, "dev1", [lot["id"]])
        self.db.execute("UPDATE work_items SET started_at = now() - interval '90 minutes'"
                        " WHERE id = %s", (lot["id"],))
        view = projects.snapshot(self.db, now=time.time())
        agent = next(a for p in view["projects"] for a in p["agents"] if a["name"] == "dev1")
        self.assertEqual(agent["lot"]["id"], lot["id"])
        self.assertEqual(agent["lot_estimate"]["minutes"], 60)
        self.assertTrue(agent["lot_estimate"]["overrun"])
        text = projects.format_text(view, 120)
        self.assertIn("DURÉE", text)
        self.assertIn("1h30/1h!", text)

    def test_mail_new_lot(self):
        env = self.env(AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.cli("send", "dev1", "Nouveau travail.", "--from", "orch", "--new-lot",
                        "Sans durée", env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("message non déposé", proc.stderr)
        self.assertIn("estimation obligatoire", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])
        proc = self.cli("send", "dev1", "Nouveau travail.", "--from", "orch", "--new-lot",
                        "Avec durée", "--estimate", "45m", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.list_items(self.db)[0]
        self.assertEqual((item["title"], item["estimate_minutes"], item["estimate_by"]),
                         ("Avec durée", 45, "orch"))
        proc = self.cli("send", "dev1", "x", "--from", "orch", "--estimate", "45m", env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--estimate accompagne --new-lot", proc.stderr)


if __name__ == "__main__":
    unittest.main()
