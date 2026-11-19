# SPDX-License-Identifier: AGPL-3.0-only
"""#7755 — « freeze-not-head » ne se lit plus sur une ref de suivi périmée.

Constat (17/11) : le débit annonçait #7630 « freeze 901b1feb is not the
published head c3af1ac1 ». En réalité c3af1ac1 était le gel de 05:39Z et
901b1feb la tête publiée, vérifiée à la forge. `candidate_refusal` lisait
`origin/<branche>` par `local_commit`, dans le clone de l'assigné
(`lot_class.repo_for_freeze`), sans fetch : une ref de suivi en retard de
~10 h accusait l'auteur à tort.

Témoins du lot :

* une ref de suivi laissée au gel N-1 alors que le gel N est publié ne rend
  jamais `freeze-not-head` : la forge est relue (`ls-remote` mis en cache) et
  le lot est prêt ;
* une forge qui a réellement avancé depuis le gel garde `freeze-not-head` ;
* forge ou ref illisible, ou tête localement en retard : `unknown`, jamais
  `freeze-not-head` — on ne nomme pas l'auteur sans preuve ;
* le cas « gel = tête locale », qui n'a rien à vérifier, ne paie aucune
  lecture de la forge ; une lecture est réutilisée dans la passe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import packet_candidates

ENV_GIT = dict(os.environ, GIT_AUTHOR_NAME="dev1",
               GIT_AUTHOR_EMAIL="dev1@ameesh.invalid",
               GIT_COMMITTER_NAME="dev1", GIT_COMMITTER_EMAIL="dev1@ameesh.invalid")


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=ENV_GIT)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def write_commit(repo, path, text, message):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as stream:
        stream.write(text)
    git(repo, "add", path)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def local_repo(root):
    """`main` ; branche `lot/7755-x` avec deux commits (N-1, N).

    La ref de suivi `origin/lot/7755-x` est laissée à N-1 : c'est le clone de
    l'assigné qui n'a pas récupéré la poussée. Aucun distant n'est déclaré,
    donc aucune lecture réseau n'est possible."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    base = write_commit(root, "f", "base\n", "base")
    git(root, "checkout", "-q", "-b", "lot/7755-x")
    previous = write_commit(root, "g", "gel N-1\n", "gel N-1")
    frozen = write_commit(root, "g", "gel N\n", "gel N")
    git(root, "checkout", "-q", "main")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    git(root, "update-ref", "refs/remotes/origin/lot/7755-x", previous)
    return base, previous, frozen


def forge_repo(root, *, frozen_is_head=True):
    """Une forge locale (dépôt nu) et le clone de l'assigné.

    L'auteur a poussé jusqu'à N quand `frozen_is_head` ; sinon il a poussé un
    commit M après le gel N. La ref de suivi du clone est ensuite remise à
    N-1, exactement comme un clone jamais réactualisé."""
    os.makedirs(root, exist_ok=True)
    origin = os.path.join(root, "origin.git")
    git(root, "init", "-q", "--bare", origin)
    author = os.path.join(root, "author")
    git(root, "clone", "-q", origin, author)
    git(author, "checkout", "-q", "-b", "main")
    base = write_commit(author, "f", "base\n", "base")
    git(author, "push", "-q", "origin", "main")
    # le dépôt nu doit désigner `main` comme tête par défaut, sinon le clone
    # du lecteur ne s'installe sur aucune branche.
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git(author, "checkout", "-q", "-b", "lot/7755-x")
    previous = write_commit(author, "g", "gel N-1\n", "gel N-1")
    frozen = write_commit(author, "g", "gel N\n", "gel N")
    head = frozen
    if not frozen_is_head:
        head = write_commit(author, "h", "après gel\n", "après gel")
    git(author, "push", "-q", "origin", "lot/7755-x")
    reader = os.path.join(root, "reader")
    git(root, "clone", "-q", origin, reader)
    # Le gel et sa suite sont dans les objets du clone, mais la ref de suivi
    # n'a pas bougé depuis N-1.
    git(reader, "update-ref", "refs/remotes/origin/lot/7755-x", previous)
    return reader, frozen, previous, base, origin, head


def refusal(repo, frozen, *, branch_head="", branch="lot/7755-x"):
    row = {"id": 7755, "branch": branch, "branch_target": "main",
           "assignee": "dev1"}
    if branch_head:
        row["branch_head"] = branch_head
    return packet_candidates.candidate_refusal(
        None, None, row, [{"kind": "frozen", "sha": frozen, "at_ts": 1.0}],
        repo=repo)


class StaleTrackingRefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-7755-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        packet_candidates._FORGE_HEADS.clear()
        self.addCleanup(packet_candidates._FORGE_HEADS.clear)

    def test_ref_locale_ancetre_du_gel_ne_fait_pas_freeze_not_head(self):
        # Le témoin minimal : origin/lot est au gel N-1, le gel N est local.
        repo = os.path.join(self.tmp, "repo")
        _base, _previous, frozen = local_repo(repo)
        cause = refusal(repo, frozen)
        self.assertNotEqual(cause["code"], "freeze-not-head")
        self.assertTrue(cause["unknown"])
        self.assertFalse(cause["ready"])

    def test_observation_ancetre_du_gel_ne_fait_pas_freeze_not_head(self):
        # Sans ref de suivi, `branch_head` reste une observation : si elle est
        # antérieure au gel, elle ne prouve pas davantage un gel périmé.
        repo = os.path.join(self.tmp, "repo")
        _base, previous, frozen = local_repo(repo)
        git(repo, "update-ref", "-d", "refs/remotes/origin/lot/7755-x")
        cause = refusal(repo, frozen, branch_head=previous)
        self.assertNotEqual(cause["code"], "freeze-not-head")
        self.assertTrue(cause["unknown"])

    def test_ref_egale_au_gel_ne_lit_pas_la_forge(self):
        # Le cas normal ne doit pas ajouter un `ls-remote` par lot aux passes
        # de débit et d'escalade.
        repo = os.path.join(self.tmp, "repo")
        _base, _previous, frozen = local_repo(repo)
        git(repo, "update-ref", "refs/remotes/origin/lot/7755-x", frozen)
        with mock.patch.object(packet_candidates, "_forge_head",
                               side_effect=AssertionError("forge lue")) as seen:
            cause = refusal(repo, frozen)
        self.assertFalse(seen.called)
        self.assertEqual(cause["code"], "")
        self.assertTrue(cause["ready"])

    def test_forge_en_retard_ne_fait_pas_accuser_l_auteur(self):
        # Témoin du lot : le gel N est poussé, le clone est resté à N-1.
        repo, frozen, _previous, _base, _origin, _head = forge_repo(self.tmp)
        cause = refusal(repo, frozen)
        self.assertNotEqual(cause["code"], "freeze-not-head")
        self.assertEqual(cause["code"], "")
        self.assertTrue(cause["ready"])
        self.assertEqual(cause["published"], frozen)
        self.assertEqual(cause["published_source"], "forge")

    def test_forge_avancee_garde_freeze_not_head(self):
        # La forge dit M après le gel N, la ref locale dit N-1 : le refus est
        # fondé sur la tête publiée, pas sur la ref périmée.
        repo, frozen, _previous, _base, _origin, head = forge_repo(
            self.tmp, frozen_is_head=False)
        cause = refusal(repo, frozen)
        self.assertEqual(cause["code"], "freeze-not-head")
        self.assertEqual(cause["owner"], "author")
        self.assertNotEqual(head, frozen)
        self.assertIn("published head %s" % head[:12], cause["detail"])

    def test_forge_illisible_ne_conclut_pas(self):
        repo = os.path.join(self.tmp, "repo")
        _base, _previous, frozen = local_repo(repo)
        git(repo, "remote", "add", "origin",
            os.path.join(self.tmp, "absent.git"))
        cause = refusal(repo, frozen)
        self.assertNotEqual(cause["code"], "freeze-not-head")
        self.assertTrue(cause["unknown"])
        self.assertFalse(cause["ready"])

    def test_branche_absente_de_la_forge_ne_conclut_pas(self):
        repo = os.path.join(self.tmp, "repo")
        _base, _previous, frozen = local_repo(repo)
        origin = os.path.join(self.tmp, "vide.git")
        git(self.tmp, "init", "-q", "--bare", origin)
        git(repo, "remote", "add", "origin", origin)
        cause = refusal(repo, frozen)
        self.assertNotEqual(cause["code"], "freeze-not-head")
        self.assertTrue(cause["unknown"])

    def test_une_lecture_est_reutilisee_dans_la_passe(self):
        repo, frozen, _previous, _base, origin, _head = forge_repo(self.tmp)
        kind, sha = packet_candidates._forge_head(repo, "lot/7755-x")
        self.assertEqual((kind, sha), ("sha", frozen))
        # La forge disparaît : la lecture en cache vaut encore pour la passe,
        # sans relancer un `ls-remote`.
        shutil.rmtree(origin)
        self.assertEqual(packet_candidates._forge_head(repo, "lot/7755-x"),
                         ("sha", frozen))


if __name__ == "__main__":  # pragma: no cover - exécution directe
    unittest.main()
