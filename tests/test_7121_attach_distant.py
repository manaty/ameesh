# SPDX-License-Identifier: AGPL-3.0-only
"""#7121 (avatar D3) : attach distant — bail, attente, journal A0, copie enfant.

Le transport (D2) et le grant (D1) ont leurs propres témoins. Ici : le bail
`attach:<tenant>:<appareil>` du wrapper hôte, l'attente de tour bornée, les
événements `agent.attach` accepté/refusé, la page de journal imprimée avant
le harnais, l'auteur humain du grant dans A0 et la copie enfant vers le
parent. Le poste (`attach_distant`) est couvert par ses fichiers et son reçu ;
la session relais complète par le banc D2.
"""
from __future__ import annotations

import io
import json
import logging
import os
import select
import signal
import struct
import threading
import time
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ameesh import attach_distant as D, attach_host, attach_journal as A
from ameesh import attach_relay, attach_session as S, conversation, registry
from ameesh.identity import Binding
from ameesh.runner import run_attach

from .support import FAKEBIN, PgTestCase
from .test_7120_attach_relay import (AGENT, COMMAND, HOST, HOST_PUBLIC_KEY,
                                     HOST_SIGNING_KEY, TOKEN, FakeVerifier,
                                     _ObservedConnector, _wait_host_connected,
                                     make_grant)

SID = "sess-attach-7121"
OWNER = "attach:t1:cred-7121"
ATT = "att_" + "a" * 26


class RelayHostCliTest(unittest.TestCase):
    def test_host_attach_command_uses_the_paired_agent_and_max(self):
        from types import SimpleNamespace
        from ameesh import attach_relay_cli
        argv = attach_relay_cli._host_attach_command(
            SimpleNamespace(agent=AGENT, max_seconds=30))
        self.assertIn("--host-attach", argv)
        self.assertIn("30s", argv)
        self.assertEqual(argv[argv.index("--max") + 1], "30s")
        self.assertEqual(argv[argv.index("--host-attach") + 1], AGENT)

    def test_command_and_host_attach_are_exclusive(self):
        from types import SimpleNamespace
        from ameesh import attach_relay_cli
        args = SimpleNamespace(command="true", host_attach=True)
        self.assertEqual(attach_relay_cli.cmd_host(args), 2)


class _StationSigner:
    """La clé d'appareil du banc D2, vue par le poste D3."""

    def __init__(self, soft):
        self.credential_id = soft.credential_id
        self._soft = soft

    def sign(self, message):
        return self._soft.key.sign(message)


class StationSessionTest(unittest.TestCase):
    """Le poste D3 contre le vrai relais D2 et un hôte à commande d'écho."""

    def setUp(self):
        self.relay = None
        self.connector = None
        self.thread = None
        self.logger = logging.getLogger("test.7121.station")
        self.logger.handlers[:] = []
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def tearDown(self):
        if self.connector is not None:
            self.connector.stop()
        if self.thread is not None:
            self.thread.join(2.0)
        if self.relay is not None:
            self.relay.stop()

    def _start(self):
        self.verifier = FakeVerifier()
        self.relay = attach_relay.RelayServer(attach_relay.RelayConfig(
            listen_host="127.0.0.1", listen_port=0, hosts={HOST: TOKEN},
            plaintext_loopback=True,
            grant_checker=lambda document, expected: (True, "", ""),
            pair_timeout=0.3, idle_timeout=10.0, max_session_seconds=10.0,
            max_sessions=8, max_sessions_per_host=4, max_sessions_per_ip=8),
            logger=self.logger)
        self.relay.start()
        self.connector = _ObservedConnector(attach_host.HostConfig(
            relay_host="127.0.0.1", relay_port=self.relay.port, token=TOKEN,
            host=HOST, agents=(AGENT,), plaintext_loopback=True, command=COMMAND,
            host_signing_key=HOST_SIGNING_KEY, max_sessions=4,
            max_session_seconds=10.0, keepalive=1.0), self.verifier,
            logger=self.logger)
        self.thread = threading.Thread(target=self.connector.run_forever, daemon=True)
        self.thread.start()
        _wait_host_connected(self.connector, self.thread)

    def test_station_opens_requests_and_pumps_bytes(self):
        self._start()
        receipt, device = make_grant()
        config = D.StationConfig(
            agent=AGENT, host=HOST, tenant="",
            relay="http://127.0.0.1:%d" % self.relay.port, receipt=receipt,
            device=_StationSigner(device), host_key=HOST_PUBLIC_KEY,
            wait=True, rows=24, cols=80, term="xterm", plaintext_loopback=True,
            max_seconds=30)
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        self.addCleanup(os.close, stdin_r)
        self.addCleanup(os.close, stdout_r)
        result = []
        worker = threading.Thread(
            target=lambda: result.append(D.run_session(
                config, stdin_fd=stdin_r, stdout_fd=stdout_w)), daemon=True)
        worker.start()
        try:
            os.write(stdin_w, b"bonjour 7121\n")
            output = bytearray()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and b"echo:bonjour 7121" not in output:
                ready, _, _ = select.select([stdout_r], [], [], 0.2)
                if ready:
                    output.extend(os.read(stdout_r, 4096))
            self.assertIn(b"echo:bonjour 7121", output)
            self.assertEqual(self.verifier.requests[0].max_seconds, 30)
            os.close(stdin_w)
            stdin_w = None
            worker.join(10.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [0])
        finally:
            if stdin_w is not None:
                os.close(stdin_w)
            os.close(stdout_w)
            worker.join(5.0)

class _FakeChannel:
    ready = True
    closed = False

    def seal(self, ftype, payload=b""):
        return payload

    def ack_frame(self):
        return None

    def close_frame(self, code="normal", message=""):
        return 0, b""


class _FakeSocket:
    def __init__(self):
        self.frames = []

    def sendall(self, data):
        self.frames.append(bytes(data))

    def close(self):
        pass


class PumpResizeTest(unittest.TestCase):
    def test_two_successive_resizes_send_the_new_size(self):
        config = D.StationConfig(
            agent=AGENT, host=HOST, tenant="", relay="http://127.0.0.1:1",
            receipt={"request": {"attach_id": ATT}}, device=None, host_key=b"",
            rows=24, cols=80)
        session = D.Session(config)
        session.sock = _FakeSocket()
        session.channel = _FakeChannel()
        session.session = b"\x00" * 16
        stop, resize = threading.Event(), threading.Event()
        resize.set()
        sizes = [(40, 120, 0, 0), (50, 132, 0, 0)]
        rounds = {"n": 0}

        def fake_select(_read, _write, _error, _timeout):
            rounds["n"] += 1
            if rounds["n"] == 1:
                resize.set()
            elif rounds["n"] >= 2:
                stop.set()
            return [], [], []

        with mock.patch.object(D, "_read_terminal_size", side_effect=sizes), \
                mock.patch.object(D.select, "select", side_effect=fake_select):
            self.assertEqual(session.pump(stdin_fd=None, stdout_fd=1,
                                          stop=stop, resize=resize), 0)
        sent = b"".join(session.sock.frames)
        self.assertIn(struct.pack(">HHHH", 40, 120, 0, 0), sent)
        self.assertIn(struct.pack(">HHHH", 50, 132, 0, 0), sent)

class RemoteOwnerTest(unittest.TestCase):
    def test_owner_shape_and_tenant(self):
        self.assertEqual(S.remote_owner("", "cred-1"), "attach:-:cred-1")
        self.assertEqual(S.remote_owner("t1", "cred-1"), "attach:t1:cred-1")
        with self.assertRaises(S.SessionError):
            S.remote_owner("bad tenant", "cred-1")
        with self.assertRaises(S.SessionError):
            S.remote_owner("t1", "bad device\n")

    def test_long_owner_stays_a_bounded_routing_identity(self):
        owner = S.remote_owner("t" * 128, "c" * 1024)
        self.assertLessEqual(len(owner), 240)
        self.assertIn("~", owner)  # le credential long est distingué par digest
        self.assertIsNotNone(S.OWNER_RE.fullmatch(owner))

    def test_env_metadata_is_strict(self):
        good = {"AMEESH_ATTACH_HUMAN": "human:alice",
                "AMEESH_ATTACH_TENANT": "t1", "AMEESH_ATTACH_DEVICE": "cred-1",
                "AMEESH_ATTACH_ID": ATT, "AMEESH_ATTACH_WAIT": "1"}
        with mock.patch.dict(os.environ, good):
            self.assertEqual(S.metadata_from_env(),
                             {"tenant": "t1", "device": "cred-1", "attach_id": ATT,
                              "human": "human:alice"})
            self.assertTrue(S.wait_from_env())
        for key, value in (("AMEESH_ATTACH_ID", "att_short"),
                           ("AMEESH_ATTACH_DEVICE", "bad device"),
                           ("AMEESH_ATTACH_TENANT", "bad tenant")):
            env = dict(good, **{key: value})
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(S.SessionError):
                    S.metadata_from_env()

    def test_unknown_human_is_ignored_not_trusted(self):
        with mock.patch.dict(os.environ, {"AMEESH_ATTACH_HUMAN": "not-a-human"}):
            self.assertEqual(S.human_from_env(), "")


class StationFilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.abspath(os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                                "test-7121-%d" % os.getpid()))
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.dir):
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def _write(self, name, data, mode=0o600):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(path, mode)
        return path

    def _key(self):
        return ec.generate_private_key(ec.SECP256R1())

    def test_device_key_pem_and_scalar(self):
        key = self._key()
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        path = self._write("device.json", {"credential_id": "cred-1", "private_key": pem})
        loaded = D.load_device_key(path)
        self.assertEqual(loaded.credential_id, "cred-1")
        key.public_key().verify(loaded.sign(b"message"), b"message",
                                ec.ECDSA(hashes.SHA256()))
        scalar = key.private_numbers().private_value.to_bytes(32, "big")
        path = self._write("device2.json", {"credential_id": "cred-2",
                                            "private_key": D.receipts.b64u_encode(scalar)})
        self.assertEqual(D.load_device_key(path).credential_id, "cred-2")

    def test_device_key_refuses_open_permissions(self):
        path = self._write("device.json", {"credential_id": "cred-1", "private_key": ""},
                           mode=0o644)
        with self.assertRaises(D.StationError) as seen:
            D.load_device_key(path)
        self.assertEqual(seen.exception.code, "file")
        self.assertIn("chmod 600", str(seen.exception))

    def test_host_key_forms(self):
        raw = bytes(range(33, 65))
        self.assertEqual(D.parse_host_key(raw.decode("latin-1")), raw)
        self.assertEqual(D.parse_host_key(raw.hex()), raw)
        self.assertEqual(D.parse_host_key(D.receipts.b64u_encode(raw)), raw)
        with self.assertRaises(D.StationError):
            D.parse_host_key("short")

    def test_receipt_bound_to_agent_host_tenant_device(self):
        receipt, device = make_grant(agent=AGENT, host=HOST, tenant="")
        D.validate_receipt(receipt, agent=AGENT, host=HOST, tenant="",
                           device_key=device.credential_id)
        for field, value in (("agent", "another"), ("host", "elsewhere"),
                             ("tenant", "t1"), ("device_key", "cred-other")):
            kwargs = dict(agent=AGENT, host=HOST, tenant="",
                          device_key=device.credential_id)
            kwargs[field] = value
            with self.assertRaises(D.StationError) as seen:
                D.validate_receipt(receipt, **kwargs)
            self.assertEqual(seen.exception.code, "receipt", field)

    def test_receipt_expired_and_too_long_lived(self):
        receipt, device = make_grant(exp=int(time.time()) - 1)
        with self.assertRaises(D.StationError):
            D.validate_receipt(receipt, agent=AGENT, host=HOST, tenant="",
                               device_key=device.credential_id)
        receipt, device = make_grant()
        receipt["request"]["iat"] -= 10_000
        with self.assertRaises(D.StationError):
            D.validate_receipt(receipt, agent=AGENT, host=HOST, tenant="",
                               device_key=device.credential_id)


class HostAttachTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.event_start = int(self.db.query(
            "SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]["id"])
        self.clear_turns()
        self.register("att7121", "claude", cwd=self.tmp)
        patch = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                             "AMEESH_TEST_LOG": self.turns_log})
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, **kwargs):
        options = dict(ttl=30, max_seconds=30, owner=OWNER, human="human:alice",
                       attach_id=ATT, tenant="t1", device="cred-7121", remote=True)
        options.update(kwargs)
        return run_attach(self.cfg, self.db, "att7121", **options)

    def _events(self):
        rows = self.db.query("SELECT type, object, data FROM ameesh_events"
                             " WHERE type = 'agent.attach' AND id > %s ORDER BY id",
                             (self.event_start,))
        return [(row["type"], row["object"], row["data"]) for row in rows]

    def test_accepted_event_lease_and_journal_page(self):
        log = conversation.Log(self.db, self.cfg, agent="att7121")
        log.append("human_message", "bonjour 7121", author="human:alice")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(self._run(), 0)
        self.assertIn("bonjour 7121", out.getvalue())
        row = registry.get(self.db, "att7121")
        self.assertIsNone(row["lease_owner"])  # bail rendu à la sortie
        events_rows = self._events()
        self.assertEqual(len(events_rows), 1)
        data = events_rows[0][2]
        self.assertEqual(events_rows[0][1], "agent:att7121")
        self.assertEqual((data["outcome"], data["lease_owner"], data["human"],
                          data["tenant"], data["device"], data["attach_id"]),
                         ("accepted", OWNER, "human:alice", "t1", "cred-7121", ATT))
        self.assertNotIn("receipt", json.dumps(data))
        self.assertNotIn("signature", json.dumps(data))
        self.assertNotIn("nonce", json.dumps(data))

    def test_turn_in_progress_is_refused_without_wait(self):
        registry.claim(self.db, "att7121", "runner-before", 60, host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET status = 'running'"
                        " WHERE name = 'att7121'")
        self.assertEqual(self._run(wait=False, max_seconds=5), 3)
        data = self._events()[0][2]
        self.assertEqual((data["outcome"], data["code"]),
                         ("refused", "turn_in_progress"))
        self.assertNotIn("attach:", registry.get(self.db, "att7121")["lease_owner"] or "")

    def test_wait_lets_the_turn_finish_then_takes_the_lease(self):
        registry.claim(self.db, "att7121", "runner-before", 60, host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET status = 'running',"
                        " session_id = 'before-7121' WHERE name = 'att7121'")
        original_sleep = time.sleep

        def finish(seconds):
            if seconds == 2.0:
                self.db.execute("UPDATE agent_registry SET status = 'idle',"
                                " session_id = 'after-7121' WHERE name = 'att7121'")
            else:
                original_sleep(min(seconds, 0.01))

        with mock.patch("ameesh.runner.time.sleep", side_effect=finish):
            self.assertEqual(self._run(wait=True), 0)
        self.assertIn("after-7121", self.turns()[0]["argv"])
        self.assertEqual(self._events()[0][2]["outcome"], "accepted")
        self.assertIsNone(registry.get(self.db, "att7121")["lease_owner"])

    def test_stop_signal_kills_the_harness_and_returns_the_lease(self):
        # Coupure du relais : `attach_host` ferme le PTY (SIGTERM/SIGHUP) ; le
        # wrapper doit arrêter le groupe du harnais et rendre le bail.
        handlers = {}
        armed = threading.Event()

        def catch(signum, handler):
            if callable(handler) and signum in (signal.SIGTERM, signal.SIGHUP,
                                                signal.SIGINT):
                handlers[signum] = handler
                if len(handlers) == 3:
                    armed.set()
            return signal.SIG_DFL

        def stopper():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                row = registry.get(self.db, "att7121") or {}
                if row.get("lease_owner") == OWNER and self.turns():
                    break
                time.sleep(0.05)
            self.assertTrue(armed.wait(5), "the stop handlers were not armed")
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        killer = threading.Thread(target=stopper, daemon=True)
        killer.start()
        with mock.patch("ameesh.runner.signal.signal", side_effect=catch), \
                mock.patch.dict(os.environ, {"AMEESH_TEST_SLEEP": "20"}):
            code = self._run(max_seconds=30)
        killer.join(5)
        self.assertEqual(code, 0)
        self.assertIsNone(registry.get(self.db, "att7121")["lease_owner"])

    def test_unknown_agent_is_an_anonymous_refusal(self):
        self.assertEqual(run_attach(self.cfg, self.db, "absent7121", owner=OWNER,
                                    human="human:alice", attach_id=ATT,
                                    tenant="t1", device="cred-7121",
                                    remote=True), 1)
        data = self._events()[0][2]
        self.assertEqual((data["outcome"], data["code"]), ("refused", "unknown_agent"))


class AttachJournalHumanTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("avatar", "kid"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET ephemeral = true,"
                        " ephemeral_expires_at = now() + interval '1 hour',"
                        " created_by = 'avatar' WHERE name = 'kid'")

    def _capture(self, name, text, human="human:alice"):
        binding = Binding(name=name, source="runner", runner_id=OWNER, ok=True)
        env = {"AMEESH_ATTACH": "1"}
        if human:
            env["AMEESH_ATTACH_HUMAN"] = human
        with mock.patch.dict(os.environ, env):
            return A.capture(self.cfg, self.db, binding, "UserPromptSubmit",
                             {"session_id": SID, "prompt": text})

    def _rows(self, name):
        return self.db.query("SELECT kind, author, text, meta FROM conversation_log"
                             " WHERE agent = %s ORDER BY seq", (name,))

    def test_grant_human_is_the_a0_author_not_the_lease_suffix(self):
        entry = self._capture("avatar", "prompt distant")
        self.assertIsNotNone(entry)
        self.assertEqual(self._rows("avatar")[0]["author"], "human:alice")

    def test_child_message_is_copied_to_the_parent(self):
        entry = self._capture("kid", "message enfant")
        self.assertIsNotNone(entry)
        parent = self._rows("avatar")
        self.assertEqual(len(parent), 1)
        self.assertEqual((parent[0]["kind"], parent[0]["author"], parent[0]["text"]),
                         ("input", "human:alice", "message enfant"))
        self.assertEqual(parent[0]["meta"]["child"], "kid")
        self.assertEqual(parent[0]["meta"]["copied_from"], entry["seq"])

    def test_child_copy_respects_the_same_responsible(self):
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice'"
                        " WHERE name = 'avatar'")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:bob'"
                        " WHERE name = 'kid'")
        self._capture("kid", "privé")
        self.assertEqual(self._rows("avatar"), [])


if __name__ == "__main__":
    unittest.main()
