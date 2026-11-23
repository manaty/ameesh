# SPDX-License-Identifier: AGPL-3.0-only
"""#8166 — exécuteur au repos, tranche 2 (manaty/ameesh#6392, suite de #8152).

Constat IMA (128 agents au repos) : chaque agent publiait son aperçu toutes
les 15 s (deux appels git et une écriture), renouvelait seul son bail (une
connexion neuve, un contrôle de schéma et un UPDATE au tiers du bail), et
refaisait sa passe complète au pas de repos.

Témoins :

* aperçu : au repos, ni git ni écriture si HEAD, l'index et le journal
  d'événements n'ont pas bougé ; confirmation au plus tard à `REFRESH` ;
  `STALE_AFTER` couvre cette confirmation ; pendant un tour, git à chaque
  capture comme avant.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import agent_peek as peek, config


def _git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo(root):
    _git(root, "init", "-b", "work")
    Path(root, "f.txt").write_text("a\n")
    _git(root, "add", "f.txt")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.org", "commit", "-m", "base")


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class ApercuAuRepos(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peek8166-")
        self.addCleanup(__import__("shutil").rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        _repo(self.work)
        self.cfg = config.Config(state_dir=self.state)
        self.active = False
        self.clock = _Clock()
        clock = mock.patch.object(peek.time, "monotonic", self.clock.monotonic)
        clock.start()
        self.addCleanup(clock.stop)

    def _publisher(self):
        publisher = peek.Publisher.__new__(peek.Publisher)
        publisher.cfg, publisher.agent = self.cfg, "w"
        publisher.cwd, publisher.reader = (lambda: self.work), (lambda: None)
        publisher.active = lambda: self.active
        publisher._workspace = publisher._workspace_mark = None
        publisher._events = publisher._events_mark = None
        return publisher

    def _captures(self, publisher, n):
        with mock.patch.object(peek, "workspace", wraps=peek.workspace) as ws:
            for _ in range(n):
                publisher._capture(self.cfg)
                self.clock.t += peek.INTERVAL
        return ws.call_count

    def test_au_repos_git_une_fois(self):
        publisher = self._publisher()
        self.assertEqual(self._captures(publisher, 10), 1)

    def test_pendant_un_tour_git_a_chaque_capture(self):
        self.active = True
        self.assertEqual(self._captures(self._publisher(), 4), 4)

    def test_index_ou_commit_relance_git(self):
        publisher = self._publisher()
        self._captures(publisher, 2)
        Path(self.work, "f.txt").write_text("b\n")
        _git(self.work, "add", "f.txt")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertIn("f.txt", publisher._workspace["diff_stat"])
        _git(self.work, "-c", "user.name=T", "-c", "user.email=t@example.org",
             "commit", "-m", "suite")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertEqual(publisher._workspace["diff_stat"], "")

    def test_changement_de_branche(self):
        publisher = self._publisher()
        self._captures(publisher, 1)
        _git(self.work, "checkout", "-q", "-b", "autre")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertEqual(publisher._workspace["branch"], "autre")

    def test_worktree(self):
        tree = os.path.join(self.root, "tree")
        _git(self.work, "worktree", "add", "-q", "-b", "lot", tree)
        self.work = tree
        publisher = self._publisher()
        self.assertEqual(self._captures(publisher, 3), 1)
        Path(tree, "f.txt").write_text("c\n")
        _git(tree, "add", "f.txt")
        self.assertEqual(self._captures(publisher, 2), 1)
        self.assertIn("f.txt", publisher._workspace["diff_stat"])

    def test_au_plus_tard_a_refresh(self):
        publisher = self._publisher()
        self._captures(publisher, 1)
        self.clock.t += peek.REFRESH
        self.assertEqual(self._captures(publisher, 1), 1)

    def test_hors_depot(self):
        self.work = os.path.join(self.root, "nu")
        os.makedirs(self.work)
        publisher = self._publisher()
        self.assertEqual(self._captures(publisher, 5), 1)
        self.assertFalse(publisher._workspace["available"])

    def test_journal_relu_seulement_s_il_bouge(self):
        publisher = self._publisher()
        os.makedirs(self.cfg.agent_dir("w"), exist_ok=True)
        journal = os.path.join(self.cfg.agent_dir("w"), "events.jsonl")
        Path(journal).write_text("{}\n")
        with mock.patch.object(peek, "selected_events", return_value=[]) as lu:
            for _ in range(3):
                publisher._capture(self.cfg)
            self.assertEqual(lu.call_count, 1)
            with open(journal, "a") as stream:
                stream.write("{}\n")
            publisher._capture(self.cfg)
            self.assertEqual(lu.call_count, 2)

    def test_fraicheur_couvre_la_confirmation(self):
        self.assertGreater(peek.STALE_AFTER, peek.REFRESH + peek.INTERVAL)


class PublicationAuRepos(unittest.TestCase):
    """Une écriture seulement si la capture change, ou à `REFRESH`."""

    def _run(self, captures, *, refresh):
        published = []
        operations = mock.Mock()
        operations.publish_peek.side_effect = lambda *a: published.append(a[3]) or True
        payloads = iter(captures)
        done = threading.Event()

        def capture(self_, cfg):
            try:
                return next(payloads)
            except StopIteration:
                done.set()
                self_.stop.wait(5)
                return captures[-1]

        with mock.patch.object(peek, "INTERVAL", 0.001), \
                mock.patch.object(peek, "REFRESH", refresh), \
                mock.patch.object(peek.Publisher, "_capture", capture), \
                mock.patch.object(peek.db_mod, "connect", return_value=mock.Mock()), \
                mock.patch.object(peek.storage, "of",
                                  return_value=types.SimpleNamespace(operations=operations)):
            publisher = peek.Publisher(config.Config(), agent="w", owner="r", epoch=1,
                                       cwd=lambda: "", reader=lambda: None,
                                       active=lambda: False)
            self.assertTrue(done.wait(10))
            n = len(published)
            publisher.close()
            publisher.thread.join(timeout=10)
        return n, published

    def test_identique_non_reecrit(self):
        same = {"workspace": {}, "events": []}
        n, _ = self._run([same] * 6, refresh=3600)
        self.assertEqual(n, 1)

    def test_changement_ecrit(self):
        a, b = {"events": [1]}, {"events": [2]}
        n, published = self._run([a, a, b, b, a], refresh=3600)
        self.assertEqual(published[:n], [a, b, a])

    def test_fermeture_ecrit_la_derniere_capture(self):
        same = {"events": []}
        _, published = self._run([same] * 3, refresh=3600)
        self.assertEqual(len(published), 2)  # la première, puis celle de fermeture


if __name__ == "__main__":
    unittest.main()
