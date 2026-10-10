# SPDX-License-Identifier: AGPL-3.0-only
"""Stockage distant : l'exécuteur médié et ses sessions (lot L109).

Un exécuteur sur un appareil prêté n'a aucun accès à la base : il parle au
serveur du mesh par `/api/exec/v1` (contrat L107,
`docs/EXECUTEUR-MEDIEE.md`). `connect(cfg)` rend une `RemoteDb` que
`storage.of()` reconnaît ; le reste du code (exécuteur, courrier, lots)
appelle les mêmes opérations qu'en Postgres.

Configuration (`backend: mediated`) :

* `exec_url` (`AMEESH_EXEC_URL`) : URL du serveur du mesh ;
* exécuteur : le jeton d'accès vient d'une `TokenSource` (L110). Par défaut,
  un fichier 0600 (`exec_token_file`, `AMEESH_EXEC_TOKEN_FILE`) ; L110
  remplace la fabrique par `set_token_source_factory` ;
* session du harnais : le jeton de session `AMEESH_EXEC_TOKEN`, posé par
  l'exécuteur après `begin_turn` ; sa présence choisit le mode session.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional

from ... import db as db_mod
from ...executeur_mediee.interfaces import ENV_SERVER_URL, ENV_SESSION_TOKEN, TokenSource
from .client import DRIVER, EXECUTOR, SESSION, LeaseBook, RemoteDb, RemoteStorage, supported
from .events import RemoteSubscription
from .http import FileTokenSource, HttpTransport, StaticTokenSource

__all__ = ["DRIVER", "EXECUTOR", "SESSION", "LeaseBook", "RemoteDb", "RemoteStorage",
           "RemoteSubscription", "HttpTransport", "FileTokenSource", "StaticTokenSource",
           "connect", "is_mediated", "set_token_source_factory", "supported"]


def _default_token_source(cfg: Any) -> TokenSource:
    path = getattr(cfg, "exec_token_file", "") or ""
    if not path:
        raise db_mod.Unavailable(
            "exécuteur médié : aucune source de jeton (exec_token_file / "
            "AMEESH_EXEC_TOKEN_FILE, ou l'identité de l'exécuteur, L110)")
    return FileTokenSource(path)


_token_source_factory: Callable[[Any], TokenSource] = _default_token_source


def set_token_source_factory(factory: Optional[Callable[[Any], TokenSource]]) -> None:
    """Point d'attache de L110 : `factory(cfg) -> TokenSource` (assertion
    ES256 signée par la clé de la VM). None rétablit la source fichier."""
    global _token_source_factory
    _token_source_factory = factory or _default_token_source


def is_mediated(cfg: Any) -> bool:
    return getattr(cfg, "backend", "") == DRIVER


def server_url(cfg: Any, env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return (getattr(cfg, "exec_url", "") or env.get(ENV_SERVER_URL) or "").strip()


def connect(cfg: Any, *, mode: Optional[str] = None,
            env: Optional[Mapping[str, str]] = None) -> RemoteDb:
    """La connexion distante de cette configuration.

    `mode` : `session` si `AMEESH_EXEC_TOKEN` est posé (harnais dans la VM),
    sinon `executor`. Lève `Unavailable` sans URL ni jeton."""
    env = os.environ if env is None else env
    url = server_url(cfg, env)
    if not url:
        raise db_mod.Unavailable("exécuteur médié : URL du serveur absente "
                                 "(exec_url / AMEESH_EXEC_URL)")
    session_token = env.get(ENV_SESSION_TOKEN) or ""
    if mode is None:
        mode = SESSION if session_token else EXECUTOR
    try:
        if mode == SESSION:
            if not session_token:
                raise db_mod.Unavailable("session médiée : AMEESH_EXEC_TOKEN absent")
            transport = HttpTransport(url, session_tokens=StaticTokenSource(session_token))
        else:
            transport = HttpTransport(url, tokens=_token_source_factory(cfg))
    except ValueError as exc:
        raise db_mod.Unavailable(str(exc))
    return RemoteDb(cfg, transport, mode=mode, url=url)
