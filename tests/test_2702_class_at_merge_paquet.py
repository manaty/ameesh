# SPDX-License-Identifier: AGPL-3.0-only
"""#2702 — un lot léger fusionné dans un paquet se classe sur SON delta.

Constat #2556 (mty-amh-impl-05, 19/10) : un lot léger (2 fichiers,
`housekeeping.py` + son test) entre dans le paquet mixte h-1100 et se classe
`sensitive` sur le diff du paquet (`db.py`, `train_shadow.py`). `catch_up`
le marque « classe sensitive confirmée non-light » et n'ouvre jamais la
relecture après fusion que le canon §2 demande le jour même.

Deux pièges, une seule vérité à lire — le delta du lot :

* le commit enregistré peut être la fusion du paquet (ou le gel du lot entré
  par un merge du paquet) : `lot_diff_range` ramène l'intervalle au gel du lot
  contre le premier parent de la fusion ;
* la cible locale de l'assigné peut être périmée : `diff_range` retombait
  alors sur `merge-base(cible, gel)..gel` et emportait tout l'historique entre
  les deux (448 fichiers dans le clone de #2556). La cible par défaut du
  dépôt est essayée quand elle porte le gel.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from ameesh import lot_measures, plan_git, review_lot

from .test_1104_review_lot_hors_file import _Witness, _git


class LotDiffRangeTest(unittest.TestCase):
    """Le delta du lot, sans base : trois enregistrements de la même fusion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle.txt")
        self.socle = _git(self.repo, "rev-parse", "HEAD")
        # un commit sensible sur la cible APRÈS le socle : une cible locale
        # restée en arrière gonflerait le diff avec ce fichier
        self.commit("db/schema.sql")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        # le lot léger, puis un voisin sensible, puis le paquet dans main
        _git(self.repo, "checkout", "-q", "-b", "lot/leger", "main")
        self.commit("docs/leger.md")
        self.leger = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "-b", "lot/sensible", "main")
        self.commit("db/voisin.sql")
        self.sensible = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "-b", "integration/paquet", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet: lot leger",
             "lot/leger")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet: lot sensible",
             "lot/sensible")
        self.paquet = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet",
             "integration/paquet")
        self.merge = _git(self.repo, "rev-parse", "HEAD")

    def write(self, path, text="x\n"):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(text)

    def commit(self, path):
        self.write(path)
        _git(self.repo, "add", path)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % path)

    def files(self, rng):
        self.assertIsNotNone(rng)
        return _git(self.repo, "diff", "--name-only", *rng).splitlines()

    def test_le_merge_du_paquet_se_lit_sur_le_gel_du_lot(self):
        """Le jalon `merged` porte le merge du paquet (b053ee26) : la classe se
        lit sur le gel du lot, jamais sur les fichiers du voisin sensible."""
        self.assertEqual(self.files(lot_measures.lot_diff_range(
            self.repo, self.merge, "main", frozen=self.leger)),
            ["docs/leger.md"])

    def test_le_gel_du_lot_se_lit_sur_son_delta(self):
        """Le jalon `merged` porte le gel lui-même (déclaration #2556) : le gel
        est un ancêtre de la cible et son intervalle de fusion est le merge du
        paquet — on lit `merge-base(premier parent, gel)..gel`."""
        self.assertEqual(self.files(lot_measures.lot_diff_range(
            self.repo, self.leger, "main", frozen=self.leger)),
            ["docs/leger.md"])

    def test_une_cible_locale_perimee_n_elargit_pas_le_diff(self):
        """#2556 : `main` local est resté au socle, `origin/main` porte la
        fusion. Sans la cible par défaut, le repli `merge-base(cible,
        gel)..gel` emportait `db/schema.sql` et tout l'historique."""
        _git(self.repo, "update-ref", "refs/heads/main", self.socle)
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.merge)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")
        self.assertEqual(self.files(lot_measures.lot_diff_range(
            self.repo, self.leger, "main", frozen=self.leger)),
            ["docs/leger.md"])

    def test_le_lot_sensible_du_paquet_reste_sensible(self):
        """Le delta propre du voisin porte `db/voisin.sql` : la lecture du lot
        ne blanchit personne."""
        self.assertEqual(self.files(lot_measures.lot_diff_range(
            self.repo, self.merge, "main", frozen=self.sensible)),
            ["db/voisin.sql"])

    def test_un_lot_qui_est_le_paquet_garde_son_delta(self):
        """Le lot qui est lui-même le paquet garde son diff entier : le gel
        est la pointe du paquet, pas celle d'un lot qu'il porte."""
        self.assertEqual(self.files(lot_measures.lot_diff_range(
            self.repo, self.merge, "main", frozen=self.paquet)),
            ["db/voisin.sql", "docs/leger.md"])


class PacketClassWitness(_Witness):
    """La même fusion, vue par `class_at_merge` et par l'ouverture L539."""

    def mixed_packet(self, lot_branch):
        """Un paquet de deux lots sur `develop` : `lot_branch` et un voisin
        sensible ; rend le merge du paquet dans la cible."""
        _git(self.repo, "checkout", "-q", "-b", "lot/2702-voisin", "develop")
        self.commit("db/voisin.sql")
        _git(self.repo, "checkout", "-q", "-b", "integration/2702", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet: lot",
             lot_branch)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet: voisin",
             "lot/2702-voisin")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet 2702",
             "integration/2702")
        return _git(self.repo, "rev-parse", "HEAD")

    def test_un_lot_leger_dans_un_paquet_mixte_ouvre_sa_relecture(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/2702-leger")
        packet = self.mixed_packet("lot/2702-leger")
        klass = review_lot.class_at_merge(self.db, lot, repo=self.repo,
                                         target="develop", sha=packet)
        self.assertEqual(klass["class"], "light", klass)
        self.assertFalse(klass.get("doubt"), klass)
        opened = review_lot.open_for_merge(self.db, lot, merged_sha=packet,
                                           repo=self.repo, target="develop")
        self.assertTrue(opened["created"], opened)
        self.assertEqual(opened["class"], "light", opened)
        self.assertEqual(len(self.review_lots(lot)), 1)

    def test_un_lot_sensible_dans_un_paquet_mixte_n_ouvre_rien(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/2702-sensible", name="db/preuve.sql")
        packet = self.mixed_packet("lot/2702-sensible")
        opened = review_lot.open_for_merge(self.db, lot, merged_sha=packet,
                                           repo=self.repo, target="develop")
        self.assertFalse(opened["created"], opened)
        self.assertEqual(opened["class"], "sensitive", opened)

    def test_une_cible_locale_perimee_ne_saute_pas_la_relecture(self):
        """#2556 : la cible locale de l'assigné est restée en arrière alors que
        `origin/develop` porte la fusion ; le gel est dans origin, le diff lu
        ne doit pas emporter l'historique entre les deux."""
        socle = _git(self.repo, "rev-parse", "develop")
        # un commit sensible sur la cible avant le branchement du lot : le
        # repli sur la cible périmée le ferait entrer dans le diff
        self.commit("db/retard.sql")
        lot = self.lot()
        sha = self.frozen_on_branch(lot, "lot/2702-perime")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/2702-perime")
        merged = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", merged)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/develop")
        _git(self.repo, "config", "--unset", plan_git.TARGET_GIT_KEY)
        _git(self.repo, "update-ref", "refs/heads/develop", socle)
        opened = review_lot.open_for_merge(self.db, lot, merged_sha=sha,
                                           repo=self.repo, target="develop")
        self.assertTrue(opened["created"], opened)
        self.assertEqual(opened["class"], "light", opened)


if __name__ == "__main__":
    unittest.main()
