# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration d'agent-mesh.

Une seule source de vérité, lue dans cet ordre :
1. `~/.config/agent-mesh/config.json` (facultatif) ;
2. les variables d'environnement `AMEESH_*` (`AGENT_MESH_*` reste accepté
   en alias, comme `agent-mail` pour la commande) ;
3. les valeurs par défaut.

Canon (spec §4) : `AMEESH_CANON` (racine du bundle OKF), `AMEESH_CANON_REF`
(révision canonique), `AMEESH_CANON_UNTRUSTED=1` (canon hors git, tests
seulement) et `AMEESH_REQUIRE_RESPONSIBLE` (R14 dans `claimable` ; par défaut
vrai dès qu'un canon est configuré, faux sinon pour la compatibilité du banc).

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

#: DSN du banc local, **sans mot de passe** : le secret vient de PGPASSWORD ou
#: de ~/.pgpass, ou d'un DSN complet dans AMEESH_DSN / le fichier de config.
DEFAULT_DSN = "postgresql://agent_mesh@127.0.0.1:55432/agent_mesh"
DEFAULT_CONFIG = "~/.config/ameesh/config.json"
LEGACY_CONFIG = "~/.config/agent-mesh/config.json"
DEFAULT_STATE = "~/.local/state/ameesh"
LEGACY_STATE = "~/.local/state/agent-mesh"
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


def _as_bool(value, default: bool | None, name: str) -> bool | None:
    """Booléen d'environnement ou de configuration ; une valeur illisible est une erreur."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "oui", "on"):
        return True
    if text in ("0", "false", "no", "non", "off"):
        return False
    raise SystemExit("%s invalide : %r (attendu 1/0, true/false)" % (name, value))


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
    #: au-delà, une requête est annulée par le serveur (0 = pas de limite).
    #: Empêche un renouvellement de bail de bloquer le battement indéfiniment.
    statement_timeout_ms: float = 30000.0
    lease_ttl: float = 300.0
    idle_nudge: float = 1200.0
    poll: float = 5.0
    #: racine des fils lisibles (transport `file`) ; vide = `<state_dir>/fils`
    threads_dir: str = ""
    #: projet par défaut d'un message dont ni l'expéditeur ni le destinataire
    #: n'ont de chantier (AMEESH_PROJECT) ; vide = « default »
    project: str = ""
    #: noms des membres humains (séparés par des virgules), pour l'auteur
    #: `human:` des entrées de fil ; les autres sont `agent:`
    humans: str = ""
    #: racine du canon OKF (spec §4.1) ; vide = pas de canon (banc historique)
    canon: str = ""
    #: révision canonique à lire (vide = `ref` du manifeste, sinon origin/main)
    canon_ref: str = ""
    #: lire les fichiers de travail d'un canon hors dépôt (tests, prototypes) :
    #: jamais par défaut, et chaque constat le signale
    canon_untrusted: bool = False
    #: R14 : un agent sans humain responsable n'est pas réclamable. None = vrai
    #: si un canon est configuré, faux sinon (compatibilité du banc)
    require_responsible: bool | None = None

    @property
    def responsible_required(self) -> bool:
        if self.require_responsible is None:
            return bool(self.canon)
        return bool(self.require_responsible)

    @property
    def runner(self) -> str:
        return self.runner_id or "%s:%d" % (self.host, os.getpid())

    @property
    def aliases_path(self) -> str:
        return os.path.join(self.config_dir, "aliases.tsv")

    def agent_dir(self, name: str) -> str:
        """État local d'un agent (événements, session, journaux) sur cette machine."""
        return os.path.join(self.state_dir, name)

    @property
    def threads_root(self) -> str:
        """Racine des fils lisibles : AMEESH_THREADS, sinon `<état>/fils`."""
        return self.threads_dir or os.path.join(self.state_dir, "fils")

    @property
    def human_names(self) -> frozenset:
        return frozenset(n.strip() for n in (self.humans or "").split(",") if n.strip())


def load(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    legacy_cfg = _expand(env.get("AGENT_MESH_CONFIG") or LEGACY_CONFIG)
    cfg_path = _expand(env.get("AMEESH_CONFIG") or env.get("AGENT_MESH_CONFIG") or DEFAULT_CONFIG)
    if not os.path.exists(cfg_path) and os.path.exists(legacy_cfg):
        cfg_path = legacy_cfg  # une config agent-mesh existante reste lue
    raw: dict = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:  # config cassée : on le dit, on continue
            raise SystemExit("config illisible %s : %s" % (cfg_path, exc))
        if not isinstance(raw, dict):
            raise SystemExit("config illisible %s : objet JSON attendu" % cfg_path)
        known = {f for f in Config.__dataclass_fields__}  # type: ignore[attr-defined]
        if isinstance(raw.get("humans"), list):
            raw = dict(raw, humans=",".join(str(n) for n in raw["humans"]))
        cfg = replace(cfg, **{k: v for k, v in raw.items() if k in known})

    def pick(*names, default=None):
        for name in names:
            if env.get(name):
                return env[name]
        return default

    # AMEESH_* est le nom courant ; AGENT_MESH_* reste accepté (alias).
    # Priorité : variable d'environnement > fichier de config > défaut. Le repli
    # sur l'ancien dossier (agent-mesh) ne vaut que si le JSON **ne dit rien** de
    # `state_dir` : une valeur explicite égale au défaut reste un choix, et
    # l'égalité de valeur ne prouve pas l'absence du champ (verdict codex3 B1).
    state_brut = pick("AMEESH_STATE", "AGENT_MESH_STATE", default=None)
    if state_brut:
        state_dir = _expand(state_brut)
    elif "state_dir" in raw:
        state_dir = _expand(str(raw["state_dir"] or DEFAULT_STATE))
    else:
        neuf, ancien = _expand(DEFAULT_STATE), _expand(LEGACY_STATE)
        state_dir = ancien if (os.path.isdir(ancien) and not os.path.isdir(neuf)) else neuf
    # Fils lisibles : AMEESH_THREADS > `threads_dir` du JSON > `<état>/fils`.
    threads_brut = pick("AMEESH_THREADS", default=cfg.threads_dir)
    cfg = replace(
        cfg,
        dsn=pick("AMEESH_DSN", "AGENT_MESH_DSN", "AMEESH_DATABASE_URL",
                 "AGENT_MESH_DATABASE_URL", default=cfg.dsn),
        schema=pick("AMEESH_SCHEMA", "AGENT_MESH_SCHEMA", default=cfg.schema),
        driver=pick("AMEESH_DRIVER", "AGENT_MESH_DRIVER", default=cfg.driver),
        backend=pick("AMEESH_BACKEND", "AGENT_MESH_BACKEND", default=cfg.backend),
        state_dir=state_dir,
        v0_state=_expand(pick("AGENT_MAIL_STATE", default=cfg.v0_state)),
        config_dir=_expand(pick("AGENT_MAIL_CONFIG", default=cfg.config_dir)),
        host=pick("AMEESH_HOST", "AGENT_MESH_HOST", default=cfg.host),
        runner_id=pick("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID", default=cfg.runner_id),
        connect_timeout=_as_float(pick("AMEESH_CONNECT_TIMEOUT", "AGENT_MESH_CONNECT_TIMEOUT"),
                                  cfg.connect_timeout),
        statement_timeout_ms=_as_float(
            pick("AMEESH_STATEMENT_TIMEOUT_MS", "AGENT_MESH_STATEMENT_TIMEOUT_MS"),
            cfg.statement_timeout_ms),
        lease_ttl=_as_float(pick("AMEESH_LEASE_TTL", "AGENT_MESH_LEASE_TTL"), cfg.lease_ttl),
        idle_nudge=_as_float(pick("AMEESH_IDLE_NUDGE", "AGENT_MESH_IDLE_NUDGE"), cfg.idle_nudge),
        poll=_as_float(pick("AMEESH_POLL", "AGENT_MESH_POLL"), cfg.poll),
        threads_dir=_expand(threads_brut) if threads_brut else "",
        project=pick("AMEESH_PROJECT", default=cfg.project) or "",
        humans=pick("AMEESH_HUMANS", default=cfg.humans) or "",
    )
    canon = pick("AMEESH_CANON", "AGENT_MESH_CANON", default=cfg.canon) or ""
    cfg = replace(
        cfg,
        canon=_expand(str(canon)) if canon else "",
        canon_ref=str(pick("AMEESH_CANON_REF", "AGENT_MESH_CANON_REF",
                           default=cfg.canon_ref) or ""),
        canon_untrusted=bool(_as_bool(
            pick("AMEESH_CANON_UNTRUSTED", "AGENT_MESH_CANON_UNTRUSTED",
                 default=cfg.canon_untrusted), False, "AMEESH_CANON_UNTRUSTED")),
        require_responsible=_as_bool(
            pick("AMEESH_REQUIRE_RESPONSIBLE", "AGENT_MESH_REQUIRE_RESPONSIBLE",
                 default=cfg.require_responsible), None, "AMEESH_REQUIRE_RESPONSIBLE"),
    )
    if cfg.canon_ref.startswith("-"):
        raise SystemExit("AMEESH_CANON_REF invalide : %r" % cfg.canon_ref)
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
