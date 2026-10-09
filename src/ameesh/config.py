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

Plusieurs canons (L42, décision 0031) : `canons` dans le fichier de
configuration — liste de `{"path", "ref", "untrusted"}` (ou de chemins) — ou
`AMEESH_CANONS` (chemins séparés par `:`). Le PREMIER est le canon par
défaut : `canon`, `canon_ref` et `canon_untrusted` restent les siens
(compatibilité), les suivants sont dans `extra_canons`. `canon` seul reste
accepté (liste à un élément). Une liste venue de l'environnement
(`AMEESH_CANONS` ou `AMEESH_CANON`) remplace celle du fichier ;
`AMEESH_CANON_REF` ne vise que le canon par défaut, `AMEESH_CANON_UNTRUSTED`
tous les canons déclarés par l'environnement.

Les chemins v0 (`~/.local/state/agent-mail`, `~/.config/agent-mail`) restent les
chemins du repli fichier et des alias : la CLI v1 reste compatible avec les
outils v0 qui tournent en production sur ce PC.
"""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field, replace

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
#: plus de liste fermée de harnais (L16, R22) : les identifiants connus sont
#: ceux des descripteurs (`ameesh.harnesses`, paquet + dossier de l'hôte).
#: politiques de session d'un agent (décision 0025, L26)
SESSION_POLICIES = ("par-lot", "taille", "jamais")

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


def _as_names(value) -> tuple[str, ...]:
    """Liste de noms séparés par des virgules -> tuple propre (ordre gardé)."""
    return tuple(n.strip() for n in str(value or "").split(",") if n.strip())


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
class CanonEntry:
    """Un canon configuré (L42, décision 0031) : racine, révision, non approuvé."""

    path: str
    ref: str = ""
    untrusted: bool = False


def _canon_entry(raw, where: str) -> CanonEntry:
    """Une entrée de `canons` (fichier de configuration) : chemin ou objet."""
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("path"), str) \
            or not raw["path"].strip():
        raise SystemExit("%s : entrée de `canons` invalide %r (attendu un chemin ou "
                         "{\"path\", \"ref\", \"untrusted\"})" % (where, raw))
    unknown = sorted(set(raw) - {"path", "ref", "untrusted"})
    if unknown:
        raise SystemExit("%s : clé(s) inconnue(s) dans `canons` : %s"
                         % (where, ", ".join(unknown)))
    ref = raw.get("ref") or ""
    if not isinstance(ref, str):
        raise SystemExit("%s : `ref` de canon invalide %r" % (where, ref))
    return CanonEntry(path=_expand(raw["path"].strip()), ref=ref.strip(),
                      untrusted=bool(_as_bool(raw.get("untrusted"), False,
                                              "%s : untrusted" % where)))


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
    #: regroupement des événements (C9) : au plus un réveil par ce délai,
    #: sauf événement `urgent`. 0 = aucun regroupement.
    event_coalesce: float = 120.0
    #: plafond (octets UTF-8) de la consigne entière d'un tour de courrier ;
    #: au-delà, les messages les plus récents attendent le tour suivant
    prompt_mail_max: float = 20000.0
    #: périodicité de `canon sync` par l'exécuteur (spec §4.4) ; 0 = seulement
    #: au démarrage. Sans canon configuré, rien n'est lancé.
    canon_sync_interval: float = 300.0
    #: garde de budget (0019, R20) : plafond horaire de l'usage payé au token
    #: (somme des tours des 60 dernières minutes) ; 0 = garde désactivée.
    budget_usd_per_hour: float = 10.0
    #: intervalle minimal entre deux vérifications de budget d'un agent en pause
    budget_check_interval: float = 30.0
    #: expéditeurs autorisés à interrompre un tour (0018, R19) ; sans canon,
    #: c'est la seule autorisation. Vide = personne.
    interrupt_senders: tuple[str, ...] = ()
    #: rotation de session (0018) : taille de contexte et latence des tours
    session_max_tokens: float = 150000.0
    session_max_turn_seconds: float = 900.0
    session_min_turns: int = 3
    #: L48 : échecs de tour — attente maximale entre deux tours en échec, durée
    #: sous laquelle un échec est « rapide », série d'échecs rapides qui arrête l'agent
    failure_backoff_max: float = 300.0
    fast_failure_s: float = 60.0
    max_fast_failures: int = 5
    #: L52b : sessions parallèles (filles) ouvertes au plus par persona, en
    #: plus de sa session principale
    max_parallel_sessions: int = 3
    #: politique de session par défaut d'un agent sans réglage (0025, L26) :
    #: `par-lot` (rotation au changement de lot, plus la rotation sur la
    #: taille), `taille` (rotation sur la taille seulement), `jamais`
    session_policy: str = "par-lot"
    #: relevé périodique du solde des fournisseurs payés au token par
    #: l'exécuteur (secondes, L26) ; 0 = jamais. Sans clé dans
    #: l'environnement, rien n'est lu.
    balance_interval: float = 900.0
    #: racines fouillées pour retrouver un dossier de travail déplacé (0018)
    worktree_roots: tuple[str, ...] = (_expand("~/development"),)
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
    #: L42 (0031) : canons configurés APRÈS le canon par défaut (`canon`),
    #: dans l'ordre ; vide = un seul canon (ou aucun)
    extra_canons: tuple[CanonEntry, ...] = ()
    #: R14 : un agent sans humain responsable n'est pas réclamable. None = vrai
    #: si un canon est configuré, faux sinon (compatibilité du banc)
    require_responsible: bool | None = None
    #: ameesh-approve (spec §9), côté client : URL de son API de service
    #: (AMEESH_APPROVE_URL ; https, ou http sur la boucle locale seulement) et
    #: fichier 0600 du jeton de service (AMEESH_APPROVE_TOKEN_FILE). Vides : pas
    #: de demande d'approbation par ameesh (`ameesh action request`).
    #: `approve_url` est l'adresse MACHINE de l'API, distincte de la
    #: `public_url` des pages humaines du service (lot L27) : boucle locale,
    #: accès privé, ou l'hôte public si le service l'ouvre explicitement
    #: (`api_via_public` dans son JSON). Elle ne règle aucune confiance : le
    #: RP ID et les origines des vérificateurs restent AMEESH_APPROVE_RP_ID et
    #: AMEESH_APPROVE_ORIGINS (contrôle : `ameesh approve-check`).
    approve_url: str = ""
    approve_token_file: str = ""
    #: TLS local du service (passthrough) appelé sur la boucle locale :
    #: `approve_url` = https://127.0.0.1:PORT, certificat vérifié pour CE nom
    #: d'hôte H (AMEESH_APPROVE_TLS_NAME) ; vide = nom de l'URL
    approve_tls_name: str = ""
    #: relevé périodique des ressources de l'hôte par l'exécuteur (secondes,
    #: L31, 0028) ; 0 = aucun relevé.
    resource_interval: float = 60.0
    #: visibilité d'une persona (L31, 0029) : durée de cache du verdict, en
    #: secondes, et délai maximal accordé à la vérification par la forge.
    visibility_ttl: float = 300.0
    visibility_timeout: float = 10.0
    #: forge de la vérification de visibilité (L31, 0029) : `gh` (API des
    #: droits), `git` (copie d'essai), `auto` (gh d'abord), ou vide.
    forge: str = ""
    #: forges (hôtes) que `gh api --hostname` sait interroger (L31, 0029) ;
    #: `github.com` par défaut, `AMEESH_FORGE_HOSTS` en ajoute (GitHub
    #: Enterprise), séparés par des virgules.
    forge_hosts: tuple[str, ...] = ("github.com",)
    #: moteur de conteneurs pour rattacher les conteneurs d'un tour (L31,
    #: 0028) : `auto` (docker puis podman), un nom, ou `none` ; et le délai
    #: maximal de la commande de lecture seule.
    container_runtime: str = "auto"
    container_timeout: float = 5.0
    #: déplacement automatique entre hôtes admis (L31, 0028) : entre deux tours,
    #: un agent d'un hôte sous pression passe à un hôte admis disponible ; faux
    #: par défaut (le geste reste humain tant qu'il n'est pas éprouvé).
    relocate: bool = False
    #: stockage des sessions partagé entre hôtes (L31, 0028) : un déplacement
    #: garde la session native au lieu de tourner avec un résumé.
    shared_sessions: bool = False
    #: comptes multiples par fournisseur (L30, décision 0027) : la clé
    #: `accounts` du fichier de configuration de l'HÔTE, telle quelle
    #: (`{harnais: [profil, …]}`), validée par `ameesh.accounts`. Ce sont des
    #: secrets d'hôte : jamais dans le canon, jamais en base.
    accounts: dict = field(default_factory=dict)
    #: alertes poussées (L38, décision 0030 point 4) : la clé `notify` du
    #: fichier de configuration de l'HÔTE, telle quelle (routes, canaux,
    #: humain par défaut, types), validée par `ameesh.notify`. Jamais de
    #: secret ici : jetons et webhooks viennent de l'environnement ou d'un
    #: fichier 0600 nommés par cette clé.
    notify: dict = field(default_factory=dict)
    #: mémoire de persona (L54, 0032 §5) : la clé `persona_memory` de l'hôte
    #: (`ssh_key` : clé limitée aux dépôts de mémoire). Jamais de secret ici.
    persona_memory: dict = field(default_factory=dict)

    @property
    def responsible_required(self) -> bool:
        if self.require_responsible is None:
            return bool(self.canon_entries)
        return bool(self.require_responsible)

    @property
    def canon_entries(self) -> tuple[CanonEntry, ...]:
        """Canons configurés, le canon par défaut d'abord (L42, 0031) ; vide
        sans canon. Le canon par défaut est toujours `canon`/`canon_ref`/
        `canon_untrusted` : un `dataclasses.replace(cfg, canon=…)` le suit."""
        if not self.canon:
            return ()
        return (CanonEntry(self.canon, self.canon_ref, self.canon_untrusted),) \
            + tuple(self.extra_canons)

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
        known.discard("extra_canons")   # `canons` (liste) ci-dessous, jamais tel quel
        if isinstance(raw.get("humans"), list):
            raw = dict(raw, humans=",".join(str(n) for n in raw["humans"]))
        cfg = replace(cfg, **{k: v for k, v in raw.items() if k in known})
        if raw.get("canons") is not None:
            cfg = replace(cfg, **_canons_from_file(raw, cfg_path))

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
        event_coalesce=_as_float(
            pick("AMEESH_EVENT_COALESCE", "AGENT_MESH_EVENT_COALESCE"), cfg.event_coalesce),
        prompt_mail_max=_as_float(pick("AMEESH_PROMPT_MAIL_MAX"), cfg.prompt_mail_max),
        canon_sync_interval=_as_float(
            pick("AMEESH_CANON_SYNC_INTERVAL", "AGENT_MESH_CANON_SYNC_INTERVAL"),
            cfg.canon_sync_interval),
        budget_usd_per_hour=_as_float(
            pick("AMEESH_BUDGET_USD_PER_HOUR", "AGENT_MESH_BUDGET_USD_PER_HOUR"),
            cfg.budget_usd_per_hour),
        budget_check_interval=_as_float(
            pick("AMEESH_BUDGET_CHECK_INTERVAL", "AGENT_MESH_BUDGET_CHECK_INTERVAL"),
            cfg.budget_check_interval),
        interrupt_senders=_as_names(
            pick("AMEESH_INTERRUPT_SENDERS", "AGENT_MESH_INTERRUPT_SENDERS")
        ) or cfg.interrupt_senders,
        session_max_tokens=_as_float(
            pick("AMEESH_SESSION_MAX_TOKENS", "AGENT_MESH_SESSION_MAX_TOKENS"),
            cfg.session_max_tokens),
        session_max_turn_seconds=_as_float(
            pick("AMEESH_SESSION_MAX_TURN_SECONDS", "AGENT_MESH_SESSION_MAX_TURN_SECONDS"),
            cfg.session_max_turn_seconds),
        session_min_turns=int(_as_float(
            pick("AMEESH_SESSION_MIN_TURNS", "AGENT_MESH_SESSION_MIN_TURNS"),
            cfg.session_min_turns)),
        failure_backoff_max=_as_float(pick("AMEESH_FAILURE_BACKOFF_MAX"),
                                      cfg.failure_backoff_max),
        fast_failure_s=_as_float(pick("AMEESH_FAST_FAILURE_S"), cfg.fast_failure_s),
        max_fast_failures=int(_as_float(pick("AMEESH_MAX_FAST_FAILURES"),
                                        cfg.max_fast_failures)),
        max_parallel_sessions=int(_as_float(pick("AMEESH_MAX_PARALLEL_SESSIONS"),
                                            cfg.max_parallel_sessions)),
        session_policy=str(pick("AMEESH_SESSION_POLICY", default=cfg.session_policy)
                           or "par-lot").strip(),
        balance_interval=_as_float(pick("AMEESH_BALANCE_INTERVAL"), cfg.balance_interval),
        worktree_roots=tuple(
            _expand(n) for n in _as_names(
                pick("AMEESH_WORKTREE_ROOTS", "AGENT_MESH_WORKTREE_ROOTS"))
        ) or cfg.worktree_roots,
        threads_dir=_expand(threads_brut) if threads_brut else "",
        project=pick("AMEESH_PROJECT", default=cfg.project) or "",
        humans=pick("AMEESH_HUMANS", default=cfg.humans) or "",
        resource_interval=_as_float(pick("AMEESH_RESOURCE_INTERVAL"),
                                    cfg.resource_interval),
        visibility_ttl=_as_float(pick("AMEESH_VISIBILITY_TTL"), cfg.visibility_ttl),
        visibility_timeout=_as_float(pick("AMEESH_VISIBILITY_TIMEOUT"),
                                     cfg.visibility_timeout),
        forge=str(pick("AMEESH_FORGE", default=cfg.forge) or "").strip().lower(),
        forge_hosts=tuple(
            h.strip().lower() for h in _as_names(
                pick("AMEESH_FORGE_HOSTS", default=None)) if h.strip()
        ) or cfg.forge_hosts,
        container_runtime=str(pick("AMEESH_CONTAINER_RUNTIME",
                                   default=cfg.container_runtime) or "").strip().lower(),
        container_timeout=_as_float(pick("AMEESH_CONTAINER_TIMEOUT"),
                                    cfg.container_timeout),
        relocate=bool(_as_bool(pick("AMEESH_RELOCATE", default=cfg.relocate), False,
                               "AMEESH_RELOCATE")),
        shared_sessions=bool(_as_bool(pick("AMEESH_SHARED_SESSIONS",
                                           default=cfg.shared_sessions), False,
                                      "AMEESH_SHARED_SESSIONS")),
    )
    # L42 (0031) : une liste venue de l'environnement remplace celle du fichier
    env_untrusted = _as_bool(pick("AMEESH_CANON_UNTRUSTED", "AGENT_MESH_CANON_UNTRUSTED",
                                  default=None), None, "AMEESH_CANON_UNTRUSTED")
    canons_env = pick("AMEESH_CANONS", default=None)
    single_env = pick("AMEESH_CANON", "AGENT_MESH_CANON", default=None)
    if canons_env:
        paths = [_expand(p.strip()) for p in str(canons_env).split(":") if p.strip()]
        if single_env and paths and _expand(str(single_env)) != paths[0]:
            raise SystemExit("AMEESH_CANON (%s) et AMEESH_CANONS (premier : %s) se "
                             "contredisent : le premier de AMEESH_CANONS est le canon par "
                             "défaut" % (single_env, paths[0]))
        cfg = replace(cfg, canon=paths[0] if paths else "", canon_ref="",
                      canon_untrusted=bool(env_untrusted),
                      extra_canons=tuple(CanonEntry(p, "", bool(env_untrusted))
                                         for p in paths[1:]))
    elif single_env:
        cfg = replace(cfg, canon=str(single_env), extra_canons=())
    canon = cfg.canon
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
    token_file = pick("AMEESH_APPROVE_TOKEN_FILE", default=cfg.approve_token_file) or ""
    cfg = replace(
        cfg,
        approve_url=str(pick("AMEESH_APPROVE_URL", default=cfg.approve_url) or "").strip(),
        approve_token_file=_expand(str(token_file)) if token_file else "",
        approve_tls_name=str(pick("AMEESH_APPROVE_TLS_NAME", default=cfg.approve_tls_name)
                             or "").strip().lower(),
    )
    if cfg.canon_ref.startswith("-"):
        raise SystemExit("AMEESH_CANON_REF invalide : %r" % cfg.canon_ref)
    seen_paths: set[str] = set()
    for entry in cfg.canon_entries:
        if entry.ref.startswith("-"):
            raise SystemExit("ref de canon invalide : %r (%s)" % (entry.ref, entry.path))
        if entry.path in seen_paths:
            raise SystemExit("canon configuré deux fois : %s" % entry.path)
        seen_paths.add(entry.path)
    if not SCHEMA_RE.match(cfg.schema):
        raise SystemExit("AGENT_MESH_SCHEMA invalide : %r" % cfg.schema)
    if cfg.session_policy not in SESSION_POLICIES:
        raise SystemExit("AMEESH_SESSION_POLICY invalide : %r (%s)"
                         % (cfg.session_policy, " | ".join(SESSION_POLICIES)))
    if cfg.driver not in ("auto", "psycopg", "psql"):
        raise SystemExit("AGENT_MESH_DRIVER invalide : %r" % cfg.driver)
    if cfg.backend not in ("auto", "pg", "file"):
        raise SystemExit("AGENT_MESH_BACKEND invalide : %r" % cfg.backend)
    return cfg


def _canons_from_file(raw: dict, where: str) -> dict:
    """`canons` du fichier de configuration (L42, 0031) → champs de Config.

    Le premier est le canon par défaut (`canon`, `canon_ref`,
    `canon_untrusted`) ; `canon` et `canons` à la fois : erreur claire."""
    items = raw.get("canons")
    if not isinstance(items, list):
        raise SystemExit("%s : `canons` doit être une liste" % where)
    if any(raw.get(k) not in (None, "") for k in ("canon", "canon_ref")) \
            or raw.get("canon_untrusted") is not None:
        raise SystemExit("%s : `canon`/`canon_ref`/`canon_untrusted` et `canons` à la fois "
                         "— le premier élément de `canons` est le canon par défaut" % where)
    entries = [_canon_entry(item, where) for item in items]
    if not entries:
        return {"canon": "", "canon_ref": "", "canon_untrusted": False, "extra_canons": ()}
    first = entries[0]
    return {"canon": first.path, "canon_ref": first.ref, "canon_untrusted": first.untrusted,
            "extra_canons": tuple(entries[1:])}


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
