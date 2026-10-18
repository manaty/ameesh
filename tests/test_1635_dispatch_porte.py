# SPDX-License-Identifier: AGPL-3.0-only
"""#1635 : un `workflow_dispatch` de tests.yml ne publie pas de check `tests`
vert qui tienne lieu de celui d'une PR.

Le check requis de main est `tests` émis par GitHub Actions (app 15368, non
strict) : il se lie au commit, pas à l'évènement. Constaté sans run de
démonstration : le run lancé à la main 38556384176 sur lot/1211 a laissé le
seul check `tests` (success) du commit 859e0658, un commit de la PR #923.

Le job `plan` refuse donc un lancement à la main hors main, release/**,
integration/** et validation/** (étape avant checkout), puis sur un commit qui
est la tête d'une PR ouverte (scripts/dispatch-guard.py) ; le refus fait
échouer `plan`, et l'agrégat rougit. Un dispatch publie en outre un contexte
distinct, même si une PR est ouverte sur ce SHA après la garde ou le run.
Ces témoins rejouent le script
exact de l'étape sous `bash -e`, le script de garde avec une API simulée, et
l'agrégat de #940 : aucun accès réseau, aucune base, aucune suite.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import unittest

from tests.test_940_agregat_bash import run_script, scenario

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
_REF_STEP = ("      - name: lancement à la main (main, release/**, integration/**, "
             "validation/** — #1635)\n")
_PR_STEP = "      - name: lancement à la main sur la tête d'une PR ouverte (#1635)\n"
SHA = "859e06583104dca278d44624b1b95c1a1dc0235e"
OTHER = "3668d4b6b75f833d15418d45157096204d71e4f0"

_spec = importlib.util.spec_from_file_location("dispatch_guard",
                                               REPO / "scripts" / "dispatch-guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ADMITTED = ("refs/heads/main", "refs/heads/release/1.11.0",
            "refs/heads/integration/paquet-11h-20261015",
            "refs/heads/validation/bureau-deploiement-12408e2b")
REFUSED = ("refs/heads/lot/1635-dispatch-porte", "refs/heads/main2",
           "refs/heads/mainx/release/1", "refs/heads/feature/release/1",
           "refs/heads/release/", "refs/heads/integration", "refs/tags/v1.11.0",
           "refs/pull/1203/merge", "", "refs/heads/$(touch pwned)")


def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def aggregate_name(event: str) -> str:
    name = workflow().split("\n  tests:\n", 1)[1].split("    name: ", 1)[1].splitlines()[0]
    if not name.startswith("${{"):
        return name
    expression = name[3:-2].strip().replace("github.event_name", "event_name")
    expression = expression.replace("&&", "and").replace("||", "or")
    return eval(expression, {"__builtins__": {}}, {"event_name": event})


def step(name: str) -> str:
    text = workflow()
    start = text.index(name)
    end = text.index("\n      - ", start + len(name))
    return text[start:end + 1]


def ref_script() -> str:
    body = step(_REF_STEP).split("        run: |\n", 1)[1]
    return "".join(line[10:] if line.startswith("          ") else "\n"
                   for line in body.splitlines(keepends=True))


def run_ref_step(ref: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DISPATCH_REF=ref)
    return subprocess.run(["bash", "-e", "-c", ref_script()], cwd=REPO, env=env,
                          capture_output=True, text=True)


def api(*pages):
    """Une API simulée : chaque appel rend la page suivante."""
    calls = []

    def fetch(url, token):
        calls.append(url)
        return pages[len(calls) - 1]
    fetch.calls = calls
    return fetch


def prs(*heads, start=1):
    return [{"number": start + i, "head": {"sha": h}} for i, h in enumerate(heads)]


class WorkflowStepsTest(unittest.TestCase):

    def plan_steps(self) -> str:
        text = workflow()
        plan = text.index("\n  plan:\n")
        return text[plan:text.index("\n  psycopg:\n")]

    def test_ref_step_runs_first_and_pr_step_before_the_plan(self):
        plan = self.plan_steps()
        steps = plan.index("    steps:\n")
        self.assertEqual(plan.index("      - ", steps), plan.index(_REF_STEP))
        self.assertLess(plan.index(_REF_STEP), plan.index("actions/checkout@v4"))
        self.assertLess(plan.index("actions/setup-python@v5"), plan.index(_PR_STEP))
        self.assertLess(plan.index(_PR_STEP), plan.index("      - id: plan\n"))
        for name in (_REF_STEP, _PR_STEP):
            self.assertIn("        if: github.event_name == 'workflow_dispatch'\n", step(name))
        self.assertIn("run: python3 scripts/dispatch-guard.py", step(_PR_STEP))
        self.assertIn("GH_TOKEN: ${{ github.token }}", step(_PR_STEP))
        self.assertIn("      pull-requests: read\n", plan)

    def test_ref_travels_through_the_environment(self):
        self.assertIn("DISPATCH_REF: ${{ github.ref }}", step(_REF_STEP))
        self.assertNotIn("${{", ref_script())

    def test_the_two_filters_agree(self):
        for ref in ADMITTED + REFUSED:
            with self.subTest(ref=ref):
                self.assertEqual(run_ref_step(ref).returncode == 0, guard.ref_admitted(ref))
        self.assertFalse((REPO / "pwned").exists())

    def test_a_failed_plan_reddens_the_aggregate(self):
        self.assertIn('test "${{ needs.plan.result }}" = success',
                      workflow()[workflow().index("\n  tests:\n"):])


class GuardTest(unittest.TestCase):

    def test_admitted_refs_off_any_open_head(self):
        for ref in ADMITTED:
            with self.subTest(ref=ref):
                ok, why = guard.check(ref, SHA, "manaty/ameesh", "t", api(prs(OTHER)))
                self.assertTrue(ok, why)

    def test_lot_branches_and_lookalikes_are_refused_before_the_api(self):
        for ref in REFUSED:
            with self.subTest(ref=ref):
                fetch = api()
                ok, why = guard.check(ref, SHA, "manaty/ameesh", "t", fetch)
                self.assertFalse(ok)
                self.assertIn("#1635", why)
                self.assertEqual(fetch.calls, [])

    def test_admitted_branch_on_an_open_pr_head_is_refused(self):
        # validation/** posée sur la tête d'un lot : la branche ne suffit pas.
        ok, why = guard.check(ADMITTED[3], SHA, "manaty/ameesh", "t",
                              api(prs(*([OTHER] * 100)), prs(SHA, start=923)))
        self.assertFalse(ok)
        self.assertIn("#923", why)

    def test_every_page_is_read(self):
        fetch = api(prs(*([OTHER] * 100)), prs(OTHER))
        self.assertTrue(guard.check(ADMITTED[0], SHA, "manaty/ameesh", "t", fetch)[0])
        self.assertEqual(len(fetch.calls), 2)
        self.assertIn("state=open", fetch.calls[0])
        self.assertTrue(fetch.calls[1].endswith("&page=2"))

    def test_doubt_refuses(self):
        def boom(url, token):
            raise OSError("network down")
        cases = {
            "api error": dict(fetch=boom),
            "unreadable row": dict(fetch=api([{"number": 1}])),
            "null head": dict(fetch=api([{"number": 1, "head": {"sha": None}}])),
            "invalid number": dict(fetch=api([{"number": True, "head": {"sha": SHA}}])),
            "not a list": dict(fetch=lambda u, t: (_ for _ in ()).throw(
                guard.GuardError("unexpected answer"))),
            "no token": dict(fetch=api([]), token=""),
            "no sha": dict(fetch=api([]), sha=""),
            "short sha": dict(fetch=api([]), sha=SHA[:12]),
        }
        for label, case in cases.items():
            with self.subTest(label):
                ok, why = guard.check(ADMITTED[0], case.get("sha", SHA), "manaty/ameesh",
                                      case.get("token", "t"), case["fetch"])
                self.assertFalse(ok)
                self.assertIn("nothing run", why)

    def test_too_many_pages_refuses(self):
        full = prs(*([OTHER] * 100))
        ok, why = guard.check(ADMITTED[0], SHA, "manaty/ameesh", "t",
                              api(*([full] * guard.MAX_PAGES)))
        self.assertFalse(ok)


class RequiredCheckTest(unittest.TestCase):
    """Le témoin discriminant : un lancement à la main sur une branche de lot
    (ou sur la tête d'une PR) rend le check `tests` rouge ; sans la garde, le
    même run (plan réussi, parts vertes) le rendait vert."""

    def aggregate(self, plan_result: str) -> int:
        return run_script(scenario(**{"needs.plan.result": plan_result,
                                      "github.event_name": "workflow_dispatch",
                                      "needs.version.result": "skipped"})).returncode

    def test_without_guard_the_dispatch_was_green(self):
        self.assertEqual(self.aggregate("success"), 0)

    def test_dispatch_before_a_pr_opens_cannot_publish_its_required_context(self):
        # La PR peut être ouverte après la garde (ou après la fin du run).
        ok, why = guard.check(ADMITTED[3], SHA, "manaty/ameesh", "t", api([]))
        self.assertTrue(ok, why)
        self.assertEqual(self.aggregate("success"), 0)
        self.assertNotEqual(aggregate_name("workflow_dispatch"), "tests")

    def test_pr_and_push_keep_the_required_context(self):
        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                self.assertEqual(aggregate_name(event), "tests")

    def test_lot_branch_dispatch_is_red(self):
        ref_ok = run_ref_step("refs/heads/lot/1211-ci-impactes-contrat").returncode == 0
        self.assertNotEqual(self.aggregate("success" if ref_ok else "failure"), 0)

    def test_dispatch_on_an_open_pr_head_is_red(self):
        ok, _ = guard.check(ADMITTED[1], SHA, "manaty/ameesh", "t", api(prs(SHA)))
        self.assertNotEqual(self.aggregate("success" if ok else "failure"), 0)

    def test_legitimate_package_dispatch_stays_green(self):
        ref = ADMITTED[2]
        ok = run_ref_step(ref).returncode == 0 and guard.check(
            ref, SHA, "manaty/ameesh", "t", api(prs(OTHER)))[0]
        self.assertEqual(self.aggregate("success" if ok else "failure"), 0)


if __name__ == "__main__":
    unittest.main()
