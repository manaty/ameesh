# SPDX-License-Identifier: AGPL-3.0-only
"""#2844: public lifecycle receipts, replayable from native facts.

Only explicitly public status lines are read from free-form notes.
The remote marker is the receipt: a crash after POST is reconciled next pass.
The designated GitHub projection host remains the single writer (L126), so
the local checkpoint needs no lock: its atomic write never exposes a partial
file, and a lost update replays from the remote markers. The checkpoint is a
cache, never the authority (#2854): a receipt entry that is malformed, of an
unknown format or manually cleared is reset and replayed from the thread. A
comment deleted by hand is repaired by removing its key from ``seen`` (or the
whole ticket entry on an open thread) and rerunning the projection; markers
already present are re-read and only missing comments are posted.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import datetime as dt
import json
from pathlib import Path
import re
import time

MARKER = "<!-- ameesh:work-stage:v1 lot=%d event=%s -->"
PUBLIC_STATUS = re.compile(r"(?im)^Public status:[ \t]*(\S[^\r\n]*)$")
WAIT_DETAIL = "The team will specify the required decision or information on this ticket."
#: Root version of the local receipt cache (#2854). A file without it is the
#: legacy v0 map and is upgraded on its next save; an unknown version is left
#: untouched so an older binary never clobbers a newer format.
CHECKPOINT_SCHEMA = "ameesh/github-stage-comments/1"
SCHEMA_KEY = "schema"
_MISSING = object()


def _saved_entry(value):
    """Return a usable receipt entry, or None when it must replay (#2854)."""
    if (isinstance(value, dict) and isinstance(value.get("enrolled"), bool)
            and isinstance(value.get("seen"), list)
            and all(isinstance(key, str) for key in value["seen"])):
        return value
    return None


def _waiting(state, detail=WAIT_DETAIL):
    return ("Blocked. " if state == "blocked" else "Awaiting a decision. ") + detail


def _date(value):
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def release_receipts(cfg, project):
    """Use durable successful release+activation reports, never a tag alone."""
    folder = getattr(cfg, "state_dir", None)
    if not folder:
        return {}
    receipts = {}
    for path in sorted((Path(folder) / "release" / "reports").glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(report, dict):
            continue
        staging = report.get("staging") or {}
        if (report.get("schema") != "ameesh-release-cutoff/1"
                or report.get("project") != project
                or report.get("result") not in ("released", "reconciled")
                or not report.get("tag")
                or not isinstance(staging, dict) or staging.get("exit") != 0
                or not isinstance(staging.get("attestation"), dict)):
            continue
        attestation = staging["attestation"]
        sha = report.get("release_sha")
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or attestation.get("schema") != "ameesh-staging-actif/1"
                or attestation.get("sha") != sha
                or not isinstance(attestation.get("hotes"), dict)
                or len(attestation["hotes"]) != 4):
            continue
        version = report.get("version")
        day = _date(report.get("completed_at"))
        if not isinstance(version, str) or not day:
            continue
        lots = report.get("included") or attestation.get("lots") or []
        if not isinstance(lots, list):
            continue
        for lot in lots:
            # actif's --lot values are strings; cutoff's included values are ints.
            if ((isinstance(lot, int) and not isinstance(lot, bool) and lot > 0)
                    or (isinstance(lot, str) and re.fullmatch(r"[1-9][0-9]*", lot))):
                receipts.setdefault(int(lot), []).append(
                    (version, day, float(report.get("completed_at"))))
    return receipts


def _current_receipts(receipts, since):
    """#2874: one delivery per lot, and only for releases cut after `since`.

    Releases completed before the stage comments were switched on are history:
    replaying them posted a "Delivered in release X" for every past release on
    every mirror. A lot listed by several reports keeps its earliest release.
    """
    kept = [r for r in receipts if len(r) < 3 or r[2] >= since]
    kept.sort(key=lambda r: r[2] if len(r) > 2 else 0.0)
    return [tuple(kept[0][:2])] if kept else []


def stages(item, milestones, events, *, receipts=(), closing=None):
    """Yield stable event keys and public text; historical notes stay private."""
    priority = {1: "P1", 2: "P2", 3: "P3"}.get(item.get("priority"), "not specified")
    target = item.get("planned_delivery") or item.get("planned_end") or "not scheduled"
    yield "accepted", "Accepted. Priority: %s; target: %s." % (priority, target)
    timeline = []
    kinds = set()
    for mark in milestones:
        kind = mark.get("kind")
        if kind not in ("frozen", "merged") or mark.get("annulled"):
            continue
        # #2861: newest-first: retain the latest freeze in the timeline.
        # One public "Frozen" per lot; a re-freeze is not a new stage
        # for the requester (a lot can be frozen ten times before it lands).
        if kind == "frozen" and "frozen" in kinds:
            continue
        kinds.add(kind)
        key = "frozen" if kind == "frozen" else "milestone-%s" % mark["id"]
        timeline.append((float(mark.get("at_ts") or 0), key, {
            "frozen": "Frozen for review.", "merged": "Merged; delivery confirmation will follow."
        }[kind]))
    previous = None
    for event in reversed(events):
        state = event.get("state")
        if state in ("blocked", "waiting_human") and state != previous:
            match = PUBLIC_STATUS.search(event.get("note") or "")
            detail = match.group(1) if match else WAIT_DETAIL
            timeline.append((float(event.get("created_ts") or 0), "event-%s" % event["id"],
                             _waiting(state, detail)))
        previous = state
    for _at, key, text in sorted(timeline):
        yield key, text
    # Process-backed stages can change without legacy milestones. A validated
    # current core supplies a receipt only when no native milestone exists.
    if lifecycle.phase(item) == "merged" and "merged" not in kinds:
        yield "merged", "Merged; delivery confirmation will follow."
    if lifecycle.phase(item) == "promoted":
        yield "completed", "Completed; release delivery confirmation will follow when available."
    if closing:
        yield "closed", closing
    for version, day in receipts:
        # Stable per release, even if the report was retried or reconciled.
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", version):
            yield "delivered-%s" % version, "Delivered in release %s on %s." % (version, day)


def project(db, st, gh_repo, report, mine, *, guard, refused, project, closings):
    from . import plan_github

    raw_receipts = release_receipts(getattr(db, "cfg", None), project)
    # Single writer, as for the issue projection. Local receipts avoid re-reading
    # completed threads; remote markers still reconcile a POST with a lost ack.
    folder = getattr(getattr(db, "cfg", None), "state_dir", None)
    path = Path(folder) / "github-stage-comments.json" if folder else None
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
        if not isinstance(checkpoint, dict):
            raise ValueError("invalid stage-comment checkpoint")
        version = checkpoint.get(SCHEMA_KEY)
        if version is not None and version != CHECKPOINT_SCHEMA:
            # Never rewrite a format a newer binary may own (#2854).
            raise ValueError("unsupported stage-comment checkpoint schema %r" % (version,))
    except (OSError, ValueError) as exc:
        report["errors"].append({"action": "stage-checkpoint", "detail": str(exc)})
        return

    # #2874: the first real pass fixes the start of the public timeline.
    since = checkpoint.get("_since")
    if not isinstance(since, (int, float)):
        since = time.time()

    def save():
        if path and not gh_repo.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint[SCHEMA_KEY] = CHECKPOINT_SCHEMA
            plan_github._atomic_write(str(path), checkpoint)

    if not gh_repo.dry_run and "_since" not in checkpoint:
        checkpoint["_since"] = since
        save()      # the cutoff must survive a pass that posts nothing

    receipts = {lot: kept for lot, rows in raw_receipts.items()
                if (kept := _current_receipts(rows, since))}
    entries = {row["work_item"]: row for row in report["issues"]}
    for item, issue in mine:
        lot = int(item["id"])
        entry = entries.get(lot, {})
        number = entry.get("number")
        if (not number or lot in refused or entry.get("error")
                or entry.get("action") in ("refused", "deferred")):
            continue
        # project_lots selected this mirror. An external issue_ref is kept as
        # the request's origin; it need not point at the projected issue.
        try:
            ticket = "%s#%s/lot-%s" % (gh_repo.repo, number, lot)
            saved = checkpoint.get(ticket, _MISSING)
            usable = _saved_entry(saved) if saved is not _MISSING else None
            if saved is _MISSING:
                saved = {"enrolled": False, "seen": []}
            elif usable is not None:
                saved = usable
            else:
                # The cache is not the authority: an unreadable entry replays
                # from the remote markers below, so a deleted comment can be
                # re-posted without duplicating the ones still present.
                report["errors"].append({
                    "work_item": lot, "number": number, "action": "stage-checkpoint",
                    "detail": "invalid receipt entry reset; replaying from remote markers"})
                saved = {"enrolled": False, "seen": []}
                checkpoint[ticket] = saved
                # Persist the reset even for a closed thread that will not
                # replay, so the anomaly is reported once, not every pass.
                save()
            # `issue` is the snapshot BEFORE project_lots applies a closure.
            # Enroll open/new mirrors, never backfill an already closed thread.
            if issue is None or (issue.get("state") or "").lower() == "open":
                if not saved["enrolled"]:
                    saved["enrolled"] = True
                    # #2861: a mirror that already existed is enrolled from
                    # its current stage only, never with the whole history.
                    saved["baseline"] = issue is not None
                    checkpoint[ticket] = saved
                    save()
            if not saved["enrolled"] and not receipts.get(lot):
                continue
            checkpoint[ticket] = saved
            candidates = list(stages(item, st.work.milestones(lot, None),
                                     st.work.events(lot, None), receipts=receipts.get(lot, ()),
                                     closing=closings.get(lot)))
            if not saved["enrolled"]:
                candidates = [(key, text) for key, text in candidates if key.startswith("delivered-")]
            if saved.get("baseline") and not gh_repo.dry_run:
                # #2861: keep the latest stage, mark the older ones as seen
                history = [key for key, _text in candidates[:-1]
                           if not key.startswith("delivered-")]
                saved["seen"].extend(k for k in history if k not in saved["seen"])
                saved["baseline"] = False
                checkpoint[ticket] = saved
                save()
            candidates = [(key, text) for key, text in candidates if key not in saved["seen"]]
            if any(key.startswith("delivered-") for key in saved["seen"]):
                candidates = [(key, text) for key, text in candidates
                              if not key.startswith("delivered-")]
            if not candidates:
                continue
            existing = "\n".join(row.get("body") or "" for row in gh_repo.comments(number))
            seen = set()
            for key, text in candidates:
                marker = MARKER % (lot, key)
                if key.startswith("delivered-") and (
                        "<!-- ameesh:work-stage:v1 lot=%d event=delivered-" % lot) in existing:
                    # A late/reconciled report can change the earliest receipt.
                    # An existing delivery still closes this lot's timeline.
                    saved["seen"].append(key)
                    continue
                if marker in existing:
                    if key not in saved["seen"]:
                        saved["seen"].append(key)
                    continue
                if marker in seen:
                    continue
                seen.add(marker)
                pr = plan_github._ref_parts(item.get("pr_ref"))
                if text.startswith("Merged;") and pr and pr[0].casefold() == gh_repo.repo.casefold():
                    text = text.replace("Merged;", "Merged by PR #%d;" % pr[1], 1)
                # All comments obey the public policy, even in private repos:
                # lifecycle receipts never publish private mesh details.
                problems = guard.problems(text, public=True)
                if problems:
                    report["withheld"].append({"work_item": lot, "field": "stage comment",
                                                "reasons": problems})
                    if key.startswith("event-"):
                        text = _waiting("blocked" if text.startswith("Blocked.") else "waiting_human")
                    else:
                        continue
                    if guard.problems(text, public=True):
                        continue
                body = marker + "\n" + plan_github._quiet(text)
                report["comments"].append({"work_item": lot, "number": number, "event": key,
                                            "body": body})
                if not gh_repo.dry_run:
                    gh_repo.comment(number, body)
                    saved["seen"].append(key)
            save()
        except (plan_github.GithubError, OSError) as exc:
            report["errors"].append({"work_item": lot, "number": number,
                                     "action": "stage-comment", "detail": plan_github._one_line(exc)})
