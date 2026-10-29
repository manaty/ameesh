# SPDX-License-Identifier: AGPL-3.0-only
"""#4299 (L243b step 3): an identity renamed in place, old name kept as alias."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest import mock

from ameesh import agent_names, agent_rename, authority, mail, registry, work
from .support import PgTestCase

# Column names that hold an agent spelling somewhere in the schema; each such
# column must be classified, so a new table is never silently left behind.
IDENTITY_COLUMNS = re.compile(
    r"^(agent|persona|assignee|reviewers?|recipient|sender|delegate|owner|approver|actor|"
    r"holder|external_executor|name|primary_name|supplied_name|old_name|new_name|"
    r"[a-z_]+_by)$")
NOT_AGENTS = {
    # Identity-like names that never hold an agent spelling.
    "agent_registry.name",
}


class AgentRenameTest(PgTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(work, "corrector", return_value="human:owner")
        patcher.start()
        self.addCleanup(patcher.stop)
        registry.upsert(self.db, 'worker', host=self.cfg.host, harness='codex')
        registry.upsert(self.db, 'reviewer', host=self.cfg.host, harness='claude')

    def tearDown(self):
        # Each test starts from spellings nobody holds, in every column.
        names = ['worker', 'new-worker', 'reviewer', 'legacy']
        for column in agent_rename.REWRITTEN + agent_rename.CASCADED:
            table, col, is_array = agent_rename._split(column)
            if table != 'agent_registry':
                self.db.execute("DELETE FROM %s WHERE %s %s" % (
                    table, col, "&& %s::text[]" if is_array else "= ANY(%s::text[])"),
                    ('{%s}' % ','.join(names),))
        self.db.execute("DELETE FROM agent_registry WHERE name = ANY(%s::text[])",
                        ('{%s}' % ','.join(names),))
        super().tearDown()

    def rename(self, old, new, **kw):
        return agent_rename.rename(self.cfg, self.db, old, new, reason='test #4299', **kw)

    def test_every_identity_column_is_classified(self):
        rows = self.db.query(
            "SELECT c.table_name || '.' || c.column_name AS col, c.data_type"
            "  FROM information_schema.columns c JOIN information_schema.tables t"
            "    USING (table_schema, table_name)"
            " WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'"
            "   AND c.data_type IN ('text', 'ARRAY', 'character varying')")
        columns = {r['col']: r['data_type'] for r in rows}
        known = {c.rstrip('[]') for c in agent_rename.REWRITTEN + agent_rename.CASCADED
                 + agent_rename.KEPT}
        unclassified = sorted(c for c in columns
                              if IDENTITY_COLUMNS.match(c.split('.', 1)[1])
                              and c not in known and c not in NOT_AGENTS)
        # `name` / `owner` are generic: only agent-valued ones are listed; the
        # others go to IGNORED below with a reason.
        unclassified = [c for c in unclassified if c not in IGNORED]
        self.assertEqual(unclassified, [], "classify these columns in agent_rename")
        self.assertEqual(sorted(c for c in known if c not in columns), [],
                         "listed columns missing from the schema")
        for column in agent_rename.REWRITTEN:
            self.assertEqual(columns[column.rstrip('[]')],
                             'ARRAY' if column.endswith('[]') else 'text', column)
        # A child of a rewritten column follows by cascade, never by its own UPDATE.
        parents = {c.rstrip('[]') for c in agent_rename.REWRITTEN} | {'agent_registry.name'}
        children = self.db.query(
            "SELECT c.conrelid::regclass::text AS child, a.attname, c.confupdtype,"
            "       c.confrelid::regclass::text || '.' || p.attname AS parent"
            "  FROM pg_constraint c"
            "  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]"
            "  JOIN pg_attribute p ON p.attrelid = c.confrelid AND p.attnum = c.confkey[1]"
            " WHERE c.contype = 'f' AND c.connamespace = current_schema()::regnamespace")
        children = [r for r in children if r['parent'] in parents]
        self.assertEqual(sorted('%s.%s' % (r['child'], r['attname']) for r in children),
                         sorted(agent_rename.CASCADED))
        self.assertEqual({r['confupdtype'] for r in children}, {'c'})

    def test_rename_moves_the_same_identity_and_keeps_old_as_alias(self):
        claimed = registry.claim(self.db, 'worker', 'runner-test', 120)
        registry.release(self.db, 'worker', 'runner-test', claimed['lease_epoch'])
        registry.set_session(self.db, 'worker', 'resident-session')
        item = work.add(self.db, title='Renamed assigned work', assignee='worker',
                        estimate='1h', cfg=self.cfg)
        reviewed = work.add(self.db, title='Renamed reviewer work', assignee='reviewer',
                            estimate='1h', cfg=self.cfg)
        self.db.execute("UPDATE work_items SET reviewer = 'worker' WHERE id = %s",
                        (reviewed['id'],))
        unread = mail.send(self.db, 'reviewer', 'worker', 'Waiting for the worker')
        sent = mail.send(self.db, 'worker', 'reviewer', 'Sent by the worker')
        before = registry.get(self.db, 'worker')
        count = self.db.query("SELECT count(*) AS n FROM agent_registry")[0]['n']

        result = self.rename('worker', 'new-worker')

        self.assertEqual(result['aliases'], ['worker'])
        self.assertEqual(result['rewritten']['work_items.assignee'], 1)
        self.assertEqual(result['rewritten']['work_items.reviewers[]'], 1)
        after = registry.get(self.db, 'worker')
        self.assertEqual(after['name'], 'new-worker')
        self.assertEqual(after['aliases'], ['worker'])
        for field in ('session_id', 'lease_epoch', 'harness', 'host'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_registry")[0]['n'], count)
        self.assertEqual(agent_names.resolve('worker', db=self.db), 'new-worker')
        self.assertEqual(work.open_lots(self.db, 'new-worker')[0]['id'], item['id'])
        self.assertEqual(work.get(self.db, reviewed['id'])['reviewers'], ['new-worker'])
        self.assertEqual([m['id'] for m in mail.unread(self.db, 'new-worker')], [unread])
        self.assertEqual(mail.get(self.db, sent)['sender'], 'new-worker')
        # Mail to the old spelling after the rename reaches the new primary.
        later = mail.send(self.db, 'reviewer', 'worker', 'Sent to the old name')
        self.assertEqual(mail.get(self.db, later)['recipient'], 'new-worker')
        journal = self.db.query("SELECT * FROM agent_renames ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((journal['old_name'], journal['new_name'], journal['actor']),
                         ('worker', 'new-worker', 'human:owner'))
        # A later claim is fenced on the same epoch sequence, not a fresh one.
        lease = registry.claim(self.db, 'worker', 'runner-test', 120)
        self.assertEqual(lease['name'], 'new-worker')
        self.assertGreater(lease['lease_epoch'], claimed['lease_epoch'])
        self.assertFalse(registry.lease_matches(
            self.db, 'new-worker', 'runner-test', claimed['lease_epoch'])[0])
        registry.release(self.db, 'new-worker', 'runner-test', lease['lease_epoch'])

    def test_reverse_is_the_same_gesture(self):
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['legacy'] WHERE name = 'worker'")
        self.rename('worker', 'new-worker')
        self.assertEqual(registry.get(self.db, 'worker')['aliases'], ['legacy', 'worker'])
        self.rename('new-worker', 'worker')
        row = registry.get(self.db, 'new-worker')
        self.assertEqual(row['name'], 'worker')
        self.assertEqual(row['aliases'], ['legacy', 'new-worker'])

    def test_dry_run_rolls_back_everything(self):
        work.add(self.db, title='Dry run assigned work', assignee='worker',
                 estimate='1h', cfg=self.cfg)
        journal = self.db.query("SELECT count(*) AS n FROM agent_renames")[0]['n']
        result = self.rename('worker', 'new-worker', dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['rewritten']['work_items.assignee'], 1)
        self.assertIsNone(result['event'])
        self.assertEqual(registry.get(self.db, 'worker')['name'], 'worker')
        self.assertIsNone(registry.get(self.db, 'new-worker'))
        self.assertEqual(work.open_lots(self.db, 'worker')[0]['assignee'], 'worker')
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_renames")[0]['n'], journal)

    def test_refusals_change_nothing(self):
        lease = registry.claim(self.db, 'worker', 'runner-test', 120)
        with self.assertRaisesRegex(agent_rename.RenameError, 'live lease'):
            self.rename('worker', 'new-worker')
        registry.release(self.db, 'worker', 'runner-test', lease['lease_epoch'])
        cases = (('reviewer', 'already designates'), ('all', 'invalid'),
                 ('bad/name', 'invalid'), ('worker', 'already the primary'))
        for new, message in cases:
            with self.subTest(new=new):
                with self.assertRaisesRegex(agent_rename.RenameError, message):
                    self.rename('worker', new)
        with self.assertRaisesRegex(agent_rename.RenameError, 'unknown agent'):
            self.rename('missing', 'new-worker')
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['legacy'] WHERE name = 'worker'")
        with self.assertRaisesRegex(agent_rename.RenameError, 'alias of worker'):
            self.rename('legacy', 'new-worker')
        # A spelling of nobody already on rows would graft a foreign history.
        mail.send(self.db, 'reviewer', 'new-worker', 'To a name nobody holds yet')
        with self.assertRaisesRegex(agent_rename.RenameError, 'agent_mailbox.recipient'):
            self.rename('worker', 'new-worker')
        self.assertEqual(registry.get(self.db, 'worker')['name'], 'worker')
        authority.register_human_key(self.db, 'alice', b'x' * 32)
        with self.assertRaises(agent_rename.RenameError):
            self.rename('worker', 'alice')
        authority.revoke_key(self.db, 'alice')

    def test_keys_and_signed_mail_are_kept(self):
        from ameesh import signing
        seed = b"s" * 32
        authority.register_key(self.db, 'worker', signing.backend().public_from_seed(seed))
        fingerprint = authority.key_info(self.db, 'worker')['public_key_fingerprint']
        signed = authority.sign_message(seed, sender='worker', recipient='reviewer',
                                        body='A signed report before renaming')
        identifier = mail.send(self.db, 'worker', 'reviewer', 'A signed report before renaming',
                               **signed)
        payload = self.db.query("SELECT signed_payload, signature FROM agent_mailbox"
                                " WHERE id = %s", (identifier,))[0]
        key_events = self.db.query("SELECT count(*) AS n FROM key_events WHERE name = 'worker'")[0]['n']
        self.rename('worker', 'new-worker')
        self.assertEqual(authority.key_info(self.db, 'new-worker')['public_key_fingerprint'],
                         fingerprint)
        self.assertEqual(self.db.query("SELECT signed_payload, signature FROM agent_mailbox"
                                       " WHERE id = %s", (identifier,))[0], payload)
        row = mail.normalize(mail.get(self.db, identifier))
        self.assertEqual(row['from'], 'new-worker')
        verdict = authority.verify_message(self.db, row)
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM key_events WHERE name = 'worker'")[0]['n'], key_events)

    def test_stray_old_spelling_is_swept_and_rerun_is_idempotent(self):
        journal = self.db.query("SELECT coalesce(max(id), 0) AS n FROM agent_renames")[0]['n']
        result = self.rename('worker', 'new-worker')
        self.assertEqual(result['swept'], {})
        # A raw write that bypassed the resolver, as in the commit race, is
        # stored under the primary (B1 of the review).
        stored = self.db.query("INSERT INTO agent_mailbox (sender, recipient, body)"
                               " VALUES ('worker', 'worker', 'Written under the old spelling')"
                               " RETURNING sender, recipient")[0]
        self.assertEqual((stored['sender'], stored['recipient']), ('new-worker', 'new-worker'))
        self.assertEqual([m['body'] for m in mail.unread(self.db, 'new-worker')],
                         ['Written under the old spelling'])
        # A row written without the trigger (before 0508) is left to the sweep.
        self.db.execute("ALTER TABLE agent_mailbox DISABLE TRIGGER ameesh_agent_spelling")
        try:
            self.db.execute("INSERT INTO agent_mailbox (sender, recipient, body)"
                            " VALUES ('reviewer', 'worker', 'Written before the trigger')")
        finally:
            self.db.execute("ALTER TABLE agent_mailbox ENABLE TRIGGER ameesh_agent_spelling")
        again = self.rename('worker', 'new-worker')
        self.assertTrue(again['already'])
        self.assertEqual(again['swept'], {'agent_mailbox.recipient': 1})
        self.assertEqual(again['aliases'], ['worker'])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_renames"
                                       " WHERE id > %s", (journal,))[0]['n'], 1)
        with self.assertRaises(agent_rename.RenameError):
            agent_rename.sweep(self.db, 'reviewer', 'new-worker')

    def test_every_rewritten_column_is_mapped_at_write_time(self):
        rows = self.db.query(
            "SELECT tgrelid::regclass::text AS tbl,"
            "       convert_from(substring(tgargs from 1 for length(tgargs) - 1), 'UTF8') AS arg"
            "  FROM pg_trigger WHERE tgname = 'ameesh_agent_spelling'"
            "   AND tgrelid::regclass::text NOT LIKE '%.%'")
        mapped = sorted('%s.%s' % (r['tbl'], c) for r in rows for c in r['arg'].split())
        self.assertEqual(mapped, sorted(agent_rename.REWRITTEN))

    def test_writer_racing_the_rename_lands_on_the_new_primary(self):
        """B1: the writer resolved OLD before the rename; the rename holds the
        registry row; the writer's INSERT waits, then stores NEW."""
        import threading
        from ameesh import db as db_mod
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        done = threading.Event()
        stored = {}

        def write():
            row = self.db.query("INSERT INTO agent_mailbox (sender, recipient, body)"
                                " VALUES ('reviewer', 'worker', 'Raced with the rename')"
                                " RETURNING recipient")[0]
            stored['recipient'] = row['recipient']
            done.set()

        with other.transaction() as tx:
            agent_rename._rename_tx(tx, 'worker', 'new-worker', 'human:owner', 'race test')
            writer = threading.Thread(target=write)
            writer.start()
            self.assertFalse(done.wait(1.5), "the writer must wait for the rename")
        writer.join(30)
        self.assertEqual(stored.get('recipient'), 'new-worker')
        self.assertEqual([m['body'] for m in mail.unread(self.db, 'new-worker')],
                         ['Raced with the rename'])

    def test_rename_waits_for_a_writer_holding_the_old_row(self):
        """The other order: the writer locks first, the rename rewrites its row."""
        import threading
        from ameesh import db as db_mod
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        done = threading.Event()

        def rename():
            self.rename('worker', 'new-worker')
            done.set()

        with other.transaction() as tx:
            tx.execute("INSERT INTO agent_mailbox (sender, recipient, body)"
                       " VALUES ('reviewer', 'worker', 'Written before the rename')")
            renamer = threading.Thread(target=rename)
            renamer.start()
            self.assertFalse(done.wait(1.5), "the rename must wait for the writer")
        renamer.join(60)
        self.assertTrue(done.is_set())
        self.assertEqual([m['body'] for m in mail.unread(self.db, 'new-worker')],
                         ['Written before the rename'])

    def test_journal_is_append_only(self):
        self.rename('worker', 'new-worker')
        for sql in ("UPDATE agent_renames SET reason = 'x'", "DELETE FROM agent_renames"):
            with self.subTest(sql=sql):
                with self.assertRaises(Exception):
                    self.db.execute(sql)


def _read(path):
    with open(path) as f:
        return f.read()


class LocalStateTest(unittest.TestCase):
    def test_move_and_reverse(self):
        root = tempfile.mkdtemp(prefix='ameesh-4299-')
        cfg = mock.Mock(agent_dir=lambda name: os.path.join(root, name))
        os.makedirs(os.path.join(root, 'worker'))
        with open(os.path.join(root, 'worker', 'session'), 'w') as f:
            f.write('sid')
        self.assertEqual(agent_rename.move_local_state(cfg, 'worker', 'new-worker'), 'moved')
        self.assertEqual(agent_rename.move_local_state(cfg, 'worker', 'new-worker'), 'already moved')
        self.assertTrue(os.path.islink(os.path.join(root, 'worker')))
        self.assertEqual(_read(os.path.join(root, 'worker', 'session')), 'sid')
        self.assertEqual(agent_rename.move_local_state(cfg, 'new-worker', 'worker'), 'moved')
        self.assertFalse(os.path.islink(os.path.join(root, 'worker')))
        self.assertTrue(os.path.islink(os.path.join(root, 'new-worker')))
        self.assertEqual(_read(os.path.join(root, 'new-worker', 'session')), 'sid')
        self.assertEqual(agent_rename.move_local_state(cfg, 'third', 'fourth'), 'no local state')
        os.makedirs(os.path.join(root, 'third'))
        os.makedirs(os.path.join(root, 'fourth'))
        with self.assertRaises(agent_rename.RenameError):
            agent_rename.move_local_state(cfg, 'third', 'fourth')


# Generic columns matched by IDENTITY_COLUMNS that never hold agent names.
IGNORED: set = set()


if __name__ == '__main__':
    unittest.main()
