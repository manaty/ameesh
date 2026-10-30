# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: shared staging progress, observable after the launcher disappears.

Each event is a complete small snapshot. The independent alerts pass needs
neither the launcher's filesystem nor its process. A deadline is fixed at
start; progress never extends it, so even a live but stuck launcher is seen.
No command, environment, arbitrary log output or credential is journaled.
"""
from __future__ import annotations

import argparse
import json
import math
import re

from ameesh import storage

EVENT = "staging.sequence"
LIMIT = 1000
TERMINAL = frozenset(("completed", "partial", "failed", "unattested"))


def record(db, snapshot: dict) -> int:
    """Write only the public, bounded progress fields, using the shared log."""
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
    return storage.of(db).events.record(EVENT, "staging-run:" + run, None, data)


def findings(rows: list[dict], *, now: float) -> list[dict]:
    """Latest snapshot of every run; no success inferred from process exit."""
    latest = {}
    ids = {}
    for row in sorted(rows, key=lambda row: int(row["id"]), reverse=True):
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("run"):
            latest.setdefault(data["run"], data)
            ids.setdefault(data["run"], int(row["id"]))
    out = []
    # Only a complete deployment by the same staging authority settles an
    # earlier failure. A partial run may omit the host that failed.
    completed = {}
    for run, data in latest.items():
        if data.get("state") == "completed":
            scope = data.get("notify", "")
            completed[scope] = max(completed.get(scope, 0), ids[run])
    for run, data in latest.items():
        state = data.get("state")
        if state in ("completed", "partial") or completed.get(data.get("notify", ""), 0) > ids[run]:
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


def alerts(db, *, now: float) -> list[dict]:
    # Do not swallow a read failure: the caller must keep the alert family
    # unreadable instead of resolving an existing alert through absence.
    rows = storage.of(db).events.latest_type(EVENT, 60, LIMIT)
    if len(rows) >= LIMIT:
        raise RuntimeError("staging history truncated; resolution unobservable")
    out = findings(rows, now=now)
    requests = storage.of(db).events.latest_type("staging.request", 60, LIMIT)
    if len(requests) >= LIMIT:
        raise RuntimeError("staging requests truncated; resolution unobservable")
    seen = set()
    for row in sorted(requests, key=lambda row: int(row.get("id", 0)), reverse=True):
        key = row["object"]
        if key in seen:
            continue
        seen.add(key)
        data = row["data"]
        state = data.get("state")
        late = state in ("pending", "running") and now >= data.get("deadline", float("inf"))
        if state in ("completed", "partial") or not (late or data.get("blocked_reason") or state == "failed"):
            continue
        out.append(dict(schema="ameesh-alert/1", type="staging_interrupted", agent=None, since=None,
                        value=None, threshold=None, reason=key,
                        urgent=True, orchestrators=[data["notify"]],
                        detail="Staging request %s (%s) blocked: %s; "
                               "reprise refusée avant preuve d'arrêt et accès "
                               "de déploiement sur le candidat élu."
                               % (data["request"], data["sha"][:12],
                                  data.get("blocked_reason") or ("deadline exceeded" if late else "failed"))))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", help="public staging snapshot, JSON")
    args = parser.parse_args(argv)
    from ameesh import config, db as db_mod
    conn = db_mod.connect(config.load())
    try:
        record(conn, json.loads(args.snapshot))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
