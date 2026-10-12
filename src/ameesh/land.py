# SPDX-License-Identifier: AGPL-3.0-only
"""Merge of the head of the queue by ameesh (lot L160e2, study L160 § 3.4).

Run by the executor at each radar pass, for a project that declares
`radar.projects.<p>.land` (`local` or `pr`; `off` by default: the queue
orders, the agent merges) and its `gates`, for the head of each queue:

1. lease per target (`merge_locks`): one merge at a time, which removes
   the catch-up merges; a dead executor frees it when the lease expires;
2. in the radar's private clone: the target, fresh from `origin`, and the
   frozen commit of the lot merged into it (`--no-ff`, line `ameesh-work:
   <lot>`), `rerere` primed by every resolution prepared with the lot; a
   conflict left ejects the lot and wakes its agent;
3. the project's gates on the merged tree (shell commands, in the clone's
   worktree); a failure: same;
4. `local`: the merge commit pushed to the target, never forced (the target
   moved meanwhile: the lot stays at the head, retried at the next pass);
   `pr`: the merge commit pushed to the lot branch (a fast-forward of the
   freeze), the queue row `landing` until the PR's CI on that commit is
   green, then the PR merged by the API (`gh pr merge --match-head-commit`);
5. a notice in the lot's thread (passive mail): resolutions replayed,
   gates; the lot is closed by the branch sweep (L118), which sees the
   merge.

    ameesh work land <lot> [--project P]   the same, by hand, for the head
                                           of its queue
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from . import merge_queue, plan_git, storage, work
from .canon import FETCH_TIMEOUT, GitError

#: the merge lease; renewed at each pass while a PR waits for its CI
LOCK_TTL_S = float(os.environ.get("AMEESH_LAND_LOCK_TTL", "1800"))
#: one gate at most
GATE_TIMEOUT_S = float(os.environ.get("AMEESH_LAND_GATE_TIMEOUT", "1800"))
#: lines of a failed gate's output kept in the ejection
GATE_TAIL = 20
TRAILER = "ameesh-work"
#: ref of the fresh target in the clone
FRESH_REF = "refs/ameesh/land/target"


class LandError(RuntimeError):
    """A lot that cannot be landed now: actionable message."""


def lock_key(project, target: str) -> str:
    return "%s:%s" % (project.name, target)


def _branch_of(target: str) -> str:
    """`origin/develop` → `develop`: the branch to push to."""
    return target.split("/", 1)[1] if target.startswith("origin/") else target


def _origin(project) -> str:
    return plan_git._out(project.repo, "remote", "get-url", "origin") or ""


def _passive(db, to, lot: int, text: str, data: dict) -> None:
    """The notice, in the lot's thread, without waking anyone (key `radar`)."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return
    try:
        mail.send(db, work.SYSTEM_SENDER, to, text, kind="event", work_item_id=str(lot),
                  payload={"radar": dict(data, kind="landed")})
    except Exception:  # noqa: BLE001 - a notice is never fatal
        pass


def _eject(db, row: dict, project, reason: str, todo: str) -> dict:
    lot = int(row["work_item_id"])
    return merge_queue._eject(db, row, reason, (
        "Merge queue of %s, lot #%d « %s »: ameesh could not merge it, %s. %s"
        % (row["target"], lot, row.get("title") or "", reason, todo)),
        {"project": project.name, "target": row["target"], "kind": "land"})


# --------------------------------------------------------------------------
# the merge in the clone
# --------------------------------------------------------------------------

def fresh_target(replayer, project, target: str) -> str:
    """The target's tip, fetched from `origin` when there is one, else the
    agents' repository."""
    from . import resolutions
    url = _origin(project)
    source = url or project.repo
    branch = _branch_of(target)
    proc = resolutions._run(replayer.path, "fetch", "--quiet", source,
                            "+refs/heads/%s:%s" % (branch, FRESH_REF), timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise GitError("git fetch %s %s: %s" % (source, branch, resolutions._err(proc)))
    return resolutions._check(resolutions._run(replayer.path, "rev-parse", FRESH_REF),
                              "rev-parse")


def merge(replayer, base: str, row: dict, rows: list[dict]) -> dict:
    """Merges the frozen commit of `row` into `base` in the clone, `rerere`
    primed by the resolutions `rows` of the lot. Leaves the merge committed
    at HEAD: `{state: clean|replayed|conflict, files, sha}`."""
    from . import resolutions
    path, run = replayer.path, resolutions._run
    lot = int(row["work_item_id"])
    commits = [r["commit_sha"] for r in rows
               if lot in (int(r["work_item_id"]), int(r["other_item_id"]))]
    missing = set(replayer.prime(commits)) if commits else set()
    resolutions._check(run(path, "checkout", "-q", "-f", "--detach", base), "checkout")
    replayer._empty_cache()
    trained = set(replayer.trained())
    for sha in commits:
        if sha in trained and sha not in missing:
            shutil.copytree(replayer._store(sha), replayer._rr_cache(), dirs_exist_ok=True)
    proc = run(path, "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=false",
               "merge", "--no-ff", "--no-commit", "--quiet", row["frozen_sha"])
    state, files = "clean", []
    try:
        if proc.returncode != 0:
            files = resolutions.unmerged(path)
            remaining = resolutions.rerere_remaining(path) if files else []
            left = sorted(set(remaining) | set(resolutions.with_markers(path, files)))
            if not files or left:
                run(path, "merge", "--abort")
                return {"state": "conflict", "files": left or files,
                        "error": "" if files else resolutions._err(proc)[:300]}
            resolutions._check(run(path, "add", "--", *files), "add")
            state = "replayed"
        message = "Merge #%d « %s » (%s)\n\n%s: %d\n" % (
            lot, row.get("title") or "", row.get("branch") or row["frozen_sha"][:12],
            TRAILER, lot)
        resolutions._check(run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                               "--no-verify", "-F", "-", stdin=message.encode()), "commit")
    finally:
        replayer._empty_cache()
    sha = resolutions._check(run(path, "rev-parse", "HEAD"), "rev-parse")
    return {"state": state, "files": files, "sha": sha}


def gates(project, path: str, *, timeout: float = GATE_TIMEOUT_S) -> dict:
    """Runs the project's gates in `path`: `{ok, gate, output}` (the
    failed gate and the tail of its output)."""
    for gate in project.gates:
        try:
            proc = subprocess.run(gate, shell=True, cwd=path, capture_output=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "gate": gate, "output": "timeout after %.0f s" % timeout}
        if proc.returncode != 0:
            out = (proc.stdout + proc.stderr).decode("utf-8", "replace").splitlines()
            return {"ok": False, "gate": gate,
                    "output": "\n".join(out[-GATE_TAIL:]) or "exit %d" % proc.returncode}
    return {"ok": True, "gate": None, "output": ""}


# --------------------------------------------------------------------------
# GitHub (land: pr)
# --------------------------------------------------------------------------

def _gh(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["gh", *args], 1, "", str(exc))


def pr_status(slug: str, branch: str, gh=_gh) -> dict | None:
    """The open PR of `branch`: `{number, head, base, ci: green|red|pending}`."""
    from . import radar
    proc = gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json",
               "number,headRefOid,baseRefName,statusCheckRollup"])
    if proc.returncode != 0:
        raise LandError("gh pr list: %s" % (proc.stderr or proc.stdout).strip()[:300])
    rows = json.loads(proc.stdout or "[]")
    if not rows:
        return None
    rollup = rows[0].get("statusCheckRollup") or []
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    return {"number": rows[0]["number"], "head": rows[0].get("headRefOid"),
            "base": rows[0].get("baseRefName"), "ci": ci}


def pr_merge(slug: str, number, sha: str, method: str, gh=_gh) -> str | None:
    """Merges the PR by the API at that very commit; the error, or None."""
    proc = gh(["pr", "merge", str(number), "--repo", slug, "--%s" % method,
               "--match-head-commit", sha], timeout=120.0)
    if proc.returncode == 0:
        return None
    # #194 r3: a failure without output is still a failure
    return (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode


# --------------------------------------------------------------------------
# one landing
# --------------------------------------------------------------------------

def land_head(cfg, db, project, row: dict, *, owner: str, replayer=None, locks=None,
              gh=_gh) -> dict:
    """Lands (or follows the landing of) `row`, the head of its queue.
    Returns `{lot, result: landed|landing|retry|ejected|busy|error, …}`."""
    from . import radar, resolutions
    locks = locks if locks is not None else {}
    lot = int(row["work_item_id"])
    key = lock_key(project, row["target"])
    ops = storage.of(db).work
    epoch = ops.land_lock(key, owner, LOCK_TTL_S, epoch=locks.get(key)) \
        if key in locks else None
    if epoch is None:
        epoch = ops.land_lock(key, owner, LOCK_TTL_S)
    if epoch is None:
        locks.pop(key, None)
        return {"lot": lot, "result": "busy"}
    locks[key] = epoch

    def unlock() -> None:
        ops.land_unlock(key, owner, epoch)
        locks.pop(key, None)

    def held() -> bool:
        """#194 r3: the lease is still ours, renewed, right before a push or
        a merge (gates may outlive it, and another runner take it)."""
        if ops.land_lock(key, owner, LOCK_TTL_S, epoch=epoch) is not None:
            return True
        locks.pop(key, None)
        return False

    lost = {"lot": lot, "result": "busy", "error": "lease on %s lost before pushing" % key}

    if replayer is None:
        replayer = resolutions.Replayer(project.repo, radar.clone_dir(cfg, project.name))
    slug = radar.github_slug(project) if project.land == "pr" else None
    try:
        if row["state"] == "landing" and project.land == "pr":
            return _follow_pr(db, project, row, slug, unlock, held, gh=gh)
        # land: local — a lot left `landing` (lease lost or runner gone) is
        # landed again from the start: the push to the target is never forced
        replayer.ensure()
        ops.queue_update(lot, state="landing", note="merge queue of %s: landing (%s)"
                         % (row["target"], project.land), actor=work.SYSTEM_SENDER)
        base = fresh_target(replayer, project, row["target"])
        rows = ops.resolutions([lot])
        merged = merge(replayer, base, row, rows)
        if merged["state"] == "conflict":
            unlock()
            out = _eject(db, row, project, "conflict on %s with %s at %s" % (
                ", ".join(merged["files"][:6]) or "?", row["target"], base[:12]),
                "Integrate %s into your branch (or prepare the resolution), freeze again, "
                "then ameesh work queue add %d." % (row["target"], lot))
            return dict(out, result="ejected")
        checked = gates(project, replayer.path)
        if not checked["ok"]:
            unlock()
            out = _eject(db, row, project, "gate `%s` failed on the merged tree"
                         % checked["gate"],
                         "Its output:\n%s\nFix it on your branch, freeze again, then "
                         "ameesh work queue add %d." % (checked["output"], lot))
            return dict(out, result="ejected", output=checked["output"])
        url = _origin(project) or project.repo
        if project.land == "local":
            if not held():
                return lost
            proc = resolutions._run(replayer.path, "push", "--quiet", "--no-verify", url,
                                    "%s:refs/heads/%s" % (merged["sha"],
                                                          _branch_of(row["target"])),
                                    timeout=FETCH_TIMEOUT)
            unlock()
            if proc.returncode != 0:
                ops.queue_update(lot, state="queued", reason="target moved during the merge: "
                                 "retried at the next pass", note="merge queue of %s: push "
                                 "refused (%s), retried" % (row["target"],
                                                            resolutions._err(proc)[-200:]),
                                 actor=work.SYSTEM_SENDER)
                return {"lot": lot, "result": "retry", "error": resolutions._err(proc)}
            return _landed(db, project, row, merged, base)
        # land: pr — the merge commit goes to the lot branch, its CI decides
        status = pr_status(slug, row["branch"], gh=gh) if slug else None
        if status is None:
            unlock()
            out = _eject(db, row, project, "no open PR for %s" % row["branch"],
                         "Open the PR, then ameesh work queue add %d." % lot)
            return dict(out, result="ejected")
        if status.get("base") != _branch_of(row["target"]):
            unlock()
            out = _eject(db, row, project, "PR #%s targets %s, not %s" % (
                status["number"], status.get("base") or "?", _branch_of(row["target"])),
                "Point the PR at %s (or queue the lot for its target), then ameesh work "
                "queue add %d." % (_branch_of(row["target"]), lot))
            return dict(out, result="ejected")
        if not merge_queue._same(status["head"], row["frozen_sha"]):
            unlock()
            out = _eject(db, row, project, "PR #%s is at %s, not at the freeze %s" % (
                status["number"], (status["head"] or "?")[:12], row["frozen_sha"][:12]),
                "Something pushed to the branch after its freeze: freeze again, then "
                "ameesh work queue add %d." % lot)
            return dict(out, result="ejected")
        if not held():
            return lost
        proc = resolutions._run(replayer.path, "push", "--quiet", "--no-verify", url,
                                "%s:refs/heads/%s" % (merged["sha"], row["branch"]),
                                timeout=FETCH_TIMEOUT)
        if proc.returncode != 0:
            unlock()
            out = _eject(db, row, project, "push to %s refused (%s)" % (
                row["branch"], resolutions._err(proc)[-200:]),
                "Something pushed to the branch after its freeze: freeze again, then "
                "ameesh work queue add %d." % lot)
            return dict(out, result="ejected")
        ops.queue_update(lot, state="landing", landing_sha=merged["sha"],
                         reason="PR #%s: CI on the merge %s" % (status["number"],
                                                                merged["sha"][:12]),
                         note="merge queue of %s: %s merged into the branch (%s), waiting "
                         "for the CI of PR #%s" % (row["target"], row["target"],
                                                   merged["sha"][:12], status["number"]),
                         actor=work.SYSTEM_SENDER)
        return {"lot": lot, "result": "landing", "sha": merged["sha"],
                "replayed": merged["files"] if merged["state"] == "replayed" else []}
    except (GitError, LandError, OSError, ValueError) as exc:
        error = {"lot": lot, "result": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
        if row["state"] == "landing":
            # its branch holds the merge commit: it stays `landing`, lease kept,
            # and the PR is read again at the next pass (an error never merges)
            ops.queue_update(lot, reason="reading PR failed (%s): retried" % str(exc)[:200])
            return error
        unlock()
        ops.queue_update(lot, state="queued", reason="landing failed (%s): retried"
                         % str(exc)[:200])
        return error


def _follow_pr(db, project, row: dict, slug, unlock, held, *, gh) -> dict:
    lot = int(row["work_item_id"])
    ops = storage.of(db).work
    status = pr_status(slug, row["branch"], gh=gh) if slug else None
    if status is None:
        unlock()
        if row.get("item_state") in work.MERGED_STATES:
            ops.queue_update(lot, state="landed", reason="merged",
                             note="merge queue of %s: landed" % row["target"],
                             actor=work.SYSTEM_SENDER)
            return {"lot": lot, "result": "landed"}
        return dict(_eject(db, row, project, "its PR is no longer open",
                           "Reopen it, then ameesh work queue add %d." % lot), result="ejected")
    if status.get("base") != _branch_of(row["target"]):
        unlock()
        return dict(_eject(db, row, project, "PR #%s targets %s, not %s" % (
            status["number"], status.get("base") or "?", _branch_of(row["target"])),
            "Point the PR at %s, freeze again, then ameesh work queue add %d."
            % (_branch_of(row["target"]), lot)), result="ejected")
    if not merge_queue._same(status["head"], row.get("landing_sha")):
        unlock()
        return dict(_eject(db, row, project, "its branch moved while ameesh was landing it "
                           "(%s)" % (status["head"] or "?")[:12],
                           "Freeze again, then ameesh work queue add %d." % lot),
                    result="ejected")
    if status["ci"] == "pending":
        return {"lot": lot, "result": "landing", "pr": status["number"]}
    if status["ci"] == "red":
        unlock()
        return dict(_eject(db, row, project, "the CI of PR #%s is red on the merge %s"
                           % (status["number"], row["landing_sha"][:12]),
                           "Fix it on your branch (it already holds the merge of %s), freeze "
                           "again, then ameesh work queue add %d." % (row["target"], lot)),
                    result="ejected")
    if not held():
        return {"lot": lot, "result": "busy",
                "error": "lease lost before merging PR #%s" % status["number"]}
    error = pr_merge(slug, status["number"], row["landing_sha"], project.merge_method, gh=gh)
    if error is not None:
        # #194 r3: the branch holds the merge commit; the lot stays `landing`
        # with it (and the lease: a lasting refusal raises `merge_blocked`)
        ops.queue_update(lot, reason="PR merge refused (%s): retried" % error[:200])
        return {"lot": lot, "result": "retry", "error": error}
    unlock()
    return _landed(db, project, row, {"sha": row["landing_sha"], "state": "pr",
                                      "files": []}, None, pr=status["number"])


def _landed(db, project, row: dict, merged: dict, base, *, pr=None) -> dict:
    lot = int(row["work_item_id"])
    storage.of(db).work.queue_update(
        lot, state="landed", reason="merged by ameesh (%s)" % merged["sha"][:12],
        note="merge queue of %s: landed by ameesh, %s%s" % (
            row["target"], merged["sha"][:12], ", PR #%s" % pr if pr else ""),
        actor=work.SYSTEM_SENDER)
    gates_text = ", ".join("`%s`" % g for g in project.gates)
    _passive(db, row.get("assignee"), lot, (
        "Merge queue of %s, lot #%d « %s »: merged by ameesh (%s%s)%s. Gates passed on the "
        "merged tree: %s. The branch sweep closes the lot."
        % (row["target"], lot, row.get("title") or "", merged["sha"][:12],
           ", PR #%s" % pr if pr else ", on %s" % (base or "?")[:12],
           "; resolutions replayed by rerere on %s" % ", ".join(merged["files"][:6])
           if merged.get("state") == "replayed" else "", gates_text)),
        {"project": project.name, "target": row["target"], "commit": merged["sha"]})
    return {"lot": lot, "result": "landed", "sha": merged["sha"], "pr": pr}


def run_pass(cfg, db, project, report: dict, *, owner: str, locks=None, gh=_gh) -> list[dict]:
    """After the queue pass (`report["queue"]`): lands the head of each
    queue of `project`, when it declares `land`. Never raises."""
    if project.land == "off":
        return []
    out = []
    try:
        rows = {int(r["work_item_id"]): r for r in storage.of(db).work.queue()}
    except Exception as exc:  # noqa: BLE001 - database without 0058
        return [{"result": "error", "error": "%s: %s" % (type(exc).__name__, exc)}]
    for target, listed in sorted(((report.get("queue") or {}).get("targets") or {}).items()):
        head = next((rows[o["lot"]] for o in listed if o["lot"] in rows
                     and rows[o["lot"]]["state"] in ("queued", "landing")), None)
        if head is None:
            continue
        try:
            out.append(land_head(cfg, db, project, head, owner=owner, locks=locks, gh=gh))
        except Exception as exc:  # noqa: BLE001 - never fatal for the pass
            out.append({"lot": int(head["work_item_id"]), "result": "error",
                        "error": "%s: %s" % (type(exc).__name__, exc)})
    return out


# --------------------------------------------------------------------------
# alert merge_blocked
# --------------------------------------------------------------------------

BLOCKED_AFTER_S = float(os.environ.get("AMEESH_MERGE_BLOCKED_MIN", "30")) * 60


def blocked(db, now: float | None = None) -> list[dict]:
    """Heads of queue (rank 1, or landing) that have not moved for
    `BLOCKED_AFTER_S`: the queue is stuck behind them."""
    now = time.time() if now is None else now
    try:
        rows = storage.of(db).work.queue()
    except Exception:  # noqa: BLE001 - database without 0058
        return []
    return [r for r in rows if r["state"] in ("queued", "landing")
            and (r["state"] == "landing" or r.get("rank") == 1)
            and now - float(r.get("updated_ts") or now) >= BLOCKED_AFTER_S]


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "land", help="merge the head of its queue now (L160e2; the executor does it at each "
                     "radar pass for a project that declares `land`)")
    parser.add_argument("id", type=int)
    parser.add_argument("--project", default=None,
                        help="the radar project (default: the only one declared)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="")
    parser.set_defaults(func=func)


def run(cfg, db, args) -> int:
    import socket
    import sys
    from . import radar
    try:
        projects = radar.settings_of(cfg).projects
        if args.project:
            project = projects.get(args.project)
        else:
            project = next(iter(projects.values())) if len(projects) == 1 else None
        if project is None:
            raise LandError("name the project (--project): declared here: %s"
                            % (", ".join(sorted(projects)) or "none (radar.projects)"))
        if project.land == "off":
            raise LandError("project %s does not declare `land` (local | pr)" % project.name)
        rows = [r for r in storage.of(db).work.queue()
                if int(r["work_item_id"]) == args.id and r["state"] in ("queued", "landing")]
        if not rows:
            raise LandError("lot #%d is not in a queue" % args.id)
        row = rows[0]
        if row["state"] == "queued" and row.get("rank") != 1:
            raise LandError("lot #%d is ranked %s in the queue of %s, not 1"
                            % (args.id, row.get("rank") or "—", row["target"]))
        out = land_head(cfg, db, project, row, owner="%s:%d" % (socket.gethostname(),
                                                                 os.getpid()))
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else
              "lot #%d: %s%s" % (args.id, out["result"],
                                 " (%s)" % out.get("error") if out.get("error") else ""))
        return 0 if out["result"] in ("landed", "landing") else 1
    except (LandError, merge_queue.QueueError, GitError, radar.SettingsError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
