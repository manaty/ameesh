# SPDX-License-Identifier: AGPL-3.0-only
"""#6643 — the auditor's hourly short pass, played only when something moved.

The short pass is woken by the mail ``Passage court (chaque heure)``.  Until
#6643 the role timer sent that mail every hour, whatever had happened: each
pass was a full model turn even when nothing had arrived and the debit was
clean.  The timer now runs this tick under ``role run auditeur`` and the tick
decides, deterministically, whether the pass is needed:

* it is **played** (one mail, as before) when a mail arrived since the previous
  pass, when a deadline falls between the previous pass and the next hour, when
  ``ameesh debit --check`` would exit 2, when a head or a gate changed since the
  previous pass (a freeze, a merge, a CI conclusion, a staging event), or when
  any of those reads failed — an unreadable debit is never a quiet debit;
* it is **skipped** otherwise, and the skip is written to the mesh journal
  (``auditor.pass``, ``decision: skipped``) **without any mail**: no turn is
  opened.  The event also carries the state of the gates it compared, so the
  next tick can compare against it.

The previous pass is the last ``auditor.pass`` event for the role.  Its
``started_ts`` is the cutoff for mail arrivals: a message that arrived while
the previous pass was running is seen by the next tick, except the pass mail
itself (tagged ``payload.pass = auditeur-court``).  The gate state is the
maximum milestone (`frozen`, `verdict`, `fire`, `unfire`, `merged`), the CI
readings and the newest staging event: all append-only or monotonic, and all
cheap reads on an hourly tick.

The tick is code, never judgement (canon §7): it takes no decision the auditor
would have taken, it only decides whether there is anything for the auditor to
read.  The class is the role-timers one (sensitive); it is reviewed and then
packaged, never merged by its author alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field

from . import config as config_mod
from . import db as db_mod
from . import events, mail, storage
from . import work as work_mod
from .role_timers import AUDITOR_INTERVAL_S, AUDITOR_MAIL, AUDITOR_ROLE

#: the journal type of one tick (played or skipped); see `events.TYPES`
EVENT = "auditor.pass"
SCHEMA = "ameesh-auditor-pass/1"
#: the payload mark of the pass mail, so the next tick never counts it as news
PASS_TAG = "auditeur-court"

#: the milestone kinds that are a gate: a lot frozen, reviewed, fired or merged
GATE_MILESTONES = ("frozen", "verdict", "fire", "unfire", "merged")
#: the sections of the debit report whose due dates announce a deadline
DEADLINE_SECTIONS = ("wakes", "external_waits", "window_waits")

#: reasons of a decision, stable identifiers for the journal and the tests
FIRST_PASS = "first-pass"
MAIL = "mail"
GATE = "gate"
DEADLINE = "deadline"
DEBIT = "debit"
READ_ERROR = "read-error"
QUIET = "quiet"


@dataclass(frozen=True)
class Decision:
    """One tick's decision: a mail (`skip` false) or a journaled skip."""

    skip: bool
    reason: str
    detail: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"skip": self.skip,
                "decision": "skipped" if self.skip else "played",
                "reason": self.reason,
                "detail": dict(self.detail)}


def _played(reason: str, **detail) -> Decision:
    return Decision(False, reason, detail)


def _skipped(reason: str, **detail) -> Decision:
    return Decision(True, reason, detail)


def _event_data(row) -> dict:
    """The `data` object of an event row (psycopg gives a dict; psql text)."""
    data = (row or {}).get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def previous_pass(db, agent: str) -> dict | None:
    """The last tick event for `agent`, or None before the first one.

    Unbounded by age: the previous tick may be older than the retention window
    on a host that slept; its absence must play the pass, never skip it.
    """
    rows = storage.of(db).events.recent_type(EVENT, None, 1, object=agent)
    return rows[0] if rows else None


def _cutoff(previous: dict | None, now: float) -> float:
    """The arrival cutoff of this tick: when the previous tick started."""
    data = _event_data(previous)
    started = data.get("started_ts")
    try:
        if started is not None:
            return float(started)
    except (TypeError, ValueError):
        pass
    try:
        return float((previous or {}).get("ts"))
    except (TypeError, ValueError):
        return now - AUDITOR_INTERVAL_S


def mail_arrivals(db, agent: str, since_ts: float) -> dict:
    """Mail that arrived since `since_ts`, and mail still undelivered.

    The pass mail itself is excluded by its payload mark: the previous tick
    sent it after `since_ts`, and counting it would force a second pass every
    hour.  An undelivered message is counted even when older than the cutoff,
    so a tick that failed to deliver its own mail does not silence the next
    one.  A single indexed query on `agent_mailbox (recipient)`.
    """
    row = db.query(
        """
        SELECT count(*) FILTER (WHERE created_at > to_timestamp(%s)
                                  AND coalesce(payload->>'pass', '') <> %s)::int
                   AS arrivals,
               count(*) FILTER (WHERE delivered_at IS NULL
                                  AND coalesce(payload->>'pass', '') <> %s)::int
                   AS undelivered
          FROM agent_mailbox
         WHERE recipient = %s
        """, (float(since_ts), PASS_TAG, PASS_TAG, agent))[0]
    return {"arrivals": int(row.get("arrivals") or 0),
            "undelivered": int(row.get("undelivered") or 0)}


def gate_state(db) -> dict:
    """A compact, monotonic witness of the heads and gates of the mesh.

    * `milestones`: the newest gate milestone (freeze, verdict, fire, merge);
    * `merged`: the newest merge, the target head moving by construction;
    * `ci_seen` / `ci_concluded` / `ci_running`: a CI run appearing, concluding
      or being reopened;
    * `staging`: the newest staging event (`staging.attested`,
      `staging.sequence`, `staging.request`), read on its partial index.

    All values are monotonic apart from `ci_running`; an equality of the whole
    mapping is therefore "nothing moved".  SQL is `::float8`/`::bigint` so the
    JSON round-trip gives back the same Python types.
    """
    row = db.query(
        """
        SELECT (SELECT coalesce(max(id), 0) FROM work_item_milestones
                 WHERE kind IN (%s))::bigint AS milestones,
               (SELECT coalesce(max(id), 0) FROM work_item_milestones
                 WHERE kind = 'merged')::bigint AS merged,
               (SELECT coalesce(max(extract(epoch from first_seen_at)), 0)::float8
                  FROM ci_runs) AS ci_seen,
               (SELECT coalesce(max(extract(epoch from concluded_at)), 0)::float8
                  FROM ci_runs) AS ci_concluded,
               (SELECT count(*) FROM ci_runs WHERE status = 'running')::bigint
                   AS ci_running,
               (SELECT coalesce(max(id), 0) FROM ameesh_events
                 WHERE type IN ('staging.attested', 'staging.sequence',
                                'staging.request'))::bigint AS staging
        """ % ", ".join("'%s'" % kind for kind in GATE_MILESTONES))[0]
    return {"milestones": int(row.get("milestones") or 0),
            "merged": int(row.get("merged") or 0),
            "ci_seen": float(row.get("ci_seen") or 0.0),
            "ci_concluded": float(row.get("ci_concluded") or 0.0),
            "ci_running": int(row.get("ci_running") or 0),
            "staging": int(row.get("staging") or 0)}


def _due(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def deadline_within(report, decisions, now: float, *,
                    since: float | None = None,
                    horizon: float = AUDITOR_INTERVAL_S) -> dict | None:
    """The first deadline that falls between `since` and `now + horizon`.

    `since` is the previous tick, so a deadline set or reached during the
    elapsed hour is still seen; the look-back is bounded by `horizon` so a
    long-overdue external wait does not keep every pass awake forever.  The
    debit report already carries the scheduled wakes and the declared
    external/observation dates; the owner decisions are read apart.
    """
    lower = now - horizon
    if since is not None:
        lower = max(lower, min(float(since), now))
    upper = now + horizon

    def hit(section: str, row, ts: float) -> dict:
        return {"section": section, "lot": row.get("lot"), "due_ts": ts}

    for section in DEADLINE_SECTIONS:
        for row in report.get(section) or ():
            for key in ("due_ts", "reminder_ts"):
                ts = _due(row.get(key))
                if ts is not None and lower <= ts <= upper:
                    return hit(section, row, ts)
    for view in decisions or ():
        ts = _due(view.get("due_ts"))
        if ts is not None and lower <= ts <= upper:
            return hit("decisions", view, ts)
    return None


def debit_check(report) -> int | None:
    """What `ameesh debit --check` would return (0 or 2), None if unreadable.

    The check is the same one as the CLI (`debit.main`): a slow wait or a
    capacity alert exits 2.  A malformed report is a read error, never a
    quiet debit.
    """
    if not isinstance(report, dict):
        return None
    totals = report.get("totals")
    if not isinstance(totals, dict):
        return None
    if "slow_waits" not in totals or "capacity_alerts" not in totals:
        return None
    return 2 if (totals.get("slow_waits") or totals.get("capacity_alerts")) else 0


def quick_decision(previous: dict | None, state: dict | None,
                   arrivals: dict | None, *, read_error: str = "") -> Decision | None:
    """Decide before the expensive debit read; None when the debit is needed."""
    if read_error:
        return _played(READ_ERROR, where=read_error)
    if previous is None:
        return _played(FIRST_PASS)
    if arrivals is None:
        return _played(READ_ERROR, where="mail")
    if int(arrivals.get("arrivals") or 0) or int(arrivals.get("undelivered") or 0):
        return _played(MAIL, arrivals=int(arrivals.get("arrivals") or 0),
                       undelivered=int(arrivals.get("undelivered") or 0))
    if state is None:
        return _played(READ_ERROR, where="gate")
    if _event_data(previous).get("state") != state:
        return _played(GATE)
    return None


def final_decision(report, decisions, now: float, *,
                   since: float | None = None,
                   read_error: str = "") -> Decision:
    """Decide once the debit has been read: deadline, debit check, or skip."""
    if read_error:
        return _played(READ_ERROR, where=read_error)
    if not isinstance(report, dict):
        return _played(READ_ERROR, where="debit")
    hit = deadline_within(report, decisions, now, since=since)
    if hit is not None:
        return _played(DEADLINE, **hit)
    status = debit_check(report)
    if status is None:
        return _played(READ_ERROR, where="debit")
    if status != 0:
        return _played(DEBIT, check=status)
    return _skipped(QUIET, check=0)


def decide(previous: dict | None, state: dict | None, arrivals: dict | None,
           report, decisions, now: float, *, since: float | None = None,
           read_error: str = "") -> Decision:
    """The whole decision, witness-testable without a database."""
    quick = quick_decision(previous, state, arrivals, read_error=read_error)
    if quick is not None:
        return quick
    return final_decision(report, decisions, now, since=since,
                          read_error=read_error)


def record(db, agent: str, decision: Decision, *, started_ts: float,
           state: dict | None, arrivals: dict | None = None,
           debit: int | None = None) -> int:
    """Journal one tick; the skip is proved by this event, with no mail."""
    data = {
        "agent": agent,
        "decision": "skipped" if decision.skip else "played",
        "reason": decision.reason,
        "started_ts": float(started_ts),
        "mail": int((arrivals or {}).get("arrivals") or 0),
        "undelivered": int((arrivals or {}).get("undelivered") or 0),
    }
    if state:
        data["state"] = state
    if debit is not None:
        data["debit"] = int(debit)
    if decision.detail:
        data["detail"] = dict(decision.detail)
    return events.record(db, EVENT, agent, **data)


def send_pass_mail(db, agent: str, *, started_ts: float) -> int:
    """The one mail that opens the pass, idempotent for its hourly slot."""
    slot = int(float(started_ts) // 3600)
    return mail.send(
        db, work_mod.SYSTEM_SENDER, agent, AUDITOR_MAIL, kind="notify",
        payload={"pass": PASS_TAG},
        idempotency_key="%s:%d" % (PASS_TAG, slot))


def _agent_name(args) -> str:
    return (args.agent or os.environ.get("AMEESH_ROLE")
            or AUDITOR_ROLE).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh auditor tick",
        description="the auditor's hourly short pass: mail only when the mesh moved "
                    "(#6643)")
    parser.add_argument("--agent", default="",
                        help="role name to wake (default: AMEESH_ROLE, else auditeur)")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and print, without mail or journal event")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    return parser


def _print(report: dict, as_json: bool, *, dry_run: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
        return
    decision = report.get("decision") or {}
    state = "skipped" if decision.get("skip") else "played"
    if dry_run:
        state = "would be " + state
    detail = decision.get("detail") or {}
    suffix = (" (%s)" % ", ".join("%s=%s" % (key, value)
                                  for key, value in sorted(detail.items()))
              if detail else "")
    print("auditor short pass %s: %s%s" % (state, decision.get("reason") or "?", suffix))


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    agent = _agent_name(args)
    try:
        cfg = config_mod.load()
        db = db_mod.connect(cfg)
    except Exception as exc:  # noqa: BLE001 - no configuration, no tick
        print("ameesh auditor tick: %s" % exc, file=sys.stderr)
        return 1
    try:
        db_mod.require_schema(db, defer=True)
        now = float(db.query(
            "select extract(epoch from clock_timestamp())::float8 as ts")[0]["ts"])
        started_ts = now
        read_error = ""
        previous = None
        state = None
        arrivals = None
        report = None
        decisions_rows = []
        try:
            previous = previous_pass(db, agent)
        except Exception:  # noqa: BLE001 - an unreadable previous pass is played
            read_error = "previous"
        try:
            state = gate_state(db)
        except Exception:  # noqa: BLE001 - an unreadable gate is played
            read_error = read_error or "gate"
        since = _cutoff(previous, now) if previous is not None else None
        try:
            arrivals = mail_arrivals(db, agent, since if since is not None else now)
        except Exception:  # noqa: BLE001 - an unreadable mailbox is played
            read_error = read_error or "mail"

        decision = quick_decision(previous, state, arrivals, read_error=read_error)
        debit = None
        if decision is None:
            try:
                from . import debit as debit_mod
                # `--check` never depends on the job-duration section: skip the
                # extra GitHub reads of `--no-jobs` semantics.
                report = debit_mod.read(db, cfg=cfg, now=now, jobs=False)
                from . import decisions as decisions_mod
                decisions_rows = decisions_mod.pending(db, now)
            except Exception:  # noqa: BLE001 - an unreadable debit is played
                read_error = "debit"
                report, decisions_rows = None, []
            decision = final_decision(report, decisions_rows, now, since=since,
                                      read_error=read_error)
            debit = debit_check(report) if report is not None else None

        payload = {
            "schema": SCHEMA,
            "agent": agent,
            "at": now,
            "decision": decision.as_json(),
            "debit": debit,
            "mail": arrivals,
            "state": state,
        }
        if args.dry_run:
            _print(payload, args.json, dry_run=True)
            return 0
        if not decision.skip:
            send_pass_mail(db, agent, started_ts=started_ts)
        record(db, agent, decision, started_ts=started_ts, state=state,
               arrivals=arrivals, debit=debit)
        _print(payload, args.json, dry_run=False)
        return 0
    except Exception as exc:  # noqa: BLE001 - the timer must notice a failure
        print("ameesh auditor tick: %s" % exc, file=sys.stderr)
        return 1
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001 - a close failure never hides the result
            pass


__all__ = ["Decision", "EVENT", "PASS_TAG", "SCHEMA",
           "build_parser", "deadline_within", "debit_check", "decide",
           "final_decision", "gate_state", "mail_arrivals", "main",
           "previous_pass", "quick_decision", "record", "send_pass_mail"]
