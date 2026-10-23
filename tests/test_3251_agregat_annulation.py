# SPDX-License-Identifier: AGPL-3.0-only
"""#3251 — une annulation interne de l'agrégat `tests` ne retient plus rien.

Décision owner #128512 (23/10/2026, option a) : « Agrégat requis jamais
conclu = preuve absente, donc bloquant […]. Les non-concluants à l'intérieur
d'un agrégat qui a conclu restent non bloquants (§3). »

L'agrégat `tests` conclut donc vert quand une part interne a été **annulée**
(capacité hébergée, runner perdu), et publie son détail `tests (bilan des
parts)` en `neutral`. `fire.ready_pr` le consigne sans retenir le paquet,
`radar.commit_ci` le consigne sans retenir la release ; un `FAILURE` de part
reste un négatif qui rougit l'agrégat et bloque.

Ce fichier couvre le classifieur pur (`scripts/ci-agregat.py`) et les deux
portes ; `tests/test_940_agregat_bash.py` garde le contrat du workflow et
l'invariant « jamais de SUCCESS muet ».
"""
from __future__ import annotations

import unittest

from ameesh import fire, radar

from . import test_2271_fire_required_checks as f2271
from . import test_940_agregat_bash as t940
from . import test_l160g_nexlink as l160g

agregat = t940.agregat


def classify(**over):
    """Un agrégat vert de référence, une part remplacée par scénario."""
    values = dict(event="pull_request", plan="success", version="success",
                  version_only="false", docs_only="false", doc_readers="skipped",
                  psycopg="success", psycopg_parts="[1, 2]",
                  psql="success", psql_parts="[1]")
    values.update(over)
    return agregat.classify(**values)


class ClassifierTest(unittest.TestCase):

    def test_le_detail_est_le_meme_nom_dans_le_script_et_radar(self):
        self.assertEqual(agregat.DETAIL_CHECK, radar.AGGREGATE_DETAIL_CHECK)

    def test_annulation_interne_conclut_vert_et_se_consigne(self):
        for part, over in (("psycopg", {"psycopg": "cancelled"}),
                           ("psql", {"psql": "cancelled"}),
                           ("plateforme macOS", {"platform_macos": "cancelled"}),
                           ("autre système de fichiers", {"fs_class": "cancelled"})):
            with self.subTest(part=part):
                result = classify(**over)
                self.assertEqual(result["state"], agregat.GREEN)
                self.assertEqual([row["part"] for row in result["consigned"]], [part])
                self.assertEqual(result["red"], [])

    def test_les_conclusions_non_concluantes_sont_toutes_acceptees(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure",
                           "stale", "action_required"):
            with self.subTest(conclusion=conclusion):
                result = classify(psql=conclusion)
                self.assertEqual(result["state"], agregat.GREEN)
                self.assertEqual(result["consigned"][0]["result"], conclusion)

    def test_un_echec_de_part_rougit_l_agregat(self):
        for part, over in (("psycopg", {"psycopg": "failure"}),
                           ("psql", {"psql": "failure"}),
                           ("plateforme macOS", {"platform_macos": "failure"}),
                           ("autre système de fichiers", {"fs_class": "failure"})):
            with self.subTest(part=part):
                result = classify(**over)
                self.assertEqual(result["state"], agregat.RED)
                self.assertTrue(any(part in gap for gap in result["red"]),
                                result["red"])

    def test_echec_et_annulation_ensemble_le_negatif_gagne(self):
        result = classify(psycopg="cancelled", psql="failure")
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual([row["part"] for row in result["consigned"]], ["psycopg"])

    def test_psql_saute_sans_part_reste_legitime(self):
        result = classify(psql="skipped", psql_parts="[]")
        self.assertEqual((result["state"], result["consigned"]), (agregat.GREEN, []))

    def test_psql_saute_avec_des_parts_rougit(self):
        result = classify(psql="skipped")
        self.assertEqual(result["state"], agregat.RED)
        self.assertIn("psql (skipped, parts [1])", result["red"])

    def test_une_part_absente_ou_inconnue_rougit(self):
        self.assertEqual(classify(psycopg="")["state"], agregat.RED)
        self.assertEqual(classify(psql="weird")["state"], agregat.RED)
        # --plateforme-macos absent (workflow antérieur à #3026) : pas exigé ;
        # présent mais vide : inconnu, donc rouge.
        self.assertEqual(classify()["state"], agregat.GREEN)
        self.assertEqual(classify(platform_macos="")["state"], agregat.RED)

    def test_le_plan_refuse_rougit_meme_si_tout_le_reste_est_vert(self):
        self.assertEqual(classify(plan="failure")["state"], agregat.RED)
        self.assertEqual(classify(plan="cancelled")["state"], agregat.RED)

    def test_version_sautee_sur_une_pr_rougit_et_reste_legitime_hors_pr(self):
        self.assertEqual(classify(version="skipped")["state"], agregat.RED)
        self.assertEqual(classify(version="skipped", event="push")["state"],
                         agregat.GREEN)

    def test_les_voies_version_seule_et_doc_seule_sont_interdites_sur_une_pr(self):
        self.assertEqual(classify(version_only="true")["state"], agregat.RED)
        self.assertEqual(classify(docs_only="true")["state"], agregat.RED)

    def test_version_verifiee_avant_les_sorties_anticipees(self):
        # L158 : la version est lue avant les voies version seule / doc seule ;
        # un échec de version rougit même quand le plan annonce une de ces
        # voies (le classifieur ne sort pas tôt).
        self.assertEqual(
            classify(event="push", version="failure", version_only="true")["state"],
            agregat.RED)
        self.assertEqual(
            classify(event="push", version="failure", docs_only="true",
                     doc_readers="success")["state"], agregat.RED)

    def test_version_seule_hors_pr_ne_demande_aucune_part(self):
        result = classify(event="push", version_only="true",
                          psycopg="skipped", psql="skipped", psql_parts="[]")
        self.assertEqual((result["state"], result["parts"]), (agregat.GREEN, []))

    def test_doc_seule_demande_ses_lecteurs(self):
        # La voie documentation seule n'existe que sur main (pas sur une PR,
        # #1343) : ses lecteurs déclarés doivent être verts.
        self.assertEqual(classify(event="push", docs_only="true",
                                  doc_readers="success")["state"], agregat.GREEN)
        self.assertEqual(classify(event="push", docs_only="true",
                                  doc_readers="failure")["state"], agregat.RED)


class DetailGateTest(unittest.TestCase):
    """`fire.ready_pr` : le détail `neutral` se consigne, sans bloquer."""

    def ready(self, rollup, *, required=(("tests", None),)):
        status = f2271._status(rollup)
        report: dict = {}
        ok, why = fire.ready_pr(status, target="main", project=None,
                                frozen=f2271.SHA, required=required,
                                slug="manaty/ameesh", gh=f2271.FakeGh(),
                                report=report)
        return ok, why, report

    def test_un_detail_neutral_ne_bloque_pas_et_se_consigne(self):
        ok, why, report = self.ready([
            f2271._check("tests", "SUCCESS"),
            f2271._check(radar.AGGREGATE_DETAIL_CHECK, "NEUTRAL")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"],
                         ["tests (bilan des parts) (NEUTRAL)"])

    def test_un_detail_vert_ne_consigne_rien(self):
        ok, why, report = self.ready([
            f2271._check("tests", "SUCCESS"),
            f2271._check(radar.AGGREGATE_DETAIL_CHECK, "SUCCESS")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], [])

    def test_un_detail_en_echec_bloque(self):
        ok, why, _report = self.ready([
            f2271._check("tests", "SUCCESS"),
            f2271._check(radar.AGGREGATE_DETAIL_CHECK, "FAILURE")])
        self.assertFalse(ok, why)
        self.assertIn("tests (bilan des parts) (FAILURE)", why)

    def test_un_detail_en_cours_attend(self):
        ok, why, _report = self.ready([
            f2271._check("tests", "SUCCESS"),
            f2271._check(radar.AGGREGATE_DETAIL_CHECK, None, "IN_PROGRESS")])
        self.assertFalse(ok, why)

    def test_sans_detail_la_porte_est_inchangee(self):
        ok, why, report = self.ready([f2271._check("tests", "SUCCESS")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], [])

    def test_la_cle_de_consignation_est_celle_du_detail(self):
        # #3209 : la clé du lot de correction est le nom du check, sans l'état
        # consigné — paquet et release retrouvent le même lot ouvert.
        self.assertEqual(
            radar.nonconclusive_source("ameesh",
                                       "tests (bilan des parts) (NEUTRAL)"),
            "nonconclusive:ameesh:tests (bilan des parts)")


class ReleaseReadingTest(unittest.TestCase):
    """`radar.commit_ci` : le détail non concluant se consigne à la release."""

    project = radar.Project("n", "/r", github="o/nexlink", ci_required=True)
    sha = "a" * 40

    def detail(self, conclusion, ident=9):
        return {"id": ident, "name": radar.AGGREGATE_DETAIL_CHECK,
                "head_sha": self.sha, "status": "completed",
                "conclusion": conclusion,
                "app": {"id": 15368, "slug": "github-actions"},
                "started_at": "2026-10-23T20:00:00Z",
                "completed_at": "2026-10-23T20:01:00Z"}

    def test_un_detail_neutral_est_non_concluant_sans_rendre_la_tete_rouge(self):
        out = radar.commit_ci(self.project, self.sha, gh=l160g._Gh(
            [l160g._run("tests", conclusion="success")],
            checks=[self.detail("neutral")]))
        self.assertTrue(out["green"], out)
        self.assertTrue(out["acceptable"], out)
        self.assertEqual(out["negative"], [])
        self.assertEqual(out["inconclusive"], ["tests (bilan des parts)"])

    def test_un_detail_en_echec_est_un_negatif(self):
        out = radar.commit_ci(self.project, self.sha, gh=l160g._Gh(
            [l160g._run("tests", conclusion="failure")],
            checks=[self.detail("failure")]))
        self.assertFalse(out["acceptable"])
        # Le run lui-même est rouge, et le détail nomme la part : deux
        # négatifs, aucun ne bloque la lecture (la porte refuse).
        self.assertEqual(out["negative"],
                         ["tests", "tests (bilan des parts)"])

    def test_un_essai_plus_recent_remplace_l_ancien_detail(self):
        # Le même nom peut venir de `tests` (PR) et de `paquet` (portier) ;
        # seul le plus récent doit décider, sinon un neutral périmé consigne
        # une tête redevenue verte.
        def detail(workflow, conclusion, at):
            return {"__typename": "CheckRun", "name": radar.AGGREGATE_DETAIL_CHECK,
                    "workflowName": workflow, "status": "COMPLETED",
                    "conclusion": conclusion, "startedAt": at}
        old = detail("paquet", "NEUTRAL", "2026-10-23T19:00:00Z")
        new = detail("tests", "SUCCESS", "2026-10-23T20:00:00Z")
        entries = radar.aggregate_detail_entries([old, new])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["conclusion"], "SUCCESS")
        self.assertEqual(fire._aggregate_detail_gate([old, new]), ([], []))

    def test_le_detail_n_est_pas_une_preuve_du_portier_paquet(self):
        # Appelé par `paquet`, le check de détail est associé au workflow
        # appelant ; il ne doit pas être pris pour la suite complète du paquet.
        entry = {"__typename": "CheckRun", "name": radar.AGGREGATE_DETAIL_CHECK,
                 "workflowName": "paquet", "status": "COMPLETED",
                 "conclusion": "NEUTRAL"}
        self.assertEqual(radar.packet_proof_entries([entry]), [])
        self.assertEqual(fire._packet_proof_gate([entry]), ([], []))

    def test_un_detail_vert_n_ajoute_rien(self):
        out = radar.commit_ci(self.project, self.sha, gh=l160g._Gh(
            [l160g._run("tests", conclusion="success")],
            checks=[self.detail("success")]))
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["inconclusive"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
