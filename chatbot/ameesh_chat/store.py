# SPDX-License-Identifier: AGPL-3.0-only
"""Persistance du plafond : un petit objet JSON, écrit sous condition d'ETag.

Choix (L33) : un objet JSON de quelques centaines d'octets dans un bucket
Object Storage **privé** du même projet Scaleway.

* coût négligeable, aucun service toujours allumé à payer (pas de Redis ni de
  base managée), aucune dépendance (signature SigV4 écrite ici) ;
* une clé d'API IAM dédiée, limitée à Object Storage du projet, en variable
  secrète de la fonction.

Interface : `load() → (état, etag)` (état `{}` et etag `None` si l'objet est
absent) ; `save(état, etag)` écrit **sous condition** — `If-Match: <etag>`, ou
`If-None-Match: *` quand l'objet n'existait pas — et lève `StoreConflict` sur
412/409 : l'appelant relit et rejoue (voir `guard.SpendGuard._transact`, qui
relit aussi après chaque écriture pour la confirmer). Object Storage Scaleway
documente ces écritures conditionnelles ; l'opérateur le vérifie au premier
déploiement (README). Sans elles, rien ne garantit l'atomicité entre instances
qui se chevauchent : le verrou n'est partagé qu'à l'intérieur d'une instance.

L'objet ne contient que des compteurs et des réservations (montant, date,
identifiant d'instance aléatoire) : jamais une question, une réponse ou une
adresse IP.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import threading
import urllib.parse

from . import transport


class StoreError(RuntimeError):
    """Lecture ou écriture du compteur impossible ; `code` est sûr à journaliser."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StoreConflict(StoreError):
    """L'objet a changé depuis la lecture (ETag) : relire et rejouer."""

    def __init__(self):
        super().__init__("store_conflict")


def _etag(body: bytes) -> str:
    return '"' + hashlib.md5(body).hexdigest() + '"'  # noqa: S324 - comme un ETag S3


class MemoryStore:
    """Pour les tests et l'essai local seulement (`STATE_BACKEND=memory`).

    Même contrat que `S3Store` : copie JSON, ETag, écriture conditionnelle."""

    def __init__(self, initial: dict | None = None):
        self.body = json.dumps(initial, sort_keys=True).encode() if initial else None
        self.writes = 0
        self.lock = threading.Lock()

    @property
    def data(self) -> dict:
        return json.loads(self.body) if self.body else {}

    def load(self) -> tuple[dict, str | None]:
        with self.lock:
            if self.body is None:
                return {}, None
            return json.loads(self.body), _etag(self.body)

    def save(self, state: dict, etag: str | None) -> None:
        body = json.dumps(state, sort_keys=True).encode()
        with self.lock:
            current = _etag(self.body) if self.body is not None else None
            if current != etag:
                raise StoreConflict()
            self.body = body
            self.writes += 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(*, method: str, host: str, path: str, query: str = "", headers: dict | None = None,
                  payload: bytes = b"", access_key: str, secret_key: str, region: str,
                  service: str = "s3", now: _dt.datetime | None = None,
                  payload_hash: str | None = None) -> dict:
    """En-têtes signés AWS SigV4 (Object Storage Scaleway parle S3).

    `path` est déjà encodé (segments `quote`), `query` déjà canonique."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = payload_hash or _sha256(payload)
    all_headers = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
    all_headers.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
    names = sorted(all_headers)
    canonical_headers = "".join(f"{n}:{all_headers[n]}\n" for n in names)
    signed = ";".join(names)
    canonical = "\n".join([method, path, query, canonical_headers, signed, payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical.encode("utf-8"))])
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {k: v for k, v in all_headers.items() if k != "host"}
    out["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


class S3Store:
    """Objet JSON dans un bucket S3 (style chemin : `https://<endpoint>/<bucket>/<clé>`)."""

    def __init__(self, *, endpoint: str, bucket: str, key: str, region: str,
                 access_key: str, secret_key: str, timeout: float = 5.0, open_with=None):
        transport.check_endpoint(endpoint)
        self.endpoint = endpoint.rstrip("/")
        self.host = urllib.parse.urlsplit(self.endpoint).netloc
        self.path = "/" + "/".join(urllib.parse.quote(s, safe="-_.~") for s in [bucket] + key.split("/"))
        self.region, self.access_key, self.secret_key = region, access_key, secret_key
        self.timeout, self.open_with = timeout, open_with

    def _call(self, method: str, body: bytes = b"", extra: dict | None = None) -> tuple[int, bytes, dict]:
        extra = dict(extra or {})
        if method == "PUT":
            extra["content-type"] = "application/json"
        headers = sigv4_headers(method=method, host=self.host, path=self.path, headers=extra,
                                payload=body, access_key=self.access_key,
                                secret_key=self.secret_key, region=self.region)
        try:
            return transport.request_full(method, self.endpoint + self.path, headers=headers,
                                          body=body if method == "PUT" else None,
                                          timeout=self.timeout, open_with=self.open_with)
        except transport.TransportError as error:
            raise StoreError("store_" + error.code) from None

    def load(self) -> tuple[dict, str | None]:
        status, data, headers = self._call("GET")
        if status == 404:
            return {}, None
        if status != 200:
            raise StoreError(f"store_get_{status}")
        etag = headers.get("etag")
        if not etag:
            raise StoreError("store_no_etag")
        try:
            state = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise StoreError("store_bad_json") from None
        if not isinstance(state, dict):
            raise StoreError("store_bad_json")
        return state, etag

    def save(self, state: dict, etag: str | None) -> None:
        body = json.dumps(state, sort_keys=True).encode("utf-8")
        condition = {"if-match": etag} if etag else {"if-none-match": "*"}
        status, _, _ = self._call("PUT", body, condition)
        if status in (409, 412):
            raise StoreConflict()
        if status not in (200, 201, 204):
            raise StoreError(f"store_put_{status}")
