# SPDX-License-Identifier: AGPL-3.0-only
"""L317c: automatic deep analysis of the auditor.

A bug lot of priority 1 opened by a human (or their chat) wakes the auditor
right away with an urgent `event` mail and writes the request on the lot; the
IT module (L317a) brings the same request when a release is withdrawn
(`deep_analysis.request`). The first report is a lot note starting with
"Deep analysis:"; its delay is measured (`work show`, `measure`) and the alert
`deep_analysis_overdue` rises after 30 min without one.
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

from ameesh import deep_analysis, exploitation, mail, notify, registry, work

from .support import PgTestCase

#: identity variables removed from the environment of in-process tests
_IDENTITY = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
             "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH", "AMEESH_CHAT", "AMEESH_CHAT_PID")


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k not in _IDENTITY}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("auditeur", "orch", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def bug(self, actor="human:alice", **kw):
        kw.setdefault("type", "bug")
        kw.setdefault("priority", 1)
        return work.add(self.db, title="Nexlink 0.2.9 does not start", actor=actor,
                        cfg=self.cfg, **kw)

    def auditor_mail(self):
        return [m for m in mail.unread(self.db, "auditeur")]


class TriggerTest(_Base):
    def test_human_p1_bug_wakes_the_auditor_and_writes_the_request(self):
        item = self.bug()
        self.assertEqual(item["deep_analysis"],
                         {"requested": True, "trigger": "bug_p1", "auditors": ["auditeur"]})
        msgs = self.auditor_mail()
        self.assertEqual(len(msgs), 1)
        self.assertTrue(mail.is_urgent(msgs[0]))
        self.assertEqual(msgs[0]["kind"], "event")
        self.assertEqual(mail.message_payload(msgs[0])["lot"], item["id"])
        self.assertEqual(mail.message_payload(msgs[0])["deep_analysis"], "bug_p1")

        body = msgs[0]["body"]
        self.assertIn("Deep analysis requested (L317c): priority-1 bug opened by "
                      "human:alice — lot #%d" % item["id"], body)
        for part in ("which lot delivered the defect", "which review let it pass",
                     "which gate was missing", "one follow-up lot per missing gate",
                     "within 30 min",
                     'ameesh work note %d "Deep analysis: …"' % item["id"]):
            self.assertIn(part, body)
        notes = [e["note"] for e in work.events(self.db, item["id"])]
        self.assertTrue(any(n.startswith("deep analysis requested (bug_p1): ") for n in notes),
                        notes)

    def test_chat_of_a_human_triggers_it(self):
        registry.upsert(self.db, "chat-alice", harness="claude", mode="externe")
        self.assertTrue(self.bug(actor="chat-alice")["deep_analysis"]["requested"])

    def test_known_human_by_bare_name(self):
        with mock.patch.object(work, "known_human", return_value="human:alice"):
            self.assertTrue(self.bug(actor="alice")["deep_analysis"]["requested"])

    def test_no_trigger_for_an_agent_a_lower_priority_or_an_evolution(self):
        # an agent (even an orchestrator recording a human's request)
        self.assertNotIn("deep_analysis", self.bug(actor="orch"))
        # a driven agent named chat-… is not a chat (mode execute)
        registry.upsert(self.db, "chat-bot", harness="claude")
        self.assertNotIn("deep_analysis", self.bug(actor="chat-bot"))
        self.assertNotIn("deep_analysis", self.bug(priority=2))
        self.assertNotIn("deep_analysis", self.bug(priority=None))
        self.assertNotIn("deep_analysis", self.bug(type="evolution"))
        self.assertNotIn("deep_analysis", self.bug(actor=""))
        self.assertNotIn("deep_analysis", self.bug(actor=work.UNKNOWN_ACTOR))
        self.assertEqual(self.auditor_mail(), [])

    def test_no_auditor_registered_still_writes_the_request(self):
        self.db.execute("DELETE FROM agent_registry WHERE name = 'auditeur'")
        item = self.bug()
        self.assertEqual(item["deep_analysis"]["auditors"], [])
        self.assertTrue(item["deep_analysis"]["requested"])
        self.assertIsNotNone(deep_analysis.status(self.db, item["id"]))

    def test_mail_failure_never_fails_the_lot_creation(self):
        with mock.patch.object(mail, "send", side_effect=RuntimeError("mailbox down")):
            item = self.bug()
        self.assertFalse(item["deep_analysis"]["requested"])
        self.assertIn("mailbox down", item["deep_analysis"]["error"])
        self.assertIsNotNone(work.get(self.db, item["id"]))

    def test_release_withdrawn_request_for_the_it_module(self):
        lot = work.add(self.db, title="Nexlink desktop 0.2.9 release", actor="orch")
        out = deep_analysis.request(self.db, lot["id"], "release_withdrawn", actor="human:alice",
                                    detail="0.2.9 pulled from latest.json", cfg=self.cfg)
        self.assertEqual(out["auditors"], ["auditeur"])
        body = self.auditor_mail()[0]["body"]
        self.assertIn("release withdrawn by human:alice (0.2.9 pulled from latest.json)", body)
        self.assertEqual(deep_analysis.status(self.db, lot["id"])["trigger"],
                         "release_withdrawn")
        with self.assertRaises(ValueError):
            deep_analysis.request(self.db, lot["id"], "tag_deleted", actor="orch")

    def test_same_trigger_not_repeated_while_waiting(self):
        item = self.bug()
        again = deep_analysis.request(self.db, item["id"], "bug_p1", actor="human:alice")
        self.assertFalse(again["requested"])
        self.assertIn("no report yet", again["reason"])
        self.assertEqual(len(self.auditor_mail()), 1)
        # past the deadline it is asked again
        later = deep_analysis.request(self.db, item["id"], "bug_p1", actor="human:alice",
                                      now=time.time() + deep_analysis.DEADLINE_S + 60)
        self.assertTrue(later["requested"])
        self.assertEqual(len(self.auditor_mail()), 2)


class MeasureTest(_Base):
    def test_report_delay_measured(self):
        item = self.bug()
        found = deep_analysis.status(self.db, item["id"])
        self.assertIsNone(found["report_ts"])
        self.assertFalse(found["overdue"])
        # an unrelated note, and ameesh's own lines, are not the report
        work.note(self.db, item["id"], "looking at it", actor="auditeur")
        self.assertIsNone(deep_analysis.status(self.db, item["id"])["report_ts"])
        work.note(self.db, item["id"], "Deep analysis: delivered by #281 (abc123, PR #309); "
                  "review ok by codex; missing gate: real run of the binary", actor="auditeur")
        found = deep_analysis.status(self.db, item["id"])
        self.assertEqual(found["reporter"], "auditeur")
        self.assertIsNotNone(found["report_ts"])
        self.assertLess(found["delay_s"], deep_analysis.DEADLINE_S)
        self.assertFalse(found["overdue"])
        late = time.time() + 2 * deep_analysis.DEADLINE_S
        self.assertFalse(deep_analysis.status(self.db, item["id"], now=late)["overdue"])
        [measured] = deep_analysis.measure(self.db, since_ts=time.time() - 3600)
        self.assertEqual(measured["lot"], item["id"])
        self.assertEqual(measured["reporter"], "auditeur")

    def test_lot_without_request(self):
        lot = work.add(self.db, title="plain", actor="orch")
        self.assertIsNone(deep_analysis.status(self.db, lot["id"]))
        self.assertEqual(deep_analysis.measure(self.db, since_ts=0), [])

    def test_overdue_alert(self):
        item = self.bug()
        now = time.time()
        self.assertEqual(exploitation._deep_analysis_overdue(self.db, now), [])
        late = now + deep_analysis.DEADLINE_S + 120
        [alert] = exploitation._deep_analysis_overdue(self.db, late)
        self.assertEqual(alert["type"], "deep_analysis_overdue")
        self.assertEqual(alert["lot"], item["id"])
        self.assertEqual(alert["reason"], "bug_p1")
        self.assertEqual(alert["threshold"], int(deep_analysis.DEADLINE_S))
        self.assertIn("no report on the lot yet (deadline 30 min)", alert["detail"])
        # one alert per request: the same key while it lasts
        [again] = exploitation._deep_analysis_overdue(self.db, late + 600)
        self.assertEqual(exploitation.alert_key(alert), exploitation.alert_key(again))
        # the report clears it
        work.note(self.db, item["id"], "deep analysis: first findings", actor="auditeur")
        self.assertEqual(exploitation._deep_analysis_overdue(self.db, late), [])
        # older than the 24 h window: no longer read
        self.assertEqual(exploitation._deep_analysis_overdue(
            self.db, now + deep_analysis.OVERDUE_WINDOW_S + 60), [])

    def test_alert_type_declared_and_pushed(self):
        self.assertIn("deep_analysis_overdue", exploitation.ALERT_TYPES)
        self.assertIn("deep_analysis_overdue", notify.TYPE_LABELS)
        self.assertIn("deep_analysis_overdue", notify.DEFAULT_TYPES)


class CliTest(_Base):
    def test_work_add_and_show(self):
        proc = self.mesh("work", "add", "--title", "Nexlink 0.2.9 does not start",
                         "--type", "bug", "--priority", "1", "--actor", "human:alice")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("analysis : deep analysis requested from auditeur, first report due "
                      "within 30 min", proc.stdout)
        lot = work.list_items(self.db)[0]["id"]
        proc = self.mesh("work", "show", str(lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("analysis : deep analysis (bug_p1) requested", proc.stdout)
        self.assertIn("no report yet, waiting for", proc.stdout)
        work.note(self.db, lot, "Deep analysis: chain in progress", actor="auditeur")
        proc = self.mesh("work", "show", str(lot))
        self.assertIn("first report after", proc.stdout)
        self.assertIn("by auditeur", proc.stdout)
        data = json.loads(self.mesh("work", "show", str(lot), "--json").stdout)
        self.assertEqual(data["deep_analysis"]["reporter"], "auditeur")

    def test_work_add_by_an_agent_says_nothing(self):
        proc = self.mesh("work", "add", "--title", "flaky test", "--type", "bug",
                         "--priority", "1", "--actor", "orch", "--estimate", "1h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("analysis", proc.stdout)
