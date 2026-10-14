# SPDX-License-Identifier: AGPL-3.0-only
"""#1316 — un dispatch ne remplace plus la carte de main en attente.

Constat N1 (bureau Claude, 14/10 17:02, PR #1004 @6be4710d6, carte.yml:43) :
le dispatch hors main partageait le groupe `carte-main`, or GitHub annule le
run en attente d'un groupe quand un nouveau run y entre : un dispatch pouvait
donc remplacer la carte de main en attente et la perdre. #1314 a rendu la
carte de main non annulable par la poussée suivante ; #1316 lui retire aussi
cette éviction par dispatch, en donnant au dispatch une identité par run
(même règle que tests.yml, #824, décision owner #17605).

Sans base : lecture du workflow, puis évaluation de l'expression de groupe
pour les événements réels (poussée main, dispatch, PR).
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")
GROUP = re.compile(r"^\s*group:\s*carte-\$\{\{\s*(.*?)\s*\}\}\s*$", re.M)


def expression() -> str:
    m = GROUP.search(WORKFLOW)
    if m is None:
        raise AssertionError("groupe de concurrence introuvable")
    return m.group(1)


def group_of(event_name, *, ref="refs/heads/main", pr_number=None, run_id=1) -> str:
    """Le groupe évalué comme GitHub : `&&`/`||` et `format`, sans base.

    L'expression vient du workflow du dépôt ; elle est évaluée avec des
    variables locales et sans `__builtins__`, il n'y a pas de code tiers.
    """
    code = expression()
    code = code.replace("github.event_name", "event_name")
    code = code.replace("github.event.pull_request.number", "pr_number")
    code = code.replace("github.ref", "ref")
    code = code.replace("github.run_id", "run_id")
    code = code.replace("&&", " and ").replace("||", " or ")
    context = {"event_name": event_name, "ref": ref, "pr_number": pr_number, "run_id": run_id}
    return "carte-" + eval(
        code, {"format": lambda template, *values: template.format(*values),
               "__builtins__": {}}, context)


class CarteDispatchTest(unittest.TestCase):

    def test_une_poussee_main_garde_le_groupe_stable(self):
        # le groupe de main ne dépend pas du run : un seul en attente, remplacé
        # par la poussée suivante (règle #1314).
        self.assertEqual(group_of("push", run_id=1), "carte-main")
        self.assertEqual(group_of("push", run_id=2), "carte-main")

    def test_un_dispatch_ne_partage_pas_le_groupe_de_main(self):
        # avant #1316 : dispatch -> carte-main, il remplaçait la carte de main
        # en attente.
        self.assertNotEqual(group_of("workflow_dispatch", run_id=7), "carte-main")
        self.assertEqual(group_of("workflow_dispatch", run_id=7), "carte-dispatch-7")
        self.assertNotEqual(
            group_of("workflow_dispatch", ref="refs/heads/lot/x", run_id=8),
            group_of("workflow_dispatch", ref="refs/heads/lot/x", run_id=9))

    def test_une_pr_garde_son_groupe(self):
        self.assertEqual(group_of("pull_request", pr_number=1004), "carte-pr-1004")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
