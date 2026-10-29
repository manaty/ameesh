# SPDX-License-Identifier: AGPL-3.0-only
"""#2400: composition renumbers only the migrations a merge adds.

Real Git trees, no SQL and no database. The L122 allocator is a fixture: its
own PostgreSQL behaviour belongs to the existing reservation tests.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest

from ameesh import train, train_migrations, work

from .support import PgTestCase
from .test_l160b_radar import _git, _write
from .test_l122_numeros_de_migration import NEXLINK, _commit, _depot


class Recorder:
    def __init__(self):
        self.calls = []

    def reserve(self, row, path, old, number, target):
        self.calls.append((row["work_item_id"], old, number))


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2400-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, "supabase/migrations/516_base.sql", "-- 516 base\n")
        _write(self.repo, "supabase/rollback/516_restore.sql", "-- 516 rollback\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "base 516")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.project = types.SimpleNamespace(
            migrations="supabase/migrations", github="manaty/nexlink")
        self.layout = train_migrations.layout(self.project)
        self.row = dict(work_item_id=1, branch="lot/1", generation=1, target="main",
                        frozen_id=1, frozen_sha="0" * 40, source="github.com/o/r")

    def merge(self, branch, files):
        """A lot branch with `files`, merged --no-ff into main; returns its head."""
        _git(self.repo, "checkout", "-q", "-b", branch, self.base)
        for path, text in files.items():
            _write(self.repo, path, text)
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", branch)
        frozen = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge " + branch, branch)
        return frozen, _git(self.repo, "rev-parse", "HEAD")

    def files(self):
        return {
            "supabase/migrations/525_media_bound.sql":
                "-- 525 — borne la purge\nselect 'adr525-keep';\n",
            "supabase/rollback/525_restore.sql":
                "-- 525 — rollback de la purge\n-- psql -f supabase/rollback/525_restore.sql\n",
            "supabase/tests/525_media_bound_test.sql":
                "-- 525 — test de la purge\n-- see 525_restore.sql\n",
            "backend/service.ts":
                "// migration 525: see 525_restore.sql\nconst tag = 'adr525-keep';\n",
        }

    def test_renumber_continues_the_target_and_keeps_code_literals(self):
        _, head = self.merge("lot/1", self.files())
        out = train_migrations.rewrite(self.repo, self.base, head, self.layout, self.row)
        self.assertEqual(out["migrations"], [{
            "work_item_id": 1, "reserved": 525, "landed": 517,
            "files": [
                {"from": "supabase/migrations/525_media_bound.sql",
                 "to": "supabase/migrations/517_media_bound.sql"},
                {"from": "supabase/rollback/525_restore.sql",
                 "to": "supabase/rollback/517_restore.sql"},
                {"from": "supabase/tests/525_media_bound_test.sql",
                 "to": "supabase/tests/517_media_bound_test.sql"},
            ]}])
        migration = _git(self.repo, "show",
                         out["tree"] + ":supabase/migrations/517_media_bound.sql")
        self.assertIn("-- 517 — borne la purge", migration)
        self.assertIn("adr525-keep", migration)
        rollback = _git(self.repo, "show",
                        out["tree"] + ":supabase/rollback/517_restore.sql")
        self.assertIn("517_restore.sql", rollback)
        self.assertIn("-- 517 — rollback", rollback)
        service = _git(self.repo, "show", out["tree"] + ":backend/service.ts")
        self.assertIn("// migration 525: see 517_restore.sql", service)
        self.assertIn("adr525-keep", service)
        with self.assertRaises(AssertionError):
            _git(self.repo, "show", out["tree"] + ":supabase/migrations/525_media_bound.sql")
        train_migrations.audit(self.repo, self.base, out["tree"], self.layout)

    def test_reconstruction_uses_the_persisted_numbers(self):
        _, head = self.merge("lot/1", self.files())
        first = train_migrations.rewrite(self.repo, self.base, head, self.layout,
                                         self.row, allocator=Recorder())
        _git(self.repo, "checkout", "-q", "-f", "--detach", head)
        again = train_migrations.rewrite(
            self.repo, self.base, head, self.layout, self.row,
            expected=first["migrations"], allocator=Recorder())
        self.assertEqual(again["tree"], first["tree"])
        self.assertEqual(again["migrations"], first["migrations"])
        changed = [dict(first["migrations"][0], landed=518)]
        _git(self.repo, "checkout", "-q", "-f", "--detach", head)
        with self.assertRaisesRegex(train.TrainError, "no longer belongs"):
            train_migrations.rewrite(self.repo, self.base, head, self.layout,
                                     self.row, expected=changed)

    def test_two_migrations_take_consecutive_numbers_with_their_own_files(self):
        """#2481 : un lot à deux migrations les renumérote toutes les deux."""
        _, head = self.merge("lot/1", {
            "supabase/migrations/524_a.sql": "-- 524 a\n",
            "supabase/migrations/525_b.sql": "-- 525 b\n",
            "supabase/rollback/525_b_restore.sql": "-- 525 rollback\n",
        })
        recorder = Recorder()
        out = train_migrations.rewrite(self.repo, self.base, head, self.layout,
                                       self.row, allocator=recorder)
        self.assertEqual([entry["reserved"] for entry in out["migrations"]], [524, 525])
        self.assertEqual([entry["landed"] for entry in out["migrations"]], [517, 518])
        self.assertEqual(out["migrations"][0]["files"], [
            {"from": "supabase/migrations/524_a.sql",
             "to": "supabase/migrations/517_a.sql"}])
        self.assertEqual(out["migrations"][1]["files"], [
            {"from": "supabase/migrations/525_b.sql",
             "to": "supabase/migrations/518_b.sql"},
            {"from": "supabase/rollback/525_b_restore.sql",
             "to": "supabase/rollback/518_b_restore.sql"}])
        self.assertEqual(recorder.calls, [(1, 524, 517), (1, 525, 518)])
        self.assertIn("-- 518", _git(self.repo, "show",
                                     out["tree"] + ":supabase/migrations/518_b.sql"))
        train_migrations.audit(self.repo, self.base, out["tree"], self.layout)

    def test_overlapping_renames_preserve_contents_and_references(self):
        _write(self.repo, "supabase/migrations/518_base.sql", "-- base\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "base 518")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _, head = self.merge("lot/1", {
            "supabase/migrations/518_step.sql": "select 'first';\n",
            "supabase/migrations/519_step.sql": "select 'second';\n",
            "backend/refs.txt": "518_step.sql 519_step.sql\n",
        })
        out = train_migrations.rewrite(self.repo, self.base, head, self.layout, self.row)
        for number, content in ((519, "first"), (520, "second")):
            self.assertIn(content, _git(self.repo, "show", out["tree"]
                                       + ":supabase/migrations/%s_step.sql" % number))
        self.assertEqual(_git(self.repo, "show", out["tree"] + ":backend/refs.txt"),
                         "519_step.sql 520_step.sql")
        train_migrations.audit(self.repo, self.base, out["tree"], self.layout)

    def test_history_is_never_changed(self):
        _, changed = self.merge("lot/2", {"supabase/migrations/516_base.sql": "-- changed\n"})
        with self.assertRaisesRegex(train.MemberDeferred, "historical migration"):
            train_migrations.rewrite(self.repo, self.base, changed, self.layout,
                                     dict(self.row, work_item_id=2, branch="lot/2",
                                          generation=2, frozen_id=2))

    def test_already_contiguous_number_is_reserved_without_a_rewrite(self):
        _, head = self.merge("lot/1", {
            "supabase/migrations/517_media.sql": "-- 517 media\n",
            "supabase/rollback/517_restore.sql": "-- 517 rollback\n",
        })
        recorder = Recorder()
        out = train_migrations.rewrite(self.repo, self.base, head, self.layout,
                                       self.row, allocator=recorder)
        self.assertEqual(out["tree"], _git(self.repo, "rev-parse", head + "^{tree}"))
        self.assertEqual(out["migrations"][0]["files"], [])
        self.assertEqual(recorder.calls, [(1, 517, 517)])
        train_migrations.audit(self.repo, self.base, out["tree"], self.layout)

    def test_audit_refuses_a_non_contiguous_composition(self):
        _, head = self.merge("lot/1", self.files())
        with self.assertRaisesRegex(train.TrainError, "not contiguous"):
            train_migrations.audit(self.repo, self.base, head, self.layout)

    def test_historical_file_type_changes_are_refused_by_every_guard(self):
        name = "supabase/migrations/516_base.sql"
        os.remove(os.path.join(self.repo, name))
        os.symlink("../../replacement.sql", os.path.join(self.repo, name))
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "replace historical SQL with symlink")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(train.MemberDeferred, "historical migration"):
            train_migrations.rewrite(self.repo, self.base, head, self.layout, self.row)
        with self.assertRaisesRegex(train.MemberDeferred, "historical migration"):
            train_migrations.guard_transport(
                self.repo, self.base, head, self.layout, self.row)
        with self.assertRaisesRegex(train.TrainError, "historical migration"):
            train_migrations.audit(self.repo, self.base, head, self.layout)

    def test_a_transport_carrying_a_migration_is_deferred(self):
        _, head = self.merge("lot/1", {
            "supabase/migrations/517_media.sql": "-- 517 media\n"})
        with self.assertRaisesRegex(train.MemberDeferred, "qualified transport"):
            train_migrations.guard_transport(self.repo, self.base, head,
                                             self.layout, self.row)

    def test_a_transport_without_a_migration_delta_is_left_to_its_qualification(self):
        _, head = self.merge("lot/1", {"backend/service.ts": "export {};\n"})
        train_migrations.guard_transport(self.repo, self.base, head,
                                         self.layout, self.row)

    def test_destination_collision_defers_the_member(self):
        _, head = self.merge("lot/1", {
            "supabase/migrations/525_media.sql": "-- 525\n",
            "supabase/rollback/525_restore.sql": "-- 525\n",
            "supabase/rollback/517_restore.sql": "-- already there\n",
        })
        with self.assertRaisesRegex(train.MemberDeferred, "destination"):
            train_migrations.rewrite(self.repo, self.base, head, self.layout, self.row)


class AllocatorTest(PgTestCase):
    """The L122 move before publication and the release after landing."""

    def setUp(self):
        super().setUp()
        self.repo = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                           NEXLINK.directory, ["516_base.sql"],
                           {"lot/9-migration": ["525_stale.sql"]})
        # The train composes the member in its own checkout: its migration is
        # present in the worktree but is not reserved yet (#2400).
        with open(os.path.join(self.repo, NEXLINK.directory, "525_stale.sql"), "w") as fh:
            fh.write("-- 525\n")
        self.layout = train_migrations.layout(types.SimpleNamespace(
            migrations=NEXLINK.directory, github="manaty/nexlink"))
        self.lot = work.add(self.db, title="lot 9", issue_ref="manaty/nexlink#2400",
                            actor="test", branch="lot/9-migration",
                            branch_target="develop")
        self.row = dict(work_item_id=self.lot["id"], branch="lot/9-migration")

    def test_reservation_moves_before_publication_then_lands_and_is_released(self):
        train_migrations.Allocator(self.db).reserve(
            self.row, self.repo, 525, 517, self.layout)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual((item["migration"], item["migration_repo"]), (517, "manaty/nexlink"))

        base = _git(self.repo, "rev-parse", "HEAD")
        os.remove(os.path.join(self.repo, NEXLINK.directory, "525_stale.sql"))
        _write(self.repo, NEXLINK.directory + "/517_stale.sql", "-- 517\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm",
             "Merge lot 9\n\nameesh-work: %d\n" % self.lot["id"])
        head = _git(self.repo, "rev-parse", "HEAD")
        # The promotion reached the remote; the private clone's local branch
        # is still on the base until the landing bookkeeping aligns it.
        _git(self.repo, "update-ref", "refs/heads/develop", base)
        plan = dict(target="develop", base=base, head=head,
                    migrations=[dict(work_item_id=self.lot["id"],
                                     reserved=525, landed=517)])
        errors = train_migrations.Allocator(self.db).landed(plan, path=self.repo)
        self.assertEqual(errors, [])
        item = work.get(self.db, self.lot["id"])
        self.assertEqual(item["state"], "delivered")
        self.assertEqual((item["migration"], item["migration_landed"]), (None, 517))
        self.assertEqual(_git(self.repo, "rev-parse", "develop"), head)

    def test_the_train_reads_the_target_it_composes(self):
        """#2620 : le train passe la cible qu'il compose ; le défaut du clone
        (`main`, qui porte 517) ne refuse pas un numéro libre sur `develop`."""
        _git(self.repo, "checkout", "-q", "-b", "main", "develop")
        _commit(self.repo, NEXLINK.directory, "517_main.sql")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "config", "ameesh.target", "main")
        lot = work.add(self.db, title="lot 2620", issue_ref="manaty/nexlink#2620",
                       actor="test", branch="lot/2620")
        row = dict(work_item_id=lot["id"], branch="lot/2620", target="develop")
        train_migrations.Allocator(self.db).reserve(row, self.repo, 525, 517, self.layout)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["migration"], item["migration_repo"]), (517, "manaty/nexlink"))

    def test_two_reservations_land_and_are_both_released(self):
        """#2481 : le plan porte une correspondance par migration ; le lot est
        fermé une fois et tous ses créneaux sont rendus."""
        allocator = train_migrations.Allocator(self.db)
        allocator.reserve(self.row, self.repo, 525, 517, self.layout)
        with open(os.path.join(self.repo, NEXLINK.directory, "526_more.sql"), "w") as fh:
            fh.write("-- 526\n")
        allocator.reserve(self.row, self.repo, 526, 518, self.layout)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual([row["number"] for row in item["migrations"]], [517, 518])
        base = _git(self.repo, "rev-parse", "HEAD")
        os.remove(os.path.join(self.repo, NEXLINK.directory, "525_stale.sql"))
        os.remove(os.path.join(self.repo, NEXLINK.directory, "526_more.sql"))
        _write(self.repo, NEXLINK.directory + "/517_stale.sql", "-- 517\n")
        _write(self.repo, NEXLINK.directory + "/518_more.sql", "-- 518\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm",
             "Merge lot 9\n\nameesh-work: %d\n" % self.lot["id"])
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "update-ref", "refs/heads/develop", base)
        plan = dict(target="develop", base=base, head=head, migrations=[
            dict(work_item_id=self.lot["id"], reserved=525, landed=517),
            dict(work_item_id=self.lot["id"], reserved=526, landed=518),
        ])
        errors = allocator.landed(plan, path=self.repo)
        self.assertEqual(errors, [])
        item = work.get(self.db, self.lot["id"])
        self.assertEqual(item["state"], "delivered")
        self.assertEqual((item["migration"], item["migration_landed"]), (None, 517))
        self.assertEqual(item["migrations"], [])
        notes = [event["note"] for event in work.events(self.db, self.lot["id"])]
        self.assertTrue(any("migration mapping 525, 526 -> 517, 518" in note
                            for note in notes), notes)
        self.assertEqual(_git(self.repo, "rev-parse", "develop"), head)
