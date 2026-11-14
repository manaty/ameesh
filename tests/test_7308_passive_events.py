# SPDX-License-Identifier: AGPL-3.0-only
"""#7308 : seuls les événements sans geste attendu deviennent passifs."""
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, ci, packet_automerge, packet_watch, work
from tests.test_3845_paquet_horaire import ph, FakeShell, ROOT, carrier, HEAD, MANIFEST, RESOLVED


class AlertTest(unittest.TestCase):
    def test_capacity_reports_and_ordinary_packet_wait_are_info(self):
        alerts = [{"type": t} for t in ("idle_capacity", "plan_underused")]
        alerts.append(dict(type="slo_breach", level=1, lot_state="validated",
                           step="verdict_merge", waiting={"what": "packet"}))
        for alert in alerts:
            with self.subTest(alert=alert):
                self.assertEqual(alert_briefs.quiet_payloads(alert, ["pilot"], {}),
                                 {"pilot": {"info": True}})
                self.assertEqual(alert_briefs.quiet_payloads(
                    dict(alert, urgent=True), ["pilot"], {}), {})
                self.assertEqual(alert_briefs.quiet_payloads(
                    dict(alert, critical=True), ["pilot"], {}), {})

    def test_fallback_to_a_higher_level_still_wakes(self):
        alert = dict(type="slo_breach", level=1, lot_state="validated",
                     step="verdict_merge", waiting={"what": "packet"})
        briefs = {"auditor": "Escalated from level 2 to level 3. SLO breach"}
        self.assertEqual(alert_briefs.quiet_payloads(alert, briefs, {}), {})

    def test_escalations_and_author_actions_still_wake(self):
        base = dict(type="slo_breach", level=1, lot_state="validated",
                    step="verdict_merge", waiting={"what": "packet"})
        for delta in ({"level": 2}, {"level": 3}, {"lot_state": "build"},
                      {"step": "blocked_freeze"},
                      {"waiting": {"what": "merge-approval"}},
                      {"waiting": {"what": "merge-outcome"}},
                      {"waiting": {"what": "merge", "who": "executor"}},
                      {"waiting": {"what": "packet", "candidate": {"owner": "author"}}},
                      {"waiting": {"what": "packet", "packet_missed": {"slot": 1}}}):
            with self.subTest(delta=delta):
                self.assertEqual(alert_briefs.quiet_payloads(dict(base, **delta), ["pilot"], {}), {})


class VerdictTest(unittest.TestCase):
    def verdict(self, status, published, branch="integration/paquet-h-test"):
        lot = {"id": 1, "branch": branch, "assignee": "pilot"}
        row = dict(work_item_id=1, run_id=2, head_sha=HEAD, branch=branch)
        st = SimpleNamespace(ci=mock.Mock())
        st.ci.failures.return_value = []
        watcher = ci.Watcher(None)
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(work, "get", return_value=lot), \
                mock.patch.object(ci, "recipient_of", return_value="pilot"), \
                mock.patch.object(watcher, "_attribution", return_value=None), \
                mock.patch.object(ci, "verdict_text", return_value="verdict"), \
                mock.patch.object(packet_automerge, "publication", return_value=published), \
                mock.patch.object(ci, "_send_posed", return_value=3) as send:
            watcher._verdict(None, "org/repo", row, {}, status)
            return send.call_args.kwargs["payload"]

    def test_only_published_armed_green_packet_is_info(self):
        armed = {"requested": True, "armed": True}
        self.assertTrue(self.verdict("success", armed)["info"])
        for status, pub, branch in (
                ("failure", armed, "integration/paquet-h-test"),
                ("success", {"requested": True, "armed": False}, "integration/paquet-h-test"),
                ("success", {"requested": False, "armed": True}, "integration/paquet-h-test"),
                ("success", armed, "lot/1-test")):
            self.assertNotIn("info", self.verdict(status, pub, branch))


class ComposerTest(unittest.TestCase):
    def test_clean_publication_is_info_but_refused_waiver_wakes(self):
        for refused, manifest in ((False, MANIFEST), (True, MANIFEST), (False, RESOLVED)):
            shell = FakeShell()
            shell.items[4000] = carrier()
            shell.refuse_waiver = refused
            ctx = ph.Context(repo=ROOT, target="main", project="ameesh", pilot="pilot",
                             actor="pilot", shell=shell, contract_publication="legacy")
            with mock.patch.object(ph, "manifest_at", return_value=manifest):
                ph.publish(ctx, shell.items[4000])
            messages = [argv for argv in shell.argvs if argv[:2] == ["agent-mail", "send"]]
            self.assertEqual("--info" in messages[-1], not refused and manifest == MANIFEST)

    def test_explicit_info_does_not_change_error_mail_default(self):
        with mock.patch.object(ph.auto, "run_command") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"sent"
            ph.auto.send_mail("pilot", 1, "summary", info=True)
            self.assertIn("--info", run.call_args.args[0])
            ph.auto.send_mail("pilot", 1, "error")
            self.assertNotIn("--info", run.call_args.args[0])


class HeldTest(unittest.TestCase):
    def setUp(self):
        self.armed = dict(id=1, state="armed", sha=HEAD, repo="org/repo", pr=7)
        self.held = dict(id=2, state="held", sha=HEAD, why="macOS queued")
        self.runs = [dict(run_id=3, workflow="linux", head_sha=HEAD,
                          status="success", concluded_ts=100)]
        self.watch = packet_watch.ProjectWatch("ameesh", ("pilot",), grace_s=900)
        self.state = {}

    def tick(self, now, send):
        st = SimpleNamespace(ci=SimpleNamespace(for_lot=lambda *a: self.runs))
        with mock.patch.object(packet_automerge, "open_carriers", return_value=[1]), \
                mock.patch.object(packet_automerge, "marks", return_value=[self.armed, self.held]), \
                mock.patch.object(packet_automerge, "active", return_value=self.armed), \
                mock.patch.object(packet_watch.storage, "of", return_value=st), \
                mock.patch.object(packet_watch.mail, "send", send):
            return packet_watch.held_after_green(None, self.watch, self.state, now=now, lots=[1])

    def test_delayed_single_wake_even_without_final_verdict(self):
        send = mock.Mock()
        self.assertEqual(self.tick(999, send), [])
        self.assertEqual(len(self.tick(1000, send)), 1)
        self.assertNotIn("info", send.call_args.kwargs["payload"])
        self.assertEqual(self.tick(2000, send), [])
        self.assertEqual(send.call_count, 1)

    def test_red_and_replaced_green_do_not_create_green_reminder(self):
        send = mock.Mock()
        self.runs.append(dict(self.runs[0], run_id=4, status="failure"))
        self.assertEqual(self.tick(2000, send), [])
        send.assert_not_called()

    def test_queued_platform_job_does_not_require_a_final_run_verdict(self):
        self.runs[0]["status"] = "running"
        self.runs[0]["concluded_ts"] = None
        view = dict(state="OPEN", head=HEAD, rollup=[
            dict(name="linux", status="COMPLETED", conclusion="SUCCESS"),
            dict(name="macOS", status="QUEUED", conclusion=None)])
        send = mock.Mock()
        with mock.patch.object(packet_automerge, "pr_status", return_value=view):
            self.assertEqual(self.tick(100, send), [])
            self.assertEqual(len(self.tick(1000, send)), 1)
        send.assert_called_once()

    def test_failed_delivery_is_retried(self):
        send = mock.Mock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            self.tick(2000, send)
        send.side_effect = None
        self.assertEqual(len(self.tick(2000, send)), 1)


if __name__ == "__main__":
    unittest.main()
