# SPDX-License-Identifier: AGPL-3.0-only
"""Reconcile Codex session readings without trusting the newest file alone."""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
import threading
import heapq
from collections import OrderedDict
from datetime import datetime


class AmbiguousCodexGauges(ValueError):
    """Live readings in one account directory contradict each other."""


def _timestamp(event, fallback):
    try:
        return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return fallback


_READINGS = OrderedDict()
_CACHE_LOCK = threading.Lock()
MAX_FILES = 32
TAIL_BYTES = 256 * 1024
META_BYTES = 64 * 1024
#: #3044 : longueur de la fenêtre courte Codex (5 h). Une courte vivante ne
#: peut être observée que depuis moins d'une fenêtre courte ; au-delà, aucun
#: relevé de l'échantillon ne peut la cacher, le compte est au repos.
SHORT_WINDOW_S = 5 * 3600.0


class SessionReadings(list):
    """A bounded parse, including whether its prefix was omitted."""

    def __init__(self, items, *, truncated):
        super().__init__(items)
        self.truncated = truncated


_PLAN_CLAIM = "https://api.openai.com/auth"


def live_plan(home):
    """Le forfait du compte vivant, lu dans `<home>/auth.json` (#3034).

    `auth.json` porte un jeton `id_token` : sa charge utile nomme le forfait
    (`chatgpt_plan_type`). On ne vérifie pas la signature — ce n'est pas une
    preuve d'identité, seulement le forfait que le compte **vivant** déclare,
    pour ne pas attribuer à ce compte des relevés d'un ancien forfait. Le jeton
    n'est jamais rendu ni journalisé ; un fichier absent, illisible ou sans
    forfait donne `None` (comportement d'avant #3034)."""
    path = os.path.join(str(home or ""), "auth.json")
    try:
        with open(path, "rb") as stream:
            node = json.loads(stream.read(1024 * 1024))
    except (OSError, ValueError):
        return None
    if not isinstance(node, dict):
        return None
    token = (node.get("tokens") or {}).get("id_token") \
        if isinstance(node.get("tokens"), dict) else None
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    auth = claims.get(_PLAN_CLAIM)
    plan = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    if not isinstance(plan, str):
        return None
    return plan.strip().lower() or None


def recent_files(root):
    """Stat once per journal, retain only the newest bounded sample."""
    def candidates():
        for directory, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(directory, name)
                try:
                    yield os.stat(path).st_mtime, path
                except OSError:
                    continue

    selected = heapq.nlargest(MAX_FILES + 1, candidates())
    return selected[:MAX_FILES], len(selected) > MAX_FILES


def session_reading(path, read_at):
    from .cost import _journal_signature, _racy

    stat = os.stat(path)
    seen = time.time_ns()
    signature = _journal_signature(stat)
    with _CACHE_LOCK:
        cached = _READINGS.get(path)
        if cached is not None and cached[0] == signature and not _racy(stat, seen):
            _READINGS.move_to_end(path)
            return cached[1]
        # Evict even on an unchanged signature: after a clock step back, a
        # formerly settled cache entry can become racy again.
        _READINGS.pop(path, None)
    readings = _session_reading(path, read_at)
    after = os.stat(path)
    # Age is measured BEFORE parsing; a slow read cannot settle its snapshot.
    if signature == _journal_signature(after) and not _racy(stat, seen):
        with _CACHE_LOCK:
            _READINGS[path] = (signature, readings)
            if len(_READINGS) > 4096:
                _READINGS.popitem(last=False)
    return readings


def _session_reading(path, read_at):
    # Local import avoids a cost -> codex_gauges -> cost import cycle.
    from .cost import Gauge, _epoch, _find_rate_limits, _fraction, _read_tail

    identity = None
    try:
        with open(path, "rb") as stream:
            first = json.loads(stream.readline(META_BYTES))
        if first.get("type") == "session_meta":
            identity = first.get("payload", {}).get("creator_account_id")
    except (OSError, ValueError, AttributeError):
        pass
    windows = {}
    for line in _read_tail(path, TAIL_BYTES).splitlines():
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = _find_rate_limits(event)
        if not limits:
            continue
        stamp = _timestamp(event, read_at)
        for slot in ("primary", "secondary"):
            value = limits.get(slot)
            if not isinstance(value, dict) or "used_percent" not in value:
                continue
            minutes = float(value.get("window_minutes") or 0)
            gauge = Gauge("codex", "codex-%dmin" % int(minutes),
                          _fraction(value["used_percent"], percent=True),
                          _epoch(value.get("resets_at")),
                          minutes * 60 or 7 * 86400.0, read_at=stamp)
            previous = windows.get(gauge.key)
            if previous is None or (stamp or 0) >= (previous[0].read_at or 0):
                windows[gauge.key] = (gauge, limits, identity)
    return SessionReadings(windows.values(), truncated=os.path.getsize(path) > TAIL_BYTES)


def reconcile(readings, now, *, truncated=False, plan=None):
    """Keep the newest observation per window; reject mixed identities/plans.

    Session metadata names the creator, not necessarily the account used by
    a resumed session. It cannot by itself certify a later rate-limit event.
    Never blend incompatible plans into a seemingly healthy account.

    #3034 : `plan` est le forfait du compte **vivant** (`live_plan`, lu dans
    `auth.json`). Quand les relevés partagent une identité unique et que ce
    forfait est connu, les relevés d'un autre forfait ne comptent plus pour
    l'ambiguïté ni dans les fenêtres. Mais un tel relevé peut être plus récent
    que le dernier du forfait vivant : si une de ses fenêtres vivantes est
    saturée ou en overage, l'ignorer masquerait une pause ou une dépense
    (argent, overage) — le compte reste refusé. Sans forfait vivant lisible,
    ou avec plusieurs identités, le refus d'ambiguïté reste la règle.

    #3044 : un échantillon tronqué d'un compte non-pro sans fenêtre courte
    **vivante** est accepté quand une courte a été retenue (échue) et que le
    relevé le plus récent est plus vieux qu'une fenêtre courte — le compte est
    au repos, la courte échue vaut 0 % (L71). Un relevé récent sans courte
    vivante, ou un échantillon sans aucune courte, garde le refus #2843.
    """
    from .cost import Gauges, _codex_overage

    live = [r for r in readings if not r[0].reset_passed(now)]
    plans = {r[1].get("plan_type") for r in live if r[1].get("plan_type")}
    identities = {r[2] for r in live if r[2]}
    if len(identities) > 1:
        raise AmbiguousCodexGauges("ambiguous Codex account: conflicting live session identities or plans")
    if plan and len(identities) == 1:
        wanted = str(plan).strip().lower()
        observed = {
            str(r[1].get("plan_type")).strip().lower()
            for r in readings if r[1].get("plan_type")}
        if observed and wanted not in observed:
            # Aucun journal ne porte le forfait vivant : on ne vide pas le
            # compte en silence (des jauges absentes se liraient comme 0 %).
            raise AmbiguousCodexGauges(
                "ambiguous Codex account: no live reading matches the declared plan")
        others = [
            r for r in readings
            if r[1].get("plan_type")
            and str(r[1].get("plan_type")).strip().lower() != wanted]

        def saturated(reading):
            # #3034 (B1) : un relevé d'un autre forfait, plus récent que le
            # dernier du forfait vivant, peut être le vrai relevé du compte.
            # L'ignorer n'est sûr que s'il ne signale ni saturation ni
            # overage ; une fenêtre échue, elle, ne bloque plus (L71).
            gauge, limits, _identity = reading
            if gauge.reset_passed(now):
                return False
            return gauge.used >= 1.0 or _codex_overage(limits, [gauge]).in_use

        if any(saturated(reading) for reading in others):
            raise AmbiguousCodexGauges(
                "ambiguous Codex account: a live window of another plan is "
                "saturated or in overage")
        readings = [
            r for r in readings
            if not r[1].get("plan_type")
            or str(r[1].get("plan_type")).strip().lower() == wanted]
        live = [r for r in readings if not r[0].reset_passed(now)]
        plans = {r[1].get("plan_type") for r in live if r[1].get("plan_type")}
    if len(plans) > 1:
        raise AmbiguousCodexGauges("ambiguous Codex account: conflicting live session identities or plans")
    windows = {}
    for gauge, limits, _identity in readings:
        previous = windows.get(gauge.key)
        if previous is None or (gauge.read_at or 0) > (previous[0].read_at or 0):
            windows[gauge.key] = (gauge, limits)
    # A long sequence of partial team readings must not evict a saturated
    # short window beyond our I/O budget. Pro legitimately has a week only.
    if truncated:
        # Expiry clears quota usage, not the observed plan's window layout.
        # Otherwise a week-only pro account would require a fresh short
        # reading to resume after reset, which it cannot produce. The *live*
        # plan names the expected layout when one is observed: a plan change
        # leaves expired team journals behind, and those must not demand a
        # short window from a pro account.
        live_plans = {r[1].get("plan_type") for r in live if r[1].get("plan_type")}
        sample_plans = live_plans or {
            r[1].get("plan_type") for r in readings if r[1].get("plan_type")}
        missing = []
        # #2843: for a non-pro plan the short window must be seen *live*, not
        # merely present. An expired short in the sample says nothing about a
        # live saturated short hidden outside the 32-journal sample: its
        # `used_at` is 0 and it would mask the overage. A team account whose
        # sampled short observations have all expired is refused (a pause)
        # until a live one appears — the same prudence as for a sample with no
        # short window at all, accepting that bounded residual.
        # #3044: an account left unused longer than the short window cannot
        # produce a live short observation: a live short seen at `now` would
        # have been observed less than one short window ago. When the newest
        # observation of the plan-filtered sample is that old *and a short was
        # observed* (even expired), the account is at rest — the retained
        # short is expired and counts 0 % (L71), and refusing would deadlock it
        # (never chosen, never refreshed). A recent observation without a live
        # short keeps the #2843 refusal: it can hide a saturated short outside
        # the bounded sample. A sample with no short at all keeps it too: the
        # layout is not proven.
        short_windows = [g for g, _limits in windows.values()
                         if g.window_s <= SHORT_WINDOW_S]
        short_live = any(not g.reset_passed(now) for g in short_windows)
        newest = max((g.read_at for g, _limits in windows.values()
                      if g.read_at is not None), default=None)
        at_rest = newest is not None and now - newest >= SHORT_WINDOW_S
        if sample_plans != {"pro"} and not short_live \
                and not (short_windows and at_rest):
            missing.append("short window")
        if not any(g.window_s > 5 * 3600 for g, _limits in windows.values()):
            missing.append("weekly window")
        if missing:
            raise AmbiguousCodexGauges(
                "incomplete Codex account reading: %s absent from bounded session sample"
                % ", ".join(missing))
    if not windows:
        return []
    gauges = [r[0] for r in windows.values()]
    # A partial reading must not clear a retained saturated window's overage.
    overages = [_codex_overage(limits, [gauge]) for gauge, limits in windows.values()]
    from .cost import Overage
    capable = True if any(o.capable is True for o in overages) else (
        False if all(o.capable is False for o in overages) else None)
    return Gauges(gauges, Overage(capable, any(o.in_use for o in overages)))
