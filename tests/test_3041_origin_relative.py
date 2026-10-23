# SPDX-License-Identifier: AGPL-3.0-only
"""#3041 — une origin locale relative se lit depuis le checkout, partout.

Non bloquants de la relecture de #3001 : `merge_queue.fetch_fresh`,
`land._origin` et `packet_candidates.list_remises` rejouaient dans le clone
du radar l'URL brute du checkout ; `../forge.git` y visait un homonyme voisin
du clone. Et `_local_from` résolvait `..` depuis le lien d'un checkout
symlinké, quand Git le résout depuis sa cible. Témoins : un leurre à chaque
mauvais endroit, jamais lu.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest

from ameesh import land, merge_queue, packet_candidates, plan_git, resolutions


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


class RelativeOriginTest(unittest.TestCase):
    """La forge `tmp/forge.git`, le checkout `tmp/checkout` (origin
    `../forge.git`), le clone `tmp/radar/nexlink.clone` ; un leurre
    `tmp/radar/forge.git`, que `../forge.git` relu depuis le clone viserait."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-3041-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.forge = os.path.join(self.tmp, "forge.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.forge)
        self.repo = os.path.join(self.tmp, "checkout")
        _git(self.tmp, "clone", "-q", self.forge, self.repo)
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        base = _commit(self.repo, "a.txt", "one\n", "base")
        _git(self.repo, "push", "-q", "origin", "main", "main:develop",
             "%s:%s3041/develop-1" % (base, resolutions.REF_PREFIX))
        _git(self.repo, "remote", "set-url", "origin", "../forge.git")
        self.published = base
        self.clone = os.path.join(self.tmp, "radar", "nexlink.clone")
        self.decoy = self._decoy(os.path.join(self.tmp, "radar", "forge.git"))

    def _decoy(self, path: str) -> str:
        """Un homonyme de la forge à `path`, avancé : sa pointe n'est jamais lue."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _git(self.tmp, "clone", "-q", "--bare", self.forge, path)
        work = path + ".work"
        _git(self.tmp, "clone", "-q", "--branch", "develop", path, work)
        _git(work, "config", "user.name", "leurre")
        _git(work, "config", "user.email", "leurre@example.invalid")
        tip = _commit(work, "d.txt", "decoy\n", "decoy")
        _git(work, "push", "-q", "origin", "develop", "develop:main",
             "+HEAD:%s3041/develop-1" % resolutions.REF_PREFIX)
        return tip

    def _replayer(self, repo: str | None = None) -> resolutions.Replayer:
        replayer = resolutions.Replayer(repo or self.repo, self.clone, target="develop")
        replayer.ensure()
        return replayer

    def test_fetch_fresh_reads_the_forge(self) -> None:
        replayer = self._replayer()
        tips, where = merge_queue.fetch_fresh(self.repo, ["develop"], clone=replayer)
        self.assertEqual(where, self.clone)
        self.assertEqual(tips["develop"], self.published)

    def test_land_origin_is_absolute_from_the_checkout(self) -> None:
        project = types.SimpleNamespace(repo=self.repo)
        self.assertEqual(land._origin(project), self.forge)
        replayer = self._replayer()
        self.assertEqual(land.fresh_target(replayer, project, "origin/main"),
                         self.published)

    def test_list_remises_reads_the_forge(self) -> None:
        replayer = self._replayer()
        found, error = packet_candidates.list_remises(self.repo, replayer.path, [3041],
                                                      "develop")
        self.assertEqual(error, "")
        self.assertEqual(found, {3041: [(resolutions.REF_PREFIX + "3041/develop-1",
                                         self.published)]})
        self.assertEqual(plan_git._commit(
            self.clone, packet_candidates.REMISE_LOCAL + "3041/develop-1"), self.published)

    def test_symlinked_checkout_resolves_from_its_target(self) -> None:
        """Relecture ameesh-dev3 : Git résout `..` depuis la cible du lien ;
        un leurre voisin du lien n'est jamais lu."""
        links = os.path.join(self.tmp, "links")
        os.makedirs(links)
        link = os.path.join(links, "checkout")
        os.symlink(self.repo, link)
        decoy = self._decoy(os.path.join(links, "forge.git"))
        self.assertEqual(resolutions._local_from(link, "../forge.git"), self.forge)
        replayer = self._replayer(link)
        self.assertEqual(_git(self.clone, "remote", "get-url", "origin"), self.forge)
        tracked = plan_git._commit(self.clone, "refs/remotes/origin/develop")
        self.assertEqual(tracked, self.published)
        self.assertNotIn(tracked, (decoy, self.decoy))
        self.assertEqual(replayer.target_doubt, "")


if __name__ == "__main__":
    unittest.main()
