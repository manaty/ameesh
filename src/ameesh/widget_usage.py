# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 (slice 3) — the gauges of the omarchy/agents widget file.

Owner report (2026-10-15 09:54): the ameesh gauge can be stale (the secondary
displayed at 30% while it was at 93%) and the
`rate_limit_event.unifiedWindows` logs do not see **model-specific** buckets —
claude2's "Fable Weekly" stayed at 0% while its general weekly was at 93%,
the whole Fable week lost at reset. The reliable source is the file written by
the widget, one per Claude account:

    ~/.local/state/omarchy/agents/usage/claude.json
    ~/.local/state/omarchy/agents/usage/claude-secondary.json

The reader is **inert** where the source is missing (hosts without omarchy,
including the agents' host): it returns `None` and the caller keeps its usual
reading. It never raises — missing directory, unreadable JSON, unknown
schema, false `ready` or stale reading all mean `None`.

Freshness (#1561): a snapshot is used only if `updatedAt` (fallback: file
mtime) is not older than the shortest window it publishes. A short window
reset since the reading also invalidates the long one: the reading no longer
says where consumption stands.

Model scope: a bucket carrying `title` (e.g. "Fable Weekly") is returned only
for a session of that model (`Gauge.scope`); filtering is done by
`accounts.gauges_of`, never here (the display shows all buckets).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from .cost import Gauge, Gauges, KNOWN_WINDOWS

#: The only schema this reader can read (`schemaVersion`).
SCHEMA_VERSION = 1
#: The snapshot directory, on the host where the widget runs (pc-smichea).
DEFAULT_DIR = "~/.local/state/omarchy/agents/usage"
#: Directory override, per host (`ameesh` also runs where omarchy is not
#: installed: the reader stays inert there, path missing).
DIR_ENV = "AMEESH_USAGE_WIDGET_DIR"

#: Label → (window in seconds, gauge key). The widget publishes no window
#: key: it is only in the label.
_WINDOWS = (
    ("5-hour", KNOWN_WINDOWS["five_hour"], "five_hour"),
    ("session", KNOWN_WINDOWS["five_hour"], "five_hour"),
    ("7-day", KNOWN_WINDOWS["seven_day"], "seven_day"),
    ("weekly", KNOWN_WINDOWS["seven_day"], "seven_day"),
)
#: The window words of a model bucket title: "Fable Weekly" → the model
#: "fable".
_MODEL_WORDS = re.compile(r"\b(weekly|daily|session|5-hour|7-day)\b", re.I)


def usage_dir(environ=None) -> str:
    """The snapshot directory for this host (variable, otherwise default)."""
    environ = os.environ if environ is None else environ
    return os.path.abspath(os.path.expanduser(
        environ.get(DIR_ENV) or DEFAULT_DIR))


def snapshot_key(profile, *, environ=None) -> str:
    """The widget's file key for this account: its configuration directory
    (`~/.claude-secondary` → `claude-secondary`; the default `~/.claude` →
    `claude`). The widget names its files after the directory it watches, not
    after the ameesh account name (finding impl-01, 15/10: `claude.json`,
    `claude-secondary.json`, `claude-tertiary.json` for accounts named
    primaire/secondaire/tertiaire)."""
    home = os.path.normpath(profile.home(environ))
    return os.path.basename(home).lstrip(".") or "claude"


def snapshot_path(profile, *, directory=None, environ=None, key=None) -> str:
    """The account's file: `claude.json` for the default directory,
    `claude-<dir>.json` otherwise (`~/.claude-secondary` →
    `claude-secondary.json`). `key` overrides the directory-derived name
    (#1789/N3: `accounts.gauges_of` keys on the full path when two declared
    directories share a basename — the widget cannot tell them apart)."""
    base = key or snapshot_key(profile, environ=environ)
    return os.path.join(directory or usage_dir(environ), base + ".json")


def _fraction(value) -> float:
    """A `percent` as 0..1 (schema 1 publishes a share, not a %).

    Above 1, the bucket is **dry** (overage): clamped to 1, never divided by
    100 — 1.05 read as 1% would make the account the freest."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)


def _iso_epoch(value) -> float | None:
    """An ISO 8601 UTC `resetsAt`/`updatedAt` as epoch, or None."""
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
    if moment.tzinfo is None:   # the format is UTC: a bare date is too
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _window_of(label: str):
    """(window, key) of the label, or None: an unknown label is ignored,
    never guessed."""
    low = label.lower()
    for token, window, key in _WINDOWS:
        if token in low:
            return window, key
    return None


def _scope_of(title: str) -> str:
    """The model of a bucket with `title`: "Fable Weekly (7-day)" → "fable".

    #1789/N4: only alphanumeric words remain — the title's punctuation formed
    "fable ( )", which matched no model and made `accounts.model_scoped` set
    the bucket aside."""
    return " ".join(re.findall(r"[a-z0-9]+", _MODEL_WORDS.sub(" ", title).lower()))


def read(profile, *, now: float | None = None, directory=None,
         environ=None, key=None) -> Gauges | None:
    """The account's gauges read from its snapshot, or None (missing source,
    unreadable, of another schema, not ready, or stale).

    A snapshot whose shortest window has reset since `updatedAt` no longer
    says anything reliable (#1561): None, the caller falls back on the logs.
    Its buckets carry their reading date (`read_at`, #1648): the caller can
    compare two dated sources.
    """
    now = time.time() if now is None else now
    path = snapshot_path(profile, directory=directory, environ=environ, key=key)
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
    # #1789/N2: the reading carries its date (`read_at`) on each gauge — the
    # age check of `rebalance._offer` (#1561) reads it like the logs.
    stamp = _iso_epoch(raw.get("updatedAt")) or stat.st_mtime
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
                            read_at=stamp, scope=scope))
    if not gauges:
        return None
    if now - stamp > min(g.window_s for g in gauges):
        return None
    return Gauges(gauges)
