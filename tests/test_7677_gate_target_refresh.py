# SPDX-License-Identifier: AGPL-3.0-only
"""#7677 — la porte de validation lit la même cible que la désignation au gel.

#7659 (gel 32bcec67, une note `docs/design` seule) : la désignation a fetch la
cible (`review_target.resolve`) et classé un fichier, light, un avis. La porte
de validation (`review_gate_for_item`, #6257) lisait le `origin/main` local du
clone qui résolvait le gel, en retard de quatre jours : le merge-base reculait,
453 chemins (`.github/`, `deploy/`) entraient dans le diff, classe sensitive,
deux avis. Le même lot avait deux exigences selon l'hôte qui le lisait.

Témoins :
(1) clone périmé, sans rafraîchir : la porte lit l'ancienne cible (le défaut) ;
(2) `refresh_target=True` : la porte lit la cible fraîche, la même que
    `prepare_target` de la désignation, et le diff ne porte que la note ;
(3) fetch impossible : la cible locale est gardée (plus stricte), sans lever ;
(4) les écrivains de `validated` (stockage, `work.move`) rafraîchissent hors
    transaction, jamais dans une transaction.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon, lot_class, merge_queue, review_designation_activation as rda, work
from ameesh.storage.postgres import work as pg_work

NOTE = "docs/design/7659-note.md"
CRITICAL = "deploy/serveur/install.sh"


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(cwd, path, text, message):
    full = os.path.join(cwd, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as handle:
        handle.write(text)
    _git(cwd, "add", path)
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


class _Clones(unittest.TestCase):
    """Un dépôt distant, un clone à jour (l'auteur) et un clone périmé (l'hôte)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.remote = os.path.join(root, "remote.git")
        _git(root, "init", "-q", "--bare", "-b", "main", self.remote)
        author = os.path.join(root, "author")
        _git(root, "clone", "-q", self.remote, author)
        _git(author, "checkout", "-q", "-b", "main")
        self.old = _commit(author, "README.md", "a\n", "old main")
        _git(author, "push", "-q", "origin", "main")
        stale = os.path.join(root, "stale")
        _git(root, "clone", "-q", self.remote, stale)
        self.stale = stale
        # La cible avance d'un chemin critique après le clone de l'hôte.
        self.fresh = _commit(author, CRITICAL, "x\n", "deploy merged")
        _git(author, "push", "-q", "origin", "main")
        _git(author, "checkout", "-q", "-b", "lot/7659-note")
        self.head = _commit(author, NOTE, "note\n", "design note")
        _git(author, "push", "-q", "origin", "lot/7659-note")
        # L'hôte relit la branche du lot, pas la cible : `origin/main` reste
        # à l'ancien commit, comme ameesh-dev1 le 17/11 à 04:42.
        _git(stale, "fetch", "-q", "origin",
             "+refs/heads/lot/7659-note:refs/remotes/origin/lot/7659-note")
        self.assertEqual(_git(stale, "rev-parse", "origin/main"), self.old)
        self.item = {"id": 7659, "app": "ameesh", "branch": "lot/7659-note",
                     "branch_target": "main", "assignee": "impl-04"}
        self.marks = [{"id": 1, "kind": "frozen", "sha": self.head,
                       "actor": "impl-04", "at_ts": 1.0}]

    def gate_read(self, **kwargs):
        """La lecture de `review_gate_for_item` sur le clone périmé : la classe
        reçoit la base lue, la porte elle-même n'est pas l'objet du témoin."""
        seen = {}

        def of_lot(db, lot, **kw):
            seen.update(kw)
            paths = canon.git_range_paths(kw["repo"], kw["base_sha"], kw["head"])
            seen["paths"] = paths
            return {"class": "light", "files": [{"path": p, "class": "light"}
                                                for p in paths],
                    "frozen_sha": kw["head"], "doubt": False}

        with mock.patch.object(lot_class, "repo_for_freeze",
                               return_value=(self.stale, self.head)), \
                mock.patch.object(lot_class, "default_base", return_value="main"), \
                mock.patch.object(lot_class, "of_lot", side_effect=of_lot), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}):
            merge_queue.review_gate_for_item(None, None, self.item, self.marks,
                                             **kwargs)
        return seen


class GateTargetRefreshTest(_Clones):

    def test_stale_clone_without_refresh_reads_the_old_target(self):
        seen = self.gate_read()
        self.assertEqual(seen["base_sha"], self.old)
        self.assertIn(CRITICAL, seen["paths"])

    def test_refresh_reads_the_designation_target(self):
        seen = self.gate_read(refresh_target=True)
        with mock.patch.object(rda, "_repository", return_value=self.stale), \
                mock.patch.object(lot_class, "default_base", return_value="main"):
            target = rda.prepare_target(None, None, self.item, self.head)
        self.assertEqual(target.sha, self.fresh)
        self.assertEqual(seen["base_sha"], target.sha)
        self.assertEqual(seen["paths"], [NOTE])
        # La désignation lit le même diff : une seule exigence.
        self.assertEqual(canon.git_range_paths(self.stale, target.sha, self.head),
                         seen["paths"])

    def test_cache_only_never_fetches(self):
        with mock.patch.object(merge_queue, "_refreshed_target") as refresh:
            merge_queue.review_gate_for_item(None, None, self.item, self.marks,
                                             refresh_target=True, cache_only=True)
        refresh.assert_not_called()

    def test_failed_fetch_keeps_the_local_target(self):
        _git(self.stale, "remote", "set-url", "origin",
             os.path.join(self.tmp.name, "missing.git"))
        seen = self.gate_read(refresh_target=True)
        self.assertEqual(seen["base_sha"], self.old)
        self.assertIn(CRITICAL, seen["paths"])


class ValidationWritersRefreshTest(unittest.TestCase):
    """Les écrivains de `validated` passent `refresh_target` hors transaction."""

    def call_storage(self, in_tx):
        store = pg_work.WorkItems.__new__(pg_work.WorkItems)
        store.db = mock.Mock()
        with mock.patch.object(pg_work, "in_transaction", return_value=in_tx), \
                mock.patch.object(merge_queue, "review_gate_for_item",
                                  return_value={"ok": True}) as gate:
            store._validation_gate(1, [], item={"id": 1})
        return gate.call_args.kwargs["refresh_target"]

    def test_storage_refreshes_outside_a_transaction_only(self):
        self.assertTrue(self.call_storage(False))
        self.assertFalse(self.call_storage(True))

    def test_work_move_to_validated_refreshes_outside_a_transaction(self):
        item = {"id": 5, "state": "qa", "app": "ameesh", "workflow_ref": None}
        with mock.patch("ameesh.db.in_transaction", return_value=False), \
                mock.patch.object(work, "get", return_value=item), \
                mock.patch.object(work, "milestones", return_value=[]), \
                mock.patch.object(merge_queue, "review_gate_for_item",
                                  return_value={"ok": False}) as gate:
            with self.assertRaises(work.WorkError):
                work.move(mock.Mock(), 5, "validated", actor="pilot")
        self.assertTrue(gate.call_args.kwargs["refresh_target"])


if __name__ == "__main__":
    unittest.main()
