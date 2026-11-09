# SPDX-License-Identifier: AGPL-3.0-only
"""U6: replay, crash recovery, poison filtering and observation-only wiring."""
import json
from pathlib import Path
import unittest
from unittest import mock

from ameesh import repair_observation as obs, repair_report, repair_signals, repair_triage, storage
from .support import PgTestCase
from .test_6323_repair_report_raccord import build, alert_row, capability

NOW = 1794144000


class ReportTest(unittest.TestCase):
    def test_missing_facts_are_unknown_not_a_product_failure(self):
        report = build(signal_alerts=[alert_row()])
        triage = capability(report, 'A2')['counters']['qualifications']
        self.assertEqual(triage['signals'], 1)
        self.assertEqual(triage['by_route'], {'unknown': 1})
        self.assertEqual(triage['effects'], 'none')

    def test_durable_counts_are_separate_from_alert_window(self):
        report = build(journal_summary={'signals': 7, 'occurrences': 9,
                                       'groups': [{'source': 'github_watch', 'signals': 7}]})
        counters = capability(report, 'A1')['counters']
        self.assertEqual(counters['journal_signaux'], 7)
        self.assertEqual(counters['signaux_recus'], 0)
        self.assertNotIn('journal_signaux', capability(report, 'A1')['unknown'])
        self.assertIn('journal_signaux', capability(build(), 'A1')['unknown'])

    def test_source_failure_does_not_abort_other_projects_or_leak_text(self):
        logs = []
        db = mock.Mock()
        db.query.return_value = [{'project': 'broken'}, {'project': 'working'}]
        with mock.patch.object(
                obs, 'scan', side_effect=[RuntimeError('SECRET'), True, True]) as scan:
            obs.observe(db, mesh_project='banc', log=logs.append)
        # Two project streams and the declared scope of the alerts without one.
        self.assertEqual(scan.call_count, 3)
        self.assertEqual(logs, ['repair-observation: source_error (RuntimeError)'])


class ObservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.api = storage.of(self.db).repair_signals
        for table in ('repair_signals', 'repair_signal_streams', 'repair_signal_refusals',
                      'repair_observation_scopes', 'github_watch_pending'):
            self.db.execute('DELETE FROM ' + table)
        self.db.execute('TRUNCATE ameesh_events, ameesh_events_pruned')

    def ingest(self, rows, expected=None, next='one', source='inbox'):
        return obs.ingest(self.db, project='acme', environment='staging',
                          source=source, rows=rows, expected_cursor=expected,
                          next_cursor=next, now=NOW)

    def row(self, ident=1, **kw):
        return {'key': ident, 'entry': {'id': ident}, **kw}

    def page(self, source='inbox'):
        return self.api.page('acme', 'staging', source)

    def event(self, key='a'*64, project='acme', **metadata):
        data = {'key': key, 'revision': 'a'*40, 'detail': 'PRIVATE', **metadata}
        return self.db.query(
            "INSERT INTO ameesh_events(type,object,project,data)"
            " VALUES ('alert.raised','alert:test',%s,%s::jsonb) RETURNING id",
            (project, json.dumps(data)))[0]['id']

    def test_alert_scan_advances_only_one_bounded_page(self):
        for i in range(obs.BATCH + 1):
            self.event('%064x' % i)
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(len(self.page('alerts')), obs.BATCH)
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(len(self.page('alerts')), obs.BATCH + 1)

    def test_iso_since_falls_back_to_observed_timestamp(self):
        self.event(since='2026-11-08T10:00:00Z')
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(len(self.page('alerts')), 1)

    def test_real_anonymized_alert_event_reaches_journal_and_report(self):
        # notify._event shape, anonymized: the key is JSON text, not a digest.
        fixture = Path(__file__).parent / 'fixtures/repair_signals/alert-raised-real.json'
        event = json.loads(fixture.read_text())
        self.db.execute(
            'INSERT INTO ameesh_events(type,object,project,data) VALUES (%s,%s,%s,%s::jsonb)',
            (event['type'], event['object'], event['project'], json.dumps(event['data'])))
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        rows = self.page('alerts')
        self.assertEqual(len(rows), 1)
        signal = repair_signals.from_alert(event['data'], project='acme',
                                          environment='staging', received_at=NOW)
        self.assertEqual(rows[0]['signal']['native_id'], signal.native_id)
        self.assertEqual(rows[0]['signal']['id'], signal.id)
        # Historical rows replay after EOF without creating a second signal.
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(len(self.page('alerts')), 1)
        signals, _, refused = repair_report._envelopes(
            [{'cle': event['data']['key'], 'cree': NOW}], project='acme',
            environment='staging', received_at=NOW)
        self.assertFalse(refused)
        self.assertEqual([s.id for s in signals], [signal.id])

    def test_replay_handover_and_stale_receiver(self):
        self.assertTrue(self.ingest([self.row()]))
        self.assertFalse(self.ingest([self.row(2)]))
        # A fresh caller resumes the durable state after holder replacement.
        cursor = storage.of(self.db).repair_signals.state('acme', 'staging', 'inbox')['cursor']
        self.assertTrue(self.ingest([self.row(), self.row(2)], expected=cursor, next='two'))
        self.assertEqual(len(self.page()), 2)
        self.assertEqual(self.page()[0]['signal']['occurrence'], 1)

    def test_poison_is_refused_once_and_valid_inputs_advance(self):
        self.assertTrue(self.ingest([self.row(-1), self.row(2)]))
        self.assertEqual(len(self.page()), 1)
        self.assertEqual(self.api.state('acme', 'staging', 'inbox')['cursor'], 'one')
        self.ingest([self.row(-1)], expected='one', next='two')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM repair_signal_refusals')[0]['n'], 1)
        self.assertNotIn('PRIVATE', json.dumps(self.page()))

    def test_existing_outcomes_only_no_additional_work(self):
        before = self.db.query('SELECT count(*) AS n FROM work_items')[0]['n']
        rows = [self.row(i, outcome=kind, reference='%s:%s' % (kind, i))
                for i, kind in enumerate(('lot', 'basket', 'decision'), 1)]
        self.ingest(rows)
        self.assertEqual([r['outcome_ref'] for r in self.page()],
                         ['lot:1', 'basket:2', 'decision:3'])
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'], before)
        self.ingest([self.row(1)], expected='one', next='two')
        self.assertEqual(self.page()[0]['outcome_ref'], 'lot:1')

    def test_qualification_route_never_opens_work(self):
        signal = repair_signals.from_inbox({'id': 1}, project='acme',
                                           environment='staging', received_at=NOW)
        facts = repair_triage.QualificationFacts(nature='evolution', confidence='confirmed',
                                                 evidence_ref=signal.evidence_ref)
        self.ingest([self.row(facts=facts)])
        self.assertEqual(self.page()[0]['signal']['outcome'], 'unknown')
        self.assertEqual(self.page()[0]['outcome_ref'], '')

    def test_crash_after_receive_settles_on_replay(self):
        signal = repair_signals.from_inbox({'id': 1}, project='acme',
                                           environment='staging', received_at=NOW)
        self.api.receive('acme', 'staging', 'inbox', [signal], expected_cursor=None, next_cursor='one')
        self.ingest([self.row(outcome='lot', reference='lot:1')], expected='one', next='two')
        self.assertEqual(self.page()[0]['outcome_ref'], 'lot:1')

    def test_unknown_is_upgraded_when_existing_proof_arrives(self):
        self.ingest([self.row()])
        self.ingest([self.row(outcome='decision', reference='decision:2')], expected='one', next='two')
        self.assertEqual(self.page()[0]['outcome_ref'], 'decision:2')

    def test_circular_scan_revisits_a_late_commit_and_counts_once(self):
        self.event()
        obs.scan(self.db, 'acme', 'alerts', now=NOW)
        obs.scan(self.db, 'acme', 'alerts', now=NOW)  # EOF resets shared cursor
        self.event('b'*64)
        obs.scan(self.db, 'acme', 'alerts', now=NOW)
        self.assertEqual(len(self.page('alerts')), 2)
        self.assertEqual(self.api.summary(project='acme')['occurrences'], 2)

    def test_repeated_raises_of_one_key_are_occurrences_not_deliveries(self):
        key = 'c'*64
        for _ in range(3):
            self.event(key)
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        rows = self.page('alerts')
        # One stable signal identity, but the source raised its key three times.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['signal']['occurrence'], 3)
        summary = self.api.summary(project='acme')
        self.assertEqual((summary['signals'], summary['occurrences']), (1, 3))
        # Replaying the same committed page is not a delivery counter.
        obs.scan(self.db, 'acme', 'alerts', now=NOW)  # EOF resets the cursor
        obs.scan(self.db, 'acme', 'alerts', now=NOW)  # same three raises again
        summary = self.api.summary(project='acme')
        self.assertEqual((summary['signals'], summary['occurrences']), (1, 3))

    def test_recurrence_high_water_spans_bounded_pages(self):
        key = 'd'*64
        total = obs.BATCH + 1
        for _ in range(total):
            self.event(key)
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        self.assertEqual(self.page('alerts')[0]['signal']['occurrence'], total)
        self.assertTrue(obs.scan(self.db, 'acme', 'alerts', now=NOW))
        rows = self.page('alerts')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['signal']['occurrence'], total)
        self.assertEqual(self.api.summary(project='acme')['occurrences'], total)

    def test_recurrence_counts_only_the_observed_project(self):
        key = 'e'*64
        self.event(key)
        self.event(key, project='other')
        obs.scan(self.db, 'acme', 'alerts', now=NOW)
        self.assertEqual(self.page('alerts')[0]['signal']['occurrence'], 1)

    def test_recurrence_high_water_rises_with_a_new_raise(self):
        key = 'f'*64
        self.event(key)
        obs.scan(self.db, 'acme', 'alerts', now=NOW)
        self.assertEqual(self.page('alerts')[0]['signal']['occurrence'], 1)
        self.event(key)
        obs.scan(self.db, 'acme', 'alerts', now=NOW)
        self.assertEqual(self.page('alerts')[0]['signal']['occurrence'], 2)

    def test_commit_order_cannot_permanently_skip_a_lower_event_id(self):
        from ameesh import db as db_mod
        other = db_mod.connect(self.cfg)
        try:
            with self.db.transaction() as tx:
                tx.execute(
                    "INSERT INTO ameesh_events(type,object,project,data)"
                    " VALUES ('alert.raised','alert:late','acme',%s::jsonb)",
                    (json.dumps({'key': 'b'*64}),))
                other.execute(
                    "INSERT INTO ameesh_events(type,object,project,data)"
                    " VALUES ('alert.raised','alert:first','acme',%s::jsonb)",
                    (json.dumps({'key': 'a'*64}),))
                obs.scan(other, 'acme', 'alerts', now=NOW)
                self.assertEqual(len(storage.of(other).repair_signals.page(
                    'acme', 'staging', 'alerts')), 1)
            obs.scan(other, 'acme', 'alerts', now=NOW)  # EOF
            obs.scan(other, 'acme', 'alerts', now=NOW)  # replay lower ID
            self.assertEqual(len(self.page('alerts')), 2)
        finally:
            other.close()

    def test_github_committed_input_and_existing_lot_are_observed(self):
        self.db.execute(
            "INSERT INTO github_watch_pending(project,repo,kind,object_id,issue_ref,url,agent,delivered_at,events_after)"
            " VALUES ('acme','org/repo','issue',1,'org/repo#1','https://github.com/org/repo/issues/1','watcher',%s,0)",
            (NOW,))
        obs.scan(self.db, 'acme', 'github_watch', now=NOW)
        self.assertEqual(self.page('github_watch')[0]['signal']['native_id'], 'org/repo#1')
        obs.scan(self.db, 'acme', 'github_watch', now=NOW)
        obs.scan(self.db, 'acme', 'github_watch', now=NOW)
        self.assertEqual(len(self.page('github_watch')), 1)

    def test_existing_lot_reference_stays_within_its_project(self):
        from ameesh import registry, work
        registry.upsert(self.db, 'watcher', harness='codex', chantier='app')
        self.db.execute(
            "INSERT INTO github_watch_pending(project,repo,kind,object_id,issue_ref,url,agent,delivered_at,events_after)"
            " VALUES ('acme','org/repo','issue',1,'org/repo#1','https://github.com/org/repo/issues/1','watcher',%s,0)",
            (NOW,))
        work.add(self.db, title='other project', app='other', issue_ref='org/repo#1',
                 actor='watcher')
        obs.scan(self.db, 'acme', 'github_watch', now=NOW)
        self.assertEqual(self.page('github_watch')[0]['signal']['outcome'], 'unknown')
        lot = work.add(self.db, title='same project', app='acme', issue_ref='org/repo#1',
                       actor='watcher')
        obs.scan(self.db, 'acme', 'github_watch', now=NOW)  # EOF
        obs.scan(self.db, 'acme', 'github_watch', now=NOW)
        self.assertEqual(self.page('github_watch')[0]['outcome_ref'], 'lot:%s' % lot['id'])

    def test_real_github_watcher_tick_feeds_journal_without_new_lot(self):
        import dataclasses
        from ameesh import github_watch, registry
        from .test_2845_github_watch import Api, SUB, NOW as watch_now, issue
        registry.upsert(self.db, 'watcher', harness='codex', chantier='app')
        cfg = dataclasses.replace(self.cfg, host='atelier', github={'host': 'atelier'})
        watcher = github_watch.Watcher(cfg, api=Api([issue()]), declarations=[SUB])
        watcher.tick(self.db, watch_now)
        rows = self.api.page('app', 'staging', 'github_watch')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['signal']['native_id'], 'acme/app#1')
        self.assertEqual(rows[0]['signal']['outcome'], 'unknown')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'], 0)

    def test_alert_role_hook_observes_committed_inputs_and_preserves_exit_code(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from ameesh import role_run
        self.event()
        self.event('b'*64, project=None)
        report = dict(role='watcher', host='banc', held=True, stopped=False,
                      releve='', epoch=1, owner='test', left_s=200)
        proxy = mock.Mock(wraps=self.db)
        proxy.close.return_value = None
        args = SimpleNamespace(role='watcher', command=['true'], pass_name='alerts',
                               json=False, dry_run=False, min_left=0)
        with mock.patch.object(role_run.db_mod, 'connect', return_value=proxy), \
             mock.patch.object(role_run, 'live', return_value=report), \
             mock.patch.object(role_run, '_alerts_delegation', return_value={'delegate': 'banc'}), \
             mock.patch.object(role_run, '_AlertsLease') as lease, \
             mock.patch.object(role_run, '_AlertsExecution') as execution, \
             mock.patch.object(role_run.alerts_delegation_pg, 'entrust', return_value=True), \
             mock.patch.object(role_run, '_run_under_lease', return_value=(7, False)), \
             contextlib.redirect_stderr(io.StringIO()):
            lease.return_value.remaining.return_value = 100
            execution.return_value.lost.is_set.return_value = False
            cfg = SimpleNamespace(host='banc', notify={}, project='banc')
            self.assertEqual(role_run.run(cfg, args), 7)
        self.assertEqual(len(self.page('alerts')), 1)
        self.assertEqual(self.page('alerts')[0]['signal']['observed_version'], 'a'*40)
        # #6477 : the role pass hands its configured project as the declared
        # scope, so the alert without a project reaches the same journal.
        without = self.api.page('banc', 'staging', 'alerts')
        self.assertEqual(len(without), 1)
        self.assertEqual(without[0]['signal']['native_id'], 'b'*64)

    def test_report_reads_durable_summary_in_read_only_transaction(self):
        self.ingest([self.row()])
        end = float(self.db.query('SELECT extract(epoch FROM now()) AS clock_epoch')[0]['clock_epoch']) + 1
        report = repair_report.read(self.db, project='acme', now=end)
        self.assertEqual(capability(report, 'A1')['counters']['journal_signaux'], 1)
        self.assertTrue(report['read_only'])


class WithoutProjectTest(PgTestCase):
    """#6477: alerts without a project are observed under the declared scope.

    They belong to the mesh project read from the configuration, but their
    scan position is their own: the project stream's cursor must neither
    advance for them nor skip them.
    """

    SCOPE = 'banc'
    ENV = 'staging'

    def setUp(self):
        super().setUp()
        self.api = storage.of(self.db).repair_signals
        for table in ('repair_signals', 'repair_signal_streams',
                      'repair_signal_refusals', 'repair_observation_scopes'):
            self.db.execute('DELETE FROM ' + table)
        self.db.execute('TRUNCATE ameesh_events, ameesh_events_pruned')

    def event(self, key='a'*64, project=None, **metadata):
        data = {'key': key, 'revision': 'a'*40, 'detail': 'PRIVATE', **metadata}
        return self.db.query(
            "INSERT INTO ameesh_events(type,object,project,data)"
            " VALUES ('alert.raised','alert:test',%s,%s::jsonb) RETURNING id",
            (project, json.dumps(data)))[0]['id']

    def page(self, project=None):
        return self.api.page(project or self.SCOPE, self.ENV, 'alerts')

    def scope_cursor(self, scope=None):
        rows = self.db.query(
            "SELECT cursor FROM repair_observation_scopes"
            " WHERE scope=%s AND environment=%s AND source='alerts'",
            (scope or self.SCOPE, self.ENV))
        return rows[0]['cursor'] if rows else None

    def observe(self, **kw):
        return obs.observe(self.db, mesh_project=self.SCOPE, **kw)

    def test_alert_without_project_is_received_once_under_the_declared_scope(self):
        ident = self.event()
        self.observe()
        rows = self.page()
        self.assertEqual(len(rows), 1)
        signal = rows[0]['signal']
        self.assertEqual(signal['project'], self.SCOPE)
        self.assertEqual(signal['source'], 'alerts')
        self.assertEqual(signal['outcome'], 'unknown')
        self.assertEqual(self.scope_cursor(), str(ident))
        self.assertNotIn('PRIVATE', json.dumps(rows))
        # EOF resets the position; replaying the same committed page is neutral.
        self.observe()
        self.observe()
        self.assertEqual(len(self.page()), 1)
        self.assertEqual(self.api.summary(project=self.SCOPE)['occurrences'], 1)

    def test_cursor_is_distinct_from_the_project_stream(self):
        project_id = self.event('p'*64, project='acme')
        without_id = self.event('w'*64)
        self.observe()
        self.assertEqual(len(self.api.page('acme', self.ENV, 'alerts')), 1)
        self.assertEqual(self.api.state('acme', self.ENV, 'alerts')['cursor'],
                         str(project_id))
        # The scope's journal stream is the receipt stream, not its position.
        self.assertIsNone(self.api.state(self.SCOPE, self.ENV, 'alerts')['cursor'])
        self.assertEqual(self.scope_cursor(), str(without_id))

    def test_without_project_pages_are_bounded(self):
        for i in range(obs.BATCH + 1):
            self.event('%064x' % i)
        self.observe()
        self.assertEqual(len(self.page()), obs.BATCH)
        self.observe()
        self.assertEqual(len(self.page()), obs.BATCH + 1)

    def test_occurrences_count_only_alerts_without_project(self):
        key = 'e'*64
        self.event(key)
        self.event(key, project='acme')
        self.observe()
        self.assertEqual(self.page()[0]['signal']['occurrence'], 1)
        self.assertEqual(
            self.api.page('acme', self.ENV, 'alerts')[0]['signal']['occurrence'], 1)

    def test_repeated_raises_are_occurrences_not_deliveries(self):
        key = 'f'*64
        for _ in range(3):
            self.event(key)
        self.observe()
        rows = self.page()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['signal']['occurrence'], 3)
        self.observe()
        self.observe()
        self.assertEqual(self.page()[0]['signal']['occurrence'], 3)

    def test_observation_opens_no_work(self):
        before = self.db.query('SELECT count(*) AS n FROM work_items')[0]['n']
        self.event()
        self.observe()
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'],
                         before)
        self.assertEqual(self.page()[0]['signal']['outcome'], 'unknown')

    def test_poison_is_refused_once_and_does_not_block_the_page(self):
        self.db.execute(
            "INSERT INTO ameesh_events(type,object,project,data)"
            " VALUES ('alert.raised','alert:bad',NULL,'{}'::jsonb)")
        self.event()
        self.observe()
        self.assertEqual(len(self.page()), 1)
        self.assertEqual(
            self.db.query('SELECT count(*) AS n FROM repair_signal_refusals')[0]['n'], 1)
        self.assertNotIn('PRIVATE', json.dumps(self.page()))

    def test_missing_declared_scope_is_not_invented(self):
        self.event()
        logs = []
        obs.observe(self.db, mesh_project='', log=logs.append)
        self.assertEqual(self.page(), [])
        self.assertEqual(logs, ['repair-observation: scope_unset'])

    def test_scope_failure_does_not_stop_the_project_scan(self):
        self.event('p'*64, project='acme')
        logs = []
        with mock.patch.object(obs, '_scan_without_project',
                               side_effect=RuntimeError('SECRET')):
            self.observe(log=logs.append)
        self.assertEqual(len(self.api.page('acme', self.ENV, 'alerts')), 1)
        self.assertEqual(logs, ['repair-observation: source_error (RuntimeError)'])

    def test_project_failure_does_not_stop_the_scope_scan(self):
        self.event('p'*64, project='acme')
        self.event()
        logs = []
        real = obs.scan

        def scan(db, project, source, **kw):
            if project == 'acme':
                raise RuntimeError('SECRET')
            return real(db, project, source, **kw)

        with mock.patch.object(obs, 'scan', side_effect=scan):
            self.observe(log=logs.append)
        self.assertEqual(len(self.page()), 1)
        self.assertEqual(logs, ['repair-observation: source_error (RuntimeError)'])

    def test_a_failed_receipt_does_not_advance_the_scope_cursor(self):
        self.event()
        with mock.patch.object(obs, 'ingest', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                obs.scan(self.db, self.SCOPE, 'alerts',
                         alerts=obs.WITHOUT_PROJECT)
        self.assertIsNone(self.scope_cursor())
        self.assertEqual(self.page(), [])
