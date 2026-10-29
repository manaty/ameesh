# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP2) — the score of the candidates of an elected role.

Study `docs/design/etudes/election-pilote.md` §2-§3. EP1 made a governance
role claimable from any candidate host; this module decides **who should win
and when**, without ever replacing the lease:

* **availability** — the share of the last seven days covered by a fresh
  `host_resources` reading (one sample stands for the host's staleness
  threshold). A host that was off a week scores near zero; a workstation that
  sleeps at night scores well below a VM that never stops. The denominator is
  always the full seven days, even for a host that appeared an hour ago: a
  newcomer has to prove itself before it can beat an old VM;
* **readiness** — a gate, not a score. Two parts: what the **candidate host
  itself** knows and only it can know (`turn_hold`: known authentication
  failures, account pause/ceiling, mesh and agent budget, harness present), and
  what the **reader recomputes from the database** for every candidate
  (placement admitted, host gate, canon `ok`, fresh reading, fresh standing).
  The standing table carries only the local part; a stale standing is
  ineligible and leaves the ranking;
* **capacity** — the room left under the host's limits (load and memory),
  `0..1`, used only as a tie-break between equal availabilities. It never
  enters the five-point hysteresis, which compares availability alone;
* **rank delay** — before trying `registry.claim`, a candidate waits
  `rank * RANK_DELAY_S` from the first time this runner saw the lease free
  (best: 0 s, then 20 s, 40 s…). Only fresh, ready candidates are ranked: a
  dead better candidate does not make the second wait. The delay is
  client-side and polite: the atomic CAS of `registry.claim` remains the only
  arbiter, so two hosts that try together still produce one holder;
* **hysteresis** — a ready holder keeps the role as long as it renews. A better
  candidate takes over only through a planned handover: it has stayed at
  least `HYSTERESIS_POINTS` availability points above the holder for a full
  hour (read from the standing history, with no hole beyond the freshness
  threshold), and the switch waits for the holder's end of turn. A workstation
  that boots therefore does not take the role back from a VM. Once the holder
  has released for a planned handover, it does not reclaim before the
  challenger's rank delay plus `HANDOVER_RECLAIM_MARGIN_S`: a dead challenger
  must not leave the role free forever (`Runner.election_claim_allowed`).

Each candidate host publishes its own standing (`role_standings`,
`role_standings_pg.py`, pruned after two days); every host reads the fresh
ones. `ameesh role status` prints the holder, the epoch and the ranked
candidates.

#3568: the standing also carries the candidate's **route** (`notify_ok`, the
local ability to push an alert through ntfy/slack, resolved by
`role_timers.notify_reach`). It does not enter readiness — a missing secret
must not make the role's lease ineligible (#3402) — but it elects the pass
separately: `alerts_delegate` gives the `alerts` timer to the holder when it
can push, otherwise to the first fresh candidate that can. The delivery thus
stops depending on the route of whoever happens to hold the lease.

#3575: two edges of that rule. `notify_ok` also requires the host's
`ameesh-role-alerts.timer` (installed, current, enabled): a route alone would
elect a host that never runs the pass. And the fallback keeps its **last
executor** while its shared `notify_liveness` beat is fresh, so two routed
candidates no longer alternate at every capacity tie-break.

#4468: a fresh standing and a published route prove a candidate **could** push,
not that its `alerts` pass runs: a stopped timer (#4437) or a pass hung past
its systemd timeout (#4469) leaves `notify_ok` true while the shared beat ages.
Every routed candidate therefore keeps the pass only while its own
`notify_liveness` beat is fresh (for the holder, a beat of its current tenure:
a beat written before `agent_registry.lease_since` proves nothing). Two
bounded graces (`ALERTS_GRACE_S`) cover a host that has not beaten yet: a
freshly claimed holder, and the host the pass was just entrusted to
(`alerts_delegations`, written by `role_run` under the shared execution
lease; only a host change starts a new window, so a wrapper that re-runs
without beating does not renew its own grace). When nobody proves a live
pass, the routed candidates take turns from the last evidence (the last
delivery, else the last handover, else the holder's tenure), one per bounded
window: the pass leaves a delegate that stopped beating, a delegate whose
timer never starts cannot keep it for good, and a recovered host gets its
turn back.
"""
from __future__ import annotations

import bisect
import time
from typing import Mapping, Sequence

from . import db as db_mod
from . import host_lifecycle
from . import role_handover_pg as handover_pg
from . import role_standings_pg as pg
from . import storage

#: period of a candidate's self-publication (a runner pass)
EVAL_INTERVAL_S = 60.0
#: #4468: how long a host that has not beaten yet (a freshly claimed holder,
#: or the delegate the pass was just entrusted to) may keep the alerts pass
#: before another routed candidate takes it. The alerts timer runs every 30 s
#: (`role_timers.ALERTS_INTERVAL_S`): four ticks cover a fresh executor, its
#: first pass and a slow database, without hiding a pass that never starts.
ALERTS_GRACE_S = 120.0
#: waiting time added per rank before trying to claim
RANK_DELAY_S = 20.0
#: the hysteresis margin, in availability points, and how long it must hold
HYSTERESIS_POINTS = 5.0
HYSTERESIS_S = 3600.0
#: how close in time the holder's standing must be to the challenger's one
MATCH_S = 180.0
#: the old holder waits this long, after a planned release, before reclaiming
#: (the challenger is rank 0 and may have crashed: the role must not stay free)
HANDOVER_RECLAIM_MARGIN_S = 60.0
#: #4086/#4253: an executor that stops while holding an elected role reserves
#: it to its own host this long: the restarted executor reclaims first and
#: keeps the session; a host that does not come back leaves the role to the
#: ranked candidates once it expires. 90 s was shorter than the measured
#: restart: on the always-on hosts the worst stop-to-reclaim over seven days
#: is 195 s (28/10: 179 s), so the reservation covers it with margin. With the
#: rank delay of the next ready candidate (20 s for the current two
#: candidates, 40 s with three) the relief stays under five minutes (§11).
RESTART_RESERVE_S = 240.0
#: `_capacity`: a load of 2×CPU is the default admission ceiling (L31, 0028)
LOAD_CEILING_FACTOR = 2.0
#: memory headroom that counts as full capacity (above the 1 GiB floor)
MEM_HEADROOM_BYTES = 4 * 1024 ** 3


# --------------------------------------------------------------------------
# availability, capacity, score
# --------------------------------------------------------------------------
def freshness_s(db, host: str, *, now: float | None = None,
                fallback_interval_s: float | None = None) -> float:
    """The staleness threshold of `host`: `max(180 s, 3 × its cadence)`.

    The cadence is measured from the host's own readings (median gap over the
    last hour), so a host that samples every ten minutes is not declared
    stale after one missed minute. `fallback_interval_s` is the configured
    interval of a local publisher, used before its first history exists. Same
    threshold as L232e (`host_lifecycle.staleness_s`), never a fixed 300 s.
    """
    now = pg.now_ts(db) if now is None else float(now)
    interval = pg.sample_interval_s(db, host, now=now)
    if interval is None:
        interval = fallback_interval_s
    return host_lifecycle.staleness_s(interval)


def availability(db, host: str, *, now: float | None = None,
                 cover_s: float | None = None) -> dict:
    """The 7-day availability of `host` from its `host_resources` samples.

    A sample covers the following `cover_s` seconds (default: the host's own
    freshness threshold), capped by the next sample and by *now*: a longer gap
    is unavailable time. The denominator is the full seven days, not the time
    since the first sample: a host that only appeared yesterday has no
    seven-day track record yet and scores low, which is what the study asks
    (*share of minutes over the last 7 days with a fresh resource sample*).

    Returns `availability` (0..100), `covered_s`, `span_s`, `first_ts`,
    `last_ts`, `age_s` and `samples`. No sample at all: availability 0 and
    `last_ts` None (the caller refuses readiness). Readings dated in the
    future are ignored.
    """
    now = pg.now_ts(db) if now is None else float(now)
    start = now - pg.WINDOW_S
    if cover_s is None:
        cover_s = freshness_s(db, host, now=now)
    # #3921: summed by the database over `host_presence`, never the 7 days of
    # readings brought back one by one
    cov = pg.coverage(db, host, start, now=now, cover_s=cover_s)
    covered = cov["covered_s"]
    span = now - start
    return {
        "availability": round(100.0 * covered / span, 3) if span > 0 else 0.0,
        "covered_s": round(covered, 3),
        "span_s": round(span, 3),
        "first_ts": cov["first_ts"],
        "last_ts": cov["last_ts"],
        "age_s": (now - cov["last_ts"]) if cov["last_ts"] is not None else None,
        "samples": cov["samples"],
    }


def capacity(reading: Mapping | None) -> float:
    """Room left under the host's limits (0..1), a tie-break only.

    Load uses the default admission ceiling (2×CPU, L31/0028) and memory the
    default floor plus 4 GiB of headroom; a host that does not measure a
    field has no tie-break advantage (0.0). It is deliberately coarse: the
    score that decides and the hysteresis compare `availability` alone.
    """
    reading = reading or {}
    load = reading.get("load1")
    cpus = reading.get("cpu_count")
    load_room = 0.0
    if isinstance(load, (int, float)) and isinstance(cpus, int) and cpus > 0:
        load_room = max(0.0, min(1.0, 1.0 - float(load) / (LOAD_CEILING_FACTOR * cpus)))
    available = reading.get("mem_available_bytes")
    mem_room = 0.0
    if isinstance(available, (int, float)):
        mem_room = max(0.0, min(1.0, float(available) / MEM_HEADROOM_BYTES))
    return round(min(load_room, mem_room), 3)


def score(availability_value: float, capacity_value: float = 0.0) -> float:
    """The election score: availability (0..100) plus a capacity tie-break.

    Capacity contributes less than one point, so it can only order candidates
    whose availability is (nearly) equal. The five-point hysteresis is
    measured on availability alone (`_streak`), so capacity can never
    manufacture the margin.
    """
    return round(float(availability_value) + float(capacity_value), 3)


# --------------------------------------------------------------------------
# readiness: the local part (published) and the reader's part (recomputed)
# --------------------------------------------------------------------------
def _probe_row(role: Mapping, host: str) -> dict:
    """A clean row for `account_turn.turn_hold` on a candidate host.

    The registered row describes the last holder (its status, session account
    and possibly its budget pause): none of that is a property of this host.
    A new holder opens a new session, so the account is left to the host's own
    choice (`session_account=None`).
    """
    return {
        "name": role.get("name"),
        "host": host,
        "harness": role.get("harness") or "",
        "model": role.get("model"),
        "session_account": None,
        "status": "idle",
        "status_text": "",
        "budget_usd": role.get("budget_usd"),
        "spent_usd": role.get("spent_usd"),
        "operator_paused": False,
        "responsible": role.get("responsible"),
        "canon": role.get("canon"),
        "canon_ref": role.get("canon_ref"),
        "team": role.get("team"),
        "chantier": role.get("chantier"),
        "ephemeral": False,
    }


def _failing_credentials(db, host: str, harness: str) -> dict[str, dict]:
    from . import auth_failure
    return auth_failure.failing(db, host, harness)


def _auth_reason(cfg, db, role: Mapping, host: str) -> str:
    """A known failing credential that would block the role's harness.

    Mirrors the executor's own `auth_ready`: with declared accounts, at least
    one profile must not be failing (the account choice then skips the sick
    ones); without declared accounts, the harness default credential (`''`)
    must not be failing. Freshness is not required here — a credential with no
    recent probe is not declared unsound, and the turn itself will report it.
    """
    from . import accounts as accounts_mod
    try:
        failing = _failing_credentials(db, host, role.get("harness") or "")
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return "credential health unreadable"
    if not failing:
        return ""
    try:
        items = accounts_mod.profiles(cfg, role.get("harness") or "")
    except accounts_mod.AccountError as exc:
        return "invalid account configuration: %s" % exc
    if not items:
        sick = failing.get("")
        return ("harness login failing (%s)" % (sick.get("reason") or "auth")
                if sick else "")
    if all(profile.name in failing for profile in items):
        return ("all accounts failing (%s)"
                % ", ".join(sorted(str(failing[profile.name].get("reason") or "auth")
                                   for profile in items)))
    return ""


def _harness_reason(role: Mapping) -> str:
    """The role's harness must exist on this host (binary and descriptor)."""
    harness = role.get("harness") or ""
    if not harness:
        return "no harness declared"
    try:
        from . import adapters
        adapters.adapter_for(harness)
    except Exception as exc:  # HarnessMissing, OSError, unreadable descriptor
        return "harness %s unavailable (%s)" % (harness, type(exc).__name__)
    return ""


def local_readiness(cfg, db, role: Mapping, host: str) -> tuple[bool, str]:
    """What only the candidate host can know: could a turn start there?

    Harness present, no known failing credential, and `account_turn.turn_hold`
    empty (account pause/ceiling, mesh and agent budget). The placement, host
    gate, canon and freshness are recomputed by the reader, never trusted from
    this host's own word.
    """
    reasons: list[str] = []
    harness = _harness_reason(role)
    if harness:
        reasons.append(harness)
    auth = _auth_reason(cfg, db, role, host)
    if auth:
        reasons.append(auth)
    if not reasons:
        from . import account_turn
        try:
            hold = account_turn.turn_hold(
                cfg, db, role.get("name") or "", _probe_row(role, host))
        except db_mod.Unavailable:
            raise
        except db_mod.DbError as exc:
            hold = "account state unavailable (%s)" % db_mod.explain(exc)
        if hold:
            reasons.append("no turn could start: %s" % hold)
    if reasons:
        return False, "; ".join(reasons)
    return True, ""


def _gate_reason(db, host: str, reading: Mapping | None = None) -> str:
    """Host gate: durable withdrawal/return (L232e), owner hold (#254), and
    the gate state published by the host's own resource reading."""
    try:
        control = storage.of(db).host_lifecycle.get(host)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return "host lifecycle unreadable"
    mode = str((control or {}).get("mode") or "")
    if mode in ("draining", "lost"):
        return "host gate closed (%s)" % mode
    try:
        holds = [row for row in storage.of(db).hosts.holds()
                 if row.get("host") == host]
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return "host holds unreadable"
    if holds:
        hold = holds[0]
        return "host held by %s: %s" % (hold.get("author") or "?",
                                        hold.get("reason") or "hold")
    gate_state = str((reading or {}).get("gate_state") or "")
    if gate_state in ("draining", "stopped"):
        return "host gate %s" % gate_state
    return ""


def _canon_reason(db, role: Mapping, host: str) -> str:
    """The role's canon must have a valid, current state on this host."""
    canon = role.get("canon") or ""
    try:
        state = storage.of(db).canon.state(host, canon)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return "canon state unreadable"
    if state is None:
        return "canon not synced on this host"
    if state.get("status") != "ok":
        return ("canon %s on this host (%s)"
                % (state.get("status") or "unknown",
                   (state.get("diagnostic") or "")[:120]))
    return ""


def _placement_reason(db, role: Mapping, host: str) -> str:
    """The candidate verdict of this host, written by its own `canon sync`.

    It must still be *current*: the claim CAS also compares the verdict's canon
    and profile (host, harness, provider, model, credentials) with the role's
    own, so a verdict written before a rename or a card change is refused by
    `registry.claim`; ranking it ready would only make the next candidate wait.
    """
    row = pg.candidate(db, role.get("name") or "", host)
    if row is None:
        return "no placement verdict on this host (canon sync)"
    if row.get("canon_fresh") is False:
        return "placement verdict is from another canon (canon sync)"
    if row.get("placement_profile_fresh") is False:
        return "placement verdict predates the current card (canon sync)"
    if row.get("placement_ok") is not True:
        return "placement refused: %s" % ((row.get("placement_diagnostic") or "")
                                          or "not admitted")
    return ""


def _reading_reason(db, host: str, *, now: float | None = None,
                    fallback_interval_s: float | None = None) -> tuple[str, dict | None]:
    """A fresh, non-future resource reading: proof that the host is alive."""
    reading = pg.latest_reading(db, host)
    if reading is None:
        return "no resource reading for this host", None
    now = (float(reading.get("now_ts") or 0.0) if now is None else float(now))
    # #4086: the caller's `now` may predate this read (one instant for a whole
    # candidacy); the row's own clock, read after it, is never behind it.
    age = max(now, float(reading.get("now_ts") or 0.0)) - float(
        reading.get("sampled_ts") or 0.0)
    if age < 0:
        return "resource reading dated in the future", reading
    limit = freshness_s(db, host, now=now,
                        fallback_interval_s=fallback_interval_s)
    if age > limit:
        return ("stale resource reading (%ds old, limit %ds)"
                % (int(age), int(limit))), reading
    return "", reading


def _notify_reach(cfg, role: Mapping) -> tuple[bool, str]:
    """#3568: can THIS candidate push an alert to the role's required humans?

    Reuses the rule of the alerts timer (`role_timers.notify_reach`): the
    role's responsible and `notify.default_human` must each have a remote
    channel (`ntfy`, `slack`) whose secret resolves in the unit environment.
    A local configuration or secret gap is *reported*, never raised: it only
    chooses the alerts delegate, it never makes the role ineligible (#3402:
    a missing secret is not a negative result, still less a deploy failure).

    #3575: a route is not enough — the delegate must be a host that will
    *run* the pass. The `ameesh-role-alerts.timer` must be installed, current
    and enabled there too; otherwise `notify_ok=true` would elect a candidate
    whose timer does not exist (or still predates `--pass alerts`), and the
    delivery would stay silent while another routed, equipped candidate
    waits.

    #4114: one alerts unit per host, rendered for the role that carries the
    pass there (`mesh-design` by default). The check compares it with the
    template of **that** role, not of `role` (the role being evaluated):
    the pass is global, so the host runs it whatever role the unit names. The
    old comparison made auditeur and orchestrateur read a false "out of date"
    on every host and publish `notify_ok=false`.
    """
    try:
        from . import role_timers
        humans, error = role_timers.notify_reach(
            cfg, responsible=role.get("responsible"))
    except Exception as exc:  # noqa: BLE001 - a local gap never breaks the election
        return False, "notify reach unreadable: %s" % " ".join(str(exc).split())[:200]
    if not humans:
        return False, error or "no remote push route on this host"
    try:
        gap = role_timers.alerts_timer_gap(role=role.get("name") or "")
    except Exception as exc:  # noqa: BLE001 - a local gap never breaks the election
        return False, "alerts timer unreadable: %s" % " ".join(str(exc).split())[:200]
    if gap:
        return False, gap
    return True, ""


def evaluate(cfg, db, role: Mapping, host: str, *, now: float | None = None) -> dict:
    """One candidate's **local** standing: availability, capacity, score.

    `ready` is the local part only (`local_readiness`); the reader adds the
    placement, gate, canon and freshness checks (`candidate_state`).
    `notify_ok` is published beside it (#3568): the candidate can push an
    alert from this host. It is deliberately NOT part of `ready`: a missing
    route does not make the role's lease ineligible, it delegates the
    `alerts` pass to a candidate that can push (`alerts_delegate`).
    """
    now = pg.now_ts(db) if now is None else float(now)
    cover = freshness_s(db, host, now=now,
                        fallback_interval_s=getattr(cfg, "resource_interval", None))
    av = availability(db, host, now=now, cover_s=cover)
    reading = pg.latest_reading(db, host)
    cap = capacity(reading)
    ready, diagnostic = local_readiness(cfg, db, role, host)
    notify_ok, notify_diagnostic = _notify_reach(cfg, role)
    if role.get("operator_paused"):
        ready = False
        pause = "operator pause"
        if role.get("operator_pause_reason"):
            pause += ": " + str(role["operator_pause_reason"])
        diagnostic = "; ".join(part for part in (diagnostic, pause) if part)
    return {
        "name": role.get("name"),
        "host": host,
        "availability": av["availability"],
        "capacity": cap,
        "score": score(av["availability"], cap),
        "ready": ready,
        "diagnostic": diagnostic,
        "notify_ok": notify_ok,
        "notify_diagnostic": notify_diagnostic,
        "sampled": av,
    }


def candidate_state(db, role: Mapping, host: str, *, now: float | None = None,
                    standing: Mapping | None = None) -> dict:
    """The full readiness of one candidate, as any reader recomputes it.

    Local part from the standing (harness, credential, account/budget) AND the
    database part (placement, gate, canon, fresh reading, fresh standing). A
    standing older than the host's freshness threshold, or dated in the
    future, is not eligible; its diagnostic says why.
    """
    now = pg.now_ts(db) if now is None else float(now)
    row = standing if standing is not None else (pg.latest(
        db, role.get("name") or "").get(host) or {})
    limit = freshness_s(db, host, now=now)
    stamp = row.get("evaluated_ts")
    if not row or stamp is None:
        fresh = False
        freshness = "no standing (host silent)"
    else:
        age = now - float(stamp)
        fresh = 0 <= age <= limit
        if age < 0:
            freshness = "standing dated in the future"
        elif age > limit:
            freshness = "standing %ds old (limit %ds)" % (int(age), int(limit))
        else:
            freshness = ""
    reasons: list[str] = []
    if freshness:
        reasons.append(freshness)
    elif not row.get("ready"):
        reasons.append(row.get("diagnostic") or "no turn could start on this host")
    placement = _placement_reason(db, role, host)
    if placement:
        reasons.append(placement)
    reading_reason, reading = _reading_reason(db, host, now=now)
    if reading_reason:
        reasons.append(reading_reason)
    gate = _gate_reason(db, host, reading)
    if gate:
        reasons.append(gate)
    canon = _canon_reason(db, role, host)
    if canon:
        reasons.append(canon)
    return {
        "host": host,
        "fresh": fresh,
        "local_ready": bool(row.get("ready")),
        "ready": not reasons,
        "diagnostic": "; ".join(reasons),
        "notify_ok": bool(row.get("notify_ok")),
        "notify_diagnostic": str(row.get("notify_diagnostic") or ""),
        "freshness_limit_s": limit,
        "standing": row,
    }


# --------------------------------------------------------------------------
# publication (each candidate host publishes only its local part)
# --------------------------------------------------------------------------
def publish_self(cfg, db, host: str, *, interval_s: float = EVAL_INTERVAL_S,
                 now: float | None = None, force: bool = False,
                 names: Sequence[str] | None = None,
                 pressure: Mapping | None = None) -> int:
    """Publish the local standing of `host` for every elected role it may hold.

    At most one row per role and per `interval_s` unless `force`; a role this
    host is not a candidate for is skipped. `names` restricts publication to
    the agents an executor can actually claim (its `agents_filter`): a runner
    that cannot claim a role must not publish readiness for it, or a holder
    would hand that role over to a challenger that never claims (#3099).
    Database errors propagate (the caller decides whether they are fatal); a
    role whose evaluation fails is still published as not ready, so silence
    never looks like readiness.
    """
    published = 0
    for role in pg.elected_roles(db, host):
        # même convention que `registry.claimable` : une liste vide ne filtre
        # pas, seule une liste non vide restreint la publication
        if names and role["name"] not in names:
            continue
        latest = pg.latest(db, role["name"]).get(host) or {}
        age = None
        if latest.get("evaluated_ts") is not None:
            now_ts = latest.get("now_ts") or pg.now_ts(db)
            age = float(now_ts) - float(latest["evaluated_ts"])
        if not force and age is not None and age < max(1.0, float(interval_s)):
            continue
        try:
            standing = evaluate(cfg, db, role, host, now=now)
            # Effective runner limits include live cgroup/PSI readings.
            if pressure and pressure.get("critical"):
                standing["ready"] = False
                standing["diagnostic"] = "; ".join(filter(None, (
                    standing["diagnostic"], "critical host pressure")))
        except db_mod.Unavailable:
            raise
        except db_mod.DbError as exc:
            standing = {
                "availability": 0.0, "capacity": 0.0, "score": 0.0,
                "ready": False,
                "diagnostic": "evaluation failed: %s" % db_mod.explain(exc),
                "notify_ok": False,
                "notify_diagnostic": "evaluation failed",
            }
        pg.record(db, role["name"], host,
                  availability=standing["availability"],
                  capacity=standing["capacity"], score=standing["score"],
                  ready=standing["ready"], diagnostic=standing["diagnostic"],
                  notify_ok=standing.get("notify_ok", False),
                  notify_diagnostic=standing.get("notify_diagnostic", ""))
        published += 1
    return published


# --------------------------------------------------------------------------
# ranking, rank delay, hysteresis
# --------------------------------------------------------------------------
def candidacy(db, name: str, *, now: float | None = None) -> list[dict]:
    """The latest standing of every declared candidate host, ranked.

    `eligible` is the full readiness (fresh, local-ready, admitted, gate open,
    canon ok, fresh reading). The ranking is availability descending, then the
    capacity tie-break, then the host name (stable). `rank` and `delay_s` are
    only set for eligible candidates, so a dead better candidate does not make
    the second wait.
    """
    now = pg.now_ts(db) if now is None else float(now)
    from . import registry
    role = registry.get(db, name)
    if role is None:
        raise ValueError("unknown agent: %s" % name)
    declared = list(role.get("candidats") or [])
    latest = pg.latest(db, name)
    placed = {row["host"]: row for row in pg.candidate_rows(db, name)}
    out = []
    for host in sorted(set(declared) | set(latest)):
        row = latest.get(host) or {}
        candidate = placed.get(host) or {}
        state = candidate_state(db, role, host, now=now, standing=row)
        admitted = candidate.get("placement_ok") is True
        declared_here = host in declared
        stopped = role.get("status") == "stopped"
        ready = bool(state["ready"] and admitted and declared_here and not stopped)
        diagnostic = state["diagnostic"]
        if stopped:
            diagnostic = "; ".join(part for part in (diagnostic, "role stopped")
                                   if part)
        out.append({
            "host": host,
            "declared": declared_here,
            "admitted": admitted,
            "fresh": state["fresh"],
            "ready": ready,
            "availability": (None if not row
                             else float(row.get("availability") or 0.0)),
            "capacity": None if not row else float(row.get("capacity") or 0.0),
            "score": None if not row else float(row.get("score") or 0.0),
            "diagnostic": diagnostic,
            "notify_ok": bool(state.get("notify_ok")),
            "notify_diagnostic": str(state.get("notify_diagnostic") or ""),
            "evaluated_ts": row.get("evaluated_ts"),
            "age_s": (now - float(row["evaluated_ts"])) if row.get("evaluated_ts") else None,
        })
    eligible = sorted(
        (item for item in out if item["ready"]),
        key=lambda item: (-item["score"], -item["capacity"], item["host"]))
    rest = sorted((item for item in out if not item["ready"]),
                  key=lambda item: item["host"])
    ordered = eligible + rest
    for rank, item in enumerate(eligible):
        item["rank"] = rank
        item["delay_s"] = rank_delay_s(rank)
    for item in rest:
        item["rank"] = None
        item["delay_s"] = None
    return ordered


def _liveness(db, role: Mapping) -> dict[str, float]:
    """#4468: the latest shared alerts beat per host for the role's human.

    The pass records a `notify_liveness` beat for every human it reaches
    (`notify_gap.beat`), so every reader sees which host actually ran it.
    Stale beats are kept: the delegation must tell "has not run the pass in
    its current tenure" (bounded grace) from "ran and stopped beating"
    (delegate now). A beat dated a little in the future (host clock) is kept
    as is; freshness is the caller's rule, as in the deliverability alert.
    """
    from . import notify as notify_mod
    human = notify_mod.normalize_human(role.get("responsible"))
    if not human:
        return {}
    try:
        rows = storage.of(db).notify_liveness.latest()
    except Exception:  # noqa: BLE001 - liveness is evidence, never a gate
        return {}
    beats: dict[str, float] = {}
    for row in rows:
        if str(row.get("human") or "") != human:
            continue
        host = str(row.get("host") or "")
        if not host:
            continue
        seen = float(row.get("seen_ts") or 0.0)
        if seen > beats.get(host, float("-inf")):
            beats[host] = seen
    return beats


def _last_executor(db, role: Mapping, pool: Sequence[Mapping], *,
                   now: float, beats: Mapping[str, float] | None = None
                   ) -> Mapping | None:
    """#3575: the freshest routed candidate that already ran the alerts pass.

    The pass records a shared `notify_liveness` beat for every human it
    reaches (`notify_gap.beat`), so the last host that actually delivered is
    visible from every reader without a new table. While that beat is fresh
    (the same ``ALIVE_S`` as the deliverability alert), keeping that host
    stops two routed fallbacks from alternating at every capacity tie-break,
    each losing its own local notify state. A tie keeps the election's order.
    """
    from . import notify_gap
    if not pool:
        return None
    beats = _liveness(db, role) if beats is None else beats
    limit = float(notify_gap.ALIVE_S)
    pick = None
    seen = None
    for item in pool:  # election order breaks a tie deterministically
        at = beats.get(str(item.get("host") or ""))
        if at is None or at < now - limit:
            continue
        if pick is None or at > seen:
            pick, seen = item, at
    return pick


def _as_ts(value) -> float | None:
    """An epoch column as a float, or None when it is absent/unreadable."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _delegation_memory(db, role: Mapping) -> dict | None:
    """#4468: the host the alerts pass was last entrusted to, and when.

    The memory is a preference, never a gate: an unreadable row falls back to
    the beat order (see :func:`alerts_delegate`).
    """
    from . import alerts_delegation_pg
    try:
        return alerts_delegation_pg.latest(db, role.get("name") or "")
    except Exception:  # noqa: BLE001 - a missing memory never blocks a pass
        return None


def alerts_delegate(db, role: Mapping, *, holder: str | None = None,
                    ranked: Sequence[Mapping] | None = None,
                    now: float | None = None) -> dict:
    """#3568: which host runs the ``alerts`` pass, and why.

    The lease holder runs the pass when its published standing is fresh and
    says it can push (`notify_ok`). Otherwise the first fresh candidate that
    can push takes it, in the election's own order (readiness, availability,
    capacity, host), so every reader computes the same delegate: the delivery
    no longer goes silent because the elected lease fell to a host without a
    route (#3383). A pinned role keeps its host: there is nowhere to delegate.

    #3575: among those fallbacks, the **last executor** keeps the pass while
    its shared `notify_liveness` beat is fresh and it is still routed
    (:func:`_last_executor`): the capacity tie-break no longer makes two
    equipped hosts alternate and lose their local notify state each time.

    #4468: the same liveness rule applies to **every** routed candidate, the
    holder included. A host keeps the pass only while its own beat is fresh
    (for the holder, a beat of its current tenure: a beat written before
    `lease_since` proves nothing about this lease). Two bounded graces remain:

    * a freshly claimed holder that has not beaten yet gets the first pass
      (:data:`ALERTS_GRACE_S` from ``lease_since``), so its route, when
      it returns, still takes precedence (#3568/#3575);
    * the host the pass was last **entrusted** to (`alerts_delegations`,
      written by `role_run` under the shared execution lease) keeps it while
      it has not beaten yet and its grace runs. Only a host change starts a
      new window: a wrapper that re-runs without beating does not renew its
      own grace (review cx3).

    When nobody proves a live pass, the routed candidates take turns from the
    last evidence (the last delivery, else the last handover, else the
    holder's tenure), one per bounded window. The pass therefore leaves a
    delegate that stopped beating, a delegate whose timer never starts cannot
    keep it for good, and a recovered host gets its turn back instead of
    staying on the oldest or the newest dead beat.

    `holder` and `ranked` let a caller that already read them (`status`,
    `role run`) avoid a second, possibly incoherent read; they must come from
    the same election snapshot. `delegate` is None when no candidate proves a
    remote route: the timer then keeps standing by, and the shared
    `notify_undeliverable` alert names the silence.
    """
    from . import notify_gap
    now = pg.now_ts(db) if now is None else float(now)
    items = list(ranked) if ranked is not None else candidacy(
        db, role.get("name") or "", now=now)
    out = {
        "role": role.get("name"),
        "holder": holder,
        "delegate": None,
        "holder_notify_ok": False,
        "last_executor": None,
        "reason": "",
    }
    if str(role.get("releve") or "") != "election":
        out["delegate"] = holder
        out["reason"] = "pinned role: its host runs the pass"
        return out
    by_host = {item["host"]: item for item in items}
    current = by_host.get(holder) if holder else None
    beats = _liveness(db, role)
    alive_s = float(notify_gap.ALIVE_S)
    began = _as_ts(role.get("lease_since_ts"))
    holder_beat = _as_ts(beats.get(holder)) if holder else None
    holder_in_tenure = (holder_beat is not None and began is not None
                        and float(holder_beat) >= began)
    # A beat written before the current lease proves the pass ran in a
    # previous tenure, not this one (#4468, review cx3): it must not keep the
    # holder through the freshness rule nor through the last-executor one.
    proof = dict(beats)
    if holder and not holder_in_tenure:
        proof.pop(holder, None)
    # "Routed" is what matters for the pass: declared, fresh and able to push.
    # The election order already puts ready candidates first; a ready host
    # that does not run the pass is worth less than a routed one that does.
    pool = [item for item in items
            if item.get("declared") and item.get("fresh") and item.get("notify_ok")]
    in_pool = {item["host"]: item for item in pool}
    holder_item = in_pool.get(holder) if holder else None

    def keep(item: Mapping, reason: str) -> dict:
        out.update(delegate=item["host"],
                   holder_notify_ok=item["host"] == holder,
                   reason=reason)
        return out

    def holder_gap() -> str:
        if holder is None:
            return "no live holder"
        if current is None or not current.get("fresh") \
                or not current.get("notify_ok"):
            return "%s cannot push" % holder
        if not holder_in_tenure:
            return ("%s has not run the alerts pass in its current tenure"
                    % holder)
        return ("%s stopped beating (%ds since its last alerts beat)"
                % (holder, max(0, int(now - float(holder_beat)))))

    # 1. A freshly claimed holder that has not beaten yet gets the first pass:
    #    its route, when it returns, must take precedence (#3568/#3575), but
    #    only for a bounded grace.
    if (holder_item is not None and began is not None
            and now - began <= ALERTS_GRACE_S and not holder_in_tenure):
        return keep(holder_item,
                    "the holder can push but has not run the alerts pass yet "
                    "(%ds of grace left)"
                    % max(0, int(ALERTS_GRACE_S - (now - began))))

    # 2. The freshest in-tenure beat keeps the pass: the last-executor
    #    hysteresis of #3575, now applied to every routed candidate.
    last = _last_executor(db, role, pool, now=now, beats=proof)
    if last is not None:
        out["last_executor"] = last["host"]
        if last["host"] == holder:
            return keep(last, "the holder can push and its alerts pass beats")
        return keep(last, "%s; last executor %s keeps the pass"
                    % (holder_gap(), last["host"]))

    # 3. The host the pass was just entrusted to keeps it while it has not
    #    beaten yet: its wrapper is alive and about to write the beat. Bounded
    #    so a child that never starts cannot keep the pass for good.
    memory = _delegation_memory(db, role)
    mem_host = str(memory.get("host") or "") if memory else ""
    mem_ts = _as_ts(memory.get("entrusted_ts")) if memory else None
    mem_item = in_pool.get(mem_host) if mem_host else None
    mem_beat = _as_ts(beats.get(mem_host)) if mem_host else None
    if (mem_item is not None and mem_ts is not None
            and now - mem_ts <= ALERTS_GRACE_S
            and (mem_beat is None or mem_beat < mem_ts)):
        return keep(mem_item,
                    "%s was just entrusted the alerts pass and has not beaten "
                    "yet (%ds of grace left)"
                    % (mem_host,
                       max(0, int(ALERTS_GRACE_S - (now - mem_ts)))))

    # 4. Nobody proves a live pass: rotate from the last evidence — the last
    #    delivery, else the last handover, else the holder's tenure — one
    #    candidate per bounded window. The candidate that just used its grace
    #    is left for the next turn, so a delegate whose timer never starts
    #    cannot keep the pass for good; a recovered host gets its turn back.
    if not pool:
        out["reason"] = ("no fresh candidate can push"
                         + (" (holder %s cannot)" % holder if holder else ""))
        return out
    evidence = []
    if beats:
        host, seen = max(beats.items(), key=lambda row: (row[1], row[0]))
        evidence.append((float(seen), host))
    if mem_ts is not None:
        evidence.append((float(mem_ts), mem_host))
    if began is not None:
        evidence.append((float(began), holder or ""))
    anchor, base_host = max(evidence) if evidence else (None, "")
    elapsed = 0.0 if anchor is None else max(0.0, now - float(anchor) - alive_s)
    steps = int(elapsed // ALERTS_GRACE_S)
    order = [item["host"] for item in pool]  # election order, deterministic
    base = order.index(base_host) if base_host in order else -1
    pick = in_pool[order[(base + 1 + steps) % len(order)]]
    if pick["host"] == holder:
        return keep(pick, "%s; no other candidate proves a live alerts pass, "
                          "the holder takes it back" % holder_gap())
    return keep(pick, "%s; no candidate proves a live pass, %s gets the pass"
                      % (holder_gap(), pick["host"]))


def rank_delay_s(rank: int) -> float:
    """The politeness delay of a rank: 0 s for the best, then 20 s, 40 s…"""
    return max(0, int(rank)) * RANK_DELAY_S


def claim_wait(db, role: Mapping, host: str, free_since: float, *,
               now: float | None = None) -> tuple[bool, str, dict | None]:
    """May this host claim the role now, given its rank and the free instant?

    `free_since` is the monotonic instant this runner first saw the lease
    free. The caller keeps it per role; this function only applies the delay.
    Returns `(allowed, reason, standing)`. An unready or unpublished host is
    refused — the CAS would refuse it anyway (EP1 placement/canon), but the
    rank delay must not let it take the slot of a ready candidate.
    """
    now = time.monotonic() if now is None else float(now)
    ranked = candidacy(db, role["name"])
    mine = next((item for item in ranked if item["host"] == host), None)
    if mine is None or not mine["ready"]:
        reason = ((mine or {}).get("diagnostic") or
                  "no fresh standing for this host")
        return False, "not ready: " + reason, mine
    # #4086: a holder that stopped to restart keeps its role on its host for
    # `RESTART_RESERVE_S`: the others wait, its own executor skips the rank.
    reserved = handover_pg.restart_reservation(db, role["name"])
    if reserved is not None:
        if reserved["host"] != host:
            return (False, "restart reservation: waits %ds for %s"
                    % (int(float(reserved["left_s"])) + 1, reserved["host"]), mine)
        return True, "", mine
    delay = float(mine["delay_s"] or 0.0)
    ready_at = float(free_since) + delay
    if now < ready_at:
        return (False, "rank %d: waits %ds" % (mine["rank"], int(ready_at - now) + 1),
                mine)
    return True, "", mine


def _nearest(rows: Sequence[Mapping], ts: float) -> Mapping | None:
    """The row closest to `ts`, or None when nothing is within `MATCH_S`."""
    if not rows:
        return None
    stamps = [float(row["evaluated_ts"]) for row in rows]
    pos = bisect.bisect_left(stamps, ts)
    best = None
    for index in (pos - 1, pos):
        if 0 <= index < len(rows):
            if best is None or abs(stamps[index] - ts) < abs(float(best["evaluated_ts"]) - ts):
                best = rows[index]
    if best is None or abs(float(best["evaluated_ts"]) - ts) > MATCH_S:
        return None
    return best


def _streak(history: Sequence[Mapping], challenger: str, holder: str,
            now: float, *, gap_limit: float) -> dict | None:
    """The current uninterrupted ≥ +5 streak of `challenger` over `holder`.

    Only evaluations matter; every challenger row needs a holder row within
    `MATCH_S`, both must be ready, and two challenger rows more than
    `gap_limit` apart break the streak. Returns `{since, seconds, gap}` when
    the streak covers a full hour, else None (fail closed: missing evidence
    never hands over a role).
    """
    rows = [row for row in history if row.get("host") in (challenger, holder)]
    mine = [row for row in rows if row.get("host") == challenger]
    theirs = [row for row in rows if row.get("host") == holder]
    if not mine or not theirs:
        return None
    floor = now - HYSTERESIS_S
    start = None
    previous_ts = None
    last_good = None
    for row in mine:
        ts = float(row["evaluated_ts"])
        other = _nearest(theirs, ts)
        ok = (other is not None and row.get("ready") and other.get("ready")
              and float(row.get("availability") or 0.0)
              - float(other.get("availability") or 0.0) >= HYSTERESIS_POINTS)
        if ok and previous_ts is not None and ts - previous_ts > gap_limit:
            ok = False
        if not ok:
            start = None
            last_good = None
            previous_ts = ts
            continue
        if start is None:
            start = ts
        last_good = row
        previous_ts = ts
    if last_good is None or start is None or start > floor:
        return None
    if float(last_good["evaluated_ts"]) != float(mine[-1]["evaluated_ts"]):
        return None
    other = _nearest(theirs, float(last_good["evaluated_ts"]))
    return {"since": start, "seconds": now - start,
            "gap": float(last_good.get("availability") or 0.0)
            - float(other.get("availability") or 0.0)}


def unready_successor(db, role: Mapping, *, now: float | None = None,
                      skip: Sequence[str] = ()) -> dict | None:
    """A ready standby for a known unready holder; the worker debounces this.

    Read-only, used between turns only. An absent holder is not evidence of
    poor health (attach/unadmitted runners must not trigger a handover).
    """
    if role.get("releve") != "election" or role.get("host") not in (role.get("candidats") or ()):
        return None
    if role.get("host") not in pg.latest(db, role["name"]):
        return None
    ranked = candidacy(db, role["name"], now=now)
    holder = next((r for r in ranked if r["host"] == role.get("host")), None)
    if holder is None or holder["ready"]:
        return None
    best = next((r for r in ranked if r["ready"]
                 and r["host"] != role.get("host") and r["host"] not in skip), None)
    if best is None:
        return None
    return dict(best, challenger=best["host"], health_reason=holder["diagnostic"])


def handover(db, role: Mapping, holder_host: str, *,
             now: float | None = None,
             skip: Sequence[str] = ()) -> dict | None:
    """Is a planned handover due at the holder's end of turn?

    `role` is the registry row of the elected role, `holder_host` the host
    that currently holds the lease and renews it. A challenger must be fully
    ready now, the holder fully ready and fresh, and the challenger must have
    stayed `HYSTERESIS_POINTS` above the holder for a full hour. `skip` names
    challengers the caller has just seen fail to claim after a planned
    release: they are ignored until their cooldown expires, so the holder
    does not hand the role over to the same dead challenger every minute
    (B1 of the #3011 review).
    """
    now = pg.now_ts(db) if now is None else float(now)
    skipped = {str(host) for host in skip}
    declared = [host for host in (role.get("candidats") or [])
                if host != holder_host and host not in skipped]
    if not declared:
        return None
    latest = pg.latest(db, role["name"])
    holder_state = candidate_state(db, role, holder_host, now=now,
                                   standing=latest.get(holder_host))
    if not holder_state["ready"]:
        return None
    limits = {host: freshness_s(db, host, now=now) for host in declared}
    limits[holder_host] = holder_state["freshness_limit_s"]
    since = now - HYSTERESIS_S - 2 * max(limits.values())
    history = pg.history(db, role["name"], since)
    due = []
    for host in declared:
        row = latest.get(host) or {}
        state = candidate_state(db, role, host, now=now, standing=row)
        if not state["ready"]:
            continue
        gap_limit = max(state["freshness_limit_s"], limits[holder_host])
        found = _streak(history, host, holder_host, now, gap_limit=gap_limit)
        if found:
            due.append({"challenger": host, "since": found["since"],
                        "seconds": found["seconds"], "gap": found["gap"],
                        "score": float(row.get("score") or 0.0)})
    if not due:
        return None
    best = max(due, key=lambda item: (item["score"], -item["since"]))
    ranked = candidacy(db, role["name"], now=now)
    mine = next((item for item in ranked if item["host"] == best["challenger"]), {})
    best["delay_s"] = float(mine.get("delay_s") or 0.0)
    return best


def handover_due(db, role: Mapping, holder_host: str, *,
                 now: float | None = None,
                 skip: Sequence[str] = ()) -> dict | None:
    """`handover`, plus the holder-side lease check, for the worker loop."""
    now = pg.now_ts(db) if now is None else float(now)
    expires = role.get("lease_expires_ts")
    if not role.get("lease_owner") or expires is None or float(expires) <= now:
        return None
    return handover(db, role, holder_host, now=now, skip=skip)


# --------------------------------------------------------------------------
# observability
# --------------------------------------------------------------------------
def elected_names(db) -> list[str]:
    rows = db.query(
        "SELECT name FROM agent_registry WHERE releve = 'election'"
        " ORDER BY name")
    return [row["name"] for row in rows]


def status(db, name: str) -> dict:
    """The observable state of one elected role (`ameesh role status`).

    The holder is the host of a live lease; the epoch is the fencing token.
    Candidates are ranked only when fresh and fully ready; a stale
    publication is shown as such, never as an old score. `ahead_s` is the
    current ≥ +5 streak of the candidate over the holder (`None` below the
    margin).
    """
    from . import registry
    role = registry.get(db, name)
    if role is None:
        raise ValueError("unknown agent: %s" % name)
    now = pg.now_ts(db)
    expires = role.get("lease_expires_ts")
    holder = (role.get("host")
              if role.get("lease_owner") and expires is not None
              and float(expires) > now else None)
    ranked = candidacy(db, name, now=now)
    alerts = alerts_delegate(db, role, holder=holder, ranked=ranked, now=now)
    history = None
    for item in ranked:
        item["holder"] = item["host"] == holder
        item["ahead_s"] = None
        if holder and item["host"] != holder and item["ready"]:
            if history is None:
                limits = {row["host"]: freshness_s(db, row["host"], now=now)
                          for row in ranked}
                history = pg.history(
                    db, name, now - HYSTERESIS_S - 2 * max(limits.values() or [1.0]))
            found = _streak(history, item["host"], holder, now,
                            gap_limit=max(limits.get(item["host"]) or 0.0,
                                          limits.get(holder) or 0.0))
            if found:
                item["ahead_s"] = round(found["seconds"], 1)
    return {
        "name": role.get("name"),
        "releve": role.get("releve"),
        "elected": role.get("releve") == "election",
        "holder": holder,
        "lease_owner": role.get("lease_owner"),
        "epoch": role.get("lease_epoch"),
        "lease_expires_ts": expires,
        "lease_left_s": (float(expires) - now
                         if holder and expires is not None else None),
        "stopped": role.get("status") == "stopped",
        "candidates": ranked,
        "alerts_delegate": alerts,
    }


def format_status(report: Mapping) -> list[str]:
    """Human lines of one `role status` report."""
    name = report.get("name") or "?"
    if not report.get("elected"):
        return ["%s: not an elected role" % name]
    if report.get("stopped"):
        head = "%s: stopped (not elected)" % name
    elif report.get("holder"):
        head = ("%s: holder %s (epoch %s, lease %ds left)"
                % (name, report["holder"], report.get("epoch"),
                   max(0, int(report.get("lease_left_s") or 0))))
    else:
        head = "%s: no holder (epoch %s)" % (name, report.get("epoch"))
    lines = [head]
    alerts = report.get("alerts_delegate") or {}
    if alerts.get("delegate") and alerts.get("delegate") != report.get("holder"):
        lines.append("  alerts: %s (%s)" % (alerts["delegate"],
                                            alerts.get("reason") or "delegated"))
    lines.append("  %-22s %-5s %-5s %8s %8s %8s %5s %6s %7s  %s"
                 % ("candidate", "ready", "route", "avail.", "capacity",
                    "score", "rank", "delay", "ahead", "diagnostic"))
    for item in report.get("candidates") or ():
        if item.get("rank") is not None:
            rank = str(item["rank"])
            delay = "%ds" % int(item.get("delay_s") or 0)
        else:
            rank, delay = "—", "—"
        route = ("—" if item.get("evaluated_ts") is None
                 else ("yes" if item.get("notify_ok") else "no"))
        lines.append("  %-22s %-5s %-5s %8s %8s %8s %5s %6s %7s  %s"
                     % (item["host"] + (" *" if item.get("holder") else ""),
                        "yes" if item.get("ready") else "no",
                        route,
                        "—" if item.get("availability") is None
                        else "%.1f%%" % item["availability"],
                        "—" if item.get("capacity") is None
                        else "%.2f" % item["capacity"],
                        "—" if item.get("score") is None
                        else "%.1f" % item["score"],
                        rank, delay,
                        "—" if item.get("ahead_s") is None
                        else "%dmn" % int(item["ahead_s"] // 60),
                        item.get("diagnostic") or ""))
    lines.append("  (* holder; rank eligible = fresh, locally ready, gate "
                 "open, canon ok, placement admitted; route = can push an "
                 "alert from this host (#3568); delay = rank × %ds)"
                 % int(RANK_DELAY_S))
    return lines
