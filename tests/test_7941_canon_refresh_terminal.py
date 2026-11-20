# SPDX-License-Identifier: AGPL-3.0-only
"""#7941 — canon refresh : un avis terminal par gel, les échecs transitoires muets.

Deux faux négatifs se répétaient à chaque époque du canon :

- une cible supprimée à la forge (branche fusionnée puis effacée) était
  re-notée et re-courriérée à chaque signature ;
- une tête poussée seulement sur une branche `review/*` de la forge, absente
  des clones locaux, n'était pas rapatriée par le fetch ciblé.

Les témoins couvrent les trois attendus du lot : la tête présente seulement à
l'origine est trouvée ; l'échec terminal n'alerte qu'une fois à travers deux
signatures ; l'échec transitoire reste muet jusqu'à la borne.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import review_canon_refresh as refresh
from ameesh import review_designation_activation as act
from ameesh import review_target, work
from ameesh.work_reviewers import designated

from . import test_2447_review_designation_activation as base
from .support import PgTestCase


def _git(root: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class OriginOnlyHeadTest(unittest.TestCase):
    """La tête gelée n'existe qu'à l'origine, sur une branche review/*."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t7941-origin-")
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.origin = os.path.join(root, "ameesh.git")
        subprocess.run(["git", "init", "-q", "--bare", self.origin], check=True)
        seed = os.path.join(root, "seed")
        subprocess.run(["git", "init", "-q", seed], check=True)
        _git(seed, "config", "user.name", "t")
        _git(seed, "config", "user.email", "t@example.invalid")
        with open(os.path.join(seed, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "base")
        self.base = _git(seed, "rev-parse", "HEAD")
        _git(seed, "push", "-q", self.origin, "HEAD:refs/heads/main")
        with open(os.path.join(seed, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("frozen\n")
        _git(seed, "commit", "-qam", "frozen")
        self.frozen = _git(seed, "rev-parse", "HEAD")
        # Une branche review/* seulement à la forge : le clone local, restreint
        # à main, ne verra jamais cette tête par un fetch ordinaire.
        _git(seed, "push", "-q", self.origin, "HEAD:refs/heads/review/lot-7941")
        self.clone = os.path.join(root, "clone")
        subprocess.run(["git", "clone", "-q", "--branch", "main", "--single-branch",
                        "file://" + self.origin, self.clone],
                       check=True, capture_output=True)
        _git(self.clone, "config", "remote.origin.url",
             "https://github.com/manaty/ameesh.git")
        _git(self.clone, "config",
             "url.file://%s.insteadOf" % self.origin,
             "https://github.com/manaty/ameesh.git")
        self.session = os.path.join(root, "session")
        os.makedirs(self.session)
        self.cfg = types.SimpleNamespace(host="h", radar={}, canon="")
        self.item = {"id": 7941, "app": "ameesh", "assignee": "author",
                     "branch": "review/lot-7941",
                     "issue_ref": "manaty/ameesh#6175"}

    def _repository(self, *, fetch=True):
        work_store = mock.Mock()
        work_store.milestones.return_value = []
        with mock.patch.object(act.registry, "get",
                               return_value={"cwd": self.clone}), \
                mock.patch.object(act, "_tracked_worktrees", return_value=[]), \
                mock.patch.object(act.storage, "of",
                                  return_value=types.SimpleNamespace(work=work_store)), \
                mock.patch.object(act.os, "getcwd", return_value=self.session):
            return act._repository(object(), self.cfg, self.item, self.frozen,
                                   fetch=fetch)

    def test_the_frozen_head_is_fetched_from_the_forge(self):
        # Avant le fetch, aucune ref locale ni aucun objet de la tête.
        with self.assertRaises(AssertionError):
            _git(self.clone, "cat-file", "-e", self.frozen)
        self.assertEqual(self._repository(), os.path.realpath(self.clone))
        self.assertEqual(_git(self.clone, "cat-file", "-t", self.frozen), "commit")

    def test_the_transaction_read_never_fetches(self):
        self.assertEqual(self._repository(fetch=False), "")
        with self.assertRaises(AssertionError):
            _git(self.clone, "cat-file", "-e", self.frozen)


class DeletedTargetClassTest(unittest.TestCase):
    """`review_target.resolve` distingue une ref disparue d'une panne réseau."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t7941-target-")
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.origin = os.path.join(root, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.origin], check=True)
        seed = os.path.join(root, "seed")
        subprocess.run(["git", "init", "-q", seed], check=True)
        _git(seed, "config", "user.name", "t")
        _git(seed, "config", "user.email", "t@example.invalid")
        with open(os.path.join(seed, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "base")
        _git(seed, "branch", "-M", "develop")
        _git(seed, "remote", "add", "origin", self.origin)
        _git(seed, "push", "-q", "origin", "develop")
        self.reader = os.path.join(root, "reader")
        subprocess.run(["git", "clone", "-q", "-b", "develop", self.origin,
                        self.reader], check=True, capture_output=True)

    def test_a_deleted_branch_is_terminal(self):
        _git(self.reader, "push", "-q", "origin", "develop:refs/heads/review/gone")
        _git(self.reader, "fetch", "-q", "origin")
        _git(self.reader, "push", "-q", "origin", ":refs/heads/review/gone")
        with self.assertRaises(review_target.TargetError) as caught:
            review_target.resolve(self.reader, "review/gone")
        self.assertTrue(caught.exception.terminal, caught.exception)
        self.assertIn("cannot refresh target", str(caught.exception))

    def test_an_unreachable_forge_is_transient(self):
        _git(self.reader, "remote", "set-url", "origin", self.origin + "-missing")
        with self.assertRaises(review_target.TargetError) as caught:
            review_target.resolve(self.reader, "develop")
        self.assertFalse(caught.exception.terminal, caught.exception)

    def test_prepare_target_propagates_the_terminal_flag(self):
        item = {"id": 7941, "app": "ameesh", "assignee": "author"}
        cfg = types.SimpleNamespace(host="h", radar={}, canon="")
        with mock.patch.object(act, "_repository", return_value=self.reader), \
                mock.patch.object(act.lot_class, "default_base", return_value="develop"), \
                mock.patch.object(review_target, "resolve",
                                  side_effect=review_target.TargetError(
                                      "cannot refresh target develop: gone",
                                      terminal=True)):
            result = act.prepare_target(cfg, None, item, "a" * 40)
        self.assertIsInstance(result, act.Unavailable)
        self.assertTrue(result.terminal)


class TerminalNoticeTest(base._ActivationFixture, PgTestCase):
    """Le contrôle du canon n'alerte qu'une fois par gel, pas par signature."""

    def setUp(self):
        super().setUp()
        self.canon = base._Canon(
            os.path.join(self.tmp, "one-review"),
            federation=base.FEDERATION.replace("            reviews: 2",
                                               "            reviews: 1"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root,
                                       canon_ref="HEAD")

    def raise_signature(self):
        self.canon.update("index.md", "# New canon revision\n")

    def raise_policy(self):
        self.canon.update("federation.yaml", base.FEDERATION)

    @staticmethod
    def unavailable(*, terminal):
        return act.Unavailable(
            "cannot refresh target codex/home-server-console: %s; "
            "stale target refused"
            % ("branch disappeared at origin" if terminal
               else "git fetch failed"),
            terminal=terminal)

    def notes_matching(self, needle):
        return [note for note in self.notes(refresh.PREFIX) if needle in note]

    def test_terminal_failure_alerts_once_across_two_canon_signatures(self):
        self.freeze()
        before = len(self.mailbox())
        with mock.patch.object(act, "prepare_target",
                               return_value=self.unavailable(terminal=True)):
            first = refresh.after_sync(self.cfg, self.db)
            self.raise_signature()
            second = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(first["errors"], [], first)
        self.assertEqual(second["errors"], [], second)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]],
                         ["pilot"])
        self.assertEqual(len(self.notes_matching(":terminal:")), 1)
        # L'époque suivante rejoue la lecture sans renvoyer de courrier.
        with mock.patch.object(act, "prepare_target",
                               return_value=self.unavailable(terminal=True)):
            again = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(again["errors"], [], again)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]],
                         ["pilot"])

    def test_transient_failure_is_silent_until_the_bound(self):
        self.freeze()
        before = len(self.mailbox())
        with mock.patch.object(act, "prepare_target",
                               return_value=self.unavailable(terminal=False)):
            for _ in range(refresh.TRANSIENT_NOTICE_AFTER - 1):
                result = refresh.after_sync(self.cfg, self.db)
                self.assertEqual(result["errors"], [], result)
            self.assertEqual(self.mailbox()[before:], [])
            self.assertEqual(self.notes_matching(":terminal:"), [])
            last = refresh.after_sync(self.cfg, self.db)
            self.assertEqual(last["errors"], [], last)
            again = refresh.after_sync(self.cfg, self.db)
            self.assertEqual(again["errors"], [], again)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]],
                         ["pilot"])
        self.assertEqual(len(self.notes_matching(":transient:")), 1)
        self.assertEqual(self.notes_matching(":terminal:"), [])

    def test_waiting_alerts_once_across_canon_signatures(self):
        _, initial = self.freeze()
        first = initial["reviewers"][0]["name"]
        other = "r2" if first == "r1" else "r1"
        self.db.execute("UPDATE agent_registry SET status = 'stopped'"
                        " WHERE name = %s", (other,))
        self.raise_policy()
        before = len(self.mailbox())
        first_pass = refresh.after_sync(self.cfg, self.db)
        self.raise_signature()
        second_pass = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(first_pass["errors"], [], first_pass)
        self.assertEqual(second_pass["errors"], [], second_pass)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]],
                         ["pilot"])
        self.assertEqual(len(self.notes_matching(":waiting:")), 1)
        self.assertEqual(designated(work.get(self.db, self.lot)), [first])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
