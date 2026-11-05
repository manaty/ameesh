# SPDX-License-Identifier: AGPL-3.0-only
"""#5971 : les tics distincts lisent la mémoire du même lot, pas du porteur."""
import pathlib
import unittest
from unittest import mock

from ameesh import packet_unit_red as memory
from tests.test_3807_paquet_auto import paquet_auto

X, Y = 'a' * 40, 'b' * 40


def candidate(source=X, target='main'):
    return dict(work_item=1, source=source, target=target, ready=True, causes=[])


def event(source=X, stamp=100, tests=('tests.test_bug',)):
    return dict(work_item_id=1, created_ts=stamp, note=memory.note(source, 'main', tests))


class SelectionTest(unittest.TestCase):
    def test_same_head_is_excluded_on_successive_ticks_but_new_head_is_ready(self):
        for now in (101, 160, 100 + memory.EXCLUSION_S - 1):
            rows = [candidate()]
            memory.apply(rows, [event()], now=now)
            self.assertFalse(rows[0]['ready'])
            self.assertEqual(rows[0]['causes'][0]['code'], 'unit-tests-red')
            self.assertIn('tests.test_bug', rows[0]['causes'][0]['detail'])
        rows = [candidate(Y)]
        memory.apply(rows, [event()], now=160)
        self.assertTrue(rows[0]['ready'])

    def test_changed_target_head_readmits_the_same_source_early(self):
        mark = dict(event(), note=memory.note(X, 'main', ['tests.test_bug'], base=X))
        for base, ready in ((X, False), (Y, True)):
            rows = [candidate()]
            memory.apply(rows, [mark], now=160, base=base)
            self.assertEqual(rows[0]['ready'], ready)
        rows = [candidate()]
        memory.apply(rows, [event()], now=160, base=Y)
        self.assertFalse(rows[0]['ready'])  # legacy note: bounded fallback

    def test_new_red_on_new_base_is_excluded_without_rearming_that_base(self):
        b1 = dict(event(stamp=100), note=memory.note(X, 'main', ['tests.b1'], base=X))
        b2 = dict(event(stamp=200), note=memory.note(X, 'main', ['tests.b2'], base=Y))
        repeated = dict(b2, created_ts=300)
        for notes, base, now, ready in (
                ([b1], X, 160, False),
                ([b1], Y, 160, True),
                ([b1, b2], Y, 260, False),
                ([repeated, b1, b2], Y, 200 + memory.EXCLUSION_S, True)):
            rows = [candidate()]
            memory.apply(rows, notes, now=now, base=base)
            self.assertEqual(rows[0]['ready'], ready)
        rows = [candidate()]
        memory.apply(rows, [b1, b2], now=260, base=Y)
        self.assertIn('tests.b2', rows[0]['causes'][0]['detail'])

    def test_exclusion_names_its_expiry_for_the_ready_list_and_watch(self):
        from ameesh import packet_watch
        rows = [candidate()]
        memory.apply(rows, [event()], now=160)
        detail = rows[0]['causes'][0]['detail']
        self.assertIn('01:01 UTC', detail)
        watch = packet_watch.ProjectWatch('ameesh', ('composer',))
        text = packet_watch.body(watch, {'label': 'test', 'at': 160},
                                 {'candidates': rows, 'ready': []}, now=160)
        self.assertIn(detail, text)
        self.assertIn('0 lot(s) prêt(s)', text)

    def test_expiry_is_bounded_and_repeated_reds_cannot_extend_it(self):
        rows = [candidate()]
        memory.apply(rows, [event(stamp=200), event(stamp=100)],
                     now=100 + memory.EXCLUSION_S)
        self.assertTrue(rows[0]['ready'])

    def test_other_target_and_other_lot_are_not_excluded(self):
        rows = [candidate(target='develop'), dict(candidate(), work_item=2)]
        memory.apply(rows, [event()], now=160)
        self.assertTrue(all(row['ready'] for row in rows))

    def test_invalid_or_unrelated_notes_do_not_create_a_hold(self):
        for note in ('not a red', memory.PREFIX + '[]', memory.PREFIX + 'broken',
                     memory.PREFIX + '{"source":"main"}'):
            rows = [candidate()]
            memory.apply(rows, [dict(event(), note=note)], now=160)
            self.assertTrue(rows[0]['ready'])


class PersistenceTest(unittest.TestCase):
    def test_failed_persistence_stops_before_sending_a_removal_notice(self):
        removal = paquet_auto.Removal(1, tests=['tests.test_bug'])
        with mock.patch.object(paquet_auto, 'remember_unit_red',
                               side_effect=paquet_auto.PaquetAutoError('offline')), \
                mock.patch.object(paquet_auto, 'send_mail') as mail:
            with self.assertRaisesRegex(paquet_auto.PaquetAutoError, 'non mémorisé'):
                paquet_auto.notify_removals(pathlib.Path('.'), 'h-28', [removal],
                    sender='pilot', sources={1:X}, target='main')
        mail.assert_not_called()

    def test_notification_is_sent_only_for_a_new_record(self):
        removal = paquet_auto.Removal(1, tests=['tests.test_bug'], attributed=True)
        with mock.patch.object(paquet_auto, 'remember_unit_red', side_effect=[True, False]) as remember, \
                mock.patch.object(paquet_auto, 'lot_author', return_value='author'), \
                mock.patch.object(paquet_auto, 'send_mail') as mail:
            for packet in ('h-28', 'h-29'):
                self.assertEqual(paquet_auto.notify_removals(pathlib.Path('.'), packet, [removal],
                    sender='pilot', sources={1:X}, target='main', base=X, log=lambda _: None), [])
        self.assertEqual(remember.call_count, 2)
        self.assertEqual(remember.call_args.kwargs["base"], X)
        mail.assert_called_once()
        self.assertIn('tests.test_bug', mail.call_args.args[2])


from .support import PgTestCase


class DurableMemoryTest(PgTestCase):
    def test_new_base_records_a_new_red_but_does_not_notify_again(self):
        from ameesh import storage
        lot = self.db.query("INSERT INTO work_items (title, state) VALUES (%s, 'qa') RETURNING id",
                            ('new base red',))[0]['id']
        def records():
            return storage.of(self.db).progress.lot_hold_events([lot], [memory.PATTERN])
        def ready(base, now):
            row = dict(candidate(), work_item=lot)
            memory.apply([row], records(), base=base, now=now)
            return row['ready']
        self.assertTrue(memory.remember(self.db, lot, X, 'main', ['tests.b1'], base=X))
        now = records()[0]['created_ts'] + 1
        self.assertFalse(ready(X, now))
        self.assertTrue(ready(Y, now))
        self.assertFalse(memory.remember(self.db, lot, X, 'main', ['tests.b2'], base=Y))
        self.assertFalse(ready(Y, now))
        self.assertFalse(memory.remember(self.db, lot, X, 'main', ['tests.repeated'], base=Y))
        notes = [memory.read_note(row['note']) for row in records()]
        self.assertEqual(len(notes), 2)
        self.assertEqual({row['base'] for row in notes}, {X, Y})
        expiry = max(row['created_ts'] for row in records()) + memory.EXCLUSION_S
        self.assertTrue(ready(Y, expiry))

    def test_record_survives_another_reader_and_more_than_a_hundred_events(self):
        from ameesh import storage
        lot = self.db.query("INSERT INTO work_items (title, state) VALUES (%s, 'qa') RETURNING id",
                            ('unit red memory',))[0]['id']
        self.assertTrue(memory.remember(self.db, lot, X, 'main', ['tests.test_bug'], actor='composer'))
        # Le journal affiché par work show est tronqué à cent événements.
        self.db.execute("INSERT INTO work_item_events (work_item_id, state, note, actor) "
                        "SELECT %s, 'qa', 'later note', 'composer' FROM generate_series(1,101)", (lot,))
        self.assertFalse(memory.remember(self.db, lot, X, 'main', ['tests.test_other'], actor='composer'))
        rows = storage.of(self.db).progress.lot_hold_events([lot], [memory.PATTERN])
        marks = [row for row in rows if memory.read_note(row.get('note'))]
        self.assertEqual(len(marks), 1)
        self.assertEqual(memory.read_note(marks[0]['note'])['tests'], ['tests.test_bug'])
        entry = dict(candidate(), work_item=lot)
        memory.apply([entry], rows, now=marks[0]['created_ts'] + 60)
        self.assertFalse(entry['ready'])
        self.assertTrue(memory.remember(self.db, lot, Y, 'main', ['tests.test_other'], actor='composer'))
