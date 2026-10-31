# SPDX-License-Identifier: AGPL-3.0-only
"""#3383: the owner channel follows the elected lease, not one host.

The alert delivery (owner pushes, decision requests, agent briefs) stayed in
the full `ameesh-notify` service, whose routes lived on `pc-smichea` only:
the post off meant no decision pushed (three times on 2026-10-24).
EXPLOITATION.md already listed `role run mesh-design -- notify --once --only
alerts` among the role timers; `role timers` did not install it.

Witnesses: a candidate with a route installs the `alerts` timer and its full
service stops delivering (no second state, no lock contention); a candidate
without a route is refused, loudly; the holder's restricted pass pushes a
pending decision on its own route, whatever the post does.
"""
from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import notify, role_timers


def settings():
    return SimpleNamespace(projects={"ameesh": "manaty/ameesh"}, interval=900)


def row():
    return {"name": "mesh-design", "releve": "election", "host": "agents-a",
            "candidats": ["agents-a", "agents-b", "poste"]}


def plan(**kw):
    return role_timers.plan(host="agents-a", role="mesh-design", row=row(),
                            settings=settings(), watch_intervals=[900], **kw)


NTFY = {"routes": {"human:owner": ["ntfy"]},
        "channels": {"ntfy": {"url": "https://ntfy.example", "topic": "t"}}}


class NotifyReachTest(unittest.TestCase):
    """The owner must be reachable from ANY holder (review cx2, b199db53de2c)."""

    def reach(self, raw, *, responsible="human:owner", env=None):
        return role_timers.notify_reach(SimpleNamespace(notify=raw),
                                        responsible=responsible,
                                        env={} if env is None else env)

    def test_a_push_route_reaches_the_role_responsible(self):
        self.assertEqual(self.reach(NTFY), (["human:owner"], ""))

    def test_default_human_is_required_too(self):
        raw = dict(NTFY, default_human="human:other")
        humans, error = self.reach(raw)
        self.assertEqual(humans, [])
        self.assertIn("human:other has no route", error)

    def test_no_key_names_the_responsible(self):
        self.assertEqual(self.reach(None), ([], "human:owner has no route on this host"))

    def test_without_any_required_human_nothing_is_proven(self):
        humans, error = self.reach(NTFY, responsible=None)
        self.assertEqual(humans, [])
        self.assertIn("no human to reach", error)

    def test_an_empty_owner_route_is_not_saved_by_the_default(self):
        # B/P1 cx2: routes {owner: []} + default [ntfy] reached nobody yet
        # passed the guard as "notify.default"
        raw = {"routes": {"human:owner": []}, "default": ["ntfy"],
               "channels": NTFY["channels"]}
        humans, error = self.reach(raw)
        self.assertEqual(humans, [])
        self.assertIn("human:owner has no route", error)

    def test_another_human_route_does_not_cover_the_owner(self):
        raw = {"routes": {"human:other": ["ntfy"]}, "channels": NTFY["channels"]}
        self.assertEqual(self.reach(raw)[0], [])

    def test_desktop_and_app_only_do_not_count(self):
        for names in (["desktop"], ["app"], ["desktop", "app"]):
            with self.subTest(names=names):
                humans, error = self.reach({"routes": {"human:owner": names}})
                self.assertEqual(humans, [])
                self.assertIn("is not a remote push", error)

    def test_a_declared_ntfy_token_must_resolve_and_is_never_shown(self):
        raw = {"routes": {"human:owner": ["ntfy"]},
               "channels": {"ntfy": {"url": "https://ntfy.example", "topic": "t",
                                     "token_env": "AMEESH_NTFY_TOKEN"}}}
        humans, error = self.reach(raw)
        self.assertEqual(humans, [])
        self.assertIn("AMEESH_NTFY_TOKEN", error)
        self.assertEqual(self.reach(raw, env={"AMEESH_NTFY_TOKEN": "tk-secret"}),
                         (["human:owner"], ""))
        humans, error = self.reach(raw, env={"AMEESH_NTFY_TOKEN": "tk-secret # note"})
        self.assertEqual(humans, [])
        self.assertNotIn("tk-secret", error)

    def test_slack_needs_its_webhook(self):
        raw = {"routes": {"human:owner": ["slack"]}}
        self.assertIn("AMEESH_SLACK_WEBHOOK", self.reach(raw)[1])
        self.assertEqual(self.reach(raw, env={
            "AMEESH_SLACK_WEBHOOK": "https://hooks.slack.example/T/B/x"})[0], ["human:owner"])

    def test_one_usable_channel_is_enough_beside_desktop(self):
        raw = dict(NTFY, routes={"human:owner": ["desktop", "ntfy"]})
        self.assertEqual(self.reach(raw), (["human:owner"], ""))

    def test_an_invalid_key_is_named(self):
        humans, error = self.reach({"routes": {"human:owner": ["ntfy"]}})
        self.assertEqual(humans, [])
        self.assertIn("invalid notify configuration", error)


class UnitEnvTest(unittest.TestCase):
    def test_notify_env_is_read_like_the_unit_does(self):
        with tempfile.TemporaryDirectory(prefix="3383-env-") as tmp:
            path = Path(tmp) / "notify.env"
            path.write_text("# secrets\n\nAMEESH_NTFY_TOKEN='tk'\nGH_TOKEN=gh\nbroken\n",
                            encoding="utf-8")
            env = role_timers.unit_env(str(path))
            self.assertEqual((env["AMEESH_NTFY_TOKEN"], env["GH_TOKEN"]), ("tk", "gh"))
            self.assertNotIn("broken", env)
            # the optional EnvironmentFile=-: a missing file is no error
            self.assertNotIn("AMEESH_NTFY_TOKEN_X",
                             role_timers.unit_env(str(Path(tmp) / "absent")))


class AlertsTimerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3383-timers-")
        self.dir = Path(self.tmp.name)
        (self.dir / "ameesh-notify.service").write_text("[Service]\n", encoding="utf-8")
        self.dropin = self.dir / "ameesh-notify.service.d" / "10-role-timers.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_candidate_with_a_route_runs_the_delivery_under_the_lease(self):
        report = plan(notify_humans=["human:owner"])
        alerts = next(item for item in report["timers"] if item["name"] == "alerts")
        self.assertEqual((alerts["interval_s"], alerts["blocked"]), (30, ""))
        self.assertIn("human:owner", alerts["detail"])
        role_timers.install(report, directory=self.dir, apply=False)
        service = (self.dir / "ameesh-role-alerts.service").read_text(encoding="utf-8")
        self.assertIn("role run mesh-design --pass alerts -- %s notify --once --only alerts"
                      % role_timers.DEFAULT_BIN, service)
        self.assertIn("EnvironmentFile=-%h/.config/ameesh/notify.env", service)
        timer = (self.dir / "ameesh-role-alerts.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=30s", timer)
        self.assertIn("--skip issues,github-watch,ci,alerts",
                      self.dropin.read_text(encoding="utf-8"))
        self.assertEqual(role_timers.check(report, directory=self.dir, apply=False), [])

    def test_a_candidate_without_a_route_is_refused_and_keeps_delivering(self):
        stale = self.dir / "ameesh-role-alerts.timer"
        stale.write_text("old", encoding="utf-8")
        report = plan(notify_humans=[], notify_error="human:owner has no route on this host")
        result = role_timers.install(report, directory=self.dir, apply=False)
        self.assertFalse(stale.exists())
        self.assertIn("ameesh-role-alerts.timer", result["removed"])
        # the service keeps the alerts pass: dropping it with no timer here
        # would silence this host's local delivery and briefs
        text = self.dropin.read_text(encoding="utf-8")
        self.assertIn("--skip issues,github-watch,ci\n", text)
        # #3402: named, but a missing secret no longer fails --check
        self.assertEqual(role_timers.check(report, directory=self.dir, apply=False), [])
        notes = role_timers.warnings(report)
        self.assertTrue(any(p.startswith("alerts: human:owner has no route")
                            and "notify key" in p for p in notes), notes)

    def test_a_route_added_later_updates_the_dropin_and_check_sees_the_old_one(self):
        role_timers.install(plan(notify_humans=[]), directory=self.dir, apply=False)
        routed = plan(notify_humans=["human:owner"])
        problems = role_timers.check(routed, directory=self.dir, apply=False)
        self.assertTrue(any("drop-in is out of date" in p for p in problems), problems)
        role_timers.install(routed, directory=self.dir, apply=False)
        self.assertEqual(role_timers.check(routed, directory=self.dir, apply=False), [])

    def test_main_reads_the_host_notify_key(self):
        cfg = SimpleNamespace(host="agents-a", notify=NTFY, config_path="", config_base="",
                              github={"projects": {"ameesh": "manaty/ameesh"}})
        role_row = dict(row(), responsible="human:owner")
        fake_db = mock.Mock()
        captured = {}

        def keep(report, **_kw):
            captured.update(report)
            return []

        with mock.patch.object(role_timers.config_mod, "load", return_value=cfg), \
                mock.patch.object(role_timers.db_mod, "connect", return_value=fake_db), \
                mock.patch.object(role_timers.registry, "get", return_value=role_row), \
                mock.patch.object(role_timers, "unit_env", return_value={}), \
                mock.patch.object(role_timers.plan_github, "settings_of",
                                  return_value=settings()), \
                mock.patch.object(role_timers, "_watch_declarations",
                                  return_value=([], "no github_watch declaration")), \
                mock.patch.object(role_timers, "check", side_effect=keep), \
                mock.patch("sys.stdout"):
            self.assertEqual(role_timers.main(["--check", "--no-apply"]), 0)
        alerts = next(item for item in captured["timers"] if item["name"] == "alerts")
        self.assertEqual((alerts["blocked"], alerts["detail"]), ("", "reaches human:owner"))


class ServiceBesideTimerTest(unittest.TestCase):
    """The full service and the alerts timer share a host and a state path."""

    def dropin_args(self):
        text = role_timers.render_notify_dropin("/opt/ameesh/bin/ameesh",
                                                ("issues", "github-watch", "ci", "alerts"))
        line = next(item for item in text.splitlines()
                    if item.startswith("ExecStart=") and "notify" in item)
        return shlex.split(line.split("=", 1)[1])[2:]

    def test_the_service_skipping_alerts_takes_no_lock_and_no_local_pass(self):
        args = notify.build_parser().parse_args(self.dropin_args())
        self.assertEqual(args.skip, ["issues,github-watch,ci,alerts"])
        fake = mock.Mock()
        fake.query.return_value = []  # compatible schema inventory
        fake.run_pass = mock.Mock(return_value=[])
        cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="3383-service-"))
        with mock.patch.object(notify, "Notifier", return_value=fake) as ctor, \
                mock.patch.object(notify, "_Lock", side_effect=AssertionError("lock")), \
                mock.patch.object(notify, "load_state", side_effect=AssertionError("state")), \
                mock.patch.object(notify.db_mod, "connect", return_value=fake), \
                mock.patch.object(notify.db_mod, "require_schema"), \
                mock.patch.object(notify.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(notify.cmd_notify(cfg, notify.NotifyConfig(), args), 0)
        self.assertEqual(ctor.call_args.kwargs["skip"],
                         frozenset({"issues", "github-watch", "ci", "alerts"}))
        fake.run_local_pass.assert_not_called()
        fake.run_pass.assert_called_once()

    def test_the_restricted_service_keeps_take_and_delivers_nothing(self):
        notifier = notify.Notifier(
            SimpleNamespace(host="agents-a", state_dir=tempfile.gettempdir()),
            notify.parse_config(NTFY), dry_run=True, take={"enabled": True},
            skip={"issues", "github-watch", "ci", "alerts"})
        with mock.patch.object(notify.exploitation, "alerts", return_value=[]), \
                mock.patch.object(notifier, "_auto_take") as take, \
                mock.patch.object(notify.notify_gap, "beat") as beat, \
                mock.patch.object(notifier, "_raise") as raised:
            notifier.run_pass(object())
        take.assert_called_once()
        beat.assert_not_called()  # only the holder's timer proves coverage
        raised.assert_not_called()

    def test_the_timer_takes_the_lock_the_service_left(self):
        cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="3383-lock-"))
        args = notify.build_parser().parse_args(["--once", "--only", "alerts"])
        fake = mock.Mock()
        fake.query.return_value = []  # compatible schema inventory
        fake.run_pass = mock.Mock(return_value=[])
        fake.state = notify.empty_state()
        with mock.patch.object(notify, "Notifier", return_value=fake), \
                mock.patch.object(notify, "enter_shared_state",
                                  return_value=(("agents-a:1", 1), True)), \
                mock.patch.object(notify, "leave_shared_state", return_value=True), \
                mock.patch.object(notify.db_mod, "connect", return_value=fake), \
                mock.patch.object(notify.db_mod, "require_schema"):
            self.assertEqual(notify.cmd_notify(cfg, notify.NotifyConfig(), args), 0)
        fake.run_local_pass.assert_called_once()
        self.assertTrue(Path(notify.state_path(cfg)).is_file())


class HolderPushesTest(unittest.TestCase):
    """Witness: post off, the VM holder pushes a pending decision at its tick."""

    def test_the_holder_pushes_a_pending_decision_on_its_route(self):
        sent = []

        class Sender:
            def send(self, channel, message):
                sent.append((channel.name, message.title))
                return None

        cfg = SimpleNamespace(host="ameesh-agents-2", state_dir=tempfile.gettempdir())
        notifier = notify.Notifier(cfg, notify.parse_config(NTFY), sender=Sender(),
                                   only={"alerts"}, clock=lambda: 5000.0)
        decision = {"schema": "ameesh-alert/1", "type": "decision_pending",
                    "agent": "mesh-design", "lot": 3383, "responsible": "human:owner",
                    "detail": "decision #1 pending", "since": 4900.0}
        with mock.patch.object(notifier, "_brief_agents"):
            notifier.run_pass(None, current=[decision])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "ntfy")


if __name__ == "__main__":
    unittest.main()
