# SPDX-License-Identifier: AGPL-3.0-only
"""#8236 — la porte lit le dépôt que la marque machine du gel désigne.

Un lot du canon (`app` ameesh, gel dans `manaty/home`) n'a pas de `pr_ref`
avant sa fermeture : `repository()` ne reconnaît pas le canon, et
`repo_for_freeze` ne trouve aucun clone qui tient le SHA. Le gel porte
pourtant `ameesh-freeze-repo: manaty/home` (#2603/#6735). La porte doit
l'utiliser — au verdict comme à la synchro (`sync_validations`) — au lieu de
rester « review gate inconclusive ».
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue, storage, work

from .support import PgTestCase


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(cwd, path, text, message):
    full = os.path.join(cwd, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    _git(cwd, "add", path)
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def canon_clone(testcase):
    """Un clone du canon `manaty/home`, avec une cible `main` et un gel.

    L'origine est nommée `manaty/home` (comme le clone de l'hôte) alors que
    les objets viennent d'un dépôt local : aucun réseau.
    """
    tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
    testcase.addCleanup(tmp.cleanup)
    root = tmp.name
    remote = os.path.join(root, "home.git")
    _git(root, "init", "-q", "--bare", "-b", "main", remote)
    checkout = os.path.join(root, "canon")
    _git(root, "clone", "-q", remote, checkout)
    _git(checkout, "checkout", "-q", "-b", "main")
    base = _commit(checkout, "processus/lots.md", "base\n", "base")
    _git(checkout, "push", "-q", "origin", "main")
    _git(checkout, "checkout", "-q", "-b", "auditeur/8236-regles")
    head = _commit(checkout, "processus/lots.md", "gel\n", "gel")
    _git(checkout, "push", "-q", "origin", "auditeur/8236-regles")
    _git(checkout, "remote", "set-url", "origin",
         "https://github.com/manaty/home.git")
    return types.SimpleNamespace(root=root, path=checkout, base=base, head=head)


def canon_cfg(clone):
    """La configuration d'un hôte dont le canon est ce clone."""
    return types.SimpleNamespace(
        canon=clone.path, canon_ref="", canon_untrusted=False,
        extra_canons=(), radar={}, packet_watch={}, release={}, host="test")


class FreezeRepoMarkerTest(unittest.TestCase):
    """La piste du dépôt nommé par le gel, et la porte qui la reçoit."""

    def setUp(self):
        self.clone = canon_clone(self)
        self.cfg = canon_cfg(self.clone)
        self.item = {"id": 8236, "app": "ameesh", "branch": "auditeur/8236-regles",
                     "branch_target": "main", "assignee": ""}

    def marks(self, note):
        return [{"id": 1, "kind": "frozen", "sha": self.clone.head,
                 "actor": "auteur", "at_ts": 1.0, "note": note}]

    def gate(self, marks):
        seen = {}

        def of_lot(db, lot, **kw):
            seen.update(kw)
            if not kw.get("repo"):
                return None
            return {"class": "light",
                    "files": [{"path": "processus/lots.md", "class": "light"}],
                    "frozen_sha": kw.get("head"), "doubt": False,
                    "repo": kw["repo"]}

        with mock.patch.object(lot_class, "of_lot", side_effect=of_lot), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}):
            found = merge_queue.review_gate_for_item(
                None, self.cfg, dict(self.item), marks)
        return found, seen

    def test_named_repo_paths_only_keeps_the_named_repository(self):
        self.assertEqual(
            lot_class.named_repo_paths(self.cfg, self.item, "manaty/home"),
            [self.clone.path])
        self.assertEqual(
            lot_class.named_repo_paths(self.cfg, self.item, "manaty/nexlink"), [])

    def test_gate_reads_the_repository_named_by_the_freeze_marker(self):
        found, seen = self.gate(self.marks(
            ("PR manaty/home#1\n"
             "ameesh-freeze-repo: manaty/home")))
        self.assertIsNotNone(found)
        self.assertTrue(found.get("ok"))
        self.assertEqual(seen.get("repo"), self.clone.path)
        self.assertEqual(seen.get("head"), self.clone.head)

    def test_gate_without_the_marker_keeps_the_old_refusal(self):
        # Contrôle : sans la marque, aucun clone de l'hôte ne tient ce SHA et
        # la porte ne conclut pas — le correctif n'ouvre rien par défaut.
        found, seen = self.gate(self.marks("PR manaty/home#1"))
        self.assertIsNone(found)
        self.assertIsNone(seen.get("repo"))


class SyncValidationsCanonLotTest(PgTestCase):
    """La synchro promeut le lot quand la marque nomme un dépôt lisible."""

    def setUp(self):
        super().setUp()
        self.clone = canon_clone(self)
        self.lot = work.add(self.db, title="porte canon", app="ameesh")["id"]
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        self.ops = storage.of(self.db).work
        # Les jalons dans une transaction : la synchro automatique de l'ajout
        # ne juge pas avant que le test ait posé sa configuration.
        with self.db.transaction() as tx:
            ops = type(self.ops)(tx)
            ops.add_milestone(
                self.lot, "frozen", sha=self.clone.head, actor="auteur",
                verdict=None,
                note=("PR manaty/home#1\n"
                      "ameesh-freeze-repo: manaty/home"),
                proof=None)
            ops.add_milestone(
                self.lot, "verdict", sha=self.clone.head, actor="relecteur",
                verdict="ok", note="", proof=None)

    def _patches(self):
        def of_lot(db, lot, **kw):
            if not kw.get("repo"):
                return None
            return {"class": "light",
                    "files": [{"path": "processus/lots.md", "class": "light"}],
                    "frozen_sha": kw.get("head"), "doubt": False,
                    "repo": kw["repo"]}

        return (mock.patch.object(lot_class, "of_lot", side_effect=of_lot),
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}))

    def test_sync_validations_promotes_the_canon_lot(self):
        self.db.cfg = dataclasses.replace(self.cfg, canon=self.clone.path,
                                          canon_ref="")
        self.addCleanup(setattr, self.db, "cfg", self.cfg)
        of_lot, gate = self._patches()
        with of_lot, gate:
            results = self.ops.sync_validations()
        self.assertFalse([r for r in results if r.get("error")], results)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")

    def test_sync_validations_without_the_named_clone_stays_pending(self):
        # Contrôle : la marque seule ne suffit pas ; sans clone lisible, la
        # porte reste fermée et le lot garde sa trace « validation pending ».
        of_lot, gate = self._patches()
        with of_lot, gate:
            results = self.ops.sync_validations()
        self.assertFalse([r for r in results if r.get("error")], results)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        notes = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE 'validation pending (#6257):%%'", (self.lot,))
        self.assertEqual(len(notes), 1)
        self.assertIn('"gate": null', notes[0]["note"])
