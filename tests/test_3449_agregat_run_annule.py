# SPDX-License-Identifier: AGPL-3.0-only
"""#3449 — l'agrégat `tests` ne verdit pas sur la seule part macOS.

Run 40144843565 (paquet h-13, 24/10 23:58Z → 25/10 00:03Z) : conclusion
`cancelled`, job `tests` SUCCESS publié sur la part macOS (2 min) alors que
les 8 psycopg et les 16 psql étaient annulés. La garde `if: cancelled()` du
workflow n'a pas joué — le job `tests`, `always()`, a démarré après que les
familles Linux eurent été annulées mais avant que le run ne soit marqué
`cancelled` — et #3251 lisait « au moins une part réussie » comme une preuve
suffisante.

#3449 exige la preuve de chaque famille Linux planifiée : leur annulation,
leur absence ou un job sauté qui devait tourner est une preuve absente, donc
un agrégat rouge. Une annulation macOS ou « autre système de fichiers »
reste, elle, un non-concluant consigné (décision #128512, canon §3).
"""
from __future__ import annotations

import unittest

from . import test_940_agregat_bash as t940

agregat = t940.agregat

#: les parts du run 40144843565 (8 psycopg, 16 psql) ; seule macOS a conclu.
PSYCOPG_PARTS = "[%s]" % ", ".join(str(i) for i in range(1, 9))
PSQL_PARTS = "[%s]" % ", ".join(str(i) for i in range(1, 17))


def classify(**over):
    """La classification du run annulé de h-13, une part remplacée."""
    values = dict(event="pull_request", plan="success", version="success",
                  version_only="false", docs_only="false", doc_readers="skipped",
                  psycopg="cancelled", psycopg_parts=PSYCOPG_PARTS,
                  psql="cancelled", psql_parts=PSQL_PARTS,
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
        self.assertIn("psycopg (cancelled, preuve absente)", missing)
        self.assertIn("psql (cancelled, preuve absente)", missing)
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
            "--publish"], gh=Gh())
        self.assertEqual(code, 1)
        [detail] = created
        self.assertEqual(detail[:2], (agregat.DETAIL_CHECK, "failure"))
        self.assertIn("preuve absente", detail[2])
        self.assertIn("psycopg (cancelled, preuve absente)", detail[3])
        self.assertIn("psql (cancelled, preuve absente)", detail[3])

    def test_une_famille_linux_absente_est_une_preuve_absente(self):
        for part, over in (("psycopg", {"psycopg": ""}),
                           ("psql", {"psql": ""})):
            with self.subTest(part=part):
                result = classify(**over)
                self.assertEqual(result["state"], agregat.RED)
                self.assertTrue(any(gap.startswith(part + " (")
                                    for gap in result["red"]), result["red"])

    def test_psql_annule_avec_psycopg_vert_ne_rachete_pas_la_preuve(self):
        result = classify(psycopg="success")
        self.assertEqual(result["state"], agregat.RED)
        self.assertTrue(any(gap.startswith("psql (cancelled")
                            for gap in result["missing"]), result["missing"])
        self.assertEqual(result["consigned"], [])

    def test_psql_non_planifie_reste_un_saut_legitime(self):
        result = classify(psycopg="success", psql="skipped", psql_parts="[]")
        self.assertEqual(result["state"], agregat.GREEN)
        self.assertEqual((result["missing"], result["consigned"]), ([], []))

    def test_une_annulation_macos_reste_non_bloquante(self):
        result = classify(psycopg="success", psql="success",
                          platform_macos="cancelled")
        self.assertEqual(result["state"], agregat.GREEN)
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS"])
        self.assertEqual(result["missing"], [])

    def test_aucune_part_reussie_reste_une_preuve_absente(self):
        # Décision #128512 : même avec macOS annulé, aucune part réussie ne
        # peut pas conclure vert.
        result = classify(psycopg="cancelled", psql="cancelled",
                          platform_macos="cancelled", fs_class="cancelled")
        self.assertEqual(result["state"], agregat.RED)
        self.assertIn("aucune part n'a conclu (preuve absente)",
                      result["missing"])
        self.assertEqual([row["part"] for row in result["consigned"]],
                         ["plateforme macOS", "autre système de fichiers"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
