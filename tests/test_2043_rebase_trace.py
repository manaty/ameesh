# SPDX-License-Identifier: AGPL-3.0-only
"""#2043 : les deux réserves P2 de la relecture de #1516 sur la trace du défaut.

1. Un corps multiligne devenu sujet n'était pas reconnu : la trace notée était
   la première ligne du corps, alors que `%s` rend le premier paragraphe joint
   par des espaces. Un vrai sujet perdu était classé « abandon volontaire ».
2. La trace était cherchée dans tous les ancêtres de HEAD : un commit d'amont
   dont le sujet valait le corps d'un commit abandonné faisait passer un drop
   volontaire pour une perte (sortie 4).

Banc hors ligne : dépôts git jetables, aucun réseau, aucune base. Les greffons
de fixture viennent du banc #1516."""
from __future__ import annotations

import unittest

from tests import test_1516_rebase_lot_options as banc

_git, LANCEUR = banc._git, banc.LANCEUR


class RebaseTraceTest(banc.RebaseLotOptionsTest):
    def _lot_corps_multiligne(self) -> None:
        """Le commit de lot a un corps de deux lignes."""
        self._lot_en_conflit()
        _git(self.depot, "commit", "-q", "--amend",
             "-m", "#900 sujet du lot",
             "-m", "premiere ligne\ndeuxieme ligne")

    def _lot_dont_le_corps_est_un_sujet_d_amont(self) -> None:
        """#2 à abandonner a pour corps « corps 2 », sujet d'un commit d'amont."""
        self._initialiser()
        self._commit("f.txt", "a\n", "base")
        _git(self.depot, "checkout", "-qb", "lot")
        self._commit("g.txt", "x\n", "#1 a garder", "corps 1")
        self._commit("h.txt", "y\n", "#2 a abandonner", "corps 2")
        _git(self.depot, "checkout", "-q", "main")
        self._commit("m.txt", "m\n", "corps 2")
        _git(self.depot, "checkout", "-q", "lot")

    def test_corps_multiligne_devenu_sujet_est_signale(self) -> None:
        """Le premier paragraphe du corps, joint, est la trace du défaut."""
        self._lot_corps_multiligne()
        self._conflit("bash", LANCEUR, "main")
        proc = self._lancer("bash", LANCEUR, "--continue", editeur="sed -i 1d")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn(banc.PERDU, proc.stderr)
        self.assertIn("#900 sujet du lot", proc.stderr)
        self.assertEqual(self._sujets()[0], "premiere ligne deuxieme ligne")

    def test_drop_dont_le_corps_est_un_sujet_d_amont_n_est_pas_une_perte(self) -> None:
        """Un sujet d'amont hors plage ne vaut pas trace d'un abandon."""
        self._lot_dont_le_corps_est_un_sujet_d_amont()
        proc = self._lancer("bash", LANCEUR, "-i", "main",
                            env={"GIT_SEQUENCE_EDITOR": "sed -i 2d"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(banc.ABANDON, proc.stderr)
        self.assertIn("#2 a abandonner", proc.stderr)
        self.assertNotIn(banc.PERDU, proc.stderr)
        sujets = self._sujets()
        self.assertIn("#1 a garder", sujets)
        self.assertNotIn("#2 a abandonner", sujets)
        self.assertIn("corps 2", sujets)


# Le banc #1516 ne prête que ses greffons : ses témoins tournent dans leur
# propre module, pas une seconde fois ici.
for _nom in dir(banc.RebaseLotOptionsTest):
    if _nom.startswith("test_"):
        setattr(RebaseTraceTest, _nom, None)


if __name__ == "__main__":
    unittest.main()
