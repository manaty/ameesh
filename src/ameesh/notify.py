# SPDX-License-Identifier: AGPL-3.0-only
"""Alertes poussées à l'humain responsable (lot L38, décision 0030 point 4).

  ameesh notify [--once] [--dry-run] [--json] [--interval S]
                [--long-turn S] [--idle-mail S] [--dead-grace S]
                [--session-tokens N] [--stale-lot S] [--orphan-lot S]
        suit les alertes d'exploitation (`ameesh alerts --follow`, mêmes
        seuils, mêmes clés de dédoublonnage) et envoie chaque alerte levée
        et chaque résolution à l'humain responsable, sur ses canaux.
  ameesh notify --test human:<id> [--json]
        envoie un message de test sur chacun des canaux de cet humain.

Les alertes restaient TIRÉES (`ameesh alerts`) : rien ne prévenait un humain
qu'un orchestrateur s'était arrêté avec du courrier (constat du 2026-10-07).
Ce service les POUSSE, comme le demande la décision 0014 (« alertes à
l'humain responsable concerné »).

Destinataire (premier trouvé) : le champ `responsible` de l'alerte, sinon le
responsable de l'agent (`agent_registry.responsible`), sinon celui du paquet
de travail du lot (en remontant les parents), sinon celui de la fiche Host
de l'hôte (canon), sinon `notify.default_human`. Une alerte sans destinataire
est journalisée, jamais perdue en silence.

Configuration : la clé `notify` du fichier de configuration de l'HÔTE
(jamais le canon) ; les secrets (jeton ntfy, webhook Slack) viennent d'une
variable d'environnement ou d'un fichier 0600 que la configuration NOMME —
une valeur secrète écrite dans la configuration est refusée. Voir
docs/EXPLOITATION.md, « Alertes poussées — ameesh notify ».

Fiabilité : l'état d'envoi est gardé dans `<état>/notify/state.json` (une
alerte qui dure n'est pas renvoyée après un redémarrage, une alerte résolue
pendant l'arrêt l'est à la reprise) ; débit limité par humain (au-delà, un
résumé) ; un canal en échec n'empêche pas les autres et il est retenté aux
passages suivants, un nombre borné de fois.
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
from . import exploitation, fil, storage
from .config import Config

NOTIFY_SCHEMA = "ameesh-notify/1"
STATE_SCHEMA = "ameesh-notify-state/1"

#: L38 (0030) : types envoyés par défaut. `delegation_expired` arrive avec L40 :
#: il est accepté ici sans dépendre de son code (un type absent n'est jamais
#: levé, c'est tout).
DEFAULT_TYPES = ("stopped_with_mail", "orphan_lot", "dead_runner", "idle_with_mail",
                 "delegation_expired")
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
    "stopped_with_mail": "agent arrêté avec du courrier",
    "orphan_lot": "lot orphelin",
    "dead_runner": "exécuteur mort",
    "idle_with_mail": "agent au repos avec du courrier",
    "delegation_expired": "délégation échue",
    "long_turn": "tour long",
    "session_too_big": "session trop grosse",
    "stale_lot": "lot stagnant",
    "host_pressure": "hôte sous pression",
    "orphan_resource": "ressource orpheline",
}
#: types urgents : notification critique (bureau), priorité haute (ntfy)
URGENT_TYPES = ("stopped_with_mail", "orphan_lot", "dead_runner", "delegation_expired")

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
                "secret en clair refusé dans la configuration (%s.%s) : nommez une variable "
                "d'environnement (`token_env`, `webhook_env`) ou un fichier 0600 "
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
    raise NotifyConfigError("%s : URL https attendue (http seulement sur la boucle locale)"
                            % what)


def _channel(name: str, raw) -> Channel:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise NotifyConfigError("notify.channels.%s : objet attendu" % name)
    _find_secret(raw, "notify.channels.%s" % name)
    kind = str(raw.get("type") or name)
    if kind not in CHANNEL_KINDS:
        raise NotifyConfigError("notify.channels.%s : type %r inconnu (%s)"
                                % (name, kind, " | ".join(CHANNEL_KINDS)))
    unknown = sorted(set(raw) - set(CHANNEL_OPTIONS[kind]))
    if kind == "slack" and "url" in raw:
        raise NotifyConfigError(
            "notify.channels.%s.url : l'URL d'un webhook Slack est un secret ; nommez la "
            "variable qui la porte (`webhook_env`, défaut %s) ou un fichier 0600 "
            "(`webhook_file`)" % (name, DEFAULT_SLACK_ENV))
    if unknown:
        raise NotifyConfigError("notify.channels.%s : option(s) inconnue(s) %s"
                                % (name, ", ".join(unknown)))
    options = dict(raw)
    if kind == "ntfy":
        if not raw.get("url") or not raw.get("topic"):
            raise NotifyConfigError("notify.channels.%s : `url` et `topic` sont requis" % name)
        check_url(str(raw["url"]), "notify.channels.%s.url" % name)
        if not _TOPIC_RE.match(str(raw["topic"])):
            raise NotifyConfigError("notify.channels.%s.topic invalide : %r"
                                    % (name, raw["topic"]))
    for key in ("token_env", "webhook_env"):
        if key in raw and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(raw[key] or "")):
            raise NotifyConfigError("notify.channels.%s.%s : nom de variable invalide"
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
        raise NotifyConfigError("%s : nombre attendu" % what) from None
    if number < 0:
        raise NotifyConfigError("%s : nombre positif attendu" % what)
    return number


def _names(value, what: str) -> tuple:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise NotifyConfigError("%s : liste de noms attendue" % what)
    return tuple(value)


def parse_config(raw) -> NotifyConfig:
    """Valide la clé `notify` (dict) ; lève `NotifyConfigError` avec la raison."""
    if raw in (None, {}, ""):
        return NotifyConfig()
    if not isinstance(raw, dict):
        raise NotifyConfigError("notify : objet JSON attendu")
    unknown = sorted(set(raw) - set(TOP_KEYS))
    if unknown:
        raise NotifyConfigError("notify : clé(s) inconnue(s) %s" % ", ".join(unknown))
    default_human = None
    if raw.get("default_human"):
        default_human = normalize_human(raw["default_human"])
        if default_human is None:
            raise NotifyConfigError("notify.default_human : `human:<id>` attendu")
    types = DEFAULT_TYPES
    if "types" in raw:
        types = _names(raw["types"], "notify.types")
        bad = [t for t in types if not _TYPE_RE.match(t)]
        if bad:
            raise NotifyConfigError("notify.types : type(s) illisible(s) %s" % ", ".join(bad))
    channels_raw = raw.get("channels") or {}
    if not isinstance(channels_raw, dict):
        raise NotifyConfigError("notify.channels : objet attendu")
    channels = {name: _channel(name, spec) for name, spec in channels_raw.items()}
    routes_raw = raw.get("routes") or {}
    if not isinstance(routes_raw, dict):
        raise NotifyConfigError("notify.routes : objet {\"human:<id>\": [canaux]} attendu")
    routes = {}
    for who, names in routes_raw.items():
        human = normalize_human(who)
        if human is None or not str(who).startswith("human:"):
            raise NotifyConfigError("notify.routes : clé %r, `human:<id>` attendu" % who)
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
                raise NotifyConfigError("canal ntfy utilisé sans déclaration : "
                                        "notify.channels.ntfy = {\"url\": …, \"topic\": …}")
            else:
                raise NotifyConfigError("canal %r utilisé dans les routes mais non déclaré "
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
    return "%d j %d h" % (s // 86400, (s % 86400) // 3600)


def _subject(alert: dict) -> str:
    parts = []
    if alert.get("agent"):
        parts.append("agent %s" % _clean(alert["agent"], 64))
    if alert.get("lot") is not None:
        lot = "lot #%s" % alert["lot"]
        if alert.get("title"):
            lot += " « %s »" % _clean(alert["title"], 80)
        parts.append(lot)
    if alert.get("host"):
        parts.append("hôte %s" % _clean(alert["host"], 64))
    return " · ".join(parts) or "mesh"


def _readable(title: str, body: str, detail_line: str | None) -> tuple:
    """R12 : même règle que les fils ; un détail illisible est retiré, pas envoyé."""
    if fil.unreadable_reason("%s\n%s" % (title, body)) and detail_line:
        body = body.replace(detail_line, "(détail retiré : texte illisible)")
    return title, body


def render(alert: dict, event: str, now: float, *, raised_ts=None) -> Message:
    """Le message d'une alerte levée (`raised`) ou résolue (`resolved`)."""
    kind = str(alert.get("type") or "?")
    label = TYPE_LABELS.get(kind, kind.replace("_", " "))
    subject = _subject(alert)
    since = alert.get("since")
    start = float(since) if since is not None else (float(raised_ts) if raised_ts else None)
    who = alert.get("agent") or alert.get("host") or (
        "lot #%s" % alert["lot"] if alert.get("lot") is not None else "")
    suffix = " (%s)" % _clean(who, 64) if who else ""
    if event == "resolved":
        title = "ameesh : résolue — %s%s" % (label, suffix)
        lines = ["[%s] %s" % (kind, subject)]
        if start is not None:
            lines.append("résolue après %s (levée le %s)" % (duration(now - start),
                                                             local_time(start)))
        else:
            lines.append("résolue")
        return Message(title, "\n".join(lines), low=True)
    title = "ameesh : %s%s" % (label, suffix)
    detail = _clean(alert.get("detail"))
    lines = ["[%s] %s" % (kind, subject)]
    if detail:
        lines.append(detail)
    if since is not None:
        lines.append("depuis le %s (%s)" % (local_time(since), duration(now - float(since))))
    elif raised_ts:
        lines.append("constatée le %s" % local_time(raised_ts))
    title, body = _readable(title, "\n".join(lines), detail)
    return Message(title, body, urgent=kind in URGENT_TYPES)


def render_summary(bucket: dict, host: str) -> Message:
    raised = bucket.get("raised") or {}
    total = sum(raised.values()) + int(bucket.get("resolved") or 0)
    parts = []
    if raised:
        parts.append("levées : " + ", ".join(
            "%d %s" % (n, kind) for kind, n in sorted(raised.items())))
    if bucket.get("resolved"):
        parts.append("résolues : %d" % int(bucket["resolved"]))
    title = "ameesh : %d alerte(s) non détaillée(s) (limite de débit)" % total
    body = "%s\nvoir `ameesh alerts` sur l'hôte %s" % (" ; ".join(parts) or "—", host)
    return Message(title, body, urgent=any(k in URGENT_TYPES for k in raised))


def render_test(human: str, channel: Channel, host: str) -> Message:
    return Message("ameesh : message de test",
                   "Canal %s (%s) de %s, depuis l'hôte %s : les alertes d'exploitation "
                   "vous parviendront ici." % (channel.name, channel.kind, human, host))


# --------------------------------------------------------------------------
# canaux
# --------------------------------------------------------------------------

def _secret_file(path: str, what: str) -> str:
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ChannelError("%s : fichier %s illisible (%s)" % (what, path, exc.strerror),
                           permanent=True) from None
    if st.st_mode & 0o077:
        raise ChannelError("%s : le fichier %s est lisible par d'autres (mode %o) : "
                           "chmod 600" % (what, path, st.st_mode & 0o777), permanent=True)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ChannelError("%s : le fichier %s n'appartient pas à cet utilisateur"
                           % (what, path), permanent=True)
    with open(path, encoding="utf-8") as fh:
        value = fh.read().strip()
    if not value:
        raise ChannelError("%s : fichier %s vide" % (what, path), permanent=True)
    return value


def _secret(channel: Channel, env: dict, env_key: str, file_key: str,
            default_env: str | None, required: bool) -> str | None:
    what = "canal %s" % channel.name
    if channel.options.get(file_key):
        return _secret_file(channel.options[file_key], what)
    var = channel.options.get(env_key) or default_env
    if not var:
        return None
    value = (env.get(var) or "").strip()
    if not value and (required or channel.options.get(env_key)):
        raise ChannelError("%s : variable d'environnement %s vide ou absente" % (what, var),
                           permanent=True)
    return value or None


#: L46 : message fixe quand l'URL est un secret (webhook Slack)
SECRET_URL_INVALID = "URL du webhook invalide"


def check_secret_value(value: str, what: str) -> str:
    """L46 : un secret (URL de webhook, jeton) sans espace ni caractère de
    contrôle — typiquement un commentaire en fin de ligne dans notify.env
    (`EnvironmentFile` de systemd ne les retire pas). Le message ne contient
    JAMAIS la valeur."""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ChannelError("%s invalide : espace ou caractère de contrôle (commentaire en "
                           "fin de ligne dans notify.env ?) — valeur non affichée" % what,
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
                                           " : %s" % extrait if extrait else "")) from None
    except urllib.error.URLError as exc:
        raise ChannelError("injoignable%s : %s" % (where, clean(exc.reason))) from None
    except (http.client.InvalidURL, ValueError) as exc:
        # L46 : http.client.InvalidURL (espace, caractère de contrôle) cite le
        # chemin de l'URL — le secret d'un webhook : message fixe
        if not show_url:
            raise ChannelError(SECRET_URL_INVALID, permanent=True) from None
        raise ChannelError("échec d'envoi%s : %s" % (where, clean(exc))) from None
    except (OSError, http.client.HTTPException) as exc:
        raise ChannelError("échec d'envoi%s : %s" % (where, clean(exc))) from None


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
                "notify-send introuvable sur cet hôte (paquet libnotify) : le canal "
                "`desktop` ne peut rien afficher ; installez-le ou retirez `desktop` des "
                "routes", permanent=True)
        urgency = "critical" if message.urgent else ("low" if message.low else "normal")
        try:
            proc = subprocess.run(
                [binary, "--app-name=ameesh", "--urgency=%s" % urgency,
                 message.title, message.body],
                capture_output=True, text=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise ChannelError("notify-send n'a pas répondu en %ds" % timeout) from None
        except OSError as exc:
            raise ChannelError("notify-send : %s" % exc.strerror) from None
        if proc.returncode != 0:
            raise ChannelError("notify-send a échoué (code %d) : %s" % (
                proc.returncode, _clean(proc.stderr, 160) or "sans message"))

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
            check_secret_value(token, "canal %s : jeton ntfy" % channel.name)
            headers["Authorization"] = "Bearer %s" % token
        _post(url, message.body.encode("utf-8"), headers, timeout, show_url=True,
              secrets=(token,) if token else ())

    def _slack(self, channel: Channel, message: Message, timeout: float) -> None:
        url = _secret(channel, self.env, "webhook_env", "webhook_file",
                      DEFAULT_SLACK_ENV, True)
        # L46 : refusée à la lecture, sans jamais citer l'URL (un secret)
        check_secret_value(url, "canal %s : %s" % (channel.name, SECRET_URL_INVALID))
        try:
            check_url(url, "webhook Slack")
        except NotifyConfigError as exc:
            raise ChannelError("canal %s : %s" % (channel.name, exc), permanent=True) from None
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
            "summary": {}, "last_summary": {}}


def load_state(path: str, log=None, *, move_aside: bool = True) -> dict:
    """L'état d'envoi ; absent = vide. Illisible : mis de côté (journalisé),
    on repart vide — au pire une alerte en cours est renvoyée une fois.
    `move_aside=False` (`--dry-run`) : rien n'est déplacé."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            raise ValueError("schéma inattendu")
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as exc:
        aside = "%s.illisible-%d" % (path, int(time.time()))
        try:
            if not move_aside:
                raise OSError
            os.replace(path, aside)
        except OSError:
            aside = "(non déplacé)"
        if log:
            log("état d'envoi illisible (%s) : mis de côté sous %s, on repart d'un état "
                "vide" % (exc, aside))
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
    return json.dumps(list(exploitation.alert_key(alert)), ensure_ascii=False)


#: champs d'une alerte gardés dans l'état (de quoi écrire la résolution)
_KEPT = ("type", "agent", "lot", "title", "host", "since", "detail", "reason", "value",
         "responsible", "stop_reason", "assignee")


# --------------------------------------------------------------------------
# le service
# --------------------------------------------------------------------------

def _stderr_log(text: str) -> None:
    print("ameesh notify : %s" % text, file=sys.stderr, flush=True)


class Notifier:
    """Un passage = lire les alertes, envoyer les nouvelles et les résolutions,
    retenter les canaux en échec, envoyer les résumés de débit."""

    def __init__(self, cfg: Config, ncfg: NotifyConfig, state: dict | None = None, *,
                 sender=None, dry_run: bool = False, emit=None, log=None, clock=None):
        self.cfg, self.ncfg = cfg, ncfg
        self.state = state if state is not None else empty_state()
        self.dry_run = dry_run
        self.sender = sender or (DrySender() if dry_run else Sender(timeout=ncfg.timeout))
        self.emit = emit or (lambda record: None)
        self.log = log or _stderr_log
        self.clock = clock or time.time

    # -- passage --------------------------------------------------------------
    def run_pass(self, db, current: list | None = None, thresholds: dict | None = None,
                 ) -> list:
        now = float(self.clock())
        if current is None:
            current = exploitation.alerts(self.cfg, db, now=now, **(thresholds or {}))
        records: list = []
        self._records = records
        router = Router(self.cfg, db, self.ncfg)
        wanted = {key_of(a): a for a in current if a.get("type") in self.ncfg.types}
        active = self.state["active"]
        for key in list(active):
            if key in wanted:
                continue
            entry = active.pop(key)
            if entry["alert"].get("type") not in self.ncfg.types:
                self.log("[%s] retiré de notify.types : oubliée sans résolution"
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
                self._raise(entry, router, now)
            elif entry["status"] in ("sans_destinataire", "sans_canal"):
                # un responsable (ou une route) a pu apparaître depuis
                self._raise(entry, router, now)
            else:
                self._retry(entry, "raised", now)
        for key in list(self.state["resolved"]):
            entry = self.state["resolved"][key]
            self._retry(entry, "resolved", now)
            if not self._pending(entry):
                del self.state["resolved"][key]
        self._summaries(now)
        for human in list(self.state["rate"]):
            window = [t for t in self.state["rate"][human] if now - t < RATE_WINDOW_S]
            if window:
                self.state["rate"][human] = window
            else:
                del self.state["rate"][human]
        return records

    # -- levée ----------------------------------------------------------------
    def _raise(self, entry: dict, router: Router, now: float) -> None:
        alert = entry["alert"]
        first = entry["status"] == "nouvelle"
        human, source = router.resolve(alert)
        if human is None:
            if first:
                self.log("alerte SANS DESTINATAIRE [%s] %s : ni `responsible`, ni "
                         "responsable d'agent, de lot ou d'hôte, ni notify.default_human — "
                         "%s" % (alert.get("type"), _subject(alert), alert.get("detail")))
                self._record("raised", entry, "sans_destinataire", {})
            entry["status"] = "sans_destinataire"
            return
        channels = self.ncfg.channels_for(human)
        entry.update(human=human, source=source)
        if not channels:
            if entry["status"] != "sans_canal":
                self.log("aucun canal pour %s (notify.routes, notify.default) : alerte [%s] "
                         "%s journalisée seulement" % (human, alert.get("type"),
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

    # -- résolution -----------------------------------------------------------
    def _resolved(self, key: str, entry: dict, now: float) -> None:
        alert, human = entry["alert"], entry.get("human")
        status = entry.get("status")
        if status in ("sans_destinataire", "sans_canal", "nouvelle"):
            self.log("alerte résolue [%s] %s (jamais envoyée : %s)" % (
                alert.get("type"), _subject(alert), status.replace("_", " ")))
            self._record("resolved", entry, status, {})
            return
        if status == "resumee":
            self._bucket(human)["resolved"] += 1
            return
        delivered = [name for name, st in entry["channels"].items() if st.get("ok")]
        if not delivered:
            self.log("alerte [%s] %s résolue avant tout envoi réussi : rien n'est envoyé"
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
                st.update(attempts=self.ncfg.max_attempts, error="canal retiré")
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
            results[name] = "échec : %s" % error
            last = st["attempts"] >= self.ncfg.max_attempts
            self.log("[%s] %s → %s, canal %s : %s%s" % (
                entry["alert"].get("type"), _subject(entry["alert"]), entry.get("human"),
                name, error,
                " — ABANDON (%d essai(s))" % st["attempts"] if last
                else " — retenté au prochain passage (%d/%d)"
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
            return False, ChannelError("erreur inattendue : %s"
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
                self.log("résumé de débit pour %s perdu : plus aucun canal" % human)
                del self.state["summary"][human]
                continue
            message = render_summary(bucket, self.cfg.host)
            results, ok = {}, False
            for channel in channels:
                sent, error = self._send(channel, message)
                ok = ok or sent
                results[channel.name] = ("a_blanc" if self.dry_run else "ok") if sent \
                    else "échec : %s" % error
                if not sent:
                    self.log("résumé de débit → %s, canal %s : %s" % (human, channel.name,
                                                                      error))
            self.state["rate"].setdefault(human, []).append(now)
            entry = {"alert": {"type": "summary"}, "human": human, "source": "debit"}
            bucket["attempts"] = int(bucket.get("attempts") or 0) + 1
            if ok or bucket["attempts"] >= self.ncfg.max_attempts:
                if not ok:
                    self.log("résumé de débit → %s : ABANDON après %d essai(s)"
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
            out.append((channel.name, "erreur inattendue : %s" % _clean(_scrub(
                exc, channel_secrets(channel, getattr(sender, "env", None))), 160)))
    return out


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def _format(record: dict) -> str:
    who = record.get("human") or "—"
    what = "[%s] %s" % (record.get("type"), _subject(record))
    event = {"raised": "levée", "resolved": "résolue", "summary": "résumé"}.get(
        record["event"], record["event"])
    channels = ", ".join("%s %s" % (k, v) for k, v in sorted(record["channels"].items()))
    line = "%s %s → %s : %s%s" % (event, what, who, record["delivery"].replace("_", " "),
                                  " (%s)" % channels if channels else "")
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
                        help="un seul passage, puis sortie")
    parser.add_argument("--dry-run", action="store_true",
                        help="imprime ce qui serait envoyé, n'envoie rien, n'écrit pas l'état")
    parser.add_argument("--json", action="store_true",
                        help="un objet JSON par envoi (schéma ameesh-notify/1)")
    parser.add_argument("--interval", type=float, default=None,
                        help="secondes entre deux passages (défaut : notify.interval, "
                             "AMEESH_NOTIFY_INTERVAL, sinon 30)")
    parser.add_argument("--test", metavar="human:ID", default=None,
                        help="envoie un message de test sur chacun des canaux de cet humain")
    exploitation.add_threshold_arguments(parser)
    return parser


def _interval(args, ncfg: NotifyConfig) -> float:
    if args.interval is not None:
        return max(0.05, args.interval)
    if ncfg.interval is not None:
        return max(0.05, ncfg.interval)
    return max(0.05, exploitation._env_float("AMEESH_NOTIFY_INTERVAL", DEFAULT_INTERVAL_S))


def cmd_test(cfg: Config, ncfg: NotifyConfig, args) -> int:
    human = normalize_human(args.test)
    if human is None or not str(args.test).startswith("human:"):
        print("ameesh notify --test : `human:<id>` attendu", file=sys.stderr)
        return 2
    if not ncfg.channels_for(human):
        print("ameesh notify --test : aucun canal pour %s (notify.routes, notify.default)"
              % human, file=sys.stderr)
        return 2
    results = send_test(ncfg, human, cfg.host)
    for name, error in results:
        if args.json:
            print(json.dumps({"schema": NOTIFY_SCHEMA, "event": "test", "human": human,
                              "channel": name, "ok": error is None, "error": error},
                             ensure_ascii=False, sort_keys=True))
        else:
            print("%s → %s : %s" % (name, human, "envoyé" if error is None
                                    else "ÉCHEC : %s" % error))
    return 0 if all(error is None for _, error in results) else 1


def cmd_notify(cfg: Config, ncfg: NotifyConfig, args) -> int:
    if not ncfg.configured:
        _stderr_log("aucun canal configuré (clé `notify` de la configuration de l'hôte : "
                    "routes, default) : les alertes seront journalisées, pas envoyées")
    path = state_path(cfg)
    lock = None
    if not args.dry_run:
        lock = _Lock(path + ".lock")
        if not lock.acquire():
            print("ameesh notify : un autre `ameesh notify` tient déjà %s.lock" % path,
                  file=sys.stderr)
            return 1
    state = load_state(path, log=_stderr_log, move_aside=not args.dry_run)

    def emit(record):
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(_format(record), flush=True)

    notifier = Notifier(cfg, ncfg, state, dry_run=args.dry_run, emit=emit)
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
                _stderr_log("base indisponible (%s) : nouvel essai dans %ds" % (exc, interval))
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
        print("ameesh notify : configuration invalide : %s" % exc, file=sys.stderr)
        return 2
    if args.test:
        return cmd_test(cfg, ncfg, args)
    try:
        return cmd_notify(cfg, ncfg, args)
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
