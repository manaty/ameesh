# SPDX-License-Identifier: AGPL-3.0-only
"""H1: handoff contract and pinned enrolled factory, with no host effect.

The factory only composes exact enrollment documents and the fixed companion;
these tests use disposable private documents and never a host, SSH, root
operation, credential or database.
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from ameesh import handoff_contract, handoffs, host_preflight


SPEC = importlib.util.spec_from_file_location(
    'handoff_factory',
    Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/fabrique-handoff.py')
factory_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(factory_module)


def operation(**changes):
    value = {
        'id': 'a' * 32,
        'agent': 'dev',
        'source': 'source',
        'target': 'target',
        'controller_epoch': 3,
        'evidence': {},
        'plan': {'repository': 'git@forge:manaty/ameesh.git',
                 'owner': 'runner', 'epoch': 7, 'reason': 'host_drain'},
    }
    value.update(changes)
    return value


class ContractTest(unittest.TestCase):
    def test_complete_operation_carries_identity_lease_and_epoch(self):
        for method in handoff_contract.REQUEST_METHODS:
            with self.subTest(method=method):
                if method == 'reconcile_effect':
                    value = operation(evidence={'pending_effect': {'epoch': 6}})
                else:
                    value = operation()
                self.assertTrue(handoff_contract.validate_request(value, method))

    def test_missing_operation_identity_is_refused(self):
        for field in ('id', 'agent', 'source', 'target', 'plan',
                      'controller_epoch', 'evidence'):
            with self.subTest(field=field):
                value = operation()
                value.pop(field)
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_request(value, 'prepare')

    def test_invalid_identity_and_epoch_values_are_refused(self):
        cases = [
            {'id': ''}, {'id': 'bad id'}, {'id': 'x\ny'}, {'agent': 'bad name'},
            {'source': ''}, {'target': '..'}, {'controller_epoch': 0},
            {'controller_epoch': True}, {'controller_epoch': '3'},
            {'evidence': []}, {'plan': []},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_request(operation(**changes), 'prepare')

    def test_lease_and_repository_are_required(self):
        self.assertTrue(handoff_contract.validate_request(
            operation(plan={'repository': 'git@forge:manaty/ameesh.git',
                            'owner': 'host:1234', 'epoch': 1, 'reason': 'host_drain'}),
            'prepare'))
        for plan in ({'repository': '', 'owner': 'runner', 'epoch': 1},
                     {'repository': 'r', 'owner': '', 'epoch': 1},
                     {'repository': 'r', 'owner': 'bad\nowner', 'epoch': 1},
                     {'repository': 'r', 'owner': 'runner'},
                     {'repository': 'r', 'owner': 'runner', 'epoch': True},
                     {'repository': 'r', 'owner': 'runner', 'epoch': -1}):
            with self.subTest(plan=plan):
                value = operation(plan=plan)
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_request(value, 'prepare')

    def test_lost_source_may_have_no_live_lease_owner(self):
        value = operation(plan={'repository': 'git@forge:manaty/ameesh.git',
                                'owner': None, 'epoch': 7, 'reason': 'host_loss'})
        self.assertTrue(handoff_contract.validate_request(value, 'recover_source'))
        value['plan']['reason'] = 'host_drain'
        with self.assertRaises(handoff_contract.ContractError):
            handoff_contract.validate_request(value, 'prepare')

    def test_reconciliation_requires_the_unfinished_effect_epoch(self):
        with self.assertRaises(handoff_contract.ContractError):
            handoff_contract.validate_request(operation(), 'reconcile_effect')
        value = operation(evidence={'pending_effect': {'epoch': True}})
        with self.assertRaises(handoff_contract.ContractError):
            handoff_contract.validate_request(value, 'reconcile_effect')
        value['evidence']['pending_effect']['epoch'] = 6
        self.assertTrue(handoff_contract.validate_request(value, 'reconcile_effect'))

    def test_discovery_methods_are_not_effect_requests(self):
        for method in ('inventory', 'repository', 'unknown'):
            with self.subTest(method=method), self.assertRaises(handoff_contract.ContractError):
                handoff_contract.validate_request(operation(), method)

    def test_controller_passes_the_full_contract_operation(self):
        received = []

        class Adapter:
            def revalidate(self, value):
                received.append(value)
                return True

        request = operation()
        self.assertTrue(handoffs._adapter_call(Adapter(), 'revalidate', request))
        self.assertEqual(received, [request])

    def test_controller_refuses_before_entering_the_adapter(self):
        class Spy:
            called = False

            def revalidate(self, value):
                self.called = True
                return True

        spy = Spy()
        with self.assertRaisesRegex(handoffs.HandoffError, 'contract refused'):
            handoffs._adapter_call(spy, 'revalidate', operation(controller_epoch=0))
        self.assertFalse(spy.called)
        with self.assertRaisesRegex(handoffs.HandoffError, 'method missing'):
            handoffs._adapter_call(SimpleNamespace(), 'revalidate', operation())
        self.assertFalse(spy.called)

    def test_controller_steps_are_contract_effects(self):
        self.assertLessEqual({step for step, _ in handoffs.STEPS.values()},
                             set(handoff_contract.EFFECTS))
        for step in ('recover_source', 'reconcile_effect', 'restore_source'):
            self.assertIn(step, handoff_contract.EFFECTS)

    def test_missing_adapter_method_is_named(self):
        class Incomplete:
            inventory = revalidate = prepare = lambda self, value: True

        with self.assertRaisesRegex(handoff_contract.ContractError, 'missing handoff methods'):
            handoff_contract.require_methods(Incomplete(), ('inventory', 'revalidate',
                                                            'prepare', 'verify'))


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.host = 'ameesh-agents-2'
        self.documents = {
            'enrollment': {'version': 1, 'host': self.host},
            'hosts': {'hosts': {
                self.host: {
                    'codex': {'accounts': ['', 'mesh'], 'tools': ['gh', 'gcloud'],
                              'secrets': ['codex/api']},
                },
                'target': {
                    'deepseek': {'accounts': ['main'], 'tools': ['gh'],
                                 'secrets': ['deepseek/api']},
                },
            }},
            'repositories': {'repositories': {
                'ameesh': 'git@forge:manaty/ameesh.git',
                'nexlink': 'https://forge.example/manaty/nexlink.git',
            }},
            'secrets': {'references': {
                'codex/api': 'harnais.env#OPENAI_API_KEY',
                'deepseek/api': 'harnais.env#DEEPSEEK_API_KEY',
            }},
        }
        for name, value in self.documents.items():
            self.write(name, value)

    def write(self, name, value=None, mode=0o600):
        path = self.root / (name + '.json')
        if value is not None:
            path.write_text(json.dumps(value))
        path.chmod(mode)

    def factory(self, **kwargs):
        return factory_module.HandoffFactory(root=self.root, **kwargs)

    def agent(self, **changes):
        value = {'name': 'dev', 'harness': 'codex', 'session_account': None,
                 'team': 'ameesh'}
        value.update(changes)
        return value

    def test_factory_admits_and_provides_every_contract_method(self):
        value = self.factory()
        self.assertIsInstance(value.requirements_for(self.agent(), self.host),
                              host_preflight.Requirements)
        for method in handoff_contract.METHODS:
            with self.subTest(method=method):
                self.assertTrue(callable(getattr(value.adapter, method, None)))

    def test_requirements_are_the_exact_enrolled_identities(self):
        value = self.factory()
        required = value.requirements_for(self.agent(), self.host)
        self.assertEqual(required, host_preflight.Requirements(
            host=self.host, harness='codex', account='',
            tools=('gh', 'gcloud'), secret_refs=('codex/api',)))
        self.assertNotIn('#', ' '.join(required.secret_refs))
        self.assertEqual(value.references_for(self.agent(), self.host),
                         ('harnais.env#OPENAI_API_KEY',))

    def test_requirements_drive_the_existing_preflight_without_new_grammar(self):
        value = self.factory()
        required = value.requirements_for(self.agent(), self.host)
        inventory = {'host': self.host, 'checked_ts': 1000, 'connected': True,
                     'tools': {'gh': True, 'gcloud': True},
                     'secrets': {'codex/api': True}}
        credentials = [
            {'host': self.host, 'harness': harness, 'account': account,
             'state': 'healthy', 'checked_ts': 1000, 'expires_ts': 2000}
            for harness, account in (('codex', ''), ('gh', ''), ('gcloud', ''))
        ]
        self.assertTrue(host_preflight.check(required, inventory, credentials,
                                             now=1000)['ready'])
        inventory['secrets']['codex/api'] = False
        self.assertEqual(host_preflight.check(required, inventory, credentials,
                                              now=1000)['missing'],
                         ['secret:codex/api'])

    def test_second_host_and_exact_account_are_resolved(self):
        value = self.factory()
        agent = self.agent(name='dsh', harness='deepseek', session_account='main')
        required = value.requirements_for(agent, 'target')
        self.assertEqual((required.host, required.harness, required.account),
                         ('target', 'deepseek', 'main'))
        self.assertEqual(value.references_for(agent, 'target'),
                         ('harnais.env#DEEPSEEK_API_KEY',))

    def test_missing_identity_is_refused(self):
        value = self.factory()
        cases = [
            (None, self.host, 'identity:agent'),
            ({}, self.host, 'identity:name'),
            (self.agent(name='bad name'), self.host, 'identity:name'),
            (self.agent(harness=None), self.host, 'identity:harness'),
            (self.agent(harness='claude'), self.host, 'identity:harness:claude'),
            (self.agent(session_account='other'), self.host, 'identity:account'),
            (self.agent(harness='deepseek', session_account=None), 'target',
             'identity:account'),
        ]
        for agent, host, message in cases:
            with self.subTest(agent=agent, host=host):
                with self.assertRaisesRegex(factory_module.FactoryError, message):
                    value.requirements_for(agent, host)
        for host in (None, '', 'unknown', 'bad host'):
            with self.subTest(host=host):
                with self.assertRaisesRegex(factory_module.FactoryError, 'identity:host'):
                    value.requirements_for(self.agent(), host)

    def test_missing_repository_is_refused(self):
        value = self.factory()
        with self.assertRaisesRegex(factory_module.FactoryError, 'repository:unknown'):
            value.adapter.repository(self.agent(team='unknown'))
        with self.assertRaisesRegex(factory_module.FactoryError, 'repository:project'):
            value.adapter.repository(self.agent(team=None))
        with self.assertRaisesRegex(factory_module.FactoryError, 'identity:name'):
            value.adapter.repository({})
        self.assertEqual(value.adapter.repository(self.agent()),
                         'git@forge:manaty/ameesh.git')

    def test_repository_credentials_query_fragment_or_control_are_refused(self):
        for repository in ('https://user:secret@forge.example/r.git',
                           'git@forge:manaty/r.git?ref=main',
                           'git@forge:manaty/r.git#fragment',
                           'git@forge:manaty/r\n.git'):
            with self.subTest(repository=repository):
                self.documents['repositories'] = {'repositories': {'ameesh': repository}}
                self.write('repositories', self.documents['repositories'])
                with self.assertRaisesRegex(factory_module.FactoryError,
                                            'document:repositories'):
                    self.factory()

    def test_missing_document_is_refused_by_name(self):
        for name in factory_module.DOCUMENTS:
            with self.subTest(name=name):
                path = self.root / (name + '.json')
                path.unlink()
                try:
                    with self.assertRaisesRegex(factory_module.FactoryError,
                                                'document:' + name):
                        self.factory()
                finally:
                    self.write(name, self.documents[name])

    def test_extra_or_unsafe_document_values_are_refused(self):
        cases = [
            ('enrollment', dict(self.documents['enrollment'], source='evil.py')),
            ('hosts', {'hosts': {self.host: {'codex': {
                'accounts': [''], 'tools': ['gh'], 'secrets': [], 'source': 'x'}}}}),
            ('secrets', {'references': {'codex/api': '../../etc/shadow#KEY'}}),
            ('secrets', {'references': {'codex/api': '..#KEY'}}),
            ('secrets', {'references': {'codex/api': '.#KEY'}}),
            ('secrets', {'references': {'../api': 'harnais.env#KEY'}}),
            ('secrets', {'references': {'codex/api': 'harnais.env#BAD-KEY'}}),
            ('hosts', {'hosts': {self.host: {'codex': {
                'accounts': [''], 'tools': ['gh'], 'secrets': ['unknown/api']}}}}),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.write(name, value)
                try:
                    with self.assertRaisesRegex(factory_module.FactoryError,
                                                'document:'):
                        self.factory()
                finally:
                    self.write(name, self.documents[name])

    def test_enrollment_version_and_controller_host_are_refused_when_invalid(self):
        for value in ({'version': 2, 'host': self.host},
                      {'version': True, 'host': self.host},
                      {'version': 1, 'host': 'bad host'},
                      {'version': 1, 'host': 'other-host'}):
            with self.subTest(value=value):
                self.write('enrollment', value)
                try:
                    with self.assertRaisesRegex(factory_module.FactoryError,
                                                'identity:host'):
                        self.factory()
                finally:
                    self.write('enrollment', self.documents['enrollment'])

    def test_documents_must_be_private_regular_and_unlinked(self):
        value = self.factory()
        self.assertIsNotNone(value)

        self.write('hosts', mode=0o644)
        with self.assertRaisesRegex(factory_module.FactoryError, 'document:hosts'):
            self.factory()
        self.write('hosts', self.documents['hosts'])

        path = self.root / 'hosts.json'
        real = self.root / 'hosts.real.json'
        path.rename(real)
        path.symlink_to(real)
        try:
            with self.assertRaisesRegex(factory_module.FactoryError, 'document:hosts'):
                self.factory()
        finally:
            path.unlink()
            real.rename(path)

        alias = self.root / 'hosts.alias.json'
        os.link(path, alias)
        try:
            with self.assertRaisesRegex(factory_module.FactoryError, 'document:hosts'):
                self.factory()
        finally:
            alias.unlink()

    def test_untrusted_enrollment_root_is_refused(self):
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(factory_module.FactoryError, 'document:root'):
                self.factory()
        finally:
            self.root.chmod(0o700)

    def test_missing_method_refuses_admission(self):
        implementation = SimpleNamespace(**{name: (lambda value: True)
                                            for name in factory_module.COMPANION_METHODS
                                            if name != 'verify'})
        with self.assertRaisesRegex(factory_module.FactoryError, 'method:verify'):
            self.factory(implementation=implementation)

    def test_pinned_companion_is_fixed_not_chosen_by_a_document(self):
        value = self.factory()
        expected = Path(factory_module.__file__).absolute().parent / 'hote-handoff.py'
        self.assertEqual(value.source_path, expected)
        self.assertEqual(factory_module.COMPANION_NAME, 'hote-handoff.py')

    def test_adapter_delegates_contract_checked_requests_to_the_provider(self):
        calls = []

        class Provider:
            def __getattr__(self, name):
                if name not in factory_module.COMPANION_METHODS:
                    raise AttributeError(name)

                def call(*args):
                    calls.append((name, args))
                    return {'ok': True}
                return call

        value = self.factory(implementation=Provider())
        request = operation()
        self.assertEqual(value.adapter.prepare(request), {'ok': True})
        self.assertEqual(calls, [('prepare', (request,))])
        with self.assertRaisesRegex(factory_module.FactoryError, 'contract:prepare'):
            value.adapter.prepare(operation(controller_epoch=0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(value.adapter.inventory(self.host), {'ok': True})
        self.assertEqual(calls[-1], ('inventory', (self.host,)))
        self.assertEqual(value.adapter.repository(self.agent()),
                         'git@forge:manaty/ameesh.git')

    def test_adapter_refuses_every_effect_it_has_not_implemented(self):
        value = self.factory()
        for method in handoff_contract.EFFECTS:
            with self.subTest(method=method):
                request = operation()
                if method == 'reconcile_effect':
                    request['evidence'] = {'pending_effect': {'epoch': 6}}
                with self.assertRaisesRegex(ValueError, 'capability:' + method):
                    getattr(value.adapter, method)(request)

    def test_adapter_rejects_an_operation_outside_the_contract(self):
        value = self.factory()
        with self.assertRaisesRegex(factory_module.FactoryError, 'contract:prepare'):
            value.adapter.prepare(operation(controller_epoch=0))
        with self.assertRaisesRegex(factory_module.FactoryError, 'identity:host'):
            value.adapter.inventory('unknown')


if __name__ == '__main__':
    unittest.main()
