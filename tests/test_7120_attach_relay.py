# SPDX-License-Identifier: AGPL-3.0-only
"""#7120 (avatar D2) : relais d'attach réservé au réseau du mesh.

Service, connexion sortante de l'hôte, PTY, trames bornées, E2E
X25519/AES-GCM, quotas, journaux sans contenu. Aucune base n'est requise :
la porte de grant est injectée ; D1 (`attach_grant`) est couvert par ses
propres tests. Aucun réseau externe : tout tourne sur 127.0.0.1.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import attach_grant, attach_host, attach_pty, attach_relay
from ameesh import attach_transport as t
from ameesh import receipts

from .webauthn_soft import ORIGIN, RP_ID, SoftDevice, SoftWebAuthn

TOKEN = "relay-token-" + "a" * 40
HOST = "ameesh-agents-1"
AGENT = "avatar-alice"
TENANT = ""
POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))
#: un shell qui répond et reste vivant (le PTY est le terminal)
COMMAND = ["/bin/sh", "-c", 'while IFS= read -r line; do printf "echo:%s\\n" "$line"; done']


def make_grant(*, agent=AGENT, host=HOST, tenant=TENANT, ttl=300, exp=None,
               web=None, device=None):
    web = web or SoftWebAuthn("ES256")
    device = device or SoftDevice()
    request, _summary = attach_grant.build_request(
        approver="human:alice", agent=agent, host=host, tenant=tenant,
        device_key=device.credential_id, requested_by="human:alice", ttl=ttl)
    if exp is not None:
        request["iat"] = exp - 290
        request["exp"] = exp
    receipt = web.receipt(dict(request, decision="approve"))
    return receipt, device


def context_for(receipt, session, *, agent=AGENT, host=HOST, tenant=TENANT):
    return t.Context(session=session, agent=agent, host=host, tenant=tenant,
                     attach_id=receipt["request"]["attach_id"],
                     digest=t.receipt_digest(receipt))


class FakeVerifier(attach_host.GrantVerifier):
    def __init__(self, result=None):
        self.requests = []
        self.result = result or attach_host.GrantResult(
            True, "ok", "granted", human="human:alice")

    def verify(self, request):
        self.requests.append(request)
        if callable(self.result):
            return self.result(request)
        return self.result


class Station:
    """Le poste de test : OPEN, poignée de main, REQ, DATA (comme D3 le fera)."""

    def __init__(self, port, receipt, device, *, agent=AGENT, host=HOST, tenant=TENANT,
                 tls_context=None, timeout=5.0):
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        if tls_context is not None:
            raw = tls_context.wrap_socket(raw, server_hostname="127.0.0.1")
        raw.settimeout(timeout)
        self.sock = raw
        self.receipt = receipt
        self.device = device
        self.sock.sendall(t.encode_frame(t.ZERO_SESSION, t.OPEN, t.json_payload(
            v=1, role="station", agent=agent, host=host, tenant=tenant, receipt=receipt)))
        frame = self._read(timeout)
        if frame is None:
            raise AssertionError("relay closed before PAIRED")
        if frame.type != t.PAIRED:
            raise AssertionError("PAIRED expected, got %d: %r"
                                 % (frame.type, frame.payload[:120]))
        data = t.parse_json(frame.payload, "PAIRED")
        self.session = bytes.fromhex(data["session"])
        self.channel = t.Channel(t.ROLE_STATION, context_for(
            receipt, self.session, agent=agent, host=host, tenant=tenant))
        self._confirm_sent = False

    def _read(self, timeout=5.0):
        self.sock.settimeout(timeout)
        return t.read_frame(self.sock.recv)

    def _send(self, ftype, payload):
        self.sock.sendall(t.encode_frame(self.session, ftype, payload))

    def handshake(self, timeout=5.0):
        self._send(*self.channel.key_frame())
        deadline = time.monotonic() + timeout
        while not self.channel.ready:
            if time.monotonic() > deadline:
                raise AssertionError("handshake timeout")
            frame = self._read(max(0.1, deadline - time.monotonic()))
            if frame is None:
                raise AssertionError("connection closed during handshake")
            events = self.channel.feed(frame.type, frame.payload)
            if any(kind == "key" for kind, _ in events) and not self._confirm_sent:
                self._send(*self.channel.confirm_frame())
                self._confirm_sent = True

    def request(self, *, term="xterm", rows=24, cols=80, timeout=5.0):
        signature = self.device.key.sign(attach_grant.device_message(
            receipts.challenge(self.receipt["request"])))
        payload = t.json_payload(
            receipt=self.receipt,
            device_signature=receipts.b64u_encode(signature),
            device_key=self.device.credential_id, rows=rows, cols=cols,
            xpix=0, ypix=0, term=term)
        self._send(t.REQ, self.channel.seal(t.REQ, payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._read(max(0.1, deadline - time.monotonic()))
            if frame is None:
                raise AssertionError("connection closed before the answer")
            for kind, value in self.channel.feed(frame.type, frame.payload):
                if kind == "accepted":
                    return value
                if kind == "refused":
                    return value
        raise AssertionError("no answer to REQ")

    def send_data(self, data: bytes):
        self._send(t.DATA, self.channel.seal(t.DATA, data))

    def read_data(self, timeout=3.0, *, until: bytes | None = None) -> bytes:
        output = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self._read(max(0.05, deadline - time.monotonic()))
            except socket.timeout:
                continue
            if frame is None:
                break
            for kind, value in self.channel.feed(frame.type, frame.payload):
                if kind == "data":
                    output.extend(value)
                    ack = self.channel.ack_frame(force=True)
                    if ack is not None:
                        self._send(t.ACK, self.channel.seal(t.ACK, ack[1]))
            if until is not None and until in output:
                break
        return bytes(output)

    def resize(self, rows, cols):
        self._send(t.WINSIZE, self.channel.seal(t.WINSIZE,
                                                struct.pack(">HHHH", rows, cols, 0, 0)))

    def close(self):
        try:
            if self.channel.ready and not self.channel.closed:
                self._send(*self.channel.close_frame("normal"))
        except (OSError, t.ProtocolError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def open_only(port, receipt, *, agent=AGENT, host=HOST, tenant=TENANT):
    """Envoie OPEN et rend la première trame (pour les refus du relais)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(5)
    sock.sendall(t.encode_frame(t.ZERO_SESSION, t.OPEN, t.json_payload(
        v=1, role="station", agent=agent, host=host, tenant=tenant, receipt=receipt)))
    frame = t.read_frame(sock.recv)
    sock.close()
    return frame


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.receipt, self.device = make_grant()
        self.session = os.urandom(16)
        self.ctx = context_for(self.receipt, self.session)
        self.station = t.Channel(t.ROLE_STATION, self.ctx)
        self.host = t.Channel(t.ROLE_HOST, self.ctx)

    def test_handshake_data_ack_and_close(self):
        t.handshake(self.station, self.host)
        self.assertTrue(self.station.ready and self.host.ready)
        frame = self.station.seal(t.DATA, b"hello")
        self.assertEqual(self.host.feed(t.DATA, frame), [("data", b"hello")])
        frame = self.host.seal(t.DATA, b"world")
        self.assertEqual(self.station.feed(t.DATA, frame), [("data", b"world")])
        ack = self.host.ack_frame(force=True)
        self.assertEqual(struct.unpack(">Q", ack[1])[0], 5)
        frame = self.station.seal(t.CLOSE, t.json_payload(code="bye"))
        self.assertEqual(self.station._send_seq, 3)
        self.assertEqual(self.host.feed(t.CLOSE, frame)[0][0], "close")

    def test_replay_et_reordonnancement_refuses(self):
        t.handshake(self.station, self.host)
        first = self.station.seal(t.DATA, b"one")
        second = self.station.seal(t.DATA, b"two")
        with self.assertRaises(t.ProtocolError) as caught:
            self.host.feed(t.DATA, second)
        self.assertEqual(caught.exception.code, "replay")
        self.host.feed(t.DATA, first)
        self.host.feed(t.DATA, second)
        with self.assertRaises(t.ProtocolError) as caught:
            self.host.feed(t.DATA, first)
        self.assertEqual(caught.exception.code, "replay")

    def test_alteration_et_mauvais_contexte_refuses(self):
        t.handshake(self.station, self.host)
        sealed = bytearray(self.station.seal(t.DATA, b"secret"))
        sealed[-1] ^= 1
        with self.assertRaises(t.ProtocolError) as caught:
            self.host.feed(t.DATA, bytes(sealed))
        self.assertEqual(caught.exception.code, "decrypt")
        # une autre session ne peut pas ouvrir la trame : émetteur neuf dans le
        # contexte d'origine (séquence 1), récepteur dans un autre contexte
        other_session = os.urandom(16)
        other_ctx = context_for(self.receipt, other_session)
        other = t.Channel(t.ROLE_HOST, other_ctx)
        t.handshake(t.Channel(t.ROLE_STATION, other_ctx), other)
        sender = t.Channel(t.ROLE_STATION, self.ctx)
        t.handshake(sender, t.Channel(t.ROLE_HOST, self.ctx))
        with self.assertRaises(t.ProtocolError) as caught:
            other.feed(t.DATA, sender.seal(t.DATA, b"x"))
        self.assertEqual(caught.exception.code, "decrypt")

    def test_confirm_avant_cles_et_type_inconnu(self):
        with self.assertRaises(t.ProtocolError) as caught:
            self.station.confirm_frame()
        self.assertEqual(caught.exception.code, "handshake_order")
        with self.assertRaises(t.ProtocolError) as caught:
            self.station.feed(transport := t.DATA, b"\x00" * 32)
        self.assertEqual(caught.exception.code, "not_ready")

    def test_bornes_de_trame(self):
        t.handshake(self.station, self.host)
        with self.assertRaises(t.ProtocolError):
            self.station.seal(t.DATA, b"x" * (t.MAX_DATA + 1))
        with self.assertRaises(t.ProtocolError):
            t.encode_frame(self.session, t.DATA, b"x" * (t.MAX_DATA + 25))
        with self.assertRaises(t.ProtocolError):
            t.encode_frame(self.session, t.HELLO, b"x" * (t.MAX_CONTROL + 1))
        stream = io.BytesIO(struct.pack(">I", 10 * t.MAX_FRAME))
        with self.assertRaises(t.ProtocolError) as caught:
            t.read_frame(stream.read)
        self.assertEqual(caught.exception.code, "frame_too_large")
        stream = io.BytesIO(struct.pack(">I", 3) + b"\x00\x00\x00")
        with self.assertRaises(t.ProtocolError) as caught:
            t.read_frame(stream.read)
        self.assertEqual(caught.exception.code, "bad_length")

    def test_fenetre_acquittement(self):
        small = t.Channel(t.ROLE_STATION, self.ctx, window=2 * t.MAX_DATA)
        host = t.Channel(t.ROLE_HOST, self.ctx, window=2 * t.MAX_DATA)
        t.handshake(small, host)
        while small.can_send:
            frame = small.seal(t.DATA, b"x" * 1024)
            host.feed(t.DATA, frame)
        self.assertFalse(small.can_send)
        ack = host.ack_frame(force=True)
        self.assertIsNotNone(ack)
        small.feed(t.ACK, host.seal(t.ACK, ack[1]))
        self.assertTrue(small.can_send)


class PtyTest(unittest.TestCase):
    def test_code_de_sortie_et_sortie(self):
        code, out = attach_pty.run(["/bin/sh", "-c", "printf ready; exit 3"], timeout=3)
        self.assertEqual(code, 3)
        self.assertIn(b"ready", out)

    def test_echange_et_redimensionnement(self):
        proc = attach_pty.PtyProcess(["/bin/sh", "-c", "read x; printf 'got:%s' \"$x\""])
        self.addCleanup(proc.close)
        proc.resize(40, 120)
        import fcntl
        import termios
        rows, cols, _x, _y = struct.unpack(
            "HHHH", fcntl.ioctl(proc.master, termios.TIOCGWINSZ, b"\x00" * 8))
        self.assertEqual((rows, cols), (40, 120))
        proc.write(b"hello\n")
        out = bytearray()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and b"got:hello" not in out:
            chunk = proc.read(4096)
            if chunk:
                out.extend(chunk)
            else:
                time.sleep(0.01)
        self.assertIn(b"got:hello", out)

    def test_fermeture_tue_le_groupe(self):
        proc = attach_pty.PtyProcess(["/bin/sh", "-c", "sleep 30"])
        proc.close()
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self._live_group(proc.pid), [])
        with self.assertRaises(attach_pty.PtyError):
            proc.send_signal(9)

    @staticmethod
    def _live_group(pgid):
        """PID vivants (hors zombies) du groupe, via /proc (Linux)."""
        members = []
        # hermeticity: allow — uniquement les processus du groupe créé ici
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                # hermeticity: allow — idem, jamais un processus de l'hôte
                with open("/proc/%s/stat" % name, "rb") as fh:
                    fields = fh.read().rsplit(b")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if len(fields) > 2 and fields[0] != b"Z" and int(fields[2]) == pgid:
                members.append(int(name))
        return members

    def test_fermeture_tue_le_descendant_apres_le_leader(self):
        # le shell sort tout de suite ; le descendant ignore TERM et HUP
        proc = attach_pty.PtyProcess(
            ["/bin/sh", "-c", "trap '' TERM HUP; sleep 30 & exit 0"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.01)
        self.assertIsNotNone(proc.poll())
        self.assertTrue(self._live_group(proc.pid), "the descendant must be alive")
        proc.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self._live_group(proc.pid):
            time.sleep(0.01)
        self.assertEqual(self._live_group(proc.pid), [])


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.relay = None
        self.connector = None
        self.verifier = FakeVerifier()
        self.logs = io.StringIO()
        self.log_handler = logging.StreamHandler(self.logs)
        self.log_handler.setLevel(logging.DEBUG)
        self.logger = logging.getLogger("test.attach7120")
        self.logger.handlers[:] = [self.log_handler]
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.relay_log = logging.getLogger("ameesh.attach.relay")
        self.old_level = self.relay_log.level
        self.relay_log.setLevel(logging.DEBUG)
        self._old_handlers = list(self.relay_log.handlers)
        self.relay_log.handlers[:] = [self.log_handler]
        self.host_log = logging.getLogger("ameesh.attach.host")
        self._old_host_handlers = list(self.host_log.handlers)
        self.host_log.handlers[:] = [self.log_handler]
        self.host_log.setLevel(logging.DEBUG)

    def tearDown(self):
        if self.connector is not None:
            self.connector.stop()
        if self.relay is not None:
            self.relay.stop()
        self.relay_log.handlers[:] = self._old_handlers
        self.relay_log.setLevel(self.old_level)
        self.host_log.handlers[:] = self._old_host_handlers
        self.logger.handlers[:] = []

    def start(self, **overrides):
        config = dict(listen_host="127.0.0.1", listen_port=0,
                      hosts={HOST: TOKEN}, plaintext_loopback=True,
                      pair_timeout=0.3, idle_timeout=10.0,
                      max_session_seconds=10.0, max_sessions=8,
                      max_sessions_per_host=4, max_sessions_per_ip=8)
        config.update(overrides)
        self.relay = attach_relay.RelayServer(attach_relay.RelayConfig(**config),
                                              logger=self.logger)
        self.relay.start()
        return self.relay

    def host(self, **overrides):
        config = dict(relay_host="127.0.0.1", relay_port=self.relay.port, token=TOKEN,
                      host=HOST, plaintext_loopback=True, command=COMMAND,
                      max_sessions=4, max_session_seconds=10.0, keepalive=1.0)
        config.update(overrides)
        self.connector = attach_host.HostConnector(
            attach_host.HostConfig(**config), self.verifier, logger=self.logger)
        thread = threading.Thread(target=self.connector.run_forever, daemon=True)
        thread.start()
        self.assertTrue(self.connector.wait_connected(5), "the host did not connect")
        self.addCleanup(thread.join, 2.0)

    def test_session_e2e_ptyt_et_journaux_sans_contenu(self):
        self.start()
        self.host()
        receipt, device = make_grant()
        station = Station(self.relay.port, receipt, device)
        self.addCleanup(station.close)
        station.handshake()
        self.assertEqual(station.request()["v"], 1)
        self.assertEqual(len(self.verifier.requests), 1)
        request = self.verifier.requests[0]
        self.assertEqual((request.agent, request.host, request.tenant),
                         (AGENT, HOST, TENANT))
        self.assertEqual(request.attach_id, receipt["request"]["attach_id"])
        secret = "contenu-confidentiel-7120"
        station.send_data((secret + "\n").encode())
        output = station.read_data(until=b"echo:" + secret.encode())
        self.assertIn(b"echo:" + secret.encode(), output)
        # les journaux ne portent ni le contenu, ni le reçu, ni le nonce
        logs = self.logs.getvalue()
        self.assertIn("session paired", logs)
        self.assertIn("session accepted", logs)
        self.assertNotIn(secret, logs)
        self.assertNotIn(receipt["request"]["nonce"], logs)
        self.assertNotIn(receipt["request"]["device_key"], logs)
        self.assertNotIn("signature", logs)

    def test_fermeture_pendant_la_verification_ne_laisse_pas_de_pty(self):
        self.start()
        self.host()
        created = []

        class FakePty:
            def __init__(self, *args, **kwargs):
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        def verify(_request):
            session = next(iter(self.connector.sessions.values()))
            session.close("test", "closed during verification")
            return attach_host.GrantResult(True, "ok", "granted", human="human:alice")

        self.verifier.result = verify
        receipt, device = make_grant()
        station = Station(self.relay.port, receipt, device)
        self.addCleanup(station.close)
        station.handshake()
        with mock.patch.object(attach_host.attach_pty, "PtyProcess", FakePty):
            with self.assertRaises((AssertionError, TimeoutError)):
                station.request(timeout=0.5)
            # le spawn peut suivre la fermeture : il doit rester sous le mock
            deadline = time.monotonic() + 2
            while not created and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(created), 1, self.logs.getvalue())
        self.assertTrue(created[0].closed)
        self.assertEqual(self.connector.sessions, {})

    def test_refus_de_la_porte(self):
        self.start()
        self.verifier.result = attach_host.GrantResult(False, "not_authorized",
                                                       "not allowed")
        self.host()
        receipt, device = make_grant()
        station = Station(self.relay.port, receipt, device)
        self.addCleanup(station.close)
        station.handshake()
        answer = station.request()
        self.assertEqual(answer.get("code"), "not_authorized")
        time.sleep(0.2)
        self.assertEqual(self.connector.sessions, {})

    def test_hote_inconnu_et_grant_invalide(self):
        self.start()
        receipt, _device = make_grant()
        frame = open_only(self.relay.port, receipt)
        self.assertEqual(frame.type, t.RELAY_ERR)
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "unknown_host")
        self.host()
        frame = open_only(self.relay.port, {"v": "x"})
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "grant_invalid")

    def test_grant_lie_a_un_autre_hote_et_expire(self):
        self.start()
        self.host()
        receipt, _device = make_grant(host="autre-hote")
        frame = open_only(self.relay.port, receipt, host=HOST)
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "grant_mismatch")
        receipt, _device = make_grant(exp=int(time.time()) - 10)
        frame = open_only(self.relay.port, receipt)
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "grant_expired")

    def test_grant_deja_apparie(self):
        self.start()
        self.host()
        receipt, device = make_grant()
        first = Station(self.relay.port, receipt, device)
        self.addCleanup(first.close)
        second_sock = socket.create_connection(("127.0.0.1", self.relay.port), timeout=5)
        second_sock.settimeout(5)
        second_sock.sendall(t.encode_frame(t.ZERO_SESSION, t.OPEN, t.json_payload(
            v=1, role="station", agent=AGENT, host=HOST, tenant=TENANT,
            receipt=receipt)))
        frame = t.read_frame(second_sock.recv)
        second_sock.close()
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "grant_in_use")

    def test_quota_par_hote(self):
        self.start(max_sessions_per_host=1)
        self.host(max_sessions=1)
        first_receipt, first_device = make_grant()
        first = Station(self.relay.port, first_receipt, first_device)
        self.addCleanup(first.close)
        second_receipt, second_device = make_grant()
        frame = open_only(self.relay.port, second_receipt)
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "host_busy")

    def test_quota_par_adresse_rendu_a_la_fermeture(self):
        self.start(max_sessions_per_ip=1)
        self.host()
        first_receipt, first_device = make_grant()
        first = Station(self.relay.port, first_receipt, first_device)
        first.handshake()
        first.request()
        first.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.connector.sessions:
            time.sleep(0.02)
        second_receipt, second_device = make_grant()
        second = Station(self.relay.port, second_receipt, second_device)
        self.addCleanup(second.close)
        second.handshake()
        self.assertEqual(second.request()["v"], 1)

    def test_quota_du_relais(self):
        self.start(max_sessions=1)
        self.host(max_sessions=1)
        first_receipt, first_device = make_grant()
        first = Station(self.relay.port, first_receipt, first_device)
        self.addCleanup(first.close)
        second_receipt, _second_device = make_grant()
        frame = open_only(self.relay.port, second_receipt)
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "relay_busy")

    def test_mauvais_jeton(self):
        self.start()
        self.connector = attach_host.HostConnector(
            attach_host.HostConfig(relay_host="127.0.0.1", relay_port=self.relay.port,
                                   token="mauvais-" + "b" * 40, host=HOST,
                                   plaintext_loopback=True, command=COMMAND),
            self.verifier, logger=self.logger)
        thread = threading.Thread(target=self.connector.run_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2.0)
        self.assertFalse(self.connector.wait_connected(1.0))

    def test_deconnexion_du_poste_ferme_la_session(self):
        self.start()
        self.host()
        receipt, device = make_grant()
        station = Station(self.relay.port, receipt, device)
        station.handshake()
        station.request()
        session = next(iter(self.connector.sessions.values()))
        station.sock.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.connector.sessions:
            time.sleep(0.02)
        self.assertEqual(self.connector.sessions, {})
        self.assertIn("peer_closed", self.logs.getvalue())
        self.assertIsNone(session.pty)

    def test_reconnexion_de_l_hote(self):
        self.start()
        self.host()
        self.connector.stop()
        time.sleep(0.2)
        receipt, device = make_grant()
        # le relais n'a plus d'hôte : refus immédiat, puis le nouvel hôte sert
        frame = open_only(self.relay.port, receipt)
        self.assertEqual(t.parse_json(frame.payload, "err")["code"], "unknown_host")
        self.host()
        station = Station(self.relay.port, receipt, device)
        self.addCleanup(station.close)
        station.handshake()
        self.assertEqual(station.request()["v"], 1)

    def test_ecoute_publique_refusee(self):
        with self.assertRaises(attach_relay.RelayConfigError):
            attach_relay.RelayConfig(hosts={HOST: TOKEN}, plaintext_loopback=False,
                                     listen_host="0.0.0.0")
        with self.assertRaises(attach_relay.RelayConfigError):
            attach_relay.RelayConfig(hosts={HOST: TOKEN}, listen_host="10.0.0.1",
                                     mesh_cidrs=("10.0.0.0/8",), plaintext_loopback=True)


class TlsTest(unittest.TestCase):
    def setUp(self):
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError:  # pragma: no cover
            self.skipTest("cryptography absent")
        import datetime
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        import ipaddress
        # dates fixes : le test ne dépend pas du jour où il tourne
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
                .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        self.cert_path = os.path.join(self.tmp.name, "cert.pem")
        self.key_path = os.path.join(self.tmp.name, "key.pem")
        with open(self.cert_path, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(self.key_path, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        self.server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_ctx.load_cert_chain(self.cert_path, self.key_path)
        self.client_ctx = ssl.create_default_context(cafile=self.cert_path)

    def test_session_tls(self):
        relay = attach_relay.RelayServer(attach_relay.RelayConfig(
            listen_host="127.0.0.1", listen_port=0, hosts={HOST: TOKEN},
            tls_context=self.server_ctx, pair_timeout=0.3, idle_timeout=10.0,
            max_session_seconds=10.0))
        relay.start()
        self.addCleanup(relay.stop)
        verifier = FakeVerifier()
        connector = attach_host.HostConnector(attach_host.HostConfig(
            relay_host="127.0.0.1", relay_port=relay.port, token=TOKEN, host=HOST,
            tls_context=self.client_ctx, command=COMMAND), verifier)
        thread = threading.Thread(target=connector.run_forever, daemon=True)
        thread.start()
        self.addCleanup(connector.stop)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(connector.wait_connected(5))
        receipt, device = make_grant()
        station = Station(relay.port, receipt, device, tls_context=self.client_ctx)
        self.addCleanup(station.close)
        station.handshake()
        self.assertEqual(station.request()["v"], 1)


class FrameTimeoutTest(unittest.TestCase):
    def test_timeout_avant_le_premier_octet_remonte(self):
        def recv(_size):
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            t.read_frame(recv)

    def test_trame_commencee_ne_se_resynchronise_pas(self):
        header = struct.pack(">I", t.SESSION_BYTES + 3) + b"\x00" * t.SESSION_BYTES + b"\x19"
        pieces = [header, b"a", TimeoutError(), TimeoutError()]
        def recv(size):
            item = pieces.pop(0) if pieces else TimeoutError()
            if isinstance(item, BaseException):
                raise item
            return item
        with self.assertRaises(t.ProtocolError) as caught:
            t.read_frame(recv, frame_timeout=0.05)
        self.assertEqual(caught.exception.code, "frame_timeout")

    def test_entete_partiel_borne(self):
        pieces = [b"\x00\x00", TimeoutError(), TimeoutError()]
        def recv(size):
            item = pieces.pop(0) if pieces else TimeoutError()
            if isinstance(item, BaseException):
                raise item
            return item
        with mock.patch.object(t, "FRAME_TIMEOUT", 0.05):
            with self.assertRaises(t.ProtocolError) as caught:
                t.read_frame(recv)
        self.assertEqual(caught.exception.code, "frame_timeout")


class CliAndConfigTest(unittest.TestCase):
    def test_listen_et_url(self):
        from ameesh import attach_relay_cli
        self.assertEqual(attach_relay_cli._listen("127.0.0.1:0"), ("127.0.0.1", 0))
        self.assertEqual(attach_relay_cli._listen("[::1]:8790"), ("::1", 8790))
        with self.assertRaises(attach_host.HostConfigError):
            attach_host.parse_relay_url("http://example.test:80")
        self.assertEqual(attach_host.parse_relay_url(
            "http://127.0.0.1:80", plaintext_loopback=True)[0:2], ("127.0.0.1", 80))

    def test_fichiers_prives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jetons.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"%s": "%s"}' % (HOST, TOKEN))
            os.chmod(path, 0o644)
            with self.assertRaises(attach_relay.RelayConfigError):
                attach_relay.load_hosts_file(path)
            os.chmod(path, 0o600)
            self.assertEqual(attach_relay.load_hosts_file(path), {HOST: TOKEN})
