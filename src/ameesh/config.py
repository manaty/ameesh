# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration d'agent-mesh.

Une seule source de vérité, lue dans cet ordre :
1. `~/.config/agent-mesh/config.json` (facultatif) ;
2. les variables d'environnement `AMEESH_*` (`AGENT_MESH_*` reste accepté
   en alias, comme `agent-mail` pour la commande) ;
3. les valeurs par défaut.

Le fichier est choisi par `AMEESH_CONFIG` (alias `AGENT_MESH_CONFIG`). Un
chemin nommé explicitement qui n'existe pas est refusé (« config file not
found »), jamais remplacé en silence par la config historique (#1127). Sans
variable explicite, `~/.config/ameesh/config.json` est lu, à défaut
`~/.config/agent-mesh/config.json`.

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

from . import dsn as dsn_mod

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
#: L70 : changement d'un plafond de budget en base (migration 0042)
CHANNEL_BUDGET = "ameesh_budget"


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bounded_positive(value, default):
    """Finite positive mesh setting; malformed input keeps the configured value."""
    parsed = _as_float(value, default)
    return parsed if 1 <= parsed <= 86400 else default


def _as_share(value, default: float) -> float:
    """Part entre 0 et 1 ; illisible ou hors bornes : défaut."""
    part = _as_float(value, default)
    return part if 0.0 <= part <= 1.0 else default


_TIME_OF_DAY_RE = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])\Z")


def _as_time_of_day(value, default: int | None = None):
    """Secondes depuis minuit UTC pour « HH:MM », un entier de secondes, ou défaut."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if 0 <= seconds < 86400 else default
    match = _TIME_OF_DAY_RE.fullmatch(str(value or "").strip())
    if not match:
        return default
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60


def _as_window(value, default):
    """Fenêtre (début, fin) en secondes depuis minuit UTC ; défaut si illisible.

    Accepte « 03:30-04:30 », `["03:30", "04:30"]` ou `[12600, 16200]`. Une fin
    antérieure ou égale au début est refusée : une fenêtre vide rendrait toute
    sauvegarde invisible.
    """
    if isinstance(value, str) and "-" in value:
        left, right = value.split("-", 1)
        parts = (left, right)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        parts = value
    else:
        return default
    start = _as_time_of_day(parts[0])
    end = _as_time_of_day(parts[1])
    if start is None or end is None or end <= start:
        return default
    return (start, end)


def _as_days(value, default: int) -> int:
    """Nombre entier de jours ≥ 0 (L361) : « 0.5 » ou « 1.9 » ne sont pas
    tronqués (0 couperait l'expiration) ; illisible ou non entier : défaut."""
    if value is None:
        return default
    text = str(value).strip()
    return int(text) if re.fullmatch(r"[0-9]{1,6}", text) else default


def _as_count(value, default: int) -> int:
    """Entier ≥ 0 (L105) ; une valeur illisible, négative ou infinie : défaut."""
    nombre = _as_float(value, float(default))
    if nombre != nombre or nombre in (float("inf"), float("-inf")) or nombre < 0:
        return default
    return int(nombre)


def _as_seconds(value, default: float) -> float:
    """Durée ≥ 0 en secondes (L125) ; illisible, négative ou infinie : défaut."""
    nombre = _as_float(value, float(default))
    if nombre != nombre or nombre in (float("inf"), float("-inf")) or nombre < 0:
        return float(default)
    return nombre


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
    raise SystemExit("%s: invalid value %r (expected 1/0, true/false)" % (name, value))


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
        raise SystemExit("%s: invalid `canons` entry %r (expected a path or "
                         "{\"path\", \"ref\", \"untrusted\"})" % (where, raw))
    unknown = sorted(set(raw) - {"path", "ref", "untrusted"})
    if unknown:
        raise SystemExit("%s: unknown key(s) in `canons`: %s"
                         % (where, ", ".join(unknown)))
    ref = raw.get("ref") or ""
    if not isinstance(ref, str):
        raise SystemExit("%s: invalid canon `ref` %r" % (where, ref))
    return CanonEntry(path=_expand(raw["path"].strip()), ref=ref.strip(),
                      untrusted=bool(_as_bool(raw.get("untrusted"), False,
                                              "%s: untrusted" % where)))


@dataclass(frozen=True)
class Config:
    #: #1466 : le fichier réellement lu et la base dont il hérite (un « profil »
    #: rangé sous le dossier de la configuration de l'hôte). Renseignés par
    #: `load`, jamais lus du fichier : ils décrivent la lecture, pas un réglage.
    config_path: str = ""
    config_base: str = ""
    #: #1466 : clés de premier niveau que le profil déclare et qui diffèrent de
    #: la configuration de l'hôte : c'est l'écart qu'un profil périmé creuse
    #: (des comptes ou un plafond d'hier). Vide pour un héritage fidèle.
    config_shadowed: tuple[str, ...] = ()
    dsn: str = DEFAULT_DSN
    schema: str = "public"
    driver: str = "auto"  # auto | psycopg | psql
    backend: str = "auto"  # auto | pg | file | mediated
    state_dir: str = _expand(DEFAULT_STATE)
    v0_state: str = _expand(DEFAULT_V0_STATE)
    config_dir: str = _expand(DEFAULT_V0_CONFIG)
    host: str = hostname()
    runner_id: str = ""
    #: délai de connexion à la base (libpq `connect_timeout`, secondes). 10 s
    #: et non 3 : une base distante (VPN, plusieurs centaines de ms d'aller-
    #: retour) dépassait 3 s au moindre à-coup du lien (L72).
    connect_timeout: float = 10.0
    #: L72 : attente maximale entre deux essais quand la base est injoignable
    #: (attente doublée à chaque échec, de 2 s jusqu'à cette borne)
    db_retry_max: float = 60.0
    #: au-delà, une requête est annulée par le serveur (0 = pas de limite).
    #: Empêche un renouvellement de bail de bloquer le battement indéfiniment.
    statement_timeout_ms: float = 30000.0
    #: #4609 : fenêtre UTC de la sauvegarde quotidienne de la base du mesh,
    #: en secondes depuis minuit (`(12600, 16200)` = 03:30–04:30), part
    #: minimale des tables lisibles qui doivent avoir été lues au moins à
    #: l'instant du `pg_dump`, et part minimale du groupe de tables qui
    #: partagent cet instant (une petite transaction ordinaire ne prouve pas
    #: une sauvegarde). La fenêtre par défaut suit la minuterie
    #: `ameesh-sauvegarde` (03:30 + jusqu'à 15 min d'aléa) ; une part nulle
    #: désactive l'alerte.
    mesh_backup_window: tuple[int, int] = (12600, 16200)
    mesh_backup_min_share: float = 0.9
    mesh_backup_min_group_share: float = 0.05
    #: L304 : session en lecture seule (`default_transaction_read_only`) ;
    #: jamais lu de l'environnement, posé par `ameesh progress --serve`
    read_only: bool = False
    lease_ttl: float = 300.0
    idle_nudge: float = 1200.0
    poll: float = 5.0
    runner_liveness_interval: float = 60.0  # #508 : écriture groupée, au moins 60 s
    #: regroupement des événements (C9) : au plus un réveil par ce délai,
    #: sauf événement `urgent`. 0 = aucun regroupement.
    event_coalesce: float = 120.0
    #: L125 : fenêtre de regroupement du courrier ordinaire (secondes) — un
    #: agent au repos n'est réveillé qu'au bout de ce délai après l'arrivée
    #: du premier message, et son tour emporte tout ce qui est arrivé entre-
    #: temps ; un message urgent ou d'un humain réveille tout de suite.
    #: Réglable par agent (`ameesh set <agent> mail_batch=…`). 0 = réveil immédiat.
    mail_batch: float = 90.0
    #: #408 (L165b, règle 1) : l'exécuteur réveille un agent au repos depuis
    #: `mail_batch` + `lot_wake_margin` qui détient un lot actionnable
    #: (`intake`/`build`), avec la consigne de le reprendre ; au plus
    #: `lot_wake_max` réveils sans effet par lot (backoff doublé, en base),
    #: puis l'auditeur prend le relais. Désactivable par hôte
    #: (`AMEESH_LOT_WAKE=0`) ou par agent (`ameesh set <agent> lot_wake=off`).
    lot_wake: bool = True
    lot_wake_margin: float = 60.0
    lot_wake_max: int = 3
    #: plafond (octets UTF-8) de la consigne entière d'un tour de courrier ;
    #: au-delà, les messages les plus récents attendent le tour suivant
    prompt_mail_max: float = 20000.0
    #: périodicité de `canon sync` par l'exécuteur (spec §4.4) ; 0 = seulement
    #: au démarrage. Sans canon configuré, rien n'est lancé.
    canon_sync_interval: float = 300.0
    #: garde de budget (0019, R20) : plafond horaire de l'usage payé au token
    #: (somme des tours des 60 dernières minutes) ; 0 = garde désactivée.
    budget_usd_per_hour: float = 10.0
    #: L70 : plafond par jour glissant (24 h) de l'usage payé au token ;
    #: 0 = aucun. Défaut local : `ameesh budget set` (en base) l'emporte.
    budget_usd_per_day: float = 0.0
    #: L70 : réglages de budget donnés EXPLICITEMENT (fichier ou environnement),
    #: pour dire la source d'un plafond (`config` ou `défaut`) ; calculé par `load`
    budget_explicit: tuple[str, ...] = ()
    #: intervalle minimal entre deux vérifications de budget d'un agent en pause
    budget_check_interval: float = 30.0
    #: expéditeurs autorisés à interrompre un tour (0018, R19) ; sans canon,
    #: c'est la seule autorisation. Vide = personne.
    interrupt_senders: tuple[str, ...] = ()
    #: L242b : agents critiques déclarés (en plus des rôles orchestrateur,
    #: conception et auditeur du canon) : jamais en pause budget tant qu'un
    #: compte a de la marge (voir docs/COMPTES.md § 7)
    critical_agents: tuple[str, ...] = ()
    #: #4150: read-only host idle window and protected baseline size.
    idle_after_s: float = 1800.0
    baseline_hosts: int = 2
    #: #4148 (B of #3167): host lifecycle adapter. `lifecycle_hook` applies to
    #: every host of the mesh; `lifecycle_hooks` is the per-host map and wins.
    #: ameesh runs `<command> busy|draining|idle-drained <host>` and owns no
    #: cluster or cloud credential. Empty = no adapter, hence no automatic
    #: release. The command also answers the read-only `runs <host>` verb.
    lifecycle_hook: str = ""
    lifecycle_hooks: dict = field(default_factory=dict)
    #: a hook that has not answered within this delay is a failure (seconds)
    lifecycle_hook_timeout: float = 300.0
    #: how often the expensive idle projection runs for a host (seconds); the
    #: cheap transitions and the automatic return are checked every pass.
    host_release_interval: float = 300.0
    #: a withdrawal that has not reached `idle-drained` after this delay is
    #: reported (the runner's own drain bound is 30 min, plus margin).
    host_release_drain_bound: float = 2100.0
    #: a failed transition is retried after this delay (seconds)
    host_release_retry: float = 300.0
    #: quiet time after the last turn before `idle-drained` is proven
    host_release_settle: float = 60.0
    #: #4148 B1: a released host whose `busy` hook answered is booting; its
    #: readings are stale on purpose. Loss detection leaves it alone this long
    #: (or until the first post-ack reading reopens claims), then gives up.
    host_release_boot_grace: float = 900.0
    #: L242b : part du plafond horaire payé au token du mesh au-delà de
    #: laquelle un agent NON critique s'arrête (décision de mesh-design du
    #: 2026-10-12 : 0.75, soit 15 $ sur 20) ; un critique va jusqu'au plafond
    #: plein. Entre 0 et 1 ; une valeur hors bornes : le défaut.
    noncritical_hourly_share: float = 0.75
    #: L242b (3) : les harnais vers lesquels un agent critique au seuil sur
    #: tous les comptes du sien tourne, dans l'ordre (un harnais au forfait,
    #: puis un harnais à clé d'API), par rotation avec résumé. Vide : aucun.
    critical_fallback_harnesses: tuple[str, ...] = ("codex", "deepseek")
    #: rotation de session (L11, #4448) : garde de **taille de session**, la
    #: somme `input_tokens + output_tokens` des tours (`S_hist`), pas une
    #: fenêtre de contexte ni un budget de relecture. La convention de cache
    #: est hétérogène selon le harnais (Codex compte le cache dans son entrée,
    #: Claude/DeepSeek le publient à part ; le cache écrit n'est pas inclus).
    #: Seuil commun de repli : le canon (`session.max_tokens*`) donne le seuil
    #: du harnais du dernier tour quand il est présent. `0` n'est pas une
    #: extinction (le canon refuse 0 ; la comparaison `>=` ferait tourner au
    #: minimum de tours), contrairement à `context_max_tokens`.
    session_max_tokens: float = 150000.0
    #: #706 : au-dessus du plafond de tour par défaut (1800 s).
    session_max_turn_seconds: float = 3600.0
    session_min_turns: int = 3
    #: plafond de contexte (L60) : au-delà de ce contexte — les jetons
    #: d'entrée du dernier appel au modèle, cache lu et écrit compris —, la
    #: session est tournée avant le tour suivant ; réglable par agent
    #: (`ameesh set context_max_tokens=…`). La somme des relectures d'un tour
    #: n'est pas la mesure (#2336). 0 = plafond désactivé.
    context_max_tokens: float = 15_000_000.0
    #: CTX-04a (#4970) : budgets de relecture opt-in, en jetons, sur `R_turn`
    #: (relu du tour) et `R_session` (relu cumulé de la session). Dépassé : le
    #: tour se clôt au prochain point sûr et le travail reprend dans la MÊME
    #: session, sans rotation ni compactage. Ni taille de fenêtre, ni
    #: pourcentage de `W`. 0 = éteint (défaut), mode legacy inchangé.
    turn_reread_budget: float = 0.0
    session_reread_budget: float = 0.0
    #: L105 : durée maximale d'un tour d'agent mené (secondes). Au-delà, le
    #: tour se clôt au prochain point sûr (fin de l'appel d'outil en cours) et
    #: le travail reprend au tour suivant, dans la même session ; réglable par
    #: agent (`ameesh set turn_max_seconds=…`). 0 = pas de durée maximale.
    turn_max_seconds: float = 1800.0
    #: L105 : messages remis par le hook de courrier pendant un même tour ;
    #: les suivants attendent le tour suivant, et l'agent est invité à
    #: conclure (jamais tant qu'un travail lancé pendant le tour tourne encore).
    #: Réglable par agent (`ameesh set turn_mail_max=…`). 0 = sans borne.
    #: 20 depuis le 2026-10-11 (5 coupait un tour sur deux sous un fort courrier).
    turn_mail_max: int = 20
    #: travail de fond d'un tour (correctif du 2026-10-11) : à la fin d'un tour
    #: normal, ce qui tourne encore (processus du groupe du tour, conteneurs du
    #: tour sans lot) a ce délai (secondes) avant le nettoyage habituel ;
    #: réglable par agent (`ameesh set turn_grace_seconds=…`). 0 = nettoyage
    #: immédiat (comportement d'avant).
    turn_grace_seconds: float = 1200.0
    #: L48 : échecs de tour — attente maximale entre deux tours en échec, durée
    #: sous laquelle un échec est « rapide », série d'échecs rapides qui arrête l'agent
    failure_backoff_max: float = 300.0
    fast_failure_s: float = 60.0
    max_fast_failures: int = 5
    #: L106 : binaires des harnais choisis par l'hôte (`{"claude": "/chemin"}`),
    #: après `AMEESH_<HARNAIS>_BIN` et avant le PATH (`adapters.resolve_harness`)
    harness_bins: dict = field(default_factory=dict)
    #: L106 : hôte non prêt (binaire, interpréteur introuvable) — attente
    #: maximale entre deux essais (doublée à chaque échec, de 15 s à cette borne)
    host_retry_max: float = 300.0
    #: L106 : arrêt propre sur batterie critique — délai laissé aux tours en
    #: cours pour finir avant d'être arrêtés (SIGTERM, consigne remise en attente)
    power_stop_grace: float = 120.0
    #: arrêt de l'exécuteur (SIGTERM de systemd, SIGINT) : drainage — plus de
    #: nouveau tour, le tour en cours et son travail de fond finissent dans
    #: cette borne (secondes), puis arrêt ; 0 = arrêt immédiat (le tour en
    #: cours est arrêté, sa consigne remise en attente). Un second signal
    #: arrête tout de suite. L'unité systemd garde TimeoutStopSec au-dessus.
    drain_seconds: float = 1800.0
    #: L159f: host gate, the state a capacity guard writes for this host
    #: (docs/GARDE-DE-CAPACITE.md). `host_gate`: empty (no gate on a classic
    #: host; a `file` gate on a lending host, one whose Host fiche has
    #: `policy.lend`), `file`, or `none` (no gate, even on a lending host).
    #: `host_gate_path`, `host_gate_ack`: the state and the acknowledgement
    #: (defaults `/run/ameesh-gate/state/state.json`, `…/ack/ack.json`).
    host_gate: str = ""
    host_gate_path: str = ""
    host_gate_ack: str = ""
    #: what an unreadable, absent or stale state counts as: empty = `stopped`
    #: on a lending host, `available` on a classic one
    host_gate_fallback: str = ""
    #: withdrawal deadline when a `draining` state gives none (seconds); 0 =
    #: the offer's `drain_seconds` on a lending host, 90 otherwise
    host_gate_drain: float = 0.0
    #: L159i: require the Linux guard heartbeat in a mediated VM's file gate.
    host_gate_lending: bool = False
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
    #: rétention des fils lisibles, en jours (L361 ; 0033 §9 : un an par
    #: défaut) : les entrées plus anciennes sont retirées par le ménage de
    #: l'exécuteur ; 0 = aucune expiration
    thread_retention_days: int = 365
    #: rétention du courrier en souffrance, en jours (L976) : le ménage de
    #: l'exécuteur re-livre au successeur d'un agent arrêté, et expire au-delà
    #: de ce délai le courrier dont personne ne lit la boîte (arrêté sans
    #: successeur, nom absent du registre) ; 0 = aucune expiration
    mail_dead_letter_days: int = 7
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
    #: L232d (#366) : jeton DÉDIÉ de l'API des défis de connexion
    #: (AMEESH_LOGIN_TOKEN_FILE, 0600), distinct du jeton de service de
    #: l'équipe ; tenu par le superviseur/exécuteur du compte, hors de portée
    #: des agents. Vide : aucune connexion publiée (l'API est de toute façon
    #: fermée par défaut côté service).
    login_token_file: str = ""
    #: TLS local du service (passthrough) appelé sur la boucle locale :
    #: `approve_url` = https://127.0.0.1:PORT, certificat vérifié pour CE nom
    #: d'hôte H (AMEESH_APPROVE_TLS_NAME) ; vide = nom de l'URL
    approve_tls_name: str = ""
    #: relevé périodique des ressources de l'hôte par l'exécuteur (secondes,
    #: L31, 0028) ; 0 = aucun relevé.
    resource_interval: float = 60.0
    #: passage périodique du ménage par l'exécuteur (secondes, L73) : quotas
    #: des dossiers temporaires et des caches, worktrees des lots finis ;
    #: 0 = aucun passage périodique (le ménage autour des tours reste).
    housekeeping_interval: float = 600.0
    #: relevé périodique des branches par l'exécuteur (secondes, L118) : un
    #: lot dont la branche est fusionnée dans sa cible passe livré ; 0 = aucun.
    branch_sweep_interval: float = 300.0
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
    #: #313 (L65b, décision 0033) : un compte dont l'identifiant (dossier de
    #: configuration, fichier de clé, valeur de clé) est déclaré aussi par un
    #: AUTRE mesh de l'hôte. `report` le signale (journal de l'exécuteur,
    #: alerte `account_shared`, `ameesh doctor`, `ameesh accounts list`) ;
    #: `strict` le refuse en plus dans CE mesh (`ameesh.account_claims`).
    account_sharing: str = "report"
    #: #2580 : la clé d'hôte qui refuse un compte partagé. `report` (défaut)
    #: le signale seulement ; `refuse` le refuse dans CE mesh, comme
    #: `account_sharing: strict`. À activer après la séparation des comptes
    #: (#65) : refuser aujourd'hui arrêterait les deux meshes du poste.
    accounts_shared: str = "report"
    #: #313/#2580 : le registre des revendications de comptes, partagé par les
    #: meshes de l'hôte ; vide = `~/.local/state/ameesh-host/accounts` (sous
    #: `$XDG_STATE_HOME` quand il est posé)
    account_registry: str = ""
    #: alertes poussées (L38, décision 0030 point 4) : la clé `notify` du
    #: fichier de configuration de l'HÔTE, telle quelle (routes, canaux,
    #: humain par défaut, types), validée par `ameesh.notify`. Jamais de
    #: secret ici : jetons et webhooks viennent de l'environnement ou d'un
    #: fichier 0600 nommés par cette clé.
    notify: dict = field(default_factory=dict)
    #: identité des humains par OIDC (L86, 0033 §4) : la clé `identity` de
    #: l'hôte (`binding` : `subject` par défaut ou `email` ; `providers` :
    #: issuer, client_id, domains), lue par `ameesh.oidc`. Jamais de secret
    #: ici : un client OIDC public suffit pour vérifier un jeton.
    identity: dict = field(default_factory=dict)
    #: L109 : exécuteur médié (`backend: mediated`) — URL du serveur du mesh
    #: (`/api/exec/v1`) et fichier 0600 du jeton d'accès de l'exécuteur (la
    #: source réelle du jeton est l'identité de l'exécuteur, L110). Dans la
    #: VM, la session du harnais reçoit AMEESH_EXEC_URL et AMEESH_EXEC_TOKEN.
    exec_url: str = ""
    exec_token_file: str = ""
    #: L109: mediated host, provider/DB credentials remain on the server.
    host_mediated: bool = False
    #: Off-device session backup; storage credentials come from the environment.
    session_backup: dict = field(default_factory=dict)
    #: L131: heavy suites run at once on this host (`ameesh heavy`); 0 = a
    #: quarter of the CPUs, at least 1
    max_heavy_suites: int = 0
    #: #453: duration cap of a heavy command (`ameesh heavy`), seconds or
    #: `90m`/`2h`; `AMEESH_HEAVY_MAX_S` overrides it; 0 = no cap
    heavy_max_seconds: float | str = 3600.0
    #: L226a: the `credential_sentinel` key of the host configuration
    #: (`probes`, `gcloud_session_hours`, `warn_hours`), read by `ameesh.sentinel`
    credential_sentinel: dict = field(default_factory=dict)
    #: issues GitHub des lots (L126) : la clé `github` du fichier de
    #: configuration de l'HÔTE, telle quelle (`projects` : projet → dépôt,
    #: `host` : hôte désigné pour la projection automatique, `interval`,
    #: `exclude_terms`), validée par `ameesh.plan_github`. Jamais de secret
    #: ici : le jeton de `gh` reste dans son environnement.
    github: dict = field(default_factory=dict)
    #: #4174/#4256: spec acceptance, validation authority routes and timezones.
    project_planning: dict = field(default_factory=dict)
    #: radar de conflits entre lots (L160b) : la clé `radar` du fichier de
    #: configuration de l'HÔTE désigné (`projects` : projet → dépôt et dossier
    #: des migrations), validée par `ameesh.radar`.
    radar: dict = field(default_factory=dict)
    #: #4006 : pont d'approbation GitHub. Clé `approvals` du fichier de
    #: configuration : `authority` (nom de l'autorité, sans défaut : #4259),
    #: `projection` (chemin local en lecture seule),
    #: `max_age_s`, `owner_uid` éventuel, et côté bureau `source` (dépôt,
    #: issue, approbateurs, `token_file` 0600, TTL). Jamais de valeur de
    #: jeton ici : seulement le chemin du fichier qui le porte.
    approvals: dict = field(default_factory=dict)
    #: #3264 A / #4291 : veille quotidienne des versions de harnais et de
    #: modèles (clé `veille` du fichier de l'hôte : `sources` — une entrée par
    #: harnais, `npm` ou `releases`, `changelog`, `provider` —, `app`,
    #: `estimate`, `host` de réconciliation, `model_days`). Validée par
    #: `ameesh.veille` ; absente = aucune veille. Aucune source propre à un
    #: client n'est codée ici : tout vient de `veille.sources`.
    veille: dict = field(default_factory=dict)
    #: #4425 : journal d'observations IMA. Clé `observations` du fichier de
    #: configuration : `authority` (sans défaut), `journal` et `projection`
    #: (chemins locaux), `max_age_s`, `owner_uid` éventuel, et côté bureau
    #: `source` (identité `journal_id`/`repos`/`issues`, `staging` pour les
    #: attestations natives, `acceptances` pour le lecteur #4006, `ttl_s`).
    #: Jamais de secret ici : la collecte lit la projection locale de #4006.
    observations: dict = field(default_factory=dict)
    #: #2448 : release aux coupures du canon compilé (clé `release` du fichier
    #: de l'hôte opérateur : `host` et `projects` — dépôt, cible, staging,
    #: rapport). Validée par `ameesh.release_cutoff` ; absente = aucune.
    release: dict = field(default_factory=dict)
    #: #2601 : alerte « créneau manqué » des paquets horaires (clé
    #: `packet_watch` du fichier de l'hôte opérateur : `host`, `projects` —
    #: destinataires et grâce). Validée par `ameesh.packet_watch` ; absente =
    #: aucune alerte.
    packet_watch: dict = field(default_factory=dict)
    #: #2446 : clé `train` du fichier de configuration de l'HÔTE désigné pour
    #: le service de train (`host`, `mode`, `projects`). Lue telle quelle et
    #: validée par `ameesh.train_assembly`, qui refuse nommément une pièce
    #: manquante : aucun passage n'est assemblé sans elle.
    train: dict = field(default_factory=dict)
    #: #2212 : fichier de configuration lu par `load` ("" sans fichier), pour
    #: relire `radar` à chaque passage du feu (`fresh_radar`) ; calculé, jamais
    #: lu tel quel
    config_file: str = ""
    #: Neutral memory transport settings; ssh_key names a host-managed key file.
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

    @property
    def config_source(self) -> str:
        """Où vient la configuration effective, en une ligne (#1466).

        Un profil est un fichier rangé sous le dossier des profils d'exécuteur
        (`~/.config/ameesh/runner-profiles/`) : il hérite des clés de
        `config.json` qu'il ne déclare pas. Cet écart — un exécuteur qui lit un
        profil et non la configuration de l'hôte — doit rester visible."""
        if not self.config_path:
            return "defaults (no config file)"
        if not self.config_base:
            return self.config_path
        texte = "%s (profile; host config %s)" % (self.config_path, self.config_base)
        if self.config_shadowed:
            texte += "; overrides: %s" % ", ".join(self.config_shadowed)
        return texte


#: dossier des profils d'exécuteur : ses fichiers héritent de `config.json`
#: (#1466). Les autres fichiers rangés à côté de `config.json` — par exemple
#: `config-ancienne-base.json`, la configuration d'une AUTRE base — restent
#: autonomes : ils n'empruntent ni comptes, ni `github`, ni `critical_agents`
#: à l'hôte (revue Claude #1466 : un héritage trop large rejouerait des
#: synchronisations GitHub). `"inherit": true` reste possible pour forcer.
def _config_dir_default() -> str:
    return os.path.join(os.path.dirname(_expand(DEFAULT_CONFIG)), "runner-profiles")


def _read_json_object(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:  # config cassée : on le dit, on continue
        raise SystemExit("unreadable config %s: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise SystemExit("unreadable config %s: JSON object expected" % path)
    return raw


def _inherit_base(cfg_path: str, raw: dict) -> str:
    """La base héritée d'un fichier de configuration (#1466), ou ''.

    Un **profil** — un fichier de configuration rangé sous le dossier des
    profils d'exécuteur (`~/.config/ameesh/runner-profiles/`) — est une
    superposition : les clés qu'il ne déclare pas (comptes, plafonds, canon…)
    viennent du `config.json` de l'hôte. C'est ce qui évite qu'un profil périmé
    fasse tourner des exécuteurs sur des comptes ou des plafonds d'hier.

    `"inherit"` force la règle : `false` rend le fichier autonome (aucun
    héritage), `true` hérite même hors du dossier des profils, une chaîne nomme
    une autre base. Hors du dossier des profils, un fichier reste autonome :
    le banc de tests, les configurations nommées dans un dossier temporaire et
    celles d'une autre base rangées à côté de `config.json` ne lisent jamais la
    configuration du poste."""
    inherit = raw.get("inherit", None)
    if inherit is False:
        return ""
    if inherit is True:
        base = _expand(DEFAULT_CONFIG)
    elif inherit is None:
        base = _expand(DEFAULT_CONFIG)
        dossier = _config_dir_default()
        # hors du dossier des profils : autonome (banc de tests, configuration
        # d'une autre base rangée à côté de `config.json`)
        if not cfg_path.startswith(dossier + os.sep):
            return ""
    elif isinstance(inherit, str) and inherit.strip():
        base = _expand(inherit.strip())
    else:
        raise SystemExit("invalid `inherit` %r (expected false, true or a path)"
                         % (inherit,))
    if os.path.abspath(base) == os.path.abspath(cfg_path):
        return ""       # un fichier ne s'hérite jamais lui-même
    if not os.path.exists(base):
        raise SystemExit("inherited config not found: %s (base of %s)" % (base, cfg_path))
    return base


#: #3345 : réglages qui valent pour TOUS les exécuteurs d'un hôte — le service
#: de train (`train`), la projection GitHub des lots (`github`) et les coupures
#: de release (`release`). Un profil qui en surcharge une partie fait diverger
#: les exécuteurs du même hôte sans le dire : le 24/10, les trois runners
#: critiques lisaient un profil resté en `train.mode: shadow` et sans `github`
#: alors que `config.json` était en train actif. Ces clés ne sont pas des
#: réglages de profil : `ameesh doctor` et le contrôle de déploiement les
#: signalent.
SHARED_CONFIG_KEYS = ("train", "github", "release")


#: sentinelle d'une clé absente d'un côté d'une comparaison de configuration
_MISSING = object()

#: #3345 : les seuls objets dont un profil peut surcharger une feuille sans
#: recopier le bloc. `train` et `release` portent des sous-réglages que les
#: profils ajustent légitimement (mode du train, hôte de coupure) ; partout
#: ailleurs la superposition reste de **premier niveau** (#1466) : une clé
#: déclarée remplace la valeur de la base en entier, y compris `accounts`
#: (isolation), `github`, `radar`, `packet_watch`, `notify`… (décision
#: mesh-design #133247 du 24/10 13:53Z : liste d'autorisation, pas exclusion).
RECURSIVE_CONFIG_KEYS = ("train", "release")


def _merge_config(base: dict, overlay: dict, *, recursive: bool = False) -> dict:
    """Superpose `overlay` (un profil) sur `base` (config.json), clé par clé.

    #3345 : un profil ne porte que ce qu'il surcharge. Une clé absente de
    l'overlay est héritée de la base ; une clé déclarée la remplace en entier,
    **sauf `train` et `release`** (`RECURSIVE_CONFIG_KEYS`) dont les objets se
    fusionnent feuille à feuille, à toute profondeur : `{"train": {"mode":
    "shadow"}}` garde le `host` et les `projects` de `config.json`, et un
    `train.projects.ameesh` partiel garde son `repo`/`target` et les autres
    projets. La liste d'autorisation ne vaut qu'au **premier niveau** : une
    fois dans `train` ou `release`, `recursive` est vrai et tous leurs objets
    se fusionnent. Tout autre objet déclaré (`radar`, `packet_watch`, `github`,
    `notify`, `accounts`…) remplace la valeur de la base en entier (un
    `notify: {}` vide bien les routes héritées). La récursion générale de la
    première version de #3345 faisait hériter en silence les sous-clés
    d'objets que le profil voulait restreindre."""
    merged = dict(base)
    for key, value in overlay.items():
        old = merged.get(key, _MISSING)
        if (isinstance(old, dict) and isinstance(value, dict)
                and (recursive or key in RECURSIVE_CONFIG_KEYS)):
            merged[key] = _merge_config(old, value, recursive=True)
        else:
            merged[key] = value
    return merged


def _config_diff(base, value, path: str) -> list[tuple[str, object, object]]:
    """Feuilles qui diffèrent entre `base` et `value`, avec leur chemin pointé.

    Les objets JSON sont parcourus récursivement (un projet, un mode) pour que
    le constat nomme `train.mode` plutôt que le bloc `train` entier ; une liste
    ou un scalaire est comparé d'un bloc."""
    if isinstance(base, dict) and isinstance(value, dict):
        out: list[tuple[str, object, object]] = []
        for key in sorted(set(base) | set(value)):
            child = "%s.%s" % (path, key)
            if key not in value:
                out.append((child, _MISSING, base[key]))
            elif key not in base:
                out.append((child, value[key], _MISSING))
            else:
                out.extend(_config_diff(base[key], value[key], child))
        return out
    if base is _MISSING and value is _MISSING:
        return []
    if base != value:
        return [(path, value, base)]
    return []


#: #3346 : marqueurs d'un nom de feuille qui porte un secret. `ameesh doctor`
#: ne montre jamais la valeur d'un jeton, d'une clé, d'un mot de passe ou d'un
#: DSN : le constat nomme la feuille (`github.token`), pas le secret. Le test
#: est sur le nom, jamais sur la valeur (une valeur secrète peut être courte).
SECRET_LEAF_MARKERS = ("token", "secret", "key", "pass", "pwd", "dsn")

#: ce qu'une valeur de feuille secrète affiche à la place d'elle-même
MASKED_SECRET = "***"


def _secret_leaf(path: str) -> bool:
    """Vrai si le chemin nomme une feuille secrète (#3346).

    Le test est sur le nom, insensible à la casse, à n'importe quel segment :
    `github.token`, `github.api_key`, `train.password` et `release.dsn` sont
    masqués ; `train.mode` ou `github.projects.ameesh.repo` restent lisibles.
    Regarder tous les segments couvre aussi un objet rangé sous un nom secret
    (`github.token.value`) : un jeton ne fuit pas par un sous-champ."""
    return any(marker in path.lower() for marker in SECRET_LEAF_MARKERS)


def _masked(value, path: str):
    """Copie affichable de `value` : toute feuille secrète devient `***`.

    `_config_diff` rend un sous-arbre entier quand une clé n'existe que d'un
    côté, disparaît ou change de type ; ce conteneur peut porter un secret
    imbriqué (`github.auth.token` ajouté, `github` remplacé par `null`, liste
    d'objets…). Le masquage descend donc dans les objets et les listes au lieu
    de ne regarder que le chemin de la divergence (#3346, avis dev7/cx1)."""
    if _secret_leaf(path):
        return MASKED_SECRET
    if isinstance(value, dict):
        return {key: _masked(child, "%s.%s" % (path, key) if path else str(key))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_masked(item, path) for item in value]
    return value


def _shown(value, path: str) -> str:
    """Une valeur de configuration en une ligne courte, jamais un secret :
    seules les clés partagées (train, github, release) passent par ici ; une
    feuille dont le nom ressemble à un secret (#3346) s'affiche masquée, y
    compris sous un objet ou dans une liste rendue en bloc."""
    if value is _MISSING:
        return "<absent>"
    if _secret_leaf(path):
        return MASKED_SECRET
    texte = json.dumps(_masked(value, path), ensure_ascii=False, sort_keys=True)
    return texte if len(texte) <= 120 else texte[:117] + "..."


@dataclass(frozen=True)
class ProfileDivergence:
    """Une feuille d'une clé partagée où un profil diffère de `config.json`."""

    profile: str
    key: str
    path: str
    value: object
    base: object


@dataclass(frozen=True)
class ProfileAudit:
    """#3345 : état des profils d'exécuteur d'un hôte face à `config.json`."""

    directory: str
    host: str
    profiles: tuple[str, ...] = ()
    divergences: tuple[ProfileDivergence, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.divergences and not self.errors

    def lines(self) -> list[str]:
        out = ["profiles   : ERROR %s" % error for error in self.errors]
        for divergence in self.divergences:
            out.append("profiles   : DIVERGENCE %s: %s (profile %s, config.json %s)"
                       % (os.path.basename(divergence.profile), divergence.path,
                          _shown(divergence.value, divergence.path),
                          _shown(divergence.base, divergence.path)))
        if self.ok:
            out.append("profiles   : %d checked in %s; %s match %s"
                       % (len(self.profiles), self.directory,
                          ", ".join(SHARED_CONFIG_KEYS), self.host))
        return out


def audit_profiles(extra=(), directory: str | None = None,
                   host: str | None = None) -> ProfileAudit:
    """#3345 : audite les profils héritant de `config.json`.

    Tout fichier `*.json` du dossier des profils qui hérite du `config.json`
    de l'hôte (le cas normal) est fusionné comme `load` le ferait, puis ses
    clés partagées (`SHARED_CONFIG_KEYS`) sont comparées à celles de la base.
    `extra` ajoute le profil que le processus courant lit lui-même, même rangé
    ailleurs (`"inherit": true`). Un profil `"inherit": false`, ou qui hérite
    d'une AUTRE base, est autonome par déclaration : il n'est pas comparé.

    Ne lève pas : un profil illisible devient une ligne d'erreur, pour que le
    diagnostic ne casse jamais et que le déploiement puisse refuser."""
    directory = _expand(directory) if directory else _config_dir_default()
    host = _expand(host) if host else _expand(DEFAULT_CONFIG)
    errors: list[str] = []
    candidates: list[str] = []
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        names = []
    except OSError as exc:
        return ProfileAudit(directory, host, (),
                            errors=("%s unreadable (%s)" % (directory, exc),))
    for name in names:
        if name.endswith(".json"):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                candidates.append(path)
    for path in extra:
        if path and os.path.isfile(_expand(path)):
            candidates.append(_expand(path))
    seen: set[str] = set()
    profiles: list[str] = []
    divergences: list[ProfileDivergence] = []
    for path in candidates:
        ident = os.path.abspath(path)
        if ident in seen:
            continue
        seen.add(ident)
        try:
            raw = _read_json_object(path)
            base_path = _inherit_base(path, raw)
            # autonome, ou base explicitement différente de l'hôte : hors du
            # contrat « un profil surcharge config.json »
            if not base_path or os.path.abspath(base_path) != os.path.abspath(host):
                continue
            base_raw = _read_json_object(base_path)
        except SystemExit as exc:
            errors.append("%s: %s" % (os.path.basename(path), exc))
            continue
        profiles.append(path)
        merged = _merge_config(base_raw, raw)
        for key in SHARED_CONFIG_KEYS:
            if key not in base_raw and key not in merged:
                continue
            for chemin, value, base_value in _config_diff(
                    base_raw.get(key, _MISSING), merged.get(key, _MISSING), key):
                divergences.append(ProfileDivergence(
                    profile=path, key=key, path=chemin, value=value, base=base_value))
    return ProfileAudit(directory=directory, host=host, profiles=tuple(profiles),
                        divergences=tuple(divergences), errors=tuple(errors))


def load(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    # #1127 : une variable explicite nomme un fichier qui doit exister. Un
    # chemin explicite absent ne se replie plus en silence sur la config
    # historique : le DSN changeait alors sans le dire, et la sonde de schéma
    # concluait à tort à un schéma absent. La variable retenue est celle qui
    # décide du chemin (AMEESH_CONFIG, sinon son alias) : un alias périmé
    # n'empêche pas la variable courante de faire foi.
    explicit = next((name for name in ("AMEESH_CONFIG", "AGENT_MESH_CONFIG")
                     if env.get(name)), "")
    if explicit:
        cfg_path = _expand(env[explicit])
        if not os.path.exists(cfg_path):
            raise SystemExit("config file not found: %s (%s)"
                             % (cfg_path, explicit))
    else:
        # Repli historique : sans config explicite, une config agent-mesh
        # existante reste lue (compatibilité du banc et des postes basculés).
        cfg_path = _expand(DEFAULT_CONFIG)
        legacy_cfg = _expand(LEGACY_CONFIG)
        if not os.path.exists(cfg_path) and os.path.exists(legacy_cfg):
            cfg_path = legacy_cfg
    raw: dict = {}
    base_path = ""
    shadowed: tuple[str, ...] = ()
    if os.path.exists(cfg_path):
        raw = _read_json_object(cfg_path)
        # #1466 : un profil (fichier rangé sous le dossier de la configuration
        # de l'hôte) hérite de `config.json` ; ses propres clés l'emportent.
        # #3345 : la récursion ne s'ouvre qu'au premier niveau, et seulement
        # pour `train` et `release` (RECURSIVE_CONFIG_KEYS) ; dans ces deux
        # blocs, tous les objets se fusionnent à toute profondeur. Toute autre
        # clé déclarée remplace la valeur de la base en entier. Les clés de
        # premier niveau dont la valeur effective diffère de la base sont
        # retenues : c'est l'écart qu'un profil périmé creuse, et il doit
        # rester lisible.
        base_path = _inherit_base(cfg_path, raw)
        if base_path:
            base_raw = _read_json_object(base_path)
            merged = _merge_config(base_raw, raw)
            shadowed = tuple(sorted(
                key for key in raw
                if key in base_raw and merged.get(key, _MISSING) != base_raw[key]))
            raw = merged
        known = {f for f in Config.__dataclass_fields__}  # type: ignore[attr-defined]
        known.discard("extra_canons")   # `canons` (liste) ci-dessous, jamais tel quel
        known.discard("budget_explicit")  # calculé, jamais lu tel quel
        known.discard("config_file")      # calculé, jamais lu tel quel
        known.discard("config_path")    # #1466 : décrit la lecture, pas un réglage
        known.discard("config_base")
        known.discard("config_shadowed")
        if isinstance(raw.get("humans"), list):
            raw = dict(raw, humans=",".join(str(n) for n in raw["humans"]))
        cfg = replace(cfg, config_file=cfg_path,
                      **{k: v for k, v in raw.items() if k in known})
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
    exec_token_file = pick("AMEESH_EXEC_TOKEN_FILE", default=cfg.exec_token_file)
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
        db_retry_max=_as_float(pick("AMEESH_DB_RETRY_MAX"), cfg.db_retry_max),
        statement_timeout_ms=_as_float(
            pick("AMEESH_STATEMENT_TIMEOUT_MS", "AGENT_MESH_STATEMENT_TIMEOUT_MS"),
            cfg.statement_timeout_ms),
        mesh_backup_window=_as_window(
            pick("AMEESH_MESH_BACKUP_WINDOW", default=cfg.mesh_backup_window),
            cfg.mesh_backup_window),
        mesh_backup_min_share=_as_share(
            pick("AMEESH_MESH_BACKUP_MIN_SHARE", default=cfg.mesh_backup_min_share),
            cfg.mesh_backup_min_share),
        mesh_backup_min_group_share=_as_share(
            pick("AMEESH_MESH_BACKUP_MIN_GROUP_SHARE",
                 default=cfg.mesh_backup_min_group_share),
            cfg.mesh_backup_min_group_share),
        lease_ttl=_as_float(pick("AMEESH_LEASE_TTL", "AGENT_MESH_LEASE_TTL"), cfg.lease_ttl),
        idle_nudge=_as_float(pick("AMEESH_IDLE_NUDGE", "AGENT_MESH_IDLE_NUDGE"), cfg.idle_nudge),
        poll=_as_float(pick("AMEESH_POLL", "AGENT_MESH_POLL"), cfg.poll),
        runner_liveness_interval=max(60.0, _as_float(
            pick("AMEESH_RUNNER_LIVENESS_INTERVAL", "AGENT_MESH_RUNNER_LIVENESS_INTERVAL"),
            cfg.runner_liveness_interval)),
        event_coalesce=_as_float(
            pick("AMEESH_EVENT_COALESCE", "AGENT_MESH_EVENT_COALESCE"), cfg.event_coalesce),
        mail_batch=_as_seconds(pick("AMEESH_MAIL_BATCH"),
                               _as_seconds(cfg.mail_batch, Config.mail_batch)),
        lot_wake=bool(_as_bool(pick("AMEESH_LOT_WAKE"), cfg.lot_wake, "AMEESH_LOT_WAKE")),
        lot_wake_margin=_as_seconds(pick("AMEESH_LOT_WAKE_MARGIN"),
                                    _as_seconds(cfg.lot_wake_margin, Config.lot_wake_margin)),
        lot_wake_max=max(0, int(_as_float(pick("AMEESH_LOT_WAKE_MAX"), cfg.lot_wake_max))),
        prompt_mail_max=_as_float(pick("AMEESH_PROMPT_MAIL_MAX"), cfg.prompt_mail_max),
        canon_sync_interval=_as_float(
            pick("AMEESH_CANON_SYNC_INTERVAL", "AGENT_MESH_CANON_SYNC_INTERVAL"),
            cfg.canon_sync_interval),
        budget_usd_per_hour=_as_float(
            pick("AMEESH_BUDGET_USD_PER_HOUR", "AGENT_MESH_BUDGET_USD_PER_HOUR"),
            cfg.budget_usd_per_hour),
        budget_usd_per_day=_as_float(
            pick("AMEESH_BUDGET_USD_PER_DAY"), cfg.budget_usd_per_day),
        budget_explicit=tuple(
            key for key, names in (
                ("budget_usd_per_hour", ("AMEESH_BUDGET_USD_PER_HOUR",
                                         "AGENT_MESH_BUDGET_USD_PER_HOUR")),
                ("budget_usd_per_day", ("AMEESH_BUDGET_USD_PER_DAY",)))
            if key in raw or pick(*names)),
        budget_check_interval=_as_float(
            pick("AMEESH_BUDGET_CHECK_INTERVAL", "AGENT_MESH_BUDGET_CHECK_INTERVAL"),
            cfg.budget_check_interval),
        interrupt_senders=_as_names(
            pick("AMEESH_INTERRUPT_SENDERS", "AGENT_MESH_INTERRUPT_SENDERS")
        ) or cfg.interrupt_senders,
        idle_after_s=_as_bounded_positive(pick("AMEESH_IDLE_AFTER_S"), cfg.idle_after_s),
        baseline_hosts=int(_as_bounded_positive(pick("AMEESH_BASELINE_HOSTS"), cfg.baseline_hosts)),
        lifecycle_hook=str(pick("AMEESH_LIFECYCLE_HOOK", default=cfg.lifecycle_hook)
                           or "").strip(),
        lifecycle_hook_timeout=_as_bounded_positive(
            pick("AMEESH_LIFECYCLE_HOOK_TIMEOUT"), cfg.lifecycle_hook_timeout),
        host_release_interval=_as_bounded_positive(
            pick("AMEESH_HOST_RELEASE_INTERVAL"), cfg.host_release_interval),
        host_release_drain_bound=_as_bounded_positive(
            pick("AMEESH_HOST_RELEASE_DRAIN_BOUND"), cfg.host_release_drain_bound),
        host_release_retry=_as_bounded_positive(
            pick("AMEESH_HOST_RELEASE_RETRY"), cfg.host_release_retry),
        host_release_settle=_as_bounded_positive(
            pick("AMEESH_HOST_RELEASE_SETTLE"), cfg.host_release_settle),
        host_release_boot_grace=_as_bounded_positive(
            pick("AMEESH_HOST_RELEASE_BOOT_GRACE"), cfg.host_release_boot_grace),
        critical_agents=_as_names(pick("AMEESH_CRITICAL_AGENTS")) or tuple(
            n.strip() for n in (cfg.critical_agents.split(",")
                                if isinstance(cfg.critical_agents, str)
                                else cfg.critical_agents or ()) if str(n).strip()),
        critical_fallback_harnesses=_as_names(pick("AMEESH_CRITICAL_FALLBACK_HARNESSES"))
        or tuple(n.strip() for n in (cfg.critical_fallback_harnesses.split(",")
                                     if isinstance(cfg.critical_fallback_harnesses, str)
                                     else cfg.critical_fallback_harnesses or ())
                 if str(n).strip()),
        noncritical_hourly_share=_as_share(
            pick("AMEESH_NONCRITICAL_HOURLY_SHARE"),
            _as_share(cfg.noncritical_hourly_share, Config.noncritical_hourly_share)),
        session_max_tokens=_as_float(
            pick("AMEESH_SESSION_MAX_TOKENS", "AGENT_MESH_SESSION_MAX_TOKENS"),
            cfg.session_max_tokens),
        session_max_turn_seconds=_as_float(
            pick("AMEESH_SESSION_MAX_TURN_SECONDS", "AGENT_MESH_SESSION_MAX_TURN_SECONDS"),
            cfg.session_max_turn_seconds),
        session_min_turns=int(_as_float(
            pick("AMEESH_SESSION_MIN_TURNS", "AGENT_MESH_SESSION_MIN_TURNS"),
            cfg.session_min_turns)),
        context_max_tokens=max(0.0, _as_float(pick("AMEESH_CONTEXT_MAX_TOKENS"),
                                              cfg.context_max_tokens)),
        turn_reread_budget=max(0.0, _as_float(pick("AMEESH_TURN_REREAD_BUDGET"),
                                              cfg.turn_reread_budget)),
        session_reread_budget=max(0.0, _as_float(pick("AMEESH_SESSION_REREAD_BUDGET"),
                                                 cfg.session_reread_budget)),
        turn_max_seconds=max(0.0, _as_float(pick("AMEESH_TURN_MAX_SECONDS"),
                                            cfg.turn_max_seconds)),
        turn_mail_max=_as_count(pick("AMEESH_TURN_MAIL_MAX"), cfg.turn_mail_max),
        turn_grace_seconds=float(_as_count(pick("AMEESH_TURN_GRACE_SECONDS"),
                                           int(cfg.turn_grace_seconds))),
        failure_backoff_max=_as_float(pick("AMEESH_FAILURE_BACKOFF_MAX"),
                                      cfg.failure_backoff_max),
        fast_failure_s=_as_float(pick("AMEESH_FAST_FAILURE_S"), cfg.fast_failure_s),
        max_fast_failures=int(_as_float(pick("AMEESH_MAX_FAST_FAILURES"),
                                        cfg.max_fast_failures)),
        host_retry_max=_as_float(pick("AMEESH_HOST_RETRY_MAX"), cfg.host_retry_max),
        power_stop_grace=_as_float(pick("AMEESH_POWER_STOP_GRACE"), cfg.power_stop_grace),
        drain_seconds=float(_as_count(pick("AMEESH_DRAIN_SECONDS"), int(cfg.drain_seconds))),
        session_policy=str(pick("AMEESH_SESSION_POLICY", default=cfg.session_policy)
                           or "par-lot").strip(),
        balance_interval=_as_float(pick("AMEESH_BALANCE_INTERVAL"), cfg.balance_interval),
        worktree_roots=tuple(
            _expand(n) for n in _as_names(
                pick("AMEESH_WORKTREE_ROOTS", "AGENT_MESH_WORKTREE_ROOTS"))
        ) or cfg.worktree_roots,
        threads_dir=_expand(threads_brut) if threads_brut else "",
        thread_retention_days=_as_days(pick("AMEESH_THREAD_RETENTION_DAYS"),
                                        cfg.thread_retention_days),
        mail_dead_letter_days=_as_days(pick("AMEESH_MAIL_DEAD_LETTER_DAYS"),
                                       cfg.mail_dead_letter_days),
        project=pick("AMEESH_PROJECT", default=cfg.project) or "",
        humans=pick("AMEESH_HUMANS", default=cfg.humans) or "",
        resource_interval=_as_float(pick("AMEESH_RESOURCE_INTERVAL"),
                                    cfg.resource_interval),
        housekeeping_interval=_as_float(pick("AMEESH_HOUSEKEEPING_INTERVAL"),
                                        cfg.housekeeping_interval),
        branch_sweep_interval=_as_float(pick("AMEESH_BRANCH_SWEEP_INTERVAL"),
                                        cfg.branch_sweep_interval),
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
        exec_url=str(pick("AMEESH_EXEC_URL", default=cfg.exec_url) or "").strip(),
        exec_token_file=_expand(str(exec_token_file)) if exec_token_file else "",
    )
    # L42 (0031) : une liste venue de l'environnement remplace celle du fichier
    env_untrusted = _as_bool(pick("AMEESH_CANON_UNTRUSTED", "AGENT_MESH_CANON_UNTRUSTED",
                                  default=None), None, "AMEESH_CANON_UNTRUSTED")
    canons_env = pick("AMEESH_CANONS", default=None)
    single_env = pick("AMEESH_CANON", "AGENT_MESH_CANON", default=None)
    if canons_env:
        paths = [_expand(p.strip()) for p in str(canons_env).split(":") if p.strip()]
        if single_env and paths and _expand(str(single_env)) != paths[0]:
            raise SystemExit("AMEESH_CANON (%s) and AMEESH_CANONS (first: %s) "
                             "conflict: the first of AMEESH_CANONS is the default "
                             "canon" % (single_env, paths[0]))
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
    login_token_file = pick("AMEESH_LOGIN_TOKEN_FILE", default=cfg.login_token_file) or ""
    cfg = replace(
        cfg,
        approve_url=str(pick("AMEESH_APPROVE_URL", default=cfg.approve_url) or "").strip(),
        approve_token_file=_expand(str(token_file)) if token_file else "",
        login_token_file=_expand(str(login_token_file)) if login_token_file else "",
        approve_tls_name=str(pick("AMEESH_APPROVE_TLS_NAME", default=cfg.approve_tls_name)
                             or "").strip().lower(),
    )
    # L159f: host gate
    gate_path = pick("AMEESH_HOST_GATE_PATH", default=cfg.host_gate_path) or ""
    gate_ack = pick("AMEESH_HOST_GATE_ACK", default=cfg.host_gate_ack) or ""
    cfg = replace(
        cfg,
        host_gate=str(pick("AMEESH_HOST_GATE", default=cfg.host_gate) or "").strip().lower(),
        host_gate_path=_expand(str(gate_path)) if gate_path else "",
        host_gate_ack=_expand(str(gate_ack)) if gate_ack else "",
        host_gate_fallback=str(pick("AMEESH_HOST_GATE_FALLBACK",
                                    default=cfg.host_gate_fallback) or "").strip().lower(),
        host_mediated=bool(_as_bool(pick("AMEESH_HOST_MEDIATED", default=cfg.host_mediated),
                                    False, "AMEESH_HOST_MEDIATED")),
        host_gate_lending=bool(_as_bool(pick("AMEESH_HOST_GATE_LENDING", default=cfg.host_gate_lending),
                                       False, "AMEESH_HOST_GATE_LENDING")),
        host_gate_drain=float(_as_count(pick("AMEESH_HOST_GATE_DRAIN"),
                                        int(cfg.host_gate_drain))),
    )
    if cfg.host_gate not in (("", "none", "file", "socket")
                             if cfg.backend == "mediated" or cfg.host_mediated
                             else ("", "none", "file")):
        raise SystemExit("AMEESH_HOST_GATE invalide : %r (file | none)" % cfg.host_gate)
    if cfg.host_gate_fallback not in ("", "available", "draining", "stopped"):
        raise SystemExit("AMEESH_HOST_GATE_FALLBACK invalide : %r "
                         "(available | draining | stopped)" % cfg.host_gate_fallback)
    # #4148: never execute a malformed adapter command (NUL, unbounded text),
    # and never let a per-host map silently keep a non-text value.
    if "\x00" in cfg.lifecycle_hook or len(cfg.lifecycle_hook) > 1000:
        raise SystemExit("invalid lifecycle hook: empty, NUL or longer than 1000 chars")
    hooks = cfg.lifecycle_hooks
    if not isinstance(hooks, dict):
        raise SystemExit("invalid lifecycle_hooks: an object {host: command} expected")
    for host, command in hooks.items():
        if not isinstance(host, str) or not NAME_RE.match(host):
            raise SystemExit("invalid lifecycle_hooks host: %r" % (host,))
        if (not isinstance(command, str) or not command.strip()
                or "\x00" in command or len(command.strip()) > 1000):
            raise SystemExit("invalid lifecycle_hooks[%s]: a command line expected" % host)
    if not 30 <= cfg.host_release_interval <= 86400:
        raise SystemExit("invalid AMEESH_HOST_RELEASE_INTERVAL: 30..86400 seconds expected")
    if not 10 <= cfg.lifecycle_hook_timeout <= 3600:
        raise SystemExit("invalid AMEESH_LIFECYCLE_HOOK_TIMEOUT: 10..3600 seconds expected")
    if not 60 <= cfg.host_release_drain_bound <= 86400:
        raise SystemExit("invalid AMEESH_HOST_RELEASE_DRAIN_BOUND: 60..86400 seconds expected")
    if cfg.host_release_settle > cfg.host_release_drain_bound:
        raise SystemExit("invalid AMEESH_HOST_RELEASE_SETTLE: beyond the drain bound")
    if not 60 <= cfg.host_release_boot_grace <= 86400:
        raise SystemExit("invalid AMEESH_HOST_RELEASE_BOOT_GRACE: 60..86400 seconds expected")
    if cfg.canon_ref.startswith("-"):
        raise SystemExit("invalid AMEESH_CANON_REF: %r" % cfg.canon_ref)
    seen_paths: set[str] = set()
    for entry in cfg.canon_entries:
        if entry.ref.startswith("-"):
            raise SystemExit("invalid canon ref: %r (%s)" % (entry.ref, entry.path))
        if entry.path in seen_paths:
            raise SystemExit("canon configured twice: %s" % entry.path)
        seen_paths.add(entry.path)
    if not SCHEMA_RE.match(cfg.schema):
        raise SystemExit("invalid AGENT_MESH_SCHEMA: %r" % cfg.schema)
    if cfg.session_policy not in SESSION_POLICIES:
        raise SystemExit("invalid AMEESH_SESSION_POLICY: %r (%s)"
                         % (cfg.session_policy, " | ".join(SESSION_POLICIES)))
    # #313/#2580 : une politique mal écrite ne retombe pas en silence sur `report`
    sharing = str(cfg.account_sharing or "report").strip().lower()
    if sharing not in ("report", "strict"):
        raise SystemExit("invalid `account_sharing`: %r (report | strict)"
                         % cfg.account_sharing)
    shared = str(cfg.accounts_shared or "report").strip().lower()
    if shared not in ("report", "refuse"):
        raise SystemExit("invalid `accounts_shared`: %r (report | refuse)"
                         % cfg.accounts_shared)
    cfg = replace(cfg, account_sharing=sharing, accounts_shared=shared,
                  account_registry=_expand(str(cfg.account_registry))
                  if cfg.account_registry else "")
    if not isinstance(cfg.harness_bins, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in cfg.harness_bins.items()):
        raise SystemExit("unreadable config: `harness_bins` expects {harness: path}")
    if cfg.driver not in ("auto", "psycopg", "psql"):
        raise SystemExit("invalid AGENT_MESH_DRIVER: %r" % cfg.driver)
    if cfg.backend not in ("auto", "pg", "file", "mediated"):
        raise SystemExit("invalid AGENT_MESH_BACKEND: %r" % cfg.backend)
    if cfg.backend == "mediated" and not cfg.exec_url:
        raise SystemExit("mediated backend: missing server URL (exec_url or "
                         "AMEESH_EXEC_URL)")
    # #1466 : la provenance de la lecture voyage avec la configuration, pour
    # que l'exécuteur (journal, `ameesh doctor`) et l'agent (CLI) voient le
    # profil réellement lu et sa base.
    cfg = replace(cfg, config_path=cfg_path, config_base=base_path,
                  config_shadowed=shadowed)
    from .platform import child_environment
    child_environment.declare_accounts(cfg.accounts)
    child_environment.notify_credentials(cfg.notify)
    return cfg


def fresh_radar(cfg: Config) -> Config:
    """#2212 : `cfg` avec la clé `radar` relue dans le fichier que `load` a lu.
    Le mode du feu (file de fusion ou fusion GitHub directe) en dépend : un
    exécuteur démarré avant qu'un projet déclare `land` ne doit pas fusionner
    en direct ce qui passe désormais par la file. Fichier devenu illisible :
    ValueError — le passage échoue et sera retenté — plutôt qu'un retour
    silencieux à des conditions sans file. Sans fichier lu au démarrage
    (`config_file` vide), `cfg` tel quel.

    Un profil (#1466) hérite de sa base comme dans `load` : un `radar` déclaré
    seulement dans le `config.json` de l'hôte vaut pour le profil, et un
    `radar` déclaré dans le profil le remplace en entier (la récursion de
    #3345 ne s'ouvre que sur `train` et `release`)."""
    if not cfg.config_file:
        return cfg
    # `_read_json_object` et `_inherit_base` refusent par SystemExit, qui
    # arrêterait le fil de l'exécuteur : ValueError, comme un fichier illisible
    try:
        raw = _read_json_object(cfg.config_file)
        base_path = _inherit_base(cfg.config_file, raw)
        if base_path:
            raw = _merge_config(_read_json_object(base_path), raw)
    except SystemExit as exc:
        raise ValueError(str(exc)) from None
    radar = raw.get("radar")
    return replace(cfg, radar={} if radar is None else radar)


def _canons_from_file(raw: dict, where: str) -> dict:
    """`canons` du fichier de configuration (L42, 0031) → champs de Config.

    Le premier est le canon par défaut (`canon`, `canon_ref`,
    `canon_untrusted`) ; `canon` et `canons` à la fois : erreur claire."""
    items = raw.get("canons")
    if not isinstance(items, list):
        raise SystemExit("%s: `canons` must be a list" % where)
    if any(raw.get(k) not in (None, "") for k in ("canon", "canon_ref")) \
            or raw.get("canon_untrusted") is not None:
        raise SystemExit("%s: `canon`/`canon_ref`/`canon_untrusted` and `canons` both set "
                         "(the first item of `canons` is the default canon)" % where)
    entries = [_canon_entry(item, where) for item in items]
    if not entries:
        return {"canon": "", "canon_ref": "", "canon_untrusted": False, "extra_canons": ()}
    first = entries[0]
    return {"canon": first.path, "canon_ref": first.ref, "canon_untrusted": first.untrusted,
            "extra_canons": tuple(entries[1:])}


#: ce qu'un DSN affichable montre à la place de son mot de passe (L537)
MASKED_PASSWORD = "password=***"


def mask_dsn(dsn: str) -> str:
    """DSN affichable : le mot de passe n'apparaît jamais dans un journal.

    Le DSN sans son mot de passe est celui d'`ameesh.dsn.split_password`, qui
    suit les règles de libpq : mot de passe de l'userinfo, paramètre
    `password` de la requête, couples clé/valeur (blancs autour du `=`, valeurs
    citées, échappements), dernière entrée gagnante. `MASKED_PASSWORD` marque
    qu'il y en avait un ; un DSN que libpq refuserait s'affiche `<invalid DSN>`,
    jamais cité. L'environnement (PGSERVICE) ne compte pas : on ne lit que le
    DSN."""
    try:
        password, rest = dsn_mod.split_password(dsn, {})
    except dsn_mod.DsnError:
        return "<invalid DSN>"
    if not password:
        return rest
    if rest.startswith(("postgresql://", "postgres://")):
        return rest + ("&" if "?" in rest else "?") + MASKED_PASSWORD
    return ("%s %s" % (rest, MASKED_PASSWORD)) if rest else MASKED_PASSWORD
