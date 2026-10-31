# SPDX-License-Identifier: AGPL-3.0-only
"""Alerts pushed to the responsible human (lot L38, decision 0030 point 4).

  ameesh notify [--once] [--dry-run] [--json] [--interval S]
                [--only alerts|take|issues|github-watch|ci|fingerprints|coherence]
                [--skip issues|github-watch|ci|alerts] [--long-turn S]
                [--idle-mail S] [--dead-grace S]
                [--session-tokens N] [--stale-lot S] [--orphan-lot S]
                [--take-idle S] [--take-max-per-hour N]
                [--take-lot-idle S] [--take-lot-max-per-hour N]
                [--take-review-max-per-hour N]
                [--handoff-max-per-hour N] [--handoff-idle S]
                [--wip-max-returns N] [--wip-apply]
        follows the operations alerts (`ameesh alerts --follow`, same
        thresholds, same dedupe keys) and sends each raised alert and each
        resolution to the responsible human, on their channels.
        L119 (0037): on each pass, auto-take gives an item of the
        improvement backlog to idle agents (`ameesh.backlog.auto_take`);
        `--take-idle 0` turns it off.
        L165: before the backlog, an agent idle for `--take-lot-idle`
        (default 15 min) takes the highest-priority unassigned lot of its
        team and the orchestrator is told; `--take-lot-idle 0` turns it
        off.
        #1677/#1738: when no lot is free, auto-take proposes a review (a
        `qa` lot without a current verdict, another model family than the
        author) or a correction (current blocking verdict, author busy) to
        an idle agent; `--take-review-max-per-hour 0` turns these
        proposals off, and the improvement backlog then keeps the pass.
        L233b/L165: before any take, the non-started (`intake`) lots of a
        non-critical Claude agent paused on budget are returned to the team
        queue (no assignee, a journal note), where the automatic take gives
        them to any free capable agent (DeepSeek or Codex); its started
        (`build`) lots go to a free DeepSeek or Codex agent of the same
        project (`ameesh.handoff.budget_handoff`);
        `--handoff-max-per-hour 0` turns it off.
        §4 (owner 14/10/2026): each pass can also return the surplus lots of
        an agent above the WIP cap (`ameesh.wip.sweep`); the pass is off by
        default (`--wip-max-returns 0`) and, without `--wip-apply`, it only
        reports (nothing is returned).
        L126: on the host named by the `github` config key, each pass also
        maintains the GitHub issues of the lots
        (`ameesh.plan_github.Projector`) and polls the running CI: each
        failing test is mailed to the lot's agent (L161c,
        `ameesh.ci.Watcher`).
  ameesh notify --test human:<id> [--json]
        sends a test message on each of this human's channels.

Alerts used to be PULLED only (`ameesh alerts`): nothing told a human that
an orchestrator had stopped with mail (seen on 2026-10-07). This service
PUSHES them, as decision 0014 requires ("alerts to the responsible human").

Recipient (first found): the alert's `responsible` field, else the agent's
responsible (`agent_registry.responsible`), else that of the lot's work
package (walking up the parents), else that of the host's Host record
(canon), else `notify.default_human`. An alert with no recipient is logged,
never silently lost.

Configuration: the `notify` key of the HOST config file (never the canon);
secrets (ntfy token, Slack webhook) come from an environment variable or a
0600 file that the config NAMES — a secret value written in the config is
refused. See docs/EXPLOITATION.md, "Alertes poussées — ameesh notify".

Reliability: #3576 — the delivery state lives in the shared database
(`notify_state`, a `ameesh-notify-state/1` document) under its own execution
lease: the `alerts` pass may change host (#3383) without re-pushing a lasting
alert or losing a resolution, and a holder whose lease expired cannot
overwrite the new holder's state. `<state>/notify/state.json` remains the
local document (read before the database for the #532 local console, and the
fallback when the database is unreachable); the shared state supersedes it
at the first pass that takes the lease. A lasting alert is not resent after
a restart; an alert resolved while stopped is sent on restart; rate limited
per human (beyond that, a summary, whose members enter the `notified` ledger
once the summary is delivered); a failing channel does not block the others
and is retried on later passes, a bounded number of times. #2395: the state
also keeps a persistent `notified` ledger — one notification per alert
identity (same `since`, same severity) and per human, even if the alert
resolves and comes back, and even after `ameesh notify` restarts;
`slo_breach` is never pushed to the owner (canon §9, owner decision 18/10),
including from an old persisted summary bucket.

Channels: `desktop`, `ntfy`, `slack`, `app`, `nexlink`. L66 — the `desktop` channel
follows the OS through `ameesh.platform.notify`: `notify-send` on Linux,
`osascript` on macOS, a Windows toast through `powershell.exe` under WSL2;
native Windows comes with L67. `ameesh doctor --notify-test` says which
backend this OS uses (and sends a test if a `desktop` route is configured).
L90 — the `app` channel is the embedded interface: it pushes nothing, the
application reads the alerts through the mesh's event stream
(`GET /api/v1/events`, `alert.raised` / `alert.resolved`) and the alerts
view. Declaring it in a route says that this human's alerts are read in the
application; without it, a human whose only interface is the application
would be counted as having no channel.

`nexlink` (#3536, owner decision #138440) pushes the alert into the owner's
Nexlink app through a signed HMAC request; the content is encrypted to the
account's X25519 key-wrap key (generation 1) and wrapped per active key, so
the hub only stores the envelope (`ameesh.nexlink_alerts`). Without an
active key the alert is sent *content-free* (the app shows "mesh alert" and
opens the console): no detail leaves the mesh. The optional `fallback`
names an `ntfy`/`slack` channel that receives the same text, prefixed
"repli nexlink —", after the second consecutive failure (or at once on a
permanent error), once per alert; the primary keeps being retried, and a
fallback success does not mark the alert delivered on the Nexlink side.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import argparse
import base64
import errno
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

from . import config as config_mod
from . import db as db_mod
from . import alert_briefs, exploitation, fil, nexlink_alerts, notify_gap, \
    sous_utilisation, storage
from . import vocab
from .config import Config
from .platform import notify as platform_notify

NOTIFY_SCHEMA = "ameesh-notify/1"
STATE_SCHEMA = "ameesh-notify-state/1"

#: L165 (EP5): the mechanical passes a role timer may run alone.
#: `alerts` owns the delivery state and its lock; `take` observes the alerts
#: but writes no state; `issues`, `github-watch` and `ci` are independent.
ONLY_PASSES = ("alerts", "take", "issues", "github-watch", "ci")
#: #3885 (canon §16) and #4172: passes that exist ONLY under `ameesh role run`.
#: The full notifier never runs them (a source must be read by the lease
#: holder alone, and the coherence pass writes lots); they are accepted by
#: `--only`, and the pass itself refuses without `AMEESH_ROLE`.
ROLE_ONLY_PASSES = ("fingerprints", "coherence")
#: #3327: the passes a role timer carries. A full notifier may skip exactly
#: these (and only these) from its loop; every other pass (take, budget
#: handoff, WIP sweep) stays in the service. #3383: `alerts` joined them — the
#: owner channel follows the elected lease instead of the one host that held
#: the routes; a service that skips it takes no state lock, so the role timer
#: of the same host can. `fingerprints` is never part of the full loop at all
#: (#3885): it lives only under `role run`.
SKIPPABLE_PASSES = ("issues", "github-watch", "ci", "alerts")

#: L38 (0030) : types envoyés par défaut. `delegation_expired` arrive avec L40 :
#: il est accepté ici sans dépendre de son code (un type absent n'est jamais
#: levé, c'est tout).
#: L94 : la sous-utilisation (forfait perdu, agents au repos, orchestrateur
#: tenu, hôte à vide) est poussée comme la surcharge.
DEFAULT_TYPES = ("stopped_with_mail", "orphan_lot", "dead_runner", "idle_with_mail",
                 "delegation_expired", "engagement_overdue", "plan_underused",
                 "idle_capacity", "orchestrator_held", "host_underused", "balance_low",
                 # #4164 : un compte fournisseur sans crédit — aucun tour accordé
                 "provider_credits_exhausted",
                 # #4166 : un compte fournisseur bas — recharger avant la panne
                 "provider_credits_low",
                 # L226b/#1883 : un compte de forfait facturé au-delà du plan —
                 # l'owner le sait tout de suite, la règle est « jamais de dépassement »
                 "account_overage",
                 # #1883 : deux configurations de l'hôte déclarent le même nom de
                 # compte vers deux dossiers — lecture ambiguë, compte refusé
                 "account_identity",
                 # #3647 : un rythme de compte local, hors du canon (§11)
                 "account_pace_local",
                 # #313 : un autre mesh de l'hôte déclare le même identifiant de
                 # compte — quota et sessions partagés, contraire à 0033
                 "account_shared",
                 "host_not_ready", "host_power_low",
                 "host_unavailable", "host_drain_overdue", "lease_not_taken", "backlog_empty", "mail_undeliverable",
                 # #3256 A : feuille de route épuisée (une alerte à l'owner)
                 "roadmap_exhausted",
                 # #2341 : un hôte dont l'échantillonneur s'est tu n'alimente plus
                 # les autres alertes ; son propriétaire l'apprend
                 "host_unsampled",
                 # #3961 : la plupart des agents d'un hôte en pause opérateur
                 # depuis 15 min — une maintenance a laissé sa pause (#3014)
                 "operator_pause_host",
                 # L124 : une demande de décision attend le propriétaire, et sa
                 # relance à l'échéance (urgentes si la demande l'est)
                 "decision_pending", "decision_overdue",
                 # L161c : la CI de main est rouge (elle bloque les déploiements)
                 "ci_main_red",
                 # #845 : la garde de la CI — runner busy sans job (panne) et
                 # file au-delà du SLO de 15 min (capacité)
                 "ci_runner_stuck", "ci_queue_saturated",
                 # #3344 : la carte de mesure a plus de 48 h (suite complète
                 # pour chaque lot) — plus seulement une annotation CI
                 "map_stale",
                 # #2951: every notifier reports a silent GitHub observer
                 "github_observation_stale",
                 # #246 : btrfs presque plein côté allocation (df ne le voit pas)
                 "host_disk_low",
                 # #2022 : un volume presque plein, ou au plancher de pause bientôt
                 "host_disk_soon_full",
                 # #2022 : des zombies s'accumulent sur un hôte
                 "host_zombies",
                 # #3103 : un pont Docker capte le réseau local de l'hôte (#3092)
                 "host_network_overlap",
                 # L225 : des tours sans tarif au barème échappent à la dépense
                 "turn_unpriced",
                 # L226b : un agent attend un compte authentifié sain
                 "auth_blocked",
                 # L226a : un identifiant est en échec, ou expire bientôt
                 "credential_failing", "credential_expiring", "module_deviation",
                 # L317c : analyse profonde demandée, sans rapport dans les 30 min
                 "deep_analysis_overdue",
                 # #411: Docker storage needs an explicit owner cleanup decision.
                 "docker_orphan_volumes", "docker_build_cache", "docker_storage_unavailable",
                 # L242b : un agent critique est en pause budget
                 "critical_budget_pause",
                 # #3969 : un compte déclaré invalide tient un agent sans tour
                 "account_config_invalid",
                 # #1268 : un agent critique enchaîne les échecs de tour réels
                 "critical_turn_failures",
                 # L242c : canon illisible, rôles critiques tenus
                 "critical_roles_unreadable",
                 # #330: an owner key registered or replaced lately
                 "owner_key_registered",
                 # L160e2: a merge queue no longer moves
                 "merge_blocked",
                 # #194 r6: a PR merged into a wrong base, or not checked
                 "land_uncertain", "db_connections_high",
                 # #4609 : la sauvegarde quotidienne de la base du mesh n'a pas
                 # lu la base dans sa fenêtre — le propriétaire doit le savoir
                 "mesh_backup_unseen",
                 # #513 : la dépense comptée s'écarte des soldes (barème faux)
                 "budget_drift",
                 # #532: a local PgBouncer pool queues its mesh clients
                 "pgbouncer_pool_waiting")
CHANNEL_KINDS = ("desktop", "ntfy", "slack", "app", "nexlink")
DEFAULT_RATE_PER_MINUTE = 10
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_INTERVAL_S = 30.0
#: #3630 : anti-rebond du push `dead_runner` au propriétaire. Une coupure de
#: bail courte (hôte volatil, bail de 90 s renouvelé toutes les 30 s) est
#: reprise seule par l'exécuteur ; l'alerte n'est poussée qu'au-delà de cette
#: grâce, mesurée depuis l'échéance du bail (`since`). Le seuil de détection
#: (`--dead-grace`, 30 s) ne change pas : `ameesh alerts` voit la coupure.
#: Les coupures mesurées sur pc-smichea le 25/10 (relecture #3630) vont
#: jusqu'à 5,2 min après l'échéance (dev1 22:12:32 → 22:17:43) ; 8 min laisse
#: ~2,8 min de marge. Une panne réelle qui persiste au-delà pousse une alerte,
#: puis un seul `resolved`.
DEAD_RUNNER_NOTIFY_GRACE_S = 480.0
#: fenêtre de la limite de débit, et intervalle minimal entre deux résumés
RATE_WINDOW_S = 60.0
#: #2395 : rétention du registre `notified` (une notification par identité
#: d'alerte, sévérité et humain) — 30 jours, avec une borne dure pour que
#: l'état reste petit même sur une longue série d'alertes.
NOTIFIED_RETENTION_S = 30 * 24 * 3600.0
NOTIFIED_MAX = 5000
DEFAULT_SLACK_ENV = "AMEESH_SLACK_WEBHOOK"

#: clés qui porteraient un secret EN CLAIR dans la configuration : refusées
SECRET_KEYS = ("token", "password", "secret", "webhook", "webhook_url", "authorization",
               "auth", "key", "api_key")
#: options admises par type de canal (une faute de frappe est une erreur)
CHANNEL_OPTIONS = {
    "desktop": ("type", "timeout"),
    "ntfy": ("type", "timeout", "url", "topic", "token_env", "token_file"),
    "slack": ("type", "timeout", "webhook_env", "webhook_file"),
    # L90 : l'application embarquée lit le journal d'événements ; le canal ne
    # porte ni adresse ni secret, il déclare seulement la route.
    "app": ("type", "timeout"),
    # #3536 : API de l'app Nexlink, secret nommé + repli remote push.
    "nexlink": ("type", "timeout", "url", "token_env", "token_file", "fallback"),
}
TOP_KEYS = ("default_human", "types", "routes", "default", "channels", "rate_per_minute",
            "max_attempts", "interval", "timeout")

#: libellés lisibles des types (R12 : un humain lit ces messages)
TYPE_LABELS = {
    "db_connections_high": "database connection capacity",
    "mesh_backup_unseen": "mesh database backup not seen",
    "pgbouncer_pool_waiting": "local PgBouncer clients waiting",
    "pgbouncer_console_unreadable": "local PgBouncer console unreadable",
    "stopped_with_mail": "agent stopped with mail",
    "orphan_lot": "orphan {work}",
    "dead_runner": "dead runner",
    "idle_with_mail": "agent idle with mail",
    "delegation_expired": "delegation expired",
    "engagement_overdue": "commitment or due date passed",
    "long_turn": "long turn",
    "session_too_big": "session too big",
    "stale_lot": "stale {work}",
    # #789 t2 (canon §9) : adressée aux agents (alert_briefs) ; poussée aux
    # humains si le type est dans `notify.types`
    "slo_breach": "internal deadline (SLO) overdue",
    # #1107 (n2 P3 de #1035) : un lot que les passes d'alerte ne savent pas lire
    "lot_unreadable": "work item unreadable by the alert passes",
    "host_pressure": "host under pressure",
    # #4433: the host's memory PSI comes from one runner's own memory.max
    "runner_pressure": "runner at its own memory ceiling",
    "host_unsampled": "host sampler silent",
    "orphan_resource": "orphan resource",
    "tmpfs_full": "tmpfs almost full",
    "worktree_kept": "worktree kept at end of {work}",
    # #3396 : le doute sur les processus retient les retraits ; #3802 : un
    # verrou d'hôte trop vieux empêche tout passage — dans les deux cas
    # l'hôte se remplit, le détail de l'alerte dit lequel.
    "housekeeping_blocked": "housekeeping blocked",
    # #2618 : le libellé dit l'apparition, pas l'imputation : le ménage ne
    # supprime jamais une entrée qu'il n'a pas attribuée (#222, #234).
    "tmp_orphan": "/tmp entry that appeared during a turn",
    "tmp_unowned": "temporary directory without owner file (kept)",
    "tmp_unremovable": "temporary directory over quota with files it cannot remove",
    "tmp_unscanned": "temporary directory too large to scan; nothing evicted",
    "dsh_scope": "dsh tool scope left by a closed turn",
    "tmp_over_quota": "temporary directory over quota, not managed by this mesh",
    "plan_underused": "underused plan",
    "idle_capacity": "idle capacity",
    "orchestrator_held": "orchestrator held by a session",
    "host_underused": "host underused",
    "host_idle_reserved": "idle reserved host capacity",
    "baseline_oversized": "oversized baseline",
    "balance_low": "low balance",
    "provider_credits_exhausted": "provider credits exhausted",
    "provider_credits_low": "provider credits low",
    "host_not_ready": "host not ready",
    "auth_blocked": "authentication failing",
    "credential_failing": "credential failing",
    "credential_expiring": "credential expiring soon",
    "host_power_low": "host battery low",
    "backlog_empty": "improvement backlog empty",
    "roadmap_exhausted": "roadmap exhausted",
    "mail_undeliverable": "undeliverable mail",
    # #3007: cet hôte n'a aucune route pour l'humain — rien ne lui est poussé
    "notify_undeliverable": "human not reachable by the notifier",
    "decision_pending": "decision pending",
    "decision_overdue": "decision unanswered by its deadline",
    "host_unavailable": "host unavailable",
    "lease_not_taken": "runner cannot take its lease",
    "host_drain_overdue": "host drain overdue",
    "host_release_failed": "host release adapter failed",
    "host_release_overdue": "host release overdue",
    "host_release_no_runs": "host release blocked by unknown runs",
    "host_release_canon_unreadable": "host release blocked by unreadable canon",
    # L125 : mesure de l'effet du regroupement du courrier ; pas poussée par
    # défaut (à ajouter dans `notify.types` pour la recevoir)
    "turn_churn": "bursts of short turns",
    # L157b : adressée aux agents (alert_briefs) ; pas poussée aux humains par
    # défaut (à ajouter dans `notify.types` pour la recevoir)
    "estimate_bias": "estimate bias",
    "ci_main_red": "CI red on main",
    "ci_runner_stuck": "CI runner busy with no job running",
    "ci_queue_saturated": "CI queue beyond its SLO",
    "map_stale": "measured test map older than 48 h",
    "github_observation_stale": "GitHub observation missing or stale",
    "deep_analysis_overdue": "deep analysis without a report in time",
    "host_disk_low": "btrfs disk almost full",
    "host_disk_soon_full": "disk almost full, or filling up",
    "host_zombies": "zombie processes piling up on a host",
    "host_network_overlap": "container network overlapping a host route",
    "docker_orphan_volumes": "Docker orphan volumes accumulating",
    "docker_build_cache": "Docker reclaimable build cache above threshold",
    "docker_storage_unavailable": "Docker storage inventory unavailable",
    "turn_unpriced": "turn without a known price",
    # L233: briefed to the assignee and the orchestrators (alert_briefs); not
    # pushed to humans by default (add it to `notify.types` to receive it)
    "waiting_without_question": "{work} waiting for the owner with no question asked",
    # L159e6: not pushed by default (add it to `notify.types`)
    "guard_restart": "agents started again by the capacity guard",
    "merge_blocked": "merge queue blocked",
    "land_uncertain": "PR merged by ameesh into a wrong or unchecked base",

    "owner_key_registered": "owner key registered",
    "module_deviation": "module procedure deviation",
    "critical_budget_pause": "critical agent paused on budget",
    "account_config_invalid": "no valid harness account on the host",
    # #1268 : deux échecs de tour réels d'affilée sur un agent critique
    "critical_turn_failures": "critical agent failing turns",
    # #2729 : plus aucun tour n'aboutit — la préemption prioritaire se répète ;
    # pas poussée par défaut (à ajouter dans `notify.types`), lue par `ameesh alerts`
    "preemption_streak": "agent preempted turn after turn",
    # #3733 : briefée aux orchestrateurs de l'équipe et aux auditeurs
    # (alert_briefs) ; pas poussée aux humains par défaut (à ajouter dans
    # `notify.types`)
    "rate_limited_chronic": "agent rate-limited a large share of the last hour",
    "critical_roles_unreadable": "canon unreadable: critical roles held",
    # #1466: briefed to the orchestrators and auditors (alert_briefs); not
    # pushed to humans by default (add it to `notify.types` to receive it)
    "budget_pause_long": "agent paused on budget for a long time",
    # #3961: briefed to the orchestrators and auditors; the host-wide one is
    # also pushed to humans by default (DEFAULT_TYPES)
    "operator_pause_long": "agent under operator pause for a long time",
    "operator_pause_host": "most agents of a host under operator pause",
    # #254: not pushed by default (add it to `notify.types`)
    "host_hold": "host on hold",
    "budget_drift": "counted spend strays from the provider balance",
}


def type_label(kind: str, vocabulary=None) -> str:
    """The readable label of an alert type, in the active module's vocabulary.

    ``{work}``/``{works}`` in a ``TYPE_LABELS`` value are the work item's
    singular/plural (L102 T1, #4391); every other label passes through
    unchanged. The alert type itself, and the keys carrying it, never change.
    """
    vocabulary = vocab.active() if vocabulary is None else vocabulary
    label = TYPE_LABELS.get(kind, kind.replace("_", " "))
    return label.format(work=vocabulary.singular, works=vocabulary.plural)


#: types urgents : notification critique (bureau), priorité haute (ntfy)
URGENT_TYPES = ("stopped_with_mail", "orphan_lot", "dead_runner", "delegation_expired",
                "mail_undeliverable", "ci_main_red", "auth_blocked",
                # #4164 : plus aucun tour sur le compte tant qu'il n'est pas rechargé
                "provider_credits_exhausted",
                # #3536 : « jamais de dépassement » — l'owner le sait tout de suite
                "account_overage",
                "credential_failing", "critical_budget_pause", "critical_turn_failures",
                # #3969 : aucun tour tant que l'hôte n'est pas réparé
                "account_config_invalid",
                "owner_key_registered",
                "land_uncertain", "db_connections_high", "pgbouncer_pool_waiting",
                "host_network_overlap")
#: L124 : types dont la résolution n'est pas envoyée — une décision se résout
#: parce que l'humain y a répondu (ou que le demandeur l'a retirée) : le lui
#: redire serait du bruit. La résolution est journalisée.
QUIET_RESOLUTION_TYPES = ("decision_pending", "decision_overdue")
#: types qu'un passage observe depuis la configuration de l'hôte seule, sans
#: la base du mesh : la console du pool PgBouncer local (#532). Une panne de
#: base ne les résout jamais, et ne les cache pas non plus. #1179 : la passe
#: ordinaire ne lit pas cette console (`local_pool=False`) et ne résout donc
#: jamais ces types ; `run_local_pass` en est seul propriétaire.
LOCAL_TYPES = ("pgbouncer_pool_waiting", "pgbouncer_console_unreadable")

_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class NotifyConfigError(ValueError):
    """Clé `notify` de la configuration invalide (ou porteuse d'un secret)."""


class ChannelError(RuntimeError):
    """Échec d'envoi sur un canal. `permanent` : inutile de retenter tel quel
    (binaire absent, secret manquant) — journalisé une fois, pas de retentative.
    `status` : le code HTTP quand le canal en a reçu un (#3536 : 409, 429…)."""

    def __init__(self, message: str, permanent: bool = False, status: int | None = None,
                 body: bytes = b""):
        super().__init__(message)
        self.permanent = permanent
        self.status = status
        self.body = body


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Channel:
    name: str
    kind: str
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NotifyConfig:
    default_human: str | None = None
    types: tuple = DEFAULT_TYPES
    routes: dict = field(default_factory=dict)
    default: tuple = ()
    channels: dict = field(default_factory=dict)
    rate_per_minute: int = DEFAULT_RATE_PER_MINUTE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    interval: float | None = None
    timeout: float = DEFAULT_TIMEOUT_S

    def channels_for(self, human: str | None) -> list:
        """Canaux d'un humain : sa route, sinon `notify.default`."""
        if not human:
            return []
        names = self.routes.get(human)
        if names is None:
            names = self.default
        return [self.channels[n] for n in names]

    @property
    def configured(self) -> bool:
        return bool(self.routes or self.default)


def normalize_human(value) -> str | None:
    """`human:<id>` ; un identifiant nu devient `human:<id>` ; un `agent:…` ou
    une valeur illisible n'est pas un humain (None : on passe au repli)."""
    text = str(value or "").strip()
    if not text:
        return None
    if ":" not in text:
        text = "human:" + text
    return text if _HUMAN_RE.match(text) else None


def _find_secret(options: dict, where: str) -> None:
    for key, value in options.items():
        if str(key).lower() in SECRET_KEYS:
            raise NotifyConfigError(
                "plaintext secret refused in the configuration (%s.%s): name an environment "
                "variable (`token_env`, `webhook_env`) or a 0600 file "
                "(`token_file`, `webhook_file`)" % (where, key))
        if isinstance(value, dict):
            _find_secret(value, "%s.%s" % (where, key))


def check_url(url: str, what: str) -> str:
    """https, ou http sur la boucle locale seulement (comme `approve_url`)."""
    parts = urllib.parse.urlsplit(str(url or ""))
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK:
        return url
    raise NotifyConfigError("%s: https URL expected (http only on loopback)"
                            % what)


def _channel(name: str, raw) -> Channel:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise NotifyConfigError("notify.channels.%s: object expected" % name)
    _find_secret(raw, "notify.channels.%s" % name)
    kind = str(raw.get("type") or name)
    if kind not in CHANNEL_KINDS:
        raise NotifyConfigError("notify.channels.%s: unknown type %r (%s)"
                                % (name, kind, " | ".join(CHANNEL_KINDS)))
    unknown = sorted(set(raw) - set(CHANNEL_OPTIONS[kind]))
    if kind == "slack" and "url" in raw:
        raise NotifyConfigError(
            "notify.channels.%s.url: a Slack webhook URL is a secret; name the variable "
            "that holds it (`webhook_env`, default %s) or a 0600 file "
            "(`webhook_file`)" % (name, DEFAULT_SLACK_ENV))
    if unknown:
        raise NotifyConfigError("notify.channels.%s: unknown option(s) %s"
                                % (name, ", ".join(unknown)))
    options = dict(raw)
    if kind == "ntfy":
        if not raw.get("url") or not raw.get("topic"):
            raise NotifyConfigError("notify.channels.%s: `url` and `topic` are required" % name)
        check_url(str(raw["url"]), "notify.channels.%s.url" % name)
        if not _TOPIC_RE.match(str(raw["topic"])):
            raise NotifyConfigError("notify.channels.%s.topic invalid: %r"
                                    % (name, raw["topic"]))
    if kind == "nexlink":
        if not raw.get("url"):
            raise NotifyConfigError("notify.channels.%s: `url` is required" % name)
        check_url(str(raw["url"]), "notify.channels.%s.url" % name)
        parts = urllib.parse.urlsplit(str(raw["url"]))
        if parts.query or parts.fragment:
            raise NotifyConfigError(
                "notify.channels.%s.url: query or fragment not allowed (the "
                "channel appends `/mesh/alerts`)" % name)
        if not raw.get("token_env") and not raw.get("token_file"):
            raise NotifyConfigError(
                "notify.channels.%s: name the shared secret (`token_env` or "
                "`token_file`), never a value in the configuration" % name)
    for key in ("token_env", "webhook_env"):
        if key in raw and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(raw[key] or "")):
            raise NotifyConfigError("notify.channels.%s.%s: invalid variable name"
                                    % (name, key))
    for key in ("token_file", "webhook_file"):
        if key in raw:
            options[key] = os.path.abspath(os.path.expanduser(str(raw[key])))
    if "timeout" in raw:
        options["timeout"] = _positive(raw["timeout"], "notify.channels.%s.timeout" % name)
    return Channel(name=name, kind=kind, options=options)


def _positive(value, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise NotifyConfigError("%s: number expected" % what) from None
    if number < 0:
        raise NotifyConfigError("%s: positive number expected" % what)
    return number


def _names(value, what: str) -> tuple:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise NotifyConfigError("%s: list of names expected" % what)
    return tuple(value)


def parse_config(raw) -> NotifyConfig:
    """Valide la clé `notify` (dict) ; lève `NotifyConfigError` avec la raison."""
    if raw in (None, {}, ""):
        return NotifyConfig()
    if not isinstance(raw, dict):
        raise NotifyConfigError("notify: JSON object expected")
    unknown = sorted(set(raw) - set(TOP_KEYS))
    if unknown:
        raise NotifyConfigError("notify: unknown key(s) %s" % ", ".join(unknown))
    default_human = None
    if raw.get("default_human"):
        default_human = normalize_human(raw["default_human"])
        if default_human is None:
            raise NotifyConfigError("notify.default_human: `human:<id>` expected")
    types = DEFAULT_TYPES
    if "types" in raw:
        types = _names(raw["types"], "notify.types")
        bad = [t for t in types if not _TYPE_RE.match(t)]
        if bad:
            raise NotifyConfigError("notify.types: unreadable type(s) %s" % ", ".join(bad))
    channels_raw = raw.get("channels") or {}
    if not isinstance(channels_raw, dict):
        raise NotifyConfigError("notify.channels: object expected")
    channels = {name: _channel(name, spec) for name, spec in channels_raw.items()}
    routes_raw = raw.get("routes") or {}
    if not isinstance(routes_raw, dict):
        raise NotifyConfigError("notify.routes: object {\"human:<id>\": [channels]} expected")
    routes = {}
    for who, names in routes_raw.items():
        human = normalize_human(who)
        if human is None or not str(who).startswith("human:"):
            raise NotifyConfigError("notify.routes: key %r, `human:<id>` expected" % who)
        routes[human] = _names(names, "notify.routes.%s" % who)
    default = _names(raw.get("default") or [], "notify.default")
    # un canal nommé par son type (desktop, slack, app) n'a pas besoin d'être déclaré
    for names in list(routes.values()) + [default]:
        for name in names:
            if name in channels:
                continue
            if name in ("desktop", "slack", "app"):
                channels[name] = _channel(name, {})
            elif name == "ntfy":
                raise NotifyConfigError("ntfy channel used without declaration: "
                                        "notify.channels.ntfy = {\"url\": …, \"topic\": …}")
            else:
                raise NotifyConfigError("channel %r used in routes but not declared "
                                        "(notify.channels)" % name)
    # #3536 : le repli d'un canal `nexlink` est un canal DÉCLARÉ, remote push,
    # jamais lui-même (pas de chaîne de replis).
    for name, channel in channels.items():
        if channel.kind != "nexlink" or not channel.options.get("fallback"):
            continue
        fallback = str(channel.options["fallback"])
        target = channels.get(fallback)
        if target is None:
            raise NotifyConfigError(
                "notify.channels.%s.fallback: channel %r is not declared "
                "(notify.channels)" % (name, fallback))
        if target.name == channel.name:
            raise NotifyConfigError(
                "notify.channels.%s.fallback: a channel cannot fall back to itself" % name)
        if target.kind not in NEXLINK_FALLBACK_KINDS:
            raise NotifyConfigError(
                "notify.channels.%s.fallback: %r is a %s channel; a nexlink fallback "
                "must be one of %s" % (name, fallback, target.kind,
                                       ", ".join(NEXLINK_FALLBACK_KINDS)))
    # #3536 : le repli ne double pas un canal déjà routé vers le même humain
    for human, names in list(routes.items()) + [(None, default)]:
        for name in names:
            channel = channels[name]
            fallback = channel.options.get("fallback") if channel.kind == "nexlink" else None
            if fallback and fallback in names:
                raise NotifyConfigError(
                    "notify.routes.%s: %r and its nexlink fallback %r are both routed "
                    "to the same human (double send); keep only the nexlink channel "
                    "and let `fallback` cover it" % (human or "(default)", name, fallback))
    rate = int(_positive(raw.get("rate_per_minute", DEFAULT_RATE_PER_MINUTE),
                         "notify.rate_per_minute"))
    attempts = max(1, int(_positive(raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
                                    "notify.max_attempts")))
    interval = (_positive(raw["interval"], "notify.interval")
                if raw.get("interval") is not None else None)
    timeout = _positive(raw.get("timeout", DEFAULT_TIMEOUT_S), "notify.timeout")
    return NotifyConfig(default_human=default_human, types=types, routes=routes,
                        default=default, channels=channels, rate_per_minute=rate,
                        max_attempts=attempts, interval=interval, timeout=timeout or
                        DEFAULT_TIMEOUT_S)


# --------------------------------------------------------------------------
# texte (R12 : court et lisible)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    title: str
    body: str
    urgent: bool = False
    low: bool = False
    #: #3536 : identité portée jusqu'au canal `nexlink` (les autres canaux
    #: l'ignorent) ; `alert_id` est posé par `_attempt`, qui connaît l'humain.
    alert_id: str | None = None
    event: str = "raised"
    occurred_at: str | None = None
    kind: str | None = None
    context: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "%s\n%s" % (self.title, self.body)


def _only_set(values) -> frozenset | None:
    """Validate ``--only``; None means the historical full pass."""
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    selected = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if part not in ONLY_PASSES + ROLE_ONLY_PASSES:
                raise ValueError("unknown pass %r (known: %s)"
                                 % (part, ", ".join(ONLY_PASSES + ROLE_ONLY_PASSES)))
            selected.add(part)
    return frozenset(selected) if selected else None


def _skip_set(values) -> frozenset | None:
    """Validate ``--skip``; only the role-timer passes may be excluded."""
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    selected = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if part not in SKIPPABLE_PASSES:
                raise ValueError("cannot skip pass %r (skippable: %s)"
                                 % (part, ", ".join(SKIPPABLE_PASSES)))
            selected.add(part)
    return frozenset(selected) if selected else None


def _clean(text, limit: int = 300) -> str:
    """Une ligne de texte : sans caractère de contrôle, bornée."""
    text = _CONTROL_RE.sub(" ", str(text or "")).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def local_time(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def iso_utc(ts) -> str:
    """L'instant UTC du protocole Nexlink (`2026-10-25T11:41:27Z`)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _content_context(alert: dict) -> dict:
    """Champs structurés joints au texte chiffré (jamais le détail brut)."""
    context = {name: alert.get(name) for name in
               ("lot", "decision", "host", "since", "agent", "project")
               if alert.get(name) is not None}
    if alert.get("title") is not None:      # le titre du LOT, pas celui du message
        context["lot_title"] = alert.get("title")
    return context


def _identify(message: Message, alert: dict, event: str, now: float) -> Message:
    """Marque un message de l'identité que le canal `nexlink` transporte."""
    return replace(message, kind=str(alert.get("type") or "?"), event=event,
                   occurred_at=iso_utc(now), context=_content_context(alert))


def duration(seconds) -> str:
    s = int(max(0.0, float(seconds)))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min" % (s // 60)
    if s < 86400:
        return "%d h %02d" % (s // 3600, (s % 3600) // 60)
    return "%d d %d h" % (s // 86400, (s % 86400) // 3600)


def _account(alert: dict) -> str | None:
    """`harnais/compte` d'une alerte de jauge (L94, `plan_underused`), ou
    `fournisseur[/compte]` d'une alerte de solde (`balance_low`)."""
    if alert.get("provider"):
        return _clean(alert["provider"], 32) + (
            "/%s" % _clean(alert["account"], 64) if alert.get("account") else "")
    if alert.get("gauge") is None and alert.get("account") is None:
        return None
    return "%s/%s" % (_clean(alert.get("harness") or "?", 32),
                      _clean(alert.get("account") or "default", 64))


def _subject(alert: dict, vocabulary=None) -> str:
    vocabulary = vocab.active() if vocabulary is None else vocabulary
    parts = []
    if _account(alert):
        parts.append("account %s" % _account(alert))
        if alert.get("gauge"):
            parts.append("window %s" % _clean(alert["gauge"], 32))
    if alert.get("agent"):
        parts.append("agent %s" % _clean(alert["agent"], 64))
    if alert.get("decision") is not None:
        parts.append("decision #%s" % alert["decision"])
    if alert.get("lot") is not None:
        lot = vocabulary.label(alert["lot"])
        if alert.get("title"):
            lot += ' "%s"' % _clean(alert["title"], 80)
        parts.append(lot)
    if alert.get("host"):
        parts.append("host %s" % _clean(alert["host"], 64))
    return " · ".join(parts) or "mesh"


def _readable(title: str, body: str, detail_line: str | None) -> tuple:
    """R12 : même règle que les fils ; un détail illisible est retiré, pas envoyé."""
    if fil.unreadable_reason("%s\n%s" % (title, body)) and detail_line:
        body = body.replace(detail_line, "(detail removed: unreadable text)")
    return title, body


def render(alert: dict, event: str, now: float, *, raised_ts=None) -> Message:
    """Le message d'une alerte levée (`raised`) ou résolue (`resolved`)."""
    vocabulary = vocab.active()
    kind = str(alert.get("type") or "?")
    if kind == "orphan_lot" and alert.get("grouped"):
        subject = "agent %s" % _clean(alert.get("agent") or "unknown", 64)
        title = "ameesh: %sorphan %s (%s)" % (
            "resolved — " if event == "resolved" else "", vocabulary.plural, subject)
        text = "[orphan_lot] %s\n%s" % (subject, _clean(alert.get("detail"), GROUPED_DETAIL_LIMIT))
        return _identify(Message(title, text, low=event == "resolved",
                                 urgent=event != "resolved"), alert, event, now)
    label = type_label(kind, vocabulary)
    subject = _subject(alert, vocabulary)
    since = alert.get("since")
    start = float(since) if since is not None else (float(raised_ts) if raised_ts else None)
    who = alert.get("agent") or alert.get("host") or _account(alert) or (
        vocabulary.label(alert["lot"]) if alert.get("lot") is not None else "")
    suffix = " (%s)" % _clean(who, 64) if who else ""
    if event == "resolved":
        title = "ameesh: resolved — %s%s" % (label, suffix)
        lines = ["[%s] %s" % (kind, subject)]
        if start is not None:
            lines.append("resolved after %s (raised on %s)" % (duration(now - start),
                                                               local_time(start)))
        else:
            lines.append("resolved")
        return _identify(Message(title, "\n".join(lines), low=True), alert, event, now)
    title = "ameesh: %s%s" % (label, suffix)
    detail = _clean(alert.get("detail"))
    lines = ["[%s] %s" % (kind, subject)]
    if detail:
        lines.append(detail)
    if since is not None:
        lines.append("since %s (%s)" % (local_time(since), duration(now - float(since))))
    elif raised_ts:
        lines.append("observed on %s" % local_time(raised_ts))
    title, body = _readable(title, "\n".join(lines), detail)
    # L94, L106 : une alerte peut se dire urgente elle-même (`balance_low`
    # sous 12 h, `host_power_low` au seuil d'arrêt), au-delà des types
    # toujours urgents
    return _identify(Message(title, body,
                             urgent=kind in URGENT_TYPES or bool(alert.get("urgent"))),
                     alert, event, now)


def _summary_raised_counts(raised) -> dict:
    """Compte les membres levés d'un bucket (`{identité: membre}`) ; accepte
    aussi l'ancien format `{type: compte}` d'un état persisté avant #2395."""
    counts = {}
    if not isinstance(raised, dict):
        return counts
    for identity, member in raised.items():
        if isinstance(member, dict):
            kind = member.get("type") or "?"
            counts[kind] = counts.get(kind, 0) + 1
        else:
            kind = str(identity)
            counts[kind] = counts.get(kind, 0) + int(member or 0)
    return counts


def _summary_resolved_count(resolved) -> int:
    if isinstance(resolved, dict):
        return len(resolved)
    return int(resolved or 0)


def render_summary(bucket: dict, host: str, *, alert_id: str | None = None,
                   occurred_at: str | None = None) -> Message:
    raised = _summary_raised_counts(bucket.get("raised"))
    resolved = _summary_resolved_count(bucket.get("resolved"))
    total = sum(raised.values()) + resolved
    parts = []
    if raised:
        parts.append("raised: " + ", ".join(
            "%d %s" % (n, kind) for kind, n in sorted(raised.items())))
    if resolved:
        parts.append("resolved: %d" % resolved)
    title = "ameesh: %d alert(s) not detailed (rate limit)" % total
    # L94 : la sous-utilisation a sa ligne dans le résumé
    underuse = sum(n for kind, n in raised.items() if kind in sous_utilisation.UNDERUSE_TYPES)
    if underuse:
        parts.append("underuse: %d (%s)" % (underuse, ", ".join(
            "%d %s" % (n, type_label(kind)) for kind, n in sorted(raised.items())
            if kind in sous_utilisation.UNDERUSE_TYPES)))
    body = "%s\nsee `ameesh alerts` on host %s" % ("; ".join(parts) or "—", host)
    return Message(title, body, urgent=any(k in URGENT_TYPES for k in raised),
                   alert_id=alert_id, kind="summary", occurred_at=occurred_at)


def test_alert_id(human: str, channel: Channel, host: str) -> str:
    """Identité stable d'un message de test (le statut/accusé la réutilise)."""
    return nexlink_alerts.alert_id_for(
        "test|%s|%s|%s" % (human, channel.name, host))


def render_test(human: str, channel: Channel, host: str) -> Message:
    return replace(Message("ameesh: test message",
                           "Channel %s (%s) of %s, from host %s: operations alerts "
                           "will reach you here." % (channel.name, channel.kind, human,
                                                     host)),
                   alert_id=test_alert_id(human, channel, host), kind="test",
                   occurred_at=iso_utc(time.time()))


# --------------------------------------------------------------------------
# canaux
# --------------------------------------------------------------------------

def _secret_file(path: str, what: str) -> str:
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ChannelError("%s: file %s unreadable (%s)" % (what, path, exc.strerror),
                           permanent=True) from None
    if st.st_mode & 0o077:
        raise ChannelError("%s: file %s is readable by others (mode %o): "
                           "chmod 600" % (what, path, st.st_mode & 0o777), permanent=True)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ChannelError("%s: file %s is not owned by this user"
                           % (what, path), permanent=True)
    with open(path, encoding="utf-8") as fh:
        value = fh.read().strip()
    if not value:
        raise ChannelError("%s: file %s is empty" % (what, path), permanent=True)
    return value


def _secret(channel: Channel, env: dict, env_key: str, file_key: str,
            default_env: str | None, required: bool) -> str | None:
    what = "channel %s" % channel.name
    if channel.options.get(file_key):
        return _secret_file(channel.options[file_key], what)
    var = channel.options.get(env_key) or default_env
    if not var:
        return None
    value = (env.get(var) or "").strip()
    if not value and (required or channel.options.get(env_key)):
        raise ChannelError("%s: environment variable %s empty or missing" % (what, var),
                           permanent=True)
    return value or None


#: L46 : message fixe quand l'URL est un secret (webhook Slack)
SECRET_URL_INVALID = "invalid webhook URL"


def check_secret_value(value: str, what: str) -> str:
    """L46 : un secret (URL de webhook, jeton) sans espace ni caractère de
    contrôle — typiquement un commentaire en fin de ligne dans notify.env
    (`EnvironmentFile` de systemd ne les retire pas). Le message ne contient
    JAMAIS la valeur."""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ChannelError("%s invalid: whitespace or control character (end-of-line "
                           "comment in notify.env?) — value not shown" % what,
                           permanent=True)
    return value


def _scrub(text: str, secrets: tuple) -> str:
    """L46 : retire d'un message d'erreur toute trace des secrets (et, pour une
    URL, de son chemin et de sa requête, qui portent le secret d'un webhook)."""
    out = str(text or "")
    pieces: list = []
    for secret in secrets:
        if not secret:
            continue
        pieces.append(secret)
        parts = urllib.parse.urlsplit(secret) if "://" in secret else None
        if parts is not None:
            pieces += [p for p in (parts.path, parts.query,
                                   urllib.parse.quote(parts.path)) if p and len(p) > 1]
    for piece in sorted(set(pieces), key=len, reverse=True):
        out = out.replace(piece, "<secret>")
    return out


def channel_secrets(channel: Channel, env: dict | None = None) -> tuple:
    """L46 : les valeurs secrètes d'un canal (URL de webhook, jeton), lues au
    mieux, pour les retirer d'un message d'erreur inattendu. Jamais
    d'exception."""
    env = dict(os.environ if env is None else env)
    out = []
    for env_key, file_key, default in (("webhook_env", "webhook_file",
                                        DEFAULT_SLACK_ENV if channel.kind == "slack" else None),
                                       ("token_env", "token_file", None)):
        try:
            value = _secret(channel, env, env_key, file_key, default, False)
        except Exception:
            value = None
        if value:
            out.append(value)
    return tuple(out)


#: #3383: the channel kinds that reach a human from ANY host. The alerts role
#: timer pushes from whichever candidate holds the lease: `desktop` needs that
#: host's graphical session, `app` only records the alert in the event log.
REMOTE_PUSH_KINDS = ("ntfy", "slack", "nexlink")

#: #3536 : canaux admis comme repli d'un canal `nexlink` (jamais un autre
#: `nexlink` : pas de chaîne de replis, pas de récursion de couverture).
NEXLINK_FALLBACK_KINDS = ("ntfy", "slack")
#: durée du cache des clés key-wrap d'un compte (`GET /mesh/alerts/keys`).
NEXLINK_KEYS_TTL_S = 3600.0


def remote_push_problem(channel: Channel, env: dict | None = None,
                        ncfg: "NotifyConfig | None" = None) -> str:
    """'' when ``channel`` can push from this environment, else why.

    The secret is resolved exactly as `Sender` does (file or environment
    variable, then L46's value check); the reason names the variable or the
    file, never the value. #3536: a `nexlink` channel also needs the
    `cryptography` extra; when either the extra or the secret is missing but
    its configured `fallback` is a remote push whose own secret resolves
    (``ncfg`` given), the channel is still covered.
    """
    if channel.kind not in REMOTE_PUSH_KINDS:
        return "%s: %s is not a remote push" % (channel.name, channel.kind)
    env = dict(os.environ if env is None else env)
    if channel.kind == "nexlink":
        problem = ""
        if not nexlink_alerts.crypto_available():
            problem = ("channel %s: the `cryptography` module is missing "
                       "(install the `ameesh[crypto]` extra)" % channel.name)
        else:
            try:
                value = _secret(channel, env, "token_env", "token_file", None, True)
                check_secret_value(value, "channel %s: mesh secret" % channel.name)
            except ChannelError as exc:
                problem = str(exc)
        if not problem:
            return ""
        fallback = (ncfg.channels.get(str(channel.options.get("fallback") or ""))
                    if ncfg is not None else None)
        if (fallback is not None and fallback.name != channel.name
                and not remote_push_problem(fallback, env)):
            return ""
        return problem
    try:
        if channel.kind == "slack":
            value = _secret(channel, env, "webhook_env", "webhook_file",
                            DEFAULT_SLACK_ENV, True)
            check_secret_value(value, "channel %s: %s" % (channel.name, SECRET_URL_INVALID))
        else:
            value = _secret(channel, env, "token_env", "token_file", None, False)
            if value:
                check_secret_value(value, "channel %s: ntfy token" % channel.name)
    except ChannelError as exc:
        return str(exc)
    return ""


def fallback_readiness(ncfg: NotifyConfig, channel: Channel,
                       env: dict | None = None) -> dict | None:
    """#3536 : l'état du repli d'un canal `nexlink`, affiché par `--test`.

    ``None`` sans repli ; sinon ``{channel, ready[, problem]}``. La question
    posée est celle de `role timers --check` : le repli est-il un remote push
    utilisable depuis cet environnement ?
    """
    if channel.kind != "nexlink":
        return None
    name = channel.options.get("fallback")
    if not name:
        return None
    target = ncfg.channels.get(str(name))
    if target is None:
        return {"channel": str(name), "ready": False,
                "problem": "channel not declared"}
    problem = remote_push_problem(target, env)
    out = {"channel": target.name, "ready": not problem}
    if problem:
        out["problem"] = problem
    return out


def nexlink_crypto_problem(channel: Channel, env: dict | None = None,
                           ncfg: "NotifyConfig | None" = None) -> str:
    """#3619 : le manque de l'extra `crypto` d'un canal `nexlink`, ou ''.

    Le chiffrement n'est pas optionnel : sans `cryptography`, un canal routé
    ne s'active pas. Le repli `ntfy`/`slack` déclaré et utilisable le couvre
    (#3536), comme pour `role timers --check`. Le secret du canal primaire
    n'est pas regardé : ce contrôle porte sur la dépendance de l'hôte, pas
    sur une credential (#3402).
    """
    if channel.kind != "nexlink" or nexlink_alerts.crypto_available():
        return ""
    env = dict(os.environ if env is None else env)
    fallback = (ncfg.channels.get(str(channel.options.get("fallback") or ""))
                if ncfg is not None else None)
    if (fallback is not None and fallback.name != channel.name
            and not remote_push_problem(fallback, env)):
        return ""
    return ("the `cryptography` module is missing (install the "
            "`ameesh[psycopg,crypto]` extra)")


def nexlink_activation(cfg, env: dict | None = None) -> tuple[bool, list[str]]:
    """#3619 : `ameesh doctor` — les canaux `nexlink` routés de cet hôte.

    Rend ``(ok, lignes)``. Un canal routé qui ne peut pas chiffrer et que
    aucun repli utilisable ne couvre est refusé (``ok=False``) et nommé,
    **avant tout envoi** : l'activation échoue fermée, jamais par un
    `ModuleNotFoundError` au premier envoi. Un canal couvert par son repli
    reste prêt (#3536). Aucun canal `nexlink` routé : ``(True, [])``.
    """
    try:
        ncfg = parse_config(getattr(cfg, "notify", None))
    except NotifyConfigError as exc:
        return False, ["configuration refused: %s" % " ".join(str(exc).split())[:200]]
    names = set()
    for route in ncfg.routes.values():
        names.update(route)
    names.update(ncfg.default)
    ok = True
    lines = []
    for name in sorted(names):
        channel = ncfg.channels.get(str(name))
        if channel is None or channel.kind != "nexlink":
            continue
        problem = nexlink_crypto_problem(channel, env, ncfg=ncfg)
        if problem:
            ok = False
            lines.append("channel %s refused: %s" % (channel.name, problem))
        else:
            lines.append("channel %s ready" % channel.name)
    return ok, lines


def _request(url: str, data: bytes | None, headers: dict, timeout: float, *,
             method: str = "POST", show_url: bool = True,
             secrets: tuple = ()) -> tuple[int, bytes]:
    """Une requête HTTP ; rend ``(statut, corps)`` et lève `ChannelError`.

    `show_url=False` : l'URL est un secret — aucun message d'erreur ne la
    contient (L46 : message fixe pour une URL invalide, et tout texte venu
    d'une exception est nettoyé). `secrets` : autres valeurs à ne jamais
    laisser passer (jeton d'un en-tête). Le code HTTP est porté par
    `ChannelError.status` (#3536 : 409, 429).
    """
    hidden = tuple(secrets) + (() if show_url else (url,))

    def clean(text) -> str:
        return _clean(_scrub(str(text), hidden), 160)

    host = urllib.parse.urlsplit(url).hostname or ""
    handlers = [urllib.request.ProxyHandler({})] if host in _LOOPBACK else []
    opener = urllib.request.build_opener(*handlers)
    where = (" (%s)" % url) if show_url else ""
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with opener.open(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0), response.read(65536)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(4096)
            extrait = clean(payload[:300].decode("utf-8", "replace"))
        except OSError:
            payload, extrait = b"", ""
        finally:
            exc.close()
        raise ChannelError("HTTP %d%s%s" % (exc.code, where,
                                           ": %s" % extrait if extrait else ""),
                           status=int(exc.code), body=payload) from None
    except urllib.error.URLError as exc:
        raise ChannelError("unreachable%s: %s" % (where, clean(exc.reason))) from None
    except (http.client.InvalidURL, ValueError) as exc:
        # L46 : http.client.InvalidURL (espace, caractère de contrôle) cite le
        # chemin de l'URL — le secret d'un webhook : message fixe
        if not show_url:
            raise ChannelError(SECRET_URL_INVALID, permanent=True) from None
        raise ChannelError("send failed%s: %s" % (where, clean(exc))) from None
    except (OSError, http.client.HTTPException) as exc:
        raise ChannelError("send failed%s: %s" % (where, clean(exc))) from None


def _post(url: str, data: bytes, headers: dict, timeout: float, *, show_url: bool,
          secrets: tuple = ()) -> None:
    """Le POST historique (ntfy, Slack) : le corps de la réponse est ignoré."""
    _request(url, data, headers, timeout, method="POST", show_url=show_url,
             secrets=secrets)


def _header(value: str) -> str:
    """En-tête HTTP : ASCII tel quel, sinon encodage RFC 2047 (compris par ntfy)."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?%s?=" % base64.b64encode(value.encode("utf-8")).decode("ascii")


def notify_send_bin(env: dict) -> str | None:
    """L66 : le binaire `notify-send` de cet environnement (compatibilite) ;
    la couche plateforme choisit le backend selon l'OS."""
    return platform_notify.notify_send_bin(env)


class Sender:
    """Envoie un message sur un canal ; lève `ChannelError` en cas d'échec."""

    def __init__(self, env: dict | None = None, timeout: float = DEFAULT_TIMEOUT_S):
        self.env = dict(os.environ if env is None else env)
        self.timeout = timeout
        #: #3536 : clés key-wrap par URL (`GET /mesh/alerts/keys`, TTL 1 h)
        self._nexlink_keys: dict = {}
        #: #3536 : dernier envoi par canal (le statut/accusé de `--test`)
        self.last: dict = {}

    def send(self, channel: Channel, message: Message) -> None:
        timeout = float(channel.options.get("timeout") or self.timeout)
        getattr(self, "_" + channel.kind)(channel, message, timeout)

    def _desktop(self, channel: Channel, message: Message, timeout: float) -> None:
        # L66 : le backend dépend de l'OS (notify-send, osascript, toast WSL2)
        urgency = "critical" if message.urgent else ("low" if message.low else "normal")
        try:
            platform_notify.send(message.title, message.body, urgency=urgency,
                                 env=self.env, timeout=timeout)
        except platform_notify.NotifyError as exc:
            raise ChannelError(str(exc), permanent=exc.permanent) from None

    def _ntfy(self, channel: Channel, message: Message, timeout: float) -> None:
        url = "%s/%s" % (str(channel.options["url"]).rstrip("/"), channel.options["topic"])
        headers = {"Title": _header(message.title),
                   "Priority": "high" if message.urgent else ("low" if message.low
                                                             else "default"),
                   "Tags": "ameesh",
                   "Content-Type": "text/plain; charset=utf-8"}
        token = _secret(channel, self.env, "token_env", "token_file", None, False)
        if token:
            # L46 : un jeton avec espace ou retour à la ligne ferait citer
            # l'en-tête (donc le jeton) par http.client
            check_secret_value(token, "channel %s: ntfy token" % channel.name)
            headers["Authorization"] = "Bearer %s" % token
        _post(url, message.body.encode("utf-8"), headers, timeout, show_url=True,
              secrets=(token,) if token else ())

    def _slack(self, channel: Channel, message: Message, timeout: float) -> None:
        url = _secret(channel, self.env, "webhook_env", "webhook_file",
                      DEFAULT_SLACK_ENV, True)
        # L46 : refusée à la lecture, sans jamais citer l'URL (un secret)
        check_secret_value(url, "channel %s: %s" % (channel.name, SECRET_URL_INVALID))
        try:
            check_url(url, "Slack webhook")
        except NotifyConfigError as exc:
            raise ChannelError("channel %s: %s" % (channel.name, exc), permanent=True) from None
        payload = {"text": "*%s*\n%s" % (message.title, message.body)}
        _post(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
              {"Content-Type": "application/json; charset=utf-8"}, timeout,
              show_url=False)   # l'URL d'un webhook est un secret : jamais journalisée

    # -- #3536 : canal nexlink ------------------------------------------------
    def _nexlink_secret(self, channel: Channel) -> str:
        """Le secret HMAC nommé du canal ; jamais une valeur de la config."""
        secret = _secret(channel, self.env, "token_env", "token_file", None, True)
        return check_secret_value(secret, "channel %s: mesh secret" % channel.name)

    def _nexlink_key_id(self) -> str:
        """L'identité HMAC de l'émetteur (`AMEESH_NEXLINK_KEY_ID`, sinon v1)."""
        value = str(self.env.get("AMEESH_NEXLINK_KEY_ID")
                    or nexlink_alerts.MESH_ALERT_KEY_ID)
        # la valeur entre dans la chaîne signée ET dans un en-tête : pas de
        # blanc, de retour à la ligne ni de séparateur de champ
        if not re.match(r"^[A-Za-z0-9._-]{1,64}$", value):
            raise ChannelError("AMEESH_NEXLINK_KEY_ID: invalid key id",
                               permanent=True)
        return value

    def _nexlink_call(self, channel: Channel, method: str, path: str,
                      payload: dict | None, timeout: float) -> bytes:
        """Une requête signée vers l'API Nexlink ; rend le corps 2xx."""
        url = str(channel.options["url"]).rstrip("/") + path
        body = (b"" if payload is None else
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8"))
        secret = self._nexlink_secret(channel)
        headers = nexlink_alerts.mesh_headers(secret, body,
                                              key_id=self._nexlink_key_id())
        headers["Accept"] = "application/json"
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        _, raw = _request(url, body if method != "GET" else None, headers, timeout,
                          method=method, show_url=True, secrets=(secret,))
        return raw

    @staticmethod
    def _json_object(raw: bytes, what: str) -> dict:
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ChannelError("%s: invalid JSON response" % what,
                               permanent=True) from None
        if not isinstance(data, dict):
            raise ChannelError("%s: JSON object expected" % what, permanent=True)
        return data

    def _nexlink_keys_for(self, channel: Channel, timeout: float) -> dict:
        """Les clés key-wrap actives du compte owner (cache 1 h)."""
        url = str(channel.options["url"]).rstrip("/") + "/mesh/alerts/keys"
        cached = self._nexlink_keys.get(url)
        now = time.time()
        if cached and now - cached[0] < NEXLINK_KEYS_TTL_S:
            return cached[1]
        raw = self._nexlink_call(channel, "GET", "/mesh/alerts/keys", None, timeout)
        data = self._json_object(raw, "mesh alerts keys")
        account = data.get("account_id")
        try:
            nexlink_alerts.uuid_bytes(account)
        except ValueError:
            raise ChannelError("mesh alerts keys: invalid account_id",
                               permanent=True) from None
        keys = data.get("keys")
        if not isinstance(keys, list):
            raise ChannelError("mesh alerts keys: `keys` list expected",
                               permanent=True)
        active = []
        for entry in keys:
            if not isinstance(entry, dict):
                raise ChannelError("mesh alerts keys: object expected", permanent=True)
            if entry.get("active") is False:
                continue
            alg = entry.get("alg")
            if alg not in (None, nexlink_alerts.KEY_ALG):
                raise ChannelError("mesh alerts keys: unsupported alg %r" % (alg,),
                                   permanent=True)
            try:
                gen = int(entry.get("gen"))
                public = bytes.fromhex(str(entry.get("public_key") or ""))
            except (TypeError, ValueError):
                raise ChannelError("mesh alerts keys: invalid key entry",
                                   permanent=True) from None
            key_id = str(entry.get("key_id") or "")
            if not key_id or gen <= 0 or len(public) != 32:
                raise ChannelError("mesh alerts keys: invalid key entry",
                                   permanent=True)
            active.append({"key_id": key_id, "gen": gen, "public_key": public})
        data = {"account_id": str(account), "keys": active}
        self._nexlink_keys[url] = (now, data)
        return data

    def _nexlink_envelope(self, message: Message, keys: dict) -> dict:
        """L'enveloppe claire : identité + chiffré + un wrap par clé active."""
        alert_id = message_alert_id(message)
        occurred_at = message.occurred_at or iso_utc(time.time())
        # #3537 refuse tout champ inconnu (`forbidNonWhitelisted`) : le clair
        # ne porte que les champs du DTO ; l'événement reste dans le chiffré.
        envelope = {"v": nexlink_alerts.ENVELOPE_V, "alert_id": alert_id,
                    "severity": "urgent" if message.urgent else "normal",
                    "occurred_at": occurred_at}
        active = keys.get("keys") or []
        if not active:
            # content-free : aucun détail ne quitte le mesh, ce n'est pas un échec
            envelope["content_free"] = True
            return envelope
        try:
            cek = nexlink_alerts.new_cek()
            nonce, sealed = nexlink_alerts.seal_content(
                cek,
                nexlink_alerts.content_plaintext(message.title, message.body,
                                                 kind=message.kind,
                                                 event=message.event,
                                                 context=message.context),
                nexlink_alerts.content_aad(alert_id, occurred_at))
            wraps = []
            for key in active:
                wrapped = nexlink_alerts.wrap_to_x25519(
                    key["public_key"],
                    nexlink_alerts.WrapContext(keys["account_id"], key["gen"]),
                    cek)
                wraps.append({"key_id": key["key_id"], "gen": key["gen"],
                              "wrap": nexlink_alerts.b64e(wrapped)})
        except nexlink_alerts.CryptoUnavailable as exc:
            raise ChannelError(str(exc), permanent=True) from None
        except (ValueError, TypeError) as exc:
            raise ChannelError("nexlink encryption: %s" % _clean(exc, 120),
                               permanent=True) from None
        envelope.update({"ciphertext": nexlink_alerts.b64e(sealed),
                         "nonce": nexlink_alerts.b64e(nonce), "wraps": wraps})
        return envelope

    def _nexlink_post(self, channel: Channel, envelope: dict, delivery: str,
                      timeout: float) -> bytes:
        url = str(channel.options["url"]).rstrip("/") + "/mesh/alerts"
        body = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        secret = self._nexlink_secret(channel)
        headers = nexlink_alerts.mesh_headers(secret, body,
                                              key_id=self._nexlink_key_id())
        headers.update({"Accept": "application/json",
                        "Content-Type": "application/json; charset=utf-8",
                        "X-Mesh-Idempotency-Key": delivery})
        _, raw = _request(url, body, headers, timeout, method="POST", show_url=True,
                          secrets=(secret,))
        return raw

    def _nexlink(self, channel: Channel, message: Message, timeout: float) -> None:
        """Pousse une alerte dans l'app Nexlink (POST signé, contenu chiffré)."""
        alert_id = message_alert_id(message)
        if (message.event or "raised") != "raised":
            # #3537 v1 ne stocke que l'alerte levée (pas d'état de résolution
            # dans son schéma) ; la ligne de l'app reste levée, sans 400.
            self.last[channel.name] = {"alert_id": alert_id,
                                       "skipped": message.event or "raised"}
            return
        delivery = nexlink_alerts.delivery_key(message.event or "raised", alert_id)
        keys = self._nexlink_keys_for(channel, timeout)
        envelope = self._nexlink_envelope(message, keys)
        try:
            raw = self._nexlink_post(channel, envelope, delivery, timeout)
        except ChannelError as exc:
            if exc.status == 409 and b"key_unknown" in (exc.body or b""):
                # clé de wrap inconnue : on relit les clés et on réessaie une fois
                self._nexlink_keys.pop(str(channel.options["url"]).rstrip("/")
                                       + "/mesh/alerts/keys", None)
                keys = self._nexlink_keys_for(channel, timeout)
                envelope = self._nexlink_envelope(message, keys)
                try:
                    raw = self._nexlink_post(channel, envelope, delivery, timeout)
                except ChannelError as again:
                    if again.status == 409:
                        raise ChannelError("key_unknown after refreshing keys",
                                           permanent=True, status=409,
                                           body=again.body) from None
                    raise
            elif exc.status and 400 <= exc.status < 500 and exc.status not in (409, 429):
                raise ChannelError(str(exc), permanent=True, status=exc.status,
                                   body=exc.body) from None
            else:
                raise
        accepted = self._json_object(raw, "mesh alert accepted")
        if accepted.get("accepted") is False:
            raise ChannelError("mesh alert not accepted", permanent=True)
        self.last[channel.name] = {"alert_id": alert_id, "delivery": delivery,
                                   "accepted": accepted.get("accepted"),
                                   "duplicate": accepted.get("duplicate"),
                                   "id": accepted.get("id")}

    def receipt(self, channel: Channel, alert_id: str,
                timeout: float | None = None) -> dict:
        """`GET /mesh/alerts/{id}` signé : l'accusé de lecture (jamais sondé)."""
        if timeout is None:
            timeout = float(channel.options.get("timeout") or self.timeout)
        path = "/mesh/alerts/" + urllib.parse.quote(str(alert_id), safe="")
        raw = self._nexlink_call(channel, "GET", path, None, timeout)
        return self._json_object(raw, "mesh alert receipt")

    def _app(self, channel: Channel, message: Message, timeout: float) -> None:
        """L90 : le canal de l'interface embarquée. Rien à pousser : l'alerte
        est déjà dans le journal d'événements (`GET /api/v1/events`), que
        l'application suit en direct. Le canal réussit donc toujours ; il sert
        à router un humain vers son application, pas à transporter un texte."""
        return


class DrySender:
    """`--dry-run` : n'envoie rien, retient ce qui serait envoyé."""

    def __init__(self):
        self.sent: list = []

    def send(self, channel: Channel, message: Message) -> None:
        self.sent.append((channel.name, message))


# --------------------------------------------------------------------------
# acheminement
# --------------------------------------------------------------------------

class Router:
    """Le destinataire humain d'une alerte (une instance par passage : les
    lectures — registre, lots, paquets, canon — sont faites au plus une fois).

    `db` peut être None pour un passage sans la base (`Notifier.run_local_pass`,
    #532) : le registre et les lots ne sont alors pas lisibles, et la route se
    limite au champ `responsible` de l'alerte, à la fiche d'hôte du canon et au
    destinataire par défaut."""

    def __init__(self, cfg: Config, db, ncfg: NotifyConfig):
        self.cfg, self.db, self.ncfg = cfg, db, ncfg
        self._agents = None
        self._canon = None
        self._canon_read = False

    def agents(self) -> dict:
        if self._agents is None:
            self._agents = {} if self.db is None else {
                row["name"]: row for row in storage.of(self.db).operations.listing()}
        return self._agents

    def agent_responsible(self, name) -> str | None:
        if not name:
            return None
        return (self.agents().get(name) or {}).get("responsible")

    def lot_responsible(self, lot) -> str | None:
        if lot is None or self.db is None:
            return None
        try:
            item = storage.of(self.db).work.get(int(lot))
        except (TypeError, ValueError):
            return None
        packages = storage.of(self.db).packages
        ident = (item or {}).get("package_id") or (item or {}).get("package_parent")
        seen = set()
        while ident and ident not in seen and len(seen) < 16:
            seen.add(ident)
            package = packages.get(ident) or {}
            if normalize_human(package.get("responsible")):
                return package["responsible"]
            ident = package.get("parent")
        return None

    def host_responsible(self, host) -> str | None:
        if not host or not self.cfg.canon:
            return None
        from . import canon as canon_mod
        if not self._canon_read:
            self._canon_read = True
            # L43 (0031) : la fiche Host du premier canon configuré qui décrit
            # l'hôte (le canon par défaut d'abord)
            self._canon = canon_mod.load_configured(self.cfg) or None
        fiche = canon_mod.find_host(self._canon or [], host)
        return fiche.responsible if fiche is not None else None

    def resolve(self, alert: dict) -> tuple:
        """(humain, source) ; source ∈ alerte, agent, lot, hote, defaut ; (None, None).

        #2395 (canon §9, owner 18/10) : un `slo_breach` n'est **jamais** poussé
        à l'owner (`notify.default_human`), même si son type est configuré ou
        si une cible `owner` traîne dans l'alerte. Ses destinataires sont les
        rôles de `targets`, par courrier d'agent ; seul un `responsible`
        explicitement nommé peut désigner un autre humain."""
        if forces_owner(alert):
            # #789 t2 (canon §9) : une alerte qui CIBLE le propriétaire
            # escalade au propriétaire — `notify.default_human`. La cible prime
            # le `responsible` de l'agent, qui peut être un pilote.
            human = normalize_human(self.ncfg.default_human)
            if human:
                return human, "defaut"
        if alert.get("type") == "module_deviation" and alert.get("project"):
            # A project procedure finding belongs to the lot, even when its
            # assignee (or their host/responsible human) belongs elsewhere.
            # A host-scoped finding (`dev` workspaces control, no project)
            # takes the generic route below: the responsible of its agent,
            # then of its host, then the default human.
            for source, value in (("lot", self.lot_responsible(alert.get("lot"))),
                                  ("defaut", self.ncfg.default_human)):
                human = normalize_human(value)
                if human:
                    return human, source
            return None, None
        agent = alert.get("agent")
        steps = (
            ("alerte", lambda: alert.get("responsible")),
            ("agent", lambda: self.agent_responsible(agent)),
            ("lot", lambda: self.lot_responsible(alert.get("lot"))),
            ("hote", lambda: self.host_responsible(
                alert.get("host") or (self.agents().get(agent) or {}).get("host")
                if agent else alert.get("host"))),
            ("defaut", lambda: self.ncfg.default_human),
        )
        for source, value in steps:
            human = normalize_human(value())
            if human:
                if (alert.get("type") == "slo_breach"
                        and human == normalize_human(self.ncfg.default_human)):
                    # #2395 : même explicitement configuré, un dépassement de
                    # délai ne part pas sur le bureau de l'owner.
                    return None, None
                return human, source
        return None, None


# --------------------------------------------------------------------------
# état persistant
# --------------------------------------------------------------------------

def state_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "notify", "state.json")


def empty_state() -> dict:
    return {"schema": STATE_SCHEMA, "active": {}, "resolved": {}, "rate": {},
            "summary": {}, "last_summary": {}, "briefs": {}, "notified": {},
            #: #3536 : replis nexlink déjà envoyés (une fois par alert_id)
            "fallback": {},
            #: #3761 : verdicts de porte des avis, relus seulement quand le
            #: gel, la cible, les jalons ou le canon changent
            "gates": {}}


#: #3576 : le document d'envoi et son bail d'exécution sont partagés en base
#: (`notify_state`), pour que la passe `alerts` change d'hôte sans re-pousser.
#: #3913 : le bail ne vaut plus 10 min. Il est RENOUVELÉ pendant la passe
#: (`_StateRenewal`) : une passe tuée (redémarrage de la base, SIGKILL, timeout
#: de l'unité) ne laisse donc pas un bail orphelin plus longtemps que cette
#: durée. Avec la cadence de la minuterie de rôle (30 s), la reprise arrive en
#: ~2 min, très en dessous de `notify_gap.ALIVE_S` (600 s) : un incident de
#: cette forme ne lève plus `notify_undeliverable`, et le témoin de #3007 n'est
#: plus confondu avec une panne de 10 min. Le plafond du renouvellement est le
#: `TimeoutStartSec=10min` de l'unité de rôle (`role_timers.render_service`) :
#: la passe s'arrête d'elle-même avant.
STATE_SCOPE = "alerts"
STATE_LEASE_S = 90.0
#: intervalle de renouvellement : trois occasions avant l'expiration
STATE_RENEW_S = STATE_LEASE_S / 3.0
#: attente maximale du fil de renouvellement au rendu du bail
STATE_RENEW_STOP_S = 2.0


def _state_from_document(data) -> dict | None:
    """Valide un document `ameesh-notify-state/1` ; None s'il n'en est pas un."""
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        return None
    state = empty_state()
    state.update({k: v for k, v in data.items() if k in state and isinstance(v, dict)})
    return state


def load_state(path: str, log=None, *, move_aside: bool = True) -> dict:
    """L'état d'envoi ; absent = vide. Illisible : mis de côté (journalisé),
    on repart vide — au pire une alerte en cours est renvoyée une fois.
    `move_aside=False` (`--dry-run`) : rien n'est déplacé."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        state = _state_from_document(data)
        if state is None:
            raise ValueError("unexpected schema")
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as exc:
        aside = "%s.illisible-%d" % (path, int(time.time()))
        try:
            if not move_aside:
                raise OSError
            os.replace(path, aside)
        except OSError:
            aside = "(not moved)"
        if log:
            log("unreadable delivery state (%s): set aside as %s, starting from an empty "
                "state" % (exc, aside))
        return empty_state()
    return state


def save_state(path: str, state: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


#: Un jeton par processus : un PID recyclé après un redémarrage ne reprend
#: jamais le bail de son incarnation précédente (l'epoch, lui, ne recule pas).
_STATE_RUN = os.urandom(4).hex()


def state_owner(cfg: Config) -> str:
    """Identité du processus sur le bail d'état : hôte, PID et jeton de run.

    Une relève change d'hôte, donc d'owner ; le jeton empêche un PID recyclé
    de se faire passer pour l'ancien détenteur.
    """
    host = str(getattr(cfg, "host", None) or "?").strip() or "?"
    return "%s:%d:%s" % (host, os.getpid(), _STATE_RUN)


def load_shared_state(db) -> dict | None:
    """#3576 : le document partagé de la passe alerts, None tant qu'il n'est
    pas amorcé (premier détenteur depuis la migration)."""
    return _state_from_document(storage.of(db).notify_liveness.read_state(STATE_SCOPE))


def save_shared_state(db, state: dict, *, owner: str, epoch: int) -> bool:
    """Écrit le document sous le bail vivant `(owner, epoch)` ; False s'il a
    été perdu ou a expiré (le détenteur ne doit alors plus livrer)."""
    return storage.of(db).notify_liveness.save_state(
        STATE_SCOPE, state, owner=owner, epoch=epoch)


def _local_entry_owned(entry, host) -> bool:
    """Une entrée de console appartient-elle à cet hôte ?

    Les `LOCAL_TYPES` naissent de la console d'UN hôte (`pgbouncer_local`
    pose `alert.host`) : le document partagé en porte plusieurs, et un hôte
    ne doit ni résoudre ni fusionner celle d'un autre (#3576, relecture cx1).
    """
    alert = (entry or {}).get("alert") or {}
    return (alert.get("type") in LOCAL_TYPES
            and str(alert.get("host") or "") == str(host or ""))


def _summary_key_host(key) -> str | None:
    """L'hôte d'une clé d'alerte (`key_of` est une liste JSON), ou None."""
    try:
        parsed = json.loads(key)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) < 3:
        return None
    return str(parsed[2] or "")


def _merge_local_state(local: dict, shared: dict, host, *,
                       removed=()) -> None:
    """Reporte dans l'état partagé ce que seul le passage local a produit.

    `run_local_pass` tourne avant la base (#532) : ses alertes de console
    (`LOCAL_TYPES`) et son registre ne peuvent pas venir du document partagé.
    Seules les entrées de CET hôte traversent : une entrée de console d'un
    autre hôte reste à lui. Une résolution locale retire la clé de `active` du
    document partagé, sinon elle y reste et le passage suivant la re-résout à
    chaque tick (relecture cx1). `removed` porte les clés que le passage local
    a retirées même quand `_resolved` n'écrit rien (acknowledged, notifiee,
    sans_canal, type retiré) : sans elles, la clé resterait active dans le
    partagé et une ligne resolved repartirait à chaque tick (relecture dev4).
    Tout le reste est remplacé par l'état partagé — c'est la règle du
    dédoublonnage entre hôtes.
    """
    owned_keys = set()
    for key, entry in (local.get("active") or {}).items():
        if _local_entry_owned(entry, host):
            shared["active"][key] = entry
            owned_keys.add(key)
    for key, entry in (local.get("resolved") or {}).items():
        if _local_entry_owned(entry, host):
            shared["active"].pop(key, None)
            shared["resolved"][key] = entry
            owned_keys.add(key)
    for identity, sent_ts in (local.get("notified") or {}).items():
        shared["notified"][identity] = max(
            float(sent_ts), float(shared["notified"].get(identity, 0.0)))
    for human, stamps in (local.get("rate") or {}).items():
        known = {float(stamp) for stamp in shared["rate"].get(human) or ()}
        shared["rate"][human] = list(shared["rate"].get(human) or ()) + [
            stamp for stamp in stamps if float(stamp) not in known]
    for human, bucket in (local.get("summary") or {}).items():
        if not isinstance(bucket, dict):
            continue
        for event in ("raised", "resolved"):
            members = bucket.get(event)
            if not isinstance(members, dict):
                continue
            for identity, member in members.items():
                if not isinstance(member, dict) or member.get("type") not in LOCAL_TYPES:
                    continue
                if member.get("key") not in owned_keys \
                        and _summary_key_host(member.get("key")) != str(host or ""):
                    continue
                target = shared["summary"].get(human)
                if not isinstance(target, dict):
                    target = shared["summary"][human] = {}
                target.setdefault(event, {})[identity] = member
    for key in removed or ():
        shared["active"].pop(key, None)


class _StateRenewal:
    """Renouvelle le bail d'état partagé pendant une passe qui dure (#3913).

    Une connexion à part : la passe tient la sienne, et une transaction longue
    du passage ne retarde pas le renouvellement. Un échec réseau n'arrête pas
    le fil (la base peut revenir avant l'expiration) ; un refus du bail
    (`False`) le marque perdu. Une **échéance monotone locale**, fixée à la
    prise et prolongée seulement par un renouvellement réussi, dit au passage
    qu'il ne doit plus rien pousser même quand la base ne répond plus
    (`expired`, lu par `Notifier._delivery_open`) : un fil bloqué ou une base
    injoignable ne laisse pas livrer au-delà de la TTL. La borne initiale est
    ancrée **avant** l'appel d'acquisition (`acquired_at`), pas après son
    retour : un aller-retour lent ou une pause du processus entre la prise
    serveur et la construction ne peut pas prolonger le droit local au-delà de
    la TTL du serveur (relecture cx1).
    """

    def __init__(self, cfg: Config, owner: str, epoch: int, *,
                 ttl: float, interval: float, log=None,
                 acquired_at: float | None = None):
        self.cfg, self.owner, self.epoch = cfg, owner, int(epoch)
        self.ttl, self.interval = float(ttl), max(0.01, float(interval))
        self.log = log or (lambda text: None)
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False
        #: échéance LOCALE : ancrée avant l'acquisition, avancée ensuite par
        #: chaque renouvellement réussi seulement. Un appelant sans ancre
        #: (tests unitaires) part de l'instant courant.
        anchor = time.monotonic() if acquired_at is None else float(acquired_at)
        self._deadline = anchor + self.ttl

    def expired(self) -> bool:
        """Le bail ne couvre plus cette passe (perdu, ou échéance locale) ?"""
        return self.lost or time.monotonic() >= self._deadline

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="notify-state-lease")
        self._thread.start()

    def _run(self) -> None:
        conn = None
        try:
            while not self._stop.wait(self.interval):
                try:
                    if conn is None:
                        conn = db_mod.connect(self.cfg)
                    # L'échéance locale démarre AVANT l'appel : l'heure de la
                    # base est postérieure au départ, donc la marge est du bon
                    # côté (on ne dépasse jamais le bail du serveur).
                    started = time.monotonic()
                    ok = storage.of(conn).notify_liveness.renew_state(
                        STATE_SCOPE, self.owner, self.epoch, self.ttl)
                except Exception as exc:  # noqa: BLE001 — le fil ne tombe pas
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                    if not self._warned:
                        self._warned = True
                        self.log("shared delivery state lease not renewed (%s): "
                                 "retrying" % exc)
                    continue
                self._warned = False
                if not ok:
                    self.lost = True
                    return
                self._deadline = started + self.ttl
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=STATE_RENEW_STOP_S)


class StateLease:
    """Le bail d'état partagé et son renouvellement (#3913).

    Il se lit comme l'ancien couple ``(owner, epoch)`` : ``lease[0]``,
    ``lease[1]``, l'itération et l'unpacking restent ceux d'avant.
    """

    __slots__ = ("owner", "epoch", "renewal")

    def __init__(self, owner: str, epoch: int, renewal=None):
        self.owner, self.epoch, self.renewal = str(owner), int(epoch), renewal

    def __iter__(self):
        return iter((self.owner, self.epoch))

    def __getitem__(self, index: int):
        return (self.owner, self.epoch)[index]

    def expired(self) -> bool:
        """Le bail ne couvre plus cette passe (#3913 B1)."""
        return self.renewal is not None and self.renewal.expired()

    def stop(self) -> None:
        if self.renewal is not None:
            self.renewal.stop()


def _pid_alive(pid: int) -> bool:
    """Ce PID existe-t-il sur CET hôte ? (0 et négatifs : jamais)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return True
    return True


def reclaim_dead_local_state(db, cfg: Config, *, log=None) -> dict | None:
    """Rend un bail d'état laissé par un processus MORT de cet hôte (#3913).

    L'owner porte ``hôte:PID:jeton`` (`state_owner`) : un autre hôte ne peut
    pas juger ce PID, mais le même hôte le voit dans son espace de PIDs. Un
    PID recyclé par un processus vivant ne fait qu'attendre la TTL (jamais un
    vol), et le jeton de run empêche un nouveau processus de reprendre le bail
    d'une incarnation précédente. Le rendu est un CAS sur ``(owner, epoch)`` :
    si le bail a bougé, rien n'est rendu.
    """
    try:
        row = storage.of(db).notify_liveness.state_lease(STATE_SCOPE)
    except Exception:  # noqa: BLE001 — pas de lecture, pas de reprise
        return None
    if not row or not row.get("owner"):
        return None
    owner = str(row["owner"])
    host, _, rest = owner.partition(":")
    pid_text, _, _token = rest.partition(":")
    if host != str(getattr(cfg, "host", "") or ""):
        return None
    try:
        pid = int(pid_text)
    except (TypeError, ValueError):
        return None
    if _pid_alive(pid):
        return None
    try:
        released = storage.of(db).notify_liveness.release_state(
            STATE_SCOPE, owner, int(row["epoch"]))
    except (db_mod.DbError, TypeError, ValueError):
        return None
    if not released:
        return None
    if log:
        log("shared delivery state lease taken back from a dead local process "
            "(%s)" % owner)
    return row


def release_shared_state(db, lease, *, cfg: Config | None = None,
                         log=None) -> bool:
    """Rend le bail, au mieux, même si la connexion de la passe est morte.

    #3913 : le rendu de la boucle `main` passait par la connexion qui venait
    de tomber (redémarrage de la base) ; l'échec était avalé et le bail restait
    orphelin jusqu'à son expiration. Une connexion NEUVE, ouverte seulement
    pour ce geste, rend le bail tout de suite quand la base est revenue.
    """
    if lease is None:
        return True
    stop = getattr(lease, "stop", None)
    if stop is not None:
        stop()
    owner, epoch = lease
    first: Exception | None = None
    try:
        return storage.of(db).notify_liveness.release_state(
            STATE_SCOPE, owner, epoch)
    except Exception as exc:  # noqa: BLE001 — la connexion de la passe est morte
        first = exc
    if cfg is None:
        return False
    fresh = None
    try:
        fresh = db_mod.connect(cfg)
        released = storage.of(fresh).notify_liveness.release_state(
            STATE_SCOPE, owner, epoch)
    except Exception:  # noqa: BLE001 — la base peut être encore en redémarrage
        return False
    finally:
        if fresh is not None:
            try:
                fresh.close()
            except Exception:
                pass
    if released and log:
        log("shared delivery state lease released on a new connection (%s)"
            % first)
    return released


def enter_shared_state(db, notifier, cfg: Config, *, dry_run: bool = False,
                       log=None, lease_s: float | None = None,
                       renew_s: float | None = None
                       ) -> tuple[StateLease | None, bool]:
    """Prend le bail d'état partagé et lit le document (rendu ``(bail,
    livre)``).

    ``livre`` est faux quand un autre hôte détient le bail : ce passage se
    met en veille, sans livrer ni écrire (l'autre hôte le fait). En
    ``--dry-run``, le document est lu sans bail et rien n'est écrit.
    ``lease_s``/``renew_s`` ne servent qu'aux tests de renouvellement.
    """
    log = log or notifier.log
    ttl = STATE_LEASE_S if lease_s is None else float(lease_s)
    interval = STATE_RENEW_S if renew_s is None else float(renew_s)
    #: aucun bail périmé d'un appel antérieur ne doit autoriser la suite
    notifier.lease = None
    if dry_run:
        shared = load_shared_state(db)
        if shared is not None:
            _merge_local_state(notifier.state, shared, getattr(cfg, "host", None),
                               removed=notifier.local_resolved_keys)
            notifier.state = shared
            notifier.local_resolved_keys.clear()
        return None, True
    owner = state_owner(cfg)
    # #3913 (relecture cx1) : l'ancre précède l'appel, pas son retour. La TTL
    # serveur commence DANS `acquire_state` ; un retour retardé ou une pause du
    # processus ne doit pas repousser l'échéance locale au-delà.
    started = time.monotonic()
    try:
        epoch = storage.of(db).notify_liveness.acquire_state(
            STATE_SCOPE, owner, ttl)
    except db_mod.DbError as exc:
        if getattr(exc, "sqlstate", None) != "42P01":
            raise
        # The migration is applied before the runners (§14), but the owner
        # channel must not fall silent on a host updated ahead of the base:
        # deliver from the local document, loudly, until `ameesh migrate`.
        log("shared delivery state table missing (migration 0487 not applied): "
            "delivering from the local state, duplicates possible until it is")
        return None, True
    if epoch is None:
        # #3913 : une passe tuée laisse `hôte:PID:jeton` ; le MÊME hôte sait
        # que ce PID n'existe plus et reprend le bail sans attendre la TTL.
        if reclaim_dead_local_state(db, cfg, log=log) is not None:
            # nouvelle tentative d'acquisition = nouvelle ancre, avant l'appel
            started = time.monotonic()
            epoch = storage.of(db).notify_liveness.acquire_state(
                STATE_SCOPE, owner, ttl)
    if epoch is None:
        log("another host holds the alerts delivery state (shared lease): "
            "standing by, nothing is delivered")
        return None, False
    lease = StateLease(owner, epoch, _StateRenewal(
        cfg, owner, epoch, ttl=ttl, interval=interval, log=log,
        acquired_at=started))
    # #3913 B1 : la passe consulte ce bail avant chaque envoi ; le retirer au
    # rendu ou sur toute sortie en erreur est aussi important que le poser.
    notifier.lease = lease
    notifier._lease_warned = False
    # #3913 : le renouvellement commence AVANT la lecture/le calcul, qui peut
    # durer : sans lui, un bail de 90 s expirerait au milieu d'une passe lente.
    lease.renewal.start()
    try:
        shared = load_shared_state(db)
        if shared is None:
            # Premier détenteur depuis la migration : son état local amorce le
            # document partagé (les autres hôtes liront celui-ci). Le passage
            # local a déjà retiré ses clés résolues de `active` ; aucun
            # tombstone n'est donc nécessaire dans le document amorcé.
            save_shared_state(db, notifier.state, owner=owner, epoch=epoch)
            notifier.local_resolved_keys.clear()
        else:
            _merge_local_state(notifier.state, shared, getattr(cfg, "host", None),
                               removed=notifier.local_resolved_keys)
            notifier.state = shared
            notifier.local_resolved_keys.clear()
    except Exception:
        # Ne pas garder un bail qu'aucune passe n'utilisera (base tombée
        # entre la prise et la lecture) : le prochain essai doit pouvoir le
        # prendre sans attendre son expiration.
        notifier.lease = None
        release_shared_state(db, lease, cfg=cfg)
        raise
    return lease, True


def leave_shared_state(db, notifier, lease, *, log=None,
                       cfg: Config | None = None) -> bool:
    """Écrit le document partagé sous le bail, puis le rend.

    False si l'écriture a été refusée (bail perdu ou expiré) : la livraison a
    eu lieu, l'état n'est plus celui du détenteur courant.
    """
    if lease is None:
        return True
    owner, epoch = lease
    log = log or notifier.log
    saved = False
    try:
        expired = getattr(lease, "expired", None)
        if expired is not None and expired():
            # #3913 B1 : ne pas écrire un document sous un bail mort — et le
            # dire, plutôt que de laisser `save_state` refuser à la fin.
            log("alerts delivery state NOT saved: the shared lease was lost or "
                "expired; the pass stopped before any new send")
            return False
        saved = save_shared_state(db, notifier.state, owner=owner, epoch=epoch)
        if not saved:
            log("alerts delivery state NOT saved: the shared lease was lost or "
                "expired; the next holder may re-push")
        return saved
    finally:
        notifier.lease = None
        release_shared_state(db, lease, cfg=cfg)


def key_of(alert: dict) -> str:
    """Clé de dédoublonnage : celle d'`ameesh alerts --follow`, en texte."""
    key = list(exploitation.alert_key(alert))
    if alert.get("type") == "orphan_lot" and alert.get("grouped"):
        key.append(alert.get("responsible"))
    return json.dumps(key, ensure_ascii=False)


#: #3236 : les alertes de jauge datent leur fenêtre de `resets_at - window_s`.
#: Le fournisseur fait giguer `resets_at` d'une seconde entre deux relevés ;
#: `key_of` arrondit à la seconde et cette gigue créait une alerte neuve,
#: donc un brief neuf pour la même fenêtre. Le brief arrondit cette date à la
#: minute : une fenêtre de forfait dure des heures, la minute suffit à la
#: reconnaître, et deux fenêtres voisines ne se confondent pas.
BRIEF_WINDOW_BUCKET_S = 60.0


def brief_key_of(alert: dict) -> str:
    """Identité d'un brief d'agent : une alerte et une fenêtre (#3236).

    Les autres alertes gardent `key_of`. Pour une alerte de jauge
    (`plan_underused` porte `gauge` et `since`), le début de fenêtre est
    arrondi à la minute et `reason` n'entre pas dans la clé : `fin_de_fenetre`
    et `sous_rythme` décrivent la même fenêtre et appellent le même geste.
    Une gigue d'une seconde de `resets_at`, un clignotement de la raison ou
    une disparition d'une passe ne refont donc pas briefer la même fenêtre."""
    since = alert.get("since")
    if alert.get("gauge") is None or since is None:
        return key_of(alert)
    try:
        bucket = round(float(since) / BRIEF_WINDOW_BUCKET_S)
    except (TypeError, ValueError):
        return key_of(alert)
    return key_of(dict(alert, since=bucket, reason=None))


def _notification_id(key, severity, human, since=None) -> str:
    """Identité du registre `notified` : clé, sévérité, humain, `since`. Les
    résumés de débit s'en servent aussi pour leurs membres (#2395 B2), d'où la
    fonction partagée : les deux chemins doivent produire exactement la même
    clé. Le `since` y entre car certaines clés n'en ont pas (`ci_runner_stuck`,
    #845) : un nouvel épisode reste une nouvelle notification."""
    return json.dumps([key, severity, human, since], ensure_ascii=False)


def message_alert_id(message: Message) -> str:
    """L'identité stable d'un message pour le canal `nexlink`.

    Posée par `_attempt` (identité du registre `notified` en UUIDv5) et par
    `_summaries` (identité de l'épisode de bucket) ; un message construit
    sans elle (test ad hoc) en reçoit une dérivée de son texte, stable entre
    deux essais et valide comme UUID pour l'API #3537.
    """
    if message.alert_id:
        return message.alert_id
    return nexlink_alerts.alert_id_for(
        "%s\n%s" % (message.title, message.body))


def _since_of(alert: dict):
    """`since` de l'épisode, arrondi comme dans `exploitation.alert_key` ; None
    si l'alerte n'en a pas (elle n'entre alors pas au registre `notified`)."""
    since = (alert or {}).get("since")
    try:
        return round(float(since)) if since is not None else None
    except (TypeError, ValueError):
        return None


def _entry_severity(entry: dict):
    """Sévérité d'une entrée d'état ; les états antérieurs au lot ne la
    persistaient pas : on la recalcule comme le fait le prochain relevé."""
    severity = entry.get("severity")
    if severity is not None:
        return severity
    alert = entry.get("alert")
    if not isinstance(alert, dict):
        return None
    from . import alert_ack
    return alert_ack.severity(alert)


def _held_dead_runner(alert: dict, now: float) -> bool:
    """#3630 : l'alerte `dead_runner` est encore dans sa grâce d'anti-rebond.

    La grâce se mesure depuis l'échéance du bail (`since`), comme la
    détection : une coupure courte (60 s) reprise avant l'échéance de la
    grâce ne pousse rien. Une alerte sans `since` n'est jamais retenue — on
    ne tait pas une alerte incomplète —, ni un âge illisible.
    """
    if alert.get("type") != "dead_runner":
        return False
    since = alert.get("since")
    if since is None:
        return False
    try:
        return float(now) - float(since) < DEAD_RUNNER_NOTIFY_GRACE_S
    except (TypeError, ValueError):
        return False


def forces_owner(alert: dict) -> bool:
    """#789 t2, restreint par #2395 : une cible `owner` force la livraison à
    l'humain même hors `notify.types`. Un `slo_breach` n'en bénéficie jamais :
    aucun dépassement de délai n'est notifié à l'owner (canon §9, décision
    owner 18/10)."""
    return (alert.get("type") != "slo_breach"
            and "owner" in (alert.get("targets") or ()))


GROUPED_LOTS_SHOWN = 5
GROUPED_DETAIL_LIMIT = 600


def group_orphan_lots(alerts: list, resolve) -> list:
    """Group orphan notifications by agent, reason and resolved human.

    Resolve each lot before grouping so different humans keep separate alerts.
    Omit lot, host and since from the group identity: changing its membership
    must not resend a notification while the orphaned-agent situation persists.
    The original alerts remain available to automatic assignment and briefs.
    """
    out, groups = [], {}
    vocabulary = vocab.active()
    for alert in alerts:
        if alert.get("type") != "orphan_lot":
            out.append(alert)
            continue
        human, _ = resolve(alert)
        groups.setdefault((alert.get("agent"), alert.get("reason"), human), []).append(alert)
    for (agent, reason, human), lots in groups.items():
        ordered = sorted(lots, key=lambda alert: int(alert["lot"]))
        names = ", ".join("#%s%s" % (
            alert["lot"], ' "%s"' % _clean(alert.get("title"), 60) if alert.get("title") else "")
            for alert in ordered[:GROUPED_LOTS_SHOWN])
        more = (" and %d more" % (len(lots) - GROUPED_LOTS_SHOWN)
                if len(lots) > GROUPED_LOTS_SHOWN else "")
        grouped = {"type": "orphan_lot", "agent": agent, "reason": reason,
                   "responsible": human, "grouped": True, "value": len(lots),
                   "detail": "%d orphan %s (%s): %s%s" % (
                       len(lots), vocabulary.abbreviation,
                       _clean(reason or "unknown", 64), names, more)}
        out.append(grouped)
    return out


#: champs d'une alerte gardés dans l'état (de quoi écrire la résolution)
_KEPT = ("module", "rule", "project", "declaration_id", "subject", "action",
         "type", "agent", "lot", "title", "host", "since", "detail", "reason", "value",
         "responsible", "stop_reason", "assignee", "harness", "account", "gauge",
         "provider", "currency", "urgent", "severity", "level",
         # L96 : `engagement_overdue` (la clé de dédoublonnage les lit)
         "commitment", "package", "due",
         # L124 : `decision_pending`, `decision_overdue`
         "decision", "grouped",
         # #789 t2 (canon §9) : un P1 garde son niveau et ses cibles dans
         # l'état — la livraison au propriétaire et la résolution les relisent
         "critical", "targets",
         # #1901 : l'hôte qui a observé l'alerte (porte d'hôte) reste dans
         # l'état — une famille non relue ne résout pas ses alertes
         "observation_host",
         # #3502 : l'épisode de biais (sens, palier, entrée) est lisible dans
         # l'état ; la clé garde `since` et `reason`, pas la publication.
         "bias_side", "bias_level", "tier_since", "biased_since")


# --------------------------------------------------------------------------
# le service
# --------------------------------------------------------------------------

def _stderr_log(text: str) -> None:
    print("ameesh notify: %s" % text, file=sys.stderr, flush=True)


class Notifier:
    """Un passage = lire les alertes, envoyer les nouvelles et les résolutions,
    retenter les canaux en échec, envoyer les résumés de débit."""

    def __init__(self, cfg: Config, ncfg: NotifyConfig, state: dict | None = None, *,
                 sender=None, dry_run: bool = False, emit=None, log=None, clock=None,
                 take: dict | None = None, handoff: dict | None = None,
                 wip: dict | None = None, only=None, skip=None):
        self.cfg, self.ncfg = cfg, ncfg
        #: L165 (EP5) : passages demandés par une minuterie de rôle ; None =
        #: le passage complet historique.
        self.only = _only_set(only)
        #: #3327 : passes mécaniques retirées du passage complet (le service
        #: complet garde alertes, take, budget handoff et wip sweep).
        self.skip = _skip_set(skip)
        #: L119 : réglages de la prise automatique (`backlog.auto_take`) ; None la coupe
        self.take = take
        #: L233b : réglages de la reprise sur pause budget
        #: (`handoff.budget_handoff`) ; None la coupe
        self.handoff = handoff
        #: §4 : réglages du balayage de la limite d'en-cours (`wip.sweep`) ;
        #: None le coupe (défaut : aucun balayage du mesh actif à ce stade)
        self.wip = wip
        self.state = state if state is not None else empty_state()
        #: #3576 (relecture dev4) : clés de console retirées de `active` par le
        #: passage local, à retirer du document partagé à la fusion suivante.
        #: En mémoire seulement : une console calmée sans envoi ne laisse
        #: aucune entrée `resolved`, et la clé resterait active dans le partagé.
        self.local_resolved_keys: set = set()
        self.dry_run = dry_run
        self.sender = sender or (DrySender() if dry_run else Sender(timeout=ncfg.timeout))
        #: #3913 B1 : le bail d'état partagé de CETTE passe (posé par
        #: `enter_shared_state`, retiré au rendu). None hors d'une passe
        #: d'alertes ou en `--dry-run`. `_delivery_open` le consulte avant
        #: chaque envoi : un détenteur périmé ne pousse plus rien.
        self.lease = None
        self._lease_warned = False
        #: les enregistrements du dernier passage (remplacé par `run_pass`)
        self._records: list = []
        self.emit = emit or (lambda record: None)
        self.log = log or _stderr_log
        self.clock = clock or time.time
        #: L126 : projection des lots en issues GitHub (créée au premier passage)
        self._projector = None
        self._github_watcher = None
        #: L161c : relevé de la CI, échecs au courrier de l'agent du lot
        self._ci_watcher = None

    # -- passage --------------------------------------------------------------
    def _delivery_open(self) -> bool:
        """Le bail partagé autorise-t-il encore des effets ? (#3913 B1)

        Un détenteur qui a perdu son bail pendant une passe longue ne doit
        plus pousser ni déposer de courrier : le refus de `save_state` à la fin
        ne répare pas un envoi déjà parti. Le bail local porte une échéance
        monotone, prolongée seulement par un renouvellement réussi ; une base
        injoignable ou un fil de renouvellement bloqué ne laisse donc pas
        livrer au-delà de la TTL. Sans bail (passage direct, `--dry-run`,
        `run_local_pass`), la passe est autorisée : le bail ne ferme que la
        livraison partagée.
        """
        lease = self.lease
        if lease is None:
            return True
        expired = getattr(lease, "expired", None)
        if expired is None or not expired():
            return True
        if not self._lease_warned:
            self._lease_warned = True
            self.log("alerts delivery lease lost: pass stopped before any new send")
        return False

    def _tail_without_alerts(self, db, now: float, selected):
        """Les passes mécaniques gardent leur tour quand le bail est perdu."""
        rest = (frozenset(selected) if selected is not None
                else frozenset(ONLY_PASSES) - set(self.skip or ()))
        rest = rest - {"alerts"}
        return self._run_tail(db, now, rest) if rest else []

    def run_pass(self, db, current: list | None = None, thresholds: dict | None = None,
                 only=None) -> list:
        from . import alert_ack
        from .mediated_executor import availability
        selected = self.only if only is None else _only_set(only)
        if selected is None and self.skip:
            selected = frozenset(ONLY_PASSES) - self.skip
        now = float(self.clock())
        # #3057 : la passe restreinte d'une minuterie de rôle ne livre aucune
        # alerte humaine (`_run_tail`). Elle rend donc AVANT le battement de
        # #3007 : sinon une minuterie vivante rafraîchirait `notify_liveness`
        # et masquerait indéfiniment un vrai notificateur arrêté (B/P1 cx2,
        # auditeur, #3202).
        if selected is not None and "alerts" not in selected:
            return self._run_tail(db, now, selected)
        # #3913 B1 : le bail peut s'être perdu ENTRE la prise et ce passage
        # (passe retardée, base tombée). Ne rien battre ni pousser : le
        # nouveau détenteur livre et son état fait foi ; les passes
        # mécaniques indépendantes gardent leur tour.
        if not self._delivery_open():
            return self._tail_without_alerts(db, now, selected)
        # #3007 : ce passage prouve, pour tout le maillage, que CE notificateur
        # joint ses humains. Un poste sain ne lève donc plus d'alerte de voie
        # depuis les VM ; son silence, lui, se voit partout.
        if db is not None and not self.dry_run:
            try:
                notify_gap.beat(self.cfg, db, self.ncfg, now=now)
            except Exception as exc:  # noqa: BLE001 — un battement ne fait pas le passage
                self.log("[%s] heartbeat not recorded (%s)"
                         % (notify_gap.GAP_TYPE, exc))
        observed_ts = storage.of(db).alert_acknowledgements.clock() if db is not None else None
        #: #1901 : familles qu'une passe n'a pas pu lire du tout (porte d'hôte
        #: au registre illisible) ; une alerte absente d'un relevé incomplet
        #: n'est pas résolue pour autant.
        unreadable: list[str] = []
        if current is None:
            # #2105 : without explicit thresholds, observe and declare the same
            # perimeter — the parser's defaults (`default_thresholds`,
            # environment included), as the CLI and the API do, not the bare
            # defaults of `alerts`, which may differ. The perimeter declared
            # below is then exactly the one observed here.
            thresholds = thresholds or alert_ack.default_thresholds()
            # #1179 : la console du pool local n'est pas lue par la passe
            # ordinaire ; `run_local_pass` la lit depuis `cfg` seule et reste
            # seul juge de ses alertes. Une console illisible n'y échoue plus.
            # #3761 : la porte des avis est un calcul Git coûteux mais stable
            # tant que le gel, la cible, les jalons et le canon ne bougent pas ;
            # son verdict est porté par l'état de la passe (document partagé).
            gate_cache = self.state.get("gates")
            if not isinstance(gate_cache, dict):
                gate_cache = self.state["gates"] = {}
            from . import canon as canon_mod
            with canon_mod.configured_cache():
                # #4579 : la passe de livraison, sous bail, date seule les
                # épisodes d'attente d'empreinte ; une passe à blanc n'écrit rien.
                current = exploitation.alerts(
                    self.cfg, db, now=now, local_pool=False,
                    unreadable=unreadable, gate_cache=gate_cache,
                    record_footprint=not self.dry_run, **thresholds)
        # #3913 B1 : la lecture/le calcul a pu durer plus que le bail. Avant
        # la première écriture (les accusés) et la première poussée, on
        # reprend le verdict : un détenteur périmé se tait, même si son
        # échéance locale n'a pas encore été vue par le fil de renouvellement.
        if not self._delivery_open():
            return self._tail_without_alerts(db, now, selected)
        records: list = []
        self._records = records
        router = Router(self.cfg, db, self.ncfg)
        acknowledgements = []
        if db is not None:
            # #1473 : un accusé se juge dans le périmètre qui l'a pris ; cette
            # passe réobserve l'alerte dans le sien avant de la dire résolue.
            def reobserve(own):
                # Sans la console locale (#1179) : ses alertes ne s'accusent pas.
                return alert_ack.current(self.cfg, db, now=now, local_pool=False, **own)

            acknowledgements = alert_ack.reconcile(
                db, current, observed_ts=observed_ts, dry_run=self.dry_run,
                observation_host=getattr(self.cfg, "host", None),
                unreadable=unreadable,
                perimeter=alert_ack.canonical(thresholds or alert_ack.default_thresholds()),
                evaluate=reobserve)
        # Reuse each subject's route for acknowledgement and group membership;
        # lot/package lookups belong to this snapshot, not to every projection.
        routes = {id(a): router.resolve(a) for a in current}

        def resolve_current(alert):
            return routes[id(alert)]

        notifications = group_orphan_lots(current, resolve_current)
        # Retain all group identities, including fully acknowledged groups:
        # filtering them out of wanted would falsely announce a resolution.
        unacknowledged = [a for a in current if alert_ack.matches(
            acknowledgements, a, resolve_current(a)[0]) is None]
        audible = {key_of(a): a for a in group_orphan_lots(unacknowledged, resolve_current)}
        subjects_by_key = {}
        for subject in current:
            group = group_orphan_lots([subject], resolve_current)[0]
            subjects_by_key.setdefault(key_of(group), []).append({
                "key": alert_ack.key_of(subject), "severity": alert_ack.severity(subject)})
        # #789 t2 (canon §9) : une alerte qui CIBLE le propriétaire
        # (`targets` contient "owner") ne se perd pas — elle atteint l'humain
        # même si son type a été retiré de `notify.types`. #2395 : sauf un
        # `slo_breach`, jamais poussé à l'owner (décision owner 18/10) ; ses
        # dépassements restent filtrables et partent aux rôles par courrier.
        wanted = {key_of(a): a for a in notifications
                  if a.get("type") in self.ncfg.types or forces_owner(a)}
        active = self.state["active"]
        self._reconcile_orphan_groups(active, wanted, router)
        # Persisted deliveries from an older routing policy must not retry or
        # resolve to an assignee's human after a project's owner is selected.
        for entry in list(active.values()) + list(self.state["resolved"].values()):
            if entry["alert"].get("type") == "module_deviation":
                human, source = router.resolve(entry["alert"])
                if human != entry.get("human"):
                    entry.update(human=human, source=source, status="nouvelle", channels={})
        for key in list(active):
            if key in wanted:
                continue
            entry = active[key]
            if entry["alert"].get("type") in LOCAL_TYPES:
                # #1179 : ces alertes appartiennent à `run_local_pass`, seul à
                # pouvoir les résoudre ; ne pas les voir ici n'est pas une
                # preuve de résolution.
                continue
            observer = entry["alert"].get("observation_host")
            if (entry["alert"].get("type") == "staging_interrupted"
                    and "staging.sequence" in unreadable):
                continue
            if (availability.GATE_FAMILY in unreadable
                    and entry["alert"].get("type") in availability.ALERT_TYPES
                    and observer in (None, getattr(self.cfg, "host", None))):
                # #1901 : la famille « porte d'hôte » n'a pas été lue par cette
                # passe ; ni résolution ni acquittement invalidé sur une absence.
                # L'état antérieur à #1901 ne gardait pas observation_host.
                continue
            if entry.get("status") == "resumee":
                # #2395 : raccorder l'ancien compte de résumé pendant que
                # l'entrée est encore visible dans `active`, avant qu'elle ne
                # disparaisse.
                self._adopt_summary_entry(entry)
            del active[key]
            if self._acknowledged(entry, acknowledgements):
                self._mute(entry)
            if entry["alert"].get("type") not in self.ncfg.types \
                    and not forces_owner(entry["alert"]):
                self.log("[%s] removed from notify.types: forgotten without resolution"
                         % entry["alert"].get("type"))
                continue
            self._resolved(key, entry, now)
            self._event(db, "alert.resolved", key, entry["alert"])
        for key, alert in wanted.items():
            entry = active.get(key)
            subjects = subjects_by_key[key]
            human, _ = (routes[id(alert)] if id(alert) in routes
                        else router.resolve(alert))
            muted = key not in audible
            # A partial orphan group only mentions its unacknowledged members.
            alert = audible.get(key, alert)
            if entry is None and _held_dead_runner(alert, now):
                # #3630 : coupure courte, peut-être déjà reprise à la passe
                # suivante ; rien n'est poussé, et faute d'entrée active,
                # aucune résolution ne partira.
                continue
            level = alert_ack.severity(alert)
            if (entry is not None and alert.get("type") == "slo_breach"
                    and entry.get("status") == "envoi" and entry.get("human")
                    and entry.get("human") == normalize_human(self.ncfg.default_human)):
                # #2395 : entrée persistée par l'ancienne politique, qui
                # retentait une livraison SLO vers l'owner. On la remet en
                # attente de destinataire : `_raise` applique les règles
                # neuves (aucun envoi owner), sans retenter ni résoudre.
                entry.update(status="sans_destinataire", channels={}, human=None)
            changed_ack_level = any(r["principal"] == human
                and any(r["alert_key"] == a["key"] and r["severity"] != a["severity"]
                        for a in subjects)
                for r in acknowledgements)
            if entry is not None and ((entry.get("status") == "acknowledged" and not muted)
                                      or changed_ack_level):
                self._forget_summary(entry)
                entry.update(status="nouvelle", channels={}, alert={
                    k: alert.get(k) for k in _KEPT if alert.get(k) is not None})
                entry.pop("escalated", None)
            if muted:
                if entry is None:
                    entry = active[key] = {"alert": {k: alert.get(k) for k in _KEPT
                        if alert.get(k) is not None}, "key": key, "severity": level,
                        "raised_ts": now, "channels": {}}
                entry.update(human=human, severity=level, ack_subjects=subjects)
                self._mute(entry)
                continue
            if entry is not None:
                # #2395 : l'identité et la sévérité de l'alerte vivent dans
                # l'entrée, pour le registre persistant `notified`.
                entry["key"] = key
                entry["severity"] = level
            if entry is None:
                entry = active[key] = {
                    "alert": {k: alert.get(k) for k in _KEPT if alert.get(k) is not None},
                    "key": key, "severity": level,
                    "raised_ts": now, "human": None, "source": None, "status": "nouvelle",
                    "channels": {}}
                if alert.get("type") in exploitation.ESCALATING:
                    entry["escalated"] = bool(alert.get("critical"))
                self._raise(entry, router, now)
                self._event(db, "alert.raised", key, alert)
            elif (alert.get("type") in exploitation.ESCALATING and alert.get("critical")
                  and not entry.get("escalated")):
                # #246 : même épisode, niveau critique atteint — poussée une
                # seconde fois, sur place (une seule fois par épisode ; le
                # retour au seuil puis au critique ne la relève pas)
                entry.update(alert={k: alert.get(k) for k in _KEPT if alert.get(k) is not None},
                             escalated=True, status="nouvelle", channels={})
                self._raise(entry, router, now)
            else:
                if alert.get("type") == "orphan_lot" and alert.get("grouped"):
                    entry["alert"] = {k: alert.get(k) for k in _KEPT if alert.get(k) is not None}
                if entry["status"] in ("nouvelle", "sans_destinataire", "sans_canal"):
                    # un responsable (ou une route) a pu apparaître depuis
                    self._raise(entry, router, now)
                else:
                    self._retry(entry, "raised", now)
            entry.update(severity=level, ack_subjects=subjects)
        self._brief_agents(db, current, router, now, unreadable=unreadable)
        for key in list(self.state["resolved"]):
            entry = self.state["resolved"][key]
            if (entry["alert"].get("type") == "slo_breach" and entry.get("human")
                    and entry.get("human") == normalize_human(self.ncfg.default_human)):
                # #2395 : une résolution SLO persistée par l'ancienne politique
                # n'est pas renvoyée à l'owner ; elle s'arrête là.
                del self.state["resolved"][key]
                continue
            if self._acknowledged(entry, acknowledgements):
                del self.state["resolved"][key]
                continue
            self._retry(entry, "resolved", now)
            if not self._pending(entry):
                del self.state["resolved"][key]
        self._summaries(now)
        take = selected is None or "take" in selected
        if take and self.handoff:
            self._budget_handoff(db, now)
        if take and self.take:
            self._auto_take(db, current, now)
        if take and self.wip:
            self._wip_sweep(db, now)
        if selected is None or "issues" in selected:
            self._github_issues(db, now)
        if selected is None or "github-watch" in selected:
            self._github_inputs(db, now)
        if selected is None or "ci" in selected:
            self._ci_failures(db)
        for human in list(self.state["rate"]):
            window = [t for t in self.state["rate"][human] if now - t < RATE_WINDOW_S]
            if window:
                self.state["rate"][human] = window
            else:
                del self.state["rate"][human]
        self._prune_notified(now)
        return records

    def _run_tail(self, db, now: float, selected) -> list:
        """The independent mechanical passes, without the alert delivery.

        `--only github-watch`, `--only ci`, `--only issues` and `--only
        fingerprints` are the role timers of EP5: they write no notify state
        and take no state lock, so two of them may share a host with the alert
        producer. `--only take` still observes the alerts first, because
        auto-take uses them. `fingerprints` (#3885) also refuses outside
        `ameesh role run`.
        """
        records: list = []
        self._records = records
        if "take" in selected:
            from . import alert_ack
            thresholds = alert_ack.default_thresholds()
            gate_cache = self.state.get("gates")
            if not isinstance(gate_cache, dict):
                gate_cache = self.state["gates"] = {}
            from . import canon as canon_mod
            with canon_mod.configured_cache():
                current = exploitation.alerts(
                    self.cfg, db, now=now, local_pool=False,
                    gate_cache=gate_cache, **thresholds)
            if self.handoff:
                self._budget_handoff(db, now)
            if self.take:
                self._auto_take(db, current, now)
                self._queue_fill(db, now)
            if self.wip:
                self._wip_sweep(db, now)
        if "issues" in selected:
            self._github_issues(db, now)
        if "github-watch" in selected:
            self._github_inputs(db, now)
        if "ci" in selected:
            self._ci_failures(db)
        if "fingerprints" in selected:
            self._fingerprints(db, now)
        if "coherence" in selected:
            self._coherence(db, now)
        return records

    def run_local_pass(self, now: float | None = None) -> list:
        """Un passage sans la base du mesh : la console du pool local (#532).

        Le notifieur atteint la base à travers ce même pool : quand
        `max_client_conn` refuse tout le monde, sa connexion ordinaire échoue
        alors que la console, elle, répond encore. Ce passage lit donc la
        console depuis `cfg` seule, puis lève, retente et résout les alertes de
        `LOCAL_TYPES` — sans connexion ni lecture préalable de la base. Les
        autres livraisons en cours sont laissées telles quelles : une alerte
        non observée n'est jamais résolue. Une console illisible est
        journalisée et ne change rien."""
        now = float(self.clock() if now is None else now)
        records: list = []
        self._records = records
        from . import pgbouncer_local
        try:
            current = pgbouncer_local.probe(self.cfg)
        except db_mod.Unavailable as exc:
            self.log("local pool console unreadable (%s): active local alerts kept"
                     % exc)
            return records
        router = Router(self.cfg, None, self.ncfg)
        wanted = {key_of(a): a for a in current if a.get("type") in self.ncfg.types}
        active = self.state["active"]
        for key, alert in wanted.items():
            # Une console qui represse la même clé annule le tombstone de
            # résolution en attente de fusion (#3576, relecture dev4).
            self.local_resolved_keys.discard(key)
            entry = active.get(key)
            if entry is None:
                entry = active[key] = {
                    "alert": {k: alert.get(k) for k in _KEPT if alert.get(k) is not None},
                    "raised_ts": now, "human": None, "source": None, "status": "nouvelle",
                    "channels": {}}
                self._raise(entry, router, now)
            elif entry["status"] in ("nouvelle", "sans_destinataire", "sans_canal"):
                # un responsable (ou une route) a pu apparaître depuis
                self._raise(entry, router, now)
            else:
                self._retry(entry, "raised", now)
        self._local_resolutions(wanted, now)
        self._prune_notified(now)
        return records

    def _local_resolutions(self, wanted: dict, now: float) -> None:
        """Résout les alertes locales que la console ne montre plus, et retente
        les résolutions en vol. Les autres types ne sont jamais touchés : la
        base peut simplement être non observée.

        #3576 : l'état partagé porte les alertes de console de tous les hôtes ;
        un hôte ne résout que les siennes (`alert.host`), jamais celle d'un
        autre dont la console ne dit rien ici (relecture cx1)."""
        host = getattr(self.cfg, "host", None)
        # Résolutions déjà en vol AVANT ce tour : seules celles-là se soldent
        # ici ; celle que `_resolved` vient d'ajouter doit atteindre la fusion
        # de l'état partagé (#3576, relecture cx1).
        before = set(self.state["resolved"])
        for key in list(self.state["active"]):
            if key in wanted:
                continue
            entry = self.state["active"][key]
            if not _local_entry_owned(entry, host):
                continue
            del self.state["active"][key]
            # #3576 (relecture dev4) : la fusion doit retirer la clé du partagé
            # même quand `_resolved` ne garde rien (acknowledged, notifiee,
            # sans_canal, type retiré) — sinon l'entrée y reste active et une
            # résolution repart à chaque tick.
            self.local_resolved_keys.add(key)
            if entry["alert"].get("type") not in self.ncfg.types:
                self.log("[%s] removed from notify.types: forgotten without resolution"
                         % entry["alert"].get("type"))
                continue
            self._resolved(key, entry, now)
        for key in list(self.state["resolved"]):
            if key not in before:
                continue
            entry = self.state["resolved"][key]
            if not _local_entry_owned(entry, host):
                continue
            self._retry(entry, "resolved", now)
            if not self._pending(entry):
                del self.state["resolved"][key]

    @staticmethod
    def _acknowledged(entry: dict, rows: list) -> bool:
        from . import alert_ack
        subjects = entry.get("ack_subjects") or [{"key": alert_ack.key_of(entry["alert"]),
                                                "severity": alert_ack.severity(entry["alert"])}]
        return all(any(r["alert_key"] == a["key"] and r["severity"] == a["severity"]
                       and r["principal"] == entry.get("human") for r in rows)
                   for a in subjects)

    def _forget_summary(self, entry: dict) -> None:
        if entry.get("status") != "resumee":
            return
        human = entry.get("human")
        bucket = self.state["summary"].get(human)
        if not bucket:
            return
        bucket = self._normalize_summary_bucket(human, bucket)
        member = self._summary_member(entry, human)
        identity = self._summary_member_id(member)
        frozen = bucket.get("frozen") or {}
        if identity in (frozen.get("raised") or {}):
            # L'alerte est dans l'épisode déjà figé : annuler l'épisode et
            # remettre ses autres membres dans le stock vivant, avec une
            # nouvelle identité (le contenu du résumé change).
            self._summary_cancel_frozen(bucket)
        members = bucket["raised"]
        if identity in members:
            del members[identity]
        else:
            # Bucket persisté avant #2395 : les identités n'y étaient pas
            # gardées. On retire un membre du même type, comme l'ancien
            # compteur, sans toucher aux identités réelles.
            for old_id, old in list(members.items()):
                if (old.get("type") == member.get("type")
                        and str(old.get("key") or "").startswith("legacy:")):
                    del members[old_id]
                    break
        if not self._summary_has_members(bucket):
            self.state["summary"].pop(human, None)

    def _mute(self, entry: dict) -> None:
        self._forget_summary(entry)
        entry.update(status="acknowledged", channels={})

    def _reconcile_orphan_groups(self, active: dict, wanted: dict, router: Router) -> None:
        """Adopt persisted per-lot deliveries before checking for resolutions.

        Keep recipient identity and successful channels: upgrading must not
        resolve/re-raise an unchanged orphan situation or resend delivered alerts.
        Multiple old alerts share the earliest raise and a single retry budget.
        """
        ranks = {"nouvelle": 0, "sans_destinataire": 1, "sans_canal": 2,
                 "resumee": 3, "envoi": 4}
        for old_key in list(active):
            entry = active[old_key]
            alert = entry["alert"]
            if alert.get("type") != "orphan_lot" or alert.get("grouped"):
                continue
            human = entry.get("human")
            if human is None:
                human, _ = router.resolve(alert)
            identity = {"type": "orphan_lot", "agent": alert.get("agent"),
                        "reason": alert.get("reason"), "responsible": human, "grouped": True}
            key = key_of(identity)
            if key not in wanted:
                continue
            active.pop(old_key)
            target = active.get(key)
            if target is None:
                target = active[key] = entry
            else:
                target["raised_ts"] = min(target["raised_ts"], entry["raised_ts"])
                if ranks.get(entry["status"], 0) > ranks.get(target["status"], 0):
                    target.update(status=entry["status"], human=entry.get("human"),
                                  source=entry.get("source"))
                for channel, state in entry.get("channels", {}).items():
                    previous = target["channels"].get(channel)
                    if previous is None or (not previous.get("ok") and
                            (state.get("ok") or state.get("attempts", 0) < previous.get("attempts", 0))):
                        target["channels"][channel] = dict(state)
            target["alert"] = {k: wanted[key].get(k) for k in _KEPT
                               if wanted[key].get(k) is not None}

    # -- issues GitHub des lots (L126) ------------------------------------------
    def _github_issues(self, db, now: float) -> None:
        """Chaque lot a son issue GitHub, tenue par ameesh : la projection
        tourne ici, sur l'hôte que désigne la clé `github` de la configuration
        (`plan_github.Projector`). Sans cette clé, rien ; une erreur (GitHub,
        `gh`, base) est journalisée et ne touche jamais aux alertes."""
        if not getattr(self.cfg, "github", None):
            return
        from . import plan_github
        try:
            if self._projector is None:
                self._projector = plan_github.Projector(
                    self.cfg, dry_run=self.dry_run, log=self.log, clock=self.clock)
            self._projector.tick(db, now)
        except Exception as exc:  # noqa: BLE001 - jamais fatal au passage des alertes
            self.log("GitHub issues: pass failed (%s)" % _clean(exc, 200))

    def _github_inputs(self, db, now: float) -> None:
        """#2845: feed external issues/comments to the canonical watcher.

        The watched repositories and cadences come from the canon's
        ``github_watch`` cards, not from the host's ``github`` key: a role
        timer on a candidate host must collect them even when that key is
        absent (#3327). The watcher's own host gate (``role_run``) decides
        whether this host may run.
        """
        from . import github_watch
        try:
            if self._github_watcher is None:
                self._github_watcher = github_watch.Watcher(
                    self.cfg, dry_run=self.dry_run, log=self.log)
            self._github_watcher.tick(db, now)
        except Exception as exc:
            self.log("GitHub watch: pass failed (%s)" % _clean(exc, 200))

    # -- échecs de CI au courrier de l'agent du lot (L161c) ----------------------
    def _ci_failures(self, db) -> None:
        """Le relevé de la CI (`ci.Watcher`), sur le même hôte désigné que les
        issues (`github.host`). Une erreur est journalisée, jamais fatale."""
        if not getattr(self.cfg, "github", None):
            return
        from . import ci
        try:
            if self._ci_watcher is None:
                self._ci_watcher = ci.Watcher(self.cfg, dry_run=self.dry_run, log=self.log,
                                              clock=self.clock)
            for record in self._ci_watcher.tick(db):
                self.log(ci.describe(record))
        except Exception as exc:  # noqa: BLE001 - jamais fatal au passage des alertes
            self.log("CI: poll failed (%s)" % _clean(exc, 200))

    # -- cohérence du backlog (#4172) ---------------------------------------
    def _coherence(self, db, now: float) -> None:
        """La passe de cohérence du backlog, sous le bail du rôle élu.

        #4172 : elle ferme les constats déterministes (déjà fait, obsolète)
        avec leur justification dans le journal du lot, et envoie UN digest
        par projet au PO pour les doublons et les contradictions. Le service
        complet ne la lance jamais : elle n'est admise que sous `ameesh role
        run`, où le bail désigne un seul hôte.
        """
        from . import backlog_coherence
        if not (os.environ.get("AMEESH_ROLE") or "").strip():
            raise backlog_coherence.CoherenceError(
                "coherence pass refused: run it under the elected role "
                "(`ameesh role run <role> -- ameesh notify --once --only "
                "coherence`), never from the full notifier")
        reports = backlog_coherence.tick(
            self.cfg, db, now=now, apply=not self.dry_run, log=self.log)
        errors = [report["error"] for report in reports if report.get("error")]
        if errors:
            raise backlog_coherence.CoherenceError(
                "coherence pass incomplete: %s" % "; ".join(errors)[:500])

    # -- empreintes d'échec (canon §16, #3885) --------------------------------
    def _fingerprints(self, db, now: float) -> None:
        """La passe des empreintes d'échec : un lot par empreinte nouvelle.

        #3885 : elle n'existe QUE sous `ameesh role run` (le détenteur du bail
        lit la source avec un jeton en lecture seule). Le service complet ne
        l'exécute jamais ; un `--only fingerprints` lancé hors du rôle refuse
        explicitement, comme une source sans jeton.
        """
        from . import fingerprints
        if not (os.environ.get("AMEESH_ROLE") or "").strip():
            raise fingerprints.FingerprintError(
                "fingerprints pass refused: run it under the elected role "
                "(`ameesh role run <role> -- ameesh notify --once --only "
                "fingerprints`), never from the full notifier")
        results = fingerprints.tick(self.cfg, db, now=now, log=self.log)
        for result in results:
            self.log("fingerprints %s: lot #%d %s (occurrences %d, users %d)"
                     % (result.get("project"), result["lot"],
                        "opened" if result.get("created") else "noted",
                        result.get("occurrences", 0), result.get("users", 0)))

    # -- prise automatique (L119, décision 0037) ------------------------------
    def _auto_take(self, db, current: list, now: float) -> None:
        from . import backlog
        try:
            taken = backlog.auto_take(self.cfg, db, now=now, alerts=current,
                                      dry_run=self.dry_run, **self.take)
        except db_mod.DbError:
            raise
        except Exception as exc:     # la prise ne fait jamais tomber les alertes
            self.log("auto-take failed: %s" % _clean(exc, 200))
            return
        for take in taken:
            item = take["item"]
            entry = {"alert": {"type": "auto_take", "agent": take["agent"],
                               "lot": int(item["id"]), "title": item.get("title")},
                     "human": None,
                     "source": {"lot": "lot de l'équipe",
                                "review": "relecture proposée",
                                "correction": "correction proposée"}.get(
                                    take.get("kind"), "file d'amélioration")}
            self._record("auto_take", entry, "a_blanc" if self.dry_run else "prise", {})

    def _queue_fill(self, db, now: float) -> None:
        """#3256 A : le lot de découpage d'un projet dont la file est vide alors
        que la capacité est là ; repli de l'auditeur, sommeil et réveil."""
        from . import file_decoupage
        try:
            gestures = file_decoupage.run(self.cfg, db, now=now, state=self.state,
                                          dry_run=self.dry_run)
        except db_mod.DbError:
            raise
        except Exception as exc:     # le découpage ne fait jamais tomber les alertes
            self.log("queue fill failed: %s" % _clean(exc, 200))
            return
        for gesture in gestures:
            entry = {"alert": {"type": "queue_fill_" + gesture["kind"],
                               "lot": gesture.get("lot"),
                               "project": gesture.get("project")},
                     "human": None, "source": "file remplie (#3256 A)"}
            self._record("queue_fill", entry, "a_blanc" if self.dry_run else "geste", {})

    # -- §4 (owner 14/10/2026) : limite d'en-cours -----------------------------
    def _wip_sweep(self, db, now: float) -> None:
        """Rend à la file le surplus des agents au-dessus du plafond (§4).
        Sans `--wip-apply` — et sous le `--dry-run` global — le passage ne rend
        rien : il ne fait que relever (`a_blanc`)."""
        from . import wip as wip_mod
        dry = bool(self.dry_run or not self.wip.get("apply"))
        try:
            returned = wip_mod.sweep(self.cfg, db, dry_run=dry,
                                     limit=int(self.wip.get("limit") or 0))
        except db_mod.DbError:
            raise
        except Exception as exc:     # le balayage ne fait jamais tomber les alertes
            self.log("WIP sweep failed: %s" % _clean(exc, 200))
            return
        for row in returned:
            item = row["item"]
            entry = {"alert": {"type": "wip_return", "agent": row["agent"],
                               "lot": int(item["id"]), "title": item.get("title")},
                     "human": None, "source": "limite d'en-cours (§4)"}
            self._record("wip_return", entry, "a_blanc" if dry else "rendu", {})

    def _budget_handoff(self, db, now: float) -> None:
        from . import handoff
        try:
            moved = handoff.budget_handoff(self.cfg, db, now=now, dry_run=self.dry_run,
                                           **self.handoff)
        except db_mod.DbError:
            raise
        except Exception as exc:     # la reprise ne fait jamais tomber les alertes
            self.log("budget handoff failed: %s" % _clean(exc, 200))
            return
        for move in moved:
            item = move["item"]
            queued = bool(move.get("queued"))
            entry = {"alert": {"type": "budget_handoff", "agent": move["agent"],
                               "lot": int(item["id"]), "title": item.get("title")},
                     "human": None, "source": "pause budget de %s" % move["previous"]}
            self._record("budget_handoff", entry,
                         "a_blanc" if self.dry_run else ("rendu" if queued else "prise"), {})

    # -- registre des notifications (#2395) ------------------------------------
    @staticmethod
    def _notified_id(entry: dict, human: str) -> str:
        """Identité du registre : la clé de l'alerte (elle porte la raison,
        donc le niveau), la sévérité, l'humain et le `since` de l'épisode (pas
        toujours dans la clé, #845). Une même alerte
        qui se résout puis revient ne repart pas ; une escalade de niveau a
        une autre identité et part bien."""
        key = entry.get("key") or key_of(entry["alert"])
        return _notification_id(key, _entry_severity(entry), human,
                                _since_of(entry["alert"]))

    def _already_notified(self, entry: dict, human: str) -> bool:
        return self._notified_id(entry, human) in (self.state.get("notified") or {})

    def _mark_notified(self, entry: dict, now: float) -> None:
        human = entry.get("human")
        if not human or _since_of(entry["alert"]) is None:
            # sans `since`, l'épisode n'a pas d'identité : un retour après
            # résolution repart, comme avant #2395
            return
        self.state.setdefault("notified", {})[
            self._notified_id(entry, human)] = round(float(now), 3)

    def _prune_notified(self, now: float) -> None:
        ledger = self.state.get("notified") or {}
        kept = {key: ts for key, ts in ledger.items()
                if now - float(ts) < NOTIFIED_RETENTION_S}
        if len(kept) > NOTIFIED_MAX:
            newest = sorted(kept.items(), key=lambda item: float(item[1]))[-NOTIFIED_MAX:]
            kept = dict(newest)
        self.state["notified"] = kept
        # #3536 : même rétention pour le registre des replis nexlink
        fallback = self.state.get("fallback") or {}
        kept = {key: ts for key, ts in fallback.items()
                if now - float(ts) < NOTIFIED_RETENTION_S}
        if len(kept) > NOTIFIED_MAX:
            newest = sorted(kept.items(), key=lambda item: float(item[1]))[-NOTIFIED_MAX:]
            kept = dict(newest)
        self.state["fallback"] = kept

    # -- levée ----------------------------------------------------------------
    def _event(self, db, kind: str, key: str, alert: dict) -> None:
        """L85: the alert in the event log (`GET /api/v1/events`); a base
        error is said, never fatal to the pass."""
        if self.dry_run or db is None or not self._delivery_open():
            return
        from . import events
        data = {name: alert.get(name) for name in ("agent", "lot", "host")
                if alert.get(name) is not None}
        try:
            events.record(db, kind, ("alert:%s" % alert.get("type"))[:300],
                          project=alert.get("project"), alert=alert.get("type"), key=key,
                          **data)
        except Exception as exc:  # noqa: BLE001 — the notification goes on
            self.log("event %s not recorded: %s" % (kind, type(exc).__name__))

    def _raise(self, entry: dict, router: Router, now: float) -> None:
        if not self._delivery_open():
            return
        alert = entry["alert"]
        first = entry["status"] == "nouvelle"
        human, source = router.resolve(alert)
        if human is None:
            if first:
                self.log("alert WITHOUT RECIPIENT [%s] %s: no `responsible`, no "
                         "agent, lot or host responsible, no notify.default_human — "
                         "%s" % (alert.get("type"), _subject(alert), alert.get("detail")))
                self._record("raised", entry, "sans_destinataire", {})
            entry["status"] = "sans_destinataire"
            return
        channels = self.ncfg.channels_for(human)
        entry.update(human=human, source=source)
        if not channels:
            if entry["status"] != "sans_canal":
                self.log("no channel for %s (notify.routes, notify.default): alert [%s] "
                         "%s only logged" % (human, alert.get("type"),
                                                       _subject(alert)))
                self._record("raised", entry, "sans_canal", {})
            entry["status"] = "sans_canal"
            return
        if self._already_notified(entry, human):
            # #2395 : une seule notification par alerte (même `since`), même
            # sévérité et par humain — y compris si l'alerte se résout puis
            # revient, et après un redémarrage d'`ameesh-notify` (registre
            # persistant). Rien n'est renvoyé : seul le journal le dit.
            entry["status"] = "notifiee"
            entry["channels"] = {}
            self.log("[%s] %s already notified to %s (same alert, same severity): "
                     "not sent again" % (alert.get("type"), _subject(alert), human))
            return
        if not self._allow(human, now):
            entry["status"] = "resumee"
            self._summary_add(self._bucket(human), "raised", entry)
            self._record("raised", entry, "resumee", {})
            return
        entry["status"] = "envoi"
        entry["channels"] = {c.name: {"ok": False, "attempts": 0, "error": None}
                             for c in channels}
        self._attempt(entry, "raised", now)

    # -- agents qui peuvent agir (L132) ---------------------------------------
    def _brief_agents(self, db, current: list, router: Router, now: float,
                      *, unreadable=()) -> None:
        """`idle_capacity`, `stale_lot`, `plan_underused`, `mail_undeliverable` :
        courrier `event` aux orchestrateurs et à l'auditeur, avec l'action
        attendue (`alert_briefs` ; `stale_lot` excepté pour l'auditeur, L713 :
        il le lit à sa passe, `ameesh alerts --json`). Une fois à la levée, une
        relance si l'alerte dure `REMIND_AFTER_S`. Pour les SLO (#2602), un
        envoi réussi par destinataire, état du lot et palier, sans relance
        périodique ; une absence transitoire ne l'oublie pas. #3036 : un agent
        sans bail vivant (ou sur un hôte aux claims fermés) n'est pas un
        destinataire ; sa remise ne compte pas, et la passe suivante réévalue
        puis remet à qui est revenu. #3236 : un brief par alerte et par
        fenêtre — la clé d'une jauge est stable à la minute, et une disparition
        d'une passe (perte projetée qui oscille autour du seuil) ne refait pas
        briefer la même fenêtre avant la relance de 6 h. Indépendant de
        `notify.types` (qui ne règle que l'humain). Jamais fatal au passage."""
        if not self._delivery_open():
            return
        sent = self.state["briefs"]
        # #3007 : le brief de `notify_undeliverable` dépend de la vie de CE
        # notificateur ; c'est `notify_gap.brief_once`, au ménage du runner,
        # qui le porte hors du poste (aucune duplication : réclamation en base).
        alive = {brief_key_of(a): a for a in current
                 if a.get("type") in alert_briefs.BRIEFED_TYPES
                 and a.get("type") not in alert_briefs.NOTIFIER_EXCLUDED}
        open_lots = None
        for key in list(sent):
            if key not in alive:
                entry = sent[key]
                if entry.get("type") == "staging_interrupted" and "staging.sequence" in unreadable:
                    continue
                if entry.get("type") == "idle_capacity" and now - float(entry["sent_ts"]) \
                        < alert_briefs.REMIND_AFTER_S:
                    # #2602: brief gaps in available capacity are not a fresh
                    # reason to wake the pilots every few minutes.
                    continue
                if entry.get("type") == "plan_underused" \
                        and now - float(entry["sent_ts"]) < alert_briefs.REMIND_AFTER_S:
                    # #3236 : la perte projetée d'une jauge oscille autour du
                    # seuil (50 % par défaut) ; l'alerte disparaît d'une passe
                    # puis revient. C'est la même fenêtre, déjà briefée : garder
                    # l'entrée évite un courrier par retour avant la relance.
                    continue
                if entry.get("type") == "slo_breach" and entry.get("lot"):
                    # #2602: a planned wait or a clock crossing below a
                    # threshold must not forget deliveries for this state.
                    try:
                        if open_lots is None:
                            open_lots = {int(row["id"]): row for row in
                                         storage.of(db).operations.open_lots_activity(None)}
                        item = open_lots.get(int(entry["lot"]))
                    except Exception:  # unreadable is not a proven transition
                        continue
                    if item and item.get("state") == entry.get("lot_state") \
                            and item.get("assignee") == entry.get("agent") \
                            and item.get("state") not in (
                                lifecycle.CLOSED,
                                *lifecycle.DELIVERED,
                            ):
                        continue
                del sent[key]
        due = []
        dead_only = set()
        for key, alert in alive.items():
            entry = sent.get(key)
            if entry is None or alert.get("type") == "slo_breach":
                due.append((key, alert, entry))
            elif entry.get("pending"):
                # #3036 : une remise a échoué ; la passe la retente.
                due.append((key, alert, entry))
            elif not entry.get("reminded") and now - float(entry["sent_ts"]) \
                    >= alert_briefs.REMIND_AFTER_S:
                due.append((key, alert, entry))
            elif entry.get("dead"):
                # #3036 : des destinataires avaient été écartés (bail mort,
                # hôte aux claims fermés) ; la passe réévalue pour remettre à
                # qui est revenu, sans redéposer chez les morts. #3057 :
                # seulement si l'un d'eux a changé (filtre plus bas).
                due.append((key, alert, entry))
                dead_only.add(key)
        if not due:
            return
        from . import mail, work
        try:
            listing = storage.of(db).operations.listing()
            # #3036 : un agent dont le bail est mort ou dont l'hôte a les
            # claims fermés ne lira pas ; la résolution des destinataires
            # l'écarte au lieu de compter une remise perdue.
            closed = alert_briefs.claims_closed_hosts(db)
            # #3057 : un bail rendu depuis quelques secondes (redémarrage)
            # compte vivant pour le routage, mais rien ne lui est remis avant
            # son retour ou la fin de la grâce.
            listing, held = alert_briefs.lease_grace(
                listing, closed, self.state.setdefault("leaseless_since", {}), now)
            by_name = {row["name"]: row for row in listing}

            def down(entry: dict) -> list:
                # les destinataires d'une entrée (remis ou écartés) qui ne
                # liraient pas à cette passe ; le fil n'a pas de bail
                names = set(entry.get("to") or ()) | set(entry.get("dead") or ())
                return sorted(n for n in names if not n.startswith("thread:") and (
                    n not in by_name or alert_briefs._unavailable(by_name[n], closed)))

            # #3057 : un destinataire mort qui le reste (agent arrêté des
            # jours) ne refait pas, à chaque passe, briefs et titres du lot.
            # L'entrée est réévaluée dès que l'un des écartés revient (ou est
            # en grâce) ou quitte le registre, et dès que l'ensemble des
            # indisponibles change depuis la dernière évaluation — un
            # destinataire déjà servi qui tombe peut appeler le repli.
            due = [(key, alert, entry) for key, alert, entry in due
                   if key not in dead_only
                   or entry.get("down") != down(entry)
                   or any(name not in by_name
                          or not alert_briefs._unavailable(by_name[name], closed)
                          for name in entry["dead"])]
            if not due:
                return
            from . import canon as canon_mod
            try:
                canons = canon_mod.load_configured(self.cfg) if self.cfg.canon else []
            except Exception:  # noqa: BLE001 - aucun auditeur explicite sans canon
                canons = []
            auditors = alert_briefs.auditors(listing, canons)
            module_auditors = alert_briefs.auditors(listing, canons, explicit_only=True)
            from . import sous_utilisation
            module_orchestrators = sous_utilisation.role_orchestrators(canons)
        except Exception as exc:  # noqa: BLE001
            self.log("mail to agents not prepared (%s)" % exc)
            return
        unevaluated: set = set()
        for key, alert, entry in due:
            kind = alert.get("type")
            # #3036 : les destinataires écartés par la vivacité, pour que les
            # passes suivantes sachent qu'il reste quelqu'un à remettre.
            dead: dict = {}
            # les destinataires qui reçoivent la relance (les autres, le brief
            # initial), et si la relance de 6 h est échue à cette passe
            reminders: set = set()
            remind_due = False
            covered: set = set()
            if kind == "slo_breach":
                # One successful delivery per recipient and escalation
                # level. Failed deliveries and newly designated pilots retry.
                # #3036 : une entrée écrite par l'ancien notificateur peut
                # nommer des destinataires dont la remise était allée sur un
                # hôte perdu. On ne se fie à son `to` que pour un destinataire
                # encore vivant ; un nom mort à cette passe est retiré, pour
                # être remis quand il revient — sans rafale vers les vivants.
                # #3100 : une entrée de ce notificateur (`live_routed`) n'a
                # remis qu'à des vivants ; son `to` fait foi même si un bail
                # cligne (redémarrage d'exécuteur, clôture de release) — le
                # courrier est dans la boîte, lu au retour — et couvre le
                # palier : ni second courrier ni escalade.
                previous = {name: "" for name in (entry or {}).get("to") or ()}
                if entry is not None and not entry.get("live_routed"):
                    by_name = {row["name"]: row for row in listing}
                    still_live, gone = alert_briefs._split_live(previous, by_name, closed)
                    entry["to"] = sorted(still_live)
                    entry["dead"] = sorted(gone)
                    covered = set(still_live)
                else:
                    covered = set(previous)
            try:
                titles = {}
                for lot in (alert.get("lots") or ())[:12]:
                    item = storage.of(db).work.get(int(lot))
                    if item:
                        titles[int(lot)] = item.get("title") or ""
                briefs = alert_briefs.briefs(
                    alert, listing, titles,
                    module_auditors if kind == "module_deviation" else auditors,
                    orchestrator_names=module_orchestrators,
                    closed_hosts=closed, dead_out=dead, covered=covered)
            except Exception as exc:  # noqa: BLE001 - l'alerte part quand même
                self.log("[%s] mail to agents not prepared (%s)" % (kind, exc))
                unevaluated.add(key)
                continue
            for name in held & set(briefs):
                # #3057 : bail rendu il y a moins de la grâce ; ni remise ni
                # escalade, la passe suivante remet à son retour.
                briefs.pop(name)
                dead[name] = "lease returned, grace"
            if kind == "slo_breach" and "bureau" in (alert.get("targets") or ()) \
                    and any(
                        row.get("name") == "bureau" and row.get("mode") == "externe"
                        for row in listing):
                # Keep the desk's copy in the readable lot thread, without
                # growing a mailbox nobody wakes to read. No `home` project
                # exception (#2640): canon lots are `app=ameesh` (#2511).
                # #3036 : `setdefault` — un repli a pu y placer le texte du
                # palier effectivement visé, avec ses absents nommés.
                briefs.setdefault("thread:bureau", alert_briefs._slo_breach(alert))
            if kind == "slo_breach":
                briefs = {n: t for n, t in briefs.items() if n not in covered}
            if not briefs:
                # #3036 : une remise en attente sans destinataire à cette
                # passe ne doit pas faire tourner les passages à vide ; les
                # morts restent nommés dans `dead`.
                if entry is not None and kind != "slo_breach":
                    entry["dead"] = sorted(dead)
                    entry["pending"] = []
                continue        # personne à prévenir : on réessaie au passage suivant
            if kind != "slo_breach" and entry is not None:
                # #1787 N1 : la relance repart aux destinataires de la levée,
                # pas à ceux que l'état vivant désignerait maintenant (le brief
                # d'un lot sans assigné est atomique, #1026).
                # #3036 : un destinataire écarté à la levée n'a rien reçu ;
                # s'il est vivant maintenant, il reçoit le brief initial (pas
                # une relance), et rien n'est redéposé chez les morts.
                already = {str(n) for n in entry.get("to") or ()}
                fresh = {n: t for n, t in briefs.items() if n not in already}
                remind_due = not entry.get("reminded") and now - float(
                    entry["sent_ts"]) >= alert_briefs.REMIND_AFTER_S
                if remind_due:
                    # #3057 : la relance échue part dans la même passe que le
                    # brief initial d'un destinataire revenu, et la relance
                    # est alors consommée : il ne la reçoit pas à la passe
                    # suivante, quelques minutes après le brief initial.
                    raised = float(entry["sent_ts"])
                    reminders = {n for n in briefs if n in already}
                    briefs = {**fresh, **{n: alert_briefs.reminder(briefs[n], raised, now)
                                          for n in reminders}}
                else:
                    briefs = fresh
                if not briefs:
                    entry["dead"] = sorted(dead)
                    # #3057 : un `pending` encore destinataire serait dans
                    # `fresh` ; ce qui reste a quitté les briefs ou a été
                    # remis depuis : oublié, la passe ne tourne pas à vide.
                    entry["pending"] = []
                    continue
            payload = {"alert": kind, "reminder": False}
            # #789 t2 (canon §9) : l'urgence portée par l'alerte elle-même
            # (un P1) est propagée au courrier d'agent ; le type reste la
            # règle générale (L242b (5) : le point de l'auditeur).
            if kind in URGENT_TYPES or alert.get("urgent"):
                payload["urgent"] = True
            for field_ in ("module", "rule", "project", "declaration_id", "subject",
                           "lot", "agents", "lots", "harness", "account", "gauge",
                           "idle_agents"):
                if alert.get(field_) is not None:
                    payload[field_] = alert[field_]
            if kind == "mail_undeliverable":
                # Contrat du courrier d'orchestrateur (L118/#3036) : le
                # destinataire mort, la raison et le compte, pour que le
                # lecteur puisse relivrer sans relire l'alerte.
                payload.update(recipient=alert.get("agent"), reason=alert.get("reason"),
                               count=alert.get("value"),
                               # contrat historique du courrier d'orchestrateur
                               # (`_brief_orchestrators`), pour ses lecteurs
                               agents=list(alert.get("agents") or ()),
                               lots=list(alert.get("lots") or ()))
            # pas de `work_item_id` : un message lié au lot compterait comme
            # son activité (stagnation) et effacerait l'alerte qu'il signale
            delivered = []
            quiet = alert_briefs.quiet_payloads(alert, briefs, by_name)
            for name, text in sorted(briefs.items()):
                if not self._delivery_open():
                    break
                if self.dry_run:
                    self.log("[%s] mail to %s (dry run): %s" % (kind, name, text))
                    continue
                meta = dict(payload, reminder=name in reminders, **quiet.get(name, {}))
                try:
                    if kind == "slo_breach" and alert.get("lot"):
                        # #3512: the alert snapshot can predate a merge. Check
                        # again before each delivery, including the desk thread.
                        item = storage.of(db).work.get(int(alert["lot"]))
                        # A missing row is no evidence that delivery happened.
                        if item is not None and item.get("state") in lifecycle.COMPLETED:
                            break
                        if item is not None and alert.get("step") == "verdict_merge":
                            from . import slo_delivery
                            if slo_delivery.merge_observed(
                                    self.cfg, db, item, overview=listing):
                                break
                    if name == "thread:bureau":
                        # #3036 : la route humaine — le fil lisible du lot,
                        # comme la copie du bureau externe (#2602/#2640).
                        if fil.record(self.cfg, db, sender=work.SYSTEM_SENDER,
                                      recipients=["bureau"], text=text,
                                      project=alert.get("project"), lot=alert.get("lot"),
                                      meta=meta) is None:
                            continue
                    elif kind == "module_deviation":
                        if not self._send_module_brief(db, alert, name, text, meta):
                            continue
                    else:
                        mail.send(db, work.SYSTEM_SENDER, name, text, kind="event",
                                  payload=meta)
                    delivered.append(name)
                    self.log("[%s] %s delivered to %s" % (
                        kind, "reminder" if meta["reminder"] else "mail", name))
                except Exception as exc:  # noqa: BLE001
                    self.log("[%s] mail to %s not delivered (%s)" % (kind, name, exc))
            # #3036 : une remise qui n'a pas eu lieu (destinataire filtré,
            # envoi en échec) ne crée pas d'entrée et ne compte pas « fait » :
            # le passage suivant réessaie. Une remise au fil lisible compte,
            # elle.
            if not delivered and not self.dry_run:
                if entry is not None:
                    entry["dead"] = sorted(dead)
                    if kind != "slo_breach":
                        # #3036 : l'échec transitoire d'un destinataire reste
                        # à tenter ; il n'est ni dans `to` (réussi) ni dans
                        # `dead` (écarté).
                        entry["pending"] = sorted(set(briefs) - set(dead))
                continue
            if kind == "slo_breach":
                if delivered:
                    sent[key] = {"type": kind, "sent_ts": now,
                                 "lot": alert.get("lot"), "lot_state": alert.get("lot_state"),
                                 # #2640: a human-assigned lot carries its holder in
                                 # `responsible`, not `agent`; the ledger must keep the
                                 # actual assignee or an absent pass resends.
                                 "agent": alert.get("agent") or alert.get("responsible"),
                                 "reminded": True,
                                 # #3100 : remis par ce notificateur, aux
                                 # seuls vivants ; `to` fait foi ensuite.
                                 "live_routed": True,
                                 "to": sorted(set((entry or {}).get("to") or ()) | set(delivered))}
            elif entry is None:
                sent[key] = {"type": kind, "sent_ts": now,
                             # `mail_undeliverable` reste un envoi unique à la
                             # levée (comportement L118) : une relance de 6 h
                             # n'ajouterait rien au courrier déjà en souffrance.
                             "reminded": kind == "mail_undeliverable",
                             "to": sorted(set(delivered)),
                             # #3036 : les écartés, pour réévaluer la passe
                             # suivante et remettre à qui est revenu, et les
                             # remises en échec, distinctes des réussies.
                             "dead": sorted(dead),
                             "pending": sorted(set(briefs) - set(delivered) - set(dead))}
            else:
                entry["to"] = sorted(set(entry.get("to") or ()) | set(delivered))
                entry["dead"] = sorted(dead)
                entry["pending"] = sorted(set(briefs) - set(delivered) - set(dead))
                if remind_due:
                    entry["reminded"] = True
        # #3057 : l'état des destinataires vu à cette évaluation, pour que les
        # passes suivantes sachent si quelque chose a changé ; une entrée non
        # évaluée (préparation en échec) garde l'ancien et sera reprise.
        for key, _alert, _entry in due:
            entry = sent.get(key)
            if entry is not None and entry.get("type") != "slo_breach" \
                    and key not in unevaluated:
                entry["down"] = down(entry)

    def _send_module_brief(self, db, alert, name, text, payload):
        """Reauthorize under a recipient row lock, held through mailbox commit.

        Canon roles are reread after the lock, not reused from collection or
        preparation. Thread recording happens only after commit and explicitly
        in the finding's project, independently of the recipient's team.
        """
        from . import canon, fil, mail, sous_utilisation, work
        project = alert.get("project")
        if not project:
            return False
        with db.transaction() as tx:
            row = storage.of(tx).mailbox.lock_recipient(name)
            if row is None:
                return False
            canons = canon.load_configured(self.cfg) if self.cfg.canon else []
            orchestrators = sous_utilisation.role_orchestrators(canons)
            auditors = sous_utilisation.role_orchestrators(canons, alert_briefs.AUDITOR_ROLES)
            authorized = (name in orchestrators and row.get("chantier") == project
                          and name in (alert.get("orchestrators") or ())) \
                or (name in auditors and row.get("chantier") in (None, "", project))
            if not authorized:
                return False
            message_id = mail.send(tx, work.SYSTEM_SENDER, name, text, kind="event",
                                   payload=payload, thread=False)
        fil.record(self.cfg, db, sender=work.SYSTEM_SENDER, recipients=[name], text=text,
                   project=project, mailbox_ids=[message_id], meta={"kind": "event"})
        return True

    # -- résolution -----------------------------------------------------------
    def _resolved(self, key: str, entry: dict, now: float) -> None:
        if not self._delivery_open():
            return
        alert, human = entry["alert"], entry.get("human")
        status = entry.get("status")
        if alert.get("type") in QUIET_RESOLUTION_TYPES:
            # L124 : l'humain a répondu (ou la demande est retirée) : rien à envoyer
            self._record("resolved", entry, "sans_envoi", {})
            return
        if (alert.get("type") == "slo_breach" and human
                and human == normalize_human(self.ncfg.default_human)):
            # #2395 : une résolution de dépassement ne part pas non plus sur le
            # bureau de l'owner — y compris pour une entrée persistée par
            # l'ancienne politique, ou si son type a été configuré.
            self.log("alert resolved [%s] %s: owner notification suppressed (#2395)"
                     % (alert.get("type"), _subject(alert)))
            self._record("resolved", entry, "sans_envoi", {})
            return
        if status in ("acknowledged", "notifiee"):
            self._record("resolved", entry, "sans_envoi", {})
            return
        if status in ("sans_destinataire", "sans_canal", "nouvelle"):
            self.log("alert resolved [%s] %s (never sent: %s)" % (
                alert.get("type"), _subject(alert), _DELIVERY_LABELS.get(status, status)))
            self._record("resolved", entry, status, {})
            return
        if status == "resumee":
            bucket = self._bucket(human)
            self._adopt_summary_entry(entry, bucket)
            self._summary_add(bucket, "resolved", entry)
            return
        # #3536 : un repli livré n'est pas une livraison du primaire ; la
        # résolution ne repart pas sur le pseudo-canal `nexlink→ntfy`.
        delivered = [name for name, st in entry["channels"].items()
                     if st.get("ok") and not st.get("fallback_of")]
        if not delivered:
            self.log("alert [%s] %s resolved before any successful primary send: "
                     "nothing is sent" % (alert.get("type"), _subject(alert)))
            self._record("resolved", entry, "abandonnee", {})
            return
        resolved = {"alert": alert, "raised_ts": entry.get("raised_ts"), "resolved_ts": now,
                    "ack_subjects": entry.get("ack_subjects"),
                    "human": human, "source": entry.get("source"), "status": "envoi",
                    "channels": {name: {"ok": False, "attempts": 0, "error": None}
                                 for name in delivered}}
        if not self._allow(human, now):
            self._summary_add(self._bucket(human), "resolved", resolved)
            self._record("resolved", resolved, "resumee", {})
            return
        self._attempt(resolved, "resolved", now)
        # #3576 : garder l'entrée même quand l'envoi a réussi. Le passage
        # local tourne AVANT la lecture de l'état partagé ; c'est cette entrée
        # qui dit à `_merge_local_state` de retirer la clé de `active` du
        # document partagé (sinon la console calme la re-résout à chaque
        # tick). Le passage complet supprime une entrée soldée dans le même
        # tour (boucle `resolved` de `run_pass`), donc rien ne s'accumule.
        self.state["resolved"][key] = resolved

    # -- envoi ----------------------------------------------------------------
    def _pending(self, entry: dict) -> list:
        return [name for name, st in entry.get("channels", {}).items()
                if not st.get("ok") and st.get("attempts", 0) < self.ncfg.max_attempts]

    def _retry(self, entry: dict, event: str, now: float) -> None:
        if not self._delivery_open():
            return
        if entry.get("status") == "envoi" and self._pending(entry):
            self._attempt(entry, event, now)

    def _attempt(self, entry: dict, event: str, now: float) -> None:
        if not self._delivery_open():
            return
        message = render(entry["alert"], event, now, raised_ts=entry.get("raised_ts"))
        # #3536 : l'identité que porte le canal nexlink — le registre `notified`
        # (#2395) haché, donc stable d'un redémarrage à l'autre ; l'instant est
        # figé au premier essai pour que deux essais envoient les mêmes octets.
        # Un épisode sans `since` (`account_overage`) n'a pas d'identité au
        # registre : son `raised_ts`, figé dans l'entrée, distingue deux
        # occurrences successives sans changer l'identité des retries
        # (relecture cx3 B/P2).
        since = _since_of(entry["alert"])
        episode = since if since is not None else round(
            float(entry.get("raised_ts") or now), 3)
        identity = _notification_id(key_of(entry["alert"]), _entry_severity(entry),
                                    entry.get("human"), episode)
        occurred_at = entry.get("occurred_at") or iso_utc(now)
        entry["occurred_at"] = occurred_at
        message = replace(message,
                          alert_id=nexlink_alerts.alert_id_for(identity),
                          occurred_at=occurred_at)
        results = {}
        fallbacks = {}
        for name in self._pending(entry):
            st = entry["channels"][name]
            if st.get("fallback_of"):
                continue                # un repli : traité après les primaires
            channel = self.ncfg.channels.get(name)
            if channel is None:          # canal retiré de la configuration
                st.update(attempts=self.ncfg.max_attempts, error="channel removed")
                continue
            ok, error = self._send(channel, message)
            st["attempts"] = st.get("attempts", 0) + 1
            if ok:
                st.update(ok=True, error=None, ts=round(now, 3))
                results[name] = "a_blanc" if self.dry_run else "ok"
                continue
            if error.permanent:
                st["attempts"] = self.ncfg.max_attempts
            st["error"] = str(error)
            results[name] = "failed: %s" % error
            last = st["attempts"] >= self.ncfg.max_attempts
            self.log("[%s] %s → %s, channel %s: %s%s" % (
                entry["alert"].get("type"), _subject(entry["alert"]), entry.get("human"),
                name, error,
                " — GIVING UP (%d attempt(s))" % st["attempts"] if last
                else " — retried on next pass (%d/%d)"
                % (st["attempts"], self.ncfg.max_attempts)))
            if channel.kind == "nexlink":
                self._nexlink_fallback(entry, channel, message, st, error, fallbacks)
        # #3536 (relecture cx1 B/P1) : le repli garde son propre compteur et
        # son propre état `pending`. Il part dans le même passage (immédiat sur
        # erreur permanente) et il est retenté même quand le primaire est
        # épuisé — sinon un premier échec temporaire du repli perdait l'alerte.
        for name in [name for name in self._pending(entry)
                     if (entry["channels"].get(name) or {}).get("fallback_of")]:
            result = self._attempt_fallback(entry, name, message, now)
            if result is not None:
                results[name] = result
                fallbacks[entry["channels"][name].get("target") or name] = result
        if results:
            primary_ok = any(
                value == "ok" and not (entry["channels"].get(name) or {}).get("fallback_of")
                for name, value in results.items())
            delivery = ("a_blanc" if self.dry_run else
                        "envoyee" if any(v == "ok" for v in results.values()) else "echec")
            if event == "raised" and primary_ok:
                # #2395 : au moins un canal a livré — l'alerte est marquée au
                # registre AVANT le journal, pour qu'un arrêt juste après ne la
                # fasse pas repartir à la reprise. Un échec total n'est pas
                # marqué : les canaux restent retentés comme avant.
                # #3536 : un repli réussi ne compte pas — le primaire continue,
                # pour que la ligne durable de l'app existe quand l'API revient.
                self._mark_notified(entry, now)
            self._record(event, entry, delivery, results, message,
                         fallback=fallbacks or None)

    def _nexlink_fallback(self, entry: dict, channel: Channel, message: Message,
                          st: dict, error: ChannelError, out: dict) -> None:
        """#3536 : met le repli `ntfy`/`slack` du canal `nexlink` en attente.

        Déclenché au 2e échec consécutif ou tout de suite sur une erreur
        permanente ; une seule fois par alerte (registre `fallback`). Le texte
        enverra « repli nexlink — » ; le primaire reste retenté, et un repli
        réussi ne vaut pas livraison côté Nexlink.
        """
        name = channel.options.get("fallback")
        if not name:
            return
        target = self.ncfg.channels.get(str(name))
        if target is None or target.name == channel.name:
            return
        # #3536 (relecture dev4/cx1, cas max_attempts=1) : le seuil de
        # déclenchement ne doit pas dépasser le nombre d'essais configuré,
        # sinon un échec transitoire du dernier essai n'ouvre jamais le repli.
        threshold = min(2, self.ncfg.max_attempts)
        if not (error.permanent or int(st.get("attempts") or 0) >= threshold):
            return
        key = "%s→%s" % (channel.name, target.name)
        if key in entry.setdefault("channels", {}):
            return
        alert_id = message_alert_id(message)
        if alert_id in (self.state.get("fallback") or {}):
            out[target.name] = "déjà envoyé"
            return
        entry["channels"][key] = {"ok": False, "attempts": 0, "error": None,
                                  "fallback_of": channel.name,
                                  "target": target.name}
        out[target.name] = "en attente"

    def _attempt_fallback(self, entry: dict, name: str, message: Message,
                          now: float) -> str | None:
        """Envoie (ou annule) un repli en attente ; rend le résultat du journal."""
        if not self._delivery_open():
            return None
        st = entry["channels"][name]
        if st.get("ok"):
            return None
        primary = entry["channels"].get(str(st.get("fallback_of") or ""))
        if primary is not None and primary.get("ok"):
            # le primaire a fini par livrer : le repli n'a plus d'objet
            st.update(ok=True, error=None, cancelled=True)
            return "annulé"
        target = self.ncfg.channels.get(str(st.get("target") or ""))
        if target is None:
            st.update(attempts=self.ncfg.max_attempts,
                      error="fallback channel removed")
            return "failed: fallback channel removed"
        fallback = replace(message, title="[repli] " + message.title,
                           body="repli nexlink — " + message.body)
        ok, failure = self._send(target, fallback)
        st["attempts"] = st.get("attempts", 0) + 1
        if ok:
            st.update(ok=True, error=None, ts=round(now, 3))
            self.state.setdefault("fallback", {})[message_alert_id(message)] = \
                round(now, 3)
            self.log("[%s] %s: fallback %s sent (primary: %s)"
                     % (message.kind or "alert", _clean(message.title, 80),
                        target.name, _clean(primary.get("error") if primary else "", 120)))
            return "a_blanc" if self.dry_run else "ok"
        st["error"] = str(failure)
        if failure.permanent:
            st["attempts"] = self.ncfg.max_attempts
        last = st["attempts"] >= self.ncfg.max_attempts
        self.log("[%s] %s: fallback %s failed: %s%s"
                 % (message.kind or "alert", _clean(message.title, 80),
                    target.name, failure,
                    " — GIVING UP (%d attempt(s))" % st["attempts"] if last
                    else " — retried on next pass (%d/%d)"
                    % (st["attempts"], self.ncfg.max_attempts)))
        return "failed: %s" % failure

    def _send(self, channel: Channel, message: Message) -> tuple:
        if not self._delivery_open():
            # Dernier verrou : un chemin d'envoi ajouté plus tard ne peut pas
            # pousser depuis un détenteur périmé, même s'il a oublié son garde.
            return False, ChannelError("alerts delivery lease lost")
        try:
            self.sender.send(channel, message)
            return True, None
        except ChannelError as exc:
            return False, exc
        except Exception as exc:   # un canal ne fait jamais tomber les autres
            return False, ChannelError("unexpected error: %s"
                                       % _clean(_scrub(exc, channel_secrets(
                                           channel, getattr(self.sender, "env", None))),
                                           160))

    # -- débit ----------------------------------------------------------------
    def _allow(self, human: str, now: float) -> bool:
        limit = self.ncfg.rate_per_minute
        window = [t for t in self.state["rate"].get(human, []) if now - t < RATE_WINDOW_S]
        if limit and len(window) >= limit:
            self.state["rate"][human] = window
            return False
        window.append(now)
        self.state["rate"][human] = window
        return True

    # -- résumés de débit (#2395 : membres identifiés) -------------------------
    @staticmethod
    def _summary_member(entry: dict, human: str) -> dict:
        alert = entry.get("alert") or {}
        since = _since_of(alert)
        # #3536 (B/P2 cx3) : un épisode sans `since` (`account_overage`) n'a
        # pas d'identité au registre. Son `raised_ts`, figé dans l'entrée, doit
        # entrer dans l'identité du membre de résumé : sinon deux occurrences
        # successives partagent le même membre et la seconde est perdue quand
        # le premier épisode se clôt.
        episode = since
        if episode is None:
            raised = entry.get("raised_ts")
            try:
                episode = round(float(raised), 3) if raised is not None else None
            except (TypeError, ValueError):
                episode = None
        return {"key": entry.get("key") or key_of(alert),
                "severity": _entry_severity(entry),
                "type": alert.get("type"),
                "since": since,
                "episode": episode,
                "human": human}

    @staticmethod
    def _summary_member_id(member: dict) -> str:
        """L'identité d'un membre : clé, sévérité, humain et épisode — `since`
        quand il existe, sinon le `raised_ts` de secours (#3536 B/P2 cx3)."""
        episode = member.get("episode")
        if episode is None:
            episode = member.get("since")
        return _notification_id(member.get("key"), member.get("severity"),
                                member.get("human"), episode)

    def _owner_human(self, human) -> bool:
        owner = normalize_human(self.ncfg.default_human)
        return bool(owner) and normalize_human(human) == owner

    def _summary_forbidden(self, human, kind) -> bool:
        return kind == "slo_breach" and self._owner_human(human)

    def _legacy_summary_members(self, human, kind, count, exclude=()) -> list:
        """Membres d'un ancien compte `{type: n}`.

        Chaque compte est raccordé à une entrée active `resumee` du même
        humain et du même type quand le raccord est identifiable (autant
        d'entrées que de comptes) ; sinon l'identité reste synthétique : le
        compte est rendu, sans prétendre dédoublonner une alerte précise."""
        excluded = set(exclude)
        candidates = []
        for entry in (self.state.get("active") or {}).values():
            if not isinstance(entry, dict):
                continue
            if entry.get("human") != human or entry.get("status") != "resumee":
                continue
            if (entry.get("alert") or {}).get("type") != kind:
                continue
            member = self._summary_member(entry, human)
            if self._summary_member_id(member) not in excluded:
                candidates.append(member)
        candidates.sort(key=self._summary_member_id)
        members = list(candidates) if len(candidates) <= count else []
        for index in range(max(count - len(members), 0)):
            members.append({"key": "legacy:%s:%s:%d" % (human, kind, index),
                            "severity": None, "type": kind, "human": human})
        return members

    def _adopt_summary_entry(self, entry: dict, bucket: dict | None = None) -> None:
        """Raccorde un ancien compte de résumé à l'entrée qui se résout.

        L'entrée a déjà quitté `active` au moment de `_resolved` : ce raccord
        remplace une identité synthétique du même type par la vraie identité
        (clé, sévérité canonique, humain), pour que le résumé et le retour de
        l'alerte partagent le registre (#2395 B2, reprise legacy)."""
        human = entry.get("human")
        if not human:
            return
        if bucket is None:
            bucket = self._bucket(human)
        member = self._summary_member(entry, human)
        identity = self._summary_member_id(member)
        raised = bucket.get("raised") or {}
        if identity in raised:
            return
        kind = (entry.get("alert") or {}).get("type")
        for old_id, old in list(raised.items()):
            if (isinstance(old, dict) and old.get("type") == kind
                    and str(old.get("key") or "").startswith("legacy:")):
                del raised[old_id]
                raised[identity] = member
                return

    def _normalize_summary_raised(self, human, raised) -> dict:
        out = {}
        if not isinstance(raised, dict):
            return out
        legacy = {}
        for identity, member in raised.items():
            if isinstance(member, dict):
                normalized = {"key": member.get("key") or str(identity),
                              "severity": member.get("severity"),
                              "type": member.get("type"),
                              "since": member.get("since"),
                              "episode": member.get("episode"),
                              "human": human}
                if self._summary_forbidden(human, normalized["type"]):
                    self.log("owner rate summary: slo_breach member filtered (#2395)")
                    continue
                out[self._summary_member_id(normalized)] = normalized
                continue
            # Ancien format `{type: compte}` : les identités n'étaient pas
            # gardées. On tente de les raccorder aux entrées actives encore
            # présentes (même humain, même type) avant de synthétiser.
            kind = str(identity)
            count = int(member or 0)
            if self._summary_forbidden(human, kind):
                if count:
                    self.log("owner rate summary: %d slo_breach alert(s) filtered (#2395)"
                             % count)
                continue
            legacy[kind] = legacy.get(kind, 0) + count
        for kind, count in legacy.items():
            for normalized in self._legacy_summary_members(human, kind, count,
                                                           exclude=out):
                out[self._summary_member_id(normalized)] = normalized
        return out

    def _normalize_summary_resolved(self, human, resolved) -> dict:
        if isinstance(resolved, dict):
            out = {}
            for identity, member in resolved.items():
                if not isinstance(member, dict):
                    continue
                normalized = {"key": member.get("key") or str(identity),
                              "severity": member.get("severity"),
                              "type": member.get("type"),
                              "since": member.get("since"),
                              "episode": member.get("episode"),
                              "human": human}
                if self._summary_forbidden(human, normalized["type"]):
                    continue
                out[self._summary_member_id(normalized)] = normalized
            return out
        count = int(resolved or 0)
        if not count:
            return {}
        if self._owner_human(human):
            # Un compte de l'ancien format ne dit pas le type : impossible de
            # garantir qu'aucun slo_breach n'y figure (règle owner 18/10).
            self.log("owner rate summary: %d untyped resolution(s) filtered (#2395)"
                     % count)
            return {}
        out = {}
        for index in range(max(count, 0)):
            normalized = {"key": "legacy-resolved:%s:%d" % (human, index),
                          "severity": None, "type": None, "human": human}
            out[self._summary_member_id(normalized)] = normalized
        return out

    def _normalize_summary_bucket(self, human, bucket) -> dict:
        if not isinstance(bucket, dict):
            bucket = {}
        bucket["raised"] = self._normalize_summary_raised(human, bucket.get("raised"))
        bucket["resolved"] = self._normalize_summary_resolved(human, bucket.get("resolved"))
        bucket["attempts"] = int(bucket.get("attempts") or 0)
        if not isinstance(bucket.get("channels"), dict):
            bucket["channels"] = {}
        frozen = bucket.get("frozen")
        if isinstance(frozen, dict):
            for event in ("raised", "resolved"):
                if not isinstance(frozen.get(event), dict):
                    frozen[event] = {}
        elif "frozen" in bucket:
            bucket.pop("frozen", None)
        return bucket

    def _bucket(self, human: str) -> dict:
        bucket = self.state["summary"].get(human)
        if not isinstance(bucket, dict):
            bucket = {}
            self.state["summary"][human] = bucket
        return self._normalize_summary_bucket(human, bucket)

    def _summary_add(self, bucket: dict, event: str, entry: dict) -> None:
        human = entry.get("human")
        if not human:
            return
        member = self._summary_member(entry, human)
        if self._summary_forbidden(human, member.get("type")):
            self.log("[slo_breach] owner summary member refused (#2395)")
            if not bucket.get("raised") and not bucket.get("resolved"):
                self.state["summary"].pop(human, None)
            return
        bucket.setdefault(event, {})[self._summary_member_id(member)] = member

    def _mark_member_notified(self, member: dict, now: float) -> None:
        human = member.get("human")
        if not human or member.get("since") is None:
            return
        self.state.setdefault("notified", {})[
            self._summary_member_id(member)] = round(float(now), 3)

    def _mark_summary_notified(self, bucket: dict, now: float) -> None:
        for event in ("raised", "resolved"):
            for member in (bucket.get(event) or {}).values():
                if isinstance(member, dict):
                    self._mark_member_notified(member, now)

    def _member_already_notified(self, member: dict) -> bool:
        return self._summary_member_id(member) in (self.state.get("notified") or {})

    def _active_member_ids(self, human: str) -> set:
        ids = set()
        for entry in (self.state.get("active") or {}).values():
            if not isinstance(entry, dict) or entry.get("human") != human:
                continue
            member = self._summary_member(entry, human)
            ids.add(self._summary_member_id(member))
        return ids

    def _drop_notified_members(self, bucket: dict, human: str) -> None:
        """Coordonne un résumé en attente avec le registre (#2395 B2, relecture
        e828508e1) : un `raised` déjà livré — en direct dans cette passe ou par
        un résumé précédent — ne repart pas ; une résolution dont l'alerte est
        revenue (active) est dépassée et ne part pas non plus."""
        active = self._active_member_ids(human)
        frozen = bucket.get("frozen") or {}
        # Un membre de l'épisode figé peut avoir été livré en direct (ou son
        # alerte revenue) depuis le gel : l'épisode est annulé et ses autres
        # membres repartent sous une nouvelle identité, plutôt que de garder
        # un contenu périmé sous le même `alert_id`.
        stale = any(
            isinstance(member, dict) and self._member_already_notified(member)
            for member in (frozen.get("raised") or {}).values())
        stale = stale or any(identity in active
                             for identity in (frozen.get("resolved") or {}))
        if stale:
            self._summary_cancel_frozen(bucket)
        raised = bucket.get("raised") or {}
        for identity, member in list(raised.items()):
            if isinstance(member, dict) and self._member_already_notified(member):
                del raised[identity]
        resolved = bucket.get("resolved") or {}
        for identity in list(resolved):
            if identity in active:
                del resolved[identity]

    def _summary_alert_id(self, human: str, bucket: dict) -> str:
        """L'identité d'un résumé est celle de son épisode de bucket, pas celle
        de son texte (#3536, relecture cx3 B/P2) : deux résumés successifs
        « 1 dead_runner » restent deux lignes dans l'app. L'épisode et
        l'`alert_id` sont figés dans l'état, donc les retries envoient le même
        UUID et l'API #3537 les déduplique."""
        alert_id = bucket.get("alert_id")
        if isinstance(alert_id, str) and alert_id:
            return alert_id
        episode = bucket.get("episode")
        if not isinstance(episode, str) or not episode:
            episode = uuid.uuid4().hex
            bucket["episode"] = episode
        alert_id = nexlink_alerts.alert_id_for("summary|%s|%s" % (human, episode))
        bucket["alert_id"] = alert_id
        return alert_id

    @staticmethod
    def _summary_has_members(bucket: dict) -> bool:
        frozen = bucket.get("frozen") or {}
        return bool(bucket.get("raised") or bucket.get("resolved")
                    or frozen.get("raised") or frozen.get("resolved"))

    @staticmethod
    def _summary_clear_episode(bucket: dict) -> None:
        for key in ("frozen", "alert_id", "episode", "occurred_at", "channels"):
            bucket.pop(key, None)
        bucket["attempts"] = 0

    def _summary_freeze(self, bucket: dict) -> dict:
        """Fige les membres du résumé avant son premier envoi (#3536, relecture
        cx1) : un membre arrivé ensuite ne peut pas changer le contenu sous le
        même `alert_id`, sinon l'API #3537 déduplique le retry et le membre
        ajouté est perdu. Les nouveaux membres forment l'épisode suivant."""
        frozen = bucket.get("frozen")
        if isinstance(frozen, dict) and (frozen.get("raised") or frozen.get("resolved")):
            return frozen
        frozen = {"raised": dict(bucket.get("raised") or {}),
                  "resolved": dict(bucket.get("resolved") or {})}
        if not frozen["raised"] and not frozen["resolved"]:
            return frozen
        bucket["frozen"] = frozen
        for key in ("alert_id", "episode", "occurred_at", "channels"):
            bucket.pop(key, None)
        return frozen

    def _summary_finish_episode(self, bucket: dict, now: float, *,
                                notified: bool) -> None:
        """Clôt l'épisode figé : ses membres quittent le stock vivant ; ceux
        qui sont arrivés entre-temps restent pour l'épisode suivant."""
        frozen = bucket.get("frozen") or {}
        for event in ("raised", "resolved"):
            live = bucket.get(event) or {}
            for identity in (frozen.get(event) or {}):
                live.pop(identity, None)
        if notified:
            self._mark_summary_notified(frozen, now)
        self._summary_clear_episode(bucket)

    def _summary_cancel_frozen(self, bucket: dict) -> None:
        """Annule un épisode figé (membre acquitté) : ses autres membres
        repartent dans le stock vivant sous une nouvelle identité."""
        frozen = bucket.get("frozen") or {}
        for event in ("raised", "resolved"):
            live = bucket.setdefault(event, {})
            for identity, member in (frozen.get(event) or {}).items():
                live.setdefault(identity, member)
        self._summary_clear_episode(bucket)

    def _summaries(self, now: float) -> None:
        if not self._delivery_open():
            return
        for human in list(self.state["summary"]):
            bucket = self._bucket(human)
            self._drop_notified_members(bucket, human)
            if not self._summary_has_members(bucket):
                # Stock entièrement filtré (ancien résumé SLO owner, #2395 B1).
                del self.state["summary"][human]
                continue
            last = self.state["last_summary"].get(human)
            if last is not None and now - float(last) < RATE_WINDOW_S:
                continue
            channels = self.ncfg.channels_for(human)
            if not channels:
                self.log("rate summary for %s lost: no channel left" % human)
                del self.state["summary"][human]
                continue
            frozen = self._summary_freeze(bucket)
            alert_id = self._summary_alert_id(human, bucket)
            occurred_at = bucket.get("occurred_at") or iso_utc(now)
            bucket["occurred_at"] = occurred_at
            message = render_summary(frozen, self.cfg.host, alert_id=alert_id,
                                     occurred_at=occurred_at)
            states = bucket.setdefault("channels", {})
            for channel in channels:
                states.setdefault(channel.name, {"ok": False, "attempts": 0,
                                                 "error": None})
            entry = {"alert": {"type": "summary"}, "human": human,
                     "source": "debit", "channels": states}
            results, fallbacks = {}, {}
            primary_ok = False
            for channel in channels:
                state = states[channel.name]
                if (state.get("fallback_of") or state.get("ok")
                        or int(state.get("attempts") or 0) >= self.ncfg.max_attempts):
                    continue
                sent, error = self._send(channel, message)
                state["attempts"] = int(state.get("attempts") or 0) + 1
                if sent:
                    state.update(ok=True, error=None, ts=round(now, 3))
                    results[channel.name] = "a_blanc" if self.dry_run else "ok"
                    primary_ok = True
                    continue
                if error.permanent:
                    state["attempts"] = self.ncfg.max_attempts
                state["error"] = str(error)
                results[channel.name] = "failed: %s" % error
                self.log("rate summary → %s, channel %s: %s" % (human, channel.name,
                                                                error))
                if channel.kind == "nexlink":
                    # #3536 (relecture cx3 B/P1) : un résumé de débit passe par
                    # le même repli qu'une alerte directe. Sans cela, une API
                    # nexlink en panne perdait le résumé malgré un repli sain.
                    self._nexlink_fallback(entry, channel, message, state, error,
                                           fallbacks)
            for name in [name for name, state in states.items()
                         if state.get("fallback_of")]:
                state = states[name]
                if (state.get("ok")
                        or int(state.get("attempts") or 0) >= self.ncfg.max_attempts):
                    continue
                result = self._attempt_fallback(entry, name, message, now)
                if result is not None:
                    results[name] = result
                    fallbacks[state.get("target") or name] = result
            # Un primaire épuisé ne bloque plus le repli ; un repli en échec
            # temporaire est retenté même quand tous les primaires ont donné.
            primary_pending = any(
                not states[channel.name].get("ok")
                and int(states[channel.name].get("attempts") or 0)
                < self.ncfg.max_attempts
                for channel in channels)
            fallback_pending = any(
                state.get("fallback_of") and not state.get("ok")
                and int(state.get("attempts") or 0) < self.ncfg.max_attempts
                for state in states.values())
            self.state["rate"].setdefault(human, []).append(now)
            bucket["attempts"] = int(bucket.get("attempts") or 0) + 1
            if primary_ok or (not primary_pending and not fallback_pending):
                if not primary_ok:
                    self.log("rate summary → %s: GIVING UP after %d attempt(s)"
                             % (human, bucket["attempts"]))
                # #2395 B2 : les membres livrés par le résumé entrent au
                # registre, sinon la même alerte repart en envoi direct après
                # la fenêtre. Après succès du primaire seulement ; un repli
                # réussi ne vaut pas livraison du primaire.
                self._summary_finish_episode(bucket, now,
                                             notified=primary_ok and not self.dry_run)
                if not self._summary_has_members(bucket):
                    del self.state["summary"][human]
                # Les membres arrivés après le gel forment l'épisode suivant,
                # à la fenêtre de débit suivante.
                self.state["last_summary"][human] = now
            self._record("summary", entry,
                         "a_blanc" if self.dry_run else ("envoyee" if primary_ok else "echec"),
                         results, message, fallback=fallbacks or None)

    # -- journal ----------------------------------------------------------------
    def _record(self, event: str, entry: dict, delivery: str, channels: dict,
                message: Message | None = None,
                fallback: dict | None = None) -> None:
        alert = entry.get("alert") or {}
        record = {"schema": NOTIFY_SCHEMA, "event": event, "type": alert.get("type"),
                  "agent": alert.get("agent"), "lot": alert.get("lot"),
                  "host": alert.get("host"), "human": entry.get("human"),
                  "source": entry.get("source"), "delivery": delivery,
                  "channels": channels, "ts": round(float(self.clock()), 3)}
        if message is not None:
            record["title"], record["text"] = message.title, message.body
            # #3536 : l'identité poussée dans l'app (utile au `--json` et à la
            # qualification du canal)
            if message.alert_id:
                record["alert_id"] = message.alert_id
        if fallback:
            record["fallback"] = fallback
        self._records.append(record)
        self.emit(record)


def send_test(ncfg: NotifyConfig, human: str, host: str, sender=None,
              kinds: tuple | None = None) -> list:
    """`--test human:<id>` : un message par canal ; [(canal, None | erreur)].

    `kinds` restreint aux types de canal demandes (L66 : `doctor
    --notify-test` n'essaie que `desktop`, jamais ntfy ni Slack)."""
    sender = sender or Sender(timeout=ncfg.timeout)
    out = []
    for channel in ncfg.channels_for(human):
        if kinds and channel.kind not in kinds:
            continue
        try:
            sender.send(channel, render_test(human, channel, host))
            out.append((channel.name, None))
        except ChannelError as exc:
            out.append((channel.name, str(exc)))
        except Exception as exc:
            out.append((channel.name, "unexpected error: %s" % _clean(_scrub(
                exc, channel_secrets(channel, getattr(sender, "env", None))), 160)))
    return out


def desktop_human(ncfg: NotifyConfig) -> str | None:
    """L66 : le premier humain dont une route porte un canal `desktop`."""
    def porte(human: str) -> bool:
        return any(c.kind == "desktop" for c in ncfg.channels_for(human))

    if ncfg.default_human and porte(ncfg.default_human):
        return ncfg.default_human
    for human in sorted(ncfg.routes):
        if porte(human):
            return human
    return None


def desktop_probe(cfg, env: dict | None = None) -> tuple[bool, str]:
    """L66 : `doctor --notify-test` — le canal `desktop` de cet OS.

    Rend `(ok, ligne)`. N'envoie rien si aucune route ne porte de canal
    `desktop` : la ligne dit alors ce que cet OS utiliserait, et ce qui
    manque le cas échéant — sans KO, car personne n'a demandé d'affichage
    sur cet hôte (un binaire absent n'y est pas un défaut). Le KO est
    réservé à une route `desktop` qui existe et dont le backend manque ou
    échoue."""
    env = dict(os.environ if env is None else env)
    backend, binary, problem = platform_notify.describe(env=env)
    ou = "WSL2" if platform_notify.is_wsl(env) else platform_notify.SYSTEM
    try:
        ncfg = parse_config(getattr(cfg, "notify", None))
    except NotifyConfigError as exc:
        return False, "config refused: %s" % exc
    human = desktop_human(ncfg)
    if human is None:
        if problem:
            return True, "%s (%s) — unavailable: %s; no `desktop` route configured, nothing sent" % (
                backend or "desktop", ou, problem)
        return True, "%s (%s) — available; no `desktop` route configured, nothing sent" % (
            backend, ou)
    if problem:
        return False, "KO — %s" % problem
    results = send_test(ncfg, human, cfg.host,
                        sender=Sender(env=env, timeout=ncfg.timeout), kinds=("desktop",))
    errors = [detail for _name, detail in results if detail]
    if errors:
        return False, "%s (%s) — test to %s failed: %s" % (backend, ou, human, errors[0])
    if not results:
        return True, "%s (%s) — available; no `desktop` channel on %s's route" % (
            backend, ou, human)
    return True, "%s (%s) — test displayed to %s" % (backend, ou, human)


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

#: display labels of the stored/emitted `delivery` and `status` values
_DELIVERY_LABELS = {"a_blanc": "dry run", "envoyee": "sent", "echec": "failed",
                    "sans_destinataire": "no recipient", "sans_canal": "no channel",
                    "resumee": "summarized", "abandonnee": "abandoned",
                    "sans_envoi": "not sent", "prise": "taken", "nouvelle": "new",
                    "rendu": "returned to the queue", "envoi": "sending"}


def _format(record: dict) -> str:
    who = record.get("human") or "—"
    what = "[%s] %s" % (record.get("type"), _subject(record))
    event = {"raised": "raised", "resolved": "resolved", "summary": "summary",
             "auto_take": "auto-take", "budget_handoff": "budget handoff",
             "wip_return": "WIP return"}.get(
        record["event"], record["event"])
    channels = ", ".join("%s %s" % (k, v) for k, v in sorted(record["channels"].items()))
    delivery = record["delivery"]
    line = "%s %s → %s: %s%s" % (event, what, who, _DELIVERY_LABELS.get(
        delivery, delivery.replace("_", " ")), " (%s)" % channels if channels else "")
    if record.get("delivery") == "a_blanc" and record.get("title"):
        line += "\n    %s\n    %s" % (record["title"], record["text"].replace("\n", "\n    "))
    return line


class _Lock:
    """Un seul `ameesh notify` par état (sinon deux envois de chaque alerte)."""

    def __init__(self, path: str):
        self.path, self.fh = path, None

    def acquire(self) -> bool:
        import fcntl
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                self.fh.close()
                self.fh = None
                return False
            raise

    def release(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh notify", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true",
                        help="a single pass, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent; send nothing, write no state")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per send (schema ameesh-notify/1)")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between two passes (default: notify.interval, "
                             "AMEESH_NOTIFY_INTERVAL, else 30)")
    parser.add_argument("--test", metavar="human:ID", default=None,
                        help="send a test message on each of this human's channels")
    parser.add_argument("--only", action="append", metavar="PASS[,PASS]",
                        default=None,
                        help="L165: run only these passes (%s; repeatable, "
                             "comma-separated); requires --once. #3885: "
                             "`fingerprints` runs only under `ameesh role run`"
                             % ", ".join(ONLY_PASSES + ROLE_ONLY_PASSES))
    parser.add_argument("--skip", action="append", metavar="PASS[,PASS]",
                        default=None,
                        help="L165/#3327: exclude these mechanical passes from "
                             "a full pass (skippable: %s; repeatable, "
                             "comma-separated)" % ", ".join(SKIPPABLE_PASSES))
    exploitation.add_threshold_arguments(parser)
    # L119 (0037) : prise automatique dans la file d'amélioration
    from . import backlog, handoff
    parser.add_argument("--take-idle", type=float,
                        default=exploitation._env_float("AMEESH_TAKE_IDLE",
                                                        backlog.DEFAULT_TAKE_IDLE_S),
                        help="auto-take: wakeable agent idle with no lot for S seconds "
                             "(default 1800; 0 turns auto-take off)")
    parser.add_argument("--take-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_TAKE_MAX_PER_HOUR", backlog.DEFAULT_TAKE_MAX_PER_HOUR)),
                        help="maximum auto-takes per rolling hour, for the whole mesh "
                             "(default 2)")
    # #754 : la prise par un agent au token est acquise — après les forfaits,
    # jamais sous `balance_low` (owner, 13/10) ; l'ancien `--take-paid` reste
    # accepté, sans effet, pour les unités qui le passent encore.
    parser.add_argument("--take-paid", action="store_true", help=argparse.SUPPRESS)
    # L165 : prise d'un lot non assigné de l'équipe, avant la file
    parser.add_argument("--take-lot-idle", type=float,
                        default=exploitation._env_float("AMEESH_TAKE_LOT_IDLE",
                                                        backlog.DEFAULT_LOT_IDLE_S),
                        help="a wakeable agent idle with no lot for S seconds takes the "
                             "highest-priority unassigned lot of its team (default 900; 0 "
                             "turns this off)")
    parser.add_argument("--take-lot-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_TAKE_LOT_MAX_PER_HOUR", backlog.DEFAULT_LOT_MAX_PER_HOUR)),
                        help="maximum team lots taken per rolling hour, for the whole mesh "
                             "(default 6)")
    # #1738 (N3) : les propositions de relecture/correction (#1677) ont leur
    # propre interrupteur — 0 les coupe, la file d'amélioration reprend alors
    # tout le passage.
    parser.add_argument("--take-review-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_TAKE_REVIEW_MAX_PER_HOUR",
                            backlog.REVIEW_PROPOSAL_MAX_PER_HOUR)),
                        help="maximum review/correction proposals per rolling hour, for "
                             "the whole mesh (default 6; 0 turns them off)")
    # L233b : reprise des lots d'un agent Claude en pause budget
    parser.add_argument("--handoff-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_HANDOFF_MAX_PER_HOUR", handoff.DEFAULT_MAX_PER_HOUR)),
                        help="maximum lots returned to the queue or handed per rolling hour "
                             "from non-critical Claude agents paused on budget, the handed "
                             "ones to free DeepSeek or Codex agents of the same project "
                             "(default 4; 0 turns this off)")
    parser.add_argument("--handoff-idle", type=float,
                        default=exploitation._env_float("AMEESH_HANDOFF_IDLE",
                                                        handoff.DEFAULT_IDLE_S),
                        help="a DeepSeek or Codex agent takes a handed lot once idle for S "
                             "seconds (default 300)")
    # §4 (owner 14/10/2026) : balayage de la limite d'en-cours, coupé par
    # défaut — l'activation du balayage mutant vient après les portes et le
    # déploiement ; sans --wip-apply, le passage ne fait que relever.
    parser.add_argument("--wip-max-returns", type=int,
                        default=int(exploitation._env_float("AMEESH_WIP_MAX_RETURNS", 0)),
                        help="WIP sweep (§4): at most N surplus lots returned per pass "
                             "(default 0: off); without --wip-apply, dry run only")
    parser.add_argument("--wip-apply", action="store_true",
                        help="WIP sweep: actually return the lots to the team queue "
                             "(never with --dry-run)")
    return parser


def handoff_settings(args) -> dict | None:
    """Réglages de `handoff.budget_handoff` lus des options ; None si coupée."""
    most = int(getattr(args, "handoff_max_per_hour", 0) or 0)
    if most <= 0:
        return None
    return {"max_per_hour": most,
            "idle_s": max(0.0, float(getattr(args, "handoff_idle", 0) or 0))}


def wip_settings(args) -> dict | None:
    """§4 : réglages du balayage de la limite d'en-cours lus des options ; None
    si le balayage est coupé (le défaut). Voir `wip.notify_settings`."""
    from . import wip as wip_mod
    return wip_mod.notify_settings(args)


def take_settings(args) -> dict | None:
    """Réglages de `backlog.auto_take` lus des options ; None si les deux
    prises (file d'amélioration, lots de l'équipe) sont coupées — les
    propositions #1677 n'existent qu'avec une prise active, et
    `review_max_per_hour` à 0 les coupe en plus."""
    from . import backlog
    idle = float(getattr(args, "take_idle", 0) or 0)
    most = int(getattr(args, "take_max_per_hour", 0) or 0)
    lot_idle = float(getattr(args, "take_lot_idle", 0) or 0)
    lot_most = int(getattr(args, "take_lot_max_per_hour", 0) or 0)
    # #1738 (N3) : absent du namespace (appelant ancien), l'interrupteur reste
    # au défaut du produit — les propositions ne sont pas coupées par surprise.
    review_most = int(getattr(args, "take_review_max_per_hour",
                              backlog.REVIEW_PROPOSAL_MAX_PER_HOUR) or 0)
    if idle <= 0 or most <= 0:
        idle, most = 0.0, 0
    if lot_idle <= 0 or lot_most <= 0:
        lot_idle, lot_most = 0.0, 0
    # #1738 R1 (B2) : les propositions suivent une prise active — les deux
    # prises à 0 coupent toujours toute la prise automatique (comme avant).
    if not most and not lot_most:
        return None
    return {"idle_s": idle, "max_per_hour": most,
            "lot_idle_s": lot_idle, "lot_max_per_hour": lot_most,
            "review_max_per_hour": max(0, review_most)}


def _interval(args, ncfg: NotifyConfig) -> float:
    if args.interval is not None:
        return max(0.05, args.interval)
    if ncfg.interval is not None:
        return max(0.05, ncfg.interval)
    return max(0.05, exploitation._env_float("AMEESH_NOTIFY_INTERVAL", DEFAULT_INTERVAL_S))


def cmd_test(cfg: Config, ncfg: NotifyConfig, args) -> int:
    human = normalize_human(args.test)
    if human is None or not str(args.test).startswith("human:"):
        print("ameesh notify --test: `human:<id>` expected", file=sys.stderr)
        return 2
    if not ncfg.channels_for(human):
        print("ameesh notify --test: no channel for %s (notify.routes, notify.default) — "
              "this host cannot push anything to that human (no decision, no alert); "
              "declare the route on every candidate of the role that carries the "
              "alerts, then `ameesh role timers --install` (docs/EXPLOITATION.md, "
              "\"Alertes poussées\")" % human, file=sys.stderr)
        return 2
    sender = Sender(timeout=ncfg.timeout)
    results = send_test(ncfg, human, cfg.host, sender=sender)
    channels = {channel.name: channel for channel in ncfg.channels_for(human)}
    for name, error in results:
        receipt = None
        channel = channels.get(name)
        last = sender.last.get(name) or {}
        if error is None and channel is not None and channel.kind == "nexlink" \
                and last.get("alert_id"):
            # #3536 : l'accusé de lecture, une lecture (jamais un sondage) ;
            # son échec ne change pas le résultat de l'envoi.
            try:
                receipt = sender.receipt(channel, last["alert_id"])
            except ChannelError as exc:
                receipt = {"error": str(exc)}
        fallback = (fallback_readiness(ncfg, channel, sender.env)
                    if channel is not None else None)
        if args.json:
            record = {"schema": NOTIFY_SCHEMA, "event": "test", "human": human,
                      "channel": name, "ok": error is None, "error": error}
            if receipt is not None:
                record["receipt"] = receipt
            if fallback is not None:
                record["fallback"] = fallback
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        else:
            print("%s → %s: %s" % (name, human, "sent" if error is None
                                   else "FAILED: %s" % error))
            if receipt is not None:
                if "error" in receipt:
                    print("  receipt: unavailable (%s)" % _clean(receipt["error"], 120))
                else:
                    print("  receipt: accepted_at=%s pushed=%s read_at=%s"
                          % (receipt.get("accepted_at"), receipt.get("pushed"),
                             receipt.get("read_at")))
            if fallback is not None:
                print("  fallback %s: %s" % (
                    fallback["channel"],
                    "ready" if fallback["ready"] else
                    "MISSING: %s" % _clean(fallback.get("problem"), 120)))
    return 0 if all(error is None for _, error in results) else 1


def cmd_notify(cfg: Config, ncfg: NotifyConfig, args) -> int:
    try:
        only = _only_set(getattr(args, "only", None))
        skip = _skip_set(getattr(args, "skip", None))
    except ValueError as exc:
        print("ameesh notify: %s" % exc, file=sys.stderr)
        return 2
    if only is not None and not args.once:
        print("ameesh notify: --only requires --once", file=sys.stderr)
        return 2
    if only is not None and skip:
        print("ameesh notify: --only and --skip are exclusive", file=sys.stderr)
        return 2
    # #3327: a full service may skip the passes now carried by a role timer;
    # every other pass (take, budget handoff, WIP sweep) stays. #3383: with
    # `alerts` skipped, the local console pass and the state lock go too.
    #: L165: the alert delivery owns the local state file and its lock. The
    #: mechanical passes (`issues`, `github-watch`, `ci`) use database state
    #: only, so a role timer can run them beside the alert producer.
    alerts_pass = ("alerts" in only) if only is not None else not (skip and "alerts" in skip)
    if alerts_pass and not ncfg.configured:
        _stderr_log("no channel configured (`notify` key of the host configuration: "
                    "routes, default): alerts will be logged, not sent")
    path = state_path(cfg)
    lock = None
    if alerts_pass and not args.dry_run:
        lock = _Lock(path + ".lock")
        if not lock.acquire():
            print("ameesh notify: another `ameesh notify` already holds %s.lock" % path,
                  file=sys.stderr)
            return 1
    state = (load_state(path, log=_stderr_log, move_aside=not args.dry_run)
             if alerts_pass else empty_state())

    def emit(record):
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(_format(record), flush=True)

    notifier = Notifier(cfg, ncfg, state, dry_run=args.dry_run, emit=emit,
                        take=take_settings(args), handoff=handoff_settings(args),
                        wip=wip_settings(args), only=only, skip=skip)
    seuils = exploitation.thresholds(args)
    interval = _interval(args, ncfg)
    db = None
    lease = None
    try:
        while True:
            # #532 : la console du pool local se lit depuis `cfg` seule, avant
            # la base — c'est ce qui fait survivre l'alerte au refus qu'elle
            # signale, puisque la base passe par ce même pool.
            if alerts_pass:
                notifier.run_local_pass()
                if not args.dry_run:
                    save_state(path, notifier.state)
            try:
                if db is None:
                    db = db_mod.connect(cfg)
                    db_mod.require_schema(db)
                if alerts_pass:
                    # #3576 : l'état de livraison est partagé et un seul hôte
                    # livre. Sans le bail, ce passage se met en veille sans
                    # rien pousser ni écrire (l'autre hôte le fait).
                    lease, deliver = enter_shared_state(
                        db, notifier, cfg, dry_run=args.dry_run)
                    if not deliver:
                        # Le bail ne ferme QUE la livraison d'alertes : les
                        # autres passes (take, issues, github-watch, ci) de ce
                        # service gardent leur tour, selon sa sélection.
                        if only is not None:
                            rest = frozenset(only) - {"alerts"}
                        else:
                            rest = frozenset(ONLY_PASSES) - {"alerts"} - set(skip or ())
                        if rest:
                            notifier.run_pass(db, thresholds=seuils, only=rest)
                        if args.once:
                            return 0
                        time.sleep(interval)
                        continue
                notifier.run_pass(db, thresholds=seuils)
                if alerts_pass and not args.dry_run:
                    # #3576 : le document partagé d'abord (le bail peut
                    # expirer), le miroir local ensuite quoi qu'il arrive.
                    try:
                        leave_shared_state(db, notifier, lease, cfg=cfg)
                    finally:
                        lease = None
                        save_state(path, notifier.state)
            except db_mod.SchemaMissing:
                raise
            except db_mod.DbError as exc:
                if lease is not None:
                    # #3913 : la connexion de la passe vient de tomber ; le
                    # rendu tente une connexion neuve au lieu d'avaler l'échec.
                    notifier.lease = None
                    release_shared_state(db, lease, cfg=cfg, log=_stderr_log)
                    lease = None
                if args.once:
                    raise
                _stderr_log("database unavailable (%s): retrying in %ds" % (exc, interval))
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass
                    db = None
            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if lease is not None:
            notifier.lease = None
            release_shared_state(db, lease, cfg=cfg, log=_stderr_log)
        if db is not None:
            db.close()
        if lock is not None:
            lock.release()


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    cfg = config_mod.load()
    try:
        ncfg = parse_config(cfg.notify)
    except NotifyConfigError as exc:
        print("ameesh notify: invalid configuration: %s" % exc, file=sys.stderr)
        return 2
    if args.test:
        return cmd_test(cfg, ncfg, args)
    try:
        return cmd_notify(cfg, ncfg, args)
    except ValueError as exc:
        print("ameesh notify: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
