# SPDX-License-Identifier: AGPL-3.0-only
"""#7443 : les courriers sans geste dû partent en passif, et le journal le dit.

Le constat de l'auditeur (#220082, point 4) : environ 23 $ de tours réveillés
sans geste le 14/11, dont des rappels SLO « acknowledge, do nothing » et des
avis de CI d'un lot que le destinataire ne tient pas. Le lot étend #7308 :

* un lot validé qui attend son véhicule de fusion (paquet, approbation,
  résultat de fusion) n'appelle aucun geste au niveau 1 : `info` ;
* une correction due (`blocked_freeze`) ou un créneau manqué garde le réveil ;
* le vert d'un paquet réveille le pilote qui porte la fusion ; le porteur du
  lot reçoit une copie passive ;
* `ci.describe` et le journal du notifieur nomment le mode et la raison.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, ci, packet_automerge, packet_watch, work

SHA = "1" * 40


class SloNoActionTest(unittest.TestCase):
    BY_NAME = {"pilot": {"name": "pilot", "team": "ameesh", "lease_live": True}}

    def _alert(self, **delta):
        alert = dict(type="slo_breach", level=1, lot=123, lot_state="validated",
                     step="verdict_merge", agent="dev1",
                     waiting={"what": "merge", "who": "human:smichea"})
        alert.update(delta)
        return alert

    def test_validated_lot_waiting_for_its_vehicle_is_passive(self):
        modes = alert_briefs.quiet_modes(self._alert(), ["pilot"], self.BY_NAME)
        self.assertEqual(modes, {"pilot": {
            "payload": {"info": True},
            "reason": "validated lot waiting for its merge vehicle: no action due"}})

    def test_a_named_third_party_keeps_no_wake(self):
        for waiting in ({"what": "merge-approval", "who": "human:smichea"},
                        {"what": "merge-outcome", "who": "human:smichea"},
                        {"what": "merge", "who": "an-executor"}):
            with self.subTest(waiting=waiting):
                modes = alert_briefs.quiet_modes(
                    self._alert(waiting=waiting), ["pilot"], self.BY_NAME)
                self.assertEqual(modes["pilot"]["payload"], {"info": True})

    def test_the_named_third_party_who_receives_it_keeps_the_wake(self):
        """The reminder names the recipient as the one who merges: not a third
        party to them, so the wake stays."""
        alert = self._alert(waiting={"what": "merge", "who": "pilot"})
        self.assertEqual(alert_briefs.quiet_modes(alert, ["pilot"], self.BY_NAME), {})

    def test_correction_due_keeps_the_wake(self):
        alert = self._alert(step="blocked_freeze", waiting=None)
        self.assertEqual(alert_briefs.quiet_modes(alert, ["pilot"], self.BY_NAME), {})
        self.assertEqual(alert_briefs.wake_reason(alert, "pilot"),
                         "correction owed after a review (SLO level 1)")

    def test_actionable_waits_keep_the_wake(self):
        for delta in ({"waiting": {"what": "merge", "candidate": {"owner": "author"}}},
                      {"waiting": {"what": "packet", "packet_missed": {"slot": 1}}},
                      {"waiting": {"what": "packet", "canon": True}},
                      {"waiting": {"what": "packet", "self": True}},
                      {"waiting": {"what": "packet", "ci": {"state": "red"}}},
                      {"vehicle_exit_ts": 1},
                      {"level": 2},
                      {"urgent": True},
                      {"critical": True}):
            with self.subTest(delta=delta):
                self.assertEqual(
                    alert_briefs.quiet_modes(self._alert(**delta), ["pilot"], self.BY_NAME),
                    {})

    def test_an_escalated_fallback_still_wakes(self):
        briefs = {"auditor": "Escalated from level 1 to level 2. SLO breach"}
        self.assertEqual(alert_briefs.quiet_modes(self._alert(), briefs, {}), {})

    def test_copy_mode_names_the_woken_recipient(self):
        alert = dict(type="slo_breach", level=2, agent="dev1", lot=42)
        modes = alert_briefs.quiet_modes(alert, ["dev1", "pilot"], self.BY_NAME)
        self.assertEqual(modes["pilot"], {"payload": {"cc": "dev1"},
                                          "reason": "copy: dev1 wakes for this SLO"})


class CiVerdictPilotTest(unittest.TestCase):
    """#7443 : le vert d'un paquet réveille le pilote, pas le composeur."""

    def _watch(self, publication=None):
        watcher = ci.Watcher(None)
        watcher.log = lambda *_a, **_k: None
        lot = {"id": 42, "branch": "integration/paquet-h-test",
               "assignee": "composer", "app": "ameesh"}
        row = dict(work_item_id=42, run_id=7, head_sha=SHA,
                   branch="integration/paquet-h-test")
        st = SimpleNamespace(ci=mock.Mock())
        st.ci.failures.return_value = []
        published = publication or {"armed": False, "requested": False}
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(work, "get", return_value=lot), \
                mock.patch.object(ci, "recipient_of", return_value="composer"), \
                mock.patch.object(watcher, "_attribution", return_value=None), \
                mock.patch.object(watcher, "_merge_pilot", return_value="pilot"), \
                mock.patch.object(ci, "verdict_text", return_value="verdict"), \
                mock.patch.object(packet_automerge, "publication", return_value=published), \
                mock.patch.object(ci, "_send_posed", return_value=11) as wake, \
                mock.patch.object(ci, "_send", return_value=12) as copy:
            out = watcher._verdict(None, "org/repo", row, {}, "success")
        return wake, copy, out

    def test_green_packet_wakes_the_pilot_and_informs_the_holder(self):
        wake, copy, out = self._watch()
        wake.assert_called_once()
        self.assertEqual(wake.call_args.args[1], "pilot")
        self.assertNotIn("info", wake.call_args.kwargs["payload"])
        copy.assert_called_once()
        self.assertEqual(copy.call_args.args[1], "composer")
        self.assertIs(copy.call_args.kwargs["payload"]["info"], True)
        self.assertEqual([(r["to"], r["mode"]) for r in out],
                         [("pilot", "wake"), ("composer", "info")])
        self.assertEqual(out[0]["reason"], "green packet CI: the pilot carries the merge")
        self.assertEqual(out[1]["reason"], "copy: the pilot carries the packet merge")

    def test_armed_green_packet_wakes_nobody(self):
        wake, _copy, out = self._watch({"armed": True, "requested": True})
        self.assertIs(wake.call_args.kwargs["payload"]["info"], True)
        self.assertEqual(out[0]["mode"], "info")
        self.assertIn("no action due", out[0]["reason"])

    def test_red_packet_stays_with_the_holder(self):
        # Le verdict rouge ne ramène pas la fusion au pilote : le porteur agit.
        watcher = ci.Watcher(None)
        watcher.log = lambda *_a, **_k: None
        lot = {"id": 42, "branch": "integration/paquet-h-test",
               "assignee": "composer", "app": "ameesh"}
        row = dict(work_item_id=42, run_id=7, head_sha=SHA,
                   branch="integration/paquet-h-test")
        st = SimpleNamespace(ci=mock.Mock())
        st.ci.failures.return_value = []
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(work, "get", return_value=lot), \
                mock.patch.object(ci, "recipient_of", return_value="composer"), \
                mock.patch.object(watcher, "_attribution", return_value=None), \
                mock.patch.object(watcher, "_merge_pilot", return_value="pilot"), \
                mock.patch.object(ci, "verdict_text", return_value="verdict"), \
                mock.patch.object(ci, "_send", return_value=11) as wake, \
                mock.patch.object(ci, "_send_posed", return_value=12) as copy:
            out = watcher._verdict(None, "org/repo", row, {}, "failure")
        wake.assert_not_called()
        copy.assert_called_once()
        self.assertEqual(out[0]["mode"], "wake")

    def test_ordinary_lot_keeps_a_single_wake(self):
        watcher = ci.Watcher(None)
        watcher.log = lambda *_a, **_k: None
        lot = {"id": 43, "branch": "lot/43-x", "assignee": "dev-a", "app": "ameesh"}
        row = dict(work_item_id=43, run_id=8, head_sha=SHA, branch="lot/43-x")
        st = SimpleNamespace(ci=mock.Mock())
        st.ci.failures.return_value = []
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(work, "get", return_value=lot), \
                mock.patch.object(ci, "recipient_of", return_value="dev-a"), \
                mock.patch.object(watcher, "_attribution", return_value=None), \
                mock.patch.object(ci, "verdict_text", return_value="verdict"), \
                mock.patch.object(ci, "_send", return_value=11) as wake, \
                mock.patch.object(ci, "_send_posed", return_value=12) as send:
            out = watcher._verdict(None, "org/repo", row, {}, "success")
        wake.assert_not_called()
        send.assert_called_once()
        self.assertEqual([(r["to"], r["mode"]) for r in out], [("dev-a", "wake")])


class MergePilotTest(unittest.TestCase):
    """#7443 : le pilote d'un paquet se lit au projet du lot, sinon au dépôt."""

    def _watcher(self, cfg=None):
        watcher = ci.Watcher(cfg)
        watcher.log = lambda *_a, **_k: None
        return watcher

    def test_the_lot_project_names_the_pilot(self):
        st = SimpleNamespace(operations=SimpleNamespace(listing=lambda: []))
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(alert_briefs, "project_pilots",
                                  return_value=["pilot"]) as pilots, \
                mock.patch.object(ci.Watcher, "_project_pilot") as repo_pilot:
            got = self._watcher()._merge_pilot(None, "org/repo", {"app": "ameesh"})
        self.assertEqual(got, "pilot")
        repo_pilot.assert_not_called()
        self.assertEqual(pilots.call_args.args[0], "ameesh")

    def test_an_unreadable_project_falls_back_to_the_repository(self):
        cfg = SimpleNamespace(canon=object())
        st = SimpleNamespace(operations=SimpleNamespace(listing=lambda: []))
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(alert_briefs, "project_pilots",
                                  side_effect=RuntimeError("canon illisible")), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(ci.Watcher, "_project_pilot",
                                  return_value="fallback") as repo_pilot:
            got = self._watcher(cfg)._merge_pilot(None, "org/repo", {"app": "ameesh"})
        self.assertEqual(got, "fallback")
        repo_pilot.assert_called_once()

    def test_no_declared_project_pilot_falls_back_to_the_repository(self):
        cfg = SimpleNamespace(canon=object())
        st = SimpleNamespace(operations=SimpleNamespace(listing=lambda: []))
        with mock.patch.object(ci.storage, "of", return_value=st), \
                mock.patch.object(alert_briefs, "project_pilots", return_value=[]), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(ci.Watcher, "_project_pilot", return_value=None):
            got = self._watcher(cfg)._merge_pilot(None, "org/repo", {"app": "ameesh"})
        self.assertIsNone(got)


class HeldAfterGreenTest(unittest.TestCase):
    """#7443 : le rappel « auto-merge held » ne réveille que le pilote."""

    def _tick(self, notify=("composer", "pilot"), pilots=None):
        pilots = {"pilot"} if pilots is None else pilots
        armed = dict(id=1, state="armed", sha=SHA, repo="org/repo", pr=7)
        held = dict(id=2, state="held", sha=SHA, why="macOS queued")
        runs = [dict(run_id=3, workflow="linux", head_sha=SHA,
                     status="success", concluded_ts=100)]
        watch = packet_watch.ProjectWatch("ameesh", tuple(notify), grace_s=900)
        st = SimpleNamespace(ci=SimpleNamespace(for_lot=lambda *a: runs))
        send = mock.Mock()
        with mock.patch.object(packet_automerge, "open_carriers", return_value=[1]), \
                mock.patch.object(packet_automerge, "marks", return_value=[armed, held]), \
                mock.patch.object(packet_automerge, "active", return_value=armed), \
                mock.patch.object(packet_watch.storage, "of", return_value=st), \
                mock.patch.object(packet_watch, "_pilot_names", return_value=set(pilots)), \
                mock.patch.object(packet_watch.mail, "send", send):
            out = packet_watch.held_after_green(None, watch, {}, now=2000, lots=[1])
        return send, out

    def test_only_the_pilot_wakes(self):
        send, out = self._tick()
        self.assertEqual(send.call_count, 2)
        by_to = {call.args[2]: call.kwargs["payload"] for call in send.call_args_list}
        self.assertNotIn("info", by_to["pilot"])
        self.assertIs(by_to["composer"]["info"], True)
        self.assertEqual(out[0]["notify"], {"composer": "info", "pilot": "wake"})

    def test_without_a_readable_pilot_the_informed_recipient_wakes(self):
        send, out = self._tick(pilots=())
        by_to = {call.args[2]: call.kwargs["payload"] for call in send.call_args_list}
        self.assertIs(by_to["composer"]["info"], True)
        self.assertNotIn("info", by_to["pilot"])
        self.assertEqual(out[0]["notify"], {"composer": "info", "pilot": "wake"})

    def test_a_single_recipient_still_wakes(self):
        send, out = self._tick(notify=("pilot",))
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("info", send.call_args.kwargs["payload"])
        self.assertEqual(out[0]["notify"], {"pilot": "wake"})


class DescribeTest(unittest.TestCase):
    def test_the_journal_names_mode_and_reason(self):
        line = ci.describe({"action": "verdict", "repo": "org/repo", "run_id": 7,
                            "status": "success", "to": "pilot", "mode": "wake",
                            "reason": "green packet CI: the pilot carries the merge"})
        self.assertIn("→ pilot [wake: green packet CI: the pilot carries the merge]",
                      line)
        line = ci.describe({"action": "failures", "repo": "org/repo", "run_id": 7,
                            "tests": ["a"], "to": "dev-a", "mode": "info",
                            "reason": "failure possibly inherited: no base reference"})
        self.assertIn("[info: failure possibly inherited: no base reference]", line)


if __name__ == "__main__":
    unittest.main()
