# SPDX-License-Identifier: AGPL-3.0-only
"""#2968 — review-after-merge : le dépôt de l'assigné ne masque plus le projet.

Constat de l'auditeur : `class_at_merge` prenait le dossier de travail de
l'assigné comme unique dépôt (puis le dépôt du projet seulement s'il était
vide). Un assigné dont le répertoire n'est pas un dépôt git, ou dont le clone
n'a pas le commit fusionné, faisait donc tomber la classe en doute et
n'ouvrait aucune relecture après fusion.

Attendu : essayer les candidats dans l'ordre (--repo explicite, dépôt de
l'assigné, dépôt du projet) et retenir le premier qui prouve l'intervalle ;
douter seulement si aucun ne le contient, en nommant les dépôts essayés.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import review_lot


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _repo(path):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q")
    return path


def _commit(path, name, text):
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", text)
    return _git(path, "rev-parse", "HEAD")


class RepoCandidatesTest(unittest.TestCase):
    """Les quatre témoins du lot #2968."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2968-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = _repo(os.path.join(self.tmp, "project"))
        self.frozen = _commit(self.project, "a.txt", "base")
        self.merged = _commit(self.project, "a.txt", "change")
        self.agent_dir = os.path.join(self.tmp, "agent")   # pas un dépôt git
        os.makedirs(self.agent_dir)
        self.item = {"id": 1, "assignee": "a", "app": "ameesh"}

    def _class(self, *, repo=None, project=None, sha=None, milestones=None):
        with mock.patch.object(review_lot, "_repo_of_agent",
                               return_value=self.agent_dir), \
                mock.patch.object(review_lot.lot_class, "repository",
                                  return_value=(self.project if project is None
                                                else project)), \
                mock.patch.object(review_lot.work, "milestones",
                                  return_value=(milestones if milestones is not None
                                                else [{"id": 1, "kind": "frozen",
                                                       "sha": self.frozen, "at_ts": 1}])), \
                mock.patch.object(review_lot.lot_class, "of_lot") as of_lot:
            of_lot.return_value = {"lot": 1, "class": "light", "files": ["a.txt"]}
            out = review_lot.class_at_merge(
                None, 1, item=dict(self.item),
                sha=self.merged if sha is None else sha, repo=repo, target="")
        return out, of_lot

    def test_1_repertoire_assigne_non_git_le_projet_prouve(self):
        out, of_lot = self._class()
        self.assertFalse(out.get("doubt"))
        self.assertEqual(out.get("class"), "light")
        of_lot.assert_called_once()
        self.assertEqual(of_lot.call_args.kwargs.get("repo"), self.project)

    def test_2_clone_assigne_sans_le_commit_le_projet_prouve(self):
        agent = _repo(os.path.join(self.tmp, "clone"))
        _commit(agent, "b.txt", "autre")       # dépôt git, sans le commit fusionné
        self.agent_dir = agent
        out, of_lot = self._class()
        self.assertFalse(out.get("doubt"))
        self.assertEqual(out.get("class"), "light")
        of_lot.assert_called_once()
        self.assertEqual(of_lot.call_args.kwargs.get("repo"), self.project)

    def test_3_aucun_depot_n_a_le_commit_doute_et_nomme_les_essayes(self):
        self.agent_dir = _repo(os.path.join(self.tmp, "agent-git"))
        _commit(self.agent_dir, "b.txt", "autre")
        other = _repo(os.path.join(self.tmp, "other"))
        _commit(other, "c.txt", "encore")
        out, of_lot = self._class(project=other)
        self.assertIs(out.get("doubt"), True)
        self.assertEqual(out.get("class"), "sensitive")
        self.assertFalse(of_lot.called, out)
        why = out.get("why") or ""
        self.assertIn(self.agent_dir, why)
        self.assertIn(other, why)
        self.assertIn("ne contient pas le commit", why)

    def test_4_repo_explicite_reste_autoritaire_sans_repli(self):
        out, of_lot = self._class(repo=self.agent_dir)   # non-git explicite
        self.assertIs(out.get("doubt"), True)
        self.assertFalse(of_lot.called, out)
        self.assertIn(self.agent_dir, out.get("why") or "")
        self.assertNotIn(self.project, out.get("why") or "")

    def test_4bis_repo_explicite_avec_le_commit(self):
        out, of_lot = self._class(repo=self.project)
        self.assertFalse(out.get("doubt"))
        self.assertEqual(out.get("class"), "light")
        self.assertEqual(of_lot.call_args.kwargs.get("repo"), self.project)

    def test_5_ni_commit_ni_gel_garde_le_motif_sans_nommer_les_depots(self):
        """Deux candidats mais aucun commit à essayer : le doute dit pourquoi,
        sans IndexError ni liste de dépôts qui n'ont rien pu lire."""
        out, of_lot = self._class(sha="", milestones=[])
        self.assertIs(out.get("doubt"), True)
        self.assertEqual(out.get("class"), "sensitive")
        self.assertFalse(of_lot.called, out)
        why = out.get("why") or ""
        self.assertIn("ni commit de fusion ni gel", why)
        self.assertNotIn("IndexError", why)

    def test_6_un_gel_hors_fusion_ferme_toute_la_lecture(self):
        """#2823 : un gel non ancêtre de la fusion est une preuve négative.
        Le candidat suivant ne doit ni relire ce gel ni retomber sur le diff
        du paquet : la lecture s'arrête sur un doute, sans of_lot."""
        self.agent_dir = _repo(os.path.join(self.tmp, "premier"))
        from ameesh import lot_measures as mesures
        with mock.patch.object(
                mesures, "lot_diff_range",
                side_effect=mesures.UnprovenLotDiff("gel hors fusion")) as lecture, \
                mock.patch.object(mesures, "diff_range") as repli:
            out, of_lot = self._class()
        lecture.assert_called_once()
        self.assertEqual(out.get("doubt"), True)
        self.assertEqual(out.get("class"), "sensitive")
        self.assertFalse(of_lot.called)
        self.assertFalse(repli.called)
        self.assertIn("gel hors fusion", out.get("why") or "")
