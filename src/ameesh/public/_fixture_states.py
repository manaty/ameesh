# SPDX-License-Identifier: AGPL-3.0-only
"""Qualification states of a fixture agent (contract 1.7, #8341, manaty/ameesh#6581).

Private implementation of :meth:`ameesh.public.fixture.Fixture.agent` and
:meth:`~ameesh.public.fixture.Fixture.agent_state`.  A consumer's module test
asks for a named state; this module reaches it through the core's own code
(registry, canon synchronisation, budgets, cost ledger) on the fixture's
disposable database, then reads the agent back through the read-only
observation builder (``ameesh agent observe``) and refuses with
``FixtureStateError`` when the state was not reached.

Two states have no production write path, by design: a canon checked too long
ago and an ambiguous spend marker only arise from time passing or from a lost
ledger key.  For those two, and only on the fixture's own database, this module
ages ``canon_state.checked_at`` and drops the marker's ``spend_key`` — the
exact facts a real runner would leave behind.  The SQL stays here, inside the
core package; the consumer never writes a core table.

Every state touches only the named agent's rows (and the named billing
account), and each one has its reverse: ``lease_free`` undoes the leases,
``canon_fresh`` the stale canon, ``placement_admitted`` the refusal,
``spend_clear`` the marker and the open reservations.
"""
from __future__ import annotations

import time
from typing import Callable, Dict

from .. import agent_observation, canon_sync, placement, registry, storage

#: the states :meth:`Fixture.agent_state` can reach (closed list, contract 1.7)
AGENT_STATES = (
    "lease_live",
    "lease_foreign",
    "lease_expired",
    "lease_free",
    "canon_fresh",
    "canon_stale",
    "placement_admitted",
    "placement_refused",
    "spend_ambiguous",
    "spend_clear",
    "reservation_open",
    "reservation_settled",
    "cost",
)

#: lease owner of ``lease_live`` and ``lease_expired`` (the consumer's runner)
FIXTURE_OWNER = "fixture-runner"
#: lease owner of ``lease_foreign`` (another holder)
FOREIGN_OWNER = "fixture-foreign"
#: billing account of the budget states when none is given
FIXTURE_ACCOUNT = "fixture-account"
#: daily envelope given to a billing account that has none
DEFAULT_LIMIT_USD = 100.0
#: default fixture agent profile; the default host is the agent's own
#: (``fixture-host-<name>``): the canon state is per host, so a canon state
#: of one agent never reaches another agent unless they share a host
DEFAULT_HOST_PREFIX = "fixture-host-"
DEFAULT_HARNESS = "claude"
DEFAULT_MODEL = "fixture-model"
DEFAULT_TEAM = "fixture"
DEFAULT_RESPONSIBLE = "human:fixture"
#: canon commit recorded when the consumer has not written a canon
PLACEHOLDER_COMMIT = "f1" * 20

_STATE_ACTOR = "fixture"

#: the options each state accepts; any other key is refused
STATE_OPTIONS = {
    "lease_live": ("owner", "ttl"),
    "lease_foreign": ("owner", "ttl"),
    "lease_expired": ("owner", "ttl"),
    "lease_free": (),
    "canon_fresh": (),
    "canon_stale": ("age_s",),
    "placement_admitted": (),
    "placement_refused": ("diagnostic",),
    "spend_ambiguous": ("start_index", "account"),
    "spend_clear": (),
    "reservation_open": ("account", "usd", "spend_key", "limit_usd"),
    "reservation_settled": ("account", "usd", "spend_key", "cost_usd", "limit_usd"),
    "cost": ("usd", "input_tokens", "output_tokens", "spend_key", "account"),
}


def default_host(name: str) -> str:
    """The agent's own fixture host."""
    return DEFAULT_HOST_PREFIX + name


class StateNotReached(Exception):
    """Raised inside this module; the fixture turns it into ``FixtureStateError``."""


def register(fixture, db, name: str, *, host: str, harness: str, model: str,
             team: str, responsible: str, governed: bool) -> dict:
    """Write (or rewrite) one agent row, its canon state and an admitted placement."""
    row = registry.get(db, name)
    commit = fixture._canon_commit or PLACEHOLDER_COMMIT
    values = {
        "harness": harness, "host": host, "cwd": fixture.root, "model": model,
        "budget_usd": None, "responsible": responsible, "team": team,
        "projects": [team], "provider": None, "credential_mode": None,
        "capabilities": ["read"],
        "canon_ref": ("agents/%s.md@%s" % (name, commit)) if governed else None,
        "placement_ok": True if governed else None,
        "placement_diagnostic": None,
        "placement_ref": ("hosts/%s.md" % host) if governed else None,
        "_expected_lease_epoch": int(row["lease_epoch"] or 0) if row else -1,
    }
    if row and row.get("lease_owner"):
        if observe(fixture, db, name)["lease"]["live"]:
            raise StateNotReached("agent %s holds a live lease (%s): put it in "
                                  "lease_free first" % (name, row["lease_owner"]))
        # An expired lease is released, as the next claim would take it over.
        _free(db, name)
        row = registry.get(db, name)
        values["_expected_lease_epoch"] = int(row["lease_epoch"] or 0)
    if not canon_sync._write_declared(db, name, values):
        raise StateNotReached("the registry refused to write agent %s" % name)
    if governed:
        _record_canon(fixture, db, host, commit)
    return observe(fixture, db, name)


def reach(fixture, db, name: str, state: str, options: dict) -> dict:
    """Reach ``state`` for ``name``; returns the state report."""
    row = registry.get(db, name)
    if row is None:
        raise StateNotReached("agent %s is not registered: call agent() first" % name)
    handler = _HANDLERS[state]
    details = handler(fixture, db, name, row, dict(options)) or {}
    report = observe(fixture, db, name)
    report["state"] = state
    report.update(details)
    _CHECKS[state](report, details)
    return report


def observe(fixture, db, name: str) -> dict:
    """The closed state report: lease, canon, placement, admission, spend marker."""
    from .. import config as config_mod
    cfg = config_mod.load(env=fixture.env())
    document = agent_observation.observe(db, name, cfg=cfg)
    lease = document.get("lease") or {}
    canon = document.get("canon") or {}
    placement_block = document.get("placement") or {}
    admission = document.get("admission") or {}
    turn = (document.get("native") or {}).get("turn") or {}
    return {
        "agent": name,
        "lease": {key: lease.get(key) for key in ("owner", "epoch", "live", "expires_ts")},
        "canon": {key: canon.get(key) for key in ("state", "last_good_commit", "age_s",
                                                  "max_age_s", "fresh", "ref_matches")},
        "placement": {key: placement_block.get(key) for key in ("ok", "ref", "diagnostic")},
        "reasons": list(admission.get("reasons") or []),
        "spend": {"marker": bool(turn.get("spend_key") or turn.get("created_ts")),
                  "spend_key": turn.get("spend_key"),
                  "ledger_ambiguous": bool(turn.get("ledger_ambiguous"))},
    }


# -- lease -----------------------------------------------------------------
def _lease(fixture, db, name, row, options, *, owner, ttl):
    _free(db, name)
    got = registry.claim(db, name, options.get("owner") or owner,
                         float(options.get("ttl", ttl)))
    if not got:
        raise StateNotReached("registry.claim refused agent %s (canon, placement or host "
                              "closed: see the report's reasons)" % name)
    return {}


def _lease_live(fixture, db, name, row, options):
    return _lease(fixture, db, name, row, options, owner=FIXTURE_OWNER, ttl=300)


def _lease_foreign(fixture, db, name, row, options):
    return _lease(fixture, db, name, row, options, owner=FOREIGN_OWNER, ttl=300)


def _lease_expired(fixture, db, name, row, options):
    # The claim takes the lease with an expiry already past: the same row a runner
    # leaves behind when it dies without releasing.
    options.setdefault("ttl", -60)
    if float(options["ttl"]) >= 0:
        raise StateNotReached("lease_expired needs a negative ttl, got %r" % options["ttl"])
    return _lease(fixture, db, name, row, options, owner=FIXTURE_OWNER, ttl=-60)


def _lease_free(fixture, db, name, row, options):
    _free(db, name)
    return {}


def _free(db, name):
    row = storage.of(db).leases.state(name)
    if row and row.get("lease_owner"):
        registry.release(db, name, row["lease_owner"], int(row["lease_epoch"] or 0))


# -- canon -----------------------------------------------------------------
def _canon_fresh(fixture, db, name, row, options):
    _record_canon(fixture, db, row["host"], _pinned_commit(fixture, row))
    return {}


def _canon_stale(fixture, db, name, row, options):
    _record_canon(fixture, db, row["host"], _pinned_commit(fixture, row))
    age = float(options.get("age_s", agent_observation.CANON_MAX_AGE_S + 60))
    if age <= agent_observation.CANON_MAX_AGE_S:
        raise StateNotReached("canon_stale needs age_s above %d, got %r"
                              % (agent_observation.CANON_MAX_AGE_S, age))
    # No production path writes an old check: a host whose synchronisation
    # stopped leaves exactly this row.
    db.execute("UPDATE canon_state SET checked_at = now() - make_interval(secs => %s)"
               " WHERE host = %s AND canon = ''", (age, row["host"]))
    return {}


def _pinned_commit(fixture, row):
    ref = row.get("canon_ref") or ""
    if "@" not in ref:
        raise StateNotReached("agent %s is not governed: register it with governed=True"
                              % row["name"])
    return ref.rsplit("@", 1)[1]


def _record_canon(fixture, db, host, commit):
    storage.of(db).canon.record_state(
        host, "ok", root=fixture.path("canon"), source="fixture", commit=commit,
        good=True, diagnostic="fixture canon state")


# -- placement -------------------------------------------------------------
def _placement_admitted(fixture, db, name, row, options):
    _pinned_commit(fixture, row)
    profile = db.query("SELECT %s AS p FROM agent_registry WHERE name = %%s"
                       % _profile_sql(), (name,))[0]["p"]
    canon_sync._set_placement(db, name, row["host"], placement.Verdict(
        name, row["host"], True, None, "hosts/%s.md" % row["host"], profile=profile))
    return {}


def _placement_refused(fixture, db, name, row, options):
    _pinned_commit(fixture, row)
    diagnostic = str(options.get("diagnostic") or "fixture: placement refused")
    canon_sync._set_placement(db, name, row["host"], placement.Verdict(
        name, row["host"], False, diagnostic, "hosts/%s.md" % row["host"]))
    return {}


def _profile_sql():
    from ..storage.postgres.placement import profile_sql
    return profile_sql()


# -- spend marker, reservations, cost --------------------------------------
def _spend_clear(fixture, db, name, row, options):
    registry.pending_spend_clear(db, name)
    budgets = storage.of(db).budgets
    for reservation in _reservations(db, name, ("open", "kept")):
        budgets.account_release(reservation["id"], actor=_STATE_ACTOR)
    return {}


def _spend_ambiguous(fixture, db, name, row, options):
    _spend_clear(fixture, db, name, row, options)
    start = int(options.get("start_index", 1))
    if not registry.pending_spend_put(db, name, start, turn="fixture-turn",
                                      model=row.get("model")):
        raise StateNotReached("the spend marker of %s was not written" % name)
    # A marker whose ledger key was lost (written before the key existed, or
    # by a crashed worker), next to an open reservation of another turn at the
    # same index: the reconciler cannot tell which turn it belongs to.
    db.execute("UPDATE spend_pending SET spend_key = NULL WHERE agent = %s", (name,))
    account = str(options.get("account") or FIXTURE_ACCOUNT)
    foreign_key = "%s:%d:foreign-%d" % (name, start, time.time_ns())
    storage.of(db).budgets.account_reserve(account, name, 0.01, foreign_key)
    return {"account": account}


def _reservation_open(fixture, db, name, row, options):
    account = str(options.get("account") or FIXTURE_ACCOUNT)
    usd = float(options.get("usd", 1.0))
    spend_key = str(options.get("spend_key") or _spend_key(name))
    budgets = storage.of(db).budgets
    if options.get("limit_usd") is not None or not budgets.account_usage(account):
        # An envelope is part of the state: without one the account is
        # unbounded and the report would carry no figure.
        budgets.account_put(account, float(options.get("limit_usd") or DEFAULT_LIMIT_USD),
                            actor=_STATE_ACTOR)
    got = budgets.account_reserve(account, name, usd, spend_key)
    if not got.get("ok"):
        raise StateNotReached("account %s refused a reservation of %.2f USD (envelope full)"
                              % (account, usd))
    return _budget(db, account, spend_key)


def _reservation_settled(fixture, db, name, row, options):
    # A real turn settles its OWN open reservation, after its cost row: the
    # agent's open reservation (or the named key) is settled, a new one is
    # opened only when there is none.
    budgets = storage.of(db).budgets
    spend_key = options.get("spend_key")
    account = str(options["account"]) if options.get("account") else None
    if not spend_key:
        opened = _reservations(db, name, ("open",), account=account)
        spend_key = opened[0]["spend_key"] if opened else None
    if spend_key:
        owners = db.query("SELECT agent, billing_account FROM account_reservations"
                          " WHERE spend_key = %s", (spend_key,))
        if owners and (owners[0]["agent"] != name or
                       (account is not None and owners[0]["billing_account"] != account)):
            raise StateNotReached("reservation does not belong to agent %s"
                                  " and the requested account" % name)
    reservation = budgets.account_get(spend_key) if spend_key else None
    if reservation is None:
        spend_key = _reservation_open(fixture, db, name, row, options)["spend_key"]
        reservation = budgets.account_get(spend_key)
    if reservation["state"] != "open":
        raise StateNotReached("reservation %s is %s, not open" % (spend_key,
                                                                reservation["state"]))
    cost = dict(options, spend_key=spend_key,
                usd=options.get("cost_usd", reservation["usd"]))
    cost.pop("account", None)
    _cost(fixture, db, name, row, cost)
    done = budgets.account_settle(spend_key, state="settled", actor=_STATE_ACTOR)
    if done is None:
        raise StateNotReached("no reservation carries spend key %s" % spend_key)
    return _budget(db, done["billing_account"], spend_key)


def _cost(fixture, db, name, row, options):
    usd = float(options.get("usd", 0.25))
    tokens = int(options.get("input_tokens", 1000))
    output = int(options.get("output_tokens", 100))
    spend_key = options.get("spend_key") or _spend_key(name)
    account = options.get("account")
    turn = "fixture-cost-%d" % time.time_ns()
    written = storage.of(db).turn_costs.insert(
        agent=name, harness=row.get("harness") or DEFAULT_HARNESS, turn=turn,
        model=row.get("model") or DEFAULT_MODEL, session=None, usd=usd,
        input_tokens=tokens, cached_input_tokens=0, output_tokens=output,
        cum_usd=usd, cum_input_tokens=tokens, cum_cached_input_tokens=0,
        cum_output_tokens=output, spend_key=spend_key)
    if not written:
        # The ledger keeps one row per turn key: a repeated key is a replay.
        raise StateNotReached("the cost ledger already has a row for spend key %s"
                              % spend_key)
    rows = db.query("SELECT count(*)::int AS n, coalesce(sum(usd), 0)::float8 AS usd"
                    " FROM turn_costs WHERE agent = %s AND spend_key = %s",
                    (name, spend_key))
    report = {"cost": {"spend_key": spend_key, "turn": turn, "usd": usd,
                       "rows": rows[0]["n"], "recorded_usd": rows[0]["usd"]}}
    if account:
        report.update(_budget(db, account, spend_key))
    return report


def _reservations(db, name, states, *, account=None):
    """This agent's reservations in ``states``, the latest first."""
    account_filter = " AND billing_account = %s" if account is not None else ""
    params = (name, list(states)) + ((account,) if account is not None else ())
    return db.query("SELECT id, spend_key FROM account_reservations"
                    " WHERE agent = %s AND state = ANY(%s)" + account_filter +
                    " ORDER BY id DESC", params)


def _spend_key(name):
    return "%s:fixture:%d" % (name, time.time_ns())


def _budget(db, account, spend_key):
    reservation = storage.of(db).budgets.account_get(spend_key) or {}
    usage = storage.of(db).budgets.account_usage(account)
    usage = usage[0] if usage else {}
    return {"account": account, "spend_key": spend_key,
            "reservation": {"state": reservation.get("state"), "usd": reservation.get("usd")},
            "budget": {key: usage.get(key) for key in ("usd", "settled", "reserved", "kept")}}


# -- what each state must show ---------------------------------------------
def _expect(condition, message):
    if not condition:
        raise StateNotReached(message)


def _check_live(report, details):
    lease = report["lease"]
    _expect(lease["live"] and lease["owner"],
            "lease of %s is not live: %r" % (report["agent"], lease))


def _check_expired(report, details):
    lease = report["lease"]
    _expect(lease["owner"] and not lease["live"],
            "lease of %s is not expired: %r" % (report["agent"], lease))


def _check_free(report, details):
    _expect(not report["lease"]["owner"], "lease of %s is still held" % report["agent"])


def _check_canon_fresh(report, details):
    _expect(report["canon"]["fresh"] is True,
            "canon of %s is not fresh: %r" % (report["agent"], report["canon"]))


def _check_canon_stale(report, details):
    _expect(report["canon"]["fresh"] is False and "canon_stale" in report["reasons"],
            "canon of %s is not stale: %r" % (report["agent"], report["canon"]))


def _check_admitted(report, details):
    _expect(report["placement"]["ok"] is True
            and "placement_refused" not in report["reasons"],
            "placement of %s is not admitted: %r" % (report["agent"], report["placement"]))


def _check_refused(report, details):
    _expect(report["placement"]["ok"] is False and "placement_refused" in report["reasons"],
            "placement of %s is not refused: %r" % (report["agent"], report["placement"]))


def _check_ambiguous(report, details):
    _expect(report["spend"]["ledger_ambiguous"],
            "spend marker of %s is not ambiguous: %r" % (report["agent"], report["spend"]))


def _check_clear(report, details):
    _expect(not report["spend"]["marker"],
            "spend marker of %s is still present" % report["agent"])


def _check_reservation(expected):
    def check(report, details):
        state = (report.get("reservation") or {}).get("state")
        _expect(state == expected, "reservation of %s is %r, not %s"
                % (report["agent"], state, expected))
    return check


def _check_cost(report, details):
    _expect(report["cost"]["rows"] >= 1, "no cost row for %s" % report["agent"])


_HANDLERS: Dict[str, Callable] = {
    "lease_live": _lease_live,
    "lease_foreign": _lease_foreign,
    "lease_expired": _lease_expired,
    "lease_free": _lease_free,
    "canon_fresh": _canon_fresh,
    "canon_stale": _canon_stale,
    "placement_admitted": _placement_admitted,
    "placement_refused": _placement_refused,
    "spend_ambiguous": _spend_ambiguous,
    "spend_clear": _spend_clear,
    "reservation_open": _reservation_open,
    "reservation_settled": _reservation_settled,
    "cost": _cost,
}

_CHECKS: Dict[str, Callable] = {
    "lease_live": _check_live,
    "lease_foreign": _check_live,
    "lease_expired": _check_expired,
    "lease_free": _check_free,
    "canon_fresh": _check_canon_fresh,
    "canon_stale": _check_canon_stale,
    "placement_admitted": _check_admitted,
    "placement_refused": _check_refused,
    "spend_ambiguous": _check_ambiguous,
    "spend_clear": _check_clear,
    "reservation_open": _check_reservation("open"),
    "reservation_settled": _check_reservation("settled"),
    "cost": _check_cost,
}

assert set(_HANDLERS) == set(AGENT_STATES) == set(_CHECKS) == set(STATE_OPTIONS)
