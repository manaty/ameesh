# SPDX-License-Identifier: AGPL-3.0-only
"""#3085 — seul un test négatif bloque un paquet, un feu ou une release.

Règle owner du 23/10/2026 (canon home §3, PR #153 `f178e568`) : « les tests
qui passent pas mais qui ne sont pas negatifs ne sont pas bloquant ». Un
résultat **non concluant** (sauté, neutralisé, annulé pour quota, expiré,
démarrage raté, statut en erreur) se consigne et n'arrête rien ; un `FAILURE`
exécuté reste un rouge prouvé qui bloque ; un check encore en cours attend.

Témoins sans réseau ni base : le faux `gh` de #2271 pour `ready_pr`, le banc
de paquet de #2813 pour `packet_automerge`, la lecture CI de #l160g pour
`commit_ci`, et les lectures injectées de #2448 pour la porte de release.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import fire, packet_automerge, prevalidation_calls, radar, release_cutoff

from . import test_2271_fire_required_checks as f2271
from . import test_2448_release_cutoff as r2448
from . import test_2813_packet_auto_merge as banc
from . import test_l160g_nexlink as l160g


class ClassificationTest(unittest.TestCase):
    """Le classifieur pur de la règle."""

    def test_a_skipped_or_cancelled_check_is_inconclusive_not_red(self):
        for conclusion in ("SKIPPED", "NEUTRAL", "CANCELLED", "TIMED_OUT",
                           "STALE", "STARTUP_FAILURE", "ACTION_REQUIRED"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(radar.check_result(f2271._check("tests", conclusion)),
                                 radar.CI_INCONCLUSIVE_RESULT)
                self.assertEqual(radar.ci_state([f2271._check("tests", conclusion)]),
                                 "green")

    def test_a_failure_is_negative_and_still_blocks(self):
        self.assertEqual(radar.check_result(f2271._check("tests", "FAILURE")),
                         radar.CI_NEGATIVE_RESULT)
        self.assertEqual(radar.ci_state([f2271._check("tests", "FAILURE")]), "red")

    def test_a_check_still_running_waits(self):
        self.assertEqual(radar.ci_state([f2271._check("tests", None, "IN_PROGRESS")]),
                         "pending")
        self.assertEqual(radar.ci_state([]), "pending")

    def test_a_status_error_is_non_conclusive(self):
        self.assertEqual(radar.state_result("ERROR"), radar.CI_INCONCLUSIVE_RESULT)
        self.assertEqual(radar.ci_state([f2271._context("approval", "ERROR")]), "green")
        self.assertEqual(radar.ci_state([f2271._context("approval", "FAILURE")]), "red")

    def test_an_unnamed_negative_check_is_never_dropped(self):
        rows = [{"status": "COMPLETED", "conclusion": "FAILURE"},
                f2271._check("tests")]
        self.assertEqual(radar.ci_result(rows)["state"], "red")

    def test_commit_ci_exposes_negative_acceptable_and_inconclusive(self):
        project = radar.Project("n", "/r", github="o/nexlink", ci_required=True)
        sha = "a" * 40
        out = radar.commit_ci(project, sha, gh=l160g._Gh([
            l160g._run("ci", conclusion="cancelled")]))
        self.assertFalse(out["green"])
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["negative"], [])
        self.assertEqual(out["inconclusive"], ["ci"])
        out = radar.commit_ci(project, sha, gh=l160g._Gh([
            l160g._run("ci", conclusion="failure")]))
        self.assertFalse(out["acceptable"])
        self.assertEqual(out["negative"], ["ci"])
        out = radar.commit_ci(project, sha, gh=l160g._Gh([
            l160g._run("ci", status="in_progress", conclusion=None)]))
        self.assertFalse(out["acceptable"])
        out = radar.commit_ci(project, sha, gh=l160g._Gh([]))
        self.assertFalse(out["acceptable"])


class ReadyPrTest(unittest.TestCase):
    """`fire.ready_pr` : les checks requis, classés ; l'optionnel ne gate pas."""

    def ready(self, rollup, *, required=(("tests", None),), gh=None,
              ci="green"):
        status = f2271._status(rollup, ci=ci)
        report: dict = {}
        ok, why = fire.ready_pr(status, target="main", project=None,
                                frozen=f2271.SHA, required=required,
                                slug="manaty/ameesh", gh=gh, report=report)
        return ok, why, report

    def test_a_skipped_required_check_does_not_block_and_is_consigned(self):
        ok, why, report = self.ready([f2271._check("tests", "SKIPPED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["tests (SKIPPED)"])

    def test_a_cancelled_required_check_does_not_block_and_is_consigned(self):
        ok, why, report = self.ready([f2271._check("tests", "CANCELLED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["tests (CANCELLED)"])

    def test_an_app_scoped_skipped_check_does_not_block(self):
        gh = f2271.FakeGh(check_runs=[f2271._run("tests", conclusion="skipped")])
        ok, why, report = self.ready([f2271._check("tests", "SKIPPED")],
                                     required=(("tests", 15368),), gh=gh)
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["tests (SKIPPED)"])

    def test_a_failed_required_check_still_blocks(self):
        ok, why, report = self.ready([f2271._check("tests", "FAILURE")])
        self.assertFalse(ok)
        self.assertIn("tests (FAILURE)", why)
        self.assertEqual(report["consigned"], [])

    def test_a_pending_required_check_waits(self):
        ok, why, _ = self.ready([f2271._check("tests", None, "IN_PROGRESS")])
        self.assertFalse(ok)
        self.assertIn("PENDING", why)

    def test_a_missing_required_check_still_blocks(self):
        ok, why, _ = self.ready([f2271._check("couverture")])
        self.assertFalse(ok)
        self.assertIn("tests (absent)", why)

    def test_an_optional_negative_check_does_not_gate_a_green_required_one(self):
        # #3066 : le macOS facultatif peut être rouge ; la porte ne lit que
        # les checks requis. `ci` dit rouge, la porte ne s'y fie plus.
        ok, why, _ = self.ready([f2271._check("tests"),
                                 f2271._check("macOS", "FAILURE")], ci="red")
        self.assertTrue(ok, why)

    def test_an_optional_pending_check_does_not_gate_a_green_required_one(self):
        # #3085 (3) : le check facultatif encore en cours (macOS) n'est pas un
        # check requis ; les requis ayant conclu, la porte ne l'attend pas.
        ok, why, _ = self.ready([f2271._check("tests"),
                                 f2271._check("macOS", None, "IN_PROGRESS")])
        self.assertTrue(ok, why)


class PacketTest(banc.Journal):
    """`packet_automerge` : la tête publiée aux requis non concluants fusionne,
    le non concluant est consigné et ouvre un lot de correction."""

    def setUp(self):
        super().setUp()
        banc.RUNS.setdefault("skipped", ("COMPLETED", "SKIPPED"))
        banc.RUNS.setdefault("cancelled", ("COMPLETED", "CANCELLED"))
        self.created: list[dict] = []

        def add(db, **kwargs):
            self.created.append(kwargs)
            return {"id": 9000 + len(self.created)}

        patcher = mock.patch.object(packet_automerge.work, "add", add)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, gh, **kw):
        return packet_automerge.check(None, None, banc.LOT, gh=gh, **kw)

    def forge(self, *, extra, **kwargs):
        gh = banc.FakeGh(**kwargs)
        answer = gh.answer

        def respond(args):
            code, out, err = answer(args)
            if args[:2] == ["pr", "view"] and not code:
                data = json.loads(out)
                data["statusCheckRollup"].extend(extra)
                out = json.dumps(data)
            return code, out, err
        gh.answer = respond
        return gh

    def optional(self, state):
        status, conclusion = banc.RUNS[state]
        return {"__typename": "CheckRun", "name": "macOS", "workflowName": "platforms",
                "status": status, "conclusion": conclusion}

    def test_a_skipped_required_check_merges_and_opens_the_correction(self):
        self.armed()
        out = self.check(banc.FakeGh(tests="skipped"))
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(out["consigned"], ["tests (SKIPPED)"])
        self.assertIn("non concluants", self.notes()[-1])
        self.assertEqual(len(self.created), 1)
        self.assertIn("tests (SKIPPED)", self.created[0]["title"])
        self.assertIn(banc.HEAD, self.created[0]["body"])
        self.assertEqual(self.created[0]["type"], "bug")

    def test_a_cancelled_required_check_merges_and_opens_the_correction(self):
        self.armed()
        out = self.check(banc.FakeGh(tests="cancelled"))
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(out["consigned"], ["tests (CANCELLED)"])
        self.assertEqual(len(self.created), 1)

    def test_a_failed_required_check_never_merges_nor_opens_a_lot(self):
        self.armed()
        gh = banc.FakeGh(tests="failure")
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.merges(), [])
        self.assertEqual(self.created, [])

    def test_a_pending_required_check_waits(self):
        self.armed()
        gh = banc.FakeGh(tests="pending")
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.merges(), [])

    def test_an_optional_red_check_does_not_hold_the_packet(self):
        self.armed()
        gh = self.forge(extra=[self.optional("failure")], tests="success")
        out = self.check(gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", banc.HEAD)])
        self.assertEqual(out["consigned"], [])


class ReleaseRuleTest(unittest.TestCase):
    """La porte de release : un non concluant ne retient pas la tête."""

    def test_a_concluded_reading_without_negative_is_release_green(self):
        self.assertTrue(release_cutoff.reading_is_green({"green": True}))
        self.assertTrue(release_cutoff.reading_is_green(
            {"green": False, "acceptable": True}))
        self.assertFalse(release_cutoff.reading_is_green(
            {"green": False, "acceptable": False}))
        self.assertFalse(release_cutoff.reading_is_green({"green": False}))

    def test_commit_is_green_accepts_a_non_conclusive_head(self):
        self.assertTrue(release_cutoff.commit_is_green(
            "", None, "a" * 40,
            reading={"green": False, "acceptable": True, "negative": [],
                     "inconclusive": ["ci"]}))
        self.assertFalse(release_cutoff.commit_is_green(
            "", None, "a" * 40,
            reading={"green": False, "acceptable": False, "negative": ["ci"],
                     "inconclusive": []}))


class ReleasePlanTest(r2448.ReleaseFixtureMixin, unittest.TestCase):
    """La tête aux seuls checks non concluants est la tête releasée."""

    def test_a_head_with_only_non_conclusive_checks_releases_itself(self):
        reading = {"green": False, "acceptable": True, "negative": [],
                   "inconclusive": ["ci"], "pending": [], "failed": ["ci"],
                   "missing": [], "runs": 1}
        with mock.patch.object(release_cutoff, "ci_reading", return_value=reading), \
                mock.patch.object(release_cutoff, "last_green", return_value=None):
            plan = release_cutoff.build_plan(self.cfg, self.project,
                                             now=r2448._paris(11, 5))
        self.assertTrue(plan.green)
        self.assertEqual(plan.base, self.head)
        self.assertIn("Checks non concluants", plan.notes)
        self.assertEqual(plan.ci["inconclusive"], ["ci"])


class PreflightTest(unittest.TestCase):
    """#3085 (2) : un préflight INCOMPLETE ne vaut pas FAIL (déjà la règle du
    19/10) — verrouillé ici pour qu'une retouche ne la perde pas."""

    def test_an_incomplete_preflight_is_unavailable_not_red(self):
        for cause in ("run-incomplete", "no-vm", "transport-lost", "gate-preempted"):
            with self.subTest(cause=cause):
                self.assertEqual(prevalidation_calls.report_state(
                    {"ok": False, "causes": [cause]}), "unavailable")
        self.assertEqual(prevalidation_calls.report_state(
            {"ok": False, "causes": ["run-red"]}), "red")

