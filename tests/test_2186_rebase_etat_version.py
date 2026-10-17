# SPDX-License-Identifier: AGPL-3.0-only
"""#2186 : un état écrit par une autre version du lanceur ne doit pas sauter
les contrôles #1669.

`scripts/rebase-lot.sh` lit la marque de l'état des sujets (`etat-v3`) et, si
elle vient d'une autre version, ignore ce seul contrôle : les sujets attendus
sont illisibles. La plage rejouée, elle, est lue de `base_etat`/`lot_etat` ;
le préfixe du lot et les marqueurs de conflit restent donc contrôlés.

Banc hors ligne : dépôts git jetables, aucun réseau, aucune base. Les greffons
de fixture viennent du banc #1516."""
from __future__ import annotations

import os
import unittest

from tests import test_1516_rebase_lot_options as banc

_git, LANCEUR = banc._git, banc.LANCEUR


class RebaseEtatVersionTest(banc.RebaseLotOptionsTest):
    def _lot_sans_prefixe_en_conflit(self) -> None:
        """``lot/1476-essai`` : un commit sans préfixe « #1476 », en conflit."""
        self._initialiser()
        self._commit("f.txt", "l1\nl2\nl3\n", "base")
        _git(self.depot, "checkout", "-qb", "lot/1476-essai")
        self._commit("f.txt", "l1\nlot\nl3\n", "sujet sans prefixe")
        _git(self.depot, "checkout", "-q", "main")
        self._commit("f.txt", "l1\nmain\nl3\n", "main avance")
        _git(self.depot, "checkout", "-q", "lot/1476-essai")

    def _etat_ancien(self) -> None:
        """Ramène la marque de l'état à l'ancienne version (v2) du lanceur."""
        chemin = _git(self.depot, "rev-parse", "--git-path",
                      "ameesh-rebase-lot.sujets").stdout.strip()
        chemin = chemin if os.path.isabs(chemin) else os.path.join(self.depot, chemin)
        with open(chemin, encoding="utf-8") as fh:
            lignes = fh.readlines()
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("rebase-lot/etat-v2\n")
            fh.writelines(lignes[1:])

    def test_etat_ancien_garde_le_controle_du_prefixe(self) -> None:
        """Un état d'une autre version ne saute que le contrôle des sujets.

        La plage rejouée est lue de base_etat/lot_etat, pas de l'état des
        sujets ; le préfixe du lot reste contrôlé (sortie 4)."""
        self._lot_sans_prefixe_en_conflit()
        self._conflit("bash", LANCEUR, "main")
        self._etat_ancien()
        proc = self._lancer("bash", LANCEUR, "--continue")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("contrôle des sujets ignoré", proc.stderr)
        self.assertIn("sans le préfixe « #1476 »", proc.stderr)
        self.assertIn("sujet sans prefixe", proc.stderr)

    def test_etat_ancien_ne_signe_plus_les_sujets_perdus(self) -> None:
        """Sous un état v2, un sujet mangé n'est pas inventé en perte."""
        self._lot_en_conflit()
        self._conflit("bash", LANCEUR, "main")
        self._etat_ancien()
        proc = self._lancer("bash", LANCEUR, "--continue", editeur="sed -i 1d")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("contrôle des sujets ignoré", proc.stderr)
        self.assertNotIn(banc.PERDU, proc.stderr)
        self.assertEqual(self._sujets()[0], "corps du lot")


# Le banc #1516 ne prête que ses greffons : ses témoins tournent dans leur
# propre module, pas une seconde fois ici.
for _nom in dir(banc.RebaseLotOptionsTest):
    if _nom.startswith("test_"):
        setattr(RebaseEtatVersionTest, _nom, None)


if __name__ == "__main__":
    unittest.main()
