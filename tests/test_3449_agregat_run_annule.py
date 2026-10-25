# SPDX-License-Identifier: AGPL-3.0-only
"""#3449 — l'agrégat `tests` ne verdit pas sans preuve Linux.

Run 40144843565 (paquet h-13, 24/10 23:58Z → 25/10 00:03Z) : conclusion
`cancelled`, job `tests` SUCCESS publié sur la part macOS (2 min) alors que
les 8 psycopg et les 16 psql étaient annulés. La garde `if: cancelled()` du
workflow n'a pas joué — le job `tests`, `always()`, a démarré après que les
familles Linux eurent été annulées mais avant que le run ne soit marqué
`cancelled` — et #3251 lisait « au moins une part réussie » comme une preuve
suffisante.

#3449 revient au texte de #128512 : « les parts annulées ou expirées restent
bloquantes ». Une part Linux planifiée annulée, expirée, absente ou en échec
rougit l'agrégat, même si les 15 autres ont réussi (15/16 = rouge) ; une
famille sans aucune part réussie est une preuve absente (rouge, cas h-13).
`needs.psql.result` d'une matrice vaut `cancelled` dès qu'une part l'est, il
faut donc lire les jobs du run. Seuls macOS et « autre système de fichiers »
restent des non-concluants consignés (canon §3).
"""
from __future__ import annotations

import unittest

from . import test_940_agregat_bash as t940

agregat = t940.agregat

#: les parts du run 40144843565 (8 psycopg, 16 psql) ; seule macOS a conclu.
PSYCOPG_PARTS = "[%s]" % ", ".join(str(i) for i in range(1, 9))
PSQL_PARTS = "[%s]" % ", ".join(str(i) for i in range(1, 17))


def part_jobs(family, total, successes):
    """Les jobs d'une famille tels que l'API Actions les rend."""
    return [{"name": "%s %d/%d" % (family, index, total),
             "status": "completed",
             "conclusion": "success" if index <= successes else "cancelled"}
            for index in range(1, total + 1)]


def run_jobs(psycopg_successes=0, psql_successes=0):
    return (part_jobs("psycopg", 8, psycopg_successes)
            + part_jobs("psql", 16, psql_successes))


def classify(**over):
    """La classification du run annulé de h-13, une part remplacée."""
    values = dict(event="pull_request", plan="success", version="success",
                  version_only="false", docs_only="false", doc_readers="skipped",
                  psycopg="cancelled", psycopg_parts=PSYCOPG_PARTS,
                  psycopg_jobs=part_jobs("psycopg", 8, 0),
                  psql="cancelled", psql_parts=PSQL_PARTS,
                  psql_jobs=part_jobs("psql", 16, 0),
                  platform_macos="success", fs_class="skipped")
    values.update(over)
    return agregat.classify(**values)


class RunAnnuleTest(unittest.TestCase):
    """Le cas exact : macOS vert, psycopg/psql annulés, run annulé."""

    def test_le_run_de_h13_conclut_l_agregat_rouge(self):
        result = classify()
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual(result["red"], [])
        self.assertEqual(result["consigned"], [])
        missing = " ".join(result["missing"])
        self.assertIn("psycopg (cancelled, 0/8 part(s) réussie(s), preuve absente)",
                      missing)
        self.assertIn("psql (cancelled, 0/16 part(s) réussie(s), preuve absente)",
                      missing)
        statuses = {row["part"]: row["status"] for row in result["parts"]}
        self.assertEqual((statuses["psycopg"], statuses["psql"]),
                         (agregat.MISSING, agregat.MISSING))
        self.assertEqual(statuses["plateforme macOS"], agregat.GREEN)
        self.assertEqual(statuses["autre système de fichiers"], agregat.GREEN)

    def test_le_script_echoue_et_publie_un_detail_failure(self):
        created = []

        class Gh:
            def create(self, name, conclusion, title, summary):
                created.append((name, conclusion, title, summary))
                return 1

        code = agregat.main([
            "--event", "pull_request", "--plan", "success",
            "--version", "success", "--version-only", "false",
            "--docs-only", "false", "--doc-readers", "skipped",
            "--psycopg", "cancelled", "--psycopg-parts", PSYCOPG_PARTS,
            "--psql", "cancelled", "--psql-parts", PSQL_PARTS,
            "--plateforme-macos", "success", "--autre-fs", "skipped",
            "--publish"], gh=Gh(), jobs=run_jobs())
        self.assertEqual(code, 1)
        [detail] = created
        self.assertEqual(detail[:2], (agregat.DETAIL_CHECK, "failure"))
        self.assertIn("preuve absente", detail[2])
        self.assertIn("psycopg (cancelled, 0/8 part(s) réussie(s), preuve absente)",
                      detail[3])
        self.assertIn("psql (cancelled, 0/16 part(s) réussie(s), preuve absente)",
                      detail[3])

    def test_une_part_annulee_sur_seize_rougit(self):
        # #128512 (prémisse : les parts annulées ou expirées restent
        # bloquantes) : 15 succès + 1 annulée sur 16 = rouge, pas neutre.
        result = classify(psycopg="success",
                          psycopg_jobs=part_jobs("psycopg", 8, 8),
                          psql_jobs=part_jobs("psql", 16, 15))
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["consigned"], [])
        self.assertIn("psql (1/16 part(s) annulée(s) ou non concluante(s), "
                      "famille cancelled)", result["red"])

    def test_deux_familles_partielles_et_macos_annule_rougissent(self):
        # Chaque famille planifiée qui a une part annulée rougit ; macOS
        # (non requis) reste le seul non-concluant consigné.
        result = classify(psycopg_jobs=part_jobs("psycopg", 8, 7),
                          psql_jobs=part_jobs("psql", 16, 15),
                          platform_macos="cancelled")
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual(result["missing"], [])
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS"])
        self.assertTrue(any(gap.startswith("psycopg (1/8 part(s) annulée(s)")
                            for gap in result["red"]), result["red"])
        self.assertTrue(any(gap.startswith("psql (1/16 part(s) annulée(s)")
                            for gap in result["red"]), result["red"])

    def test_psycopg_partiel_avec_psql_non_planifie_rougit(self):
        # psql n'est pas planifiée (neutre), mais la part psycopg annulée
        # rougit quand même ; macOS annulé reste consigné.
        result = classify(psycopg_jobs=part_jobs("psycopg", 8, 7),
                          psql="skipped", psql_parts="[]",
                          platform_macos="cancelled")
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual(result["missing"], [])
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS"])
        self.assertTrue(any(gap.startswith("psycopg (1/8 part(s) annulée(s)")
                            for gap in result["red"]), result["red"])

    def test_un_echec_individuel_dans_une_famille_annulee_rougit(self):
        # B2 : une famille `cancelled` avec 6 succès, 1 failure et 1 annulée
        # ne doit pas passer neutre ; macOS vert ne rachète pas l'échec.
        jobs = ([{"name": "psycopg %d/8" % index, "conclusion": "success"}
                 for index in range(1, 7)]
                + [{"name": "psycopg 7/8", "conclusion": "failure"},
                   {"name": "psycopg 8/8", "conclusion": "cancelled"}])
        result = classify(psycopg_jobs=jobs, psql="success",
                          psql_jobs=part_jobs("psql", 16, 16),
                          platform_macos="success")
        self.assertEqual(result["state"], agregat.RED)
        self.assertTrue(any(gap.startswith("psycopg (1/8 part(s) en échec")
                            for gap in result["red"]), result["red"])
        self.assertEqual(result["consigned"], [])
        self.assertEqual(result["missing"], [])

    def test_une_part_planifiee_absente_rougit(self):
        # B3 de cx2 : le plan sélectionne 3 parts psql, un seul job est lu ;
        # les deux autres ne sont pas une preuve, même si le seul job est vert.
        result = classify(psycopg="success",
                          psycopg_jobs=part_jobs("psycopg", 8, 8),
                          psql="cancelled", psql_parts="[1, 2, 3]",
                          psql_jobs=[{"name": "psql 1/3",
                                      "conclusion": "success"}])
        self.assertEqual(result["state"], agregat.RED)
        self.assertIn("psql (2/3 part(s) planifiée(s) absente(s), famille "
                      "cancelled)", result["red"])
        self.assertEqual(result["consigned"], [])

    def test_un_echec_individuel_avec_plan_trois_rougit(self):
        # Reproduction cx2 : success, failure et cancelled dans la même
        # famille ; le failure explicite ne doit pas être noyé.
        result = classify(psycopg="success",
                          psycopg_jobs=part_jobs("psycopg", 8, 8),
                          psql="cancelled", psql_parts="[1, 2, 3]",
                          psql_jobs=[{"name": "psql 1/3",
                                      "conclusion": "success"},
                                     {"name": "psql 2/3",
                                      "conclusion": "failure"},
                                     {"name": "psql 3/3",
                                      "conclusion": "cancelled"}])
        self.assertEqual(result["state"], agregat.RED)
        self.assertTrue(any(gap.startswith("psql (1/3 part(s) en échec")
                            for gap in result["red"]), result["red"])
        self.assertEqual(result["consigned"], [])

    def test_le_script_publie_failure_pour_une_part_annulee(self):
        created = []

        class Gh:
            def create(self, name, conclusion, title, summary):
                created.append((name, conclusion, title, summary))
                return 1

        code = agregat.main([
            "--event", "pull_request", "--plan", "success",
            "--version", "success", "--version-only", "false",
            "--docs-only", "false", "--doc-readers", "skipped",
            "--psycopg", "success", "--psycopg-parts", PSYCOPG_PARTS,
            "--psql", "cancelled", "--psql-parts", PSQL_PARTS,
            "--plateforme-macos", "success", "--autre-fs", "skipped",
            "--publish"], gh=Gh(), jobs=run_jobs(psycopg_successes=8,
                                                  psql_successes=15))
        self.assertEqual(code, 1)
        [detail] = created
        self.assertEqual(detail[:2], (agregat.DETAIL_CHECK, "failure"))
        self.assertIn("1/16 part(s) annulée(s)", detail[3])

    def test_les_jobs_prefixes_par_le_portier_sont_reconnus(self):
        # Appelée par paquet.yml, tests.yml est un workflow réutilisable :
        # l'API préfixe les jobs appelés par « suite complète du paquet / ».
        jobs = [{"name": "suite complète du paquet / psql 1/16",
                 "conclusion": "success"},
                {"name": "suite complète du paquet / psycopg 1/8",
                 "conclusion": "cancelled"},
                {"name": "suite complète du paquet / tests",
                 "conclusion": "success"}]
        self.assertEqual([job["name"] for job in agregat._family_jobs(jobs, "psql")],
                         ["suite complète du paquet / psql 1/16"])
        self.assertEqual([job["name"] for job in agregat._family_jobs(jobs, "psycopg")],
                         ["suite complète du paquet / psycopg 1/8"])

    def test_le_script_lit_les_jobs_du_run_par_l_api(self):
        seen = []
        created = []

        class Gh:
            def jobs(self, run_id):
                seen.append(run_id)
                return run_jobs(psycopg_successes=8, psql_successes=15)

            def create(self, name, conclusion, title, summary):
                created.append((name, conclusion))
                return 1

        code = agregat.main([
            "--event", "pull_request", "--run-id", "40144843565",
            "--plan", "success", "--version", "success",
            "--version-only", "false", "--docs-only", "false",
            "--doc-readers", "skipped",
            "--psycopg", "success", "--psycopg-parts", PSYCOPG_PARTS,
            "--psql", "cancelled", "--psql-parts", PSQL_PARTS,
            "--plateforme-macos", "success", "--autre-fs", "skipped",
            "--publish"], gh=Gh())
        self.assertEqual(seen, ["40144843565"])
        self.assertEqual(code, 1)
        self.assertEqual(created, [(agregat.DETAIL_CHECK, "failure")])

    def test_sans_jobs_lisibles_une_famille_annulee_reste_rouge(self):
        # Une lecture impossible ne verdit jamais : repli sur preuve absente.
        result = classify(psycopg_jobs=None, psql_jobs=None)
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual(result["consigned"], [])
        self.assertTrue(any(gap.startswith("psycopg (cancelled, preuve absente")
                            for gap in result["missing"]), result["missing"])

    def test_une_famille_linux_absente_est_une_preuve_absente(self):
        for part, over in (("psycopg", {"psycopg": ""}),
                           ("psql", {"psql": ""})):
            with self.subTest(part=part):
                result = classify(**over)
                self.assertEqual(result["state"], agregat.RED)
                self.assertTrue(any(gap.startswith(part + " (")
                                    for gap in result["red"]), result["red"])

    def test_psql_annule_avec_psycopg_vert_ne_rachete_pas_la_preuve(self):
        result = classify(psycopg="success",
                          psycopg_jobs=part_jobs("psycopg", 8, 8))
        self.assertEqual(result["state"], agregat.RED)
        self.assertTrue(any(gap.startswith("psql (cancelled, 0/16")
                            for gap in result["missing"]), result["missing"])
        self.assertEqual(result["consigned"], [])

    def test_psql_non_planifie_reste_un_saut_legitime(self):
        result = classify(psycopg="success", psql="skipped", psql_parts="[]")
        self.assertEqual(result["state"], agregat.GREEN)
        self.assertEqual((result["missing"], result["consigned"]), ([], []))

    def test_une_annulation_macos_reste_non_bloquante(self):
        result = classify(psycopg="success",
                          psycopg_jobs=part_jobs("psycopg", 8, 8),
                          psql="success",
                          psql_jobs=part_jobs("psql", 16, 16),
                          platform_macos="cancelled")
        self.assertEqual(result["state"], agregat.GREEN)
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS"])
        self.assertEqual(result["missing"], [])

    def test_aucune_part_reussie_reste_une_preuve_absente(self):
        # Décision #128512 : même avec macOS annulé, aucune part réussie ne
        # peut pas conclure vert.
        result = classify(psycopg_jobs=[], psql_jobs=[],
                          platform_macos="cancelled", fs_class="cancelled")
        self.assertEqual(result["state"], agregat.RED)
        self.assertIn("aucune part n'a conclu (preuve absente)",
                      result["missing"])
        self.assertTrue(any("planifiée(s) absente(s)" in gap
                            for gap in result["red"]), result["red"])
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS", "autre système de fichiers"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
