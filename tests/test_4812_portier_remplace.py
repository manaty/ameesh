# SPDX-License-Identifier: AGPL-3.0-only
"""#4812/#4810 — le portier remplacé par le run de la PR n'est pas un non-concluant.

`ameesh ci run --full` annule le portier `paquet` de la tête avant d'étiqueter
la PR (#2876) : `couverture (PR ouverte sur cette tête)` finit CANCELLED et
`suite complète du paquet` SKIPPED, la suite complète tourne dans le run
`pull_request`. Consignés (#3085), ces deux checks ouvraient deux lots de
« CI non concluante » à chaque paquet (PR #3632, #3645).
"""
from __future__ import annotations

import unittest

from ameesh import fire, radar

from . import test_2271_fire_required_checks as f2271

SUITE = radar.PACKET_SUITE
COVERAGE = radar.PACKET_COVERAGE


def _packet(name, conclusion, *, suite_id=7):
    return {"__typename": "CheckRun", "name": name, "workflowName": "paquet",
            "status": "COMPLETED", "conclusion": conclusion, "checkSuiteId": suite_id}


def _tests(conclusion="SUCCESS"):
    return dict(f2271._check("tests", conclusion), workflowName="tests")


class PortierReplacedTest(unittest.TestCase):

    def ready(self, rollup):
        report: dict = {}
        ok, why = fire.ready_pr(f2271._status(rollup), target="main", project=None,
                                frozen=f2271.SHA, required=(("tests", None),),
                                slug="manaty/ameesh", gh=None, report=report)
        return ok, why, report

    def test_the_portier_replaced_by_a_green_pr_run_is_not_consigned(self):
        ok, why, report = self.ready([_tests(), _packet(COVERAGE, "CANCELLED"),
                                      _packet(SUITE, "SKIPPED")])
        self.assertTrue(ok, why)
        self.assertEqual(report.get("consigned") or [], [])

    def test_without_the_tests_workflow_aggregate_it_stays_consigned(self):
        # un `tests` vert d'un autre workflow ne prouve pas la suite de la PR
        ok, why, report = self.ready([f2271._check("tests"),
                                      _packet(COVERAGE, "CANCELLED"),
                                      _packet(SUITE, "SKIPPED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["%s (CANCELLED)" % COVERAGE,
                                               "%s (SKIPPED)" % SUITE])

    def test_a_cancelled_suite_stays_consigned(self):
        ok, why, report = self.ready([_tests(), _packet(COVERAGE, "CANCELLED"),
                                      _packet(SUITE, "CANCELLED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["%s (CANCELLED)" % SUITE])

    def test_the_cancelled_coverage_of_another_run_proves_nothing(self):
        ok, why, report = self.ready([_tests(), _packet(COVERAGE, "CANCELLED", suite_id=7),
                                      _packet(SUITE, "SKIPPED", suite_id=8)])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["%s (SKIPPED)" % SUITE])

    def test_a_red_pr_run_still_blocks(self):
        ok, _why, _ = self.ready([_tests("FAILURE"), _packet(COVERAGE, "CANCELLED"),
                                  _packet(SUITE, "SKIPPED")])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
