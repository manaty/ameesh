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
import re
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
    return "%s:%s" % (project.name, merge_queue._remote_branch(target))


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


def _fire_guard(landing) -> dict | None:
    """#1374, P1-2 : le feu et les blocages sont revalidés au dernier geste.

    Le retrait ou l'invalidation d'un feu retire la ligne de file au passage
    suivant ; entre les deux, `land` pourrait encore pousser ou fusionner. Sous
    son bail, juste avant le geste, il redemande l'autorisation : un lot qui a
    porté un feu ne part que sous un feu actif couvrant le gel de sa file et
    sans verdict bloqué non levé. Rend l'éjection, ou None si la voie est
    libre."""
    from . import fire
    ok, why = fire.land_guard(landing.db, landing.lot, landing.row.get("frozen_sha") or "",
                              repo=landing.project.repo)
    if ok:
        return None
    return landing.eject(why, "The fire no longer authorizes this landing: ask the pilot for a "
                              "fire on the current freeze, or take the lot out of the queue "
                              "(`ameesh work queue remove %d`)." % landing.lot)


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


def _shell(command: str, path: str, timeout: float) -> str | None:
    """Runs `command` in `path`: None if it passes, else the tail of its output."""
    try:
        proc = subprocess.run(command, shell=True, cwd=path, capture_output=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return "timeout after %.0f s" % timeout
    if proc.returncode == 0:
        return None
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace").splitlines()
    return "\n".join(out[-GATE_TAIL:]) or "exit %d" % proc.returncode


def _regenerate(path: str, generated, touched, *, timeout: float = GATE_TIMEOUT_S) -> dict:
    """L160g (#473): regenerates, on the merged tree in `path`, each
    generated file in `touched`, stages
    it, then runs its check: `{ok, files, file, output}`."""
    from . import resolutions
    done = []
    for item in generated:
        if item.path not in touched:
            continue
        failed = _shell(item.regen, path, timeout)
        if failed is None:
            staged = resolutions._run(path, "add", "-A", "--", item.path)
            if staged.returncode != 0:
                failed = "cannot stage generated output: %s" % resolutions._err(staged)
            else:
                failed = _shell(item.check, path, timeout) if item.check else None
                if failed is None and resolutions._run(
                        path, "diff", "--exit-code", "--", item.path).returncode != 0:
                    failed = "generated check changed the staged output"
        if failed is not None:
            return {"ok": False, "files": done, "file": item.path, "output": failed}
        done.append(item.path)
    return {"ok": True, "files": done, "file": None, "output": ""}


def merge(replayer, base: str, row: dict, rows: list[dict], *, generated=()) -> dict:
    """Merges the frozen commit of `row` into `base` in the clone, `rerere`
    primed by the resolutions `rows` of the lot. Leaves the merge committed
    at HEAD: `{state: clean|replayed|conflict|generated, files, sha,
    regenerated}`. `generated` (L160g, `radar.Generated`): those files are
    never taken from a merge nor from rerere — regenerated on the merged
    tree for every merge, including input-only changes, then checked (`generated`:
    the regeneration or its check failed, `output`)."""
    from . import resolutions
    path, run = replayer.path, resolutions._run
    lot = int(row["work_item_id"])
    paths = {g.path for g in generated}
    commits = [r["commit_sha"] for r in rows
               if lot in (int(r["work_item_id"]), int(r["other_item_id"]))]
    missing = set(replayer.prime(commits)) if commits else set()
    resolutions._check(run(path, "checkout", "-q", "-f", "--detach", base), "checkout")
    replayer._empty_cache()
    trained = set(replayer.trained())
    for sha in commits:
        if sha in trained and sha not in missing:
            shutil.copytree(replayer._store(sha), replayer._rr_cache(), dirs_exist_ok=True)
    proc = run(path, "-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false",
               "merge", "--no-ff", "--no-commit", "--quiet", row["frozen_sha"])
    state, files, regenerated = "clean", [], []
    try:
        if proc.returncode != 0:
            files = resolutions.unmerged(path)
            resolutions.exclude_generated(path, files, paths)
            resolutions._check(run(path, "-c", "rerere.enabled=true", "-c",
                                   "rerere.autoUpdate=false", "rerere"), "rerere")
            others = [f for f in files if f not in paths]
            remaining = [f for f in resolutions.rerere_remaining(path) if f not in paths] \
                if others else []
            left = sorted(set(remaining) | set(resolutions.with_markers(path, others)))
            if not files or left:
                run(path, "merge", "--abort")
                return {"state": "conflict", "files": left or files,
                        "error": "" if files else resolutions._err(proc)[:300]}
            if others:
                resolutions._check(run(path, "add", "--", *others), "add")
                state = "replayed"
        touched = paths
        if touched:
            made = _regenerate(path, generated, touched)
            if not made["ok"]:
                run(path, "merge", "--abort")
                return {"state": "generated", "files": [made["file"]],
                        "output": made["output"]}
            regenerated = made["files"]
        message = "Merge #%d « %s » (%s)\n\n%s: %d\n" % (
            lot, row.get("title") or "", row.get("branch") or row["frozen_sha"][:12],
            TRAILER, lot)
        resolutions._check(run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                               "--no-verify", "-F", "-", stdin=message.encode()), "commit")
    finally:
        replayer._empty_cache()
    sha = resolutions._check(run(path, "rev-parse", "HEAD"), "rev-parse")
    return {"state": state, "files": [f for f in files if f not in paths], "sha": sha,
            "regenerated": regenerated}


def changed(path: str, base: str, sha: str) -> list | None:
    """L160g (#473): the files the merge `sha` changes on `base`; None if
    git cannot tell (then every path gate runs)."""
    from . import resolutions
    proc = resolutions._run(path, "diff", "--name-only", "--no-renames", base, sha)
    if proc.returncode != 0:
        return None
    return [f for f in resolutions._text(proc).splitlines() if f.strip()]


def gates(project, path: str, *, files=None, timeout: float = GATE_TIMEOUT_S) -> dict:
    """Runs the project's gates in `path`: `{ok, gate, output}` (the
    failed gate and the tail of its output); with its path gates (L160g)
    for the `files` the merge touches (`radar.gates_for`)."""
    from . import radar
    for gate in radar.gates_for(project, files):
        failed = _shell(gate, path, timeout)
        if failed is not None:
            return {"ok": False, "gate": gate, "output": failed}
    return {"ok": True, "gate": None, "output": ""}


# --------------------------------------------------------------------------
# GitHub (land: pr)
# --------------------------------------------------------------------------

#: #194 r8: the exit status of a `gh` call that timed out — its effect unknown
GH_TIMEOUT = 124


def _gh(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(["gh", *args], GH_TIMEOUT, "", str(exc))
    except OSError as exc:
        return subprocess.CompletedProcess(["gh", *args], 1, "", str(exc))


def _json(proc, what: str, kind):
    """#194 r8: the output of `gh … --json`, of the type expected, or LandError."""
    try:
        data = json.loads(proc.stdout or ("[]" if kind is list else "{}"))
    except ValueError as exc:
        raise LandError("%s: unreadable output (%s)" % (what, exc)) from exc
    if not isinstance(data, kind) or (kind is list and not all(
            isinstance(r, dict) for r in data)):
        raise LandError("%s: unexpected output %s" % (what, (proc.stdout or "")[:100]))
    return data


def pr_status(slug: str, branch: str, gh=_gh) -> dict | None:
    """The open PR of `branch`: `{number, head, base, ci: green|red|pending,
    auto}`. `auto` (#1656) says GitHub auto-merge is armed: its
    `--match-head-commit` is checked when it is armed, never again, so a head
    rewritten under it would be merged without a new proof."""
    from . import radar
    proc = gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json",
               "number,headRefOid,baseRefName,statusCheckRollup,autoMergeRequest"])
    if proc.returncode != 0:
        raise LandError("gh pr list: %s" % (proc.stderr or proc.stdout).strip()[:300])
    rows = _json(proc, "gh pr list", list)
    if not rows:
        return None
    rollup = rows[0].get("statusCheckRollup") or []
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    return {"number": rows[0]["number"], "head": rows[0].get("headRefOid"),
            "base": rows[0].get("baseRefName"), "ci": ci,
            "auto": bool(rows[0].get("autoMergeRequest"))}


def pr_disable_auto(slug: str, number, gh=_gh) -> str | None:
    """#1656: disarms GitHub auto-merge on PR `number`; the error, or None.

    A `gh pr merge --auto --match-head-commit` only checks the head when it
    is armed: GitHub keeps no link to that commit. A head rewritten after
    (even under `[skip ci]`) is then merged with no new qualification. Any
    PR the queue manages is merged by ameesh itself, on the exact frozen
    head; its auto-merge, whoever armed it, is disarmed first."""
    proc = gh(["pr", "merge", str(number), "--repo", slug, "--disable-auto"])
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode


def pr_merge(slug: str, number, sha: str, method: str, gh=_gh) -> str | None:
    """Merges the PR by the API at that very commit; the error, or None. A
    timeout starts with `TIMED_OUT`: GitHub may have merged it all the same."""
    proc = gh(["pr", "merge", str(number), "--repo", slug, "--%s" % method,
               "--match-head-commit", sha], timeout=120.0)
    if proc.returncode == 0:
        return None
    # #194 r3: a failure without output is still a failure
    error = (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode
    return TIMED_OUT + error if proc.returncode == GH_TIMEOUT else error


TIMED_OUT = "timed out: "


def pr_merged(slug: str, branch: str, sha: str, gh=_gh) -> dict | None:
    """#194 r8: the merged PR of `branch` whose head is `sha` (`{number,
    base}`), None if there is none — a merge that completed after its call
    timed out, or between two passes. Raises when GitHub cannot be read."""
    proc = gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "merged",
               "--json", "number,headRefOid,baseRefName"])
    if proc.returncode != 0:
        raise LandError("gh pr list --state merged: %s"
                        % (proc.stderr or proc.stdout).strip()[:300])
    for row in _json(proc, "gh pr list --state merged", list):
        if merge_queue._same(row.get("headRefOid"), sha):
            return {"number": row.get("number"), "base": row.get("baseRefName")}
    return None


def _merged_after_disarm(slug: str, branch: str, sha: str, gh) -> tuple[dict | None, str]:
    """#1718: `pr_merged` read after a failed disarm, and what to add to the
    ejection when it cannot be read. Its LandError must not reach the
    retry path of `land`: the entry would go back to `queued` (or stay
    `landing`) silently, auto-merge still armed — the ejection that asks
    to disarm by hand is kept, fail closed."""
    try:
        return pr_merged(slug, branch, sha, gh=gh), ""
    except LandError as exc:
        return None, "; merged PRs unreadable (%s)" % str(exc)[:200]


# --------------------------------------------------------------------------
# one landing
# --------------------------------------------------------------------------

#: #194 r5: the lease of a target, on the remote itself (local mode): moved by
#: each new holder before anything else, checked by every landing push
LEASE_REF = "refs/ameesh/land/"
_LEASE_ENV = {"GIT_AUTHOR_NAME": "ameesh", "GIT_AUTHOR_EMAIL": "ameesh@localhost",
              "GIT_COMMITTER_NAME": "ameesh", "GIT_COMMITTER_EMAIL": "ameesh@localhost"}
_LEASE_RE = re.compile(r"^ameesh land lease (\S+) epoch ([0-9]+) \((.*?)\)")
#: where the clone keeps the remote lease ref it last read
_SEEN_LEASE = "refs/ameesh/seen-lease"


class _LeaseRefused(LandError):
    """#194 r6: the remote lease ref could not be taken: nothing is written."""


def lease_commit(path: str, key: str, epoch: int, owner: str, *, parent: str | None = None,
                 landed: int | None = None) -> str:
    """A commit (empty tree) that stands for the lease `key` at `epoch`, held
    by `owner`; child of `parent` (the ref's value it replaces); `landed`:
    the commit that a landing push writes over the holder's own."""
    env = dict(os.environ, **_LEASE_ENV)
    tree = subprocess.run(["git", "-C", path, "mktree"], input=b"", capture_output=True,
                          env=env, check=False)
    if tree.returncode != 0:
        raise GitError("git mktree: %s" % tree.stderr.decode("utf-8", "replace")[:200])
    message = "ameesh land lease %s epoch %d (%s)%s" % (
        key, epoch, owner, ": landed lot %d" % landed if landed is not None else "")
    proc = subprocess.run(["git", "-C", path, "commit-tree", tree.stdout.decode().strip(),
                           *(["-p", parent] if parent else []), "-m", message],
                          capture_output=True, env=env, check=False)
    if proc.returncode != 0:
        raise GitError("git commit-tree: %s" % proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout.decode().strip()


def remote_lease(path: str, url: str, ref: str) -> tuple:
    """The remote lease ref, fetched into the clone: `(sha or None, epoch or
    None, owner or None)`. Raises when the remote cannot be read."""
    from . import resolutions
    proc = resolutions._run(path, "ls-remote", url, ref, timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise _LeaseRefused("git ls-remote %s: %s" % (ref, resolutions._err(proc)[-200:]))
    found = resolutions._text(proc).split()
    if not found:
        return None, None, None
    sha = found[0]
    proc = resolutions._run(path, "fetch", "--quiet", url, "+%s:%s" % (ref, _SEEN_LEASE),
                            timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise _LeaseRefused("git fetch %s: %s" % (ref, resolutions._err(proc)[-200:]))
    text = resolutions._text(resolutions._run(path, "log", "-1", "--format=%s", sha))
    match = _LEASE_RE.match(text)
    return (sha, int(match.group(2)), match.group(3)) if match else (sha, None, None)


def take_remote_lease(path: str, url: str, ref: str, key: str, epoch: int,
                      owner: str) -> str:
    """#194 r6: the remote lease ref IS the lock. Taken by a compare-and-swap
    from the value read to this holder's epoch commit (never `--force`: a late
    stale publisher cannot roll the epoch back). Kept as is when it already
    stands for this holder at this epoch. Returns the value this holder now
    expects; `_LeaseRefused` if a newer epoch holds it or the push is refused."""
    from . import resolutions
    sha, held_epoch, held_by = remote_lease(path, url, ref)
    if held_epoch == epoch and held_by == owner:
        return sha
    if held_epoch is not None and held_epoch > epoch:
        raise _LeaseRefused("the remote lease %s is at epoch %d (%s), newer than ours (%d)"
                            % (ref, held_epoch, held_by, epoch))
    mine = lease_commit(path, key, epoch, owner, parent=sha)
    proc = resolutions._run(path, "push", "--quiet", "--no-verify",
                            "--force-with-lease=%s:%s" % (ref, sha or ""), url,
                            "%s:%s" % (mine, ref), timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise _LeaseRefused("lease ref %s not taken (%s)" % (ref, resolutions._err(proc)[-200:]))
    return mine


class _Landing:
    """One landing under the lease `(key, owner, epoch)` (#194 r5): every
    queue write is a compare-and-swap on the lease, in the same statement — a
    stale holder's writes fail, and it reports `busy`; the lease is released
    once, at the end (`land_head`)."""

    def __init__(self, db, project, row, key, owner, epoch, locks, projects=(),
                 cfg=None, clone=None, replayer=None, parts=None):
        self.db, self.project, self.row = db, project, row
        self.lot = int(row["work_item_id"])
        self.ops = storage.of(db).work
        self.key, self.owner, self.epoch, self.locks = key, owner, epoch, locks
        self.release = False
        self.projects = projects
        self.cfg, self.clone = cfg, clone
        #: L1651 : le clone radar (pour relire la source de sélection versionnée
        #: de la cible) et les parts dues déjà requalifiées par l'appelant
        #: (§12 : le manifeste de paquet remplace la CI individuelle)
        self.replayer, self.parts = replayer, parts
        self.entry = tuple(row.get(k) for k in
                           ("generation", "target", "frozen_id", "frozen_sha"))

    @property
    def lease(self):
        return (self.key, self.owner, self.epoch)

    def busy(self, why: str) -> dict:
        self.locks.pop(self.key, None)
        self.release = False
        return {"lot": self.lot, "result": "busy", "error": why}

    def held(self) -> bool:
        """The lease is still ours, renewed, right before a push or a merge."""
        if self.ops.land_lock(self.key, self.owner, LOCK_TTL_S, epoch=self.epoch) is not None:
            return True
        self.locks.pop(self.key, None)
        return False

    def write(self, **kw) -> bool:
        return self.ops.queue_update(self.lot, lease=self.lease, entry=self.entry, **kw) is not None

    def eject(self, reason: str, todo: str, **extra) -> dict:
        self.release = True
        row = self.row
        if not self.write(state="ejected", reason=reason,
                          note="merge queue of %s: ejected, %s" % (row["target"], reason),
                          actor=work.SYSTEM_SENDER):
            return self.busy("lease on %s lost: ejection not written" % self.key)
        sent = merge_queue._wake(self.db, row.get("assignee"), self.lot, (
            "Merge queue of %s, lot #%d « %s »: ameesh could not merge it, %s. %s"
            % (row["target"], self.lot, row.get("title") or "", reason, todo)),
            {"project": self.project.name, "target": row["target"], "kind": "land",
             "lot": self.lot, "reason": reason})
        return dict({"lot": self.lot, "to": row.get("assignee"), "kind": "ejected",
                     "message_id": sent, "result": "ejected"}, **extra)

    def record(self, reason: str, note: str, *, state: str = "landed") -> str:
        # Once the remote merge may have happened, persistence errors must
        # never send execution back through the pre-merge retry/reset path.
        for _ in range(2):
            try:
                return self._record(reason, note, state=state)
            except Exception as exc:  # noqa: BLE001 - post-merge fact, retry once
                error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        merge_queue._wake(self.db, self.row.get("assignee"), self.lot,
                          "Merge outcome could not be recorded after two attempts: %s. %s. "
                          "Check the remote merge and queue by hand." % (reason, error),
                          {"project": self.project.name, "target": self.row["target"],
                           "kind": "land", "lot": self.lot, "reason": UNCERTAIN + error})
        return "unrecorded"

    def _record(self, reason: str, note: str, *, state: str = "landed") -> str:
        """#194 r8: a merge that happened is a fact, recorded whatever became
        of the lease: under the lease; else under the freeze (the lot still
        `landing` on the same freeze and landing commit); else — a newer
        freeze or entry is there, never overwritten — as history only, a
        line in the lot's journal. Returns `lease`, `freeze` or `history`."""
        row = self.row
        freeze = (row["frozen_sha"], row.get("landing_sha"))
        if self.write(state=state, reason=reason, note=note, actor=work.SYSTEM_SENDER, freeze=freeze):
            return "lease"
        if self.ops.queue_update(self.lot, state=state, reason=reason, note=note,
                                 actor=work.SYSTEM_SENDER,
                                 freeze=freeze, entry=self.entry) is not None:
            return "freeze"
        self.ops.note(self.lot, row.get("item_state") or "", "%s (history: %s)" % (reason, note),
                      work.SYSTEM_SENDER)
        return "history"

    def required_ci(self):
        if getattr(self.project, "ci_required", False):
            try:
                parts = self.parts
                if parts is None and getattr(self.project, "ci_parts", None):
                    # L1651 (#1650) : la preuve de complétude est refaite ici,
                    # sur la cible de cette entrée de file, avec la source de
                    # sélection versionnée relue maintenant (le contrat peut
                    # avoir bougé depuis l'admission).
                    parts = merge_queue._parts_reader(
                        self.project, self.project.repo, self.row.get("target") or "",
                        self.row["frozen_sha"], self.replayer, cfg=self.cfg)
                merge_queue._required_ci(self.project, self.row["frozen_sha"], parts=parts)
            except merge_queue.QueueError as exc:
                return self.eject(str(exc), "Restore green CI on the frozen commit, then "
                                  "ameesh work queue add %d." % self.lot)
        return None

    def review_after_merge(self, *, merged_sha: str = "", pr=None) -> dict:
        """L539 : ouvre le lot de relecture d'un lot **léger** (canon §2), une
        seule fois, après que la fusion est un fait enregistré. La classe est
        relue sur le diff gelé (`merge_queue._class_of`, canon §8) ; un doute
        ou une classe supérieure n'ouvre rien. Ne lève jamais."""
        from . import merge_queue, review_lot
        try:
            item = work.get(self.db, self.lot)
            klass = merge_queue._class_of(
                self.db, self.lot, item, self.project.repo, self.clone,
                self.row.get("target"), self.row.get("frozen_sha") or "", cfg=self.cfg)
            return review_lot.open_after_merge(
                self.db, lot=self.lot, klass=klass, item=item, merged_sha=merged_sha,
                pr=pr, target=self.row.get("target") or "")
        except Exception as exc:  # noqa: BLE001 - la fusion est un fait, jamais défait
            return {"lot": self.lot, "id": None, "created": False, "class": "",
                    "why": "lot de relecture non ouvert : %s: %s"
                           % (type(exc).__name__, str(exc)[:200])}

    def landed(self, merged: dict, base, *, pr=None) -> dict:
        self.release = True
        row, project = self.row, self.project
        # #1682: a merge GitHub's auto-merge made at the frozen head, before
        # the queue replayed anything, is constated: the queue's gates never
        # ran on the merged tree, and the record must not say they did
        constated = merged.get("state") == "constated"
        who = "merged by GitHub auto-merge, constated by ameesh" if constated else "merged by ameesh"
        reason = "%s (%s)" % (who, merged["sha"][:12])
        gates_text = ", ".join("`%s`" % g for g in project.gates)
        _passive(self.db, row.get("assignee"), self.lot, (
            "Merge queue of %s, lot #%d « %s »: %s (%s%s)%s. %s The branch sweep closes the lot."
            % (row["target"], self.lot, row.get("title") or "", who, merged["sha"][:12],
               ", PR #%s" % pr if pr else ", on %s" % (base or "?")[:12],
               "; resolutions replayed by rerere on %s" % ", ".join(merged["files"][:6])
               if merged.get("state") == "replayed" else "",
               "The queue's gates (%s) did not run on the merged tree: only the PR's "
               "required checks did." % gates_text if constated
               else "Gates passed on the merged tree: %s." % gates_text)),
            {"project": project.name, "target": row["target"], "commit": merged["sha"]})
        how = self.record(reason, "merge queue of %s: %s, %s%s" % (
            row["target"], "constated GitHub auto-merge" if constated else "landed by ameesh",
            merged["sha"][:12], ", PR #%s" % pr if pr else ""))
        review = self.review_after_merge(merged_sha=str(merged.get("sha") or ""), pr=pr)
        return dict({"lot": self.lot, "result": "landed", "sha": merged["sha"], "pr": pr,
                     "review_lot": review},
                    **({"warning": "queue entry changed: recorded as history only (a newer entry is "
                        "queued)"} if how == "history" else {"warning": "remote merge succeeded; recording failed"}
                       if how == "unrecorded" else {}))


def land_head(cfg, db, project, row: dict, *, owner: str, replayer=None, locks=None,
              gh=_gh, parts=None) -> dict:
    """Lands (or follows the landing of) `row`, the head of its queue.
    Returns `{lot, result: landed|landing|retry|ejected|busy|error, …}`.

    L1651 (#1650) : `parts` porte la requalification d'un lot dont la CI
    individuelle est remplacée par celle d'un paquet (§12, #1467) — les
    contextes dus que l'appelant atteste, ou `()` ; None : la porte du projet
    (`ci_parts`) s'applique telle quelle."""
    from . import radar, resolutions
    locks = locks if locks is not None else {}
    lot = int(row["work_item_id"])
    source = merge_queue.normalize_source(row.get("source") or "")
    if not source or source != merge_queue.source_of(project.repo):
        return {"lot": lot, "result": "error", "error": (
            "queue source %s does not match project %s repository; select the source "
            "project (or requeue a legacy entry without a source)"
            % (source or "<missing>", project.name))}
    projects = tuple(radar.settings_of(cfg).projects.values())
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
    if replayer is None:
        replayer = resolutions.Replayer(project.repo, radar.clone_dir(cfg, project.name))
    landing = _Landing(db, project, row, key, owner, epoch, locks, projects,
                       cfg=cfg, clone=getattr(replayer, "path", None), replayer=replayer,
                       parts=parts)
    slug = radar.github_slug(project) if project.land == "pr" else None
    try:
        try:
            merge_queue.radar_project(cfg, project.repo)
        except merge_queue.QueueError as exc:
            return landing.eject(str(exc), "Resolve the repository landing configuration, "
                                 "then ameesh work queue add %d." % lot)
        glob = radar.protected_target(project, row["target"])
        if glob:
            # L160g (#473): queued before the target was protected — never
            # landed; ejected under the lease, like any ejection
            return landing.eject("%s is a protected target (%s)" % (row["target"], glob),
                                 "Merging into %s is the orchestrator's gesture: ask it."
                                 % row["target"])
        return _land(landing, replayer, slug, gh)
    except (GitError, LandError, OSError, ValueError) as exc:
        error = {"lot": lot, "result": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
        if row["state"] == "landing" and project.land == "pr":
            # its branch holds the merge commit: it stays `landing`, lease kept,
            # and the PR is read again at the next pass (an error never merges)
            landing.write(reason="reading PR failed (%s): retried" % str(exc)[:200])
            return error
        landing.release = True
        landing.write(state="queued", reason="landing failed (%s): retried" % str(exc)[:200])
        return error
    finally:
        if landing.release:
            ops.land_unlock(key, owner, epoch)
            locks.pop(key, None)


def _land(landing: _Landing, replayer, slug, gh) -> dict:
    from . import radar, resolutions
    project, row, lot = landing.project, landing.row, landing.lot
    if row["state"] == "landing" and project.land == "pr":
        return _follow_pr(landing, slug, gh=gh, replayer=replayer)

    replayer.ensure()
    url = _origin(project) or project.repo
    branch = _branch_of(row["target"])
    lease_ref = (getattr(project, "lease_ref", "") or LEASE_REF) + branch
    mine = None
    if project.land == "local":
        prefix = getattr(project, "lease_ref", "") or LEASE_REF
        targets = {branch}
        for other in landing.projects:
            targets.add(other.target or plan_git.default_target(other.repo))
        # Queue targets can differ from settings (a lot's branch_target).
        # Reserve lease namespaces across all configured projects/queues,
        # even if two checkout paths happen to hide the same remote.
        targets.update(r["target"] for r in landing.ops.queue())
        collision = next((t for t in sorted(targets) if radar.lease_collides(prefix, t)), None)
        if collision is not None:
            landing.release = True
            return {"lot": lot, "result": "error", "error": (
                "lease ref %s collides with the target %s: nothing written; set "
                "radar.projects.%s.lease_ref under %s" % (
                    lease_ref, collision, project.name, " or ".join(radar.LEASE_NAMESPACES)))}
    if project.land == "local":
        # #194 r6: the remote lease ref is the lock itself, taken (compare and
        # swap) BEFORE any write of this holder; the database expiry only
        # allows trying. Refused: nothing written, the lease released
        try:
            mine = take_remote_lease(replayer.path, url, lease_ref, landing.key,
                                     landing.epoch, landing.owner)
        except _LeaseRefused as exc:
            landing.release = True
            return {"lot": lot, "result": "error", "error": (
                "%s: nothing written, retried; if the remote refuses this namespace, set "
                "radar.projects.%s.lease_ref to a branch prefix (e.g. ameesh-land/)"
                % (exc, project.name))}
    # land: local — a lot left `landing` (lease lost or runner gone) is
    # landed again from the start: the push to the target is never forced
    if not landing.write(state="landing", note="merge queue of %s: landing (%s)"
                         % (row["target"], project.land), actor=work.SYSTEM_SENDER):
        return landing.busy("lease on %s lost before landing" % landing.key)
    if project.land == "pr":
        found = None
        status = pr_status(slug, row["branch"], gh=gh) if slug else None
        if status is None:
            # #1682: an armed auto-merge can merge the PR between two passes,
            # at the exact frozen head. That merge is a fact like any other:
            # found here, it is recorded instead of ejecting the queue entry.
            # A merge at any other head (the #1162 rewrite) finds no match and
            # keeps the ejection below.
            found = pr_merged(slug, row["branch"], row["frozen_sha"], gh=gh) if slug else None
            if found is not None:
                return _after_merge(landing, found["number"], found["base"], None,
                                    sha=row["frozen_sha"])
            return landing.eject("no open PR for %s" % row["branch"],
                                 "Open the PR, then ameesh work queue add %d." % lot)
        # #1656: never leave an active auto-merge on a PR the queue manages. It
        # would merge whatever head it finds — a rewritten one included, even
        # under `[skip ci]` — with no new fire, freeze or parts proof (GitHub
        # checks `--match-head-commit` only when the auto-merge is armed). ameesh
        # merges itself, on the exact frozen head; disarm first, fail closed —
        # before any ejection below (conflict, gate, CI), which would otherwise
        # leave it armed for the head the author pushes next.
        if status.get("auto"):
            error = pr_disable_auto(slug, status["number"], gh=gh)
            if error is not None:
                # #1682: the auto-merge may have fired between the read and
                # the disarm — the disarm then fails on an already merged PR.
                # A merge at the frozen head into its target is constated;
                # anything else keeps the fail-closed ejection.
                found, unread = _merged_after_disarm(slug, row["branch"], row["frozen_sha"], gh)
                if found is not None:
                    return _after_merge(landing, found["number"], found["base"], None,
                                        sha=row["frozen_sha"])
                return landing.eject(
                    "PR #%s: auto-merge armed and not disarmed (%s)%s"
                    % (status["number"], error, unread),
                    "Disarm it by hand (gh pr merge --disable-auto %s), then ameesh work "
                    "queue add %d: an armed auto-merge merges a head the queue has not "
                    "qualified (#1656)." % (status["number"], lot))
    base = fresh_target(replayer, project, row["target"])
    if project.land == "local" and resolutions._run(
            replayer.path, "merge-base", "--is-ancestor", row["frozen_sha"],
            base).returncode == 0:
        # #194 r4: another executor (an earlier epoch) already pushed it
        return landing.landed({"sha": base, "state": "already", "files": []}, base)
    refused = landing.required_ci()
    if refused is not None:
        return refused
    merged = merge(replayer, base, row, landing.ops.resolutions([lot]),
                   generated=getattr(project, "generated", ()))
    if merged["state"] == "generated":
        return landing.eject("generated file %s: regeneration or check failed on the merged "
                             "tree" % merged["files"][0],
                             "Its output:\n%s\nFix the generator or its inputs on your branch, "
                             "freeze again, then ameesh work queue add %d."
                             % (merged["output"], lot), output=merged["output"])
    if merged["state"] == "conflict":
        return landing.eject("conflict on %s with %s at %s" % (
            ", ".join(merged["files"][:6]) or "?", row["target"], base[:12]),
            "Integrate %s into your branch (or prepare the resolution), freeze again, "
            "then ameesh work queue add %d." % (row["target"], lot))
    checked = gates(project, replayer.path, files=changed(replayer.path, base, merged["sha"]))
    if not checked["ok"]:
        return landing.eject("gate `%s` failed on the merged tree" % checked["gate"],
                             "Its output:\n%s\nFix it on your branch, freeze again, then "
                             "ameesh work queue add %d." % (checked["output"], lot),
                             output=checked["output"])
    refused = landing.required_ci()
    if refused is not None:
        return refused
    if project.land == "local":
        if not landing.held():
            return landing.busy("lease on %s lost before pushing" % landing.key)
        stopped = _fire_guard(landing)
        if stopped is not None:
            return stopped
        # #194 r6: one atomic push, two compare-and-swaps that the server
        # checks: the target on the base seen, the lease ref on the value this
        # holder wrote — moved to a child commit, so git sends that update
        # and receive-pack checks its old value (an unchanged ref is skipped)
        target_ref = "refs/heads/%s" % branch
        after = lease_commit(replayer.path, landing.key, landing.epoch, landing.owner,
                             parent=mine, landed=lot)
        proc = resolutions._run(replayer.path, "push", "--quiet", "--no-verify", "--atomic",
                                "--force-with-lease=%s:%s" % (target_ref, base),
                                "--force-with-lease=%s:%s" % (lease_ref, mine), url,
                                "%s:%s" % (merged["sha"], target_ref),
                                "%s:%s" % (after, lease_ref), timeout=FETCH_TIMEOUT)
        if proc.returncode != 0:
            if not landing.held():
                return landing.busy("lease on %s lost: push refused" % landing.key)
            landing.release = True
            if not landing.write(state="queued", reason="target moved during the merge: "
                                 "retried at the next pass", note="merge queue of %s: push "
                                 "refused (%s), retried" % (row["target"],
                                                            resolutions._err(proc)[-200:]),
                                 actor=work.SYSTEM_SENDER):
                return landing.busy("lease on %s lost: push refused" % landing.key)
            return {"lot": lot, "result": "retry", "error": resolutions._err(proc)}
        return landing.landed(merged, base)
    # land: pr — the merge commit goes to the lot branch, its CI decides
    if status.get("base") != branch:
        return landing.eject("PR #%s targets %s, not %s" % (
            status["number"], status.get("base") or "?", branch),
            "Point the PR at %s (or queue the lot for its target), then ameesh work "
            "queue add %d." % (branch, lot))
    if not merge_queue._same(status["head"], row["frozen_sha"]):
        return landing.eject("PR #%s is at %s, not at the freeze %s" % (
            status["number"], (status["head"] or "?")[:12], row["frozen_sha"][:12]),
            "Something pushed to the branch after its freeze: freeze again, then "
            "ameesh work queue add %d." % lot)
    if not landing.held():
        return landing.busy("lease on %s lost before pushing" % landing.key)
    stopped = _fire_guard(landing)
    if stopped is not None:
        return stopped
    # #194 r4: compare and swap on the freeze (see `local`)
    branch_ref = "refs/heads/%s" % row["branch"]
    proc = resolutions._run(replayer.path, "push", "--quiet", "--no-verify",
                            "--force-with-lease=%s:%s" % (branch_ref, row["frozen_sha"]),
                            url, "%s:%s" % (merged["sha"], branch_ref),
                            timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        if not landing.held():
            return landing.busy("lease on %s lost: push refused" % landing.key)
        return landing.eject("push to %s refused (%s)" % (
            row["branch"], resolutions._err(proc)[-200:]),
            "Something pushed to the branch after its freeze: freeze again, then "
            "ameesh work queue add %d." % lot)
    if not landing.write(state="landing", landing_sha=merged["sha"],
                         reason="PR #%s: CI on the merge %s" % (status["number"],
                                                                merged["sha"][:12]),
                         note="merge queue of %s: %s merged into the branch (%s), waiting "
                         "for the CI of PR #%s" % (row["target"], row["target"],
                                                   merged["sha"][:12], status["number"]),
                         actor=work.SYSTEM_SENDER):
        return landing.busy("lease on %s lost after pushing" % landing.key)
    return {"lot": lot, "result": "landing", "sha": merged["sha"],
            "replayed": merged["files"] if merged["state"] == "replayed" else []}


def target_moved(replayer, project, row: dict) -> str | None:
    """#194 r4 (PR mode): the target's tip now, when it is no longer the base
    of the merge commit `landing_sha` (its first parent); None if it has not
    moved. Raises (LandError, GitError) when either cannot be read: the lot
    stays `landing` and is read again (never a merge on an unknown base)."""
    from . import resolutions
    url = _origin(project) or project.repo
    proc = resolutions._run(project.repo, "ls-remote", url,
                            "refs/heads/%s" % _branch_of(row["target"]), timeout=FETCH_TIMEOUT)
    tip = resolutions._text(proc).split()
    if proc.returncode != 0 or not tip:
        raise LandError("git ls-remote %s: %s" % (_branch_of(row["target"]),
                                                  resolutions._err(proc)[:200] or "no tip"))
    parent = resolutions._run(replayer.path, "rev-parse", "%s^1" % row["landing_sha"])
    if parent.returncode != 0:
        replayer.ensure()
        resolutions._run(replayer.path, "fetch", "--quiet", url,
                         "+refs/heads/%s:%s" % (row["branch"], FRESH_REF + "-lot"),
                         timeout=FETCH_TIMEOUT)
        parent = resolutions._run(replayer.path, "rev-parse", "%s^1" % row["landing_sha"])
    base = resolutions._check(parent, "rev-parse")
    return None if tip[0] == base else tip[0]


def pr_view(slug: str, number, gh=_gh) -> dict:
    """#194 r5: base and head of PR `number`, read together in one call."""
    proc = gh(["pr", "view", str(number), "--repo", slug, "--json",
               "baseRefName,headRefOid,state"])
    if proc.returncode != 0:
        raise LandError("gh pr view: %s" % (proc.stderr or proc.stdout or "").strip()[:300])
    data = _json(proc, "gh pr view", dict)
    return {"base": data.get("baseRefName"), "head": data.get("headRefOid"),
            "state": data.get("state")}


def _follow_pr(landing: _Landing, slug, *, gh, replayer) -> dict:
    project, row, lot = landing.project, landing.row, landing.lot
    branch = _branch_of(row["target"])
    status = pr_status(slug, row["branch"], gh=gh) if slug else None
    if status is None and slug and row.get("landing_sha"):
        # #194 r8: merged since (a merge call that timed out, or between two
        # passes): checked like any merge of ameesh's, never ejected
        merged = pr_merged(slug, row["branch"], row["landing_sha"], gh=gh)
        if merged is not None:
            return _after_merge(landing, merged["number"], merged["base"], None)
    if status is None:
        if row.get("item_state") in work.MERGED_STATES:
            landing.release = True
            if not landing.write(state="landed", reason="merged",
                                 note="merge queue of %s: landed" % row["target"],
                                 actor=work.SYSTEM_SENDER):
                return landing.busy("lease on %s lost" % landing.key)
            review = landing.review_after_merge(merged_sha=_merged_sha(landing.db, lot))
            return {"lot": lot, "result": "landed", "review_lot": review}
        return landing.eject("its PR is no longer open",
                             "Reopen it, then ameesh work queue add %d." % lot)
    # #1656: an armed auto-merge would merge the head it finds without the
    # parts proof and without the base re-read below — and would land a head
    # rewritten after the fire. ameesh is the only actor that merges a PR it
    # manages: disarm first, fail closed.
    if status.get("auto"):
        error = pr_disable_auto(slug, status["number"], gh=gh)
        if error is not None:
            # #1682: the auto-merge may have fired between the read and the
            # disarm (the disarm then fails on a merged PR): a merge at the
            # landing head is constated, like the "merged since" path above.
            merged, unread = _merged_after_disarm(slug, row["branch"], row["landing_sha"], gh)
            if merged is not None:
                return _after_merge(landing, merged["number"], merged["base"], None)
            return landing.eject(
                "PR #%s: auto-merge armed and not disarmed (%s)%s — it could merge a head "
                "the queue has not qualified" % (status["number"], error, unread),
                "Disarm it by hand (gh pr merge --disable-auto %s), then ameesh work "
                "queue add %d; ameesh merges itself on the exact frozen head (#1656)."
                % (status["number"], lot))
    if status.get("base") != branch:
        return landing.eject("PR #%s targets %s, not %s" % (
            status["number"], status.get("base") or "?", branch),
            "Point the PR at %s, freeze again, then ameesh work queue add %d." % (branch, lot))
    if not merge_queue._same(status["head"], row.get("landing_sha")):
        return landing.eject("its branch moved while ameesh was landing it (%s)"
                             % (status["head"] or "?")[:12],
                             "Freeze again, then ameesh work queue add %d." % lot)
    if status["ci"] == "pending":
        return {"lot": lot, "result": "landing", "pr": status["number"]}
    if status["ci"] == "red":
        return landing.eject("the CI of PR #%s is red on the merge %s"
                             % (status["number"], row["landing_sha"][:12]),
                             "Fix it on your branch (it already holds the merge of %s), "
                             "freeze again, then ameesh work queue add %d."
                             % (row["target"], lot))
    if not landing.held():
        return landing.busy("lease lost before merging PR #%s" % status["number"])
    # #194 r4: the CI ran on the merge with the target as it was
    moved = target_moved(replayer, project, row)
    if moved:
        return landing.eject("%s moved to %s since the merge %s: its CI ran on an older "
                             "base" % (row["target"], moved[:12], row["landing_sha"][:12]),
                             "Integrate %s again into your branch, freeze again, then "
                             "ameesh work queue add %d." % (row["target"], lot))
    # #194 r5: base and head read again together, right before the merge.
    # GitHub's merge takes no base nor epoch precondition: the window left —
    # between this read and `gh pr merge` — is a residual risk the owner
    # accepts or not (land: local has none); a wrong base is checked after
    now = pr_view(slug, status["number"], gh=gh)
    if now["base"] != branch or not merge_queue._same(now["head"], row["landing_sha"]):
        return landing.eject("PR #%s changed right before its merge (base %s, head %s)" % (
            status["number"], now["base"] or "?", (now["head"] or "?")[:12]),
            "Check the PR, freeze again, then ameesh work queue add %d." % lot)
    refused = landing.required_ci()
    if refused is not None:
        return refused
    if not landing.held():
        return landing.busy("lease lost before merging PR #%s" % status["number"])
    stopped = _fire_guard(landing)
    if stopped is not None:
        return stopped
    error = pr_merge(slug, status["number"], row["landing_sha"], project.merge_method, gh=gh)
    # #194 r8: past this call, the PR may be merged: nothing raises any more,
    # and every outcome is read back from GitHub
    try:
        now = pr_view(slug, status["number"], gh=gh)
        after, unread = now["base"], None
    except Exception as exc:  # noqa: BLE001 - the merge may have happened
        now, after, unread = None, None, "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if error is not None:
        if error.startswith(TIMED_OUT) and (now is None or now["state"] != "MERGED"):
            detail = unread if now is None else (
                "read-back state %s; remote operation may complete later" % now["state"])
            return _merged_in_doubt(landing, status["number"], None,
                                    "%s; %s" % (error[:200], detail), pending=True)
        if now is not None and now["state"] != "MERGED":
            # #194 r3: the branch holds the merge commit; the lot stays
            # `landing` with it (and the lease: a lasting refusal raises
            # `merge_blocked`); a merge completed later is found by `pr_merged`
            landing.write(reason="PR merge refused (%s): retried" % error[:200])
            return {"lot": lot, "result": "retry", "error": error}
        if now is None:
            landing.write(reason="PR merge refused (%s), PR unreadable (%s): retried"
                          % (error[:200], unread))
            return {"lot": lot, "result": "retry", "error": error}
    return _after_merge(landing, status["number"], after, unread)


def _after_merge(landing: _Landing, pr, after, unread, *, sha: str | None = None) -> dict:
    """#194 r8: PR `pr` is merged (by this call or found merged): landed if
    into its target, otherwise reported. `sha` names the merged head when the
    caller knows it (#1682: a merge found before the queue pushed its own
    merge commit, recorded as constated; the default is the `landing_sha`
    the queue recorded)."""
    state = "pr" if sha is None else "constated"
    if sha is None:
        sha = landing.row["landing_sha"]
    if after != _branch_of(landing.row["target"]):
        return _merged_in_doubt(landing, pr, after, unread)
    return landing.landed({"sha": sha, "state": state, "files": []}, None, pr=pr)


def _merged_sha(db, lot: int) -> str:
    """Le sha de fusion déclaré au jalon `merged`, si connu (chemin « déjà
    fusionné » de `_follow_pr`, L539)."""
    try:
        rows = work.milestones(db, int(lot))
    except Exception:  # noqa: BLE001 - jalon illisible : sha inconnu
        return ""
    for row in rows or ():
        if str(row.get("kind") or "") == "merged" and row.get("sha"):
            return str(row["sha"])
    return ""


#: #194 r6: reasons of a PR merged where it should not have been, or that
#: cannot be checked — the urgent alert `land_uncertain` reads them
WRONG_BASE = "WRONG BASE: "
UNCERTAIN = "UNCERTAIN: "


def _merged_in_doubt(landing: _Landing, pr, after, unread, *, pending=False) -> dict:
    """The PR is merged, but into another base (retargeted inside the last
    window) or its base cannot be read back; `pending`: its merge call timed
    out and completion is still unknown (OPEN or unreadable). The warning goes first,
    whatever happens to the lease; then the fact, under the lease or the
    freeze, else as history only — never over a newer freeze (#194 r8).
    `land_uncertain` alerts on it. Pending, the lot stays `landing` (the next
    pass reads it again: `pr_merged`)."""
    row, lot, branch = landing.row, landing.lot, _branch_of(landing.row["target"])
    reason = (WRONG_BASE + "PR #%s merged into %s, not %s: check it by hand"
              % (pr, after, branch)) if after else (
        UNCERTAIN + "PR #%s merge timed out, completion uncertain (%s): read again at the "
        "next pass, check it by hand" % (pr, unread)) if pending else (
        UNCERTAIN + "PR #%s merged, base unreadable after the merge (%s): check it by hand"
        % (pr, unread))
    merge_queue._wake(landing.db, row.get("assignee"), lot, (
        "Merge queue of %s, lot #%d: %s." % (row["target"], lot, reason)),
        {"project": landing.project.name, "target": row["target"], "kind": "land",
         "lot": lot, "reason": reason})
    landing.release = not pending
    how = landing.record(reason, "merge queue of %s: %s" % (row["target"], reason),
                         state="landing" if pending else "landed")
    return {"lot": lot, "result": "uncertain" if after is None else "landed",
            "wrong_base": after, "pr": pr, "reason": reason, "recorded": how}


#: #194 r8: how long a doubt recorded as history only (a newer freeze queued
#: over it) stays an alert
UNCERTAIN_HISTORY_S = float(os.environ.get("AMEESH_LAND_UNCERTAIN_H", "24")) * 3600


def uncertain(db, now: float | None = None) -> list[dict]:
    """#194 r6: lots merged by ameesh into a wrong base, or not checked: in
    the queue, or (#194 r8) as history only, for `UNCERTAIN_HISTORY_S`."""
    now = time.time() if now is None else now
    ops = storage.of(db).work
    try:
        rows = ops.queue()
    except Exception:  # noqa: BLE001 - database without 0058
        return []
    out = [r for r in rows if (r.get("reason") or "").startswith((WRONG_BASE, UNCERTAIN))]
    seen = {int(r["work_item_id"]) for r in out}
    queued = {int(r["work_item_id"]): r for r in rows}
    for prefix in (WRONG_BASE, UNCERTAIN):
        try:
            noted = ops.events_noted(prefix, now - UNCERTAIN_HISTORY_S)
        except Exception:  # noqa: BLE001 - read again at the next pass
            noted = []
        for event in noted:
            lot = int(event["work_item_id"])
            if lot in seen or "(history: " not in (event.get("note") or ""):
                continue
            seen.add(lot)
            out.append(dict(queued.get(lot) or {}, work_item_id=lot,
                            assignee=event.get("assignee"),
                            reason=event["note"], updated_ts=event.get("created_ts")))
    return out


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
