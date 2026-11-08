# SPDX-License-Identifier: AGPL-3.0-only
"""U6: replay, crash recovery, poison filtering and observation-only wiring."""
import json
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
        with mock.patch.object(obs, 'scan', side_effect=[RuntimeError('SECRET'), True]) as scan:
            obs.observe(db, log=logs.append)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(logs, ['repair-observation: source_error (RuntimeError)'])


class ObservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.api = storage.of(self.db).repair_signals
        for table in ('repair_signals', 'repair_signal_streams', 'repair_signal_refusals',
                      'github_watch_pending'):
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
            cfg = SimpleNamespace(host='banc', notify={})
            self.assertEqual(role_run.run(cfg, args), 7)
        self.assertEqual(len(self.page('alerts')), 1)
        self.assertEqual(self.page('alerts')[0]['signal']['observed_version'], 'a'*40)

    def test_report_reads_durable_summary_in_read_only_transaction(self):
        self.ingest([self.row()])
        end = float(self.db.query('SELECT extract(epoch FROM now()) AS clock_epoch')[0]['clock_epoch']) + 1
        report = repair_report.read(self.db, project='acme', now=end)
        self.assertEqual(capability(report, 'A1')['counters']['journal_signaux'], 1)
        self.assertTrue(report['read_only'])
