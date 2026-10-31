# SPDX-License-Identifier: AGPL-3.0-only
"""S5: real PostgreSQL, shared read contract, filtering, accounting and alerts."""
import json
import time
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation, registry, sandbox_observation as obs, serve, storage
from ameesh import turn_observation
from ameesh.storage.postgres import sandbox
from .support import PgTestCase
from .test_l159a_capacity_guard import errors


class SandboxReadTest(PgTestCase):
    turn = 'a' * 32

    def setUp(self):
        super().setUp()
        for table in ('tranche_relay_requests', 'sandbox_results', 'sandbox_turns',
                      'tranche_reservations', 'tranche_events', 'tranche_limits',
                      'spend_pending', 'turn_resources', 'account_reservations'):
            self.db.execute('DELETE FROM ' + table)
        registry.upsert(self.db, 'sand', chantier='demo', harness='deepseek', host='h1')
        self.db.execute("update agent_registry set lease_owner='runner', lease_epoch=7,"
                        " lease_expires_at=now()+interval '1 hour' where name='sand'")
        self.db.execute("insert into sandbox_turns (turn_id,spend_key,agent,owner,lease_epoch,"
                        "scope,scope_digest,retry_family,scope_generation,attempt,max_attempts,"
                        "profile_ref,profile_digest,placement_ref,placement_digest,canon_ref,limits,"
                        "image_digest,provider,namespace,job_name,deadline_ts,source) values"
                        " (%s,'spend-1','sand','runner',7,'{}','digest','family',1,1,2,"
                        " 'profile','pd','place','pld','canon','{}','sha256:image',"
                        " 'kubernetes','default','job-1',now()+interval '1 hour','{}')", (self.turn,))
        self.db.execute("insert into account_reservations (billing_account,agent,usd,period,spend_key)"
                        " values ('test','sand',2,current_date,'spend-1')")
        self.db.execute("insert into tranche_limits (id,project,cap_usd,starts_at,admitted_by,admitted_ref)"
                        " values ('tr','demo',10,now(),'test','decision')")
        self.db.execute("insert into tranche_reservations (tranche_id,spend_key,turn_id,agent,"
                        " billing_account,path,reserved_usd,usd) values"
                        " ('tr','spend-1',%s,'sand','test','sandbox',2,2)", (self.turn,))
        storage.of(self.db).turn_resources.open_turn(self.turn, 'sand', 'h1', pgid=None, label='tour')

    def test_same_document_cli_api_and_turn_observation_read_only(self):
        document = obs.show(self.db, self.turn)
        self.assertEqual(errors(serve.SCHEMAS['ameesh-sandbox-turn/1'], document), [])
        cli = self.mesh('sandbox', 'show', self.turn, '--json')
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout), document)
        ctx = SimpleNamespace(sees=lambda _: True)
        self.assertEqual(serve._sandbox_turn(ctx, self.db, {'turn': self.turn}, {}), document)
        observed = turn_observation.observe(self.db, turn=self.turn)
        self.assertEqual(observed['sandbox'], document)
        self.assertEqual(observed['accounting']['state'], 'unknown')
        self.assertEqual(document['tranche']['state'], 'open')
        self.assertEqual(obs.current_agents(self.db, ['sand']), {'sand': document})
        self.assertEqual(obs.current_agents(self.db, ['sand'], sees=lambda _: False), {})
        self.assertNotIn('sandbox', turn_observation.observe(self.db, turn=self.turn, sees=lambda _: False))
        self.db.execute("update turn_resources set status='done' where turn_id=%s", (self.turn,))
        self.assertEqual(obs.current_agents(self.db, ['sand']), {})
        self.assertEqual(obs.show(self.db, self.turn), document)

    def test_filters_bounds_unknown_and_visibility(self):
        self.assertEqual(obs.listing(self.db, state='intent', scope='digest')['turns'][0]['turn_id'], self.turn)
        self.assertEqual(obs.listing(self.db, agent='other')['turns'], [])
        self.assertEqual(obs.listing(self.db, scope="' OR true --")['turns'], [])
        self.assertEqual(obs.listing(self.db, sees=lambda e: e['project'] == 'other')['turns'], [])
        with self.assertRaises(obs.Unknown):
            obs.show(self.db, self.turn, sees=lambda _: False)
        for limit in (0, -1, 1001):
            with self.assertRaises(ValueError):
                obs.listing(self.db, limit=limit)
        self.assertEqual(self.mesh('sandbox', 'show', 'b'*32, '--json').returncode, 3)
        self.assertEqual(self.mesh('sandbox', 'list', '--limit', '0').returncode, 2)
        self.assertEqual(self.mesh('sandbox', 'show', 'bad').returncode, 2)

    def test_result_projection_and_distinct_kept_tranche(self):
        self.db.execute("insert into sandbox_results (turn_id,epoch,result_id,result_sha256,payload,receipt)"
                        " values (%s,7,'r','hash',%s::jsonb,%s::jsonb)",
                        (self.turn, json.dumps(dict(usage=dict(turns=1, tool_calls=2, seconds=3, model_calls=4),
                                                   verdict='green', findings=[{'summary': 'private-result'}],
                                                   raw_transcript='private-transcript')),
                         json.dumps(dict(schema='ameesh-sandbox-receipt/1', turn_id=self.turn, epoch=7,
                                         scope={}, scope_digest='digest', result_id='r', result_sha256='hash',
                                         job={}, source={}, replayed=False))))
        self.db.execute("update tranche_reservations set state='kept' where turn_id=%s", (self.turn,))
        # Admission snapshots can contain integration-specific metadata. The
        # public projection exposes only contract keys, even inside receipts.
        self.db.execute("update sandbox_results set receipt=receipt || %s::jsonb where turn_id=%s",
                        (json.dumps({'source': {'contract': 'ameesh-sandbox/1', 'token': 'private-source'},
                                     'job': {'uid': 'uid', 'token': 'private-job'},
                                     'scope': {'repository': 'a/b', 'token': 'private-scope'}}), self.turn))
        document = obs.show(self.db, self.turn)
        self.assertEqual(errors(serve.SCHEMAS['ameesh-sandbox-turn/1'], document), [])
        self.assertEqual(document['verdict'], 'green')
        self.assertEqual(document['counters']['tool_calls'], 2)
        self.assertEqual(document['tranche']['kept_usd'], 2)
        self.assertIsNone(document['tranche']['settled_usd'])
        self.assertNotIn('private-', json.dumps(document))
        self.assertEqual(document['receipt']['result_id'], 'r')

    def test_alerts_come_from_durable_facts_and_do_not_write(self):
        self.assertEqual(exploitation._sandbox_alerts(self.db, time.time()), [])
        self.db.execute("update sandbox_turns set job_phase='Succeeded',refusal='revoked' where turn_id=%s", (self.turn,))
        self.db.execute("update agent_registry set lease_expires_at=now()-interval '1 second' where name='sand'")
        types = {a['type'] for a in exploitation._sandbox_alerts(self.db, time.time()+7200)}
        self.assertEqual(types, {'sandbox_orphan', 'sandbox_stale', 'sandbox_result_unread'})
        self.db.execute("update sandbox_turns set state='cancelled',cancel_observed_at=now() where turn_id=%s", (self.turn,))
        self.assertEqual(exploitation._sandbox_alerts(self.db, time.time()+7200), [])

    def test_bound_event_once_and_orphan_event(self):
        sandbox.ameesh_sandbox_bind(self.db, self.turn, 'runner', 7, 'uid')
        sandbox.ameesh_sandbox_bind(self.db, self.turn, 'runner', 7, 'uid')
        with self.assertRaises(sandbox.Refusal):
            sandbox.observe_metadata(self.db, self.turn, 'runner', 7,
                                     job_uid='other', job_phase='Failed')
        self.assertIsNone(obs.show(self.db, self.turn)['job']['phase'])
        sandbox.ameesh_sandbox_cancel(self.db, self.turn, 'runner', 7, orphaned=True)
        sandbox.ameesh_sandbox_cancel(self.db, self.turn, 'runner', 7, orphaned=True)
        rows = self.db.query("select type from ameesh_events where object=%s and type like 'sandbox.%%'", ('turn:'+self.turn,))
        types = [r['type'] for r in rows]
        self.assertEqual(types.count('sandbox.bound'), 1)
        self.assertEqual(types.count('sandbox.orphan'), 1)

    def test_supervisor_metadata_is_fenced_and_lifecycle_unchanged(self):
        sandbox.observe_metadata(self.db, self.turn, 'runner', 7,
                                 job_phase='Succeeded', refusal='revoked')
        document = obs.show(self.db, self.turn)
        self.assertEqual(document['state'], 'intent')
        self.assertEqual(document['job']['phase'], 'Succeeded')
        self.assertEqual(document['refusal'], 'revoked')
        with self.assertRaises(sandbox.Refusal):
            sandbox.observe_metadata(self.db, self.turn, 'runner', 6, job_phase='Failed')
        self.assertEqual(obs.show(self.db, self.turn), document)
        with self.assertRaises(sandbox.Refusal):
            sandbox.observe_metadata(self.db, self.turn, 'runner', 7, refusal='private transcript')

    def test_observation_still_composes_after_resource_retention(self):
        self.db.execute('delete from turn_resources where turn_id=%s', (self.turn,))
        document = turn_observation.observe(self.db, turn=self.turn)
        self.assertEqual(document['sandbox']['turn_id'], self.turn)
        self.assertEqual(document['turn']['state'], 'unknown')
        self.assertEqual(document['accounting']['state'], 'unknown')

    def test_current_uses_verified_identity_and_adds_the_same_sandbox(self):
        from ameesh import turn_context
        from .test_l2894_turn_context import _document
        document = _document(turn_id=self.turn, agent='sand', host='h1',
                             lease_owner='runner', lease_epoch=7,
                             marker={'start': 1, 'created_ts': time.time(),
                                     'model': 'test', 'work_item_id': '4513', 'turn_id': self.turn})
        filename = turn_context.write(self.tmp, document, secrets=[])
        registry.pending_spend_begin(self.db, 'sand', 1, turn_id=self.turn)
        env = self.env(AMEESH_TURN_CONTEXT=filename, AMEESH_TURN_ID=self.turn,
                       AGENT_MAIL_NAME='sand', AMEESH_RUNNER_ID='runner', AMEESH_LEASE_EPOCH='7')
        result = self.mesh('turn', 'current', '--json', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sandbox'], obs.show(self.db, self.turn))
        local = self.mesh('turn', 'current', '--local', '--json', env=env)
        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertNotIn('sandbox', json.loads(local.stdout))

    def test_cli_session_refuses_a_write(self):
        from ameesh import db as db_mod, sandbox_cli
        def try_write(db, turn):
            db.execute("update sandbox_turns set refusal='revoked' where turn_id=%s", (turn,))
        with mock.patch.object(obs, 'show', side_effect=try_write):
            with self.assertRaises(db_mod.DbError):
                sandbox_cli.cmd_read(self.cfg, SimpleNamespace(sandbox_command='show', turn=self.turn, json=True))
        self.assertIsNone(obs.show(self.db, self.turn)['refusal'])

    def test_psql_reads_the_same_projection_and_current_turn(self):
        from ameesh import db as db_mod
        db = db_mod.connect(self.cfg, driver='psql')
        try:
            expected = obs.show(self.db, self.turn)
            self.assertEqual(obs.show(db, self.turn), expected)
            self.assertEqual(obs.current_agents(db, ['sand', 'missing']), {'sand': expected})
            self.assertEqual(obs.listing(db)['turns'], [expected])
        finally:
            db.close()
