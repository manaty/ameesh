# SPDX-License-Identifier: AGPL-3.0-only
"""Release at the canon cutoffs, without an agent (#2448, canon §11 bis/§14).

At each compiled cutoff (`mechanics.cutoffs`, 11:00/23:00 Europe/Paris when the
canon declares them), the tool takes `B`: the target head when its CI is green,
otherwise the most recent green first-parent commit (a release `[skip ci]`
commit inherits its first parent's state). It writes the version and the
release notes generated from the packet manifests on `V`, a `[skip ci]` child
of `B`, tags `V`, lands it as a fast-forward or a merge `M = merge(main, V)`,
publishes the GitHub release, then runs the scripted staging sequence
(`deploy/mise-a-jour/staging.sh`) on the **tag** (never the main head) under a
single-operator lock and reports the result to the owner.

No parameter is hard-coded: the calendar, the target and the staging script
come from the compiled canon and the host configuration. A missing mechanics
document is refused (never a fallback), like every connected consumer of
`ameesh.mechanics`.

State is local (`<state_dir>/release/state.json`) and the durable facts are the
tag and the staging attestation: a retry after a crash reconciles them instead
of publishing a second version. The target merge lease (`land.lock_key`) is
held during the whole gesture, so a packet cannot move the target while the
release commit and the staging run are in progress.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import uuid

from . import canon as canon_mod
from . import land
from . import mail
from . import mechanics
from . import plan_github
from . import radar
from . import storage
from . import work

#: schema of the JSON report and of the durable state
SCHEMA = "ameesh-release-cutoff/1"
#: default window after a cutoff during which a tick may still run it
WINDOW_S = 1800.0
#: default timeout of the whole staging sequence
STAGING_TIMEOUT_S = 7200.0
#: default target lease duration, the same as the train and the merge queue
LOCK_TTL_S = land.LOCK_TTL_S
#: default bounded wait for the target lease (a packet CI may run 45 min)
LOCK_WAIT_S = 3600.0
#: a release tag created at or after the cutoff means that cutoff was released
TAG_MARGIN_S = 120.0
#: a cutoff older than this is not worth a "missed" report: the next one is due
MISSED_REPORT_S = 6 * 3600.0
#: version file of a project
VERSION_FILE = "pyproject.toml"
#: release notes of a version
NOTES_DIR = "docs/versions"
#: staging attestation of the installed source
ATTESTATION_DIR = "mise-a-jour"
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"\s]+)"\s*$', re.MULTILINE)
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_PACKAGE_MERGE = re.compile(
    r"^Merge pull request #(\d+) from [^/\s]+/(integration/[^\s]+)\s*$")
_PR_MERGE = re.compile(r"^Merge pull request #(\d+) from [^/\s]+/([^\s]+)\s*$")
_LOT_BRANCH = re.compile(r"^lot/(\d+)(?:-|$)")
_LOT_REF = re.compile(r"(?<![\w/])#(\d{1,7})(?![\w])")
_TRAILER = re.compile(r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?(\d+)[ \t]*$")


class ReleaseError(RuntimeError):
    """A release cannot proceed; the message says what is missing."""


class TransientReleaseError(ReleaseError):
    """A read, push or lock was transient: retry at the next tick."""


class ReleaseBusy(TransientReleaseError):
    """Another operator (or another host) holds the release/staging lock."""


# --------------------------------------------------------------------------
# versions, cutoffs: pure functions
# --------------------------------------------------------------------------

def version_tuple(text: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(str(text or "").strip())
    if not match:
        raise ReleaseError("%r is not a MAJOR.MINOR.PATCH version" % text)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def version_text(triple: tuple[int, int, int]) -> str:
    return "%d.%d.%d" % triple


def bump_patch(triple: tuple[int, int, int]) -> tuple[int, int, int]:
    return triple[0], triple[1], triple[2] + 1


def bump_minor(triple: tuple[int, int, int]) -> tuple[int, int, int]:
    return triple[0], triple[1] + 1, 0


def next_version(current: str, *, bump: str = "level",
                 floor: str = "patch") -> str:
    """The next version from the `version-level.py` floor (L158).

    `current` is the version already in `pyproject.toml` at the released
    commit (it may be an untagged version bump left by a crashed run). `bump`
    is `level` (the content's floor, the pilot's rule for #2448) or `patch`
    (an explicit override). A `major` floor is never published automatically:
    it needs the owner's go (CONTRIBUTING.md, "Numérotation des versions").
    """
    triple = version_tuple(current)
    if bump == "patch":
        return version_text(bump_patch(triple))
    if bump != "level":
        raise ReleaseError("version_bump: level | patch expected, got %r" % bump)
    if floor == "unknown":
        raise ReleaseError("the content's version level is unknown "
                           "(scripts/version-level.py): no release")
    if floor == "major":
        raise ReleaseError("a major version needs the owner's go (content is major)")
    if floor == "minor":
        return version_text(bump_minor(triple))
    return version_text(bump_patch(triple))


_RANK = {"patch": 0, "minor": 1, "major": 2}


def version_level(previous: str, version: str) -> str:
    """The level a version takes from the previous one: patch, minor or major."""
    old, new = version_tuple(previous), version_tuple(version)
    if new[0] != old[0]:
        return "major"
    if new[1] != old[1]:
        return "minor"
    return "patch"


def validate_version(previous: dict | None, version: str, floor: str,
                     bump: str) -> None:
    """Refuse a version below the content's floor (B4), whatever chose it.

    `next_version` validates the computed path; an explicit `--version` or an
    untagged version bump read from `main` bypassed that check before this.
    A major floor always needs the owner's go, unknown is never guessed.
    """
    triple = version_tuple(version)
    if previous is not None:
        if triple <= version_tuple(previous["version"]):
            raise ReleaseError("version %s is not greater than the previous tag %s"
                               % (version, previous["tag"]))
    if bump != "level":
        # Explicit `version_bump=patch` is an operator override; it never
        # authorises a major release, which is the owner's decision.
        if floor == "major":
            raise ReleaseError("a major version needs the owner's go (content is major)")
        return
    if floor == "unknown":
        raise ReleaseError("the content's version level is unknown "
                           "(scripts/version-level.py): no release")
    if floor == "major":
        raise ReleaseError("a major version needs the owner's go (content is major)")
    if previous is not None and version_level(previous["version"], version) == "major":
        raise ReleaseError("a major version needs the owner's go")
    if previous is not None and _RANK[version_level(previous["version"], version)] \
            < _RANK[floor]:
        raise ReleaseError("version %s is below the content's %s floor"
                           % (version, floor))


def version_at(repo: str, ref: str) -> str:
    proc = _git(repo, "show", "%s:%s" % (ref, VERSION_FILE), check=True)
    match = _VERSION_LINE.search(proc.stdout or "")
    if not match:
        raise ReleaseError("no version line in %s at %s" % (VERSION_FILE, ref[:12]))
    return match.group(1)


def cutoff_slot(rules, now: float) -> tuple[float, str]:
    """The last compiled cutoff at or before `now`, and its `HH:MM` label."""
    at = rules.cutoff(now)
    local = _dt.datetime.fromtimestamp(at, rules.zone())
    label = local.strftime("%H:%M")
    if label not in rules.cutoffs:
        raise ReleaseError("compiled cutoff %s is not in the declared list" % label)
    return at, label


def cutoff_date(at: float, rules) -> str:
    return _dt.datetime.fromtimestamp(at, rules.zone()).strftime("%d/%m/%Y")


def cutoff_iso(at: float, rules) -> str:
    return _dt.datetime.fromtimestamp(at, rules.zone()).isoformat()


def due(rules, now: float, state: dict, window_s: float = WINDOW_S) -> dict | None:
    """`{at, label}` when this tick must run, None when nothing is due.

    A cutoff outside its window is never run late: the head would no longer be
    the head of the cutoff. The caller records the miss and says so once.
    """
    at, label = cutoff_slot(rules, now)
    if float(state.get("last_cutoff") or 0) >= at:
        return None
    if now < at:
        return None
    if now - at > float(window_s):
        if now - at > MISSED_REPORT_S:
            return None
        return {"at": at, "label": label, "missed": True}
    return {"at": at, "label": label, "missed": False}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def state_path(cfg) -> str:
    return os.path.join(cfg.state_dir, "release", "state.json")


def load_state(cfg) -> dict:
    path = state_path(cfg)
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {"schema": SCHEMA, "projects": {}}
    except (OSError, ValueError) as exc:
        # A corrupt local state never blocks a release: the durable facts are
        # the tag and the attestation, which the next plan re-reads.
        raise ReleaseError("release state unreadable (%s): %s" % (path, exc)) from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA \
            or not isinstance(data.get("projects"), dict):
        raise ReleaseError("release state has an unknown schema: %s" % path)
    return data


def save_state(cfg, state: dict) -> None:
    path = state_path(cfg)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def project_state(state: dict, project: str) -> dict:
    return (state.setdefault("projects", {}).setdefault(project, {}))


@contextlib.contextmanager
def _locked(path: str):
    """One release at a time on this host; a dead process drops the lock."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseBusy("another release holds %s" % path) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------
# git / GitHub plumbing
# --------------------------------------------------------------------------

def _git(repo: str, *args: str, check: bool = False, timeout: float = 120.0,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args], input=input_text, capture_output=True,
            text=True, timeout=timeout, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except subprocess.TimeoutExpired as exc:
        raise TransientReleaseError("git %s timed out" % " ".join(args[:2])) from exc
    except OSError as exc:
        raise ReleaseError("git %s: %s" % (" ".join(args[:2]), exc)) from exc
    if check and proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise ReleaseError("git %s: %s" % (" ".join(args[:3]), detail or "failed"))
    return proc


def _out(repo: str, *args: str, timeout: float = 120.0) -> str:
    return (_git(repo, *args, timeout=timeout).stdout or "").strip()


def fetch(repo: str, remote: str, target: str) -> None:
    proc = _git(repo, "fetch", "--quiet", "--tags", "--prune", remote,
                "+refs/heads/%s:refs/remotes/%s/%s" % (target, remote, target),
                timeout=300.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise TransientReleaseError("git fetch %s: %s" % (remote, detail or "failed"))


def commit_of(repo: str, ref: str) -> str:
    proc = _git(repo, "rev-parse", "--verify", "%s^{commit}" % ref)
    if proc.returncode:
        raise ReleaseError("%s is not a commit in %s" % (ref, repo))
    return (proc.stdout or "").strip()


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def tags(repo: str, target: str) -> list[dict]:
    """`[{version, tag, commit, at}]`, reachable from `target`, newest first."""
    out = []
    listing = _out(repo, "tag", "--merged", target, "--list", "v*")
    for tag in listing.splitlines():
        tag = tag.strip()
        match = _TAG.fullmatch(tag)
        if not match:
            continue
        commit = _git(repo, "rev-parse", "--verify", "%s^{commit}" % tag)
        if commit.returncode:
            continue
        stamp = _git(repo, "for-each-ref", "--format=%(taggerdate:unix)",
                     "refs/tags/%s" % tag)
        try:
            at = float((stamp.stdout or "").strip() or 0)
        except ValueError:
            at = 0.0
        out.append({"version": version_text(tuple(int(n) for n in match.groups())),
                    "tag": tag, "commit": (commit.stdout or "").strip(), "at": at})
    return sorted(out, key=lambda row: version_tuple(row["version"]), reverse=True)


def latest_tag(repo: str, target: str) -> dict | None:
    rows = tags(repo, target)
    return rows[0] if rows else None


def recent_tag(repo: str, target: str, cutoff: float, now: float) -> dict | None:
    """A tag created at/after the cutoff: that cutoff was already released.

    This is the durable idempotency fact: a crash after the tag push, a second
    release host, or a manual release just before the tick all reconcile the
    same version instead of publishing another one.
    """
    for row in tags(repo, target):
        if cutoff - TAG_MARGIN_S <= row["at"] <= now + TAG_MARGIN_S:
            return row
    return None


def remote_tag(repo: str, remote: str, tag: str) -> str | None:
    """The commit a remote tag names (its peeled line for an annotated tag).

    `ls-remote` is read without a refspec: filtering on `refs/tags/T` drops
    `refs/tags/T^{}`, and the unpeeled line is the tag object, not the commit
    (B3). The commit is what `git rev-parse T^{commit}` gives locally.
    """
    proc = _git(repo, "ls-remote", "--tags", remote, timeout=120.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-200:]
        raise TransientReleaseError("git ls-remote %s: %s" % (remote, detail))
    peeled = ""
    plain = ""
    for line in (proc.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if name == "refs/tags/%s^{}" % tag:
            peeled = sha
        elif name == "refs/tags/%s" % tag:
            plain = sha
    return peeled or plain or None


def github_slug(project: "ProjectRelease") -> str:
    if project.github:
        return project.github
    url = _out(project.repo, "remote", "get-url", project.remote)
    match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", url.strip())
    if not match:
        raise ReleaseError("cannot read the GitHub repository of %s (set release.projects.%s.github)"
                           % (project.name, project.name))
    return match.group(1)


def ci_reading(project: "ProjectRelease", sha: str) -> dict:
    """`radar.commit_ci` on the exact head; a failed read is transient."""
    target = radar.Project(name=project.name, repo=project.repo, target=project.target,
                           github=project.github)
    try:
        return radar.commit_ci(target, sha)
    except plan_github.GithubError as exc:
        raise TransientReleaseError("CI of %s unreadable: %s" % (sha[:12], exc)) from exc


def _release_commit(repo: str, sha: str) -> bool:
    """A `[skip ci]` release commit inherits its first parent's CI state."""
    message = _git(repo, "log", "-1", "--format=%B", sha).stdout or ""
    return "[skip ci]" in message and ("ameesh-release:" in message
                                       or message.lstrip().startswith("release:"))


def commit_is_green(repo: str, project: "ProjectRelease", sha: str,
                    depth: int = 4, reading: dict | None = None) -> bool:
    """The exact commit is green; a release commit inherits its parent."""
    reading = ci_reading(project, sha) if reading is None else reading
    if reading.get("green"):
        return True
    if depth > 0 and _release_commit(repo, sha):
        parent = _git(repo, "rev-parse", "--verify", "%s^1" % sha)
        if parent.returncode == 0:
            return commit_is_green(repo, project, (parent.stdout or "").strip(), depth - 1)
    return False


def last_green(repo: str, head: str, project: "ProjectRelease", limit: int = 20) -> str | None:
    """The green content of the first-parent chain, or the latest release tag.

    A red landing merge `M = merge(main, V)` carries the published release V as
    its second parent; the first-parent walk reaches the older green B, but V
    is the green content that was actually released and must be the next base
    (B1). A tag V is preferred only when it descends from that first-parent
    green commit, so a genuinely newer green commit still wins.
    """
    cursor = head
    first = None
    for _ in range(limit):
        if commit_is_green(repo, project, cursor):
            first = cursor
            break
        parent = _git(repo, "rev-parse", "--verify", "%s^1" % cursor)
        if parent.returncode:
            break
        cursor = (parent.stdout or "").strip()
    latest = latest_tag(repo, head)
    if latest and latest["commit"] != first and _release_commit(repo, latest["commit"]):
        parent = _git(repo, "rev-parse", "--verify", "%s^1" % latest["commit"])
        if parent.returncode == 0 and commit_is_green(
                repo, project, (parent.stdout or "").strip()) \
                and (first is None or is_ancestor(repo, first, latest["commit"])):
            return latest["commit"]
    return first


def version_floor(repo: str, since: str, target: str) -> tuple[str, str]:
    """The `version-level.py` floor of the range, or `unknown`."""
    script = os.path.join(repo, "scripts", "version-level.py")
    if not os.path.isfile(script):
        return "unknown", "scripts/version-level.py absent"
    try:
        proc = subprocess.run([sys.executable, script, "level", since, target],
                              capture_output=True, text=True, cwd=repo, timeout=300.0)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "unknown", "version-level.py: %s" % exc
    if proc.returncode != 0:
        return "unknown", " ".join((proc.stderr or proc.stdout or "").split())[-300:]
    text = (proc.stdout or "").strip()
    head, _, why = text.partition(":")
    head = head.strip()
    if head not in ("major", "minor", "patch"):
        return "unknown", text[:300]
    return head, why.strip()


# --------------------------------------------------------------------------
# notes generated from the merged packets
# --------------------------------------------------------------------------

_PACKAGE_HEADING = re.compile(r"^##+\s+(admissions?|composition|écart|ecart|exclu|retrait)",
                              re.I)
_TABLE_LOT = re.compile(r"^\|\s*#?(\d{1,7})\s*\|(.*)\|\s*$")
_BULLET_LOT = re.compile(r"^[-*]\s*#?(\d{1,7})\b(.*)$")
_EXCLUDED_HEADING = re.compile(r"^##+\s+.*(écart|ecart|exclu|retrait|report)", re.I)


def _clean(text: str, limit: int = 400) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _lots_in(text: str) -> list[int]:
    return [int(number) for number in _LOT_REF.findall(text)]


def _excluded_reason(reason: str) -> bool:
    """A row of the "Écartés" table that is not an exclusion after all."""
    import unicodedata
    lowered = unicodedata.normalize("NFKD", re.sub(r"[`*_]", "", reason.lower()))
    lowered = "".join(char for char in lowered if not unicodedata.combining(char))
    return not any(marker in lowered for marker in
                   ("non retire", "pas une omission", "bien compose",
                    "deja merged", "already merged", "deja fusionne"))


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def parse_manifest(text: str) -> dict:
    """Included/excluded lots of a packet manifest (`reviews/integration-*.md`)."""
    included: set[int] = set()
    excluded: list[tuple[int, str]] = []
    section = ""
    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        stripped = line.strip()
        cells = _cells(stripped)
        if _EXCLUDED_HEADING.match("## " + section):
            if len(cells) >= 2:
                row_lots = _lots_in(cells[0])
                reason = _clean(cells[1])
                if row_lots and _excluded_reason(reason):
                    excluded.extend((lot, reason) for lot in row_lots)
                continue
            match = _BULLET_LOT.match(stripped)
            if match:
                row_lots = [int(match.group(1))] + _lots_in(match.group(2))
                reason = _clean(match.group(2).lstrip(" :—-"))
                if _excluded_reason(reason):
                    excluded.extend((lot, reason) for lot in row_lots)
            continue
        if section.lower().startswith("composition"):
            # | unité | lot(s) | source | commit | note |
            if len(cells) >= 2:
                included.update(_lots_in(cells[1]))
            continue
        # Admissions are prose; the Composition table names the lots
        # actually carried, without picking up lots cited in a note column.
    return {"included": sorted(included), "excluded": excluded}


def _manifest_for(repo: str, target: str, package: str) -> str:
    """The manifest path of a merged packet, read at the target commit."""
    name = package.split("/", 1)[-1]
    base = name if name.startswith("integration-") else "integration-" + name
    for candidate in ("reviews/%s.md" % base, "reviews/%s.md" % name):
        if _git(repo, "cat-file", "-e", "%s:%s" % (target, candidate)).returncode == 0:
            return candidate
    listing = _out(repo, "ls-tree", "-r", "--name-only", target, "reviews/")
    for path in listing.splitlines():
        if path.endswith(".md") and name in os.path.basename(path):
            return path
    return ""


def _manifest_text(repo: str, target: str, path: str) -> str:
    if not path:
        return ""
    proc = _git(repo, "show", "%s:%s" % (target, path))
    return proc.stdout or "" if proc.returncode == 0 else ""


@dataclasses.dataclass
class Packet:
    pr: int
    branch: str
    package: str
    units: list[int]
    manifest: str
    excluded: list[tuple[int, str]]


def packets_between(repo: str, since: str, target: str) -> tuple[list[Packet], list[dict]]:
    """Packets and direct lot merges merged on the first-parent chain."""
    log = _out(repo, "log", "--first-parent", "--reverse", "--format=%H%x1f%s%x1f%b%x1e",
               "%s..%s" % (since, target))
    packets: list[Packet] = []
    direct: list[dict] = []
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, subject, body = parts
        match = _PACKAGE_MERGE.match(subject)
        if not match:
            # A package merged outside the GitHub API still names its branch
            # in the merge subject (work merge-pr, incident vehicle).
            local = re.search(r"(integration/[^\s]+)", subject)
            if local and subject.startswith("Merge"):
                match = types.SimpleNamespace(group=lambda n, value=local: value)
                pr, branch = 0, local.group(1)
            else:
                match = None
        if match:
            if isinstance(match, types.SimpleNamespace):
                pr, branch = 0, match.group(1)
            else:
                pr, branch = int(match.group(1)), match.group(2)
            units = sorted({int(n) for n in _LOT_REF.findall(body)})
            manifest = _manifest_for(repo, target, branch)
            parsed = parse_manifest(_manifest_text(repo, target, manifest))
            # The merge body may omit units; the manifest composition is the
            # complete list. Union, never prefer the shorter body (N1).
            units = sorted(set(units) | set(parsed["included"]))
            packets.append(Packet(pr=pr, branch=branch, package=branch.split("/", 1)[-1],
                                  units=units, manifest=manifest,
                                  excluded=parsed["excluded"]))
            continue
        match = _PR_MERGE.match(subject)
        if not match:
            continue
        pr, branch = int(match.group(1)), match.group(2)
        lots = sorted({int(n) for n in _LOT_BRANCH.findall(branch)}
                      | {int(n) for n in _TRAILER.findall(body)})
        if lots:
            direct.append({"pr": pr, "branch": branch, "lots": lots,
                           "subject": subject, "sha": sha})
    return packets, direct


def migrations_between(repo: str, since: str, target: str) -> list[str]:
    names = _out(repo, "diff", "--name-only", "%s..%s" % (since, target),
                 "--", "src/ameesh/migrations")
    return sorted({os.path.basename(name) for name in names.splitlines()
                   if name.endswith(".sql")})


def generate_notes(*, project: "ProjectRelease", repo: str, since: str, target: str,
                   version: str, cutoff_label: str, cutoff_at: float, rules,
                   previous: dict | None, floor: str, floor_reason: str,
                   packets: list[Packet], direct: list[dict], migrations: list[str],
                   runs: list[str] | None = None,
                   version_bump: str = "level") -> tuple[str, list[int], list[tuple[int, str]]]:
    """The markdown notes of a release, generated from the merged packets."""
    included: set[int] = set()
    excluded: list[tuple[int, str]] = []
    for packet in packets:
        included.update(packet.units)
        excluded.extend(packet.excluded)
    for row in direct:
        included.update(row["lots"])
    by_lot: dict[int, str] = {}
    for lot, reason in excluded:
        if lot in included:
            continue
        if lot not in by_lot or len(reason) > len(by_lot[lot]):
            by_lot[lot] = reason
    excluded = sorted(by_lot.items())
    previous_text = ("%s (%s)" % (previous["version"], previous["tag"])
                     if previous else "the initial release")
    date = cutoff_date(cutoff_at, rules)
    kind = "Release"
    if previous:
        old_version = version_tuple(previous["version"])
        new_version = version_tuple(version)
        if new_version[:2] == old_version[:2]:
            kind = "Patch release"
    lines = [
        "%s at the %s cut of %s (build/beta rhythm, canon §11 bis, "
        "hourly batches home#94). It brings %d packet%s (%d lot%s) "
        "and %d direct lot merge%s since %s, up to `%s`."
        % (kind, cutoff_label, date, len(packets), "" if len(packets) == 1 else "s",
           len(included), "" if len(included) == 1 else "s",
           len(direct), "" if len(direct) == 1 else "s", previous_text, target[:12]),
        "",
    ]
    if migrations:
        lines += ["Migrations: %s." % ", ".join("`%s`" % name for name in migrations), ""]
    if floor == "minor" and version_bump == "level":
        lines += ["**Version level** : the content since the previous tag is `minor` "
                  "(version-level.py: %s); the number above follows that floor."
                  % _clean(floor_reason, 500), ""]
    elif floor == "minor":
        lines += ["**Version level** : the content since the previous tag is `minor` "
                  "(version-level.py: %s). The number above is a patch chosen by an "
                  "explicit `version_bump=patch` override; the auditor can read this "
                  "note as the reason." % _clean(floor_reason, 500), ""]
    elif floor in ("major", "unknown"):
        lines += ["**Version level** : the content's floor is `%s` "
                  "(version-level.py: %s); no automatic release."
                  % (floor, _clean(floor_reason, 500)), ""]
    if packets:
        lines += ["**Packets**"]
        for packet in packets:
            lots = ", ".join("#%d" % lot for lot in packet.units) or "(units not named)"
            manifest = " — manifest `%s`" % packet.manifest if packet.manifest else ""
            label = " (PR #%d)" % packet.pr if packet.pr else ""
            lines.append("- %s%s: %s%s" % (packet.package, label, lots, manifest))
        lines.append("")
    if direct:
        lines += ["**Direct lot merges**"]
        for row in direct:
            lines.append("- PR #%d (`%s`): %s"
                         % (row["pr"], row["branch"],
                            ", ".join("#%d" % lot for lot in row["lots"])))
        lines.append("")
    if included:
        lines += ["**Included lots**", ", ".join("#%d" % lot for lot in sorted(included)), ""]
    if excluded:
        lines += ["**Excluded at this cut** (from the merged packet manifests)",
                  "", "| lot | reason |", "|---|---|"]
        for lot, reason in excluded[:120]:
            lines.append("| #%d | %s |" % (lot, _clean(reason).replace("|", "\\|")))
        lines.append("")
    if runs:
        lines += ["**Validation** : " + "; ".join(runs), ""]
    lines += [
        "**Release commit** : version and notes only, `[skip ci]`; the code is the "
        "green target head named above.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n", sorted(included), excluded


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProjectRelease:
    name: str
    repo: str
    target: str = "main"
    remote: str = "origin"
    staging: str = "deploy/mise-a-jour/staging.sh"
    report: str = "smichea"
    github: str = ""
    window_s: float = WINDOW_S
    staging_timeout_s: float = STAGING_TIMEOUT_S
    lock_ttl_s: float = LOCK_TTL_S
    lock_wait_s: float = LOCK_WAIT_S
    version_bump: str = "level"

    @classmethod
    def from_config(cls, cfg, name: str, entry: dict) -> "ProjectRelease":
        if not isinstance(entry, dict):
            raise ReleaseError("release.projects.%s: an object is required" % name)
        unknown = set(entry) - {"repo", "target", "remote", "staging", "report",
                                "github", "window_s", "staging_timeout_s",
                                "lock_ttl_s", "lock_wait_s", "version_bump"}
        if unknown:
            raise ReleaseError("release.projects.%s: unknown key(s) %s"
                               % (name, ", ".join(sorted(unknown))))
        repo = entry.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ReleaseError("release.projects.%s.repo is required" % name)
        repo = os.path.abspath(os.path.expanduser(repo))
        target = entry.get("target", "main")
        if not isinstance(target, str) or not target.strip():
            raise ReleaseError("release.projects.%s.target is required" % name)
        report = entry.get("report")
        if report is None:
            humans = [h.strip() for h in str(getattr(cfg, "humans", "") or "").split(",")
                      if h.strip()]
            report = humans[0] if humans else "smichea"
        bump = entry.get("version_bump", "level")
        if bump not in ("level", "patch"):
            raise ReleaseError("release.projects.%s.version_bump: level | patch" % name)
        def number(key, default):
            value = entry.get(key, default)
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ReleaseError("release.projects.%s.%s: a positive number is required"
                                   % (name, key)) from exc
            if not value > 0:
                raise ReleaseError("release.projects.%s.%s: a positive number is required"
                                   % (name, key))
            return value
        staging = str(entry.get("staging", "deploy/mise-a-jour/staging.sh"))
        if os.path.isabs(staging):
            raise ReleaseError("release.projects.%s.staging must be relative to repo" % name)
        return cls(
            name=name, repo=repo, target=target.strip(), remote=str(entry.get("remote", "origin")),
            staging=staging,
            report=str(report), github=str(entry.get("github", "") or ""),
            window_s=number("window_s", WINDOW_S),
            staging_timeout_s=number("staging_timeout_s", STAGING_TIMEOUT_S),
            lock_ttl_s=number("lock_ttl_s", LOCK_TTL_S),
            lock_wait_s=number("lock_wait_s", LOCK_WAIT_S), version_bump=bump)


def projects(cfg) -> dict[str, ProjectRelease]:
    block = getattr(cfg, "release", None) or {}
    if not isinstance(block, dict):
        raise ReleaseError("release: an object is required")
    unknown = set(block) - {"host", "projects"}
    if unknown:
        raise ReleaseError("release: unknown key(s) %s" % ", ".join(sorted(unknown)))
    host = block.get("host")
    if host and str(host) != cfg.host:
        return {}
    declared = block.get("projects") or {}
    if not isinstance(declared, dict):
        raise ReleaseError("release.projects: an object is required")
    return {name: ProjectRelease.from_config(cfg, name, entry)
            for name, entry in sorted(declared.items())}


# --------------------------------------------------------------------------
# mechanics / plan
# --------------------------------------------------------------------------

def compiled_rules(cfg, project: str):
    """The compiled mechanics of the applied canon; absent is a refusal."""
    canons = canon_mod.load_configured(cfg)
    if not canons:
        raise ReleaseError("no readable canon: the release calendar is unavailable")
    root = canons[0]
    source = next((src for src in root.sources if src.mode == "git" and src.commit), None)
    if source is None:
        raise ReleaseError("the canon is not a git source: the release calendar is unavailable")
    try:
        rules = mechanics.load(source.repo, source.commit, project)
    except mechanics.MechanicsAbsent as exc:
        raise ReleaseError("processus/mecanique.yaml is absent from the canon at %s "
                           "(the release calendar cannot be compiled)" % source.commit[:12]) from exc
    except mechanics.MechanicsError as exc:
        raise ReleaseError("the compiled canon mechanics are unusable: %s" % exc) from exc
    return rules, source


@dataclasses.dataclass
class Plan:
    project: ProjectRelease
    rules: object
    canon_commit: str
    cutoff_at: float
    cutoff_label: str
    head: str
    ci: dict
    green: bool
    last_green: str | None
    base: str
    current_version: str
    previous: dict | None
    version: str
    floor: str
    floor_reason: str
    packets: list[Packet]
    direct: list[dict]
    migrations: list[str]
    included: list[int]
    excluded: list[tuple[int, str]]
    notes: str


def build_plan(cfg, project: ProjectRelease, *, now: float,
               version: str | None = None) -> Plan:
    rules, source = compiled_rules(cfg, project.name)
    cutoff_at, cutoff_label = cutoff_slot(rules, now)
    fetch(project.repo, project.remote, project.target)
    head = commit_of(project.repo, "%s/%s" % (project.remote, project.target))
    reading = ci_reading(project, head)
    green = commit_is_green(project.repo, project, head, reading=reading)
    if not green:
        green_commit = last_green(project.repo, head, project)
    else:
        green_commit = head
    base = green_commit or head
    current = version_at(project.repo, base)
    previous = latest_tag(project.repo, base)
    if previous and version_tuple(current) < version_tuple(previous["version"]):
        raise ReleaseError("the target %s is behind its tag %s: refusing to release"
                           % (base[:12], previous["tag"]))
    floor, floor_reason = ("patch", "no previous tag")
    if previous:
        floor, floor_reason = version_floor(project.repo, previous["tag"], base)
    if version is None:
        if previous and version_tuple(current) > version_tuple(previous["version"]):
            # An untagged version bump left by a crashed run: keep it only if
            # it still satisfies the floor of the content it carries.
            if project.version_bump == "level" \
                    and _RANK[version_level(previous["version"], current)] \
                    < _RANK.get(floor, 0):
                version = next_version(previous["version"], bump="level", floor=floor)
            else:
                validate_version(previous, current, floor, project.version_bump)
                version = current
        else:
            version = next_version(current, bump=project.version_bump, floor=floor)
    else:
        validate_version(previous, version, floor, project.version_bump)
    version_tuple(version)
    since = previous["commit"] if previous else base
    packets, direct = packets_between(project.repo, since, base)
    migrations = migrations_between(project.repo, since, base)
    notes, included, excluded = generate_notes(
        project=project, repo=project.repo, since=since, target=base, version=version,
        cutoff_label=cutoff_label, cutoff_at=cutoff_at, rules=rules, previous=previous,
        floor=floor, floor_reason=floor_reason, packets=packets, direct=direct,
        migrations=migrations, version_bump=project.version_bump)
    return Plan(project=project, rules=rules, canon_commit=source.commit,
                cutoff_at=cutoff_at, cutoff_label=cutoff_label, head=head, ci=reading,
                green=green, last_green=green_commit, base=base, current_version=current,
                previous=previous, version=version, floor=floor, floor_reason=floor_reason,
                packets=packets, direct=direct, migrations=migrations,
                included=included, excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# release commit, tag, GitHub release
# --------------------------------------------------------------------------

def _worktree(repo: str, sha: str) -> tuple[str, str]:
    folder = tempfile.mkdtemp(prefix="ameesh-release-", dir=os.path.dirname(repo) or None)
    path = os.path.join(folder, "worktree")
    proc = _git(repo, "worktree", "add", "--detach", path, sha)
    if proc.returncode:
        shutil.rmtree(folder, ignore_errors=True)
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise ReleaseError("git worktree add: %s" % (detail or "failed"))
    return folder, path


def _drop_worktree(repo: str, folder: str, path: str) -> None:
    _git(repo, "worktree", "remove", "--force", path)
    _git(repo, "worktree", "prune")
    shutil.rmtree(folder, ignore_errors=True)


def _write_version(path: str, version: str) -> None:
    file = os.path.join(path, VERSION_FILE)
    with open(file, encoding="utf-8") as stream:
        text = stream.read()
    replaced, count = _VERSION_LINE.subn('version = "%s"' % version, text, count=1)
    if count != 1:
        raise ReleaseError("no version line in %s" % VERSION_FILE)
    with open(file, "w", encoding="utf-8") as stream:
        stream.write(replaced)


def _write_notes(path: str, version: str, notes: str) -> None:
    folder = os.path.join(path, NOTES_DIR)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "%s.md" % version), "w", encoding="utf-8") as stream:
        stream.write(notes)


def _commit(path: str, message: str, *, author: str = "ameesh release") -> str:
    env = dict(os.environ,
               GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL="release@ameesh.invalid",
               GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL="release@ameesh.invalid")
    proc = subprocess.run(["git", "-C", path, "commit", "--no-verify", "-q", "-m", message],
                          capture_output=True, text=True, env=env, timeout=120.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise ReleaseError("git commit: %s" % (detail or "failed"))
    return _out(path, "rev-parse", "HEAD")


def create_version_commit(project: ProjectRelease, plan: Plan) -> str:
    """Create (or reconcile) the version commit V on B, without pushing.

    V only changes `pyproject.toml` and the generated notes. It is the commit
    the tag names and the one staging deploys; when `main` is red it stays off
    `main` until the landing merge `M` (see `land_release`).
    """
    head = plan.base
    current = version_at(project.repo, head)
    # A crashed run may have left V and its local tag: reuse that exact commit
    # instead of creating a second one (the tag then names the same tree).
    tag = "v%s" % plan.version
    existing = _git(project.repo, "rev-parse", "--verify", "refs/tags/%s^{commit}" % tag)
    if existing.returncode == 0:
        sha = (existing.stdout or "").strip()
        parent = _git(project.repo, "rev-parse", "--verify", "%s^1" % sha)
        if (parent.returncode == 0 and (parent.stdout or "").strip() == head
                and version_at(project.repo, sha) == plan.version):
            return sha
    notes_path = "%s/%s.md" % (NOTES_DIR, plan.version)
    has_notes = _git(project.repo, "cat-file", "-e",
                     "%s:%s" % (head, notes_path)).returncode == 0
    if version_tuple(current) == version_tuple(plan.version) and has_notes:
        return head
    folder, worktree = _worktree(project.repo, head)
    try:
        if version_tuple(current) != version_tuple(plan.version):
            _write_version(worktree, plan.version)
        _write_notes(worktree, plan.version, plan.notes)
        _git(worktree, "add", VERSION_FILE, notes_path)
        message = ("release: %s (coupure de %s du %s ; %d paquets, %d lots) [skip ci]\n\n"
                   "ameesh-release: %s"
                   % (plan.version, plan.cutoff_label,
                      cutoff_date(plan.cutoff_at, plan.rules),
                      len(plan.packets), len(plan.included), plan.version))
        commit = _commit(worktree, message)
    finally:
        _drop_worktree(project.repo, folder, worktree)
    return commit


def _ensure_local_tag(project: ProjectRelease, release_sha: str, version: str) -> str:
    tag = "v%s" % version
    local = _git(project.repo, "rev-parse", "--verify", "refs/tags/%s^{commit}" % tag)
    if local.returncode == 0:
        if (local.stdout or "").strip() != release_sha:
            remote = remote_tag(project.repo, project.remote, tag)
            if remote is None:
                # A local tag left by a crashed run, never published: recreate
                # it on this attempt's V instead of blocking forever.
                _git(project.repo, "tag", "-d", tag)
            else:
                raise ReleaseError("tag %s already exists on %s, not on the release commit %s"
                                   % (tag, (local.stdout or "").strip()[:12],
                                      release_sha[:12]))
        else:
            remote = remote_tag(project.repo, project.remote, tag)
            if remote and remote != release_sha:
                raise ReleaseError("tag %s exists on the remote at %s, not at %s"
                                   % (tag, remote[:12], release_sha[:12]))
            return tag
    remote = remote_tag(project.repo, project.remote, tag)
    if remote:
        if remote != release_sha:
            raise ReleaseError("tag %s exists on the remote at %s, not at %s"
                               % (tag, remote[:12], release_sha[:12]))
        fetched = _git(project.repo, "fetch", "--quiet", project.remote,
                       "refs/tags/%s:refs/tags/%s" % (tag, tag), timeout=120.0)
        if fetched.returncode:
            detail = " ".join((fetched.stderr or fetched.stdout or "").split())[-200:]
            raise TransientReleaseError("remote tag %s not fetched: %s" % (tag, detail))
        local = _git(project.repo, "rev-parse", "--verify", "refs/tags/%s^{commit}" % tag)
        if local.returncode or (local.stdout or "").strip() != release_sha:
            raise ReleaseError("tag %s fetched on another commit than %s"
                               % (tag, release_sha[:12]))
        return tag
    env = dict(os.environ, GIT_COMMITTER_NAME="ameesh release",
               GIT_COMMITTER_EMAIL="release@ameesh.invalid")
    proc = subprocess.run(
        ["git", "-C", project.repo, "tag", "-a", tag, "-m", "ameesh %s" % version,
         release_sha], capture_output=True, text=True, env=env, timeout=120.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise ReleaseError("git tag: %s" % (detail or "failed"))
    return tag


def _landing_merge(project: ProjectRelease, plan: Plan, release_sha: str,
                   main: str) -> str:
    """M = merge(main, V), with `[skip ci]`, or a named refusal."""
    folder, worktree = _worktree(project.repo, main)
    try:
        merged = _git(worktree, "merge", "--no-ff", "--no-commit", release_sha)
        if merged.returncode:
            _git(worktree, "merge", "--abort")
            detail = " ".join((merged.stderr or merged.stdout or "").split())[-300:]
            raise ReleaseError("the landing merge of %s on %s conflicts: %s"
                               % (release_sha[:12], main[:12], detail))
        if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
            raise ReleaseError("the landing merge of %s on %s carries no change"
                               % (release_sha[:12], main[:12]))
        message = ("release: %s landing (coupure de %s du %s) [skip ci]\n\n"
                   "ameesh-release: %s"
                   % (plan.version, plan.cutoff_label,
                      cutoff_date(plan.cutoff_at, plan.rules), plan.version))
        return _commit(worktree, message)
    finally:
        _drop_worktree(project.repo, folder, worktree)


def land_release(project: ProjectRelease, plan: Plan, release_sha: str,
                 version: str) -> dict:
    """Land V (or M = merge(main, V)) and the tag in one atomic push.

    No force, ever: a target that moved is a transient refusal, retried once
    by the caller. The tag names V, the green content; `main` may carry a
    landing merge M when the cutoff head was red.
    """
    fetch(project.repo, project.remote, project.target)
    main = commit_of(project.repo, "%s/%s" % (project.remote, project.target))
    landing = release_sha if main == plan.base else _landing_merge(
        project, plan, release_sha, main)
    tag = _ensure_local_tag(project, release_sha, version)
    spec = ["%s:refs/heads/%s" % (landing, project.target), "refs/tags/%s" % tag]
    proc = _git(project.repo, "push", "--atomic", "--no-verify", project.remote, *spec,
                timeout=300.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        # Never leave an unpublished local tag behind: the next attempt must
        # be able to build its own atomic transaction.
        if remote_tag(project.repo, project.remote, tag) is None:
            _git(project.repo, "tag", "-d", tag)
        raise TransientReleaseError("atomic push of %s and %s refused: %s"
                                    % (landing[:12], tag, detail))
    return {"landing": landing, "tag": tag}


def ensure_github_release(project: ProjectRelease, tag: str, version: str,
                          notes: str) -> bool:
    """Create the GitHub release if absent; True when it exists afterwards."""
    slug = github_slug(project)
    try:
        gh = plan_github.Gh()
        code, _out_text, _err = gh.run(["release", "view", tag, "-R", slug])
        if code == 0:
            return True
        folder = tempfile.mkdtemp(prefix="ameesh-release-notes-")
        path = os.path.join(folder, "notes.md")
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(notes)
            code, out, err = gh.run(["release", "create", tag, "-R", slug, "--verify-tag",
                                     "--title", "ameesh %s" % version,
                                     "--notes-file", path])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    except plan_github.GithubError as exc:
        raise TransientReleaseError("GitHub release %s: %s" % (tag, exc)) from exc
    if code == 0:
        return True
    if "already exists" in (err or out or "").lower():
        return True
    detail = " ".join((err or out or "").split())[-300:]
    raise TransientReleaseError("GitHub release %s not created: %s" % (tag, detail))


# --------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------

def attestation_path(sha: str) -> str:
    # `actif.py` names its file with the first 12 characters of the commit.
    return os.path.join(os.path.expanduser("~"), ".local", "state", "ameesh",
                        ATTESTATION_DIR, "actif-%s.json" % sha[:12])


def read_attestation(sha: str) -> dict | None:
    """The durable 4-host attestation of `sha`, if present and complete."""
    path = attestation_path(sha)
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("sha", "")[:len(sha)] != sha[:len(sha)]:
        return None
    hosts = data.get("hotes")
    if not isinstance(hosts, dict) or len(hosts) != 4:
        return None
    return data


def staging_journal(day: str | None = None) -> str:
    day = day or _dt.date.today().isoformat()
    return os.path.join(os.path.expanduser("~"), ".local", "state", "ameesh",
                        ATTESTATION_DIR, "staging-%s.jsonl" % day)


def read_journal(since: str = "") -> list[dict]:
    """The staging events of this run (the journal is daily, `since` UTC ISO)."""
    path = staging_journal()
    out = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if since and str(row.get("ts") or "") < since:
                    continue
                out.append(row)
    except FileNotFoundError:
        return []
    return out


def staging_summary(events: list[dict], sha: str) -> list[dict]:
    """Last event per host of this run, with the rollback evidence."""
    by_host: dict[str, list[dict]] = {}
    for row in events:
        if str(row.get("sha") or "")[:len(sha)] != sha[:len(sha)]:
            continue
        host = str(row.get("hote") or "")
        if host:
            by_host.setdefault(host, []).append(row)
    out = []
    for host in ("ameesh-agents-1", "ameesh-agents-2", "manaty-mesh-1", "pc-smichea"):
        rows = by_host.get(host, [])
        if not rows:
            continue
        last = rows[-1]
        rollback = next((row for row in rows
                         if row.get("etape") == "retour" and row.get("verdict") == "ok"), None)
        out.append({"host": host, "step": last.get("etape"), "verdict": last.get("verdict"),
                    "detail": _clean(last.get("detail"), 300),
                    "rollback": bool(rollback)})
    return out


def _kill_group(proc, sig=signal.SIGTERM) -> None:
    """Kill the process group of a timed-out staging run (its ssh children)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_staging(project: ProjectRelease, release_sha: str, included: list[int],
                *, state_dir: str, timeout_s: float | None = None) -> dict:
    """Run `staging.sh` from a worktree of the release commit, under its lock."""
    relative = project.staging
    if os.path.isabs(relative):
        relative = os.path.relpath(relative, project.repo)
    folder, worktree = _worktree(project.repo, release_sha)
    try:
        script = os.path.join(worktree, relative)
        if not os.path.isfile(script):
            raise ReleaseError("staging script absent at %s in the release commit"
                               % project.staging)
        args = ["bash", script]
        if included:
            args += ["--lots", " ".join(str(lot) for lot in included)]
        args.append(release_sha)
        log_dir = os.path.join(state_dir, "release")
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        log_path = os.path.join(log_dir, "staging-%s.log" % release_sha[:9])
        started = time.time()
        since_iso = _dt.datetime.fromtimestamp(started, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_path, "w", encoding="utf-8") as log:
            try:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        env=dict(os.environ), start_new_session=True)
            except OSError as exc:
                raise ReleaseError("staging could not start: %s" % exc) from exc
            try:
                output, _ = proc.communicate(
                    timeout=timeout_s or project.staging_timeout_s)
            except subprocess.TimeoutExpired as exc:
                partial = exc.output or ""
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", "replace")
                log.write(partial)
                # `communicate` kills bash only; the sequence's ssh/vm.sh
                # children would survive and keep the worktree/flock. Kill the
                # whole session started for this run, then wait for it.
                _kill_group(proc, signal.SIGTERM)
                try:
                    output, _ = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    _kill_group(proc, signal.SIGKILL)
                    output, _ = proc.communicate()
                log.write(output or "")
                raise TransientReleaseError(
                    "staging timed out after %ss (log %s)"
                    % (timeout_s or project.staging_timeout_s, log_path)) from exc
            log.write(output or "")
        return {"exit": proc.returncode, "duration_s": round(time.time() - started, 1),
                "log": log_path,
                "output_tail": "\n".join((output or "").splitlines()[-20:]),
                "summary": staging_summary(read_journal(since_iso), release_sha),
                "attestation": read_attestation(release_sha)}
    finally:
        _drop_worktree(project.repo, folder, worktree)


# --------------------------------------------------------------------------
# report / execute
# --------------------------------------------------------------------------

def _report_body(project: ProjectRelease, plan: Plan | None, *, result: str,
                 version: str, tag: str, release_sha: str, staging: dict | None,
                 deviations: list[str], previous: dict | None,
                 cutoff_at: float, cutoff_label: str, rules,
                 landing_sha: str = "") -> str:
    lines = ["Release %s — coupure %s du %s"
             % (version, cutoff_label, cutoff_date(cutoff_at, rules))]
    head = plan.head if plan else release_sha
    lines.append("Commit %s (tête %s), tag %s." % (release_sha[:12], head[:12], tag))
    if landing_sha and landing_sha != release_sha:
        lines.append("Atterrissage sur %s: commit de fusion %s."
                     % (project.target, landing_sha[:12]))
    if plan:
        lines.append("Paquets: %s." % (", ".join("%s (#%d)" % (packet.package, packet.pr)
                                                 for packet in plan.packets) or "aucun"))
        lines.append("Lots inclus: %s." % (", ".join("#%d" % lot for lot in plan.included)
                                           or "aucun"))
    if plan and plan.excluded:
        lines.append("Lots écartés: %s." % ", ".join(
            "#%d (%s)" % (lot, reason) for lot, reason in plan.excluded[:30]))
    if previous:
        lines.append("Avant: %s (tag %s, commit %s)."
                     % (previous["version"], previous["tag"], previous["commit"][:12]))
    lines.append("Après: %s (commit %s)." % (version, release_sha[:12]))
    if plan and plan.ci:
        lines.append("CI tête: %s%s." % (
            "verte" if plan.ci.get("green") else "non verte",
            " ; dernier vert %s" % plan.last_green[:12]
            if not plan.ci.get("green") and plan.last_green else ""))
    if staging:
        lines.append("Staging: sortie %s en %ss (journal %s)."
                     % (staging.get("exit"), staging.get("duration_s"), staging.get("log")))
        for row in staging.get("summary") or []:
            lines.append("  - %s: %s %s%s" % (row["host"], row["step"], row["verdict"],
                                             " (retour effectué)" if row["rollback"] else ""))
        if staging.get("error"):
            lines.append("  erreur: %s" % staging["error"])
        if staging.get("attestation"):
            lines.append("  attestation: %s" % attestation_path(release_sha))
    if deviations:
        lines += ["Écarts:", *["- %s" % text for text in deviations]]
    lines.append("")
    return "\n".join(lines)


def send_report(cfg, db, project: ProjectRelease, body: str, payload: dict,
                *, urgent: bool = False) -> None:
    try:
        mail.send(db, work.SYSTEM_SENDER, project.report, body, kind="event",
                  payload={**(payload or {}), **({"urgent": True} if urgent else {})})
    except Exception as exc:  # a report failure never undoes a release
        print("release: report not delivered to %s: %s" % (project.report, exc),
              file=sys.stderr)


def write_report(cfg, project: ProjectRelease, report: dict) -> str:
    folder = os.path.join(cfg.state_dir, "release", "reports")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stamp = _dt.datetime.fromtimestamp(report["cutoff_at"]).strftime("%Y%m%d-%H%M")
    path = os.path.join(folder, "%s-%s.json" % (report["version"], stamp))
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def _acquire_target(db, project: ProjectRelease) -> tuple[str, str, int]:
    """Wait, bounded, for the target lease shared with the train (#2446).

    A packet CI may hold it for tens of minutes; never force, never share an
    epoch. The owner is unique to this release, so the holder can be told
    apart in `merge_locks`.
    """
    key = land.lock_key(project, project.target)
    owner = "release:" + uuid.uuid4().hex
    deadline = time.monotonic() + project.lock_wait_s
    while True:
        epoch = storage.of(db).work.land_lock(key, owner, project.lock_ttl_s)
        if epoch is not None:
            return key, owner, epoch
        if time.monotonic() >= deadline:
            raise ReleaseBusy("target lease %s still held after %ss"
                              % (key, int(project.lock_wait_s)))
        time.sleep(5.0)


def execute(cfg, db, project: ProjectRelease, *, now: float | None = None,
            force: bool = False, no_staging: bool = False,
            version: str | None = None) -> dict:
    """One release attempt; the report is the durable answer."""
    now = time.time() if now is None else float(now)
    state = load_state(cfg)
    state_slot = project_state(state, project.name)
    rules, source = compiled_rules(cfg, project.name)
    at, label = cutoff_slot(rules, now)
    if not force and float(state_slot.get("last_cutoff") or 0) >= at:
        return {"result": "already-recorded", "cutoff_at": at, "label": label}
    due_slot = due(rules, now, state_slot, project.window_s)
    if not force and due_slot is None:
        return {"result": "not-due", "cutoff_at": at, "label": label}
    if not force and due_slot and due_slot.get("missed"):
        fetch(project.repo, project.remote, project.target)
        if recent_tag(project.repo, "%s/%s" % (project.remote, project.target), at, now):
            # This cutoff's tag is published (staging lock or crash after the
            # push): reconcile it late, never report "nothing published".
            due_slot = dict(due_slot, missed=False)
    if not force and due_slot and due_slot.get("missed"):
        state_slot["last_cutoff"] = at
        state_slot["last_result"] = "missed"
        state_slot["last_label"] = label
        save_state(cfg, state)
        report = {"schema": SCHEMA, "result": "missed", "cutoff_at": at, "label": label,
                  "project": project.name, "canon_commit": source.commit,
                  "completed_at": time.time(),
                  "message": "cutoff missed: the tick started more than %ss after it"
                             % int(project.window_s)}
        if db is not None:
            send_report(cfg, db, project, "Release %s NON faite : coupure %s du %s manquée "
                        "(démarrage hors fenêtre de %ss). Rien n'a été publié ni déployé."
                        % (project.name, label, cutoff_date(at, rules),
                           int(project.window_s)), report, urgent=True)
        return report

    lock = os.path.join(cfg.state_dir, "release", "release.lock")
    with _locked(lock):
        return _execute_locked(cfg, db, project, now=now, force=force,
                               no_staging=no_staging, version=version, at=at,
                               label=label, rules=rules, source=source, state=state,
                               state_slot=state_slot)


def _execute_locked(cfg, db, project: ProjectRelease, *, now: float, force: bool,
                    no_staging: bool, version: str | None, at: float, label: str,
                    rules, source, state: dict, state_slot: dict) -> dict:
    """The body of a release, under the local lock and the target lease."""
    started = time.time()
    # Durable reconciliation: a tag created at/after the cutoff means that
    # cutoff was already released (manual release, second host, crash).
    fetch(project.repo, project.remote, project.target)
    target_head = commit_of(project.repo, "%s/%s" % (project.remote, project.target))
    recent_ref = "%s/%s" % (project.remote, project.target)
    recent = None if force else recent_tag(project.repo, recent_ref, at, now)
    if recent:
        release_sha, version_out = recent["commit"], recent["version"]
        older = [row for row in tags(project.repo, release_sha)
                 if row["tag"] != recent["tag"]]
        previous = older[0] if older else None
        notes_path = "%s/%s.md" % (NOTES_DIR, version_out)
        notes = (_git(project.repo, "show", "%s:%s" % (release_sha, notes_path)).stdout
                 or "") if _git(project.repo, "cat-file", "-e",
                                "%s:%s" % (release_sha, notes_path)).returncode == 0 else ""
        since = previous["commit"] if previous else release_sha
        packets, direct = packets_between(project.repo, since, release_sha)
        migrations = migrations_between(project.repo, since, release_sha)
        included = sorted({lot for packet in packets for lot in packet.units}
                          | {lot for row in direct for lot in row["lots"]})
        if not notes:
            notes, included, _ = generate_notes(
                project=project, repo=project.repo, since=since, target=release_sha,
                version=version_out, cutoff_label=label, cutoff_at=at, rules=rules,
                previous=previous, floor="patch", floor_reason="reconciled release",
                packets=packets, direct=direct, migrations=migrations,
                version_bump=project.version_bump)
        ensure_github_release(project, recent["tag"], version_out, notes)
        staging = (None if no_staging else
                   _staging_step(project, release_sha, included, cfg.state_dir))
        report = {"schema": SCHEMA, "result": "reconciled", "project": project.name,
                  "version": version_out, "tag": recent["tag"],
                  "release_sha": release_sha, "target_head": target_head,
                  "cutoff_at": at, "label": label, "canon_commit": source.commit,
                  "previous": previous, "staging": staging,
                  "started_at": started,
                  "deviations": ["reconciled an existing tag created at/after the cutoff"]}
        return finish(cfg, db, project, report, state, state_slot, at, label, rules,
                      previous, None, staging, no_staging)

    plan = build_plan(cfg, project, now=now, version=version)
    if not plan.green and not plan.last_green:
        state_slot["last_cutoff"] = at
        state_slot["last_result"] = "refused-no-green"
        state_slot["last_label"] = label
        state_slot.pop("last_error", None)
        report = {"schema": SCHEMA, "result": "refused-no-green",
                  "project": project.name, "version": plan.version,
                  "target_head": plan.head, "cutoff_at": at, "label": label,
                  "ci": plan.ci, "started_at": started,
                  "deviations": ["no green first-parent commit: nothing to tag"],
                  "canon_commit": plan.canon_commit}
        report["completed_at"] = time.time()
        report["duration_s"] = round(report["completed_at"] - started, 1)
        report["report_path"] = write_report(cfg, project, report)
        save_state(cfg, state)
        if db is not None:
            send_report(cfg, db, project,
                        "Release %s NON faite : aucune tête verte sur %s à la coupure "
                        "%s (tête %s, CI %s). Aucun tag, aucun déploiement."
                        % (plan.version, project.target, label, plan.head[:12],
                           "verte" if plan.ci.get("green") else "non verte"),
                        report, urgent=True)
        return report

    deviations = []
    if not plan.green:
        deviations.append("tête %s non verte: release sur le dernier commit vert %s"
                          % (plan.head[:12], plan.base[:12]))
    version_sha = create_version_commit(project, plan)
    push_error = None
    landed = None
    if db is not None:
        key, owner, epoch = _acquire_target(db, project)
    else:
        key = owner = epoch = None
    try:
        try:
            landed = land_release(project, plan, version_sha, plan.version)
        except TransientReleaseError:
            # One recomputation, as the pilot asked; a second refusal is a
            # report, never a force.
            try:
                landed = land_release(project, plan, version_sha, plan.version)
            except TransientReleaseError as exc:
                push_error = exc
    finally:
        if epoch is not None:
            storage.of(db).work.land_unlock(key, owner, epoch)
    if push_error is not None:
        state_slot["last_cutoff"] = at
        state_slot["last_result"] = "refused-push"
        state_slot["last_label"] = label
        state_slot.pop("last_error", None)
        report = {"schema": SCHEMA, "result": "refused-push", "project": project.name,
                  "version": plan.version, "release_sha": version_sha,
                  "target_head": plan.head, "cutoff_at": at, "label": label,
                  "canon_commit": plan.canon_commit, "ci": plan.ci,
                  "started_at": started, "deviations": [str(push_error)]}
        report["completed_at"] = time.time()
        report["duration_s"] = round(report["completed_at"] - started, 1)
        report["report_path"] = write_report(cfg, project, report)
        save_state(cfg, state)
        if db is not None:
            send_report(cfg, db, project,
                        "Release %s NON landée : deux refus de la poussée atomique sur "
                        "%s à la coupure %s. Aucun tag n'a été publié ; le commit de "
                        "version local %s reste réutilisable. %s"
                        % (plan.version, project.target, label, version_sha[:12],
                           push_error), report, urgent=True)
        return report
    tag = landed["tag"]
    fetch(project.repo, project.remote, project.target)
    ensure_github_release(project, tag, plan.version, plan.notes)
    # Staging deploys the tag's commit V (the green content), never the main
    # head; `origin/main` now contains the landing merge M when main was red.
    staging = (None if no_staging else
               _staging_step(project, version_sha, plan.included, cfg.state_dir))
    if plan.floor != "patch":
        deviations.append("version level %s: %s" % (plan.floor, plan.floor_reason[:300]))
    report = {"schema": SCHEMA, "result": "released", "project": project.name,
              "version": plan.version, "tag": tag, "release_sha": version_sha,
              "landing_sha": landed["landing"], "target_head": plan.head,
              "cutoff_at": at, "label": label,
              "canon_commit": plan.canon_commit, "floor": plan.floor,
              "floor_reason": plan.floor_reason, "previous": plan.previous,
              "packets": [dataclasses.asdict(packet) for packet in plan.packets],
              "direct": plan.direct, "included": plan.included,
              "excluded": plan.excluded, "migrations": plan.migrations,
              "notes": plan.notes, "staging": staging, "started_at": started,
              "deviations": deviations}
    return finish(cfg, db, project, report, state, state_slot, at, label, rules,
                  plan.previous, plan, staging, no_staging)


def _staging_step(project: ProjectRelease, release_sha: str, included: list[int],
                  state_dir: str) -> dict:
    """Run staging; a lock conflict retries, a timeout is reported as a gap."""
    try:
        result = maybe_staging(project, release_sha, included, state_dir)
    except ReleaseBusy:
        raise
    except TransientReleaseError as exc:
        return {"exit": None, "duration_s": 0.0, "log": "", "summary": [],
                "attestation": None, "error": str(exc)}
    if result.get("exit") == 75:
        # staging.sh's single-operator lock: another sequence is running. Not
        # a release failure; the next tick reconciles the tag and retries.
        raise ReleaseBusy("staging is running (code 75, single-operator lock)")
    return result


def maybe_staging(project: ProjectRelease, release_sha: str, included: list[int],
                  state_dir: str) -> dict:
    attestation = read_attestation(release_sha)
    if attestation:
        return {"exit": 0, "duration_s": 0.0, "log": "", "summary": [],
                "attestation": attestation, "skipped": "already attested"}
    return run_staging(project, release_sha, included, state_dir=state_dir)


def finish(cfg, db, project: ProjectRelease, report: dict, state: dict,
           state_slot: dict, at: float, label: str, rules, previous: dict | None,
           plan: Plan | None, staging: dict | None, no_staging: bool) -> dict:
    """Write the durable report, mail the owner, record the cutoff."""
    result = report.get("result")
    report["staging_ok"] = bool(no_staging or (staging and staging.get("exit") == 0))
    report["completed_at"] = time.time()
    if isinstance(report.get("started_at"), (int, float)):
        report["duration_s"] = round(report["completed_at"] - report["started_at"], 1)
    path = write_report(cfg, project, report)
    report["report_path"] = path
    state_slot["last_cutoff"] = at
    state_slot["last_label"] = label
    state_slot["last_result"] = result
    state_slot.pop("last_error", None)
    state_slot["last_release"] = {"version": report.get("version", ""),
                                  "tag": report.get("tag", ""),
                                  "sha": report.get("release_sha", ""),
                                  "at": time.time()}
    if staging is not None:
        state_slot["last_staging"] = {key: staging.get(key) for key in
                                      ("exit", "duration_s", "log", "skipped")}
    save_state(cfg, state)
    if db is not None:
        body = _report_body(project, plan, result=result,
                            version=report.get("version", "?"),
                            tag=report.get("tag", "?"),
                            release_sha=report.get("release_sha", "?"),
                            staging=staging, deviations=report.get("deviations") or [],
                            previous=previous, cutoff_at=at, cutoff_label=label,
                            rules=rules, landing_sha=report.get("landing_sha", ""))
        body += "\n\nRapport JSON: %s\nJournal de staging: %s" % (
            path, (staging or {}).get("log", "—"))
        send_report(cfg, db, project, body, report,
                    urgent=not report["staging_ok"] or result == "refused-red")
    return report
