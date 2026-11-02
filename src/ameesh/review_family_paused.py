# SPDX-License-Identifier: AGPL-3.0-only
"""#5424: identify a review quorum held by a whole budget-paused family.

Pure calculation shared by alerts and debit. Registry eligibility and the
latest freeze's designation are evidence; names and prose holds are not.
"""
from __future__ import annotations

from . import accounts, budget, review_designation as rd, work_lifecycle as lifecycle


def _account(row):
    reason = str(row.get("status_text") or "")[len(budget.PAUSE_PREFIX):].strip()
    if reason.lower().startswith(budget.CRITICAL_MARK.lower()):
        reason = reason[len(budget.CRITICAL_MARK):].strip()
    nature = accounts.pause_nature(reason).split("|")
    return nature[1] if nature[0] == "pause" else row.get("session_account")


def blocked(designation, listing, *, project, families, now):
    """Return indispensable paused families, never infer an unknown author."""
    required = designation.get("required")
    authors = set(designation.get("author_families") or ())
    if (not required or not authors or designation.get("unknown_authors")
            or designation.get("quorum") or not designation.get("shortfall")):
        return []
    candidates = rd.candidates_from_registry(listing, project=project, families=families)
    by_name = {row["name"]: row for row in listing}
    by_family = {}
    for candidate in candidates:
        if candidate.family and candidate.family not in authors:
            by_family.setdefault(candidate.family, []).append(candidate)
    acquired = set(designation.get("acquired_families") or ()) - authors
    reachable = acquired | {family for family, rows in by_family.items()
                            if any(row.available for row in rows)}
    if len(reachable) >= required or len(set(by_family) | acquired) < required:
        return []
    out = []
    for family, candidates in sorted(by_family.items()):
        if family in reachable:
            continue
        pauses = []
        for candidate in candidates:
            row = by_name[candidate.name]
            text = str(row.get("status_text") or "")
            # A dead/retired/operator-held reviewer is not evidence of a
            # budget pause. Short account windows remain eligible (#3876).
            resumed = dict(row, status="idle", status_text="")
            if (not rd.is_available(resumed)
                    or str(row.get("status") or "").lower() in ("dead", "stopped")
                    or not text.lower().startswith(budget.PAUSE_PREFIX)
                    or rd.is_short_window_pause(text)):
                break
            deadline = accounts.pause_deadline(text)
            if deadline is not None and deadline <= now:
                break
            account = _account(row)
            pauses.append({"name": candidate.name,
                           "account": account,
                           "resume_ts": deadline, "reason": text})
        else:
            if pauses:
                deadlines = [p["resume_ts"] for p in pauses if p["resume_ts"] is not None]
                out.append({"family": family, "pauses": pauses,
                            "resume_ts": min(deadlines) if deadlines else None})
    return out


def alerts(items, milestones, events, listing, *, families, now):
    """One alert per family/episode, across projects and frozen lots."""
    from . import debit
    grouped = {}
    ms, ev = {}, {}
    for row in milestones:
        ms.setdefault(int(row["work_item_id"]), []).append(row)
    for row in events:
        event = dict(row)
        event.setdefault("at_ts", event.get("created_ts"))
        ev.setdefault(int(row["work_item_id"]), []).append(event)
    agent_families = {str(row["name"]).casefold(): rd.live_family(row, families=families)
                      for row in listing}
    for item in items:
        ident = int(item["id"])
        if item.get("state") and item["state"] not in lifecycle.TO_VALIDATE:
            continue
        designation = debit.designations(ms.get(ident, []), ev.get(ident, []),
                                          agent_families=agent_families)
        if not designation.get("freeze"):
            continue
        for pause in blocked(designation, listing, project=item.get("app") or "",
                             families=families, now=now):
            family = pause["family"]
            alert = grouped.setdefault(family, dict(
                type="review_family_paused", agent=None, since=None, value=None,
                threshold=None, family=family,
                resume_ts=pause["resume_ts"], pauses=[], held_lots=[]))
            for row in pause["pauses"]:
                if row not in alert["pauses"]:
                    alert["pauses"].append(row)
            frozen = float(designation["freeze"].get("at_ts") or now)
            alert["held_lots"].append(dict(lot=ident, issue_ref=item.get("issue_ref"),
                                      project=item.get("app"), age_s=max(0, now-frozen)))
    for alert in grouped.values():
        # The episode belongs to the family, not to today's held projects.
        # Removing a project's last lot must not create another notification.
        alert["episode"] = sorted({
            (_account(row) or "", accounts.pause_deadline(row.get("status_text") or ""))
            for row in listing if rd.live_family(row, families=families) == alert["family"]
            and str(row.get("status_text") or "").lower().startswith(budget.PAUSE_PREFIX)
            and not rd.is_short_window_pause(row["status_text"])}, key=repr)
        deadlines = [p["resume_ts"] for p in alert["pauses"] if p["resume_ts"] is not None]
        alert["resume_ts"] = min(deadlines) if deadlines else None
        alert["held_lots"].sort(key=lambda row: row["lot"])
        alert["lots"] = [row["lot"] for row in alert["held_lots"]]
        alert["detail"] = description(alert)
    return list(grouped.values())


def description(alert):
    import datetime
    deadline = alert.get("resume_ts")
    resume = (datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc).isoformat()
              if deadline is not None else "unknown (check account)")
    accounts_ = ", ".join(sorted({p.get("account") or "unknown" for p in alert["pauses"]}))
    lots = ", ".join("#%s (%s, %.1f h)" % (row["lot"], row.get("issue_ref") or "no issue",
                                          row["age_s"] / 3600) for row in alert["held_lots"])
    return ("Review family %s paused on budget; account(s): %s; earliest resume: %s. "
            "Held lots: %s. Expected action: inspect ameesh accounts list and ameesh budget; "
            "use an authorized fallback account, obtain a cap decision, or request an owner "
            "decision. Preserve the required independent review families."
            % (alert["family"], accounts_, resume, lots))
