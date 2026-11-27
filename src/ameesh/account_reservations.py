# SPDX-License-Identifier: AGPL-3.0-only
"""#8343 (manaty/ameesh#6583) : lecture publique des réservations de comptes.

`ameesh reservations --json` (`ameesh-reservations/1`) et
`GET /api/v1/reservations` publient, pour toute la flotte, les réservations
de compte encore comptées contre l'enveloppe d'un compte de facturation :
`open` (le tour est en vol) et `kept` (le tour ne s'est jamais réglé ; la
réservation compte toujours jusqu'à sa libération humaine ou la fin de son
jour calendaire). Une réservation est la propriété du cœur : cette lecture
n'ouvre, ne règle et ne libère rien.

La projection est fermée : identifiant, agent, compte de facturation, profil
enregistré pour l'agent, montant, état, période, âge. Jamais `spend_key`,
jamais une clé, un chemin, un DSN ou un secret. Une ligne que le lecteur ne
peut pas rendre fidèlement (montant non fini, date illisible, état inconnu)
est rangée dans `unreadable` avec une raison fermée : elle ne fait pas tomber
les autres. Un agent qui n'existe plus n'est pas une ligne illisible : la
réservation reste listée avec `agent_known: false`, car elle occupe encore
l'enveloppe.

Bornes : `limit` défaut 100, maximum 500 ; les plus anciennes d'abord ;
`truncated` dit que des réservations au-delà de la borne n'ont pas été
rendues. Tout est lu dans une transaction `REPEATABLE READ, READ ONLY`, avec
un seul instant `observed_ts` pour les âges.
"""
from __future__ import annotations

import math

from . import db as db_mod
from . import storage

SCHEMA = "ameesh-reservations/1"
#: bornes de la lecture (une ligne par réservation en vol au plus)
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
#: états d'une réservation encore comptée contre l'enveloppe
STATES = ("open", "kept")
#: valeurs acceptées par le filtre (`all` = open + kept)
STATE_FILTERS = ("all",) + STATES
#: raisons fermées d'une ligne illisible
UNREADABLE_REASONS = (
    "invalid_id",
    "invalid_identity",
    "invalid_state",
    "invalid_amount",
    "invalid_timestamp",
    "invalid_period",
)


def _finite(value):
    """Un flottant fini, ou None — jamais NaN/Infinity dans un document JSON."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value):
    return value if isinstance(value, str) and value else None


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _check_limit(limit) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit must be between 1 and %d" % MAX_LIMIT)
    return limit


def _unreadable(raw: dict, reason: str) -> dict:
    """La ligne illisible, réduite à ce qui peut encore la désigner."""
    return {
        "id": _positive_int(raw.get("id")),
        "agent": _text(raw.get("agent")),
        "billing_account": _text(raw.get("billing_account")),
        "state": _text(raw.get("state")),
        "reason": reason,
    }


def _row(raw: dict) -> tuple[dict | None, dict | None]:
    """(réservation lisible, ligne illisible) — exactement l'un des deux."""
    rid = _positive_int(raw.get("id"))
    if rid is None:
        return None, _unreadable(raw, "invalid_id")
    agent = _text(raw.get("agent"))
    account = _text(raw.get("billing_account"))
    if agent is None or account is None:
        return None, _unreadable(raw, "invalid_identity")
    state = raw.get("state")
    if state not in STATES:
        return None, _unreadable(raw, "invalid_state")
    usd = _finite(raw.get("usd"))
    if usd is None or usd < 0:
        return None, _unreadable(raw, "invalid_amount")
    created = _finite(raw.get("created_ts"))
    if created is None:
        return None, _unreadable(raw, "invalid_timestamp")
    period = _text(raw.get("period"))
    if period is None:
        return None, _unreadable(raw, "invalid_period")
    age = _finite(raw.get("age_s"))
    if age is None:
        return None, _unreadable(raw, "invalid_timestamp")
    reset = _finite(raw.get("period_reset_ts"))
    return {
        "id": rid,
        "agent": agent,
        "agent_known": bool(raw.get("agent_known")),
        "account_profile": _text(raw.get("session_account")),
        "billing_account": account,
        "usd": round(usd, 6),
        "state": state,
        "period": period,
        "created_ts": round(created, 6),
        "age_s": round(max(0.0, age), 3),
        "account_envelope": bool(raw.get("account_envelope")),
        "period_reset_ts": None if reset is None else round(reset, 6),
    }, None


def _document(raw_rows, *, at: float, limit: int, account, agent, states) -> dict:
    truncated = len(raw_rows) > limit
    reservations, unreadable = [], []
    for raw in raw_rows[:limit]:
        row, bad = _row(raw)
        if row is not None:
            reservations.append(row)
        else:
            unreadable.append(bad)
    return {
        "schema": SCHEMA,
        "observed_ts": round(float(at), 6),
        "limit": limit,
        "truncated": truncated,
        "filter": {"account": account, "agent": agent,
                   "states": list(states)},
        "count": len(reservations),
        "reservations": reservations,
        "unreadable": unreadable,
    }


def open_reservations(db, *, limit: int = DEFAULT_LIMIT, account: str | None = None,
                      agent: str | None = None, state: str = "all") -> dict:
    """Le document `ameesh-reservations/1` de la flotte.

    Lecture seule : une transaction `REPEATABLE READ, READ ONLY` porte l'instant
    `observed_ts` et toutes les lignes, puis rien n'est écrit. `account` et
    `agent` filtrent exactement (`None` : tous) ; `state` vaut `open`, `kept`
    ou `all`. Une erreur de base remonte (une absence de lecture n'est pas une
    absence de réservation) ; une ligne illisible est signalée dans
    `unreadable`.
    """
    _check_limit(limit)
    if state not in STATE_FILTERS:
        raise ValueError("state must be one of %s" % ", ".join(STATE_FILTERS))
    if account is not None and not _text(account):
        raise ValueError("account must be a non-empty name")
    if agent is not None and not _text(agent):
        raise ValueError("agent must be a non-empty name")
    states = list(STATES) if state == "all" else [state]
    with db.transaction(isolation="REPEATABLE READ", read_only=True) as tx:
        at = float(tx.query(
            "select extract(epoch from clock_timestamp())::float8 as ts")[0]["ts"])
        domain = getattr(storage.of(tx), "budgets", None)
        getter = getattr(domain, "account_open_reservations", None)
        if getter is None:
            raise db_mod.DbError(
                "account reservations are not available on this storage backend")
        raw_rows = getter(limit=limit + 1, account=account, agent=agent,
                          states=states, at=at)
    return _document(raw_rows, at=at, limit=limit, account=account, agent=agent,
                     states=states)
