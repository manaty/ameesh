# SPDX-License-Identifier: AGPL-3.0-only
"""L226b — recognize a turn that failed for an authentication reason.

A failed turn whose error, short result text or stderr tail says the
credential is no longer valid (HTTP 401, `authentication_error`, "please run
/login", gcloud "Reauthentication failed", expired or revoked OAuth token)
must not be relaunched in a loop: the agent goes to `auth_blocked` and waits
for the credential to be healthy again (L226a sentinel) or for a failover to
a healthy account.

Only failed turns are classified, and only short texts: a successful turn, or
a long final answer that merely *talks* about 401 errors, is never an auth
failure.
"""
from __future__ import annotations

import os
import re
import time

#: longest result text still read as a harness error message (a real auth
#: failure is one short line; a long text is the agent's own answer)
MAX_RESULT_CHARS = 600
#: bytes read at the end of the turn's stderr file
STDERR_TAIL_BYTES = 8192

#: (pattern, reason) — the first match gives the reason
PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), reason) for p, reason in (
        (r"authentication_error", "authentication_error"),
        (r"\b401\b[^\n]{0,80}(unauthori[sz]ed|authenticat|invalid|credential|token)"
         r"|(unauthori[sz]ed|authenticat)[^\n]{0,80}\b401\b", "http 401"),
        (r"invalid (x-)?api[ _-]?key", "invalid api key"),
        (r"please run /login|run `?/login`?|/login to (re)?authenticate", "login required"),
        (r"reauthentication failed", "gcloud reauthentication failed"),
        (r"oauth token (has )?(expired|been revoked)|token (has )?(expired|been revoked)"
         r"|invalid_grant", "token expired or revoked"),
        (r"not logged in|login required|credentials? (have |has )?expired",
         "login required"),
        # #8180 : le harnais dit que la session de connexion a atteint sa
        # limite et ne se renouvellera plus (rotation vers un compte dont le
        # login était expiré, incident avatar-smichea du 23/11). C'est une
        # panne d'authentification, pas un échec rapide : l'agent doit passer
        # `auth_blocked` et reprendre seul après une reconnexion. Le motif est
        # ancré sur la forme du fournisseur : « failed to authenticate » seul
        # est générique (un serveur MCP ou un outil tiers le dit aussi) et
        # condamnerait un compte sain (verdict B de auditeur, 24/11).
        (r"this login reached its time limit|cannot be renewed"
         r"|failed to authenticate[.:][^\n]{0,40}\bapi error:?\s*401\b",
         "login expired"),
    ))


def classify(error: str | None = None, result: str | None = None,
             stderr_tail: str | None = None) -> str | None:
    """The auth reason of a FAILED turn, None if it is not an auth failure.

    `error`: the error the stream reader extracted; `result`: the turn's
    final text (read only if short); `stderr_tail`: the end of its stderr.
    """
    texts = [error or "", stderr_tail or ""]
    if result and len(result) <= MAX_RESULT_CHARS:
        texts.append(result)
    for text in texts:
        if not text:
            continue
        for pattern, reason in PATTERNS:
            if pattern.search(text):
                return reason
    return None


def stderr_tail(path: str | None, limit: int = STDERR_TAIL_BYTES, *,
                start: int | None = 0) -> str:
    """The last `limit` bytes of a stderr file from offset `start` (the
    turn's own output in a log opened in append mode), decoded leniently;
    '' if none, or if `start` is None (the turn never opened the log)."""
    if not path or start is None:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(start, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# credential health (table `credential_health`, migration 0060)
# --------------------------------------------------------------------------

#: `status_text` of an agent blocked on authentication (status `blocked`),
#: followed by `<harness>/<account>`; `last_error` starts with it too
STATUS_PREFIX = "auth_blocked:"


def account_name(profile) -> str:
    """The health key of a turn's account: the L30 account name, '' for the
    harness's default credential (no accounts declared)."""
    return profile.name if profile is not None else ""


def status_text(harness: str, account: str) -> str:
    return "%s %s/%s" % (STATUS_PREFIX, harness, account or "(default)")


#: #8180: a forecast expiry is evidence only on a reading the probe still
#: refreshes. Three probe periods (`sentinel.PERIOD_S` × 3 = 30 min) is the
#: same bound the `credential_expiring` alert uses (#8042: a healthy row the
#: probe no longer reads must not alert forever with a passed expiry).
EXPIRY_EVIDENCE_S = 1800.0


def expiry(row: dict | None) -> float | None:
    """The forecast expiry of a credential row (epoch), or None."""
    try:
        value = (row or {}).get("expires_ts")
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _checked(row: dict | None) -> float | None:
    try:
        value = (row or {}).get("checked_ts")
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _current(row: dict, now: float) -> bool:
    """The row was read by the probe recently enough for its expiry to count
    (same freshness rule as the `credential_expiring` alert, #8042)."""
    checked = _checked(row)
    # a reading taken microseconds after `now` (the caller captured the clock
    # before the query) is fresh, not negative evidence.
    return checked is not None and now - checked <= EXPIRY_EVIDENCE_S


def _fix_of(reason: str | None) -> str:
    """The `fix: …` tail a sentinel reason carries, or ''."""
    found = str(reason or "")
    marker = "; fix: "
    return found.split(marker, 1)[1].strip() if marker in found else ""


def expired(row: dict | None, now: float | None = None) -> bool:
    """#8180: a `healthy` credential whose absolute forecast expiry is past.

    The login can no longer be renewed: the provider refuses every turn
    (incident avatar-smichea, 23/11). A past expiry on a row the probe no
    longer reads is not evidence (the credential may have been replaced), so
    the freshness bound of the alert applies. A stored `failing` row is not
    this case, it is `failing` already."""
    if not row or row.get("state") != "healthy":
        return False
    deadline = expiry(row)
    if deadline is None:
        return False
    now = time.time() if now is None else float(now)
    return deadline <= now and _current(row, now)


def _expired_row(row: dict, now: float) -> dict:
    """The row of an expired login, read as a failure with an actionable
    reason (account and the real login command, when the sentinel wrote it)."""
    out = dict(row)
    deadline = expiry(row) or now
    out["state"] = "failing"
    out["reason"] = ("forecast expiry passed at %s (absolute: set at login, "
                     "not extended by use); login no longer valid"
                     % time.strftime("%Y-%m-%d %H:%M UTC",
                                     time.gmtime(deadline)))
    fix = _fix_of(row.get("reason"))
    if fix:
        out["reason"] += "; fix: %s" % fix
    return out


def effective(row: dict | None, now: float | None = None) -> dict | None:
    """The row as a placement decision reads it: unchanged when usable,
    `failing` (with an actionable reason) when its absolute expiry is past."""
    if not row:
        return None
    import time as _time
    now = _time.time() if now is None else float(now)
    if row.get("state") == "failing":
        return row
    if expired(row, now):
        return _expired_row(row, now)
    return row


def unusable(db, host: str, harness: str, now: float | None = None) -> dict[str, dict]:
    """{account: row} of the credentials that must not receive a turn:
    reported `failing` (probe or refused turn), or a `healthy` login whose
    absolute expiry is past (#8180 — `failing` only knew the stored state).

    Same database contract as `failing`: a database without migration 0060
    has none; an unreachable database raises (L72)."""
    now = time.time() if now is None else float(now)
    from . import db as db_mod, storage
    try:
        rows = storage.of(db).credentials.health(host, harness)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return {}
    out: dict[str, dict] = {}
    for name, row in rows.items():
        eff = effective(row, now)
        if eff is not None and eff.get("state") == "failing":
            out[name] = eff
    return out


def failing(db, host: str, harness: str) -> dict[str, dict]:
    """{account: row} of the credentials reported `failing` for a harness,
    including a login whose absolute expiry is past (#8180).

    A database without migration 0060 has no failing credential (the
    feature is off until it is migrated); an unreachable database raises
    (L72: the caller suspends the turns)."""
    return unusable(db, host, harness)


def expiring(db, host: str, harness: str, *, now: float,
             horizon: float) -> set[str]:
    """#8180: accounts whose login expires before `horizon` but is still
    valid (a turn may end after it). They stay usable, only ranked after the
    accounts that will outlive the turn; a reading the probe no longer
    refreshes is not evidence."""
    from . import db as db_mod, storage
    try:
        rows = storage.of(db).credentials.health(host, harness)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return set()
    out = set()
    for name, row in rows.items():
        if row.get("state") != "healthy" or not _current(row, now):
            continue
        deadline = expiry(row)
        if deadline is not None and now < deadline <= horizon:
            out.add(name)
    return out


def report(db, host: str, harness: str, account: str, state: str, *, reason: str = "",
           source: str, expires_at: float | None = None,
           fingerprint: str | None = None) -> dict:
    """Records a credential's health; {} on a database without 0060.
    `fingerprint`: of the credential that failed (`sentinel.fingerprint`)."""
    from . import db as db_mod, storage
    try:
        return storage.of(db).credentials.report(host, harness, account, state,
                                                 reason=reason, source=source,
                                                 expires_at=expires_at,
                                                 fingerprint=fingerprint)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return {}
