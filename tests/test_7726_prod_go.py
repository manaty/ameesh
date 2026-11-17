# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded owner proofs and immutable authority during the signed window."""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest import mock
import unittest

from ameesh import decisions, mechanics, prod_go
from .test_l1616_mechanics import MECHANICS
from . import test_l124_decisions as fixtures

SHA = 'a' * 40
OPTIONS = [{'key': 'a', 'label': 'Full production'}, {'key': 'b', 'label': 'Refuse'}, {'key': 'c', 'label': 'Migrations'}]


def question(now, *, hours=1):
    until = datetime.fromtimestamp(now + hours * 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ('Production acme\nprod-go/1 project=acme sha=%s version=release-11x '
            'deploy=a migrations=a,c merge=a until=%s' % (SHA, until))


class ContractTest(unittest.TestCase):
    def test_parse_is_exact_and_each_operation_has_explicit_options(self):
        rule = prod_go.parse(question(1_800_000_000), OPTIONS)
        self.assertEqual(rule['allowed'], {'deploy': ('a',), 'migrate': ('a', 'c'), 'release': ('a',)})
        self.assertIsNone(prod_go.parse('Historical yes', OPTIONS))
        for value in (question(1_800_000_000) * 2,
                      question(1_800_000_000).replace('deploy=a', 'deploy=z'),
                      question(1_800_000_000).replace('deploy=a', 'deploy=a,a'),
                      question(1_800_000_000).replace('sha=' + SHA, 'sha=abc'),
                      question(1_800_000_000).replace('prod-go/1', 'prod-go/2')):
            with self.subTest(value=value), self.assertRaises(prod_go.ProdGoError):
                prod_go.parse(value, OPTIONS)

    def test_window_and_project_are_validated_against_compiled_canon(self):
        now = 1_800_000_000
        with mock.patch.object(mechanics, 'configured', return_value=SimpleNamespace(prod_go_max_hours=24)):
            self.assertIsNotNone(prod_go.validate_request(None, question(now), OPTIONS, 'acme', now=now))
            for q, project in ((question(now, hours=-1), 'acme'), (question(now, hours=25), 'acme'), (question(now), 'other')):
                with self.subTest(q=q, project=project), self.assertRaises(prod_go.ProdGoError):
                    prod_go.validate_request(None, q, OPTIONS, project, now=now)
        with mock.patch.object(mechanics, 'configured', return_value=SimpleNamespace(prod_go_max_hours=None)):
            with self.assertRaisesRegex(prod_go.ProdGoError, 'not compiled'):
                prod_go.validate_request(None, question(now), OPTIONS, 'acme', now=now)

    def test_answered_window_cannot_be_revoked_but_pending_can(self):
        now = 1_800_000_000
        view = {'id': 42, 'question': question(now), 'options': OPTIONS, 'state': 'answered'}
        with self.assertRaisesRegex(prod_go.ProdGoError, 'cancel the production run'):
            prod_go.require_unlocked(view, now=now)
        prod_go.require_unlocked(view, now=now + 7200)
        prod_go.require_unlocked({**view, 'state': 'pending'}, now=now)

    def test_compiler_exports_optional_positive_bound_without_a_default(self):
        self.assertIsNone(mechanics.compile_document(MECHANICS).prod_go_max_hours)
        d = mechanics.compile_document(MECHANICS + '\nprod_go_max_hours: 17\n')
        self.assertEqual(d.to_dict()['prod_go_max_hours'], 17)
        for value in ('0', '-1', 'true', '1.5'):
            with self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(MECHANICS + '\nprod_go_max_hours: %s\n' % value)


class SignedExportTest(fixtures._Base):
    owner_key = fixtures.SignatureTest.owner_key

    def setUp(self):
        super().setUp()
        import time
        self.now = time.time()
        self.db.execute("UPDATE work_items SET app = 'acme' WHERE id = %s", (self.lot['id'],))
        patch = mock.patch.object(mechanics, 'configured', return_value=SimpleNamespace(prod_go_max_hours=24))
        patch.start()
        self.addCleanup(patch.stop)
        self.key = self.owner_key()

    def production(self):
        return self.ask(question=question(self.now), options=['%s=%s' % (o['key'], o['label']) for o in OPTIONS])

    def test_ask_forces_owner_signature_and_export_binds_exact_document(self):
        view = self.production()
        self.assertTrue(view['needs_signature'])
        with self.assertRaisesRegex(decisions.DecisionError, 'signed answer'):
            self.answer(view['id'], 'a')
        self.answer(view['id'], 'a', key_path=self.key)
        proof = prod_go.export_proof(self.cfg, self.db, view['id'])
        self.assertEqual(proof['schema'], 'ameesh-prod-go-proof/1')
        signed = json.loads(proof['signed_payload'].split('|', 2)[2])
        data = json.dumps(proof['answer_document'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.assertEqual(signed['artifact_hash'], hashlib.sha256(data).hexdigest())
        from ameesh import signing, authority
        self.assertTrue(signing.verify(authority.unb64(proof['public_key']), proof['signed_payload'].encode(), authority.unb64(proof['signature'])))
        prod_go.authorize(self.cfg, self.db, view['id'], project='acme', sha=SHA, operation='release')
        for kw in ({'project': 'other', 'sha': SHA, 'operation': 'release'},
                   {'project': 'acme', 'sha': 'b' * 40, 'operation': 'deploy'}):
            with self.assertRaises(prod_go.ProdGoError):
                prod_go.authorize(self.cfg, self.db, view['id'], **kw)
        with self.assertRaises(prod_go.ProdGoError):
            prod_go.export_proof(self.cfg, self.db, view['id'], now=self.now + 7200)

    def test_production_signature_covers_until_and_short_presign_is_refused(self):
        from ameesh import authority
        view = self.ask(question=question(self.now, hours=2), options=['%s=%s' % (o['key'], o['label']) for o in OPTIONS])
        self.answer(view['id'], 'a', key_path=self.key)
        proof = prod_go.export_proof(self.cfg, self.db, view['id'])
        signed = json.loads(proof['signed_payload'].split('|', 2)[2])
        self.assertGreaterEqual(signed['expires'] / 1e6, prod_go.parse(view['question'], view['options'])['until'])
        other = self.production()
        create = authority.create_approval
        def short(*args, **kwargs):
            return create(*args, **{**kwargs, 'ttl': 60})
        with mock.patch.object(authority, 'create_approval', side_effect=short):
            with self.assertRaisesRegex(decisions.DecisionError, 'exceeds signed approval expiry'):
                self.answer(other['id'], 'a', key_path=self.key)

    def test_signed_migration_only_answer_cannot_deploy_or_merge(self):
        view = self.production()
        self.answer(view['id'], 'c', key_path=self.key)
        prod_go.authorize(self.cfg, self.db, view['id'], project='acme', sha=SHA, operation='migrate')
        for operation in ('deploy', 'release'):
            with self.assertRaisesRegex(prod_go.ProdGoError, 'does not authorize'):
                prod_go.authorize(self.cfg, self.db, view['id'], project='acme', sha=SHA, operation=operation)

    def test_supersession_under_storage_lock_cannot_revoke_an_exported_window(self):
        view = self.production()
        self.answer(view['id'], 'a', key_path=self.key)
        replacement = self.ask(question='Replace current production authority?', supersedes=view['id'], supersedes_option='a')
        with self.assertRaisesRegex(decisions.DecisionError, 'cannot be withdrawn or replaced'):
            self.answer(replacement['id'], 'a', key_path=self.key)
        self.assertEqual(decisions.get(self.db, replacement['id'])['state'], 'pending')
        self.assertIsNone(decisions.get(self.db, view['id'])['superseded_by'])
        prod_go.export_proof(self.cfg, self.db, view['id'])

    def test_pending_and_unsigned_historical_decisions_cannot_be_exported(self):
        view = self.production()
        with self.assertRaises(prod_go.ProdGoError):
            prod_go.export_proof(self.cfg, self.db, view['id'])
        ordinary = self.ask(question='Historical release approval?')
        self.answer(ordinary['id'], 'a')
        with self.assertRaisesRegex(prod_go.ProdGoError, 'not VALID'):
            prod_go.export_proof(self.cfg, self.db, ordinary['id'])


class ReleaseMergeTest(unittest.TestCase):
    def test_packet_exception_cannot_bypass_production_authorization(self):
        from ameesh import packet_guard
        with mock.patch.object(prod_go, 'check_release_merge', side_effect=prod_go.ProdGoError('missing proof')):
            with self.assertRaisesRegex(packet_guard.PacketGuardError, 'missing proof'):
                packet_guard.check_merge(None, 7, 'release/11x', 'main', SHA,
                                         exception='incident', reason='target red')

    def test_exact_pr_context_is_required(self):
        from ameesh import work
        item = {'app': 'acme', 'pr_ref': 'acme/app#42'}
        document = SimpleNamespace(override=lambda project: SimpleNamespace(production_branch='main'))
        pr = {'body': 'Production decision: #123', 'headRefOid': SHA,
              'headRefName': 'release/11x', 'baseRefName': 'main'}
        cases = ({}, {'body': ''}, {'body': 'Production decision: #123\nProduction decision: #456'},
                 {'headRefOid': 'b' * 40}, {'headRefName': 'release/other'}, {'baseRefName': 'develop'})
        for changes in cases:
            gh = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({**pr, **changes})))
            with self.subTest(changes=changes), mock.patch.object(work, 'get', return_value=item), mock.patch.object(mechanics, 'configured', return_value=document), mock.patch.object(prod_go, 'authorize') as authorize:
                if changes:
                    with self.assertRaises(prod_go.ProdGoError):
                        prod_go.check_release_merge(None, object(), 7, 'release/11x', 'main', SHA, gh=gh)
                    authorize.assert_not_called()
                else:
                    prod_go.check_release_merge(None, object(), 7, 'release/11x', 'main', SHA, gh=gh)
                    self.assertEqual(authorize.call_args.args[2], '123')
                    self.assertEqual(authorize.call_args.kwargs, dict(project='acme', sha=SHA, operation='release'))
