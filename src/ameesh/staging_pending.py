# SPDX-License-Identifier: AGPL-3.0-only
"""#3291: merged fixes that still hold work because staging has not caught up.

Only active holds and explicit dependency edges count. Mentioning a lot in a
description is not a dependency. Evidence is dated: the last attestation of
each host, not a tag, a package version number or the lot's branch head.
"""
from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path

from . import planned_waits, storage

SCHEMA = "ameesh-staging-pending/1"
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_LOT = re.compile(r"(?<![\w/])#([1-9][0-9]*)\b")
_MIGRATION = re.compile(r"\bmigration\s+0*([1-9][0-9]*)\b", re.I)


def active_hosts() -> dict[str, dict]:
    """Latest attested commit per host; keep partial-host evidence too.

    A newer attestation supersedes an older one (including a deliberate
    rollback). An unknown commit is never resolved by substituting a tag.
    """
    from . import release_cutoff as rc
    folder = Path(rc.attestation_path("0" * 40)).parent
    out = {}
    for path in sorted(folder.glob("actif-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stamp = data.get("date")
            at = (datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                  if isinstance(stamp, str) else path.stat().st_mtime)
            sha = str(data.get("sha") or "").lower()
            if not _SHA.fullmatch(sha) or not isinstance(data.get("hotes"), dict):
                continue
            for host, detail in data["hotes"].items():
                current = str(detail.get("actif") or "").lower()
                if not _SHA.fullmatch(current) or not (
                        sha.startswith(current) or current.startswith(sha)):
                    continue
                if host not in out or at > out[host]["at"]:
                    out[host] = {"sha": sha, "at": at, "attestation": str(path)}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return out


def regressions(project, candidate: str, active=None) -> dict[str, str]:
    """Hosts whose attested content would be lost by deploying candidate.

    An unresolvable attestation refuses the action rather than permitting a
    rollback because this clone has not fetched that commit yet.
    """
    from . import release_cutoff as rc
    out = {}
    if project.name != "ameesh":
        return out
    for host, evidence in (active_hosts() if active is None else active).items():
        sha = rc.commit_of(project.repo, evidence["sha"])
        result = rc._git(project.repo, "merge-base", "--is-ancestor", sha, candidate)
        if result.returncode not in (0, 1):
            raise rc.TransientReleaseError("cannot compare active %s to candidate %s"
                                           % (sha[:12], candidate[:12]))
        if result.returncode:
            out[host] = sha
    return out


def _merged(milestones):
    rows = [m for m in milestones if m.get("kind") == "merged"
            and not m.get("annulled") and m.get("sha")]
    return max(rows, key=lambda m: (float(m.get("at_ts") or 0), int(m.get("id") or 0)),
               default=None)


def waiting(db, project, *, active=None) -> dict:
    """Read-only, shared by the staging alert and the auditor's throughput view."""
    from . import release_cutoff as rc, work
    st = storage.of(db)
    all_rows = st.work.items(state=None, assignee=None, limit=None)
    rows = {int(r["id"]): r for r in all_rows}
    candidates = [r for r in all_rows
                  if r.get("state") in ("intake", "build", "qa", "blocked")]
    ids = [int(r["id"]) for r in candidates]
    holds = planned_waits.active_holds(
        db, st.progress.lot_hold_events(ids, planned_waits.HOLD_NOTE_PATTERNS))
    edges = st.work_dependencies.for_items(ids)
    refs: dict[int, set[int]] = {}
    for edge in edges:
        refs.setdefault(int(edge["work_item_id"]), set()).add(int(edge["blocker_id"]))
    # A migration reference is resolved only against declared reservations of
    # this project. No bare integer or ticket number can become a migration.
    migrations = {}
    for row in all_rows:
        if row.get("app") != project.name:
            continue
        reservations = work.reserved_migrations(row)
        if row.get("migration_landed") is not None:
            reservations = [{"number": int(row["migration_landed"])}]
        for reservation in reservations:
            migrations.setdefault(reservation["number"], set()).add(int(row["id"]))
    for ident, by_actor in holds.items():
        for hold in by_actor.values():
            note = hold["note"]
            refs.setdefault(ident, set()).update(int(m) for m in _LOT.findall(note))
            for number in _MIGRATION.findall(note):
                refs[ident].update(migrations.get(int(number), ()))
    active = active_hosts() if active is None else active
    needed, unknown, cache = [], [], {}
    for ident, blockers in sorted(refs.items()):
        for blocker in sorted(blockers):
            source = rows.get(blocker)
            if not source or source.get("app") != project.name or blocker == ident:
                continue
            if blocker not in cache:
                cache[blocker] = _merged(st.work.milestones(blocker, limit=None))
            merged = cache[blocker]
            if not merged:
                continue
            sha = rc.commit_of(project.repo, merged["sha"])
            missing_hosts = []
            for host, evidence in active.items():
                current = rc.commit_of(project.repo, evidence["sha"])
                check = rc._git(project.repo, "merge-base", "--is-ancestor", sha, current)
                if check.returncode not in (0, 1):
                    raise rc.TransientReleaseError("unreadable staging ancestry")
                if check.returncode:
                    missing_hosts.append(host)
            if not active:
                unknown.append({"lot": ident, "blocker": blocker, "sha": sha})
            elif missing_hosts:
                needed.append({"lot": ident, "app": rows[ident].get("app"),
                               "blocker": blocker, "sha": sha,
                               "hosts": sorted(missing_hosts), "merged_at": merged.get("at_ts")})
    return {"schema": SCHEMA, "project": project.name, "active": active,
            "waiting": needed, "unknown": unknown,
            "lots": sorted({r["lot"] for r in needed})}


def _window(rules, now):
    policy = rules.staging
    cutoff = rules.next_cutoff(now) - now
    packet = rules.next_packet(now) - now
    return {"before_cutoff_seconds": cutoff, "before_packet_seconds": packet,
            "window_ok": cutoff >= policy.before_cutoff_seconds
                         and packet > policy.before_packet_seconds}


def inspect(cfg, db, project, *, now: float) -> dict:
    from . import release_cutoff as rc
    rules, _source = rc.compiled_rules(cfg, project.name)
    policy = getattr(rules, "staging", None)
    if policy is None or project.name != "ameesh":
        return {"schema": SCHEMA, "project": project.name, "result": "off",
                "reason": "staging rule or host attestation reader absent"}
    rc.fetch(project.repo, project.remote, project.target)
    report = waiting(db, project)
    report["threshold"] = policy.waiting_lots_min
    report["result"] = "needed" if len(report["lots"]) >= policy.waiting_lots_min else "below-threshold"
    if report["result"] != "needed":
        return report
    head = rc.commit_of(project.repo, "%s/%s" % (project.remote, project.target))
    candidate = head if rc.commit_is_green(project.repo, project, head) else rc.last_green(
        project.repo, head, project)
    report["candidate"] = candidate
    report.update(_window(rules, now))
    report["regressions"] = regressions(project, candidate, report["active"]) if candidate else {}
    # A last-green commit before the merged fix would deploy nothing useful.
    missing = set()
    for row in report["waiting"]:
        if not candidate or rc._git(project.repo, "merge-base", "--is-ancestor",
                                    row["sha"], candidate).returncode != 0:
            missing.add(row["lot"])
    report["candidate_unblocks"] = sorted(set(report["lots"]) - missing)
    report["candidate_ready"] = len(report["candidate_unblocks"]) >= policy.waiting_lots_min
    return report


def apply(cfg, db, project, *, now: float, actor=None) -> dict:
    """Pilot/bureau gesture: the usual staging script, no version or tag."""
    from . import release_cutoff as rc, work
    work.corrector(cfg, db, actor, what="staging", history=False)
    started = time.monotonic()
    # Nonblocking lease: never wait through a cutoff then use an old plan.
    key = rc.land.lock_key(project, project.target)
    owner = "staging:" + rc.uuid.uuid4().hex
    ttl = max(project.lock_ttl_s, project.staging_timeout_s + rc.STAGING_UNIT_GRACE_S
              + rc.STAGING_KILL_GRACE_S + rc.STAGING_DRAIN_S)
    epoch = storage.of(db).work.land_lock(key, owner, ttl)
    if epoch is None:
        raise rc.ReleaseBusy("target lease held by train/release")
    try:
        report = inspect(cfg, db, project, now=now)
        if (report["result"] != "needed" or not report.get("candidate")
                or not report.get("candidate_ready") or report.get("regressions")):
            return report
        rules, _source = rc.compiled_rules(cfg, project.name)
        report.update(_window(rules, now + time.monotonic() - started))
        if not report["window_ok"]:
            return report
        if storage.of(db).work.land_lock(key, owner, ttl, epoch=epoch) is None:
            raise rc.ReleaseBusy("target lease lost before staging")
        included = sorted({r["blocker"] for r in report["waiting"]
                           if r["lot"] in report["candidate_unblocks"]})
        report["staging"] = rc._staging_step(project, report["candidate"],
                                             included, cfg.state_dir)
        report["result"] = "staged" if report["staging"].get("exit") == 0 else "failed"
        return report
    finally:
        storage.of(db).work.land_unlock(key, owner, epoch)


def tick(cfg, db, project, *, now: float) -> dict:
    """Alert on the existing release timer, once per candidate/waiting set."""
    from . import release_cutoff as rc
    report = inspect(cfg, db, project, now=now)
    lock = str(Path(cfg.state_dir) / "release" / "release.lock")
    with rc._locked(lock):
        return _notify(cfg, db, project, report)


def _notify(cfg, db, project, report):
    from . import mail, release_cutoff as rc, work
    state = rc.load_state(cfg)
    slot = rc.project_state(state, project.name)
    if report["result"] != "needed":
        if slot.pop("staging_alerts", None) is not None:
            rc.save_state(cfg, state)
        return report
    fingerprint = json.dumps([report["lots"], report["candidate"], report["active"]], sort_keys=True)
    sent = slot.setdefault("staging_alerts", {})
    notify = ((getattr(cfg, "packet_watch", {}) or {}).get("projects", {})
              .get(project.name, {}).get("notify", ()))
    if isinstance(notify, str):
        notify = [notify]
    if not notify:
        from . import sous_utilisation
        notify = sous_utilisation.alert_orchestrators(
            cfg, db, storage.of(db).operations.listing(), declared=())
    targets = sorted(set(notify) | {"bureau"})
    text = ("Staging %s : %d lots attendent des correctifs fusionnés non actifs : %s. "
            "Candidate verte : %s. Vérifier puis lancer ameesh release staging-needed "
            "--project %s --apply (sans tag/version)."
            % (project.name, len(report["lots"]), ", ".join("#%d" % n for n in report["lots"]),
               report["candidate"] or "aucune", project.name))
    for target in targets:
        if sent.get(target) == fingerprint:
            continue
        mail.send(db, work.SYSTEM_SENDER, target, text, kind="event",
                  payload={"urgent": True, "staging_pending": report})
        sent[target] = fingerprint
        rc.save_state(cfg, state)
    return report
