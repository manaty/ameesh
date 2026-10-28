# SPDX-License-Identifier: AGPL-3.0-only
"""#4046: opposition lookup spans lots and preserves exact commit scope."""
from ameesh import storage, work
from .support import PgTestCase


class CanonVerdictsTest(PgTestCase):
    def test_all_lots_and_abbreviations_without_other_heads_or_non_verdicts(self):
        gate = 'abcdef0123456789' * 2 + 'abcdef01'
        lots = [work.add(self.db, title='canon source %s' % n, actor='pilot')['id']
                for n in range(2)]
        expected = []
        for lot, kind, sha, verdict in (
                (lots[0], 'verdict', gate, 'ok'),
                (lots[1], 'verdict', gate[:12].upper(), 'blocked'),
                (lots[1], 'verdict', 'f' * 40, 'blocked'),
                (lots[1], 'verdict', 'HEAD', 'blocked'),
                (lots[1], 'verdict', gate[:11], 'blocked'),
                (lots[1], 'frozen', gate, None)):
            row = self.db.query(
                "INSERT INTO work_item_milestones "
                "(work_item_id, kind, sha, actor, verdict) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (lot, kind, sha, 'pilot', verdict))[0]
            if kind == 'verdict' and sha.lower() in (gate, gate[:12]):
                expected.append(row['id'])
        marks = storage.of(self.db).work.verdicts_for_commit(gate)
        self.assertEqual({m['id'] for m in marks}, set(expected))
        self.assertEqual({m['work_item_id'] for m in marks}, set(lots))
