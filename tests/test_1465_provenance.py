# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — le fournisseur exact d'origine d'un commit et d'un jalon.

Deux niveaux : le contrat pur (sujets, SHA complets, capture d'identité) sans
base, puis la lecture réelle sur Postgres (migration 0445) : la preuve jointe
au fait, l'absence qui rend `unknown`, la divergence refusée, l'ajout seul.
"""
from __future__ import annotations

import unittest

from ameesh import review_provenance as prov, storage, work

from .support import PgTestCase

SHA1 = "a" * 40
SHA1_FULL = "b" * 40
SHA256 = "c" * 64


class ContractTest(unittest.TestCase):
    """Le contrat seul : ce qui est refusé avant toute base."""

    def test_full_sha_and_subjects(self):
        self.assertEqual(prov.full_sha("A" * 40), "a" * 40)
        self.assertEqual(prov.full_sha(SHA256), SHA256)
        self.assertEqual(prov.subject_of_commit(SHA1), "commit:" + SHA1)
        self.assertEqual(prov.subject_of_milestone(42), "milestone:42")
        self.assertEqual(prov.subject_of_milestone("42"), "milestone:42")
        for bad in ("", "abc", "a" * 39, "z" * 40, None):
            with self.assertRaises(prov.ProvenanceError):
                prov.full_sha(bad)
        for bad in (0, -3, "x", None):
            with self.assertRaises(prov.ProvenanceError):
                prov.subject_of_milestone(bad)

    def test_capture_refuses_outside_a_managed_turn(self):
        env = {"AGENT_MAIL_NAME": "mty-amh-impl-07", "AMEESH_HARNESS": "deepseek"}
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # pas de tour
        env["AMEESH_TURN_ID"] = "t" * 32
        self.assertEqual(prov.capture(env),
                         {"actor": "mty-amh-impl-07", "turn": "t" * 32,
                          "harness": "deepseek"})
        env["AMEESH_HARNESS"] = "autre"
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # famille inconnue
        del env["AMEESH_HARNESS"]
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # pas de harnais
        del env["AGENT_MAIL_NAME"]
        env["AMEESH_HARNESS"] = "claude"
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # pas d'acteur


class PgProvenanceTest(PgTestCase):
    """La lecture réelle : la preuve jointe au fait, ou `unknown`."""

    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).provenance
        self.lot = work.add(self.db, title="un lot à preuve", app="ameesh",
                            actor="orchestrateur")

    def _receipt(self, subject, sha, *, actor="mty-amh-impl-07", session="s-1",
                 turn="t" * 32, harness="deepseek", work_item_id=None):
        return self.ops.add(subject, sha, actor, session, turn, harness,
                            work_item_id=work_item_id)

    def test_commit_receipt_is_exact_or_unknown(self):
        self.assertIsNone(prov.receipt_for_commit(self.db, SHA1))
        self._receipt("commit:" + SHA1, SHA1, work_item_id=int(self.lot["id"]))
        recu = prov.receipt_for_commit(self.db, SHA1)
        self.assertEqual(recu["subject"], "commit:" + SHA1)
        self.assertEqual(recu["commit"], SHA1)
        self.assertEqual(recu["actor"], "mty-amh-impl-07")
        self.assertEqual(recu["session"], "s-1")
        self.assertEqual(recu["turn"], "t" * 32)
        self.assertEqual(recu["harness"], "deepseek")
        self.assertEqual(recu["record"], "provenance_receipts:commit:" + SHA1)
        self.assertIsNone(recu["milestone"])
        # une abréviation n'est pas un commit pour le fournisseur
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_commit(self.db, SHA1[:12])
        # un autre commit reste inconnu : pas de voisin, pas de repli
        self.assertIsNone(prov.receipt_for_commit(self.db, SHA1_FULL))

    def test_milestone_receipt_copies_the_declared_facts(self):
        mark = work.milestone(self.db, int(self.lot["id"]), "verdict", sha=SHA1[:12],
                              actor="mty-amh-impl-07", verdict="ok")
        self.assertIsNone(prov.receipt_for_milestone(self.db, mark["id"]))
        self._receipt("milestone:%d" % mark["id"], SHA1, work_item_id=int(self.lot["id"]))
        recu = prov.receipt_for_milestone(self.db, mark["id"])
        self.assertEqual(recu["subject"], "milestone:%d" % mark["id"])
        # la preuve porte le commit COMPLET que le sha déclaré résout
        self.assertEqual(recu["commit"], SHA1)
        # les faits recopiés sont ceux du jalon, à l'identique
        self.assertEqual(recu["milestone"], {
            "id": mark["id"], "actor": mark["actor"], "at_ts": mark["at_ts"],
            "sha": mark["sha"], "verdict": mark["verdict"]})
        # et ils coïncident avec la lecture des jalons du lot
        lu = [m for m in work.milestones(self.db, int(self.lot["id"]))
              if m["id"] == mark["id"]][0]
        self.assertEqual(recu["milestone"],
                         {key: lu[key] for key in ("id", "actor", "at_ts", "sha", "verdict")})
        self.assertEqual(recu["harness"], "deepseek")

    def test_milestone_proof_must_match_the_mark(self):
        mark = work.milestone(self.db, int(self.lot["id"]), "verdict", sha=SHA1[:12],
                              actor="mty-amh-impl-07", verdict="ok")
        # acteur de la preuve différent de l'acteur du jalon : refus
        self._receipt("milestone:%d" % mark["id"], SHA1, actor="autre-agent")
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_milestone(self.db, mark["id"])
        # une abréviation qui ne résout pas le commit enregistré : refus
        self.db.execute("DELETE FROM provenance_receipts WHERE subject = %s",
                        ("milestone:%d" % mark["id"],))
        self._receipt("milestone:%d" % mark["id"], SHA1_FULL)
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_milestone(self.db, mark["id"])

    def test_milestone_proof_without_mark_is_unknown(self):
        self._receipt("milestone:999999", SHA1)
        self.assertIsNone(prov.receipt_for_milestone(self.db, 999999))

    def test_a_receipt_is_append_only(self):
        first = self._receipt("commit:" + SHA1, SHA1)
        self.assertTrue(first["recorded"])
        again = self._receipt("commit:" + SHA1, SHA1_FULL, actor="quelqu-un")
        self.assertFalse(again["recorded"])
        self.assertEqual(again["actor"], "mty-amh-impl-07")
        self.assertEqual(again["sha"], SHA1)
        recu = prov.receipt_for_commit(self.db, SHA1)
        self.assertEqual(recu["commit"], SHA1)


if __name__ == "__main__":
    unittest.main()
