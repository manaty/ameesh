# SPDX-License-Identifier: AGPL-3.0-only
"""A signed production decision raises the gate without path heuristics."""
import unittest
from unittest import mock
from types import SimpleNamespace

from ameesh import decisions, merge_queue, prod_go, review_floor


class ProductionFloorTest(unittest.TestCase):
    def setUp(self):
        self.line = ('prod-op/1 project=acme lot=42 sha=' + 'a' * 40 +
                     ' until=2027-01-01T00:00:00Z')
        self.view = {'id': 7, 'lot': 42, 'project': 'acme', 'question': self.line,
                     'options': [], 'state': 'answered', 'needs_signature': True}
        self.store = SimpleNamespace(decisions=SimpleNamespace(for_work_item=lambda lot: [self.view]))

    def read(self, view=None, valid=True):
        if view is not None:
            self.store.decisions.for_work_item = lambda lot: [view]
        with mock.patch.object(review_floor.storage, 'of', return_value=self.store), \
             mock.patch.object(decisions, 'view_of', side_effect=lambda row: row), \
             mock.patch.object(decisions, 'verify_proof', return_value=(valid, 'invalid')):
            return review_floor.production_decision_floor(object(), 42)

    def test_mediated_floor_and_old_server_fallback(self):
        remote = SimpleNamespace(work=SimpleNamespace(review_floor=mock.Mock()))
        with mock.patch.object(review_floor.storage, "is_remote", return_value=True), \
             mock.patch.object(review_floor.storage, "of", return_value=remote):
            remote.work.review_floor.return_value = {"floor": 2, "codes": ["production"]}
            klass = review_floor.apply({}, object(), 42)
            self.assertEqual(klass["production_reviews_floor"], 2)
            self.assertTrue(review_floor.production_decision_floor(object(), 42))
            remote.work.review_floor.side_effect = RuntimeError("operation unavailable")
            with self.assertLogs("ameesh.review_floor", level="WARNING") as logs:
                klass = review_floor.apply({}, object(), 42)
            self.assertEqual(klass["reviews_floor"], 2)
            self.assertEqual(klass["production_reviews_floor"], 2)
            self.assertIn("operation unavailable", logs.output[0])

    def test_verified_and_pending_decisions(self):
        self.assertEqual(self.read(), 'decision #7')
        self.assertEqual(self.read({**self.view, 'state': 'pending'}), '')

    def test_invalid_signature_and_wrong_lot_fail_closed(self):
        with self.assertRaises(ValueError):
            self.read(valid=False)
        with self.assertRaises(ValueError):
            self.read({**self.view, 'question': self.line.replace('lot=42', 'lot=43')})

    def test_gate_requires_two_even_without_changed_paths(self):
        self.assertEqual(merge_queue.required_verdicts(
            {'class': 'light', 'files': [], 'production_reviews_floor': 2, 'reviews_floor': 2}), 2)
        self.assertEqual(merge_queue.required_verdicts({'class': 'light', 'files': []}), 0)

    def test_unreadable_decision_disables_waivers_and_fallbacks(self):
        with mock.patch.object(review_floor, 'of_lot', return_value={'floor': 0}), \
             mock.patch.object(review_floor, 'production_decision_floor', side_effect=ValueError('unreadable')):
            klass = review_floor.apply({'class': 'light', 'files': []}, object(), 42)
        self.assertEqual(klass['production_reviews_floor'], 2)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_go_decision_also_raises_floor(self):
        line = ('prod-go/1 project=acme sha=' + 'a' * 40 +
                ' version=release-1 deploy=a migrations=- merge=- until=2027-01-01T00:00:00Z')
        self.assertEqual(self.read({**self.view, 'question': line, 'options': [{'key': 'a'}]}), 'decision #7')

    def test_signature_rechecks_a_validated_lot(self):
        db = mock.Mock()
        db.query.return_value = [{'id': 42}]
        with mock.patch.object(decisions.work, 'get', return_value={'id': 42, 'state': 'validated'}), \
             mock.patch.object(decisions.work, 'milestones', return_value=[]), \
             mock.patch.object(merge_queue, 'review_gate_for_item', return_value={'ok': False}):
            decisions._recheck_production_review(db, None, 42)
            self.assertIn("SET state = 'qa'", db.query.call_args.args[0])
            self.assertIn('two families', db.execute.call_args.args[1][1])
        db.reset_mock()
        with mock.patch.object(decisions.work, 'get', return_value={'id': 42, 'state': 'validated'}), \
             mock.patch.object(decisions.work, 'milestones', return_value=[]), \
             mock.patch.object(merge_queue, 'review_gate_for_item', return_value={'ok': True}):
            decisions._recheck_production_review(db, None, 42)
            db.query.assert_not_called()


from datetime import datetime, timezone
import time
from . import test_l124_decisions as fixtures
from ameesh import mechanics


class SignedProductionFloorTest(fixtures._Base):
    owner_key = fixtures.SignatureTest.owner_key

    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE work_items SET app = 'acme' WHERE id = %s", (self.lot['id'],))
        self.configured = mock.patch.object(mechanics, 'configured',
                                            return_value=SimpleNamespace(prod_go_max_hours=24))
        self.configured.start()
        self.addCleanup(self.configured.stop)
        self.key = self.owner_key()

    def question(self):
        until = datetime.fromtimestamp(time.time() + 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return 'prod-op/1 project=acme lot=%s sha=%s until=%s' % (self.lot['id'], 'a' * 40, until)

    def test_signed_decision_raises_lot_floor_and_unsigned_does_not(self):
        view = self.ask(question=self.question(), options=['a=Proceed', 'b=Refuse'])
        self.assertTrue(view['needs_signature'])
        self.assertEqual(review_floor.production_decision_floor(self.db, self.lot['id']), '')
        self.assertEqual(review_floor.storage.of(self.db).work.review_floor(self.lot['id']),
                         {'floor': 0, 'codes': []})
        with self.assertRaises(decisions.DecisionError):
            self.answer(view['id'], 'a')
        self.answer(view['id'], 'a', key_path=self.key)
        self.assertEqual(review_floor.storage.of(self.db).work.review_floor(self.lot['id']),
                         {'floor': 2, 'codes': ['production']})
        klass = review_floor.apply({'class': 'normal', 'files': [{'path': 'script'}]}, self.db, self.lot['id'])
        self.assertEqual(klass['production_reviews_floor'], 2)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_signed_operation_requires_two_verdict_families(self):
        from ameesh import registry, storage, work
        lot = self.lot['id']
        head = 'a' * 40
        registry.upsert(self.db, 'dev1', harness='deepseek', host=self.cfg.host)
        ops = storage.of(self.db).work
        ops.add_milestone(lot, 'frozen', sha=head, actor='dev1', verdict=None, note='')
        view = self.ask(question=self.question(), options=['a=Proceed'])
        self.answer(view['id'], 'a', key_path=self.key)
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot,))
        for name, family in [('r_codex', 'codex'), ('r_claude', 'claude')]:
            registry.upsert(self.db, name, harness=family, host=self.cfg.host)

        def gate():
            klass = review_floor.apply({'class': 'sensitive', 'project': 'acme',
                                        'files': [{'path': 'operation'}]}, self.db, lot)
            return merge_queue.review_gate(self.db, work.milestones(self.db, lot), head,
                                           klass=klass, item=work.get(self.db, lot),
                                           families=('claude', 'codex', 'deepseek'))

        for index, (name, family) in enumerate([('r_codex', 'codex'), ('r_claude', 'claude')]):
            mark = ops.add_milestone(lot, 'verdict', sha=head, actor=name, verdict='ok', note='')
            self.db.execute("INSERT INTO provenance_receipts"
                            " (subject, sha, actor, session, turn, harness, work_item_id)"
                            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            ('milestone:%d' % mark['id'], head, name, 'session', 'turn', family, lot))
            result = gate()
            self.assertEqual(result['ok'], index == 1, result)
            self.assertEqual(result['required'], 2, result)
            with mock.patch.object(type(ops), '_validation_gate', side_effect=lambda *a, **k: gate()):
                if index == 0:
                    with self.assertRaises(work.WorkError):
                        ops.move(lot, 'validated', current='qa', loops=0, note='', actor='dev1')
                    self.assertEqual(self.lot_state(), 'qa')
                else:
                    ops.move(lot, 'validated', current='qa', loops=0, note='', actor='dev1')
                    self.assertEqual(self.lot_state(), 'validated')

    def test_malformed_declaration_is_rejected_on_ask(self):
        with self.assertRaises(decisions.DecisionError):
            self.ask(question=self.question().replace('sha=' + 'a' * 40, 'sha=broken'), options=['a=Proceed'])

    def test_signing_does_not_restore_validated_without_two_reviews(self):
        self.db.execute("UPDATE work_items SET state = 'validated' WHERE id = %s",
                        (self.lot['id'],))
        view = self.ask(question=self.question(), options=['a=Proceed'])
        self.assertEqual(self.lot_state(), 'waiting_human')
        with mock.patch.object(merge_queue, 'review_gate_for_item', return_value={'ok': False}):
            self.answer(view['id'], 'a', key_path=self.key)
        self.assertEqual(self.lot_state(), 'qa')

    def test_validation_refuses_when_decision_arrives_during_gate_read(self):
        from ameesh import storage, work
        ops = storage.of(self.db).work
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (self.lot['id'],))
        with mock.patch.object(review_floor, 'production_decision_floor',
                               side_effect=['', 'decision #7']), \
             mock.patch.object(type(ops), '_validation_gate', return_value={'ok': True}):
            with self.assertRaises(work.WorkError):
                ops.move(self.lot['id'], 'validated', current='qa', loops=0, note='', actor='dev1')
        self.assertEqual(self.lot_state(), 'qa')

    def test_storage_refuses_unsigned_production_closure(self):
        from ameesh import storage
        view = self.ask(question=self.question(), options=['a=Proceed'])
        with self.assertRaises(decisions.DecisionError):
            storage.of(self.db).decisions.close(
                view['id'], status='answered',
                record={'state': 'answered', 'answer': {'text': 'a'}})
        self.assertEqual(decisions.get(self.db, view['id'])['state'], 'pending')

from . import test_3183_review_floor as floor_fixtures
from . import test_2447_review_designation_activation as designation_fixtures
from . import test_6990_paused_family_fallback as fallback_fixtures


class ProductionGateTest(floor_fixtures.FloorGateTest):
    def test_signed_decision_requires_two_distinct_reviews(self):
        with mock.patch.object(review_floor, 'production_decision_floor', return_value='decision #7'):
            self.assertFalse(self.gate()['ok'])
            self.verdict('r_codex')
            gate = self.gate()
            self.assertFalse(gate['ok'], gate)
            self.assertEqual(gate['required'], 2)
            self.verdict('r_claude')
            gate = self.gate()
            self.assertTrue(gate['ok'], gate)


class ProductionDesignationTest(designation_fixtures._ActivationFixture,
                                designation_fixtures.PgTestCase):
    def test_empty_diff_still_designates_two_families(self):
        self.repo.git('reset', '--hard', self.base)
        self.repo.git('commit', '--allow-empty', '-m', 'production operation')
        self.frozen = self.repo.git('rev-parse', 'HEAD').strip()
        self.receipts(commits=[self.frozen])
        with mock.patch.object(review_floor, 'production_decision_floor', return_value='decision #7'), \
             mock.patch('ameesh.freeze_checks.changed_paths', return_value=['operation']):
            _, result = self.freeze()
        self.assertEqual(result.get('required'), 2, result)
        self.assertEqual(len({r['family'] for r in result['reviewers']}), 2, result)

    def test_production_floor_overrides_waiver(self):
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['-none'] WHERE id = %s", (self.lot,))
        with mock.patch.object(review_floor, 'production_decision_floor', return_value='decision #7'):
            _, result = self.freeze()
        self.assertEqual(result['required'], 2, result)
        self.assertEqual(len({r['family'] for r in result['reviewers']}), 2, result)

    def test_production_decision_completes_existing_waived_freeze(self):
        from ameesh import review_designation_activation as activation, work
        work.move(self.db, self.lot, 'qa')
        self.freeze()
        work.waive_review(self.cfg, self.db, self.lot, actor='human:smichea')
        with mock.patch.object(review_floor, 'production_decision_floor', return_value='decision #7'):
            result = activation.designate(self.cfg, self.db, self.lot)
        self.assertEqual(result.get('required'), 2, result)
        self.assertEqual(len({r['family'] for r in result['reviewers']}), 2, result)
        self.assertEqual(set(work.get(self.db, self.lot)['reviewers']),
                         {r['name'] for r in result['reviewers']})


class ProductionFallbackTest(fallback_fixtures.GateFallbackTest):
    def test_production_never_accepts_the_paused_family_fallback(self):
        klass = dict(self.KLASS, production_reviews_floor=2, reviews_floor=2)
        out, recorded = self.gate(klass, packet=True, record=True)
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['required'], 2)
        recorded.assert_not_called()

    def test_empty_vehicle_delta_keeps_the_production_floor(self):
        klass = dict(self.KLASS, production_reviews_floor=2, reviews_floor=2)
        with mock.patch.object(merge_queue, '_paused_family_fallback', return_value=None):
            out = merge_queue.review_gate(
                None, [], fallback_fixtures.HEAD, klass=klass, item=self.item(),
                vehicle={'ok': True, 'required': 0, 'klass': None, 'commits': []})
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['required'], 2)
