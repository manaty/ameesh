# SPDX-License-Identifier: AGPL-3.0-only
"""Les règles de retrait du catalogue (L14) — la partie qui retire des modèles.

Trois gardes, chacune avec son test, plus le cas normal : ce sont les seules
choses qui séparent « un modèle a disparu » de « la source a mal répondu ».
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ameesh.catalog import ActiveModel, reconcile  # noqa: E402
from ameesh.discovery.base import ModelSeen, SourceResult  # noqa: E402


def active(*ids: str, source: str = "openai", provider: str = "openai") -> list[ActiveModel]:
    return [ActiveModel(provider=provider, model_id=model, source=source) for model in ids]


def seen(*ids: str, source: str = "openai", complete: bool = True) -> SourceResult:
    return SourceResult(
        source=source,
        complete=complete,
        models=tuple(ModelSeen(provider="openai", model_id=model, source=source) for model in ids),
    )


class RetraitTest(unittest.TestCase):
    def test_absence_dans_une_liste_complete_retire_le_modele(self):
        decision = reconcile(active("a", "b", "c"), seen("a", "b"))
        self.assertEqual([row.model_id for row in decision.to_retire], ["c"])
        self.assertEqual(decision.refused_reason, "")

    def test_une_liste_incomplete_ne_retire_rien(self):
        decision = reconcile(active("a", "b"), seen("a", complete=False))
        self.assertEqual(decision.to_retire, ())
        self.assertEqual(decision.refused_reason, "incomplete")

    def test_une_liste_complete_mais_vide_ne_retire_rien_et_le_dit(self):
        decision = reconcile(active("a", "b", "c"), seen())
        self.assertEqual(decision.to_retire, ())
        self.assertEqual(decision.refused_reason, "empty")
        self.assertIn("rien n'est retiré", decision.detail)

    def test_un_retrait_massif_est_refuse_et_un_humain_tranche(self):
        # 4 modèles actifs, la source n'en voit plus qu'un : 3 retraits sur 4 > moitié.
        decision = reconcile(active("a", "b", "c", "d"), seen("a"))
        self.assertEqual(decision.to_retire, ())
        self.assertEqual(decision.refused_reason, "mass-withdrawal")
        self.assertEqual(len(decision.mass_withdrawal_refused), 3)
        self.assertIn("un humain tranche", decision.detail)

    def test_exactement_la_moitie_passe(self):
        # La garde est « plus de la moitié », pas « la moitié » : 2 sur 4 est permis.
        decision = reconcile(active("a", "b", "c", "d"), seen("a", "b"))
        self.assertEqual(sorted(row.model_id for row in decision.to_retire), ["c", "d"])
        self.assertEqual(decision.refused_reason, "")

    def test_une_autre_source_ne_retire_jamais_les_modeles_d_une_autre(self):
        state = active("a", "b", source="anthropic", provider="anthropic")
        decision = reconcile(state, seen("x", source="openai"))
        self.assertEqual(decision.to_retire, (), "le catalogue d'une source ne touche pas à l'autre")
        # Rien à faire, et c'est la bonne réponse : cette source n'a aucune ligne active,
        # donc il n'y a rien à retirer. La garde « liste vide » vise l'autre cas — des
        # lignes actives et une liste complète vide — qui a son propre test.
        self.assertFalse(decision.changed)
        self.assertEqual(decision.refused_reason, "")

    def test_rien_a_faire_quand_tout_est_la(self):
        decision = reconcile(active("a", "b"), seen("a", "b"))
        self.assertFalse(decision.changed)
        self.assertEqual(decision.seen, ("a", "b"))
