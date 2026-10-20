# SPDX-License-Identifier: AGPL-3.0-only
"""#2844: public lifecycle receipts, replayable from native facts.

Only explicitly public status lines are read from free-form notes.
The remote marker is the receipt: a crash after POST is reconciled next pass.
The designated GitHub projection host remains the single writer (L126).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re

MARKER = "<!-- ameesh:work-stage:v1 lot=%d event=%s -->"
PUBLIC_STATUS = re.compile(r"(?im)^Public status:[ \t]*(\S[^\r\n]*)$")


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
            if isinstance(lot, int) and not isinstance(lot, bool):
                receipts.setdefault(lot, []).append((version, day))
    return receipts


def stages(item, milestones, events, *, receipts=(), closing=None):
    """Yield stable event keys and public text; historical notes stay private."""
    priority = {1: "P1", 2: "P2", 3: "P3"}.get(item.get("priority"), "not specified")
    target = item.get("planned_delivery") or item.get("planned_end") or "not scheduled"
    yield "accepted", "Accepted. Priority: %s; target: %s." % (priority, target)
    timeline = []
    kinds = set()
    for mark in reversed(milestones):
        kind = mark.get("kind")
        if kind not in ("frozen", "merged") or mark.get("annulled"):
            continue
        kinds.add(kind)
        timeline.append((float(mark.get("at_ts") or 0), "milestone-%s" % mark["id"], {
            "frozen": "Frozen for review.", "merged": "Merged; delivery confirmation will follow."
        }[kind]))
    previous = None
    for event in reversed(events):
        state = event.get("state")
        if state in ("blocked", "waiting_human") and state != previous:
            match = PUBLIC_STATUS.search(event.get("note") or "")
            detail = (match.group(1) if match else
                      "The team will specify the required decision or information on this ticket.")
            timeline.append((float(event.get("created_ts") or 0), "event-%s" % event["id"],
                             ("Blocked. " if state == "blocked" else
                              "Awaiting a decision. ") + detail))
        previous = state
    for _at, key, text in sorted(timeline):
        yield key, text
    # Process-backed stages can change without legacy milestones. A validated
    # current core supplies a receipt only when no native milestone exists.
    if item.get("state") == "merged" and "merged" not in kinds:
        yield "merged", "Merged; delivery confirmation will follow."
    if item.get("state") == "promoted":
        yield "completed", "Completed; release delivery confirmation will follow when available."
    if closing:
        yield "closed", closing
    for version, day in receipts:
        # Stable per release, even if the report was retried or reconciled.
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", version):
            yield "delivered-%s" % version, "Delivered in release %s on %s." % (version, day)


def project(db, st, gh_repo, report, mine, *, guard, refused, project, closings):
    from . import plan_github

    receipts = release_receipts(getattr(db, "cfg", None), project)
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
            candidates = list(stages(item, st.work.milestones(lot, None),
                                     st.work.events(lot, None), receipts=receipts.get(lot, ()),
                                     closing=closings.get(lot)))
            existing = "\n".join(row.get("body") or "" for row in gh_repo.comments(number))
            seen = set()
            for key, text in candidates:
                marker = MARKER % (lot, key)
                if marker in existing or marker in seen:
                    continue
                seen.add(marker)
                # All comments obey the public policy, even in private repos:
                # lifecycle receipts never publish private mesh details.
                problems = guard.problems(text, public=True)
                if problems:
                    report["withheld"].append({"work_item": lot, "field": "stage comment",
                                                "reasons": problems})
                    continue
                body = marker + "\n" + plan_github._quiet(text)
                report["comments"].append({"work_item": lot, "number": number, "event": key,
                                            "body": body})
                if not gh_repo.dry_run:
                    gh_repo.comment(number, body)
        except plan_github.GithubError as exc:
            report["errors"].append({"work_item": lot, "number": number,
                                     "action": "stage-comment", "detail": plan_github._one_line(exc)})
