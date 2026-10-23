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
from unittest import mock

from ameesh import notify, role_run, train_assembly, train_runner

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

    # -- wrapper ------------------------------------------------------------
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

    def test_the_command_acts_as_the_role_under_the_lease(self):
        self.lease(epoch=7, owner="runner-test")
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "env")
            command = ["python3", "-c",
                       "import os, pathlib; pathlib.Path(%r).write_text('|'.join(["
                       "os.environ.get('AGENT_MAIL_NAME', 'none'),"
                       " os.environ.get('AMEESH_RUNNER_ID', 'none'),"
                       " os.environ.get('AMEESH_LEASE_EPOCH', 'none'),"
                       " os.environ.get('AMEESH_TURN_ID', 'none')]))" % marker]
            with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "foreign",
                                             "AMEESH_RUNNER_ID": "foreign-runner",
                                             "AMEESH_TURN_ID": "turn-1"}):
                code, _ = self.run_cli(self._cfg(), self.args(command))
            self.assertEqual(code, 0)
            with open(marker, encoding="utf-8") as source:
                self.assertEqual(source.read(), "%s|runner-test|7|none" % NAME)

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

    def test_only_ci_takes_no_state_lock_and_runs_no_alert_pass(self):
        args = notify.build_parser().parse_args(["--once", "--only", "ci"])
        fake = mock.Mock()
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
            mock.Mock(host="banc"), [object()], stop=mock.Mock(), report=mock.Mock(),
            role="pilote")
        fake = mock.Mock()
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=fake), \
                mock.patch("ameesh.role_run.holder", return_value=False):
            self.assertFalse(service._role_ready())
            self.assertEqual(service.pending, {0})
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=fake), \
                mock.patch("ameesh.role_run.holder", return_value=True):
            self.assertTrue(service._role_ready())

    def test_service_without_a_role_keeps_the_old_behaviour(self):
        service = train_runner.Service(
            mock.Mock(host="banc"), [object()], stop=mock.Mock(), report=mock.Mock())
        self.assertTrue(service._role_ready())


if __name__ == "__main__":
    unittest.main()
