# SPDX-License-Identifier: AGPL-3.0-only
"""Repository identity, read-only radar and atomic legacy reservation import."""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from ameesh import db as db_mod, migration_reservations as reservations, radar, storage, work
from ameesh.storage.postgres.work import WorkItems

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

    def test_stale_collision_rolls_back_the_entire_import(self):
        # Inject a stale holder observation: the repository lock serializes
        # even legacy SQL writers through trigger 0419. The second reservation
        # must still roll back the first if an unexpected unique collision occurs.
        other = self.lot("Concurrent holder", 3)
        conn = db_mod.connect(self.cfg)
        self.addCleanup(conn.close)
        with conn.transaction() as tx:
            tx.execute("UPDATE work_items SET migration = 324, migration_repo = %s"
                       " WHERE id = %s", (self.slug, other))
        with mock.patch.object(WorkItems, "migration_holder", return_value=None):
            with self.assertRaises(db_mod.DbError):
                self.run_import(apply=True)
        self.assertIsNone(work.get(self.db, self.a)["migration"])
        self.assertIsNone(work.get(self.db, self.b)["migration"])
        self.assertEqual(work.get(self.db, other)["migration"], 324)

    def test_import_locks_repository_before_mapped_lots(self):
        conn = self.connect()
        self.addCleanup(conn.close)
        errors, pids = [], []

        def run_import():
            try:
                with conn.transaction() as tx:
                    pids.append(tx.query("SELECT pg_backend_pid() AS pid")[0]["pid"])
                    reservations.import_reservations(
                        tx, self.rows, self.mapping, repo=self.slug, actor="orch", apply=True)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_import)
        try:
            with self.db.transaction() as tx:
                tx.execute("SET LOCAL lock_timeout = '5s'")
                storage.of(tx).migration_reservations.lock_target("github.com/" + self.slug, "main")
                thread.start()
                deadline = time.monotonic() + 5
                while not pids or not tx.query(
                        "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory'"
                        " AND NOT granted", (pids[0],)):
                    self.assertFalse(errors, errors)
                    self.assertLess(time.monotonic(), deadline, "import never waited for repository lock")
                    time.sleep(0.01)
                storage.of(tx).work.reserve_migration(
                    self.a, self.slug, 323, 0, actor="orch", note="reserved %(number)s")
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([work.get(self.db, lot)["migration"] for lot in (self.a, self.b)], [323, 324])

    def test_concurrent_reservation_waits_for_the_import_repository_lock(self):
        # #711: pendant la transaction d'import, un second écrivain coopérant
        # attend le verrou consultatif du dépôt (observé non accordé dans
        # pg_locks) au lieu de courir vers l'index unique ; une fois l'import
        # commité, il prend le numéro suivant sans collision.
        other = self.lot("Waiting holder", 3)
        original = WorkItems.reserve_migration
        results, errors, backend, threads, hooked = [], [], [], [], []

        def wait_writer():
            conn = db_mod.connect(self.cfg)
            try:
                # Le pid est relevé dans la transaction persistante qui va
                # attendre : le pilote psql ouvre une session par transaction.
                with conn.transaction() as tx:
                    backend.append(tx.query("SELECT pg_backend_pid() AS pid")[0]["pid"])
                    results.append(original(storage.of(tx).work, other, self.slug, None, 0,
                                            actor="other", note="waiting %(number)s")["migration"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        def race(registry, item_id, *args, **kwargs):
            if item_id == self.b and not hooked:
                hooked.append(True)
                thread = threading.Thread(target=wait_writer)
                threads.append(thread)
                thread.start()
                deadline = time.monotonic() + 5
                while not backend and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(backend, "second writer never connected")
                waited = False
                while time.monotonic() < deadline:
                    waited = bool(self.db.query(
                        "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory'"
                        " AND NOT granted", (backend[0],)))
                    if waited:
                        break
                    time.sleep(0.01)
                self.assertTrue(waited, "second writer never waited for the repository lock")
            return original(registry, item_id, *args, **kwargs)

        with mock.patch.object(WorkItems, "reserve_migration", new=race):
            self.run_import(apply=True)
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [325])
        self.assertEqual(work.get(self.db, self.a)["migration"], 323)
        self.assertEqual(work.get(self.db, self.b)["migration"], 324)
        self.assertEqual(work.get(self.db, other)["migration"], 325)

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
