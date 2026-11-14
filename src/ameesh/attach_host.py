# SPDX-License-Identifier: AGPL-3.0-only
"""Connexion sortante de l'hôte de l'agent au relais d'attach (#7120, D2).

L'hôte ne prend **aucun port entrant** : il ouvre une connexion TCP/TLS
sortante vers le relais, s'y annonce avec un jeton 0600, puis sert les
sessions appariées. Pour chaque session :

1. poignée de main X25519/AES-GCM (`attach_transport`) : l'hôte signe son
   `KEY` avec sa clé Ed25519, dont la clé publique est épinglée dans le
   canon du poste ;
2. `REQ` chiffré qui porte le reçu d'attach (D1), la preuve d'appareil —
   signée avec le hash de CETTE transcription — et ce hash ;
3. vérification et **consommation** du grant (`attach_grant.redeem`), par
   défaut sur la base et le canon de l'hôte — l'hôte est la porte ;
4. ouverture du PTY (`attach_pty`) et lancement de la commande interactive
   (le harnais, fourni par l'appelant ; D3 branche le bail et la session) ;
5. pont borné PTY ↔ trames, avec acquittements et contre-pression ; à la
   coupure, le groupe du harnais est arrêté et le bail sera rendu par D3.

Le module ne prend aucun bail et n'écrit pas le journal de conversation :
c'est le périmètre de D3. Il expose `HostConnector` et `GrantVerifier` pour
que D3 s'y branche sans recopier le transport.

Journaux : identité de routage, issue du grant, tailles, durée, code de
fermeture. Jamais le reçu, la preuve, la commande complète, ni un octet du
terminal.
"""
from __future__ import annotations

import dataclasses
import hmac
import logging
import os
import queue
import re
import select
import socket
import ssl
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from . import attach_grant, attach_pty, attach_transport as transport
from . import actions, canon as canon_mod, config as config_mod
from . import db as db_mod, receipts

log = logging.getLogger("ameesh.attach.host")

#: une entrée `REQ` bornée : reçu, preuve, taille de terminal
MAX_SIGNATURE = 1024
CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9_:.=-]{1,1024}$")
ATTACH_RE = re.compile(r"^att_[0-9a-z]{26}$")
TERM_RE = re.compile(r"^[A-Za-z0-9._+-]{0,64}$")
#: backoff de reconnexion au relais (secondes)
RECONNECT_MIN = 0.5
RECONNECT_MAX = 10.0


class HostConfigError(ValueError):
    """Configuration de la connexion refusée."""


class HostRefused(RuntimeError):
    """Le relais refuse l'hôte (jeton, version, rôle) : ne pas réessayer."""


@dataclasses.dataclass(frozen=True)
class AttachRequest:
    """La demande vérifiée par l'hôte, telle qu'elle traverse `REQ`."""

    session: str
    agent: str
    host: str
    tenant: str
    attach_id: str
    receipt: dict
    device_signature: bytes
    transcript_hash: bytes
    device_key: str
    rows: int = 24
    cols: int = 80
    xpix: int = 0
    ypix: int = 0
    term: str = ""
    #: #7121 : le poste demande l'attente de fin de tour (`--wait`) avant le
    #: bail ; sans ce drapeau, un tour en cours refuse l'attach.
    wait: bool = False


@dataclasses.dataclass(frozen=True)
class GrantResult:
    ok: bool
    code: str = ""
    reason: str = ""
    human: str = ""
    attach_id: str = ""

    def __bool__(self) -> bool:
        return self.ok


class GrantVerifier:
    """La porte : rend un `GrantResult` pour une demande de session."""

    def verify(self, request: AttachRequest) -> GrantResult:  # pragma: no cover
        raise NotImplementedError


def _canon_for(cfg, agent: str):
    for canon in canon_mod.load_configured(cfg):
        if canon.agent(agent) is not None:
            return canon
    return None


class DbGrantVerifier(GrantVerifier):
    """Vérifie et consomme le grant avec la base et le canon de l'hôte.

    C'est la porte de D1 (`attach_grant.redeem`) : refus par défaut, audit
    de chaque issue, nonce consommé une seule fois. La politique WebAuthn
    vient d'`AMEESH_APPROVE_RP_ID`/`AMEESH_APPROVE_ORIGINS` (ou est injectée
    pour les tests) ; sans elle, aucun grant n'est accepté.
    """

    def __init__(self, cfg=None, *, policy: receipts.Policy | None = None,
                 checked_by: str = "attach-relay", canon_loader: Callable | None = None):
        self._cfg = cfg
        self._policy = policy
        self._canon_loader = canon_loader
        self.checked_by = checked_by

    def verify(self, request: AttachRequest) -> GrantResult:
        cfg = self._cfg or config_mod.load()
        policy = self._policy if self._policy is not None else actions.policy_from_env()
        if not policy.rp_id or not policy.origins:
            return GrantResult(False, "policy_unavailable",
                               "AMEESH_APPROVE_RP_ID/AMEESH_APPROVE_ORIGINS missing")
        try:
            canon = (self._canon_loader(cfg, request.agent) if self._canon_loader
                     else _canon_for(cfg, request.agent))
        except Exception as exc:  # noqa: BLE001 — le canon est une entrée non fiable
            return GrantResult(False, "canon_unavailable",
                               "canon unreadable (%s)" % type(exc).__name__)
        try:
            db = db_mod.open_db(cfg)
        except Exception as exc:  # noqa: BLE001 — base inaccessible : refus
            return GrantResult(False, "audit_unavailable",
                               "database unreachable (%s)" % type(exc).__name__)
        try:
            grant = attach_grant.redeem(
                db, request.receipt, canon=canon, agent=request.agent, host=request.host,
                tenant=request.tenant, device_key=request.device_key,
                device_signature=request.device_signature,
                transcript_hash=request.transcript_hash, policy=policy,
                checked_by=self.checked_by)
        except Exception as exc:  # noqa: BLE001 — jamais d'ouverture sur erreur
            return GrantResult(False, "audit_unavailable",
                               "attach verification failed (%s)" % type(exc).__name__)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        return GrantResult(grant.ok, grant.code, grant.reason, human=grant.human,
                           attach_id=grant.attach_id)


@dataclasses.dataclass
class HostConfig:
    """Paramètres de la connexion sortante et des sessions servies."""

    relay_host: str
    relay_port: int
    token: str
    host: str
    tenant: str = ""
    agents: tuple = ()
    tls_context: ssl.SSLContext | None = None
    plaintext_loopback: bool = False
    command: Callable[[AttachRequest], list] | list | None = None
    host_signing_key: bytes | None = None
    env: dict | None = None
    cwd: str | None = None
    max_sessions: int = 4
    max_session_seconds: float = 2 * 3600.0
    request_timeout: float = 30.0
    keepalive: float = 30.0
    connect_timeout: float = 10.0

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                                                              self.host):
            raise HostConfigError("invalid host name")
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise HostConfigError("relay token must be at least 32 characters")
        if not 0 < self.relay_port <= 65535:
            raise HostConfigError("invalid relay port")
        if self.tls_context is None and not (self.plaintext_loopback
                                             and self.relay_host in ("127.0.0.1", "::1",
                                                                     "localhost")):
            raise HostConfigError(
                "TLS is required for the attach relay outside loopback "
                "(give --ca-file, or --plaintext-loopback on 127.0.0.1)")
        if not callable(self.command) and self.command is not None:
            argv = list(self.command)
            if not argv or not all(isinstance(part, str) and part for part in argv):
                raise HostConfigError("command must be a non-empty argv")
            if len(self.agents) != 1:
                raise HostConfigError(
                    "a static command requires exactly one --agent: it would "
                    "otherwise run the same harness for every agent of the host")
        if not 1 <= self.max_sessions <= 64:
            raise HostConfigError("max_sessions must be between 1 and 64")
        if not 1 <= self.max_session_seconds <= 24 * 3600:
            raise HostConfigError("max_session_seconds must be between 1 s and 24 h")
        if not 1 <= self.request_timeout <= 300:
            raise HostConfigError("request_timeout must be between 1 s and 5 min")
        for agent in self.agents:
            if not isinstance(agent, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", agent):
                raise HostConfigError("invalid agent name %r" % (agent,))
        if not isinstance(self.host_signing_key, (bytes, bytearray)) or len(
                bytes(self.host_signing_key)) != 32:
            raise HostConfigError(
                "host_signing_key: a 32-byte Ed25519 private key is required")

    @property
    def host_public_key(self) -> bytes:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.from_private_bytes(bytes(self.host_signing_key))
        return key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class HostSession:
    """Une session appariée côté hôte : preuve, PTY, pont, fermeture."""

    def __init__(self, connector: "HostConnector", session_id: bytes, *,
                 agent: str, tenant: str, attach_id: str, digest: bytes):
        self.connector = connector
        self.session_id = session_id
        self.agent = agent
        self.tenant = tenant
        self.attach_id = attach_id
        self.context = transport.Context(session=session_id, agent=agent,
                                         host=connector.config.host, tenant=tenant,
                                         attach_id=attach_id, digest=digest,
                                         host_key=connector.config.host_public_key)
        self.channel = transport.Channel(
            transport.ROLE_HOST, self.context,
            host_signing_key=connector.config.host_signing_key)
        self.inbox: queue.Queue = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.pty: attach_pty.PtyProcess | None = None
        self.created = time.monotonic()
        self.bytes_to_host = 0
        self.bytes_to_station = 0
        self.refusal = ""

    def _current_pty(self) -> attach_pty.PtyProcess | None:
        with self._lock:
            return self.pty

    # -- entrées -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="attach-host-session",
                                        daemon=True)
        self._thread.start()

    def feed(self, frame: transport.Frame) -> None:
        if self._closed.is_set():
            return
        try:
            self.inbox.put_nowait(frame)
        except queue.Full:
            self.close("slow_session", "session inbox full")

    def relay_error(self, code: str, message: str) -> None:
        if self._closed.is_set():
            return
        try:
            self.inbox.put_nowait(("relay-error", code, message))
        except queue.Full:
            self.close("slow_session", "session inbox full")

    def wait(self, timeout: float = 3.0) -> None:
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    # -- boucle ------------------------------------------------------------

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._current_pty() is None:
                    # pas de PTY avant REQ : on attend la trame suivante sans
                    # tourner à vide (la poignée de main est asynchrone), mais
                    # une session appariée qui n'envoie jamais REQ expire
                    if (time.monotonic() - self.created
                            > self.connector.config.request_timeout):
                        self.close("timeout", "no REQ before the deadline")
                        break
                    try:
                        item = self.inbox.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if not self._item(item):
                        break
                    continue
                self._drain()
                if self._stop.is_set() or self._current_pty() is None:
                    continue
                self._pump()
        finally:
            self.close(self.refusal or "closed")

    def _item(self, item) -> bool:
        if isinstance(item, tuple) and item and item[0] == "relay-error":
            self.close(item[1], item[2])
            return False
        self._handle(item)
        return True

    def _drain(self) -> None:
        for _ in range(64):
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return
            if not self._item(item):
                return

    def _handle(self, frame: transport.Frame) -> None:
        try:
            events = self.channel.feed(frame.type, frame.payload)
        except transport.ProtocolError as exc:
            self.close(exc.code, exc.message)
            return
        for kind, value in events:
            if kind == "key":
                # la station parle d'abord : l'hôte signe la transcription
                # avec les deux clés une fois la sienne reçue
                if not self.channel.key_sent:
                    self._send(*self.channel.key_frame())
                confirms = self.channel.confirm_frame()
                if confirms is not None:
                    self._send(*confirms)
            elif kind == "request":
                self._request(value)
            elif kind == "data":
                pty = self._current_pty()
                if pty is None:
                    self.close("not_ready", "DATA before the session is accepted")
                    return
                self.bytes_to_host += len(value)
                try:
                    pty.write(value)
                except attach_pty.PtyOverflow as exc:
                    self.close("pty_overflow", str(exc))
                    return
                except attach_pty.PtyError as exc:
                    self.close("pty_error", str(exc))
                    return
                ack = self.channel.ack_frame(force=True)
                if ack is not None:
                    self._send(*self._seal(*ack))
            elif kind == "winsize":
                pty = self._current_pty()
                if pty is not None:
                    rows, cols, xpix, ypix = value
                    pty.resize(rows, cols, xpix, ypix)
            elif kind == "signal":
                pty = self._current_pty()
                if pty is not None:
                    try:
                        pty.send_signal(value)
                    except attach_pty.PtyError as exc:
                        self.close("pty_error", str(exc))
                        return
            elif kind == "ack":
                pass
            elif kind == "close" or kind == "refused":
                self.close("peer_closed", "the station closed the session")
                return

    def _request(self, data: dict) -> None:
        if self._current_pty() is not None:
            self.close("protocol", "duplicate REQ")
            return
        try:
            request = self._parse_request(data)
        except (ValueError, transport.ProtocolError) as exc:
            self._refuse("bad_request", str(exc))
            return
        verifier = self.connector.verifier
        try:
            result = verifier.verify(request)
        except Exception as exc:  # noqa: BLE001 — la porte ne casse pas la session
            self._refuse("verifier_error", "grant verification failed (%s)"
                         % type(exc).__name__)
            return
        if not result.ok:
            self.connector.note("grant refused", self, code=result.code,
                                reason=result.reason)
            self._refuse(result.code or "refused", result.reason)
            return
        argv = self.connector.command_for(request)
        if not argv:
            self._refuse("command_unavailable", "no interactive command is configured")
            return
        env = dict(os.environ if self.connector.config.env is None
                   else self.connector.config.env)
        if request.term:
            env["TERM"] = request.term
        env.setdefault("AMEESH_ATTACH_REMOTE", "1")
        # #7121 (D3) : le wrapper hôte reçoit l'identité VÉRIFIÉE du grant
        # (jamais le reçu, le nonce ou la signature) ; il ne fait confiance
        # qu'à ces champs pour le bail, l'humain du journal et l'attente.
        env["AMEESH_ATTACH_HUMAN"] = result.human
        env["AMEESH_ATTACH_TENANT"] = request.tenant
        env["AMEESH_ATTACH_DEVICE"] = request.device_key
        env["AMEESH_ATTACH_ID"] = request.attach_id
        if request.wait:
            env["AMEESH_ATTACH_WAIT"] = "1"
        else:
            env.pop("AMEESH_ATTACH_WAIT", None)
        try:
            pty = attach_pty.PtyProcess(argv, env=env, cwd=self.connector.config.cwd,
                                        rows=request.rows, cols=request.cols)
        except attach_pty.PtyError as exc:
            self._refuse("spawn_failed", str(exc))
            return
        # fermeture concurrente pendant la vérification ou le spawn : le PTY
        # ne doit jamais survivre à la session qui l'a créé
        with self._lock:
            if self._closed.is_set():
                pty.close()
                return
            self.pty = pty
        self.connector.note("session accepted", self, human=result.human[:128],
                            attach_id=request.attach_id, rows=request.rows,
                            cols=request.cols, pid=pty.pid)
        self._send(*self._seal(transport.OK, transport.json_payload(
            v=transport.VERSION, attach_id=request.attach_id)))

    def _parse_request(self, data: dict) -> AttachRequest:
        receipt = data.get("receipt")
        if not isinstance(receipt, dict):
            raise ValueError("receipt: object expected")
        try:
            parsed = receipts.parse_receipt(receipt)
        except receipts.ReceiptError as exc:
            raise ValueError("receipt: %s" % exc) from exc
        if parsed.kind != "attach":
            raise ValueError("receipt: attach kind expected")
        if transport.receipt_digest(parsed.document) != self.context.digest:
            raise ValueError("receipt: digest differs from the paired one")
        if parsed.request.get("attach_id") != self.attach_id:
            raise ValueError("receipt: attach_id differs from the paired one")
        signature = data.get("device_signature")
        if not isinstance(signature, str):
            raise ValueError("device_signature: base64url text expected")
        try:
            raw_signature = receipts.b64u_decode(signature, "device_signature")
        except receipts.ReceiptError as exc:
            raise ValueError("device_signature: %s" % exc) from exc
        if not raw_signature or len(raw_signature) > MAX_SIGNATURE:
            raise ValueError("device_signature: %d bytes, maximum %d"
                             % (len(raw_signature), MAX_SIGNATURE))
        transcript = data.get("transcript_hash")
        if not isinstance(transcript, str):
            raise ValueError("transcript_hash: base64url text expected")
        try:
            transcript_hash = receipts.b64u_decode(transcript, "transcript_hash")
        except receipts.ReceiptError as exc:
            raise ValueError("transcript_hash: %s" % exc) from exc
        if len(transcript_hash) != 32:
            raise ValueError("transcript_hash: %d bytes, 32 expected"
                             % len(transcript_hash))
        if not self.channel.ready:
            raise ValueError("transcript_hash: the channel is not established")
        wait = data.get("wait", False)
        if not isinstance(wait, bool):
            raise ValueError("wait: boolean expected")
        if not hmac.compare_digest(transcript_hash, self.channel.transcript_hash):
            raise ValueError("transcript_hash: differs from this session")
        device_key = data.get("device_key")
        if not isinstance(device_key, str) or not CREDENTIAL_RE.fullmatch(device_key):
            raise ValueError("device_key: invalid credential id")
        if device_key != parsed.request.get("device_key"):
            raise ValueError("device_key: differs from the receipt")
        rows = _bounded_int(data.get("rows", 24), "rows", 1, 65535)
        cols = _bounded_int(data.get("cols", 80), "cols", 1, 65535)
        xpix = _bounded_int(data.get("xpix", 0), "xpix", 0, 65535)
        ypix = _bounded_int(data.get("ypix", 0), "ypix", 0, 65535)
        term = data.get("term", "") or ""
        if not isinstance(term, str) or not TERM_RE.fullmatch(term):
            raise ValueError("term: invalid terminal name")
        return AttachRequest(session=self.session_id.hex(), agent=self.agent,
                             host=self.connector.config.host, tenant=self.tenant,
                             attach_id=self.attach_id, receipt=parsed.document,
                             device_signature=raw_signature,
                             transcript_hash=transcript_hash, device_key=device_key,
                             rows=rows, cols=cols, xpix=xpix, ypix=ypix, term=term,
                             wait=wait)

    def _refuse(self, code: str, reason: str) -> None:
        self.refusal = code or "refused"
        if self.channel.ready and not self.channel.closed:
            try:
                self._send(*self._seal(transport.NO, transport.json_payload(
                    code=str(code)[:64], message=str(reason)[:300])))
            except transport.ProtocolError:
                pass
        self.close(self.refusal, reason)

    # -- pont PTY ----------------------------------------------------------

    def _pump(self) -> None:
        pty = self._current_pty()
        if pty is None:
            return
        if time.monotonic() - self.created > self.connector.config.max_session_seconds:
            self.close("max_duration", "maximum session duration reached")
            return
        read_fd = pty.master if self.channel.can_send else None
        write_fd = pty.master if pty.writable() else None
        try:
            ready, writable, _ = select.select(
                [read_fd] if read_fd is not None else [],
                [write_fd] if write_fd is not None else [], [], 0.05)
        except (OSError, ValueError):
            self.close("pty_error", "select on the pseudo-terminal failed")
            return
        if writable:
            try:
                pty.flush()
            except attach_pty.PtyError as exc:
                self.close("pty_error", str(exc))
                return
        if ready:
            chunk = pty.read(transport.MAX_DATA)
            if chunk:
                self.bytes_to_station += len(chunk)
                self._send(*self._seal(transport.DATA, chunk))
                if self._closed.is_set() or self._current_pty() is None:
                    return
            elif pty.eof:
                self.close("eof", "the terminal closed")
                return
        if pty.poll() is not None:
            self.close("exit", "the interactive command exited")

    def _seal(self, ftype: int, payload: bytes) -> tuple[int, bytes]:
        return ftype, self.channel.seal(ftype, payload)

    def _send(self, ftype: int, payload: bytes) -> None:
        if not self.connector.send(self.session_id, ftype, payload):
            self.close("relay_lost", "the relay connection is gone")

    # -- fermeture ---------------------------------------------------------

    def close(self, code: str = "closed", reason: str = "") -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._stop.set()
            pty, self.pty = self.pty, None
        if self.channel.ready and not self.channel.closed:
            try:
                self._send(*self.channel.close_frame(code, reason))
            except transport.ProtocolError:
                pass
        if pty is not None:
            pty.close()
        self.connector.unregister(self.session_id)
        self.connector.note("session closed", self, code=code,
                            bytes_to_host=self.bytes_to_host,
                            bytes_to_station=self.bytes_to_station,
                            seconds=round(time.monotonic() - self.created, 3))


class HostConnector:
    """Connexion sortante persistante + sessions ; reconnexion avec backoff."""

    def __init__(self, config: HostConfig, verifier: GrantVerifier, *,
                 logger: logging.Logger | None = None):
        self.config = config
        self.verifier = verifier
        self.log = logger or log
        self.sessions: dict[bytes, HostSession] = {}
        self._lock = threading.Lock()
        self._out: queue.Queue = queue.Queue(maxsize=256)
        self._sock: socket.socket | None = None
        self._writer: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._session_count = 0
        self._io_lock = threading.Lock()

    # -- cycle de vie ------------------------------------------------------

    def run_forever(self) -> None:
        delay = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                self._connect()
                delay = RECONNECT_MIN
                self._read_loop()
            except HostRefused as exc:
                self.log.error("attach-host: relay refused: %s", exc)
                self._stop.wait(5.0)
            except (OSError, ssl.SSLError, transport.ProtocolError) as exc:
                if not self._stop.is_set():
                    self.log.warning("attach-host: relay unavailable (%s); retrying in %.1fs",
                                     type(exc).__name__, delay)
                    self._stop.wait(delay)
                    delay = min(delay * 2.0, RECONNECT_MAX)
            finally:
                self._teardown()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._teardown()

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self._connected.wait(timeout)

    def wait(self, timeout: float | None = None) -> None:
        """Attend la fin du fil appelant (tests) — jamais bloquant sans borne."""
        pass

    # -- connexion ---------------------------------------------------------

    def _connect(self) -> None:
        self._teardown()
        sock = socket.create_connection((self.config.relay_host, self.config.relay_port),
                                        timeout=self.config.connect_timeout)
        try:
            sock.settimeout(self.config.connect_timeout)
            if self.config.tls_context is not None:
                sock = self.config.tls_context.wrap_socket(
                    sock, server_hostname=self.config.relay_host)
            self._sock = sock
            # HELLO/WELCOME synchrones : le lecteur et l'écrivain ne doivent
            # jamais se partager la SSLSocket avant la fin de la poignée
            hello = transport.encode_frame(
                transport.ZERO_SESSION, transport.HELLO,
                transport.json_payload(v=transport.VERSION, role="host",
                                       host=self.config.host,
                                       tenant=self.config.tenant,
                                       token=self.config.token))
            with self._io_lock:
                sock.sendall(hello)
            frame = transport.read_frame(self._recv, max_frame=transport.MAX_FRAME,
                                         frame_timeout=self.config.connect_timeout)
            if frame is None:
                raise OSError("relay closed before WELCOME")
            if frame.type == transport.RELAY_ERR:
                data = transport.parse_json(frame.payload, "RELAY_ERR")
                raise HostRefused(str(data.get("code") or "refused"))
            if frame.type != transport.WELCOME:
                raise transport.ProtocolError("protocol", "WELCOME expected")
            data = transport.parse_json(frame.payload, "WELCOME")
            if data.get("v") != transport.VERSION:
                raise transport.ProtocolError("protocol", "WELCOME version mismatch")
            sock.settimeout(1.0)
            self._out = queue.Queue(maxsize=256)
            self._writer = threading.Thread(target=self._write_loop,
                                            name="attach-host-writer", daemon=True)
            self._writer.start()
            self._connected.set()
            self.note("host connected", None, relay=self.config.relay_host,
                      port=self.config.relay_port)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
            raise

    def _teardown(self) -> None:
        self._connected.clear()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._out.put_nowait(None)
        except queue.Full:
            pass
        writer, self._writer = self._writer, None
        if writer is not None and writer is not threading.current_thread():
            writer.join(2.0)
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close("host_closed", "the relay connection is gone")

    # -- lecture / écriture ------------------------------------------------

    def _recv(self, size: int) -> bytes:
        # SSLSocket n'est pas thread-safe : lecture et écriture sérialisées
        with self._io_lock:
            return self._sock.recv(size)

    def _read_loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        sock.settimeout(transport.RECV_POLL)
        last_ping = time.monotonic()
        while not self._stop.is_set():
            try:
                frame = transport.read_frame(self._recv, max_frame=transport.MAX_FRAME)
            except socket.timeout:
                if time.monotonic() - last_ping >= self.config.keepalive:
                    self.send(transport.ZERO_SESSION, transport.PING)
                    last_ping = time.monotonic()
                continue
            if frame is None:
                raise OSError("relay closed the connection")
            if frame.type == transport.PING:
                self.send(frame.session, transport.PONG)
                continue
            if frame.type == transport.PONG:
                continue
            if frame.type == transport.PAIR:
                self._pair(frame)
                continue
            if frame.type == transport.RELAY_ERR:
                data = transport.parse_json(frame.payload, "RELAY_ERR")
                code = str(data.get("code") or "relay_error")
                if frame.session == transport.ZERO_SESSION:
                    raise HostRefused(code)
                session = self.sessions.get(frame.session)
                if session is not None:
                    session.relay_error(code, str(data.get("message") or ""))
                continue
            if frame.type == transport.PAIRED:
                continue
            if frame.type in (transport.PAIR_NO, transport.HELLO, transport.WELCOME,
                              transport.OPEN):
                raise transport.ProtocolError("protocol", "unexpected control frame %d"
                                              % frame.type)
            if frame.session == transport.ZERO_SESSION:
                raise transport.ProtocolError("protocol", "unexpected control frame %d"
                                              % frame.type)
            session = self.sessions.get(frame.session)
            if session is not None:
                session.feed(frame)

    def _write_loop(self) -> None:
        while True:
            item = self._out.get()
            if item is None:
                break
            sock = self._sock
            if sock is None:
                break
            try:
                with self._io_lock:
                    sock.sendall(item)
            except (OSError, ssl.SSLError):
                break

    def send(self, session_id: bytes, ftype: int, payload: bytes = b"") -> bool:
        if self._sock is None or self._stop.is_set():
            return False
        try:
            self._out.put_nowait(transport.encode_frame(session_id, ftype, payload))
        except queue.Full:
            return False
        except transport.ProtocolError:
            return False
        return True

    # -- sessions ----------------------------------------------------------

    def _pair(self, frame: transport.Frame) -> None:
        data = transport.parse_json(frame.payload, "PAIR")
        if data.get("v") != transport.VERSION:
            self._pair_no(frame.session, "protocol", "PAIR version mismatch")
            return
        if data.get("session") != frame.session.hex():
            self._pair_no(frame.session, "protocol", "PAIR session mismatch")
            return
        agent = data.get("agent")
        tenant = data.get("tenant", "")
        attach_id = data.get("attach_id")
        digest_hex = data.get("receipt_digest")
        if not isinstance(agent, str) or not agent or data.get("host") != self.config.host:
            self._pair_no(frame.session, "unknown_agent", "agent does not belong to this host")
            return
        if self.config.agents and agent not in self.config.agents:
            self._pair_no(frame.session, "unknown_agent", "agent is not served here")
            return
        if not isinstance(tenant, str):
            self._pair_no(frame.session, "protocol", "invalid tenant")
            return
        if tenant != self.config.tenant:
            self._pair_no(frame.session, "unknown_agent", "tenant does not match this host")
            return
        if not isinstance(attach_id, str) or not ATTACH_RE.fullmatch(attach_id):
            self._pair_no(frame.session, "protocol", "invalid attach id")
            return
        if not isinstance(digest_hex, str):
            self._pair_no(frame.session, "protocol", "invalid receipt digest")
            return
        try:
            digest = bytes.fromhex(digest_hex)
        except ValueError:
            digest = b""
        if len(digest) != 32:
            self._pair_no(frame.session, "protocol", "invalid receipt digest")
            return
        with self._lock:
            if len(self.sessions) >= self.config.max_sessions:
                self._pair_no(frame.session, "host_busy", "host is at its session quota")
                return
            if frame.session in self.sessions:
                self._pair_no(frame.session, "protocol", "duplicate session")
                return
            try:
                session = HostSession(self, frame.session, agent=agent, tenant=tenant,
                                      attach_id=attach_id, digest=digest)
            except transport.ProtocolError as exc:
                self._pair_no(frame.session, exc.code, exc.message)
                return
            self.sessions[frame.session] = session
        self.note("session paired", session, agent=agent, tenant=tenant,
                  attach_id=attach_id)
        session.start()

    def _pair_no(self, session: bytes, code: str, reason: str) -> None:
        self.send(session, transport.PAIR_NO,
                  transport.json_payload(code=code, reason=str(reason)[:200]))

    def unregister(self, session: bytes) -> None:
        with self._lock:
            self.sessions.pop(session, None)

    def command_for(self, request: AttachRequest) -> list:
        command = self.config.command
        if command is None:
            return []
        if callable(command):
            try:
                return list(command(request))
            except Exception as exc:  # noqa: BLE001 — refus, jamais de trace
                self.log.warning("attach-host: command builder failed (%s)",
                                 type(exc).__name__)
                return []
        return list(command)

    # -- journal -----------------------------------------------------------

    def note(self, event: str, session: HostSession | None, **fields) -> None:
        parts = ["attach-host", event]
        for name in ("agent", "tenant", "session", "attach_id", "human", "code",
                     "reason", "rows", "cols", "pid", "bytes_to_host",
                     "bytes_to_station", "seconds", "relay", "port"):
            if name in fields and fields[name] not in (None, "", [], ()):
                parts.append("%s=%s" % (name, _short(fields[name])))
        self.log.info(" ".join(parts))


def _short(value) -> str:
    text = "".join(ch if ch.isprintable() and ch not in " \r\n\t" else "?"
                   for ch in str(value))
    return text[:160]


def _bounded_int(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%s: integer between %d and %d expected" % (name, low, high))
    return value


def parse_relay_url(url: str, *, plaintext_loopback: bool = False,
                    ca_file: str | None = None, insecure: bool = False
                    ) -> tuple[str, int, ssl.SSLContext | None]:
    """`https://hôte:port` (TLS vérifié) ou `http://` sur la boucle locale.

    Aucune URL n'est déduite d'une autre : le schéma, l'hôte et le port sont
    explicites. Un `http://` hors boucle locale est refusé, et
    `--plaintext-loopback` doit être donné pour accepter le clair.
    """
    parts = urlsplit(url or "")
    if parts.scheme not in ("https", "http"):
        raise HostConfigError("relay URL must be https:// (or http:// on loopback)")
    if not parts.hostname:
        raise HostConfigError("relay URL has no host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise HostConfigError("relay URL must not carry credentials, query or fragment")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise HostConfigError("relay URL has an invalid port") from exc
    if parts.scheme == "http":
        if not plaintext_loopback or parts.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise HostConfigError("plaintext relay URL is only accepted on loopback "
                                  "with --plaintext-loopback")
        return parts.hostname, port, None
    if insecure:
        if parts.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise HostConfigError("--insecure-tls is only accepted on loopback")
        context = ssl._create_unverified_context()  # tests/loopback uniquement
    elif ca_file:
        context = ssl.create_default_context(cafile=os.path.abspath(
            os.path.expanduser(ca_file)))
    else:
        context = ssl.create_default_context()
    return parts.hostname, port, context


def token_from_file(path: str) -> str:
    """Jeton de relais dans un fichier 0600, jamais dans l'environnement."""
    path = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.stat(path)
    except OSError as exc:
        raise HostConfigError("cannot read the relay token file: %s" % exc.strerror) from exc
    from . import httpkit
    httpkit.check_private(st, "attach relay token", path, error=HostConfigError)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read(4096).strip()
    except OSError as exc:
        raise HostConfigError("cannot read the relay token file: %s" % exc.strerror) from exc
    if len(token) < 32 or any(ch.isspace() for ch in token):
        raise HostConfigError("relay token must be at least 32 non-space characters")
    return token


def host_key_from_file(path: str) -> bytes:
    """Clé privée Ed25519 de l'hôte, fichier 0600.

    Le fichier porte 32 octets bruts, ou leur écriture en hexadécimal (64
    caractères) ou en base64url (43 caractères). La clé publique
    correspondante est épinglée dans le canon du poste ; elle n'est jamais
    lue dans l'environnement.
    """
    path = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.stat(path)
    except OSError as exc:
        raise HostConfigError("cannot read the host key file: %s" % exc.strerror) from exc
    from . import httpkit
    httpkit.check_private(st, "attach host key", path, error=HostConfigError)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError as exc:
        raise HostConfigError("cannot read the host key file: %s" % exc.strerror) from exc
    if len(raw) == 32:
        return raw
    data = raw.strip()
    if len(data) == 32:
        key = data
    elif len(data) == 64:
        try:
            key = bytes.fromhex(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HostConfigError("host key file: invalid hexadecimal key") from exc
    else:
        try:
            key = receipts.b64u_decode(data.decode("ascii"), "host key")
        except (UnicodeDecodeError, receipts.ReceiptError) as exc:
            raise HostConfigError("host key file: 32 raw bytes, hex or base64url "
                                  "expected") from exc
    if len(key) != 32:
        raise HostConfigError("host key file: %d bytes, 32 expected" % len(key))
    return key
