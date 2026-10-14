# SPDX-License-Identifier: AGPL-3.0-only
"""REL-11 (#1003) — merging a PR that touches `.github/workflows/**` outside the gh API.

Why: GitHub refuses (`gh pr merge`, HTTP 403) the merge of a PR whose diff
touches `.github/workflows/**` when the host's token lacks the `workflow`
OAuth scope (incident #562/REL-11: the release PR was merged by a local merge
commit pushed over SSH; GitHub marked the PR MERGED). The gesture is
reproducible, but it must be framed: it writes directly on the target branch,
so it can bypass what the API verifies (branch protection, required checks).

    ameesh work merge-pr <pr> --head <frozen sha> --lot N [--repo owner/repo]
                          [--target branch] [--fetch-url URL] [--push-url URL]
                          [--dry-run] [--json]

The gesture, and its guards (all checked BEFORE any write):

1. **one repository identity** (#1003 B/P1, R1 delta then cx3 delta): the
   checkout, the PR's `--repo`, `--fetch-url` and `--push-url` must designate
   the SAME repository — `owner/repo` key for GitHub, real path for a local
   transport — and the checkout must carry that identity (its `origin`). The
   two forms never mix: a local transport does not prove a GitHub key (nor
   the reverse), it is the same refusal. A divergence, or an unverifiable
   identity (no `origin`, unknown URL), refuses before the first read: from a
   clone of A, `--repo B` with B's remotes cannot make A be admitted by B's
   conditions, nor the reverse;
2. **the lot** (`--lot`, required) carries the authorization: it must be
   frozen on `--head` and carry a review verdict `ok` on that same commit
   (the class is read by the queue: a light lot is exempt). It is the
   admission **of the merge queue itself** (`merge_queue.readiness`) that is
   replayed here — protected target, freeze, verdict, class, green CI on the
   frozen head — and not a copy: this path is never weaker than the queue;
3. **the project** is resolved by the radar configuration
   (`merge_queue.radar_project`): its conditions (`protected`, `ci_required`,
   `gates`) apply. A protected target is refused (that is the orchestrator's
   gesture, outside this command); `ci_required` requires the frozen commit's
   green CI (`radar.commit_ci`), and the project's `gates` run on the merged
   tree before the push;
4. **the PR** is read by `gh`: open, not a draft, its base is the target, its
   head is the given frozen SHA, its `mergeStateStatus` is `CLEAN`/`HAS_HOOKS`
   and all its checks are concluded and green (`radar.ci_green`) — nothing is
   weakened: a blocked PR, behind (`BEHIND`) or with a non-green check is
   refused;
5. **the diff** is computed locally (`merge-base` then `diff --name-only`):
   the PR must touch at least one `.github/workflows/**` path — this path
   therefore never replaces the API merge for the other PRs;
6. **the frozen SHA** is read back on the remote repository
   (`refs/pull/<pr>/head`) and must be exactly `--head`;
7. **the target has not moved**: the target's fresh head is an ancestor of
   the freeze (`merge-base --is-ancestor`); otherwise nothing is written and
   the target must be merged back into the branch, CI and review redone;
8. **the merge commit tree is identical to the freeze tree**: the local
   `merge --no-ff` must produce exactly the reviewed tree; two parents
   (target head, freeze), a message naming the PR and the guards;
9. **fresh guard just before the write**: the PR is read a second time
   (state, head, base, mergeability, checks), the queue admission is replayed
   on that view, and `refs/pull/<pr>/head` as well as the target head are
   read again on the remote. Any mutation since the merge commit was built
   stops the gesture without writing. The window between this last read and
   the push cannot be closed by git alone: the compare-and-swap below closes
   the race on the target, not a PR closure or a check turning red in that
   instant — the final read-back notes it and never declares the lot on a
   non-`MERGED` PR;
10. **the push is a compare-and-swap**: `--force-with-lease=<ref>:<head>` on
    a remote **outside the token** (SSH); if the target moved between the
    read and the push, git refuses — never `--force`;
11. **read-back**: the PR is read until `MERGED`, its merge commit must be
    the one pushed, the target head read back must be that commit; the lot is
    declared merged only after that read-back.

An **already `MERGED`** PR is bound by the same link (#1003 N1, P2 blocker):
its content evidence (`refs/pull/<pr>/head`, ancestor of the target) is not
enough to declare a lot. `--lot` must carry the freeze of that head, on the
PR's branch and target; a missing, closed or frozen-elsewhere lot, or one on
another branch/target, refuses without declaring anything. `--dry-run` never
declares, `already` included.

The read remote can stay the token one (reading a workflow does not need the
scope); only the push must be outside the token. `--dry-run` runs every guard
(the fresh one included) and builds the merge commit without pushing or
declaring: it is the verification a review can require from a merge gesture.

Without a remote outside the token (no SSH key on the host), the command
stops before any write and names the owner's decision: `gh auth refresh -s
workflow`, a fine-grained PAT with `Workflows: write` given to the API, a
GitHub App, or an SSH deploy key. See docs/EXPLOITATION.md, section "Merge
outside the gh API of a PR touching .github/workflows/**".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

from . import land, merge_queue, migrations, plan_git, radar, resolutions, work
from .canon import FETCH_TIMEOUT, GitError

#: the path that makes the API refuse the merge
WORKFLOW_PREFIX = ".github/workflows/"
#: reproducible merge commit identity; GitHub attributes the push to the SSH
#: key, not to this identity
IDENTITY = ("ameesh", "ameesh@localhost")
#: "PR MERGED" read-back after the push: number of attempts and wait
POLL_S = float(os.environ.get("AMEESH_MERGE_PR_POLL_S", "5"))
ATTEMPTS = int(os.environ.get("AMEESH_MERGE_PR_ATTEMPTS", "12"))
#: short `git ls-remote`/`rev-parse` calls; fetch and push use FETCH_TIMEOUT
SHORT_TIMEOUT = 60.0
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HTTPS = re.compile(r"^https?://", re.I)
#: a URL carrying credentials (`https://user:token@…`): never shown as such,
#: neither in a report nor in an error
_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")
#: a bare GitHub key `owner/repo` (the PR's `--repo`): exactly two segments,
#: no spaces and no absolute path
_GITHUB_KEY = re.compile(r"^[^/\s]+/[^/\s]+$")

def _redact(text: str) -> str:
    """Strip the credentials from a URL before quoting or writing it."""
    return _CREDENTIALS.sub(r"\1***@", text or "")


class MergePrError(RuntimeError):
    """A refused or impossible gesture: actionable message, nothing written."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _json(proc, what: str) -> dict:
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MergePrError("%s: unreadable output (%s)" % (what, exc)) from exc
    if not isinstance(data, dict):
        raise MergePrError("%s: unexpected output %s" % (what, (proc.stdout or "")[:120]))
    return data


def _run(cwd: str, *args: str, timeout: float = SHORT_TIMEOUT, stdin: bytes | None = None):
    return resolutions._run(cwd, *args, timeout=timeout, stdin=stdin)


def _check(cwd: str, *args: str, timeout: float = SHORT_TIMEOUT) -> str:
    proc = _run(cwd, *args, timeout=timeout)
    if proc.returncode != 0:
        raise MergePrError("git %s: %s" % (args[0] if args else "?",
                                           _redact(resolutions._err(proc)[-300:])))
    return resolutions._text(proc)


def _out(cwd: str, *args: str, timeout: float = SHORT_TIMEOUT) -> str:
    proc = _run(cwd, *args, timeout=timeout)
    return resolutions._text(proc) if proc.returncode == 0 else ""


def _same(sha, other) -> bool:
    return bool(sha) and bool(other) and str(sha).lower() == str(other).lower()


def check_push_url(url: str) -> str:
    """The push remote: never the token one on github.com (https), which is
    exactly what lacks the `workflow` scope."""
    text = (url or "").strip()
    if not text:
        raise MergePrError("no push remote: give --push-url (an SSH remote whose key can "
                           "write) or fix the token scope and use `gh pr merge`")
    if _HTTPS.match(text) and migrations.github_repo(text):
        raise MergePrError(
            "push URL %s goes through the token (https): that is the path GitHub "
            "refuses for a workflow PR; give the SSH remote (git@github.com:owner/repo.git)"
            % _redact(text))
    return text


def check_head(head: str) -> str:
    text = (head or "").strip().lower()
    if not FULL_SHA.match(text):
        raise MergePrError("--head: the full frozen commit (40 hex), not %r" % (head,))
    return text


def _branch_name(target: str) -> str:
    """`origin/main`, `refs/heads/main`, `refs/remotes/origin/main` and `main`
    all name `main` — the merge queue's normalization, reused here so that a
    `branch_target` written `refs/remotes/origin/main` is not refused against a
    PR based on `main` (#1253, cx3)."""
    return merge_queue._remote_branch((target or "").strip())


def _pr_guards(view: dict, pr: int, head: str, target: str) -> None:
    """The guards carried by the PR view, replayed before any write."""
    state = (view.get("state") or "").upper()
    if state != "OPEN":
        raise MergePrError("PR #%d is %s, not open: nothing to merge" % (pr, state or "?"))
    if view.get("isDraft"):
        raise MergePrError("PR #%d is a draft: mark it ready for review first" % pr)
    if not _same(view.get("headRefOid"), head):
        raise MergePrError("PR #%d head moved: it is at %s, the freeze is %s; nothing "
                           "written — refreeze and re-review the new head"
                           % (pr, (view.get("headRefOid") or "?")[:12], head[:12]))
    base = (view.get("baseRefName") or "").strip()
    if base and _branch_name(base) != _branch_name(target):
        raise MergePrError("PR #%d targets %s, not %s" % (pr, base, target))
    merge_state = (view.get("mergeStateStatus") or "").upper()
    if merge_state not in ("CLEAN", "HAS_HOOKS"):
        raise MergePrError(
            "PR #%d is not mergeable now (mergeStateStatus %s): the direct path never "
            "weakens what the API checks — required checks, reviews or the base state "
            "must be settled first" % (pr, merge_state or "?"))
    if not radar.ci_green(view.get("statusCheckRollup") or []):
        raise MergePrError("PR #%d: the checks on %s are not all concluded and green"
                           % (pr, head[:12]))


def _project_of(cfg, repo: str):
    """The repository's radar project, or None (like the queue: no declared
    conditions). An unreadable or ambiguous configuration refuses; it never
    assumes the absence of conditions."""
    try:
        return merge_queue.radar_project(cfg, repo)
    except merge_queue.QueueError as exc:
        raise MergePrError("cannot read the landing conditions of %s: %s" % (repo, exc)) \
            from exc


def _admission(db, cfg, lot: int, repo: str, project, owner: str, view: dict) -> dict:
    """The merge queue admission, replayed as is (#1003 B1): freeze, `ok`
    verdict on the head (class read from the frozen diff), non-protected
    target, green CI of the PR on that head, the project's `ci_required`. The
    radar clone is this gesture's working repository: `readiness` reads the
    lot's branch there on the remote, never in the author's checkout."""
    branch = (view.get("headRefName") or "").strip()

    def ci_status(name: str):
        if not branch or name != branch:
            return None
        return {"number": view.get("number"), "url": view.get("url") or "",
                "head": (view.get("headRefOid") or "").lower(),
                "green": radar.ci_green(view.get("statusCheckRollup") or []),
                "draft": bool(view.get("isDraft"))}

    clone = resolutions.Replayer(repo, owner)
    try:
        ready = merge_queue.readiness(db, lot, repo, ci=ci_status, clone=clone,
                                      project=project, cfg=cfg)
    except merge_queue.QueueError as exc:
        raise MergePrError("lot #%d is not admitted by the merge queue: %s" % (lot, exc)) \
            from exc
    return ready


def _ls_remote_ref(owner: str, url: str, ref: str) -> str:
    """The value of a ref on the remote, lowercased; "" when unreadable."""
    text = _out(owner, "ls-remote", url, ref)
    return text.split()[0].lower() if text.split() else ""


def _remote_identity(url: str):
    """A remote's identity (#1003 B/P1): `("github", owner/repo)` when the URL
    designates a GitHub repository, `("path", real path)` when it is a local
    transport (`file://` included), None when it is not verifiable — an
    unverifiable identity is not accepted."""
    text = (url or "").strip()
    if not text:
        return None
    slug = migrations.github_repo(text)
    if slug:
        return ("github", migrations.repo_key(slug))
    path = text[len("file://"):] if text.startswith("file://") else text
    if os.path.isabs(path) and os.path.exists(path):
        return ("path", os.path.realpath(path))
    return None


def _slug_identity(slug: str):
    """The identity of the repository `--repo` designates: `("path", real
    path)` when it is an existing local path, `("github", owner/repo)` when it
    is a bare GitHub key, None when it is not verifiable. An unverifiable
    identity is not accepted, and two different forms never bind to each
    other: a local transport is not the proof of a GitHub key (R1 delta, cx3,
    2026-10-13)."""
    text = (slug or "").strip()
    local = _remote_identity(text)
    if local is not None:
        return local
    if _GITHUB_KEY.match(text):
        return ("github", migrations.repo_key(text))
    return None


def _bind_repo_identity(repo: str, slug: str, fetch_url: str, push_url: str) -> tuple:
    """One repository identity for the PR, the lot, the admission, the read
    and the push (#1003 B/P1).

    The checkout (`repo`), `--fetch-url` and `--push-url` must designate the
    SAME repository: `owner/repo` key for GitHub, real path for a local
    transport. The PR's `slug` must designate that same identity, in the same
    form: from a clone of A, `--repo B` with A's remotes is refused as a
    divergence, even when A and B share the same commits; a local transport
    with a GitHub key is not verifiable and refuses too. Without a readable
    `origin`, nothing is verifiable: the gesture refuses before any PR read,
    any admission and any write."""
    origin_url = plan_git._out(repo, "remote", "get-url", "origin") or ""
    origin = _remote_identity(origin_url)
    if origin is None:
        raise MergePrError(
            "cannot verify the repository of this checkout (%s): its origin is missing or "
            "unreadable — run the gesture from the checkout of the repository it merges; "
            "nothing written" % (_redact(origin_url) or "no origin"))
    for what, url in (("--fetch-url", fetch_url), ("--push-url", push_url)):
        ident = _remote_identity(url)
        if ident is None:
            raise MergePrError("cannot verify the identity of %s (%s): nothing written"
                               % (what, _redact(url)))
        if ident != origin:
            raise MergePrError(
                "%s (%s) and this checkout's origin (%s) are not the same repository: the "
                "lot, its admission, the PR read and the push must be one repository; "
                "nothing written" % (what, _redact(url), _redact(origin_url)))
    slug_ident = _slug_identity(slug)
    if slug_ident is None:
        raise MergePrError(
            "cannot verify the repository named by --repo %s: give owner/repo or the "
            "checkout's real path; nothing written" % (slug,))
    if slug_ident != origin:
        raise MergePrError(
            "--repo %s and this checkout's origin (%s) are not the same repository: the PR "
            "read by gh and the repository whose guards admit and whose branch is pushed "
            "must be one; nothing written" % (slug, _redact(origin_url)))
    return origin


def pr_view(slug: str, pr: int, gh=land._gh) -> dict:
    """The PR read by `gh`: state, base, head, mergeability, checks."""
    proc = gh(["pr", "view", str(pr), "--repo", slug, "--json",
               "number,state,isDraft,baseRefName,headRefOid,headRefName,mergeStateStatus,"
               "mergeCommit,statusCheckRollup,url,title"], timeout=60.0)
    if proc.returncode != 0:
        raise MergePrError("gh pr view #%s: %s"
                           % (pr, (proc.stderr or proc.stdout or "").strip()[:300]))
    return _json(proc, "gh pr view #%s" % pr)


def message(pr: int, head: str, target: str, tip: str, tree: str, *,
            title: str = "", head_ref: str = "", lot: int | None = None) -> str:
    """The merge commit message: the PR, the guards, the lot."""
    subject = "Merge PR #%d%s into %s (merge outside gh, #1003/REL-11)" % (
        pr, " from %s" % head_ref if head_ref else "", target)
    lines = [subject, ""]
    if title:
        lines += ["%s (head %s)." % (title, head[:12]), ""]
    lines += [
        "Local merge commit: frozen head == the PR head (%s), %s %s is an ancestor of the "
        "freeze," % (head[:12], target, tip[:12]),
        "merge commit tree == the freeze tree (%s), push --force-with-lease "
        "(compare-and-swap)." % tree,
        "The gh API refuses to merge a PR touching %s** without the workflow scope."
        % WORKFLOW_PREFIX,
    ]
    if lot is not None:
        lines += ["", "ameesh-work: %d" % lot]
    return "\n".join(lines) + "\n"


def _scratch() -> str:
    path = tempfile.mkdtemp(prefix="ameesh-merge-pr-")
    _check(path, "init", "-q")
    return path


def _fetch(owner: str, fetch_url: str, target: str, pr: int) -> tuple[str, str]:
    """The target head and the PR head, read on the read remote (GitHub's
    `refs/pull/<pr>/head`, authoritative)."""
    proc = _run(owner, "fetch", "--quiet", "--no-tags", fetch_url,
                "+refs/heads/%s:refs/ameesh/merge-pr/target" % target,
                "+refs/pull/%d/head:refs/ameesh/merge-pr/head" % pr,
                timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise MergePrError(
            "cannot read %s and refs/pull/%d/head from %s (%s): the read remote must "
            "reach the repository" % (target, pr, _redact(fetch_url),
                                      _redact(resolutions._err(proc)[-300:])))
    tip = _check(owner, "rev-parse", "refs/ameesh/merge-pr/target")
    head = _check(owner, "rev-parse", "refs/ameesh/merge-pr/head")
    return tip, head


def _ls_remote(owner: str, url: str, target: str) -> str:
    text = _out(owner, "ls-remote", url, "refs/heads/%s" % target)
    return text.split()[0].lower() if text.split() else ""


def _already_lot_links(db, lot: int, head: str, view: dict, target: str, repo: str,
                       cfg) -> None:
    """An already-MERGED PR (#1003 N1, P2 blocker): the lot must carry the
    freeze of this head, on the PR's branch and target, before any declaration.

    A PR merged outside this command never passed the queue admission: linking
    it by `--lot` alone would close an unrelated lot. Nothing is written here —
    a missing or different link raises, and `work.merged` is not called. The
    correct case (lot frozen on the head, same branch, same target) keeps its
    previous behaviour."""
    try:
        row = work.get(db, lot)
    except Exception as exc:  # noqa: BLE001 - unreadable registry: nothing is declared
        raise MergePrError("lot #%d cannot be read (%s): the merge of an already-MERGED PR "
                           "is not declared on it" % (lot, type(exc).__name__)) from exc
    if row is None:
        raise MergePrError("lot #%d not found: the merge of an already-MERGED PR is not "
                           "declared on it" % lot)
    if (row.get("state") or "").strip() == "closed":
        raise MergePrError("lot #%d is closed (%s): the merge of an already-MERGED PR is not "
                           "declared on it" % (lot, row.get("close_reason") or "closed"))
    # N1 review (cx3): the MERGED view must name the frozen head (headRefOid),
    # not only the fetched ref; a silent view proves nothing.
    oid = (view.get("headRefOid") or "").strip().lower()
    if not oid or not _same(oid, head):
        raise MergePrError("PR #%s head is %s, the freeze is %s: nothing declared"
                           % (view.get("number") or "?", oid[:12] or "?", head[:12]))
    try:
        marks = work.milestones(db, lot)
    except Exception as exc:  # noqa: BLE001 - unreadable registry: nothing is declared
        raise MergePrError("lot #%d milestones cannot be read (%s): nothing declared"
                           % (lot, type(exc).__name__)) from exc
    freeze = plan_git.latest_freeze(marks)
    frozen = ((freeze or {}).get("sha") or "").strip().lower()
    if not frozen:
        raise MergePrError("lot #%d has no frozen commit: the merge of an already-MERGED PR "
                           "is not declared on it" % lot)
    if not _same(frozen, head):
        raise MergePrError("lot #%d is frozen on %s, not on the PR head %s: nothing declared "
                           "(an already-MERGED PR does not close another lot's freeze)"
                           % (lot, frozen[:12], head[:12]))
    branch = (view.get("headRefName") or "").strip()
    lot_branch = (row.get("branch") or "").strip()
    if not branch or not lot_branch or lot_branch != branch:
        raise MergePrError("lot #%d is on branch %s, the PR head branch is %s: nothing "
                           "declared" % (lot, lot_branch or "?", branch or "?"))
    project = None
    if cfg is not None:
        project = _project_of(cfg, repo)
    app = (row.get("app") or "").strip()
    if project is not None and app and app != project.name:
        raise MergePrError("lot #%d is declared on project %s, %s belongs to project %s: "
                           "nothing declared" % (lot, app, repo, project.name))
    expected = (row.get("branch_target") or "").strip()
    if not expected and project is not None:
        expected = (getattr(project, "target", "") or "").strip()
    if not expected:
        try:
            expected = (plan_git.default_target(repo) or "").strip()
        except (GitError, OSError):
            expected = ""
    if not expected or _branch_name(expected) != _branch_name(target):
        raise MergePrError("lot #%d targets %s, the PR targets %s: nothing declared"
                           % (lot, expected or "?", target))


def _already(slug: str, pr: int, head: str, view: dict, *, target: str,
             fetch_url: str, owner: str) -> dict:
    """Already MERGED PR: check that the freeze is in the target."""
    tip, fetched = _fetch(owner, fetch_url, target, pr)
    contains = _run(owner, "merge-base", "--is-ancestor", head, tip).returncode == 0
    out = {"state": "already" if contains and _same(fetched, head) else "already-unverified",
           "pr": pr, "url": view.get("url") or "", "target": target, "frozen": head,
           "tip": tip, "merge": ((view.get("mergeCommit") or {}).get("oid") or "").lower(),
           "content_verified": contains,
           "why": "" if contains else
           "the frozen head %s is not an ancestor of %s %s (squash/rebase merge?): "
           "check the merged content by hand" % (head[:12], target, tip[:12])}
    return out


# --------------------------------------------------------------------------
# the gesture
# --------------------------------------------------------------------------

def direct_merge(slug: str, pr: int, head: str, *, lot: int, repo: str, db=None, cfg=None,
                 project=None, target: str | None = None,
                 fetch_url: str | None = None, push_url: str | None = None,
                 gh=land._gh, dry_run: bool = False,
                 attempts: int | None = None, poll_s: float | None = None,
                 sleep=time.sleep, work_dir: str | None = None) -> dict:
    """Merge PR `pr` (head `head`, lot `lot`) onto `target` by a local commit
    and a compare-and-swap push outside the token, with the module's guards —
    including the queue admission (`merge_queue.readiness`), replayed before
    any write and a second time just before the push. Nothing is written
    before the fresh guard; `dry_run` stops before the push. Returns the
    report (`state`: merged | already | already-unverified | dry-run |
    pushed-unverified)."""
    head = check_head(head)
    if lot is None:
        raise MergePrError("--lot is required: the frozen lot carries the freeze and the "
                           "review verdict this gesture replays, and the merge is declared "
                           "on it")
    slug = (slug or "").strip()
    if not slug:
        raise MergePrError("no GitHub repository: give --repo owner/repo")
    slug_ident = _slug_identity(slug)
    if slug_ident is None:
        raise MergePrError("cannot verify the repository named by --repo %s: give owner/repo "
                           "or the checkout's real path; nothing written" % (slug,))
    if slug_ident[0] == "github":
        slug = slug_ident[1]
    fetch_url = (fetch_url or "").strip() or ("https://github.com/%s.git" % slug)
    push_url = check_push_url(push_url or ("git@github.com:%s.git" % slug))
    # #1003 B/P1: one repository identity, verified before the first read; a
    # divergence writes nothing and admits nothing.
    _bind_repo_identity(repo, slug, fetch_url, push_url)
    view = pr_view(slug, pr, gh=gh)
    branch = (view.get("baseRefName") or "").strip()
    target = (target or "").strip() or branch
    try:
        target = plan_git.check_ref(target, "target")
    except ValueError as exc:
        raise MergePrError(str(exc)) from exc
    if branch and _branch_name(branch) != _branch_name(target):
        raise MergePrError("PR #%d targets %s, not %s" % (pr, branch, target))

    owner = work_dir or _scratch()
    temporary = work_dir is None
    try:
        state = (view.get("state") or "").upper()
        if state == "MERGED":
            # N1 (#1003): an already-MERGED PR does not declare a lot without
            # the lot/freeze/branch/target link; nothing is written otherwise.
            _already_lot_links(db, lot, head, view, target, repo, cfg)
            return _already(slug, pr, head, view, target=target, fetch_url=fetch_url,
                            owner=owner)
        if project is None:
            project = _project_of(cfg, repo)
        _pr_guards(view, pr, head, target)
        ready = _admission(db, cfg, lot, repo, project, owner, view)
        if not _same(ready.get("frozen_sha"), head):
            raise MergePrError("lot #%d is admitted on %s, not on the freeze %s: refreeze and "
                               "re-review" % (lot, (ready.get("frozen_sha") or "?")[:12],
                                              head[:12]))
        if _branch_name(ready.get("target")) != _branch_name(target):
            raise MergePrError("lot #%d is queued for %s, not %s: merge it on its own target"
                               % (lot, ready.get("target") or "?", target))
        api_base = (view.get("baseRefOid") or "").lower()
        merge_state = (view.get("mergeStateStatus") or "").upper()

        tip, fetched = _fetch(owner, fetch_url, target, pr)
        if not _same(fetched, head):
            raise MergePrError("the remote PR head is %s, the freeze is %s: nothing written"
                               % (fetched[:12], head[:12]))
        base = _out(owner, "merge-base", tip, head)
        if not base:
            raise MergePrError("no common ancestor between %s and %s" % (tip[:12], head[:12]))
        files = [f for f in _check(owner, "diff", "--name-only", base, head).splitlines()
                 if f.strip()]
        touched = [f for f in files if f.startswith(WORKFLOW_PREFIX)]
        if not touched:
            raise MergePrError("PR #%d does not touch %s**: use `gh pr merge` (the API), "
                               "this path is reserved for the workflow-scope refusal"
                               % (pr, WORKFLOW_PREFIX))
        if _run(owner, "merge-base", "--is-ancestor", tip, head).returncode != 0:
            raise MergePrError(
                "%s moved since the freeze (%s is not an ancestor of %s): nothing written — "
                "refusion %s into the branch, rerun the exact CI and the review, freeze again"
                % (target, tip[:12], head[:12], target))

        _check(owner, "checkout", "-q", "--detach", tip)
        proc = _run(owner, "merge", "--no-ff", "--no-commit", "--quiet", head,
                    timeout=FETCH_TIMEOUT)
        if proc.returncode != 0:
            conflicts = [f for f in resolutions.unmerged(owner)]
            _run(owner, "merge", "--abort")
            detail = ", ".join(conflicts[:6]) or _redact(resolutions._err(proc)[-200:])
            raise MergePrError("the merge of %s on %s is not clean (%s): nothing written"
                               % (head[:12], tip[:12], detail))
        tree = _check(owner, "write-tree")
        frozen_tree = _check(owner, "rev-parse", "%s^{tree}" % head)
        if tree != frozen_tree:
            _run(owner, "merge", "--abort")
            raise MergePrError(
                "the merge tree %s differs from the frozen tree %s: the merge would not be a "
                "no-op — nothing written" % (tree[:12], frozen_tree[:12]))
        text = message(pr, head, target, tip, tree, title=(view.get("title") or ""),
                       head_ref=(view.get("headRefName") or ""), lot=lot)
        proc = _run(owner, "-c", "user.name=%s" % IDENTITY[0],
                    "-c", "user.email=%s" % IDENTITY[1],
                    "commit", "--quiet", "--no-verify", "-F", "-", stdin=text.encode(),
                    timeout=SHORT_TIMEOUT)
        if proc.returncode != 0:
            raise MergePrError("cannot write the merge commit: %s"
                               % _redact(resolutions._err(proc)[-300:]))
        merge_sha = _check(owner, "rev-parse", "HEAD")
        parents = _check(owner, "rev-list", "--parents", "-n", "1", merge_sha).split()
        if parents != [merge_sha, tip, head]:
            raise MergePrError("the merge commit %s does not have (%s, %s) as parents: %s"
                               % (merge_sha[:12], tip[:12], head[:12], " ".join(parents)))
        evidence = {"pr": pr, "url": view.get("url") or "", "target": target,
                    "frozen": head, "tip": tip, "api_base": api_base, "merge": merge_sha,
                    "tree": tree, "frozen_tree": frozen_tree,
                    "workflow_files": touched, "changed_files": len(files),
                    "merge_state": merge_state,
                    "admission": {"target": ready.get("target") or "",
                                  "class": ready.get("class") or "",
                                  "class_why": ready.get("class_why") or ""},
                    "message": text}
        # #1003 B1: the radar project's gates, on the merged tree, as the
        # queue does before pushing (no declared gate: nothing to run).
        if project is not None and (getattr(project, "gates", ())
                                    or getattr(project, "path_gates", ())):
            checked = land.gates(project, owner, files=files)
            if not checked["ok"]:
                _run(owner, "merge", "--abort")
                raise MergePrError("gate `%s` failed on the merged tree: %s"
                                   % (checked["gate"], checked["output"]))
            evidence["gates"] = {"ok": True, "gate": checked["gate"]}
        # #1003 B2: fresh guard just before the write — the PR, the queue
        # admission and both remote refs are read again; any mutation since
        # the merge commit was built stops without writing.
        fresh = pr_view(slug, pr, gh=gh)
        _pr_guards(fresh, pr, head, target)
        ready = _admission(db, cfg, lot, repo, project, owner, fresh)
        if not _same(ready.get("frozen_sha"), head) \
                or _branch_name(ready.get("target")) != _branch_name(target):
            raise MergePrError("lot #%d moved during the gesture (admission now %s on %s): "
                               "nothing written — refreeze and re-review"
                               % (lot, (ready.get("frozen_sha") or "?")[:12],
                                  ready.get("target") or "?"))
        remote_pr = _ls_remote_ref(owner, fetch_url, "refs/pull/%d/head" % pr)
        if not _same(remote_pr, head):
            raise MergePrError("the remote PR head is now %s, the freeze is %s: nothing "
                               "written — refreeze and re-review the new head"
                               % (remote_pr[:12] or "?", head[:12]))
        remote_target = _ls_remote(owner, fetch_url, target)
        if not _same(remote_target, tip):
            raise MergePrError("%s moved to %s during the gesture (read %s): nothing written "
                               "— refusion %s into the branch, rerun the exact CI and the "
                               "review, freeze again"
                               % (target, remote_target[:12] or "?", tip[:12], target))
        if dry_run:
            evidence.update(state="dry-run", pushed=False, remote_tip="")
            return evidence

        ref = "refs/heads/%s" % target
        try:
            proc = _run(owner, "push", "--quiet", "--no-verify",
                        "--force-with-lease=%s:%s" % (ref, tip), push_url,
                        "%s:%s" % (merge_sha, ref), timeout=FETCH_TIMEOUT)
            error = "" if proc.returncode == 0 else _redact(resolutions._err(proc)[-300:])
        except GitError as exc:  # unknown push outcome: read the target again
            error = _redact("%s: %s" % (type(exc).__name__, exc))
        remote_tip = _ls_remote(owner, fetch_url, target)
        if error and not _same(remote_tip, merge_sha):
            raise MergePrError("push refused or unreadable (%s); %s is at %s, not %s — nothing "
                               "was written by this call; read it again before retrying"
                               % (error, target, remote_tip[:12] or "?", merge_sha[:12]))

        ok, last, final, merged_sha = False, None, "", ""
        attempts = ATTEMPTS if attempts is None else max(1, int(attempts))
        poll_s = POLL_S if poll_s is None else max(0.0, float(poll_s))
        for attempt in range(attempts):
            last = pr_view(slug, pr, gh=gh)
            final = (last.get("state") or "").upper()
            merged_sha = ((last.get("mergeCommit") or {}).get("oid") or "").lower()
            if final == "MERGED" and _same(merged_sha, merge_sha) \
                    and _same(remote_tip, merge_sha):
                ok = True
                break
            if attempt + 1 < attempts:
                sleep(poll_s)
        remote_tip = _ls_remote(owner, fetch_url, target)
        evidence.update(state="merged" if ok else "pushed-unverified", pushed=True,
                        remote_tip=remote_tip, pr_state=final, pr_merge=merged_sha)
        if not ok:
            evidence["why"] = (
                "pushed %s on %s, but the read-back is not conclusive (PR %s, merged commit "
                "%s, %s tip %s): check the PR and the branch by hand before declaring the lot"
                % (merge_sha[:12], target, final or "?", merged_sha[:12] or "?",
                   target, remote_tip[:12] or "?"))
        return evidence
    finally:
        if temporary:
            shutil.rmtree(owner, ignore_errors=True)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "merge-pr", help="merge a PR touching .github/workflows/** when the token lacks the "
                         "workflow scope (REL-11/#1003): local merge with guards, "
                         "compare-and-swap push over a non-token remote, read-back")
    parser.add_argument("pr", type=int)
    parser.add_argument("--head", required=True,
                        help="the frozen/reviewed commit, full 40-hex SHA")
    parser.add_argument("--repo", default=None, help="GitHub owner/repo (default: origin)")
    parser.add_argument("--target", default=None,
                        help="target branch (default: the PR base)")
    parser.add_argument("--fetch-url", default=None,
                        help="remote read for the PR and the target (default: origin)")
    parser.add_argument("--push-url", default=None,
                        help="non-token remote pushed to (default: git@github.com:owner/repo.git)")
    parser.add_argument("--lot", type=int, required=True,
                        help="the frozen lot that authorizes the gesture (freeze, review "
                             "verdict, admission); the merge is declared on it")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every guard and build the merge commit; push nothing")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="",
                        help="who declares the merge on the lot (default: the session's bound "
                             "identity, AGENT_MAIL_NAME); an unknown actor declares nothing")
    parser.set_defaults(func=func)


def _human(out: dict) -> str:
    state = out.get("state")
    if state == "dry-run":
        return ("PR #%s: guards pass, merge commit %s would be pushed on %s (tree %s == frozen "
                "tree %s, %s workflow file(s)); nothing pushed"
                % (out["pr"], out["merge"][:12], out["target"], out["tree"][:12],
                   out["frozen_tree"][:12], len(out.get("workflow_files") or [])))
    if state == "already":
        return ("PR #%s is already MERGED: %s is on %s (%s)"
                % (out["pr"], out["frozen"][:12], out["target"], out["tip"][:12]))
    if state == "already-unverified":
        return "PR #%s is already MERGED but %s" % (out["pr"], out.get("why") or "")
    if state == "merged":
        text = ("PR #%s merged: %s on %s (tree %s == frozen tree %s, push --force-with-lease)"
                % (out["pr"], out["merge"][:12], out["target"], out["tree"][:12],
                   out["frozen_tree"][:12]))
        recorded = out.get("recorded")
        if recorded:
            text += "; lot #%s declared merged" % out.get("lot")
        elif out.get("record_warning"):
            text += "; WARNING lot not declared: %s" % out["record_warning"]
        return text
    return "PR #%s: %s" % (out["pr"], out.get("why") or state)


def run(cfg, db, args) -> int:
    import sys
    try:
        cwd = os.getcwd()
        slug = (args.repo or migrations.origin_repo(cwd) or "").strip()
        if not slug:
            raise MergePrError("cannot read the GitHub repository from origin: give --repo "
                               "owner/repo")
        # the operator path merges only a GitHub PR: a local path is not
        # verifiable against a GitHub slug (cx3 delta) and the command
        # refuses it, rather than letting `gh` fail further on.
        slug_ident = _slug_identity(slug)
        if slug_ident is None:
            raise MergePrError("cannot verify the repository named by --repo %s: give "
                               "owner/repo; nothing written" % (slug,))
        if slug_ident[0] != "github":
            raise MergePrError("--repo %s is a local transport: this command merges a GitHub "
                               "PR, give --repo owner/repo; nothing written" % (slug,))
        origin = plan_git._out(cwd, "remote", "get-url", "origin") or ""
        fetch_url = args.fetch_url or origin or ("https://github.com/%s.git" % slug)
        push_url = check_push_url(args.push_url or ("git@github.com:%s.git" % slug))
        out = direct_merge(slug, args.pr, args.head, lot=args.lot, repo=cwd, db=db, cfg=cfg,
                           target=args.target, fetch_url=fetch_url, push_url=push_url,
                           dry_run=args.dry_run)
        # N1 (#1003): `--dry-run` never declares anything, including on an
        # already-MERGED PR (the `already` finding is not a write).
        if out.get("state") in ("merged", "already") and not args.dry_run:
            out["lot"] = args.lot
            sha = out.get("merge") or out.get("tip") or ""
            actor = (getattr(args, "actor", "") or "").strip()
            try:
                if not sha:
                    raise MergePrError("no merge commit to declare")
                # N2 (#1253): never record the milestone as UNKNOWN_ACTOR. The
                # CLI resolves --actor from the bound identity (AGENT_MAIL_NAME);
                # an unbound session leaves it empty or UNKNOWN_ACTOR, and the
                # merge is a fact that is not undone — only the declaration is
                # skipped, with a warning naming the fix.
                if not actor or actor == work.UNKNOWN_ACTOR:
                    raise MergePrError(
                        "unknown actor: pass --actor <agent|human:id>, or run from a bound "
                        "session; the merge is a fact, the lot is not declared")
                out["recorded"] = work.merged(
                    db, args.lot, sha=sha, actor=actor,
                    note="ameesh work merge-pr %d --head %s (merge outside gh, "
                         "#1003/REL-11)" % (args.pr, out["frozen"][:12]))
            except Exception as exc:  # noqa: BLE001 - the merge is a fact, never undone
                out["record_warning"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else _human(out))
        if out["state"] in ("merged", "already", "dry-run"):
            return 0
        return 1 if out["state"] == "already-unverified" else 2
    except (MergePrError, GitError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
