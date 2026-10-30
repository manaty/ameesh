# SPDX-License-Identifier: AGPL-3.0-only
"""L99 T2b (#4366) — les constats des balayages et de la porte de paquet
livrent par `work.deliver` (connecteur `git-merge`, preuve `{seen, sha, pr}`).

`plan_git`, `plan_github`, `packet_close`, `merge_pr` et `fire` n'appellent
plus `work.close_merged` : le témoin refuse tout nouvel appel direct hors du
chemin de `deliver` (le connecteur `git-merge` et `work.record_merge`).
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from ameesh import work

from .support import PgTestCase

SHA = "0123456789abcdef0123456789abcdef01234567"
SRC = pathlib.Path(work.__file__).resolve().parent

#: (fichier relatif à src/ameesh, fonction englobante) qui peuvent appeler
#: `close_merged` : le connecteur `git-merge`, `record_merge` (sa preuve
#: déclarée) et `close_merged` lui-même vers le stockage
ALLOWED = {("modules/dev/__init__.py", "deliver_git_merge"),
           ("work.py", "record_merge"), ("work.py", "close_merged")}


def _close_merged_callers():
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("storage/"):
            continue        # la couche de stockage, appelée par work.close_merged
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        stack = []

        def visit(node):
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if named:
                stack.append(node.name)
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else "")
                if name == "close_merged":
                    found.add((rel, stack[-1] if stack else "<module>", node.lineno))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if named:
                stack.pop()

        visit(tree)
    return found


class WitnessTest(unittest.TestCase):
    def test_aucun_appel_direct_a_close_merged_hors_de_deliver(self):
        stray = sorted(c for c in _close_merged_callers() if c[:2] not in ALLOWED)
        self.assertEqual(stray, [], "close_merged appelé hors de work.deliver : %s"
                         % ", ".join("%s:%d (%s)" % (f, n, fn) for f, fn, n in stray))

    def test_le_temoin_voit_le_connecteur(self):
        # le témoin lit bien les appels : celui du connecteur est trouvé
        self.assertIn(("modules/dev/__init__.py", "deliver_git_merge"),
                      {c[:2] for c in _close_merged_callers()})


class ObservedMergeTest(PgTestCase):
    def notes(self, item_id):
        return [e["note"] or "" for e in work.events(self.db, item_id)]

    def test_un_constat_ferme_le_lot_et_note_le_connecteur(self):
        lot = work.add(self.db, title="fusion vue", app="ameesh")
        done = work.deliver(self.db, lot["id"], connector="git-merge",
                            evidence={"seen": "sync-github", "sha": SHA, "pr": "o/r#7"},
                            actor="t")
        self.assertEqual(done["result"], "merged")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")
        self.assertIn("merge seen by sync-github — o/r#7 (commit %s) [via git-merge]"
                      % SHA[:12], self.notes(lot["id"]))
        again = work.deliver(self.db, lot["id"], connector="git-merge",
                             evidence={"seen": "sync-github", "sha": SHA}, actor="t")
        self.assertEqual(again["result"], "already")

    def test_un_constat_exige_sa_source_et_rien_d_autre(self):
        lot = work.add(self.db, title="fusion vue", app="ameesh")
        with self.assertRaisesRegex(work.WorkError, "no source"):
            work.deliver(self.db, lot["id"], connector="git-merge",
                         evidence={"seen": "", "sha": SHA})
        with self.assertRaisesRegex(work.WorkError, "not head"):
            work.deliver(self.db, lot["id"], connector="git-merge",
                         evidence={"seen": "x", "head": SHA})
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")


if __name__ == "__main__":
    unittest.main()
