# SPDX-License-Identifier: AGPL-3.0-only
"""#3008 : un re-run vert sur la même tête lève le check requis rouge.

Constat (auditeur, 23/10) : avec une application imposée (`checks[].app_id`),
`fire._required_gap` lisait la provenance brute de `_head_check_runs`, sans la
sélection des derniers checks que #2524 applique au rollup. Un re-run vert sur
la même tête laissait l'ancien run `FAILURE` dans la liste : porte rouge.

Les check runs portent désormais leur date et leur suite ; seul le run
strictement le plus récent d'une (application, suite, nom) compte. Date ou
suite absente, ex aequo : toutes les entrées restent, un échec n'est jamais
masqué. Le rollup construit par `required_on_head` porte la suite dans sa
clé : deux workflows homonymes ne se masquent pas. Témoins sans base.
"""
from __future__ import annotations

import json
import types
import unittest

from ameesh import fire, radar

from tests.test_2271_fire_required_checks import SHA, FakeGh, _project

APP = 15368
REQUIRED = (("tests", APP),)


def _run(conclusion, *, started="2026-10-22T10:00:00Z", suite=1, app_id=APP,
         status="completed", name="tests"):
    run = {"name": name, "status": status, "conclusion": conclusion,
           "head_sha": SHA, "app": {"id": app_id}}
    if started is not None:
        run["started_at"] = started
    if suite is not None:
        run["check_suite"] = {"id": suite}
    return run


class StatusGh(FakeGh):
    """`FakeGh` qui lit aussi les statuts de commit de la tête (aucun)."""

    def __call__(self, argv, **kwargs):
        if any(arg.endswith("/status?per_page=100") for arg in argv):
            self.calls.append(list(argv))
            page = {"state": "success", "total_count": 0, "statuses": []}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps([page]),
                                         stderr="")
        return super().__call__(argv, **kwargs)


class RequiredGapRerunTest(unittest.TestCase):

    def _gap(self, *runs):
        provenance = fire._head_check_runs("manaty/ameesh", SHA,
                                           gh=FakeGh(check_runs=runs))
        return fire._required_gap(REQUIRED, (), provenance)

    def test_a_green_rerun_after_a_red_run_is_green(self):
        self.assertEqual(self._gap(_run("failure", started="2026-10-22T10:00:00Z"),
                                   _run("success", started="2026-10-22T10:30:00Z")), [])

    def test_a_red_rerun_after_a_green_run_is_red(self):
        self.assertEqual(self._gap(_run("success", started="2026-10-22T10:00:00Z"),
                                   _run("failure", started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_a_missing_date_keeps_the_failure(self):
        self.assertEqual(self._gap(_run("failure", started=None),
                                   _run("success", started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_an_unreadable_date_keeps_the_failure(self):
        self.assertEqual(self._gap(_run("failure", started="hier"),
                                   _run("success", started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_a_tie_keeps_the_failure(self):
        self.assertEqual(self._gap(_run("success"), _run("failure")),
                         ["tests (FAILURE)"])

    def test_a_missing_suite_keeps_the_failure(self):
        self.assertEqual(self._gap(_run("failure", suite=None,
                                        started="2026-10-22T10:00:00Z"),
                                   _run("success", suite=None,
                                        started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_two_homonymous_suites_one_red_is_red(self):
        self.assertEqual(self._gap(_run("failure", suite=1,
                                        started="2026-10-22T10:00:00Z"),
                                   _run("success", suite=2,
                                        started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_a_newer_run_of_another_app_does_not_hide_the_failure(self):
        self.assertEqual(self._gap(_run("failure", started="2026-10-22T10:00:00Z"),
                                   _run("success", app_id=99,
                                        started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_a_pending_rerun_is_not_green(self):
        self.assertEqual(self._gap(_run("success", started="2026-10-22T10:00:00Z"),
                                   _run("", status="in_progress",
                                        started="2026-10-22T10:30:00Z")),
                         ["tests (PENDING)"])

    def test_the_head_reader_keeps_the_date_and_the_suite(self):
        runs = fire._head_check_runs("manaty/ameesh", SHA, gh=FakeGh(check_runs=[
            _run("success", suite=7), _run("success", suite=True, started=3)]))
        self.assertEqual([(r["started_at"], r["check_suite_id"]) for r in runs],
                         [("2026-10-22T10:00:00Z", 7), (None, None)])


class RequiredOnHeadRerunTest(unittest.TestCase):
    """Le chemin de la file (`required_on_head`), sans application imposée :
    le rollup construit des check runs porte la suite dans la clé de
    `radar.latest_rollup`."""

    PROTECTION = {"required_status_checks": {"strict": False, "contexts": ["tests"]}}

    def _gap(self, *runs):
        gh = StatusGh(check_runs=runs, protection=self.PROTECTION)
        return fire.required_on_head(_project(required_checks=("tests",)), "main",
                                     SHA, gh=gh)

    def test_a_green_rerun_after_a_red_run_is_green(self):
        self.assertEqual(self._gap(_run("failure", started="2026-10-22T10:00:00Z"),
                                   _run("success", started="2026-10-22T10:30:00Z")), [])

    def test_two_homonymous_suites_one_red_is_red(self):
        self.assertEqual(self._gap(_run("failure", suite=1,
                                        started="2026-10-22T10:00:00Z"),
                                   _run("success", suite=2,
                                        started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_without_a_suite_the_date_is_not_used(self):
        self.assertEqual(self._gap(_run("failure", suite=None,
                                        started="2026-10-22T10:00:00Z"),
                                   _run("success", suite=None,
                                        started="2026-10-22T10:30:00Z")),
                         ["tests (FAILURE)"])

    def test_the_gh_rollup_keys_are_unchanged(self):
        rollup = [{"__typename": "CheckRun", "workflowName": "tests", "name": "tests",
                   "conclusion": "FAILURE", "startedAt": "2026-10-22T10:00:00Z"},
                  {"__typename": "CheckRun", "workflowName": "tests", "name": "tests",
                   "conclusion": "SUCCESS", "startedAt": "2026-10-22T10:30:00Z"}]
        self.assertEqual([r["conclusion"] for r in radar.latest_rollup(rollup)],
                         ["SUCCESS"])


if __name__ == "__main__":
    unittest.main()
