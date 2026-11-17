# SPDX-License-Identifier: AGPL-3.0-only
"""#7622: exhausted splitting intake is not assignable idle capacity."""
from ameesh import sous_utilisation, work
from .test_l94_underuse import _Base


class IdleRoadmapTest(_Base):
    def setUp(self):
        super().setUp()
        self.agent('dev1')
        self.split = work.add(self.db, title='split', app='ameesh',
                              source='file-decoupage:ameesh')

    def alerts(self):
        return sous_utilisation.idle_capacity(
            self.db, self.listing(), self.now, [], paid=())

    def exhaust(self):
        work.note(self.db, self.split['id'], 'roadmap: épuisée ameesh')

    def test_exhausted_intake_does_not_alert(self):
        self.exhaust()
        self.assertEqual(self.alerts(), [])

    def test_unexhausted_intake_still_alerts(self):
        self.assertEqual(self.alerts()[0]['startable_lots'], [self.split['id']])

    def test_reopened_roadmap_alerts_again(self):
        self.exhaust()
        work.note(self.db, self.split['id'], 'roadmap: rouverte ameesh')
        self.assertEqual(self.alerts()[0]['startable_lots'], [self.split['id']])

    def test_real_work_remains_visible(self):
        self.exhaust()
        real = work.add(self.db, title='real work', app='ameesh')
        self.assertEqual(self.alerts()[0]['startable_lots'], [real['id']])

    def test_exhausted_split_with_work_in_progress_does_not_alert(self):
        self.exhaust()
        self.agent('dev2')
        active = work.add(self.db, title='priority work', app='ameesh', assignee='dev2')
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s", (active['id'],))
        self.assertEqual(self.alerts(), [])

    def test_exhaustion_suppresses_paid_work_aggravation_for_its_project(self):
        self.exhaust()
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = 'dev1'")
        self.agent('paid', 'deepseek', status='running', since_s=60)
        self.assertEqual(sous_utilisation.idle_capacity(
            self.db, self.listing(), self.now, [], paid=('deepseek',)), [])

    def test_other_project_stays_alertable(self):
        self.exhaust()
        self.db.execute("UPDATE agent_registry SET team = 'nexlink' WHERE name = 'dev1'")
        real = work.add(self.db, title='other project', app='nexlink')
        self.assertEqual(self.alerts()[0]['startable_lots'], [real['id']])
