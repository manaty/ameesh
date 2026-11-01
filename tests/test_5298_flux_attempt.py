# SPDX-License-Identifier: AGPL-3.0-only
"""#5298 — le flux d'un attempt antérieur ne bloque pas une tête rejouée verte.

Constat (paquet h-20261101-10, #5273, PR #3986, tête 99efb551, run
41290078741) : l'attempt 1 laisse `tests (flux) psycopg 3/8` en `failure` ;
`gh run rerun --failed` conclut l'attempt 2 vert, mais `statusCheckRollup`
(lecture `filter=latest`) ne rend qu'un check par nom et garde l'ancien rouge
(son `startedAt` est plus grand, horloge du runner).

La porte ne remplace **que sur preuve identifiée** (relecture cx1) : même
exécution, même part, tentative plus récente verte — le check de flux
homonyme (`external_id` `ameesh-ci-flow/<exécution>/<tentative>`) ou, à
défaut, le job de même part de la dernière tentative. Un agrégat `tests` vert
d'une autre exécution ou d'un autre plan ne prouve rien, et un nom remplacé
n'emporte pas un homonyme courant. Sans preuve, le rouge garde la porte
fermée.
"""
from __future__ import annotations

import json
import re
import subprocess
import unittest

from ameesh import fire, merge_pr, packet_automerge, radar
from . import test_2813_packet_auto_merge as banc

SLUG = "manaty/ameesh"
SHA = "a" * 40
RUN = 41290078741
OTHER_RUN = 41290000000
FLUX = "tests (flux) psycopg 3/8"
PART = "psycopg 3/8"


def rest(ident, *, name="tests", conclusion="success", status="completed",
         run=None, attempt=None, app=banc.APP, head=SHA, suite=1,
         started="2026-11-01T09:00:00Z", completed="2026-11-01T09:10:00Z"):
    """Un check run de la tête, dans la forme REST lue par `fire`."""
    check = {"id": ident, "name": name, "head_sha": head, "status": status,
             "conclusion": conclusion, "app": {"id": app},
             "check_suite": {"id": suite}, "started_at": started,
             "completed_at": completed}
    if run is None:
        check["details_url"] = ("https://github.com/%s/actions/runs/200/job/%d"
                                % (SLUG, ident))
    else:
        check["external_id"] = "ameesh-ci-flow/%d/%d" % (run, attempt or 1)
        check["details_url"] = "https://github.com/%s/runs/%d" % (SLUG, ident)
    return check


def entry(ident, *, name=FLUX, conclusion="FAILURE", workflow="paquet",
          suite=1, started="2026-11-01T09:00:00Z",
          completed="2026-11-01T09:18:14Z"):
    """Une entrée de `statusCheckRollup` (celle que lit la porte)."""
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow,
            "status": "COMPLETED", "conclusion": conclusion,
            "startedAt": started, "completedAt": completed,
            "detailsUrl": "https://github.com/%s/runs/%d" % (SLUG, ident),
            "checkSuiteId": suite}


def tests_entry(conclusion="SUCCESS", *, suite=9):
    return entry(90, name="tests", conclusion=conclusion, workflow="tests",
                 suite=suite)


def status(rollup):
    return {"number": 1, "state": "OPEN", "head": SHA, "base": "main",
            "branch": "integration/paquet-h-20261101-10", "draft": False,
            "rollup": rollup, "ci": "green", "ci_rule": "green"}


class FluxGh:
    """Le `gh` de la preuve : checks de la tête, exécution, jobs d'attempt."""

    def __init__(self, checks, *, runs=None, jobs=None, checks_error=False):
        self.checks, self.runs, self.jobs = checks, runs or {}, jobs or {}
        self.checks_error, self.calls = checks_error, []

    def __call__(self, argv, **kwargs):
        path = argv[-1] if argv else ""
        self.calls.append(list(argv))
        if "/check-runs" in path:
            if self.checks_error:
                return subprocess.CompletedProcess(argv, 1, "", "unavailable")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"total_count": len(self.checks),
                                      "check_runs": self.checks}]), "")
        match = re.search(r"/actions/runs/([0-9]+)$", path)
        if match:
            data = self.runs.get(int(match.group(1)))
            if data is None:
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        match = re.search(r"/actions/runs/([0-9]+)/attempts/([0-9]+)/jobs", path)
        if match:
            rows = self.jobs.get((int(match.group(1)), int(match.group(2))))
            if rows is None:
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"jobs": rows}]), "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected %s" % argv)


class IdentifiedProofTest(unittest.TestCase):
    def proof(self, rollup, gh):
        return fire.replaced_flux_checks(rollup, slug=SLUG, head=SHA, gh=gh)

    def test_a_later_green_flux_of_the_same_run_replaces_its_entry(self):
        old = entry(11)
        rollup = [tests_entry(), old]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1,
                               completed="2026-11-01T09:18:14Z"),
                  rest(12, name=FLUX, conclusion="success", run=RUN, attempt=2,
                       started="2026-11-01T09:27:11Z",
                       completed="2026-11-01T09:32:23Z")]
        proof = self.proof(rollup, FluxGh(checks))
        self.assertEqual(proof, {radar.flux_entry_key(old)})
        self.assertEqual(fire._packet_proof_gate(rollup, proof), ([], []))

    def test_another_run_or_aggregate_alone_proves_nothing(self):
        # Le rouge n'a pas été rejoué : un agrégat `tests` vert d'une autre
        # exécution (ou un autre plan) ne le remplace pas.
        old = entry(11)
        rollup = [tests_entry("SUCCESS"), old]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1)]
        proof = self.proof(rollup, FluxGh(checks, runs={
            RUN: {"run_attempt": 1, "head_sha": SHA}}))
        self.assertEqual(proof, set())
        self.assertEqual(fire._packet_proof_gate(rollup, proof),
                         (["%s (FAILURE)" % FLUX], []))

    def test_a_later_green_flux_of_another_run_proves_nothing(self):
        old = entry(11)
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(21, name=FLUX, conclusion="success", run=OTHER_RUN,
                       attempt=2)]
        self.assertEqual(self.proof([tests_entry(), old], FluxGh(checks)), set())

    def test_a_later_green_job_of_the_same_part_replaces_the_entry(self):
        # Le publisher peut manquer sa ligne `fin` : le job de la part de la
        # dernière tentative de la même exécution reste la preuve.
        old = entry(11)
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1)]
        gh = FluxGh(checks, runs={RUN: {"run_attempt": 2, "head_sha": SHA}},
                    jobs={(RUN, 2): [{"name": PART, "conclusion": "success"}]})
        proof = self.proof([tests_entry(), old], gh)
        self.assertEqual(proof, {radar.flux_entry_key(old)})

    def test_a_later_failed_job_does_not_replace_the_entry(self):
        old = entry(11)
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1)]
        gh = FluxGh(checks, runs={RUN: {"run_attempt": 2, "head_sha": SHA}},
                    jobs={(RUN, 2): [{"name": PART, "conclusion": "failure"}]})
        self.assertEqual(self.proof([tests_entry(), old], gh), set())

    def test_a_current_homonym_is_not_erased_by_a_replaced_one(self):
        # Deux entrées homonymes : l'ancienne (attempt 1, remplacée par le
        # vert de l'attempt 2) et la courante (autre exécution, rouge après
        # l'agrégat). Seule la première est écartée.
        old = entry(11, suite=1, started="2026-11-01T09:00:00Z")
        current = entry(13, suite=2, started="2026-11-01T11:00:00Z",
                        completed="2026-11-01T11:30:00Z")
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="success", run=RUN, attempt=2),
                  rest(13, name=FLUX, conclusion="failure", run=OTHER_RUN,
                       attempt=1)]
        rollup = [tests_entry(), old, current]
        proof = self.proof(rollup, FluxGh(checks))
        self.assertEqual(proof, {radar.flux_entry_key(old)})
        kept = radar.packet_proof_entries(rollup, proof)
        self.assertEqual([row["detailsUrl"].rsplit("/", 1)[1] for row in kept],
                         ["13"])
        self.assertEqual(fire._packet_proof_gate(rollup, proof),
                         (["%s (FAILURE)" % FLUX], []))

    def test_a_current_attempt_red_is_never_replaced(self):
        current = entry(12)
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="failure", run=RUN, attempt=2)]
        proof = self.proof([tests_entry(), current], FluxGh(checks, runs={
            RUN: {"run_attempt": 2, "head_sha": SHA}}))
        self.assertEqual(proof, set())
        self.assertEqual(fire._packet_proof_gate([tests_entry(), current], proof),
                         (["%s (FAILURE)" % FLUX], []))

    def test_an_absent_external_id_or_unreadable_checks_prove_nothing(self):
        old = entry(11)
        missing = rest(11, name=FLUX, conclusion="failure", run=RUN, attempt=1)
        missing["external_id"] = ""
        self.assertEqual(self.proof([tests_entry(), old],
                                    FluxGh([rest(90), missing])), set())
        self.assertEqual(self.proof([tests_entry(), old],
                                    FluxGh([rest(90)], checks_error=True)), set())

    def test_a_different_head_is_not_a_proof(self):
        old = entry(11)
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="success", run=RUN, attempt=2,
                       head="b" * 40)]
        gh = FluxGh(checks)          # la lecture lève (tête différente)
        self.assertEqual(self.proof([tests_entry(), old], gh), set())


class FireGateTest(unittest.TestCase):
    """La porte du feu/auto-merge sur le rollup du paquet h-10."""

    def ready(self, rollup, checks):
        return fire.ready_pr(status(rollup), target="main", project=None,
                             frozen=SHA, required=(("tests", banc.APP),),
                             slug=SLUG, gh=FluxGh(checks))

    def stale(self):
        rollup = [tests_entry(), entry(11)]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="success", run=RUN, attempt=2)]
        return rollup, checks

    def test_ready_pr_passes_with_the_identified_proof(self):
        rollup, checks = self.stale()
        ok, why = self.ready(rollup, checks)
        self.assertTrue(ok, why)

    def test_ready_pr_still_refuses_a_current_flux(self):
        rollup = [tests_entry(), entry(12)]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="failure", run=RUN, attempt=2)]
        ok, why = self.ready(rollup, checks)
        self.assertFalse(ok, why)
        self.assertIn(FLUX, why)

    def test_ready_pr_still_refuses_without_the_later_proof(self):
        rollup = [tests_entry(), entry(11)]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1)]
        ok, why = self.ready(rollup, checks)
        self.assertFalse(ok, why)
        self.assertIn(FLUX, why)


class MergePrGateTest(unittest.TestCase):
    """`merge-pr` sous `UNSTABLE` : même preuve identifiée, même règle."""

    def view(self, rollup):
        return {"number": 2194, "state": "OPEN", "headRefOid": SHA,
                "headRefName": "integration/paquet-h-20261101-10",
                "baseRefName": "main", "mergeStateStatus": "UNSTABLE",
                "statusCheckRollup": rollup}

    def test_unstable_tolerates_the_identified_replacement(self):
        rollup = [tests_entry(), entry(11)]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="success", run=RUN, attempt=2)]
        gh = FluxGh(checks)
        self.assertTrue(merge_pr._unstable_ci_ok(
            rollup, slug=SLUG, head=SHA, gh=gh))
        merge_pr._pr_guards(self.view(rollup), 2194, SHA, "main",
                            slug=SLUG, gh=FluxGh(checks))

    def test_unstable_still_refuses_a_current_flux(self):
        rollup = [tests_entry(), entry(12)]
        checks = [rest(90), rest(11, name=FLUX, conclusion="failure",
                               run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="failure", run=RUN, attempt=2)]
        self.assertFalse(merge_pr._unstable_ci_ok(
            rollup, slug=SLUG, head=SHA, gh=FluxGh(checks)))
        with self.assertRaises(merge_pr.MergePrError):
            merge_pr._pr_guards(self.view(rollup), 2194, SHA, "main",
                                slug=SLUG, gh=FluxGh(checks))


class PacketAutoMergeTest(banc.Journal):
    """Le passage d'auto-merge du porteur, avec les checks de la PR #3986."""

    def forge(self, extra, checks):
        gh = banc.FakeGh(tests="success")
        answer = gh.answer

        def respond(args):
            path = args[-1] if args else ""
            if args[:1] == ["api"] and "/check-runs" in path:
                return 0, json.dumps([{"total_count": len(checks),
                                       "check_runs": checks}]), ""
            code, out, err = answer(args)
            if args[:2] == ["pr", "view"] and not code:
                data = json.loads(out)
                data["statusCheckRollup"].extend(extra)
                out = json.dumps(data)
            return code, out, err

        gh.answer = respond
        return gh

    def test_a_green_packet_with_an_identified_replacement_merges(self):
        self.armed()
        checks = [rest(90, head=banc.HEAD),
                  rest(11, name=FLUX, conclusion="failure", head=banc.HEAD,
                       run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="success", head=banc.HEAD,
                       run=RUN, attempt=2)]
        gh = self.forge([entry(11)], checks)
        out = packet_automerge.check(None, None, banc.LOT, gh=gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", banc.HEAD)])

    def test_a_current_flux_still_holds_the_packet(self):
        self.armed()
        checks = [rest(90, head=banc.HEAD),
                  rest(11, name=FLUX, conclusion="failure", head=banc.HEAD,
                       run=RUN, attempt=1),
                  rest(12, name=FLUX, conclusion="failure", head=banc.HEAD,
                       run=RUN, attempt=2)]
        gh = self.forge([entry(12)], checks)
        out = packet_automerge.check(None, None, banc.LOT, gh=gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertTrue(any(FLUX in note for note in self.notes()), self.notes())


if __name__ == "__main__":
    unittest.main()
