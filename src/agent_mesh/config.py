"""Configuration d'agent-mesh.

Une seule source de vérité, lue dans cet ordre :
1. `~/.config/agent-mesh/config.json` (facultatif) ;
2. les variables d'environnement `AGENT_MESH_*` ;
3. les valeurs par défaut.

Les chemins v0 (`~/.local/state/agent-mail`, `~/.config/agent-mail`) restent les
chemins du repli fichier et des alias : la CLI v1 reste compatible avec les
outils v0 qui tournent en production sur ce PC.
"""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, replace

DEFAULT_DSN = "postgresql://agent_mesh:agent_mesh@127.0.0.1:55432/agent_mesh"
DEFAULT_CONFIG = "~/.config/agent-mesh/config.json"
DEFAULT_STATE = "~/.local/state/agent-mesh"
DEFAULT_V0_STATE = "~/.local/state/agent-mail"
DEFAULT_V0_CONFIG = "~/.config/agent-mail"

SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HARNESSES = ("claude", "codex", "deepseek")

#: canaux LISTEN/NOTIFY
CHANNEL_MAIL = "agent_mail"
CHANNEL_LEASE = "agent_lease"


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def hostname() -> str:
    """Nom court de la machine : l'hôte auquel un agent est épinglé."""
    return socket.gethostname().split(".")[0]


@dataclass(frozen=True)
class Config:
    dsn: str = DEFAULT_DSN
    schema: str = "public"
    driver: str = "auto"  # auto | psycopg | psql
    backend: str = "auto"  # auto | pg | file
    state_dir: str = _expand(DEFAULT_STATE)
    v0_state: str = _expand(DEFAULT_V0_STATE)
    config_dir: str = _expand(DEFAULT_V0_CONFIG)
    host: str = hostname()
    runner_id: str = ""
    connect_timeout: float = 3.0
    lease_ttl: float = 300.0
    idle_nudge: float = 1200.0
    poll: float = 5.0

    @property
    def runner(self) -> str:
        return self.runner_id or "%s:%d" % (self.host, os.getpid())

    @property
    def aliases_path(self) -> str:
        return os.path.join(self.config_dir, "aliases.tsv")

    def agent_dir(self, name: str) -> str:
        """État local d'un agent (événements, session, journaux) sur cette machine."""
        return os.path.join(self.state_dir, name)


def load(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    cfg_path = _expand(env.get("AGENT_MESH_CONFIG", DEFAULT_CONFIG))
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:  # config cassée : on le dit, on continue
            raise SystemExit("config illisible %s : %s" % (cfg_path, exc))
        known = {f for f in Config.__dataclass_fields__}  # type: ignore[attr-defined]
        cfg = replace(cfg, **{k: v for k, v in raw.items() if k in known})

    def pick(*names, default=None):
        for name in names:
            if env.get(name):
                return env[name]
        return default

    cfg = replace(
        cfg,
        dsn=pick("AGENT_MESH_DSN", "AGENT_MESH_DATABASE_URL", default=cfg.dsn),
        schema=pick("AGENT_MESH_SCHEMA", default=cfg.schema),
        driver=pick("AGENT_MESH_DRIVER", default=cfg.driver),
        backend=pick("AGENT_MESH_BACKEND", default=cfg.backend),
        state_dir=_expand(pick("AGENT_MESH_STATE", default=cfg.state_dir)),
        v0_state=_expand(pick("AGENT_MAIL_STATE", default=cfg.v0_state)),
        config_dir=_expand(pick("AGENT_MAIL_CONFIG", default=cfg.config_dir)),
        host=pick("AGENT_MESH_HOST", default=cfg.host),
        runner_id=pick("AGENT_MESH_RUNNER_ID", default=cfg.runner_id),
        connect_timeout=_as_float(env.get("AGENT_MESH_CONNECT_TIMEOUT"), cfg.connect_timeout),
        lease_ttl=_as_float(env.get("AGENT_MESH_LEASE_TTL"), cfg.lease_ttl),
        idle_nudge=_as_float(env.get("AGENT_MESH_IDLE_NUDGE"), cfg.idle_nudge),
        poll=_as_float(env.get("AGENT_MESH_POLL"), cfg.poll),
    )
    if not SCHEMA_RE.match(cfg.schema):
        raise SystemExit("AGENT_MESH_SCHEMA invalide : %r" % cfg.schema)
    if cfg.driver not in ("auto", "psycopg", "psql"):
        raise SystemExit("AGENT_MESH_DRIVER invalide : %r" % cfg.driver)
    if cfg.backend not in ("auto", "pg", "file"):
        raise SystemExit("AGENT_MESH_BACKEND invalide : %r" % cfg.backend)
    return cfg


def mask_dsn(dsn: str) -> str:
    """DSN affichable : le mot de passe n'apparaît jamais dans un journal."""
    if "://" in dsn:
        head, rest = dsn.split("://", 1)
        if "@" in rest:
            creds, tail = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return "%s://%s:***@%s" % (head, user, tail)
        return dsn
    out = []
    for token in dsn.split():
        if token.startswith("password="):
            out.append("password=***")
        else:
            out.append(token)
    return " ".join(out)
