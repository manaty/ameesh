# SPDX-License-Identifier: AGPL-3.0-only
"""#6641: conditional replacement and real concurrent review writers."""
import concurrent.futures
import json
import threading
from unittest import mock

from ameesh.storage.postgres.mailbox import Mailbox

from ameesh import mail, registry, storage, work, review_designation_pg as pg
from .support import PgTestCase, scaled

A, B, C = ('a' * 40, 'b' * 40, 'c' * 40)


class ReviewMailTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.item = work.add(self.db, title='coalesce reviewer requests', app='ameesh')
        self.lot = int(self.item['id'])
        registry.upsert(self.db, 'reader', harness='claude', host=self.cfg.host, status='idle')

    def send(self, sha, *, db=None, manual=False):
        payload = ({'review_request': {'freeze': {'sha': sha}, 'manual': True}}
                   if manual else {'review_designation': {'sha': sha}})
        return mail.send(db or self.db, 'ameesh', 'reader', 'Review request ' + sha,
            kind='request', work_item_id=str(self.lot), payload=payload, thread=False)

    def messages(self):
        return mail.review_requests(self.db, 'reader', self.lot)

    def test_two_refreezes_replace_only_unread_request_and_keep_queue_age(self):
        first = self.send(A)
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '10 minutes'"
                        ' WHERE id = %s', (first,))
        before = self.messages()[0]['created_ts']
        self.assertEqual(self.send(B), first)
        self.assertEqual(self.send(C), first)
        rows = self.messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payload']['review_designation']['sha'], C)
        self.assertEqual(rows[0]['body'], 'Review request ' + C)
        self.assertEqual(rows[0]['created_ts'], before)
        events = storage.of(self.db).work.events_noted('review-designation-replaced:', 0,
                                                     ids=[self.lot])
        changes = [json.loads(row['note'].split(': ', 1)[1]) for row in events]
        self.assertEqual({(row['old_sha'], row['new_sha']) for row in changes}, {(A, B), (B, C)})

    def legacy_send(self, sha, *, manual=False):
        # Seed the mailbox as it existed before structured writers coalesced.
        def insert(box, sender, recipient, body, args, *, automatic):
            return box._insert(sender, recipient, body, **args)
        with mock.patch.object(Mailbox, '_send_review', insert):
            return self.send(sha, manual=manual)

    def test_legacy_duplicates_are_retired_with_sha_trace(self):
        first = self.legacy_send(A)
        second = self.legacy_send(B)
        self.assertEqual(self.send(C), first)
        rows = self.messages()
        self.assertEqual([row['id'] for row in rows if row['delivered_ts'] is None], [first])
        self.assertEqual(storage.of(self.db).mailbox.get(second)['payload']
                         ['review_designation']['sha'], B)
        events = storage.of(self.db).work.events_noted('review-designation-replaced:', 0,
                                                     ids=[self.lot])
        changes = [json.loads(row['note'].split(': ', 1)[1]) for row in events]
        self.assertEqual({(row['old_sha'], row['new_sha']) for row in changes},
                         {(A, C), (B, C)})

    def test_pending_manual_request_is_replaced_at_refreeze(self):
        first = self.send(A, manual=True)
        self.assertEqual(self.send(B), first)
        rows = self.messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payload']['review_designation']['sha'], B)

    def test_reserved_manual_request_is_preserved(self):
        first = self.send(A, manual=True)
        storage.of(self.db).mailbox.reserve('reader', None, None, 'token',
                    ids=[first], porteur='test', ttl_seconds=60)
        self.assertNotEqual(self.send(B), first)
        self.assertEqual(storage.of(self.db).mailbox.get(first)['payload']
                         ['review_request']['freeze']['sha'], A)

    def test_legacy_retirement_rolls_back(self):
        first, second = self.legacy_send(A), self.legacy_send(B)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.db.transaction() as tx:
                self.send(C, db=tx)
                raise RuntimeError('rollback')
        self.assertEqual(sorted(row['id'] for row in self.messages()
                                if row['delivered_ts'] is None), [first, second])

    def test_read_designation_is_never_rewritten(self):
        first = self.send(A)
        mail.mark_delivered(self.db, [first])
        self.assertNotEqual(self.send(B), first)
        original = storage.of(self.db).mailbox.get(first)
        self.assertEqual(original['payload']['review_designation']['sha'], A)
        self.assertIsNotNone(original['delivered_ts'])

    def test_freeze_port_replaces_unread_designation_and_records_new_mark(self):
        for sha in (A, B):
            freeze = storage.of(self.db).work.add_milestone(self.lot, 'frozen', sha=sha,
                actor='author', verdict=None, note='')
            result = pg.PgPort(self.db).commit(self.lot, freeze=freeze,
                required=1, state='designated', reviewers=[{'name': 'reader', 'family': 'claude'}],
                missing=0, why='test', authors=[{'name': 'author', 'family': 'codex'}],
                policy_ref='canon@test', notify='', messages=[{
                    'to': 'reader', 'kind': 'request', 'body': 'Review request ' + sha}])
            self.assertEqual(result['freeze']['sha'], sha)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.messages()[0]['payload']['review_designation']['sha'], B)
        self.assertEqual(pg.PgPort(self.db).recorded(self.lot, int(freeze['id']))['freeze']['sha'], B)

    def test_reserved_designation_is_never_rewritten(self):
        first = self.send(A)
        rows = storage.of(self.db).mailbox.reserve('reader', None, None, 'token',
                    ids=[first], porteur='test', ttl_seconds=60)
        self.assertEqual([row['id'] for row in rows], [first])
        self.assertNotEqual(self.send(B), first)
        self.assertEqual(storage.of(self.db).mailbox.get(first)['body'], 'Review request ' + A)

    def test_pending_designation_refuses_manual_request_even_for_another_head(self):
        self.send(A)
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.send(B, manual=True)
        result = work._reviewer_request(self.db, self.item, 'reader', 'author')
        self.assertIn('pending', result['reviewer_request_warning'])
        self.assertNotIn('notify them', result['reviewer_request_warning'])
        self.assertEqual(len(self.messages()), 1)

    def test_verdict_refuses_manual_request_after_mailbox_was_purged(self):
        storage.of(self.db).work.add_milestone(self.lot, 'verdict', sha=B,
                                              actor='reader', verdict='ok', note='reviewed')
        with self.assertRaisesRegex(ValueError, 'verdict already'):
            self.send(B, manual=True)
        self.assertEqual(self.messages(), [])
        # A verdict on another head does not erase the new review obligation.
        self.send(C, manual=True)

    def test_manual_path_refuses_verdict_instead_of_reusing_delivered_request(self):
        storage.of(self.db).work.add_milestone(self.lot, 'frozen', sha=A,
                                              actor='author', verdict=None, note='')
        first = self.send(A, manual=True)
        mail.mark_delivered(self.db, [first])
        storage.of(self.db).work.add_milestone(self.lot, 'verdict', sha=A,
                                              actor='reader', verdict='ok', note='reviewed')
        result = work._reviewer_request(self.db, self.item, 'reader', 'author')
        self.assertIn('verdict already', result['reviewer_request_warning'])
        self.assertEqual(len(self.messages()), 1)

    def race(self, shas, *, manual=False):
        barrier = threading.Barrier(2, timeout=scaled(10))
        def writer(sha):
            db = self.connect()
            try:
                barrier.wait()
                try:
                    return self.send(sha, db=db, manual=manual)
                except ValueError as exc:
                    return str(exc)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(writer, shas))

    def test_two_real_automatic_writers_leave_one_live_request(self):
        results = self.race((A, B))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.messages()), 1)
        self.assertIn(self.messages()[0]['payload']['review_designation']['sha'], (A, B))

    def test_two_real_manual_writers_have_one_winner(self):
        results = self.race((A, A), manual=True)
        self.assertEqual(sum(isinstance(result, int) for result in results), 1, results)
        self.assertEqual(len(self.messages()), 1)

    def test_replacement_rolls_back_with_the_freeze_transaction(self):
        first = self.send(A)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.db.transaction() as tx:
                self.assertEqual(self.send(B, db=tx), first)
                raise RuntimeError('rollback')
        self.assertEqual(self.messages()[0]['payload']['review_designation']['sha'], A)
        self.assertEqual(storage.of(self.db).work.events_noted(
            'review-designation-replaced:', 0, ids=[self.lot]), [])
