# SPDX-License-Identifier: AGPL-3.0-only
"""#3350 — le dépôt d'un lot du canon se choisit parmi TOUS les canons lisibles.

Témoin de la relecture de #3331 (gel 641de8afb2f7) : avec deux canons
configurés (`acme/rules` puis `manaty/home`) et un lot dont la PR est
`manaty/home#148` (branche `canon/2987-porte-prod-main`), `canon_clone()`
retenait le premier checkout lisible et `_canon_lot()` ne comparait la PR
qu'à ce seul clone : `repository()` rendait le clone `acme/rules`.

Attendu : chercher le slug de la PR dans tous les canons lisibles ; sans
correspondance, un doute explicite (None), jamais le premier canon configuré.

Témoins : la PR nomme le second canon (branche `canon/…` ou PR seule) ; le
premier canon reste choisi quand la PR le nomme ; la PR d'un canon absent des
clones lisibles est un doute ; une branche `canon/…` sans PR n'est lisible
qu'avec un seul canon ; un lot ordinaire garde le dépôt de son projet.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest

from ameesh import lot_class
from ameesh.config import CanonEntry

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

CANON_ITEM = {"id": 3350, "app": "ameesh", "branch": "canon/2987-porte-prod-main",
              "branch_target": "main", "pr_ref": "manaty/home#148"}


def _git(root, *args) -> str:
    proc = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          env=ENV)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _repo(path, origin: str = "") -> str:
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    return path


def _cfg(canons, state_dir: str, project: str = "") -> types.SimpleNamespace:
    """Une configuration factice avec `canon_entries`, comme `Config` (L42)."""
    entries = tuple(CanonEntry(path) for path in canons)
    radar = {"projects": {"ameesh": {"repo": project}}} if project else {}
    return types.SimpleNamespace(canon=(canons[0] if canons else ""), canon_ref="",
                                 canon_untrusted=False,
                                 extra_canons=entries[1:], canon_entries=entries,
                                 state_dir=state_dir, radar=radar)


class RepositoryAmongCanonsTest(unittest.TestCase):
    """Le dépôt du lot suit le slug de sa PR parmi les canons lisibles."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3350-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rules = _repo(os.path.join(self.tmp, "rules"),
                           origin="https://github.com/acme/rules.git")
        self.home = _repo(os.path.join(self.tmp, "home"),
                          origin="https://github.com/manaty/home.git")
        self.project = _repo(os.path.join(self.tmp, "ameesh"),
                             origin="https://github.com/manaty/ameesh.git")

    def test_the_pr_names_the_second_canon_clone(self):
        cfg = _cfg([self.rules, self.home], self.tmp, project=self.project)
        self.assertEqual(lot_class.repository(cfg, dict(CANON_ITEM)), self.home)

    def test_the_pr_alone_names_the_second_canon_clone(self):
        cfg = _cfg([self.rules, self.home], self.tmp, project=self.project)
        item = dict(CANON_ITEM, branch="lot/2987-x")
        self.assertEqual(lot_class.repository(cfg, item), self.home)

    def test_the_first_canon_is_kept_when_the_pr_names_it(self):
        cfg = _cfg([self.rules, self.home], self.tmp, project=self.project)
        item = dict(CANON_ITEM, pr_ref="acme/rules#7")
        self.assertEqual(lot_class.repository(cfg, item), self.rules)

    def test_a_canon_pr_without_a_readable_clone_is_a_doubt(self):
        cfg = _cfg([self.rules, os.path.join(self.tmp, "missing")], self.tmp,
                   project=self.project)
        self.assertIsNone(lot_class.repository(cfg, dict(CANON_ITEM)))

    def test_a_canon_branch_without_a_pr_is_a_doubt_with_two_canons(self):
        cfg = _cfg([self.rules, self.home], self.tmp, project=self.project)
        item = dict(CANON_ITEM, pr_ref="")
        self.assertIsNone(lot_class.repository(cfg, item))

    def test_a_canon_branch_without_a_pr_still_reads_the_only_canon(self):
        cfg = _cfg([self.home], self.tmp, project=self.project)
        item = dict(CANON_ITEM, pr_ref="")
        self.assertEqual(lot_class.repository(cfg, item), self.home)

    def test_a_normal_lot_keeps_the_project_repository(self):
        cfg = _cfg([self.rules, self.home], self.tmp, project=self.project)
        item = {"app": "ameesh", "branch": "lot/9-x", "branch_target": "main",
                "pr_ref": "manaty/ameesh#9"}
        self.assertEqual(lot_class.repository(cfg, item), self.project)

    def test_a_lone_canon_without_origin_still_reads_the_canon_branch(self):
        # #3331 conservé : un seul canon, sans `origin` comparable, ne peut
        # pas infirmer la PR ; la branche `canon/…` le désigne encore.
        canon = _repo(os.path.join(self.tmp, "untrusted"))
        cfg = _cfg([canon], self.tmp, project=self.project)
        self.assertEqual(lot_class.repository(cfg, dict(CANON_ITEM)), canon)

    def test_a_lone_unknown_canon_does_not_hide_another_canon(self):
        unknown = _repo(os.path.join(self.tmp, "untrusted"))
        cfg = _cfg([unknown, self.rules], self.tmp, project=self.project)
        item = dict(CANON_ITEM, pr_ref="manaty/home#148")
        self.assertIsNone(lot_class.repository(cfg, item))

    def test_canon_clones_lists_only_readable_checkouts(self):
        missing = os.path.join(self.tmp, "missing")
        cfg = _cfg([self.rules, missing, self.home], self.tmp, project=self.project)
        self.assertEqual(lot_class.canon_clones(cfg),
                         [(self.rules, "acme/rules"), (self.home, "manaty/home")])


if __name__ == "__main__":
    unittest.main()
