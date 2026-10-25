# SPDX-License-Identifier: AGPL-3.0-only
"""#3535 : une part tolérée ne rougit pas la tête par son check de flux.

`suite_reading` pose `job["tolerated"]` en comparant le nom du job aux
tolérances déclarées (`suite_tolerated`). Le check du flux L161c s'appelle
`tests (flux) <part>`, et sous le portier
`suite complète du paquet / tests (flux) <part>` : sans retirer ces préfixes,
la part tolérée reste lue comme négative par `ci_reading`.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import release_cutoff as release
from . import test_2448_release_cutoff as fixture

PACKET = "suite complète du paquet / "
FLUX = "tests (flux) "

#: #3186 : `ci_reading` lit aussi les checks requis de la cible ; aucun ici.
_REQUIRED = mock.patch.object(release, "required_checks", return_value=())


def setUpModule():
    _REQUIRED.start()


def tearDownModule():
    _REQUIRED.stop()


def rollup():
    return {"green": False, "acceptable": False, "runs": 1,
            "negative": ["tests"], "pending": [], "failed": ["tests"],
            "inconclusive": [], "missing": []}


def suite(jobs, *, run_id=600, workflow="tests"):
    return {"runs": [fixture._run(run_id, list(jobs), workflow=workflow)]}


class FlowToleranceTest(unittest.TestCase):
    def project(self, *tolerated):
        return release.ProjectRelease("ameesh", "/unused", github="acme/project",
                                      suite_tolerated=tuple(tolerated))

    def read(self, project, data):
        with mock.patch.object(release.radar, "commit_ci", return_value=rollup()), \
             mock.patch.object(release.radar, "commit_suite", return_value=data):
            return release.ci_reading(project, "a" * 40)

    def test_a_tolerated_part_does_not_redden_the_head_through_its_flow_check(self):
        # Témoin du panier : tests=success + part tolérée failure + flux
        # failure → pas de négatif.
        project = self.project("psql 1/16")
        data = suite([
            fixture._job("plan"),
            fixture._job("psql 1/16", "failure"),
            fixture._job(FLUX + "psql 1/16", "failure"),
            fixture._job("tests"),
        ])
        with mock.patch.object(release.radar, "commit_suite", return_value=data):
            reading = release.suite_reading(project, "a" * 40)
        jobs = reading["runs"][0]["jobs"]
        self.assertTrue(jobs[1]["tolerated"])
        self.assertTrue(jobs[2]["tolerated"])
        # Le libellé du check reste intact : la tolérance ne le réécrit pas.
        self.assertEqual(jobs[2]["name"], FLUX + "psql 1/16")
        result = self.read(project, data)
        self.assertTrue(result["green"], result)
        self.assertEqual(result["negative"], [])

    def test_the_packet_caller_prefix_is_stripped_before_the_flow_prefix(self):
        project = self.project("psql 1/16")
        data = suite([
            fixture._job(PACKET + "plan"),
            fixture._job(PACKET + "psql 1/16", "failure"),
            fixture._job(PACKET + FLUX + "psql 1/16", "failure"),
            fixture._job(PACKET + "tests"),
        ], workflow="paquet")
        with mock.patch.object(release.radar, "commit_suite", return_value=data):
            reading = release.suite_reading(project, "a" * 40)
        self.assertTrue(reading["runs"][0]["jobs"][2]["tolerated"])
        result = self.read(project, data)
        self.assertTrue(result["green"], result)
        self.assertEqual(result["negative"], [])

    def test_an_untolerated_flow_failure_still_blocks(self):
        project = self.project("psql 2/16")
        data = suite([
            fixture._job("plan"),
            fixture._job("psql 1/16", "failure"),
            fixture._job(FLUX + "psql 1/16", "failure"),
            fixture._job("tests"),
        ])
        result = self.read(project, data)
        self.assertFalse(result["green"], result)
        self.assertTrue(any("tests (flux) psql 1/16" in name
                            for name in result["negative"]), result)


if __name__ == "__main__":
    unittest.main()
