# SPDX-License-Identifier: AGPL-3.0-only
"""Exact correlation of production workflow runs and pre-act receipts.

Pure correlation plus the elected watcher's durable GitHub Actions collector.
The pure correlate helper expects complete stable history; the collector stores
consumption and alert delivery transactionally across watcher invocations.
"""
from __future__ import annotations

import re

_MARKER = re.compile(r"(?:^|\s)pregeste=([^\s]*)")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def correlate(runs, receipts):
    """Return anomalies; receipt IDs are single-use, never matched by time.

    Each run has a unique stable ``id`` and ``display_title``. Receipts are
    evidence objects, of which only well-formed production receipts count.
    Duplicate receipt IDs are ambiguous and therefore cannot authorize a run.
    """
    counts = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("type") != "pre-act/1" or receipt.get("kind") != "prod":
            continue
        value = receipt.get("pregeste_id")
        if isinstance(value, str) and _UUID4.fullmatch(value):
            counts[value] = counts.get(value, 0) + 1
    consumed = set()
    seen_runs = set()
    anomalies = []
    for run in runs:
        run_id = run["id"]
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        title = run.get("display_title")
        values = _MARKER.findall(title) if isinstance(title, str) else []
        if len(values) != 1 or not _UUID4.fullmatch(values[0]):
            reason = "missing-or-invalid-id"
        elif counts.get(values[0], 0) != 1:
            reason = "unknown-or-ambiguous-id"
        elif values[0] in consumed:
            reason = "already-consumed-id"
        else:
            consumed.add(values[0])
            continue
        anomalies.append({"run_id": run_id, "reason": reason})
    return anomalies


def production_run(run):
    """Mode-marked workflow: exclude only explicit, unambiguous safe modes."""
    title = run.get("display_title") or ""
    env = re.findall(r"(?:^|\s)environment=([^\s]*)", title)
    observe = re.findall(r"(?:^|\s)observe_only=([^\s]*)", title)
    if len(env) != 1 or len(observe) != 1:
        return None  # unknown mode must be audited, never silently excluded
    if env[0] not in ("production", "staging") or observe[0] not in ("true", "false"):
        return None
    return env[0] == "production" and observe[0] == "false"


def collect(api, repo, workflow, start, until):
    """Read a fixed creation window; split GitHub's 1000-result limit.

    The caller uses the existing GitHub watch's five minute settlement lag.
    Inclusive windows are re-read from activation to observe reruns. An incomplete or stale response never advances
    the durable cursor. Attempts are retained separately from their run ID.
    """
    from datetime import datetime, timezone
    from urllib.parse import quote, urlencode
    from . import plan_github

    def stamp(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def window(lo, hi):
        rows = []
        page = 1
        expected = None
        while True:
            path = "repos/%s/actions/workflows/%s/runs?%s" % (
                repo, quote(workflow, safe=""), urlencode({
                    "created": stamp(lo) + ".." + stamp(hi),
                    "per_page": 100, "page": page}))
            data = api.get(path, ttl=0)
            if getattr(getattr(api, "last_read", None), "stale", False):
                raise plan_github.GithubError("pre-act audit: stale API response")
            if not isinstance(data, dict) or not isinstance(data.get("total_count"), int) \
                    or not isinstance(data.get("workflow_runs"), list):
                raise plan_github.GithubError("pre-act audit: invalid run response")
            count = data["total_count"]
            if count > 1000:
                if hi <= lo:
                    raise plan_github.GithubError("pre-act audit: saturated one-second window")
                mid = (lo + hi) // 2
                return window(lo, mid) + window(mid + 1, hi)
            if expected is not None and expected != count:
                raise plan_github.GithubError("pre-act audit: run window changed during pagination")
            expected = count
            batch = data["workflow_runs"]
            for row in batch:
                if not isinstance(row, dict) or not isinstance(row.get("id"), int) \
                        or not isinstance(row.get("run_attempt", 1), int) \
                        or row.get("run_attempt", 1) < 1:
                    raise plan_github.GithubError("pre-act audit: invalid run identity")
            rows.extend(batch)
            if len(rows) >= count:
                if len({r["id"] for r in rows}) != count:
                    raise plan_github.GithubError("pre-act audit: incomplete pagination")
                return rows
            if not batch:
                raise plan_github.GithubError("pre-act audit: truncated pagination")
            page += 1

    return window(int(start), int(until))


def deliver(db, sub, workflow, runs, until, auditor, *, mode_marked=False):
    """Serialize consumption globally; mail, runs and cursor commit together."""
    from . import mail, storage
    after_commit = []
    with db.transaction() as tx:
        st = storage.of(tx).github_watch
        st.audit_lock()
        known = st.audit_known(sub.repo, runs)
        pending = {}
        for run in runs:
            # The list endpoint exposes only the latest attempt. Every prior
            # attempt necessarily existed; materialize it so rapid retries
            # between scans cannot hide receipt reuse.
            for attempt in range(1, int(run.get("run_attempt", 1)) + 1):
                key = (int(run["id"]), attempt)
                if key not in known:
                    pending[key] = dict(run, run_attempt=attempt)
        ids = {value for run in pending.values()
               for value in _MARKER.findall(run.get("display_title") or "")
               if _UUID4.fullmatch(value)}
        receipts = st.audit_receipts(ids) if ids else []
        consumed = st.audit_consumed(ids) if ids else set()
        for key, run in sorted(pending.items()):
            values = _MARKER.findall(run.get("display_title") or "")
            value = values[0] if len(values) == 1 and _UUID4.fullmatch(values[0]) else None
            mode = production_run(run) if mode_marked else True
            if mode is False:
                reason = "non-production"
            elif mode is None:
                reason = "missing-or-invalid-mode"
            else:
                anomalies = correlate([run], receipts)
                reason = anomalies[0]["reason"] if anomalies else ""
                if not reason and value in consumed:
                    reason = "already-consumed-id"
                if not reason:
                    consumed.add(value)
            st.audit_record(sub.repo, workflow, run, value, reason)
            if reason and reason != "non-production":
                url = "https://github.com/%s/actions/runs/%d/attempts/%d" % (sub.repo, *key)
                sent = mail.send(tx, "ameesh", auditor,
                          "Pré-geste : anomalie %s (%s, %s). %s" %
                          (reason, sub.project, workflow, url), host=db.cfg.host,
                          kind="event", after_commit=after_commit)
                if sent is None:
                    raise ValueError("pre-act audit: auditor mailbox refused delivery")
        st.audit_advance(sub.project, sub.repo, workflow, until)
    for callback in after_commit:
        callback(db.cfg, db)
    return len(pending)


def tick(db, cfg, now, subscriptions, api, log):
    """Called by the elected GitHub watcher, using the applied canon inventory."""
    from . import canon, mechanics, storage
    document = mechanics.configured(cfg)
    watched = [(sub, document.override(sub.project)) for sub in subscriptions]
    watched = [(sub, spec) for sub, spec in watched
               if spec and spec.production_branch and spec.production_workflows]
    if not watched:
        return
    auditors = {agent.title for bundle in canon.load_configured(cfg) for agent in bundle.agents
                if "auditeur" in (getattr(agent, "roles", None) or ())}
    if len(auditors) != 1:
        raise ValueError("pre-act audit requires one declared auditor")
    auditor = auditors.pop()
    for sub, spec in watched:
        for workflow in spec.production_workflows:
            try:
                with db.transaction() as tx:
                    cursor = storage.of(tx).github_watch.audit_activate(
                        sub.project, sub.repo, workflow, now)
                # Never collect before activation; defer indexing until settled.
                # Re-read since activation: a rerun retains its original
                # created_at, so a moving creation cursor would lose retries.
                start = float(cursor["activated_at"])
                until = int(now - 300)
                if until <= start or until - float(cursor["scanned_through"]) < min(sub.interval, 900):
                    continue
                runs = collect(api, sub.repo, workflow, start, until)
                count = deliver(db, sub, workflow, runs, until, auditor,
                                mode_marked=workflow in spec.production_mode_marked_workflows)
                if count:
                    log("pre-act audit %s/%s: %d runs" % (sub.repo, workflow, count))
            except Exception as exc:
                log("pre-act audit %s/%s failed; cursor retained: %s" %
                    (sub.repo, workflow, str(exc)))
