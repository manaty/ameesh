# SPDX-License-Identifier: AGPL-3.0-only
"""#7442: previous seats and review context from the real milestone journal."""
from __future__ import annotations

from ameesh import mail, registry, storage, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as activation
from ameesh import review_designation_pg as pg
from .support import PgTestCase

A, B = 'a' * 40, 'b' * 40
FAMILIES = ('codex', 'claude', 'deepseek')


class StableJournalTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.item = work.add(self.db, title='Stable reviewer after correction', app='acme')
        self.lot = int(self.item['id'])
        for name in ('old', 'new', 'pilot'):
            registry.upsert(self.db, name, harness='claude', host=self.cfg.host, status='idle')

    def freeze(self, sha, note=''):
        return storage.of(self.db).work.add_milestone(
            self.lot, 'frozen', sha=sha, actor='author', verdict=None, note=note)

    def apply(self, sha, candidates, **kwargs):
        port = pg.PgPort(self.db)
        return rd.apply(self.item, policy=(), paths=['src/ameesh/thing.py'], required=1,
                        port=port, freeze_sha=sha, base='c' * 40,
                        authors=[{'name': 'author', 'family': 'codex'}],
                        candidates=candidates, families=FAMILIES, notify='pilot', **kwargs)

    def first_review(self):
        self.first = self.freeze(A)
        self.apply(A, [rd.Candidate('old', 'claude')])
        # The activation owns the lot column, independently from the PgPort mark.
        self.db.execute('UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s',
                        ('old', self.lot))
        rows = self.db.query('SELECT id FROM agent_mailbox WHERE work_item_id = %s',
                             (str(self.lot),))
        mail.mark_delivered(self.db, [r['id'] for r in rows])

    def test_code_correction_keeps_seat_and_sends_current_head_request(self):
        self.first_review()
        blocked = storage.of(self.db).work.add_milestone(
            self.lot, 'verdict', sha=A, actor='old', verdict='blocked', note='B: wrong boundary')
        current = self.freeze(B, 'Boundary fixed; 4 targeted tests green.')
        context = pg.PgPort(self.db).previous_review(self.lot, current['id'])
        self.assertEqual(context['freeze']['sha'], A)
        self.assertEqual(context['blocked'][0]['id'], blocked['id'])
        out = self.apply(B, [rd.Candidate('old', 'claude', load=20),
                             rd.Candidate('new', 'claude')])
        self.assertEqual(out['reviewers'], [{'name': 'old', 'family': 'claude'}])
        rows = self.db.query('SELECT recipient, body FROM agent_mailbox'
                             ' WHERE work_item_id = %s ORDER BY id', (str(self.lot),))
        self.assertEqual([r['recipient'] for r in rows], ['old', 'old'])
        self.assertIn('git diff %s..%s' % (A[:12], B[:12]), rows[-1]['body'])
        self.assertIn('wrong boundary', rows[-1]['body'])
        self.assertIn('Boundary fixed; 4 targeted tests green.', rows[-1]['body'])
        self.assertIn('--sha ' + B, rows[-1]['body'])
        self.apply(B, [rd.Candidate('new', 'claude')])
        self.assertEqual(len(self.db.query('SELECT id FROM agent_mailbox'
                                         ' WHERE work_item_id = %s', (str(self.lot),))), 2)

    def test_activation_selection_keeps_seat_through_both_port_wrappers(self):
        self.first_review()
        current = self.freeze(B, 'Correction ready for delta review.')
        inputs = activation.Inputs(
            item=self.item, freeze=current, repo='', base='c' * 40,
            paths=['src/ameesh/thing.py'], commits=[],
            policy=activation.Policy((), FAMILIES, 'sensitive', 1, '', '', ''),
            authors=[{'name': 'author', 'family': 'codex'}],
            unknown_authors=[], correctors=[], notify='pilot',
            candidates=[rd.Candidate('old', 'claude', load=20),
                        rd.Candidate('new', 'claude')])
        out = activation._selection(inputs, pg.PgPort(self.db), force=True)
        self.assertEqual(out['reviewers'], [{'name': 'old', 'family': 'claude'}])
        rows = self.db.query('SELECT recipient, body FROM agent_mailbox'
                             ' WHERE work_item_id = %s ORDER BY id', (str(self.lot),))
        # #7891: keep the pending seat through the activation wrappers,
        # update its current freeze/delta without another request.
        self.assertEqual([r['recipient'] for r in rows], ['old'])
        mark = pg.PgPort(self.db).recorded(self.lot, current['id'])
        self.assertEqual(mark['freeze']['sha'], B)
        self.assertEqual(mark['delta'], {'base': A, 'head': B})
        self.assertEqual(mark['continued'], ['old'])

    def messages(self):
        return self.db.query('SELECT id, recipient, body FROM agent_mailbox'
                             ' WHERE work_item_id = %s ORDER BY id', (str(self.lot),))

    def test_pending_seat_advances_across_refreezes_without_new_mail(self):
        self.first_review()  # first request already displayed, no verdict
        original = self.messages()
        for sha in (B, 'd' * 40):
            current = self.freeze(sha)
            out = self.apply(sha, [rd.Candidate('old', 'claude')])
            self.assertEqual(out['freeze']['sha'], sha)
            self.assertEqual(self.messages(), original)
            mark = pg.PgPort(self.db).recorded(self.lot, current['id'])
            self.assertEqual(mark['freeze']['sha'], sha)
            self.assertEqual(mark['reviewers'], [{'name': 'old', 'family': 'claude'}])
            self.assertEqual(mark['continued'], ['old'])
            self.assertEqual(mark['sent'], [])
            self.assertEqual(mark['delta']['head'], sha)

    def test_unread_request_does_not_trigger_another_mail(self):
        self.first_review()
        message = self.messages()[0]
        self.db.execute("UPDATE agent_mailbox SET delivered_at = NULL, status = 'pending'"
                        " WHERE id = %s", (message['id'],))
        self.freeze(B)
        self.apply(B, [rd.Candidate('old', 'claude')])
        self.assertEqual(self.messages(), [message])

    def test_reserved_request_does_not_trigger_another_mail(self):
        self.first_review()
        message = self.messages()[0]
        # Restore the original request to unread, then reserve it for display.
        self.db.execute("UPDATE agent_mailbox SET delivered_at = NULL, status = 'pending'"
                        " WHERE id = %s", (message['id'],))
        storage.of(self.db).mailbox.reserve('old', None, None, 'token',
            ids=[message['id']], porteur='test', ttl_seconds=60)
        self.freeze(B)
        self.apply(B, [rd.Candidate('old', 'claude')])
        self.assertEqual(self.messages(), [message])

    def test_carried_verdict_does_not_trigger_a_delta_request(self):
        self.first_review()
        storage.of(self.db).work.add_milestone(
            self.lot, 'verdict', sha=A, actor='old', verdict='ok', note='Reviewed A')
        current = self.freeze(B)
        # The carry path keeps the seat, then records its opinion on B.
        with self.db.transaction() as tx:
            out = activation._carried_in_transaction(
                tx, storage.of(tx).work.get(self.lot), current,
                {'carried': [{'actor': 'old', 'from': A, 'verdict': 'ok'}]})
        self.assertEqual(out['freeze']['sha'], B)
        self.assertEqual([r['name'] for r in out['reviewers']], ['old'])
        self.assertEqual(len(self.messages()), 1)

    def test_changed_seat_gets_one_new_request(self):
        self.first_review()
        self.freeze(B)
        candidates = [rd.Candidate('old', 'claude', available=False),
                      rd.Candidate('new', 'claude')]
        out = self.apply(B, candidates)
        self.assertEqual(out['reviewers'], [{'name': 'new', 'family': 'claude'}])
        self.assertEqual([r['recipient'] for r in self.messages()], ['old', 'new'])
        self.apply(B, candidates)
        self.assertEqual(len(self.messages()), 2)

    def test_refreeze_mark_rolls_back_with_its_transaction(self):
        self.first_review()
        self.freeze(B)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.db.transaction() as tx:
                rd.apply(self.item, policy=(), paths=['src/ameesh/thing.py'], required=1,
                    port=pg.PgPort(tx, after_commit=[]), freeze_sha=B,
                    authors=[{'name': 'author', 'family': 'codex'}],
                    candidates=[rd.Candidate('old', 'claude')], families=FAMILIES)
                raise RuntimeError('rollback')
        current = pg.PgPort(self.db).latest_freeze(self.lot)
        self.assertIsNone(pg.PgPort(self.db).recorded(self.lot, current['id']))
        self.assertEqual(len(self.messages()), 1)

    def test_removed_seat_is_not_resurrected(self):
        self.first_review()
        self.db.execute("UPDATE work_items SET reviewers = '{}' WHERE id = %s", (self.lot,))
        self.freeze(B)
        out = self.apply(B, [rd.Candidate('old', 'claude', load=20),
                             rd.Candidate('new', 'claude')])
        self.assertEqual(out['reviewers'], [{'name': 'new', 'family': 'claude'}])

    def test_last_effective_mark_wins_over_an_earlier_selection(self):
        self.first_review()
        from ameesh import verdict_carry
        note = verdict_carry.carried_mark_note(
            self.lot, self.first, {'carried': [{'actor': 'new', 'from': A, 'verdict': 'ok'}]})
        storage.of(self.db).work.note(self.lot, 'build', note, 'ameesh')
        self.db.execute('UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s',
                        ('new', self.lot))
        current = self.freeze(B)
        context = pg.PgPort(self.db).previous_review(self.lot, current['id'])
        self.assertEqual([r['name'] for r in context['reviewers']], ['new'])
