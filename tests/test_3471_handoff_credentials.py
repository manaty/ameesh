# SPDX-License-Identifier: AGPL-3.0-only
"""H4 integration: synthetic L232c store/helper, no DB, root or network."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from ameesh import handoff_credentials as bridge, host_preflight
from tests import test_l232c_coffre as fixture
from tests import test_3468_handoff_factory as h1


class DeliveryTest(fixture.Materiel):
    def setUp(self):
        super().setUp()
        self.agent = {'name': 'dev', 'harness': 'deepseek',
                      'session_account': fixture.UTILISATEUR}
        self.required = host_preflight.Requirements(
            fixture.HOST, 'deepseek', fixture.UTILISATEUR,
            tools=('gh',), secret_refs=('api-key',))
        self.op = h1.operation(target=fixture.HOST)
        self.op['plan'].update(harness='deepseek', account=fixture.UTILISATEUR)
        self.rows = [dict(host=fixture.HOST, harness=h, account=a,
                          checked_ts=time.time(), state='healthy', expires_ts=None)
                     for h, a in [('deepseek', fixture.UTILISATEUR), ('gh', '')]]
        self.inventory = dict(host=fixture.HOST, checked_ts=time.time(), connected=True,
                              tools={'gh': True}, secrets={'api-key': True})
        self.factory = SimpleNamespace(
            db=object(), requirements_for=lambda a, h: self.required,
            references_for=lambda a, h: (fixture.REFERENCE,),
            adapter=SimpleNamespace(inventory=lambda h: self.inventory))
        self.held = False
        self.frames = []
        self.boundaries = []
        self.guard_requests = []
        self.health_reads = 0
        api = SimpleNamespace(credentials=SimpleNamespace(listing=self.health))
        self.patch = patch.object(bridge.storage, 'of', return_value=api)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.service = bridge.CredentialBridge(
            self.factory, delivery_for=self.builder, guard=self.guard)

    def health(self, host):
        self.assertTrue(self.held)
        self.assertEqual(host, fixture.HOST)
        self.health_reads += 1
        return self.rows

    @contextmanager
    def guard(self, op, required, *, rotation):
        self.guard_requests.append((deepcopy(op), required, rotation))
        self.assertFalse(self.held)
        self.held = True
        try:
            yield True
        finally:
            self.held = False

    def builder(self, required, *, boundary):
        self.assertTrue(self.held)
        self.boundaries.append(boundary)
        self.assertTrue(boundary(required.host, required.account))
        self.assertFalse(boundary('another-host', required.account))
        self.assertFalse(boundary(required.host, 'another-account'))

        def exchange(payload, timeout):
            self.assertTrue(self.held)
            self.frames.append(payload)
            return self.installeur.handle(payload)

        return fixture.coffre.Livraison(
            self.policy, SimpleNamespace(exchange=exchange),
            fixture.coffre.Journal(self.root / 'journal'), boundary=boundary)

    def deliver(self, **kwargs):
        return self.service.deliver(self.op, self.agent, fixture.HOST, **kwargs)

    def test_delivery_real_helper_and_fresh_health_no_canary_leak(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = self.deliver()
        self.assertTrue(result['credentials_ready'])
        self.assertEqual(self.key.read_bytes(), fixture.CANARY)
        self.assertEqual(self.health_reads, 1)
        self.assertFalse(self.held)
        self.assertFalse(self.boundaries[0](fixture.HOST, fixture.UTILISATEUR))
        public = output.getvalue() + json.dumps(result)
        for path in (self.root / 'journal').rglob('*'):
            if path.is_file():
                public += path.read_text()
        self.assertNotIn(fixture.CANARY.decode(), public)

    def test_rotation_boundary_held_for_exact_account_then_expires(self):
        self.deliver()
        self.env.write_bytes(b'DEEPSEEK_API_KEY=synthetic-replacement\n')
        self.deliver(operation_kind='rotate')
        self.assertEqual(self.key.read_bytes(), b'synthetic-replacement')
        self.assertTrue(self.guard_requests[-1][2])
        self.assertEqual(self.guard_requests[-1][0]['controller_epoch'], 3)
        self.assertEqual(self.guard_requests[-1][0]['plan']['epoch'], 7)
        self.assertTrue(all(not b(fixture.HOST, fixture.UTILISATEUR)
                            for b in self.boundaries))

    def test_absent_guard_no_delivery(self):
        self.service.guard = None
        with self.assertRaisesRegex(bridge.CredentialRefused, 'guard_unavailable'):
            self.deliver()
        self.assertEqual(self.frames, [])
        self.assertFalse(self.key.exists())

    def test_refused_epoch_guard_no_delivery(self):
        @contextmanager
        def refused(*a, **kw):
            yield False
        self.service.guard = refused
        with self.assertRaisesRegex(bridge.CredentialRefused, 'secret_delivery_unavailable'):
            self.deliver()
        self.assertEqual(self.frames, [])

    def test_missing_and_changed_operation_identity_refuse_before_guard(self):
        original = deepcopy(self.op)
        for field in ('id', 'controller_epoch', 'plan', 'target', 'agent'):
            self.op = deepcopy(original)
            self.op.pop(field)
            with self.subTest(field=field), self.assertRaises(bridge.CredentialRefused):
                self.deliver()
        for field, value in [('account', 'different'), ('harness', 'codex')]:
            self.op = deepcopy(original)
            self.op['plan'][field] = value
            with self.subTest(field=field), self.assertRaises(bridge.CredentialRefused):
                self.deliver()
        self.assertEqual(self.guard_requests, [])
        self.assertEqual(self.frames, [])

    def test_unknown_policy_reference_and_wrong_destination_no_send(self):
        self.factory.references_for = lambda a, h: ('unknown.env#KEY',)
        with self.assertRaises(bridge.CredentialRefused):
            self.deliver()
        self.factory.references_for = lambda a, h: (fixture.REFERENCE,)
        self.policy = self.politique(self.destination_document(host='other'))
        with self.assertRaises(bridge.CredentialRefused):
            self.deliver()
        self.assertEqual(self.frames, [])

    def test_missing_secret_no_send_and_private_exception_suppressed(self):
        self.env.unlink()
        with self.assertRaises(bridge.CredentialRefused) as error:
            self.deliver()
        self.assertEqual(self.frames, [])
        self.assertNotIn(str(self.env), str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_aliases_of_one_reference_are_delivered_once(self):
        self.factory.references_for = lambda a, h: (fixture.REFERENCE, fixture.REFERENCE)
        self.assertTrue(self.deliver()['credentials_ready'])
        self.assertEqual(len(self.frames), 1)

    def test_failed_health_after_delivery_stays_uncertain(self):
        self.factory.adapter.inventory = lambda host: (_ for _ in ()).throw(ValueError('private'))
        with self.assertRaises(bridge.CredentialUncertain):
            self.deliver()
        self.assertTrue(self.key.exists())
        self.assertFalse(self.held)

    def test_install_does_not_bypass_rotation(self):
        self.deliver()
        self.env.write_bytes(b'DEEPSEEK_API_KEY=another-synthetic-value\n')
        with self.assertRaises(bridge.CredentialRefused):
            self.deliver()
        self.assertEqual(self.key.read_bytes(), fixture.CANARY)

    def test_revoked_missing_expired_stale_login_refuses_readiness(self):
        for change in ('missing', 'failing', 'expired', 'stale', 'future', 'wrong-account'):
            with self.subTest(change=change):
                self.rows[0].update(state='healthy', checked_ts=time.time(),
                                    account=fixture.UTILISATEUR, expires_ts=None)
                if change == 'missing':
                    self.rows[0]['state'] = 'missing'
                if change == 'failing':
                    self.rows[0]['state'] = 'failing'
                if change == 'expired':
                    self.rows[0]['expires_ts'] = time.time() - 1
                if change == 'stale':
                    self.rows[0]['checked_ts'] -= 301
                if change == 'future':
                    self.rows[0]['checked_ts'] += 300
                if change == 'wrong-account':
                    self.rows[0]['account'] = 'another'
                # No new delivery necessary for read-only admission checks.
                with self.guard(self.op, self.required, rotation=False):
                    result = self.service.check(self.agent, fixture.HOST)
                self.assertFalse(result['ready'])
                self.assertTrue(any(s.startswith('login:deepseek/') for s in result['missing']))
        self.assertEqual(self.frames, [])

    def test_file_delivery_cannot_manufacture_missing_tool_login(self):
        self.rows = self.rows[:1]
        result = self.deliver()
        self.assertFalse(result['credentials_ready'])
        self.assertIn('login:gh/default', result['missing'])
        self.assertTrue(self.key.exists())

    def test_missing_or_revoked_inventory_reference_refuses(self):
        for secrets in ({}, {'api-key': False}):
            self.inventory['secrets'] = secrets
            with self.guard(self.op, self.required, rotation=False):
                result = self.service.check(self.agent, fixture.HOST)
            self.assertFalse(result['ready'])
            self.assertIn('secret:api-key', result['missing'])

    def test_uncertain_transport_keeps_same_l232c_version(self):
        original = self.builder
        def builder(required, *, boundary):
            delivery = original(required, boundary=boundary)
            exchange = delivery.transport.exchange
            def lose_reply(payload, timeout):
                exchange(payload, timeout)
                raise RuntimeError(fixture.CANARY.decode())
            delivery.transport.exchange = lose_reply
            return delivery
        self.service.delivery_for = builder
        with self.assertRaises(bridge.CredentialUncertain) as error:
            self.deliver()
        self.assertNotIn(fixture.CANARY.decode(), str(error.exception))
        self.service.delivery_for = self.builder
        self.assertTrue(self.deliver()['credentials_ready'])
        first, _ = fixture.coffre.unframe(self.frames[0])
        second, _ = fixture.coffre.unframe(self.frames[1])
        self.assertEqual(first['version'], second['version'])


class FactoryCompositionTest(unittest.TestCase):
    setUp = h1.FactoryTest.setUp
    write = h1.FactoryTest.write
    factory = h1.FactoryTest.factory
    def test_credential_bridge_is_lazy_and_missing_guard_refuses(self):
        factory = self.factory()
        service = factory.credential_bridge()
        self.assertIsInstance(service, bridge.CredentialBridge)
        self.assertIsNotNone(service.delivery_for)
        self.assertIsNone(service.guard)


class EnrolledBuilderTest(unittest.TestCase):
    def setUp(self):
        self.module = h1.factory_module
        # Load via the fixed factory path, without reading any real document.
        fixture_case = h1.FactoryTest()
        fixture_case.setUp()
        self.addCleanup(fixture_case.doCleanups)
        self.builder = fixture_case.factory().credential_bridge().delivery_for
        import sys
        # The class globals are the fixed module, not selected by a message.
        self.ns = self.builder.__call__.__globals__
        self.coffre = self.ns['coffre']
        self.channel = self.ns['transport']
        self.required = host_preflight.Requirements('target', 'codex', 'mesh', tools=())

    def test_protected_documents_and_exact_transport_host(self):
        docs = {'policy.json': {'synthetic': 'policy'},
                'transport.json': {'hosts': {'target': {'host': 'other'}}}}
        def read(path, **kwargs):
            self.assertEqual(kwargs['owners'], (0,))
            self.assertEqual(kwargs['modes'], (0o600, 0o640))
            return json.dumps(docs[path.name]).encode()
        with patch.object(self.coffre, '_trusted_store') as trust, \
                patch.object(self.coffre, '_read_private', side_effect=read), \
                patch.object(self.coffre, 'Politique',
                             return_value=SimpleNamespace(host=self.builder.controller_host)), \
                patch.object(self.channel, 'SSHSecretTransport') as ssh:
            with self.assertRaisesRegex(ValueError, 'secret_transport_identity'):
                self.builder(self.required, boundary=lambda *a: True)
            trust.assert_called_once_with(self.builder.root, uid=0)
            ssh.assert_not_called()

    def test_unsafe_or_absent_policy_refuses_before_transport(self):
        with patch.object(self.coffre, '_trusted_store'), \
                patch.object(self.coffre, '_read_private',
                             side_effect=ValueError('synthetic-permission-refusal')), \
                patch.object(self.channel, 'SSHSecretTransport') as ssh:
            with self.assertRaises(ValueError):
                self.builder(self.required, boundary=lambda *a: True)
            ssh.assert_not_called()

    def test_composes_actual_livraison_with_enrolled_channel_and_boundary(self):
        # Policy and transport validation have their own negative contract
        # tests; this checks the H4 composition keeps the boundary binding.
        endpoint = {'host': 'target'}
        docs = {'policy.json': {}, 'transport.json': {'hosts': {'target': endpoint}}}
        boundary = lambda *a: False
        with patch.object(self.coffre, '_trusted_store'), \
                patch.object(self.coffre, '_read_private',
                             side_effect=lambda p, **kw: json.dumps(docs[p.name]).encode()), \
                patch.object(self.coffre, 'Politique') as policy, \
                patch.object(self.channel, 'SSHSecretTransport') as ssh:
            policy.return_value.host = self.builder.controller_host
            delivery = self.builder(self.required, boundary=boundary)
            self.assertIsInstance(delivery, self.coffre.Livraison)
            self.assertIs(delivery.boundary, boundary)
            self.assertIs(delivery.politique, policy.return_value)
            self.assertIs(delivery.transport, ssh.return_value)
            ssh.assert_called_once_with(endpoint)

    def test_wrong_controller_policy_refuses_before_transport(self):
        with patch.object(self.coffre, '_trusted_store'), \
                patch.object(self.coffre, '_read_private', return_value=b'{}'), \
                patch.object(self.coffre, 'Politique',
                             return_value=SimpleNamespace(host='another-controller')), \
                patch.object(self.channel, 'SSHSecretTransport') as ssh:
            with self.assertRaisesRegex(ValueError, 'secret_policy_identity'):
                self.builder(self.required, boundary=lambda *a: True)
            ssh.assert_not_called()
