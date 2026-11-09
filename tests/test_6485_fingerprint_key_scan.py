# SPDX-License-Identifier: AGPL-3.0-only
"""#6485: the U6 scan carries a producer's historical fingerprint key to A3.

An alert event may carry ``fingerprint_key`` (the key of an ``empreinte:``
lot). The scan passes it through the production path, not only by direct
injection; an absent or JSON-null key invents no alias; an unusable key is a
counted refusal of its row, never a receipt lost after the page is received.
"""
import json

from ameesh import fingerprints as fp, repair_observation as obs, storage
from .support import PgTestCase
from .test_6317_repair_correlation import NOW


class FingerprintKeyScanTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.api = storage.of(self.db).repair_signals
        for table in ('fingerprint_cause_observations', 'repair_signals',
                      'repair_signal_streams', 'repair_signal_refusals',
                      'repair_observation_scopes'):
            self.db.execute('DELETE FROM ' + table)
        self.db.execute('TRUNCATE ameesh_events, ameesh_events_pruned')

    def event(self, key, project='acme', **metadata):
        data = {'key': key, 'revision': 'a' * 40, **metadata}
        return self.db.query(
            "INSERT INTO ameesh_events(type,object,project,data)"
            " VALUES ('alert.raised','alert:test',%s,%s::jsonb) RETURNING id",
            (project, json.dumps(data)))[0]['id']

    def receipts(self):
        return {row['record']['alias']: row['record'] for row in self.db.query(
            'SELECT record FROM fingerprint_cause_observations')}

    def refusals(self):
        return [row['reason'] for row in self.db.query(
            'SELECT reason FROM repair_signal_refusals ORDER BY reason')]

    def test_scan_carries_the_historical_key_to_its_alias(self):
        opened = fp.open_lot(self.db, app='acme', key='old-command-rc1',
                             title='Old symptom', environment='staging', actor='test')
        self.event('a' * 64, fingerprint_key='old-command-rc1')
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        record = self.receipts()[opened['source']]
        self.assertEqual(record['alias_candidates'], [opened['lot']])
        self.assertEqual(record['candidates'], [])
        self.assertEqual(record['relation'], 'same_symptom_unknown')
        self.assertEqual(self.refusals(), [])

    def test_absent_or_null_key_invents_no_alias(self):
        self.event('a' * 64)
        self.event('b' * 64, fingerprint_key=None)
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(len(self.api.page('acme', obs.ENVIRONNEMENT, 'alerts')), 2)
        aliases = set(self.receipts())
        self.assertFalse(any(alias.startswith(fp.PREFIX) for alias in aliases))
        self.assertEqual(len(aliases), 2)
        self.assertEqual(self.refusals(), [])

    def test_unusable_key_refuses_its_row_and_the_page_advances(self):
        for index, value in enumerate((42, '', 'x' * 201, 'someone@example.com',
                                       {'nested': 'key'})):
            self.event('%064x' % index, fingerprint_key=value)
        self.event('f' * 64, fingerprint_key='good-key')
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(self.refusals(), ['invalid_envelope'] * 5)
        self.assertEqual(len(self.api.page('acme', obs.ENVIRONNEMENT, 'alerts')), 1)
        self.assertEqual(list(self.receipts()), ['empreinte:acme:good-key'])
        # A replay of the page neither duplicates the refusals nor the receipt.
        self.db.execute("UPDATE repair_signal_streams SET cursor=NULL")
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(len(self.refusals()), 5)
        self.assertEqual(len(self.receipts()), 1)

    def test_alerts_without_project_carry_the_key_under_the_declared_scope(self):
        opened = fp.open_lot(self.db, app='mesh', key='host-down',
                             title='Host down', environment='staging', actor='test')
        self.db.query(
            "INSERT INTO ameesh_events(type,object,project,data)"
            " VALUES ('alert.raised','alert:test',NULL,%s::jsonb) RETURNING id",
            (json.dumps({'key': 'c' * 64, 'fingerprint_key': 'host-down'}),))
        self.assertTrue(obs.scan(self.db, 'mesh', 'alerts', now=NOW,
                                 alerts=obs.WITHOUT_PROJECT))
        record = self.receipts()[opened['source']]
        self.assertEqual(record['alias_candidates'], [opened['lot']])
        self.assertEqual(record['relation'], 'same_symptom_unknown')

    def test_historical_alias_refuses_without_coercion(self):
        self.assertEqual(fp.historical_alias('acme', 'k'), 'empreinte:acme:k')
        for value in (1, None, b'k', ['k']):
            with self.assertRaises(fp.FingerprintError):
                fp.historical_alias('acme', value)
