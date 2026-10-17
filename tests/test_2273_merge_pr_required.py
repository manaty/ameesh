# SPDX-License-Identifier: AGPL-3.0-only
"""#2273 : la fusion hors API exige les checks requis de la cible.

Constat #2255 : `ameesh work merge-pr` ne lisait la CI que par
`radar.ci_green`, qui compte `SKIPPED` comme vert, et ne vérifiait jamais la
présence des checks requis de la branche cible. Le geste pousse hors API : la
protection de branche ne le rattrape pas, et le rollup de la PR #1638
(couverture verte, « suite complète du paquet » sautée, check requis `tests`
absent) passait.

Le chemin applique désormais la lecture du feu (`fire.required_checks` :
protection de la forge complétée par `radar.projects.<p>.required_checks`) et
sa comparaison (`fire._required_gap` : absent, non conclu ou `SKIPPED` ne
prouve rien). Quand la protection impose une application (`checks[].app_id`),
la provenance se lit sur la tête exacte (`commits/<sha>/check-runs`) : le
rollup de `gh pr view` ne porte ni application ni source, donc un statut de
commit ou un homonyme d'une autre application ne satisfait pas l'exigence. Une
protection ou une source illisible sans déclaration refuse.
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import merge_pr, merge_queue, radar

HEAD = "9e790414749cdb05e5c446a8e8291f26eaf039e7"
#: le rollup exact de la PR #1638 : couverture verte, suite du paquet sautée,
#: aucun check `tests`.
INCIDENT = [
    {"__typename": "CheckRun", "name": "couverture (PR ouverte sur cette tête)",
     "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "suite complète du paquet",
     "status": "COMPLETED", "conclusion": "SKIPPED"},
]
#: la protection lue à la forge pour `main` le 17/10 : `tests`, app Actions.
PROTECTED = {"required_status_checks": {
    "strict": False, "contexts": ["tests"],
    "checks": [{"context": "tests", "app_id": 15368}]}}


def _check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


def _context(name, state="SUCCESS"):
    return {"__typename": "StatusContext", "context": name, "state": state}


def _run(name, app_id=15368, conclusion="success", status="completed", sha=HEAD):
    """Un check run de la tête, tel que le lit `commits/<sha>/check-runs`."""
    return {"name": name, "status": status, "conclusion": conclusion,
            "head_sha": sha, "app": {"id": app_id}}


def _project(**extra):
    return radar.Project("ameesh", "/r", github="manaty/ameesh", **extra)


class FakeGh:
    """Le peu de `gh` que lit `merge_pr` : la protection de la cible et les
    check runs de la tête exacte (leur application source)."""

    def __init__(self, *, protection=PROTECTED, error="", check_runs=(),
                 check_error="", rows=()):
        self.protection = protection
        self.error = error
        self.check_runs = list(check_runs)
        self.check_error = check_error
        self.rows = list(rows)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["pr", "list"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.rows),
                                         stderr="")
        if argv[:1] == ["api"] and len(argv) > 1 and argv[1].endswith("/protection"):
            if self.error:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=self.error)
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps(self.protection), stderr="")
        path = next((arg for arg in argv if "/check-runs" in arg), "")
        if path:
            if self.check_error:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr=self.check_error)
            page = {"total_count": len(self.check_runs),
                    "check_runs": self.check_runs}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps([page]),
                                         stderr="")
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="unexpected gh %s" % " ".join(argv))


def _view(rollup, **over):
    data = {"number": 1638, "state": "OPEN", "isDraft": False,
            "baseRefName": "main", "baseRefOid": "", "headRefOid": HEAD,
            "headRefName": "lot/7-work", "mergeStateStatus": "CLEAN",
            "mergeCommit": None, "url": "", "statusCheckRollup": rollup}
    data.update(over)
    return data


class RequiredChecksRefusalTest(unittest.TestCase):
    """Le refus nomme le check que la tête ne prouve pas."""

    def refusal(self, gh, rollup, *, project=None, slug="manaty/ameesh",
                target="main", head=HEAD):
        return merge_pr._required_refusal(slug, target, project, rollup,
                                          head=head, gh=gh)

    def test_the_incident_rollup_is_refused_for_the_missing_tests_check(self):
        reason = self.refusal(FakeGh(), INCIDENT)
        self.assertIn("tests (absent)", reason)
        self.assertIn("main", reason)

    def test_a_skipped_required_check_is_refused(self):
        gh = FakeGh(check_runs=[_run("tests", conclusion="skipped")])
        reason = self.refusal(gh, [_check("tests", "SKIPPED")])
        self.assertIn("tests (SKIPPED)", reason)

    def test_a_pending_required_check_is_refused(self):
        gh = FakeGh(check_runs=[_run("tests", conclusion=None, status="in_progress")])
        reason = self.refusal(gh, [_check("tests", None, "IN_PROGRESS")])
        self.assertIn("tests (PENDING)", reason)

    def test_a_required_check_from_the_imposed_app_is_proven(self):
        gh = FakeGh(check_runs=[_run("tests")])
        self.assertEqual(self.refusal(gh, [_check("tests")]), "")
        self.assertTrue(any("/commits/%s/check-runs" % HEAD in " ".join(call)
                            for call in gh.calls))

    def test_a_check_from_another_app_is_refused(self):
        # B1 : même nom, même `SUCCESS`, mais une autre application : le
        # fournisseur imposé par la protection n'est pas prouvé.
        gh = FakeGh(check_runs=[_run("tests", app_id=99999)])
        reason = self.refusal(gh, [_check("tests")])
        self.assertIn("tests (app 99999, expected 15368)", reason)

    def test_a_check_name_with_an_extra_space_is_not_the_required_name(self):
        # #2271 B2 : le nom est exact, l'espace final compte ; `merge_pr`
        # réutilise la comparaison du feu, sans la réécrire.
        gh = FakeGh(check_runs=[_run("tests ")])
        reason = self.refusal(gh, [_check("tests ")])
        self.assertIn("tests (absent)", reason)

    def test_a_commit_status_cannot_prove_an_imposed_app(self):
        # B1 : le rollup de `gh pr view` ne porte pas la source ; un statut de
        # commit `tests` vert ne satisfait pas une application imposée.
        reason = self.refusal(FakeGh(), [_context("tests")])
        self.assertIn("tests (source absent)", reason)

    def test_an_unreadable_check_source_refuses(self):
        gh = FakeGh(check_error="gh: Resource not accessible by integration (HTTP 403)")
        reason = self.refusal(gh, [_check("tests")])
        self.assertIn("cannot be proven", reason)
        self.assertIn("403", reason)

    def test_a_missing_frozen_head_cannot_prove_an_imposed_app(self):
        reason = self.refusal(FakeGh(), [_check("tests")], head="")
        self.assertIn("cannot be proven", reason)
        self.assertIn("no frozen head", reason)

    def test_a_github_target_is_needed_to_read_the_protection(self):
        reason = self.refusal(FakeGh(), [_check("tests")], target="")
        self.assertIn("cannot be proven", reason)
        self.assertIn("no target", reason)

    def test_a_legacy_context_is_proven_by_the_rollup(self):
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests"], "checks": []}})
        self.assertEqual(self.refusal(gh, [_context("tests")]), "")
        self.assertFalse(any("/check-runs" in " ".join(call) for call in gh.calls))

    def test_a_required_check_green_on_all_its_entries_is_proven(self):
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests"], "checks": []}})
        self.assertEqual(self.refusal(gh, [_check("tests"),
                                           _context("tests")]), "")
        self.assertTrue(any("/protection" in call[1] for call in gh.calls))

    def test_an_unreadable_protection_without_a_declaration_refuses(self):
        gh = FakeGh(error="gh: Resource not accessible by integration (HTTP 403)")
        reason = self.refusal(gh, INCIDENT)
        self.assertIn("cannot be proven", reason)
        self.assertIn("403", reason)

    def test_a_project_declaration_replaces_an_unreadable_protection(self):
        gh = FakeGh(error="gh: Resource not accessible by integration (HTTP 403)")
        self.assertEqual(self.refusal(gh, [_check("tests")],
                                      project=_project(required_checks=("tests",))), "")

    def test_the_declaration_completes_the_forge_without_weakening_it(self):
        gh = FakeGh(error="gh: Branch not protected (HTTP 404)")
        # la protection ne demande rien : la déclaration seule s'applique
        self.assertEqual(self.refusal(gh, INCIDENT), "")
        reason = self.refusal(gh, INCIDENT,
                              project=_project(required_checks=("tests",)))
        self.assertIn("tests (absent)", reason)

    def test_a_local_transport_has_no_forge_protection_to_read(self):
        gh = FakeGh()
        with tempfile.TemporaryDirectory() as repo:
            self.assertEqual(self.refusal(gh, INCIDENT, slug=repo), "")
            self.assertEqual(self.refusal(gh, INCIDENT, slug="file://" + repo), "")
        self.assertEqual(gh.calls, [])

    def test_a_malformed_slug_refuses_instead_of_passing_as_local(self):
        """#2289 : seul un transport local déclaré (chemin réel existant)
        dispense des checks requis ; un slug vide, malformé ou un chemin
        absent refuse, sans dépendre de `_bind_repo_identity`."""
        gh = FakeGh(check_runs=[_run("tests")])
        rollup = [_check("tests")]
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "absent")
            for slug in ("", "  ", "manaty", "manaty/ameesh/extra", "manaty /ameesh",
                         "https://example.org/x", missing):
                with self.subTest(slug=slug):
                    reason = self.refusal(gh, rollup, slug=slug)
                    self.assertIn("cannot be proven", reason)
                    self.assertIn("neither owner/repo nor a local transport", reason)
        self.assertEqual(gh.calls, [])

    def test_a_padded_github_key_is_read_as_that_key(self):
        gh = FakeGh(check_runs=[_run("tests")])
        self.assertEqual(self.refusal(gh, [_check("tests")], slug=" manaty/ameesh "), "")
        self.assertTrue(any("repos/manaty/ameesh/" in arg
                            for call in gh.calls for arg in call), gh.calls)


class AdmissionTest(unittest.TestCase):
    """`_admission` refuse avant de rejouer la file, et la file voit la
    même tête : le chemin direct n'est jamais plus faible que la file."""

    def _admission(self, gh, view, *, target="main", head=HEAD):
        captured = {}

        def fake_readiness(db, lot, repo, *, ci=None, clone=None, project=None,
                           cfg=None):
            captured["ci"] = ci(view["headRefName"]) if ci else None
            if captured["ci"] and not captured["ci"]["green"]:
                raise merge_queue.QueueError("the CI of PR #%s is not green"
                                             % view["number"])
            return {"target": "main", "frozen_sha": HEAD}

        with mock.patch.object(merge_queue, "readiness", side_effect=fake_readiness):
            try:
                ready = merge_pr._admission(None, None, 7, "manaty/ameesh",
                                            _project(), "/tmp/x", view,
                                            slug="manaty/ameesh", target=target,
                                            head=head, gh=gh)
            except merge_pr.MergePrError as exc:
                return None, captured, str(exc)
        return ready, captured, ""

    def test_the_incident_pr_is_refused_before_the_queue_replay(self):
        ready, captured, error = self._admission(FakeGh(), _view(INCIDENT))
        self.assertIsNone(ready)
        self.assertIn("tests (absent)", error)
        self.assertEqual(captured, {}, "the missing check must refuse before the replay")

    def test_the_command_target_binds_even_when_the_view_has_no_base(self):
        ready, _, error = self._admission(FakeGh(), _view(INCIDENT, baseRefName=""))
        self.assertIsNone(ready)
        self.assertIn("tests (absent)", error)

    def test_a_skipped_required_check_is_refused(self):
        gh = FakeGh(check_runs=[_run("tests", conclusion="skipped")])
        ready, _, error = self._admission(gh, _view([_check("tests", "SKIPPED")]))
        self.assertIsNone(ready)
        self.assertIn("tests (SKIPPED)", error)

    def test_an_unreadable_check_source_refuses_before_the_queue_replay(self):
        gh = FakeGh(check_error="gh: Resource not accessible by integration (HTTP 403)")
        ready, captured, error = self._admission(gh, _view([_check("tests")]))
        self.assertIsNone(ready)
        self.assertIn("cannot be proven", error)
        self.assertEqual(captured, {})

    def test_a_green_required_check_goes_through_and_the_queue_sees_it(self):
        gh = FakeGh(check_runs=[_run("tests")])
        ready, captured, error = self._admission(gh, _view([_check("tests")]))
        self.assertEqual(error, "")
        self.assertEqual(ready["target"], "main")
        self.assertTrue(captured["ci"]["green"])
        self.assertEqual(captured["ci"]["head"], HEAD)
        self.assertTrue(any("/commits/%s/check-runs" % HEAD in " ".join(call)
                            for call in gh.calls))

    def test_the_direct_path_never_asks_for_admin_rights(self):
        import inspect

        source = inspect.getsource(merge_pr)
        self.assertNotIn("--admin", source)
        self.assertIn("--force-with-lease", source)


if __name__ == "__main__":
    unittest.main()
