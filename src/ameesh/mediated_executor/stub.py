# SPDX-License-Identifier: AGPL-3.0-only
"""Bouchon d'identité du serveur de l'exécuteur médié, en attendant L110.

`StaticAuth` implémente `interfaces.ExecutorAuth.verify` sur une table de
jetons fixée à l'avance : seule l'empreinte SHA-256 de chaque jeton est
gardée, comme le fera L110. Il sert :

* aux essais (faux fournisseur, `add`) ;
* à `ameesh serve --exec-only --auth-file FICHIER` tant que L110 n'est pas
  fusionné. Le fichier (`ameesh-exec-static-auth/1`, mode 0600) :

      {"schema": "ameesh-exec-static-auth/1",
       "tokens": [{"sha256": "<hex du jeton>", "kind": "executor",
                   "executor_id": "7f3a", "mesh": "mesh-exemple",
                   "host": "anna-portable", "agents_allowlist": null,
                   "expires_ts": 1791640600}],
       "revoked": ["0000"]}

Il ne sait ni enrôler ni émettre : `enroll`, `token` et `session-token`
répondent 404 tant qu'aucun `IdentityProvider` (L110) n'est branché.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import threading
import time
from typing import Callable, Iterable, Optional

from .interfaces import AuthError, ExecutorAuth, Principal

SCHEMA_STATIC_AUTH = "ameesh-exec-static-auth/1"


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StaticAuth(ExecutorAuth):
    """Jetons fixes → principal. `clock` : horloge (essais)."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._tokens: dict[str, Principal] = {}
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    def add(self, token: str, principal: Principal) -> None:
        with self._lock:
            self._tokens[token_sha256(token)] = principal

    def revoke(self, executor_id: str) -> None:
        with self._lock:
            self._revoked.add(executor_id)

    def verify(self, token: str) -> Principal:
        if not isinstance(token, str) or not token.startswith(("amx1.", "ams1.")):
            raise AuthError("token_invalid", "jeton mal formé")
        with self._lock:
            principal = self._tokens.get(token_sha256(token))
            revoked = principal is not None and principal.executor_id in self._revoked
        if principal is None:
            raise AuthError("token_invalid", "jeton inconnu")
        if revoked:
            raise AuthError("executor_revoked", "exécuteur révoqué")
        if principal.expires_ts and principal.expires_ts <= self.clock():
            raise AuthError("token_expired", "jeton échu")
        return principal

    @classmethod
    def from_file(cls, path: str) -> "StaticAuth":
        """Lit le fichier de jetons ; refuse un fichier lisible par d'autres."""
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("%s : droits trop ouverts (0600 attendu)" % path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("schema") != SCHEMA_STATIC_AUTH:
            raise ValueError("%s : schéma %s attendu" % (path, SCHEMA_STATIC_AUTH))
        auth = cls()
        fields = {f.name for f in dataclasses.fields(Principal)}
        for entry in data.get("tokens") or ():
            digest = str(entry["sha256"]).lower()
            values = {k: v for k, v in entry.items() if k in fields}
            if values.get("agents_allowlist") is not None:
                values["agents_allowlist"] = tuple(values["agents_allowlist"])
            auth._tokens[digest] = Principal(**values)
        auth._revoked.update(_strings(data.get("revoked")))
        return auth


def _strings(values: Optional[Iterable]) -> set:
    return {str(v) for v in values or ()}
