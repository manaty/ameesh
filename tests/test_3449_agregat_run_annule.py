# SPDX-License-Identifier: AGPL-3.0-only
"""#3449 — l'agrégat `tests` ne verdit pas sans preuve Linux.

Run 40144843565 (paquet h-13, 24/10 23:58Z → 25/10 00:03Z) : conclusion
`cancelled`, job `tests` SUCCESS publié sur la part macOS (2 min) alors que
les 8 psycopg et les 16 psql étaient annulés. La garde `if: cancelled()` du
workflow n'a pas joué — le job `tests`, `always()`, a démarré après que les
familles Linux eurent été annulées mais avant que le run ne soit marqué
`cancelled` — et #3251 lisait « au moins une part réussie » comme une preuve
suffisante.

#3449 exige la preuve de chaque famille Linux planifiée : une famille sans
**aucune** part réussie est une preuve absente, donc un agrégat rouge. Une
annulation partielle (15 parts sur 16 réussies) reste un non-concluant
consigné, vert neutre : `needs.psql.result` d'une matrice vaut `cancelled`
dès qu'une part l'est, il faut donc lire les jobs du run (B1 de la relecture
claude). Une annulation macOS ou « autre système de fichiers » reste, elle,
un non-concluant consigné (décision #128512, canon §3).
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

    def test_une_part_annulee_sur_seize_reste_non_bloquante(self):
        # B1 de la relecture claude : une matrice `cancelled` avec 15/16
        # parts vertes n'est pas une preuve absente (décision #128512).
        result = classify(psycopg="success",
                          psycopg_jobs=part_jobs("psycopg", 8, 8),
                          psql_jobs=part_jobs("psql", 16, 15))
        self.assertEqual(result["state"], agregat.GREEN)
        self.assertEqual(result["missing"], [])
        self.assertEqual([row["part"] for row in result["consigned"]], ["psql"])
        self.assertEqual(result["consigned"][0]["parts_ok"], "15/16")

    def test_le_script_publie_neutral_pour_une_annulation_partielle(self):
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
        self.assertEqual(code, 0)
        [detail] = created
        self.assertEqual(detail[:2], (agregat.DETAIL_CHECK, "neutral"))
        self.assertIn("psql", detail[3])
        self.assertIn("15/16 part(s) réussie(s)", detail[3])

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
        self.assertEqual(code, 0)
        self.assertEqual(created, [(agregat.DETAIL_CHECK, "neutral")])

    def test_sans_jobs_lisibles_une_famille_annulee_reste_rouge(self):
        # Une lecture impossible ne verdit jamais : repli sur preuve absente.
        result = classify(psycopg_jobs=None, psql_jobs=None)
        self.assertEqual(result["state"], agregat.RED)
        self.assertEqual(result["consigned"], [])
        self.assertTrue(any(gap.startswith("psycopg (cancelled, preuve absente)")
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
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS", "autre système de fichiers"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
