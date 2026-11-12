# SPDX-License-Identifier: AGPL-3.0-only
"""#7012 — la preuve de repli jouée sur un vrai commit de composition.

#6990 attache la relecture due après fusion à la marque de repli écrite sur le
gel admis ; `_fallback_on_merge` ne la retient que si `_heads_on_merge` prouve
que ce gel est une des têtes entrées dans la fusion (`_entered`, patch-id). Les
témoins de #6990 simulaient cette preuve : le gel y était le sha fusionné
lui-même, jamais un membre d'un paquet.

Ici un dépôt réel compose un lot dans un paquet (`composition : #<lot> <gel>`)
avec une seconde unité, puis fusionne le tout sur la cible. La preuve est jouée
sur le commit de composition du membre — celui que `sync-merges` enregistre
comme jalon `merged` — et non sur le commit de fusion final, dont le delta
porte toutes les unités.

Le rattrapage suit : quand le clone ne porte pas encore le commit (preuve
absente), une marque de repli ne se perd pas en terminal ; elle reçoit une
tentative comptée, et la passe suivante, avec le clone complet, ouvre la
mission de la famille en pause. Sans aucune marque de repli, une classe
confirmée non légère reste terminale comme avant.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import review_family_paused, review_lot, storage, work

from .test_1104_review_lot_hors_file import _Witness, _git

T = 1792812000.0
H = 3600.0


class PacketCompositionProofTest(_Witness):
    """Un vrai paquet : composition, preuve par patch-id, puis rattrapage."""

    def setUp(self):
        super().setUp()
        self.lot = self.lot("un lot de porte")
        # Le gel du lot est un vrai commit de sa branche, pas le futur sha de
        # fusion : c'est ce que la composition du paquet doit faire entrer.
        self.freeze = self.frozen_on_branch(
            self.lot, "lot/7012-porte", name="src/ameesh/merge_queue.py")
        self.build_packet()

    # -- dépôt réel ------------------------------------------------------
    def build_packet(self):
        """Compose le lot avec une seconde unité, puis fusionne le paquet.

        `unit` est le commit de composition qui fait entrer le gel du lot (ce
        que `sync-merges` retient comme fusion du membre) ; `merged` est la
        fusion finale du véhicule sur la cible, dont le delta porte les deux
        unités et ne peut donc pas prouver le seul gel du lot.
        """
        _git(self.repo, "checkout", "-q", "-b", "integration/paquet-7012",
             "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "composition : #%d %s" % (self.lot, self.freeze),
             "lot/7012-porte")
        self.unit = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "-q", "-b", "lot/7012-autre", "develop")
        self.commit("docs/autre-unite.md")
        other = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "integration/paquet-7012")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "composition : #999999 %s" % other, "lot/7012-autre")
        self.integration = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge paquet 7012",
             "integration/paquet-7012")
        self.merged = _git(self.repo, "rev-parse", "HEAD")

    def fallback_note(self):
        work.note(
            self.db, self.lot,
            review_family_paused.fallback_note(
                {"family": "codex", "resume_ts": T + 30 * H,
                 "accounts": ["primary"]}, self.freeze),
            actor="ameesh")

    def declare_merge(self, sha):
        """Enregistre `sha` comme fusion du lot, sans ouvrir de mission ici.

        L'ouverture après fusion est l'objet du test ; `catch_up` la refait
        depuis le jalon `merged`, comme un balayage réel.
        """
        with mock.patch.object(
                review_lot, "open_after_merge",
                return_value={"id": None, "created": False,
                              "why": "témoin : ouverture contrôlée par catch_up"}):
            out = work.merged(self.db, self.lot, sha=sha, actor="claude1",
                              repo=self.repo)
        self.assertEqual(out["result"], "merged", out)

    def item(self):
        return work.get(self.db, self.lot)

    def reviews(self):
        return storage.of(self.db).work.by_issue_ref(
            review_lot.ref_of(self.lot))

    def catch_up(self, *, repo, retry_s=0):
        with mock.patch.object(
                review_lot, "class_at_merge",
                return_value={"class": "sensitive", "why": "porte de fusion"}):
            return review_lot.catch_up(self.db, repo=repo, retry_s=retry_s)

    # -- témoins ---------------------------------------------------------
    def test_the_composition_unit_proves_the_frozen_head(self):
        """La preuve réelle : `_heads_on_merge` puis `_entered` par patch-id."""
        heads = review_lot._heads_on_merge(
            self.db, self.lot, self.unit, repo=self.repo)
        self.assertEqual([head[:12] for head in heads],
                         [self.unit[:12], self.freeze[:12]])
        # Le commit de fusion final porte le delta des deux unités : il ne
        # prouve pas le seul gel du lot, et ne doit pas servir de preuve.
        self.assertFalse(review_lot._entered(
            self.repo, self.merged, self.freeze))

        self.fallback_note()
        payload = review_lot._fallback_on_merge(
            self.db, storage.of(self.db).work, self.lot, self.item(),
            self.unit, repo=self.repo)
        self.assertIsNotNone(payload, "la marque de repli n'a pas été rattachée")
        self.assertEqual(payload["family"], "codex")

    def test_the_after_merge_mission_is_opened_on_the_composition_proof(self):
        self.fallback_note()
        self.declare_merge(self.unit)
        out = review_lot.open_after_merge(
            self.db, lot=self.lot,
            klass={"class": "sensitive", "why": "porte de fusion"},
            merged_sha=self.unit, repo=self.repo)
        self.assertTrue(out["created"], out)
        self.assertEqual(out["reviews"], 1)
        self.assertEqual(out["fallback"]["family"], "codex")
        (mission,) = self.reviews()
        self.assertIn("Repli #6990", work.get(self.db, mission["id"])["body"])

    def test_a_missing_proof_is_a_counted_attempt_not_a_terminal(self):
        """Clone incomplet : la mission reste due et se réessaie.

        `decoy` est un dépôt lisible mais sans les commits du paquet : la
        classe est confirmée, la marque de repli est là, la preuve Git manque.
        Le premier passage compte une tentative ; le second, avec le clone
        complet, ouvre la mission — la relecture n'est pas perdue.
        """
        self.fallback_note()
        self.declare_merge(self.unit)
        decoy = os.path.join(self.tmp, "clone-incomplet")
        os.makedirs(decoy)
        _git(decoy, "init", "-q", "-b", "develop")

        first = self.catch_up(repo=decoy)
        self.assertEqual(
            (first["examined"], first["missing"], first["attempted"],
             first["terminal"]), (1, 1, 1, 0), first)
        self.assertEqual(self.reviews(), [])
        attempts = [event for event in storage.of(self.db).work.events(
            self.lot, 50)
            if str(event.get("note") or "").startswith(
                review_lot.ATTEMPT_PREFIX + str(self.lot))]
        self.assertEqual(len(attempts), 1, attempts)

        second = self.catch_up(repo=self.repo)
        self.assertEqual((second["examined"], second["opened"]), (1, 1), second)
        (mission,) = self.reviews()
        self.assertEqual(mission["issue_ref"], review_lot.ref_of(self.lot))
        body = work.get(self.db, mission["id"])["body"]
        self.assertIn("Repli #6990", body)
        self.assertIn("famille **codex**", body)

    def test_without_a_fallback_mark_a_sensitive_class_stays_terminal(self):
        """La distinction #7012 : aucun repli marqué, aucune mission due."""
        self.declare_merge(self.unit)
        out = self.catch_up(repo=self.repo)
        self.assertEqual(
            (out["examined"], out["missing"], out["attempted"],
             out["terminal"]), (1, 1, 0, 1), out)
        self.assertEqual(self.reviews(), [])

    def test_a_fallback_mark_on_another_freeze_does_not_retry(self):
        """Une marque d'un autre gel ne réclame pas la tête examinée (#2585)."""
        work.note(
            self.db, self.lot,
            review_family_paused.fallback_note(
                {"family": "codex", "resume_ts": T + 30 * H,
                 "accounts": ["primary"]}, "b" * 40),
            actor="ameesh")
        self.declare_merge(self.unit)
        out = self.catch_up(repo=self.repo)
        self.assertEqual(
            (out["examined"], out["missing"], out["attempted"],
             out["terminal"]), (1, 1, 0, 1), out)
        self.assertEqual(self.reviews(), [])


if __name__ == "__main__":
    unittest.main()
