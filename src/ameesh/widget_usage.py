# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 (tranche 3) — les jauges du fichier du widget omarchy/agents.

Constat du propriétaire (2026-10-15 09:54) : la jauge ameesh peut être périmée
(le secondaire affiché à 30 % alors qu'il était à 93 %) et les journaux
`rate_limit_event.unifiedWindows` ne voient pas les seaux **propres à un
modèle** — le « Fable Weekly » de claude2 resté à 0 % quand son hebdomadaire
général était à 93 %, toute la semaine Fable perdue à la remise à zéro. La
source fiable est le fichier écrit par le widget, un par compte Claude :

    ~/.local/state/omarchy/agents/usage/claude.json
    ~/.local/state/omarchy/agents/usage/claude-secondary.json

Le lecteur est **inerte** là où la source manque (les hôtes sans omarchy,
dont l'hôte des agents) : il rend `None` et l'appelant garde sa lecture
habituelle. Il ne lève jamais — dossier absent, JSON illisible, schéma
inconnu, `ready` faux ou relevé périmé valent `None`.

Fraîcheur (#1561) : un instantané n'est utilisé que si `updatedAt` (repli :
mtime du fichier) n'est pas plus vieux que la plus courte fenêtre qu'il
publie. Une courte fenêtre remise à zéro depuis le relevé invalide aussi la
longue : le relevé ne dit plus où en est la consommation.

Portée par modèle : un seau qui porte `title` (ex. « Fable Weekly ») n'est
rendu que pour une session de ce modèle (`Gauge.scope`) ; le filtrage est
fait par `accounts.gauges_of`, jamais ici (l'affichage montre tous les seaux).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from .cost import Gauge, Gauges, KNOWN_WINDOWS

#: Le seul schéma que ce lecteur sait lire (`schemaVersion`).
SCHEMA_VERSION = 1
#: Le dossier des instantanés, sur l'hôte où tourne le widget (pc-smichea).
DEFAULT_DIR = "~/.local/state/omarchy/agents/usage"
#: Surcharge du dossier, par hôte (`ameesh` tourne aussi là où omarchy n'est
#: pas installé : le lecteur y reste inerte, chemin absent).
DIR_ENV = "AMEESH_USAGE_WIDGET_DIR"

#: Libellé → (fenêtre en secondes, clé de jauge). Le widget ne publie aucune
#: clé de fenêtre : elle n'est que dans le libellé.
_WINDOWS = (
    ("5-hour", KNOWN_WINDOWS["five_hour"], "five_hour"),
    ("session", KNOWN_WINDOWS["five_hour"], "five_hour"),
    ("7-day", KNOWN_WINDOWS["seven_day"], "seven_day"),
    ("weekly", KNOWN_WINDOWS["seven_day"], "seven_day"),
)
#: Les mots de fenêtre d'un titre de seau de modèle : « Fable Weekly » → le
#: modèle « fable ».
_MODEL_WORDS = re.compile(r"\b(weekly|daily|session|5-hour|7-day)\b", re.I)


def usage_dir(environ=None) -> str:
    """Le dossier des instantanés pour cet hôte (variable, sinon défaut)."""
    environ = os.environ if environ is None else environ
    return os.path.abspath(os.path.expanduser(
        environ.get(DIR_ENV) or DEFAULT_DIR))


def snapshot_path(profile, *, directory=None, environ=None) -> str:
    """Le fichier du compte : `claude.json` pour le dossier par défaut,
    `claude-<nom>.json` sinon (`~/.claude-secondary` →
    `claude-secondary.json`)."""
    base = os.path.basename(os.path.normpath(profile.home(environ))).lstrip(".") or "claude"
    return os.path.join(directory or usage_dir(environ), base + ".json")


def _fraction(value) -> float:
    """Un `percent` en 0..1 (le schéma 1 publie une part, pas un %).

    Au-delà de 1, le seau est **à sec** (dépassement) : borné à 1, jamais
    divisé par 100 — 1.05 lu 1 % rendrait le compte le plus libre."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)


def _iso_epoch(value) -> float | None:
    """Un `resetsAt`/`updatedAt` ISO 8601 UTC en epoch, ou None."""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:   # le format est UTC : une date nue l'est aussi
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _window_of(label: str):
    """(fenêtre, clé) du libellé, ou None : un libellé inconnu est ignoré,
    jamais deviné."""
    low = label.lower()
    for token, window, key in _WINDOWS:
        if token in low:
            return window, key
    return None


def _scope_of(title: str) -> str:
    """Le modèle d'un seau à `title` : « Fable Weekly » → « fable »."""
    return " ".join(_MODEL_WORDS.sub(" ", title).split()).lower()


def read(profile, *, now: float | None = None, directory=None,
         environ=None) -> Gauges | None:
    """Les jauges du compte lues dans son instantané, ou None (source absente,
    illisible, d'un autre schéma, non prête, ou périmée).

    Un instantané dont la plus courte fenêtre s'est remise à zéro depuis
    `updatedAt` ne dit plus rien de fiable (#1561) : None, l'appelant
    retombe sur les journaux.
    """
    now = time.time() if now is None else now
    path = snapshot_path(profile, directory=directory, environ=environ)
    try:
        stat = os.stat(path)
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
        return None
    if raw.get("ready") is False:
        return None
    limits = raw.get("limits")
    if not isinstance(limits, list) or not limits:
        return None
    gauges = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        found = _window_of(item.get("label") or "")
        if found is None:
            continue
        window, key = found
        title = item.get("title")
        scope = _scope_of(title) if isinstance(title, str) and title.strip() else ""
        if scope:
            key = "%s@%s" % (key, scope)
        gauges.append(Gauge("claude", key, _fraction(item.get("percent")),
                            _iso_epoch(item.get("resetsAt")), window,
                            scope=scope))
    if not gauges:
        return None
    stamp = _iso_epoch(raw.get("updatedAt")) or stat.st_mtime
    if now - stamp > min(g.window_s for g in gauges):
        return None
    return Gauges(gauges)
