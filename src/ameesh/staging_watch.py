# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: shared staging progress, observable after the launcher disappears.

Each event is a complete small snapshot. The independent alerts pass needs
neither the launcher's filesystem nor its process. #5425: a run is late on its
own evidence, not on one fixed deadline for the whole path: the launcher's
heartbeat, its last progress (a step or a runner), and the budget of the
current host, a function of its runners and of the turn cap. The fixed
`deadline` only judges a launcher older than the heartbeat.
#5450: the launchers themselves read the same verdict (`Monitor`): a late run
is stopped only once the reason persisted after the alert (TERM, grace, KILL),
never on the global fallback while it progresses. The service unit remains the
last-resort hard bound above that fallback.
The shared `staging.attested` proof settles a run whose last snapshot
publication failed (#4898 N2). Request history is read per key: only a later
completed deployment settles an earlier failure, never a partial one.
#5558: a later run on a descendant commit settles a failure host by host: once
every host the failed run left unhealthy is healthy again (or declared absent
and deferred, canon §14), a partial run that omits the workstation clears it.
#5693: failures of the same staging authority on a descending line of commits
are one alert, carried by the most recent failed or running run, with the union
of the hosts still to repair: a retry that fails before installing proves
nothing about the earlier hosts, so it neither hides nor duplicates that alert.
No command, environment, arbitrary log output or credential is journaled.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

from ameesh import storage

EVENT = "staging.sequence"
LIMIT = 1000
#: Per request key, the first history read. The pass escalates (doubling) when
#: the window is full, so a busy key never makes the alert family unreadable;
#: the read is bounded by HISTORY_MAX.
HISTORY_LIMIT = 200
HISTORY_MAX = 10000
TERMINAL = frozenset(("completed", "partial", "failed", "unattested"))
#: #5425: liveness fields of a snapshot, all absent from an older launcher's
_LIVENESS = ("beat", "progress", "silence_s", "stall_s")
#: #5558: last step of a host proved healthy by its run (installer, restart,
#: control and real health all passed: `sante` is traced ok last)
_HEALTHY = ("sante", "ok")
#: #5558: host declared absent and deferred by its run (canon §14)
_DEFERRED = ("absent", "reporte")
#: exit code of a refused exclusive start, as the local flock of staging.sh
BUSY = 75
#: #5450: global fallback of a launcher without heartbeat. It stays below the
#: 12 h between two cutoffs; the service unit is bounded 30 min later.
STAGING_FALLBACK_S = 11 * 3600.0
#: #5450: a late run is stopped only after this delay, so the independent
#: alerts pass reports it to the operator first.
STOP_AFTER_ALERT_S = 900.0
#: #5450: after SIGTERM, the launcher gets this grace to trace the open
#: host/step and mail the manual rollback before SIGKILL. It does not attempt
#: the rolling restart of a rollback inside this window.
STOP_GRACE_S = 300.0
#: #5450: re-read interval of a monitored launcher.
WATCH_INTERVAL_S = 5.0


#: latest snapshot of every run; same predicate as `latest_type`, so the
#: partial staging-progress index applies
_LATEST_SQL = ("select * from (select distinct on (object) id, object, data"
               " from ameesh_events where type = %s"
               " and type in ('staging.sequence', 'staging.request')"
               " and created_at > now() - make_interval(days => 60)"
               " order by object, id desc) latest order by id desc limit %s")


class Busy(RuntimeError):
    """#4885: another staging sequence is in flight in the shared log."""


def _finite(value, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid staging " + name)
    return number


def _public_step(step: dict) -> dict:
    public = {key: str(step.get(key) or "")[:128] for key in ("stage", "verdict")}
    for key in ("since", "deadline"):
        if step.get(key) is not None:
            public[key] = _finite(step[key], key)
    return public


def overdue(data: dict, now: float) -> tuple[str, float, float] | None:
    """#5425: why a running snapshot is late, its measure and its threshold.

    A launcher that stopped beating is late after `silence_s`. A beating one
    is late when nothing progressed for a whole turn plus that margin, or for
    `silence_s` once its current host is past its own budget. A slow rolling
    restart, which progresses runner by runner, is therefore never late.
    """
    if not all(isinstance(data.get(key), (int, float)) for key in _LIVENESS):
        started, deadline = float(data["started"]), float(data["deadline"])
        if now < deadline:
            return None
        return "délai dépassé", now - started, deadline - started
    silence, stall = float(data["silence_s"]), float(data["stall_s"])
    progress = float(data["progress"])
    quiet = now - max(float(data["beat"]), progress)
    if quiet >= silence:
        return ("délai dépassé : ni battement ni progrès depuis %d min"
                % (quiet // 60)), quiet, silence
    idle = now - progress
    limits = [step["deadline"] for step in (data.get("hosts") or {}).values()
              if isinstance(step, dict) and isinstance(step.get("deadline"), (int, float))]
    limit = silence if any(now >= value for value in limits) else stall
    if idle >= limit:
        return ("délai dépassé : lanceur vivant, aucun progrès depuis %d min"
                % (idle // 60)), idle, limit
    return None


def latest_snapshot(db, run: str) -> dict | None:
    """Newest shared snapshot of one run, or None before its first publish."""
    rows = storage.of(db).events.recent_type(
        EVENT, 60, 1, object="staging-run:" + run)
    return rows[0]["data"] if rows else None


class Monitor:
    """#5450: the single stop verdict shared by both staging launchers.

    `overdue()` remains the only judge of lateness. This monitor only adds the
    operator delay: the first late verdict opens the delay, and the run is
    stopped when `overdue()` still answers `STOP_AFTER_ALERT_S` later. A run
    that recovers closes the delay, so a later outage gets its own warning.

    `fallback` (the executor's request, or the synchronous launcher's
    synthetic started/deadline pair) judges the run before its first snapshot
    and a launcher that publishes no heartbeat. Once a snapshot exists it is
    the only evidence; a database outage falls back to the fixed deadline
    instead of stopping on an unreadable state.
    """

    def __init__(self, db, run: str, *, fallback: dict | None = None,
                 after_s: float | None = None, clock=time.time):
        self.db = db
        self.run = run
        self.fallback = fallback
        # Resolve the module constant at call time, so a test (and a future
        # deployment-level tuning) can adjust the operator delay.
        self.after_s = STOP_AFTER_ALERT_S if after_s is None else float(after_s)
        self.clock = clock
        #: first time `overdue()` answered for the current outage
        self.first_late: float | None = None

    def _snapshot(self) -> dict | None:
        if self.db is None:
            return self.fallback
        try:
            return latest_snapshot(self.db, self.run) or self.fallback
        except Exception:  # noqa: BLE001 - unreadable is not a late proof
            return self.fallback

    def check(self, now: float | None = None) -> str | None:
        """Why the run must be stopped now, or None while it may continue."""
        at = self.clock() if now is None else now
        data = self._snapshot()
        if not data or data.get("state") != "running":
            self.first_late = None
            return None
        try:
            late = overdue(data, at)
        except (KeyError, TypeError, ValueError):
            # A malformed snapshot is not a late proof: the unit stays the
            # last resort rather than stopping a healthy sequence on garbage.
            self.first_late = None
            return None
        if late is None:
            self.first_late = None
            return None
        if self.first_late is None:
            self.first_late = at
            return None
        if at - self.first_late < self.after_s:
            return None
        return late[0]

    def stop(self, reason: str) -> int | None:
        """Record the monitored stop with its overdue() reason, best effort."""
        if self.db is None:
            return None
        data = self._snapshot()
        if not data:
            return None
        try:
            return record(self.db, dict(data, state="failed",
                                        reason=str(reason)[:300]))
        except Exception:  # noqa: BLE001 - the unit bound remains
            return None


def _step_text(host: str, step: dict) -> str:
    text = "%s: %s/%s" % (host, step.get("stage", "?"), step.get("verdict", "?"))
    since = step.get("since")
    if isinstance(since, (int, float)):
        text += " depuis " + time.strftime("%H:%M:%SZ", time.gmtime(since))
    return text


def in_flight(rows: list[dict], *, exempt=(), attested=()) -> dict:
    """Runs whose latest snapshot is `running`, by run, with that snapshot.

    Lateness is an alert, never a proof of stop: a run is released only by a
    later shared attestation of the same commit, a terminal snapshot or an
    explicit `exempt`. The launchers' `Monitor` stops a late run and records
    that terminal snapshot (#5450); a run stopped by another path waits for
    its proved stop (the executor's quiescence probe, or the operator).
    """
    latest = {}
    ids = {}
    proofs = _attested_ids(attested)
    for row in sorted(rows, key=lambda row: int(row["id"]), reverse=True):
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("run"):
            latest.setdefault(data["run"], data)
            ids.setdefault(data["run"], int(row["id"]))
    return {run: data for run, data in sorted(latest.items())
            if run not in exempt and data.get("state") == "running"
            and proofs.get(data.get("sha"), 0) <= ids[run]}


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
            "hosts": {str(host)[:128]: _public_step(step)
                      for host, step in snapshot.get("hosts", {}).items()},
            "notify": str(snapshot.get("notify") or "")[:128]}
    for key in _LIVENESS:
        if key in snapshot:
            data[key] = _finite(snapshot[key], key)
    # #5450: the monitored stop keeps the overdue() reason that caused it.
    if isinstance(snapshot.get("reason"), str) and snapshot["reason"].strip():
        data["reason"] = snapshot["reason"].strip()[:300]
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
        # Plain SQL supports packages with an older installed event store.
        # Missing proofs retain blockers, so a bounded read fails closed.
        attested = tx.query(
            "select id, data from ameesh_events where type = %s"
            " and created_at > now() - make_interval(days => 60)"
            " order by id desc limit %s", ("staging.attested", LIMIT))
        active = in_flight(rows, attested=attested)
        others = {run: data for run, data in active.items() if run not in supersede}
        if others:
            raise Busy("staging sequence already running: " + ", ".join(others))
        # A superseded run was proved stopped: close it as failed, in the
        # same transaction, so it blocks no later start. The alerts pass
        # keeps reporting it until a successful run settles it.
        for old in active:
            if old in supersede:
                st.events.record(EVENT, "staging-run:" + old, None,
                                 dict(active[old], state="failed"))
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


#: #5558: proved (ancestor, descendant) pairs; a refusal is not cached, the
#: clone may fetch the descendant before the next pass
_DESCENDS: set = set()


def ancestry_repos(cfg=None) -> list[str]:
    """#5558: local ameesh clones that can prove a commit's ancestry.

    In order: the clone declared to the host (`radar.projects`, github
    manaty/ameesh), the deployment clone beside the sealed venv
    (`~/.local/share/ameesh/src`, fetched by every install, and the
    `src-<sha7>` snapshot of a `venv-<sha7>`), then the source checkout this
    module runs from. Only existing directories are kept; a non-repository
    simply never proves anything.
    """
    found = []
    if cfg is not None:
        try:
            from . import migrations
            declared = migrations.checkout_for(migrations.AMEESH_REPO, cfg)
        except Exception:  # noqa: BLE001 - a malformed radar proves nothing
            declared = None
        if declared:
            found.append(Path(declared))
    prefix = Path(sys.prefix).resolve()
    found.append(prefix.parent / "src")
    if prefix.name.startswith("venv-"):
        found.append(prefix.parent / ("src-" + prefix.name[len("venv-"):]))
    found.append(Path(__file__).resolve().parents[2])
    out = []
    for path in found:
        if path.is_dir() and str(path) not in out:
            out.append(str(path))
    return out


def git_descends(repos):
    """#5558: `descends(ancestor, descendant)` over `repos`.

    Any clone that proves the ancestry settles; an unknown commit, a
    non-repository or a git error answer False: doubt never settles a
    failure (the alert stays)."""
    def descends(ancestor: str, descendant: str) -> bool:
        if (ancestor, descendant) in _DESCENDS:
            return True
        from . import canon
        if not any(canon.git_contains(repo, ancestor, descendant)
                   for repo in repos):
            return False
        if len(_DESCENDS) >= 1024:
            _DESCENDS.clear()
        _DESCENDS.add((ancestor, descendant))
        return True
    return descends


def _git_descends(ancestor: str, descendant: str) -> bool:
    """Default ancestry, over the clones found without configuration."""
    return git_descends(ancestry_repos())(ancestor, descendant)


def _step(data: dict, host: str) -> tuple[str, str]:
    step = (data.get("hosts") or {}).get(host)
    if not isinstance(step, dict):
        return ("", "")
    return (str(step.get("stage") or ""), str(step.get("verdict") or ""))


def _unrepaired(run: str, latest: dict, ids: dict, descends) -> tuple[dict, bool]:
    """#5558: hosts `run` left unhealthy that later runs did not repair.

    A host the run left unhealthy (any last step but a passed health check or
    a declared absence) is repaired by a later run, on a commit descending
    from the failed one, that made it healthy or declared it absent and
    deferred (§14: a workstation off does not hold staging). The second value
    says whether at least one such later run made a host healthy, so a failure
    before any host step is not settled by an empty run. Unreached hosts were
    not modified and need no repair.
    """
    data = latest.get(run)
    if not data or data.get("state") in ("completed", "partial", "running"):
        return {}, False
    broken = {host: step for host, step in (data.get("hosts") or {}).items()
              if _step(data, host) not in (_HEALTHY, _DEFERRED)}
    proved = False
    for name in sorted(latest, key=ids.get):
        other = latest[name]
        if ids[name] <= ids[run] or not isinstance(other.get("sha"), str):
            continue
        healthy = {host for host in other.get("hosts") or {}
                   if _step(other, host) == _HEALTHY}
        if not healthy or not descends(data.get("sha"), other["sha"]):
            continue
        proved = True
        broken = {host: step for host, step in broken.items()
                  if _step(other, host) not in (_HEALTHY, _DEFERRED)}
        if not broken:
            break
    return broken, proved


def _repaired(run: str, latest: dict, ids: dict, descends) -> bool:
    """#5558: every host `run` left unhealthy was repaired by a later run."""
    broken, proved = _unrepaired(run, latest, ids, descends)
    return proved and not broken


def _latest_runs(rows) -> tuple[dict, dict]:
    latest = {}
    ids = {}
    for row in sorted(rows, key=lambda row: int(row["id"]), reverse=True):
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("run"):
            latest.setdefault(data["run"], data)
            ids.setdefault(data["run"], int(row["id"]))
    return latest, ids


def findings(rows: list[dict], *, now: float, attested=(),
             descends=None) -> list[dict]:
    """Latest snapshot of every run; no success inferred from process exit.

    `attested` carries the shared `staging.attested` events: a successful
    sequence whose last snapshot publication failed must not be alerted as
    interrupted (#4898 N2). An attestation settles a run only when it was
    written after that run's snapshot. #5558: a failure is also settled
    host by host (`_repaired`); `descends(ancestor, descendant)`
    defaults to git ancestry in the clones of `ancestry_repos()`.
    #5693: failures of one staging authority on a descending line of commits
    are one alert, carried by the most recent failed or running run of that
    line, with the union of the hosts still to repair. A newer run that fails
    before installing settles nothing; it keeps the alert, it does not add a
    second one.
    """
    latest, ids = _latest_runs(rows)
    proofs = _attested_ids(attested)
    descends = descends or _git_descends
    # A complete deployment by the same staging authority settles an earlier
    # failure. A partial run settles it only through the hosts it repaired.
    completed = {}
    for run, data in latest.items():
        if (data.get("state") == "completed"
                or proofs.get(data.get("sha"), 0) > ids[run]):
            scope = data.get("notify", "")
            completed[scope] = max(completed.get(scope, 0), ids[run])
    # #5693: every unsettled run's hosts still to repair, computed once.
    still_broken = {}

    def remaining(run):
        if run not in still_broken:
            still_broken[run] = _unrepaired(run, latest, ids, descends)
        return still_broken[run]

    def settled(run):
        data = latest[run]
        if (data.get("state") in ("completed", "partial")
                or completed.get(data.get("notify", ""), 0) > ids[run]
                or proofs.get(data.get("sha"), 0) > ids[run]):
            return True
        broken, proved = remaining(run)
        return proved and not broken

    # Newest first: a failure joins the most recent unsettled run of the same
    # authority whose commit contains it, so a retry that proves nothing about
    # the earlier hosts cannot hide or duplicate their alert.
    groups = []
    for run in sorted(latest, key=ids.get, reverse=True):
        if settled(run):
            continue
        data = latest[run]
        for group in groups:
            head = latest[group[0]]
            if (head.get("notify", "") == data.get("notify", "")
                    and isinstance(data.get("sha"), str)
                    and isinstance(head.get("sha"), str)
                    and descends(data["sha"], head["sha"])):
                group.append(run)
                break
        else:
            groups.append([run])
    out = []
    for group in groups:
        run = group[0]
        data = latest[run]
        # #5693: the alert is carried by the newest run but keeps the identity
        # of the first failure of the line (`since`, `reason`): a later retry
        # neither resolves it nor raises a fresh one. Only a completed,
        # attested or repaired line makes it disappear; a human ack
        # acknowledges it without resolving it.
        first = group[-1]
        first_data = latest[first]
        state = data.get("state")
        late = overdue(data, now) if state == "running" else None
        # A running retry keeps the alert of the failures it is replacing:
        # nothing proves their hosts repaired yet (#5693).
        replaces_failure = any(latest[member].get("state") in ("failed", "unattested")
                               for member in group[1:])
        if state == "running" and late is None and not replaces_failure:
            continue
        hosts = dict(data.get("hosts") or {})
        for member in group[1:]:
            for host, step in sorted(remaining(member)[0].items()):
                hosts.setdefault(host, step)
        if len(group) == 1:
            steps = "; ".join(_step_text(host, step)
                              for host, step in sorted(hosts.items()))
            detail = ("Staging %s (%s): %s; %s. Vérifier le lanceur et "
                      "reprendre la séquence sous verrou, après arrêt "
                      "prouvé de l'ancien exécutant."
                      % (run, data["sha"][:12],
                         late[0] if late
                         else data.get("reason") or state,
                         steps or "aucune étape d'hôte terminée"))
        else:
            # Compact: the mail body is bounded, so the runs and the hosts
            # still to repair come before any per-run narration.
            names = ", ".join("%s (%s)" % (member, latest[member]["sha"][:12])
                              for member in group)
            steps = "; ".join(_step_text(host, step)
                              for host, step in sorted(hosts.items()))
            detail = ("Staging %s (%s): %s; échecs regroupés : %s; hôtes non "
                      "réparés : %s. Vérifier le lanceur et reprendre la "
                      "séquence sous verrou, après arrêt prouvé de l'ancien "
                      "exécutant."
                      % (run, data["sha"][:12],
                         late[0] if late
                         else data.get("reason") or state,
                         names, steps or "aucune étape d'hôte terminée"))
        out.append({"schema": "ameesh-alert/1", "type": "staging_interrupted", "agent": None,
                    "since": first_data["started"],
                    "value": late[1] if late else now - first_data["started"],
                    "threshold": (late[2] if late
                                  else first_data["deadline"] - first_data["started"]),
                    "run": run, "sha": data["sha"], "hosts": hosts,
                    "reason": first, "urgent": True,
                    "orchestrators": [data["notify"]] if data.get("notify") else [],
                    "detail": detail})
    return out


def _beating_runs(rows, now: float) -> set:
    """#5425: running sequences that a heartbeat still proves on time."""
    latest = {}
    for row in sorted(rows, key=lambda row: int(row["id"])):
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("run"):
            latest[data["run"]] = data
    return {run for run, data in latest.items()
            if data.get("state") == "running" and "beat" in data
            and overdue(data, now) is None}


def request_findings(rows: list[dict], *, now: float, attested=(),
                     sequences=(), descends=None) -> list[dict]:
    """One alert per request key for the newest failure still unsettled.

    Reading only the latest request per key hid an earlier failed or blocked
    request behind a later partial one (#4898, P3/N3). History is read here;
    only a later *completed* deployment, or a shared attestation of the same
    commit, settles an earlier failure - a partial run may omit the failed
    host. #5558: a failed request whose run later runs repaired host by host
    (`_repaired`) is settled too.
    """
    proofs = _attested_ids(attested)
    beating = _beating_runs(sequences, now)
    latest, ids = _latest_runs(sequences)
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
            # #5425: the sequence's own evidence judges a running request;
            # its fixed deadline raised a second, false alert.
            late = (state in ("pending", "running")
                    and now >= data.get("deadline", float("inf"))
                    and not (state == "running" and data.get("run") in beating))
            if ident <= completed or proofs.get(data.get("sha"), 0) > ident:
                continue
            if state == "failed" and _repaired(
                    data.get("run"), latest, ids, descends or _git_descends):
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
                     data.get("blocked_reason") or data.get("reason")
                     or ("deadline exceeded" if late else "failed")))
        if later:
            detail += (" %d demande(s) ultérieure(s), partielle(s) ou en cours, "
                       "ne soldent pas cet échec : seul un déploiement completed, "
                       "ou des hôtes rendus sains sur un commit descendant, "
                       "le solde." % later)
        out.append(dict(schema="ameesh-alert/1", type="staging_interrupted",
                        agent=None, since=None, value=None, threshold=None,
                        reason=key, urgent=True,
                        orchestrators=([data["notify"]]
                                       if data.get("notify") else []),
                        detail=detail))
    return out


def alerts(db, *, now: float, cfg=None) -> list[dict]:
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
    # #5558: the host's clones, not this module's path: the sealed venv's
    # site-packages is not a repository.
    descends = git_descends(ancestry_repos(cfg))
    return (findings(rows, now=now, attested=attested, descends=descends)
            + request_findings(requests, now=now, attested=attested,
                               sequences=rows, descends=descends))


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
