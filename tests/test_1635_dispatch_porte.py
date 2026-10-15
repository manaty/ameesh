# SPDX-License-Identifier: AGPL-3.0-only
"""#1635 : un `workflow_dispatch` de tests.yml n'est admis que sur main,
release/** et integration/**.

Le run lancé à la main publie le check requis `tests` sur la tête de la branche
lancée : sur une branche de lot, il satisferait le check de sa PR sans merge-ref
ni passage par la PR. L'étape de garde du job `plan` refuse alors, `plan`
échoue et le job `tests` rougit (`test needs.plan.result = success`).

Ce contrat rejoue le script exact de l'étape sous `bash -e`, la référence
passée par l'environnement comme dans le workflow : aucun accès base, aucune
suite.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
_STEP = "      - name: lancement à la main (main, release/**, integration/** — #1635)\n"


def step() -> str:
    """Le texte de l'étape de garde, jusqu'à l'étape suivante."""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(_STEP)
    end = text.index("\n      - ", start + len(_STEP))
    return text[start:end + 1]


def guard_script() -> str:
    """Le `run` de l'étape, dé-indenté."""
    body = step().split("        run: |\n", 1)[1]
    return "".join(line[10:] if line.startswith("          ") else "\n"
                   for line in body.splitlines(keepends=True))


def run_guard(ref: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DISPATCH_REF=ref)
    return subprocess.run(["bash", "-e", "-c", guard_script()], cwd=REPO, env=env,
                          capture_output=True, text=True)


class DispatchGuardTest(unittest.TestCase):

    def test_step_is_first_in_plan_and_only_on_dispatch(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        plan = text.index("\n  plan:\n")
        steps = text.index("    steps:\n", plan)
        self.assertEqual(text.index("      - ", steps), text.index(_STEP))
        self.assertIn("        if: github.event_name == 'workflow_dispatch'\n", step())

    def test_ref_travels_through_the_environment(self):
        self.assertIn("DISPATCH_REF: ${{ github.ref }}", step())
        self.assertNotIn("${{", guard_script())

    def test_main_release_and_integration_are_admitted(self):
        for ref in ("refs/heads/main", "refs/heads/release/1.11.0",
                    "refs/heads/integration/paquet-1-20261014"):
            with self.subTest(ref=ref):
                out = run_guard(ref)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn("admitted", out.stdout)

    def test_lot_branches_and_lookalikes_are_refused(self):
        for ref in ("refs/heads/lot/1635-dispatch-porte", "refs/heads/main2",
                    "refs/heads/mainx/release/1", "refs/heads/feature/release/1",
                    "refs/tags/v1.11.0", "refs/heads/release", "",
                    "refs/heads/$(touch pwned)"):
            with self.subTest(ref=ref):
                out = run_guard(ref)
                self.assertNotEqual(out.returncode, 0, out.stdout)
                self.assertIn("::error::", out.stdout)
        self.assertFalse((REPO / "pwned").exists())

    def test_failed_plan_reddens_the_required_check(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        agg = text[text.index("\n  tests:\n"):]
        self.assertIn('test "${{ needs.plan.result }}" = success', agg)


if __name__ == "__main__":
    unittest.main()
