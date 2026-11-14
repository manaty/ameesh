# SPDX-License-Identifier: AGPL-3.0-only
"""Poste d'un attach distant (#7121, avatar D3).

`ameesh attach <agent>` ouvre, depuis un poste enrôlé, une session sur un
agent épinglé sur un autre hôte : le harnais tourne sur l'hôte de l'agent, le
poste ne reçoit que des octets (D2, `attach_transport`), le grant est celui de
D1 (`attach_grant`). Ce module fournit :

- la lecture du **reçu signé** (fichier 0600, ou récupération après signature
  WebAuthn quand le poste a un accès `ameesh-approve`) et sa vérification
  locale contre l'agent, l'hôte, le tenant et la clé d'appareil ;
- la **clé d'appareil** du poste (fichier 0600) qui signe le challenge du
  reçu lié à la transcription de la session ;
- la **session relais** : `OPEN`, poignée de main E2E, `REQ` (avec l'attente
  de tour), puis le pont terminal brut (`DATA`, `WINSIZE`, `SIGNAL`, `ACK`,
  `CLOSE`) ;
- la restauration du terminal dans tous les cas, et aucun secret dans les
  journaux.

Le poste n'a besoin ni de base ni de credential base pour le transport : la
page de conversation est imprimée par l'hôte dans le PTY (D3, §5.4). S'il a
une base, `ameesh attach` résout l'hôte de l'agent ; sinon `--host` le donne.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import select
import signal
import socket
import ssl
import stat
import struct
import sys
import termios
import time
import tty

from . import attach_grant, attach_host, attach_session, attach_transport as transport
from . import approve_client, canon as canon_mod, config as config_mod
from . import db as db_mod, receipts

#: borne d'attente d'une trame de contrôle (poignée de main, OK/NO)
CONTROL_TIMEOUT = 60.0
#: attente maximale d'un reçu signé quand le poste l'a demandé (s)
GRANT_WAIT = 300.0
#: taille d'une lecture de terminal
READ_SIZE = 64 * 1024
#: fin d'une saisie non-TTY (tests, tube) : fermer proprement la session
EOF_CLOSE = 2.0


class StationError(RuntimeError):
    """Le poste refuse la session ; `code` est stable pour l'affichage."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code or "station"


@dataclasses.dataclass(frozen=True)
class DeviceKey:
    """La clé d'appareil du poste : identité publique + signature ES256."""

    credential_id: str
    key: object

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        return self.key.sign(message, ec.ECDSA(hashes.SHA256()))


@dataclasses.dataclass
class StationConfig:
    """Ce qu'il faut pour ouvrir une session distante, vérifié avant le réseau."""

    agent: str
    host: str
    tenant: str
    relay: str
    receipt: dict
    device: DeviceKey
    host_key: bytes
    wait: bool = False
    rows: int = 24
    cols: int = 80
    xpix: int = 0
    ypix: int = 0
    term: str = ""
    tls_context: ssl.SSLContext | None = None
    plaintext_loopback: bool = False
    ca_file: str | None = None
    insecure_tls: bool = False
    connect_timeout: float = 10.0
    #: #7121 (B1 relecture) : borne demandée par le poste (`--max`), portée par
    #: la trame REQ jusqu'au wrapper de l'hôte.
    max_seconds: float = 0.0


# --------------------------------------------------------------------------
# fichiers du poste : reçu, clé d'appareil, clé d'hôte
# --------------------------------------------------------------------------

def _private_file(path: str, what: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise StationError("file", "cannot read the %s: %s" % (what, exc.strerror)) from exc
    if stat.S_ISLNK(st.st_mode):
        raise StationError("file", "%s %s: real file expected (not a link)"
                           % (what, path))
    from . import httpkit
    try:
        # `check_private` lève l'erreur qu'on lui donne avec UN argument ;
        # `StationError` en attend deux. On garde donc `ValueError` puis on
        # traduit, plutôt que de fabriquer un `TypeError` sur un fichier 0644.
        httpkit.check_private(st, what, path)
    except ValueError as exc:
        raise StationError("file", str(exc)) from exc
    return path


def load_device_key(path: str) -> DeviceKey:
    """Lit la clé d'appareil (fichier 0600) : `{"credential_id", "private_key"}`.

    `private_key` est un PEM PKCS#8/SEC1 (P-256) ou le scalaire privé en
    base64url (32 octets). La partie publique correspondante est déclarée au
    canon comme authentificateur `device-es256` (D1) ; la clé privée ne
    quitte jamais le poste et n'est jamais envoyée dans une trame.
    """
    path = _private_file(path, "attach device key")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise StationError("file", "unreadable attach device key: %s"
                           % type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise StationError("file", "attach device key: JSON object expected")
    credential_id = data.get("credential_id")
    if not isinstance(credential_id, str) or not attach_session.DEVICE_RE.fullmatch(
            credential_id):
        raise StationError("file", "attach device key: invalid credential_id")
    raw = data.get("private_key")
    if not isinstance(raw, str) or not raw.strip():
        raise StationError("file", "attach device key: private_key expected")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        if raw.lstrip().startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(raw.encode("ascii"), password=None)
        else:
            scalar = receipts.b64u_decode(raw.strip(), "device private key")
            if len(scalar) != 32:
                raise StationError("file", "attach device key: 32-byte scalar expected")
            key = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
        if not isinstance(key, ec.EllipticCurvePrivateKey) or \
                not isinstance(key.curve, ec.SECP256R1):
            raise StationError("file", "attach device key: an EC P-256 key is required")
    except StationError:
        raise
    except Exception as exc:  # noqa: BLE001 — toute clé illisible est refusée
        raise StationError("file", "attach device key: unreadable key (%s)"
                           % type(exc).__name__) from exc
    return DeviceKey(credential_id=credential_id, key=key)


def load_receipt(path: str) -> dict:
    """Le reçu signé tel que `ameesh-approve` l'a rendu (fichier 0600)."""
    path = _private_file(path, "attach receipt")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(64 * 1024)
    except OSError as exc:
        raise StationError("file", "unreadable attach receipt: %s" % exc.strerror) from exc
    try:
        parsed = receipts.parse_receipt(raw)
    except receipts.ReceiptError as exc:
        raise StationError("receipt", "attach receipt refused: %s" % exc) from exc
    return parsed.document


def parse_host_key(value: str) -> bytes:
    """La clé publique Ed25519 de l'hôte : 32 octets bruts, hex ou base64url."""
    text = str(value or "").strip()
    if not text:
        raise StationError("host_key", "empty host key")
    raw = text.encode("ascii")
    if len(raw) == 32:
        return raw
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise StationError("host_key", "host key: invalid hexadecimal key") from exc
    try:
        key = receipts.b64u_decode(text, "host key")
    except receipts.ReceiptError as exc:
        raise StationError("host_key", "host key: 32 raw bytes, hex or base64url "
                           "expected") from exc
    if len(key) != 32:
        raise StationError("host_key", "host key: %d bytes, 32 expected" % len(key))
    return key


def load_host_key_file(path: str) -> bytes:
    """Clé publique Ed25519 épinglée, fichier 0600 (32 octets bruts, hex, b64u)."""
    path = _private_file(path, "attach host key")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError as exc:
        raise StationError("host_key", "unreadable host key: %s" % exc.strerror) from exc
    if len(raw) == 32:
        return raw
    return parse_host_key(raw.decode("ascii", "replace"))


def host_key_from_canon(cfg, host: str) -> bytes:
    """La clé d'hôte épinglée dans la carte du canon (D2, §5.2).

    Absente : refus explicite, jamais un TLS « suffisant » pour authentifier
    l'hôte (le relais pourrait sinon terminer le canal).
    """
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001 — canon illisible : pas de clé
        raise StationError("host_key", "canon unreadable (%s)"
                           % type(exc).__name__) from exc
    for canon in canons:
        card = canon.host(host)
        if card is None:
            continue
        value = (card.fiche.data or {}).get("attach_host_key")
        if isinstance(value, str) and value.strip():
            try:
                return parse_host_key(value)
            except StationError as exc:
                raise StationError("host_key", "%s: %s" % (host, exc)) from exc
    raise StationError("host_key", "no attach host key pinned for %s in the canon; "
                       "give --host-key-file" % host)


# --------------------------------------------------------------------------
# reçu : obtention et vérification locale
# --------------------------------------------------------------------------

def validate_receipt(receipt: dict, *, agent: str, host: str, tenant: str,
                     device_key: str, now: float | None = None) -> dict:
    """Vérifie le reçu AVANT le réseau : forme, liaison, validité, appareil.

    L'hôte et le relais refont la vérification qui fait foi ; ce contrôle
    local évite d'ouvrir une session pour un reçu d'un autre agent, d'un autre
    hôte, expiré, ou signé pour une autre clé d'appareil.
    """
    try:
        parsed = receipts.parse_receipt(receipt)
    except receipts.ReceiptError as exc:
        raise StationError("receipt", "attach receipt refused: %s" % exc) from exc
    if parsed.kind != "attach":
        raise StationError("receipt", "receipt of kind %s, attach expected" % parsed.kind)
    request = parsed.request
    if request.get("decision") != "approve":
        raise StationError("receipt", "attach receipt is not an approval")
    expected = {"agent": agent, "host": host, "tenant": tenant,
                "device_key": device_key}
    for field, value in expected.items():
        if request.get(field, "" if field == "tenant" else None) != value:
            raise StationError("receipt", "attach receipt is bound to another %s" % field)
    try:
        iat, exp = int(request["iat"]), int(request["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StationError("receipt", "attach receipt has no usable validity") from exc
    moment = time.time() if now is None else float(now)
    if exp <= moment:
        raise StationError("receipt", "attach receipt expired")
    if exp - iat > attach_grant.GRANT_TTL:
        raise StationError("receipt", "attach receipt valid more than %d s"
                           % attach_grant.GRANT_TTL)
    return parsed.document


def _fetch_signed(cfg, request_id: str, *, wait: float, out, err) -> dict:
    """Récupère un reçu signé ; attend la signature WebAuthn dans la borne."""
    deadline = time.monotonic() + max(0.0, float(wait))
    pending = False
    while True:
        try:
            status, payload = approve_client.fetch_receipt(cfg, request_id)
        except approve_client.ApproveClientError as exc:
            raise StationError("grant", "attach grant: %s" % exc.message) from exc
        if status == "signed":
            try:
                parsed = receipts.parse_receipt(bytes(payload))
            except receipts.ReceiptError as exc:
                raise StationError("grant", "attach grant receipt unreadable: %s"
                                   % exc) from exc
            return parsed.document
        if status == "expired":
            raise StationError("grant", "attach request %s expired without a receipt"
                               % request_id)
        if not pending:
            print("attach: request %s not signed yet; sign it on your phone"
                  % request_id, file=err)
            pending = True
        if time.monotonic() >= deadline:
            raise StationError("grant", "attach request %s still unsigned (link valid "
                               "until %s); rerun when signed"
                               % (request_id, time.strftime(
                                   "%H:%M:%S", time.localtime(
                                       float((payload or {}).get("link_exp") or 0)))))
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


def request_grant(cfg, *, agent: str, host: str, tenant: str, device_key: str,
                  approver: str | None, canon_ref: str | None, out, err,
                  wait: float = GRANT_WAIT) -> dict:
    """Demande un grant (D1) et attend la signature ; rend le reçu signé."""
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001
        raise StationError("grant", "canon unreadable (%s)" % type(exc).__name__) from exc
    canon = None
    for candidate in canons:
        if candidate.agent(agent) is not None:
            canon = candidate
            break
    if approver is None:
        humans, problems = (attach_grant.authorized(canon, agent) if canon else ([], []))
        if len(humans) != 1:
            raise StationError(
                "grant", "attach-grant: --approver human:<id> required (%s)"
                % (", ".join(humans) or "nobody may attach to %s" % agent))
        approver = humans[0]
    code, reason = attach_grant.check_authorized(canon, agent, approver)
    if code:
        raise StationError("grant", "attach-grant refused [%s]: %s" % (code, reason))
    requester = os.environ.get("AGENT_MAIL_NAME")
    requested_by = "agent:%s" % requester if requester else approver
    try:
        data = approve_client.create_attach_request(
            cfg, agent=agent, host=host, tenant=tenant, device_key=device_key,
            approver=approver, requested_by=requested_by,
            canon=(canon_ref if canon_ref is not None
                   else attach_grant.canon_key(canon)))
    except approve_client.ApproveClientError as exc:
        raise StationError("grant", "attach-grant: %s" % exc.message) from exc
    print("attach: grant %s for %s on %s; sign on your phone: %s"
          % (data.get("attach_id", "?"), agent, host, data.get("link", "?")), file=err)
    return _fetch_signed(cfg, data["request_id"], wait=wait, out=out, err=err)


# --------------------------------------------------------------------------
# la session relais côté poste
# --------------------------------------------------------------------------

class Session:
    """Une session appariée côté poste : poignée de main, REQ, pont."""

    def __init__(self, config: StationConfig):
        self.config = config
        self.sock: socket.socket | None = None
        self.session: bytes = transport.ZERO_SESSION
        self.channel: transport.Channel | None = None
        self.closed = False

    # -- cycle -------------------------------------------------------------

    def connect(self) -> None:
        cfg = self.config
        try:
            host, port, context = attach_host.parse_relay_url(
                cfg.relay, plaintext_loopback=cfg.plaintext_loopback,
                ca_file=cfg.ca_file, insecure=cfg.insecure_tls)
        except attach_host.HostConfigError as exc:
            raise StationError("relay", "attach relay URL refused: %s" % exc) from exc
        raw = socket.create_connection((host, port), timeout=cfg.connect_timeout)
        try:
            if context is not None:
                raw = context.wrap_socket(raw, server_hostname=host)
            raw.settimeout(CONTROL_TIMEOUT)
            self.sock = raw
            self._open()
            self._handshake()
            self._request()
        except Exception:
            self.close()
            raise

    def _read(self, timeout: float = CONTROL_TIMEOUT) -> transport.Frame | None:
        assert self.sock is not None
        self.sock.settimeout(timeout)
        return transport.read_frame(self.sock.recv)

    def _send(self, ftype: int, payload: bytes = b"", *, session: bytes | None = None) -> None:
        assert self.sock is not None
        self.sock.sendall(transport.encode_frame(
            self.session if session is None else session, ftype, payload))

    def _open(self) -> None:
        cfg = self.config
        self._send(transport.OPEN, transport.json_payload(
            v=transport.VERSION, role=transport.ROLE_STATION, agent=cfg.agent,
            host=cfg.host, tenant=cfg.tenant, receipt=cfg.receipt),
            session=transport.ZERO_SESSION)
        frame = self._read()
        if frame is None:
            raise StationError("relay", "the relay closed before pairing")
        if frame.type == transport.RELAY_ERR:
            code, message = _relay_error(frame)
            raise StationError(code, "attach relay refused: %s" % message)
        if frame.type != transport.PAIRED:
            raise StationError("relay", "PAIRED expected, got frame %d" % frame.type)
        data = transport.parse_json(frame.payload, "PAIRED")
        try:
            session = bytes.fromhex(str(data.get("session") or ""))
        except ValueError as exc:
            raise StationError("relay", "PAIRED: invalid session") from exc
        if len(session) != transport.SESSION_BYTES:
            raise StationError("relay", "PAIRED: invalid session length")
        if data.get("attach_id") != self.config.receipt["request"].get("attach_id"):
            raise StationError("relay", "PAIRED: another attach id")
        digest = _hex_digest(data.get("receipt_digest"))
        try:
            self.session = session
            self.channel = transport.Channel(
                transport.ROLE_STATION,
                transport.Context(session=session, agent=self.config.agent,
                                  host=self.config.host, tenant=self.config.tenant,
                                  attach_id=self.config.receipt["request"]["attach_id"],
                                  digest=digest, host_key=self.config.host_key))
        except transport.ProtocolError as exc:
            raise StationError(exc.code, "attach session refused: %s" % exc.message) from exc

    def _handshake(self) -> None:
        assert self.channel is not None
        self._send(*self.channel.key_frame())
        confirm_sent = False
        deadline = time.monotonic() + CONTROL_TIMEOUT
        while not self.channel.ready:
            if time.monotonic() >= deadline:
                raise StationError("handshake", "the host did not answer the handshake")
            try:
                frame = self._read(1.0)
            except TimeoutError:
                continue
            if frame is None:
                raise StationError("handshake", "the relay closed during the handshake")
            events = self._feed(frame)
            if any(kind == "key" for kind, _ in events) and not confirm_sent:
                confirms = self.channel.confirm_frame()
                if confirms is not None:
                    self._send(*confirms)
                    confirm_sent = True

    def _request(self) -> None:
        cfg = self.config
        assert self.channel is not None
        transcript_hash = self.channel.transcript_hash
        assert transcript_hash is not None
        signature = cfg.device.sign(attach_grant.device_message(
            receipts.challenge(cfg.receipt["request"]), transcript_hash))
        payload = transport.json_payload(
            receipt=cfg.receipt,
            device_signature=receipts.b64u_encode(signature),
            transcript_hash=receipts.b64u_encode(transcript_hash),
            device_key=cfg.device.credential_id, rows=cfg.rows, cols=cfg.cols,
            xpix=cfg.xpix, ypix=cfg.ypix, term=cfg.term, wait=bool(cfg.wait),
            max=int(cfg.max_seconds) if cfg.max_seconds else 0)
        self._send(transport.REQ, self.channel.seal(transport.REQ, payload))
        deadline = time.monotonic() + CONTROL_TIMEOUT
        while True:
            if time.monotonic() >= deadline:
                raise StationError("session", "the host did not answer the attach "
                                   "request within %ds" % int(CONTROL_TIMEOUT))
            try:
                frame = self._read(1.0)
            except TimeoutError:
                continue
            if frame is None:
                raise StationError("session", "the relay closed before the answer")
            for kind, value in self._feed(frame):
                if kind == "accepted":
                    return
                if kind == "refused":
                    code = str((value or {}).get("code") or "refused")
                    reason = str((value or {}).get("reason") or "")
                    raise StationError(code, "attach refused: %s" % (reason or code))

    def _feed(self, frame: transport.Frame) -> list:
        assert self.channel is not None
        try:
            return self.channel.feed(frame.type, frame.payload)
        except transport.ProtocolError as exc:
            raise StationError(exc.code, "attach session: %s" % exc.message) from exc

    def close(self, code: str = "normal") -> None:
        if self.closed:
            return
        self.closed = True
        channel = self.channel
        try:
            if channel is not None and channel.ready and not channel.closed:
                frame = channel.close_frame(code)
                self._send(*frame)
        except (OSError, transport.ProtocolError):
            pass
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass

    # -- pont --------------------------------------------------------------

    def pump(self, *, stdin_fd: int | None, stdout_fd: int, stop, resize) -> int:
        """Pont terminal brut ; rend un code de sortie.

        `stop` et `resize` sont des `threading.Event` posés par les
        gestionnaires de signaux du CLI. Une déconnexion du relais est un
        échec de session (code 1), jamais un gel ; la fermeture demandée par
        le poste rend 0.
        """
        assert self.sock is not None and self.channel is not None
        channel = self.channel
        while not stop.is_set():
            if resize.is_set():
                resize.clear()
                # B2 (relecture cx2) : un SIGWINCH peut en cacher un autre ;
                # relire la taille réelle, jamais la valeur du démarrage.
                size = _read_terminal_size(stdout_fd)
                if size is not None:
                    (self.config.rows, self.config.cols,
                     self.config.xpix, self.config.ypix) = size
                self._send(transport.WINSIZE, channel.seal(
                    transport.WINSIZE, struct.pack(
                        ">HHHH", self.config.rows, self.config.cols,
                        min(self.config.xpix, 65535), min(self.config.ypix, 65535))))
            watch = [self.sock]
            if stdin_fd is not None:
                watch.append(stdin_fd)
            try:
                ready, _, _ = select.select(watch, [], [], 0.2)
            except InterruptedError:
                continue
            if self.sock in ready:
                try:
                    frame = transport.read_frame(self.sock.recv)
                except TimeoutError:
                    frame = None
                except transport.ProtocolError as exc:
                    self.close(exc.code)
                    return 1
                if frame is None:
                    return 1  # le relais a fermé : coupure, pas un gel
                for kind, value in self._feed(frame):
                    if kind == "data":
                        _write_all(stdout_fd, bytes(value))
                        ack = channel.ack_frame()
                        if ack is not None:
                            self._send(transport.ACK, channel.seal(transport.ACK, ack[1]))
                    elif kind == "close" or kind == "refused":
                        return 0 if kind == "close" else 1
            if stdin_fd is not None and stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, READ_SIZE)
                except OSError:
                    data = b""
                if data:
                    self._send(transport.DATA, channel.seal(transport.DATA, data))
                else:
                    self.close("eof")
                    return 0
            ack = channel.ack_frame()
            if ack is not None:
                self._send(transport.ACK, channel.seal(transport.ACK, ack[1]))
        self.close("detached")
        return 0


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except OSError:
            return
        view = view[written:]


def _hex_digest(value) -> bytes:
    try:
        digest = bytes.fromhex(str(value or ""))
    except ValueError as exc:
        raise StationError("relay", "PAIRED: invalid receipt digest") from exc
    if len(digest) != 32:
        raise StationError("relay", "PAIRED: invalid receipt digest length")
    return digest


def _relay_error(frame: transport.Frame) -> tuple[str, str]:
    try:
        data = transport.parse_json(frame.payload, "RELAY_ERR")
    except transport.ProtocolError:
        return "relay", "unreadable refusal"
    return str(data.get("code") or "relay"), str(data.get("message") or "")


def _read_terminal_size(fd: int) -> tuple[int, int, int, int] | None:
    """Taille réelle du terminal, ou None si `fd` n'en est pas un (tube)."""
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        return None
    return max(1, size.lines), max(1, size.columns), 0, 0


def _terminal_size(fd: int | None = None) -> tuple[int, int, int, int]:
    fd = sys.stdout.fileno() if fd is None else fd
    return _read_terminal_size(fd) or (24, 80, 0, 0)


def run_session(config: StationConfig, *, stdin_fd: int | None = None,
                stdout_fd: int | None = None) -> int:
    """Ouvre la session, met le terminal en brut, pompe, restaure le terminal."""
    import threading
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    stdout_fd = sys.stdout.fileno() if stdout_fd is None else stdout_fd
    rows, cols, xpix, ypix = _terminal_size(stdout_fd)
    if not config.rows:
        config.rows, config.cols, config.xpix, config.ypix = rows, cols, xpix, ypix
    stop, resize = threading.Event(), threading.Event()
    previous: dict = {}
    old_term = None
    try:
        session = Session(config)
        session.connect()
        raw_fd = stdin_fd
        try:
            old_term = termios.tcgetattr(raw_fd) if os.isatty(raw_fd) else None
            if old_term is not None:
                tty.setraw(raw_fd)
        except (OSError, termios.error):
            old_term = None

        def on_stop(_signum, _frame):
            stop.set()

        def on_resize(_signum, _frame):
            resize.set()

        for signum, handler in ((signal.SIGTERM, on_stop), (signal.SIGHUP, on_stop),
                                (signal.SIGINT, on_stop), (signal.SIGWINCH, on_resize)):
            try:
                previous[signum] = signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        try:
            return session.pump(stdin_fd=raw_fd, stdout_fd=stdout_fd,
                                stop=stop, resize=resize)
        finally:
            session.close()
    except StationError as exc:
        print("ameesh attach: refused [%s]: %s" % (exc.code, exc), file=sys.stderr)
        return 1
    except (OSError, ssl.SSLError) as exc:
        print("ameesh attach: relay unreachable (%s)" % type(exc).__name__,
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — jamais de trace ni de chemin secret
        print("ameesh attach: session failed (%s)" % type(exc).__name__,
              file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if old_term is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
            except (OSError, termios.error):
                pass


# --------------------------------------------------------------------------
# CLI : `ameesh attach` distant
# --------------------------------------------------------------------------

def resolve_host(cfg, agent: str, host: str | None) -> tuple[str, str]:
    """(hôte, tenant) de l'agent : `--host` d'abord, sinon la base locale.

    Un poste sans credential base donne `--host` et `--tenant` (le reçu les
    porte) ; un poste qui a la base résout l'agent sans le deviner.
    """
    if host:
        return host, ""
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        raise StationError(
            "host", "cannot resolve %s without the database (%s); give --host"
            % (agent, type(exc).__name__)) from exc
    try:
        from . import registry
        row = registry.get(db, agent) or {}
    finally:
        db.close()
    pinned = row.get("host") or ""
    if not pinned:
        raise StationError("host", "%s has no execution host" % agent)
    return pinned, ""


def main(cfg, args) -> int:
    """Entrée `ameesh attach` en mode distant (arguments déjà analysés)."""
    out, err = sys.stdout, sys.stderr
    if not args.relay:
        raise StationError("relay", "distant attach needs --relay URL "
                           "(or AMEESH_ATTACH_RELAY)")
    if args.device_key_file is None:
        raise StationError("device", "distant attach needs --device-key-file "
                           "(the station's enrolled device key)")
    if args.receipt and args.grant:
        raise StationError("grant", "--receipt and --grant are exclusive")
    device = load_device_key(args.device_key_file)
    if args.device_key and args.device_key != device.credential_id:
        raise StationError("device", "--device-key differs from the device key file")
    host, tenant = resolve_host(cfg, args.agent, args.host)
    if tenant == "" and args.tenant:
        tenant = args.tenant
    if host == cfg.host:
        raise StationError("agent", "%s is pinned to this host: use a local attach "
                           "(no --relay)" % args.agent)
    if args.receipt:
        receipt = load_receipt(args.receipt)
    elif args.grant:
        receipt = _fetch_signed(cfg, args.grant, wait=args.grant_wait, out=out, err=err)
    else:
        receipt = request_grant(cfg, agent=args.agent, host=host, tenant=tenant,
                                device_key=device.credential_id,
                                approver=args.approver, canon_ref=args.canon,
                                out=out, err=err, wait=args.grant_wait)
    validate_receipt(receipt, agent=args.agent, host=host, tenant=tenant,
                     device_key=device.credential_id)
    if args.host_key_file:
        host_key = load_host_key_file(args.host_key_file)
    elif args.host_key:
        host_key = parse_host_key(args.host_key)
    else:
        host_key = host_key_from_canon(cfg, host)
    term = (os.environ.get("TERM") or "xterm")[:64]
    if not attach_host.TERM_RE.fullmatch(term):
        term = "xterm"
    config = StationConfig(
        agent=args.agent, host=host, tenant=tenant, relay=args.relay,
        receipt=receipt, device=device, host_key=host_key, wait=bool(args.wait),
        rows=0, cols=0, term=term,
        plaintext_loopback=bool(getattr(args, "plaintext_loopback", False)),
        ca_file=getattr(args, "ca_file", None),
        insecure_tls=bool(getattr(args, "insecure_tls", False)),
        connect_timeout=float(getattr(args, "connect_timeout", 10.0)),
        max_seconds=float(getattr(args, "max", 0.0) or 0.0))
    return run_session(config)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Options distantes ajoutées à l'analyseur d'`ameesh attach`."""
    parser.add_argument("--host", default=None,
                        help="hôte de l'agent (sinon la base locale)")
    parser.add_argument("--tenant", default="", help="tenant de l'hôte partagé")
    parser.add_argument("--relay", default=os.environ.get("AMEESH_ATTACH_RELAY"),
                        help="URL du relais (défaut : AMEESH_ATTACH_RELAY)")
    parser.add_argument("--grant", default=None,
                        help="request id req_… déjà signé (ou à signer)")
    parser.add_argument("--receipt", default=None,
                        help="fichier 0600 du reçu signé (poste sans ameesh-approve)")
    parser.add_argument("--device-key-file", default=None,
                        help="clé d'appareil du poste, fichier 0600")
    parser.add_argument("--device-key", default=None,
                        help="credential_id attendu (contrôle du fichier)")
    parser.add_argument("--host-key-file", default=None,
                        help="clé publique Ed25519 de l'hôte, fichier 0600")
    parser.add_argument("--host-key", default=None,
                        help="clé publique Ed25519 de l'hôte (hex/base64url)")
    parser.add_argument("--approver", default=None,
                        help="humain qui signe le grant (sinon l'unique autorisé)")
    parser.add_argument("--canon", default=None, help="canon du grant")
    parser.add_argument("--grant-wait", type=float, default=GRANT_WAIT,
                        help="attente d'une signature WebAuthn (s, défaut 300)")
    parser.add_argument("--ca-file", default=None,
                        help="CA du certificat du relais")
    parser.add_argument("--insecure-tls", action="store_true",
                        help="tests locaux seulement : TLS non vérifié sur la boucle locale")
    parser.add_argument("--plaintext-loopback", action="store_true",
                        help="tests locaux : relais en clair sur 127.0.0.1")
    parser.add_argument("--connect-timeout", type=float, default=10.0,
                        help=argparse.SUPPRESS)
