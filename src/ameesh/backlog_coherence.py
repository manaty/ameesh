# SPDX-License-Identifier: AGPL-3.0-only
"""#4172 — backlog coherence pass (manaty/ameesh#3185, volet 2).

The intake check of #2988 (`intake_neighbors`) compares a **new** lot with the
lots already known. This module runs the same idea over **every open lot** of a
project, periodically:

* **already done** — a merged change in the project's base ref references the
  lot (`ameesh-work: <id>`, a `lot/<id>-…` merge);
* **probably done** — a merged change touches the code the lot cites (same
  file and line, or the whole file when no line is given): a touch is not a
  proof that the lot's ask was met, so it goes to the digest, never closed;
* **probably obsolete** — every path the lot cites is absent from the base ref
  and at least one of them was removed by a merged change: an absence is not a
  proof that the request is obsolete (a restoration or data-recovery request
  can follow a deletion), so it goes to the digest;
* **duplicate** — two open lots of the project carry the same normalised title
  (or the same non-empty issue reference and at least two significant title
  words in common: one issue can legitimately be split into several lots);
* **contradiction** — two open lots cite the same path and their titles start
  with opposite verbs (add/enable/keep versus remove/disable/delete).

Only `already done` is **deterministic**: the pass closes it itself (`closed`,
reason `done`), with the proof written in the lot's journal. Duplicates,
contradictions and probably-done/obsolete lots are **never** closed here: they
go into **one digest per project** addressed to the project's PO
(`notify.default_human` when the canon has no responsible package), so the PO
can answer with a typed link (`duplicates`, `conflicts_with`) or a justified
closure.

Command::

  ameesh work coherence [--project P] [--repo DIR] [--apply] [--json]

Without `--apply` the command is read-only (the default). The **coherence**
pass of `ameesh notify` runs it under the elected role lease, through
`ameesh role run`; the full notifier never runs it. Its timer wakes every six
hours (`role_timers.COHERENCE_INTERVAL_S`).

Safety rules, learned from the false positives of an intake check:

* a clone is never fetched; without a readable clone the git checks are said
  "non vérifié" and only the textual duplicate/contradiction checks run;
* a lot with a branch or a PR is never closed: its work may be real, so the
  finding is left to the PO;
* only `intake` and `blocked` lots are closed (never `build`, `qa` or
  `waiting_human`);
* the digest is idempotent: its body is stable while the findings do not
  change, so a periodic timer does not repeat the same message every six
  hours (`mail.send` `idempotency_key`).
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from . import canon, intake_neighbors, mail, plan_git, storage, work

#: schema of the JSON report (`ameesh work coherence --json`)
SCHEMA = "ameesh-backlog-coherence/1"

#: the mechanical actor of the closures and of the digest
ACTOR = work.SYSTEM_SENDER

#: never scan more lots of one project in a pass (a backlog is a backlog)
MAX_LOTS = 2000
#: never read more commits of the base ref in a pass
MAX_COMMITS = 5000
#: bound of the base-ref read for the references; a commit that references a
#: lot cannot predate it by much (ids are attributed before the commit)
SLACK_S = 300.0
#: git calls are bounded; `git log -L` is the expensive one
GIT_TIMEOUT = 30.0
#: the pass closes only lots that have not started
CLOSABLE_STATES = ("intake", "blocked")

#: verbs that ask for a presence (a lot whose title starts with one of these
#: and another whose title starts with the opposite verb contradict each other)
POSITIVE_VERBS = frozenset("""
add ajoute ajouter create cree creer implement implemente implementer
introduce introduis introduire enable active activer allow autorise
autoriser support supporte supporter keep garde garder conserver preserver
restore restaure restaurer retablir remettre recreer rajouter reintegrer
maintenir
""".split())
#: verbs that ask for an absence
NEGATIVE_VERBS = frozenset("""
remove retire retirer delete supprime supprimer drop disable desactive
desactiver desactive forbid interdis interdire prevent empeche empecher
avoid evite eviter reject refuse refuser stop arrete arreter without sans
""".split())
#: a negated positive verb ("ne pas ajouter") counts as a negative
_NEGATION = frozenset(("ne", "pas", "not", "no", "never", "jamais", "sans"))


class CoherenceError(ValueError):
    """The pass refuses explicitly (actionable message)."""


# --------------------------------------------------------------------------
# git, bounded and read-only
# --------------------------------------------------------------------------

def _git(repo: str, *args: str, timeout: float = GIT_TIMEOUT):
    return canon._git(repo, *args, env_extra=canon.NO_LAZY_FETCH, timeout=timeout)


def _records(out: str, *, with_message: bool = True) -> list[dict]:
    """Parse the `%x1e%H%x00<ct>%x00<s>%x00[%B%x00]` log format."""
    records: list[dict] = []
    for chunk in str(out or "").split("\x1e"):
        chunk = chunk.lstrip("\n")
        if not chunk:
            continue
        fields = chunk.split("\x00", 3)
        if len(fields) < 2:
            continue
        sha = fields[0].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            continue
        try:
            moment = int(float(fields[1]))
        except (TypeError, ValueError):
            moment = 0
        if with_message:
            if len(fields) < 3:
                continue
            subject, message = fields[2].strip(), fields[3] if len(fields) > 3 else ""
        else:
            subject, message = (fields[2].strip() if len(fields) > 2 else ""), ""
        records.append({"sha": sha, "ts": moment, "subject": subject,
                        "message": message})
    return records


def _target_commits(repo: str, target: str, since: float) -> list[dict]:
    """The base-ref commits since `since` (bounded by MAX_COMMITS)."""
    try:
        proc = _git(repo, "log", "--format=%x1e%H%x00%ct%x00%s%x00%B%x00",
                    "--since=%d" % max(0, int(since)),
                    "--max-count=%d" % MAX_COMMITS, target)
    except Exception:  # noqa: BLE001 - an unreadable history is not a finding
        return []
    if proc.returncode != 0:
        return []
    return _records(proc.stdout.decode("utf-8", "replace"))


def _cited_by_lot(repo: str, commits: list[dict]) -> dict[int, dict]:
    """`{lot: commit}` for the commits of the base ref that designate a lot."""
    try:
        hash_refs = plan_git.lot_ref_hash(repo)
    except Exception:  # noqa: BLE001 - configuration illisible
        hash_refs = False
    found: dict[int, dict] = {}
    for commit in commits:
        try:
            lots = plan_git.cited_lots(commit.get("message") or "",
                                       hash_refs=hash_refs)
        except Exception:  # noqa: BLE001 - an unreadable message cites nothing
            continue
        for lot in lots:
            found.setdefault(int(lot), commit)
    return found


def _deletion(repo: str, target: str, path: str) -> Optional[dict]:
    """The most recent commit of `target` that deleted `path`, or None."""
    try:
        proc = _git(repo, "log",
                    "--format=%x1e%H%x00%ct%x00%s%x00",
                    "--diff-filter=D", "--no-renames", target, "--", path)
    except Exception:  # noqa: BLE001 - a history error proves nothing
        return None
    if proc.returncode != 0:
        return None
    records = _records(proc.stdout.decode("utf-8", "replace"), with_message=False)
    return records[0] if records else None


def _deleted_index(repo: str, target: str) -> dict[str, list[tuple[str, dict]]]:
    """Deleted paths of `target`, indexed by basename.

    A lot often cites only a basename (`mobile-build.yml:121`); when the file is
    gone from the base ref, the exact path cannot be resolved and a per-path
    `git log -- path` would miss the deletion. One bounded `--name-only` walk
    gives the basename index, used only when the cited path has no directory.
    """
    try:
        proc = _git(repo, "log", "--format=%x1e%H%x00%ct%x00%s%x00",
                    "--diff-filter=D", "--no-renames", "--name-only", target)
    except Exception:  # noqa: BLE001 - a history error proves nothing
        return {}
    if proc.returncode != 0:
        return {}
    index: dict[str, list[tuple[str, dict]]] = {}
    for chunk in proc.stdout.decode("utf-8", "replace").split("\x1e"):
        chunk = chunk.lstrip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        if not lines:
            continue
        header = lines[0].split("\x00")
        if len(header) < 3:
            continue
        sha = header[0].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            continue
        try:
            moment = int(float(header[1]))
        except (TypeError, ValueError):
            moment = 0
        commit = {"sha": sha, "ts": moment, "subject": header[2].strip()}
        for path in lines[1:]:
            path = path.strip()
            if not path:
                continue
            index.setdefault(path.rsplit("/", 1)[-1], []).append((path, commit))
    return index


def _tree_listing(repo: str, target: str) -> list[str]:
    try:
        proc = _git(repo, "ls-tree", "-r", "--name-only", target)
    except Exception:  # noqa: BLE001 - no tree, no move test
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.decode("utf-8", "replace").splitlines()


def _code_hit(lot: dict, repo: str, target: str, since: float,
              trees: dict) -> Optional[dict]:
    """A merged change of `target` touching the code the lot cites."""
    text = " ".join([str(lot.get("title") or ""), str(lot.get("body") or "")])
    refs = intake_neighbors.cited_paths(text)
    # A line is the precise claim; a bare path in the title is only a fallback
    # when the lot cites no line at all. Mixing both would let a commit that
    # touches another line of the same file close the lot.
    with_lines = [ref for ref in refs if ref.get("line")]
    for ref in (with_lines or refs):
        git_path = ref.get("git_path") or ref["path"]
        path = intake_neighbors._resolve_git_path(repo, target, git_path, trees)
        if not path:
            continue
        line = ref.get("line")
        if line:
            span = "%d,%d:%s" % (int(line), max(int(ref.get("end") or line),
                                                 int(line)), path)
            try:
                proc = _git(repo, "log", "--format=%x1e%H%x00%ct%x00%s%x00",
                            "--since=%d" % max(0, int(since)), "-L" + span,
                            target)
            except Exception:  # noqa: BLE001 - a line we cannot follow is no proof
                continue
        else:
            try:
                proc = _git(repo, "log", "--format=%x1e%H%x00%ct%x00%s%x00",
                            "--since=%d" % max(0, int(since)), target, "--", path)
            except Exception:  # noqa: BLE001 - a file we cannot follow is no proof
                continue
        if proc.returncode != 0:
            continue
        records = _records(proc.stdout.decode("utf-8", "replace"),
                           with_message=False)
        if records:
            hit = records[0]
            return {"sha": hit["sha"], "subject": hit["subject"],
                    "ts": hit["ts"], "path": path,
                    "line": int(line) if line else None}
    return None


def _obsolete(lot: dict, repo: str, target: str,
              trees: dict, listings: dict,
              deleted_cache: Optional[dict] = None) -> Optional[dict]:
    """Every cited path is gone from `target` and at least one was deleted.

    This is evidence only: the caller reports it as `probably_obsolete` for the
    PO, never as a deterministic closure (#4172 B1 dev2/cx2/cx1).
    """
    text = " ".join([str(lot.get("title") or ""), str(lot.get("body") or "")])
    refs = intake_neighbors.cited_paths(text)
    if not refs:
        return None
    missing: list[dict] = []
    for ref in refs:
        git_path = ref.get("git_path") or ref["path"]
        if intake_neighbors._resolve_git_path(repo, target, git_path, trees):
            return None
        missing.append(ref)
    # A same-basename file elsewhere is a move, not an obsolescence: leave it
    # to the PO (a wrong closure of a moved component would lose the lot).
    listing = listings.get(target)
    if listing is None:
        listing = _tree_listing(repo, target)
        listings[target] = listing
    basenames = {value.rsplit("/", 1)[-1] for value in listing}
    for ref in missing:
        if (ref.get("git_path") or ref["path"]).rsplit("/", 1)[-1] in basenames:
            return None
    removed = []
    basename_deleted = None
    for ref in missing:
        path = ref.get("git_path") or ref["path"]
        commit = _deletion(repo, target, path)
        if commit:
            removed.append((path, commit))
            continue
        if "/" in path:
            continue
        # A basename-only citation: same-basename deletions are indexed once
        # per project. More than one deleted path with that basename is
        # ambiguous; the lot is left open.
        if deleted_cache is not None and target not in deleted_cache:
            deleted_cache[target] = _deleted_index(repo, target)
        index = (deleted_cache or {}).get(target) or {}
        matches = index.get(path, ())
        candidates = {candidate for candidate, _commit in matches}
        if len(candidates) == 1:
            basename_deleted = matches[0]
    if not removed and basename_deleted is None:
        return None
    if removed:
        path, commit = removed[0]
        paths = list(dict.fromkeys(item[0] for item in removed))
    else:
        path, commit = basename_deleted
        paths = [path]
    return {"sha": commit["sha"], "subject": commit["subject"],
            "ts": commit["ts"], "path": path, "paths": paths}


# --------------------------------------------------------------------------
# textual findings: duplicates and contradictions
# --------------------------------------------------------------------------

def _fold_title(title: str) -> str:
    return intake_neighbors._fold(title)


def _title_words(title: str) -> list[str]:
    return _fold_title(title).split()


def _polarity(title: str) -> tuple[int, str]:
    """`(1|0|-1, verb)` for a title whose first action verb is clear."""
    words = _title_words(title)
    for index, word in enumerate(words):
        if word in POSITIVE_VERBS:
            before = set(words[max(0, index - 2):index])
            return (-1 if before & _NEGATION else 1), word
        if word in NEGATIVE_VERBS:
            return -1, word
    return 0, ""


def _duplicate_findings(lots: list[dict]) -> list[dict]:
    findings: list[dict] = []
    seen: set[int] = set()
    groups: dict[str, list[dict]] = {}
    for lot in lots:
        title = _fold_title(str(lot.get("title") or ""))
        if len(intake_neighbors.significant_words(str(lot.get("title") or ""))) < 2:
            continue
        groups.setdefault(title, []).append(lot)
    for title, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda row: int(row["id"]))
        canonical = group[0]
        for lot in group[1:]:
            seen.add(int(lot["id"]))
            findings.append({
                "kind": "duplicate", "deterministic": False,
                "lot": int(lot["id"]), "other": int(canonical["id"]),
                "title": lot.get("title") or "",
                "other_title": canonical.get("title") or "",
                "evidence": "same normalised title (%r)" % title,
            })
    by_issue: dict[str, list[dict]] = {}
    for lot in lots:
        issue = str(lot.get("issue_ref") or "").strip()
        if issue:
            by_issue.setdefault(issue, []).append(lot)
    for issue, group in by_issue.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda row: int(row["id"]))
        for index, lot in enumerate(group):
            if int(lot["id"]) in seen:
                continue
            words = intake_neighbors.significant_words(str(lot.get("title") or ""))
            for canonical in group[:index]:
                common = words & intake_neighbors.significant_words(
                    str(canonical.get("title") or ""))
                if len(common) < 2:
                    continue
                seen.add(int(lot["id"]))
                findings.append({
                    "kind": "duplicate", "deterministic": False,
                    "lot": int(lot["id"]), "other": int(canonical["id"]),
                    "title": lot.get("title") or "",
                    "other_title": canonical.get("title") or "",
                    "evidence": "same issue reference (%s), close titles (%s)"
                                % (issue, ", ".join(sorted(common))),
                })
                break
    return findings


def _contradiction_findings(lots: list[dict]) -> list[dict]:
    findings: list[dict] = []
    polarities = {int(row["id"]): _polarity(str(row.get("title") or ""))
                  for row in lots}
    paths = {}
    for row in lots:
        text = " ".join([str(row.get("title") or ""),
                         str(row.get("body") or "")])
        paths[int(row["id"])] = {ref["path"]
                                 for ref in intake_neighbors.cited_paths(text)}
    for index, one in enumerate(lots):
        one_id = int(one["id"])
        one_pol, one_verb = polarities[one_id]
        if not one_pol:
            continue
        for other in lots[index + 1:]:
            other_id = int(other["id"])
            other_pol, other_verb = polarities[other_id]
            if not other_pol or other_pol == one_pol:
                continue
            shared = sorted(paths[one_id] & paths[other_id])
            if not shared:
                continue
            findings.append({
                "kind": "contradiction", "deterministic": False,
                "lot": one_id, "other": other_id,
                "title": one.get("title") or "",
                "other_title": other.get("title") or "",
                "path": shared[0], "paths": shared,
                "evidence": ("opposite behaviour on %s (%s vs %s)"
                             % (shared[0], one_verb, other_verb)),
            })
    return findings


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

def _open_lots(db, project: str) -> list[dict]:
    return [dict(row) for row in db.query("""
        SELECT id, title, body, state, app, assignee, branch, branch_target,
               pr_ref, issue_ref,
               extract(epoch FROM created_at)::float8 AS created_ts
          FROM work_items
         WHERE app = %s AND state NOT IN ('merged', 'promoted', 'closed')
         ORDER BY id
         LIMIT %s""", (project, MAX_LOTS))]


def projects(db) -> list[str]:
    """The projects that still have at least one open lot, sorted."""
    return [str(row["app"]) for row in db.query("""
        SELECT DISTINCT app FROM work_items
         WHERE state NOT IN ('merged', 'promoted', 'closed')
           AND coalesce(app, '') <> ''
         ORDER BY app""")]


def _base(repo: Optional[str]) -> tuple[str, str]:
    if not repo:
        return "", ""
    try:
        target = intake_neighbors.default_target(repo)
    except Exception:  # noqa: BLE001 - an unreadable target is said unverified
        return "", ""
    if not target:
        return "", ""
    sha = intake_neighbors.resolve_head(repo, target) or ""
    return target, sha


def scan(db, project: str, *, cfg=None, repo: Optional[str] = None,
         now: Optional[float] = None) -> dict:
    """Read-only coherence report for the open lots of `project`.

    The result is a JSON-ready dict:
    `{schema, project, git, base, base_sha, open_lots, findings, closures,
    digest}`. `findings` carries every finding, deterministic or not; `apply`
    is the only writer.
    """
    project = str(project or "").strip()
    report = {"schema": SCHEMA, "project": project, "git": "",
              "base": "", "base_sha": "", "open_lots": 0,
              "findings": [], "closures": [],
              "digest": {"sent": False}}
    if not project:
        raise CoherenceError("a project is required")
    lots = _open_lots(db, project)
    report["open_lots"] = len(lots)
    if not lots:
        return report
    clone = None
    try:
        clone = (intake_neighbors.resolve_clone(cfg, project, repo)
                 if (cfg is not None or repo) else None)
    except Exception:  # noqa: BLE001 - no clone, textual checks only
        clone = None
    target, base_sha = _base(clone)
    report["base"], report["base_sha"] = target, base_sha
    report["git"] = "vérifié" if (clone and target) else "non vérifié"

    referenced: dict[int, dict] = {}
    commits: list[dict] = []
    trees: dict = {}
    listings: dict = {}
    deleted_cache: dict = {}
    if report["git"] == "vérifié":
        oldest = min(float(row.get("created_ts") or 0) for row in lots)
        commits = _target_commits(clone, target, oldest - SLACK_S)
        referenced = _cited_by_lot(clone, commits)

    by_lot: dict[int, dict] = {}
    for lot in lots:
        lot_id = int(lot["id"])
        declared = str(lot.get("branch_target") or "").strip()
        if declared and target and declared.rsplit("/", 1)[-1] != target.rsplit("/", 1)[-1]:
            # The lot declares another base: a change in the project's default
            # base proves nothing for it. Leave it to the pass run on its base.
            continue
        # A code touch is a deterministic proof only when the commit is
        # strictly newer than the lot: a same-second commit (the base commit
        # that added the file) is not a fix for a lot filed after it.
        moment = float(lot.get("created_ts") or 0) + 1.0
        finding = None
        commit = referenced.get(lot_id)
        # A `#<id>` older than the lot names something else (an issue, a PR).
        if commit and float(commit.get("ts") or 0) < moment - 1.0 - SLACK_S:
            commit = None
        if commit:
            finding = {
                "kind": "already_done", "deterministic": True,
                "lot": lot_id, "title": lot.get("title") or "",
                "sha": commit["sha"], "subject": commit["subject"],
                "evidence": ("merged change %s %r references the lot"
                             % (commit["sha"][:12], commit["subject"])),
            }
        elif report["git"] == "vérifié":
            obsolete = _obsolete(lot, clone, target, trees, listings,
                                 deleted_cache)
            if obsolete:
                # #4172 (B1 dev2/cx2/cx1): a deletion is not a proof that the
                # request is obsolete. A restoration or data-recovery request
                # can be filed after the deletion; a lexical verb list cannot
                # decide. Every absence/removal therefore goes to the digest
                # as `probably_obsolete`; only the explicit citation of the
                # lot (already_done) closes here.
                finding = {
                    "kind": "probably_obsolete", "deterministic": False,
                    "lot": lot_id, "title": lot.get("title") or "",
                    "sha": obsolete["sha"], "subject": obsolete["subject"],
                    "path": obsolete["path"], "paths": obsolete["paths"],
                    "evidence": ("%s is absent at %s and was removed by %s %r; "
                                 "a deletion does not prove the request is "
                                 "obsolete"
                                 % (obsolete["path"], target,
                                    obsolete["sha"][:12],
                                    obsolete["subject"])),
                }
            else:
                hit = _code_hit(lot, clone, target, moment, trees)
                if hit:
                    where = hit["path"] + (":%d" % hit["line"] if hit["line"] else "")
                    # Review #4172: a touch of the cited code is a hint, not
                    # a proof (an unrelated change of a cited file would close
                    # the lot); the PO confirms it.
                    finding = {
                        "kind": "probably_done", "deterministic": False,
                        "lot": lot_id, "title": lot.get("title") or "",
                        "sha": hit["sha"], "subject": hit["subject"],
                        "path": hit["path"], "line": hit["line"],
                        "evidence": ("merged change %s %r touches %s in %s"
                                     % (hit["sha"][:12], hit["subject"],
                                        where, target)),
                    }
        if finding:
            by_lot[lot_id] = finding

    findings = list(by_lot.values())
    findings.extend(_duplicate_findings(lots))
    findings.extend(_contradiction_findings(lots))
    findings.sort(key=lambda row: (str(row.get("kind") or ""),
                                   int(row.get("lot") or 0),
                                   int(row.get("other") or 0)))
    report["findings"] = findings
    return report


def _note(finding: dict, report: dict) -> str:
    return ("coherence: already done — %s (base %s, %s)"
            % (finding.get("evidence") or "merged change",
               report.get("base") or "?",
               (report.get("base_sha") or "?")[:12]))


def _po_of(db, cfg, project: str) -> Optional[str]:
    """The project's PO: a responsible WorkPackage, else the default human."""
    try:
        packages = storage.of(db).packages.all()
    except Exception:  # noqa: BLE001 - a package read is not the pass
        packages = []
    for package in packages:
        if package.get("team") != project and package.get("id") != project:
            continue
        try:
            from .notify import normalize_human
            human = normalize_human(package.get("responsible"))
        except Exception:  # noqa: BLE001 - an unreadable responsible
            human = None
        if human:
            return human
    try:
        from .notify import normalize_human, parse_config
        human = normalize_human(parse_config(getattr(cfg, "notify", None)).default_human)
        if human:
            return human
    except Exception:  # noqa: BLE001 - no notify configuration
        pass
    try:
        names = sorted(cfg.human_names)
        if len(names) == 1:
            from .notify import normalize_human
            return normalize_human(names[0])
    except Exception:  # noqa: BLE001 - no declared human
        pass
    return None


def _open(item: dict) -> bool:
    return str(item.get("state") or "") not in ("merged", "promoted", "closed")


def _refused(finding: dict, reason: str) -> dict:
    return {"lot": int(finding["lot"]), "kind": finding.get("kind") or "",
            "result": "refused", "reason": reason}


def apply_report(db, report: dict, *, cfg=None, actor: str = ACTOR,
                 digest: bool = True) -> dict:
    """Apply the deterministic closures and send the one digest.

    Only `intake`/`blocked` lots without a branch or a PR are closed. Every
    closure (or refusal) is journaled in the report; the digest is sent only
    for the non-deterministic findings and for deterministic findings whose
    closure was refused. The call is idempotent: a second pass over the same
    findings sends no second message (the body is stable and `mail.send`
    deduplicates on its `idempotency_key`).
    """
    project = report.get("project") or ""
    actor = (str(actor or "").strip() or ACTOR)
    report.setdefault("closures", [])
    report.setdefault("digest", {"sent": False})
    refused: list[dict] = []
    for finding in report.get("findings") or ():
        if not finding.get("deterministic"):
            continue
        lot_id = int(finding["lot"])
        item = work.get(db, lot_id)
        if item is None:
            entry = _refused(finding, "lot not found")
            refused.append(entry)
            report["closures"].append(entry)
            continue
        state = str(item.get("state") or "")
        if state not in CLOSABLE_STATES:
            entry = _refused(finding, "state %s is not closable" % state)
            refused.append(entry)
            report["closures"].append(entry)
            continue
        if str(item.get("branch") or "").strip() or str(item.get("pr_ref") or "").strip():
            entry = _refused(finding, "lot carries a branch or a PR")
            refused.append(entry)
            report["closures"].append(entry)
            continue
        if finding["kind"] != "already_done":
            entry = _refused(finding, "deterministic kind not closable")
            refused.append(entry)
            report["closures"].append(entry)
            continue
        try:
            work.close(db, lot_id, done=True, note=_note(finding, report),
                       actor=actor)
            reason = "done"
        except work.WorkError as exc:
            entry = _refused(finding, str(exc))
            refused.append(entry)
            report["closures"].append(entry)
            continue
        report["closures"].append({
            "lot": lot_id, "kind": finding["kind"], "result": "closed",
            "reason": reason, "sha": finding.get("sha") or "",
        })

    pending = []
    for finding in report.get("findings") or ():
        if finding.get("deterministic"):
            continue
        lot = work.get(db, int(finding["lot"]))
        other = work.get(db, int(finding["other"])) if finding.get("other") else None
        if lot is None or not _open(lot):
            continue
        if other is not None and not _open(other):
            continue
        pending.append(finding)
    if refused:
        pending.extend(refused)
    if not digest or not pending:
        return report
    recipient = _po_of(db, cfg, project)
    if not recipient:
        report["digest"] = {"sent": False, "error": "no PO found for %s" % project}
        return report
    body = format_digest(project, pending, report)
    key = "coherence:%s:%s" % (
        re.sub(r"[^A-Za-z0-9._-]+", "-", project) or "project",
        hashlib.sha256(body.encode("utf-8")).hexdigest()[:32])
    try:
        message_id = mail.send(
            db, actor, recipient, body, kind="event",
            payload={"coherence": {"project": project,
                                   "findings": len(pending)}},
            idempotency_key=key)
    except Exception as exc:  # noqa: BLE001 - the closures stay, the digest is reported
        report["digest"] = {"sent": False, "error": str(exc)[:300],
                            "recipient": recipient}
        return report
    report["digest"] = {"sent": True, "message_id": int(message_id),
                        "recipient": recipient, "findings": len(pending)}
    return report


def format_digest(project: str, findings: list[dict], report: dict) -> str:
    """The one digest of a project. Its body is stable while findings are."""
    lines = ["Passe de cohérence du backlog — %s" % project]
    if report.get("base"):
        lines.append("Base : %s." % report["base"])
    lines.append("")
    duplicates = [row for row in findings if row.get("kind") == "duplicate"]
    contradictions = [row for row in findings
                      if row.get("kind") == "contradiction"]
    refusals = [row for row in findings if row.get("result") == "refused"]
    probable = [row for row in findings if row.get("kind") == "probably_done"]
    if probable:
        lines.append("Déjà faits probables à confirmer (%d) :" % len(probable))
        for row in probable:
            lines.append("- #%d « %s » — %s."
                         % (int(row["lot"]), row.get("title") or "",
                            row.get("evidence") or ""))
        lines.append("")
    probable_obsolete = [row for row in findings
                         if row.get("kind") == "probably_obsolete"]
    if probable_obsolete:
        lines.append("Obsolètes probables à confirmer (%d) :"
                     % len(probable_obsolete))
        for row in probable_obsolete:
            lines.append("- #%d « %s » — %s."
                         % (int(row["lot"]), row.get("title") or "",
                            row.get("evidence") or ""))
        lines.append("")
    if duplicates:
        lines.append("Doublons à confirmer (%d) :" % len(duplicates))
        for row in duplicates:
            if row.get("result") == "refused":
                continue
            lines.append("- #%d « %s » et #%d « %s » — %s."
                         % (int(row["lot"]), row.get("title") or "",
                            int(row["other"]), row.get("other_title") or "",
                            row.get("evidence") or ""))
        lines.append("")
    if contradictions:
        lines.append("Contradictions à trancher (%d) :" % len(contradictions))
        for row in contradictions:
            if row.get("result") == "refused":
                continue
            lines.append("- #%d « %s » et #%d « %s » — %s."
                         % (int(row["lot"]), row.get("title") or "",
                            int(row["other"]), row.get("other_title") or "",
                            row.get("evidence") or ""))
        lines.append("")
    if refusals:
        lines.append("Fermetures déterministes refusées (%d) :" % len(refusals))
        for row in refusals:
            lines.append("- #%d (%s) : %s."
                         % (int(row["lot"]), row.get("kind") or "?",
                            row.get("reason") or "?"))
        lines.append("")
    lines.append("Les fermetures déterministes éventuelles (déjà fait, obsolète) "
                 "sont justifiées dans le journal de chaque lot.")
    lines.append("Ces constats ne sont pas fermés d'office : répondez par un lien "
                 "typé (`ameesh work links <lot> --add <autre> --kind "
                 "duplicates|conflicts_with`) ou par une clôture motivée.")
    return "\n".join(lines).strip() + "\n"


def summary_line(report: dict) -> str:
    """One log line for a project report."""
    if report.get("error"):
        return "coherence %s: %s" % (report.get("project") or "?", report["error"])
    findings = report.get("findings") or []
    deterministic = sum(1 for row in findings if row.get("deterministic"))
    other = len(findings) - deterministic
    closed = sum(1 for row in report.get("closures") or ()
                 if row.get("result") == "closed")
    digest = report.get("digest") or {}
    sent = "digest sent" if digest.get("sent") else (
        "digest error: %s" % digest.get("error") if digest.get("error")
        else "no digest")
    return ("coherence %s: %d open lot(s), %d deterministic finding(s), "
            "%d other, %d closed, %s"
            % (report.get("project") or "?", int(report.get("open_lots") or 0),
               deterministic, other, closed, sent))


def tick(cfg, db, *, now: Optional[float] = None, apply: bool = True,
         project: Optional[str] = None, repo: Optional[str] = None,
         actor: str = ACTOR, log=None) -> list[dict]:
    """Scan (and optionally apply) every project, or one named project."""
    selected = [project] if project else projects(db)
    out: list[dict] = []
    for name in selected:
        try:
            report = scan(db, name, cfg=cfg, repo=repo, now=now)
            if apply:
                report = apply_report(db, report, cfg=cfg, actor=actor)
        except Exception as exc:  # noqa: BLE001 - one project never hides another
            report = {"schema": SCHEMA, "project": name, "error":
                      "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                      "findings": [], "closures": [], "digest": {"sent": False}}
        out.append(report)
        if log is not None:
            log(summary_line(report))
    return out


def format_report(report: dict) -> str:
    """Human-readable form of one project report (CLI)."""
    if report.get("error"):
        return "coherence %s: %s" % (report.get("project") or "?", report["error"])
    lines = ["coherence %s — %d open lot(s), git %s%s"
             % (report.get("project") or "?",
                int(report.get("open_lots") or 0),
                report.get("git") or "non vérifié",
                " (base %s)" % report["base"] if report.get("base") else "")]
    for finding in report.get("findings") or ():
        label = {"already_done": "already done", "obsolete": "obsolete",
                 "probably_done": "probably done",
                 "probably_obsolete": "probably obsolete",
                 "duplicate": "duplicate",
                 "contradiction": "contradiction"}.get(
                     finding.get("kind"), finding.get("kind") or "?")
        if finding.get("other"):
            lines.append("  %-13s #%d vs #%d — %s"
                         % (label, int(finding["lot"]), int(finding["other"]),
                            finding.get("evidence") or ""))
        else:
            lines.append("  %-13s #%d — %s"
                         % (label, int(finding["lot"]),
                            finding.get("evidence") or ""))
    for closure in report.get("closures") or ():
        lines.append("  %-13s #%d — %s (%s)"
                     % (closure.get("result") or "?", int(closure["lot"]),
                        closure.get("reason") or "?",
                        closure.get("kind") or "?"))
    digest = report.get("digest") or {}
    if digest.get("sent"):
        lines.append("  digest        %s (message %s)"
                     % (digest.get("recipient") or "?",
                        digest.get("message_id")))
    elif digest.get("error"):
        lines.append("  digest        refusé — %s" % digest["error"])
    return "\n".join(lines)
