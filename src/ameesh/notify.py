# SPDX-License-Identifier: AGPL-3.0-only
"""Alerts pushed to the responsible human (lot L38, decision 0030 point 4).

  ameesh notify [--once] [--dry-run] [--json] [--interval S]
                [--long-turn S] [--idle-mail S] [--dead-grace S]
                [--session-tokens N] [--stale-lot S] [--orphan-lot S]
                [--take-idle S] [--take-max-per-hour N]
                [--take-lot-idle S] [--take-lot-max-per-hour N]
                [--handoff-max-per-hour N] [--handoff-idle S]
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
        L233b: before any take, the intake and build lots of a non-critical
        Claude agent paused on budget go to a free DeepSeek agent of the
        same project (`ameesh.handoff.budget_handoff`);
        `--handoff-max-per-hour 0` turns it off.
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

Reliability: the delivery state is kept in `<state>/notify/state.json` (a
lasting alert is not resent after a restart; an alert resolved while
stopped is sent on restart); rate limited per human (beyond that, a
summary); a failing channel does not block the others and is retried on
later passes, a bounded number of times.
"""
from __future__ import annotations

import argparse
import base64
import errno
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config as config_mod
from . import db as db_mod
from . import alert_briefs, exploitation, fil, sous_utilisation, storage
from .config import Config

NOTIFY_SCHEMA = "ameesh-notify/1"
STATE_SCHEMA = "ameesh-notify-state/1"

#: L38 (0030) : types envoyés par défaut. `delegation_expired` arrive avec L40 :
#: il est accepté ici sans dépendre de son code (un type absent n'est jamais
#: levé, c'est tout).
#: L94 : la sous-utilisation (forfait perdu, agents au repos, orchestrateur
#: tenu, hôte à vide) est poussée comme la surcharge.
DEFAULT_TYPES = ("stopped_with_mail", "orphan_lot", "dead_runner", "idle_with_mail",
                 "delegation_expired", "engagement_overdue", "plan_underused",
                 "idle_capacity", "orchestrator_held", "host_underused", "balance_low",
                 "host_not_ready", "host_power_low",
                 "host_unavailable", "host_drain_overdue", "lease_not_taken", "backlog_empty", "mail_undeliverable",
                 # L124 : une demande de décision attend le propriétaire, et sa
                 # relance à l'échéance (urgentes si la demande l'est)
                 "decision_pending", "decision_overdue",
                 # L161c : la CI de main est rouge (elle bloque les déploiements)
                 "ci_main_red",
                 # #747 : la CI attend un runner depuis plus de 20 min
                 # #246 : btrfs presque plein côté allocation (df ne le voit pas)
                 "host_disk_low",
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
                 # #330: an owner key registered or replaced lately
                 "owner_key_registered",
                 # L160e2: a merge queue no longer moves
                 "merge_blocked",
                 # #194 r6: a PR merged into a wrong base, or not checked
                 "land_uncertain", "db_connections_high",
                 # #513 : la dépense comptée s'écarte des soldes (barème faux)
                 "budget_drift")
CHANNEL_KINDS = ("desktop", "ntfy", "slack")
DEFAULT_RATE_PER_MINUTE = 10
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_INTERVAL_S = 30.0
#: fenêtre de la limite de débit, et intervalle minimal entre deux résumés
RATE_WINDOW_S = 60.0
DEFAULT_SLACK_ENV = "AMEESH_SLACK_WEBHOOK"

#: clés qui porteraient un secret EN CLAIR dans la configuration : refusées
SECRET_KEYS = ("token", "password", "secret", "webhook", "webhook_url", "authorization",
               "auth", "key", "api_key")
#: options admises par type de canal (une faute de frappe est une erreur)
CHANNEL_OPTIONS = {
    "desktop": ("type", "timeout"),
    "ntfy": ("type", "timeout", "url", "topic", "token_env", "token_file"),
    "slack": ("type", "timeout", "webhook_env", "webhook_file"),
}
TOP_KEYS = ("default_human", "types", "routes", "default", "channels", "rate_per_minute",
            "max_attempts", "interval", "timeout")

#: libellés lisibles des types (R12 : un humain lit ces messages)
TYPE_LABELS = {
    "db_connections_high": "database connection capacity",
    "stopped_with_mail": "agent stopped with mail",
    "orphan_lot": "orphan lot",
    "dead_runner": "dead runner",
    "idle_with_mail": "agent idle with mail",
    "delegation_expired": "delegation expired",
    "engagement_overdue": "commitment or due date passed",
    "long_turn": "long turn",
    "session_too_big": "session too big",
    "stale_lot": "stale lot",
    "host_pressure": "host under pressure",
    "orphan_resource": "orphan resource",
    "tmpfs_full": "tmpfs almost full",
    "worktree_kept": "worktree kept at end of lot",
    "tmp_orphan": "/tmp entry left by a turn",
    "tmp_unremovable": "temporary directory over quota with files it cannot remove",
    "tmp_unscanned": "temporary directory too large to scan; nothing evicted",
    "plan_underused": "underused plan",
    "idle_capacity": "idle capacity",
    "orchestrator_held": "orchestrator held by a session",
    "host_underused": "host underused",
    "balance_low": "low balance",
    "host_not_ready": "host not ready",
    "auth_blocked": "authentication failing",
    "credential_failing": "credential failing",
    "credential_expiring": "credential expiring soon",
    "host_power_low": "host battery low",
    "backlog_empty": "improvement backlog empty",
    "mail_undeliverable": "undeliverable mail",
    "decision_pending": "decision pending",
    "decision_overdue": "decision unanswered by its deadline",
    "host_unavailable": "host unavailable",
    "lease_not_taken": "runner cannot take its lease",
    "host_drain_overdue": "host drain overdue",
    # L125 : mesure de l'effet du regroupement du courrier ; pas poussée par
    # défaut (à ajouter dans `notify.types` pour la recevoir)
    "turn_churn": "bursts of short turns",
    # L157b : adressée aux agents (alert_briefs) ; pas poussée aux humains par
    # défaut (à ajouter dans `notify.types` pour la recevoir)
    "estimate_bias": "estimate bias",
    "ci_main_red": "CI red on main",
    "deep_analysis_overdue": "deep analysis without a report in time",
    "host_disk_low": "btrfs disk almost full",
    "docker_orphan_volumes": "Docker orphan volumes accumulating",
    "docker_build_cache": "Docker reclaimable build cache above threshold",
    "docker_storage_unavailable": "Docker storage inventory unavailable",
    "turn_unpriced": "turn without a known price",
    # L233: briefed to the assignee and the orchestrators (alert_briefs); not
    # pushed to humans by default (add it to `notify.types` to receive it)
    "waiting_without_question": "lot waiting for the owner with no question asked",
    # L159e6: not pushed by default (add it to `notify.types`)
    "guard_restart": "agents started again by the capacity guard",
    "merge_blocked": "merge queue blocked",
    "land_uncertain": "PR merged by ameesh into a wrong or unchecked base",

    "owner_key_registered": "owner key registered",
    "module_deviation": "module procedure deviation",
    "critical_budget_pause": "critical agent paused on budget",
    # #254: not pushed by default (add it to `notify.types`)
    "host_hold": "host on hold",
    "budget_drift": "counted spend strays from the provider balance",
}
#: types urgents : notification critique (bureau), priorité haute (ntfy)
URGENT_TYPES = ("stopped_with_mail", "orphan_lot", "dead_runner", "delegation_expired",
                "mail_undeliverable", "ci_main_red", "auth_blocked",
                "credential_failing", "critical_budget_pause", "owner_key_registered",
                "land_uncertain", "db_connections_high")
#: L124 : types dont la résolution n'est pas envoyée — une décision se résout
#: parce que l'humain y a répondu (ou que le demandeur l'a retirée) : le lui
#: redire serait du bruit. La résolution est journalisée.
QUIET_RESOLUTION_TYPES = ("decision_pending", "decision_overdue")

_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class NotifyConfigError(ValueError):
    """Clé `notify` de la configuration invalide (ou porteuse d'un secret)."""


class ChannelError(RuntimeError):
    """Échec d'envoi sur un canal. `permanent` : inutile de retenter tel quel
    (binaire absent, secret manquant) — journalisé une fois, pas de retentative."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


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
    # un canal nommé par son type (desktop, slack) n'a pas besoin d'être déclaré
    for names in list(routes.values()) + [default]:
        for name in names:
            if name in channels:
                continue
            if name in ("desktop", "slack"):
                channels[name] = _channel(name, {})
            elif name == "ntfy":
                raise NotifyConfigError("ntfy channel used without declaration: "
                                        "notify.channels.ntfy = {\"url\": …, \"topic\": …}")
            else:
                raise NotifyConfigError("channel %r used in routes but not declared "
                                        "(notify.channels)" % name)
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

    @property
    def text(self) -> str:
        return "%s\n%s" % (self.title, self.body)


def _clean(text, limit: int = 300) -> str:
    """Une ligne de texte : sans caractère de contrôle, bornée."""
    text = _CONTROL_RE.sub(" ", str(text or "")).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def local_time(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


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


def _subject(alert: dict) -> str:
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
        lot = "lot #%s" % alert["lot"]
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
    kind = str(alert.get("type") or "?")
    if kind == "orphan_lot" and alert.get("grouped"):
        subject = "agent %s" % _clean(alert.get("agent") or "unknown", 64)
        title = "ameesh: %sorphan lots (%s)" % (
            "resolved — " if event == "resolved" else "", subject)
        text = "[orphan_lot] %s\n%s" % (subject, _clean(alert.get("detail"), GROUPED_DETAIL_LIMIT))
        return Message(title, text, low=event == "resolved", urgent=event != "resolved")
    label = TYPE_LABELS.get(kind, kind.replace("_", " "))
    subject = _subject(alert)
    since = alert.get("since")
    start = float(since) if since is not None else (float(raised_ts) if raised_ts else None)
    who = alert.get("agent") or alert.get("host") or _account(alert) or (
        "lot #%s" % alert["lot"] if alert.get("lot") is not None else "")
    suffix = " (%s)" % _clean(who, 64) if who else ""
    if event == "resolved":
        title = "ameesh: resolved — %s%s" % (label, suffix)
        lines = ["[%s] %s" % (kind, subject)]
        if start is not None:
            lines.append("resolved after %s (raised on %s)" % (duration(now - start),
                                                               local_time(start)))
        else:
            lines.append("resolved")
        return Message(title, "\n".join(lines), low=True)
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
    return Message(title, body, urgent=kind in URGENT_TYPES or bool(alert.get("urgent")))


def render_summary(bucket: dict, host: str) -> Message:
    raised = bucket.get("raised") or {}
    total = sum(raised.values()) + int(bucket.get("resolved") or 0)
    parts = []
    if raised:
        parts.append("raised: " + ", ".join(
            "%d %s" % (n, kind) for kind, n in sorted(raised.items())))
    if bucket.get("resolved"):
        parts.append("resolved: %d" % int(bucket["resolved"]))
    title = "ameesh: %d alert(s) not detailed (rate limit)" % total
    # L94 : la sous-utilisation a sa ligne dans le résumé
    underuse = sum(n for kind, n in raised.items() if kind in sous_utilisation.UNDERUSE_TYPES)
    if underuse:
        parts.append("underuse: %d (%s)" % (underuse, ", ".join(
            "%d %s" % (n, TYPE_LABELS.get(kind, kind)) for kind, n in sorted(raised.items())
            if kind in sous_utilisation.UNDERUSE_TYPES)))
    body = "%s\nsee `ameesh alerts` on host %s" % ("; ".join(parts) or "—", host)
    return Message(title, body, urgent=any(k in URGENT_TYPES for k in raised))


def render_test(human: str, channel: Channel, host: str) -> Message:
    return Message("ameesh: test message",
                   "Channel %s (%s) of %s, from host %s: operations alerts will "
                   "reach you here." % (channel.name, channel.kind, human, host))


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


def _post(url: str, data: bytes, headers: dict, timeout: float, *, show_url: bool,
          secrets: tuple = ()) -> None:
    """`show_url=False` : l'URL est un secret — aucun message d'erreur ne la
    contient (L46 : message fixe pour une URL invalide, et tout texte venu
    d'une exception est nettoyé). `secrets` : autres valeurs à ne jamais
    laisser passer (jeton d'un en-tête)."""
    hidden = tuple(secrets) + (() if show_url else (url,))

    def clean(text) -> str:
        return _clean(_scrub(str(text), hidden), 160)

    host = urllib.parse.urlsplit(url).hostname or ""
    handlers = [urllib.request.ProxyHandler({})] if host in _LOOPBACK else []
    opener = urllib.request.build_opener(*handlers)
    where = (" (%s)" % url) if show_url else ""
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with opener.open(request, timeout=timeout) as response:
            response.read(4096)
    except urllib.error.HTTPError as exc:
        try:
            extrait = clean(exc.read(300).decode("utf-8", "replace"))
        except OSError:
            extrait = ""
        finally:
            exc.close()
        raise ChannelError("HTTP %d%s%s" % (exc.code, where,
                                           ": %s" % extrait if extrait else "")) from None
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


def _header(value: str) -> str:
    """En-tête HTTP : ASCII tel quel, sinon encodage RFC 2047 (compris par ntfy)."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?%s?=" % base64.b64encode(value.encode("utf-8")).decode("ascii")


def notify_send_bin(env: dict) -> str | None:
    """`AMEESH_NOTIFY_SEND_BIN` (explicite : sans repli), puis
    `AMEESH_BIN_DIR/notify-send`, puis le PATH — comme `gh` et les harnais."""
    explicit = env.get("AMEESH_NOTIFY_SEND_BIN")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    bin_dir = env.get("AMEESH_BIN_DIR") or env.get("AGENT_MESH_BIN_DIR")
    if bin_dir:
        candidate = os.path.join(bin_dir, "notify-send")
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("notify-send", path=env.get("PATH") or os.defpath)


class Sender:
    """Envoie un message sur un canal ; lève `ChannelError` en cas d'échec."""

    def __init__(self, env: dict | None = None, timeout: float = DEFAULT_TIMEOUT_S):
        self.env = dict(os.environ if env is None else env)
        self.timeout = timeout

    def send(self, channel: Channel, message: Message) -> None:
        timeout = float(channel.options.get("timeout") or self.timeout)
        getattr(self, "_" + channel.kind)(channel, message, timeout)

    def _desktop(self, channel: Channel, message: Message, timeout: float) -> None:
        binary = notify_send_bin(self.env)
        if not binary:
            raise ChannelError(
                "notify-send not found on this host (libnotify package): the `desktop` "
                "channel cannot show anything; install it or remove `desktop` from the "
                "routes", permanent=True)
        urgency = "critical" if message.urgent else ("low" if message.low else "normal")
        try:
            proc = subprocess.run(
                [binary, "--app-name=ameesh", "--urgency=%s" % urgency,
                 message.title, message.body],
                capture_output=True, text=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise ChannelError("notify-send did not answer within %ds" % timeout) from None
        except OSError as exc:
            raise ChannelError("notify-send: %s" % exc.strerror) from None
        if proc.returncode != 0:
            raise ChannelError("notify-send failed (code %d): %s" % (
                proc.returncode, _clean(proc.stderr, 160) or "no message"))

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
    lectures — registre, lots, paquets, canon — sont faites au plus une fois)."""

    def __init__(self, cfg: Config, db, ncfg: NotifyConfig):
        self.cfg, self.db, self.ncfg = cfg, db, ncfg
        self._agents = None
        self._canon = None
        self._canon_read = False

    def agents(self) -> dict:
        if self._agents is None:
            self._agents = {row["name"]: row
                            for row in storage.of(self.db).operations.listing()}
        return self._agents

    def agent_responsible(self, name) -> str | None:
        if not name:
            return None
        return (self.agents().get(name) or {}).get("responsible")

    def lot_responsible(self, lot) -> str | None:
        if lot is None:
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
        """(humain, source) ; source ∈ alerte, agent, lot, hote, defaut ; (None, None)."""
        if alert.get("type") == "module_deviation":
            # A project procedure finding belongs to the lot, even when its
            # assignee (or their host/responsible human) belongs elsewhere.
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
                return human, source
        return None, None


# --------------------------------------------------------------------------
# état persistant
# --------------------------------------------------------------------------

def state_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "notify", "state.json")


def empty_state() -> dict:
    return {"schema": STATE_SCHEMA, "active": {}, "resolved": {}, "rate": {},
            "summary": {}, "last_summary": {}, "briefs": {}}


def load_state(path: str, log=None, *, move_aside: bool = True) -> dict:
    """L'état d'envoi ; absent = vide. Illisible : mis de côté (journalisé),
    on repart vide — au pire une alerte en cours est renvoyée une fois.
    `move_aside=False` (`--dry-run`) : rien n'est déplacé."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
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
    state = empty_state()
    state.update({k: v for k, v in data.items() if k in state and isinstance(v, dict)})
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


def key_of(alert: dict) -> str:
    """Clé de dédoublonnage : celle d'`ameesh alerts --follow`, en texte."""
    key = list(exploitation.alert_key(alert))
    if alert.get("type") == "orphan_lot" and alert.get("grouped"):
        key.append(alert.get("responsible"))
    return json.dumps(key, ensure_ascii=False)


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
                   "detail": "%d orphan lot(s) (%s): %s%s" % (
                       len(lots), _clean(reason or "unknown", 64), names, more)}
        out.append(grouped)
    return out


#: champs d'une alerte gardés dans l'état (de quoi écrire la résolution)
_KEPT = ("module", "rule", "project", "declaration_id", "subject", "action",
         "type", "agent", "lot", "title", "host", "since", "detail", "reason", "value",
         "responsible", "stop_reason", "assignee", "harness", "account", "gauge",
         "provider", "currency", "urgent",
         # L96 : `engagement_overdue` (la clé de dédoublonnage les lit)
         "commitment", "package", "due",
         # L124 : `decision_pending`, `decision_overdue`
         "decision", "grouped")


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
                 take: dict | None = None, handoff: dict | None = None):
        self.cfg, self.ncfg = cfg, ncfg
        #: L119 : réglages de la prise automatique (`backlog.auto_take`) ; None la coupe
        self.take = take
        #: L233b : réglages de la reprise sur pause budget
        #: (`handoff.budget_handoff`) ; None la coupe
        self.handoff = handoff
        self.state = state if state is not None else empty_state()
        self.dry_run = dry_run
        self.sender = sender or (DrySender() if dry_run else Sender(timeout=ncfg.timeout))
        self.emit = emit or (lambda record: None)
        self.log = log or _stderr_log
        self.clock = clock or time.time
        #: L126 : projection des lots en issues GitHub (créée au premier passage)
        self._projector = None
        #: L161c : relevé de la CI, échecs au courrier de l'agent du lot
        self._ci_watcher = None

    # -- passage --------------------------------------------------------------
    def run_pass(self, db, current: list | None = None, thresholds: dict | None = None,
                 ) -> list:
        now = float(self.clock())
        if current is None:
            current = exploitation.alerts(self.cfg, db, now=now, **(thresholds or {}))
        records: list = []
        self._records = records
        router = Router(self.cfg, db, self.ncfg)
        notifications = group_orphan_lots(current, router.resolve)
        wanted = {key_of(a): a for a in notifications if a.get("type") in self.ncfg.types}
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
            entry = active.pop(key)
            if entry["alert"].get("type") not in self.ncfg.types:
                self.log("[%s] removed from notify.types: forgotten without resolution"
                         % entry["alert"].get("type"))
                continue
            self._resolved(key, entry, now)
        for key, alert in wanted.items():
            entry = active.get(key)
            if entry is None:
                entry = active[key] = {
                    "alert": {k: alert.get(k) for k in _KEPT if alert.get(k) is not None},
                    "raised_ts": now, "human": None, "source": None, "status": "nouvelle",
                    "channels": {}}
                if alert.get("type") in exploitation.ESCALATING:
                    entry["escalated"] = bool(alert.get("critical"))
                self._raise(entry, router, now)
                if alert.get("type") not in alert_briefs.BRIEFED_TYPES:
                    self._brief_orchestrators(db, alert, router)
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
        self._brief_agents(db, current, router, now)
        for key in list(self.state["resolved"]):
            entry = self.state["resolved"][key]
            self._retry(entry, "resolved", now)
            if not self._pending(entry):
                del self.state["resolved"][key]
        self._summaries(now)
        if self.handoff:
            self._budget_handoff(db, now)
        if self.take:
            self._auto_take(db, current, now)
        self._github_issues(db, now)
        self._ci_failures(db)
        for human in list(self.state["rate"]):
            window = [t for t in self.state["rate"][human] if now - t < RATE_WINDOW_S]
            if window:
                self.state["rate"][human] = window
            else:
                del self.state["rate"][human]
        return records

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
                     "source": "lot de l'équipe" if take.get("kind") == "lot"
                     else "file d'amélioration"}
            self._record("auto_take", entry, "a_blanc" if self.dry_run else "prise", {})

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
            entry = {"alert": {"type": "budget_handoff", "agent": move["agent"],
                               "lot": int(item["id"]), "title": item.get("title")},
                     "human": None, "source": "pause budget de %s" % move["previous"]}
            self._record("budget_handoff", entry, "a_blanc" if self.dry_run else "prise", {})

    # -- levée ----------------------------------------------------------------
    def _raise(self, entry: dict, router: Router, now: float) -> None:
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
        if not self._allow(human, now):
            entry["status"] = "resumee"
            bucket = self._bucket(human)
            bucket["raised"][alert["type"]] = bucket["raised"].get(alert["type"], 0) + 1
            self._record("raised", entry, "resumee", {})
            return
        entry["status"] = "envoi"
        entry["channels"] = {c.name: {"ok": False, "attempts": 0, "error": None}
                             for c in channels}
        self._attempt(entry, "raised", now)

    # -- orchestrateurs (L118) ------------------------------------------------
    def _brief_orchestrators(self, db, alert: dict, router: Router) -> None:
        """`idle_capacity` levée : l'orchestrateur de chaque projet reçoit, par
        courrier `event`, ses agents au repos sans lot et les lots ouverts sans
        agent — c'est lui qui peut répartir. `mail_undeliverable` levée : les
        orchestrateurs de l'équipe reçoivent le courrier en souffrance et le
        geste (`ameesh mail forward`). Une fois par levée (même dédoublonnage
        que les canaux) ; jamais fatal au passage."""
        from . import undeliverable
        try:
            if alert.get("type") == "mail_undeliverable":
                briefs = undeliverable.orchestrator_briefs(alert)
            else:
                listing = list(router.agents().values())
                titles = {}
                for lot in (alert.get("lots") or ())[:12]:
                    item = storage.of(db).work.get(int(lot))
                    if item:
                        titles[int(lot)] = item.get("title") or ""
                briefs = sous_utilisation.orchestrator_briefs(alert, listing, titles)
        except Exception as exc:  # noqa: BLE001 - l'alerte part quand même
            self.log("mail to orchestrators not prepared (%s)" % exc)
            return
        from . import mail, work
        for orch, text in sorted(briefs.items()):
            if self.dry_run:
                self.log("[%s] mail to orchestrator %s (dry run): %s"
                         % (alert.get("type"), orch, text))
                continue
            try:
                payload = {"alert": alert.get("type"),
                           "agents": list(alert.get("agents") or ()),
                           "lots": list(alert.get("lots") or ())}
                if alert.get("type") == "mail_undeliverable":
                    payload.update(recipient=alert.get("agent"), reason=alert.get("reason"),
                                   count=alert.get("value"))
                mail.send(db, work.SYSTEM_SENDER, orch, text, kind="event", payload=payload)
                self.log("[%s] mail delivered to orchestrator %s"
                         % (alert.get("type"), orch))
            except Exception as exc:  # noqa: BLE001
                self.log("[%s] mail to orchestrator %s not delivered (%s)"
                         % (alert.get("type"), orch, exc))

    # -- agents qui peuvent agir (L132) ---------------------------------------
    def _brief_agents(self, db, current: list, router: Router, now: float) -> None:
        """`idle_capacity`, `stale_lot`, `plan_underused` : courrier `event` aux
        orchestrateurs et à l'auditeur, avec l'action attendue
        (`alert_briefs` ; `stale_lot` excepté pour l'auditeur, L713 : il le lit
        à sa passe, `ameesh alerts --json`). Une fois à la levée, une relance
        si l'alerte dure `REMIND_AFTER_S` ; indépendant de `notify.types` (qui
        ne règle que l'humain). Jamais fatal au passage."""
        sent = self.state["briefs"]
        alive = {key_of(a): a for a in current if a.get("type") in alert_briefs.BRIEFED_TYPES}
        for key in list(sent):
            if key not in alive:
                del sent[key]
        due = []
        for key, alert in alive.items():
            entry = sent.get(key)
            if entry is None:
                due.append((key, alert, None))
            elif not entry.get("reminded") and now - float(entry["sent_ts"]) \
                    >= alert_briefs.REMIND_AFTER_S:
                due.append((key, alert, entry))
        if not due:
            return
        from . import mail, work
        try:
            listing = storage.of(db).operations.listing()
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
        for key, alert, entry in due:
            kind = alert.get("type")
            try:
                titles = {}
                for lot in (alert.get("lots") or ())[:12]:
                    item = storage.of(db).work.get(int(lot))
                    if item:
                        titles[int(lot)] = item.get("title") or ""
                briefs = alert_briefs.briefs(
                    alert, listing, titles,
                    module_auditors if kind == "module_deviation" else auditors,
                    orchestrator_names=module_orchestrators)
            except Exception as exc:  # noqa: BLE001 - l'alerte part quand même
                self.log("[%s] mail to agents not prepared (%s)" % (kind, exc))
                continue
            if not briefs:
                continue        # personne à prévenir : on réessaie au passage suivant
            if entry is not None:
                raised = float(entry["sent_ts"])
                briefs = {n: alert_briefs.reminder(t, raised, now) for n, t in briefs.items()}
            payload = {"alert": kind, "reminder": entry is not None}
            if kind in URGENT_TYPES:
                payload["urgent"] = True        # L242b (5) : le point de l'auditeur
            for field_ in ("module", "rule", "project", "declaration_id", "subject",
                           "lot", "agents", "lots", "harness", "account", "gauge",
                           "idle_agents"):
                if alert.get(field_) is not None:
                    payload[field_] = alert[field_]
            # pas de `work_item_id` : un message lié au lot compterait comme
            # son activité (stagnation) et effacerait l'alerte qu'il signale
            delivered = []
            for name, text in sorted(briefs.items()):
                if self.dry_run:
                    self.log("[%s] mail to %s (dry run): %s" % (kind, name, text))
                    continue
                try:
                    if kind == "module_deviation":
                        if not self._send_module_brief(db, alert, name, text, payload):
                            continue
                    else:
                        mail.send(db, work.SYSTEM_SENDER, name, text, kind="event",
                                  payload=payload)
                    delivered.append(name)
                    self.log("[%s] %s delivered to %s" % (
                        kind, "reminder" if entry is not None else "mail", name))
                except Exception as exc:  # noqa: BLE001
                    self.log("[%s] mail to %s not delivered (%s)" % (kind, name, exc))
            if kind == "module_deviation" and not delivered:
                continue
            if entry is None:
                sent[key] = {"type": kind, "sent_ts": now, "reminded": False,
                             "to": delivered if kind == "module_deviation" else sorted(briefs)}
            else:
                entry["reminded"] = True

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
        alert, human = entry["alert"], entry.get("human")
        status = entry.get("status")
        if alert.get("type") in QUIET_RESOLUTION_TYPES:
            # L124 : l'humain a répondu (ou la demande est retirée) : rien à envoyer
            self._record("resolved", entry, "sans_envoi", {})
            return
        if status in ("sans_destinataire", "sans_canal", "nouvelle"):
            self.log("alert resolved [%s] %s (never sent: %s)" % (
                alert.get("type"), _subject(alert), _DELIVERY_LABELS.get(status, status)))
            self._record("resolved", entry, status, {})
            return
        if status == "resumee":
            self._bucket(human)["resolved"] += 1
            return
        delivered = [name for name, st in entry["channels"].items() if st.get("ok")]
        if not delivered:
            self.log("alert [%s] %s resolved before any successful send: nothing is sent"
                     % (alert.get("type"), _subject(alert)))
            self._record("resolved", entry, "abandonnee", {})
            return
        resolved = {"alert": alert, "raised_ts": entry.get("raised_ts"), "resolved_ts": now,
                    "human": human, "source": entry.get("source"), "status": "envoi",
                    "channels": {name: {"ok": False, "attempts": 0, "error": None}
                                 for name in delivered}}
        if not self._allow(human, now):
            self._bucket(human)["resolved"] += 1
            self._record("resolved", resolved, "resumee", {})
            return
        self._attempt(resolved, "resolved", now)
        if self._pending(resolved):
            self.state["resolved"][key] = resolved

    # -- envoi ----------------------------------------------------------------
    def _pending(self, entry: dict) -> list:
        return [name for name, st in entry.get("channels", {}).items()
                if not st.get("ok") and st.get("attempts", 0) < self.ncfg.max_attempts]

    def _retry(self, entry: dict, event: str, now: float) -> None:
        if entry.get("status") == "envoi" and self._pending(entry):
            self._attempt(entry, event, now)

    def _attempt(self, entry: dict, event: str, now: float) -> None:
        message = render(entry["alert"], event, now, raised_ts=entry.get("raised_ts"))
        results = {}
        for name in self._pending(entry):
            st = entry["channels"][name]
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
        if results:
            delivery = ("a_blanc" if self.dry_run else
                        "envoyee" if any(v == "ok" for v in results.values()) else "echec")
            self._record(event, entry, delivery, results, message)

    def _send(self, channel: Channel, message: Message) -> tuple:
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

    def _bucket(self, human: str) -> dict:
        return self.state["summary"].setdefault(
            human, {"raised": {}, "resolved": 0, "attempts": 0})

    def _summaries(self, now: float) -> None:
        for human in list(self.state["summary"]):
            bucket = self.state["summary"][human]
            last = self.state["last_summary"].get(human)
            if last is not None and now - float(last) < RATE_WINDOW_S:
                continue
            channels = self.ncfg.channels_for(human)
            if not channels:
                self.log("rate summary for %s lost: no channel left" % human)
                del self.state["summary"][human]
                continue
            message = render_summary(bucket, self.cfg.host)
            results, ok = {}, False
            for channel in channels:
                sent, error = self._send(channel, message)
                ok = ok or sent
                results[channel.name] = ("a_blanc" if self.dry_run else "ok") if sent \
                    else "failed: %s" % error
                if not sent:
                    self.log("rate summary → %s, channel %s: %s" % (human, channel.name,
                                                                    error))
            self.state["rate"].setdefault(human, []).append(now)
            entry = {"alert": {"type": "summary"}, "human": human, "source": "debit"}
            bucket["attempts"] = int(bucket.get("attempts") or 0) + 1
            if ok or bucket["attempts"] >= self.ncfg.max_attempts:
                if not ok:
                    self.log("rate summary → %s: GIVING UP after %d attempt(s)"
                             % (human, bucket["attempts"]))
                del self.state["summary"][human]
                self.state["last_summary"][human] = now
            self._record("summary", entry,
                         "a_blanc" if self.dry_run else ("envoyee" if ok else "echec"),
                         results, message)

    # -- journal ----------------------------------------------------------------
    def _record(self, event: str, entry: dict, delivery: str, channels: dict,
                message: Message | None = None) -> None:
        alert = entry.get("alert") or {}
        record = {"schema": NOTIFY_SCHEMA, "event": event, "type": alert.get("type"),
                  "agent": alert.get("agent"), "lot": alert.get("lot"),
                  "host": alert.get("host"), "human": entry.get("human"),
                  "source": entry.get("source"), "delivery": delivery,
                  "channels": channels, "ts": round(float(self.clock()), 3)}
        if message is not None:
            record["title"], record["text"] = message.title, message.body
        self._records.append(record)
        self.emit(record)


def send_test(ncfg: NotifyConfig, human: str, host: str, sender=None) -> list:
    """`--test human:<id>` : un message par canal ; [(canal, None | erreur)]."""
    sender = sender or Sender(timeout=ncfg.timeout)
    out = []
    for channel in ncfg.channels_for(human):
        try:
            sender.send(channel, render_test(human, channel, host))
            out.append((channel.name, None))
        except ChannelError as exc:
            out.append((channel.name, str(exc)))
        except Exception as exc:
            out.append((channel.name, "unexpected error: %s" % _clean(_scrub(
                exc, channel_secrets(channel, getattr(sender, "env", None))), 160)))
    return out


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

#: display labels of the stored/emitted `delivery` and `status` values
_DELIVERY_LABELS = {"a_blanc": "dry run", "envoyee": "sent", "echec": "failed",
                    "sans_destinataire": "no recipient", "sans_canal": "no channel",
                    "resumee": "summarized", "abandonnee": "abandoned",
                    "sans_envoi": "not sent", "prise": "taken", "nouvelle": "new",
                    "envoi": "sending"}


def _format(record: dict) -> str:
    who = record.get("human") or "—"
    what = "[%s] %s" % (record.get("type"), _subject(record))
    event = {"raised": "raised", "resolved": "resolved", "summary": "summary",
             "auto_take": "auto-take", "budget_handoff": "budget handoff"}.get(
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
    # L233b : reprise des lots d'un agent Claude en pause budget
    parser.add_argument("--handoff-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_HANDOFF_MAX_PER_HOUR", handoff.DEFAULT_MAX_PER_HOUR)),
                        help="maximum lots handed per rolling hour from non-critical Claude "
                             "agents paused on budget to free DeepSeek agents of the same "
                             "project (default 4; 0 turns this off)")
    parser.add_argument("--handoff-idle", type=float,
                        default=exploitation._env_float("AMEESH_HANDOFF_IDLE",
                                                        handoff.DEFAULT_IDLE_S),
                        help="a DeepSeek agent takes a handed lot once idle for S seconds "
                             "(default 300)")
    return parser


def handoff_settings(args) -> dict | None:
    """Réglages de `handoff.budget_handoff` lus des options ; None si coupée."""
    most = int(getattr(args, "handoff_max_per_hour", 0) or 0)
    if most <= 0:
        return None
    return {"max_per_hour": most,
            "idle_s": max(0.0, float(getattr(args, "handoff_idle", 0) or 0))}


def take_settings(args) -> dict | None:
    """Réglages de `backlog.auto_take` lus des options ; None si les deux
    prises (file d'amélioration, lots de l'équipe) sont coupées."""
    idle = float(getattr(args, "take_idle", 0) or 0)
    most = int(getattr(args, "take_max_per_hour", 0) or 0)
    lot_idle = float(getattr(args, "take_lot_idle", 0) or 0)
    lot_most = int(getattr(args, "take_lot_max_per_hour", 0) or 0)
    if idle <= 0 or most <= 0:
        idle, most = 0.0, 0
    if lot_idle <= 0 or lot_most <= 0:
        lot_idle, lot_most = 0.0, 0
    if not most and not lot_most:
        return None
    return {"idle_s": idle, "max_per_hour": most,
            "lot_idle_s": lot_idle, "lot_max_per_hour": lot_most}


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
        print("ameesh notify --test: no channel for %s (notify.routes, notify.default)"
              % human, file=sys.stderr)
        return 2
    results = send_test(ncfg, human, cfg.host)
    for name, error in results:
        if args.json:
            print(json.dumps({"schema": NOTIFY_SCHEMA, "event": "test", "human": human,
                              "channel": name, "ok": error is None, "error": error},
                             ensure_ascii=False, sort_keys=True))
        else:
            print("%s → %s: %s" % (name, human, "sent" if error is None
                                   else "FAILED: %s" % error))
    return 0 if all(error is None for _, error in results) else 1


def cmd_notify(cfg: Config, ncfg: NotifyConfig, args) -> int:
    if not ncfg.configured:
        _stderr_log("no channel configured (`notify` key of the host configuration: "
                    "routes, default): alerts will be logged, not sent")
    path = state_path(cfg)
    lock = None
    if not args.dry_run:
        lock = _Lock(path + ".lock")
        if not lock.acquire():
            print("ameesh notify: another `ameesh notify` already holds %s.lock" % path,
                  file=sys.stderr)
            return 1
    state = load_state(path, log=_stderr_log, move_aside=not args.dry_run)

    def emit(record):
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(_format(record), flush=True)

    notifier = Notifier(cfg, ncfg, state, dry_run=args.dry_run, emit=emit,
                        take=take_settings(args), handoff=handoff_settings(args))
    seuils = exploitation.thresholds(args)
    interval = _interval(args, ncfg)
    db = None
    try:
        while True:
            try:
                if db is None:
                    db = db_mod.connect(cfg)
                    db_mod.require_schema(db)
                notifier.run_pass(db, thresholds=seuils)
                if not args.dry_run:
                    save_state(path, notifier.state)
            except db_mod.SchemaMissing:
                raise
            except db_mod.DbError as exc:
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
