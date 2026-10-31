# SPDX-License-Identifier: AGPL-3.0-only
"""#3327: the GitHub observer is a role timer, installed on every candidate.

The witness is the one the lot asks for: a host that is not a candidate gets
no unit, a candidate without the local `github` key installs the canon-driven
watch but is refused for the CI/projection passes, and the two candidate
hosts share one lease so only the holder's timer runs (`ameesh role run`).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import notify, role_timers


def settings(*, projects=True, interval=900):
    return SimpleNamespace(
        projects={"ameesh": "manaty/ameesh"} if projects else {},
        interval=interval)


def row(*, releve="election", host="agents-a", candidates=("agents-a", "agents-b")):
    return {"name": "mesh-design", "releve": releve, "host": host,
            "candidats": list(candidates) if candidates else None}


class PlanTest(unittest.TestCase):
    def plan(self, *, host="agents-a", row_value=None, value=None, watch=(900,),
             watch_error=""):
        return role_timers.plan(
            host=host, role="mesh-design", row=row_value or row(),
            settings=value or settings(), watch_intervals=watch,
            watch_error=watch_error, notify_humans=["human:owner"])

    def test_a_candidate_gets_the_mechanical_passes(self):
        report = self.plan()
        self.assertTrue(report["candidate"])
        # #3383: the alert delivery joined the three observation passes
        self.assertEqual([item["name"] for item in report["timers"]],
                         ["github-watch", "issues", "ci", "alerts", "coherence"])
        by_name = {item["name"]: item for item in report["timers"]}
        self.assertEqual(by_name["ci"]["interval_s"], 60)
        self.assertEqual(by_name["issues"]["interval_s"], 90)
        self.assertEqual(by_name["coherence"]["interval_s"], 6 * 3600)
        # The watch keeps a 5 min wake-up for the takeover; its persisted
        # cursor keeps the declared 900 s cadence.
        self.assertEqual(by_name["github-watch"]["interval_s"], 90)
        self.assertFalse(any(item["blocked"] for item in report["timers"]))

    def test_a_host_outside_the_candidates_gets_no_unit(self):
        report = self.plan(host="agents-c")
        self.assertFalse(report["candidate"])
        self.assertEqual(report["timers"], [])

    def test_a_pinned_role_is_installed_only_on_its_host(self):
        pinned = row(releve="hote", host="agents-a", candidates=None)
        self.assertTrue(self.plan(row_value=pinned)["candidate"])
        self.assertFalse(self.plan(host="agents-b", row_value=pinned)["candidate"])

    def test_missing_github_projects_blocks_ci_and_projection_but_not_the_watch(self):
        report = self.plan(value=settings(projects=False))
        by_name = {item["name"]: item for item in report["timers"]}
        self.assertFalse(by_name["github-watch"]["blocked"])
        self.assertEqual(by_name["issues"]["blocked"], "github.projects missing on this host")
        self.assertEqual(by_name["ci"]["blocked"], "github.projects missing on this host")
        self.assertIn("github.projects", by_name["ci"]["detail"])
        self.assertIn("Minuteries de rôle", by_name["ci"]["detail"])

    def test_without_a_canon_declaration_the_watch_is_blocked_naming_the_gap(self):
        report = self.plan(watch=(), watch_error="no github_watch declaration in the canon")
        watch = report["timers"][0]
        self.assertEqual(watch["name"], "github-watch")
        self.assertEqual(watch["blocked"], "no watched repository")
        self.assertIn("github_watch", watch["detail"])

    def test_a_canon_project_missing_from_the_local_key_is_reported(self):
        report = role_timers.plan(
            host="agents-a", role="mesh-design", row=row(), settings=settings(),
            watch_intervals=[900],
            expected_projects={"ameesh": "manaty/ameesh", "nexlink": "manaty/nexlink"})
        self.assertEqual(report["missing_projects"],
                         [{"project": "nexlink", "repo": "manaty/nexlink"}])
        report = role_timers.plan(
            host="agents-a", role="mesh-design", row=row(),
            settings=settings(), watch_intervals=[900],
            expected_projects={"ameesh": "manaty/ameesh"})
        self.assertEqual(report["missing_projects"], [])


class RenderTest(unittest.TestCase):
    spec = role_timers.TimerSpec(
        "ci", "ci", 60, "ameesh — CI watches under the elected role lease (EP5)")

    def test_service_runs_the_pass_under_the_role_lease(self):
        text = role_timers.render_service(self.spec, role="mesh-design",
                                          bin_path="/opt/ameesh/bin/ameesh")
        self.assertIn("ExecStart=/opt/ameesh/bin/ameesh role run mesh-design -- "
                      "/opt/ameesh/bin/ameesh notify --once --only ci", text)
        self.assertIn("EnvironmentFile=%h/.config/ameesh/env", text)
        self.assertIn("EnvironmentFile=-%h/.config/ameesh/notify.env", text)
        self.assertIn("Type=oneshot", text)
        self.assertIn("TimeoutStartSec=10min", text)

    def test_coherence_service_runs_the_pass_with_its_own_bound(self):
        spec = role_timers.TimerSpec(
            "coherence", "coherence", role_timers.COHERENCE_INTERVAL_S,
            "ameesh — backlog coherence pass")
        text = role_timers.render_service(spec, role="mesh-design",
                                          bin_path="/opt/ameesh/bin/ameesh")
        self.assertIn("--only coherence", text)
        self.assertIn("TimeoutStartSec=30min", text)

    def test_timer_wakes_the_service_and_installs_as_a_user_timer(self):
        text = role_timers.render_timer(self.spec)
        self.assertIn("OnUnitActiveSec=60s", text)
        self.assertIn("Persistent=true", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_notify_dropin_removes_the_full_service_execstart(self):
        text = role_timers.render_notify_dropin("/opt/ameesh/bin/ameesh")
        self.assertIn("[Service]\nExecStart=\nExecStart=/opt/ameesh/bin/ameesh "
                      "notify --interval 30 --skip issues,github-watch,ci\n", text)

    def test_a_binary_path_with_whitespace_is_refused(self):
        with self.assertRaises(role_timers.TimerError):
            role_timers.render_service(self.spec, role="mesh-design",
                                       bin_path="/opt/ameesh bin/ameesh")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3327-timers-")
        self.dir = Path(self.tmp.name)
        (self.dir / "ameesh-notify.service").write_text("[Service]\n", encoding="utf-8")
        self.report = role_timers.plan(
            host="agents-a", role="mesh-design", row=row(), settings=settings(),
            watch_intervals=[900], notify_humans=["human:owner"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_writes_the_units_and_limits_the_full_notifier(self):
        result = role_timers.install(self.report, directory=self.dir, apply=False)
        self.assertEqual(len(result["installed"]), 11)  # 5 services + 5 timers + drop-in
        for name in ("github-watch", "issues", "ci", "alerts", "coherence"):
            self.assertTrue((self.dir / ("ameesh-role-%s.service" % name)).is_file())
            self.assertTrue((self.dir / ("ameesh-role-%s.timer" % name)).is_file())
        dropin = self.dir / "ameesh-notify.service.d" / "10-role-timers.conf"
        self.assertIn("--skip issues,github-watch,ci,alerts",
                      dropin.read_text(encoding="utf-8"))
        self.assertEqual(role_timers.check(self.report, directory=self.dir, apply=False), [])

    def test_install_is_idempotent(self):
        role_timers.install(self.report, directory=self.dir, apply=False)
        again = role_timers.install(self.report, directory=self.dir, apply=False)
        self.assertEqual(again["installed"], [])
        self.assertFalse(again["applied"])

    def test_a_blocked_pass_loses_its_stale_unit(self):
        stale = self.dir / "ameesh-role-ci.timer"
        stale.write_text("old", encoding="utf-8")
        report = role_timers.plan(host="agents-a", role="mesh-design", row=row(),
                                  settings=settings(projects=False), watch_intervals=[900])
        result = role_timers.install(report, directory=self.dir, apply=False)
        self.assertFalse(stale.exists())
        self.assertIn("ameesh-role-ci.timer", result["removed"])
        self.assertTrue((self.dir / "ameesh-role-github-watch.timer").is_file())

    def test_check_names_a_missing_unit_and_a_missing_dropin(self):
        role_timers.install(self.report, directory=self.dir, apply=False)
        (self.dir / "ameesh-role-ci.timer").unlink()
        (self.dir / "ameesh-notify.service.d" / "10-role-timers.conf").unlink()
        problems = role_timers.check(self.report, directory=self.dir, apply=False)
        self.assertTrue(any("missing ameesh-role-ci.timer" in text for text in problems))
        self.assertTrue(any("--skip" in text for text in problems))

    def test_check_flags_a_unit_from_another_configuration(self):
        role_timers.install(self.report, directory=self.dir, apply=False)
        (self.dir / "ameesh-role-ci.service").write_text(
            "[Service]\nExecStart=/old/ameesh notify\n", encoding="utf-8")
        problems = role_timers.check(self.report, directory=self.dir, apply=False)
        self.assertTrue(any("ameesh-role-ci.service is out of date" in text
                            for text in problems))

    def test_check_reports_the_configuration_gap(self):
        report = role_timers.plan(host="agents-a", role="mesh-design", row=row(),
                                  settings=settings(projects=False), watch_intervals=[900])
        problems = role_timers.check(report, directory=self.dir, apply=False)
        self.assertTrue(any("issues: github.projects missing" in text for text in problems))
        self.assertTrue(any("ci: github.projects missing" in text for text in problems))

    def test_remove_deletes_the_units_and_the_dropin(self):
        role_timers.install(self.report, directory=self.dir, apply=False)
        result = role_timers.remove(self.report, directory=self.dir, apply=False)
        self.assertEqual(len(result["removed"]), 11)
        self.assertFalse((self.dir / "ameesh-role-ci.timer").exists())
        self.assertFalse((self.dir / "ameesh-notify.service.d" / "10-role-timers.conf").exists())

    def test_a_non_candidate_touches_nothing(self):
        report = role_timers.plan(host="agents-c", role="mesh-design", row=row(),
                                  settings=settings(), watch_intervals=[900])
        result = role_timers.install(report, directory=self.dir, apply=False)
        self.assertEqual(result["installed"], [])
        self.assertFalse(any(self.dir.glob("ameesh-role-*")))


class SyncProjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3327-config-")
        self.path = Path(self.tmp.name) / "config.json"
        self.original = {
            "host": "agents-a",
            "notify": {"default_human": "human:owner"},
            "github": {"projects": {"ameesh": "manaty/ameesh"}, "interval": 900},
        }
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        os.chmod(self.path, 0o600)
        self.cfg = SimpleNamespace(config_path=str(self.path))
        self.declarations = [
            SimpleNamespace(project="ameesh", repo="manaty/ameesh"),
            SimpleNamespace(project="nexlink", repo="manaty/nexlink"),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_the_canon_project_without_touching_the_rest(self):
        result = role_timers.sync_github_projects(self.cfg, self.declarations)
        self.assertEqual(result["added"], [{"project": "nexlink",
                                            "repo": "manaty/nexlink"}])
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["github"]["projects"]["nexlink"], "manaty/nexlink")
        self.assertEqual(written["github"]["projects"]["ameesh"], "manaty/ameesh")
        self.assertEqual(written["github"]["interval"], 900)
        self.assertEqual(written["notify"], self.original["notify"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        backup = Path(str(self.path) + ".role-timers.bak")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), self.original)

    def test_is_idempotent(self):
        role_timers.sync_github_projects(self.cfg, self.declarations)
        result = role_timers.sync_github_projects(self.cfg, self.declarations)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["conflicts"], [])

    def test_a_local_mapping_that_contradicts_the_canon_is_kept_and_reported(self):
        self.path.write_text(json.dumps({
            "github": {"projects": {"nexlink": "acme/other"}}}), encoding="utf-8")
        result = role_timers.sync_github_projects(self.cfg, self.declarations)
        self.assertEqual(len(result["conflicts"]), 1)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["github"]["projects"]["nexlink"], "acme/other")

    def test_a_long_form_mapping_keeps_its_base(self):
        self.path.write_text(json.dumps({"github": {"projects": {
            "ameesh": "manaty/ameesh",
            "nexlink": {"repo": "manaty/nexlink", "base": ["develop"]}}}}),
            encoding="utf-8")
        result = role_timers.sync_github_projects(self.cfg, self.declarations)
        self.assertEqual(result["added"], [])
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["github"]["projects"]["nexlink"],
                         {"repo": "manaty/nexlink", "base": ["develop"]})

    def test_an_unreadable_projects_value_is_refused(self):
        self.path.write_text(json.dumps({"github": {"projects": []}}), encoding="utf-8")
        with self.assertRaises(role_timers.TimerError):
            role_timers.sync_github_projects(self.cfg, self.declarations)
        # Nothing was rewritten.
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"github": {"projects": []}})

    def test_a_profile_does_not_shadow_the_inherited_github_key(self):
        profile = Path(self.tmp.name) / "profiles" / "runner.json"
        profile.parent.mkdir()
        profile.write_text(json.dumps({"host": "agents-a"}), encoding="utf-8")
        os.chmod(profile, 0o600)
        cfg = SimpleNamespace(config_path=str(profile),
                              config_base=str(self.path))
        result = role_timers.sync_github_projects(cfg, self.declarations)
        self.assertEqual(result["path"], str(self.path))
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("nexlink", written["github"]["projects"])
        self.assertEqual(json.loads(profile.read_text(encoding="utf-8")),
                         {"host": "agents-a"})

    def test_check_names_a_project_missing_from_the_local_key(self):
        report = role_timers.plan(
            host="agents-a", role="mesh-design", row=row(), settings=settings(),
            watch_intervals=[900],
            expected_projects={"ameesh": "manaty/ameesh", "nexlink": "manaty/nexlink"})
        problems = role_timers.check(report, directory=Path(self.tmp.name), apply=False)
        self.assertTrue(any("github.projects lacks nexlink (manaty/nexlink)" in text
                            for text in problems))


class DropinCommandTest(unittest.TestCase):
    def test_the_dropin_runs_a_real_restricted_loop_and_keeps_take(self):
        text = role_timers.render_notify_dropin("/opt/ameesh/bin/ameesh")
        line = next(item for item in text.splitlines()
                    if item.startswith("ExecStart=") and "notify" in item)
        argv = shlex.split(line.split("=", 1)[1])[2:]  # drop the binary and `notify`
        args = notify.build_parser().parse_args(argv + ["--dry-run"])
        self.assertEqual(args.skip, ["issues,github-watch,ci"])
        self.assertIsNone(args.only)
        self.assertFalse(args.once)
        fake_notifier = mock.Mock()
        fake_db = mock.Mock()
        fake_db.query.return_value = []  # compatible schema inventory
        cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="3327-dropin-"))
        with mock.patch.object(notify, "Notifier", return_value=fake_notifier) as ctor, \
                mock.patch.object(notify.db_mod, "connect", return_value=fake_db), \
                mock.patch.object(notify.db_mod, "require_schema"), \
                mock.patch.object(notify, "load_state", return_value=notify.empty_state()), \
                mock.patch.object(notify, "load_shared_state", return_value=None), \
                mock.patch.object(notify.time, "sleep", side_effect=KeyboardInterrupt):
            rc = notify.cmd_notify(cfg, SimpleNamespace(configured=True), args)
        self.assertEqual(rc, 0, "the drop-in command must not be refused")
        self.assertEqual(ctor.call_args.kwargs["skip"],
                         frozenset({"issues", "github-watch", "ci"}))
        fake_notifier.run_local_pass.assert_called_once()
        fake_notifier.run_pass.assert_called_once()


class CandidateMutationTest(unittest.TestCase):
    def test_a_non_candidate_is_checked_before_any_config_sync(self):
        cfg = SimpleNamespace(host="agents-c", config_path="/does/not/exist",
                              config_base="", github={})
        row = {"releve": "election", "host": "agents-a",
               "candidats": ["agents-a", "agents-b"]}
        fake_db = mock.Mock()
        with mock.patch.object(role_timers.config_mod, "load", return_value=cfg), \
                mock.patch.object(role_timers.db_mod, "connect", return_value=fake_db), \
                mock.patch.object(role_timers.registry, "get", return_value=row), \
                mock.patch.object(role_timers, "sync_github_projects") as sync, \
                mock.patch.object(role_timers, "_watch_declarations") as declarations, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = role_timers.main(["--install", "--no-apply", "--json"])
        self.assertEqual(rc, 0)
        sync.assert_not_called()
        declarations.assert_not_called()
        fake_db.close.assert_called_once()


class NotifyGateTest(unittest.TestCase):
    def test_the_canon_driven_watch_runs_without_the_local_github_key(self):
        """#3327: a role timer on a candidate must not be silenced by cfg.github."""
        cfg = SimpleNamespace(host="agents-a", github=None, state_dir=tempfile.gettempdir())
        notifier = notify.Notifier.__new__(notify.Notifier)
        notifier.cfg = cfg
        notifier.dry_run = True
        notifier.log = lambda _text: None
        notifier._github_watcher = None
        with mock.patch("ameesh.github_watch.Watcher") as watcher:
            watcher.return_value.tick = mock.Mock(return_value=[])
            notifier._github_inputs(object(), 1000.0)
            watcher.assert_called_once_with(cfg, dry_run=True, log=notifier.log)
            watcher.return_value.tick.assert_called_once_with(mock.ANY, 1000.0)


if __name__ == "__main__":
    unittest.main()
