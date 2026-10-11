# SPDX-License-Identifier: AGPL-3.0-only
"""Actual time of a delivered lot by phase, and size of its merged diff (L157c).

  ameesh work measure [<id> …] [--repo DIR] [--no-ci] [--json]

L157 measures one actual duration per lot (merge − start); L157b asks the
estimators to estimate by phase. This module measures the phases, once,
after the merge, and stores them in `work_item_measures` (migration 0056):

* **Phases** (minutes of wall-clock time, adding up to merge − start). Each
  instant belongs to ONE phase, by priority:

  1. `human`  — the lot is in state `waiting_human`;
  2. `review` — waiting for review and merge: from a freeze (milestone
     `frozen`) to the next return to `build` (or the merge), and state `qa`;
  3. `ci`     — a CI run of the lot's branch is in progress (GitHub Actions
     runs read with `gh run list --branch`); unknown (None) when the runs
     cannot be read, that time then stays in `agent`;
  4. `agent`  — the rest: the agent works.

* **Size of the change**: the merged diff (first parent of the merge commit
  → merge commit, which is also right for a squash or a fast-forward commit):
  files, inserted and deleted lines, and the size class (`s` up to
  `SIZE_S_MAX` changed lines, `m` up to `SIZE_M_MAX`, `l` beyond) compared
  with the size DECLARED at creation (`work add --size`).

The runner measures the lots delivered in the last `PENDING_WINDOW_S` and
not measured yet, after its branch sweep (`measure_pending`): the repository is the working directory of
an agent of its host that holds the merge commit. A lot measured without a
diff (no repository holds the merge commit yet) is measured again for
`RETRY_DIFF_S`. Older lots: `work measure` (the 50 most recent not
measured yet, or the lots named). The measure is a fact about one lot; the analysis across
lots stays with the auditor (`work estimates --publish`).
"""
from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import time

from . import storage

PHASES = ("agent", "ci", "review", "human")
#: priority of the phases when they overlap (first wins)
PRIORITY = ("human", "review", "ci", "agent")
#: states counted as human time, and as review time
HUMAN_STATES = ("waiting_human",)
REVIEW_STATES = ("qa",)
#: the state that ends a review window (the agent works again)
WORK_STATES = ("build",)
#: size classes on changed lines (insertions + deletions)
SIZE_S_MAX = 100
SIZE_M_MAX = 500
#: a lot measured without a diff is measured again for this long after its merge
RETRY_DIFF_S = 2 * 86400.0
#: the runner pass only measures the lots merged this recently (older ones:
#: `work measure`)
PENDING_WINDOW_S = 2 * 86400.0
#: lots measured at most by one runner pass
PASS_LIMIT = 50
#: timeout of one git or gh call (s)
CALL_TIMEOUT_S = 20
#: CI runs read at most for one branch
CI_RUN_LIMIT = 100

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SHORTSTAT_RE = {
    "files": re.compile(r"(\d+) files? changed"),
    "insertions": re.compile(r"(\d+) insertions?\(\+\)"),
    "deletions": re.compile(r"(\d+) deletions?\(-\)"),
}


# --------------------------------------------------------------------------
# phases (pure)
# --------------------------------------------------------------------------

def _clip(intervals, start: float, end: float) -> list[tuple[float, float]]:
    out = []
    for lo, hi in intervals:
        lo, hi = max(float(lo), start), min(float(hi), end)
        if hi > lo:
            out.append((lo, hi))
    return out


def state_spans(events: list[dict], start: float, end: float) -> list[tuple[str, float, float]]:
    """The state timeline of a lot between `start` and `end`, from its
    journal (`work_item_events`, any order): `(state, from, to)`. The state
    at `start` is the last one entered before it."""
    rows = sorted(((float(e["created_ts"]), int(e.get("id") or 0), e["state"])
                   for e in events if e.get("created_ts") is not None),
                  key=lambda r: (r[0], r[1]))
    spans, current, since = [], None, start
    for at, _, state in rows:
        if at <= start:
            current = state
            continue
        if at >= end:
            break
        if state == current:
            continue
        if current is not None:
            spans.append((current, since, at))
        current, since = state, at
    if current is not None and end > since:
        spans.append((current, since, end))
    return spans


def review_windows(events: list[dict], milestones: list[dict], end: float
                   ) -> list[tuple[float, float]]:
    """Waiting for review and merge: from each freeze to the next return to
    `build` after it (or `end`)."""
    back = sorted(float(e["created_ts"]) for e in events
                  if e.get("state") in WORK_STATES and e.get("created_ts") is not None)
    out = []
    for m in milestones:
        if m.get("kind") != "frozen" or m.get("at_ts") is None:
            continue
        at = float(m["at_ts"])
        out.append((at, next((b for b in back if b > at), end)))
    return out


def split(start: float, end: float, *, events: list[dict], milestones: list[dict],
          ci_runs: list[dict] | None) -> dict:
    """The phases of a lot between its start and its merge: `{total_minutes,
    agent_minutes, ci_minutes, review_minutes, human_minutes, ci_runs}`.
    `ci_runs`: `[{start_ts, end_ts}]`, or None when unknown (`ci_minutes` is
    then None and that time counts as agent time)."""
    start, end = float(start), max(float(start), float(end))
    spans = state_spans(events, start, end)
    marks = {
        "human": _clip([(lo, hi) for s, lo, hi in spans if s in HUMAN_STATES], start, end),
        "review": _clip([(lo, hi) for s, lo, hi in spans if s in REVIEW_STATES]
                        + review_windows(events, milestones, end), start, end),
        "ci": _clip([(r["start_ts"], r["end_ts"]) for r in ci_runs or ()
                     if r.get("start_ts") is not None and r.get("end_ts") is not None],
                    start, end),
    }
    cuts = sorted({start, end} | {t for iv in marks.values() for pair in iv for t in pair})
    seconds = dict.fromkeys(PHASES, 0.0)
    for lo, hi in zip(cuts, cuts[1:]):
        mid = (lo + hi) / 2.0
        phase = next((p for p in PRIORITY[:-1]
                      if any(a <= mid < b for a, b in marks[p])), "agent")
        seconds[phase] += hi - lo
    out = {"total_minutes": round((end - start) / 60.0, 1)}
    for phase in PHASES:
        out["%s_minutes" % phase] = round(seconds[phase] / 60.0, 1)
    if ci_runs is None:
        out["ci_minutes"] = None
    out["ci_runs"] = None if ci_runs is None else len(marks["ci"])
    return out


# --------------------------------------------------------------------------
# size of the change (git)
# --------------------------------------------------------------------------

def size_class(changed_lines: int | None) -> str | None:
    """`s`, `m` or `l` from the changed lines (insertions + deletions)."""
    if changed_lines is None:
        return None
    if changed_lines <= SIZE_S_MAX:
        return "s"
    return "m" if changed_lines <= SIZE_M_MAX else "l"


def _git(repo: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=CALL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def has_commit(repo: str, sha: str) -> bool:
    return bool(sha and _SHA_RE.match(sha)
                and _git(repo, "cat-file", "-e", "%s^{commit}" % sha) is not None)


def parse_shortstat(text: str) -> dict:
    out = {}
    for key, rx in _SHORTSTAT_RE.items():
        match = rx.search(text or "")
        out[key] = int(match.group(1)) if match else 0
    return out


def diff_stat(repo: str, sha: str) -> dict | None:
    """The merged diff of `sha` (first parent → sha): `{diff_files,
    diff_insertions, diff_deletions, measured_size}`, or None when the
    repository does not hold the commit."""
    if not has_commit(repo, sha):
        return None
    parent = "%s^1" % sha if _git(repo, "rev-parse", "--verify", "-q", "%s^1" % sha) \
        else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"     # the empty tree
    text = _git(repo, "diff", "--shortstat", "--no-renames", parent, sha)
    if text is None:
        return None
    stat = parse_shortstat(text)
    return {"diff_files": stat["files"], "diff_insertions": stat["insertions"],
            "diff_deletions": stat["deletions"],
            "measured_size": size_class(stat["insertions"] + stat["deletions"])}


# --------------------------------------------------------------------------
# CI runs (gh)
# --------------------------------------------------------------------------

def _iso_ts(text) -> float | None:
    if not text:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def parse_runs(payload) -> list[dict]:
    """`gh run list --json createdAt,updatedAt,status` → `[{start_ts,
    end_ts}]` (finished runs only)."""
    out = []
    for run in payload or ():
        if run.get("status") != "completed":
            continue
        lo, hi = _iso_ts(run.get("createdAt")), _iso_ts(run.get("updatedAt"))
        if lo is not None and hi is not None and hi >= lo:
            out.append({"start_ts": lo, "end_ts": hi})
    return out


def gh_ci_runs(repo: str, branch: str | None) -> list[dict] | None:
    """The CI runs of `branch` (GitHub Actions, through `gh` in `repo`), or
    None when they cannot be read (no branch, no `gh`, no access)."""
    if not branch or not repo:
        return None
    try:
        proc = subprocess.run(
            ["gh", "run", "list", "--branch", branch, "--limit", str(CI_RUN_LIMIT),
             "--json", "createdAt,updatedAt,status"], cwd=repo, capture_output=True,
            text=True, timeout=CALL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return parse_runs(json.loads(proc.stdout or "[]"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# measure and store
# --------------------------------------------------------------------------

def _repo_for(sha: str | None, repos) -> str | None:
    for repo in repos or ():
        if repo and os.path.isdir(repo) and sha and has_commit(repo, sha):
            return repo
    return None


def measure_one(db, cand: dict, *, repos=(), ci=gh_ci_runs) -> dict:
    """Measures one delivered lot (a row of `measure_candidates`) and stores
    it. `repos`: repositories where the merge commit may be; `ci(repo,
    branch)`: its CI runs, or None (`ci=None`: not read)."""
    st = storage.of(db).work
    item_id = int(cand["id"])
    events = st.events(item_id, 1000)
    milestones = st.milestones(item_id, 1000)
    repo = _repo_for(cand.get("merge_sha"), repos)
    # the CI runs are read in the repository that holds the merge commit
    runs = ci(repo, cand.get("branch")) if ci and repo else None
    out = split(cand["started_ts"], cand["merged_ts"], events=events,
                milestones=milestones, ci_runs=runs)
    out.update(started_ts=cand["started_ts"], merged_ts=cand["merged_ts"],
               merge_sha=cand.get("merge_sha") or None,
               declared_size=cand.get("change_size") or None)
    if repo:
        out.update(diff_stat(repo, cand["merge_sha"]) or {})
    return st.save_measure(item_id, out)


def measure(db, ids=None, *, repos=(), ci=gh_ci_runs, limit: int = PASS_LIMIT,
            since_s: float | None = None) -> list[dict]:
    """Measures the given delivered lots (`ids`, measured again), or those
    still to measure (`ids` None; merged less than `since_s` ago when
    given), the most recent first. Returns the stored measures."""
    cands = storage.of(db).work.measure_candidates(
        ids=None if ids is None else [int(i) for i in ids], retry_diff_s=RETRY_DIFF_S,
        since_s=since_s, limit=limit)
    if ids is None:
        # a lot already measured is only retried for its diff, when a
        # repository holds the merge commit now
        cands = [c for c in cands if not c["measured"]
                 or _repo_for(c.get("merge_sha"), repos)]
    return [measure_one(db, c, repos=repos, ci=ci) for c in cands]


def host_repos(db, host: str | None, agents=None) -> list[str]:
    """The working directories (registry `cwd`) of the agents of `host`."""
    from . import registry

    out = []
    for agent in registry.overview(db):
        if host and agent.get("host") != host:
            continue
        if agents and agent.get("name") not in agents:
            continue
        repo = os.path.expanduser(agent.get("cwd") or "")
        if repo and os.path.isdir(repo) and repo not in out:
            out.append(repo)
    return out


def measure_pending(db, *, host: str | None = None, agents=None, ci=gh_ci_runs) -> list[dict]:
    """The runner pass: measures the lots delivered recently and not
    measured yet."""
    return measure(db, None, repos=host_repos(db, host, agents), ci=ci,
                   since_s=PENDING_WINDOW_S)


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

def view(row: dict | None) -> dict | None:
    """The measure of a lot for `work show --json` (None if not measured),
    with `size_match`: measured size == declared size (None if either is
    unknown)."""
    if not row:
        return None
    out = {k: row.get(k) for k in (
        "measured_ts", "total_minutes", "agent_minutes", "ci_minutes", "review_minutes",
        "human_minutes", "ci_runs", "merge_sha", "diff_files", "diff_insertions",
        "diff_deletions", "measured_size", "declared_size")}
    out["size_match"] = (None if not out["measured_size"] or not out["declared_size"]
                         else out["measured_size"] == out["declared_size"])
    return out


def describe_phases(v: dict) -> str:
    """« agent 1 h 10 · CI 12 min · review 35 min · human 0 min »."""
    from . import estimates

    parts = []
    for phase, name in (("agent", "agent"), ("ci", "CI"), ("review", "review"),
                        ("human", "human")):
        value = v.get("%s_minutes" % phase)
        parts.append("%s %s" % (name, "unknown" if value is None
                                else estimates.label(value) if value else "0 min"))
    return " · ".join(parts)


def describe_size(v: dict) -> str:
    """« declared m · measured l (612 lines, 9 files) — differs »."""
    declared = v.get("declared_size") or "—"
    if v.get("measured_size") is None:
        return "declared %s · measured: unknown (merge commit not found)" % declared
    text = "declared %s · measured %s (%d lines, %d files)" % (
        declared, v["measured_size"], int(v["diff_insertions"] or 0)
        + int(v["diff_deletions"] or 0), int(v["diff_files"] or 0))
    if v.get("size_match") is False:
        text += " — differs"
    return text


def format_measures(rows: list[dict]) -> str:
    if not rows:
        return "no delivered lot to measure"
    out = []
    for row in rows:
        v = view(row)
        out.append("lot #%s: %s — %s" % (row["work_item_id"], describe_phases(v),
                                         describe_size(v)))
    return "\n".join(out)
