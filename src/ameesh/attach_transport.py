# SPDX-License-Identifier: AGPL-3.0-only
"""Trames et chiffrement de bout en bout du relais d'attach (#7120, avatar D2).

Conception 5322 §5.3 : le poste et l'hôte de l'agent établissent une clé de
session (X25519 + HKDF-SHA-256) et échangent des trames AES-256-GCM ; le
relais ne voit que des trames chiffrées et des métadonnées de routage
(`session_id`, tailles, horodatages). Ce module ne fait aucune E/S : il
définit le format des trames, la machine de poignée de main et le canal
chiffré utilisés par `attach_relay` (service) et `attach_host` (connexion
sortante de l'hôte).

**Trame sur le fil** : ``longueur u32 (big endian) | session 16 octets |
type 1 octet | charge``. La longueur couvre session + type + charge ;
elle est bornée par `MAX_FRAME` avant toute lecture et une trame de
longueur nulle ou trop courte est refusée. Une session de contrôle (avant
appariement) porte l'identifiant nul `ZERO_SESSION`.

**Poignée de main** (par session, après appariement) : chaque extrémité
envoie `KEY` (version, rôle, clé publique X25519), dérive la clé partagée,
puis envoie `CONFIRM`. La transcription lie la version, l'identifiant de
session, l'agent, l'hôte, le tenant, l'`attach_id`, l'empreinte du reçu et
les deux clés publiques : deux sessions ne partagent jamais une clé, et un
reçu substitué change la transcription. La confirmation est chiffrée et
authentifiée ; un désaccord ferme la session (`handshake_failed`).

**Trames de données** : ``séquence u64 | chiffré+tag``. Le nonce AES-GCM
est la séquence sur 12 octets, l'AAD lie le type, la séquence et la
transcription. Chaque direction a sa clé et sa séquence : une trame
rejouée, réordonnée ou altérée est refusée (`replay`, `decrypt`). La
fenêtre d'acquittement (`DEFAULT_WINDOW`) borne les octets non acquittés ;
l'émetteur cesse de lire le PTY quand elle est pleine (contre-pression),
il ne bloque pas indéfiniment.

Aucun message d'erreur ne porte de charge utile : les codes et les tailles
suffisent au diagnostic et les journaux restent sans contenu.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import struct
from typing import Callable, Iterable

from . import jcs

#: protocole et version : une trame d'une autre version est refusée
PROTOCOL = "ameesh-attach-relay/1"
VERSION = 1

#: identifiant de session (16 octets) et identifiant nul des trames de contrôle
SESSION_BYTES = 16
ZERO_SESSION = b"\x00" * SESSION_BYTES

#: bornes des trames (octets) : la longueur est lue avant la charge
MAX_FRAME = 64 * 1024
MAX_CONTROL = 8 * 1024
MAX_DATA = 16 * 1024

#: fenêtre d'acquittement par direction et fréquence des ACK
DEFAULT_WINDOW = 1024 * 1024
ACK_EVERY = 64 * 1024

#: types de trame de contrôle (relais, en clair) et de session (bout en bout)
HELLO = 1
WELCOME = 2
OPEN = 3
PAIRED = 4
PAIR = 5
PAIR_NO = 6
RELAY_ERR = 7
PING = 8
PONG = 9

KEY = 20
CONFIRM = 21
REQ = 22
OK = 23
NO = 24
DATA = 25
WINSIZE = 26
SIGNAL = 27
ACK = 28
CLOSE = 29

ROLE_STATION = "station"
ROLE_HOST = "host"
_ROLE_BYTE = {ROLE_STATION: 1, ROLE_HOST: 2}
_ROLE_NAME = {1: ROLE_STATION, 2: ROLE_HOST}

#: types qu'un canal de bout en bout sait sceller/ouvrir, et leur borne
E2E_TYPES = frozenset({KEY, CONFIRM, REQ, OK, NO, DATA, WINSIZE, SIGNAL, ACK, CLOSE})
MAX_PAYLOAD = {
    KEY: 64,
    CONFIRM: 64,
    REQ: MAX_CONTROL,
    OK: MAX_CONTROL,
    NO: MAX_CONTROL,
    DATA: MAX_DATA,
    WINSIZE: 8,
    SIGNAL: 1,
    ACK: 8,
    CLOSE: 2048,
}

#: attente maximale d'une lecture quand aucune donnée n'est prête (s)
RECV_POLL = 0.1

#: surcharge d'une trame scellée : séquence (8) + tag GCM (16)
SEAL_OVERHEAD = 8 + 16


def wire_bound(ftype: int) -> int:
    """Borne de la charge SUR LE FIL pour un type (scellée ou en clair)."""
    bound = MAX_PAYLOAD.get(ftype, MAX_CONTROL)
    if ftype in E2E_TYPES and ftype != KEY:
        return bound + SEAL_OVERHEAD
    return bound


#: signaux qu'une station peut demander à l'hôte (jamais SIGKILL)
ALLOWED_SIGNALS = frozenset({1, 2, 3, 15, 18, 20})  # HUP, INT, QUIT, TERM, CONT, TSTP


class ProtocolError(Exception):
    """Trame invalide, poignée de main rompue ou chiffrement refusé.

    `code` est stable et destiné aux journaux ; `message` ne contient
    jamais la charge utile (ni clé, ni terminal, ni reçu).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CryptoUnavailable(RuntimeError):
    """`cryptography` absent : l'attach distant ne peut pas être sûr."""


def crypto_available() -> bool:
    try:
        _crypto()
    except CryptoUnavailable:
        return False
    return True


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey, X25519PublicKey)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise CryptoUnavailable(
            "the remote attach relay needs the 'cryptography' package: "
            "install ameesh[crypto]") from exc
    return hashes, serialization, X25519PrivateKey, X25519PublicKey, AESGCM, HKDF


# --------------------------------------------------------------------------
# trames
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Frame:
    session: bytes
    type: int
    payload: bytes

    def encode(self) -> bytes:
        return encode_frame(self.session, self.type, self.payload)


def check_session(session: bytes) -> bytes:
    if not isinstance(session, (bytes, bytearray)) or len(session) != SESSION_BYTES:
        raise ProtocolError("bad_session", "session id must be %d bytes" % SESSION_BYTES)
    return bytes(session)


def encode_frame(session: bytes, ftype: int, payload: bytes = b"") -> bytes:
    """La trame sur le fil ; refuse une charge hors bornes avant écriture."""
    session = check_session(session)
    if not isinstance(ftype, int) or not 0 < ftype < 256:
        raise ProtocolError("bad_type", "frame type must be 1..255")
    payload = bytes(payload or b"")
    bound = wire_bound(ftype)
    if len(payload) > bound:
        raise ProtocolError("payload_too_large", "type %d: %d bytes, maximum %d"
                            % (ftype, len(payload), bound))
    body = session + bytes([ftype]) + payload
    if len(body) + 4 > MAX_FRAME:
        raise ProtocolError("frame_too_large", "frame of %d bytes, maximum %d"
                            % (len(body) + 4, MAX_FRAME))
    return struct.pack(">I", len(body)) + body


def _read_exact(recv: Callable[[int], bytes], size: int) -> bytes | None:
    """`size` octets, ou None si le pair a fermé avant (fin propre).

    Un délai d'attente est rendu à l'appelant s'il tombe AVANT le premier
    octet (le tour de boucle peut alors vérifier inactivité et keepalive) ;
    au milieu d'une trame il est absorbé : une trame à moitié reçue ne doit
    jamais être perdue.
    """
    chunks = []
    remaining = size
    while remaining:
        try:
            chunk = recv(remaining)
        except TimeoutError:
            if not chunks:
                raise
            continue
        if not chunk:
            if remaining == size:
                return None
            raise ProtocolError("truncated", "connection closed inside a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(recv: Callable[[int], bytes], *, max_frame: int = MAX_FRAME) -> Frame | None:
    """Lit une trame d'un flux ; None sur fin de flux entre deux trames.

    `recv` est `socket.recv` ou un équivalent de test. Une longueur hors
    bornes est refusée AVANT de lire la charge (jamais d'allocation sur la
    foi d'un champ non vérifié).
    """
    header = _read_exact(recv, 4)
    if header is None:
        return None
    length = struct.unpack(">I", header)[0]
    if length < SESSION_BYTES + 1:
        raise ProtocolError("bad_length", "frame length %d too short" % length)
    if length > max_frame:
        raise ProtocolError("frame_too_large", "frame length %d, maximum %d"
                            % (length, max_frame))
    body = _read_exact(recv, length)
    if body is None:
        raise ProtocolError("truncated", "connection closed after a frame header")
    session = body[:SESSION_BYTES]
    ftype = body[SESSION_BYTES]
    payload = body[SESSION_BYTES + 1:]
    if not ftype:
        raise ProtocolError("bad_type", "frame type 0")
    return Frame(session, ftype, payload)


def json_payload(**fields) -> bytes:
    """Charge JSON compacte et triée (déterministe, sans flottants)."""
    return json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def parse_json(payload: bytes, what: str, *, limit: int = MAX_CONTROL) -> dict:
    if len(payload) > limit:
        raise ProtocolError("payload_too_large", "%s: %d bytes, maximum %d"
                            % (what, len(payload), limit))
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("bad_json", "%s: unreadable JSON" % what) from exc
    if not isinstance(value, dict):
        raise ProtocolError("bad_json", "%s: JSON object expected" % what)
    return value


def receipt_digest(receipt: dict) -> bytes:
    """Empreinte canonique du reçu, liée à la transcription de la session."""
    try:
        return hashlib.sha256(jcs.canonicalize(receipt)).digest()
    except jcs.JcsError as exc:
        raise ProtocolError("bad_receipt", "receipt cannot be canonicalized") from exc


# --------------------------------------------------------------------------
# canal de bout en bout
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Context:
    """Ce que les deux extrémités doivent connaître AVANT la poignée de main.

    Le relais fournit `attach_id` et `receipt_digest` dans ses trames
    d'appariement ; l'hôte vérifie ensuite que le reçu reçu dans `REQ`
    correspond à cette empreinte. Les deux côtés ne s'accordent donc pas
    sur un reçu qu'ils n'auraient pas vu.
    """

    session: bytes
    agent: str
    host: str
    tenant: str
    attach_id: str
    digest: bytes

    def __post_init__(self) -> None:
        check_session(self.session)
        if not isinstance(self.digest, (bytes, bytearray)) or len(self.digest) != 32:
            raise ProtocolError("bad_digest", "receipt digest must be 32 bytes")
        object.__setattr__(self, "digest", bytes(self.digest))
        for name in ("agent", "host", "tenant", "attach_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or "\x00" in value:
                raise ProtocolError("bad_context", "%s: invalid text" % name)


def _part(value: bytes) -> bytes:
    return struct.pack(">H", len(value)) + value


def transcript(context: Context, station_public: bytes, host_public: bytes) -> bytes:
    """Transcription liée : protocole, route, reçu, clés publiques."""
    for name, key in (("station", station_public), ("host", host_public)):
        if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
            raise ProtocolError("bad_key", "%s public key must be 32 bytes" % name)
    return b"".join((
        _part(PROTOCOL.encode("ascii")),
        _part(context.session),
        _part(context.agent.encode("utf-8")),
        _part(context.host.encode("utf-8")),
        _part(context.tenant.encode("utf-8")),
        _part(context.attach_id.encode("utf-8")),
        _part(context.digest),
        _part(bytes(station_public)),
        _part(bytes(host_public)),
    ))


def _aad(context: Context, digest: bytes, ftype: int, seq: int) -> bytes:
    return b"".join((
        PROTOCOL.encode("ascii"), b"|", bytes([ftype]), b"|",
        struct.pack(">Q", seq), b"|", digest,
    ))


class Channel:
    """Poignée de main et canal chiffré d'une session d'attach.

    Les méthodes ne font aucune E/S. `key_frame`/`confirm_frame` rendent la
    trame à envoyer ; `feed` rend les événements à traiter ; `seal`/`open`
    servent aux trames de données. Une erreur lève `ProtocolError` et la
    session doit être fermée : le canal ne réessaie jamais.
    """

    def __init__(self, role: str, context: Context, *,
                 window: int = DEFAULT_WINDOW, private_key: bytes | None = None):
        if role not in _ROLE_BYTE:
            raise ProtocolError("bad_role", "role %r" % (role,))
        if not isinstance(window, int) or window < MAX_DATA:
            raise ProtocolError("bad_window", "ack window must be at least %d" % MAX_DATA)
        _crypto()  # refuse tôt, avant toute session
        self.role = role
        self.context = context
        self.window = window
        self._private_raw = bytes(private_key) if private_key is not None else os.urandom(32)
        if len(self._private_raw) != 32:
            raise ProtocolError("bad_key", "X25519 private key must be 32 bytes")
        hashes, serialization, X25519PrivateKey, _X25519PublicKey, AESGCM, HKDF = _crypto()
        self._Hashes, self._Serialization, self._AESGCM, self._HKDF = hashes, serialization, AESGCM, HKDF
        self._private = X25519PrivateKey.from_private_bytes(self._private_raw)
        self._public = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        # notre propre clé occupe déjà sa place dans la transcription : seule
        # la clé du pair reste à recevoir, une seule fois (`_peer_key`)
        self._station_public: bytes | None = self._public if role == ROLE_STATION else None
        self._host_public: bytes | None = self._public if role == ROLE_HOST else None
        self._peer_key = False
        self._send_key: bytes | None = None
        self._recv_key: bytes | None = None
        self._transcript_hash: bytes | None = None
        self._sent_key = False
        self._sent_confirm = False
        self._peer_confirmed = False
        self._ready = False
        self._closed = False
        self._send_seq = 0
        self._recv_seq = 0
        self._sent_bytes = 0
        self._acked_bytes = 0
        self._recv_bytes = 0
        self._ack_pending = 0
        self._ack_sent = 0
        self._events: list[tuple[str, object]] = []

    # -- propriétés --------------------------------------------------------

    @property
    def public_key(self) -> bytes:
        return self._public

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def unacked(self) -> int:
        return self._sent_bytes - self._acked_bytes

    @property
    def can_send(self) -> bool:
        """La fenêtre d'acquittement permet-elle encore une trame de données ?"""
        return self._ready and self.unacked < self.window

    @property
    def received_bytes(self) -> int:
        return self._recv_bytes

    # -- poignée de main ---------------------------------------------------

    def key_frame(self) -> tuple[int, bytes]:
        if self._sent_key:
            raise ProtocolError("handshake_order", "KEY already sent")
        payload = bytes([VERSION, _ROLE_BYTE[self.role]]) + self._public
        self._sent_key = True
        return KEY, payload

    def confirm_frame(self) -> tuple[int, bytes] | None:
        if not self._keys_ready():
            raise ProtocolError("handshake_order", "CONFIRM before both public keys")
        if self._sent_confirm:
            return None
        payload = self._seal_with(0, CONFIRM, b"confirm")
        self._sent_confirm = True
        self._send_seq = 1
        if self._peer_confirmed:
            # la confirmation du pair est arrivée avant la nôtre (ordre
            # possible quand les deux côtés envoient leur KEY ensemble)
            self._ready = True
        return CONFIRM, payload

    def _keys_ready(self) -> bool:
        return self._send_key is not None and self._recv_key is not None

    def _derive(self) -> None:
        hashes, _serialization, _X25519PrivateKey, X25519PublicKey, _AESGCM, HKDF = _crypto()
        if self._station_public is None or self._host_public is None:
            return
        peer = self._host_public if self.role == ROLE_STATION else self._station_public
        try:
            shared = self._private.exchange(X25519PublicKey.from_public_bytes(peer))
        except ValueError as exc:
            raise ProtocolError("bad_key", "peer X25519 key unusable") from exc
        transcript_bytes = transcript(self.context, self._station_public, self._host_public)
        digest = hashlib.sha256(transcript_bytes).digest()
        self._transcript_hash = digest
        salt = digest
        station_to_host = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                               info=b"ameesh-attach-relay/1 station->host").derive(shared)
        host_to_station = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                               info=b"ameesh-attach-relay/1 host->station").derive(shared)
        if self.role == ROLE_STATION:
            self._send_key, self._recv_key = station_to_host, host_to_station
        else:
            self._send_key, self._recv_key = host_to_station, station_to_host

    def feed(self, ftype: int, payload: bytes) -> list[tuple[str, object]]:
        """Traite une trame de session ; rend les événements correspondants.

        Événements : `key`, `confirmed`, `request`, `accepted`, `refused`,
        `data`, `winsize`, `signal`, `ack`, `close`. L'appelant envoie la
        confirmation quand `key` est rendu (les deux clés sont alors là).
        """
        if self._closed:
            raise ProtocolError("closed", "channel is closed")
        if not isinstance(ftype, int) or ftype not in E2E_TYPES:
            raise ProtocolError("unexpected_type", "type %d is not a session frame" % ftype)
        payload = bytes(payload or b"")
        bound = wire_bound(ftype)
        if len(payload) > bound:
            raise ProtocolError("payload_too_large", "type %d: %d bytes, maximum %d"
                                % (ftype, len(payload), bound))
        self._events = []
        if ftype == KEY:
            self._feed_key(payload)
        elif ftype == CONFIRM:
            self._feed_confirm(payload)
        else:
            if not self._ready:
                raise ProtocolError("not_ready", "session frame type %d before handshake" % ftype)
            plaintext = self._open_with(self._recv_seq, ftype, payload)
            self._recv_seq += 1
            if ftype == DATA:
                self._recv_bytes += len(plaintext)
                self._ack_pending += len(plaintext)
                self._events.append(("data", plaintext))
            elif ftype == REQ:
                self._events.append(("request", parse_json(plaintext, "REQ")))
            elif ftype == OK:
                self._events.append(("accepted", parse_json(plaintext, "OK")))
            elif ftype == NO:
                self._events.append(("refused", parse_json(plaintext, "NO")))
            elif ftype == WINSIZE:
                if len(plaintext) != 8:
                    raise ProtocolError("bad_winsize", "WINSIZE: 8 bytes expected")
                rows, cols, xpix, ypix = struct.unpack(">HHHH", plaintext)
                if not rows or not cols:
                    raise ProtocolError("bad_winsize", "WINSIZE: rows and columns must be positive")
                self._events.append(("winsize", (rows, cols, xpix, ypix)))
            elif ftype == SIGNAL:
                if len(plaintext) != 1 or plaintext[0] not in ALLOWED_SIGNALS:
                    raise ProtocolError("bad_signal", "SIGNAL: signal not allowed")
                self._events.append(("signal", plaintext[0]))
            elif ftype == ACK:
                if len(plaintext) != 8:
                    raise ProtocolError("bad_ack", "ACK: 8 bytes expected")
                value = struct.unpack(">Q", plaintext)[0]
                if value < self._acked_bytes or value > self._sent_bytes:
                    raise ProtocolError("bad_ack", "ACK %d outside [%d, %d]"
                                        % (value, self._acked_bytes, self._sent_bytes))
                self._acked_bytes = value
                self._events.append(("ack", value))
            elif ftype == CLOSE:
                self._closed = True
                self._events.append(("close", parse_json(plaintext, "CLOSE")))
        return self._events

    def _feed_key(self, payload: bytes) -> None:
        if len(payload) != 2 + 32:
            raise ProtocolError("bad_key", "KEY: %d bytes, 34 expected" % len(payload))
        if payload[0] != VERSION:
            raise ProtocolError("bad_version", "KEY: version %d, %d expected"
                                % (payload[0], VERSION))
        if payload[1] not in _ROLE_NAME:
            raise ProtocolError("bad_role", "KEY: unknown role %d" % payload[1])
        role = _ROLE_NAME[payload[1]]
        if role == self.role:
            raise ProtocolError("bad_role", "KEY: peer claims our role %s" % role)
        if self._peer_key:
            raise ProtocolError("handshake_replay", "KEY: peer key already received")
        public = payload[2:]
        if public == b"\x00" * 32:
            raise ProtocolError("bad_key", "KEY: null public key")
        if role == ROLE_STATION:
            self._station_public = public
        else:
            self._host_public = public
        self._peer_key = True
        if self._station_public and self._host_public:
            self._derive()
        self._events.append(("key", role))

    def _feed_confirm(self, payload: bytes) -> None:
        if not self._keys_ready():
            raise ProtocolError("handshake_order", "CONFIRM before both public keys")
        if self._peer_confirmed:
            raise ProtocolError("handshake_replay", "CONFIRM already received")
        plaintext = self._open_with(0, CONFIRM, payload)
        if plaintext != b"confirm":
            raise ProtocolError("handshake_failed", "CONFIRM: wrong confirmation")
        self._peer_confirmed = True
        self._recv_seq = 1
        if self._sent_confirm:
            self._ready = True
            self._events.append(("confirmed", None))

    # -- chiffrement -------------------------------------------------------

    def _seal_with(self, seq: int, ftype: int, plaintext: bytes) -> bytes:
        if self._send_key is None or self._transcript_hash is None:
            raise ProtocolError("handshake_order", "no session key")
        nonce = seq.to_bytes(12, "big")
        sealed = self._AESGCM(self._send_key).encrypt(
            nonce, plaintext, _aad(self.context, self._transcript_hash, ftype, seq))
        return struct.pack(">Q", seq) + sealed

    def _open_with(self, expected: int, ftype: int, payload: bytes) -> bytes:
        if self._recv_key is None or self._transcript_hash is None:
            raise ProtocolError("handshake_order", "no session key")
        if len(payload) < 8:
            raise ProtocolError("bad_frame", "type %d: sequence missing" % ftype)
        seq = struct.unpack(">Q", payload[:8])[0]
        if seq != expected:
            raise ProtocolError("replay", "type %d: sequence %d, %d expected"
                                % (ftype, seq, expected))
        nonce = seq.to_bytes(12, "big")
        try:
            return self._AESGCM(self._recv_key).decrypt(
                nonce, payload[8:], _aad(self.context, self._transcript_hash, ftype, seq))
        except Exception as exc:  # InvalidTag ; jamais la charge dans le message
            raise ProtocolError("decrypt", "type %d: authentication failed" % ftype) from exc

    def seal(self, ftype: int, payload: bytes = b"") -> bytes:
        """Scelle une trame de session prête (DATA comprise)."""
        if self._closed:
            raise ProtocolError("closed", "channel is closed")
        if not self._ready:
            raise ProtocolError("not_ready", "session is not established")
        if ftype not in E2E_TYPES or ftype in (KEY, CONFIRM):
            raise ProtocolError("unexpected_type", "type %d cannot be sealed here" % ftype)
        payload = bytes(payload or b"")
        bound = MAX_PAYLOAD.get(ftype, MAX_CONTROL)
        if len(payload) > bound:
            raise ProtocolError("payload_too_large", "type %d: %d bytes, maximum %d"
                                % (ftype, len(payload), bound))
        sealed = self._seal_with(self._send_seq, ftype, payload)
        self._send_seq += 1
        if ftype == DATA:
            self._sent_bytes += len(payload)
        return sealed

    def ack_frame(self, *, force: bool = False) -> tuple[int, bytes] | None:
        """L'ACK cumulé, si le seuil est atteint ou si `force`."""
        if not self._ready or self._closed:
            return None
        if not force and self._ack_pending < ACK_EVERY:
            return None
        if self._recv_bytes == self._ack_sent:
            return None
        self._ack_pending = 0
        self._ack_sent = self._recv_bytes
        return ACK, struct.pack(">Q", self._recv_bytes)

    def close_frame(self, code: str = "normal", message: str = "") -> tuple[int, bytes]:
        if not self._ready:
            raise ProtocolError("not_ready", "session is not established")
        if self._closed:
            raise ProtocolError("closed", "channel is closed")
        payload = json_payload(code=str(code)[:64], message=str(message)[:500])
        self._closed = True
        return CLOSE, self._seal_with(self._send_seq, CLOSE, payload)


def handshake(station: Channel, host: Channel) -> None:
    """Poignée de main complète en mémoire (tests, bancs)."""
    key = station.key_frame()
    host_event = host.feed(*key)
    if ("key", ROLE_STATION) not in host_event:
        raise ProtocolError("handshake_failed", "host did not receive the station key")
    key = host.key_frame()
    station_event = station.feed(*key)
    if ("key", ROLE_HOST) not in station_event:
        raise ProtocolError("handshake_failed", "station did not receive the host key")
    confirm = host.confirm_frame()
    assert confirm is not None
    station.feed(*confirm)
    confirm = station.confirm_frame()
    assert confirm is not None
    host.feed(*confirm)
    if not station.ready or not host.ready:
        raise ProtocolError("handshake_failed", "key confirmation incomplete")
