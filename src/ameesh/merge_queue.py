# SPDX-License-Identifier: AGPL-3.0-only
"""Merge queue per target (lot L160e1, study L160 § 3.3).

    ameesh work queue add <lot>       the lot is ready: into its target's queue
    ameesh work queue remove <lot>    out of the queue (to push again, or give up)
    ameesh work queue show [--target T] [--json]

A lot enters the queue of its target (`branch_target`, else the repository's
default target) when it is ready, which replaces the "merge light" given by
hand:

* frozen: a `frozen` milestone with its commit (`ameesh work milestone <lot>
  frozen --sha …`), and the branch still at that commit;
* review verdict `ok` on that very commit (`ameesh work milestone <lot>
  verdict --verdict ok --sha …`);
* CI green when the branch has an open PR (a team that merges locally has
  none: nothing to check).

A queued lot is frozen: its agent no longer pushes to its branch without
taking it out first. At each pass (`radar.run_pass`), for each queue:

1. a lot whose branch moved past its freeze is ejected; a lot that merged
   is `landed`;
2. the queue is ordered: priority first, then the lots without conflict with
   another queued lot, then those whose conflicts are prepared (L160d), then
   arrival. A lot whose conflict with a lot ahead of it is not prepared waits
   behind it, and the resolver of that pair is woken once: the conflict now
   blocks;
3. the queue is replayed stacked in the radar's private clone: the target,
   plus the lots ahead, plus this one, each merge with `rerere` primed by
   the resolutions of this lot with the lots ahead. A conflict that no
   resolution covers ejects the lot (its agent is woken) instead of blocking
   the lots behind it.

Nothing is merged here: `ameesh work land` (L160e2) merges the head of the
queue.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from . import plan_git, storage, work
from .canon import GitError

STATES = ("queued", "landing", "landed", "ejected")
#: pair states where the conflict between two queued lots is not covered
UNPREPARED = ("conflict", "stale", "error")


class QueueError(RuntimeError):
    """A lot that cannot enter the queue: actionable message."""


# --------------------------------------------------------------------------
# entering and leaving
# --------------------------------------------------------------------------

def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def latest_verdict(rows: list[dict]) -> dict | None:
    verdicts = [r for r in rows if r.get("kind") == "verdict"]
    return max(verdicts, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0))) \
        if verdicts else None


def readiness(db, lot: int, repo: str, *, ci=None, fetch: bool = True) -> dict:
    """Checks that `lot` may enter its queue. Returns `{target, frozen_sha,
    pr}`; QueueError says what is missing."""
    from . import resolutions
    row = work.get(db, lot)
    if row is None:
        raise QueueError("lot #%d not found" % lot)
    if row.get("state") in work.MERGED_STATES or row.get("state") == "closed":
        raise QueueError("lot #%d is %s: nothing to merge" % (lot, row["state"]))
    if not row.get("branch"):
        raise QueueError("lot #%d has no branch (ameesh work assign %d <agent> --branch …)"
                         % (lot, lot))
    marks = work.milestones(db, lot)
    freeze = plan_git.latest_freeze(marks)
    if not freeze or not freeze.get("sha"):
        raise QueueError("lot #%d is not frozen: ameesh work milestone %d frozen --sha <commit>"
                         % (lot, lot))
    tip = resolutions._branch_tip(repo, row["branch"], fetch=fetch)
    if not tip:
        raise QueueError("branch %s of lot #%d is neither local nor on origin"
                         % (row["branch"], lot))
    if not _same(tip, freeze["sha"]):
        raise QueueError("branch %s is at %s, not at its freeze %s: freeze it again "
                         "(ameesh work milestone %d frozen --sha %s), then have it reviewed"
                         % (row["branch"], tip[:12], freeze["sha"][:12], lot, tip[:12]))
    verdict = latest_verdict(marks)
    if not verdict or verdict.get("verdict") != "ok" or not _same(verdict.get("sha"), tip):
        raise QueueError("lot #%d has no review verdict ok on its freeze %s%s"
                         % (lot, tip[:12], " (last verdict: %s on %s)"
                            % (verdict.get("verdict"), (verdict.get("sha") or "—")[:12])
                            if verdict else ""))
    status = ci(row["branch"]) if ci else None
    if status and not status.get("green"):
        raise QueueError("the CI of PR #%s is not green" % status.get("number"))
    target = row.get("branch_target") or plan_git.default_target(repo)
    return {"target": target, "frozen_sha": tip, "pr": (status or {}).get("number")}


def enter(db, lot: int, *, repo: str, actor: str = "", ci="github") -> dict:
    """Puts `lot` into the queue of its target, once `readiness` passes."""
    if ci == "github":
        from . import radar
        ci = radar.github_ci(radar.Project(name="", repo=repo))
    ready = readiness(db, lot, repo, ci=ci)
    row = storage.of(db).work.queue_add(
        lot, target=ready["target"], frozen_sha=ready["frozen_sha"],
        note="merge queue of %s: queued at %s%s" % (
            ready["target"], ready["frozen_sha"][:12],
            ", PR #%s green" % ready["pr"] if ready["pr"] else ""), actor=actor)
    if row is None:
        raise QueueError("lot #%d is no longer open" % lot)
    return row


def leave(db, lot: int, *, actor: str = "", reason: str = "") -> bool:
    return storage.of(db).work.queue_remove(
        lot, note="merge queue: taken out%s" % (" (%s)" % reason if reason else ""),
        actor=actor)


# --------------------------------------------------------------------------
# order
# --------------------------------------------------------------------------

def _priority(row: dict) -> int:
    return int(row.get("priority") or 2)


def pair_states(report: dict) -> dict:
    """{frozenset(lots): pair} of the radar report."""
    return {frozenset(p["lots"]): p for p in report.get("pairs") or ()}


def order(entries: list[dict], pairs: dict) -> list[dict]:
    """The queued lots of one target in merge order: `[{work_item, rank,
    reason, waits: [(other, resolver)]}]`. `pairs`: `pair_states`."""
    ids = [int(e["work_item_id"]) for e in entries]

    def conflicts(lot: int) -> dict:
        out = {}
        for other in ids:
            pair = pairs.get(frozenset((lot, other))) if other != lot else None
            if pair and pair["state"] in UNPREPARED + ("prepared",):
                out[other] = pair
        return out

    def klass(lot: int) -> int:
        found = conflicts(lot)
        if not found:
            return 0
        return 2 if any(p["state"] in UNPREPARED for p in found.values()) else 1

    ranked = sorted(entries, key=lambda e: (_priority(e), klass(int(e["work_item_id"])),
                                            e.get("queued_ts") or 0, int(e["work_item_id"])))
    out = []
    ahead: list[int] = []
    for n, e in enumerate(ranked, 1):
        lot = int(e["work_item_id"])
        found = conflicts(lot)
        waits = [(other, (p.get("resolver") or {}).get("lot"))
                 for other, p in sorted(found.items())
                 if other in ahead and p["state"] in UNPREPARED]
        if waits:
            reason = "waits for the resolution of its conflict with %s" % ", ".join(
                "#%d" % o for o, _ in waits)
        elif found and any(o in ahead for o in found):
            reason = "conflicts with %s prepared" % ", ".join(
                "#%d" % o for o in sorted(found) if o in ahead)
        else:
            reason = "priority %d, %s" % (_priority(e), "no conflict in the queue"
                                          if not found else "conflicts with lots behind it")
        out.append({"work_item": lot, "rank": n, "reason": reason, "waits": waits})
        ahead.append(lot)
    return out


# --------------------------------------------------------------------------
# stacked replay in the radar's clone
# --------------------------------------------------------------------------

def stack(replayer, target_head: str, lots: list[dict], rows: list[dict]) -> dict:
    """Replays the queue stacked: from `target_head`, merges each lot of
    `lots` (`{work_item, tip}`, in order) with `rerere` primed by the
    resolutions (`rows`) of this lot with the lots already stacked. A lot
    that leaves a conflict is skipped. Returns `{lot: {state: clean|
    replayed|conflict, files}}`."""
    from . import resolutions
    latest: dict = {}
    for row in rows:
        latest.setdefault(frozenset((int(row["work_item_id"]), int(row["other_item_id"]))), row)
    ids = {int(one["work_item"]) for one in lots}
    wanted = [r["commit_sha"] for key, r in latest.items() if key <= ids]
    missing = set(replayer.prime(wanted)) if wanted else set()
    path = replayer.path
    run = resolutions._run
    resolutions._check(run(path, "checkout", "-q", "-f", "--detach", target_head), "checkout")
    stacked: list[int] = []
    out: dict = {}
    try:
        for one in lots:
            lot = int(one["work_item"])
            base = resolutions._check(run(path, "rev-parse", "HEAD"), "rev-parse")
            replayer._empty_cache()
            for other in stacked:
                row = latest.get(frozenset((lot, other)))
                if row and row["commit_sha"] not in missing \
                        and row["commit_sha"] in replayer.trained():
                    shutil.copytree(replayer._store(row["commit_sha"]), replayer._rr_cache(),
                                    dirs_exist_ok=True)
            merge = run(path, "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=false",
                        "merge", "--no-ff", "--no-commit", "--quiet", one["tip"])
            state, files = "clean", []
            if merge.returncode != 0:
                files = resolutions.unmerged(path)
                remaining = resolutions.rerere_remaining(path) if files else []
                left = sorted(set(remaining) | set(resolutions.with_markers(path, files)))
                if not files or left:
                    run(path, "merge", "--abort")
                    run(path, "reset", "-q", "--hard", base)
                    out[lot] = {"state": "conflict", "files": left or files,
                                "error": "" if files else resolutions._err(merge)[:300]}
                    continue
                resolutions._check(run(path, "add", "--", *files), "add")
                state = "replayed"
            resolutions._check(run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                                   "--no-verify", "-m", "queue: #%d" % lot), "commit")
            out[lot] = {"state": state, "files": files}
            stacked.append(lot)
    finally:
        run(path, "reset", "-q", "--hard")
        run(path, "clean", "-q", "-f", "-d")
        replayer._empty_cache()
    return out


# --------------------------------------------------------------------------
# a radar pass
# --------------------------------------------------------------------------

def _notified_path(cfg, project: str) -> str:
    from . import radar
    return os.path.join(radar.journal_dir(cfg), project + ".queue-notified")


def _read_notified(cfg, project: str) -> set:
    try:
        with open(_notified_path(cfg, project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _write_notified(cfg, project: str, keys: set) -> None:
    from . import radar
    directory = radar.journal_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s.queue-notified." % project)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(sorted(keys), fh)
    os.replace(tmp, _notified_path(cfg, project))


def _wake(db, to: str | None, lot: int, text: str, data: dict) -> int | None:
    """An ordinary mail (it wakes the agent, L125 batching): the queue is
    blocked on it."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot), payload={"merge_queue": data})
    except Exception:  # noqa: BLE001 - retried at the next pass
        return None


def _eject(db, row: dict, reason: str, text: str, data: dict) -> dict:
    lot = int(row["work_item_id"])
    storage.of(db).work.queue_update(lot, state="ejected", reason=reason,
                                     note="merge queue of %s: ejected, %s"
                                     % (row["target"], reason), actor=work.SYSTEM_SENDER)
    sent = _wake(db, row.get("assignee"), lot, text, dict(data, lot=lot, reason=reason))
    return {"lot": lot, "to": row.get("assignee"), "kind": "ejected", "message_id": sent}


def run_pass(cfg, db, project, report: dict, *, replayer=None, cache=None) -> dict:
    """One pass of the queues of `project`, after the radar scan and the
    replay of the resolutions. Writes ranks and ejections; returns
    `{targets: {target: [{lot, rank, state, reason, stack}]}, mail: […]}`,
    kept in the radar report. Never raises for one queue: the error is
    noted."""
    from . import resolutions
    out: dict = {"targets": {}, "mail": []}
    try:
        rows = storage.of(db).work.queue()
    except Exception as exc:  # noqa: BLE001 - e.g. database not yet migrated (0058)
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    branches = {b["work_item"]: b for b in report.get("branches") or ()}
    mine = [r for r in rows if r["state"] in ("queued", "landing")]
    if not mine:
        return out
    notified = _read_notified(cfg, project.name)
    keys = set()
    pairs = pair_states(report)
    by_target: dict = {}
    for row in mine:
        lot = int(row["work_item_id"])
        if row.get("item_state") in work.MERGED_STATES:
            storage.of(db).work.queue_update(lot, state="landed", reason="merged",
                                             note="merge queue of %s: landed" % row["target"],
                                             actor=work.SYSTEM_SENDER)
            continue
        branch = branches.get(lot)
        if branch is None or branch.get("target") != row["target"]:
            continue  # another repository, or not visible on this host
        if not _same(branch["tip"], row["frozen_sha"]):
            reason = "branch moved past its freeze (%s, frozen %s)" % (
                branch["tip"][:12], row["frozen_sha"][:12])
            out["mail"].append(_eject(db, row, reason, (
                "Merge queue of %s, lot #%d « %s »: out of the queue, its branch %s moved "
                "past the freeze (%s, frozen %s). A queued lot is frozen: freeze it again, "
                "have it reviewed, then ameesh work queue add %d."
                % (row["target"], lot, row.get("title") or "", row.get("branch") or "?",
                   branch["tip"][:12], row["frozen_sha"][:12], lot)),
                {"project": project.name, "target": row["target"], "kind": "ejected"}))
            continue
        by_target.setdefault(row["target"], []).append(dict(row, tip=branch["tip"],
                                                            target_head=branch["target_head"]))
    try:
        resolution_rows = storage.of(db).work.resolutions(
            [int(r["work_item_id"]) for rs in by_target.values() for r in rs]) \
            if by_target else []
    except Exception:  # noqa: BLE001 - database without 0057: no replay
        resolution_rows = []
    if replayer is None and by_target:
        from . import radar
        replayer = resolutions.Replayer(project.repo, radar.clone_dir(cfg, project.name))
    stacks = getattr(cache, "stacks", None) if cache is not None else None
    stacks = {} if stacks is None else stacks
    for target, entries in sorted(by_target.items()):
        ranked = order(entries, pairs)
        rows_by_id = {int(e["work_item_id"]): e for e in entries}
        for one in ranked:
            row = rows_by_id[one["work_item"]]
            if row.get("rank") != one["rank"] or row.get("reason") != one["reason"]:
                storage.of(db).work.queue_update(one["work_item"], rank=one["rank"],
                                                 reason=one["reason"])
        heads = {e["target_head"] for e in entries}
        result: dict = {}
        if len(heads) == 1:
            commits = tuple(sorted(r["commit_sha"] for r in resolution_rows))
            key = (heads.pop(), tuple((o["work_item"], rows_by_id[o["work_item"]]["tip"])
                                      for o in ranked), commits)
            if key not in stacks:
                try:
                    replayer.ensure()
                    stacks[key] = stack(replayer, key[0],
                                        [{"work_item": o["work_item"],
                                          "tip": rows_by_id[o["work_item"]]["tip"]}
                                         for o in ranked], resolution_rows)
                except (GitError, OSError) as exc:
                    stacks[key] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            result = stacks[key]
        listed = []
        for one in ranked:
            row = rows_by_id[one["work_item"]]
            trial = result.get(one["work_item"]) if isinstance(result, dict) else None
            state = "queued"
            if trial and trial["state"] == "conflict":
                lot = one["work_item"]
                reason = "stacked trial: conflict not covered on %s" % ", ".join(
                    trial["files"][:6] or ["?"])
                out["mail"].append(_eject(db, row, reason, (
                    "Merge queue of %s, lot #%d « %s »: out of the queue. Merged after the "
                    "lots ahead of it, it conflicts on %s and no prepared resolution covers "
                    "it. Prepare it (ameesh work resolve %d --with <lot>) or integrate %s, "
                    "freeze again, then ameesh work queue add %d."
                    % (target, lot, row.get("title") or "", ", ".join(trial["files"][:6]),
                       lot, target, lot)),
                    {"project": project.name, "target": target, "kind": "ejected",
                     "files": trial["files"]}))
                state = "ejected"
            listed.append({"lot": one["work_item"], "rank": one["rank"], "state": state,
                           "reason": one["reason"], "stack": trial})
        for one in ranked:
            ejected = (result.get(one["work_item"]) or {}).get("state") == "conflict" \
                if isinstance(result, dict) else False
            for other, resolver in one["waits"]:
                key = "wait:%d-%d" % (other, one["work_item"])
                keys.add(key)
                if key in notified:
                    continue
                who = resolver or one["work_item"]
                if ejected and who == one["work_item"]:
                    continue  # its ejection mail already says to prepare it
                holder = rows_by_id.get(who) or {}
                to = holder.get("assignee") or (branches.get(who) or {}).get("assignee")
                sent = _wake(db, to, who, (
                    "Merge queue of %s: lot #%d waits behind lot #%d, and their conflict "
                    "is not prepared. It now blocks the queue: ameesh work resolve %d "
                    "--with %d." % (target, one["work_item"], other, who,
                                    other if who == one["work_item"] else one["work_item"])),
                    {"project": project.name, "target": target, "kind": "wait",
                     "lots": [other, one["work_item"]]})
                out["mail"].append({"lot": who, "to": to, "kind": "wait",
                                    "message_id": sent})
        out["targets"][target] = listed
        if isinstance(result, dict) and result.get("error"):
            out["error"] = result["error"]
    if keys != notified:
        _write_notified(cfg, project.name, keys)
    return out


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "queue", help="merge queue per target (L160e): a ready lot enters it, ameesh orders "
                      "it and replays it stacked")
    sub = parser.add_subparsers(dest="queue_command", required=True)
    add = sub.add_parser("add", help="put a frozen, reviewed lot into its target's queue")
    add.add_argument("id", type=int)
    add.add_argument("--repo", default=None,
                     help="the project repository (default: the current git repository)")
    rm = sub.add_parser("remove", help="take a lot out of the queue")
    rm.add_argument("id", type=int)
    rm.add_argument("-m", "--message", default="", help="why")
    show = sub.add_parser("show", help="the queues, in merge order")
    show.add_argument("--target", default=None)
    for p in (add, rm, show):
        p.add_argument("--json", action="store_true")
        p.add_argument("--actor", default="",
                       help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def show_lines(rows: list[dict]) -> list[str]:
    out = []
    target = None
    for r in rows:
        if r["target"] != target:
            target = r["target"]
            out.append("queue of %s:" % target)
        out.append("  %-3s %-8s #%d « %s » (%s, frozen %s)%s" % (
            r["rank"] if r.get("rank") else "—", r["state"], int(r["work_item_id"]),
            r.get("title") or "", r.get("assignee") or "unassigned", r["frozen_sha"][:12],
            " : %s" % r["reason"] if r.get("reason") else ""))
    return out


def run(cfg, db, args) -> int:
    import sys
    from . import resolutions
    try:
        if args.queue_command == "add":
            repo = resolutions.repo_root(args.repo)
            row = enter(db, args.id, repo=repo, actor=args.actor)
            print(json.dumps(row, ensure_ascii=False, indent=2, default=str) if args.json else
                  "lot #%d queued for %s at %s; the radar orders the queue at its next pass"
                  % (args.id, row["target"], row["frozen_sha"][:12]))
            return 0
        if args.queue_command == "remove":
            done = leave(db, args.id, actor=args.actor, reason=args.message)
            print(json.dumps({"lot": args.id, "removed": done}) if args.json else
                  "lot #%d %s" % (args.id, "taken out of the queue" if done
                                  else "was not queued"))
            return 0
        rows = storage.of(db).work.queue(args.target)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(show_lines(rows)) if rows else "no lot queued")
        return 0
    except (QueueError, GitError, plan_git.MergeProbeError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
