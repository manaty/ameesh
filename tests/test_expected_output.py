# SPDX-License-Identifier: AGPL-3.0-only
"""#749 — expected negative output is prefixed in the log, never renamed at the source.

The full-suite bilan reader (`scripts/test-parallele.sh`) looks for a unittest
verdict at the start of a line (`grep -E '^(Ran |OK|FAILED)'`). A test that
verifies a deliberate negative output — the production message
`ROLE CONTRACT FAILED` of `deploy/sql/role-executor.sql`, the cache failure
`FAILED: neither the cache nor a local npm ci` — surfaces it in its assertion
message; the raw line must not be able to pass for a verdict. `tests.support.attendu()`
prefixes it at that surface, and the production message itself is left alone
(mesh-design decision of 13/10/2026, #749 point 3).

The witness freezes the three sides: the helper, the reader's pattern as it is
actually written in the script, and the application in the module of the
release incident (`tests/test_l56_role_executor.py`). No database needed.
"""
from __future__ import annotations

import ast
import pathlib
import re
import unittest

from tests.support import attendu

REPO = pathlib.Path(__file__).resolve().parent.parent
PARALLELE = REPO / "scripts" / "test-parallele.sh"
ROLE_SQL = REPO / "deploy" / "sql" / "role-executor.sql"
L56 = REPO / "tests" / "test_l56_role_executor.py"


def bilan_pattern() -> str:
    """Le motif du bilan, lu dans le script (jamais recopié : s'il change,
    le témoin doit le dire)."""
    texte = PARALLELE.read_text(encoding="utf-8")
    trouve = re.search(r"grep -E '([^']+)'", texte)
    if trouve is None:
        raise AssertionError("scripts/test-parallele.sh : lecteur de bilan introuvable")
    return trouve.group(1)


class AttenduTest(unittest.TestCase):
    def test_prefixe_chaque_ligne(self):
        self.assertEqual(attendu("FAILED: contrat\nligne suivante"),
                         "attendu: FAILED: contrat\nattendu: ligne suivante")
        self.assertEqual(attendu(""), "")
        self.assertEqual(attendu("seule"), "attendu: seule")
        # les lignes vides ne portent pas de préfixe inutile
        self.assertEqual(attendu("a\n\nb"), "attendu: a\n\nattendu: b")

    def test_le_bilan_ne_confond_pas_une_sortie_attendue_avec_un_verdict(self):
        motif = bilan_pattern()
        # le lecteur reconnaît toujours les vrais verdicts
        for verdict in ("Ran 12 tests in 3.4s\n\nOK\n",
                        "Ran 12 tests in 3.4s\n\nFAILED (failures=1)\n"):
            self.assertIsNotNone(re.search(motif, verdict, re.M), verdict)
        # ... et une sortie négative volontaire brute (src/ameesh/deps.py,
        # surfacee par un test d'échec attendu) lui ressemble : c'est le piège
        sortie = "FAILED: neither the cache nor a local npm ci"
        self.assertIsNotNone(re.search(motif, sortie, re.M),
                             "témoin sans objet si la sortie brute ne ressemble pas à un verdict")
        self.assertIsNone(re.search(motif, attendu(sortie), re.M),
                          "une sortie négative volontaire préfixée ne doit plus passer pour un verdict")

    def test_le_message_de_production_nest_pas_renomme(self):
        texte = ROLE_SQL.read_text(encoding="utf-8")
        self.assertIn("RAISE EXCEPTION 'ROLE CONTRACT FAILED", texte,
                      "le message de production de role-executor.sql ne se renomme pas "
                      "pour un lecteur de CI (#749)")

    def test_les_sorties_negatives_volontaires_de_l56_sont_prefixees(self):
        """Toute assertion `assertIn` sur un stderr, dans le module de
        l'incident de release, passe la sortie attendue par `attendu()`."""
        arbre = ast.parse(L56.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if not (isinstance(noeud, ast.Call)
                    and isinstance(noeud.func, ast.Attribute)
                    and noeud.func.attr == "assertIn"
                    and len(noeud.args) >= 2):
                continue
            if ".stderr" not in ast.unparse(noeud.args[1]):
                continue
            messages = [ast.unparse(a) for a in noeud.args[2:]]
            messages += [ast.unparse(k.value) for k in noeud.keywords]
            self.assertTrue(any("attendu(" in m for m in messages),
                            "ligne %d : sortie négative volontaire non préfixée (#749) : %s"
                            % (noeud.lineno, ast.unparse(noeud)[:120]))


if __name__ == "__main__":
    unittest.main()
