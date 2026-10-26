# SPDX-License-Identifier: AGPL-3.0-only
"""#3589 — le gel d'un lot empilé refuse les copies de la tête gelée du parent.

Constat du 25/10 (chaîne H6–H8, #3506) : après la fusion d'un parent, les
agents rebasaient la chaîne par `rebase --onto main` depuis l'ancien parent.
Chaque lot embarquait alors des copies, à SHA différents, des commits du
parent ; la porte des membres (#3513) ne reconnaissait que la tête gelée
exacte et comptait ces copies comme des commits tiers, d'où des désignations
impossibles et une heure de ré-empilements à la main.

Le garde lit la dépendance ouverte (`depends` ou paquet), compare les
patch-ids de la plage gelée à ceux de la tête gelée du parent, et refuse avec
la commande exacte `git rebase --onto <tête du parent> <dernière copie>
<branche>` quand la dernière copie reproduit l'arbre du parent (rebase à
arbre identique). Un lot sans parent, une tête empilée correctement ou un
bloc copié d'une tête plus ancienne ne changent rien.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import freeze_stack, storage, work, work_dependencies

from .support import PgTestCase

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=GIT_ENV)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class Repo:
    """Un dépôt minimal : commits nommés, branches, cherry-pick."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.name", "t")
        git(root, "config", "user.email", "t@example.invalid")

    def write(self, name):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")

    def commit(self, name):
        self.write(name)
        git(self.root, "add", name)
        git(self.root, "commit", "-qm", name)
        return git(self.root, "rev-parse", "HEAD")

    def branch(self, name, start):
        git(self.root, "checkout", "-q", "-b", name, start)

    def checkout(self, ref):
        git(self.root, "checkout", "-q", ref)

    def cherry_pick(self, *commits):
        """Des copies : même diff, SHA différent (message de commit distinct)."""
        for commit in commits:
            git(self.root, "cherry-pick", "--no-commit", commit)
            git(self.root, "commit", "-qm", "copy of " + commit[:12])

    def tree(self, rev):
        return git(self.root, "rev-parse", rev + "^{tree}")


class FindTest(unittest.TestCase):
    """La lecture git seule : copie refusée, pile correcte acceptée."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3589-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = Repo(os.path.join(self.tmp, "repo"))
        self.base = self.repo.commit("base")
        self.repo.branch("parent", self.base)
        self.p1 = self.repo.commit("parent-1")
        self.p2 = self.repo.commit("parent-2")

    def copies_child(self):
        """Une branche enfant qui recopie p1 et p2 puis ajoute son commit."""
        self.repo.checkout("main")
        self.repo.branch("child", self.base)
        self.repo.cherry_pick(self.p1, self.p2)
        copies = [git(self.repo.root, "rev-parse", "HEAD~1"),
                  git(self.repo.root, "rev-parse", "HEAD")]
        own = self.repo.commit("child-own")
        return copies, own

    def find(self, head, parents, branch="lot/child"):
        return freeze_stack.find(self.repo.root, head, target="main",
                                 parents=parents, branch=branch)

    def test_les_copies_sont_refusees_avec_la_recette(self):
        copies, own = self.copies_child()
        result = self.find(own, [{"lot": 1606, "frozen": self.p2,
                                  "source": "depends"}])
        self.assertIsNotNone(result)
        self.assertEqual(result["frozen"], self.p2)
        self.assertEqual(result["last_copy"], copies[1])
        self.assertEqual(result["copies"], copies)
        text = freeze_stack.message(result)
        self.assertIn("freeze refused (stacked-copies)", text)
        self.assertIn("git rebase --onto %s %s lot/child" % (self.p2, copies[1]),
                      text)
        self.assertIn("tree identical", text)
        # La recette est réelle : le rebase laisse l'arbre identique.
        before = self.repo.tree(own)
        self.repo.checkout("child")
        git(self.repo.root, "rebase", "--onto", self.p2, copies[1], "child")
        self.assertEqual(self.repo.tree("child"), before)

    def test_une_pile_sur_la_tete_gelee_est_acceptee(self):
        self.repo.checkout(self.p2)
        self.repo.branch("stacked", self.p2)
        own = self.repo.commit("stacked-own")
        self.assertIsNone(self.find(own, [
            {"lot": 1606, "frozen": self.p2, "source": "depends"}]))

    def test_un_lot_sans_parent_ne_change_pas(self):
        self.repo.checkout(self.base)
        self.repo.branch("alone", self.base)
        own = self.repo.commit("alone-own")
        self.assertIsNone(self.find(own, []))

    def test_un_bloc_copie_d_une_tete_plus_ancienne_ne_refuse_pas(self):
        # La copie ne reproduit pas l'arbre de la tête gelée : ce n'est pas
        # une copie de CE parent, la recette ne serait pas à arbre identique.
        self.repo.checkout("main")
        self.repo.branch("old-copy", self.base)
        self.repo.cherry_pick(self.p1)
        own = self.repo.commit("old-copy-own")
        self.assertIsNone(self.find(own, [
            {"lot": 1606, "frozen": self.p2, "source": "depends"}]))

    def test_une_fusion_apres_les_copies_ne_promet_pas_l_arbre_identique(self):
        copies, _own = self.copies_child()
        self.repo.branch("side", "child")
        self.repo.checkout("side")
        self.repo.commit("side")
        self.repo.checkout("child")
        git(self.repo.root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        head = self.repo.commit("after-merge")
        self.assertIsNone(self.find(head, [
            {"lot": 1606, "frozen": self.p2, "source": "depends"}]))

    def test_le_parent_le_plus_proche_gagne(self):
        # p2 contient p1 : le parent p1 est écarté, seule la tête p2 est la
        # base immédiate de l'empilement.
        self.repo.checkout("main")
        self.repo.branch("nearest", self.base)
        self.repo.cherry_pick(self.p1, self.p2)
        own = self.repo.commit("nearest-own")
        result = self.find(own, [
            {"lot": 1, "frozen": self.p1, "source": "depends"},
            {"lot": 2, "frozen": self.p2, "source": "depends"},
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["parent"], 2)


class GateTest(PgTestCase):
    """Le garde branché dans la porte du gel : refus, et aucune écriture."""

    def setUp(self):
        super().setUp()
        self.repo = Repo(os.path.join(self.tmp, "repo3589"))
        self.base = self.repo.commit("base")
        self.parent = work.add(self.db, title="parent", app="ameesh",
                               actor="mesh")["id"]
        work.set_branch(self.db, self.parent, "lot/parent", "main", actor="mesh")
        self.repo.branch("lot/parent", self.base)
        self.p1 = self.repo.commit("parent-1")
        self.p2 = self.repo.commit("parent-2")
        storage.of(self.db).work.add_milestone(
            self.parent, "frozen", sha=self.p2, actor="auteur", verdict=None,
            note="")

    def child(self, branch, *, from_ref):
        lot = work.add(self.db, title="child", app="ameesh", actor="mesh")["id"]
        work.set_branch(self.db, lot, branch, "main", actor="mesh")
        work.move(self.db, lot, "build", actor="mesh")
        self.repo.checkout(from_ref)
        self.repo.branch(branch, from_ref)
        return lot

    def copies_child(self, branch="lot/child"):
        lot = self.child(branch, from_ref="main")
        self.repo.cherry_pick(self.p1, self.p2)
        own = self.repo.commit("child-own")
        return lot, own

    def freezes(self, lot):
        return [m for m in work.milestones(self.db, lot) if m["kind"] == "frozen"]

    def test_le_gel_est_refuse_et_rien_n_est_ecrit(self):
        lot, own = self.copies_child()
        work_dependencies.add(self.db, lot, self.parent, actor="mesh")
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(self.db, lot, "frozen", sha=own,
                           repo=self.repo.root, actor="auteur")
        text = str(caught.exception)
        self.assertIn("freeze refused (stacked-copies)", text)
        self.assertIn("git rebase --onto %s" % self.p2, text)
        self.assertIn("lot/child", text)
        self.assertEqual(self.freezes(lot), [])

    def test_une_pile_correcte_gele(self):
        lot = self.child("lot/stacked", from_ref=self.p2)
        own = self.repo.commit("stacked-own")
        work_dependencies.add(self.db, lot, self.parent, actor="mesh")
        row = work.milestone(self.db, lot, "frozen", sha=own,
                             repo=self.repo.root, actor="auteur")
        self.assertEqual(row["sha"], own)
        self.assertEqual([m["sha"] for m in self.freezes(lot)], [own])

    def test_un_lot_sans_parent_gele_comme_avant(self):
        lot = self.child("lot/alone", from_ref=self.base)
        own = self.repo.commit("alone-own")
        row = work.milestone(self.db, lot, "frozen", sha=own,
                             repo=self.repo.root, actor="auteur")
        self.assertEqual(row["sha"], own)

    def test_la_cli_rend_le_refus_et_la_recette(self):
        lot, own = self.copies_child()
        work_dependencies.add(self.db, lot, self.parent, actor="mesh")
        proc = self.mesh("work", "milestone", str(lot), "frozen",
                         "--sha", own, "--repo", self.repo.root,
                         "--actor", "auteur",
                         env=self.env(AGENT_MAIL_NAME="auteur"))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("freeze refused (stacked-copies)", proc.stderr)
        self.assertIn("git rebase --onto %s" % self.p2, proc.stderr)
        self.assertEqual(self.freezes(lot), [])

    def test_un_membre_du_meme_paquet_est_un_parent(self):
        lot, own = self.copies_child()
        package = work.add(self.db, title="paquet", app="ameesh",
                           branch="integration/paquet", branch_target="main",
                           actor="mesh")["id"]
        storage.of(self.db).work.record_packet(
            package, branch="integration/paquet", base_sha=self.base,
            head_sha=self.p2,
            units=[{"work_item_id": self.parent, "unit_sha": self.p2,
                    "frozen_sha": self.p2},
                   {"work_item_id": lot, "unit_sha": own, "frozen_sha": own}],
            note="packet composition recorded", actor="mesh")
        self.assertEqual(
            freeze_stack.candidates(self.db, lot),
            [{"lot": self.parent, "source": "packet"}])
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(self.db, lot, "frozen", sha=own,
                           repo=self.repo.root, actor="auteur")
        self.assertIn("stacked-copies", str(caught.exception))
        self.assertIn("#%d" % self.parent, str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
