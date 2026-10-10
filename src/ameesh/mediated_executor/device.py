# SPDX-License-Identifier: AGPL-3.0-only
"""Côté appareil (lot L110) : la clé de l'exécuteur, l'enrôlement, les jetons.

Dans la VM Linux de l'appareil prêté :

* **la clé** P-256 de l'exécuteur est générée sur place et gardée dans le
  volume persistant de la VM (`/var/lib/ameesh-exec/key.pem`, PKCS#8 PEM,
  fichier `0600` dans un dossier `0700` ; `AMEESH_EXEC_HOME` change le
  dossier). Une clé lisible par d'autres que son propriétaire est refusée,
  comme le fait ssh. Le propriétaire de l'appareil peut la lire (étude §3.1) :
  elle prouve QUEL appareil parle, pas son honnêteté ;
* **l'enrôlement** présente le code à usage unique, la clé publique et la
  preuve de possession (`POST /api/exec/v1/enroll`) ; il peut joindre la
  liaison à la clé d'appareil Nexlink (`device_attestation`), signée hors
  d'ici par l'application de bureau sur `attestation_challenge` ;
* **les jetons d'accès** (10 min) s'obtiennent par une assertion ES256 de
  60 s au plus (`POST /api/exec/v1/token`) : `HttpTokenSource` implémente
  `interfaces.TokenSource` pour le transport de L109.

Signature : `cryptography` quand il est importable (extra `crypto`, présent
dans l'image de la VM, L114) ; sinon un repli pur Python, déterministe
(RFC 6979), qui n'est PAS à temps constant. Le repli suffit à un banc d'essai
ou à une VM dont le propriétaire peut lire la clé de toute façon ;
`AMEESH_EXEC_SIGNER=cryptography` l'interdit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

from .. import p256
from ..db import Unavailable
from . import contract as C
from . import jose
from .jose import normalize_code
from .interfaces import ASSERTION_MAX_TTL_S, TokenSource

DEFAULT_HOME = "/var/lib/ameesh-exec"
KEY_FILE = "key.pem"
STATE_FILE = "executor.json"
SCHEMA_STATE = "ameesh-exec-device/1"
API_PREFIX = "/api/exec/v1"
#: un jeton en cache sert tant qu'il vit encore plus longtemps que ceci
TOKEN_MARGIN_S = 60.0
#: durée demandée pour une assertion (le serveur en refuse plus de 60 s)
ASSERTION_TTL_S = 50

_PKCS8_HEAD = bytes.fromhex("3041020100301306072a8648ce3d020106082a8648ce3d030107"
                            "042730250201010420")
_PEM_HEAD = "-----BEGIN PRIVATE KEY-----"
_PEM_FOOT = "-----END PRIVATE KEY-----"


class DeviceError(RuntimeError):
    """Clé ou état de l'appareil inutilisable (droits, format)."""


def home(path: str | None = None) -> str:
    return path or os.environ.get("AMEESH_EXEC_HOME") or DEFAULT_HOME


# --------------------------------------------------------------------------
# signature ES256
# --------------------------------------------------------------------------

def _rfc6979_k(d: int, digest: bytes) -> int:
    """k déterministe (RFC 6979 §3.2, HMAC-SHA-256, qlen = hlen = 256)."""
    x = d.to_bytes(32, "big")
    h = (int.from_bytes(digest, "big") % p256.N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < p256.N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_pure(d: int, message: bytes) -> bytes:
    """ES256 `r‖s` (64 octets), RFC 6979. Pas à temps constant."""
    digest = hashlib.sha256(message).digest()
    e = int.from_bytes(digest, "big")
    k = _rfc6979_k(d, digest)
    point = p256.scalar_mult(k, (p256.GX, p256.GY))
    r = point[0] % p256.N
    s = pow(k, -1, p256.N) * (e + r * d) % p256.N
    if r == 0 or s == 0:  # probabilité négligeable ; RFC 6979 §3.4 : k suivant
        raise DeviceError("signature dégénérée")
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _signer_wanted() -> str:
    return (os.environ.get("AMEESH_EXEC_SIGNER") or "auto").strip().lower()


class DeviceKey:
    """La clé P-256 de l'exécuteur (scalaire privé `d`)."""

    def __init__(self, d: int, path: str | None = None):
        if not (1 <= d < p256.N):
            raise DeviceError("clé privée P-256 hors bornes")
        self._d = d
        self.path = path
        self.point = p256.scalar_mult(d, (p256.GX, p256.GY))
        self._crypto = None
        wanted = _signer_wanted()
        if wanted in ("auto", "cryptography"):
            try:
                from cryptography.hazmat.primitives.asymmetric import ec
                self._crypto = ec.derive_private_key(d, ec.SECP256R1())
            except ImportError:
                if wanted == "cryptography":
                    raise DeviceError("AMEESH_EXEC_SIGNER=cryptography : module absent "
                                      "(pip install 'ameesh[crypto]')")

    @classmethod
    def generate(cls) -> "DeviceKey":
        return cls(secrets.randbelow(p256.N - 1) + 1)

    @property
    def signer(self) -> str:
        return "cryptography" if self._crypto is not None else "pure"

    def public_jwk(self) -> dict:
        return jose.jwk_from_point(self.point)

    def thumbprint(self) -> str:
        return jose.thumbprint(self.public_jwk())

    def sign(self, message: bytes) -> bytes:
        """Signature ES256 `r‖s` (64 octets)."""
        if self._crypto is not None:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec, utils
            der = self._crypto.sign(message, ec.ECDSA(hashes.SHA256()))
            r, s = utils.decode_dss_signature(der)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return sign_pure(self._d, message)

    def sign_b64(self, message: bytes) -> str:
        return jose.b64u(self.sign(message))

    # -- fichier ------------------------------------------------------------
    def pem(self) -> str:
        der = _PKCS8_HEAD + self._d.to_bytes(32, "big")
        body = base64.b64encode(der).decode("ascii")
        return "%s\n%s\n%s\n" % (_PEM_HEAD, body, _PEM_FOOT)

    @classmethod
    def from_pem(cls, text: str, path: str | None = None) -> "DeviceKey":
        lines = [ln.strip() for ln in text.strip().splitlines()]
        if len(lines) < 3 or lines[0] != _PEM_HEAD or lines[-1] != _PEM_FOOT:
            raise DeviceError("clé de l'exécuteur : PEM PKCS#8 attendu")
        try:
            der = base64.b64decode("".join(lines[1:-1]), validate=True)
        except ValueError as exc:
            raise DeviceError("clé de l'exécuteur : base64 illisible") from exc
        if len(der) == len(_PKCS8_HEAD) + 32 and der.startswith(_PKCS8_HEAD):
            return cls(int.from_bytes(der[len(_PKCS8_HEAD):], "big"), path)
        # une clé P-256 écrite par un autre outil (avec clé publique jointe…)
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.serialization import load_der_private_key
            key = load_der_private_key(der, password=None)
        except ImportError:
            raise DeviceError("clé de l'exécuteur : format non reconnu sans `cryptography`")
        except ValueError as exc:
            raise DeviceError("clé de l'exécuteur illisible : %s" % exc) from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise DeviceError("clé de l'exécuteur : P-256 attendue")
        return cls(key.private_numbers().private_value, path)


def _check_private(path: str, what: str) -> None:
    """Refuse un fichier ou dossier ouvert à d'autres que son propriétaire."""
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise DeviceError("%s %s : n'appartient pas à l'utilisateur courant" % (what, path))
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise DeviceError("%s %s : droits %o trop ouverts (0600 attendu, comme ssh)"
                          % (what, path, stat.S_IMODE(st.st_mode)))


def _ensure_dir(directory: str) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _check_private(directory, "dossier")


def _write_private(path: str, text: str, *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    if not exclusive:
        tmp = path + ".tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, flags | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def load_key(directory: str | None = None) -> DeviceKey:
    path = os.path.join(home(directory), KEY_FILE)
    _check_private(path, "clé")
    with open(path, encoding="ascii") as fh:
        return DeviceKey.from_pem(fh.read(), path)


def load_or_create_key(directory: str | None = None) -> tuple[DeviceKey, bool]:
    """(clé, créée ?) : la clé du volume persistant, générée au premier appel."""
    directory = home(directory)
    _ensure_dir(directory)
    path = os.path.join(directory, KEY_FILE)
    if os.path.exists(path):
        return load_key(directory), False
    key = DeviceKey.generate()
    try:
        _write_private(path, key.pem(), exclusive=True)
    except FileExistsError:  # course avec un autre processus : la sienne gagne
        return load_key(directory), False
    key.path = path
    return key, True


def load_state(directory: str | None = None) -> dict | None:
    path = os.path.join(home(directory), STATE_FILE)
    if not os.path.exists(path):
        return None
    _check_private(path, "état")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_STATE:
        raise DeviceError("état de l'exécuteur %s : schéma %s attendu" % (path, SCHEMA_STATE))
    return data


def save_state(state: Mapping, directory: str | None = None) -> str:
    directory = home(directory)
    _ensure_dir(directory)
    path = os.path.join(directory, STATE_FILE)
    _write_private(path, json.dumps({"schema": SCHEMA_STATE, **dict(state)},
                                    ensure_ascii=False, indent=2) + "\n", exclusive=False)
    return path


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def code_sha256(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("ascii", "replace")).hexdigest()


def attestation_challenge(key: DeviceKey, *, server_url: str, code: str) -> bytes:
    """Le défi à faire signer par la clé d'appareil Nexlink (facultatif)."""
    return jose.attestation_transcript(server_url=server_url, code_sha256=code_sha256(code),
                                       executor_thumbprint=key.thumbprint())


def enroll_request(key: DeviceKey, *, code: str, server_url: str, label: str = "",
                   device_attestation: Optional[Mapping] = None) -> dict:
    """Corps de `POST /enroll` (`ameesh-exec-enroll/1`)."""
    code = normalize_code(code)
    jwk = key.public_jwk()
    return {"schema": C.SCHEMA_ENROLL, "code": code, "public_key": jwk,
            "proof": key.sign_b64(jose.enroll_proof_message(code, jwk, server_url)),
            "device_attestation": dict(device_attestation) if device_attestation else None,
            "label": label or ""}


def assertion(key: DeviceKey, *, executor_id: str, server_url: str,
              now: float | None = None, ttl: int = ASSERTION_TTL_S) -> str:
    """Assertion ES256 de l'exécuteur pour `POST /token` (JWS compact)."""
    iat = int(time.time() if now is None else now)
    header = {"alg": "ES256", "typ": "JWT", "kid": key.thumbprint()}
    payload = {"iss": executor_id, "aud": jose.normalize_server_url(server_url),
               "iat": iat, "exp": iat + min(int(ttl), ASSERTION_MAX_TTL_S),
               "jti": secrets.token_urlsafe(18)}
    signing_input = jose.jws_signing_input(header, payload)
    return "%s.%s" % (signing_input.decode("ascii"), key.sign_b64(signing_input))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

#: (méthode, URL, en-têtes, corps) -> (statut, corps JSON|None) ; injectable
HttpPost = Callable[[str, Mapping[str, str], bytes, float], tuple[int, Any]]


def urllib_post(url: str, headers: Mapping[str, str], body: bytes,
                timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise Unavailable("serveur du mesh injoignable : %s" % exc) from exc
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return status, None


def _post_json(post: HttpPost, server_url: str, route: str, payload: Mapping,
               timeout: float) -> Any:
    url = jose.normalize_server_url(server_url) + API_PREFIX + route
    status, body = post(url, {}, json.dumps(dict(payload)).encode("utf-8"), timeout)
    if 200 <= status < 300 and isinstance(body, dict):
        return body
    raise C.client_exception(status, body)


def enroll(server_url: str, code: str, *, directory: str | None = None, label: str = "",
           device_attestation: Optional[Mapping] = None, post: HttpPost = urllib_post,
           timeout: float = 30.0) -> dict:
    """Enrôle cet appareil : clé (créée au besoin), `POST /enroll`, état écrit.

    Refuse de ré-enrôler un appareil déjà enrôlé (état présent) : effacer
    l'état est un geste explicite."""
    if load_state(directory) is not None:
        raise DeviceError("appareil déjà enrôlé (%s) : supprimez cet état pour ré-enrôler"
                          % os.path.join(home(directory), STATE_FILE))
    key, _created = load_or_create_key(directory)
    body = enroll_request(key, code=code, server_url=server_url, label=label,
                          device_attestation=device_attestation)
    reply = _post_json(post, server_url, "/enroll", body, timeout)
    for field in ("executor_id", "mesh", "host"):
        if not isinstance(reply.get(field), str) or not reply.get(field):
            raise DeviceError("réponse d'enrôlement sans %s" % field)
    state = {"executor_id": reply["executor_id"], "mesh": reply["mesh"], "host": reply["host"],
             "server_url": jose.normalize_server_url(server_url),
             "thumbprint": key.thumbprint(), "enrolled_ts": time.time(),
             "server_time": reply.get("server_time"),
             "device_attestation": bool(device_attestation)}
    save_state(state, directory)
    return state


class HttpTokenSource(TokenSource):
    """`TokenSource` de l'appareil : assertion ES256 → jeton d'accès de 10 min.

    Le jeton est gardé en mémoire tant qu'il vit encore plus de 60 s ;
    `refresh=True` (après un 401) en force un nouveau. Jamais écrit sur
    disque."""

    def __init__(self, *, server_url: str, executor_id: str, key: DeviceKey,
                 post: HttpPost = urllib_post, timeout: float = 30.0,
                 clock: Callable[[], float] = time.time):
        self.server_url = jose.normalize_server_url(server_url)
        self.executor_id = executor_id
        self.key = key
        self._post = post
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires = 0.0

    @classmethod
    def from_device(cls, directory: str | None = None, **kwargs) -> "HttpTokenSource":
        state = load_state(directory)
        if state is None:
            raise DeviceError("appareil non enrôlé : `ameesh device enroll` d'abord")
        return cls(server_url=state["server_url"], executor_id=state["executor_id"],
                   key=load_key(directory), **kwargs)

    def access_token(self, *, refresh: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if not refresh and self._token and self._expires - now > TOKEN_MARGIN_S:
                return self._token
            jws = assertion(self.key, executor_id=self.executor_id,
                            server_url=self.server_url, now=now)
            body = _post_json(self._post, self.server_url, "/token", {"assertion": jws},
                              self._timeout)
            token = body.get("access_token")
            if body.get("schema") != C.SCHEMA_TOKEN or not isinstance(token, str):
                raise Unavailable("réponse de jeton illisible")
            self._token, self._expires = token, float(body.get("expires_ts") or 0.0)
            return token


# --------------------------------------------------------------------------
# branchement dans le client de L109
# --------------------------------------------------------------------------

def token_source_for(cfg: Any) -> TokenSource:
    """Fabrique de `storage.remote` (`set_token_source_factory`) : le
    fichier de jeton s'il est configuré (banc d'essai, `exec_token_file`),
    sinon l'identité de l'appareil enrôlé (`HttpTokenSource`, clé du volume
    `AMEESH_EXEC_HOME`). L'URL de l'état d'enrôlement doit être celle de la
    configuration : un volume enrôlé ailleurs est refusé."""
    path = getattr(cfg, "exec_token_file", "") or ""
    if path:
        from ..storage.remote.http import FileTokenSource
        return FileTokenSource(path)
    try:
        source = HttpTokenSource.from_device()
    except (DeviceError, OSError, ValueError) as exc:
        raise Unavailable("exécuteur médié : aucune source de jeton (%s)" % exc) from None
    wanted = getattr(cfg, "exec_url", "") or os.environ.get("AMEESH_EXEC_URL", "")
    if wanted and jose.normalize_server_url(wanted) != source.server_url:
        raise Unavailable("appareil enrôlé auprès de %s, pas de %s"
                          % (source.server_url, jose.normalize_server_url(wanted)))
    return source


def install_token_source() -> None:
    """Branche `token_source_for` dans le client de L109, sauf si une autre
    fabrique a déjà été posée (essais)."""
    from ..storage import remote
    if remote.token_source_factory_is_default():
        remote.set_token_source_factory(token_source_for)
