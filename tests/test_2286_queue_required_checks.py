# SPDX-License-Identifier: AGPL-3.0-only
"""#2286 : la voie file de fusion ne contourne pas les checks requis.

Constat de la relecture de #2271 : `ready_pr` (feu direct) exige les checks
requis de la branche cible, mais la voie file (`land`, projet `land=local`
comme Nexlink) ne les vérifiait que si `ci_required` était vrai — et Nexlink
n'a pas de protection de branche, donc la déclaration
`radar.projects.<p>.required_checks` est la seule source.

`fire.required_on_head` lit cette déclaration (complétée par la protection
quand elle se lit) et prouve chaque contexte **sur la tête exacte** : un check
absent, sauté, en échec ou produit par une autre application ne passe pas.
`fire.land_guard` l'appelle au dernier geste, pour tout atterrissage de la
file, avec ou sans jalon de feu. Sans déclaration, rien ne change.

#2306 : une lecture passagère des checks (HTTP 5xx, délai, coupure) ne prouve
pas un manque — la ligne de file reste en place et repart au passage suivant,
au lieu d'éjecter ; seuls un manque prouvé, un refus de droits (4xx) ou une
réponse malformée éjectent. La cible vide, qui sautait la garde, est refusée,
et les statuts sont paginés jusqu'au `total_count` annoncé (un total de 100
n'est plus pris pour une troncature).
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import unittest
from unittest import mock

from ameesh import fire, land

from .test_l160e2_land import _Land
from .test_l160b_radar import _git

SHA = "4cc0c5f6b69d382dbf9e06e4c883ed90cefcf5e6"
PROTECTED = {"required_status_checks": None}


class FakeHeadGh:
    """Le peu de `gh` lu par la garde : protection, check runs, statuts."""

    def __init__(self, *, runs=(), statuses=(), protection=PROTECTED,
                 runs_error="", statuses_error="", protection_error="",
                 runs_body=None, statuses_total=None):
        self.runs = list(runs)
        self.statuses = list(statuses)
        self.protection = protection
        self.runs_error = runs_error
        self.statuses_error = statuses_error
        self.protection_error = protection_error
        self.runs_body = runs_body
        self.statuses_total = statuses_total
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:1] == ["api"] and len(argv) > 1 and argv[1].endswith("/protection"):
            if self.protection_error:
                return subprocess.CompletedProcess(argv, 1, "", self.protection_error)
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.protection), "")
        path = next((arg for arg in argv if "/check-runs" in arg), "")
        if path:
            if self.runs_error:
                return subprocess.CompletedProcess(argv, 1, "", self.runs_error)
            if self.runs_body is not None:
                return subprocess.CompletedProcess(argv, 0, self.runs_body, "")
            page = {"total_count": len(self.runs), "check_runs": self.runs}
            return subprocess.CompletedProcess(argv, 0, json.dumps([page]), "")
        path = next((arg for arg in argv if "/commits/" in arg and "/status" in arg), "")
        if path:
            if self.statuses_error:
                return subprocess.CompletedProcess(argv, 1, "", self.statuses_error)
            total = (len(self.statuses) if self.statuses_total is None
                     else self.statuses_total)
            pages = [{"sha": SHA, "total_count": total,
                      "statuses": self.statuses[i:i + 100]}
                     for i in range(0, len(self.statuses), 100)]
            if not pages:
                pages = [{"sha": SHA, "total_count": total, "statuses": []}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(pages), "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected gh %s" % " ".join(argv))


def _project(**extra):
    from ameesh import radar
    extra.setdefault("land", "local")
    return radar.Project("p", "/r", github="o/r", **extra)


def _run(name="tests", *, app_id=15368, conclusion="success", status="completed",
         sha=SHA):
    return {"name": name, "status": status, "conclusion": conclusion,
            "head_sha": sha, "app": {"id": app_id}}


def _status(context="tests", state="success"):
    return {"context": context, "state": state}


class RequiredOnHeadTest(unittest.TestCase):
    def _gaps(self, gh, *, project=None, target="main", sha=SHA):
        return fire.required_on_head(project or _project(required_checks=("tests",)),
                                     target, sha, gh=gh)

    def test_a_green_check_on_the_head_proves_the_declared_context(self):
        self.assertEqual(self._gaps(FakeHeadGh(runs=[_run()])), [])

    def test_a_missing_check_is_not_proven(self):
        self.assertEqual(self._gaps(FakeHeadGh()), ["tests (absent)"])

    def test_a_skipped_check_is_not_a_green_one(self):
        self.assertEqual(self._gaps(FakeHeadGh(runs=[_run(conclusion="skipped")])),
                         ["tests (SKIPPED)"])

    def test_a_check_from_another_application_does_not_prove_an_imposed_context(self):
        # la protection impose `tests` app 15368 ; un homonyme d'une autre
        # application ne vaut pas ce check
        protection = {"required_status_checks": {
            "contexts": ["tests"], "checks": [{"context": "tests", "app_id": 15368}]}}
        gh = FakeHeadGh(protection=protection, runs=[_run(app_id=42)])
        self.assertEqual(self._gaps(gh), ["tests (app 42, expected 15368)"])

    def test_a_commit_status_proves_a_context_without_imposed_app(self):
        self.assertEqual(self._gaps(FakeHeadGh(statuses=[_status()])), [])

    def test_an_unreadable_check_source_fails_closed(self):
        with self.assertRaises(land.TransientLandError):
            self._gaps(FakeHeadGh(runs_error="gh: HTTP 502"))

    def test_an_unreadable_status_source_fails_closed(self):
        with self.assertRaises(land.TransientLandError):
            self._gaps(FakeHeadGh(statuses_error="gh: HTTP 502"))

    def test_a_connection_closed_with_eof_is_transient(self):
        # gh peut rendre EOF seul quand la connexion ferme sans réponse.
        detail = 'Get "https://api.github.com/repos/o/r": EOF'
        for source in ("runs_error", "statuses_error"):
            with self.subTest(source=source), self.assertRaises(land.TransientLandError):
                self._gaps(FakeHeadGh(**{source: detail}))

    def test_eof_in_the_requested_path_does_not_make_a_404_transient(self):
        detail = 'gh: Not Found (https://api.github.com/repos/eof/r) (HTTP 404)'
        with self.assertRaises(land.LandError) as raised:
            self._gaps(FakeHeadGh(runs_error=detail))
        self.assertNotIsInstance(raised.exception, land.TransientLandError)

    def test_a_refusal_of_rights_is_not_retried(self):
        # un 4xx ne dit pas « réessaie » : la source est refusée, pas passagère
        with self.assertRaises(land.LandError) as raised:
            self._gaps(FakeHeadGh(runs_error="gh: Not Found (404)"))
        self.assertNotIsInstance(raised.exception, land.TransientLandError)

    def test_a_malformed_check_source_is_not_a_transient_error(self):
        with self.assertRaises(land.LandError) as raised:
            self._gaps(FakeHeadGh(runs_body="not json"))
        self.assertNotIsInstance(raised.exception, land.TransientLandError)

    def test_without_a_declaration_nothing_is_read_and_nothing_is_added(self):
        # la protection seule reste portée par la forge au moment de la
        # fusion d'une PR ; cette garde de la voie file ne s'active que sur
        # une déclaration, pour ne jamais retirer au comportement existant
        gh = FakeHeadGh()
        self.assertEqual(fire.required_on_head(_project(), "main", SHA, gh=gh), [])
        self.assertEqual(gh.calls, [])

    def test_an_unreadable_protection_falls_back_to_the_declaration(self):
        gh = FakeHeadGh(protection_error="gh: Resource not accessible by integration (HTTP 403)",
                        runs=[_run()])
        self.assertEqual(self._gaps(gh), [])

    def test_a_transient_protection_error_does_not_fall_back_to_the_declaration(self):
        gh = FakeHeadGh(protection_error="gh: Service Unavailable (HTTP 503)",
                        runs=[_run()])
        with self.assertRaises(land.TransientLandError):
            self._gaps(gh)

    def test_a_full_status_page_proves_the_context(self):
        # #2306 N3 : un total_count de 100 n'est plus pris pour une troncature
        gh = FakeHeadGh(statuses=[_status()] * 100)
        self.assertEqual(self._gaps(gh), [])

    def test_more_statuses_are_paginated(self):
        gh = FakeHeadGh(statuses=[_status()] * 150)
        self.assertEqual(self._gaps(gh), [])

    def test_a_status_total_not_covered_by_the_pages_is_refused(self):
        gh = FakeHeadGh(statuses=[_status()], statuses_total=101)
        with self.assertRaises(land.LandError):
            self._gaps(gh)


class LandGuardRequiredChecksTest(unittest.TestCase):
    """La garde du dernier geste exige les checks requis, même sans feu."""

    def setUp(self):
        patch = mock.patch.object(fire.work, "milestones", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def _guard(self, gh, *, project=None, target="main"):
        return fire.land_guard(None, 7, SHA, project=project or _project(
            required_checks=("tests",)), target=target, gh=gh)

    def test_a_missing_required_check_refuses_the_landing(self):
        ok, why = self._guard(FakeHeadGh())
        self.assertFalse(ok)
        self.assertIn("tests (absent)", why)
        self.assertIn("landing refused", why)

    def test_a_green_required_check_leaves_the_way_free(self):
        ok, why = self._guard(FakeHeadGh(runs=[_run()]))
        self.assertTrue(ok, why)

    def test_the_checks_are_read_on_the_frozen_head(self):
        gh = FakeHeadGh(runs=[_run()])
        ok, why = self._guard(gh)
        self.assertTrue(ok, why)
        self.assertTrue(any(SHA in " ".join(call) for call in gh.calls))

    def test_pr_mode_is_left_to_the_forge_and_to_the_fire(self):
        # en mode pr, les checks de PR vivent sur la ref de fusion, pas sur le
        # commit gelé : la garde de la file locale ne les lit pas (la forge et
        # `ready_pr` les exigent au bon endroit)
        gh = FakeHeadGh()
        project = _project(land="pr", required_checks=("tests",))
        self.assertEqual(self._guard(gh, project=project), (True, ""))
        self.assertEqual(gh.calls, [])

    def test_without_a_project_the_guard_keeps_its_old_contract(self):
        # les appelants historiques (tests, appels directs) ne fournissent pas
        # de projet : la garde du feu seule s'applique
        self.assertEqual(fire.land_guard(None, 7, SHA), (True, ""))

    def test_an_empty_target_refuses_the_landing(self):
        # #2306 N2 : la cible vide sautait la garde ; elle est refusée
        ok, why = self._guard(FakeHeadGh(runs=[_run()]), target="")
        self.assertFalse(ok)
        self.assertIn("no target branch known", why)
        self.assertIn("landing refused", why)

    def test_a_transient_read_propagates_for_a_retry(self):
        # #2306 N1 : la lecture passagère n'est pas convertie en refus prouvé ;
        # `land` la traduit en nouvelle tentative sans éjecter
        with self.assertRaises(land.TransientLandError):
            self._guard(FakeHeadGh(runs_error="gh: HTTP 503"))


class LocalLandingRequiredChecksTest(_Land):
    """Bout en bout de la voie file locale : ni poussée ni fusion sans preuve."""

    def setUp(self):
        super().setUp()
        self.project = dataclasses.replace(self.project, required_checks=("tests",))

    def test_a_missing_required_check_ejects_before_any_push(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4, gh=FakeHeadGh())
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("tests (absent)", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertIn("required checks", self.sent[-1][1])

    def test_a_green_required_check_lands(self):
        self.queued(4)
        out = self.land(4, gh=FakeHeadGh(runs=[_run(sha=self.tip(4))]))
        self.assertEqual(out["result"], "landed", out)
        self.assertIn("ameesh-work: 4", self.origin_log())

    def test_an_unreadable_source_keeps_the_line_for_retry(self):
        # #2306 N1 : un 502 passager ne sort pas le lot de la file ; la raison
        # est consignée, la ligne reste `queued` et repart au passage suivant
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4, gh=FakeHeadGh(runs_error="gh: HTTP 502"))
        self.assertEqual(out["result"], "retry", out)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.assertIn("retried at the next pass", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(self.sent, [])

    def test_a_closed_connection_retries_then_lands_without_requeueing(self):
        self.queued(4)
        entry = dict(self.fake.queue_rows[4])
        before = _git(self.origin, "rev-parse", "main")
        locks = {}
        out = self.land(4, gh=FakeHeadGh(runs_error='Get "https://api.github.com/": EOF'),
                        locks=locks)
        self.assertEqual(out["result"], "retry", out)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.assertEqual(self.fake.queue_rows[4]["generation"], entry["generation"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(locks, {})
        self.assertIsNone(self.locks.held["p:main"][0])
        self.assertEqual(self.sent, [])
        out = self.land(4, gh=FakeHeadGh(runs=[_run(sha=self.tip(4))]), locks=locks)
        self.assertEqual(out["result"], "landed", out)

    def test_a_refusal_of_rights_ejects_instead_of_merging(self):
        # #2306 N1 : un 404 n'est pas passager — un manque de droits ou une
        # source absente reste un refus prouvé, qui éjecte
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4, gh=FakeHeadGh(runs_error="gh: Not Found (404)"))
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("cannot be proven", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_a_transient_protection_read_retries_instead_of_landing(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        gh = FakeHeadGh(protection_error="gh: Service Unavailable (HTTP 503)",
                        runs=[_run(sha=self.tip(4))])
        locks = {}
        out = self.land(4, gh=gh, locks=locks)
        self.assertEqual(out["result"], "retry", out)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(locks, {})
        self.assertIsNone(self.locks.held["p:main"][0])
        self.assertEqual(self.sent, [])
        # La protection retrouvée exige plus que la déclaration du projet.
        gh.protection_error = ""
        gh.protection = {"required_status_checks": {"contexts": ["tests", "security"]}}
        out = self.land(4, gh=gh, locks=locks)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("security (absent)", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_a_malformed_response_ejects_instead_of_merging(self):
        # #2306 N1 : une réponse malformée éjecte (elle ne prouve rien et ne
        # guérira pas au passage suivant)
        self.queued(4)
        out = self.land(4, gh=FakeHeadGh(runs_body="not json"))
        self.assertEqual(out["result"], "ejected", out)


if __name__ == "__main__":
    unittest.main()
