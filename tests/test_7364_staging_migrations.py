# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from ameesh import staging_migrations as sm
from ameesh import nexlink_release as nx, release_cutoff as rc


class StagingMigrationsTest(unittest.TestCase):
    def setUp(self):
        self.policy = sm.load_policy((Path(__file__).parents[1] / "deploy" /
                                     "staging-migrations-policy.example.json").read_text())
        self.path = "migrations/796_example.sql"

    def check(self, sql, **kwargs):
        return sm.check(self.path, sql.encode(), self.policy,
                        read_file=kwargs.get("read_file", lambda path: b"-- restore"),
                        read_notes=kwargs.get("read_notes", lambda lot: []),
                        signed_decision=kwargs.get("signed_decision", lambda *args: False))

    def declare(self, sql, **extra):
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        self.policy["declarations"][self.path] = dict(checksum=checksum, **extra)
        return sm.declaration_text(self.path, checksum)

    def test_additive_and_comment_literals(self):
        sql = "-- DROP TABLE users;\nCREATE TABLE messages (body text default 'delete from users');"
        self.assertEqual(self.check(sql).kind, "additive")

    def test_relaxation_needs_matching_note_and_frozen_restore(self):
        sql = "ALTER TABLE messages DROP CONSTRAINT IF EXISTS fk;"
        with self.assertRaisesRegex(sm.Refused, "missing declaration"):
            self.check(sql)
        text = self.declare(sql, lot=7266, restore="rollback/796_restore.sql")
        with self.assertRaisesRegex(sm.Refused, "carrier lot"):
            self.check(sql)
        with self.assertRaisesRegex(sm.Refused, "restore"):
            self.check(sql, read_notes=lambda lot: [text], read_file=lambda path: b"")
        self.assertEqual(self.check(sql, read_notes=lambda lot: [text]).kind, "relaxation")
        with self.assertRaisesRegex(sm.Refused, "checksum"):
            self.check(sql + "\n", read_notes=lambda lot: [text])

    def test_multiple_alter_actions_cannot_hide_a_constraint_drop(self):
        sql = "ALTER TABLE messages ADD COLUMN v int, DROP CONSTRAINT fk;"
        self.assertEqual(sm.classify(sql, self.policy).kind, "relaxation")
        with self.assertRaises(sm.Refused):
            self.check(sql)

    def test_destructive_requires_explicit_signed_authorization(self):
        for sql in ("DROP TABLE messages;", "ALTER TABLE messages DROP COLUMN body;",
                    "REVOKE SELECT ON messages FROM public;", "ALTER TYPE state ADD VALUE 'gone';",
                    "DELETE FROM messages;", "UPDATE messages SET body = 'gone';",
                    "ALTER TABLE messages ALTER COLUMN body SET NOT NULL;",
                    "ALTER TABLE messages ADD CONSTRAINT ck CHECK (body IS NOT NULL);"):
            with self.subTest(sql=sql):
                self.assertEqual(sm.classify(sql, self.policy).kind, "destructive")
                text = self.declare(sql, decision=42)
                with self.assertRaisesRegex(sm.Refused, "signed owner"):
                    self.check(sql)
                calls = []
                self.assertEqual(self.check(sql, signed_decision=lambda *args:
                                           calls.append(args) or True).kind, "destructive")
                self.assertEqual(calls, [(42, "allow-" + text)])

    def test_unknown_or_malformed_never_inherits_a_declaration(self):
        for sql in ("DO $$ BEGIN DROP TABLE messages; END $$;", "SELECT dangerous();",
                    "CREATE TABLE x (v text default 'unterminated);",
                    "ALTER TABLE x DROP CONSTRAINT fk; VACUUM x;", "-- no SQL"):
            with self.subTest(sql=sql), self.assertRaisesRegex(sm.Refused, "unknown"):
                self.declare(sql, lot=1, restore="x_restore.sql", decision=1)
                self.check(sql, signed_decision=lambda *args: True,
                           read_notes=lambda lot: [self.declare(sql)])

    def test_796_and_797_incident_shapes_are_refused(self):
        sql796 = """BEGIN;
        ALTER TABLE public.channel_command_grants DROP CONSTRAINT IF EXISTS channel_id_fkey;
        ALTER TABLE public.channel_commands DROP CONSTRAINT IF EXISTS channel_id_fkey;
        CREATE OR REPLACE FUNCTION public.guard() RETURNS trigger LANGUAGE plpgsql
        AS $$ BEGIN RETURN NEW; END $$;
        COMMIT;"""
        self.assertEqual(sm.classify(sql796, self.policy).kind, "relaxation")
        with self.assertRaises(sm.Refused):
            self.check(sql796)
        # 797 wraps its five drops in an anonymous block: lexical doubt refuses.
        sql797 = "BEGIN; DO $$ BEGIN " + " ".join(
            "ALTER TABLE t%d DROP CONSTRAINT fk%d;" % (i, i) for i in range(5)) + " END $$; COMMIT;"
        self.assertEqual(sm.classify(sql797, self.policy).kind, "unknown")
        with self.assertRaises(sm.Refused):
            self.check(sql797)

    def test_policy_missing_or_invalid_refuses_before_handoff(self):
        with self.assertRaises(sm.Refused):
            sm.load_policy('{}')
        with mock.patch.object(nx, "staging_jobs"), \
                mock.patch.object(nx, "migration_requirements", return_value={"hub": [{}]}), \
                mock.patch.object(nx.subprocess, "run", side_effect=nx.subprocess.CalledProcessError(128, "git")):
            with self.assertRaisesRegex(rc.ReleaseError, "preflight refused"):
                nx.handoff_preflight(mock.Mock(), {"sha": "a" * 40, "main": "b" * 40})


if __name__ == "__main__":
    unittest.main()
