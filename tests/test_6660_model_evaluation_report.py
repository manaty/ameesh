# SPDX-License-Identifier: AGPL-3.0-only
"""#6660 : rapport d'évaluation des modèles (étude #6650, unité M1).

Témoins demandés par le lot : fenêtre UTC fixe de 14 jours sans limite
silencieuse à 1000, porteurs séparés et deux cohortes, SHA et famille
historiques (`mixed`/`unknown`), cache Codex, compte forfait ≠ facture,
donnée absente ≠ zéro, fixtures des requêtes de l'étude.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from ameesh import model_evaluation_report as report

from .support import PgTestCase

END = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
START = END - 14 * 86400


def designation(authors, *, state="designated", unknown=(), families=None):
    payload = {"state": state,
               "authors": list(authors),
               "unknown_authors": list(unknown)}
    if families is not None:
        payload["families"] = list(families)
    return "review-designation:1:1: " + json.dumps(payload)


def unit(ident, *, family="claude", branch=None, type_="bug", size="s",
         started=START + 3600, finished=START + 7200, active=START + 9000,
         estimate=60, notes=None, turns=2, usd=1.0, cost_rows=2,
         void_rows=0, prompt=100.0, output=20.0, blocked=0, heads=1,
         input_tokens=80.0, cached=20.0, cache_write=5.0):
    """Une ligne telle que `_UNITS_SQL` la rend (noms de colonnes SQL)."""
    return {
        "id": ident,
        "type": type_,
        "change_size": size,
        "branch": branch if branch is not None else "lot/%d" % ident,
        "started_at": started,
        "finished_at": finished,
        "active_at": active,
        "estimate_minutes": estimate,
        "estimated_by": "auditeur",
        "notes": notes if notes is not None else [
            designation([{"name": "auteur", "family": family}])],
        "turns": turns,
        "usd": usd,
        "cost_rows": cost_rows,
        "void_rows": void_rows,
        "prompt": prompt,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": output,
        "model_unknown": 0,
        "turn_ids": turns or 0,
        "active_turns": turns,
        "active_usd": usd,
        "active_cost_rows": cost_rows,
        "active_void_rows": void_rows,
        "active_prompt": prompt,
        "active_input_tokens": input_tokens,
        "active_cached_input_tokens": cached,
        "active_cache_write_tokens": cache_write,
        "active_output_tokens": output,
        "active_model_unknown": 0,
        "active_turn_ids": turns or 0,
        "blocked": blocked,
        "distinct_heads": heads,
    }


def base_units():
    """Deux cohortes, deux porteurs, une famille mixte et une inconnue."""
    return [
        unit(101, family="claude", type_="bug", size="s"),
        unit(102, family="codex", type_="evolution", size=None,
             active=None, turns=0, usd=None, cost_rows=0, prompt=None,
             output=None, input_tokens=None, cached=None, cache_write=None),
        unit(103, family="deepseek", type_="improvement", size="m",
             started=START + 3600, finished=START + 36000, estimate=180,
             active=START + 43200, turns=3, usd=3.0, blocked=1, heads=3),
        unit(104, family="claude", branch="integration/paquet-1",
             type_="evolution", size="l", turns=1, usd=0.25,
             active=START + 8000),
        unit(105, family="mixed", type_="bug", size="s",
             notes=[designation([
                 {"name": "a", "family": "claude"},
                 {"name": "b", "family": "codex"}])],
             turns=1, usd=0.5, prompt=50.0, output=10.0,
             input_tokens=40.0, cached=10.0, cache_write=0.0),
        unit(106, family="unknown", type_="evolution", size=None,
             started=None, estimate=None, turns=None, usd=None, cost_rows=0,
             prompt=None, output=None, input_tokens=None, cached=None,
             cache_write=None, heads=0,
             notes=[designation([{"name": "c", "family": ""}],
                                unknown=["d"])]),
        unit(107, family="claude", type_="bug", size="s",
             started=START - 90000, finished=START - 86400,
             active=START + 5000, estimate=60, turns=1, usd=0.2,
             prompt=20.0, output=4.0, input_tokens=15.0, cached=5.0,
             cache_write=0.0),
        # attestation avant l'intégration : candidate, mais pas de staging
        unit(108, family="codex", type_="bug", size="s",
             finished=START + 7200, active=None, turns=4, usd=2.0,
             prompt=200.0, output=40.0, input_tokens=150.0, cached=50.0,
             cache_write=0.0, heads=2),
        unit(109, family="codex", branch="packet/p9", type_="evolution",
             size="m", turns=2, usd=0.75),
    ]


STAGED = [
    {"event_id": 1, "lot": 101, "at": START + 9000},
    {"event_id": 1, "lot": 103, "at": START + 43200},
    {"event_id": 1, "lot": 104, "at": START + 8000},
    {"event_id": 2, "lot": 107, "at": START + 5000},
    {"event_id": 2, "lot": 108, "at": START + 6000},
    {"event_id": 2, "lot": 999, "at": START + 5000},
    # un événement sans identifiant est une attestation réelle, comptée
    {"event_id": 3, "lot": None, "at": START + 1000},
]

PERIOD = [
    {"harness": "codex", "rows": 3, "with_lot": 3, "void_rows": 1,
     "cost_rows": 2, "usd": 2.0, "input_tokens": 300.0,
     "cached_input_tokens": 120.0, "cache_write_tokens": 0.0,
     "output_tokens": 60.0, "prompt": 300.0, "model_unknown": 0,
     "billing_account_unknown": 1, "turn_ids": 3},
    {"harness": "claude", "rows": 2, "with_lot": 2, "void_rows": 0,
     "cost_rows": 2, "usd": 1.0, "input_tokens": 200.0,
     "cached_input_tokens": 80.0, "cache_write_tokens": 10.0,
     "output_tokens": 40.0, "prompt": 280.0, "model_unknown": 0,
     "billing_account_unknown": 0, "turn_ids": 2},
]

REVIEWS = [
    {"kind": "verdict", "verdict": "blocked", "family": "claude",
     "n": 2, "lots": 2},
    {"kind": "verdict", "verdict": "ok", "family": "claude",
     "n": 3, "lots": 2},
    {"kind": "verdict", "verdict": "blocked", "family": "codex",
     "n": 1, "lots": 1},
    {"kind": "frozen", "verdict": "", "family": "deepseek",
     "n": 5, "lots": 3},
    {"kind": "verdict", "verdict": "ok", "family": "unknown",
     "n": 1, "lots": 1},
]

DESIGNATED = [
    {"family": "claude", "mails": 6, "seats": 4},
    {"family": "codex", "mails": 2, "seats": 2},
    {"family": "", "mails": 7, "seats": 7},
]


def run(**overrides):
    values = dict(
        project="ameesh", at=END, start=START, end=END,
        units=base_units(), staged=list(STAGED),
        period_project=list(PERIOD), period_mesh=list(PERIOD),
        reviews=list(REVIEWS), designated=list(DESIGNATED),
    )
    values.update(overrides)
    return report.build(**values)


def family(report_value, name, cohort="staging"):
    return next(row for row in report_value["families"][cohort]
                if row["name"] == name)


class WindowTest(unittest.TestCase):
    """Fenêtre UTC fixe, quatorze jours ou plus, sans troncature muette."""

    def test_fenetre_de_quatorze_jours_conservee_a_la_seconde(self):
        start, end = report.window(start="2026-10-27T00:00:00Z",
                                   end="2026-11-10T00:00:00Z")
        self.assertEqual(report.iso_z(start), "2026-10-27T00:00:00Z")
        self.assertEqual(report.iso_z(end), "2026-11-10T00:00:00Z")
        self.assertEqual(end - start, 14 * 86400)

    def test_defaut_quatorze_jours_et_bornes_explicites(self):
        with mock.patch("ameesh.model_evaluation_report.time.time",
                        return_value=END):
            start, end = report.window()
        self.assertEqual(report.iso_z(end), "2026-11-10T00:00:00Z")
        self.assertEqual(end - start, 14 * 86400)

    def test_start_seul_ajoute_les_jours_et_end_seul_les_retire(self):
        start, end = report.window(start="2026-10-27T00:00:00Z", days=14)
        self.assertEqual(report.iso_z(end), "2026-11-10T00:00:00Z")
        start, end = report.window(end="2026-11-10T00:00:00Z", days=14)
        self.assertEqual(report.iso_z(start), "2026-10-27T00:00:00Z")

    def test_decalage_horaire_ramene_en_utc(self):
        start, _ = report.window(start="2026-10-27T01:00:00+01:00",
                                 end="2026-11-10T00:00:00Z")
        self.assertEqual(report.iso_z(start), "2026-10-27T00:00:00Z")

    def test_instant_sans_fuseau_ou_fenetre_inversee_refuses(self):
        with self.assertRaises(report.ModelEvaluationReportError):
            report.parse_instant("2026-10-27T00:00:00")
        with self.assertRaises(report.ModelEvaluationReportError):
            report.window(start="2026-11-10T00:00:00Z",
                          end="2026-10-27T00:00:00Z")
        with self.assertRaises(report.ModelEvaluationReportError):
            report.window(start="2026-11-10T00:00:00Z",
                          end="2026-11-10T00:00:00Z")

    def test_days_borne_sans_limite_silencieuse_de_1000_lignes(self):
        for days in (0, -1, report.MAX_DAYS + 1):
            with self.assertRaises(report.ModelEvaluationReportError):
                report.window(days=days)
        # la fenêtre elle-même n'a pas de plafond de lignes : `read` ne
        # fabrique aucun `LIMIT 1000`, et les requêtes de l'étude sont lues
        # entières.
        self.assertNotIn("LIMIT 1000", report._UNITS_SQL)
        self.assertNotIn("LIMIT 1000", report._PERIOD_SQL)


class ReadOnlyReadTest(unittest.TestCase):
    """`read` ouvre une transaction READ ONLY, sans limite de lignes."""

    class FakeTx:
        def __init__(self):
            self.statements = []
            self.queries = []

        def execute(self, sql, params=()):
            self.statements.append((sql, params))

        def query(self, sql, params=()):
            self.statements.append((sql, params))
            return [{"present": False}]

        def query_batch(self, queries):
            self.queries = list(queries)
            return [[] for _ in queries]

    class FakeDb:
        def __init__(self, tx):
            self.tx = tx

        def transaction(self):
            class _Context:
                def __init__(self, tx):
                    self.tx = tx

                def __enter__(self):
                    return self.tx

                def __exit__(self, *exc):
                    return False

            return _Context(self.tx)

    def test_transaction_read_only_et_aucun_limit_1000(self):
        tx = self.FakeTx()
        report.read(self.FakeDb(tx), project="ameesh",
                    now=END, start="2026-10-27T00:00:00Z",
                    end="2026-11-10T00:00:00Z")
        self.assertIn(("SET TRANSACTION READ ONLY", ()), tx.statements)
        self.assertEqual(len(tx.queries), 11)
        for sql, _params in tx.queries:
            self.assertNotIn("LIMIT 1000", sql)
        self.assertFalse(any("review_finding_resolutions" in sql
                             for sql, _params in tx.queries))

    def test_le_journal_des_resolutions_est_lu_s_il_existe(self):
        tx = self.FakeTx()
        tx.query = lambda sql, params=(): [{"present": True}]
        report.read(self.FakeDb(tx), project="ameesh",
                    now=END, start="2026-10-27T00:00:00Z",
                    end="2026-11-10T00:00:00Z")
        self.assertEqual(len(tx.queries), 12)
        self.assertTrue(any("review_finding_resolutions" in sql
                            for sql, _params in tx.queries))


class FamilyTest(unittest.TestCase):
    """Famille = harnais exécuté historique, mixed/unknown conservés."""

    def test_une_famille_prouvee(self):
        notes = [designation([{"name": "a", "family": "claude"}])]
        self.assertEqual(report.family_of(notes), ("claude", None))

    def test_deux_familles_prouvees_donnent_mixed(self):
        notes = [designation([{"name": "a", "family": "claude"},
                              {"name": "b", "family": "codex"}])]
        self.assertEqual(report.family_of(notes), ("mixed", None))

    def test_familles_multiples_d_un_auteur_donnent_mixed(self):
        notes = [designation([{"name": "a", "family": "",
                               "families": ["claude", "codex"]}])]
        self.assertEqual(report.family_of(notes), ("mixed", None))

    def test_unknown_authors_donnent_unknown(self):
        notes = [designation([{"name": "a", "family": "claude"}],
                             unknown=["b"])]
        family, reason = report.family_of(notes)
        self.assertEqual(family, "unknown")
        self.assertIn("attribution", reason)

    def test_attribution_absente_donne_unknown(self):
        notes = [designation([])]
        self.assertEqual(report.family_of(notes)[0], "unknown")
        self.assertEqual(report.family_of([])[0], "unknown")
        self.assertEqual(report.family_of(None)[0], "unknown")
        self.assertEqual(
            report.family_of([designation([{"name": "a", "family": ""}])])[0],
            "unknown")

    def test_la_derniere_designation_designated_avant_le_gel_fait_foi(self):
        notes = [
            designation([{"name": "a", "family": "deepseek"}]),
            designation([{"name": "a", "family": "claude"}], state="manual"),
            designation([{"name": "a", "family": "codex"}]),
        ]
        # la plus récente d'abord : la seconde note est `manual`, on descend
        # jusqu'à la dernière `designated`.
        self.assertEqual(report.family_of(notes), ("deepseek", None))

    def test_note_illisible_ignoree_puis_la_precedente(self):
        notes = ["review-designation:1:1: pas-du-json",
                 designation([{"name": "a", "family": "claude"}])]
        self.assertEqual(report.family_of(notes), ("claude", None))

    def test_famille_historique_pas_le_registre_courant(self):
        # aucune lecture du registre : la famille vient de la charge du gel,
        # même si l'agent a changé de harnais depuis.
        notes = [designation([{"name": "ex-membre", "family": "codex"}])]
        self.assertEqual(report.family_of(notes), ("codex", None))


class CohortTest(unittest.TestCase):
    """Porteurs séparés et deux cohortes distinctes."""

    def test_porteurs_hors_des_deux_cohortes(self):
        value = run()
        carriers = value["cohorts"]["carriers"]["units"]
        self.assertEqual(carriers, 2)
        self.assertEqual(value["cohorts"]["integration"]["units"], 6)
        self.assertEqual(value["cohorts"]["staging"]["units"], 5)
        # les porteurs ne sont ni dans les unités ni dans leurs tableaux
        self.assertEqual(sum(row["lots"] for row in
                             value["families"]["staging"]), 5)
        self.assertEqual(value["families"]["carriers"][0]["lots"], 1)

    def test_deux_cohortes_publiees_separement(self):
        value = run()
        self.assertIn("integration", value["families"])
        self.assertIn("staging", value["families"])
        # une unité intégrée avant la fenêtre et attestée dedans est dans la
        # seule cohorte de staging (107) ; les deux cohortes peuvent se
        # recouvrir (101, 103, 105, 106).
        self.assertEqual(value["cohorts"]["staging"]["units"], 5)
        self.assertEqual(value["cohorts"]["integration"]["units"], 6)

    def test_attestation_avant_integration_ne_fait_pas_entrer_au_staging(self):
        value = run()
        coverage = value["coverage"]["staging_attestations"]
        self.assertEqual(coverage["attestation_before_integration"], [108])
        self.assertEqual(coverage["carrier"], [104])
        self.assertEqual(coverage["no_eligible_integration"], [999])
        self.assertEqual(coverage["events"], 3)
        self.assertEqual(coverage["lots"], 6)
        self.assertEqual(coverage["matched"], 3)

    def test_porteur_ratio_separe_dans_sa_cohorte(self):
        value = run()
        carrier = next(row for row in value["families"]["carriers"]
                       if row["name"] == "claude")
        self.assertEqual(carrier["lots"], 1)
        self.assertEqual(carrier["median_ratio"], 1.0)


class FamilyRowsTest(unittest.TestCase):
    """Les tableaux de l'étude et leurs dénominateurs."""

    def test_table_de_staging(self):
        value = run()
        claude = family(value, "claude")
        self.assertEqual(claude["lots"], 2)
        self.assertEqual(claude["ratios_known"], 2)
        self.assertEqual(claude["median_ratio"], 1.0)
        self.assertEqual(claude["p80_ratio"], 1.0)
        self.assertEqual(claude["turns"], 3)
        self.assertEqual(claude["usd"], 1.2)
        self.assertEqual(claude["mean_usd"], 0.6)
        self.assertEqual(claude["median_usd"], 0.6)
        deepseek = family(value, "deepseek")
        self.assertEqual(deepseek["median_ratio"], 3.0)
        self.assertEqual(deepseek["blocked_rows"], 1)
        self.assertEqual(deepseek["lots_blocked"], 1)
        self.assertEqual(deepseek["refreezes"], 2)

    def test_familles_mixed_et_unknown_visibles(self):
        value = run()
        mixed = family(value, "mixed")
        self.assertEqual(mixed["lots"], 1)
        self.assertEqual(mixed["median_ratio"], 1.0)
        unknown = family(value, "unknown")
        self.assertEqual(unknown["lots"], 1)
        self.assertEqual(unknown["ratios_known"], 0)
        self.assertIsNone(unknown["median_ratio"])
        self.assertIsNone(unknown["turns"])
        self.assertIsNone(unknown["usd"])
        self.assertEqual(unknown["unknown"]["ratio"], 1)

    def test_croisements_type_taille(self):
        value = run()
        by_type = {row["name"]: row for row in
                   value["cross"]["staging"]["by_type"]}
        self.assertEqual(by_type["bug"]["lots"], 3)
        self.assertEqual(by_type["improvement"]["lots"], 1)
        self.assertEqual(by_type["evolution"]["lots"], 1)
        by_size = {row["name"]: row for row in
                   value["cross"]["staging"]["by_size"]}
        self.assertEqual(by_size["s"]["lots"], 3)
        self.assertEqual(by_size["m"]["lots"], 1)
        self.assertEqual(by_size["unknown"]["lots"], 1)
        self.assertEqual(len(value["cross"]["staging"]["by_family_type"]), 4)
        self.assertEqual(
            len(value["cross"]["staging"]["by_family_type_size"]), 4)

    def test_percentile_interpolation_lineaire(self):
        self.assertEqual(report._percentile([1.0, 2.0], 0.5), 1.5)
        self.assertEqual(report._percentile([], 0.5), None)
        self.assertEqual(report._percentile([None, 3.0], 0.5), 3.0)


class HistoricalShaTest(unittest.TestCase):
    """SHA de gel comparés par texte, regels distincts."""

    def test_un_seul_gel_ne_compte_pas_comme_regel(self):
        value = report._unit(unit(201, heads=1))
        self.assertEqual(value["refreezes"], 0)

    def test_trois_textes_distincts_donnent_deux_regels(self):
        value = report._unit(unit(202, heads=3))
        self.assertEqual(value["refreezes"], 2)

    def test_le_meme_texte_repete_n_est_pas_un_regel(self):
        value = report._unit(unit(203, heads=["a" * 40, "a" * 40]))
        self.assertEqual(value["refreezes"], 0)

    def test_prefixe_et_texte_complet_sont_distincts(self):
        value = report._unit(unit(204, heads=["a" * 12, "a" * 40]))
        self.assertEqual(value["refreezes"], 1)

    def test_sha_vide_ne_compte_pas_comme_tete(self):
        value = report._unit(unit(205, heads=["", "b" * 40]))
        self.assertEqual(value["refreezes"], 0)


class CostTest(unittest.TestCase):
    """Cache Codex, compte forfait ≠ facture, void et données absentes."""

    def test_convention_cache_codex_et_claude(self):
        value = run()
        activity = {row["harness"]: row for row in value["activity"]["mesh"]}
        # Codex : input_tokens inclut déjà le cache lu.
        self.assertEqual(activity["codex"]["prompt"], 300.0)
        # Claude : input + cache lu ; le cache écrit reste séparé.
        self.assertEqual(activity["claude"]["prompt"], 280.0)
        self.assertEqual(activity["claude"]["cached_input_tokens"], 80.0)
        self.assertEqual(activity["claude"]["cache_write_tokens"], 10.0)

    def test_usd_n_est_pas_une_facture(self):
        value = run()
        self.assertIs(value["activity"]["usd_is_invoice"], False)
        self.assertIn("facture", value["activity"]["usd_basis"])
        self.assertIn("forfait", value["activity"]["usd_basis"])

    def test_lignes_void_comptees_et_exclues_des_sommes(self):
        value = run()
        codex = next(row for row in value["activity"]["project"]
                     if row["harness"] == "codex")
        self.assertEqual(codex["void_rows"], 1)
        self.assertEqual(codex["usd"], 2.0)
        self.assertEqual(codex["cost_rows"], 2)

    def test_donnee_absente_n_est_pas_zero(self):
        value = run()
        unknown = family(value, "unknown")
        self.assertIsNone(unknown["turns"])
        self.assertIsNone(unknown["usd"])
        self.assertIsNone(unknown["prompt"])
        self.assertIsNone(unknown["mean_usd"])
        self.assertEqual(unknown["lots_with_usd"], 0)
        # un vrai zéro lu reste un zéro
        codex = family(value, "codex", cohort="integration")
        self.assertEqual(codex["turns"], 4.0)
        self.assertEqual(codex["lots_with_turns"], 2)

    def test_unite_sans_ligne_de_cout_a_un_usd_inconnu(self):
        value = report._unit(unit(301, usd=None, cost_rows=0, prompt=None,
                                  output=None))
        self.assertIsNone(value["usd"])
        self.assertIsNone(value["prompt"])
        self.assertEqual(value["turns"], 2)

    def test_compte_de_facturation_inconnu_compte(self):
        value = run()
        codex = next(row for row in value["activity"]["project"]
                     if row["harness"] == "codex")
        self.assertEqual(codex["billing_account_unknown"], 1)


class ReviewTest(unittest.TestCase):
    """Verdicts B par famille de reçu, sièges notifiés à part."""

    def test_b_ok_et_sieges_par_famille(self):
        value = run()
        by_family = {row["family"]: row for row in value["review"]["by_family"]}
        self.assertEqual(by_family["claude"]["blocked"], 2)
        self.assertEqual(by_family["claude"]["ok"], 3)
        self.assertEqual(by_family["claude"]["blocked_rate"], 0.4)
        self.assertEqual(by_family["claude"]["notified_seats"], 4)
        self.assertEqual(by_family["codex"]["blocked"], 1)
        self.assertEqual(by_family["codex"]["blocked_rate"], 1.0)
        self.assertEqual(by_family["deepseek"]["frozen"], 5)
        self.assertIsNone(by_family["deepseek"]["blocked_rate"])
        self.assertIsNone(by_family["claude"]["b_justes"])
        self.assertEqual(by_family["claude"]["b_justes_unknown"], 2)

    def test_notifications_sans_famille_hors_des_trois_lignes(self):
        value = run()
        self.assertEqual(value["review"]["notified_unattributed"]["seats"], 7)
        self.assertEqual(value["review"]["unknown_receipt_family"]["verdicts"], 1)
        self.assertNotIn("", [row["family"] for row in
                              value["review"]["by_family"]])


class CoverageTest(unittest.TestCase):
    """Dénominateurs exacts, inconnues nommées, pas de zéro inventé."""

    def test_denominateurs(self):
        value = run()
        denominators = value["coverage"]["denominators"]
        self.assertEqual(denominators["candidates"], 9)
        self.assertEqual(denominators["integration"], 6)
        self.assertEqual(denominators["staging"], 5)
        self.assertEqual(denominators["carriers"], 2)
        self.assertEqual(denominators["staging_ratios_known"], 4)
        self.assertEqual(denominators["integration_ratios_known"], 5)
        self.assertEqual(denominators["staging_usd_known"], 4)
        self.assertEqual(denominators["staging_class_unknown"], 5)
        self.assertEqual(denominators["staging_effort_unknown"], 5)
        self.assertEqual(denominators["b_justes_known"], 0)

    def test_classe_effort_et_b_justes_inconnus(self):
        value = run()
        self.assertIn("class", value["coverage"]["unknown"])
        self.assertIn("effort", value["coverage"]["unknown"])
        self.assertIn("b_justes", value["coverage"]["unknown"])
        self.assertIn("pas persistée", value["coverage"]["unknown"]["class"])

    def test_regles_de_couverture_exportees(self):
        value = run()
        ids = {row["id"] for row in value["coverage"]["rules"]}
        for expected in ("window", "family", "sha", "carriers", "cohorts",
                         "ratio", "cost", "cache", "usd", "missing", "b",
                         "class", "effort", "review", "causality"):
            self.assertIn(expected, ids)
        self.assertGreaterEqual(len(value["coverage"]["limits"]), 8)

    def test_staging_non_lu_reste_inconnu(self):
        value = run(staged=None)
        coverage = value["coverage"]["staging_attestations"]
        self.assertIsNone(coverage["lots"])
        self.assertIsNone(coverage["matched"])
        self.assertIn("attestation", coverage["unknown"])

    def test_schema_et_version_de_calcul(self):
        value = run()
        self.assertEqual(value["schema"], "ameesh-model-evaluation-report/1")
        self.assertEqual(value["source"],
                         "docs/design/etudes/evaluation-modeles.md#M1")
        self.assertEqual(value["calculation_version"], 3)
        self.assertIs(value["read_only"], True)
        self.assertEqual(value["window"]["timezone"], "UTC")
        self.assertIs(value["window"]["end_exclusive"], True)


class FormatTest(unittest.TestCase):
    """Le texte affiche « inconnu », jamais un zéro inventé."""

    def test_texte_montre_les_cohortes_et_l_inconnu(self):
        text = report.format_text(run())
        self.assertIn("lecture seule", text)
        self.assertIn("Unités attestées", text)
        self.assertIn("Unités intégrées", text)
        self.assertIn("Porteurs, séparés", text)
        self.assertIn("inconnu", text)
        self.assertIn("USD ≠ facture", text)
        self.assertNotIn("0 USD", text)


class DatabaseReadTest(PgTestCase):
    """La requête complète s'exécute sur le schéma réel, en lecture seule."""

    def test_read_trouve_une_unite_attestee(self):
        lot = 666001
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, "
            "branch, change_size) VALUES (%s, 'bug', 'test', 'M1', 'ameesh', "
            "'qa', 'lot/666001', 's')", (lot,))
        self.db.execute(
            "UPDATE work_items SET started_at = to_timestamp(%s) WHERE id = %s",
            (START + 3600, lot))
        self.db.execute(
            "INSERT INTO work_item_estimates (work_item_id, minutes, source, "
            "estimated_by, at) VALUES (%s, 60, 'test', 'auditeur', "
            "to_timestamp(%s))", (lot, START + 100))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, "
            "actor) VALUES (%s, 'merged', to_timestamp(%s), %s, 'auteur')",
            (lot, START + 7200, "a" * 40))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, "
            "actor) VALUES (%s, 'frozen', to_timestamp(%s), %s, 'auteur')",
            (lot, START + 5400, "a" * 40))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, "
            "actor, verdict) VALUES (%s, 'verdict', to_timestamp(%s), %s, "
            "'relecteur', 'blocked')", (lot, START + 6000, "a" * 40))
        note = designation([{"name": "auteur", "family": "claude"}])
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, "
            "created_at) VALUES (%s, 'qa', %s, 'auteur', to_timestamp(%s))",
            (lot, note, START + 6000))
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, project, data, created_at) "
            "VALUES ('staging.attested', 'staging', 'ameesh', %s::jsonb, "
            "to_timestamp(%s))",
            (json.dumps({"lots": [str(lot)]}), START + 9000))
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, work_item_id, recorded_at, "
            "usd, input_tokens, cached_input_tokens, output_tokens, turn_id) "
            "VALUES ('a', 'claude', %s, to_timestamp(%s), 1.5, 100, 20, 10, "
            "'t1')", (lot, START + 8000))
        value = report.read(self.db, project="ameesh",
                            start=report.iso_z(START),
                            end=report.iso_z(END))
        self.assertEqual(value["cohorts"]["staging"]["units"], 1)
        self.assertEqual(value["cohorts"]["integration"]["units"], 1)
        claude = family(value, "claude")
        self.assertEqual(claude["median_ratio"], 1.0)
        self.assertEqual(claude["prompt"], 120.0)
        self.assertEqual(claude["blocked_rows"], 1)
        self.assertEqual(value["coverage"]["staging_attestations"]["lots"], 1)
        self.assertEqual(value["coverage"]["staging_attestations"]["matched"], 1)
