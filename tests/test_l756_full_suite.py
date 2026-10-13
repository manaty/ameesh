# SPDX-License-Identifier: AGPL-3.0-only
"""L756 (#566) : la suite **complète** lue sur un commit exact.

`radar.full_suite_ci` ne retient que les exécutions déclenchées par une
poussée ou un lancement manuel — jamais une PR, dont le plan peut avoir été
impacté seulement (ou vide, quand elle ne change qu'une version). C'est cette
lecture qui certifie la tête gelée d'un lot de classe release.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from ameesh import plan_github, radar

from .test_l160b_radar import _git
from .test_l160g_nexlink import _Gh, _run


class FullSuiteReaderTest(unittest.TestCase):
    project = radar.Project("a", "/r", github="o/ameesh")

    def test_only_a_full_trigger_counts(self):
        gh = _Gh([dict(_run("tests"), event="pull_request", conclusion="success")])
        out = radar.full_suite_ci(self.project, "a" * 40, gh=gh)
        self.assertEqual((out["runs"], out["green"]), (0, False))
        gh = _Gh([dict(_run("tests"), event="push", conclusion="success")])
        self.assertTrue(radar.full_suite_ci(self.project, "a" * 40, gh=gh)["green"])
        gh = _Gh([dict(_run("tests"), event="workflow_dispatch", conclusion="success")])
        self.assertTrue(radar.full_suite_ci(self.project, "a" * 40, gh=gh)["green"])

    def test_the_latest_full_run_of_each_workflow_counts(self):
        rows = [dict(_run("tests", conclusion="failure", at="2026-10-12T19:00:00Z"),
                     event="push"),
                dict(_run("tests", at="2026-10-12T20:00:00Z"), event="push")]
        self.assertTrue(radar.full_suite_ci(self.project, "a" * 40, gh=_Gh(rows))["green"])
        rows[1]["conclusion"] = "failure"
        out = radar.full_suite_ci(self.project, "a" * 40, gh=_Gh(rows))
        self.assertEqual((out["green"], out["failed"]), (False, ["tests"]))

    def test_pending_and_empty_are_reported(self):
        out = radar.full_suite_ci(self.project, "a" * 40,
                                  gh=_Gh([dict(_run("tests", "in_progress", None),
                                               event="push")]))
        self.assertEqual((out["green"], out["pending"]), (False, ["tests"]))
        out = radar.full_suite_ci(self.project, "a" * 40, gh=_Gh([]))
        self.assertEqual((out["runs"], out["green"]), (0, False))

    def test_an_unreadable_or_wrong_read_is_never_green(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-l756-nogithub-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _git(tmp, "init", "-q")
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(radar.Project("a", tmp), "a" * 40, gh=_Gh([]))
        rows = [dict(_run("tests"), event="push", headSha="b" * 40)]
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40, gh=_Gh(rows))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_Gh([dict(_run("t"), event="push",
                                             workflowDatabaseId=None)]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_Gh([dict(_run("t"), event="push", createdAt=None)]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_Gh([dict(_run("t"), event="push")] * 100))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
