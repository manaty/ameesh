# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP5) : `ameesh role run` et les passages mécaniques gardés par le bail.

Le wrapper ne réclame jamais : il lit le bail vivant de l'agent (hôte + époque
+ expiration, horloge de la base) et n'exécute la commande que sur l'hôte
détenteur. La garde de transaction des gestes du pilote reste EP4 (`pilot_gesture`) :
ce wrapper ne fait que l'entrée, sans réclamer ni écrire de bail. `notify --only`
isole les passages mécaniques (GitHub, CI, issues) de la livraison d'alertes et
de son état local ; le train ombre/actif saute ses passages quand le bail élu
n'est pas tenu.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import tempfile
import unittest
from argparse import Namespace
from types import SimpleNamespace
from unittest import mock

from ameesh import (ci, github_watch, notify, plan_github, role_run,
                    train_assembly, train_runner)

from .support import PgTestCase

NAME = "pilote-3014"


class _Conn:
    """Connexion de test : lit la base du banc sans la fermer."""

    def __init__(self, db):
        self.db = db

    def query(self, sql, params):
        return self.db.query(sql, params)

    def close(self):
        pass


class RoleRunGuardTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM agent_registry WHERE name = %s", (NAME,))
        self.db.execute(
            "INSERT INTO agent_registry (name, harness, host, cwd, status)"
            " VALUES (%s, 'deepseek', 'banc', '/work', 'idle')", (NAME,))
        self.host = "banc"

    def lease(self, *, host: str = "banc", epoch: int = 7, expires_in: float = 300.0,
              status: str = "idle", owner: str = "runner-test") -> None:
        self.db.execute(
            "UPDATE agent_registry SET host = %s, status = %s, lease_owner = %s,"
            " lease_epoch = %s, lease_expires_at = clock_timestamp()"
            " + make_interval(secs => %s) WHERE name = %s",
            (host, status, owner, epoch, expires_in, NAME))

    def _cfg(self, host: str | None = None):
        return dataclasses.replace(self.cfg, host=host or self.host)

    def args(self, command, *, role: str = NAME, min_left: float = 0.0):
        return Namespace(role=role, min_left=min_left, dry_run=False, json=False,
                         command=list(command))

    def run_cli(self, cfg, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(role_run.db_mod, "connect",
                                   return_value=_Conn(self.db)):
                code = role_run.run(cfg, args)
        return code, out.getvalue()

    # -- lecture ------------------------------------------------------------
    def test_live_reads_the_database_clock_and_the_holder(self):
        self.lease(expires_in=120)
        report = role_run.live(self.db, NAME)
        self.assertTrue(report["held"])
        self.assertEqual(report["host"], "banc")
        self.assertEqual(report["epoch"], 7)
        self.assertGreater(report["left_s"], 100)
        self.assertLessEqual(report["left_s"], 120)

    def test_live_expired_is_not_held(self):
        self.lease(expires_in=-1)
        report = role_run.live(self.db, NAME)
        self.assertFalse(report["held"])
        self.assertIsNone(report["holder"])

    def test_live_unknown_role_raises(self):
        with self.assertRaises(role_run.RoleError):
            role_run.live(self.db, "does-not-exist-3014")

    def test_holder_is_false_for_another_host_and_true_for_the_holder(self):
        self.lease(host="banc")
        self.assertTrue(role_run.holder(self.db, NAME, "banc"))
        self.assertFalse(role_run.holder(self.db, NAME, "atelier"))
        self.assertFalse(role_run.holder(self.db, NAME, ""))

    # -- train service lease ------------------------------------------------
    def test_train_service_lease_follows_the_role_epoch(self):
        from ameesh import train_runner
        key = "train-service-3014"
        self.db.execute("DELETE FROM merge_locks WHERE target LIKE %s",
                        (key + "%",))
        cfg = self._cfg()
        try:
            old = train_runner.HostLease(cfg, key=key, ttl=900, retry=1)
            old.bind_role(NAME, 1)
            self.assertTrue(old.acquire(self.db))
            self.assertEqual(old.key, "%s:%s:1" % (key, NAME))
            # Same tenure: a second service incarnation is still excluded.
            same = train_runner.HostLease(cfg, key=key, ttl=900, retry=1)
            same.bind_role(NAME, 1)
            self.assertFalse(same.acquire(self.db))
            # New role epoch: the dead holder's 900 s service lease must not
            # block the new holder (study §3, new pilot in under 5 min).
            new = train_runner.HostLease(cfg, key=key, ttl=900, retry=1)
            new.bind_role(NAME, 2)
            self.assertTrue(new.acquire(self.db))
            self.assertEqual(new.key, "%s:%s:2" % (key, NAME))
        finally:
            self.db.execute("DELETE FROM merge_locks WHERE target LIKE %s",
                            (key + "%",))

    # -- wrapper ------------------------------------------------------------
    def test_main_filters_provider_keys_from_the_real_role_child(self):
        import subprocess
        self.lease(epoch=11)
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "credentials")
            command = ["python3", "-c", "import json,os,pathlib; pathlib.Path(%r).write_text(str((os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('GH_TOKEN'))))" % marker]
            with mock.patch.object(role_run.config_mod, "load", return_value=self._cfg()), \
                    mock.patch.object(role_run.db_mod, "connect", return_value=_Conn(self.db)), \
                    mock.patch.dict(os.environ, DEEPSEEK_API_KEY="provider", GH_TOKEN="transport"), \
                    mock.patch.object(subprocess.Popen, "__init__", subprocess.Popen.__init__):
                code = role_run.main([NAME, "--", *command])
            self.assertEqual(code, 0)
            with open(marker) as source:
                self.assertEqual(source.read(), "(None, 'transport')")

    def test_holder_runs_the_command_with_the_fencing_environment(self):
        self.lease(epoch=11)
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "env")
            command = ["python3", "-c",
                       "import os, pathlib; pathlib.Path(%r).write_text(' '.join("
                       "[os.environ['AMEESH_ROLE'], os.environ['AMEESH_ROLE_HOST'],"
                       " os.environ['AMEESH_LEASE_EPOCH']]))" % marker]
            code, _ = self.run_cli(self._cfg(), self.args(command))
            self.assertEqual(code, 0)
            with open(marker, encoding="utf-8") as source:
                self.assertEqual(source.read(), "%s banc 11" % NAME)

    def test_the_command_carries_the_role_actor_and_removes_the_identity(self):
        self.lease(epoch=7, owner="runner-test")
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "env")
            command = ["python3", "-c",
                       "import os, pathlib; pathlib.Path(%r).write_text('|'.join(["
                       "os.environ.get('AMEESH_ROLE', 'none'),"
                       " os.environ.get('AMEESH_ROLE_HOST', 'none'),"
                       "os.environ.get('AMEESH_LEASE_EPOCH', 'none'),"
                       "os.environ.get('AGENT_MAIL_NAME', 'none'),"
                       "os.environ.get('AMEESH_RUNNER_ID', 'none'),"
                       " os.environ.get('AMEESH_TURN_ID', 'none')]))" % marker]
            with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "foreign",
                                             "AMEESH_RUNNER_ID": "foreign-runner",
                                             "AMEESH_TURN_ID": "turn-1"}):
                code, _ = self.run_cli(self._cfg(), self.args(command))
            self.assertEqual(code, 0)
            with open(marker, encoding="utf-8") as source:
                self.assertEqual(source.read(),
                                 "%s|banc|7|none|none|none" % NAME)

    def test_other_host_is_standby_and_runs_nothing(self):
        self.lease(host="banc")
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "ran")
            command = ["python3", "-c", "open(%r, 'w').write('x')" % marker]
            code, out = self.run_cli(self._cfg("atelier"), self.args(command))
            self.assertEqual(code, 0)
            self.assertIn("standby", out)
            self.assertFalse(os.path.exists(marker))

    def test_expired_lease_is_standby(self):
        self.lease(expires_in=-1)
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "ran")
            command = ["python3", "-c", "open(%r, 'w').write('x')" % marker]
            code, out = self.run_cli(self._cfg(), self.args(command))
            self.assertEqual(code, 0)
            self.assertIn("standby", out)
            self.assertFalse(os.path.exists(marker))

    def test_min_left_is_standby(self):
        self.lease(expires_in=20)
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "ran")
            command = ["python3", "-c", "open(%r, 'w').write('x')" % marker]
            code, out = self.run_cli(self._cfg(), self.args(command, min_left=60))
            self.assertEqual(code, 0)
            self.assertIn("standby", out)
            self.assertFalse(os.path.exists(marker))

    def test_unknown_role_is_an_invocation_error(self):
        code, _ = self.run_cli(self._cfg(), self.args(["true"], role="unknown-3014"))
        self.assertEqual(code, 2)

    def test_command_exit_code_is_returned(self):
        self.lease()
        code, _ = self.run_cli(self._cfg(), self.args(["python3", "-c",
                                                      "raise SystemExit(7)"]))
        self.assertEqual(code, 7)

    def test_dry_run_does_not_execute(self):
        self.lease()
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "ran")
            args = self.args(["python3", "-c", "open(%r, 'w').write('x')" % marker])
            args.dry_run = True
            code, out = self.run_cli(self._cfg(), args)
            self.assertEqual(code, 0)
            self.assertIn("would-run", out)
            self.assertFalse(os.path.exists(marker))


class NotifyOnlyTest(unittest.TestCase):
    def _cfg(self):
        return mock.Mock(github=True, state_dir=tempfile.mkdtemp(prefix="3014-"),
                         host="banc")

    def test_only_set_is_strict_and_normalizes(self):
        self.assertIsNone(notify._only_set(None))
        self.assertEqual(notify._only_set(["github-watch,ci"]),
                         frozenset({"github-watch", "ci"}))
        with self.assertRaises(ValueError):
            notify._only_set(["bogus"])

    def test_parser_accepts_repeated_and_comma_values(self):
        args = notify.build_parser().parse_args(
            ["--once", "--only", "ci", "--only", "github-watch"])
        self.assertEqual(notify._only_set(args.only),
                         frozenset({"ci", "github-watch"}))

    def test_only_github_runs_the_github_pass_alone(self):
        notifier = notify.Notifier(self._cfg(), notify.NotifyConfig(),
                                   dry_run=True, only={"github-watch"})
        with mock.patch.object(notifier, "_github_inputs") as github, \
                mock.patch.object(notifier, "_github_issues") as issues, \
                mock.patch.object(notifier, "_ci_failures") as ci:
            notifier.run_pass(object())
        github.assert_called_once()
        issues.assert_not_called()
        ci.assert_not_called()

    def test_only_ci_runs_the_ci_pass_alone(self):
        notifier = notify.Notifier(self._cfg(), notify.NotifyConfig(),
                                   dry_run=True, only={"ci"})
        with mock.patch.object(notifier, "_github_inputs") as github, \
                mock.patch.object(notifier, "_github_issues") as issues, \
                mock.patch.object(notifier, "_ci_failures") as ci:
            notifier.run_pass(object())
        ci.assert_called_once()
        github.assert_not_called()
        issues.assert_not_called()

    def test_only_requires_once(self):
        args = Namespace(only=["ci"], once=False, dry_run=False)
        self.assertEqual(notify.cmd_notify(self._cfg(), notify.NotifyConfig(), args), 2)

    def test_skip_is_accepted_in_a_service_loop_and_keeps_other_passes(self):
        # #3327: the drop-in excludes the role-timer passes; the loop must
        # accept it, and the notifier receives the exclusion.
        args = notify.build_parser().parse_args(
            ["--skip", "issues,github-watch,ci", "--dry-run"])
        fake = mock.Mock()
        fake.query.return_value = []  # compatible schema inventory (#4975)
        fake.run_pass = mock.Mock(return_value=[])
        fake.run_local_pass = mock.Mock()
        with mock.patch.object(notify, "Notifier", return_value=fake) as ctor, \
                mock.patch.object(notify.db_mod, "connect", return_value=fake), \
                mock.patch.object(notify.db_mod, "require_schema"), \
                mock.patch.object(notify, "load_shared_state", return_value=None), \
                mock.patch.object(notify.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(notify.cmd_notify(self._cfg(), notify.NotifyConfig(), args), 0)
        self.assertEqual(ctor.call_args.kwargs["skip"],
                         frozenset({"issues", "github-watch", "ci"}))
        fake.run_pass.assert_called_once()

    def test_skip_refuses_a_pass_without_a_role_timer(self):
        args = notify.build_parser().parse_args(["--skip", "take", "--dry-run"])
        self.assertEqual(notify.cmd_notify(self._cfg(), notify.NotifyConfig(), args), 2)

    def test_skip_keeps_take_and_drops_only_the_role_timer_passes(self):
        notifier = notify.Notifier(
            self._cfg(), notify.NotifyConfig(), dry_run=True,
            take={"enabled": True}, wip={"apply": False},
            skip={"issues", "github-watch", "ci"})
        with mock.patch.object(notifier, "_github_inputs") as github, \
                mock.patch.object(notifier, "_github_issues") as issues, \
                mock.patch.object(notifier, "_ci_failures") as ci, \
                mock.patch.object(notifier, "_auto_take") as take, \
                mock.patch.object(notifier, "_wip_sweep") as wip:
            notifier.run_pass(None, current=[])
        github.assert_not_called()
        issues.assert_not_called()
        ci.assert_not_called()
        take.assert_called_once()
        wip.assert_called_once()

    def test_only_ci_takes_no_state_lock_and_runs_no_alert_pass(self):
        args = notify.build_parser().parse_args(["--once", "--only", "ci"])
        fake = mock.Mock()
        fake.query.return_value = []  # compatible schema inventory (#4975)
        fake.run_pass = mock.Mock(return_value=[])
        fake.run_local_pass = mock.Mock()
        fake.state = {}
        with mock.patch.object(notify, "Notifier", return_value=fake) as factory, \
                mock.patch.object(notify, "_Lock", side_effect=AssertionError("lock")), \
                mock.patch.object(notify, "load_state",
                                  side_effect=AssertionError("state")), \
                mock.patch.object(notify.db_mod, "connect", return_value=fake), \
                mock.patch.object(notify.db_mod, "require_schema"):
            self.assertEqual(notify.cmd_notify(self._cfg(), notify.NotifyConfig(), args), 0)
        self.assertEqual(factory.call_args.kwargs["only"], frozenset({"ci"}))
        fake.run_local_pass.assert_not_called()
        fake.run_pass.assert_called_once()


class RoleHostGateTest(unittest.TestCase):
    """L165 (EP5): `github.host` no longer silences the elected host's timer.

    The three host-designated passes read `AMEESH_ROLE`/`AMEESH_ROLE_HOST`
    (set by `role run` after the lease check). Without that pair the historical
    pin is unchanged. These witnesses exercise the real gates, not a mock of
    them; only the GitHub API and the projection's downstream call are fake.
    """

    def cfg(self, host="candidate-b"):
        return SimpleNamespace(
            host=host, state_dir=tempfile.mkdtemp(prefix="3014-gate-"),
            github={"host": "candidate-a",
                    "projects": {"ameesh": "manaty/ameesh"},
                    "interval": 900})

    def role_env(self, host="candidate-b"):
        return mock.patch.dict(os.environ, {"AMEESH_ROLE": "mesh-design",
                                           "AMEESH_ROLE_HOST": host})

    def test_host_gate_needs_the_role_pair(self):
        cfg = self.cfg()
        self.assertFalse(role_run.host_authorized(cfg, "candidate-a"))
        self.assertTrue(role_run.host_authorized(cfg, "candidate-b"))
        with self.role_env():
            self.assertTrue(role_run.host_authorized(cfg, "candidate-a"))
            self.assertTrue(role_run.host_authorized(cfg, None))
        with self.role_env(host="candidate-a"):
            self.assertFalse(role_run.host_authorized(cfg, "candidate-a"))

    def test_ci_repos_accepts_the_role_host(self):
        cfg = self.cfg()
        self.assertEqual(ci.Watcher(cfg).repos(), [])
        with self.role_env():
            self.assertEqual(ci.Watcher(cfg).repos(), ["manaty/ameesh"])

    def test_github_watch_tick_crosses_the_host_gate_with_the_role(self):
        cfg = self.cfg()
        subscription = github_watch.Subscription(
            project="ameesh", repo="manaty/ameesh", agent="docs-3014", interval=900)
        fake = mock.Mock()
        fake.github_watch.cursor.return_value = None
        fake.github_watch.scan.return_value = None
        log = mock.Mock()
        watcher = github_watch.Watcher(
            cfg, api=mock.Mock(), log=log, declarations=[subscription])
        with self.role_env(), \
                mock.patch.object(github_watch.storage, "of", return_value=fake), \
                mock.patch.object(github_watch, "collect_batch",
                                  side_effect=RuntimeError("gate-passed-3014")):
            self.assertEqual(watcher.tick(object(), 1000.0), [])
        self.assertTrue(any("gate-passed-3014" in str(call)
                            for call in log.call_args_list))

        log.reset_mock()
        watcher = github_watch.Watcher(
            cfg, api=mock.Mock(), log=log, declarations=[subscription])
        with mock.patch.object(github_watch.storage, "of", return_value=fake), \
                mock.patch.object(github_watch, "collect_batch",
                                  side_effect=RuntimeError("gate-passed-3014")):
            self.assertEqual(watcher.tick(object(), 1000.0), [])
        self.assertFalse(any("gate-passed-3014" in str(call)
                             for call in log.call_args_list))

    def test_projector_tick_runs_on_the_role_host(self):
        cfg = self.cfg()
        fake = mock.Mock()
        fake.work.issue_feed.return_value = []
        # Le témoin ne porte que la porte d'hôte : un `gh` factice le rend
        # hermétique (sans binaire `gh`, `Projector.tick` reculerait avant
        # d'appeler la projection et le second passage rendrait []).
        projector = plan_github.Projector(cfg, dry_run=True, gh=mock.Mock())
        projector._journal = mock.Mock()
        with mock.patch.object(plan_github.storage, "of", return_value=fake), \
                mock.patch.object(plan_github, "project_lots",
                                  return_value={"errors": []}) as project:
            self.assertEqual(projector.tick(object(), now=0), [])
        project.assert_not_called()

        with self.role_env(), \
                mock.patch.object(plan_github.storage, "of", return_value=fake), \
                mock.patch.object(plan_github, "project_lots",
                                  return_value={"errors": []}) as project:
            self.assertEqual(projector.tick(object(), now=0), [{"errors": []}])
        project.assert_called_once()


class TrainRoleTest(unittest.TestCase):
    def raw(self, *, role="pilote"):
        return {
            "schema": "ameesh-train-host/1",
            "host": "pc-smichea",
            "mode": "shadow",
            "projects": {
                "ameesh": {
                    "repo": "/tmp/repo", "target": "main",
                    "github": "manaty/ameesh", "canon": "/tmp/canon",
                    "canon_commit": "a" * 40,
                    "ci_contract": "ameesh", "provenance": "review_provenance",
                    "reservation": "packet_reservation",
                    "deadline_minutes": 30, "candidate": False,
                    "initial_since": None,
                },
            },
            "role": role,
        }

    def test_settings_parse_the_role(self):
        settings = train_assembly.Settings.parse(self.raw())
        self.assertEqual(settings.role, "pilote")

    def test_settings_refuse_an_invalid_role(self):
        with self.assertRaises(train_assembly.ConfigurationError):
            train_assembly.Settings.parse(self.raw(role="not a name"))

    def test_assembly_follows_the_role_instead_of_the_host_pin(self):
        cfg = mock.Mock(host="ameesh-agents-1", runner="runner-3014",
                        train=self.raw())
        with mock.patch.object(train_assembly, "_passage", return_value=object()):
            assembly = train_assembly.assemble(cfg, notify=lambda text: None)
        self.assertEqual(assembly.role, "pilote")
        self.assertEqual(len(assembly.passages), 1)

    def test_single_candidate_role_keeps_the_historical_assembly(self):
        # The current deployment has one candidate host (the train runs on
        # agents-2). Enabling `role` on that host must not change the packet
        # assembly: same mode, same passages, same service lease, shadow and
        # active alike.
        for mode in ("shadow", "active"):
            raw = self.raw(role="pilote")
            raw["mode"] = mode
            cfg = mock.Mock(host="pc-smichea", runner="runner-3014", train=raw)
            with mock.patch.object(train_assembly, "_passage", return_value=object()):
                with_role = train_assembly.assemble(cfg, notify=lambda text: None)
            pinned_raw = dict(raw)
            pinned_raw.pop("role")
            pinned = mock.Mock(host="pc-smichea", runner="runner-3014",
                              train=pinned_raw)
            with mock.patch.object(train_assembly, "_passage", return_value=object()):
                without_role = train_assembly.assemble(pinned, notify=lambda text: None)
            self.assertEqual(len(with_role.passages), len(without_role.passages))
            self.assertEqual(with_role.settings.mode, mode)
            self.assertEqual(without_role.settings.mode, mode)
            self.assertIsNotNone(with_role.lease)
            self.assertIsNotNone(without_role.lease)
            self.assertEqual(with_role.role, "pilote")
            self.assertIsNone(without_role.role)

    def test_assembly_keeps_the_host_pin_without_a_role(self):
        cfg = mock.Mock(host="ameesh-agents-1", runner="runner-3014",
                        train=self.raw())
        raw = self.raw()
        raw.pop("role")
        cfg.train = raw
        assembly = train_assembly.assemble(cfg, notify=lambda text: None)
        self.assertFalse(assembly.active)
        self.assertIn("designates", assembly.reason)

    def test_service_waits_without_the_role_and_schedules_with_it(self):
        service = train_runner.Service(
            mock.Mock(host="banc"), [SimpleNamespace(guard=None, lease=None)], stop=mock.Mock(), report=mock.Mock(),
            role="pilote")
        fake = mock.Mock()
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=fake), \
                mock.patch("ameesh.role_run.live",
                           return_value={"held": False, "host": "banc", "epoch": 1}):
            self.assertFalse(service._role_ready())
            self.assertEqual(service.pending, {0})
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=fake), \
                mock.patch("ameesh.role_run.live",
                           return_value={"held": True, "host": "banc", "epoch": 2}):
            self.assertTrue(service._role_ready())

    def test_service_without_a_role_keeps_the_old_behaviour(self):
        service = train_runner.Service(
            mock.Mock(host="banc"), [SimpleNamespace(guard=None, lease=None)], stop=mock.Mock(), report=mock.Mock())
        self.assertTrue(service._role_ready())


if __name__ == "__main__":
    unittest.main()
