# SPDX-License-Identifier: AGPL-3.0-only
"""Repository identity, read-only radar and atomic legacy reservation import."""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import db as db_mod, migration_reservations as reservations, radar, storage, work
from ameesh.storage.postgres.work import WorkItems
from ameesh.storage.postgres.migration_reservations import MigrationReservations

from .support import PgTestCase

TEXT = '''| Number | Lot | Who | State |
|---|---|---|---|
| **323** | Billing (#87) | agent-a | **réservé (2026-10-10)** |
| 324 | Routing | agent-b | reserved |
| 325 | Done | agent-c | fusionné |
| 326 | Unused | agent-d | libéré |
'''


class MarkdownTest(unittest.TestCase):
    def test_only_active_rows_and_no_issue_number_inference(self):
        rows = reservations.parse_markdown(TEXT)
        self.assertEqual([r["number"] for r in rows], [323, 324])
        self.assertEqual(rows[0]["label"], "Billing (#87)")

    def test_ambiguous_rows_refused(self):
        for text in (TEXT + '| 323 | Other | agent | reserved |',
                     '| 327 | Other | agent | unclear |',
                     '| 327 | Other | reserved |'):
            with self.subTest(text=text):
                with self.assertRaises(work.WorkError):
                    reservations.parse_markdown(text)


class ReservationImportTest(PgTestCase):
    slug = "acme/product"

    def setUp(self):
        super().setUp()
        self.a = self.lot("Billing", 1)
        self.b = self.lot("Routing", 2)
        self.rows = reservations.parse_markdown(TEXT)
        self.mapping = {"323": self.a, "324": self.b}
        self.project = radar.Project("product", "/repo", migrations="supabase/migrations", github=self.slug)
        self.layout = mock.patch("ameesh.migrations.layout_for", return_value=object())
        self.layout.start()
        self.addCleanup(self.layout.stop)

    def lot(self, title, issue, slug=None):
        return work.add(self.db, title=title, issue_ref="%s#%s" % (slug or self.slug, issue),
                        actor="orch")["id"]

    def run_import(self, mapping=None, **kwargs):
        return reservations.import_reservations(self.db, self.rows,
                                               self.mapping if mapping is None else mapping,
                                               repo=self.slug, actor="orch", **kwargs)

    def test_default_is_read_only_and_apply_is_idempotent(self):
        events = self.db.query("SELECT count(*) AS n FROM work_item_events")[0]["n"]
        preview = self.run_import()
        self.assertEqual([r["action"] for r in preview], ["reserve", "reserve"])
        self.assertIsNone(work.get(self.db, self.a)["migration"])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM work_item_events")[0]["n"], events)
        self.assertEqual([r["action"] for r in self.run_import(apply=True)], ["reserved", "reserved"])
        events = self.db.query("SELECT count(*) AS n FROM work_item_events")[0]["n"]
        self.assertEqual([r["action"] for r in self.run_import(apply=True)], ["existing", "existing"])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM work_item_events")[0]["n"], events)

    def test_mapping_is_explicit_complete_and_repository_scoped(self):
        self.assertEqual([r["action"] for r in self.run_import({})], ["unmapped", "unmapped"])
        other = self.lot("Other", 3, "acme/other")
        for mapping in ({}, {"323": self.a}, {"323": self.a, "324": self.a},
                        {"323": self.a, "324": other}, {"323": self.a, "325": self.b},
                        {"323": True, "324": self.b}, {"323": 1.5, "324": self.b},
                        {"323": self.a, "0323": self.b, "324": other}):
            with self.subTest(mapping=mapping):
                with self.assertRaises(work.WorkError):
                    self.run_import(mapping, apply=True)
        self.assertIsNone(work.get(self.db, self.a)["migration"])

    def test_unreadable_registry_is_not_taken_as_a_clean_probe(self):
        branch = {"work_item": self.a, "target": "develop",
                  "added": ["supabase/migrations/323_billing.sql"]}
        with mock.patch.object(WorkItems, "migration_holder", side_effect=db_mod.DbError("unavailable")):
            from ameesh.canon import GitError
            with self.assertRaisesRegex(GitError, "registry unreadable"):
                reservations.probe(self.db, self.project, [branch])

    def test_collision_and_existing_lot_reservation_not_overwritten(self):
        other = self.lot("Other", 3)
        storage.of(self.db).work.reserve_migration(other, self.slug, 324, 0,
                                                  actor="orch", note="reserved %(number)s")
        with self.assertRaisesRegex(work.WorkError, "already held"):
            self.run_import(apply=True)
        self.assertIsNone(work.get(self.db, self.a)["migration"])
        storage.of(self.db).work.reserve_migration(self.a, self.slug, 327, 0,
                                                  actor="orch", note="reserved %(number)s")
        with self.assertRaisesRegex(work.WorkError, "different reservation"):
            self.run_import(apply=True)
        self.assertEqual(work.get(self.db, self.a)["migration"], 327)

    def test_concurrent_collision_rechecked_after_repository_lock(self):
        other = self.lot("Concurrent holder", 3)
        conn = db_mod.connect(self.cfg)
        self.addCleanup(conn.close)
        original = MigrationReservations.lock_target
        raced = []

        def race(registry, *args):
            if not raced:
                raced.append(True)
                WorkItems(conn).reserve_migration(other, self.slug, 324, 0,
                    actor="other", note="concurrent %(number)s")
            return original(registry, *args)

        with mock.patch.object(MigrationReservations, "lock_target", new=race):
            with self.assertRaisesRegex(work.WorkError, "already held"):
                self.run_import(apply=True)
        self.assertTrue(raced)
        self.assertIsNone(work.get(self.db, self.a)["migration"])
        self.assertIsNone(work.get(self.db, self.b)["migration"])
        self.assertEqual(work.get(self.db, other)["migration"], 324)

    def test_second_write_failure_rolls_back_the_entire_import(self):
        original = WorkItems.reserve_migration
        written = []

        def fail_second(registry, item_id, *args, **kwargs):
            if item_id == self.b:
                self.assertEqual(written, [self.a])
                raise db_mod.DbError("second reservation rejected")
            result = original(registry, item_id, *args, **kwargs)
            written.append(item_id)
            return result

        with mock.patch.object(WorkItems, "reserve_migration", new=fail_second):
            with self.assertRaisesRegex(db_mod.DbError, "second reservation rejected"):
                self.run_import(apply=True)
        self.assertEqual(written, [self.a])
        self.assertIsNone(work.get(self.db, self.a)["migration"])
        self.assertIsNone(work.get(self.db, self.b)["migration"])

    def test_probe_checks_owner_and_repository_and_reports_new_files_only(self):
        self.run_import(apply=True)
        branch = {"work_item": self.a, "target": "develop", "added": [
            "supabase/migrations/323_billing.sql", "supabase/migrations/324_wrong_owner.sql",
            "supabase/migrations/328_missing.sql", "supabase/migrations/RESERVATIONS.md",
            "other/329_ignore.sql"]}
        found = reservations.probe(self.db, self.project, [branch])
        self.assertEqual([r["number"] for r in found], ["324", "328"])
        self.assertEqual(found[0]["holder"], self.b)
        other_project = radar.Project("other", "/r", migrations="supabase/migrations", github="acme/other")
        self.assertEqual(len(reservations.probe(self.db, other_project, [branch])), 3)
        report = {"unreserved_migrations": found}
        self.assertEqual(radar.summary(report)["unreserved_migrations"], 2)
        self.assertEqual(len(radar.changes({}, report)), 2)
        conflict = next(iter(radar.conflicts_of(report).values()))
        self.assertIn("ameesh work migration", radar.message_text(conflict, self.a, {}))
        self.assertIn("unreserved migration", "\n".join(radar.render(report)))
