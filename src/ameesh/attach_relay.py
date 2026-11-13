# SPDX-License-Identifier: AGPL-3.0-only
"""Service de relais d'attach, réservé au réseau du mesh (#7120, avatar D2).

Conception 5322 §5.3 : un petit service de relais appareille un **poste**
et l'**hôte de l'agent** (qui ouvre une connexion sortante, jamais de port
entrant sur le poste ni sur l'hôte). Il route des trames de bout en bout
chiffrées ; il ne voit ni ne conserve le contenu. Cette décision (Q3
#213938, option a) réserve le relais au réseau du mesh : aucune exposition
publique n'est possible ici — l'écoute doit être sur la boucle locale ou
dans un CIDR de mesh déclaré, et un pair hors de ces plages est refusé.

**Rôles et trames.** L'hôte s'annonce (`HELLO`, jeton de relais 0600), puis
reste connecté ; plusieurs sessions partagent sa connexion (l'identifiant
de session est dans chaque trame). Un poste demande (`OPEN`) une session
pour un agent, un hôte et un tenant, en présentant le reçu d'attach (D1,
`ameesh-receipt/1`). Le relais **pré-contrôle** le reçu (forme, nature,
liaison agent/hôte/tenant, échéance, durée ≤ 5 min) et l'empreinte, puis
apparie ; il ne vérifie pas la signature WebAuthn — c'est l'hôte qui la
vérifie et qui consomme le nonce (`attach_grant.redeem`). Aucun octet de
terminal n'est routé avant que l'hôte n'ait lui-même accepté la preuve.

**E2E.** Dès l'appariement, les deux extrémités échangent leurs clés
X25519 et une confirmation AES-256-GCM (voir `attach_transport`) ; le
relais ne voit que `KEY`/`CONFIRM` (clés publiques, chiffré) puis des
trames opaques. Les journaux ne portent que l'identité de routage, les
tailles, la durée et le code de fermeture : jamais un reçu, une clé, un
octet de terminal.

**Quotas.** Sessions simultanées (globales, par hôte, par adresse), durée
maximale, inactivité, octets et trames par seconde, octets par session,
file d'écriture bornée. Un dépassement ferme la session avec un code stable
(`quota_*`, `rate_*`, `slow_peer`) : le relais ne bloque pas les autres.

Le service ne dépend pas de la base : il ne lit ni n'écrit le registre ;
c'est l'hôte qui reste la porte d'autorisation. TLS est exigé hors boucle
locale (`--plaintext-loopback` réservé aux tests).
"""
from __future__ import annotations

import dataclasses
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import ssl
import struct
import threading
import time
from typing import Callable

from . import attach_transport as transport
from . import httpkit, receipts

log = logging.getLogger("ameesh.attach.relay")

#: noms acceptés pour un agent, un hôte et un tenant (comme le registre)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TOKEN_MIN = 32
#: codes stables de fermeture (journalisés, jamais de charge utile)
BAD_REQUEST = "bad_request"
AUTH_FAILED = "auth_failed"
GRANT_INVALID = "grant_invalid"
GRANT_EXPIRED = "grant_expired"
GRANT_MISMATCH = "grant_mismatch"
GRANT_IN_USE = "grant_in_use"
UNKNOWN_HOST = "unknown_host"
HOST_BUSY = "host_busy"
RELAY_BUSY = "relay_busy"
QUOTA_SESSIONS = "quota_sessions"
QUOTA_BYTES = "quota_bytes"
RATE_BYTES = "rate_bytes"
SLOW_PEER = "slow_peer"
PEER_CLOSED = "peer_closed"
HOST_CLOSED = "host_closed"
IDLE = "idle"
MAX_DURATION = "max_duration"
PROTOCOL = "protocol"
SHUTDOWN = "shutdown"


class RelayConfigError(ValueError):
    """Configuration du relais refusée (TLS, exposition, jeton, quota)."""


def _parse_cidrs(values) -> tuple:
    networks = []
    for value in values or ():
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError as exc:
            raise RelayConfigError("invalid mesh CIDR %r: %s" % (value, exc)) from exc
    return tuple(networks)


def _loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return address == "localhost"


def _in_mesh(address: str, networks: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    for network in networks:
        if ip.version == network.version and ip in network:
            return True
    return False


def load_hosts_file(path: str) -> dict[str, str]:
    """Lit le fichier 0600 des jetons d'hôtes : `{"hôte": "jeton", …}`.

    Le fichier doit appartenir à l'utilisateur effectif et n'être lisible
    que par lui (même contrôle qu'un jeton de service) ; un jeton de moins
    de 32 caractères est refusé. Aucun jeton n'est journalisé.
    """
    path = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.stat(path)
    except OSError as exc:
        raise RelayConfigError("cannot read the hosts file: %s" % exc.strerror) from exc
    httpkit.check_private(st, "attach relay hosts", path, error=RelayConfigError)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read(1024 * 1024)
    except OSError as exc:
        raise RelayConfigError("cannot read the hosts file: %s" % exc.strerror) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RelayConfigError("hosts file: unreadable JSON") from exc
    if not isinstance(data, dict) or not data:
        raise RelayConfigError("hosts file: a non-empty JSON object is required")
    hosts = {}
    for name, token in data.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise RelayConfigError("hosts file: invalid host name %r" % (name,))
        if not isinstance(token, str) or len(token) < TOKEN_MIN or any(ch.isspace() for ch in token):
            raise RelayConfigError("hosts file: token for %s must be at least %d non-space "
                                   "characters" % (name, TOKEN_MIN))
        hosts[name] = token
    return hosts


@dataclasses.dataclass(frozen=True)
class RelayConfig:
    """Bornes et politique du service ; les valeurs sont des défauts sûrs."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 8790
    mesh_cidrs: tuple = ()
    hosts: dict = dataclasses.field(default_factory=dict, hash=False)
    tls_context: ssl.SSLContext | None = None
    plaintext_loopback: bool = False
    max_connections: int = 128
    max_sessions: int = 64
    max_sessions_per_host: int = 4
    max_sessions_per_ip: int = 8
    max_pending_pairs: int = 32
    pair_timeout: float = 5.0
    handshake_timeout: float = 15.0
    idle_timeout: float = 900.0
    max_session_seconds: float = 2 * 3600.0
    max_session_bytes: int = 512 * 1024 * 1024
    max_bytes_per_second: int = 4 * 1024 * 1024
    max_frame: int = transport.MAX_FRAME
    write_queue: int = 256
    keepalive: float = 30.0
    connection_rate_per_minute: int = 120

    def __post_init__(self) -> None:
        if not self.hosts:
            raise RelayConfigError("no host token configured")
        for name, token in self.hosts.items():
            if not NAME_RE.fullmatch(name or ""):
                raise RelayConfigError("invalid host name %r" % (name,))
            if not isinstance(token, str) or len(token) < TOKEN_MIN:
                raise RelayConfigError("token for %s is too short" % name)
        if not _loopback(self.listen_host) and not _in_mesh(self.listen_host,
                                                             _parse_cidrs(self.mesh_cidrs)):
            raise RelayConfigError(
                "refusing to listen on %s: the attach relay is reserved to the mesh "
                "(give --mesh-cidr covering this address, or bind to loopback); "
                "a public exposure would need a separate owner decision"
                % self.listen_host)
        if self.tls_context is None and not (self.plaintext_loopback
                                             and _loopback(self.listen_host)):
            raise RelayConfigError(
                "TLS is required for the attach relay outside loopback "
                "(give --tls-cert/--tls-key, or --plaintext-loopback on 127.0.0.1)")
        if self.plaintext_loopback and not _loopback(self.listen_host):
            raise RelayConfigError("--plaintext-loopback is only accepted on loopback")
        if not 0 <= self.listen_port <= 65535:
            raise RelayConfigError("invalid port %r" % (self.listen_port,))
        for name in ("max_connections", "max_sessions", "max_sessions_per_host",
                     "max_sessions_per_ip", "max_session_bytes", "max_frame",
                     "write_queue"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise RelayConfigError("%s must be a positive integer" % name)
        if self.max_frame > 1024 * 1024:
            raise RelayConfigError("max_frame is capped at 1 MiB")
        if not 0 < self.pair_timeout <= 120 or not 0 < self.handshake_timeout <= 300:
            raise RelayConfigError("pair and handshake timeouts must be positive and bounded")
        if self.max_session_seconds <= 0 or self.idle_timeout <= 0:
            raise RelayConfigError("session and idle timeouts must be positive")


# --------------------------------------------------------------------------
# pairs
# --------------------------------------------------------------------------

class _Peer:
    """Une connexion TCP (TLS) : lecteur, écrivain borné, arrêt unique."""

    def __init__(self, relay: "RelayServer", sock: socket.socket,
                 address: tuple, secure: bool):
        self.relay = relay
        self.sock = sock
        self.address = address
        self.ip = _normalise_ip(address[0] if address else "")
        self.secure = secure
        self._out: queue.Queue = queue.Queue(maxsize=relay.config.write_queue)
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._closed_once = False
        self._close_lock = threading.Lock()
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self.created = time.monotonic()
        self.last_activity = self.created
        self.bytes_in = 0
        self.bytes_out = 0
        self.close_code = ""
        self.role = ""

    # -- cycle de vie ------------------------------------------------------

    def start(self) -> None:
        self._writer = threading.Thread(target=self._write_loop, name="attach-relay-writer",
                                        daemon=True)
        self._writer.start()
        self._reader = threading.Thread(target=self.run, name="attach-relay-peer",
                                        daemon=True)
        self._reader.start()

    def run(self) -> None:
        try:
            self.sock.settimeout(self.relay.config.handshake_timeout)
            first = transport.read_frame(self._recv, max_frame=self.relay.config.max_frame,
                                         frame_timeout=self.relay.config.handshake_timeout)
            if first is None:
                self.close("eof", notify=False)
                return
            if first.type == transport.HELLO:
                self._hello(first)
            elif first.type == transport.OPEN:
                self._open(first)
            else:
                self.fail(BAD_REQUEST, "first frame must be HELLO or OPEN")
                return
            self._read_loop()
        except socket.timeout:
            self.fail("timeout", "handshake or frame timeout")
        except transport.ProtocolError as exc:
            self.fail(exc.code, exc.message)
        except (OSError, ssl.SSLError) as exc:
            self.close("io_error", notify=False)
        finally:
            self.close(self.close_code or "closed", notify=False)

    def _read_loop(self) -> None:
        self.sock.settimeout(transport.RECV_POLL)
        while not self._stop.is_set():
            try:
                frame = transport.read_frame(self._recv,
                                             max_frame=self.relay.config.max_frame)
            except socket.timeout:
                if time.monotonic() - self.last_activity > self.relay.config.idle_timeout:
                    self.close(IDLE, notify=True)
                    return
                continue
            if frame is None:
                self.close("eof", notify=True)
                return
            if len(frame.payload) > self.relay.config.max_frame:
                self.fail("frame_too_large", "frame exceeds the configured bound")
                return
            self.last_activity = time.monotonic()
            self.bytes_in += 4 + len(frame.session) + 1 + len(frame.payload)
            self.handle(frame)

    # -- envoi -------------------------------------------------------------

    def send(self, session_id: bytes, ftype: int, payload: bytes = b"") -> bool:
        if self._closed.is_set() or self._stop.is_set():
            return False
        try:
            frame = transport.encode_frame(session_id, ftype, payload)
        except transport.ProtocolError:
            return False
        try:
            self._out.put_nowait(frame)
        except queue.Full:
            self.close(SLOW_PEER, notify=True)
            return False
        return True

    def send_json(self, session_id: bytes, ftype: int, **fields) -> bool:
        return self.send(session_id, ftype, transport.json_payload(**fields))

    def _recv(self, size: int) -> bytes:
        # SSLSocket n'est pas thread-safe : lecture et écriture sérialisées
        with self._send_lock:
            return self.sock.recv(size)

    def _write_loop(self) -> None:
        while True:
            item = self._out.get()
            if item is None:
                break
            try:
                with self._send_lock:
                    if self._closed.is_set():
                        break
                    self.sock.sendall(item)
                    self.bytes_out += len(item)
            except (OSError, ssl.SSLError):
                self._stop.set()
                self._closed.set()
                break
        self._closed.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def fail(self, code: str, message: str, session: bytes = transport.ZERO_SESSION) -> None:
        """Dernière trame d'erreur en clair, puis fermeture."""
        if not self.close_code:
            self.close_code = code
        station_session = getattr(getattr(self, "session", None), "session_id", None)
        if session == transport.ZERO_SESSION and station_session:
            session = station_session
        if not self._stop.is_set():
            try:
                frame = transport.encode_frame(
                    session, transport.RELAY_ERR,
                    transport.json_payload(code=code, message=str(message)[:300]))
                with self._send_lock:
                    self.sock.settimeout(2.0)
                    self.sock.sendall(frame)
            except (OSError, ssl.SSLError, transport.ProtocolError):
                pass
        self.close(code, notify=False)

    def close(self, code: str = "closed", *, notify: bool = False) -> None:
        with self._close_lock:
            if self._closed_once:
                return
            self._closed_once = True
        if not self.close_code:
            self.close_code = code
        self._stop.set()
        try:
            self._out.put_nowait(None)
        except queue.Full:
            pass
        # le writer ferme la socket ; le lecteur est débloqué par shutdown
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.relay.forget(self)

    def wait_closed(self, timeout: float = 2.0) -> None:
        if self._writer is not None:
            self._writer.join(timeout)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout)

    # -- à implémenter -----------------------------------------------------

    def handle(self, frame: transport.Frame) -> None:  # pragma: no cover
        raise NotImplementedError

    def _hello(self, frame: transport.Frame) -> None:  # pragma: no cover
        raise NotImplementedError

    def _open(self, frame: transport.Frame) -> None:  # pragma: no cover
        raise NotImplementedError


def _normalise_ip(value: str) -> str:
    if value.startswith("::ffff:"):
        return value[7:]
    return value


class HostPeer(_Peer):
    """La connexion sortante d'un hôte, qui porte plusieurs sessions."""

    def __init__(self, relay, sock, address, secure, *, host: str, tenant: str):
        super().__init__(relay, sock, address, secure)
        self.role = "host"
        self.host = host
        self.tenant = tenant
        self.sessions: dict[bytes, Session] = {}
        self.lock = threading.Lock()

    def _hello(self, frame: transport.Frame) -> None:
        data = transport.parse_json(frame.payload, "HELLO")
        if data.get("v") != transport.VERSION:
            self.fail(BAD_REQUEST, "HELLO: unsupported version")
            return
        if data.get("role") != "host":
            self.fail(BAD_REQUEST, "HELLO: role host expected")
            return
        host, tenant = data.get("host"), data.get("tenant", "")
        if not isinstance(host, str) or not NAME_RE.fullmatch(host):
            self.fail(BAD_REQUEST, "HELLO: invalid host")
            return
        if tenant and (not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant)):
            self.fail(BAD_REQUEST, "HELLO: invalid tenant")
            return
        expected = self.relay.config.hosts.get(host, "")
        token = data.get("token")
        if not expected or not isinstance(token, str) or not hmac.compare_digest(
                token.encode("utf-8", "replace"), expected.encode("utf-8")):
            self.fail(AUTH_FAILED, "host authentication failed")
            return
        self.host, self.tenant = host, tenant
        if not self.relay.register_host(self):
            self.fail(RELAY_BUSY, "relay or host already has too many sessions")
            return
        self.send_json(transport.ZERO_SESSION, transport.WELCOME, v=transport.VERSION,
                       session=None, keeplive=self.relay.config.keepalive,
                       limits={"max_sessions": self.relay.config.max_sessions_per_host,
                               "max_frame": self.relay.config.max_frame})
        self.relay.note("host connected", self, host=host, tenant=tenant)

    def _open(self, frame: transport.Frame) -> None:
        self.fail(BAD_REQUEST, "a station must send OPEN, not HELLO/OPEN twice")
        return

    def handle(self, frame: transport.Frame) -> None:
        if frame.session == transport.ZERO_SESSION:
            if frame.type == transport.PING:
                # le keepalive de l'hôte couvre ses sessions : un harnais qui
                # réfléchit n'est pas une session morte
                now = time.monotonic()
                with self.lock:
                    sessions = list(self.sessions.values())
                for session in sessions:
                    session.last_activity = now
                self.send(transport.ZERO_SESSION, transport.PONG)
                return
            if frame.type == transport.PONG:
                return
            self.fail(BAD_REQUEST, "host frame without a session")
            return
        session = self.sessions.get(frame.session)
        if session is None:
            self.send_json(frame.session, transport.RELAY_ERR, code="unknown_session",
                           message="no such session on this host")
            return
        if frame.type == transport.PAIR_NO:
            data = transport.parse_json(frame.payload, "PAIR_NO")
            session.close(HOST_BUSY, "host refused the pair: %s"
                          % str(data.get("reason") or data.get("code") or "")[:120],
                          notify_station=True)
            return
        if frame.type == transport.PING:
            self.send(frame.session, transport.PONG)
            return
        if frame.type in transport.E2E_TYPES:
            session.forward_from_host(frame)
            return
        self.fail(BAD_REQUEST, "unexpected host frame type %d" % frame.type)

    def close_sessions(self, code: str, reason: str) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close(code, reason, notify_station=True)


class StationPeer(_Peer):
    """La connexion d'un poste : une session après appariement."""

    def __init__(self, relay, sock, address, secure):
        super().__init__(relay, sock, address, secure)
        self.role = "station"
        self.session: Session | None = None

    def _hello(self, frame: transport.Frame) -> None:
        self.fail(BAD_REQUEST, "a host sends HELLO, a station sends OPEN")

    def _open(self, frame: transport.Frame) -> None:
        data = transport.parse_json(frame.payload, "OPEN")
        if data.get("v") != transport.VERSION:
            self.fail(BAD_REQUEST, "OPEN: unsupported version")
            return
        if data.get("role") not in (None, "station"):
            self.fail(BAD_REQUEST, "OPEN: role station expected")
            return
        agent, host, tenant = data.get("agent"), data.get("host"), data.get("tenant", "")
        if not isinstance(agent, str) or not NAME_RE.fullmatch(agent):
            self.fail(BAD_REQUEST, "OPEN: invalid agent")
            return
        if not isinstance(host, str) or not NAME_RE.fullmatch(host):
            self.fail(BAD_REQUEST, "OPEN: invalid host")
            return
        if tenant and (not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant)):
            self.fail(BAD_REQUEST, "OPEN: invalid tenant")
            return
        receipt = data.get("receipt")
        try:
            parsed = receipts.parse_receipt(receipt)
        except receipts.ReceiptError:
            self.fail(GRANT_INVALID, "OPEN: unreadable attach receipt")
            return
        if parsed.kind != "attach":
            self.fail(GRANT_INVALID, "OPEN: an attach receipt is required")
            return
        request = parsed.request
        if request.get("decision") != "approve":
            self.fail(GRANT_INVALID, "OPEN: receipt is not an approval")
            return
        if (request.get("agent") != agent or request.get("host") != host
                or request.get("tenant", "") != tenant):
            self.fail(GRANT_MISMATCH, "OPEN: receipt is bound to another agent, host or tenant")
            return
        try:
            iat, exp = int(request["iat"]), int(request["exp"])
        except (KeyError, TypeError, ValueError):
            self.fail(GRANT_INVALID, "OPEN: receipt has no usable validity")
            return
        now = time.time()
        if exp <= now:
            self.fail(GRANT_EXPIRED, "OPEN: attach receipt expired")
            return
        if exp - iat > 300:
            self.fail(GRANT_INVALID, "OPEN: attach receipt valid more than 5 minutes")
            return
        try:
            digest = transport.receipt_digest(parsed.document)
        except transport.ProtocolError:
            self.fail(GRANT_INVALID, "OPEN: receipt cannot be canonicalized")
            return
        self.relay.open_station(self, agent, host, tenant, request["attach_id"], digest)

    def handle(self, frame: transport.Frame) -> None:
        session = self.session
        if session is None:
            self.fail(BAD_REQUEST, "station frame before OPEN")
            return
        if frame.session != session.session_id:
            self.fail(BAD_REQUEST, "station frame with an unknown session")
            return
        if frame.type == transport.PING:
            self.send(frame.session, transport.PONG)
            return
        if frame.type in transport.E2E_TYPES:
            session.forward_from_station(frame)
            return
        self.fail(BAD_REQUEST, "unexpected station frame type %d" % frame.type)


class Session:
    """Une session appariée : deux pairs, des compteurs, une seule fermeture."""

    def __init__(self, relay: "RelayServer", *, session_id: bytes, host: HostPeer,
                 station: StationPeer, agent: str, tenant: str, attach_id: str,
                 digest: bytes):
        self.relay = relay
        self.session_id = session_id
        self.host = host
        self.station = station
        self.agent = agent
        self.tenant = tenant
        self.attach_id = attach_id
        self.digest = digest
        self.created = time.monotonic()
        self.last_activity = self.created
        self.bytes_to_host = 0
        self.bytes_to_station = 0
        self.frames = 0
        self._closed = threading.Event()
        self._window_start = self.created
        self._window_bytes = 0
        with host.lock:
            host.sessions[session_id] = self
        station.session = self
        relay.sessions[session_id] = self

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def forward_from_station(self, frame: transport.Frame) -> None:
        self._forward(frame, to_host=True)

    def forward_from_host(self, frame: transport.Frame) -> None:
        self._forward(frame, to_host=False)

    def _forward(self, frame: transport.Frame, *, to_host: bool) -> None:
        if self.closed:
            return
        self.last_activity = time.monotonic()
        self.frames += 1
        size = len(frame.payload)
        used = self.bytes_to_host + self.bytes_to_station + size
        if used > self.relay.config.max_session_bytes:
            self.close(QUOTA_BYTES, "session byte quota reached", notify_station=True)
            return
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._window_bytes = 0
        self._window_bytes += size
        if self._window_bytes > self.relay.config.max_bytes_per_second:
            self.close(RATE_BYTES, "session byte rate exceeded", notify_station=True)
            return
        target = self.host if to_host else self.station
        if to_host:
            self.bytes_to_host += size
        else:
            self.bytes_to_station += size
        if not target.send(frame.session, frame.type, frame.payload):
            self.close(SLOW_PEER, "peer is not reading", notify_station=True)

    def close(self, code: str, reason: str, *, notify_station: bool = False) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self.relay._lock:
            self.relay.sessions.pop(self.session_id, None)
            self.relay.by_attach.pop(self.attach_id, None)
            count = self.relay._ip_sessions.get(self.station.ip, 0)
            if count <= 1:
                self.relay._ip_sessions.pop(self.station.ip, None)
            else:
                self.relay._ip_sessions[self.station.ip] = count - 1
        with self.host.lock:
            self.host.sessions.pop(self.session_id, None)
        if notify_station:
            self.station.send_json(self.session_id, transport.RELAY_ERR, code=code,
                                   message=str(reason)[:200])
        # l'hôte doit arrêter son PTY : la coupure du poste ne laisse pas de
        # harnais orphelin (§5.4). Un hôte déjà fermé ignore la trame.
        self.host.send_json(self.session_id, transport.RELAY_ERR, code=code,
                            message=str(reason)[:200])
        self.relay.note("session closed", self.station, session=self.session_id.hex(),
                        agent=self.agent, host=self.host.host, tenant=self.tenant,
                        attach_id=self.attach_id, code=code,
                        bytes_to_host=self.bytes_to_host,
                        bytes_to_station=self.bytes_to_station,
                        seconds=round(time.monotonic() - self.created, 3))
        # la station n'a qu'une session : la fermer est sans ambiguïté
        self.station.close(code, notify=False)


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------

class RelayServer:
    """Le service : écoute, apparie, route, borne, journalise les métadonnées."""

    def __init__(self, config: RelayConfig, *, logger: logging.Logger | None = None):
        self.config = config
        self.log = logger or log
        self.sessions: dict[bytes, Session] = {}
        self.by_attach: dict[str, Session] = {}
        self._hosts: dict[tuple[str, str], HostPeer] = {}
        self._lock = threading.Lock()
        self._peers: set[_Peer] = set()
        self._peer_lock = threading.Lock()
        self._stop = threading.Event()
        self._listen: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._reaper: threading.Thread | None = None
        self._slots = threading.BoundedSemaphore(config.max_connections)
        self._ip_limiter = httpkit.RateLimiter(config.connection_rate_per_minute)
        self._ip_sessions: dict[str, int] = {}
        self.port = 0

    # -- démarrage / arrêt -------------------------------------------------

    def start(self) -> None:
        family = socket.AF_INET6 if ":" in self.config.listen_host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.config.listen_host, self.config.listen_port))
            sock.listen(64)
        except OSError as exc:
            sock.close()
            raise RelayConfigError("cannot listen on %s:%s: %s"
                                   % (self.config.listen_host, self.config.listen_port,
                                      exc.strerror)) from exc
        sock.settimeout(0.5)
        self._listen = sock
        self.port = sock.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="attach-relay-accept", daemon=True)
        self._accept_thread.start()
        self._reaper = threading.Thread(target=self._reap_loop,
                                        name="attach-relay-reaper", daemon=True)
        self._reaper.start()
        self.note("relay listening", None, host=self.config.listen_host, port=self.port,
                  tls=bool(self.config.tls_context),
                  mesh=[str(n) for n in self.config.mesh_cidrs])

    def serve_forever(self) -> None:
        self.start()
        self.wait()

    def wait(self) -> None:
        """Attend l'arrêt (service au premier plan, tests) puis ferme."""
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        sock = self._listen
        self._listen = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(3.0)
        with self._peer_lock:
            peers = list(self._peers)
        for peer in peers:
            peer.close(SHUTDOWN, notify=False)
        for peer in peers:
            peer.wait_closed(3.0)
        with self._lock:
            self._hosts.clear()
        self.sessions.clear()
        self.by_attach.clear()

    # -- acceptation -------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._listen
            if sock is None:
                return
            try:
                conn, address = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._allowed(address[0]):
                self._close_quietly(conn)
                self.log.warning("attach relay: peer outside the mesh refused")
                continue
            if not self._ip_limiter.allow(_normalise_ip(address[0])):
                self._close_quietly(conn)
                self.log.warning("attach relay: connection rate exceeded")
                continue
            if not self._slots.acquire(blocking=False):
                self._close_quietly(conn)
                self.log.warning("attach relay: too many connections")
                continue
            try:
                conn.settimeout(self.config.handshake_timeout)
                if self.config.tls_context is not None:
                    conn = self.config.tls_context.wrap_socket(conn, server_side=True)
                secure = self.config.tls_context is not None
                peer = _FirstPeer(self, conn, address, secure)
                with self._peer_lock:
                    self._peers.add(peer)
                peer.start()
            except (OSError, ssl.SSLError):
                self._slots.release()
                self._close_quietly(conn)

    def _allowed(self, address: str) -> bool:
        return _in_mesh(_normalise_ip(address), _parse_cidrs(self.config.mesh_cidrs))

    @staticmethod
    def _close_quietly(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    # -- enregistrement ----------------------------------------------------

    def register_host(self, peer: HostPeer) -> bool:
        key = (peer.host, peer.tenant)
        with self._lock:
            if len(self._hosts) >= self.config.max_connections:
                return False
            previous = self._hosts.get(key)
            self._hosts[key] = peer
        if previous is not None and previous is not peer:
            self.note("host replaced", peer, host=peer.host, tenant=peer.tenant)
            previous.close_sessions(HOST_CLOSED, "host reconnected")
            previous.close("replaced", notify=False)
        return True

    def host_for(self, host: str, tenant: str) -> HostPeer | None:
        with self._lock:
            return self._hosts.get((host, tenant))

    def forget(self, peer: _Peer) -> None:
        with self._peer_lock:
            self._peers.discard(peer)
        self._slots.release()
        if isinstance(peer, HostPeer):
            with self._lock:
                if self._hosts.get((peer.host, peer.tenant)) is peer:
                    del self._hosts[(peer.host, peer.tenant)]
            peer.close_sessions(HOST_CLOSED, "host connection closed")
        if isinstance(peer, StationPeer) and peer.session is not None:
            peer.session.close(PEER_CLOSED, "station disconnected",
                               notify_station=False)

    # -- appariement -------------------------------------------------------

    def open_station(self, station: StationPeer, agent: str, host: str, tenant: str,
                     attach_id: str, digest: bytes) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self.sessions) >= self.config.max_sessions:
                station.fail(RELAY_BUSY, "relay is at its session quota")
                return
            if self._ip_sessions.get(station.ip, 0) >= self.config.max_sessions_per_ip:
                station.fail(QUOTA_SESSIONS, "too many sessions for this address")
                return
            if attach_id in self.by_attach:
                station.fail(GRANT_IN_USE, "this attach grant already has a session")
                return
        host_peer = self._wait_host(host, tenant, deadline=now + self.config.pair_timeout)
        if host_peer is None:
            station.fail(UNKNOWN_HOST, "no host %s%s is connected"
                         % (host, " (tenant %s)" % tenant if tenant else ""))
            return
        with self._lock:
            if len(self.sessions) >= self.config.max_sessions:
                station.fail(RELAY_BUSY, "relay is at its session quota")
                return
            if self._ip_sessions.get(station.ip, 0) >= self.config.max_sessions_per_ip:
                station.fail(QUOTA_SESSIONS, "too many sessions for this address")
                return
            if attach_id in self.by_attach:
                station.fail(GRANT_IN_USE, "this attach grant already has a session")
                return
            if len(host_peer.sessions) >= self.config.max_sessions_per_host:
                station.fail(HOST_BUSY, "host is at its session quota")
                return
            session_id = os.urandom(transport.SESSION_BYTES)
            while session_id in self.sessions:
                session_id = os.urandom(transport.SESSION_BYTES)
            session = Session(self, session_id=session_id, host=host_peer,
                              station=station, agent=agent, tenant=tenant,
                              attach_id=attach_id, digest=digest)
            self.by_attach[attach_id] = session
            self._ip_sessions[station.ip] = self._ip_sessions.get(station.ip, 0) + 1
        payload = dict(v=transport.VERSION, session=session_id.hex(), agent=agent,
                       host=host, tenant=tenant, attach_id=attach_id,
                       receipt_digest=digest.hex(), deadline=self.config.max_session_seconds)
        if not host_peer.send_json(session_id, transport.PAIR, **payload):
            session.close(SLOW_PEER, "host is not reading", notify_station=True)
            return
        if not station.send_json(session_id, transport.PAIRED, v=transport.VERSION,
                                 session=session_id.hex(), attach_id=attach_id,
                                 receipt_digest=digest.hex()):
            session.close(SLOW_PEER, "station is not reading", notify_station=False)
            return
        self.note("session paired", station, session=session_id.hex(), agent=agent,
                  host=host, tenant=tenant, attach_id=attach_id)

    def _wait_host(self, host: str, tenant: str, *, deadline: float) -> HostPeer | None:
        while not self._stop.is_set():
            peer = self.host_for(host, tenant)
            if peer is not None:
                return peer
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        return None

    # -- supervision -------------------------------------------------------

    def _reap_loop(self) -> None:
        while not self._stop.wait(5.0):
            now = time.monotonic()
            with self._lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                if session.closed:
                    continue
                if now - session.created > self.config.max_session_seconds:
                    session.close(MAX_DURATION, "maximum session duration reached",
                                  notify_station=True)
                elif now - session.last_activity > self.config.idle_timeout:
                    session.close(IDLE, "session idle timeout", notify_station=True)

    # -- journal (métadonnées seulement) -----------------------------------

    def note(self, event: str, peer: _Peer | None, **fields) -> None:
        """Une ligne de journal sans contenu : ni reçu, ni clé, ni terminal."""
        parts = ["attach-relay", event]
        if peer is not None:
            parts.append("peer=%s" % (peer.ip or "?"))
        for name in ("host", "tenant", "agent", "session", "attach_id", "port",
                     "tls", "mesh", "code", "bytes_to_host", "bytes_to_station",
                     "seconds"):
            if name in fields and fields[name] not in (None, "", [], ()):
                parts.append("%s=%s" % (name, _short(fields[name])))
        self.log.info(" ".join(parts))


def _short(value) -> str:
    text = str(value)
    text = "".join(ch if ch.isprintable() and ch not in " \r\n\t" else "?" for ch in text)
    return text[:160]


class _FirstPeer(_Peer):
    """Lit la première trame, puis délègue au pair du bon rôle.

    Le rôle n'est connu qu'après `HELLO` ou `OPEN` ; un pair générique lit
    donc cette première trame, crée le pair définitif (qui possède alors la
    socket, son écrivain et ses compteurs) et lui passe la main. Aucune
    trame n'est écrite avant ce choix.
    """

    def __init__(self, relay, sock, address, secure):
        super().__init__(relay, sock, address, secure)
        self._delegate: _Peer | None = None

    def start(self) -> None:
        self._reader = threading.Thread(target=self.run, name="attach-relay-peer",
                                        daemon=True)
        self._reader.start()

    def run(self) -> None:
        delegate = None
        try:
            self.sock.settimeout(self.relay.config.handshake_timeout)
            first = transport.read_frame(self._recv, max_frame=self.relay.config.max_frame,
                                         frame_timeout=self.relay.config.handshake_timeout)
            if first is None:
                self.close("eof", notify=False)
                return
            if first.type == transport.HELLO:
                try:
                    hello = transport.parse_json(first.payload, "HELLO")
                except transport.ProtocolError as exc:
                    self.fail(exc.code, exc.message)
                    return
                delegate = HostPeer(self.relay, self.sock, self.address, self.secure,
                                    host=str(hello.get("host") or ""),
                                    tenant=str(hello.get("tenant") or ""))
            elif first.type == transport.OPEN:
                delegate = StationPeer(self.relay, self.sock, self.address, self.secure)
            else:
                self.fail(BAD_REQUEST, "first frame must be HELLO or OPEN")
                return
            delegate.ip = self.ip
            delegate.created = self.created
            delegate.last_activity = self.last_activity
            delegate.bytes_in = self.bytes_in + 4 + len(first.session) + 1 + len(first.payload)
            delegate._reader = self._reader
            delegate._writer = threading.Thread(target=delegate._write_loop,
                                                name="attach-relay-writer", daemon=True)
            self._delegate = delegate
            with self.relay._peer_lock:
                self.relay._peers.discard(self)
                self.relay._peers.add(delegate)
            delegate._writer.start()
            if first.type == transport.HELLO:
                delegate._hello(first)
            else:
                delegate._open(first)
            if delegate.close_code:
                return
            delegate._read_loop()
        except socket.timeout:
            (delegate or self).fail("timeout", "handshake timeout")
        except transport.ProtocolError as exc:
            (delegate or self).fail(exc.code, exc.message)
        except (OSError, ssl.SSLError):
            (delegate or self).close("io_error", notify=False)
        finally:
            target = delegate or self
            target.close(target.close_code or "closed", notify=False)

    def handle(self, frame):  # pragma: no cover - délégué après la première trame
        if self._delegate is not None:
            self._delegate.handle(frame)

    def _hello(self, frame):  # pragma: no cover
        raise AssertionError("first frame handled in run")

    def _open(self, frame):  # pragma: no cover
        raise AssertionError("first frame handled in run")
