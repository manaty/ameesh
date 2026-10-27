# SPDX-License-Identifier: AGPL-3.0-only
"""Actual time of a delivered lot by phase, and size of its merged diff (L157c).

  ameesh work measure [<id> …] [--repo DIR] [--no-ci] [--json]

L157 measures one actual duration per lot (merge − start); L157b asks the
estimators to estimate by phase. This module measures the phases, once,
after the merge, and stores them in `work_item_measures` (migration 0056):

* **Phases** (minutes of wall-clock time; each instant belongs to ONE phase,
  by priority):

  1. `human`  — the lot is in state `waiting_human`;
  2. `review` — waiting for review and merge: from a freeze (milestone
     `frozen`) to the next return to `build` (or the merge), and state `qa`;
  3. `ci`     — a CI run of the lot's branch is in progress (GitHub Actions
     runs read with `gh run list --branch`); unknown (None) when the runs
     cannot be read, that time then stays in `agent`;
  4. `agent`  — the agent's own work on the lot: the union of the turn spans
     the ledger attributes to it (`turn_costs.work_item_id` + `started_at`,
     #796), clipped to the window and to the priority phases. Only when no
     turn carries the lot — lots worked before migration 0423 — is it the
     rest of the timeline, as before.

  With attributed turns the phases no longer add up to the total: an instant
  in `build` that no turn covers is unattended (waiting, another lot, an
  idle host), not agent time. `agent_source` ('turns' or 'states') says which
  rule the measure used, so a series is never read across the change without
  knowing it.

* **Size of the change**: the merged diff (`diff_range`: first parent →
  the real merge commit, found from the recorded commit — merge commit,
  branch tip, frozen commit, squash commit — or merge-base → commit for a
  lot merged by rebase):
  files, inserted and deleted lines, and the size class (`s` up to
  `SIZE_S_MAX` changed lines, `m` up to `SIZE_M_MAX`, `l` beyond) compared
  with the size DECLARED at creation (`work add --size`).

  The target it is read against is the **published** one (#3038): the
  remote-tracking ref (`origin/<branch>`), never a local branch when the
  clone has a remote; a declared target is authoritative, otherwise the
  repository's default target is tried and then the other published
  integration refs (`develop`, `main`, …, `release/*`), the first that
  carries the merged commit winning. When no readable ref carries it, or
  when the published tip of the target for a rebase-merged lot cannot be
  proven (`ls-remote` unreadable, tip not fetched), the measure **doubts**:
  no size, rather than the history between a stale ref and the commit
  (the #2999 measure: 654430 lines for 332).

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
from .canon import NO_LAZY_FETCH

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
#: time budget of one runner pass (s): the backlog never delays the radar
PASS_BUDGET_S = 60.0
#: timeout of one git or gh call (s)
CALL_TIMEOUT_S = 20
#: timeout of a `git ls-remote` read of the published target (s): short, the
#: runner's measure pass has a budget and a forge that does not answer must
#: not eat it (#3038)
LS_REMOTE_TIMEOUT_S = 8.0
#: timeout of the exact-target fetch before a merge interval is read (#3275)
FETCH_TIMEOUT_S = 120.0
#: CI runs read at most for one branch
CI_RUN_LIMIT = 100

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
#: how long a published tip read by `ls-remote` is reused (s): a measure pass
#: clears it anyway, but `review_lot.class_at_merge` reads it from a
#: long-lived process and must not see a tip older than this (#3056)
PUBLISHED_TIP_TTL_S = 60.0
#: published tips read recently, `(repo, remote, branch)` → `(read_at, (kind,
#: sha))`, see `_remote_tip` (#3038, #3056)
_PUBLISHED_TIPS: dict = {}
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
          ci_runs: list[dict] | None, turns: list[tuple[float, float]] | None = None) -> dict:
    """The phases of a lot between its start and its merge: `{total_minutes,
    agent_minutes, ci_minutes, review_minutes, human_minutes, ci_runs,
    agent_source}`.
    `ci_runs`: `[{start_ts, end_ts}]`, or None when unknown (`ci_minutes` is
    then None and that time counts as agent time).
    `turns` (#796): the `(start_ts, end_ts)` spans of the turns the ledger
    attributes to the lot, or None when the attribution is unknown (no turn
    carries the lot). When they are known, agent time is the union of those
    spans — clipped to the window and to the priority phases — and not the
    rest of the timeline: what no turn covers stays unattended. `total_minutes`
    is then the sum of the four phases, as it is under the states rule: it no
    longer counts the time no turn and no priority phase claims. `agent_source`
    says which rule was used: 'turns' or 'states'."""
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
    covered = (None if turns is None
               else _clip([(float(lo), float(hi)) for lo, hi in turns], start, end))
    cuts = {start, end} | {t for iv in marks.values() for pair in iv for t in pair}
    if covered is not None:
        cuts |= {t for pair in covered for t in pair}
    cuts = sorted(cuts)
    seconds = dict.fromkeys(PHASES, 0.0)
    for lo, hi in zip(cuts, cuts[1:]):
        mid = (lo + hi) / 2.0
        phase = next((p for p in PRIORITY[:-1]
                      if any(a <= mid < b for a, b in marks[p])), None)
        if phase is None:
            if covered is not None and not any(a <= mid < b for a, b in covered):
                continue  # unattended: no turn attributed to the lot
            phase = "agent"
        seconds[phase] += hi - lo
    # total = agent + ci + review + human (bureau, #796) : sous la règle des
    # états, les phases couvrent toute la fenêtre ; sous celle des tours, le
    # temps qu'aucun tour ne couvre n'est compté nulle part — le total est
    # alors le temps couvert, et les quatre phases y additionnent toujours.
    out = {"total_minutes": round(sum(seconds.values()) / 60.0, 1)}
    for phase in PHASES:
        out["%s_minutes" % phase] = round(seconds[phase] / 60.0, 1)
    if ci_runs is None:
        out["ci_minutes"] = None
    out["ci_runs"] = None if ci_runs is None else len(marks["ci"])
    out["agent_source"] = "turns" if covered is not None else "states"
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


def _git(repo: str, *args: str, env_extra: dict | None = None,
         timeout: float = CALL_TIMEOUT_S) -> str | None:
    env = {**os.environ, **env_extra} if env_extra else None
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def has_commit(repo: str, sha: str) -> bool:
    # #1749 : sonde de présence — pas de fetch paresseux dans un clone
    # partiel (chaque échec laissait un `git <defunct>` adopté par
    # l'exécuteur).
    return bool(sha and _SHA_RE.match(sha)
                and _git(repo, "cat-file", "-e", "%s^{commit}" % sha,
                         env_extra=NO_LAZY_FETCH) is not None)


def parse_shortstat(text: str) -> dict:
    out = {}
    for key, rx in _SHORTSTAT_RE.items():
        match = rx.search(text or "")
        out[key] = int(match.group(1)) if match else 0
    return out


def _parents(repo: str, sha: str) -> list[str]:
    out = _git(repo, "rev-list", "--parents", "-n", "1", sha,
               env_extra=NO_LAZY_FETCH)
    return out.split()[1:] if out else []


def _is_ancestor(repo: str, a: str, b: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", a, b,
                env_extra=NO_LAZY_FETCH) is not None


def _remotes(repo: str) -> list[str]:
    """The declared remotes of `repo` (empty for a local-only repository)."""
    return (_git(repo, "remote") or "").split()


def _remote_branch(repo: str, ref: str | None) -> tuple[str, str] | None:
    """(remote, branch) named by `ref`, None for a commit SHA (#3038)."""
    text = (ref or "").strip()
    if not text or _SHA_RE.match(text):
        return None
    if text.startswith("refs/remotes/"):
        remote, _, branch = text[len("refs/remotes/"):].partition("/")
        if not remote or not branch:
            return None
    elif text.startswith("refs/heads/"):
        remote, branch = "origin", text[len("refs/heads/"):]
    elif text == "HEAD":
        remote, branch = "origin", "HEAD"
    elif "/" in text and text.partition("/")[0] in _remotes(repo):
        remote, _, branch = text.partition("/")
    else:
        remote, branch = "origin", text
    if branch == "HEAD":
        symbolic = (_git(repo, "symbolic-ref", "--quiet",
                         "refs/remotes/%s/HEAD" % remote) or "").strip()
        prefix = "refs/remotes/%s/" % remote
        if not symbolic.startswith(prefix) or len(symbolic) == len(prefix):
            return None
        branch = symbolic[len(prefix):]
    return remote, branch


def _remote_tip(repo: str, remote: str, branch: str) -> tuple[str, str | None]:
    """The published tip of `branch` on `remote`, read now with `ls-remote`
    (#3038), or `('local', None)` when the repository has no such remote: the
    clone is then the only reference.

    `('absent', None)` when the forge does not carry the branch, `('error',
    None)` when it cannot be read (network, credentials, timeout): the caller
    then doubts, never concludes. One read per (repo, remote, branch) and per
    measure pass, reused at most `PUBLISHED_TIP_TTL_S` seconds (#3056)."""
    key = (repo, remote, branch)
    cached = _PUBLISHED_TIPS.get(key)
    if cached is not None and time.monotonic() - cached[0] < PUBLISHED_TIP_TTL_S:
        return cached[1]
    if remote not in _remotes(repo):
        out = ("local", None)
    else:
        text = _git(repo, "ls-remote", "--heads", remote,
                    "refs/heads/" + branch, timeout=LS_REMOTE_TIMEOUT_S,
                    env_extra={"GIT_TERMINAL_PROMPT": "0"})
        words = (text or "").split()
        if text is None:
            out = ("error", None)
        elif not words:
            out = ("absent", None)
        elif len(words) >= 1 and _SHA_RE.match(words[0]):
            out = ("sha", words[0])
        else:
            out = ("error", None)
    _PUBLISHED_TIPS[key] = (time.monotonic(), out)
    return out


def _forge_unreadable(repos) -> bool:
    """True when a recent `ls-remote` of one of `repos` failed (#3056): a
    doubt read then is transient, not a fact about the lot."""
    now = time.monotonic()
    wanted = set(repos or ())
    return any(key[0] in wanted and now - read_at < PUBLISHED_TIP_TTL_S
               and out[0] == "error"
               for key, (read_at, out) in list(_PUBLISHED_TIPS.items()))


def _published_ref(repo: str, name: str) -> str | None:
    """The PUBLISHED ref that `name` designates (#3038), or None.

    `plan_git.published_target` maps `develop` to `origin/develop`; a local
    branch is never read when the clone has a remote: it can be days behind
    the forge (the #2999 measure, 654430 lines for 332)."""
    from . import plan_git

    ref = plan_git.published_target(repo, name)
    if not _git(repo, "rev-parse", "--verify", "-q", "%s^{commit}" % ref,
                env_extra=NO_LAZY_FETCH):
        return None
    local = (_git(repo, "rev-parse", "--symbolic-full-name", ref,
                  env_extra=NO_LAZY_FETCH) or "").strip().startswith("refs/heads/")
    if local and _remotes(repo):
        return None
    return ref


def _fresh_target(repo: str, ref: str) -> str | None:
    """The commit of `ref` to measure against when the merge is not in it, or
    None when that commit is not the published tip (#3038).

    `ref` may be a stale remote-tracking ref; `ls-remote` gives the forge tip
    read now. The local commit is kept when it is that tip, or the published
    one when the clone holds it; otherwise the tip is not fetched and the
    caller doubts instead of reading `merge-base` on an unknown base."""
    tip = (_git(repo, "rev-parse", "--verify", "-q", "%s^{commit}" % ref,
                env_extra=NO_LAZY_FETCH) or "").strip()
    if not tip:
        return None
    split = _remote_branch(repo, ref)
    if split is None:
        return tip                     # an explicit commit: nothing to confront
    kind, sha = _remote_tip(repo, split[0], split[1])
    if kind == "local":
        return tip                     # no remote: the clone is the reference
    if kind != "sha" or not sha:
        return None                    # unreadable or absent: doubt
    if sha == tip:
        return tip
    return sha if has_commit(repo, sha) else None


def _target(repo: str, target: str | None, sha: str) -> str | None:
    """The published integration ref to measure `sha` against (#3038).

    A target declared by the lot is authoritative: only its published ref is
    read, and it is returned even when it does not carry `sha` (a rebase or
    squash merge leaves no merge commit there). Without one, the repository's
    default target is tried, then the other published integration refs
    (`develop`, `main`, …, `release/*`), and the first that carries `sha`
    wins. When none carries it, the repository's DECLARED target
    (`ameesh.target`) is the only fallback — the branch the project says it
    integrates into; the clone's `origin/HEAD` alone (often `main` while the
    project integrates on `develop`) would measure against the wrong branch.
    None otherwise: the caller then doubts."""
    from . import plan_git

    explicit = str(target or "").strip()
    if explicit:
        return _published_ref(repo, explicit)
    names: list[str] = []
    try:
        default = plan_git.default_target(repo)
    except Exception:                  # cible déclarée introuvable : on essaie les refs
        default = None
    if default:
        names.append(default)
    try:
        names += plan_git.target_candidates(repo, default)
    except Exception:
        pass
    seen = set()
    for name in names:
        ref = _published_ref(repo, name)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        if _is_ancestor(repo, sha, ref):
            return ref
    declared = plan_git.declared_target(repo)
    return _published_ref(repo, declared) if declared else None


def _refresh_refs(repo: str, target: str | None) -> list[tuple[str, str]]:
    """The (remote, branch) to refresh for `target` (#3275): the declared
    target, then the repository's declared and default targets, which
    `lot_diff_range` also reads; only branches of a declared remote."""
    from . import plan_git

    remotes = _remotes(repo)
    out: list[tuple[str, str]] = []
    for name in (target, plan_git.declared_target(repo), "HEAD"):
        split = _remote_branch(repo, name) if name else None
        if split and split[0] in remotes and split not in out:
            out.append(split)
    return out


def refresh_target(repo: str, target: str | None, shas) -> str:
    """Fetch the exact target before a merge interval is read in `repo` (#3275).

    A shared clone is only as fresh as its last fetch: a merge pushed since
    is absent, or not yet in the remote-tracking ref of its target, and the
    interval reads « commit absent » although the merge is a fact (Nexlink
    #3268). When a commit of `shas` is absent or carried by no target ref,
    each target branch is fetched with an explicit refspec (a single-branch
    clone is updated too), then every full commit still absent is fetched by
    its SHA. A clone that already proves the interval is not fetched; a
    repository without remotes is its own reference.

    Best effort, never raises: returns "" when nothing was missing, else a
    short note of what was fetched or failed, for the caller's doubt."""
    wanted = [s for s in dict.fromkeys(str(s or "").strip() for s in shas or ())
              if _SHA_RE.match(s)]
    try:
        remotes = _remotes(repo)
        if not wanted or not remotes:
            return ""
        refs = _refresh_refs(repo, target)
        tracked = ["refs/remotes/%s/%s" % split for split in refs]

        def carried(sha: str) -> bool:
            return has_commit(repo, sha) and (
                not tracked or any(_is_ancestor(repo, sha, ref) for ref in tracked))

        if all(carried(sha) for sha in wanted):
            return ""
        env = {"GIT_TERMINAL_PROMPT": "0"}
        done, failed = [], []
        for remote, branch in refs:
            name = "%s/%s" % (remote, branch)
            out = _git(repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                       "--", remote,
                       "+refs/heads/%s:refs/remotes/%s" % (branch, name),
                       env_extra=env, timeout=FETCH_TIMEOUT_S)
            (failed if out is None else done).append(name)
        remote = refs[0][0] if refs else ("origin" if "origin" in remotes else remotes[0])
        # every branch fetch failed: the remote does not answer, the SHA
        # fetches would only wait for their own timeout
        for sha in ([] if failed and not done else wanted):
            if _FULL_SHA_RE.match(sha) and not has_commit(repo, sha):
                out = _git(repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                           "--", remote, sha, env_extra=env, timeout=FETCH_TIMEOUT_S)
                (failed if out is None else done).append("%s %s" % (remote, sha[:12]))
        parts = []
        if done:
            parts.append("fetch de %s" % ", ".join(done))
        if failed:
            parts.append("fetch impossible : %s" % ", ".join(failed))
        return "; ".join(parts) or "aucune cible à rafraîchir"
    except Exception as exc:  # noqa: BLE001 - best effort: the reading doubts
        return "rafraîchissement impossible : %s: %s" % (type(exc).__name__,
                                                        str(exc)[:120])


def diff_range(repo: str, sha: str, target: str | None = None) -> tuple[str, str] | None:
    """The (base, head) of the change merged by `sha`, the commit recorded by
    the `merged` milestone — a merge commit, the tip of the branch (merge by
    a merge commit), the frozen commit, or a squash commit:

    1. `sha` is a merge commit: first parent → sha;
    2. `sha` is in the target: the first merge commit of the target that
       brought it in through a second parent, first parent → that merge;
       none (squash or single commit on the first-parent chain): sha^1 → sha;
    3. `sha` is not in the target (merged by rebase, as a patch):
       merge-base(target, sha) → sha — but only when the target is the
       PUBLISHED tip (`_fresh_target`): on a stale ref that base would sweep
       in the history accumulated since it (#3038).
    None when the repository does not hold `sha`, when no readable target
    carries it and none is declared, or when the published tip cannot be
    proven: the measure then doubts and stores no size."""
    if not has_commit(repo, sha):
        return None
    empty = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"      # the empty tree
    parents = _parents(repo, sha)
    if len(parents) >= 2:
        return parents[0], sha
    tgt = _target(repo, target, sha)
    if tgt and not _is_ancestor(repo, sha, tgt):
        # the remote-tracking ref may be behind the forge (#3038): read the
        # published tip before concluding « merged by rebase » or doubting
        tgt = _fresh_target(repo, tgt)
        if tgt is None:
            return None
    if tgt and _is_ancestor(repo, sha, tgt):
        merges = (_git(repo, "rev-list", "--ancestry-path", "--merges", "--reverse",
                       "%s..%s" % (sha, tgt), env_extra=NO_LAZY_FETCH) or "").split()
        for merge in merges:
            first = _parents(repo, merge)[:1]
            if first and not _is_ancestor(repo, sha, first[0]):
                return first[0], merge
        return (parents[0] if parents else empty), sha
    if tgt:
        base = (_git(repo, "merge-base", tgt, sha,
                     env_extra=NO_LAZY_FETCH) or "").strip()
        return (base, sha) if base else None
    return None


class UnprovenLotDiff(ValueError):
    """The frozen lot cannot be tied to the observed integration merge."""


def lot_diff_range(repo: str, sha: str, target: str | None = None,
                   frozen: str | None = None) -> tuple[str, str] | None:
    """The (base, head) of the **lot's own** change brought by `sha` (#2374).

    `diff_range` reads what the merge made enter the target: a merge whose tree
    equals its first parent has no diff there, even though the lot changed
    files that another commit had already carried (a rebase, a cherry-pick, an
    adaptation merged first). Classifying that empty diff would say « light »
    without ever reading the lot.

    The lot's delta is `merge-base(first parent, lot head)..lot head`: the
    commit the merge brought in (its second parent), or `sha` itself when the
    merge record is the branch tip. None without a merge commit to anchor it —
    never a guess.

    `frozen` (#2702) : le gel du lot, quand il est connu. Une fusion de paquet
    porte plusieurs lots : son deuxième parent est la pointe du paquet, pas
    celle du lot, et le diff du merge couvre tout le paquet. Le gel donne la
    tête du lot (`merge-base(premier parent, gel)..gel`), soit la même lecture
    que le gel d'avant fusion ; sans gel porté par la fusion, le deuxième
    parent reste la tête lue (#2374).

    Un gel non ancêtre exige une preuve de contenu dans CETTE fusion
    (#3938) : `plan_git.probe` reconnaît les patch-ids verbatim (y compris
    squash) et refuse un contenu annulé. Sans preuve, `UnprovenLotDiff`
    (#2823) interdit toujours le repli sur le diff du paquet.

    Une cible locale périmée (#2702) ne porte pas `sha` : le repli
    `merge-base(cible, sha)..sha` emporterait tout l'historique entre les deux.
    La cible par défaut du dépôt (`origin/HEAD`) est donc essayée quand elle
    porte `sha`."""
    if not has_commit(repo, sha):
        return None
    merge = ""
    # A frozen lot may itself merge main for realignment. Its second parent
    # is then main, not the lot: find the later integration merge instead.
    if sha != (frozen or "").strip() and len(_parents(repo, sha)) >= 2:
        merge = sha
    else:
        tgt = _target(repo, target, sha)
        if tgt and not _is_ancestor(repo, sha, tgt):
            alt = _target(repo, None, sha)
            if alt and alt != tgt and _is_ancestor(repo, sha, alt):
                tgt = alt
        if tgt and _is_ancestor(repo, sha, tgt):
            merges = (_git(repo, "rev-list", "--ancestry-path", "--merges", "--reverse",
                           "%s..%s" % (sha, tgt), env_extra=NO_LAZY_FETCH) or "").split()
            for candidate in merges:
                first = _parents(repo, candidate)[:1]
                if first and not _is_ancestor(repo, sha, first[0]):
                    merge = candidate
                    break
    parents = _parents(repo, merge) if merge else []
    if len(parents) < 2:
        return None
    head = (frozen or "").strip()
    if head and not _is_ancestor(repo, head, merge):
        from . import plan_git
        try:
            # Le SHA de fusion borne la preuve : une copie entrée plus tard
            # dans la cible ne prouve pas le contenu de cette livraison.
            proof = plan_git.probe(repo, head, merge)
        except Exception:  # une preuve illisible ne permet aucun repli
            proof = {}
        if not proof.get("merged") or proof.get("how") not in ("patch-id", "squash"):
            raise UnprovenLotDiff(
                "gel %s non ancêtre de la fusion %s : delta du lot non prouvé"
                % (head[:12], merge[:12]))
    if head == merge:
        head = ""
    if head:
        base = (_git(repo, "merge-base", parents[0], head,
                     env_extra=NO_LAZY_FETCH) or "").strip()
        if base and base != head:
            return base, head
    base = (_git(repo, "merge-base", parents[0], parents[1],
                 env_extra=NO_LAZY_FETCH) or "").strip()
    return (base or parents[0], parents[1])


def diff_paths(repo: str, base: str, head: str) -> list[str] | None:
    """The paths changed between two commits, or None when the diff is not
    readable (the caller then doubts, never concludes « light »)."""
    text = _git(repo, "diff", "--name-only", "--no-renames", base, head)
    if text is None:
        return None
    return [line for line in text.splitlines() if line]


def diff_stat(repo: str, sha: str, target: str | None = None) -> dict | None:
    """The size of the change merged by `sha` (`diff_range`): `{diff_files,
    diff_insertions, diff_deletions, measured_size}`, or None when the
    repository does not hold the commit."""
    rng = diff_range(repo, sha, target)
    return None if rng is None else _range_stat(repo, rng)


def _range_stat(repo: str, rng: tuple[str, str]) -> dict | None:
    """The size of the proven range `rng`, or None when `git diff` cannot read
    it (an incomplete clone, a missing object): a read failure, not a doubt
    about the range (#3056)."""
    text = _git(repo, "diff", "--shortstat", "--no-renames", rng[0], rng[1])
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


def measure_one(db, cand: dict, *, repos=(), ci=gh_ci_runs,
                replace_size: bool = False) -> dict:
    """Measures one delivered lot (a row of `measure_candidates`) and stores
    it. `repos`: repositories where the merge commit may be; `ci(repo,
    branch)`: its CI runs, or None (`ci=None`: not read).

    A stored size is kept when this measure cannot read one (another host may
    hold what this one lacks). `replace_size` (a measure asked again by id):
    repositories hold the merge commit but none proves its range, the forge
    being readable — the stored size is cleared, never left to show a value
    the current rule refutes (#3056). A proven range whose `git diff` fails
    is a read failure: the size stays."""
    st = storage.of(db).work
    item_id = int(cand["id"])
    events = st.events(item_id, 1000)
    milestones = st.milestones(item_id, 1000)
    holding = [repo for repo in repos or ()
               if repo and os.path.isdir(repo) and cand.get("merge_sha")
               and has_commit(repo, cand["merge_sha"])]
    repo = holding[0] if holding else None
    # #3038 : le premier checkout qui porte le commit peut ne pas porter la
    # cible publiée ; un suivant le fait. On ne conclut au doute qu'après.
    stat = None
    # #3056 : la taille stockée n'est effacée que si AUCUN dépôt porteur ne
    # prouve de plage (cible publiée illisible ou absente) et que la forge a
    # répondu. Une plage prouvée dont `git diff` échoue est un échec de
    # lecture : la taille reste. Décidé ici, avant la lecture de la CI, dont
    # l'attente pourrait faire expirer une erreur ls-remote en cache.
    proven = False
    for candidate in holding:
        rng = diff_range(candidate, cand["merge_sha"], cand.get("branch_target"))
        if rng is None:
            continue
        proven = True
        stat = _range_stat(candidate, rng)
        if stat is not None:
            repo = candidate
            break
    size_doubt = (replace_size and bool(holding) and not proven
                  and not _forge_unreadable(holding))
    # the CI runs are read in the repository that holds the merge commit
    runs = ci(repo, cand.get("branch")) if ci and repo else None
    # #796 : les tours que le grand livre attribue au lot ; vide = inconnu
    # (lots travaillés avant la migration 0423), la chronologie d'états sert
    # alors de mesure.
    spans = [(r["start_ts"], r["end_ts"]) for r in st.turn_spans(item_id)
             if r.get("start_ts") is not None and r.get("end_ts") is not None]
    # #796 : le début du lot est celui du premier tour attribué (le marqueur
    # est posé avant le lancement, la ressource du tour juste après) — une
    # date déclarée tardivement, à la prise en main, est ainsi corrigée. Une
    # date déclarée plus tôt (le lot attendait avant son premier tour) est
    # gardée : le début retenu est le plus tôt connu.
    debut = float(cand["started_ts"])
    if spans:
        debut = min(debut, min(lo for lo, _ in spans))
    out = split(debut, cand["merged_ts"], events=events,
                milestones=milestones, ci_runs=runs, turns=spans or None)
    out.update(started_ts=debut, merged_ts=cand["merged_ts"],
               merge_sha=cand.get("merge_sha") or None,
               declared_size=cand.get("change_size") or None)
    if stat is not None:
        out.update(stat)
    elif size_doubt:
        out["size_doubt"] = True
    return st.save_measure(item_id, out)


def measure(db, ids=None, *, repos=(), ci=gh_ci_runs, limit: int = PASS_LIMIT,
            since_s: float | None = None, budget_s: float | None = None,
            errors: list | None = None) -> list[dict]:
    """Measures the given delivered lots (`ids`, measured again), or those
    still to measure (`ids` None; merged less than `since_s` ago when
    given), the most recent first. Returns the stored measures. A lot named
    in `ids` is measured again for good: a size that now doubts is cleared
    (`measure_one(replace_size=True)`, #3056).

    A lot that fails is skipped (`(id, exception)` appended to `errors`)
    and the next one measured; past `budget_s` seconds, the pass stops (the
    rest waits for the next pass)."""
    started = time.monotonic()
    _PUBLISHED_TIPS.clear()          # one read of each published tip per pass
    cands = storage.of(db).work.measure_candidates(
        ids=None if ids is None else [int(i) for i in ids], retry_diff_s=RETRY_DIFF_S,
        since_s=since_s, limit=limit)
    out = []
    for cand in cands:
        if budget_s is not None and time.monotonic() - started >= budget_s:
            break
        try:
            # a lot already measured is retried (diff, CI) only when a
            # repository holds its merge commit now
            if ids is None and cand["measured"] and not _repo_for(cand.get("merge_sha"),
                                                                  repos):
                continue
            out.append(measure_one(db, cand, repos=repos, ci=ci,
                                   replace_size=ids is not None))
        except Exception as exc:  # noqa: BLE001 - one lot never blocks the others
            if errors is not None:
                errors.append((int(cand["id"]), exc))
    return out


def host_repos(db, host: str | None, agents=None) -> list[str]:
    """The working directories (registry `cwd`) of the agents of `host`.

    `agents=None` keeps every agent of the host; a given filter (including an
    empty one) selects exactly its agents, so a runner admitting nobody has
    no repository — never "all agents" (#3972)."""
    from . import registry

    out = []
    for agent in registry.overview(db):
        if host and agent.get("host") != host:
            continue
        if agents is not None and agent.get("name") not in agents:
            continue
        repo = os.path.expanduser(agent.get("cwd") or "")
        if repo and os.path.isdir(repo) and repo not in out:
            out.append(repo)
    return out


def measure_pending(db, *, host: str | None = None, agents=None, ci=gh_ci_runs,
                    errors: list | None = None) -> list[dict]:
    """The runner pass: measures the lots delivered recently and not
    measured yet, within `PASS_BUDGET_S`.

    With no repository of the host — no admitted agent, no readable working
    directory — nothing is measured: the diff cannot be read, and a measure
    written without one would claim a repository this host does not have
    (#3972)."""
    repos = host_repos(db, host, agents)
    if not repos:
        return []
    return measure(db, None, repos=repos, ci=ci,
                   since_s=PENDING_WINDOW_S, budget_s=PASS_BUDGET_S, errors=errors)


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
        "diff_deletions", "measured_size", "declared_size", "agent_source")}
    out["size_match"] = (None if not out["measured_size"] or not out["declared_size"]
                         else out["measured_size"] == out["declared_size"])
    return out


def describe_phases(v: dict) -> str:
    """« agent 1 h 10 (turns) · CI 12 min · review 35 min · human 0 min ».

    Le temps d'agent porte sa source (#796) : `turns` = union des tours
    attribués au lot, `states` = reste de la chronologie d'états. Sans source
    connue (mesure d'avant 0423 non renseignée), rien n'est ajouté."""
    from . import estimates

    source = v.get("agent_source")
    parts = []
    for phase, name in (("agent", "agent"), ("ci", "CI"), ("review", "review"),
                        ("human", "human")):
        value = v.get("%s_minutes" % phase)
        text = ("unknown" if value is None
                else estimates.label(value) if value else "0 min")
        if phase == "agent" and source in ("turns", "states"):
            text += " (%s)" % source
        parts.append("%s %s" % (name, text))
    return " · ".join(parts)


def describe_size(v: dict) -> str:
    """« declared m · measured l (612 lines, 9 files) — differs ».

    Sans taille, le doute dit sa cause (#3038) : commit de fusion introuvable
    dans les dépôts de l'hôte, ou cible publiée illisible / non récupérée."""
    declared = v.get("declared_size") or "—"
    if v.get("measured_size") is None:
        if v.get("merge_sha"):
            return ("declared %s · measured: unknown (published target "
                    "unreadable or not fetched)" % declared)
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
