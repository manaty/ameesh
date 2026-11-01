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

    def test_a_non_conclusive_packet_proof_does_not_paint_the_ci_red(self):
        # B2 : SKIPPED/CANCELLED/TIMED_OUT du workflow `paquet` restaient
        # bloquants par exception ; seule la sentinelle et un `FAILURE`
        # exécuté prouvent un défaut.
        for conclusion in ("SKIPPED", "CANCELLED", "TIMED_OUT"):
            with self.subTest(conclusion=conclusion):
                packet = {"__typename": "CheckRun",
                          "name": "suite complète du paquet / tests",
                          "workflowName": "paquet", "status": "COMPLETED",
                          "conclusion": conclusion}
                self.assertEqual(radar.ci_state([packet]), "green")
        sentinel = {"__typename": "CheckRun",
                    "name": "paquet (run proof unavailable)",
                    "status": "COMPLETED", "conclusion": "FAILURE"}
        self.assertEqual(radar.ci_state([sentinel]), "red")

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
              ci="green", project=None):
        status = f2271._status(rollup, ci=ci)
        report: dict = {}
        ok, why = fire.ready_pr(status, target="main", project=project,
                                frozen=f2271.SHA, required=required,
                                slug="manaty/ameesh", gh=gh, report=report)
        return ok, why, report

    def test_a_skipped_required_check_does_not_block_and_is_consigned(self):
        ok, why, report = self.ready([f2271._check("tests", "SKIPPED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["tests (SKIPPED)"])

    def test_a_cancelled_required_check_blocks_as_absent_proof(self):
        # #3186 (décision owner #128512) : l'agrégat requis annulé n'a jamais
        # conclu — preuve absente, bloquant ; il n'est pas consigné.
        ok, why, report = self.ready([f2271._check("tests", "CANCELLED")])
        self.assertFalse(ok)
        self.assertIn("never concluded", why)
        self.assertEqual(report["consigned"], [])

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

    def test_the_global_ci_required_read_does_not_reintroduce_optional_checks(self):
        # B1 de la relecture de 966bf4d : avec `ci_required`, la seconde
        # lecture globale (`commit_ci`) remettait le macOS facultatif
        # `pending`/`FAILURE` dans les bloquants, malgré `tests` vert.
        project = radar.Project("ameesh", "/r", github="manaty/ameesh",
                                ci_required=True)
        for optional in (f2271._check("macOS", None, "IN_PROGRESS"),
                         f2271._check("macOS", "FAILURE")):
            with self.subTest(optional=optional):
                with mock.patch.object(
                        fire.merge_queue, "_required_ci",
                        side_effect=fire.merge_queue.QueueError("global read")) as read:
                    ok, why, _ = self.ready([f2271._check("tests"), optional],
                                            project=project)
                self.assertTrue(ok, why)
                read.assert_not_called()

    def test_ci_required_still_reads_the_commit_without_a_required_check(self):
        # La lecture globale reste la seule preuve d'un projet `ci_required`
        # dont la branche n'expose aucun check requis.
        project = radar.Project("ameesh", "/r", github="manaty/ameesh",
                                ci_required=True)
        with mock.patch.object(
                fire.merge_queue, "_required_ci",
                side_effect=fire.merge_queue.QueueError("no run on the freeze")) as read:
            ok, why, _ = self.ready([f2271._check("tests")], required=(),
                                    project=project)
        self.assertFalse(ok)
        self.assertIn("no run", why)
        read.assert_called_once()

    def test_a_skipped_packet_proof_is_consigned_not_blocking(self):
        # B2 : un check du portier `paquet` sauté/annulé n'est pas un défaut
        # prouvé ; il se consigne au lieu de retenir la porte.
        packet = {"__typename": "CheckRun", "name": "suite complète du paquet / tests",
                  "workflowName": "paquet", "status": "COMPLETED",
                  "conclusion": "SKIPPED"}
        ok, why, report = self.ready([f2271._check("tests"), packet])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"],
                         ["suite complète du paquet / tests (SKIPPED)"])

    def test_a_failed_or_pending_packet_proof_still_blocks(self):
        for status, conclusion in (("COMPLETED", "FAILURE"),
                                   ("IN_PROGRESS", None)):
            with self.subTest(status=status, conclusion=conclusion):
                packet = {"__typename": "CheckRun",
                          "name": "suite complète du paquet / tests",
                          "workflowName": "paquet", "status": status,
                          "conclusion": conclusion}
                ok, why, _ = self.ready([f2271._check("tests"), packet])
                self.assertFalse(ok)
                self.assertIn("paquet", why)

    def test_a_required_status_in_error_is_consigned_not_blocking(self):
        # B3 : `ci/tests=ERROR` était consigné par `_required_gate` puis
        # rejeté par `ci_green(required_statuses)`.
        project = radar.Project("nexlink", "/r", github="o/nexlink",
                                required_statuses=(("integration/**", "ci/"),))
        ok, why, report = self.ready(
            [f2271._check("tests"), f2271._context("ci/tests", "ERROR")],
            project=project)
        self.assertTrue(ok, why)
        self.assertIn("ci/* (ERROR)", report["consigned"])

    def test_a_required_status_failure_or_pending_still_blocks(self):
        project = radar.Project("nexlink", "/r", github="o/nexlink",
                                required_statuses=(("integration/**", "ci/"),))
        for state in ("FAILURE", "PENDING"):
            with self.subTest(state=state):
                ok, why, _ = self.ready(
                    [f2271._check("tests"), f2271._context("ci/tests", state)],
                    project=project)
                self.assertFalse(ok)
                self.assertIn("ci/*", why)

    def test_an_optional_commit_status_does_not_gate(self):
        # Un statut hors préfixe requis n'est pas un check requis ; l'ancien
        # `ci_green` refusait tout statut non `SUCCESS`, même facultatif.
        project = radar.Project("nexlink", "/r", github="o/nexlink",
                                required_statuses=(("integration/**", "ci/"),))
        ok, why, _ = self.ready(
            [f2271._check("tests"), f2271._context("ci/tests"),
             f2271._context("macos/optional", "FAILURE")],
            project=project)
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

    def test_a_cancelled_required_check_never_merges(self):
        # #3186 (décision owner #128512) : preuve absente, la porte attend ;
        # la relance du run est témoignée dans test_3186.
        self.armed()
        gh = banc.FakeGh(tests="cancelled")
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.merges(), [])
        self.assertEqual(self.created, [])

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

    def portier(self, gh):
        """#5219 : la 1re lecture de la PR montre `tests` en cours, le portier
        annulé et sauté ; la provenance et la relecture d'avant le geste
        montrent `tests` vert."""
        answer, seen = gh.answer, []

        def respond(args):
            code, out, err = answer(args)
            if args[:2] == ["pr", "view"] and not code:
                seen.append(args)
                data = json.loads(out)
                rollup = data["statusCheckRollup"]
                for entry in rollup:
                    entry.setdefault("workflowName", "tests")
                rollup.extend(
                    {"__typename": "CheckRun", "name": name, "workflowName": "paquet",
                     "status": "COMPLETED", "conclusion": conclusion}
                    for name, conclusion in ((radar.PACKET_COVERAGE, "CANCELLED"),
                                             (radar.PACKET_SUITE, "SKIPPED")))
                if len(seen) == 1:
                    for entry in rollup:
                        if entry.get("name") == "tests":
                            entry.update(status="IN_PROGRESS", conclusion="")
                out = json.dumps(data)
            return code, out, err
        gh.answer = respond
        return gh

    def test_a_portier_replaced_while_the_gate_read_is_not_consigned(self):
        self.armed()
        gh = self.portier(banc.FakeGh(tests="success"))
        out = self.check(gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(out["consigned"], [])
        self.assertEqual(self.created, [])

    def test_another_nonconclusive_stays_consigned_after_the_fresh_read(self):
        view = {"rollup": [
            {"__typename": "CheckRun", "name": radar.PACKET_SUITE, "workflowName": "paquet",
             "status": "COMPLETED", "conclusion": "SKIPPED"}], "branch": "x"}
        self.assertEqual(
            packet_automerge._still_consigned(
                ["tests (SKIPPED)", "%s (SKIPPED)" % radar.PACKET_SUITE],
                view, view, (("tests", None),), None),
            ["tests (SKIPPED)", "%s (SKIPPED)" % radar.PACKET_SUITE])


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

