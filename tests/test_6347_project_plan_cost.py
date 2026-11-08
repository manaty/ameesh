# SPDX-License-Identifier: AGPL-3.0-only
"""The plan snapshot retains the assignment guards on a shared open set."""
from tests.support import PgTestCase
from ameesh import storage, work
from ameesh.storage.postgres.work_footprints import (
    cap_reached_sql, in_flight_sql, overlap_exists_sql,
)


class SnapshotGuardsTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")
        self.ids = []
        for state, app, assignee, source, branch, kind in (
            ('intake', ' acme ', None, '', '', 'bug'),
            ('intake', 'acme', 'holder', '', '', 'evolution'),
            ('build', 'acme', None, '', '', 'evolution'),
            ('intake', 'acme', 'holder', '', '', 'improvement'),
            ('build', 'acme', 'holder', '', 'origin/integration/p', 'bug'),
            ('build', 'acme', 'holder', 'decision', '', 'bug'),
            ('build', 'acme', 'holder', 'file-decoupage:x', '', 'bug'),
            ('closed', 'acme', 'holder', '', '', 'bug'),
            ('merged', 'acme', 'holder', '', '', 'bug'),
            ('build', 'other', 'holder', '', '', 'bug'),
            ('intake', ' ', None, '', '', 'bug'),
            ('intake', '', None, '', '', 'bug'),
        ):
            item = work.add(self.db, title='guard fixture', app='acme')['id']
            self.db.execute(
                "UPDATE work_items SET state=%s, app=%s, team='acme', assignee=%s,"
                " source=%s, branch=%s, type=%s, expected_value='useful', value_score=40, close_reason=CASE WHEN %s='closed' THEN 'abandoned' END WHERE id=%s",
                (state, app, assignee, source, branch, kind, state, item))
            self.db.execute("INSERT INTO work_item_footprints(work_item_id, source, path, created_by)"
                            " VALUES (%s, 'manual', 'src/a.py', 'test')", (item,))
            self.ids.append(item)
        self.db.execute("INSERT INTO repository_footprint_policies(app, max_in_flight, updated_by)"
                        " VALUES ('acme', 2, 'test') ON CONFLICT(app)"
                        " DO UPDATE SET max_in_flight=2")

    def test_snapshot_matches_canonical_assignment_guards(self):
        for cap, shared in ((2, []), (None, []), (2, ['src'])):
            with self.subTest(cap=cap, shared=shared):
                self.db.execute("UPDATE repository_footprint_policies"
                                " SET max_in_flight=%s, shared_paths=%s WHERE app='acme'",
                                (cap, shared))
                self.assert_snapshot_guards()
        self.db.execute("DELETE FROM repository_footprint_policies WHERE app='acme'")
        self.assert_snapshot_guards()

    def assert_snapshot_guards(self):
        for limit in (None, self.ids[0], self.ids[1]):
            with self.subTest(assignment_id=limit):
                rows = storage.of(self.db).work.project_plan_rows('acme', assignment_id=limit)
                self.assertTrue(rows)
                for row in rows:
                    expected = self.db.query(
                        "SELECT (w.state='intake' AND " + overlap_exists_sql('w') + ") AS overlap,"
                        " (w.state='intake' AND w.assignee IS NULL AND "
                        + cap_reached_sql('w') + ") AS cap,"
                        " CASE WHEN w.state='intake' THEN " + in_flight_sql('w')
                        + " END AS count FROM work_items w WHERE w.id=%s", (row['id'],))[0]
                    self.assertEqual(row['footprint_overlap'], expected['overlap'])
                    self.assertEqual(row['repository_cap'], expected['cap'])
                    self.assertEqual(row['repository_in_flight'], expected['count'])
                self.assertNotIn(self.ids[3], [row['id'] for row in rows])
                self.assertNotIn(self.ids[7], [row['id'] for row in rows])
                self.assertNotIn(self.ids[8], [row['id'] for row in rows])
