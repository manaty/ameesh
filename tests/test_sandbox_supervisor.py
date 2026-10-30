# SPDX-License-Identifier: AGPL-3.0-only
"""Crash-window and cancellation contract for #4889, without a cluster."""
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ameesh import sandbox_provider as p
from ameesh.sandbox_supervisor import SandboxSupervisor
from ameesh.storage.postgres import sandbox

IMAGE = 'registry/reviewer@sha256:' + 'a' * 64


def admitted():
    identity = p.JobIdentity('turn-1', 'sand', 7, 'place', 'profile', 'reviewers')
    spec = p.JobSpec(identity, IMAGE, 120, 3600)
    row = dict(turn_id='turn-1', agent='sand', owner='runner', lease_epoch=7,
               namespace='reviewers', job_name=identity.name, job_uid=None,
               profile_ref='profile', placement_ref='place', image_digest=IMAGE,
               state='intent', deadline_ts=200, cancel_requested_at=None,
               scope={'repository': 'example/repo', 'pr': 1, 'head': 'a' * 40},
               scope_digest='scope-digest', source={'contract': 'ameesh-sandbox/1'},
               limits=dict(turns=1, tool_calls=10, seconds=60, model_calls=10,
                           session='none', tools=[], capabilities=[]))
    return row, spec


def result_for(row):
    return dict(schema='ameesh-sandbox-result/1', turn_id=row['turn_id'],
                epoch=row['lease_epoch'], scope=row['scope'],
                scope_digest=row['scope_digest'], result_id='result-1',
                outcome='accepted', verdict='green', findings=[], evidence=[],
                usage=dict(turns=1, tool_calls=1, seconds=2, model_calls=1),
                cost={'usd': 0.01}, sandbox_session=None, source=row['source'],
                finished_at='2026-10-30T10:00:00Z')


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.row, self.spec = admitted()
        self.clock = [100.0]
        self.provider = p.FakeJobProvider(clock=lambda: self.clock[0])
        self.events = []
        self.supervisor = self.restart()

    def restart(self, **kw):
        return SandboxSupervisor(self.provider, self.events.append,
                                 orphan_after=10, clock=lambda: self.clock[0], **kw)

    def step(self, **kw):
        return self.supervisor.step(self.row, self.spec, **kw)

    def bind(self):
        event = self.step()
        self.assertEqual(event.kind, 'bound')
        self.row.update(job_uid=event.job_uid, state='bound')
        return event.job_uid

    def cancel(self):
        self.row.update(state='cancelling', cancel_requested_at=self.clock[0])

    def success(self, result=None, message=None):
        result = result_for(self.row) if result is None else result
        self.provider.append_log('reviewers', self.spec.identity.name, json.dumps(result))
        digest = sandbox._canonical(result)[1]
        self.provider.set_status('reviewers', self.spec.identity.name,
                                 {'conditions': [{'type': 'Complete', 'status': 'True'}]},
                                 message=json.dumps({'result_sha256': digest})
                                 if message is None else message)
        return result

    def test_intent_creates_then_requires_durable_uid_before_result(self):
        uid = self.step().job_uid
        self.success()
        self.assertEqual(self.step().kind, 'bound')
        self.assertIsNone(self.row['job_uid'])
        self.row.update(job_uid=uid, state='bound')
        self.assertEqual(self.step().kind, 'result')
        self.assertEqual(len(self.provider.list('reviewers')), 1)

    def test_restart_after_create_response_lost_adopts(self):
        self.provider.create(self.spec)
        self.supervisor = self.restart()
        self.assertEqual(self.step().job_uid, 'fake-uid-1')
        self.assertEqual(len(self.provider.list('reviewers')), 1)

    def test_transient_create_after_side_effect_never_creates_second_job(self):
        create = self.provider.create
        def lost(spec):
            create(spec)
            raise p.ProviderUnavailable()
        with mock.patch.object(self.provider, 'create', side_effect=lost):
            self.assertEqual(self.step().kind, 'retry')
        self.supervisor = self.restart()
        self.bind()
        self.assertEqual(len(self.provider.list('reviewers')), 1)

    def test_create_409_reloads_existing_job(self):
        create = self.provider.create
        def conflict(spec):
            create(spec)
            raise p.JobAlreadyExists()
        with mock.patch.object(self.provider, 'create', side_effect=conflict):
            self.assertEqual(self.step().kind, 'bound')

    def test_two_supervisors_create_same_job(self):
        with ThreadPoolExecutor(2) as pool:
            events = list(pool.map(lambda _: self.restart().step(self.row, self.spec), range(2)))
        self.assertEqual({event.job_uid for event in events}, {'fake-uid-1'})
        self.assertEqual(len(self.provider.list('reviewers')), 1)

    def test_restart_bound_adopts_and_does_not_create(self):
        uid = self.bind()
        self.supervisor = self.restart()
        self.assertEqual(self.step().kind, 'heartbeat')
        self.assertEqual(self.step().job_uid, uid)

    def test_restart_completed_reads_same_result_and_private_file(self):
        self.bind()
        result = self.success()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            self.supervisor = self.restart(result_path=path)
            self.assertEqual(self.step().result, result)
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(self.step().result, result)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_result_digest_must_match_termination_message(self):
        self.bind()
        self.success(message='{"result_sha256":"wrong"}')
        event = self.step()
        self.assertEqual((event.kind, event.refusal), ('failed', 'result-mismatch'))

    def test_result_scope_and_usage_are_validated_by_s1(self):
        self.bind()
        result = result_for(self.row)
        result['scope'] = {'pr': 999}
        self.success(result)
        self.assertEqual(self.step().refusal, 'scope-mismatch')

    def test_invalid_and_oversize_logs_are_refused(self):
        self.bind()
        self.provider.set_phase('reviewers', self.spec.identity.name, p.PHASE_SUCCEEDED)
        for raw in ('not json', 'x' * (sandbox.MAX_RESULT_BYTES + 1)):
            with mock.patch.object(self.provider, 'logs', return_value=raw):
                self.assertEqual(self.step().refusal, 'missing')

    def test_cancel_before_create_never_launches(self):
        self.cancel()
        self.assertEqual(self.step().kind, 'cancelled')
        self.assertEqual(self.provider.list('reviewers'), [])

    def test_cancel_after_create_response_lost_stops_unbound_job(self):
        self.provider.create(self.spec)
        self.cancel()
        self.assertEqual(self.step().state, 'cancelling')
        self.provider.finish_delete('reviewers', self.spec.identity.name)
        self.assertEqual(self.step().state, 'cancelled')

    def test_cancel_waits_for_async_deletion_and_survives_restart(self):
        self.bind()
        self.cancel()
        self.assertEqual(self.step().state, 'cancelling')
        job = self.provider.get('reviewers', self.spec.identity.name)
        self.assertIsNotNone(job.deletion_timestamp)
        with self.assertRaises(p.JobAlreadyExists):
            self.provider.create(self.spec)
        self.clock[0] += 11
        self.supervisor = self.restart()
        event = self.step()
        self.assertEqual((event.state, event.refusal), ('orphaned', 'sandbox_orphan'))
        self.row['state'] = 'orphaned'
        self.provider.finish_delete('reviewers', self.spec.identity.name)
        self.assertEqual(self.step().state, 'cancelled')

    def test_cancel_wins_over_completed_result(self):
        self.bind()
        self.success()
        self.cancel()
        with mock.patch.object(self.provider, 'logs', side_effect=AssertionError('no logs')):
            self.assertEqual(self.step().state, 'cancelled')

    def test_accepted_result_terminal_row_never_deleted(self):
        self.bind()
        self.success()
        self.row['state'] = 'succeeded'
        self.assertEqual(self.step().kind, 'terminal')
        self.assertIsNone(self.provider.get('reviewers', self.spec.identity.name).deletion_timestamp)

    def test_deadline_requests_fence_before_deletion(self):
        self.bind()
        self.clock[0] = 201
        self.assertEqual(self.step().kind, 'cancel-requested')
        self.assertIsNone(self.provider.get('reviewers', self.spec.identity.name).deletion_timestamp)
        self.cancel()
        self.assertEqual(self.step().state, 'cancelling')

    def test_deadline_passed_during_logs_cannot_emit_result(self):
        self.bind()
        self.success()
        logs = self.provider.logs
        def slow(*args):
            self.clock[0] = 201
            return logs(*args)
        with mock.patch.object(self.provider, 'logs', side_effect=slow):
            self.assertEqual(self.step().kind, 'cancel-requested')

    def test_deadline_passed_during_observation_never_creates(self):
        get = self.provider.get
        def slow(*args):
            self.clock[0] = 201
            return get(*args)
        with mock.patch.object(self.provider, 'get', side_effect=slow):
            self.assertEqual(self.step().kind, 'cancel-requested')
        self.assertEqual(self.provider.list('reviewers'), [])

    def test_iso_timestamp_handoff_is_supported(self):
        for value in ('1970-01-01T00:03:20+00:00', '1970-01-01T00:03:20Z'):
            self.row['deadline_ts'] = value
            self.assertEqual(self.step().kind, 'bound')

    def test_bound_uid_disappears_is_never_replaced(self):
        self.bind()
        self.provider.delete('reviewers', self.spec.identity.name)
        self.provider.finish_delete('reviewers', self.spec.identity.name)
        event = self.step()
        self.assertEqual((event.kind, event.refusal), ('cancel-requested', 'job-lost'))
        self.assertEqual(self.provider.list('reviewers'), [])

    def test_foreign_uid_refuses_adoption_and_delete(self):
        self.bind()
        self.provider.delete('reviewers', self.spec.identity.name)
        self.provider.finish_delete('reviewers', self.spec.identity.name)
        replacement = self.provider.create(self.spec)
        self.cancel()
        self.assertEqual(self.step().refusal, 'job-conflict')
        self.assertIsNone(self.provider.get('reviewers', replacement.name).deletion_timestamp)

    def test_foreign_labels_or_image_cannot_be_adopted(self):
        for kw in ({'identity': replace(self.spec.identity, epoch=8)},
                   {'image': 'registry/other@sha256:' + 'b' * 64}):
            self.provider = p.FakeJobProvider()
            self.supervisor = self.restart()
            self.provider.create(replace(self.spec, **kw))
            self.assertEqual(self.step().refusal, 'job-conflict')

    def test_extra_turn_job_is_ambiguous_and_never_deleted(self):
        self.bind()
        extra = replace(self.provider.get('reviewers', self.spec.identity.name),
                        name='other', uid='other-uid')
        self.cancel()
        with mock.patch.object(self.provider, 'list', return_value=[extra]):
            self.assertEqual(self.step().refusal, 'job-conflict')
        self.assertIsNone(self.provider.get('reviewers', self.spec.identity.name).deletion_timestamp)

    def test_uid_precondition_protects_replacement_during_delete(self):
        old = self.bind()
        delete = self.provider.delete
        def race(namespace, name, **kw):
            delete(namespace, name)
            self.provider.finish_delete(namespace, name)
            self.provider.create(self.spec)
            return delete(namespace, name, **kw)
        self.cancel()
        with mock.patch.object(self.provider, 'delete', side_effect=race):
            self.assertEqual(self.step().refusal, 'job-conflict')
        self.assertNotEqual(self.provider.get('reviewers', self.spec.identity.name).uid, old)
        self.assertIsNone(self.provider.get('reviewers', self.spec.identity.name).deletion_timestamp)

    def test_provider_failure_during_cancel_is_orphaned(self):
        self.bind()
        self.cancel()
        with mock.patch.object(self.provider, 'get', side_effect=p.ProviderUnavailable()):
            self.assertEqual(self.step().state, 'orphaned')

    def test_recovery_requires_fence_and_never_creates(self):
        self.assertEqual(self.step(recovery=True).kind, 'cancel-requested')
        self.assertEqual(self.provider.list('reviewers'), [])
        self.cancel()
        self.assertEqual(self.step(recovery=True).state, 'lost')

    def test_recovery_observes_old_job_termination(self):
        self.bind()
        self.provider.set_phase('reviewers', self.spec.identity.name, p.PHASE_FAILED)
        self.cancel()
        self.assertEqual(self.step(recovery=True).state, 'expired')

    def test_live_job_condition_counters(self):
        self.bind()
        self.provider.set_status('reviewers', self.spec.identity.name,
                                 {'active': 1, 'failed': 2})
        self.assertEqual(self.step().state, 'running')
        self.provider.set_status('reviewers', self.spec.identity.name,
                                 {'conditions': [{'type': 'FailureTarget', 'status': 'True'}],
                                  'active': 1, 'failed': 3})
        self.assertEqual(self.step().kind, 'heartbeat')
        self.provider.set_status('reviewers', self.spec.identity.name,
                                 {'conditions': [{'type': 'Failed', 'status': 'True'}]})
        self.assertEqual(self.step().state, 'failed')

    def test_snapshot_identity_checked_before_provider_access(self):
        self.row['lease_epoch'] = 8
        with mock.patch.object(self.provider, 'get', side_effect=AssertionError):
            with self.assertRaises(sandbox.Refusal):
                self.step()

    def test_emit_failure_propagates_and_restart_adopts(self):
        with mock.patch.object(self.supervisor, 'emit', side_effect=RuntimeError('runner gone')):
            with self.assertRaises(RuntimeError):
                self.step()
        self.supervisor = self.restart()
        self.assertEqual(self.step().job_uid, 'fake-uid-1')


class JobStatusTest(unittest.TestCase):
    def test_terminal_conditions_only(self):
        for status in ({'succeeded': 1}, {'failed': 1},
                       {'conditions': [{'type': 'Complete', 'status': 'False'}]}):
            self.assertEqual(p.phase_from_status(status), p.PHASE_PENDING)
        self.assertEqual(p.phase_from_status({'startTime': 'now'}), p.PHASE_RUNNING)
        self.assertEqual(p.phase_from_status({'conditions': [
            {'type': 'Complete', 'status': 'True'}]}), p.PHASE_SUCCEEDED)
        with self.assertRaises(p.InvalidJob):
            p.phase_from_status({'conditions': [
                {'type': 'Complete', 'status': 'True'},
                {'type': 'Failed', 'status': 'True'}]})
