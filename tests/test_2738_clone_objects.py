# SPDX-License-Identifier: AGPL-3.0-only
"""#2738 — les objets d'une tête fraîche sont garantis dans le clone de calcul.

Le clone radar est un `git clone --shared` : il lit les objets du checkout des
agents par alternates. Un fetch peut donc créer `refs/ameesh/queue/<branche>`
sans transférer les siens, quand un autre ref du clone (la copie
`origin/<branche>` prise au clone, une résolution) atteint déjà la tête ;
l'élagage du clone source (`gc`, ménage disque) retire alors des objets que
seul le clone partagé utilisait. `merge-tree` échoue sur le blob absent et
`paquet-candidats.py` écarte un lot prêt en « erreur de lecture » (h-1200,
#2681, blob `04b645d2`, manifeste l.66-70).

Attendu : `fetch_fresh` vérifie que la fermeture de chaque tête **et de la
cible** est lisible dans le clone (la fusion d'essai lit aussi la base de
fusion et l'ancêtre commun), refait un `--refetch` de la branche quand un
objet manque, et ne rend la tête qu'une fois ses objets présents ; une
`QueueError` explicite sinon, jamais une tête dont les objets sont absents.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import merge_queue, packet_candidates, plan_git, resolutions
from ameesh.canon import GitError


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str) -> None:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


def _commit(repo: str, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


class ObjectsGuardTest(unittest.TestCase):
    """Le garde lui-même : quels objets manquent, et comment il les nomme."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2738-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _write(self.repo, "old.txt", "ancien\n")
        self.old = _commit(self.repo, "ancien")
        os.unlink(os.path.join(self.repo, "old.txt"))
        _write(self.repo, "shared.py", "one\ntwo\n")
        self.base = _commit(self.repo, "base")
        _git(self.repo, "switch", "-q", "-c", "lot")
        _write(self.repo, "shared.py", "FOUR\ntwo\n")
        self.tip = _commit(self.repo, "lot")
        self.blob = _git(self.repo, "rev-parse", self.tip + ":shared.py")

    def _drop_blob(self) -> None:
        # Les objets sont loose ici : l'état exact de l'incident (commit et
        # arbre lisibles, contenu du gel absent) se reproduit en retirant le
        # seul fichier du blob.
        path = os.path.join(self.repo, ".git", "objects", self.blob[:2], self.blob[2:])
        self.assertTrue(os.path.isfile(path), "le blob doit être loose pour ce témoin")
        os.unlink(path)

    def test_an_intact_tip_has_no_unreadable_object(self):
        self.assertEqual(merge_queue._unreadable_objects(self.repo, self.tip), [])
        self.assertEqual(merge_queue._unreadable_objects(self.repo,
                                                         [self.tip, self.base]), [])

    def test_a_missing_blob_is_named(self):
        self._drop_blob()
        self.assertEqual(merge_queue._unreadable_objects(self.repo, self.tip),
                         [self.blob])

    def test_an_unreadable_tip_is_named(self):
        # tête absente : le garde ne peut pas conclure par un rev-list, il la
        # nomme pour déclencher le refetch.
        self.assertEqual(merge_queue._unreadable_objects(self.repo, "0" * 40),
                         ["0" * 40])

    def test_a_missing_merge_base_blob_is_named(self):
        # B1 : l'ancêtre commun à la tête et à la cible est lu par la fusion
        # trois voies ; il fait partie de la fermeture vérifiée, pas d'un
        # « reste » exclu par la borne.
        base_blob = _git(self.repo, "rev-parse", self.base + ":shared.py")
        path = os.path.join(self.repo, ".git", "objects", base_blob[:2], base_blob[2:])
        os.unlink(path)
        self.assertEqual(merge_queue._unreadable_objects(self.repo, self.tip),
                         [base_blob])
        self.assertEqual(merge_queue._unreadable_objects(self.repo,
                                                         [self.tip, self.base]),
                         [base_blob])

    def test_a_bounded_walk_ignores_history_outside_the_three_sides(self):
        # La borne par la cible exclut l'histoire commune : un blob ancien que
        # la fusion d'essai ne lit jamais ne doit pas déclencher de refetch.
        old_blob = _git(self.repo, "rev-parse", self.old + ":old.txt")
        os.unlink(os.path.join(self.repo, ".git", "objects",
                               old_blob[:2], old_blob[2:]))
        self.assertEqual(merge_queue._unreadable_objects(self.repo, [self.tip]),
                         [old_blob])
        self.assertEqual(merge_queue._unreadable_objects(self.repo,
                                                         [self.tip, self.base],
                                                         self.base), [])

    def test_a_bounded_walk_still_names_the_merge_base_blob(self):
        # La borne garde les arbres des trois côtés : celui du commit de bord
        # (la base de fusion) est lu par merge-tree, donc vérifié.
        base_blob = _git(self.repo, "rev-parse", self.base + ":shared.py")
        os.unlink(os.path.join(self.repo, ".git", "objects",
                               base_blob[:2], base_blob[2:]))
        self.assertEqual(merge_queue._unreadable_objects(self.repo,
                                                         [self.tip, self.base],
                                                         self.base), [base_blob])

    def test_multiple_merge_bases_fall_back_to_the_full_closure(self):
        # Criss-cross : deux bases de fusion pour la tête. merge-tree les
        # fusionne récursivement et lit l'ancêtre de leur fusion ; la borne
        # seule le laisserait passer.
        _git(self.repo, "switch", "-q", "main")
        _write(self.repo, "shared.py", "MAIN\ntwo\n")
        other = _commit(self.repo, "main other")
        tree = _git(self.repo, "rev-parse", self.tip + "^{tree}")
        left = _git(self.repo, "commit-tree", tree, "-p", self.tip,
                    "-p", other, "-m", "left cross merge")
        tree = _git(self.repo, "rev-parse", other + "^{tree}")
        right = _git(self.repo, "commit-tree", tree, "-p", other,
                     "-p", self.tip, "-m", "right cross merge")
        self.assertEqual(len(_git(self.repo, "merge-base", "--all",
                                  left, right).split()), 2)
        ancestor_blob = _git(self.repo, "rev-parse", self.base + ":shared.py")
        os.unlink(os.path.join(self.repo, ".git", "objects",
                               ancestor_blob[:2], ancestor_blob[2:]))
        self.assertEqual(merge_queue._unreadable_objects(self.repo,
                                                         [left], right),
                         [ancestor_blob])


class FetchFreshObjectsTest(unittest.TestCase):
    """Le clone partagé perd ses objets quand sa source est élaguée."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2738-fetch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.agents = os.path.join(self.tmp, "agents")
        os.makedirs(self.agents)
        _git(self.agents, "init", "-q", "-b", "main")
        _git(self.agents, "config", "user.name", "banc")
        _git(self.agents, "config", "user.email", "banc@example.invalid")
        _write(self.agents, "shared.py", "one\ntwo\n")
        _commit(self.agents, "base")
        _git(self.agents, "remote", "add", "origin", self.origin)
        _git(self.agents, "push", "-q", "-u", "origin", "main")
        self.clone = resolutions.Replayer(self.agents, os.path.join(self.tmp, "radar"))

    def publish(self, branch: str) -> str:
        """Publie le lot et la cible qui touchent tous deux `shared.py`."""
        _git(self.agents, "switch", "-q", "-c", branch, "main")
        _write(self.agents, "shared.py", "FOUR\ntwo\n")
        tip = _commit(self.agents, "lot")
        _git(self.agents, "push", "-q", "origin", branch)
        _git(self.agents, "switch", "-q", "main")
        _write(self.agents, "shared.py", "MAIN\ntwo\n")
        _commit(self.agents, "main avance")
        _git(self.agents, "push", "-q", "origin", "main")
        return tip

    def prune(self, branch: str) -> None:
        """Le ménage de la source : la branche et ses objets disparaissent."""
        _git(self.agents, "branch", "-D", branch)
        _git(self.agents, "update-ref", "-d", "refs/remotes/origin/" + branch)
        _git(self.agents, "reflog", "expire", "--expire=now", "--all")
        _git(self.agents, "gc", "--prune=now", "-q")

    def assert_readable(self, tip: str, base: str) -> None:
        self.assertIsNotNone(plan_git._commit(self.clone.path, tip))
        missing = merge_queue._unreadable_objects(self.clone.path, [tip, base])
        self.assertEqual(missing, [])
        # la lecture qui échouait dans l'incident : la fusion d'essai lit les
        # blobs des deux côtés et rend un conflit, jamais une erreur de lecture
        merged = packet_candidates.merge_tree(self.clone.path, base, tip)
        self.assertEqual(merged["state"], "conflict", merged)

    def test_a_pruned_source_clone_is_refetched(self):
        """Le clone a hérité `origin/<lot>`, puis la source élague la branche :
        la tête n'est plus lisible que par une ref morte. Le garde refait le
        fetch et ne rend que des objets présents."""
        branch = "lot/x"
        tip = self.publish(branch)
        self.clone.ensure()             # clone partagé, refs copiés de la source
        self.assertTrue(_git(self.clone.path, "rev-parse",
                             "refs/remotes/origin/" + branch))
        self.prune(branch)
        self.assertIsNone(plan_git._commit(self.clone.path, tip))  # objets prêtés

        tips, where = merge_queue.fetch_fresh(self.agents, [branch, "main"],
                                              self.clone, base="main")
        self.assertEqual(where, self.clone.path)
        self.assertEqual(tips[branch], tip)
        self.assert_readable(tip, tips["main"])
        # le dépôt des agents n'a pas été écrit
        self.assertFalse(_git(self.agents, "for-each-ref",
                              "refs/ameesh/queue/"))

    def test_a_missing_blob_of_the_freeze_is_refetched(self):
        """L'état de l'incident : commit et arbre présents, contenu du gel
        absent (blob retiré du clone, source élaguée). Le fetch répare."""
        branch = "lot/x"
        self.clone.ensure()
        # le premier fetch transfère les objets (la source ne les a plus) :
        # les garder loose pour pouvoir retirer exactement le blob
        _git(self.clone.path, "config", "transfer.unpackLimit", "1000")
        tip = self.publish(branch)
        self.prune(branch)
        tips, _where = merge_queue.fetch_fresh(self.agents, [branch, "main"],
                                               self.clone, base="main")
        self.assertEqual(tips[branch], tip)
        base = tips["main"]
        blob = _git(self.clone.path, "rev-parse", tip + ":shared.py")
        path = os.path.join(self.clone.path, ".git", "objects", blob[:2], blob[2:])
        self.assertTrue(os.path.isfile(path), "le blob doit être loose pour ce témoin")
        os.unlink(path)
        self.assertEqual(merge_queue._unreadable_objects(self.clone.path, tip),
                         [blob])

        tips, _where = merge_queue.fetch_fresh(self.agents, [branch, "main"],
                                               self.clone, base="main")
        self.assertEqual(tips[branch], tip)
        self.assert_readable(tip, tips["main"])

    def test_an_unrepairable_tip_raises_a_named_error(self):
        """Le remote ne sert pas les objets : QueueError, jamais une tête dont
        les objets sont absents (la liste des candidats doit rester
        incomplète et le dire)."""
        branch = "lot/x"
        self.clone.ensure()
        tip = self.publish(branch)
        self.prune(branch)       # les objets du lot ne viennent plus que du fetch
        merge_queue.fetch_fresh(self.agents, [branch, "main"], self.clone, base="main")
        blob = _git(self.clone.path, "rev-parse", tip + ":shared.py")
        os.unlink(os.path.join(self.clone.path, ".git", "objects", blob[:2], blob[2:]))
        with mock.patch.object(merge_queue, "_refetch_tip_objects", return_value=""):
            with self.assertRaises(merge_queue.QueueError) as raised:
                merge_queue.fetch_fresh(self.agents, [branch, "main"], self.clone,
                                        base="main")
        self.assertIn("cannot guarantee the objects of %s" % branch, str(raised.exception))

    def test_a_missing_merge_base_blob_of_the_target_is_refetched(self):
        """B1 : la fusion trois voies lit l'ancêtre commun et la cible. Un blob
        de cet ancêtre, absent de la source, ne doit pas faire basculer
        l'essai de fusion en erreur de lecture."""
        branch = "lot/x"
        base_blob = _git(self.agents, "rev-parse", "main:shared.py")  # ancêtre commun
        tip = self.publish(branch)
        self.clone.ensure()
        path = os.path.join(self.agents, ".git", "objects",
                            base_blob[:2], base_blob[2:])
        self.assertTrue(os.path.isfile(path), "le blob doit être loose pour ce témoin")
        os.unlink(path)
        tips, _where = merge_queue.fetch_fresh(self.agents, [branch, "main"],
                                               self.clone, base="main")
        self.assertEqual(tips[branch], tip)
        self.assert_readable(tip, tips["main"])       # merge-tree lit les trois côtés
        self.assertEqual(merge_queue._unreadable_objects(self.clone.path,
                                                         [tip, tips["main"]]), [])

    def test_a_target_fetched_alone_is_repaired_too(self):
        """La cible n'est pas toujours dans `names` (verdict_carry ne récupère
        que la branche du gel, la cible l'a été avant) : le garde doit la
        vérifier et la réparer nommément."""
        branch = "lot/x"
        tip = self.publish(branch)
        # un objet de la cible seule, absent du lot (fichier ajouté sur main)
        _git(self.agents, "switch", "-q", "main")
        _write(self.agents, "main_only.txt", "cible\n")
        _commit(self.agents, "main seule")
        _git(self.agents, "push", "-q", "origin", "main")
        self.clone.ensure()
        merge_queue.fetch_fresh(self.agents, ["main"], self.clone)
        base = plan_git._commit(self.clone.path, merge_queue.QUEUE_REF + "main")
        source_blob = _git(self.agents, "rev-parse", "main:main_only.txt")
        path = os.path.join(self.agents, ".git", "objects",
                            source_blob[:2], source_blob[2:])
        self.assertTrue(os.path.isfile(path), "le blob doit être loose pour ce témoin")
        os.unlink(path)
        tips, _where = merge_queue.fetch_fresh(self.agents, [branch], self.clone,
                                               base="main")
        self.assertEqual(tips[branch], tip)
        self.assertEqual(merge_queue._unreadable_objects(self.clone.path,
                                                         [tip, base]), [])

    def test_a_guard_git_failure_is_a_queue_error(self):
        """B2 : un échec git du garde (timeout de rev-list ou de --refetch) est
        une QueueError du contrat de fetch_fresh, jamais un GitError qui ferait
        planter paquet-candidats."""
        branch = "lot/x"
        self.clone.ensure()
        self.publish(branch)
        with mock.patch.object(merge_queue, "_unreadable_objects",
                               side_effect=GitError("git rev-list: timed out")):
            with self.assertRaises(merge_queue.QueueError) as raised:
                merge_queue.fetch_fresh(self.agents, [branch, "main"], self.clone,
                                        base="main")
        self.assertIn("cannot guarantee the objects", str(raised.exception))

    def test_an_intact_tip_is_not_refetched(self):
        branch = "lot/x"
        self.clone.ensure()
        tip = self.publish(branch)
        merge_queue.fetch_fresh(self.agents, [branch, "main"], self.clone, base="main")
        with mock.patch.object(merge_queue, "_refetch_tip_objects",
                               side_effect=AssertionError("refetch inutile")) as refetch:
            tips, _where = merge_queue.fetch_fresh(self.agents, [branch, "main"],
                                                   self.clone, base="main")
        self.assertEqual(tips[branch], tip)
        refetch.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
