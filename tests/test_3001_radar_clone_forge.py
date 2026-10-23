# SPDX-License-Identifier: AGPL-3.0-only
"""#3001 — le clone radar suit la forge, pas les branches locales du checkout.

`git clone --shared <checkout>` donnait au clone radar le checkout pour
`origin` : ses `refs/remotes/origin/*` étaient les branches locales du
checkout (nexlink : `origin/develop` figé au 11/10, ameesh : `main` divergent).
Témoins : checkout en retard, checkout divergent, clone existant à l'ancienne
URL migré sans être recloné, forge illisible (doute, jamais la branche locale),
cible plus ancienne que la forge refusée.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import plan_git, resolutions, review_designation_activation


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          check=True)
    return proc.stdout.strip()


def _commit(repo: str, path: str, text: str, message: str) -> str:
    with open(os.path.join(repo, path), "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


class CloneFollowsForgeTest(unittest.TestCase):
    """Une forge (dépôt nu), le checkout des agents, un pousseur qui avance la
    forge sans que le checkout ne récupère rien."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3001-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.forge = os.path.join(self.tmp, "forge.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.forge)
        self.repo = os.path.join(self.tmp, "checkout")
        _git(self.tmp, "clone", "-q", self.forge, self.repo)
        for repo in (self.repo,):
            _git(repo, "config", "user.name", "banc")
            _git(repo, "config", "user.email", "banc@example.invalid")
        self.base = _commit(self.repo, "a.txt", "one\n", "base")
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "push", "-q", "origin", "main:develop")
        self.pusher = os.path.join(self.tmp, "pusher")
        _git(self.tmp, "clone", "-q", self.forge, self.pusher)
        _git(self.pusher, "config", "user.name", "autre")
        _git(self.pusher, "config", "user.email", "autre@example.invalid")
        _git(self.pusher, "checkout", "-q", "develop")
        self.published = _commit(self.pusher, "b.txt", "forge\n", "published")
        _git(self.pusher, "push", "-q", "origin", "develop")
        self.clone = os.path.join(self.tmp, "radar", "nexlink.clone")

    def _replayer(self, target: str = "develop") -> resolutions.Replayer:
        return resolutions.Replayer(self.repo, self.clone, target=target)

    def _tracked(self, branch: str) -> str | None:
        return plan_git._commit(self.clone, "refs/remotes/origin/" + branch)

    def test_lagging_checkout_reads_the_forge_tip(self) -> None:
        # le checkout a une branche locale develop figée sur la base
        _git(self.repo, "branch", "develop", self.base)
        replayer = self._replayer()
        replayer.ensure()
        self.assertEqual(_git(self.clone, "remote", "get-url", "origin"), self.forge)
        self.assertEqual(_git(self.clone, "config", "--get-all", "remote.origin.fetch"),
                         resolutions.FORGE_REFSPEC)
        self.assertEqual(self._tracked("develop"), self.published)
        self.assertEqual(plan_git.default_target(self.clone), "origin/develop")
        self.assertEqual(replayer.target_doubt, "")
        # les objets restent lus du checkout (`--shared`)
        with open(os.path.join(self.clone, ".git", "objects", "info", "alternates"),
                  encoding="utf-8") as fh:
            self.assertIn(os.path.join(self.repo, ".git", "objects"), fh.read())

    def test_divergent_checkout_is_not_read(self) -> None:
        _git(self.repo, "checkout", "-q", "-b", "develop", self.base)
        local = _commit(self.repo, "c.txt", "local\n", "divergent, never pushed")
        replayer = self._replayer()
        replayer.ensure()
        self.assertEqual(self._tracked("develop"), self.published)
        self.assertNotEqual(self._tracked("develop"), local)
        self.assertIsNone(plan_git._commit(self.clone, "refs/heads/develop"))
        # la forge avance encore : le passage suivant la suit
        newer = _commit(self.pusher, "b.txt", "forge 2\n", "published again")
        _git(self.pusher, "push", "-q", "origin", "develop")
        replayer.ensure()
        self.assertEqual(self._tracked("develop"), newer)

    def test_existing_clone_is_migrated_without_recloning(self) -> None:
        _git(self.repo, "branch", "develop", self.base)
        # un clone de l'ancienne façon : origin = le checkout
        os.makedirs(os.path.dirname(self.clone))
        _git(os.path.dirname(self.clone), "clone", "-q", "--shared", "--no-checkout",
             self.repo, self.clone)
        _git(self.clone, "checkout", "-q", "-f", "--detach", self.base)
        _git(self.clone, "config", "ameesh.target", "origin/develop")
        self.assertEqual(self._tracked("develop"), self.base)
        marker = os.path.join(self.clone, ".git", "ameesh-rr-v4")
        os.makedirs(marker)
        replayer = self._replayer()
        replayer.ensure()
        self.assertTrue(os.path.isdir(marker), "the clone was recloned")
        self.assertEqual(_git(self.clone, "remote", "get-url", "origin"), self.forge)
        self.assertEqual(_git(self.clone, "config", "--get-all", "remote.origin.fetch"),
                         resolutions.FORGE_REFSPEC)
        self.assertEqual(self._tracked("develop"), self.published)
        self.assertEqual(plan_git.declared_target(self.clone), "origin/develop")

    def test_unreadable_forge_is_a_doubt_never_the_local_branch(self) -> None:
        _git(self.repo, "branch", "develop", self.base)
        _git(self.repo, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))
        replayer = self._replayer()
        replayer.ensure()
        self.assertIn("unreadable", replayer.target_doubt)
        self.assertIsNone(self._tracked("develop"))
        self.assertIsNone(plan_git._commit(self.clone, "refs/heads/develop"))
        self.assertIsNone(plan_git.declared_target(self.clone))
        self.assertNotEqual(plan_git.default_target(self.clone), "develop")
        # la forge revient : la migration aboutit au passage suivant
        _git(self.repo, "remote", "set-url", "origin", self.forge)
        replayer.ensure()
        self.assertEqual(replayer.target_doubt, "")
        self.assertEqual(self._tracked("develop"), self.published)

    def test_forge_lost_after_migration_drops_the_target(self) -> None:
        replayer = self._replayer()
        replayer.ensure()
        self.assertEqual(self._tracked("develop"), self.published)
        os.rename(self.forge, self.forge + ".moved")
        replayer.ensure()
        self.assertIn("unreadable", replayer.target_doubt)
        self.assertIsNone(self._tracked("develop"))
        with self.assertRaises(plan_git.MergeProbeError):
            plan_git.default_target(self.clone)

    def test_older_target_is_refused_not_read(self) -> None:
        replayer = self._replayer()
        replayer.ensure()
        newer = _commit(self.pusher, "b.txt", "forge 3\n", "not fetchable")
        _git(self.pusher, "push", "-q", "origin", "develop")
        real = resolutions._run

        def no_fetch(cwd, *args, **kwargs):
            if args and args[0] == "fetch":
                return subprocess.CompletedProcess(args, 1, b"", b"fetch refused")
            return real(cwd, *args, **kwargs)

        with mock.patch.object(resolutions, "_run", side_effect=no_fetch):
            with self.assertRaises(resolutions.StaleTarget) as caught:
                replayer.forge_tip("develop")
            self.assertIn(newer[:12], str(caught.exception))
            replayer.ensure()
        self.assertIn("older target is refused", replayer.target_doubt)
        self.assertIsNone(self._tracked("develop"))

    def test_merge_gate_range_reads_the_forge_target(self) -> None:
        """Témoin réel du 23/10 (#3025) : la porte (`lot_class` base `develop`,
        puis `review_designation_activation._resolve_base`) lisait
        `origin/develop` du clone radar, copie figée du checkout : la plage
        gelée portait 3201 commits au lieu d'un."""
        _git(self.repo, "branch", "develop", self.base)     # local figé du checkout
        os.makedirs(os.path.dirname(self.clone))
        _git(os.path.dirname(self.clone), "clone", "-q", "--shared", "--no-checkout",
             self.repo, self.clone)
        _git(self.clone, "checkout", "-q", "-f", "--detach", self.base)
        for n in range(5):                                 # d'autres auteurs
            _commit(self.pusher, "b.txt", "other %d\n" % n, "other %d" % n)
        _git(self.pusher, "checkout", "-q", "-b", "lot/9-x")
        head = _commit(self.pusher, "c.txt", "lot\n", "the lot's own commit")
        _git(self.pusher, "push", "-q", "origin", "develop", "lot/9-x")
        _git(self.clone, "fetch", "-q", self.forge, "lot/9-x")  # la tête gelée lue
        _start, _paths, before = review_designation_activation._range(
            self.clone, "develop", head)
        self.assertEqual(len(before), 7)                   # la copie du checkout
        self._replayer().ensure()
        self.assertEqual(review_designation_activation._resolve_base(self.clone, "develop"),
                         "origin/develop")
        _start, _paths, after = review_designation_activation._range(
            self.clone, "develop", head)
        self.assertEqual(after, [head])
        # un fetch ultérieur par le nom `origin` (passage radar, `_fetch_frozen`)
        # lit la forge : la branche locale du checkout ne revient pas
        _git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(self._tracked("develop"), _git(self.pusher, "rev-parse", "develop"))
        _start, _paths, again = review_designation_activation._range(
            self.clone, "develop", head)
        self.assertEqual(again, [head])

    def test_checkout_without_origin_keeps_its_branches(self) -> None:
        alone = os.path.join(self.tmp, "alone")
        _git(self.tmp, "init", "-q", "-b", "main", alone)
        _git(alone, "config", "user.name", "banc")
        _git(alone, "config", "user.email", "banc@example.invalid")
        head = _commit(alone, "a.txt", "x\n", "x")
        clone = os.path.join(self.tmp, "radar", "alone.clone")
        replayer = resolutions.Replayer(alone, clone)
        replayer.ensure()
        self.assertEqual(_git(clone, "remote", "get-url", "origin"), alone)
        self.assertEqual(plan_git._commit(clone, "refs/remotes/origin/main"), head)
        self.assertEqual(replayer.target_doubt, "")


if __name__ == "__main__":
    unittest.main()
