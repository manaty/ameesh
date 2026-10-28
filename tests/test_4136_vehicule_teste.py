# SPDX-License-Identifier: AGPL-3.0-only
"""#4136 — un véhicule de paquet-auto n'est pas prévalidé une seconde fois.

paquet-auto a joué en local, avant le gel, les tests des unités, des voisins
et des contrats ; son manifeste les consigne. Ni le gel (`after_freeze`) ni
`ci run --full` (`gate_packet`) ne relancent d'offload sur ce véhicule. Un
lot ordinaire et un véhicule composé à la main gardent leur prévalidation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import prevalidation_calls as calls

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
BRANCH = "integration/paquet-h-20261028-02"
MANIFEST = "reviews/integration-paquet-h-20261028-02.md"
AUTO = """# Paquet integration/paquet-h-20261028-02 — composition (§12)

Porteur : **#4125**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 3.1 s.
- Tests locaux : 4 séries, 15 modules, %d tests exécutés, %d échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 10 sauté(s) : `tests.test_cluster`.
"""


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


class Tree:
    def __init__(self, manifest: str | None):
        self.root = tempfile.mkdtemp(prefix="ameesh-4136-")
        git(self.root, "init", "-q", "-b", "main")
        with open(os.path.join(self.root, "README"), "w") as fh:
            fh.write("base\n")
        if manifest is not None:
            os.makedirs(os.path.join(self.root, "reviews"))
            with open(os.path.join(self.root, MANIFEST), "w", encoding="utf-8") as fh:
                fh.write(manifest)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "tête")
        self.head = git(self.root, "rev-parse", "HEAD")


class ComposerTestedTest(unittest.TestCase):
    def tree(self, manifest):
        tree = Tree(manifest)
        self.addCleanup(shutil.rmtree, tree.root, True)
        return tree

    def dispensed(self, tree, branch=BRANCH):
        return calls.composer_tested(None, None, {"id": 4125, "branch": branch},
                                     tree.head, repo=tree.root)

    def test_paquet_auto_vehicle_with_green_local_tests(self):
        tree = self.tree(AUTO % (1969, 0))
        got = self.dispensed(tree)
        self.assertEqual(got["sha"], tree.head)
        self.assertEqual(got["manifest"], MANIFEST)
        self.assertEqual(got["tests"], 1969)

    def test_ordinary_lot_is_not_dispensed(self):
        tree = self.tree(AUTO % (1969, 0))
        self.assertIsNone(self.dispensed(tree, branch="lot/4136-x"))

    def test_hand_composed_vehicle_is_not_dispensed(self):
        tree = self.tree(AUTO.replace("`scripts/paquet-auto.py` (sans agent)",
                                      "ameesh-pilote") % (1969, 0))
        self.assertIsNone(self.dispensed(tree))

    def test_vehicle_without_manifest_is_not_dispensed(self):
        self.assertIsNone(self.dispensed(self.tree(None)))

    def test_a_commit_after_the_manifest_is_not_dispensed(self):
        tree = self.tree(AUTO % (1969, 0))
        with open(os.path.join(tree.root, "README"), "a") as fh:
            fh.write("correctif à la main\n")
        git(tree.root, "commit", "-qam", "correctif")
        tree.head = git(tree.root, "rev-parse", "HEAD")
        self.assertIsNone(self.dispensed(tree))

    def test_no_local_test_or_a_failure_is_not_dispensed(self):
        self.assertIsNone(self.dispensed(self.tree(AUTO % (0, 0))))
        self.assertIsNone(self.dispensed(self.tree(AUTO % (1969, 2))))
        self.assertIsNone(self.dispensed(self.tree(AUTO.split("- Tests locaux")[0] % ())))


class HooksTest(unittest.TestCase):
    """Témoins : véhicule de paquet-auto → aucun offload, ni au gel ni à ci run."""

    def setUp(self):
        self.tree = Tree(AUTO % (1969, 0))
        self.addCleanup(shutil.rmtree, self.tree.root, True)
        self.notes = []
        for patch in (mock.patch("ameesh.work.note", side_effect=lambda db, lot, text, **kw:
                                 self.notes.append(text)),
                      mock.patch.object(calls, "run", side_effect=AssertionError("offload"))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_freeze_launches_no_offload(self):
        item = {"id": 4125, "app": "ameesh", "branch": BRANCH, "branch_target": "main"}
        with mock.patch("ameesh.work.get", return_value=item):
            got = calls.after_freeze(None, object(), 4125, self.tree.head,
                                     repo=self.tree.root)
        self.assertEqual(got["state"], "skipped")
        self.assertEqual(self.notes, [
            "prevalidation skipped: vehicle tested by the composer (manifest %s)"
            % self.tree.head[:12]])

    def test_ci_run_launches_no_offload(self):
        got = calls.gate_packet(None, None, {"id": 4125, "app": "ameesh", "branch": BRANCH},
                                {"sha": self.tree.head, "base": "b" * 40},
                                repo=self.tree.root)
        self.assertEqual(got["state"], "skipped")
        self.assertIn("vehicle tested by the composer", got["why"])

    def test_ordinary_lot_freeze_still_prevalidates(self):
        item = {"id": 4136, "app": "ameesh", "branch": "lot/4136-x", "branch_target": "main"}
        with mock.patch("ameesh.work.get", return_value=item), \
                mock.patch.object(calls, "target_base", return_value="b" * 40):
            got = calls.after_freeze(None, object(), 4136, self.tree.head,
                                     repo=self.tree.root)
        # `run` lève : la prévalidation a bien été tentée (rendue indisponible)
        self.assertEqual(got["state"], "unavailable")
        self.assertIn("offload", got["why"])


if __name__ == "__main__":
    unittest.main()
