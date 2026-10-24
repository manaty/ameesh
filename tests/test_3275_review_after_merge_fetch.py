# SPDX-License-Identifier: AGPL-3.0-only
"""#3275 — relecture après fusion : la cible est rafraîchie avant la lecture.

Constat (Nexlink #3268, deux échecs) : le commit fusionné 0d19ba3 (develop
6327d8e94) était absent du clone partagé du projet ; `class_at_merge` lisait
l'intervalle de fusion sans fetch et doutait « le dépôt ne contient pas le
commit ». Ce n'était pas un trou de fusion, seulement un clone pas rafraîchi.

Attendu : rafraîchir la cible exacte (refspec de la branche cible, puis le SHA
de fusion) dans le clone lu ; un SHA encore introuvable reste un doute nommé,
retenté au balayage suivant. Témoin : un commit fusionné après le dernier
fetch du clone est trouvé et la relecture s'ouvre.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import lot_measures, review_lot


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _commit(path, name, text):
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", text)
    return _git(path, "rev-parse", "HEAD")


class RefreshBeforeMergeIntervalTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3275-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.forge = os.path.join(self.tmp, "forge.git")
        subprocess.run(["git", "init", "-q", "--bare", self.forge], check=True)
        self.dev = os.path.join(self.tmp, "dev")
        subprocess.run(["git", "clone", "-q", self.forge, self.dev],
                       check=True, capture_output=True)
        _git(self.dev, "checkout", "-q", "-b", "main")
        _commit(self.dev, "README", "base")
        _git(self.dev, "push", "-q", "origin", "main")
        _git(self.dev, "checkout", "-q", "-b", "develop")
        _git(self.dev, "push", "-q", "origin", "develop")
        _git(self.forge, "symbolic-ref", "HEAD", "refs/heads/main")
        self.item = {"id": 3268, "assignee": "a", "app": "nexlink",
                     "branch_target": "develop"}

    def _shared(self, *extra):
        """Le clone partagé, fetché AVANT la fusion."""
        path = os.path.join(self.tmp, "shared")
        subprocess.run(["git", "clone", "-q", *extra, self.forge, path],
                       check=True, capture_output=True)
        return path

    def _merge_lot(self, name="lot.sql"):
        """Une fusion --no-ff du lot dans develop, poussée à la forge."""
        _git(self.dev, "checkout", "-q", "-b", "lot/3268", "develop")
        frozen = _commit(self.dev, name, "lot")
        _git(self.dev, "checkout", "-q", "develop")
        _git(self.dev, "merge", "-q", "--no-ff", "-m", "paquet", "lot/3268")
        merged = _git(self.dev, "rev-parse", "HEAD")
        _git(self.dev, "push", "-q", "origin", "develop")
        return frozen, merged

    def _class(self, repo, *, frozen, merged, target="develop"):
        with mock.patch.object(review_lot.work, "milestones",
                               return_value=[{"id": 1, "kind": "frozen",
                                              "sha": frozen, "at_ts": 1}]), \
                mock.patch.object(review_lot.lot_class, "of_lot") as of_lot:
            of_lot.return_value = {"lot": 3268, "class": "light", "files": ["lot.sql"]}
            out = review_lot.class_at_merge(None, 3268, item=dict(self.item),
                                            repo=repo, target=target, sha=merged)
        return out, of_lot

    def test_1_fusion_poussee_apres_le_dernier_fetch_est_trouvee(self):
        shared = self._shared()
        frozen, merged = self._merge_lot()
        self.assertFalse(lot_measures.has_commit(shared, merged))   # le constat
        out, of_lot = self._class(shared, frozen=frozen, merged=merged)
        self.assertFalse(out.get("doubt"), out)
        self.assertEqual(out.get("class"), "light")
        of_lot.assert_called_once()
        self.assertEqual(of_lot.call_args.kwargs.get("repo"), shared)
        self.assertEqual(of_lot.call_args.kwargs.get("head"), frozen)
        self.assertEqual(_git(shared, "rev-parse", "origin/develop"), merged)

    def test_2_clone_a_une_branche_la_refspec_explicite_rapporte_la_cible(self):
        shared = self._shared("--single-branch", "--branch", "main")
        frozen, merged = self._merge_lot()
        out, of_lot = self._class(shared, frozen=frozen, merged=merged)
        self.assertFalse(out.get("doubt"), out)
        of_lot.assert_called_once()
        self.assertEqual(_git(shared, "rev-parse", "refs/remotes/origin/develop"),
                         merged)

    def test_3_sha_introuvable_apres_fetch_reste_un_doute_nomme(self):
        shared = self._shared()
        ghost = "0d19ba3" + "0" * 33
        out, of_lot = self._class(shared, frozen=ghost, merged=ghost)
        self.assertIs(out.get("doubt"), True)
        self.assertFalse(of_lot.called)
        why = out.get("why") or ""
        self.assertIn("ne contient pas le commit", why)
        self.assertIn("après fetch de origin/develop", why)

    def test_4_fetch_impossible_dit_le_doute_sans_lever(self):
        shared = self._shared()
        frozen, merged = self._merge_lot()
        _git(shared, "remote", "set-url", "origin", os.path.join(self.tmp, "absent.git"))
        out, of_lot = self._class(shared, frozen=frozen, merged=merged)
        self.assertIs(out.get("doubt"), True)
        self.assertFalse(of_lot.called)
        self.assertIn("fetch impossible : origin/develop", out.get("why") or "")

    def test_5_clone_qui_prouve_deja_n_est_pas_fetche(self):
        frozen, merged = self._merge_lot()
        shared = self._shared()
        with mock.patch.object(lot_measures, "_git", wraps=lot_measures._git) as git:
            self.assertEqual(
                lot_measures.refresh_target(shared, "develop", [merged, frozen]), "")
        self.assertFalse([c for c in git.call_args_list if "fetch" in c.args], git.call_args_list)

    def test_6_depot_sans_remote_est_sa_propre_reference(self):
        local = os.path.join(self.tmp, "local")
        os.makedirs(local)
        _git(local, "init", "-q")
        sha = _commit(local, "a", "a")
        self.assertEqual(lot_measures.refresh_target(local, "develop", [sha]), "")
        self.assertEqual(lot_measures.refresh_target(local, "develop", ["f" * 40]), "")


if __name__ == "__main__":
    unittest.main()
