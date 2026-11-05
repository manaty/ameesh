# SPDX-License-Identifier: AGPL-3.0-only
"""#3885 — failure fingerprints: one open lot per identity, P1 on a user.

The tests mirror the lot's witnesses: two concurrent passes open ONE lot; a
duplicate fingerprint is noted on the open lot, never opened twice; a user
touched gives P1; a recurrence after closure opens a new linked lot; the read
contract refuses an unknown field; and the pass refuses explicitly without
its read-only token.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import db as db_mod, fingerprints, httpkit, migrations, notify, role_timers, storage, work

from .support import PgTestCase
from .test_l122_numeros_de_migration import AMEESH, _depot

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "fingerprints", "nexlink-page.json")


def page():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def declaration(url="https://nexlink.example/api/fingerprints", interval=60,
                environment="production"):
    return fingerprints.Declaration("nexlink", url, float(interval), "watch",
                                    environment)


TOKEN = "a" * 40


class PrimitiveTest(PgTestCase):
    def open(self, **changes):
        values = dict(app="ameesh", key="v1:0123456789abcdef0123456789abcdef",
                      title="Panne serveur POST /api/v1/requests — 23505",
                      users=0, count=3, route="POST /api/v1/requests",
                      code="23505", revision="rev-abc",
                      first_seen="2026-10-27T04:00:00Z",
                      last_seen="2026-10-27T04:30:00Z", actor="test")
        values.update(changes)
        return fingerprints.open_lot(self.db, **values)

    def rows(self, source=None):
        if source:
            return self.db.query("SELECT id, state, priority, title, body FROM work_items"
                                 " WHERE source = %s ORDER BY id", (source,))
        return self.db.query("SELECT id, state, priority, title, body FROM work_items"
                             " ORDER BY id")

    def notes(self, lot):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def test_first_open_is_p2_and_carries_the_contract(self):
        result = self.open(users=0)
        self.assertTrue(result["created"])
        self.assertEqual(result["priority"], 2)
        item = work.get(self.db, result["lot"])
        self.assertEqual(item["source"],
                         "empreinte:ameesh:v1:0123456789abcdef0123456789abcdef")
        self.assertEqual(item["type"], "bug")
        self.assertEqual(item["app"], "ameesh")
        for text in ("POST /api/v1/requests", "23505", "rev-abc",
                     "2026-10-27T04:30:00Z", "Occurrences : 3",
                     "Utilisateurs touchés : 0", "Un seul lot ouvert par empreinte",
                     "Environnement : production"):
            self.assertIn(text, item["body"])

    def test_a_staging_lot_is_marked_capped_p2_and_the_identity_stays_shared(self):
        first = self.open(users=1, environment="staging")
        self.assertTrue(first["created"])
        self.assertEqual(first["environment"], "staging")
        # #5987: one user on staging is a tester, not a P1.
        self.assertEqual(first["priority"], 2)
        item = work.get(self.db, first["lot"])
        self.assertTrue(item["title"].startswith("[staging] "), item["title"])
        self.assertIn("Environnement : staging", item["body"])
        self.assertIn("P2 au plus", item["body"])
        self.assertEqual(item["priority"], 2)
        self.assertEqual(item["source"],
                         "empreinte:ameesh:v1:0123456789abcdef0123456789abcdef")
        # production sees the same fingerprint: one lot, noted, not a second,
        # and a real user there still promotes it to P1.
        second = self.open(users=1, environment="production")
        self.assertFalse(second["created"])
        self.assertTrue(second["noted"])
        self.assertTrue(second["escalated"])
        self.assertEqual(second["lot"], first["lot"])
        self.assertEqual(second["priority"], 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(any("environnement=production" in note
                            for note in self.notes(first["lot"])))
        self.assertTrue(work.get(self.db, first["lot"])["title"].startswith("[staging] "))
        self.assertEqual(work.get(self.db, first["lot"])["priority"], 1)

    def test_a_staging_occurrence_never_promotes_an_open_lot(self):
        first = self.open(users=0)
        second = self.open(users=3, count=9, environment="staging")
        self.assertFalse(second["created"])
        self.assertTrue(second["noted"])
        self.assertFalse(second["escalated"])
        self.assertEqual(second["priority"], 2)
        self.assertEqual(work.get(self.db, first["lot"])["priority"], 2)
        notes = self.notes(first["lot"])
        self.assertTrue(any("staging : pas d'escalade P1" in note
                            for note in notes), notes)

    def test_an_unknown_environment_is_refused(self):
        with self.assertRaisesRegex(fingerprints.FingerprintError, "environment"):
            self.open(environment="preprod")
        self.assertEqual(self.rows(), [])

    def test_a_user_touched_gives_p1_at_creation(self):
        result = self.open(users=1)
        self.assertEqual(result["priority"], 1)
        self.assertEqual(work.get(self.db, result["lot"])["priority"], 1)

    def test_duplicate_is_noted_not_opened_and_promotes_to_p1(self):
        first = self.open(users=0)
        second = self.open(users=2, count=7, revision="rev-def",
                           last_seen="2026-10-27T05:00:00Z")
        self.assertFalse(second["created"])
        self.assertTrue(second["noted"])
        self.assertTrue(second["escalated"])
        self.assertEqual(second["lot"], first["lot"])
        self.assertEqual(len(self.rows()), 1)
        item = work.get(self.db, first["lot"])
        self.assertEqual(item["priority"], 1)
        notes = self.notes(first["lot"])
        self.assertTrue(any("occurrences=7" in note and "rev-def" in note
                            and "2026-10-27T05:00:00Z" in note and "P1" in note
                            for note in notes), notes)

    def test_recurrence_after_closure_opens_a_linked_new_lot(self):
        first = self.open(users=0)
        work.close(self.db, first["lot"], abandoned=True, actor="test")
        second = self.open(users=1)
        self.assertTrue(second["created"])
        self.assertEqual(second["recurrence_of"], first["lot"])
        self.assertEqual(second["priority"], 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertIn("récidive de #%d" % first["lot"], rows[1]["title"])
        self.assertIn("Récidive du lot #%d" % first["lot"], rows[1]["body"])
        # the old lot is still the same identity, closed
        self.assertEqual(rows[0]["state"], "closed")

    def test_database_index_refuses_a_second_open_without_the_primitive(self):
        opened = self.open(users=1)
        source = work.get(self.db, opened["lot"])["source"]
        with self.assertRaises(db_mod.DbError) as ctx:
            work.add(self.db, title="manual duplicate", source=source,
                     app="ameesh", type="bug", actor="test")
        self.assertTrue(db_mod.is_unique_violation(
            ctx.exception, fingerprints.FINGERPRINT_INDEX))

    def test_two_concurrent_passes_open_one_lot_and_note_the_other(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                results.append(open_with(db))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                db.close()

        def open_with(db):
            return fingerprints.open_lot(
                db, app="ameesh", key="v1:concurrent", title="Panne concurrente",
                users=1, count=1, route="/x", code="23505", revision="rev",
                actor="test")

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for row in results if row["created"]), 1)
        self.assertEqual(sum(1 for row in results if row["noted"]), 1)
        rows = self.rows(fingerprints.source_of("ameesh", "v1:concurrent"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("Empreinte revue" in note
                            for note in self.notes(rows[0]["id"])))

    def test_a_secret_or_an_email_in_the_source_is_refused(self):
        for field, value in (
                ("route", "/x token=abc123456789012"),
                ("code", "user@example.com"),
                ("key", "user@example.com"),
                ("key", "/x token=abc123456789012"),
                ("app", "ghp_0123456789abcdefghij")):
            with self.subTest(field=field):
                with self.assertRaises(fingerprints.FingerprintError):
                    self.open(**{field: value})
                self.assertEqual(self.rows(), [])

    def test_the_cli_opens_then_notes(self):
        args = ("work", "fingerprint", "open", "--app", "ameesh",
                "--key", "cli-key", "--title", "Panne CLI", "--users", "1",
                "--route", "/cli", "--code", "XX000", "--revision", "rev-cli")
        first = self.mesh(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("opened", first.stdout)
        second = self.mesh(*args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("occurrence noted", second.stdout)
        rows = self.rows(fingerprints.source_of("ameesh", "cli-key"))
        self.assertEqual(len(rows), 1)


class ContractTest(unittest.TestCase):
    def test_the_fixture_is_read_with_its_cursor(self):
        entries, cursor = fingerprints.parse_page(page())
        self.assertEqual(cursor, "2026-10-27T04:40:00Z:2")
        self.assertEqual([entry.fingerprint for entry in entries],
                         ["v1:0123456789abcdef0123456789abcdef",
                          "v1:fedcba9876543210fedcba9876543210"])
        self.assertEqual(entries[0].users, 1)
        self.assertEqual(entries[1].occurrences, 2)

    def test_an_unknown_field_is_refused_at_both_levels(self):
        payload = page()
        payload["extra"] = 1
        with self.assertRaisesRegex(fingerprints.FingerprintError, "unknown field"):
            fingerprints.parse_page(payload)
        payload = page()
        payload["fingerprints"][0]["personal_email"] = "x@y.z"
        with self.assertRaisesRegex(fingerprints.FingerprintError, "unknown field"):
            fingerprints.parse_page(payload)

    def test_a_missing_field_and_a_bad_schema_are_refused(self):
        payload = page()
        del payload["fingerprints"][1]["revision"]
        with self.assertRaisesRegex(fingerprints.FingerprintError, "missing field"):
            fingerprints.parse_page(payload)
        payload = page()
        payload["schema"] = "other/1"
        with self.assertRaisesRegex(fingerprints.FingerprintError, "schema"):
            fingerprints.parse_page(payload)

    def test_a_wrong_type_is_refused(self):
        payload = page()
        payload["fingerprints"][0]["users"] = "1"
        with self.assertRaisesRegex(fingerprints.FingerprintError, "integer"):
            fingerprints.parse_page(payload)
        payload = page()
        payload["fingerprints"][0]["users"] = True
        with self.assertRaisesRegex(fingerprints.FingerprintError, "integer"):
            fingerprints.parse_page(payload)

    def test_a_same_fingerprint_on_two_revisions_is_kept_per_revision(self):
        payload = page()
        payload["fingerprints"].append(dict(
            payload["fingerprints"][0], revision="rev-def", occurrences=4))
        entries, _cursor = fingerprints.parse_page(payload)
        self.assertEqual(len(entries), 3)
        self.assertEqual([entry.revision for entry in entries],
                         ["rev-abc", "rev-abc", "rev-def"])

    def test_a_secret_in_an_entry_is_refused(self):
        payload = page()
        payload["fingerprints"][0]["route"] = "/x token=abc123456789012"
        with self.assertRaisesRegex(fingerprints.FingerprintError, "secret"):
            fingerprints.parse_page(payload)


class DeclarationTest(unittest.TestCase):
    def bundle(self, **changes):
        spec = dict(title="watcher", roles=["github-watch"],
                    effective_projects=["nexlink"],
                    fiche=types.SimpleNamespace(data={"fingerprints": {
                        "nexlink": {"url": "https://example.test/fingerprints",
                                    "interval": 120}}}))
        spec.update(changes)
        return types.SimpleNamespace(agents=[types.SimpleNamespace(**spec)])

    def test_a_valid_card_is_read(self):
        decls, error = fingerprints.declarations([self.bundle()])
        self.assertEqual(error, "")
        self.assertEqual(decls, [fingerprints.Declaration(
            "nexlink", "https://example.test/fingerprints", 120.0, "watcher")])

    def test_role_project_url_and_unknown_setting_are_isolated(self):
        cases = (
            (dict(roles=[]), "role github-watch"),
            (dict(effective_projects=["other"]), "must belong"),
            (dict(fiche=types.SimpleNamespace(data={"fingerprints": {
                "nexlink": {"url": "http://example.test/fingerprints"}}})),
             "https"),
            (dict(fiche=types.SimpleNamespace(data={"fingerprints": {
                "nexlink": {"url": "https://example.test/f", "token": "x"}}})),
             "unknown"),
        )
        for change, reason in cases:
            with self.subTest(reason=reason):
                warnings = []
                self.assertEqual(fingerprints.declarations([self.bundle(**change)],
                                                           log=warnings.append), ([], ""))
                self.assertTrue(any(reason in warning for warning in warnings), warnings)

    def test_a_bad_cadence_is_isolated(self):
        for interval in (True, 0, 3601, "60"):
            with self.subTest(interval=interval):
                warnings = []
                bundle = self.bundle(
                    fiche=types.SimpleNamespace(data={"fingerprints": {
                        "nexlink": {"url": "https://example.test/f",
                                    "interval": interval}}}))
                self.assertEqual(fingerprints.declarations([bundle],
                                                           log=warnings.append),
                                 ([], ""))
                self.assertTrue(any("interval" in warning for warning in warnings))

    def test_several_sources_carry_their_environment(self):
        bundle = self.bundle(fiche=types.SimpleNamespace(data={"fingerprints": {
            "nexlink": {"sources": [
                {"url": "https://api.example.test/f", "interval": 60},
                {"url": "https://api-staging.example.test/f", "interval": 120,
                 "environment": "staging"},
            ]}}}))
        decls, error = fingerprints.declarations([bundle])
        self.assertEqual(error, "")
        self.assertEqual(decls, [
            fingerprints.Declaration("nexlink", "https://api.example.test/f",
                                     60.0, "watcher"),
            fingerprints.Declaration("nexlink", "https://api-staging.example.test/f",
                                     120.0, "watcher", "staging"),
        ])
        self.assertEqual(decls[0].token_path(), fingerprints.DEFAULT_TOKEN_FILE)
        self.assertEqual(decls[1].token_path(),
                         fingerprints.DEFAULT_STAGING_TOKEN_FILE)

    def test_an_invalid_source_does_not_discard_its_siblings(self):
        warnings = []
        bundle = self.bundle(fiche=types.SimpleNamespace(data={"fingerprints": {
            "nexlink": {"sources": [
                {"url": "https://api.example.test/f"},
                {"url": "https://api-staging.example.test/f",
                 "environment": "preprod"},
            ]}}}))
        decls, error = fingerprints.declarations([bundle], log=warnings.append)
        self.assertEqual(error, "")
        self.assertEqual([item.url for item in decls],
                         ["https://api.example.test/f"])
        self.assertTrue(any("environment" in warning for warning in warnings), warnings)

    def test_a_duplicate_url_is_kept_once(self):
        warnings = []
        bundle = self.bundle(fiche=types.SimpleNamespace(data={"fingerprints": {
            "nexlink": {"sources": [
                {"url": "https://api.example.test/f"},
                {"url": "https://api.example.test/f", "interval": 120},
            ]}}}))
        decls, _error = fingerprints.declarations([bundle], log=warnings.append)
        self.assertEqual(len(decls), 1)
        self.assertTrue(any("duplicate" in warning for warning in warnings), warnings)

    def test_a_duplicate_environment_is_kept_once(self):
        warnings = []
        bundle = self.bundle(fiche=types.SimpleNamespace(data={"fingerprints": {
            "nexlink": {"sources": [
                {"url": "https://api.example.test/f", "environment": "staging"},
                {"url": "https://api-2.example.test/f", "environment": "staging"},
            ]}}}))
        decls, _error = fingerprints.declarations([bundle], log=warnings.append)
        self.assertEqual([item.url for item in decls],
                         ["https://api.example.test/f"])
        self.assertTrue(any("duplicate" in warning for warning in warnings), warnings)

    def test_url_and_sources_cannot_be_mixed(self):
        warnings = []
        bundle = self.bundle(fiche=types.SimpleNamespace(data={"fingerprints": {
            "nexlink": {"url": "https://api.example.test/f",
                        "sources": [{"url": "https://api-staging.example.test/f"}]}}}))
        self.assertEqual(fingerprints.declarations([bundle], log=warnings.append),
                         ([], ""))
        self.assertTrue(any("unknown" in warning for warning in warnings), warnings)

    def test_a_token_file_setting_is_refused(self):
        # The 0600 path is derived from the environment: a card must not be
        # able to point the pass at another credential's file (#5968 review B1).
        warnings = []
        bundle = self.bundle(fiche=types.SimpleNamespace(data={"fingerprints": {
            "nexlink": {"sources": [{"url": "https://api.example.test/f",
                                     "token_file": "~/.config/gh/hosts.yml"}]}}}))
        self.assertEqual(fingerprints.declarations([bundle], log=warnings.append),
                         ([], ""))
        self.assertTrue(any("unknown" in warning for warning in warnings), warnings)


class TickTest(PgTestCase):
    def setUp(self):
        # These witnesses share the project name and read the cursor: start
        # from an empty state, never from the previous test's rows.
        self.db.execute("DELETE FROM fingerprint_source_state")
        self.db.execute("DELETE FROM fingerprint_watch_state")
        self.db.execute("DELETE FROM work_item_events")
        self.db.execute("DELETE FROM work_items")

    def cursor_state(self, project="nexlink", environment="production"):
        return storage.of(self.db).fingerprints.state(project, environment)

    def token_file(self, content=TOKEN, name="fingerprints.token"):
        path = os.path.join(self.tmp, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (content + "\n").encode("ascii"))
        finally:
            os.close(fd)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        return path

    def test_the_pass_opens_both_entries_and_advances_its_cursor(self):
        payload = page()
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=payload):
            results = fingerprints.tick(self.cfg, self.db,
                                        token_file=self.token_file(),
                                        actor="mesh-design")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row["created"] for row in results))
        rows = self.db.query("SELECT id, priority, title FROM work_items ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["priority"] for row in rows], [1, 2])
        state = self.cursor_state()
        self.assertEqual(state["cursor"], payload["cursor"])
        self.assertEqual(state["last_error"], "")

    def test_a_second_read_notes_the_entries_without_new_lots(self):
        payload = page()
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=payload):
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=1000.0)
            results = fingerprints.tick(self.cfg, self.db,
                                        token_file=self.token_file(),
                                        actor="mesh-design", now=1061.0)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not row["created"] and row["noted"] for row in results))
        self.assertEqual(len(self.db.query("SELECT id FROM work_items")), 2)
        notes = [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events ORDER BY id")]
        self.assertEqual(sum(1 for note in notes if "Empreinte revue" in note), 2)

    def test_a_null_cursor_never_clears_the_previous_one(self):
        payload = page()
        first = dict(payload, cursor="cur-1")
        second = dict(payload, cursor=None)
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page",
                                  side_effect=[first, second]) as read:
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=1000.0)
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=1061.0)
        state = self.cursor_state()
        self.assertEqual(state["cursor"], "cur-1")
        # the second read still asked for the previous cursor, never without
        self.assertEqual(read.call_args_list[1].args[1], "cur-1")
        self.assertEqual(len(self.db.query("SELECT id FROM work_items")), 2)

    def test_the_declared_cadence_skips_a_too_early_read(self):
        payload = page()
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration(interval=60)], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=payload) as read:
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=1000.0)
            self.assertEqual(fingerprints.tick(self.cfg, self.db,
                                               token_file=self.token_file(),
                                               actor="mesh-design", now=1030.0), [])
            self.assertEqual(read.call_count, 1)
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=1061.0)
            self.assertEqual(read.call_count, 2)

    def test_without_a_token_the_pass_refuses_and_opens_nothing(self):
        missing = os.path.join(self.tmp, "missing.token")
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")):
            with self.assertRaisesRegex(fingerprints.FingerprintError, "token"):
                fingerprints.tick(self.cfg, self.db, token_file=missing)
        self.assertEqual(self.db.query("SELECT id FROM work_items"), [])
        state = self.cursor_state()
        self.assertIsNotNone(state)
        self.assertIn("token", state["last_error"])
        self.assertIsNone(state["cursor"])

    def test_two_sources_keep_two_cursors_and_share_the_lot(self):
        payload = page()
        staging = declaration(url="https://staging.example.test/f",
                              environment="staging")
        production = declaration(url="https://prod.example.test/f")
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([staging, production], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=payload):
            first = fingerprints.tick(self.cfg, self.db,
                                      token_file=self.token_file(), now=1000.0)
            second = fingerprints.tick(self.cfg, self.db,
                                       token_file=self.token_file(), now=1061.0)
        # staging is read first: it opens the two lots, marked
        self.assertEqual(len(first), 4)
        self.assertTrue(all(row["created"] for row in first[:2]))
        self.assertTrue(all(not row["created"] and row["noted"]
                            for row in first[2:]))
        self.assertEqual({row["environment"] for row in first[:2]}, {"staging"})
        lots = self.db.query("SELECT id, title FROM work_items ORDER BY id")
        self.assertEqual(len(lots), 2)
        self.assertTrue(all(row["title"].startswith("[staging] ") for row in lots))
        rows = self.db.query(
            "SELECT project, url, environment, cursor FROM fingerprint_source_state"
            " ORDER BY url")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["environment"] for row in rows},
                         {"staging", "production"})
        self.assertTrue(all(row["cursor"] == payload["cursor"] for row in rows))
        # the second pass notes both lots from both sources, no new lot
        self.assertEqual(len(second), 4)
        self.assertTrue(all(not row["created"] and row["noted"] for row in second))
        self.assertEqual(len(self.db.query("SELECT id FROM work_items")), 2)
        self.assertTrue(any("environnement=production" in note
                            for note in (row["note"] for row in self.db.query(
                                "SELECT note FROM work_item_events"))))

    def test_a_staging_pass_opens_p2_then_a_production_pass_promotes(self):
        # #5987: a staging-only source never gives P1; the production
        # occurrence of the same fingerprint still promotes the shared lot.
        payload = page()
        staging = declaration(url="https://staging.example.test/f",
                              environment="staging")
        production = declaration(url="https://prod.example.test/f")
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([staging], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=payload):
            opened = fingerprints.tick(self.cfg, self.db,
                                       token_file=self.token_file(), now=1000.0)
        self.assertEqual(len(opened), 2)
        self.assertEqual([row["priority"] for row in opened], [2, 2])
        self.assertEqual([row["priority"] for row in self.db.query(
            "SELECT id, priority FROM work_items ORDER BY id")], [2, 2])
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([production], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=payload):
            noted = fingerprints.tick(self.cfg, self.db,
                                      token_file=self.token_file(), now=1061.0)
        self.assertEqual(len(noted), 2)
        self.assertTrue(all(not row["created"] and row["noted"] for row in noted))
        self.assertEqual([row["escalated"] for row in noted], [True, False])
        self.assertEqual([row["priority"] for row in self.db.query(
            "SELECT id, priority FROM work_items ORDER BY id")], [1, 2])

    def test_a_changed_endpoint_does_not_lend_its_cursor(self):
        # #5968 review P2: a failure at the new URL must not leave the old
        # journal's cursor behind; the next tick reads from the beginning.
        api = storage.of(self.db).fingerprints
        api.advance("nexlink", "https://old.example.test/f", "old-cursor",
                    now=1000.0)
        api.fail("nexlink", "https://new.example.test/f", "HTTP 503",
                 now=1000.0)
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration(
                                   url="https://new.example.test/f")], "")), \
                mock.patch.object(fingerprints, "read_page",
                                  return_value=page()) as read:
            fingerprints.tick(self.cfg, self.db,
                              token_file=self.token_file(), now=2000.0)
        self.assertIsNone(read.call_args.args[1])

    def test_each_source_reads_its_own_token_file(self):
        staging = declaration(url="https://staging.example.test/f",
                              environment="staging")
        production = declaration(url="https://prod.example.test/f")
        seen = []

        def read(decl, cursor, token, **kwargs):
            seen.append((decl.url, token))
            return page()

        def token(path, **kwargs):
            return {fingerprints.DEFAULT_STAGING_TOKEN_FILE: "b" * 40,
                    fingerprints.DEFAULT_TOKEN_FILE: TOKEN}[path]

        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([staging, production], "")), \
                mock.patch.object(fingerprints, "read_page", side_effect=read), \
                mock.patch.object(httpkit, "read_token_file",
                                  side_effect=token) as read_token:
            fingerprints.tick(self.cfg, self.db, now=1000.0)
        self.assertEqual([call.args[0] for call in read_token.call_args_list],
                         [fingerprints.DEFAULT_STAGING_TOKEN_FILE,
                          fingerprints.DEFAULT_TOKEN_FILE])
        self.assertEqual(seen, [("https://staging.example.test/f", "b" * 40),
                                ("https://prod.example.test/f", TOKEN)])

    def test_the_legacy_token_env_only_names_production(self):
        # #5968 review B2: a staging source must never read the production
        # token through AMEESH_FINGERPRINTS_TOKEN_FILE.
        staging = declaration(url="https://staging.example.test/f",
                              environment="staging")
        production = declaration(url="https://prod.example.test/f")
        paths = []

        def token(path, **kwargs):
            paths.append(path)
            return {fingerprints.DEFAULT_STAGING_TOKEN_FILE: "b" * 40,
                    "/legacy/fingerprints.token": TOKEN}[path]

        with mock.patch.dict(os.environ,
                             {"AMEESH_FINGERPRINTS_TOKEN_FILE":
                              "/legacy/fingerprints.token"}), \
                mock.patch.object(fingerprints, "declarations_of_config",
                                  return_value=([staging, production], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=page()), \
                mock.patch.object(httpkit, "read_token_file", side_effect=token):
            fingerprints.tick(self.cfg, self.db, now=1000.0)
        self.assertEqual(paths, [fingerprints.DEFAULT_STAGING_TOKEN_FILE,
                                 "/legacy/fingerprints.token"])

    def test_a_missing_staging_token_does_not_stop_production(self):
        staging = declaration(url="https://staging.example.test/f",
                              environment="staging")
        production = declaration(url="https://prod.example.test/f")

        def token(path, **kwargs):
            if path == fingerprints.DEFAULT_STAGING_TOKEN_FILE:
                raise ValueError("fingerprints read-only token not found: %s" % path)
            return TOKEN

        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([staging, production], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=page()), \
                mock.patch.object(httpkit, "read_token_file", side_effect=token):
            with self.assertRaisesRegex(fingerprints.FingerprintError, "not found"):
                fingerprints.tick(self.cfg, self.db, now=1000.0)
        self.assertEqual(len(self.db.query("SELECT id FROM work_items")), 2)
        failed = self.cursor_state(environment="staging")
        self.assertIn("not found", failed["last_error"])
        healthy = self.cursor_state(environment="production")
        self.assertEqual(healthy["last_error"], "")
        self.assertEqual(healthy["cursor"], page()["cursor"])

    def test_an_unknown_field_refuses_the_page_and_keeps_the_cursor(self):
        bad = page()
        bad["root_extra"] = True
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=bad):
            with self.assertRaisesRegex(fingerprints.FingerprintError, "unknown field"):
                fingerprints.tick(self.cfg, self.db, token_file=self.token_file())
        self.assertEqual(self.db.query("SELECT id FROM work_items"), [])
        state = self.cursor_state()
        self.assertIn("unknown field", state["last_error"])
        self.assertIsNone(state["cursor"])

    def source_alerts(self, now, **kwargs):
        kwargs.setdefault("sources", [declaration()])
        return fingerprints.alerts(self.db, now, **kwargs)

    def refused(self):
        return fingerprints.FingerprintError("fingerprints source nexlink: HTTP 401")

    def test_a_refused_token_is_retried_every_15_min_not_each_minute(self):
        # #5695: AG2 failed 1 365 times on 03/11 on one rejected token.
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page",
                                  side_effect=self.refused()) as read:
            with self.assertRaisesRegex(fingerprints.FingerprintError, "HTTP 401"):
                fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                                  now=1000.0)
            self.assertEqual(fingerprints.tick(self.cfg, self.db,
                                               token_file=self.token_file(),
                                               now=1061.0), [])
            self.assertEqual(read.call_count, 1)
            with self.assertRaisesRegex(fingerprints.FingerprintError, "HTTP 401"):
                fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                                  now=1000.0 + fingerprints.AUTH_RETRY_S)
            self.assertEqual(read.call_count, 2)
        state = self.cursor_state()
        self.assertIn("HTTP 401", state["last_error"])

    def test_another_failure_keeps_the_declared_cadence(self):
        error = fingerprints.FingerprintError("fingerprints source nexlink: HTTP 503")
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page", side_effect=error) as read:
            for moment in (1000.0, 1061.0):
                with self.assertRaisesRegex(fingerprints.FingerprintError, "HTTP 503"):
                    fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                                      now=moment)
        self.assertEqual(read.call_count, 2)

    def test_a_refused_source_alerts_its_project_with_the_gesture(self):
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page", side_effect=self.refused()):
            with self.assertRaises(fingerprints.FingerprintError):
                fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                                  now=1000.0)
        # never read: no healthy moment to wait from, the alert is immediate
        alerts = self.source_alerts(1001.0, orchestrators=["pilot"])
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "fingerprints_source_failing")
        self.assertEqual(alert["project"], "nexlink")
        self.assertTrue(alert["auth_refused"])
        self.assertIn("with its owner", alert["detail"])
        self.assertNotIn("Nexlink", alert["detail"])
        self.assertIn("every 900s", alert["detail"])
        self.assertNotIn(TOKEN, alert["detail"])
        self.assertIn("fingerprints_source_failing", notify.TYPE_LABELS)
        from ameesh import alert_briefs, exploitation
        self.assertIn("fingerprints_source_failing", exploitation.ALERT_TYPES)
        listing = [{"name": "pilot", "projects": ["nexlink"]},
                   {"name": "other", "projects": ["elsewhere"]}]
        alert["orchestrators"] = ["pilot", "other"]
        self.assertEqual(set(alert_briefs.briefs(alert, listing)) - {"auditeur"},
                         {"pilot"})

    def test_active_source_reports_its_actual_retry_interval(self):
        source = fingerprints.Declaration("acme", "https://acme.example/f", 1800)
        storage.of(self.db).fingerprints.fail(
            source.project, source.url, "fingerprints source acme: HTTP 401", now=1000)
        alerts = self.source_alerts(1001, sources=[source])
        self.assertEqual(len(alerts), 1)
        self.assertIn("every 1800s", alerts[0]["detail"])
        self.assertNotIn("Nexlink", alerts[0]["detail"])

    def test_removed_or_replaced_source_does_not_alert(self):
        storage.of(self.db).fingerprints.fail(
            "nexlink", declaration().url, str(self.refused()), now=1000)
        self.assertEqual(self.source_alerts(1001, sources=[]), [])
        self.assertEqual(self.source_alerts(1001, sources=[declaration(
            url="https://replacement.example/f")]), [])
        self.assertEqual(len(self.source_alerts(1001)), 1)

    def test_two_failing_sources_of_one_pass_are_two_alerts(self):
        # #5695 verdict mty-amh-impl-03: `advance` dates both reads with the
        # same `now`; the alert identity must still tell the projects apart.
        from ameesh import exploitation
        api = storage.of(self.db).fingerprints
        for project in ("nexlink", "liquichain"):
            api.advance(project, "https://%s.example/f" % project, "c", now=1000.0)
            api.fail(project, "https://%s.example/f" % project,
                     "fingerprints source %s: HTTP 401" % project, now=1061.0)
        alerts = self.source_alerts(1000.0 + fingerprints.FAILING_ALERT_S,
                                   sources=[fingerprints.Declaration(
                                       p, "https://%s.example/f" % p, 60)
                                       for p in ("nexlink", "liquichain")])
        self.assertEqual(sorted(a["project"] for a in alerts), ["liquichain", "nexlink"])
        self.assertEqual(len({exploitation.alert_key(a) for a in alerts}), 2)

    def test_two_failing_sources_of_one_project_are_two_alerts(self):
        # #5964: production and staging of one project may fail in the same
        # pass; the URL tells them apart, not only the project.
        from ameesh import exploitation
        api = storage.of(self.db).fingerprints
        sources = [
            fingerprints.Declaration("nexlink", "https://prod.example/f", 60),
            fingerprints.Declaration("nexlink", "https://staging.example/f", 60,
                                     "watch", "staging"),
        ]
        for source in sources:
            api.advance(source.project, source.url, "c",
                        environment=source.environment, now=1000.0)
            api.fail(source.project, source.url,
                     "fingerprints source nexlink: HTTP 401", now=1061.0,
                     environment=source.environment)
        alerts = self.source_alerts(1000.0 + fingerprints.FAILING_ALERT_S,
                                   sources=sources)
        self.assertEqual(len(alerts), 2)
        self.assertEqual({alert["url"] for alert in alerts},
                         {"https://prod.example/f", "https://staging.example/f"})
        self.assertEqual({alert["environment"] for alert in alerts},
                         {"production", "staging"})
        self.assertEqual(len({exploitation.alert_key(alert) for alert in alerts}), 2)

    def test_a_read_source_alerts_only_after_the_delay_and_clears_on_success(self):
        payload = page()
        error = fingerprints.FingerprintError("fingerprints source nexlink: HTTP 503")
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page",
                                  side_effect=[payload, error, payload]):
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=1000.0)
            with self.assertRaises(fingerprints.FingerprintError):
                fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                                  now=1061.0)
            self.assertEqual(self.source_alerts(1100.0), [])
            late = self.source_alerts(1000.0 + fingerprints.FAILING_ALERT_S)
            self.assertEqual([a["since"] for a in late], [1000.0])
            self.assertFalse(late[0]["auth_refused"])
            fingerprints.tick(self.cfg, self.db, token_file=self.token_file(),
                              actor="mesh-design", now=3000.0)
        self.assertEqual(self.source_alerts(9000.0), [])

    def test_the_notify_cli_refuses_fingerprints_outside_role_run(self):
        proc = self.mesh("notify", "--once", "--only", "fingerprints",
                         env=self.env(AMEESH_ROLE=""))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("fingerprints pass refused", proc.stderr)

    def test_the_notifier_refuses_fingerprints_outside_role_run(self):
        fake = types.SimpleNamespace(cfg=self.cfg, log=lambda _text: None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMEESH_ROLE", None)
            with self.assertRaisesRegex(fingerprints.FingerprintError, "role"):
                notify.Notifier._fingerprints(fake, self.db, 0.0)
        with mock.patch.dict(os.environ, {"AMEESH_ROLE": "mesh-design"}), \
                mock.patch.object(fingerprints, "tick", return_value=[]):
            self.assertEqual(notify.Notifier._fingerprints(fake, self.db, 0.0), None)


class MigrationRepositoryTest(PgTestCase):
    """#3885: a lot whose mirror ticket lives in another repository reserves
    its migration in the repository where the code lives, explicitly."""

    def test_repository_override_reserves_in_ameesh_not_in_the_issue_repo(self):
        repo = _depot(os.path.join(self.tmp, "ameesh"), "manaty/ameesh",
                      AMEESH.directory, ["0491_base.sql"])
        lot = work.add(self.db, title="lot ameesh, miroir nexlink",
                       issue_ref="manaty/nexlink#2401", app="ameesh",
                       actor="test")
        # An exact number reads the target only (#2620); the fixture branch
        # is `develop`. 493 is free there and in the source package.
        item = work.reserve_migration(self.db, lot["id"], 493, repo=repo,
                                      repository="manaty/ameesh",
                                      target="develop", actor="test")
        self.assertEqual(item["migration"], 493)
        self.assertEqual(item["migration_repo"], "manaty/ameesh")
        self.assertEqual(item["migrations"][0]["repo"], "manaty/ameesh")
        self.assertIn("src/ameesh/migrations/0493_", item["migration_file"])


class RoleTimerTest(unittest.TestCase):
    def plan(self, **changes):
        values = dict(host="agents-a", role="mesh-design",
                      row={"name": "mesh-design", "releve": "election",
                           "host": "agents-a", "candidats": ["agents-a"]},
                      settings=types.SimpleNamespace(projects={}, interval=900),
                      watch_intervals=(), notify_humans=["human:owner"])
        values.update(changes)
        return role_timers.plan(**values)

    def test_a_declared_source_installs_the_fingerprints_timer(self):
        report = self.plan(fingerprint_intervals=[120])
        by_name = {item["name"]: item for item in report["timers"]}
        self.assertIn("fingerprints", by_name)
        self.assertEqual(by_name["fingerprints"]["only"], "fingerprints")
        self.assertEqual(by_name["fingerprints"]["interval_s"], 90)
        self.assertFalse(by_name["fingerprints"].get("blocked"))

    def test_no_declaration_installs_no_fingerprints_timer(self):
        report = self.plan()
        self.assertNotIn("fingerprints", {item["name"] for item in report["timers"]})

    def test_install_writes_the_fingerprints_unit(self):
        report = self.plan(fingerprint_intervals=[60])
        with tempfile.TemporaryDirectory() as tmp:
            role_timers.install(report, directory=Path(tmp), apply=False)
            service = Path(tmp, "ameesh-role-fingerprints.service").read_text()
            timer = Path(tmp, "ameesh-role-fingerprints.timer").read_text()
        self.assertIn("--only fingerprints", service)
        self.assertIn("OnUnitActiveSec=60s", timer)

    def test_an_unreadable_declaration_is_reported_blocked(self):
        report = self.plan(fingerprint_error="canon unreadable: boom")
        by_name = {item["name"]: item for item in report["timers"]}
        self.assertEqual(by_name["fingerprints"]["blocked"],
                         "canon unreadable: boom")


if __name__ == "__main__":
    unittest.main()
