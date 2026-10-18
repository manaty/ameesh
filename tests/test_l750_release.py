# SPDX-License-Identifier: AGPL-3.0-only
"""L750/L756 (#561/#566) : le périmètre d'une release et sa porte.

Un lot de release fige son périmètre à un commit **nommé** de sa cible ; les
fusions postérieures vont à la release suivante. `ameesh release check` refuse
une branche dont la base a bougé sans raison enregistrée, et l'absence d'une
exécution de **suite complète** (poussée ou lancement manuel, jamais le plan
impacté d'une PR) verte sur la **tête fraîche** exacte — un commit postérieur
au gel (correctif cherry-pické) doit être certifié lui-même, jamais par la CI
de l'ancien gel.

Tests sans base : dépôt git réel, journal du lot simulé, `gh` simulé.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import release, work

from .test_l160b_radar import _git, make_repo
from .test_l160d_resolutions import _identity


class Ledger:
    """Le journal du lot que `release` lit et écrit, en mémoire."""

    def __init__(self, lots):
        self.lots = {int(r["id"]): dict(r) for r in lots}
        self.notes: list = []
        self.marks: dict = {}

    def get(self, item_id):
        return self.lots.get(int(item_id))

    def note(self, item_id, text, actor=""):
        self.notes.append((int(item_id), text, actor))
        return True

    def events(self, item_id, limit=100):
        rows = [{"note": text, "actor": actor}
                for lot, text, actor in reversed(self.notes) if lot == int(item_id)]
        return rows[:limit]

    def milestones(self, item_id, limit=100):
        return list(self.marks.get(int(item_id), []))[:limit]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l750-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        _identity(self.repo)
        # une branche de release coupée de main, et le périmètre = ce main
        _git(self.repo, "checkout", "-q", "-b", "release/1.0", "main")
        with open(os.path.join(self.repo, "release.txt"), "w", encoding="utf-8") as fh:
            fh.write("1.0\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "release 1.0")
        _git(self.repo, "checkout", "-q", "main")
        self.perimeter = _git(self.repo, "rev-parse", "main")
        self.ledger = Ledger([{"id": 750, "app": "ameesh", "type": "release",
                               "state": "build", "branch": "release/1.0",
                               "branch_target": "main", "title": "release 1.0"},
                              {"id": 1, "app": "ameesh", "type": "improvement",
                               "state": "build", "branch": "lot/1-a",
                               "branch_target": "main", "title": "not a release"}])
        self.ledger.marks[750] = [
            {"id": 1, "kind": "frozen", "sha": self.tip("release/1.0"), "at_ts": 1.0}]
        for patch in (
                mock.patch.object(work, "get",
                                  side_effect=lambda db, n: self.ledger.get(n)),
                mock.patch.object(work, "note",
                                  side_effect=lambda db, n, text, actor="":
                                  self.ledger.note(n, text, actor)),
                mock.patch.object(work, "events",
                                  side_effect=lambda db, n, *a, **k:
                                  self.ledger.events(n)),
                mock.patch.object(work, "milestones",
                                  side_effect=lambda db, n, *a, **k:
                                  self.ledger.milestones(n))):
            patch.start()
            self.addCleanup(patch.stop)

    def tip(self, ref):
        return _git(self.repo, "rev-parse", ref)

    def advance_main(self):
        _git(self.repo, "checkout", "-q", "main")
        with open(os.path.join(self.repo, "main.txt"), "w", encoding="utf-8") as fh:
            fh.write("later\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "main moved")
        _git(self.repo, "checkout", "-q", "-")

    def absorb_main(self):
        """La branche de release fusionne le main d'après le gel."""
        _git(self.repo, "checkout", "-q", "release/1.0")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "absorb main", "main")
        _git(self.repo, "checkout", "-q", "main")

    def check(self, lot=750, **kw):
        kw.setdefault("repo", self.repo)
        kw.setdefault("full_ci", lambda sha: {"runs": 1, "green": True,
                                              "pending": [], "failed": []})
        return release.check(None, lot, **kw)


class PerimeterTest(_Base):
    def test_the_perimeter_is_recorded_once_by_commit_id(self):
        self.assertEqual(release.perimeter(None, 750), "")
        base = release.record_perimeter(None, 750, self.perimeter)
        self.assertEqual(base, self.perimeter)
        self.assertEqual(release.perimeter(None, 750), self.perimeter)
        # idempotent pour le même commit, abrégé compris
        self.assertEqual(release.record_perimeter(None, 750, self.perimeter[:12]),
                         self.perimeter)
        with self.assertRaisesRegex(release.ReleaseError, "already freezes"):
            release.record_perimeter(None, 750, "b" * 40)

    def test_a_bad_commit_id_is_refused(self):
        for value in ("", "main", "12345", "z" * 40):
            with self.subTest(value=value):
                with self.assertRaisesRegex(release.ReleaseError, "not a commit id"):
                    release.record_perimeter(None, 750, value)

    def test_an_unknown_lot_is_refused(self):
        with self.assertRaisesRegex(release.ReleaseError, "not found"):
            release.record_perimeter(None, 999, self.perimeter)
        with self.assertRaisesRegex(release.ReleaseError, "not found"):
            release.record_reason(None, 999, "because")

    def test_a_reason_needs_text_and_is_journaled(self):
        with self.assertRaisesRegex(release.ReleaseError, "reason is required"):
            release.record_reason(None, 750, "   ")
        release.record_reason(None, 750, "hotfix 1.0.1", actor="orch")
        self.assertEqual(release.reason(None, 750), "hotfix 1.0.1")
        self.assertEqual(self.ledger.notes[-1],
                         (750, "release-base-moved: hotfix 1.0.1", "orch"))


class BaseMovedTest(_Base):
    def test_a_branch_that_did_not_absorb_the_target_is_frozen(self):
        self.advance_main()
        out = release.base_moved(self.repo, "release/1.0", "main", self.perimeter)
        self.assertFalse(out["moved"])
        self.assertEqual(out["base"], self.perimeter)

    def test_a_branch_that_absorbed_the_target_has_moved(self):
        self.advance_main()
        self.absorb_main()
        out = release.base_moved(self.repo, "release/1.0", "main", self.perimeter)
        self.assertTrue(out["moved"])
        self.assertNotEqual(out["base"], self.perimeter)

    def test_a_rebased_branch_has_moved_too(self):
        self.advance_main()
        moved_main = self.tip("main")
        _git(self.repo, "checkout", "-q", "release/1.0")
        _git(self.repo, "rebase", "-q", "main")
        _git(self.repo, "checkout", "-q", "main")
        out = release.base_moved(self.repo, "release/1.0", "main", self.perimeter)
        self.assertTrue(out["moved"])
        self.assertEqual(out["base"], moved_main)

    def test_an_unreadable_branch_target_or_perimeter_is_refused(self):
        with self.assertRaisesRegex(release.ReleaseError, "branch nope"):
            release.base_moved(self.repo, "nope", "main", self.perimeter)
        with self.assertRaisesRegex(release.ReleaseError, "target nope"):
            release.base_moved(self.repo, "release/1.0", "nope", self.perimeter)
        with self.assertRaisesRegex(release.ReleaseError, "not a commit"):
            release.base_moved(self.repo, "release/1.0", "main", "b" * 40)


class CheckTest(_Base):
    def test_a_lot_without_a_perimeter_does_not_open(self):
        with self.assertRaisesRegex(release.ReleaseError, "no frozen release perimeter"):
            self.check()

    def test_a_lot_that_is_not_a_release_is_refused(self):
        with self.assertRaisesRegex(release.ReleaseError, "not a release lot"):
            self.check(lot=1)

    def test_a_release_branch_is_enough_without_a_release_type(self):
        self.ledger.lots[1]["branch"] = "release/1.0"
        self.ledger.marks[1] = [{"id": 1, "kind": "frozen",
                                 "sha": self.tip("release/1.0"), "at_ts": 1.0}]
        release.record_perimeter(None, 1, self.perimeter)
        out = self.check(lot=1)
        self.assertEqual((out["lot"], out["moved"]), (1, False))

    def test_a_moved_base_without_a_reason_is_refused(self):
        release.record_perimeter(None, 750, self.perimeter)
        self.advance_main()
        self.absorb_main()
        with self.assertRaisesRegex(release.ReleaseError, "absorbed main after the freeze"):
            self.check()
        release.record_reason(None, 750, "hotfix cherry-picked, main absorbed once")
        out = self.check()
        self.assertTrue(out["moved"])
        self.assertEqual(out["reason"], "hotfix cherry-picked, main absorbed once")

    def test_the_frozen_head_must_name_a_commit_of_the_repository(self):
        release.record_perimeter(None, 750, self.perimeter)
        self.ledger.marks[750] = []
        with self.assertRaisesRegex(release.ReleaseError, "not frozen"):
            self.check()
        self.ledger.marks[750] = [{"id": 1, "kind": "frozen", "sha": "b" * 40,
                                   "at_ts": 1.0}]
        with self.assertRaisesRegex(release.ReleaseError, "not a commit of"):
            self.check()

    def test_the_full_suite_must_be_green_on_the_head(self):
        release.record_perimeter(None, 750, self.perimeter)
        cases = (({"runs": 0, "green": False, "pending": [], "failed": []},
                  "no full-suite run"),
                 ({"runs": 1, "green": False, "pending": ["tests"], "failed": []},
                  "still running"),
                 ({"runs": 1, "green": False, "pending": [], "missing": [".github/w.yml"]},
                  "did not run"),
                 ({"runs": 1, "green": False, "pending": [], "failed": ["tests"]},
                  "is red"),
                 ({"runs": 1, "green": False, "pending": [], "failed": []},
                  "not green"))
        for runs, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(release.ReleaseError, expected):
                    self.check(full_ci=lambda sha, runs=runs: runs)
        seen = []
        out = self.check(full_ci=lambda sha: (seen.append(sha) or
                                              {"runs": 1, "green": True,
                                               "pending": [], "failed": []}))
        self.assertEqual(seen[-1], self.tip("release/1.0"))
        self.assertTrue(out["full_suite"]["green"])
        self.assertEqual(out["frozen_sha"], self.tip("release/1.0"))
        self.assertEqual(out["certified"], self.tip("release/1.0"))
        self.assertFalse(out["head_moved"])
        self.assertEqual(len(release.render(out)), 6)

    def test_a_commit_after_the_freeze_is_certified_on_itself(self):
        """B2 (R1 auditeur/Codex PR #633) : un commit postérieur au gel sur la
        branche de release ne peut pas s'abriter derrière la CI de l'ancien
        gel ; c'est la tête fraîche qui est certifiée."""
        release.record_perimeter(None, 750, self.perimeter)
        frozen = self.tip("release/1.0")
        _git(self.repo, "checkout", "-q", "release/1.0")
        with open(os.path.join(self.repo, "hotfix.txt"), "w", encoding="utf-8") as fh:
            fh.write("hotfix\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "hotfix after the freeze")
        head = self.tip("release/1.0")
        _git(self.repo, "checkout", "-q", "main")
        self.assertNotEqual(head, frozen)
        seen = []

        def reader(sha):
            seen.append(sha)
            if sha == frozen:
                return {"runs": 1, "green": True, "pending": [], "failed": []}
            return {"runs": 0, "green": False, "pending": [], "failed": []}

        with self.assertRaisesRegex(release.ReleaseError, "no full-suite run"):
            self.check(full_ci=reader)
        self.assertEqual(seen[-1], head)  # la tête fraîche, pas l'ancien gel
        out = self.check(full_ci=lambda sha: {"runs": 1, "green": True,
                                              "pending": [], "failed": []})
        self.assertEqual(out["tip"], head)
        self.assertEqual(out["frozen_sha"], frozen)
        self.assertTrue(out["head_moved"])
        self.assertIn("MOVED after the freeze", "\n".join(release.render(out)))

    def test_without_a_repository_the_check_refuses(self):
        release.record_perimeter(None, 750, self.perimeter)
        with self.assertRaisesRegex(release.ReleaseError, "has no repository"):
            release.check(None, 750)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
