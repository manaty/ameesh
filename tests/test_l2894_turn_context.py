# SPDX-License-Identifier: AGPL-3.0-only
"""TPK #2167 / lot #2894 A1 — the turn-context contract.

The runner allocates one turn id before the first side effect and hands every
turn an `ameesh-turn-context/1` document through `AMEESH_TURN_CONTEXT`, tied
to the lease and to the accounting state opened for that turn.  These tests
cover the document contract itself (schema, missing fields, atomic private
write, secret refusal) and the runner paths: a normal turn, a mail turn whose
reservation records `meta.consigne.turn`, an interrupted turn, and two chained
resident turns under the same lease.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import adapters, harnesses, mail, registry, resident_runner, storage
from ameesh import turn_context
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase
from .test_l75_resident import claude_doc


def _document(**overrides) -> dict:
    now = time.time()
    values = dict(
        turn_id="turn-1",
        agent="doc-agent",
        host="host-1",
        kind="mail",
        started_ts=now,
        session=None,
        work_item_id="4513",
        lease_owner="runner-1",
        lease_epoch=7,
        lease_expires_ts=now + 300,
        requests=[{
            "id": 12,
            "sender": "orch",
            "kind": "request",
            "created_ts": now,
            "delivered_ts": None,
            "work_item_id": "",
            "deja_consigne": False,
            "body": "hello",
            "payload": {"x": 1},
        }],
        harness="claude",
        model="claude-test",
        budget_usd=None,
        spent_usd=0.0,
        spend_key="doc-agent:1:%.6f" % now,
        marker={"start": 1, "created_ts": now, "model": "claude-test",
                "work_item_id": "4513", "turn_id": "turn-1"},
        reservation={"billing_account": "acct", "usd": 2.0,
                     "period": "2026-10-29", "state": "open"},
        generated_ts=now,
    )
    values.update(overrides)
    return turn_context.build(**values)


class TurnContextContractTest(unittest.TestCase):
    """The document is a contract, not a dump of internal state."""

    def test_the_document_matches_schema_1(self):
        document = _document()
        self.assertEqual(document["schema"], "ameesh-turn-context/1")
        self.assertEqual(document["turn"]["id"], "turn-1")
        self.assertEqual(document["turn"]["work_item_id"], "4513")
        self.assertEqual(document["lease"]["owner"], "runner-1")
        self.assertEqual(document["lease"]["epoch"], 7)
        self.assertEqual(set(document["lease"]), {"owner", "epoch", "expires_ts"})
        self.assertEqual(document["requests"][0]["id"], 12)
        # `work_item_id` is text and an empty mailbox lot is null, not "".
        self.assertIsNone(document["requests"][0]["work_item_id"])
        self.assertFalse(document["requests"][0]["redelivered"])
        self.assertEqual(document["accounting"]["marker"]["start_index"], 1)
        self.assertEqual(document["accounting"]["reservation"]["state"], "open")
        turn_context.validate(document)

    def test_unknown_schema_is_refused(self):
        document = _document()
        document["schema"] = "ameesh-turn-context/2"
        with self.assertRaises(turn_context.ContextError):
            turn_context.validate(document)
        document.pop("schema")
        with self.assertRaises(turn_context.ContextError):
            turn_context.validate(document)

    def test_missing_contract_fields_are_refused(self):
        for path in (
            ("generated_ts",),
            ("turn", "id"),
            ("turn", "agent"),
            ("turn", "kind"),
            ("lease", "owner"),
            ("lease", "epoch"),
            ("requests", 0, "id"),
            ("requests", 0, "sender"),
            ("requests", 0, "redelivered"),
            ("agent", "harness"),
            ("accounting",),
            ("accounting", "marker", "turn_id"),
            ("accounting", "reservation", "state"),
        ):
            with self.subTest(path=path):
                document = _document()
                target = document
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                with self.assertRaises(turn_context.ContextError):
                    turn_context.validate(document)

    def test_extra_optional_fields_are_accepted(self):
        document = _document()
        document["turn"]["future_optional"] = "ignored"
        document["requests"][0]["future_optional"] = {"kept": True}
        turn_context.validate(document)

    def test_write_is_private_atomic_and_removable(self):
        state = os.path.join(tempfile.mkdtemp(), "agent")
        document = _document()
        target = turn_context.write(state, document)
        self.assertEqual(target, turn_context.path(state))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["schema"], turn_context.SCHEMA)
        self.assertEqual(sorted(os.listdir(state)), ["turn-context.json"])
        turn_context.remove(state)
        self.assertFalse(os.path.exists(target))
        turn_context.remove(state)  # absent is not an error

    def test_a_symlink_at_the_target_is_replaced_not_followed(self):
        state = os.path.join(tempfile.mkdtemp(), "agent")
        os.makedirs(state, mode=0o700)
        victim = os.path.join(state, "victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("do not touch\n")
        target = turn_context.path(state)
        os.symlink(victim, target)
        turn_context.write(state, _document())
        self.assertFalse(os.path.islink(target))
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "do not touch\n")
        turn_context.remove(state)
        self.assertTrue(os.path.exists(victim))

    def test_a_secret_value_is_refused(self):
        state = os.path.join(tempfile.mkdtemp(), "agent")
        document = _document()
        secret = "sk-live-supersecret"
        document["turn"]["session"] = secret
        with self.assertRaises(turn_context.ContextError):
            turn_context.write(state, document, secrets={secret})
        self.assertFalse(os.path.exists(turn_context.path(state)))

    def test_the_secret_guard_does_not_block_the_agents_mail(self):
        """A request body is already the agent's mail: quoting a credential
        there must not stop the turn that delivers it (the document stays
        `0600` in the agent's state directory, like the mailbox)."""
        state = os.path.join(tempfile.mkdtemp(), "agent")
        secret = "sk-live-supersecret"
        document = _document(requests=[{
            "id": 1, "sender": "orch", "kind": "request", "created_ts": time.time(),
            "delivered_ts": None, "work_item_id": "", "deja_consigne": False,
            "body": "the key " + secret + " was quoted to me",
            "payload": {},
        }])
        turn_context.write(state, document, secrets={secret})
        self.assertTrue(os.path.exists(turn_context.path(state)))

    def test_secret_values_come_from_the_known_environment_names(self):
        values = turn_context.environment_secrets({
            "AMEESH_DSN": "postgresql://agent:secret@db/agent_mesh",
            "DEEPSEEK_API_KEY": "sk-deepseek-1234",
            "AMEESH_RELAY_TOKEN": "relay-token-1234",
            "AMEESH_SCHEMA": "not-a-secret",
            "SHORT_KEY": "abc",
        })
        self.assertIn("postgresql://agent:secret@db/agent_mesh", values)
        self.assertIn("sk-deepseek-1234", values)
        self.assertIn("relay-token-1234", values)
        self.assertNotIn("not-a-secret", values)
        self.assertNotIn("abc", values)  # too short to be a usable secret


class TurnContextRunnerTest(PgTestCase):
    """A real worker and the fake harness: the document handed at launch."""

    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _worker(self, name: str, *, prompt: str | None = None):
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name), prompt=prompt)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def test_current_verification_tracks_real_marker_and_lease(self):
        name = "ctx-reader"
        runner, worker = self._worker(name, prompt="read")
        document = _document(agent=name, lease_owner=runner.runner_id,
                             lease_epoch=worker.epoch)
        registry.pending_spend_begin(self.db, name, 1, turn_id="turn-1")
        turn_context.verify(document, self.db)
        document["turn"]["id"] = "old-turn"
        with self.assertRaisesRegex(turn_context.ContextError, "stale-turn"):
            turn_context.verify(document, self.db)
        document["turn"]["id"] = "turn-1"
        document["lease"]["epoch"] += 1
        with self.assertRaisesRegex(turn_context.ContextError, "lease-mismatch"):
            turn_context.verify(document, self.db)

    def _capture_env(self, capture: str, **extra: str):
        env = {
            "AMEESH_BIN_DIR": FAKEBIN,
            "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_TEST_RUN": 'cp "$AMEESH_TURN_CONTEXT" %s' % shlex.quote(capture),
        }
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def _captured(self, path: str) -> dict:
        self.assertTrue(os.path.exists(path), "the harness did not receive a context")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_prompt_turn_gets_one_id_and_a_private_document(self):
        name = "ctx-prompt"
        capture = os.path.join(self.tmp, "ctx-prompt.json")
        _runner, worker = self._worker(name, prompt="travail")
        with self._capture_env(capture):
            spec = worker.pick()
            self.assertEqual(spec["kind"], "prompt")
            self.assertTrue(worker.run_turn(spec))
        document = self._captured(capture)
        turns = [entry for entry in self.turns()
                 if (entry.get("env") or {}).get("AMEESH_TURN_ID")]
        self.assertTrue(turns)
        env = turns[-1]["env"]
        self.assertEqual(document["schema"], turn_context.SCHEMA)
        self.assertEqual(document["turn"]["id"], env["AMEESH_TURN_ID"])
        self.assertEqual(document["turn"]["agent"], name)
        self.assertEqual(document["turn"]["kind"], "prompt")
        self.assertEqual(document["turn"]["work_item_id"], None)
        self.assertEqual(document["lease"]["owner"], worker.runner.runner_id)
        self.assertEqual(document["lease"]["epoch"], worker.epoch)
        self.assertEqual(document["agent"]["name"], name)
        self.assertEqual(document["agent"]["harness"], "claude")
        self.assertEqual(document["requests"], [])
        # the same id is the one persisted in `turn_resources`
        resource = storage.of(self.db).turn_resources.get(document["turn"]["id"])
        self.assertIsNotNone(resource)
        self.assertEqual(resource["agent"], name)
        path = env[turn_context.ENV]
        self.assertTrue(os.path.isabs(path))
        self.assertEqual(path, os.path.join(self.state, name, "turn-context.json"))
        # the document lives exactly the turn
        self.assertFalse(os.path.exists(path))

    def test_a_mail_turn_records_the_turn_on_the_reservation(self):
        name = "ctx-mail"
        capture = os.path.join(self.tmp, "ctx-mail.json")
        _runner, worker = self._worker(name)
        message_id = mail.send(self.db, "orch", name, "corps du message")
        with self._capture_env(capture):
            spec = worker.pick()
            self.assertEqual(spec["kind"], "mail")
            self.assertTrue(worker.run_turn(spec))
        document = self._captured(capture)
        self.assertEqual(document["turn"]["kind"], "mail")
        self.assertEqual(len(document["requests"]), 1)
        request = document["requests"][0]
        self.assertEqual(request["id"], message_id)
        self.assertEqual(request["sender"], "orch")
        self.assertEqual(request["body"], "corps du message")
        self.assertIsNone(request["delivered_ts"])     # written before launch
        self.assertFalse(request["redelivered"])
        # the same id went into the reservation, before delivery
        line = mail.get(self.db, message_id)
        self.assertEqual(line["consigne_turn"], document["turn"]["id"])
        self.assertIsNotNone(line["delivered_ts"])
        self.assertEqual(document["accounting"]["marker"]["turn_id"],
                         document["turn"]["id"])
        self.assertTrue(document["accounting"]["spend_key"])

    def test_an_interrupted_turn_removes_the_document(self):
        name = "ctx-interrupted"
        capture = os.path.join(self.tmp, "ctx-interrupted.json")
        _runner, worker = self._worker(name, prompt="meurt")
        with self._capture_env(capture, AMEESH_TEST_KILL_SELF="1"):
            spec = worker.pick()
            self.assertFalse(worker.run_turn(spec))
        # the fake harness copied the document before killing itself
        document = self._captured(capture)
        self.assertEqual(document["turn"]["agent"], name)
        path = turn_context.path(worker.state_dir)
        self.assertFalse(os.path.exists(path))
        row = registry.get(self.db, name)
        self.assertNotEqual(row["status"], "running")

    def test_a_secret_in_a_runner_field_is_not_written(self):
        """The runner refuses to launch rather than write a live credential."""
        name = "ctx-secret"
        _runner, worker = self._worker(name)
        secret = "sk-runner-live-0123456789"
        fake_env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
                    "AMEESH_TEST_SECRET_KEY": secret}
        original = turn_context.build

        def leaky(**fields):
            document = original(**fields)
            document["agent"]["model"] = secret
            return document

        with mock.patch.dict(os.environ, fake_env, clear=False):
            with mock.patch.object(turn_context, "environment_secrets",
                                   return_value={secret}):
                message_id = mail.send(self.db, "orch", name, "message")
                spec = worker.pick()
                self.assertIsNotNone(spec)
                self.assertEqual(spec["kind"], "mail")
                with mock.patch.object(turn_context, "build", side_effect=leaky):
                    with mock.patch.object(turn_context, "write",
                                           wraps=turn_context.write) as write:
                        self.assertFalse(worker.run_turn(spec))
                self.assertTrue(write.called)
        self.assertFalse(os.path.exists(turn_context.path(worker.state_dir)))
        # nothing was left open for the next turn: the mail stays deliverable
        self.assertIsNone(mail.get(self.db, message_id)["delivered_ts"])
        self.assertIsNone(registry.pending_spend_get(self.db, name))
        self.assertEqual([entry for entry in self.turns()
                          if entry.get("harness")], [])


class ResidentTurnContextTest(PgTestCase):
    """A resident process serves A then B under the same lease."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending, turn_resources CASCADE")
        self.log = os.path.join(self.tmp, "resident-trace.jsonl")
        path = os.path.join(self.tmp, "fake-claude.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(claude_doc(), handle)
        self.adapter = adapters.HarnessAdapter(harnesses.load(path, source="test"),
                                               sys.executable)
        for patch in (mock.patch.dict(os.environ, {"FAKE_RESIDENT_LOG": self.log}),
                      mock.patch.object(resident_runner, "binary_version",
                                        return_value="99.0.0")):
            patch.start()
            self.addCleanup(patch.stop)

    def _worker(self, name: str = "ctx-resident"):
        runner = Runner(self.cfg, self.db, once=False)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker._close_resident, "end of test")
        worker._adapter = lambda: self.adapter
        os.makedirs(worker.state_dir, exist_ok=True)
        with open(os.path.join(worker.state_dir, "resident"), "w", encoding="utf-8") as fh:
            fh.write("on")
        return runner, worker

    def _trace(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _turn(self, worker, prompt: str) -> bool:
        return worker.run_turn({"kind": "prompt", "prompt": prompt, "ids": []})

    def test_a_to_b_rewrite_the_document_under_the_same_lease(self):
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "premier"))
        process = worker.resident
        self.assertIsNotNone(process)
        self.assertFalse(os.path.exists(turn_context.path(worker.state_dir)))
        self.assertEqual(
            resident_runner.current_turn({
                resident_runner.TURN_FILE_ENV:
                    worker._path(resident_runner.TURN_FILE)}), "")
        self.assertTrue(self._turn(worker, "second"))
        self.assertIs(worker.resident, process)
        self.assertEqual(process.turns, 2)
        self.assertFalse(os.path.exists(turn_context.path(worker.state_dir)))
        documents = [entry["turn_context"] for entry in self._trace()
                     if "turn_context" in entry]
        self.assertEqual(len(documents), 2)
        first, second = documents
        self.assertNotEqual(first["turn"]["id"], second["turn"]["id"])
        self.assertEqual(first["turn"]["id"], first["accounting"]["marker"]["turn_id"])
        self.assertEqual(second["turn"]["id"], second["accounting"]["marker"]["turn_id"])
        for document in documents:
            self.assertEqual(document["lease"]["owner"], worker.runner.runner_id)
            self.assertEqual(document["lease"]["epoch"], worker.epoch)
            self.assertEqual(document["turn"]["agent"], "ctx-resident")
            self.assertEqual(document["requests"], [])
        self.assertGreaterEqual(second["generated_ts"], first["generated_ts"])
        self.assertEqual(
            resident_runner.current_turn({
                resident_runner.TURN_FILE_ENV:
                    worker._path(resident_runner.TURN_FILE)}), "")


if __name__ == "__main__":
    unittest.main()


class TurnCurrentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.document = _document()
        filename = turn_context.write(self.tmp.name, self.document)
        self.env = {"AMEESH_TURN_CONTEXT": filename, "AMEESH_TURN_ID": "turn-1",
                    "AGENT_MAIL_NAME": "doc-agent", "AMEESH_RUNNER_ID": "runner-1",
                    "AMEESH_LEASE_EPOCH": "7"}

    def call(self, *args):
        import contextlib
        import io
        from ameesh import main
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main._dispatch(["turn", "current", "--json", *args])
        return code, out.getvalue(), err.getvalue()

    def test_local_never_opens_backend(self):
        with mock.patch("ameesh.backend.open_backend", side_effect=AssertionError):
            code, out, _ = self.call("--local")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.document)

    def test_resident_boundary_fences_lingering_operation_before_database(self):
        pointer = os.path.join(self.tmp.name, "turn-id")
        self.env["AMEESH_TURN_ID_FILE"] = pointer
        for turn in ("turn-1", "turn-2"):
            with open(pointer, "w") as handle:
                handle.write(turn)
            self.document["turn"]["id"] = turn
            self.document["accounting"]["marker"]["turn_id"] = turn
            turn_context.write(self.tmp.name, self.document)
            with mock.patch("ameesh.registry.lease_matches", return_value=(True, "")), \
                    mock.patch("ameesh.registry.pending_spend_get", return_value={"turn_id": turn}):
                turn_context.verify(self.document, object())
            self.assertEqual(self.call("--local", "--expect-turn", turn)[0], 0)
        for flags in ((), ("--local",)):
            with mock.patch("ameesh.backend.open_backend", side_effect=AssertionError):
                code, out, err = self.call(*flags, "--expect-turn", "turn-1")
            self.assertEqual((code, out), (3, ""))
            self.assertIn("turn-mismatch", err)
        # Empty/missing resident pointer cannot fall back to the lifetime id.
        for missing in (False, True):
            with open(pointer, "w") as handle:
                handle.write("")
            if missing:
                os.unlink(pointer)
            self.assertEqual(self.call("--local")[0], 3)

    def test_invalid_documents_and_identity_fail_closed(self):
        for field, value in (("AMEESH_TURN_CONTEXT", "/absent/context"),
                             ("AMEESH_TURN_ID", "old"),
                             ("AMEESH_RUNNER_ID", "other"),
                             ("AMEESH_LEASE_EPOCH", "bad"),
                             ("AGENT_MAIL_NAME", "other")):
            with self.subTest(field=field):
                saved = self.env[field]
                self.env[field] = value
                code, out, _ = self.call("--local")
                self.assertEqual((code, out), (3, ""))
                self.env[field] = saved
        self.document["schema"] = "unknown/2"
        with open(self.env["AMEESH_TURN_CONTEXT"], "w") as handle:
            json.dump(self.document, handle)
        self.assertEqual(self.call("--local")[:2], (3, ""))

    def test_verified_command_and_database_failures(self):
        backend = mock.Mock(kind="pg")
        with mock.patch("ameesh.backend.open_backend", return_value=(backend, "")), \
                mock.patch("ameesh.registry.lease_matches", return_value=(True, "")), \
                mock.patch("ameesh.registry.pending_spend_get", return_value={"turn_id": "turn-1"}):
            self.assertEqual(self.call()[0], 0)
        for live, marker in ((False, {"turn_id": "turn-1"}), (True, None),
                             (True, {"turn_id": "old"})):
            with mock.patch("ameesh.backend.open_backend", return_value=(backend, "")), \
                    mock.patch("ameesh.registry.lease_matches", return_value=(live, "")), \
                    mock.patch("ameesh.registry.pending_spend_get", return_value=marker):
                self.assertEqual(self.call()[:2], (3, ""))
        with mock.patch("ameesh.backend.open_backend", side_effect=RuntimeError("secret-dsn")):
            code, out, err = self.call()
        self.assertEqual((code, out), (3, ""))
        self.assertNotIn("secret-dsn", err)

    def test_usage_errors(self):
        with mock.patch("ameesh.cli.config_mod.load", side_effect=ValueError("secret-path")):
            code, out, err = self.call()
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn("secret-path", err)
        for args in (("--expect-turn",), ("--unknown",)):
            self.assertEqual(self.call(*args)[0], 2)
