# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import grp
import re
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from ameesh import credential_gate as gate


def _group_name():
    return grp.getgrgid(os.getegid()).gr_name


def _rule(**over):
    rule = {"id": "echo", "kind": "kms", "exe": "/bin/echo", "argv": ["hello"]}
    rule.update(over)
    return rule


def _policy(directory, rules=None, **over):
    data = {
        "schema": gate.SCHEMA,
        "socket": os.path.join(directory, "broker.sock"),
        "socket_group": _group_name(),
        "agent": "bureau",
        "callers": {"uids": [os.geteuid()]},
        "rules": rules if rules is not None else [_rule()],
    }
    data.update(over)
    path = os.path.join(directory, "broker.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(path, 0o600)
    return gate.load_policy(path)


def _request(**over):
    request = {
        "schema": gate.REQUEST_SCHEMA,
        "request_id": str(uuid.uuid4()),
        "lot": 5772,
        "kind": "kms",
        "target": "",
        "wait_until": None,
        "argv": ["/bin/echo", "hello", "world"],
    }
    request.update(over)
    return request


class Transaction:
    """The two queries pre_act.inspect needs, plus the evidence inserts."""

    def __init__(self, state="build", duplicate=False, events=(), messages=()):
        self.state = state
        self.duplicate = duplicate
        self.events = events
        self.messages = messages
        self.writes = []

    def query(self, sql, params):
        if "FROM work_items" in sql:
            return [{"state": self.state, "now_ts": 200}]
        if "FROM work_item_events" in sql:
            if "evidence->>'type'" in sql:
                return [{"id": 1}] if self.duplicate else []
            return list(self.events)
        if "FROM agent_mailbox" in sql:
            return list(self.messages)
        raise AssertionError(sql)

    def execute(self, sql, params):
        self.writes.append((sql, params))


class Database:
    def __init__(self, tx):
        self.tx = tx
        self.closed = 0

    def transaction(self, **_kwargs):
        return nullcontext(self.tx)

    def close(self):
        self.closed += 1


class Document:
    def __init__(self, branch="main", workflows=("deploy-api-prod.yml", "promote-prod.yml")):
        self.branch = branch
        self.workflows = workflows
        self.commit = "1" * 40

    def override(self, project):
        return SimpleNamespace(production_branch=self.branch,
                               production_workflows=self.workflows)


def _broker(policy, tx=None, **over):
    tx = tx or Transaction()
    database = Database(tx)
    over.setdefault("cfg", object())
    over.setdefault("db_factory", lambda: database)
    over.setdefault("clock", lambda: 100.0)
    over.setdefault("monotonic", lambda: 0.0)
    broker = gate.Broker(policy, **over)
    broker.test_database = database
    return broker


def _evidence(write):
    """The evidence JSON is the last parameter of every pre-act insert."""
    return json.loads(write[1][4])


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def test_valid_policy_loads(self):
        policy = _policy(self.directory)
        self.assertEqual(policy.agent, "bureau")
        self.assertEqual(policy.rules[0].exe, "/bin/echo")
        self.assertEqual(policy.callers_uids, (os.geteuid(),))

    def test_unknown_top_level_key_is_refused(self):
        with self.assertRaisesRegex(gate.PolicyError, "unknown key"):
            _policy(self.directory, unexpected=1)

    def test_relative_socket_is_refused(self):
        with self.assertRaisesRegex(gate.PolicyError, "absolute"):
            _policy(self.directory, socket="broker.sock")

    def test_unknown_kind_is_refused(self):
        with self.assertRaisesRegex(gate.PolicyError, "kind"):
            _policy(self.directory, rules=[_rule(kind="other")])

    def test_prod_needs_project_and_repo(self):
        with self.assertRaisesRegex(gate.PolicyError, "project"):
            _policy(self.directory, rules=[_rule(kind="prod")])
        with self.assertRaisesRegex(gate.PolicyError, "repo"):
            _policy(self.directory, rules=[_rule(kind="prod", project="nexlink")])

    def test_prod_takes_no_fixed_flags(self):
        with self.assertRaisesRegex(gate.PolicyError, "prod rules"):
            _policy(self.directory, rules=[_rule(kind="prod", project="nexlink",
                                                repo="manaty/nexlink",
                                                fixed={"--project": "x"})])

    def test_empty_rules_are_refused(self):
        with self.assertRaisesRegex(gate.PolicyError, "non-empty"):
            _policy(self.directory, rules=[])

    def test_duplicate_rule_ids_are_refused(self):
        with self.assertRaisesRegex(gate.PolicyError, "duplicate"):
            _policy(self.directory, rules=[_rule(), _rule()])

    def test_policy_writable_by_others_is_refused(self):
        path = os.path.join(self.directory, "broker.json")
        _policy(self.directory)
        os.chmod(path, 0o666)
        with self.assertRaisesRegex(gate.PolicyError, "writable"):
            gate.load_policy(path)

    def test_environment_key_is_bounded(self):
        with self.assertRaisesRegex(gate.PolicyError, "not admitted"):
            _policy(self.directory, env={"LD_PRELOAD": "/tmp/x.so"})

    def test_forbid_flag_is_allowed_on_kms(self):
        policy = _policy(self.directory, rules=[_rule(forbid=["--destroy"])])
        self.assertEqual(policy.rules[0].forbid, ("--destroy",))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def test_dangerous_flag_is_refused(self):
        for argv in (["/usr/bin/gcloud", "kms", "--log-http"],
                     ["/usr/bin/gcloud", "kms", "--log-http=1"],
                     ["/usr/bin/gcloud", "kms", "--verbosity", "debug"],
                     ["/usr/bin/gcloud", "kms", "--verbosity=debug"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(gate.Refused, "refused"):
                gate._check_argv(argv)

    def test_flags_file_is_refused_globally(self):
        for argv in (["/usr/bin/gcloud", "kms", "--flags-file", "x"],
                     ["/usr/bin/gcloud", "kms", "--flags-file=x"],
                     ["/bin/echo", "hello", "--flags-file", "x"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(gate.Refused, "refused"):
                gate._check_argv(argv)

    def test_unknown_flag_is_refused_by_rule(self):
        policy = _policy(self.directory, rules=[_rule()])
        for flag in ("--format=json", "--impersonate-service", "--project=other"):
            with self.subTest(flag=flag), self.assertRaisesRegex(gate.Refused, "not admitted by rule"):
                gate._apply_rule(policy, policy.rules[0],
                                 ["/bin/echo", "hello", flag])

    def test_short_flag_is_refused(self):
        policy = _policy(self.directory, rules=[_rule()])
        with self.assertRaisesRegex(gate.Refused, "short flag"):
            gate._apply_rule(policy, policy.rules[0], ["/bin/echo", "hello", "-q"])

    def test_free_flag_is_admitted(self):
        policy = _policy(self.directory, rules=[_rule(free=["--quiet"])])
        rule = policy.rules[0]
        gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--quiet"])
        gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--quiet=1"])
        with self.assertRaisesRegex(gate.Refused, "not admitted by rule"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--format=json"])

    def test_prod_rule_refuses_free(self):
        with self.assertRaisesRegex(gate.PolicyError, "free"):
            _policy(self.directory, rules=[_rule(
                id="prod", kind="prod", exe="/bin/echo", argv=["workflow", "run"],
                project="nexlink", repo="manaty/nexlink", free=["--quiet"])])

    def test_multiline_and_empty_arguments_are_refused(self):
        with self.assertRaises(gate.Refused):
            gate._check_argv(["/bin/echo", ""])
        with self.assertRaises(gate.Refused):
            gate._check_argv(["/bin/echo", "a\nb"])

    def test_longest_rule_prefix_wins(self):
        policy = _policy(self.directory, rules=[
            _rule(id="short", argv=["hello"]),
            _rule(id="long", argv=["hello", "world"]),
        ])
        rule = gate._match_rule(policy, "kms", ["/bin/echo", "hello", "world", "x"])
        self.assertEqual(rule.id, "long")
        rule = gate._match_rule(policy, "kms", ["/bin/echo", "hello", "x"])
        self.assertEqual(rule.id, "short")

    def test_unknown_command_is_refused(self):
        policy = _policy(self.directory)
        with self.assertRaisesRegex(gate.Refused, "not admitted"):
            gate._match_rule(policy, "kms", ["/bin/echo", "other"])

    def test_fixed_flags_are_appended_and_checked(self):
        policy = _policy(self.directory, rules=[_rule(
            fixed={"--project": "nexlink-prod", "--location": "asia-southeast1"},
            allow={"--keyring": ["nexlink-messages"]})])
        rule = policy.rules[0]
        final = gate._apply_rule(policy, rule,
                                 ["/bin/echo", "hello", "job", "--keyring", "nexlink-messages"])
        self.assertIn("--project=nexlink-prod", final)
        self.assertIn("--location=asia-southeast1", final)
        final = gate._apply_rule(policy, rule, [
            "/bin/echo", "hello", "job", "--project=nexlink-prod",
            "--location=asia-southeast1"])
        self.assertEqual(final.count("--project=nexlink-prod"), 1)
        with self.assertRaisesRegex(gate.Refused, "fixed flag"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--project", "other"])
        with self.assertRaisesRegex(gate.Refused, "not admitted"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--keyring", "other"])

    def test_positional_targets_are_exact(self):
        policy = _policy(self.directory, rules=[_rule(
            targets=["nexlink-messages"],
            fixed={"--location": "asia-southeast1"},
            allow={"--keyring": ["nexlink-messages"]})])
        rule = policy.rules[0]
        final = gate._apply_rule(policy, rule, [
            "/bin/echo", "hello", "nexlink-messages", "--location", "asia-southeast1",
            "--keyring", "nexlink-messages"])
        self.assertEqual(final[2], "nexlink-messages")
        with self.assertRaisesRegex(gate.Refused, "positional target"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello", "another-keyring"])
        with self.assertRaisesRegex(gate.Refused, "positional target"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello"])
        with self.assertRaisesRegex(gate.Refused, "positional target"):
            gate._apply_rule(policy, rule, [
                "/bin/echo", "hello", "nexlink-messages", "extra"])

    def test_empty_targets_refuse_any_positional(self):
        policy = _policy(self.directory, rules=[_rule(targets=[], free=["--keyring"])])
        rule = policy.rules[0]
        gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--keyring", "k"])
        with self.assertRaisesRegex(gate.Refused, "positional target"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello", "value"])

    def test_prod_rule_refuses_targets(self):
        with self.assertRaisesRegex(gate.PolicyError, "targets"):
            _policy(self.directory, rules=[_rule(
                id="prod", kind="prod", exe="/bin/echo", argv=["workflow", "run"],
                project="nexlink", repo="manaty/nexlink", targets=[])])

    def test_required_flag_needs_a_value(self):
        policy = _policy(self.directory, rules=[_rule(required=["--keyring"])])
        rule = policy.rules[0]
        with self.assertRaisesRegex(gate.Refused, "requires a value"):
            gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--keyring"])
        gate._apply_rule(policy, rule, ["/bin/echo", "hello", "--keyring", "k"])

    def test_forbidden_rule_flag_is_refused(self):
        policy = _policy(self.directory, rules=[_rule(forbid=["--destroy"])])
        with self.assertRaisesRegex(gate.Refused, "refused by rule"):
            gate._apply_rule(policy, policy.rules[0], ["/bin/echo", "hello", "--destroy"])


class ProdTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.policy = _policy(self.directory, rules=[_rule(
            id="prod", kind="prod", exe="/bin/echo", argv=["workflow", "run"],
            project="nexlink", repo="manaty/nexlink", inputs=["sha"])])
        self.rule = self.policy.rules[0]

    def command(self, *extra):
        return ["/bin/echo", "workflow", "run", "deploy-api-prod",
                "--repo", "manaty/nexlink", "--ref", "main"] + list(extra)

    def test_target_and_workflow_are_resolved(self):
        argv, target = gate._parse_prod(self.rule, self.command(), Document())
        self.assertEqual(target, "manaty/nexlink#deploy-api-prod.yml@main")
        self.assertEqual(argv, self.command())

    def test_wrong_repository_is_refused(self):
        command = self.command()
        command[command.index("manaty/nexlink")] = "manaty/other"
        with self.assertRaisesRegex(gate.Refused, "--repo"):
            gate._parse_prod(self.rule, command, Document())

    def test_wrong_ref_is_refused(self):
        command = self.command()
        command[command.index("main")] = "develop"
        with self.assertRaisesRegex(gate.Refused, "--ref"):
            gate._parse_prod(self.rule, command, Document())

    def test_workflow_outside_the_canon_is_refused(self):
        command = self.command()
        command[command.index("deploy-api-prod")] = "anything"
        with self.assertRaisesRegex(gate.Refused, "declared production workflow"):
            gate._parse_prod(self.rule, command, Document())

    def test_production_none_is_refused(self):
        with self.assertRaisesRegex(gate.Refused, "no production"):
            gate._parse_prod(self.rule, self.command(), Document(branch="", workflows=()))

    def test_unknown_flag_is_refused(self):
        with self.assertRaisesRegex(gate.Refused, "not admitted"):
            gate._parse_prod(self.rule, self.command("--hostname", "evil"), Document())

    def test_pregeste_input_is_refused(self):
        with self.assertRaisesRegex(gate.Refused, "pregeste"):
            gate._parse_prod(self.rule, self.command("-f", "pregeste=x"), Document())

    def test_undeclared_input_is_refused(self):
        with self.assertRaisesRegex(gate.Refused, "input other"):
            gate._parse_prod(self.rule, self.command("-f", "other=x"), Document())

    def test_declared_input_is_accepted(self):
        gate._parse_prod(self.rule, self.command("-f", "sha=abc"), Document())


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        patcher = mock.patch.object(gate.storage, "is_remote", return_value=False)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_success_records_receipt_and_effect(self):
        policy = _policy(self.directory)
        tx = Transaction()
        broker = _broker(policy, tx)
        result = broker.handle(_request(), os.geteuid(), os.getegid())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["exit"], 0)
        self.assertEqual(result["audit"], "recorded")
        self.assertEqual(len(tx.writes), 4)   # receipt, updated_at, start, end
        receipt = _evidence(tx.writes[0])
        self.assertEqual(receipt["type"], "pre-act/1")
        self.assertEqual(receipt["request_id"], result["request_id"])
        start = _evidence(tx.writes[2])
        self.assertEqual((start["type"], start["phase"]), ("pre-act/2", "start"))
        self.assertIn("argv_sha256", start)
        self.assertEqual(start["argv_head"], ["/bin/echo", "hello", "world"])
        self.assertNotIn("argv", start)
        end = _evidence(tx.writes[3])
        self.assertEqual((end["type"], end["phase"]), ("pre-act/2", "end"))
        self.assertEqual(end["exit"], 0)
        self.assertIn("duration_ms", end)

    def test_unknown_caller_is_refused(self):
        broker = _broker(_policy(self.directory))
        result = broker.handle(_request(), 999999, 999999)
        self.assertFalse(result["ok"])
        self.assertIn("may not use", result["error"])

    def test_duplicate_request_is_refused_before_any_act(self):
        tx = Transaction(duplicate=True)
        broker = _broker(_policy(self.directory), tx)
        result = broker.handle(_request(), os.geteuid(), os.getegid())
        self.assertFalse(result["ok"])
        self.assertIn("already consumed", result["error"])
        self.assertEqual(tx.writes, [])

    def test_wait_until_captures_the_start_before_waiting(self):
        policy = _policy(self.directory)
        tx = Transaction()
        sleeps = []
        broker = _broker(policy, tx, sleep=sleeps.append)
        request = _request(wait_until="1970-01-01T00:02:40Z")
        result = broker.handle(request, os.geteuid(), os.getegid())
        self.assertTrue(result["ok"], result)
        self.assertEqual(sleeps, [60.0])
        receipt = _evidence(tx.writes[0])
        self.assertIn("since=100.0", tx.writes[0][1][2])
        self.assertEqual(receipt["request_id"], request["request_id"])

    def test_too_long_wait_is_refused(self):
        policy = _policy(self.directory, max_wait_seconds=10)
        tx = Transaction()
        broker = _broker(policy, tx)
        result = broker.handle(_request(wait_until="1970-01-01T00:02:40Z"),
                               os.geteuid(), os.getegid())
        self.assertFalse(result["ok"])
        self.assertIn("longer", result["error"])
        self.assertEqual(tx.writes, [])

    def test_stale_check_does_not_start_the_act(self):
        policy = _policy(self.directory)
        tx = Transaction()
        ticks = iter([0.0, 31.0])
        broker = _broker(policy, tx, monotonic=lambda: next(ticks))
        result = broker.handle(_request(), os.geteuid(), os.getegid())
        self.assertFalse(result["ok"])
        self.assertIn("older", result["error"])
        # Only the receipt and updated_at were written: no start event.
        self.assertEqual(len(tx.writes), 2)

    def test_runner_failure_still_closes_the_trace(self):
        policy = _policy(self.directory)
        tx = Transaction()

        def fail(_argv):
            raise gate._Unavailable("cannot start")

        broker = _broker(policy, tx, runner=fail)
        result = broker.handle(_request(), os.geteuid(), os.getegid())
        self.assertFalse(result["ok"])
        self.assertEqual(len(tx.writes), 4)
        end = _evidence(tx.writes[3])
        self.assertEqual(end["phase"], "end")
        self.assertIsNone(end["exit"])

    def test_remote_database_is_refused(self):
        broker = _broker(_policy(self.directory))
        with mock.patch.object(gate.storage, "is_remote", return_value=True):
            result = broker.handle(_request(), os.geteuid(), os.getegid())
        self.assertFalse(result["ok"])
        self.assertIn("transactional", result["error"])

    def test_prod_injects_the_receipt_and_checks_the_target(self):
        policy = _policy(self.directory, rules=[_rule(
            id="prod", kind="prod", exe="/bin/echo", argv=["workflow", "run"],
            project="nexlink", repo="manaty/nexlink")])
        tx = Transaction()
        seen = []

        def run(argv):
            seen.append(argv)
            return 0, False, b"", b""

        broker = _broker(policy, tx, runner=run, document=Document())
        request = _request(kind="prod", argv=[
            "/bin/echo", "workflow", "run", "deploy-api-prod",
            "--repo", "manaty/nexlink", "--ref", "main"])
        result = broker.handle(request, os.geteuid(), os.getegid())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["target"], "manaty/nexlink#deploy-api-prod.yml@main")
        self.assertEqual(seen[0][-2:], ["-f", "pregeste=" + result["pregeste"]])
        start = _evidence(tx.writes[2])
        self.assertEqual(start["target"], result["target"])
        self.assertEqual(start["rule"], "prod")

        request["request_id"] = str(uuid.uuid4())
        request["target"] = "somewhere-else"
        result = broker.handle(request, os.geteuid(), os.getegid())
        self.assertFalse(result["ok"])
        self.assertIn("must match", result["error"])


class AuditTest(unittest.TestCase):
    def test_a_complete_trace_has_no_anomaly(self):
        events = [
            {"type": "pre-act/2", "phase": "start", "pregeste_id": "p", "started": 10.0},
            {"type": "pre-act/2", "phase": "end", "pregeste_id": "p", "started": 10.0},
        ]
        self.assertEqual(gate.audit_events(events, 20.0, stale_seconds=5), [])

    def test_unpaired_start_is_reported_after_the_stale_delay(self):
        events = [{"type": "pre-act/2", "phase": "start", "pregeste_id": "p",
                   "started": 10.0, "lot": 1, "kind": "kms"}]
        self.assertEqual(gate.audit_events(events, 12.0, stale_seconds=5), [])
        anomalies = gate.audit_events(events, 20.0, stale_seconds=5)
        self.assertEqual(anomalies[0]["kind"], "unpaired-start")
        self.assertEqual(anomalies[0]["pregeste"], "p")

    def test_unpaired_end_is_an_anomaly(self):
        events = [{"type": "pre-act/2", "phase": "end", "pregeste_id": "p"}]
        self.assertEqual(gate.audit_events(events, 20.0)[0]["kind"], "unpaired-end")

    def test_duplicate_start_is_an_anomaly(self):
        events = [
            {"type": "pre-act/2", "phase": "start", "pregeste_id": "p", "started": 10.0},
            {"type": "pre-act/2", "phase": "start", "pregeste_id": "p", "started": 11.0},
            {"type": "pre-act/2", "phase": "end", "pregeste_id": "p"},
        ]
        kinds = [item["kind"] for item in gate.audit_events(events, 20.0)]
        self.assertIn("duplicate-start", kinds)


class SocketTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.policy = _policy(self.directory)

    def test_one_request_round_trip(self):
        class Fake:
            def handle(self, value, uid, gid):
                return {"schema": gate.RESULT_SCHEMA, "ok": True, "exit": 0,
                        "stdout": "hello\n", "stderr": "", "request_id": value["request_id"]}

        server = gate._bind_socket(self.policy.socket, gate._group_gid(self.policy.socket_group))
        server.broker = Fake()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        try:
            result = gate.call(self.policy.socket, _request())
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            try:
                os.unlink(self.policy.socket)
            except OSError:
                pass
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "hello\n")

    def test_missing_socket_is_a_refusal(self):
        with self.assertRaisesRegex(gate.Refused, "unavailable"):
            gate.call(os.path.join(self.directory, "missing.sock"), _request())


class CheckPolicyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def test_check_reports_the_rule_and_canon(self):
        policy = _policy(self.directory, rules=[_rule(
            id="prod", kind="prod", exe="/bin/echo", argv=["workflow", "run"],
            project="nexlink", repo="manaty/nexlink")])
        cfg = SimpleNamespace(canon="/canon", extra_canons=())
        db = mock.Mock()
        with mock.patch.object(gate.mechanics, "configured", return_value=Document()), \
             mock.patch.object(gate.db_mod, "open_db", return_value=db), \
             mock.patch.object(gate.storage, "is_remote", return_value=False):
            report = gate._check_policy(policy, cfg)
        self.assertEqual(report["canon"], "1" * 40)
        self.assertTrue(report["database"])
        db.close.assert_called_once()

    def test_check_requires_private_credential_directories(self):
        config_dir = os.path.join(self.directory, "gcloud")
        os.mkdir(config_dir, 0o700)
        policy = _policy(self.directory, env={"CLOUDSDK_CONFIG": config_dir})
        report = gate._check_policy(policy, None)
        self.assertEqual(report["rules"], 1)
        os.chmod(config_dir, 0o777)
        with self.assertRaisesRegex(gate.PolicyError, "writable by group or others"):
            gate._check_policy(policy, None)
        os.rmdir(config_dir)
        with self.assertRaisesRegex(gate.PolicyError, "unreadable"):
            gate._check_policy(policy, None)

    def test_check_requires_a_private_credential_file(self):
        key = os.path.join(self.directory, "key.json")
        with open(key, "w", encoding="utf-8") as handle:
            handle.write("{}")
        os.chmod(key, 0o600)
        policy = _policy(self.directory, env={"GOOGLE_APPLICATION_CREDENTIALS": key})
        gate._check_policy(policy, None)
        os.chmod(key, 0o644)
        with self.assertRaisesRegex(gate.PolicyError, "0600"):
            gate._check_policy(policy, None)

    def test_broker_serve_runs_the_full_check_first(self):
        order = []
        with mock.patch.object(gate, "load_policy", return_value=object()), \
             mock.patch.object(gate, "_broker_config", return_value=object()), \
             mock.patch.object(gate, "_check_policy",
                               side_effect=lambda policy, cfg: order.append("check")
                               or {"rules": 1, "canon": "c"}), \
             mock.patch.object(gate, "serve",
                               side_effect=lambda policy, cfg: order.append("serve") or 7), \
             mock.patch("builtins.print"):
            self.assertEqual(gate.broker_main(["serve"]), 7)
        self.assertEqual(order, ["check", "serve"])

    def test_broker_check_prints_a_json_report(self):
        report = {"rules": 2, "canon": "x", "policy": "p", "socket": "s",
                  "agent": "a", "database": True}
        with mock.patch.object(gate, "load_policy", return_value=object()), \
             mock.patch.object(gate, "_broker_config", return_value=object()), \
             mock.patch.object(gate, "_check_policy", return_value=report), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(gate.broker_main(["check", "--json"]), 0)
        self.assertEqual(json.loads(printed.call_args.args[0]), report)

    def test_check_refuses_a_prod_rule_without_canon(self):
        policy = _policy(self.directory, rules=[_rule(
            id="prod", kind="prod", exe="/bin/echo", argv=["workflow", "run"],
            project="nexlink", repo="manaty/nexlink")])
        cfg = SimpleNamespace(canon="", extra_canons=())
        with self.assertRaisesRegex(gate.PolicyError, "applied canon"):
            gate._check_policy(policy, cfg)


class InstallerTreeCheck(unittest.TestCase):
    """Le contrôle de l'arbre source avant le pip root (B de la revue).

    `find -perm -022` exige g+w ET o+w : un fichier root:groupe-client en 0664
    passait, alors que pip exécute ensuite son backend de build en root. Le
    test lit l'expression exacte d'install.sh et la rejoue sur un arbre
    temporaire ; seul le bras de propriété est neutralisé, faute de root dans
    le test, pour que le mode décide.
    """

    def _command(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "deploy", "gate", "install.sh")
        with open(path, encoding="utf-8") as handle:
            script = handle.read()
        match = re.search(r'unsafe=\$\((find .*?)\) \|\|', script)
        self.assertIsNotNone(match, "install.sh: arbre source non vérifié")
        return match.group(1).replace("! -user 0", "! -user %d" % os.geteuid())

    def _unsafe_entry(self, directory):
        result = subprocess.run(
            ["bash", "-c", self._command()],
            env=dict(os.environ, SRC=directory),
            capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_a_group_only_writable_entry_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "backend.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("import os\n")
            os.chmod(path, 0o664)
            self.assertEqual(self._unsafe_entry(directory), path)

    def test_a_root_private_entry_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "backend.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("import os\n")
            os.chmod(path, 0o644)
            self.assertEqual(self._unsafe_entry(directory), "")


if __name__ == "__main__":
    unittest.main()
