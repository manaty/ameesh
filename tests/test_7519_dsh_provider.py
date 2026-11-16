# SPDX-License-Identifier: AGPL-3.0-only
import copy
import json
import types
import unittest
from decimal import Decimal
from unittest import mock

from ameesh import cycle_budget, dsh_provider as provider
from .support import PgTestCase


def config():
    return {'provider': 'test-route', 'endpoint': 'https://provider.invalid/v1',
            'key_env': 'SELECTED_API_KEY', 'billing_account': 'test-billing',
            'models': {'gpt-test': {'context_tokens': 1000, 'max_output_tokens': 100,
                                   'tariffs': {'default': ['2', '0.2', '8'], 'priority': ['3', '0.3', '12']},
                                   'source': 'dated-provider-contract'}}}


def response(**extra):
    return {'status': 'completed', 'model': 'gpt-test', 'service_tier': 'default',
            'usage': {'input_tokens': 50, 'output_tokens': 10, 'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 7}}, **extra}


class ConfigurationTest(unittest.TestCase):
    def test_requires_explicit_policy_and_safe_endpoint(self):
        for field, value in [('endpoint', 'http://provider.invalid'),
                             ('endpoint', 'https://secret@provider.invalid'),
                             ('endpoint', 'https://provider.invalid/?key=secret'),
                             ('key_env', 'not a reference'), ('models', {}),
                             ('billing_account', ''), ('provider', 'route\ninjection')]:
            raw = config()
            raw[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                provider.parse(raw)

    def test_missing_prices_context_or_evidence_refuses(self):
        for field, value in [('tariffs', {}), ('context_tokens', 0),
                             ('max_output_tokens', True), ('source', '')]:
            raw = config()
            raw['models']['gpt-test'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                provider.parse(raw)

    def test_credential_destination_cannot_be_an_automatic_route_or_runtime_variable(self):
        for name in ('DEEPSEEK_API_KEY', 'CODEX_API_KEY', 'GH_TOKEN', 'AMEESH_DSN',
                     'DSH_HOME', 'HOME', 'PATH', 'LD_PRELOAD'):
            raw = config()
            raw['key_env'] = name
            with self.subTest(name=name), self.assertRaisesRegex(
                    provider.ProviderError, 'dedicated environment reference'):
                provider.parse(raw)

    def test_response_model_identities_are_an_explicit_list(self):
        for aliases in ('gpt-test-snapshot', [None], ['gpt-test\ninjection']):
            raw = config()
            raw['models']['gpt-test']['response_models'] = aliases
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                provider.parse(raw)

    def test_account_replaces_host_in_full(self):
        host = config()
        account = config()
        account['provider'] = 'account-route'
        cfg = types.SimpleNamespace(dsh_provider=host, accounts={'deepseek': [
            {'name': 'chosen', 'dsh_provider': account}]})
        profile = types.SimpleNamespace(name='chosen', billing_account='test-billing')
        self.assertEqual(provider.configured(cfg, profile).name, 'account-route')
        cfg.accounts['deepseek'][0]['dsh_provider'] = {'provider': 'incomplete'}
        with self.assertRaises(ValueError):
            provider.configured(cfg, profile)

    def test_billing_mismatch_refuses(self):
        cfg = types.SimpleNamespace(dsh_provider=config(), accounts={})
        with self.assertRaises(ValueError):
            provider.configured(cfg, types.SimpleNamespace(name='chosen', billing_account='other'))

    def test_route_patch_is_after_descriptor_patch_before_app_arguments(self):
        self.assertEqual(provider.provider_argv(
            ['dsh', '--profile', 'agent', '--patch', 'model.yml', '--json', 'prompt'], 'provider.yml'),
            ['dsh', '--profile', 'agent', '--patch', 'model.yml', '--patch', 'provider.yml', '--json', 'prompt'])

    def test_absent_config_preserves_existing_route(self):
        self.assertIsNone(provider.configured(types.SimpleNamespace()))

    def test_patch_has_one_explicit_responses_route_and_no_secret(self):
        route = provider.parse(config())
        patch = provider.patch_document(route, 'gpt-test', 'high', 'http://127.0.0.1:123/v1', 'BROKER_KEY')
        entries = {entry['id']: entry for entry in patch}
        for name in provider.DISABLED:
            self.assertTrue(entries[name]['disabled'])
        route_config = entries['llm-pi-ai']['config']['providers']['test-route']
        self.assertEqual(route_config['api'], 'openai-responses')
        self.assertEqual(route_config['apiKeyEnv'], 'BROKER_KEY')
        self.assertEqual(route_config['retryPolicy']['maxRetries'], 0)
        self.assertEqual(entries['agent-default-model']['config']['provider'], 'test-route')
        self.assertNotIn('SELECTED_API_KEY', json.dumps(patch))


class WireTest(unittest.TestCase):
    def setUp(self):
        self.route = provider.parse(config())

    def test_quote_uses_whole_context_and_most_expensive_tier(self):
        outgoing, bound = provider.prepare(self.route, {'model': 'gpt-test', 'input': 'hello'})
        self.assertEqual(outgoing['max_output_tokens'], 100)
        self.assertFalse(outgoing['store'])
        self.assertEqual(bound.quote()[0], 4200)

    def test_refuses_unpriced_or_hidden_billable_routes(self):
        for extra in ({'model': 'unknown'}, {'previous_response_id': 'resp-history'},
                      {'tools': [{'type': 'web_search'}]}, {'service_tier': 'auto'},
                      {'max_output_tokens': 101}, {'max_output_tokens': True},
                      {'store': True}, {'truncation': 'auto'}, {'background': True},
                      {'input': [{'type': 'input_image', 'image_url': 'https://image.invalid'}]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                provider.prepare(self.route, {'model': 'gpt-test', **extra})

    def test_reasoning_is_not_charged_twice(self):
        outgoing, _ = provider.prepare(self.route, {'model': 'gpt-test'})
        self.assertEqual(provider.response_cost(self.route, outgoing, response()), Decimal('0.00018'))

    def test_only_explicit_snapshot_names_share_the_requested_model_tariffs(self):
        raw = config()
        raw['models']['gpt-test']['response_models'] = ['gpt-test-2026-01-01']
        route = provider.parse(raw)
        outgoing, _ = provider.prepare(route, {'model': 'gpt-test'})
        self.assertEqual(provider.response_cost(route, outgoing,
                         response(model='gpt-test-2026-01-01')), Decimal('0.00018'))
        self.assertIsNone(provider.response_cost(route, outgoing,
                          response(model='gpt-test-2026-02-01')))

    def test_partial_or_invalid_usage_retains_full_reservation(self):
        outgoing, _ = provider.prepare(self.route, {'model': 'gpt-test'})
        for result in (None, response(usage={}), response(model='alias'),
                       response(status='in_progress'), response(usage={'input_tokens': -1, 'output_tokens': 5})):
            self.assertIsNone(provider.response_cost(self.route, outgoing, result))

    def test_terminal_usage_from_stream(self):
        raw = b'data: ' + json.dumps({'type': 'response.completed', 'response': response()}).encode() + b'\n\ndata: [DONE]\n'
        self.assertEqual(provider.terminal(raw, True), response())
        self.assertIsNone(provider.terminal(b'data: {"type":"response.created"}\n', True))


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.api = mock.Mock()
        self.api.cycle_reserve.return_value = {'ok': True}
        self.db = mock.Mock()
        self.record = mock.Mock()
        self.sent = []
        def send(route, key, outgoing):
            self.assertEqual(key, 'fake-provider-secret')
            self.assertTrue(self.api.cycle_reserve.called)
            self.sent.append(copy.deepcopy(outgoing))
            return json.dumps(response()).encode(), 'application/json'
        self.broker = provider.Broker(provider.parse(config()), 'fake-provider-secret',
                                      database=lambda: self.db, agent='test-agent',
                                      daily_admit=lambda *args: True, record=self.record,
                                      daily_finish=mock.Mock(), send=send)
        self.addCleanup(self.broker.server.server_close)
        self.patch = mock.patch('ameesh.cycle_budget._api', return_value=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_each_main_tool_compaction_subagent_retry_is_separately_admitted(self):
        # At this boundary the origin is irrelevant: every network request is
        # independently reserved, including an internal pi-ai retry.
        for origin in ('main', 'reasoning', 'tool-followup', 'compaction', 'subagent', 'retry'):
            self.broker.request({'model': 'gpt-test', 'input': origin})
        self.assertEqual(len(self.sent), 6)
        ids = [call.args[2] for call in self.api.cycle_reserve.call_args_list]
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(self.api.cycle_settle.call_count, 6)
        self.assertEqual(self.record.call_count, 6)

    def test_daily_refusal_sends_nothing(self):
        self.broker.daily_admit = lambda *args: False
        with self.assertRaises(cycle_budget.CreditRefused):
            self.broker.request({'model': 'gpt-test'})
        self.assertFalse(self.sent)
        self.api.cycle_reserve.assert_not_called()

    def test_credit_refusal_sends_nothing(self):
        self.api.cycle_reserve.side_effect = RuntimeError('exhausted')
        with self.assertRaises(cycle_budget.CreditRefused):
            self.broker.request({'model': 'gpt-test'})
        self.assertFalse(self.sent)

    def test_transport_error_keeps_credit_and_records_daily_bound(self):
        self.broker.send = mock.Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            self.broker.request({'model': 'gpt-test'})
        self.assertIsNone(self.api.cycle_settle.call_args.args[2])
        self.assertEqual(self.record.call_args.args[-1], Decimal('0.0042'))
        with self.assertRaises(provider.ProviderError):
            self.broker.request({'model': 'gpt-test'})

    def test_missing_usage_keeps_credit(self):
        self.broker.send = lambda *args: (b'{"status":"completed"}', 'application/json')
        self.broker.request({'model': 'gpt-test'})
        self.assertIsNone(self.api.cycle_settle.call_args.args[2])
        self.assertEqual(self.record.call_args.args[-1], Decimal('0.0042'))

    def test_accounting_failure_closes_broker(self):
        self.record.side_effect = RuntimeError('storage failed')
        with self.assertRaises(RuntimeError):
            self.broker.request({'model': 'gpt-test'})
        self.assertTrue(self.broker.failed)
        self.assertEqual(len(self.sent), 1)
        with self.assertRaises(provider.ProviderError):
            self.broker.request({'model': 'gpt-test'})

    def test_close_refuses_further_requests_before_admission_or_dispatch(self):
        self.broker.close()
        with self.assertRaisesRegex(provider.ProviderError, 'broker is closed'):
            self.broker.request({'model': 'gpt-test'})
        self.assertFalse(self.sent)
        self.api.cycle_reserve.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(self.broker._key, '')
        with self.assertRaisesRegex(provider.ProviderError, 'broker is closed'):
            self.broker.start()

    def test_http_token_and_route_are_required(self):
        import urllib.error
        import urllib.request
        self.broker.start()
        self.addCleanup(self.broker.close)
        for path, token in [('/responses', 'wrong'), ('/models', self.broker.token)]:
            request = urllib.request.Request(self.broker.base + path, data=b'{"model":"gpt-test"}',
                                             headers={'Authorization': 'Bearer ' + token})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(request)
        self.assertFalse(self.sent)


class DshIntegrationTest(unittest.TestCase):
    def test_installed_dsh_uses_guarded_responses_only(self):
        import os
        import shutil
        import subprocess
        import tempfile
        binary = shutil.which('dsh')
        if not binary:
            self.skipTest('dsh unavailable; fake broker contract is tested separately')
        version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=10)
        if '0.2.1-rc.4' not in version.stdout:
            self.skipTest('installed dsh composition contract differs from 0.2.1-rc.4')
        raw = config()
        raw['models']['gpt-test']['reasoning_efforts'] = {'off': None, 'high': 'high'}
        route = provider.parse(raw)
        calls = []
        def send(route, key, body):
            calls.append(body)
            result = response(id='resp_fake', object='response', created_at=1,
                              output=[{'type': 'message', 'id': 'msg_fake', 'role': 'assistant',
                                       'status': 'completed', 'content': [
                                           {'type': 'output_text', 'text': 'Fake supplier OK', 'annotations': []}]}])
            events = [
                {'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0, 'item': result['output'][0]},
                {'type': 'response.content_part.added', 'item_id': 'msg_fake', 'output_index': 0,
                 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                {'type': 'response.output_text.delta', 'item_id': 'msg_fake', 'output_index': 0,
                 'content_index': 0, 'delta': 'Fake supplier OK'},
                {'type': 'response.output_text.done', 'item_id': 'msg_fake', 'output_index': 0,
                 'content_index': 0, 'text': 'Fake supplier OK'},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': result['output'][0]},
                {'type': 'response.completed', 'response': result},
            ]
            return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode(), 'text/event-stream'
        api = mock.Mock()
        broker = provider.Broker(route, 'fake-secret', database=mock.Mock, agent='test-agent',
                                  daily_admit=lambda *args: True, record=mock.Mock(),
                                  daily_finish=mock.Mock(), send=send).start()
        seen = []
        original = broker.request
        def observed(body):
            seen.append(body)
            return original(body)
        broker.request = observed
        try:
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory, \
                 mock.patch('ameesh.cycle_budget._api', return_value=api):
                path = os.path.join(directory, 'patch.yml')
                with open(path, 'w') as handle:
                    json.dump(provider.patch_document(route, 'gpt-test', 'high', broker.base, 'BROKER_KEY'), handle)
                result = subprocess.run(provider.provider_argv([binary, '--profile', 'headless', '--json', 'Say OK'], path),
                                        env={'PATH': os.environ['PATH'], 'HOME': directory, 'DSH_HOME': directory,
                                             'BROKER_KEY': broker.token}, cwd=directory,
                                        capture_output=True, text=True, timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr[-1000:] + str([sorted(b) for b in seen]))
                self.assertEqual(len(calls), 1)
                self.assertIn('Fake supplier OK', result.stdout)
                api.cycle_reserve.assert_called_once()
                api.cycle_settle.assert_called_once()
        finally:
            broker.close()


class ProviderLedgerTest(PgTestCase):
    def setUp(self):
        super().setUp()
        from datetime import datetime, timedelta
        from ameesh import storage
        self.db.execute('delete from credit_requests')
        self.db.execute('delete from credit_cycles')
        self.db.execute('delete from account_reservations')
        self.db.execute('delete from account_budgets')
        self.db.execute('delete from turn_costs')
        now = datetime.fromisoformat(self.db.query('select clock_timestamp()::text as at')[0]['at'])
        storage.of(self.db).budgets.cycle_put(
            'test-billing', 1_000_000, 1_000_000,
            (now - timedelta(minutes=1)).isoformat(),
            (now + timedelta(days=1)).isoformat(),
            (now + timedelta(hours=1)).isoformat(), 'fake-statement', 'human:test')
        storage.of(self.db).budgets.account_put('test-billing', 1, actor='human:test')

    def launch(self):
        import os
        import threading
        from ameesh import accounts
        from ameesh.platform import child_environment
        directory = os.path.join(self.tmp, 'test-agent')
        os.makedirs(directory, exist_ok=True)
        worker = types.SimpleNamespace(
            name='test-agent', cfg=self.cfg, lease_lost=threading.Event(),
            runner=types.SimpleNamespace(mediated=False, budget_limits=lambda db:
                                         types.SimpleNamespace(book_kwargs=lambda: {})),
            _path=lambda name: os.path.join(directory, name))
        profile = accounts.Profile(harness='deepseek', name='chosen', kind='api_key_env',
                                   env='SELECTED_API_KEY', billing_account='test-billing', reserve_usd=0.01)
        env = child_environment.grant({'SELECTED_API_KEY': 'fake-real-key', 'PATH': os.environ['PATH']},
                                       {'SELECTED_API_KEY'})
        with mock.patch('ameesh.db.open_db', side_effect=lambda cfg: self.connect()):
            broker, argv = provider.launch(worker, provider.parse(config()), profile, env,
                                           ['dsh', '--profile', 'headless', '--json'], 'gpt-test', 'high', 'test-turn')
        # launch's database callback opens a new connection for every request.
        patch = mock.patch('ameesh.db.open_db', side_effect=lambda cfg: self.connect())
        patch.start()
        self.addCleanup(patch.stop)
        broker.send = lambda *args: (json.dumps(response()).encode(), 'application/json')
        self.addCleanup(broker.close)
        self.assertNotIn('fake-real-key', str(env))
        self.assertEqual(env['SELECTED_API_KEY'], broker.token)
        self.assertEqual(argv[-3], '--patch')
        return broker

    def test_response_is_recorded_once_and_daily_bound_settles(self):
        from ameesh import cost, storage
        broker = self.launch()
        with mock.patch('ameesh.cost.CostBook.over', return_value=''):
            broker.request({'model': 'gpt-test', 'input': 'test'})
        rows = self.db.query("select harness, source, billing_account, usd::float8 as usd from turn_costs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['harness'], 'deepseek')
        self.assertEqual(rows[0]['source'], 'relay')
        self.assertEqual(rows[0]['billing_account'], 'test-billing')
        self.assertAlmostEqual(rows[0]['usd'], 0.00018)
        usage = storage.of(self.db).budgets.account_usage('test-billing')[0]
        self.assertEqual(usage['reserved'], 0)
        self.assertAlmostEqual(usage['settled'], 0.00018)
        book = cost.CostBook(state_dir=self.tmp, db=self.db, tools={'test-agent': 'deepseek'})
        self.assertEqual(book.turn_usage('test-agent', model='gpt-test').usd, 0)

    def test_atomic_daily_reservation_refuses_before_provider(self):
        from ameesh import storage
        storage.of(self.db).budgets.account_put('test-billing', 0.001, actor='human:test')
        broker = self.launch()
        broker.send = mock.Mock()
        with mock.patch('ameesh.cost.CostBook.over', return_value=''), \
             self.assertRaises(cycle_budget.CreditRefused):
            broker.request({'model': 'gpt-test'})
        broker.send.assert_not_called()
        self.assertEqual(self.db.query('select count(*) as n from credit_requests')[0]['n'], 0)

    def test_small_requests_fit_without_reserving_a_second_whole_turn(self):
        from ameesh import storage
        api = storage.of(self.db).budgets
        api.account_put('test-billing', 0.02, actor='human:test')
        self.assertTrue(api.account_reserve('test-billing', 'test-agent', 0.01, 'turn')['ok'])
        broker = self.launch()
        with mock.patch('ameesh.cost.CostBook.over', return_value=''):
            for _ in range(2):
                broker.request({'model': 'gpt-test'})
        usage = api.account_usage('test-billing')[0]
        self.assertAlmostEqual(usage['reserved'], 0.01)
        self.assertAlmostEqual(usage['settled'], 0.00036)
        self.assertEqual(api.account_get('turn')['state'], 'open')

    def test_malformed_usage_still_records_and_settles_the_daily_bound(self):
        from ameesh import storage
        broker = self.launch()
        for result in ([], response(usage=[]), response(usage={'input_tokens': 'oops',
                       'output_tokens': None, 'input_tokens_details': []})):
            broker.send = lambda *args: (json.dumps(result).encode(), 'application/json')
            with mock.patch('ameesh.cost.CostBook.over', return_value=''):
                broker.request({'model': 'gpt-test'})
        rows = self.db.query('select usd::float8 as usd, input_tokens, cached_input_tokens,'
                             ' output_tokens from turn_costs')
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertAlmostEqual(row['usd'], 0.0042)
            self.assertEqual((row['input_tokens'], row['cached_input_tokens'], row['output_tokens']), (0, 0, 0))
        usage = storage.of(self.db).budgets.account_usage('test-billing')[0]
        self.assertEqual(usage['reserved'], 0)
        self.assertAlmostEqual(usage['settled'], 0.0126)

    def test_session_usage_survives_restart_without_double_cost_or_extra_turns(self):
        from ameesh import cost, storage
        broker = self.launch()
        result = response()
        result['usage']['input_tokens_details']['cached_tokens'] = 20
        broker.send = lambda *args: (json.dumps(result).encode(), 'application/json')
        with mock.patch('ameesh.cost.CostBook.over', return_value=''):
            for _ in range(2):
                broker.request({'model': 'gpt-test'})
        book = cost.CostBook(state_dir=self.tmp, db=self.db, tools={'test-agent': 'deepseek'})
        book.record('test-agent', session='dsh-session', model='gpt-test',
                    turn_id='test-turn', key='native-turn')
        # A fresh reader uses only durable state. The response rows account
        # for dollars; the single native timeline row carries session tokens.
        fresh = cost.CostBook(state_dir=self.tmp, db=self.db, tools={'test-agent': 'deepseek'})
        total = storage.of(fresh.db).turn_costs.session_total('test-agent', 'dsh-session')
        reread = storage.of(fresh.db).turn_costs.session_reread('test-agent', 'dsh-session')
        self.assertEqual((total['tokens'], total['turns']), (80, 1))
        self.assertEqual((reread['tokens'], reread['turns']), (100, 1))
        self.assertAlmostEqual(fresh.spent('test-agent', 3600), 0.000288)
        self.assertEqual(storage.of(self.db).turn_costs.provider_usage('other-agent', 'test-turn'),
                         dict(input_tokens=0, cached_input_tokens=0, output_tokens=0))
        self.assertEqual(storage.of(self.db).turn_costs.provider_usage('test-agent', 'other-turn'),
                         dict(input_tokens=0, cached_input_tokens=0, output_tokens=0))
        with self.assertRaisesRegex(cost.CostError, 'does not identify this turn'):
            fresh.turn_usage('test-agent', model='gpt-test', turn_id='other-turn')

    def test_cycle_refusal_releases_proven_unsent_daily_reservation(self):
        from ameesh import storage
        self.db.execute('delete from credit_cycles')
        broker = self.launch()
        broker.send = mock.Mock()
        with mock.patch('ameesh.cost.CostBook.over', return_value=''), \
             self.assertRaises(cycle_budget.CreditRefused):
            broker.request({'model': 'gpt-test'})
        broker.send.assert_not_called()
        self.assertEqual(storage.of(self.db).budgets.account_usage('test-billing')[0]['reserved'], 0)
        self.assertEqual(self.db.query('select state from account_reservations')[0]['state'], 'released')


class RunnerSelectionTest(unittest.TestCase):
    def test_gpt_model_stays_on_deepseek_harness(self):
        from ameesh.runner import AgentWorker
        worker = types.SimpleNamespace(
            cfg=types.SimpleNamespace(dsh_provider=config(), accounts={}),
            agent={'model': 'gpt-test', 'harness': 'deepseek'},
            _state_read=lambda key: '', _account=None)
        self.assertEqual(AgentWorker.modele_du_tour(worker, 'deepseek', None, emprunte=False), 'gpt-test')
        worker.agent['model'] = 'gpt-unpriced'
        with self.assertRaises(provider.ProviderError):
            AgentWorker.modele_du_tour(worker, 'deepseek', None, emprunte=False)
        worker.agent['model'] = ''
        self.assertEqual(AgentWorker.modele_du_tour(worker, 'deepseek', None, emprunte=False), 'gpt-test')


class TransportTest(unittest.TestCase):
    def test_upstream_failure_is_not_retried_or_redirected(self):
        route = provider.parse(config())
        connection = mock.Mock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.read.return_value = b'credential-bearing error body'
        with mock.patch('http.client.HTTPSConnection', return_value=connection), \
             self.assertRaisesRegex(provider.ProviderError, '^provider request did not complete$'):
            provider.transport(route, 'fake-key', {'model': 'gpt-test'})
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[1], '/v1/responses')
        connection.close.assert_called_once()

    def test_cached_input_is_priced_from_complete_usage(self):
        route = provider.parse(config())
        request, _ = provider.prepare(route, {'model': 'gpt-test'})
        result = response()
        result['usage']['input_tokens_details']['cached_tokens'] = 20
        self.assertEqual(provider.response_cost(route, request, result), Decimal('0.000144'))


class BalanceIsolationTest(unittest.TestCase):
    def test_unreadable_accounts_never_fall_back_to_an_ambient_key(self):
        from ameesh import accounts, balance
        cfg = types.SimpleNamespace(dsh_provider={}, accounts={'deepseek': [
            {'name': 'alternate', 'dsh_provider': config()}]})
        transport = mock.Mock()
        with mock.patch('ameesh.accounts.profiles', side_effect=accounts.AccountError('invalid account')):
            sources = balance.account_sources(cfg, env={'DEEPSEEK_API_KEY': 'fake-alternate-key'},
                                              names=['deepseek'], transport=transport)
        self.assertEqual(sources, [])
        transport.assert_not_called()

    def test_invalid_alternate_route_does_not_read_or_probe_its_key(self):
        from ameesh import accounts, balance
        profile = accounts.Profile(harness='deepseek', name='chosen', kind='api_key_env',
                                   env='SELECTED_API_KEY', key_env='FAKE_SOURCE',
                                   billing_account='test-billing')
        cfg = types.SimpleNamespace(dsh_provider={'provider': 'incomplete'}, accounts={})
        with mock.patch('ameesh.accounts.profiles', return_value=[profile]), \
             mock.patch('ameesh.accounts.apply_env') as read_key:
            self.assertEqual(balance.account_sources(cfg, env={'FAKE_SOURCE': 'fake'}, names=['deepseek']), [])
            read_key.assert_not_called()

    def test_configured_route_never_probes_official_deepseek_with_selected_key(self):
        from ameesh import accounts, balance
        profile = accounts.Profile(harness='deepseek', name='chosen', kind='api_key_env',
                                   env='SELECTED_API_KEY', key_env='FAKE_SOURCE',
                                   billing_account='test-billing')
        cfg = types.SimpleNamespace(dsh_provider=config(), accounts={})
        with mock.patch('ameesh.accounts.profiles', return_value=[profile]), \
             mock.patch('ameesh.accounts.apply_env') as read_key:
            self.assertEqual(balance.account_sources(cfg, env={'FAKE_SOURCE': 'fake'}, names=['deepseek']), [])
            read_key.assert_not_called()
