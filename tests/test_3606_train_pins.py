# SPDX-License-Identifier: AGPL-3.0-only
"""#3606 : le gel refuse une source du contrat de train sans son épingle.

#3581 (3a5bc588) a changé `.github/workflows/tests.yml` sans ré-épingler
`src/ameesh/train_contracts.py` ; le gel l'a accepté, et le rouge n'est apparu
que dans la CI du paquet, quatre heures plus tard, au prix d'une CI perdue et
d'une recomposition. Le contrôle est statique et instantané : il lit l'arbre
construit (un `git archive`, sans `.git`), hashe chaque source du contrat comme
Git le ferait et la compare à son épingle.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from ameesh import train_contracts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def message(mismatches) -> str:
    """Le refus lisible : chemin, blob de l'arbre, blob épinglé, fichier à jour."""
    return "\n".join(
        "%s: tree blob %s, pinned %s; update %s" % (
            path, actual or "missing", pinned, pins_at)
        for path, actual, pinned, pins_at in mismatches)


class TrainPinsTest(unittest.TestCase):
    def test_the_current_tree_matches_its_pins(self):
        mismatches = train_contracts.stale_pins(ROOT)
        self.assertEqual([], mismatches, message(mismatches))

    def test_a_changed_source_with_its_old_pin_is_refused(self):
        # L'arbre de 3a5bc588, reconstitué : tests.yml changé, épingle
        # inchangée. Le commit historique n'est pas garanti dans un clone
        # superficiel de CI ; le témoin reproduit sa forme dans un arbre jetable.
        contract, = train_contracts.ameesh()
        with tempfile.TemporaryDirectory(prefix="ameesh-3606-") as tree:
            for path, _blob in contract.sources:
                target = os.path.join(tree, path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(os.path.join(ROOT, path), target)
            changed = os.path.join(tree, ".github/workflows/tests.yml")
            with open(changed, "a", encoding="utf-8") as handle:
                handle.write("\n# source modifiée sans ré-épinglage\n")
            mismatches = train_contracts.stale_pins(tree)
        self.assertEqual([".github/workflows/tests.yml"],
                         [path for path, _actual, _pinned, _pins_at in mismatches],
                         message(mismatches))
        path, actual, pinned, pins_at = mismatches[0]
        contract, = train_contracts.ameesh()
        self.assertNotEqual(actual, pinned)
        self.assertEqual(dict(contract.sources)[path], pinned)
        self.assertEqual(contract.pins_at, pins_at)
        self.assertEqual("src/ameesh/train_contracts.py", pins_at)
        refusal = message(mismatches)
        for part in (path, actual, pinned, pins_at):
            self.assertIn(part, refusal)


if __name__ == "__main__":
    unittest.main()
