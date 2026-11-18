# SPDX-License-Identifier: AGPL-3.0-only
import unittest

from ameesh.pre_act_audit import correlate


ID = "1d67c20e-77aa-4f81-88bb-08b3b11444bc"
OTHER = "2d67c20e-77aa-4f81-88bb-08b3b11444bc"


def receipt(value=ID, kind="prod"):
    return {"type": "pre-act/1", "kind": kind, "pregeste_id": value}


def run(number, title):
    return {"id": number, "display_title": title}


class CorrelationTest(unittest.TestCase):
    def test_exact_match_and_reuse(self):
        runs = [run(1, "Deploy pregeste=" + ID), run(2, "Deploy pregeste=" + ID)]
        self.assertEqual(correlate(runs, [receipt()]),
                         [{"run_id": 2, "reason": "already-consumed-id"}])

    def test_missing_invalid_unknown_and_ambiguous(self):
        runs = [run(1, "Deploy"), run(2, "pregeste="),
                run(3, "pregeste=" + OTHER), run(4, "pregeste=" + ID),
                run(5, "pregeste=" + ID + " pregeste=" + OTHER)]
        self.assertEqual([x["reason"] for x in correlate(runs, [receipt(), receipt()])],
                         ["missing-or-invalid-id", "missing-or-invalid-id",
                          "unknown-or-ambiguous-id", "unknown-or-ambiguous-id",
                          "missing-or-invalid-id"])

    def test_non_prod_receipt_and_nonexact_marker_do_not_match(self):
        runs = [run(1, "pregeste=" + ID), run(2, "xpregeste=" + ID)]
        self.assertEqual([x["reason"] for x in correlate(runs, [receipt(kind="kms")])],
                         ["unknown-or-ambiguous-id", "missing-or-invalid-id"])

    def test_duplicate_api_page_does_not_consume_twice(self):
        self.assertEqual(correlate([run(1, "pregeste=" + ID)] * 2, [receipt()]), [])


class CollectionTest(unittest.TestCase):
    def test_modes_never_silently_skip_unknown_input(self):
        from ameesh.pre_act_audit import production_run
        for title, expected in (("environment=production observe_only=false", True),
                                ("environment=staging observe_only=false", False),
                                ("environment=production observe_only=true", False),
                                ("environment=production", None),
                                ("environment=x observe_only=false", None),
                                ("environment=production environment=staging observe_only=false", None)):
            with self.subTest(title=title):
                self.assertIs(production_run(run(1, title)), expected)

    def test_pagination_and_stale_or_truncated_responses(self):
        from types import SimpleNamespace as NS
        from ameesh.pre_act_audit import collect
        from unittest.mock import Mock
        api = Mock(last_read=None)
        api.get.side_effect = [dict(total_count=101, workflow_runs=[run(n, "") for n in range(100)]),
                               dict(total_count=101, workflow_runs=[run(100, "")])]
        self.assertEqual(len(collect(api, "acme/app", "deploy.yml", 100, 200)), 101)
        self.assertIn("page=2", api.get.call_args.args[0])
        api.get.side_effect = None
        api.get.return_value = dict(total_count=2, workflow_runs=[])
        with self.assertRaisesRegex(Exception, "truncated"):
            collect(api, "acme/app", "deploy.yml", 100, 200)
        api.last_read = NS(stale=True)
        with self.assertRaisesRegex(Exception, "stale"):
            collect(api, "acme/app", "deploy.yml", 100, 200)

    def test_github_result_limit_splits_window_without_gaps(self):
        from ameesh.pre_act_audit import collect
        from unittest.mock import Mock
        api = Mock(last_read=None)
        api.get.side_effect = [dict(total_count=1001, workflow_runs=[]),
                               dict(total_count=1, workflow_runs=[run(1, "")]),
                               dict(total_count=1, workflow_runs=[run(2, "")])]
        self.assertEqual([r["id"] for r in collect(api, "acme/app", "deploy.yml", 100, 101)], [1, 2])
        from urllib.parse import parse_qs, urlsplit
        windows = [parse_qs(urlsplit(call.args[0]).query)["created"][0]
                   for call in api.get.call_args_list]
        self.assertEqual(windows[1], "1970-01-01T00:01:40Z..1970-01-01T00:01:40Z")
        self.assertEqual(windows[2], "1970-01-01T00:01:41Z..1970-01-01T00:01:41Z")


from .support import PgTestCase


class DurableAuditTest(PgTestCase):
    def setUp(self):
        super().setUp()
        from ameesh import registry, storage
        self.db.execute("TRUNCATE pre_act_audit_cursors, pre_act_audit_runs")
        registry.upsert(self.db, "auditor", harness="codex", chantier="app")
        self.st = storage.of(self.db).github_watch
        self.sub = type("Sub", (), dict(project="app", repo="acme/app", interval=60))()
        self.st.audit_activate("app", "acme/app", "deploy.yml", 100)
        item = self.db.query("INSERT INTO work_items (title) VALUES ('test') RETURNING id")[0]["id"]
        import json
        self.db.execute("INSERT INTO work_item_events (work_item_id, state, evidence)"
                        " VALUES (%s,'build',%s::jsonb)", (item, json.dumps(receipt())))

    def messages(self):
        return self.db.query("SELECT body FROM agent_mailbox WHERE recipient='auditor'")

    def test_restart_reuse_fast_retries_and_transactional_alert(self):
        from ameesh import mail, pre_act_audit as audit
        from unittest import mock
        first = run(1, "pregeste=" + ID)
        self.assertEqual(audit.deliver(self.db, self.sub, "deploy.yml", [first], 200, "auditor"), 1)
        self.assertEqual(self.messages(), [])
        # A fresh storage instance / replay must keep consumption and activation.
        from ameesh import storage
        st = storage.of(self.db).github_watch
        self.assertEqual(float(st.audit_activate("app", "acme/app", "deploy.yml", 999)["activated_at"]), 100)
        self.assertEqual(audit.deliver(self.db, self.sub, "deploy.yml", [first], 300, "auditor"), 0)
        reused = run(2, "pregeste=" + ID)
        with mock.patch.object(mail, "send", side_effect=RuntimeError("mailbox unavailable")):
            with self.assertRaisesRegex(RuntimeError, "mailbox"):
                audit.deliver(self.db, self.sub, "deploy.yml", [first, reused], 400, "auditor")
        self.assertEqual(st.audit_known(self.sub.repo, [reused]), set())
        self.assertEqual(float(st.audit_activate("app", "acme/app", "deploy.yml", 999)["scanned_through"]), 300)
        audit.deliver(self.db, self.sub, "deploy.yml", [first, reused], 400, "auditor")
        self.assertIn("already-consumed-id", self.messages()[0]["body"])
        # Even unseen attempts 2 and 3 must both be audited on a fast rerun.
        audit.deliver(self.db, self.sub, "deploy.yml", [dict(first, run_attempt=3)], 500, "auditor")
        self.assertEqual(len(self.messages()), 3)
        audit.deliver(self.db, self.sub, "deploy.yml", [dict(first, run_attempt=3)], 600, "auditor")
        self.assertEqual(len(self.messages()), 3)

    def test_mixed_workflow_modes_and_missing_identifiers(self):
        from ameesh import pre_act_audit as audit
        runs = [run(1, "environment=staging observe_only=false"),
                run(2, "environment=production observe_only=true"),
                run(3, "environment=production observe_only=false"),
                run(4, "pregeste=" + ID)]
        audit.deliver(self.db, self.sub, "deploy.yml", runs, 200, "auditor", mode_marked=True)
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("missing-or-invalid-id", self.messages()[0]["body"])
        self.assertIn("missing-or-invalid-mode", self.messages()[1]["body"])
        self.assertEqual(self.st.audit_consumed([ID]), set())

    def test_tick_uses_canon_inventory_and_activation_not_history(self):
        from ameesh import pre_act_audit as audit, mechanics, canon
        from types import SimpleNamespace as NS
        from unittest import mock
        spec = NS(production_branch="main", production_workflows=("deploy.yml",),
                  production_mode_marked_workflows=())
        api = mock.Mock(last_read=None)
        api.get.return_value = dict(total_count=1, workflow_runs=[run(1, "pregeste=" + ID)])
        declarations = [NS(agents=[NS(title="auditor", roles=["auditeur"])])]
        with mock.patch.object(mechanics, "configured", return_value=NS(override=lambda _: spec)), \
                mock.patch.object(canon, "load_configured", return_value=declarations):
            # Before indexing settles there are no external reads.
            audit.tick(self.db, self.cfg, 101, [self.sub], api, lambda _: None)
            api.get.assert_not_called()
            audit.tick(self.db, self.cfg, 1000, [self.sub], api, lambda _: None)
            self.assertIn("1970-01-01T00%3A01%3A40Z", api.get.call_args.args[0])
            self.assertEqual(self.messages(), [])
            # Independent invocation still reads from activation for reruns.
            api.get.return_value = dict(total_count=1, workflow_runs=[dict(run(1, "pregeste=" + ID), run_attempt=2)])
            audit.tick(self.db, self.cfg, 2000, [self.sub], api, lambda _: None)
            self.assertEqual(len(self.messages()), 1)
