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

    def apply(self, sha, candidates):
        port = pg.PgPort(self.db)
        return rd.apply(self.item, policy=(), paths=['src/ameesh/thing.py'], required=1,
                        port=port, freeze_sha=sha, base='c' * 40,
                        authors=[{'name': 'author', 'family': 'codex'}],
                        candidates=candidates, families=FAMILIES, notify='pilot')

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
