# SPDX-License-Identifier: AGPL-3.0-only
"""#2766 — la porte d'un véhicule de paquet lit les avis par lot membre.

Un paquet (`integration/**`) compose des lots déjà relus sur leur tête gelée.
Classer le diff gelé entier faisait demander deux avis au véhicule dès qu'une
migration d'un membre le rendait sensible, contre le §11 bis (« verdicts
acquis : ni gel répété, ni second avis sur delta »). Les témoins ci-dessous
prennent les trois cas du constat : paquet de lots relus avec une migration
(aucun avis de véhicule), résolution non mécanique (un avis sur ce delta), lot
membre sans avis (refus).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue, packet_reviews

from .support import patch_families
from .test_l160b_radar import _git, git_env


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo, branch, lot, source=None):
    """Une composition comme #2157 : fusion --no-ff, trailer `ameesh-work`."""
    source = source or _git(repo, "rev-parse", branch)
    _git(repo, "merge", "-q", "--no-ff", branch, "-m",
         "Merge #%d — %s\n\nameesh-work: %d" % (lot, source[:12], lot))
    return _git(repo, "rev-parse", "HEAD")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2766-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch_families(self)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, "README.md", "base\n")
        _commit_all(self.repo, "base")
        self.base = _git(self.repo, "rev-parse", "main")

    def member(self, branch, path, text):
        _git(self.repo, "checkout", "-q", "-b", branch, "main")
        _write(self.repo, path, text)
        sha = _commit_all(self.repo, "member %s" % branch)
        _git(self.repo, "checkout", "-q", "main")
        return sha

    def compose(self):
        """Paquet sans conflit : migration d'un membre, code de l'autre."""
        self.m1 = self.member("m1", "supabase/migrations/528_x.sql", "select 1;\n")
        self.m2 = self.member("m2", "src/app.py", "print(1)\n")
        _merge(self.repo, "m1", 101)
        _merge(self.repo, "m2", 102)
        self.head = _git(self.repo, "rev-parse", "HEAD")


class CompositionMembersTest(_Base):
    def test_members_come_from_the_composition_trailers(self):
        self.compose()
        found = packet_reviews.members(None, self.repo, self.base, self.head, 999)
        self.assertEqual([entry["lot"] for entry in found], [101, 102])
        self.assertEqual({entry["source"] for entry in found}, {self.m1, self.m2})

    def test_a_member_migration_and_code_are_not_the_vehicle_delta(self):
        self.compose()
        own = packet_reviews.own_changes(self.repo, self.base, self.head,
                                         [self.m1, self.m2])
        self.assertEqual(own["paths"], [])
        self.assertEqual(own["commits"], [])

    def test_a_manifest_is_not_a_reviewed_delta(self):
        self.compose()
        _write(self.repo, "reviews/integration-paquet-h-1700.md",
               "# Paquet\n\n| lot | source | commit |\n|---|---|---|\n")
        self.head = _commit_all(self.repo, "h-1700 : manifeste")
        own = packet_reviews.own_changes(self.repo, self.base, self.head,
                                         [self.m1, self.m2])
        self.assertEqual(own["paths"], [])

    def test_a_test_adaptation_is_not_a_reviewed_delta(self):
        self.compose()
        _write(self.repo, "tests/test_2102.py", "# fixture\n")
        self.head = _commit_all(self.repo, "h-1700 : adapte un test")
        own = packet_reviews.own_changes(self.repo, self.base, self.head,
                                         [self.m1, self.m2])
        self.assertEqual(own["paths"], [])

    def test_a_clean_merge_with_an_extra_change_is_the_delta(self):
        self.compose()
        # la fusion est propre, mais son auteur y ajoute un contenu que le
        # membre n'apporte pas : c'est un delta propre, pas du bruit.
        _write(self.repo, "README.md", "post-composition\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "--amend", "--no-edit")
        head = _git(self.repo, "rev-parse", "HEAD")
        own = packet_reviews.own_changes(self.repo, self.base, head,
                                         [self.m1, self.m2])
        self.assertEqual(own["paths"], ["README.md"])


class ResolutionDeltaTest(_Base):
    def setUp(self):
        super().setUp()
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "README.md", "m1\n")
        self.m1 = _commit_all(self.repo, "m1")
        _git(self.repo, "checkout", "-q", "-b", "m2", "main")
        _write(self.repo, "README.md", "m2\n")
        self.m2 = _commit_all(self.repo, "m2")
        _git(self.repo, "checkout", "-q", "main")
        _merge(self.repo, "m1", 101)
        # conflit de composition, résolu par un contenu qui n'est ni m1 ni m2
        proc = subprocess.run(["git", "-C", self.repo, "merge", "-q", "--no-ff", "m2",
                               "-m", "Merge #102 — m2"], capture_output=True,
                              env=git_env())
        self.assertNotEqual(proc.returncode, 0)
        _write(self.repo, "README.md", "resolved\n")
        self.resolution = _commit_all(
            self.repo, "Merge #102 — m2\n\nameesh-work: 102")
        self.head = self.resolution

    def test_the_resolution_is_the_only_vehicle_delta(self):
        own = packet_reviews.own_changes(self.repo, self.base, self.head,
                                         [self.m1, self.m2])
        self.assertEqual(own["paths"], ["README.md"])
        self.assertEqual(own["commits"], [self.resolution])

    def test_a_resolution_that_copies_a_parent_is_still_a_judgment(self):
        # reproduction B de cx1 (#2766) : recopier intégralement ours ou theirs
        # est un choix, pas une déduction ; le chemin reste à relire.
        for parent in (self.m1, self.m2):
            with self.subTest(parent=parent):
                _git(self.repo, "checkout", parent, "--", "README.md")
                _git(self.repo, "commit", "-q", "--amend", "--no-edit")
                head = _git(self.repo, "rev-parse", "HEAD")
                own = packet_reviews.own_changes(
                    self.repo, self.base, head, [self.m1, self.m2])
                self.assertEqual(own["paths"], ["README.md"])
                self.assertEqual(own["commits"], [head])


class DesignationTest(_Base):
    """Au gel, la désignation ne lit que le delta propre du véhicule."""

    class _NoLandings:
        def query(self, _sql, _args=()):
            return []

    def designation(self, rows, marks, head):
        with mock.patch.object(packet_reviews.work, "get",
                               side_effect=lambda _db, lot: {"id": lot, "app": "ameesh"}):
            with mock.patch.object(packet_reviews.work, "milestones",
                                   side_effect=lambda _db, lot: marks[lot]):
                return packet_reviews.designation_changes(
                    self.db(rows), 999, {"id": 999, "app": "ameesh",
                                         "branch": "integration/paquet"},
                    self.repo, self.base, head)

    @staticmethod
    def db(rows):
        class DB:
            def query(self, _sql, _args=()):
                return rows
        return DB()

    def test_a_reviewed_migration_packet_has_no_vehicle_path(self):
        self.compose()
        rows = [{"work_item_id": 101, "pinned_sha": self.m1},
                {"work_item_id": 102, "pinned_sha": self.m2}]
        marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1, "at_ts": 1.0}],
                 102: [{"id": 2, "kind": "frozen", "sha": self.m2, "at_ts": 2.0}]}
        own = self.designation(rows, marks, self.head)
        self.assertEqual(own["paths"], [])
        self.assertEqual(own["commits"], [])

    def test_a_non_vehicle_is_left_to_the_historical_diff(self):
        self.assertIsNone(packet_reviews.designation_changes(
            self._NoLandings(), 1, {"id": 1, "branch": "lot/1-x"},
            self.repo, self.base, self.base))


class GateTest(_Base):
    FAMILIES = {"author": "deepseek", "rev1": "codex"}

    def gate(self, vehicle, actors=()):
        head = "a" * 40
        marks = [{"kind": "verdict", "verdict": "ok", "sha": head, "actor": actor,
                  "at_ts": index, "id": index + 1}
                 for index, actor in enumerate(actors)]
        klass = {"class": "sensitive",
                 "files": [{"path": "supabase/migrations/528_x.sql",
                            "class": "sensitive"}]}
        with mock.patch.object(merge_queue, "actor_family",
                               side_effect=lambda _db, name, **_: self.FAMILIES.get(
                                   str(name or "").strip(), "")):
            return merge_queue.review_gate(None, marks, head, klass=klass,
                                           item={"assignee": "author"},
                                           vehicle=vehicle)

    def test_a_reviewed_packet_with_a_migration_needs_no_vehicle_verdict(self):
        out = self.gate({"ok": True, "required": 0, "klass": None,
                         "why": "2 constituents, no proper delta"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["required"], 0)

    def test_a_member_without_a_verdict_refuses_the_vehicle(self):
        out = self.gate({"ok": False, "required": None, "klass": None,
                         "why": "constituent #101: no review verdict ok"})
        self.assertFalse(out["ok"])
        self.assertIn("constituent #101", out["why"])

    def test_a_non_mechanical_resolution_needs_one_vehicle_verdict(self):
        vehicle = {"ok": None, "required": 1, "klass": {"class": "normal"}}
        self.assertFalse(self.gate(vehicle)["ok"])
        self.assertTrue(self.gate(vehicle, ["rev1"])["ok"])

    def test_a_blocked_verdict_still_refuses_a_vehicle_without_delta(self):
        head = "a" * 40
        marks = [{"kind": "verdict", "verdict": "blocked", "sha": head,
                  "actor": "rev1", "at_ts": 1.0, "id": 1}]
        with mock.patch.object(merge_queue, "actor_family", return_value="codex"):
            out = merge_queue.review_gate(
                None, marks, head, klass={"class": "sensitive"},
                item={"assignee": "author"},
                vehicle={"ok": True, "required": 0, "klass": None})
        self.assertFalse(out["ok"])
        self.assertIn("blocked", out["why"])


class NeedTest(_Base):
    """`need` compose la lecture des membres et du delta propre."""

    class _Landings:
        def __init__(self, rows):
            self.rows = rows

        def query(self, _sql, _args=()):
            return self.rows

    def need(self, rows, marks, head, gate=None, klass=None):
        db = self._Landings(rows)
        with mock.patch.object(packet_reviews.work, "get",
                               side_effect=lambda _db, lot: {"id": lot, "app": "ameesh"}):
            with mock.patch.object(packet_reviews.work, "milestones",
                                   side_effect=lambda _db, lot: marks[lot]):
                with mock.patch.object(lot_class, "of_lot",
                                       return_value=klass or {"class": "light",
                                                              "files": [], "doubt": False}):
                    with mock.patch.object(merge_queue, "review_gate",
                                           return_value=gate or {"ok": True}):
                        return packet_reviews.need(
                            db, 999, {"id": 999, "app": "ameesh",
                                      "branch": "integration/paquet"},
                            marks[101], self.repo, self.base, head, cfg=None)

    def test_a_packet_of_reviewed_lots_has_no_vehicle_requirement(self):
        self.compose()
        rows = [{"work_item_id": 101, "pinned_sha": self.m1},
                {"work_item_id": 102, "pinned_sha": self.m2}]
        marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1, "at_ts": 1.0}],
                 102: [{"id": 2, "kind": "frozen", "sha": self.m2, "at_ts": 2.0}]}
        out = self.need(rows, marks, self.head)
        self.assertIsNotNone(out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["required"], 0)
        self.assertEqual(out["members"], [
            {"lot": 101, "source": self.m1}, {"lot": 102, "source": self.m2}])

    def test_a_non_mechanical_resolution_requires_its_own_delta_review(self):
        self.setUpResolution()
        rows = [{"work_item_id": 101, "pinned_sha": self.m1},
                {"work_item_id": 102, "pinned_sha": self.m2}]
        marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1, "at_ts": 1.0}],
                 102: [{"id": 2, "kind": "frozen", "sha": self.m2, "at_ts": 2.0}]}
        out = self.need(rows, marks, self.head)
        self.assertEqual(out["ok"], None)
        self.assertEqual(out["required"], 1)
        self.assertEqual(out["own_paths"], ["README.md"])

    def test_a_member_without_a_verdict_is_refused(self):
        self.compose()
        rows = [{"work_item_id": 101, "pinned_sha": self.m1},
                {"work_item_id": 102, "pinned_sha": self.m2}]
        marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1, "at_ts": 1.0}],
                 102: [{"id": 2, "kind": "frozen", "sha": self.m2, "at_ts": 2.0}]}
        out = self.need(rows, marks, self.head,
                        gate={"ok": False, "why": "no verdict on the freeze"})
        self.assertFalse(out["ok"])
        self.assertIn("constituent #101", out["why"])

    def test_a_landing_not_yet_composed_is_not_a_constituent(self):
        # Une déclaration d'atterrissage peut précéder la composition : elle
        # ne doit pas faire refuser le véhicule où le lot n'est pas encore.
        self.compose()
        _git(self.repo, "checkout", "-q", "-b", "pending", "main")
        _write(self.repo, "src/pending.py", "print(2)\n")
        pending = _commit_all(self.repo, "pending member")
        _git(self.repo, "checkout", "-q", "main")
        rows = [{"work_item_id": 101, "pinned_sha": self.m1},
                {"work_item_id": 102, "pinned_sha": self.m2},
                {"work_item_id": 103, "pinned_sha": pending}]
        marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1, "at_ts": 1.0}],
                 102: [{"id": 2, "kind": "frozen", "sha": self.m2, "at_ts": 2.0}],
                 103: [{"id": 3, "kind": "frozen", "sha": pending, "at_ts": 3.0}]}
        out = self.need(rows, marks, self.head)
        self.assertTrue(out["ok"])
        self.assertEqual(out["required"], 0)

    def test_a_composed_source_must_carry_the_frozen_head(self):
        self.compose()
        rows = [{"work_item_id": 101, "pinned_sha": self.m1}]
        # le membre a été regelé après la composition : le véhicule porte une
        # tête différente de celle qui a été relue.
        _write(self.repo, "supabase/migrations/528_x.sql", "select 2;\n")
        later = _commit_all(self.repo, "re-freeze after composition")
        marks = {101: [{"id": 1, "kind": "frozen", "sha": later, "at_ts": 1.0}]}
        out = self.need(rows, marks, self.head)
        self.assertFalse(out["ok"])
        self.assertIn("does not carry its frozen head", out["why"])

    def setUpResolution(self):
        # même composition conflictuelle que ResolutionDeltaTest, sur ce banc.
        _git(self.repo, "checkout", "-q", "-b", "r1", "main")
        _write(self.repo, "README.md", "r1\n")
        self.m1 = _commit_all(self.repo, "r1")
        _git(self.repo, "checkout", "-q", "-b", "r2", "main")
        _write(self.repo, "README.md", "r2\n")
        self.m2 = _commit_all(self.repo, "r2")
        _git(self.repo, "checkout", "-q", "main")
        _merge(self.repo, "r1", 101)
        subprocess.run(["git", "-C", self.repo, "merge", "-q", "--no-ff", "r2",
                        "-m", "Merge #102 — r2"], capture_output=True, env=git_env())
        _write(self.repo, "README.md", "resolved\n")
        self.head = _commit_all(self.repo, "Merge #102 — r2\n\nameesh-work: 102")


if __name__ == "__main__":
    unittest.main()
