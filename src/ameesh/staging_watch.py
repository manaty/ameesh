# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: shared staging progress, observable after the launcher disappears.

Each event is a complete small snapshot. The independent alerts pass needs
neither the launcher's filesystem nor its process. A deadline is fixed at
start; progress never extends it, so even a live but stuck launcher is seen.
The shared `staging.attested` proof settles a run whose last snapshot
publication failed (#4898 N2). Request history is read per key: only a later
completed deployment settles an earlier failure, never a partial one.
No command, environment, arbitrary log output or credential is journaled.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys

from ameesh import storage

EVENT = "staging.sequence"
LIMIT = 1000
#: Per request key, the first history read. The pass escalates (doubling) when
#: the window is full, so a busy key never makes the alert family unreadable;
#: the read is bounded by HISTORY_MAX.
HISTORY_LIMIT = 200
HISTORY_MAX = 10000
TERMINAL = frozenset(("completed", "partial", "failed", "unattested"))
#: exit code of a refused exclusive start, as the local flock of staging.sh
BUSY = 75


#: latest snapshot of every run; same predicate as `latest_type`, so the
#: partial staging-progress index applies
_LATEST_SQL = ("select * from (select distinct on (object) id, object, data"
               " from ameesh_events where type = %s"
               " and type in ('staging.sequence', 'staging.request')"
               " and created_at > now() - make_interval(days => 60)"
               " order by object, id desc) latest order by id desc limit %s")


class Busy(RuntimeError):
    """#4885: another staging sequence is in flight in the shared log."""


def in_flight(rows: list[dict], *, exempt=()) -> dict:
    """Runs whose latest snapshot is `running`, by run, with that snapshot.

    The deadline is an alert threshold, never a proof of stop: nothing ends
    a manual `staging.sh` at `STAGING_TIMEOUT_S`, so a late run still blocks
    (review cx1/docs-01). Only a terminal snapshot or an explicit `exempt`
    releases it: a previous run whose stop was proved (the executor's
    quiescence probe, or the operator's check).
    """
    latest = {}
    for row in sorted(rows, key=lambda row: int(row["id"]), reverse=True):
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("run"):
            latest.setdefault(data["run"], data)
    return {run: data for run, data in sorted(latest.items())
            if run not in exempt and data.get("state") == "running"}


def record(db, snapshot: dict, *, exclusive: bool = False,
           supersede=()) -> int:
    """Write only the public, bounded progress fields, using the shared log.

    #4885: with `exclusive`, the start of a sequence is refused (`Busy`) while
    another run is in flight, whatever path launched it: the elected executor
    (#4880), a manual `staging.sh`, or the synchronous release. Check and
    write share one transaction under an advisory lock, so two starts never
    both pass.
    """
    run = str(snapshot["run"])
    sha = str(snapshot["sha"])
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", run):
        raise ValueError("invalid staging run identifier")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid staging commit")
    state = snapshot["state"]
    if state not in TERMINAL | {"running"}:
        raise ValueError("invalid staging state")
    started, deadline = float(snapshot["started"]), float(snapshot["deadline"])
    if not math.isfinite(started) or not math.isfinite(deadline) or deadline <= started:
        raise ValueError("invalid staging deadline")
    if len(snapshot.get("hosts", {})) > 64:
        raise ValueError("too many staging hosts")
    data = {"run": run, "sha": sha, "state": state,
            "started": started, "deadline": deadline,
            "hosts": {str(host)[:128]: {
                key: str(step.get(key) or "")[:128]
                for key in ("stage", "verdict")}
                for host, step in snapshot.get("hosts", {}).items()},
            "notify": str(snapshot.get("notify") or "")[:128]}
    if not exclusive:
        return storage.of(db).events.record(EVENT, "staging-run:" + run, None, data)
    with db.transaction() as tx:
        tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (EVENT,))
        st = storage.of(tx)
        # Plain SQL, not `events.latest_type`: from bureau-goal this module
        # runs from stdin against the package installed on HOTE_OUTILS, which
        # may predate it (review qa-01). Only `events.record` is assumed.
        rows = tx.query(_LATEST_SQL, (EVENT, LIMIT))
        if len(rows) >= LIMIT:
            # Unobservable is refused like busy: no host is modified.
            raise Busy("staging history truncated; exclusivity unobservable")
        # A start is a new run: an identifier already in the log (inherited
        # or reused `STAGING_RUN_ID`, even with a proved stop) never passes,
        # else two launchers sharing it would both start (review cx3/qa-01).
        # Progress updates of the started run use `exclusive=False`.
        if any((r.get("data") or {}).get("run") == run for r in rows):
            raise Busy("staging run identifier already used: " + run)
        others = in_flight(rows, exempt=set(supersede))
        if others:
            raise Busy("staging sequence already running: " + ", ".join(others))
        # A superseded run was proved stopped: close it as failed, in the
        # same transaction, so it blocks no later start. The alerts pass
        # keeps reporting it until a successful run settles it.
        for old in in_flight(rows):
            if old in supersede:
                st.events.record(EVENT, "staging-run:" + old, None,
                                 dict(in_flight(rows)[old], state="failed"))
        return st.events.record(EVENT, "staging-run:" + run, None, data)


def _attested_ids(rows) -> dict[str, int]:
    """Newest shared attestation id per commit (actif.py writes one per sha)."""
    out = {}
    for row in rows or ():
        data = row.get("data") or {}
        sha = data.get("sha")
        if isinstance(sha, str) and len(sha) == 40:
            out[sha] = max(out.get(sha, 0), int(row.get("id") or 0))
    return out


def findings(rows: list[dict], *, now: float, attested=()) -> list[dict]:
    """Latest snapshot of every run; no success inferred from process exit.

    `attested` carries the shared `staging.attested` events: a successful
    sequence whose last snapshot publication failed must not be alerted as
    interrupted (#4898 N2). An attestation settles a run only when it was
    written after that run's snapshot.
    """
    latest = {}
    ids = {}
    for row in sorted(rows, key=lambda row: int(row["id"]), reverse=True):
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("run"):
            latest.setdefault(data["run"], data)
            ids.setdefault(data["run"], int(row["id"]))
    proofs = _attested_ids(attested)
    out = []
    # Only a complete deployment by the same staging authority settles an
    # earlier failure. A partial run may omit the host that failed.
    completed = {}
    for run, data in latest.items():
        if (data.get("state") == "completed"
                or proofs.get(data.get("sha"), 0) > ids[run]):
            scope = data.get("notify", "")
            completed[scope] = max(completed.get(scope, 0), ids[run])
    for run, data in latest.items():
        state = data.get("state")
        if (state in ("completed", "partial")
                or completed.get(data.get("notify", ""), 0) > ids[run]
                or proofs.get(data.get("sha"), 0) > ids[run]):
            continue
        if state == "running" and now < float(data["deadline"]):
            continue
        hosts = data.get("hosts") or {}
        steps = "; ".join("%s: %s/%s" % (host, step.get("stage", "?"),
                                        step.get("verdict", "?"))
                          for host, step in sorted(hosts.items()))
        out.append({"schema": "ameesh-alert/1", "type": "staging_interrupted", "agent": None,
                    "since": data["started"], "value": now - data["started"],
                    "threshold": data["deadline"] - data["started"],
                    "run": run, "sha": data["sha"], "hosts": hosts,
                    "reason": run, "urgent": True,
                    "orchestrators": [data["notify"]] if data.get("notify") else [],
                    "detail": "Staging %s (%s): %s; %s. Vérifier le lanceur et "
                              "reprendre la séquence sous verrou, après arrêt "
                              "prouvé de l'ancien exécutant."
                              % (run, data["sha"][:12],
                                 "délai dépassé" if state == "running" else state,
                                 steps or "aucune étape d'hôte terminée")})
    return out


def request_findings(rows: list[dict], *, now: float, attested=()) -> list[dict]:
    """One alert per request key for the newest failure still unsettled.

    Reading only the latest request per key hid an earlier failed or blocked
    request behind a later partial one (#4898, P3/N3). History is read here;
    only a later *completed* deployment, or a shared attestation of the same
    commit, settles an earlier failure - a partial run may omit the failed
    host.
    """
    proofs = _attested_ids(attested)
    by_key = {}
    for row in rows:
        data = row.get("data")
        key = row.get("object")
        if isinstance(data, dict) and key:
            by_key.setdefault(key, []).append(row)
    out = []
    for key in sorted(by_key):
        # One request is one uuid: its pending, running and terminal rows are
        # snapshots of the SAME request. Only the newest snapshot speaks for
        # it. Considering each row separately made the initial pending line
        # expire at its 300 s start deadline while the request was still
        # running healthily (the deployment deadline is set on the running
        # line) — a false urgent alert after 5 minutes, permanent after a
        # partial (#4898 B1).
        newest = {}
        for row in by_key[key]:
            request = row["data"].get("request")
            ident = int(row.get("id") or 0)
            if request not in newest or ident > int(newest[request].get("id") or 0):
                newest[request] = row
        group = sorted(newest.values(),
                       key=lambda row: int(row.get("id") or 0), reverse=True)
        completed = max((int(row["id"]) for row in group
                         if (row.get("data") or {}).get("state") == "completed"),
                        default=0)
        unsettled = []
        for row in group:
            data = row["data"]
            ident = int(row.get("id") or 0)
            state = data.get("state")
            late = (state in ("pending", "running")
                    and now >= data.get("deadline", float("inf")))
            if ident <= completed or proofs.get(data.get("sha"), 0) > ident:
                continue
            if state in ("completed", "partial"):
                continue
            if state == "failed" or data.get("blocked_reason") or late:
                unsettled.append((row, late))
        if not unsettled:
            continue
        row, late = unsettled[0]
        data = row["data"]
        later = sum(1 for other in group
                    if int(other.get("id") or 0) > int(row.get("id") or 0))
        detail = ("Staging request %s (%s) blocked: %s; reprise refusée avant "
                  "preuve d'arrêt et accès de déploiement sur le candidat élu."
                  % (data.get("request"), str(data.get("sha") or "")[:12],
                     data.get("blocked_reason")
                     or ("deadline exceeded" if late else "failed")))
        if later:
            detail += (" %d demande(s) ultérieure(s), partielle(s) ou en cours, "
                       "ne soldent pas cet échec : seul un déploiement completed "
                       "le solde." % later)
        out.append(dict(schema="ameesh-alert/1", type="staging_interrupted",
                        agent=None, since=None, value=None, threshold=None,
                        reason=key, urgent=True,
                        orchestrators=([data["notify"]]
                                       if data.get("notify") else []),
                        detail=detail))
    return out


def alerts(db, *, now: float) -> list[dict]:
    # Do not swallow a read failure: the caller must keep the alert family
    # unreadable instead of resolving an existing alert through absence.
    events = storage.of(db).events
    rows = events.latest_type(EVENT, 60, LIMIT)
    if len(rows) >= LIMIT:
        raise RuntimeError("staging history truncated; resolution unobservable")
    from . import release_cutoff
    attested = events.latest_type(release_cutoff.STAGING_EVENT, 60, LIMIT)
    if len(attested) >= LIMIT:
        raise RuntimeError("staging attestations truncated; resolution unobservable")
    # The latest request per key only names the keys; the read of the history
    # itself is per key, on the partial (type, object, id) index. Reading one
    # latest row per key was the bug: a later partial hid an older failure
    # (#4898, P3/N3).
    latest_requests = events.latest_type("staging.request", 60, LIMIT)
    if len(latest_requests) >= LIMIT:
        raise RuntimeError("staging requests truncated; resolution unobservable")
    requests = []
    for row in latest_requests:
        key = row.get("object")
        if not key:
            continue
        # Escalate the window instead of refusing when a busy key grows: a
        # fixed 200-row read made the whole alert family unreadable after
        # about a month of normal releases (#4898 B2). At the cap the newest
        # rows are still read; only history older than the cap is not.
        limit = HISTORY_LIMIT
        while True:
            history = events.recent_type("staging.request", 60, limit,
                                         object=key)
            if len(history) < limit or limit >= HISTORY_MAX:
                break
            limit = min(limit * 2, HISTORY_MAX)
        requests.extend(history)
    return (findings(rows, now=now, attested=attested)
            + request_findings(requests, now=now, attested=attested))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", help="public staging snapshot, JSON")
    parser.add_argument("--exclusive", action="store_true",
                        help="refuse (exit 75) while another run is in flight")
    parser.add_argument("--supersede", action="append", default=[],
                        help="previous run whose stop was proved")
    args = parser.parse_args(argv)
    from ameesh import config, db as db_mod
    conn = db_mod.connect(config.load())
    try:
        record(conn, json.loads(args.snapshot), exclusive=args.exclusive,
               supersede=[run for run in args.supersede if run])
    except Busy as exc:
        print(exc, file=sys.stderr)
        return BUSY
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
