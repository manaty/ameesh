# SPDX-License-Identifier: AGPL-3.0-only
"""#1756 : gel effectif dérivé du dernier avis favorable (`plan_git.freeze_candidate`).
#1905 : report de l'avis sur un gel republié quand le delta du lot est
mécaniquement constant (`plan_git.rebase_identity`)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import plan_git

A = "a" * 40
B = "b" * 40

#: config git neutralisée : le dépôt jetable ne dépend pas du poste.
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(depot: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", depot, *args], capture_output=True, text=True,
                          env=GIT_ENV)
    if check and proc.returncode:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr or proc.stdout))
    return proc



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


class RebaseIdentityTest(unittest.TestCase):
    """#1905 : la tête relue et le gel republié portent-ils le même delta ?"""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-1905-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.depot = os.path.join(self.base, "depot")
        os.makedirs(self.depot)
        _git(self.depot, "init", "-q", "-b", "main")
        _git(self.depot, "config", "user.name", "t")
        _git(self.depot, "config", "user.email", "t@t")
        self._ecrire("a.txt", "base\n")
        _git(self.depot, "add", "a.txt")
        _git(self.depot, "commit", "-qm", "base")
        _git(self.depot, "checkout", "-qb", "lot")
        self._ecrire("a.txt", "lot 1\n")
        _git(self.depot, "commit", "-qam", "#1905 lot 1")
        self._ecrire("b.txt", "lot 2\n")
        _git(self.depot, "add", "b.txt")
        _git(self.depot, "commit", "-qm", "#1905 lot 2")
        self.reviewed = self._rev("HEAD")
        _git(self.depot, "checkout", "-q", "main")
        self._ecrire("c.txt", "main\n")
        _git(self.depot, "add", "c.txt")
        _git(self.depot, "commit", "-qm", "main avance")
        _git(self.depot, "checkout", "-q", "lot")
        _git(self.depot, "rebase", "-q", "main")
        self.frozen = self._rev("HEAD")

    def _ecrire(self, nom: str, contenu: str) -> None:
        with open(os.path.join(self.depot, nom), "w") as fh:
            fh.write(contenu)

    def _rev(self, rev: str) -> str:
        return _git(self.depot, "rev-parse", rev).stdout.strip()

    def test_rebase_mecanique_est_reconnu(self) -> None:
        self.assertNotEqual(self.reviewed, self.frozen)
        out = plan_git.rebase_identity(self.depot, self.reviewed, self.frozen, "main")
        self.assertTrue(out["mechanical"], out)

    def test_union_de_main_est_mecanique(self) -> None:
        # gel republié par une fusion à deux parents (#508) : la base du lot
        # est celle de la nouvelle main, les commits du lot sont inchangés.
        _git(self.depot, "checkout", "-q", "-b", "union", self.reviewed)
        _git(self.depot, "merge", "-q", "--no-ff", "-m", "union", "main")
        union = self._rev("HEAD")
        out = plan_git.rebase_identity(self.depot, self.reviewed, union, "main")
        self.assertTrue(out["mechanical"], out)

    def test_contenu_change_n_est_pas_mecanique(self) -> None:
        self._ecrire("a.txt", "lot 1 corrige\n")
        _git(self.depot, "commit", "-qam", "#1905 correction")
        corrige = self._rev("HEAD")
        out = plan_git.rebase_identity(self.depot, self.reviewed, corrige, "main")
        self.assertFalse(out["mechanical"], out)
        rows = [verdict(self.reviewed, 10), frozen(corrige, 20)]
        refus = plan_git.freeze_candidate(rows, repo=self.depot, target="main")
        self.assertEqual(refus["sha"], "")
        self.assertIn("freeze-after-verdict", refus["causes"])

    def test_meme_commit_et_shas_illisibles(self) -> None:
        self.assertTrue(plan_git.rebase_identity(
            self.depot, self.reviewed, self.reviewed, "main")["mechanical"])
        self.assertFalse(plan_git.rebase_identity(
            self.depot, self.reviewed, "0" * 40, "main")["mechanical"])

    def test_avis_porte_sur_le_gel_mecanique(self) -> None:
        rows = [verdict(self.reviewed, 10), frozen(self.frozen, 20)]
        out = plan_git.freeze_candidate(rows, repo=self.depot, target="main")
        self.assertEqual(out["sha"], self.frozen)
        self.assertEqual(out["origin"], "frozen")
        self.assertEqual(out["causes"], [])
        self.assertTrue(any(n.startswith("mechanical-rebase") for n in out["notes"]), out)

    def test_sans_repo_le_refus_demeure(self) -> None:
        rows = [verdict(self.reviewed, 10), frozen(self.frozen, 20)]
        out = plan_git.freeze_candidate(rows)
        self.assertEqual(out["sha"], "")
        self.assertIn("freeze-after-verdict", out["causes"])

    def test_blocked_sur_la_tete_relue_n_est_pas_masque(self) -> None:
        rows = [verdict(self.reviewed, 10, id=2, actor="cx1", kind="blocked"),
                verdict(self.reviewed, 11, id=3, actor="dev4"),
                frozen(self.frozen, 20, id=4)]
        out = plan_git.freeze_candidate(rows, repo=self.depot, target="main")
        self.assertEqual(out["sha"], "")
        self.assertIn("blocked-verdict", out["causes"])


if __name__ == "__main__":
    unittest.main()
