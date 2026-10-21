# SPDX-License-Identifier: AGPL-3.0-only
"""#2962 — un gel et un verdict ne croient plus un SHA complet sur parole.

Constat du 21/10 : `freeze_gate.reviewed_head` rendait tel quel tout texte de
40 hexadécimaux, et `freeze_head` ne refusait pas un commit complet qu'aucun
dépôt ne portait. Deux témoins réels : un verdict de l'orchestrateur sur un SHA
inexistant (#15106), et un gel de #2914 sur `66a06cf0`, jamais poussé, remplacé
par `2df95bee` après que la désignation eut été envoyée sur une tête que
personne ne pouvait lire.

Règles couvertes :

1. gel : un SHA complet se résout dans `--repo` (autoritaire) ou dans
   `_head_repos` ; sinon refus `head-unresolved`, même message qu'une
   abréviation. Exception tranchée : quand **aucun dépôt de l'auteur** n'est
   lisible (gel distant), le SHA complet est accepté avec la note
   `ameesh-freeze-unverified`, jamais tenu pour vérifié ;
2. verdict : un SHA complet se résout, ou nomme la tête gelée courante, sinon
   refus `head-unresolved` ;
3. gel résolu mais absent de `origin/<branche>` après rafraîchissement :
   accepté, avec l'avertissement `freeze-unpublished` dans la note et la sortie.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import registry, storage, work

from .support import PgTestCase

ENV_GIT = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
UNKNOWN = "e" * 40
OTHER = "d" * 40


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=ENV_GIT)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def repo_with_own_commit(root):
    """Un dépôt avec `main` et un commit propre de branche ; rend `(dépôt, sha)`."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    with open(os.path.join(root, "socle"), "w", encoding="utf-8") as fh:
        fh.write("socle\n")
    git(root, "add", "socle")
    git(root, "commit", "-qm", "socle")
    git(root, "checkout", "-q", "-b", "lot/2962-test")
    with open(os.path.join(root, "lot"), "w", encoding="utf-8") as fh:
        fh.write("lot\n")
    git(root, "add", "lot")
    git(root, "commit", "-qm", "lot")
    return root, git(root, "rev-parse", "HEAD")


def author_with_forge(root):
    """Une forge nue, un clone auteur, `main` publiée ; rend forge, auteur."""
    forge = os.path.join(root, "forge.git")
    author = os.path.join(root, "auteur")
    os.makedirs(forge)
    git(forge, "init", "-q", "--bare", "-b", "main")
    git(root, "clone", "-q", forge, author)
    with open(os.path.join(author, "socle"), "w", encoding="utf-8") as fh:
        fh.write("socle\n")
    git(author, "add", "socle")
    git(author, "commit", "-qm", "socle")
    git(author, "push", "-q", "origin", "HEAD:refs/heads/main")
    return forge, author


class FreezeUnresolvedTest(PgTestCase):
    """Le gel refuse une tête qu'aucun dépôt lisible ne porte."""

    def setUp(self):
        super().setUp()
        self.repo_root = tempfile.mkdtemp(prefix="ameesh-2962-")
        self.addCleanup(shutil.rmtree, self.repo_root, True)
        self.repo, self.head = repo_with_own_commit(
            os.path.join(self.repo_root, "depot"))
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.repo)
        self.author = work.add(self.db, title="un lot", assignee="author",
                               actor="auteur")["id"]
        for state in ("build",):
            work.move(self.db, self.author, state, actor="auteur")

    def freeze(self, sha, lot=None, **kw):
        return work.milestone(self.db, lot or self.author, "frozen", sha=sha,
                              actor="auteur", **kw)

    def freezes(self, lot=None):
        return [m for m in work.milestones(self.db, lot or self.author)
                if m["kind"] == "frozen"]

    def test_un_sha_complet_absent_d_un_depot_lisible_est_refuse(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze(UNKNOWN)
        self.assertIn("head-unresolved", str(caught.exception))
        self.assertIn(UNKNOWN, str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_un_sha_complet_absent_avec_repo_nomme_est_refuse(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze(UNKNOWN, repo=self.repo)
        self.assertIn("head-unresolved", str(caught.exception))
        self.assertIn(self.repo, str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_un_sha_complet_present_est_ecrit_inchange(self):
        row = self.freeze(self.head, repo=self.repo)
        self.assertEqual(row["sha"], self.head)
        self.assertEqual([m["sha"] for m in self.freezes()], [self.head])

    def test_le_gel_distant_sans_depot_d_auteur_est_accepte_avec_sa_note(self):
        # Exception #2962 : ni dépôt de projet ni dossier d'assigné lisible.
        # La tête complète est écrite, mais la note dit qu'elle n'est pas
        # vérifiée — on ne devine pas un commit que l'hôte ne peut pas lire.
        lot = work.add(self.db, title="gel distant", actor="auteur")["id"]
        row = work.milestone(self.db, lot, "frozen", sha=UNKNOWN, actor="auteur")
        self.assertEqual(row["sha"], UNKNOWN)
        self.assertIn(work.FREEZE_UNVERIFIED_MARK, row["note"])
        self.assertIn("written unverified", row["note"])

    def test_la_cli_refuse_le_sha_complet_absent(self):
        # #2928 : une session sans identité liée ne nomme qu'un humain.
        proc = self.mesh("work", "milestone", str(self.author), "frozen",
                         "--sha", UNKNOWN, "--actor", "human:t")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-unresolved", proc.stderr)
        self.assertEqual(self.freezes(), [])


class FreezeUnpublishedTest(PgTestCase):
    """Geler puis pousser reste permis ; l'absence de publication est dite."""

    def setUp(self):
        super().setUp()
        self.tmp_repo = tempfile.mkdtemp(prefix="ameesh-2962-")
        self.addCleanup(shutil.rmtree, self.tmp_repo, True)
        self.forge, self.author = author_with_forge(self.tmp_repo)
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.author)
        self.lot = work.add(self.db, title="gel local", assignee="author",
                            actor="auteur")["id"]
        work.move(self.db, self.lot, "build", actor="auteur")
        work.set_branch(self.db, self.lot, "lot/2962-local", "main", actor="auteur")
        git(self.author, "checkout", "-q", "-b", "lot/2962-local")
        with open(os.path.join(self.author, "propre"), "w", encoding="utf-8") as fh:
            fh.write("propre\n")
        git(self.author, "add", "propre")
        git(self.author, "commit", "-qm", "propre")
        self.head = git(self.author, "rev-parse", "HEAD")

    def freeze(self, lot=None):
        return work.milestone(self.db, lot or self.lot, "frozen", sha=self.head,
                              actor="auteur")

    def test_un_gel_local_non_pousse_est_accepte_avec_avertissement(self):
        row = self.freeze()
        self.assertEqual(row["sha"], self.head)
        self.assertNotIn("freeze refused", row["note"])
        self.assertIn(work.FREEZE_UNPUBLISHED_MARK, row["note"])
        self.assertIn("origin/lot/2962-local", row["note"])

    def test_la_sortie_de_la_cli_dit_freeze_unpublished(self):
        # #2928 : une session sans identité liée ne nomme qu'un humain.
        proc = self.mesh("work", "milestone", str(self.lot), "frozen",
                         "--sha", self.head, "--actor", "human:t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning: " + work.FREEZE_UNPUBLISHED_MARK, proc.stderr)
        self.assertIn("origin/lot/2962-local", proc.stderr)

    def test_une_branche_poussee_ne_produit_plus_l_avertissement(self):
        git(self.author, "push", "-q", "origin", "lot/2962-local")
        row = self.freeze()
        self.assertNotIn(work.FREEZE_UNPUBLISHED_MARK, row["note"])


class VerdictUnresolvedTest(PgTestCase):
    """Un verdict complet se résout, ou nomme la tête gelée courante."""

    def setUp(self):
        super().setUp()
        self.tmp_repo = tempfile.mkdtemp(prefix="ameesh-2962-")
        self.addCleanup(shutil.rmtree, self.tmp_repo, True)
        self.repo, self.head = repo_with_own_commit(
            os.path.join(self.tmp_repo, "depot"))
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.repo)
        self.lot = work.add(self.db, title="un lot", assignee="author",
                            actor="auteur")["id"]
        work.move(self.db, self.lot, "build", actor="auteur")

    def verdicts(self, lot=None):
        return [m for m in work.milestones(self.db, lot or self.lot)
                if m["kind"] == "verdict"]

    def test_un_sha_complet_absent_est_refuse_au_verdict(self):
        work.milestone(self.db, self.lot, "frozen", sha=self.head, actor="auteur",
                       repo=self.repo)
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(self.db, self.lot, "verdict", verdict="ok", sha=UNKNOWN,
                           actor="relecteur", repo=self.repo)
        self.assertIn("head-unresolved", str(caught.exception))
        self.assertIn(UNKNOWN, str(caught.exception))
        self.assertEqual(self.verdicts(), [])

    def test_un_sha_complet_present_est_accepte_au_verdict(self):
        work.milestone(self.db, self.lot, "frozen", sha=self.head, actor="auteur",
                       repo=self.repo)
        row = work.milestone(self.db, self.lot, "verdict", verdict="ok",
                             sha=self.head, actor="relecteur", repo=self.repo)
        self.assertEqual(row["sha"], self.head)

    def test_le_verdict_sur_la_tete_gelee_absente_localement_est_accepte(self):
        # Témoin #15106 : la tête gelée courante fait foi même quand aucun
        # dépôt local ne la porte (elle a été gelée sur un autre hôte).
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=OTHER, actor="auteur", verdict=None, note="")
        row = work.milestone(self.db, self.lot, "verdict", verdict="ok",
                             sha=OTHER, actor="relecteur")
        self.assertEqual(row["sha"], OTHER)

    def test_la_tete_gelee_gagne_sur_un_repo_qui_ne_la_porte_pas(self):
        # `--repo` est autoritaire pour une abréviation, mais une tête
        # complète qui EST la tête gelée courante reste la tête relue.
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=OTHER, actor="auteur", verdict=None, note="")
        row = work.milestone(self.db, self.lot, "verdict", verdict="ok",
                             sha=OTHER, actor="relecteur", repo=self.repo)
        self.assertEqual(row["sha"], OTHER)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
