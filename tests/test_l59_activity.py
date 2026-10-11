# SPDX-License-Identifier: AGPL-3.0-only
"""L59: persona activity log against the current schema."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import activity, mail, registry, work

from .support import PgTestCase


class DurationTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(activity.parse_since("30m"), 1800)
        self.assertEqual(activity.parse_since("24h"), 86400)
        self.assertEqual(activity.parse_since("7d"), 7 * 86400)
        with self.assertRaises(ValueError):
            activity.parse_since("tomorrow")


class ActivityDatabaseTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "worker", harness="deepseek", host="pc", mode="execute")
        registry.upsert(self.db, "coord", harness="claude", host="pc", mode="execute")
        registry.upsert(self.db, "other", harness="claude", host="pc", mode="execute")
        self.db.execute("DELETE FROM turn_costs WHERE agent = %s", ("worker",))

    def test_merged_timeline(self):
        mail.send(self.db, "coord", "worker", "take the lot")
        mail.send(self.db, "worker", "coord", "done")
        mail.send(self.db, "other", "coord", "unrelated")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, model, session, usd, input_tokens, "
            "output_tokens) VALUES ('worker', 'deepseek', 't1', 'deepseek-flash', 's1', 0.25, "
            "100, 50)")
        item = work.add(self.db, title="refactor", assignee="worker", actor="coord")
        work.move(self.db, item["id"], "build", actor="worker")
        items = activity.events(self.db, "worker", 3600)
        kinds = [e["kind"] for e in items]
        self.assertIn("message_received", kinds)
        self.assertIn("message_sent", kinds)
        self.assertIn("turn", kinds)
        self.assertIn("lot", kinds)
        self.assertFalse(any("unrelated" in e["text"] for e in items))
        self.assertEqual([e["at"] for e in items], sorted(e["at"] for e in items))
        turn = [e for e in items if e["kind"] == "turn"][0]
        self.assertIn("$0.2500", turn["text"])

    def test_outside_period(self):
        mail.send(self.db, "coord", "worker", "old")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '3 days'")
        self.assertEqual(activity.events(self.db, "worker", 86400), [])
        self.assertEqual(len(activity.events(self.db, "worker", 4 * 86400)), 1)

    def test_command(self):
        mail.send(self.db, "coord", "worker", "hello")
        out = io.StringIO()
        with mock.patch("ameesh.config.load", return_value=self.cfg), \
                mock.patch("ameesh.db.connect", return_value=_KeepOpen(self.db)), \
                redirect_stdout(out):
            code = activity.main(["worker", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["present"]["name"], "worker")
        self.assertEqual(data["events"][0]["kind"], "message_received")
        with mock.patch("ameesh.config.load", return_value=self.cfg), \
                mock.patch("ameesh.db.connect", return_value=_KeepOpen(self.db)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(activity.main(["unknown"]), 2)

    def test_current_milestones_and_delegation_resolution(self):
        item = work.add(self.db, title="milestone test", assignee="worker", actor="coord")
        work.milestone(self.db, item["id"], "frozen", sha="a" * 40, actor="worker")
        self.db.execute(
            "INSERT INTO work_item_delegations (work_item_id, delegate, delegated_by, delegated_at, "
            "due_at, outcome, resolved_at) VALUES (%s, 'worker', 'coord', now() - interval '3 days', "
            "now() - interval '2 days', 'rendue', now())", (item["id"],))
        items = activity.events(self.db, "worker", 3600)
        self.assertTrue(any(e["kind"] == "milestone" and "frozen" in e["text"] for e in items))
        self.assertTrue(any(e["kind"] == "delegation_resolved" and "returned" in e["text"] for e in items))
        self.assertFalse(any(e["kind"] == "delegation" for e in items))
        self.db.execute("UPDATE work_item_delegations SET delegated_at = now()")
        self.assertTrue(any(e["kind"] == "delegation" for e in activity.events(self.db, "coord", 3600)))

    def test_voided_costs_are_excluded(self):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, usd, void_reason) "
            "VALUES ('worker', 'deepseek', 999, 'duplicate reading')")
        self.assertEqual(activity.events(self.db, "worker", 3600), [])

    def test_limit_returns_latest_events(self):
        identifiers = [mail.send(self.db, "coord", "worker", "message %s" % i) for i in range(4)]
        items = activity.events(self.db, "worker", 3600, limit=2)
        self.assertEqual([event["message"] for event in items], identifiers[-2:])


class OfflineActivityTest(unittest.TestCase):
    def test_recent_delegation_resolution_and_order(self):
        class FakeDatabase:
            def query(self, sql, params):
                if "FROM turn_costs" in sql:
                    return [dict(id=1, recorded_at=80, harness="codex", usd=0)]
                if "FROM work_item_delegations" in sql:
                    return [dict(id=1, work_item_id=9, delegate="worker", delegated_by="coord",
                                 delegated_at=10, due_at=20, resolved_at=90, outcome="rendue")]
                return []
        items = activity.events(FakeDatabase(), "worker", 50, now=100)
        self.assertEqual([event["kind"] for event in items], ["turn", "delegation_resolved"])
        self.assertIn("returned", items[-1]["text"])
        self.assertEqual(activity.events(FakeDatabase(), "worker", 50, now=100, limit=1), items[-1:])

    def test_invalid_arguments_do_not_connect(self):
        from contextlib import redirect_stderr
        for args in (["worker", "--limit", "0"], ["worker", "--limit", "-1"],
                     ["worker", "--since", "0h"], ["worker", "--since", "2j"],
                     ["worker", "--since", "9" * 400 + "h"]):
            with self.subTest(args=args), mock.patch("ameesh.db.connect") as connect, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(activity.main(args), 2)
                connect.assert_not_called()

    def test_render_reports_current_state(self):
        text = activity.render("worker", dict(status="idle", harness="codex", host="host",
                                             responsible="human:owner", lease_owner="runner",
                                             last_error="sample error"), [], "24h")
        self.assertIn("harness codex on host", text)
        self.assertIn("lease: runner", text)
        self.assertIn("last error: sample error", text)
        self.assertIn("0 event(s)", text)


class _KeepOpen:
    """Keep the test database open when the CLI closes its wrapper."""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
