# SPDX-License-Identifier: AGPL-3.0-only
"""#5562 — la garde du bloc des règles mesure la fusion avec la cible.

La garde de #5556 mesurait la tête gelée. Deux lots du canon, chacun sous la
limite, partis d'une cible plus ancienne, pouvaient donc couper `main` une
fois fusionnés (N2 de la revue de #5556). Témoins : la fusion d'essai de la
tête avec la cible courante est mesurée ; une tête coupée dont la fusion tient
passe ; une cible déjà coupée n'est refusée que si la fusion l'aggrave ; un
conflit ne mesure pas et le dit sans bloquer le gel.
"""
from __future__ import annotations

import tempfile
import types
import unittest
from unittest import mock

from ameesh import canon, freeze_gate, lot_class
from ameesh.work import WorkError

from .test_canon import commit_all, git, write
from .test_l538_process_inject import RACINE, _canon_approuve, carte

ITEM = {"branch": "canon/5562-lot", "branch_target": "main"}


def _carte(octets: int, titre: str) -> str:
    """Une carte Process au corps calibré, sous le plafond d'une carte."""
    return carte(body="# %s\n\n- %s\n" % (titre, "a" * octets), title=titre)


class FusionCibleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clone = _canon_approuve(tmp.name, contenu=_carte(5200, "Socle"))
        git(self.clone, "checkout", "-qb", "canon/5562-lot")
        patcher = mock.patch.object(lot_class, "canon_clones",
                                    return_value=[(self.clone, "")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _commit(self, path: str, texte: str, message: str, *, push: bool = False) -> str:
        write(self.clone, path, texte)
        return commit_all(self.clone, message, push=push)

    def _gate(self, sha: str) -> str:
        return freeze_gate._canon_rules_block(
            types.SimpleNamespace(cfg=None), ITEM, sha, self.clone)

    def _mesure(self, ref: str) -> canon.ProcessBlock:
        return canon.process_block(canon.load(self.clone, ref=ref))

    def test_deux_lots_sous_la_limite_coupent_la_fusion(self):
        # Le lot part d'une cible ancienne et ajoute SA carte ; la cible a
        # depuis reçu une autre carte. Chacune tient seule, la fusion coupe.
        sha = self._commit("processus/lot-a.md", _carte(5200, "Règle A"), "lot A")
        self.assertFalse(self._mesure(sha).cut, "la tête seule doit tenir")
        git(self.clone, "checkout", "-q", "main")
        self._commit("processus/lot-b.md", _carte(5200, "Règle B"), "lot B", push=True)
        cible = self._mesure("origin/main")
        self.assertFalse(cible.cut, "la cible seule doit tenir")
        with self.assertRaisesRegex(
                WorkError, r"process-rules-block-too-long.*CUT.*merging head .* "
                           r"into main cuts the rules block.*head alone"):
            self._gate(sha)

    def test_tete_coupee_mais_fusion_avec_la_cible_sous_la_limite_passe(self):
        # La cible a raccourci le socle depuis le départ du lot : la tête,
        # coupée seule, ne coupe plus une fois fusionnée.
        git(self.clone, "checkout", "-q", "main")
        self._commit(RACINE, _carte(10000, "Socle"), "socle long", push=True)
        git(self.clone, "checkout", "-qB", "canon/5562-lot")
        sha = self._commit("processus/lot-a.md", _carte(5200, "Règle A"), "lot A")
        self.assertTrue(self._mesure(sha).cut, "la tête seule doit couper")
        git(self.clone, "checkout", "-q", "main")
        self._commit(RACINE, _carte(1000, "Socle"), "socle court", push=True)
        self.assertFalse(self._mesure("origin/main").cut)
        note = self._gate(sha)
        self.assertIn("merge result of %s into main" % sha[:12], note)
        self.assertIn("margin", note)

    def test_cible_deja_coupee_non_aggravee_passe(self):
        git(self.clone, "checkout", "-q", "main")
        self._commit(RACINE, _carte(canon.RULES_BLOC_ROOM_COMPAT + 500, "Socle"),
                     "cible coupée", push=True)
        git(self.clone, "checkout", "-qB", "canon/5562-lot")
        sha = self._commit(RACINE, _carte(canon.RULES_BLOC_ROOM_COMPAT + 100, "Socle"),
                           "lot qui raccourcit")
        note = self._gate(sha)
        self.assertIn("already cut at the target", note)
        self.assertIn("not grown", note)

    def test_conflit_ne_mesure_pas_et_le_dit(self):
        sha = self._commit(RACINE, _carte(2000, "Socle"), "lot")
        git(self.clone, "checkout", "-q", "main")
        self._commit(RACINE, _carte(3000, "Socle"), "cible", push=True)
        note = self._gate(sha)
        self.assertIn("merge with the target conflicts", note)
        self.assertIn("rules block", note)

    def test_marge_de_la_fusion_dans_la_note(self):
        sha = self._commit("processus/lot-a.md", _carte(100, "Règle A"), "lot A")
        git(self.clone, "checkout", "-q", "main")
        self._commit("processus/lot-b.md", _carte(100, "Règle B"), "lot B", push=True)
        note = self._gate(sha)
        self.assertRegex(note, r"^canon-rules-block: rules block \d+/\d+ bytes, "
                               r"margin \d+ bytes \(oldest served version\) "
                               r"\(merge result of %s into main\)"
                         % sha[:12])


if __name__ == "__main__":
    unittest.main()
