# SPDX-License-Identifier: AGPL-3.0-only
"""#1756 : gel effectif dérivé du dernier avis favorable (`plan_git.freeze_candidate`)."""
from __future__ import annotations

import unittest

from ameesh import plan_git

A = "a" * 40
B = "b" * 40


def frozen(sha, at, id=1):
    return {"kind": "frozen", "sha": sha, "at_ts": at, "id": id,
            "actor": "", "verdict": ""}


def verdict(sha, at, id=2, actor="cx1", kind="ok"):
    return {"kind": "verdict", "sha": sha, "at_ts": at, "id": id,
            "actor": actor, "verdict": kind}


class FreezeCandidateTest(unittest.TestCase):
    def test_commit_id_et_meme_commit(self):
        self.assertEqual(plan_git.commit_id(" 86EBEBEDb8 "), "86ebebedb8")
        self.assertEqual(plan_git.commit_id(""), "")
        self.assertEqual(plan_git.commit_id("xyz"), "")
        self.assertTrue(plan_git.same_commit(A, A[:12]))
        self.assertTrue(plan_git.same_commit(A[:12], A))
        self.assertFalse(plan_git.same_commit(A, B))
        self.assertFalse(plan_git.same_commit(A, ""))

    def test_gel_declare_et_avis_du_meme_commit(self):
        out = plan_git.freeze_candidate([frozen(A[:12], 10), verdict(A, 11)])
        self.assertEqual(out["sha"], A)              # la forme la plus longue
        self.assertEqual(out["origin"], "frozen")
        self.assertEqual(out["causes"], [])
        self.assertEqual(out["notes"], [])
        self.assertEqual(out["declared"]["sha"], A[:12])
        self.assertEqual(out["verdict"]["actor"], "cx1")

    def test_avis_sans_gel_declare(self):
        out = plan_git.freeze_candidate([verdict(A, 11)])
        self.assertEqual(out["sha"], A)
        self.assertEqual(out["origin"], "verdict")
        self.assertIsNone(out["declared"])

    def test_gel_perime_et_avis_plus_recent(self):
        out = plan_git.freeze_candidate([frozen(A, 10), verdict(B, 11)])
        self.assertEqual(out["sha"], B)
        self.assertEqual(out["origin"], "verdict")
        self.assertIn("declared-freeze-stale", out["notes"])

    def test_gel_posterieur_a_l_avis_refuse(self):
        # #1283 : le gel suit l'avis, un nouvel avis est réellement dû
        out = plan_git.freeze_candidate([verdict(A, 10), frozen(B, 20)])
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["freeze-after-verdict"])

    def test_gel_sans_avis_reste_candidat(self):
        # classe légère : le gel déclaré suffit ; la note dit l'avis manquant
        out = plan_git.freeze_candidate([frozen(A, 10)])
        self.assertEqual(out["sha"], A)
        self.assertEqual(out["origin"], "frozen")
        self.assertIn("no-verdict", out["notes"])

    def test_ni_gel_ni_avis_refuse(self):
        out = plan_git.freeze_candidate(
            [{"kind": "requested", "sha": "", "at_ts": 1, "id": 1}])
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["no-verdict"])

    def test_dernier_avis_bloque_refuse(self):
        out = plan_git.freeze_candidate([frozen(A, 10), verdict(A, 11, kind="blocked")])
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["blocked-verdict"])

    def test_blocage_jamais_masque_par_un_autre_ok(self):
        out = plan_git.freeze_candidate([
            frozen(A, 10),
            verdict(A, 11, id=2, actor="cx1", kind="blocked"),
            verdict(A, 12, id=3, actor="dev1", kind="ok")])
        self.assertEqual(out["causes"], ["blocked-verdict"])
        self.assertIn("blocked by cx1", out["notes"])

    def test_dernier_avis_du_relecteur_compte(self):
        out = plan_git.freeze_candidate([
            frozen(A, 10),
            verdict(A, 11, id=2, actor="cx1", kind="blocked"),
            verdict(A, 12, id=3, actor="cx1", kind="ok")])
        self.assertEqual(out["sha"], A)
        self.assertEqual(out["causes"], [])

    def test_blocage_ancien_supersede_par_un_regel(self):
        out = plan_git.freeze_candidate([
            frozen(A, 10, id=1),
            verdict(A, 11, id=2, kind="blocked"),
            frozen(B, 12, id=3),
            verdict(B, 13, id=4, kind="ok")])
        self.assertEqual(out["sha"], B)
        self.assertEqual(out["origin"], "frozen")
        self.assertEqual(out["causes"], [])

    def test_avis_sans_commit_garde_le_gel(self):
        out = plan_git.freeze_candidate([frozen(A, 10), verdict("", 11)])
        self.assertEqual(out["sha"], A)
        self.assertEqual(out["origin"], "frozen")
        self.assertIn("verdict-names-no-commit", out["notes"])

    def test_gel_inexistant_repare_par_l_avis(self):
        # #1374 avant réparation : gel 86ebebede (inexistant) contre tête
        # 86ebebedb8… relue ; l'avis couvre la tête publiée, le gel est périmé
        out = plan_git.freeze_candidate([
            frozen("86ebebede", 10),
            verdict("86ebebedb87f9e0634fd7d2b3439edbd261a0a60", 11)])
        self.assertEqual(out["sha"], "86ebebedb87f9e0634fd7d2b3439edbd261a0a60")
        self.assertEqual(out["origin"], "verdict")
        self.assertIn("declared-freeze-stale", out["notes"])

    def test_avis_tardif_sur_l_ancienne_tete_ne_declasse_pas_le_gel(self):
        # #1767 : gel A (ancienne tête), regel B, ok de cx1 sur B, puis ok
        # tardif de dev1 sur A — A était nommé avant le gel courant
        out = plan_git.freeze_candidate([
            frozen(A, 5, id=1),
            frozen(B, 10, id=2),
            verdict(B, 11, id=3, actor="cx1"),
            verdict(A, 12, id=4, actor="dev1")])
        self.assertEqual(out["sha"], B)
        self.assertEqual(out["origin"], "frozen")
        self.assertEqual(out["causes"], [])
        self.assertEqual(out["verdict"]["id"], 3)
        self.assertIn("late-verdict-on-older-commit", out["notes"])
        self.assertNotIn("declared-freeze-stale", out["notes"])

    def test_ancienne_tete_nommee_par_un_avis_avant_le_gel(self):
        # l'ancienne tête n'a jamais été gelée, mais un avis la nommait
        out = plan_git.freeze_candidate([
            verdict(A, 5, id=1, actor="dev1"),
            frozen(B, 10, id=2),
            verdict(B, 11, id=3, actor="cx1"),
            verdict(A, 12, id=4, actor="dev1")])
        self.assertEqual(out["sha"], B)
        self.assertIn("late-verdict-on-older-commit", out["notes"])

    def test_avis_tardif_seul_un_avis_reste_du_sur_le_gel(self):
        # aucun avis sur B : l'ok tardif sur A ne rend pas A candidat, le
        # dernier avis pertinent précède le gel — un nouvel avis est dû
        out = plan_git.freeze_candidate([
            frozen(A, 5, id=1),
            verdict(A, 6, id=2),
            frozen(B, 10, id=3),
            verdict(A, 12, id=4, actor="dev1")])
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["freeze-after-verdict"])

    def test_commit_jamais_nomme_avant_le_gel_reste_un_rebase(self):
        # sans trace antérieure, l'ancêtreté est inconnue : lecture inchangée
        out = plan_git.freeze_candidate([
            frozen(B, 10, id=1),
            verdict(B, 11, id=2, actor="cx1"),
            verdict(A, 12, id=3, actor="dev1")])
        self.assertEqual(out["sha"], A)
        self.assertEqual(out["origin"], "verdict")
        self.assertIn("declared-freeze-stale", out["notes"])
        self.assertNotIn("late-verdict-on-older-commit", out["notes"])

    def test_blocage_tardif_sur_l_ancienne_tete_jamais_ecarte(self):
        out = plan_git.freeze_candidate([
            frozen(A, 5, id=1),
            frozen(B, 10, id=2),
            verdict(B, 11, id=3, actor="cx1"),
            verdict(A, 12, id=4, actor="dev1", kind="blocked")])
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["blocked-verdict"])


if __name__ == "__main__":
    unittest.main()
