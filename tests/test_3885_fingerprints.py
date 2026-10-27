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

from ameesh import db as db_mod, fingerprints, migrations, notify, role_timers, storage, work

from .support import PgTestCase
from .test_l122_numeros_de_migration import AMEESH, _depot

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "fingerprints", "nexlink-page.json")


def page():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def declaration(url="https://nexlink.example/api/fingerprints", interval=60):
    return fingerprints.Declaration("nexlink", url, float(interval), "watch")


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
                     "Utilisateurs touchés : 0", "Un seul lot ouvert par empreinte"):
            self.assertIn(text, item["body"])

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


class TickTest(PgTestCase):
    def setUp(self):
        # These witnesses share the project name and read the cursor: start
        # from an empty state, never from the previous test's rows.
        self.db.execute("DELETE FROM fingerprint_watch_state")
        self.db.execute("DELETE FROM work_item_events")
        self.db.execute("DELETE FROM work_items")

    def token_file(self, content=TOKEN):
        path = os.path.join(self.tmp, "fingerprints.token")
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
        state = storage.of(self.db).fingerprints.state("nexlink")
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
        state = storage.of(self.db).fingerprints.state("nexlink")
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
        state = storage.of(self.db).fingerprints.state("nexlink")
        self.assertIsNotNone(state)
        self.assertIn("token", state["last_error"])
        self.assertIsNone(state["cursor"])

    def test_an_unknown_field_refuses_the_page_and_keeps_the_cursor(self):
        bad = page()
        bad["root_extra"] = True
        with mock.patch.object(fingerprints, "declarations_of_config",
                               return_value=([declaration()], "")), \
                mock.patch.object(fingerprints, "read_page", return_value=bad):
            with self.assertRaisesRegex(fingerprints.FingerprintError, "unknown field"):
                fingerprints.tick(self.cfg, self.db, token_file=self.token_file())
        self.assertEqual(self.db.query("SELECT id FROM work_items"), [])
        state = storage.of(self.db).fingerprints.state("nexlink")
        self.assertIn("unknown field", state["last_error"])
        self.assertIsNone(state["cursor"])

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
