# SPDX-License-Identifier: AGPL-3.0-only
"""#6670 : tableaux descriptifs et sensibilité (étude #6650, unité M5).

Témoins demandés par le lot : dénominateurs exacts, petites cellules et
inconnus gardés, aucune conclusion causale ; B croisés famille du relecteur ×
famille des auteurs (N de mty-amh-impl-06 sur l'étude), strates comparables,
cohortes abandonnée/ouverte distinctes, rouges reliés à leurs sources et aux
non-concluants, cas #3392.
"""
from __future__ import annotations

import json
import unittest

from ameesh import model_evaluation_report as report

from .support import PgTestCase
from .test_6660_model_evaluation_report import END, START, run, unit

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SRC_1 = "1" * 40
SRC_2 = "2" * 40


def mark(authors, sha, *, state="designated", risk=None):
    payload = {"state": state, "authors": list(authors),
               "unknown_authors": [], "freeze": {"id": 1, "sha": sha}}
    if risk is not None:
        payload["risk_trace"] = {
            "schema": "ameesh-review-designation-risk/1", "risk": risk,
            "required_from": {}, "policy": {},
            "freeze": {"id": 1, "sha": sha}, "base": ""}
    return "review-designation:1:1: " + json.dumps(payload)


def verdict(ident, lot, *, reviewer="codex", author="claude", sha=SHA_A,
            verdict_="blocked", actor="r1", branch=None, notes=None,
            risk=None):
    return {
        "id": ident, "lot": lot, "sha": sha, "actor": actor,
        "verdict": verdict_, "reviewer_family": reviewer,
        "branch": branch if branch is not None else "lot/%d" % lot,
        "at": START + 100 + ident,
        "notes": notes if notes is not None else [
            mark([{"name": "a", "family": author}], sha, risk=risk)],
    }


def verdicts():
    return [
        verdict(1, 201, reviewer="codex", author="claude", risk="sensitive"),
        # la même ligne B répétée : deux lignes, un B distinct
        verdict(2, 201, reviewer="codex", author="claude", risk="sensitive"),
        verdict(3, 201, reviewer="codex", author="claude", verdict_="ok",
                sha=SHA_B, notes=[mark([{"name": "a", "family": "claude"}],
                                       SHA_B, risk="light")]),
        verdict(4, 202, reviewer="claude", author="deepseek",
                verdict_="ok", risk="light"),
        # désignation d'un gel voisin seulement : auteurs inconnus
        verdict(5, 203, reviewer="deepseek", author="codex", sha=SHA_C,
                notes=[mark([{"name": "a", "family": "codex"}], SHA_A)]),
        # reçu absent : relecteur inconnu, jamais le registre courant
        verdict(6, 204, reviewer="", author="codex", verdict_="ok"),
        # porteur : à part
        verdict(7, 205, reviewer="codex", author="claude",
                branch="integration/paquet-1"),
        # auteurs mixtes ; relecteur claude d'une famille auteur
        verdict(8, 206, reviewer="claude", verdict_="ok",
                notes=[mark([{"name": "a", "family": "claude"},
                             {"name": "b", "family": "codex"}], SHA_A)]),
    ]


def others():
    def row(ident, cohort, **kw):
        values = dict(unit(ident, **kw))
        values.update(cohort=cohort, close_reason=kw.get("reason"),
                      finished_at=None, active_at=None)
        return values
    return [
        dict(row(301, "abandoned", family="deepseek"), close_reason="abandoned"),
        dict(row(302, "abandoned", family="claude", blocked=2),
             close_reason="superseded"),
        dict(row(303, "open", family="codex", notes=[], usd=None,
                 cost_rows=0)),
        dict(row(304, "open", family="claude", branch="packet/p1")),
    ]


def red_note(source, tests, *, target="main", base=None):
    value = {"source": source, "target": target, "tests": list(tests)}
    if base is not None:
        value["base"] = base
    return "packet-unit-red: " + json.dumps(value, sort_keys=True)


def reds():
    return [
        {"id": 1, "lot": 401, "note": red_note(SRC_1, ["t.a", "t.b"]),
         "branch": "lot/401", "source": SRC_1, "source_family": "codex",
         "runs": 3, "failure": 1, "success": 0, "inconclusive": 2},
        # même source, autre base : nouvelle mémoire, même source et tests
        {"id": 2, "lot": 401,
         "note": red_note(SRC_1, ["t.a"], base="f" * 40),
         "branch": "lot/401", "source": SRC_1, "source_family": "codex",
         "runs": 3, "failure": 1, "success": 0, "inconclusive": 2},
        # cible préfixée : même mémoire une fois normalisée
        {"id": 3, "lot": 401,
         "note": red_note(SRC_1, ["t.a"], target="origin/main"),
         "branch": "lot/401", "source": SRC_1, "source_family": "codex",
         "runs": 3, "failure": 1, "success": 0, "inconclusive": 2},
        # source sans reçu : famille inconnue
        {"id": 4, "lot": 402, "note": red_note(SRC_2, ["t.c"]),
         "branch": "lot/402", "source": SRC_2, "source_family": "",
         "runs": 0, "failure": 0, "success": 0, "inconclusive": 0},
        {"id": 5, "lot": 402, "note": "packet-unit-red: {illisible",
         "branch": "lot/402", "source": None, "source_family": "",
         "runs": None, "failure": None, "success": None,
         "inconclusive": None},
    ]


CI = [
    {"status": "success", "without_lot": False, "runs": 5, "heads": 4,
     "lots": 3, "failed_tests": 0, "inherited_tests": 0},
    {"status": "failure", "without_lot": False, "runs": 2, "heads": 2,
     "lots": 2, "failed_tests": 7, "inherited_tests": 1},
    {"status": "cancelled", "without_lot": False, "runs": 3, "heads": 2,
     "lots": 1, "failed_tests": 0, "inherited_tests": 0},
    {"status": "superseded", "without_lot": False, "runs": 4, "heads": 4,
     "lots": 2, "failed_tests": 0, "inherited_tests": 0},
    {"status": "failure", "without_lot": True, "runs": 9, "heads": 9,
     "lots": 0, "failed_tests": 0, "inherited_tests": 0},
]

REVIEW_COST = [
    {"harness": "codex", "cost_rows": 4, "void_rows": 1, "usd": 1.25,
     "lots": 2, "agents": 1},
    {"harness": "deepseek", "cost_rows": 0, "void_rows": 2, "usd": None,
     "lots": 1, "agents": 1},
]


def m5(**overrides):
    values = dict(verdicts=verdicts(), others=others(), reds=reds(), ci=CI,
                  review_cost=REVIEW_COST)
    values.update(overrides)
    return run(**values)["sensitivity"]


def cell(rows, **keys):
    return next(row for row in rows
                if all(row[key] == value for key, value in keys.items()))


class BCrossTest(unittest.TestCase):
    """B croisés famille du relecteur × famille des auteurs."""

    def test_denominateurs_exacts_cellules_marges_et_total(self):
        cross = m5()["b_cross"]
        total = cross["total"]
        self.assertEqual(total["verdict_lines"], 7)  # porteur à part
        self.assertEqual(total["blocked"], 3)
        for key in ("verdict_lines", "blocked", "ok"):
            self.assertEqual(sum(row[key] for row in cross["cells"]),
                             total[key])
            self.assertEqual(sum(row[key] for row in cross["by_risk"]),
                             total[key])
            self.assertEqual(sum(row[key] for row in cross["reviewer_margin"]),
                             total[key])
            self.assertEqual(sum(row[key] for row in cross["author_margin"]),
                             total[key])
        self.assertEqual(cross["carriers"]["verdict_lines"], 1)

    def test_lignes_repetees_et_b_distincts_separes(self):
        row = cell(m5()["b_cross"]["cells"], reviewer_family="codex",
                   author_family="claude")
        self.assertEqual(row["verdict_lines"], 3)
        self.assertEqual(row["blocked"], 2)
        self.assertEqual(row["blocked_distinct"], 1)
        self.assertEqual(row["lots"], 1)
        self.assertEqual(row["lots_blocked"], 1)
        self.assertEqual(row["blocked_rate"], round(2 / 3, 4))

    def test_gel_voisin_ne_donne_pas_la_famille_des_auteurs(self):
        cross = m5()["b_cross"]
        row = cell(cross["cells"], reviewer_family="deepseek")
        self.assertEqual(row["author_family"], "unknown")
        self.assertEqual(cross["author_unknown_lines"], 1)

    def test_recu_absent_donne_un_relecteur_inconnu_garde(self):
        cross = m5()["b_cross"]
        row = cell(cross["cells"], reviewer_family="unknown")
        self.assertEqual(row["author_family"], "codex")
        self.assertEqual(cross["reviewer_unknown_lines"], 1)

    def test_auteurs_mixtes_et_meme_famille_comptee(self):
        row = cell(m5()["b_cross"]["cells"], reviewer_family="claude",
                   author_family="mixed")
        self.assertEqual(row["same_family_lines"], 1)

    def test_risque_au_gel_trace_m2_sinon_inconnu(self):
        cross = m5()["b_cross"]
        by_risk = cross["by_risk"]
        self.assertEqual(cell(by_risk, reviewer_family="codex",
                              author_family="claude",
                              risk="sensitive")["verdict_lines"], 2)
        self.assertEqual(cell(by_risk, reviewer_family="codex",
                              author_family="claude",
                              risk="light")["verdict_lines"], 1)
        # marques sans trace (N-1) et gel sans marque : inconnu, pas léger
        self.assertEqual(cross["risk_unknown_lines"], 3)

    def test_petites_cellules_gardees_et_marquees(self):
        cells = m5()["b_cross"]["cells"]
        self.assertTrue(all(row["small_cell"] for row in cells))
        big = [verdict(i, 500 + i, reviewer="claude", author="codex",
                       verdict_="ok") for i in range(report.SMALL_CELL)]
        row = m5(verdicts=big)["b_cross"]["cells"][0]
        self.assertFalse(row["small_cell"])
        self.assertEqual(row["verdict_lines"], report.SMALL_CELL)

    def test_non_lu_reste_inconnu_lecture_vide_est_zero(self):
        cross = run()["sensitivity"]["b_cross"]
        self.assertIsNone(cross["cells"])
        self.assertTrue(cross["unknown"])
        empty = m5(verdicts=[])["b_cross"]
        self.assertEqual(empty["cells"], [])
        self.assertEqual(empty["total"]["verdict_lines"], 0)
        self.assertIsNone(empty["total"]["blocked_rate"])


class StrataTest(unittest.TestCase):
    """Strates (type, taille) des unités à ratio connu."""

    def test_seules_les_unites_a_ratio_connu_et_dénominateurs(self):
        strata = m5()["strata"]["integration"]
        units = sum(row["units"] for row in strata)
        known = sum(row["ratios_known"] for row in strata)
        unknown = sum(row["ratio_unknown"] for row in strata)
        self.assertEqual(units, known + unknown)
        for row in strata:
            self.assertEqual(sum(f["lots"] for f in row["families"]),
                             row["ratios_known"])

    def test_comparable_exige_deux_familles_nommees(self):
        units = [unit(1, family="claude"), unit(2, family="codex"),
                 unit(3, notes=[]),
                 unit(4, family="deepseek", type_="evolution")]
        strata = run(units=units)["sensitivity"]["strata"]["integration"]
        bug = cell(strata, type="bug", size="s")
        self.assertTrue(bug["comparable"])
        self.assertEqual(bug["named_families"], 2)
        evolution = cell(strata, type="evolution", size="s")
        self.assertFalse(evolution["comparable"])
        self.assertTrue(all(f["small_cell"] for f in bug["families"]))

    def test_cas_3392_attestation_avant_integration_hors_du_staging(self):
        # #108 (codex, bug, s) est attesté avant sa première intégration :
        # il reste dans la strate d'intégration, jamais dans celle du staging
        strata = m5()["strata"]
        staging = cell(strata["staging"], type="bug", size="s")
        integration = cell(strata["integration"], type="bug", size="s")
        self.assertNotIn("codex", [f["family"] for f in staging["families"]])
        self.assertIn("codex", [f["family"] for f in integration["families"]])
        self.assertEqual(sum(row["units"] for row in strata["staging"]),
                         run()["cohorts"]["staging"]["units"])


class OtherCohortsTest(unittest.TestCase):
    """Abandonnées et ouvertes : cohortes distinctes."""

    def test_cohortes_distinctes_hors_des_cohortes_m1(self):
        value = run(others=others())
        cohorts = value["sensitivity"]["other_cohorts"]
        self.assertEqual(cohorts["abandoned"]["summary"]["units"], 2)
        self.assertEqual(cohorts["open"]["summary"]["units"], 1)
        self.assertEqual(cohorts["carriers"], 1)
        self.assertEqual(value["cohorts"], run()["cohorts"])

    def test_sans_ratio_et_raisons_de_fermeture(self):
        cohorts = m5()["other_cohorts"]
        abandoned = cohorts["abandoned"]
        self.assertNotIn("median_ratio", abandoned["summary"])
        self.assertTrue(all("median_ratio" not in row
                            for row in abandoned["families"]))
        self.assertEqual(abandoned["close_reasons"],
                         {"abandoned": 1, "superseded": 1})
        self.assertEqual(abandoned["summary"]["lots_blocked"], 1)

    def test_famille_et_usd_inconnus_gardes(self):
        opened = m5()["other_cohorts"]["open"]
        self.assertEqual(opened["family_unknown"], 1)
        self.assertEqual(opened["summary"]["lots_with_usd"], 0)
        self.assertIsNone(opened["summary"]["usd"])

    def test_non_lu_reste_inconnu(self):
        cohorts = run()["sensitivity"]["other_cohorts"]
        self.assertIsNone(cohorts["abandoned"])
        self.assertTrue(cohorts["unknown"])


class RedsTest(unittest.TestCase):
    """Rouges reliés aux sources et aux non-concluants."""

    def test_deduplication_note_memoire_source_test(self):
        value = m5()["reds"]
        self.assertEqual(value["notes"], 5)
        self.assertEqual(value["unreadable"], 1)
        self.assertEqual(value["memories"], 3)
        self.assertEqual(value["sources"], 2)
        self.assertEqual(value["source_tests"], 3)

    def test_source_reliee_au_recu_et_aux_runs(self):
        value = m5()["reds"]
        first = cell(value["by_source"], source=SRC_1)
        self.assertEqual(first["family"], "codex")
        self.assertEqual(first["tests"], 2)
        self.assertEqual(first["lots"], [401])
        self.assertEqual(first["ci"], {"runs": 3, "failure": 1, "success": 0,
                                       "inconclusive": 2})
        second = cell(value["by_source"], source=SRC_2)
        self.assertEqual(second["family"], "unknown")
        self.assertEqual(value["source_family_unknown"], 1)

    def test_borne_basse_sans_taux_par_famille(self):
        value = m5()["reds"]
        self.assertTrue(value["lower_bound"])
        self.assertIsNone(value["rate_by_family"])
        self.assertEqual(m5(reds=[])["reds"]["notes"], 0)
        self.assertIsNone(run()["sensitivity"]["reds"]["notes"])

    def test_non_concluants_separes_des_echecs(self):
        ci = m5()["ci"]
        self.assertEqual(ci["project"]["failure"], 2)
        self.assertEqual(ci["project"]["inconclusive"], 7)
        self.assertEqual(ci["project"]["success"], 5)
        self.assertEqual(ci["project"]["runs"], 14)
        self.assertEqual(ci["without_lot"]["failure"], 9)
        self.assertEqual(ci["project"]["by_status"]["failure"]["inherited_tests"],
                         1)


class CostTest(unittest.TestCase):
    """Coût de revue et coût sans lot, à part des unités."""

    def test_cout_de_revue_inconnu_si_seules_des_lignes_ecartees(self):
        cost = m5()["review_cost"]
        self.assertEqual(cell(cost, harness="codex")["usd"], 1.25)
        self.assertIsNone(cell(cost, harness="deepseek")["usd"])
        self.assertIsNone(run()["sensitivity"]["review_cost"])

    def test_cout_sans_lot_du_mesh(self):
        mesh = [{"harness": "claude", "rows": 5, "with_lot": 2,
                 "cost_rows": 5, "usd": 3.0, "without_lot_rows": 3,
                 "without_lot_usd": 1.5},
                {"harness": "codex", "rows": 1, "with_lot": 1,
                 "cost_rows": 1, "usd": 1.0, "without_lot_rows": 0,
                 "without_lot_usd": None}]
        value = run(period_mesh=mesh)["sensitivity"]["without_lot"]
        self.assertEqual(cell(value, harness="claude"),
                         {"harness": "claude", "rows": 3, "usd": 1.5})
        self.assertEqual(cell(value, harness="codex")["usd"], None)


class NoCausalityTest(unittest.TestCase):
    """Aucune conclusion causale ni recommandation."""

    def test_rapport_descriptif(self):
        value = m5()
        self.assertFalse(value["causal"])
        self.assertIsNone(value["recommendation"])
        text = report.format_text(run(verdicts=verdicts(), others=others(),
                                      reds=reds(), ci=CI)).lower()
        for word in ("meilleur", "recommand", "plus fiable", "cause"):
            self.assertNotIn(word, text.replace("ne conclut aucune causalité",
                                                ""))
        self.assertIn("b croisés", text)
        self.assertIn("borne basse", text)

    def test_texte_sans_sources_m5_dit_inconnu(self):
        text = report.format_text(run())
        self.assertIn("abandonnées : inconnu", text)


class DatabaseReadTest(PgTestCase):
    """Les requêtes M5 s'exécutent sur le schéma réel, en lecture seule."""

    def test_read_croise_cohortes_et_rouges(self):
        lot, abandoned, opened = 667001, 667002, 667003
        for ident, state, branch in ((lot, "qa", "lot/667001"),
                                     (abandoned, "qa", "lot/667002"),
                                     (opened, "build", "lot/667003")):
            self.db.execute(
                "INSERT INTO work_items (id, type, source, title, app, state, "
                "branch, change_size) VALUES (%s, 'bug', 'test', 'M5', "
                "'ameesh', %s, %s, 's')", (ident, state, branch))
            self.db.execute(
                "UPDATE work_items SET started_at = to_timestamp(%s) "
                "WHERE id = %s", (START + 3600, ident))
        self.db.execute(
            "UPDATE work_items SET state = 'closed', close_reason = "
            "'abandoned', closed_at = to_timestamp(%s) WHERE id = %s",
            (START + 7200, abandoned))
        milestone = self.db.query(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, "
            "actor, verdict) VALUES (%s, 'verdict', to_timestamp(%s), %s, "
            "'relecteur', 'blocked') RETURNING id",
            (lot, START + 6000, SHA_A))
        self.db.execute(
            "INSERT INTO provenance_receipts (subject, sha, actor, session, "
            "turn, harness) VALUES (%s, %s, 'relecteur', 's1', 't1', 'codex')",
            ("milestone:%s" % milestone[0]["id"], SHA_A))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, "
            "created_at) VALUES (%s, 'qa', %s, 'auteur', to_timestamp(%s))",
            (lot, mark([{"name": "auteur", "family": "claude"}], SHA_A,
                       risk="light"), START + 5000))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, "
            "created_at) VALUES (%s, 'qa', %s, 'paquet', to_timestamp(%s))",
            (lot, red_note(SRC_1, ["t.a"]), START + 6500))
        self.db.execute(
            "INSERT INTO ci_runs (repo, run_id, head_sha, branch, status, "
            "work_item_id, first_seen_at) VALUES ('manaty/ameesh', 1, %s, "
            "'lot/667001', 'cancelled', %s, to_timestamp(%s))",
            (SRC_1, lot, START + 6400))
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, work_item_id, recorded_at, "
            "usd, input_tokens, cached_input_tokens, output_tokens, turn_id) "
            "VALUES ('relecteur', 'codex', %s, to_timestamp(%s), 0.5, 10, 0, "
            "1, 't1')", (lot, START + 5900))
        value = report.read(self.db, project="ameesh",
                            start=report.iso_z(START),
                            end=report.iso_z(END))
        sensitivity = value["sensitivity"]
        row = cell(sensitivity["b_cross"]["by_risk"], reviewer_family="codex")
        self.assertEqual((row["author_family"], row["risk"], row["blocked"]),
                         ("claude", "light", 1))
        others_value = sensitivity["other_cohorts"]
        self.assertEqual(others_value["abandoned"]["summary"]["units"], 1)
        # le lot jugé n'est pas intégré : ouvert, à côté de #667003
        self.assertEqual(others_value["open"]["summary"]["units"], 2)
        red = sensitivity["reds"]["by_source"][0]
        self.assertEqual(red["ci"]["inconclusive"], 1)
        self.assertEqual(sensitivity["ci"]["project"]["inconclusive"], 1)
        self.assertEqual(cell(sensitivity["review_cost"],
                              harness="codex")["usd"], 0.5)


if __name__ == "__main__":
    unittest.main()
