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
of publishing a second version. The attestation is also mirrored as a compact
`ameesh_events` event (#3981), so a sequence run from a VM — where `actif.py`
runs on `HOTE_OUTILS` — is read by the poste without ssh. The target merge
lease (`land.lock_key`) is held during the whole gesture, so a packet cannot
move the target while the release commit and the staging run are in progress.
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
#: after a staging timeout, how long the workstation's transient units
#: (`poste-maj-staging-<pid>-*`, restart and rollback, #3039) may finish before
#: they are stopped: they run from the release worktree (#3049)
STAGING_UNIT_GRACE_S = 600.0
#: pause between two reads of those units
STAGING_UNIT_POLL_S = 5.0
#: after SIGTERM to a timed-out staging group, before SIGKILL
STAGING_KILL_GRACE_S = 10.0
#: last bounded read of the staging output once its units are settled: a
#: survivor may still hold the pipe (`systemd-run --pipe`)
STAGING_DRAIN_S = 10.0
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
# #4786: the release commit builds its own assets (wheel, pin files,
# SHA256SUMS), like it runs its own staging.sh; a commit without these files
# (another project, a version before #4778) publishes no asset.
ASSETS_SCRIPT = "deploy/mise-a-jour/construction-runtime.py"
ASSETS_SOURCES = (ASSETS_SCRIPT, "deploy/mise-a-jour/runtime-release.py",
                  "deploy/mise-a-jour/runtime-pins.txt",
                  "deploy/mise-a-jour/runtime-build-pins.txt")
ASSETS_SUMS = "SHA256SUMS"
ASSETS_TIMEOUT_S = 1800.0
#: #3981 : the shared form of the attestation, in the append-only event log.
#: `actif.py` may run on HOTE_OUTILS (a VM) when the launcher has no venv/DB;
#: the event is what the poste reads without ssh. The window is the durability
#: the proof needs: an older attestation no longer proves the active content.
STAGING_EVENT = "staging.attested"
STAGING_EVENT_DAYS = 60
STAGING_EVENT_LIMIT = 1000
#: staging hosts (canon §14): the agent VMs are always proved; the server and
#: the poste may be reported absent (#3848, #3965: same lists as actif.py)
STAGING_AGENTS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")
STAGING_SERVEUR = "manaty-mesh-1"
STAGING_POSTE = "pc-smichea"
STAGING_VMS = (*STAGING_AGENTS, STAGING_SERVEUR)
STAGING_HOSTS = (*STAGING_VMS, STAGING_POSTE)
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"\s]+)"\s*$', re.MULTILINE)
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_PACKAGE_MERGE = re.compile(
    r"^Merge pull request #(\d+) from [^/\s]+/(integration/[^\s]+)\s*$")
_PR_MERGE = re.compile(r"^Merge pull request #(\d+) from [^/\s]+/([^\s]+)\s*$")
_LOT_BRANCH = re.compile(r"^lot/(\d+)(?:-|$)")
_LOT_REF = re.compile(r"(?<![\w/])#(\d{1,7})(?![\w])")
_TRAILER = re.compile(r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?(\d+)[ \t]*$")
#: #2692: prefixes GitHub adds to a full-suite job on a packet head — the
#: packet gate's caller job and the flow check runs of tests.yml. A required
#: job is matched exactly or under one of these, never by a loose suffix.
#: #3530 : le check du flux L161c (`tests (flux) <part>`) figure aussi dans la
#: liste des jobs de l'API ; il double le job de la part, il n'est pas un job.
_PACKET_CALLER_PREFIX = "suite complète du paquet / "
_FLUX_CHECK_PREFIX = "tests (flux) "
_JOB_PREFIXES = (_PACKET_CALLER_PREFIX, _FLUX_CHECK_PREFIX)


def _is_flow_check(name) -> bool:
    """#3530 : un check du flux L161c (`tests (flux) <part>`), pas un job.

    Le préfixe d'appel du portier (`suite complète du paquet / …`) peut le
    précéder quand la suite tourne en workflow réutilisable."""
    text = str(name or "")
    if text.startswith(_PACKET_CALLER_PREFIX):
        text = text[len(_PACKET_CALLER_PREFIX):]
    return text.startswith(_FLUX_CHECK_PREFIX)


def _bare_job_name(name: str) -> str:
    """The job name without GitHub's packet-caller and flow prefixes (#2692)."""
    for prefix in _JOB_PREFIXES:
        name = name.removeprefix(prefix)
    return name


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
    if floor == "major":
        raise ReleaseError("a major version needs the owner's go (content is major)")
    if floor == "unknown":
        raise ReleaseError("the content's version level is unknown "
                           "(scripts/version-level.py): no release")
    if bump == "patch":
        return version_text(bump_patch(triple))
    if bump != "level":
        raise ReleaseError("version_bump: level | patch expected, got %r" % bump)
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
    if floor == "unknown":
        raise ReleaseError("the content's version level is unknown "
                           "(scripts/version-level.py): no release")
    if previous is not None and version_level(previous["version"], version) == "major":
        raise ReleaseError("a major version needs the owner's go")
    if floor == "major":
        raise ReleaseError("a major version needs the owner's go (content is major)")
    if bump != "level":
        # Explicit `version_bump=patch` is an operator override; a major
        # version is refused above, and unknown is never guessed.
        return
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
         input_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args], input=input_text, capture_output=True,
            text=True, timeout=timeout,
            env=dict(os.environ if env is None else env, GIT_TERMINAL_PROMPT="0"))
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


#: #3186 : durée de vie de la lecture des checks requis de la cible (une
#: coupure lit plusieurs commits ; la protection ne change pas entre eux)
REQUIRED_TTL_S = 300.0
_REQUIRED: dict = {}


def required_checks(project: "ProjectRelease") -> tuple | None:
    """#3186 (décision owner #128512) : les checks requis de la cible, lus à
    la forge (protection et rulesets, `fire.required_checks`) — les agrégats
    dont la release exige une conclusion. Une cible sans protection n'en a
    aucun. Relus au plus toutes les `REQUIRED_TTL_S`.

    Une lecture impossible est passagère, comme celle de la CI ; un refus de
    droits prouvé (403, #2344) rend None : un identifiant sans droits est un
    non concluant (§3), il ne retient pas la release, qui lit alors la tête
    sans requis connus et le dit dans son rapport."""
    from . import fire, land, packet_automerge
    slug = github_slug(project)
    key = (slug, project.target)
    now = time.monotonic()
    cached = _REQUIRED.get(key)
    if cached is not None and now - cached[0] < REQUIRED_TTL_S:
        return cached[1]
    try:
        pairs = tuple(fire.required_checks(slug, project.target, None,
                                           gh=packet_automerge.gh_of(plan_github.Gh())))
    except land.ForbiddenLandError:
        pairs = None
    except (land.LandError, plan_github.GithubError) as exc:
        raise TransientReleaseError("required checks of %s unreadable: %s"
                                    % (project.target, exc)) from exc
    _REQUIRED[key] = (now, pairs)
    return pairs


def ci_reading(project: "ProjectRelease", sha: str) -> dict:
    """Exact CI, with ameesh's required aggregate (#3244, decision #128512).

    A workflow's conclusion includes optional jobs. For ameesh, `tests`
    must itself complete successfully; other jobs block only on FAILURE.
    Other workflows and external checks retain their existing policy.

    #3186 : les checks requis de la cible y sont lus par leur nom, pour tous
    les projets : un agrégat requis jamais conclu est une preuve absente
    (`unconcluded`, relancé une fois par `rerun_head`) et un workflow
    facultatif annulé reste non concluant ; a failed read is transient.
    """
    target = radar.Project(name=project.name, repo=project.repo, target=project.target,
                           github=project.github)
    required = required_checks(project)
    try:
        reading = radar.commit_ci(target, sha, required=required or ())
        if required is None:
            reading["required_unread"] = True
        if project.name != "ameesh":
            return reading
        suite = suite_reading(project, sha)
        run = _suite_attempt(suite)
        aggregate_ok = run is not None and _aggregate_success(run)
        # These workflows are reclassified job by job below, including the
        # separate macOS suite (#3198). Its unfinished rollup must not block
        # a successful tests aggregate; a job FAILURE still blocks below.
        managed = {"tests", "paquet", "macos"}
        # #3530 : un check « tests (flux) » double le job de la part ; ses
        # non concluants ne se consignent pas deux fois. Un flux **négatif**
        # reste une preuve (le publisher peut avoir écrit `fin=echec` avant
        # que le job ne soit annulé) : il n'est jamais filtré.
        negative = [name for name in reading.get("negative", ())
                    if name not in managed]
        pending = [name for name in reading.get("pending", ())
                   if name not in managed and not _is_flow_check(name)]
        inconclusive = [name for name in reading.get("inconclusive", ())
                        if name not in managed and not _is_flow_check(name)]
        # B3 d'impl-04 (#3186) : le filtre vise les rollups reclassés job par
        # job, pas un check requis rouge — celui d'une autre suite que la
        # tentative lue ci-dessous resterait sinon masqué.
        negative += [name for name in reading.get("required_negative", ())
                     if name not in negative]
        for attempt in _suite_runs(suite, run):
            for job in attempt["jobs"]:
                label = "%s (%s; %s)" % (
                    job["name"], job["conclusion"] or job["status"], attempt["url"])
                if _job_negative(job):
                    negative.append(label)
                elif _is_flow_check(job.get("name")):
                    # #3530 : doublon non concluant/en attente du job de la
                    # part, déjà lu ; jamais un second lot de correction.
                    continue
                elif _job_inconclusive(job):
                    inconclusive.append(label)
        if not aggregate_ok:
            # Never silently reject an absent/cancelled aggregate with empty
            # diagnostic lists, and never let an older green redeem it.
            pending.append("suite complète : agrégat tests non validé%s" % (
                " (%s)" % run["url"] if run else " (tentative absente)"))
        # #3186 : un requis jamais conclu (`unconcluded`) reste une preuve
        # absente, même hors des workflows reclassés ici.
        acceptable = bool(aggregate_ok and not negative and not pending
                          and not reading.get("missing") and not reading.get("unconcluded"))
        return dict(reading, green=acceptable, acceptable=acceptable,
                    negative=negative, pending=pending, inconclusive=inconclusive)
    except plan_github.GithubError as exc:
        raise TransientReleaseError("CI of %s unreadable: %s" % (sha[:12], exc)) from exc


def suite_reading(project: "ProjectRelease", sha: str) -> dict:
    """The runs and jobs of the exact head (#2692); a failed read is transient."""
    target = radar.Project(name=project.name, repo=project.repo, target=project.target,
                           github=project.github)
    try:
        suite = radar.commit_suite(target, sha)
        # Reuse the host's declared full-suite tolerances (#566), rather
        # than inferring continue-on-error from a green workflow rollup.
        # #3535 : le check du flux L161c double le job de sa part ; son nom
        # porte `tests (flux) ` — retiré avant le match, comme le préfixe
        # d'appel du portier, pour qu'une part tolérée ne rougisse pas la
        # tête par son flux.
        for run in suite.get("runs", ()):
            if run.get("workflow") not in ("tests", "paquet"):
                continue
            for job in run["jobs"]:
                name = _bare_job_name(job["name"])
                job["tolerated"] = any(radar._job_matches(pattern, name)
                                       for pattern in project.suite_tolerated)
        return suite
    except plan_github.GithubError as exc:
        raise TransientReleaseError("CI jobs of %s unreadable: %s" % (sha[:12], exc)) from exc


def _release_commit(repo: str, sha: str) -> bool:
    """A `[skip ci]` release commit inherits its first parent's CI state."""
    message = _git(repo, "log", "-1", "--format=%B", sha).stdout or ""
    return "[skip ci]" in message and ("ameesh-release:" in message
                                       or message.lstrip().startswith("release:"))


def _landing_release(repo: str, sha: str) -> bool:
    """A release landing merge M = merge(main, V), with two parents."""
    message = _git(repo, "log", "-1", "--format=%B", sha).stdout or ""
    if "ameesh-release:" not in message or not message.lstrip().startswith("release:"):
        return False
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    return len(parents) == 3


def reading_is_green(reading: dict) -> bool:
    """#3085 : la tête est verte pour la release quand elle l'est strictement,
    ou quand chaque check a conclu sans qu'aucun ne soit négatif.

    Un job sauté, annulé pour quota ou une panne de démarrage ne démontre pas
    un défaut : il se consigne (le plan le cite et un lot de correction
    s'ouvre) et ne retient pas la release. Un check encore en cours, un rouge
    prouvé, un préfixe requis absent ou une tête sans aucun check ne sont
    jamais verts (`acceptable` est alors faux). Une lecture sans clé
    `acceptable` garde l'ancien sens strict.

    #3186 (décision owner #128512) : un run terminé sans avoir conclu
    lui-même (`unconcluded` : annulé, expiré, démarrage raté) est une preuve
    absente, pas un non concluant — `acceptable` est faux et la tête ne
    l'est pas ; `rerun_head` relance ce run une fois."""
    if reading.get("green"):
        return True
    if "acceptable" in reading:
        return bool(reading.get("acceptable"))
    return False


def ci_state_text(reading: dict | None) -> str:
    """#3641 : l'état réel de la CI de la tête, pour les rapports de coupe.

    #2692 ne laissait hériter qu'une tête **en cours** ; #3633 étend
    l'héritage d'arbre à une tête rouge, annulée ou absente. Le rapport ne
    peut donc plus écrire « en cours » par défaut : il nomme ce qui a été lu,
    et la preuve d'arbre reste citée à part.
    """
    if not reading:
        return "absente"
    if reading_is_green(reading):
        return "verte"
    if reading.get("negative") or reading.get("required_negative"):
        return "rouge"
    if reading.get("pending"):
        return "en cours"
    if reading.get("unconcluded"):
        return "non conclue"
    if reading.get("missing"):
        return "absente"
    if reading.get("inconclusive"):
        return "non concluante"
    if reading.get("failed"):
        return "rouge"
    if not reading.get("runs", 1):
        return "absente"
    return "non verte"


def commit_is_green(repo: str, project: "ProjectRelease", sha: str,
                    reading: dict | None = None) -> bool:
    """The exact commit is green; a non-green release commit inherits its chain.

    The exact CI reading always wins, including on a landing merge M: content
    re-validated green on M is green (B1). Only a commit whose exact CI is not
    green may inherit, and a landing merge never inherits: its first parent is
    the red main, its published green content is found through the tag
    (last_green). The chain is followed iteratively through `[skip ci]` release
    commits, with no fixed depth: a long series of red-landing releases must
    stay green as a chain.

    #3085 : une lecture `acceptable` (tous les checks ont conclu, aucun
    négatif) vaut le vert ; voir `reading_is_green`.
    """
    seen = set()
    current = sha
    first = True
    while current and current not in seen:
        seen.add(current)
        current_reading = reading if first else None
        first = False
        current_reading = (ci_reading(project, current)
                           if current_reading is None else current_reading)
        if reading_is_green(current_reading):
            return True
        if _landing_release(repo, current):
            return False
        if not _release_commit(repo, current):
            return False
        parent = _git(repo, "rev-parse", "--verify", "%s^1" % current)
        if parent.returncode:
            return False
        current = (parent.stdout or "").strip()
    return False


def _latest_green_tag(repo: str, head: str, project: "ProjectRelease") -> str | None:
    """The highest reachable release tag whose first parent is green."""
    latest = latest_tag(repo, head)
    if not latest or not _release_commit(repo, latest["commit"]):
        return None
    if not is_ancestor(repo, latest["commit"], head):
        return None
    parent = _git(repo, "rev-parse", "--verify", "%s^1" % latest["commit"])
    if parent.returncode:
        return None
    if commit_is_green(repo, project, (parent.stdout or "").strip()):
        return latest["commit"]
    return None


def _release_on_green_tree(repo: str, project: "ProjectRelease", candidates, reference):
    """#3641 : la plus récente release dont le parent porte une preuve d'arbre.

    Une coupe sur une tête rouge à arbre vert crée V sur M (`[skip ci]`) ; la
    coupe suivante doit repartir de V, déjà taguée, et non du vert antérieur
    B (sinon `staging_pending.regressions(B)` retient le paquet suivant et le
    tag existe déjà). On rejoue la preuve d'arbre sur le parent M de V contre
    la référence verte B : mêmes gardes de contenu que #2692/#3633. Rend le
    candidat `(release, parent, lecture du parent, lecture de la release)`, ou
    None."""
    if not reference:
        return None
    for candidate in candidates:
        release, merge, reading, _own = candidate
        if tree_green(repo, project, merge, reference, reading=reading) is not None:
            return candidate
    return None


def last_green(repo: str, head: str, project: "ProjectRelease", limit: int = 100,
               *, evidence: dict | None = None) -> str | None:
    """The green content of the history, without a release-chain cap.

    A red landing merge `M = merge(main, V)` names the published green content
    as its second parent; the latest release tag is then the base (B1). A
    landing merge whose own CI is green is the green content itself, also for
    a red child of it. A landing merge is recognised directly, so any number of
    successive red cutoffs works. Without a landing merge, only a truly green
    first-parent commit is accepted; if none is found the caller refuses (N5).

    #3641 : un commit de release `V` posé sur une tête rouge dont l'arbre est
    celui d'une tête de paquet verte (#3633) est le vert le plus récent. Son
    propre CI est absente (`[skip ci]`) et son parent M est rouge ; sans cette
    reconnaissance, la coupe suivante sautait V et repartait de B.

    `evidence` reçoit la lecture du vert retenu et, quand c'est une release sur
    arbre vert, `tree_reference` : le vert B dont la suite a prouvé l'arbre de
    M. La coupe part de V, mais la preuve de suite se rejoue contre B.
    """
    cursor = head
    candidates: list[tuple[str, str, dict, dict]] = []
    for _ in range(limit):
        reading = ci_reading(project, cursor)
        if commit_is_green(repo, project, cursor, reading=reading):
            chosen = _release_on_green_tree(repo, project, candidates, cursor)
            if chosen is not None:
                if evidence is not None:
                    evidence.update(chosen[3])
                    # #3641 : la référence de suite de `tree_green` est le
                    # vert qui a prouvé l'arbre de M, jamais la release V
                    # elle-même (sans suite) ; la coupe, elle, part de V.
                    evidence["tree_reference"] = cursor
                return chosen[0]
            if evidence is not None:
                evidence.update(reading)
            return cursor
        if _landing_release(repo, cursor):
            # Exact CI first: a re-validated landing is green content, never
            # an inheritance from its red first parent.
            reference = _latest_green_tag(repo, head, project)
            chosen = _release_on_green_tree(repo, project, candidates, reference)
            if chosen is not None:
                if evidence is not None:
                    evidence["tree_reference"] = reference
                return chosen[0]
            return reference
        if _release_commit(repo, cursor):
            parent = _git(repo, "rev-parse", "--verify", "%s^1" % cursor)
            if not parent.returncode:
                merge = (parent.stdout or "").strip()
                if merge and packet_head(repo, merge) is not None:
                    candidates.append(
                        (cursor, merge, ci_reading(project, merge), reading))
        parent = _git(repo, "rev-parse", "--verify", "%s^1" % cursor)
        if parent.returncode:
            break
        cursor = (parent.stdout or "").strip()
    return None


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
    staging_executor: str = ""
    report: str = "smichea"
    github: str = ""
    window_s: float = WINDOW_S
    staging_timeout_s: float = STAGING_TIMEOUT_S
    lock_ttl_s: float = LOCK_TTL_S
    lock_wait_s: float = LOCK_WAIT_S
    version_bump: str = "level"
    suite_tolerated: tuple = ()
    model: str = "semver"

    @classmethod
    def from_config(cls, cfg, name: str, entry: dict) -> "ProjectRelease":
        if not isinstance(entry, dict):
            raise ReleaseError("release.projects.%s: an object is required" % name)
        unknown = set(entry) - {"repo", "target", "remote", "staging", "report",
                                "github", "window_s", "staging_timeout_s", "staging_executor",
                                "lock_ttl_s", "lock_wait_s", "version_bump", "model"}
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
        model = entry.get("model", "semver")
        if model not in ("semver", "nexlink"):
            raise ReleaseError("release model: semver | nexlink expected")
        if model == "nexlink" and (name != "nexlink" or target != "develop"
                                    or "staging" in entry):
            raise ReleaseError("nexlink model requires project nexlink, target develop, "
                               "and its closed staging workflow list (no staging script)")
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
        suite_project = radar.settings_of(cfg).projects.get(name)
        tolerated = radar._suite_jobs_of(
            suite_project, ".github/workflows/tests.yml", "full_suite_tolerated") or ()
        return cls(
            name=name, repo=repo, target=target.strip(), remote=str(entry.get("remote", "origin")),
            staging=staging,
            staging_executor=str(entry.get("staging_executor") or ""),
            report=str(report), github=str(entry.get("github", "") or ""),
            window_s=number("window_s", WINDOW_S),
            staging_timeout_s=number("staging_timeout_s", STAGING_TIMEOUT_S),
            lock_ttl_s=number("lock_ttl_s", LOCK_TTL_S),
            lock_wait_s=number("lock_wait_s", LOCK_WAIT_S), version_bump=bump,
            suite_tolerated=tolerated, model=model)


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
    # N4: the base carries the previous tag's exact tree, nothing to release.
    empty: bool = False
    #: #2692: the target head's CI is not green (#3633: red, cancelled, absent
    #: or in progress), but its tree is the packet head's green content; the
    #: evidence (head, runs) or None.
    tree_green: dict | None = None
    #: #3291: this green candidate would discard content already active.
    kept_active: dict[str, str] = dataclasses.field(default_factory=dict)


def same_tree(repo: str, left: str, right: str) -> bool:
    """Both commits carry the same tree (no content between them)."""
    trees = [_git(repo, "rev-parse", "--verify", "%s^{tree}" % ref) for ref in (left, right)]
    if any(tree.returncode for tree in trees):
        return False
    return trees[0].stdout.strip() == trees[1].stdout.strip()


def packet_head(repo: str, sha: str) -> str | None:
    """The head a packet merge integrated, when the merge carries its tree.

    A packet merge `M = merge(main, H)` has the packet head as second parent
    (`H`). A conflict resolution makes M's tree differ from H's: the CI of H
    then proves nothing about the merged content and no proof is inherited
    (#2692). A fast-forward has no merge commit (and its exact CI is H's).
    """
    subject = _out(repo, "log", "-1", "--format=%s", sha)
    if not _PACKAGE_MERGE.match(subject):
        return None
    parents = _out(repo, "rev-list", "--parents", "-n", "1", sha).split()
    if len(parents) != 3:
        return None
    head = parents[2]
    return head if same_tree(repo, sha, head) else None


def _same_job(required: str, actual: str) -> bool:
    """A job the target ran, as the packet head names it.

    GitHub surfaces a reusable workflow's jobs under the caller job
    (`suite complète du paquet / …`) and the flow check runs under
    `tests (flux) …`: only those two known prefixes are accepted, so a
    similarly named unrelated job never stands in for a required one.
    """
    return actual == required or any(actual == prefix + required
                                     for prefix in _JOB_PREFIXES)


def _job_success(job: dict) -> bool:
    return job.get("status") == "completed" and job.get("conclusion") == "success"


def _job_waiting(job: dict) -> bool:
    """#3530 : un job encore en cours (`in_progress`, `queued`, …) attend ;
    ce n'est pas un résultat non concluant et il n'ouvre aucun lot avant la
    fin du run ou le délai du job."""
    return str(job.get("status") or "").strip().lower() != "completed"


def _job_inconclusive(job: dict) -> bool:
    """A job that neither succeeded nor was skipped by its `if:` (#3244).

    On every push to main, `version number`, `lecteurs de la doc` and
    `autre système de fichiers` are skipped by design: the aggregate decides
    whether they were due, so they are neither consigned nor reopened.
    #3530 : un job encore en cours attend ; il n'est pas consigné."""
    if _job_waiting(job):
        return False
    tolerated = job.get("tolerated", False)
    return not _job_success(job) and job.get("conclusion") != "skipped" and not tolerated


def _aggregate_success(run: dict) -> bool:
    """The stable aggregate certifies its dependencies, regardless of macOS."""
    aggregates = _aggregates(run)
    return bool(aggregates) and all(_job_success(job) for job in aggregates)


def _aggregates(run: dict) -> list[dict]:
    # tests.yml deliberately publishes a distinct context on manual runs.
    return [job for job in run.get("jobs", ())
            if job["name"] in ("tests", "tests (dispatch)",
                               "suite complète du paquet / tests")]


def _suite_attempt(suite: dict) -> dict | None:
    """Latest suite attempt, including failures, never an auxiliary run.

    A skipped tests run on ci-macos has no jobs. A coverage-only packet
    porter publishes no aggregate. Neither replaces the actual suite.
    An unfinished/aborted tests attempt still supersedes an older green,
    even if its aggregate has not been created yet.
    """
    runs = [run for run in suite.get("runs", ())
            if run.get("workflow") in ("tests", "paquet")
            and run.get("conclusion") != "skipped"
            and (run.get("workflow") == "tests"
                 or any(job["name"].startswith("suite complète du paquet / ")
                        for job in run.get("jobs", ())))]
    return max(runs, key=lambda row: int(row["id"]), default=None)


def _suite_runs(suite: dict, attempt: dict | None) -> list[dict]:
    """Latest jobs, plus failures not repaired by a newer job success.

    A cancelled or unfinished retry supplies no evidence that an earlier
    failure was fixed. Historical runs contribute only those failures,
    never successful jobs a newer suite omitted from its proof.
    """
    latest = {}
    for run in suite.get("runs", ()):
        if run.get("conclusion") == "skipped":
            continue
        workflow = run.get("workflow")
        if workflow not in latest or int(run["id"]) > int(latest[workflow]["id"]):
            latest[workflow] = run
    auxiliaries = [run for run in latest.values()
                   if run is not attempt and not _aggregates(run)
                   and (attempt is None or run.get("workflow") != attempt.get("workflow"))]
    selected = ([attempt] if attempt is not None else []) + auxiliaries

    def source(run, job):
        workflow, name = run.get("workflow"), job["name"]
        if workflow in ("tests", "paquet"):
            workflow = "tests"
            name = _bare_job_name(name)
            if name == "tests (dispatch)":
                name = "tests"
        return workflow, name

    repaired = set()
    for run in sorted(suite.get("runs", ()), key=lambda row: int(row["id"]), reverse=True):
        if run.get("conclusion") == "skipped":
            continue
        failures = [job for job in run.get("jobs", ())
                    if _job_negative(job) and source(run, job) not in repaired]
        if run not in selected and failures:
            selected.append(dict(run, jobs=failures))
        groups = {}
        for job in run.get("jobs", ()):
            groups.setdefault(source(run, job), []).append(job)
        repaired.update(key for key, jobs in groups.items() if all(_job_success(job) for job in jobs))
    return selected


def _job_negative(job: dict) -> bool:
    # A workflow's success never hides a failing required job. Only the
    # configured tolerance attested by suite_reading exempts a job.
    return job.get("conclusion") == "failure" and not job.get("tolerated", False)


def _full_suite_run(suite: dict) -> dict | None:
    """The run of a commit that carries the target's full suite.

    Use the same latest attempt as ci_reading: an older successful matrix
    cannot redeem a newer failed attempt. A run without a successful
    `tests` aggregate certifies nothing. The workflow
    itself may be unfinished or cancelled because of an optional job.
    """
    run = _suite_attempt(suite)
    if run is None or not _aggregate_success(run):
        return None
    if any(_job_negative(job) for evidence in _suite_runs(suite, run) for job in evidence["jobs"]):
        return None
    return run


def _suite_proof(reference: dict, candidate: dict) -> dict | None:
    """The candidate proves the target's full suite, job by job.

    Every job that ran green in the target's full-suite run must exist on the
    candidate under the same name (a caller/flow prefix is allowed). Its
    `tests` aggregate must complete successfully; other jobs may be non
    conclusive, but a non-tolerated FAILURE always blocks (#3244). The suite is re-read on
    the last green commit, so a packet that changes the job set (a workflow
    edit, a reduced matrix) inherits nothing: it needs its own green target CI. Returns
    `{reference_run, runs}` when the proof holds, else None.
    """
    reference_run = _full_suite_run(reference)
    if reference_run is None:
        return None
    # Only the jobs that actually ran prove the target's suite. A job skipped
    # on the target (a doc lane, a version lane) executed nothing: its absence
    # on the packet head is not a hole, and a tolerated failure certifies
    # nothing either.
    required = {}
    for job in reference_run["jobs"]:
        if job["conclusion"] == "success" and not job.get("tolerated", False):
            name = job["name"]
            if name.startswith("suite complète du paquet / "):
                name = name[len("suite complète du paquet / "):]
            required["tests" if name == "tests (dispatch)" else name] = "success"
    attempt = _full_suite_run(candidate)
    if attempt is None:
        return None
    candidates = [(run, job) for run in _suite_runs(candidate, attempt)
                  for job in run.get("jobs") or ()]
    if any(_job_negative(job) for run, job in candidates):
        return None
    proof_runs: dict[int, dict] = {}
    for name, conclusion in required.items():
        hits = [(run, job) for run, job in candidates
                if not job.get("tolerated", False)
                and (_same_job(name, job["name"])
                     or (name == "tests" and job in _aggregates(run)))]
        if not hits:
            return None
        for run, job in hits:
            proof_runs[int(run["id"])] = run
    if not proof_runs:
        return None
    return {"reference_run": reference_run,
            "runs": [proof_runs[key] for key in sorted(proof_runs)]}


def _run_evidence(run: dict) -> dict:
    """The durable identity of a run cited by the notes and the report."""
    return {"id": run["id"], "url": run["url"], "workflow": run["workflow"],
            "event": run["event"], "jobs": len(run["jobs"]),
            "inconclusive": ["%s (%s)" % (job["name"], job["conclusion"] or job["status"])
                             for job in run["jobs"] if _job_inconclusive(job)]}


def tree_green(repo: str, project: "ProjectRelease", sha: str, reference: str | None,
               reading: dict | None = None) -> dict | None:
    """#2692: the head's tree is content a green packet head validated.

    The packet merge must carry the packet head's exact tree (no conflict
    resolution), that head must be green, and its jobs must prove the same
    full suite as the target's last green commit. Returns the evidence or
    None; a read failure stays transient.

    #3633 (décision owner #141353 a, 26/10/2026) : la CI propre de la tête
    n'entre plus en compte. Rouge, annulée, absente ou en cours, une tête dont
    l'arbre est celui d'une tête de paquet entièrement verte vaut verte pour
    la coupe — le contenu a déjà passé la suite complète, et le critère se
    calcule sans jugement (canon §7, §11 bis). Avant, seule une tête encore en
    cours héritait.
    """
    own = ci_reading(project, sha) if reading is None else reading
    if own.get("green"):
        return None
    if not reference or reference == sha:
        return None
    head = packet_head(repo, sha)
    if head is None:
        return None
    head_reading = ci_reading(project, head)
    if not head_reading.get("green"):
        return None
    candidate = suite_reading(project, head)
    # The target's suite is read on its last green commit; a release commit
    # inherits its first parent's content and has no run of its own.
    cursor = reference
    for _ in range(4):
        proof = _suite_proof(suite_reading(project, cursor), candidate)
        if proof is not None:
            tree = _out(repo, "rev-parse", "--verify", "%s^{tree}" % sha)
            return {"head": head, "tree": tree, "reference": cursor,
                    "reference_run": _run_evidence(proof["reference_run"]),
                    "runs": [_run_evidence(run) for run in proof["runs"]]}
        # A failed match on a tested commit is final: an older, smaller
        # suite cannot certify the current target's required jobs.
        if not _release_commit(repo, cursor):
            break
        parent = _out(repo, "rev-parse", "--verify", "%s^1" % cursor)
        if not parent:
            break
        cursor = parent
    return None


def tree_proof_text(head: str, proof: dict) -> str:
    """The validation line cited in the release notes (SHA and run, #2692)."""
    runs = ", ".join(run["url"] or "run %s" % run["id"] for run in proof["runs"])
    reference = proof["reference_run"]
    caveats = ["%s: %s" % (run["url"], ", ".join(run["inconclusive"]))
               for run in proof["runs"] if run.get("inconclusive")]
    return ("tree of %s equals packet head %s, whose full suite (same jobs as the "
            "target's green %s) is green: %s%s"
            % (head[:12], proof["head"][:12], proof["reference"][:12], runs,
               "; reference run %s" % reference["url"] if reference.get("url") else "")
            + ("; non concluants: " + "; ".join(caveats) if caveats else ""))


def build_plan(cfg, project: ProjectRelease, *, now: float,
               version: str | None = None, db=None) -> Plan:
    if project.model == "nexlink":
        from . import nexlink_release
        return nexlink_release.plan(cfg, project, now=now, version=version)
    rules, source = compiled_rules(cfg, project.name)
    cutoff_at, cutoff_label = cutoff_slot(rules, now)
    fetch(project.repo, project.remote, project.target)
    head = commit_of(project.repo, "%s/%s" % (project.remote, project.target))
    reading = ci_reading(project, head)
    green = commit_is_green(project.repo, project, head, reading=reading)
    tree_proof = None
    base_reading = reading
    if not green:
        base_reading = {}
        green_commit = last_green(project.repo, head, project, evidence=base_reading)
        # #2692: a packet merge whose tree is the green packet head's content
        # is green — #3633 : whatever the target's own CI says (décision
        # owner #141353 a).
        # #3641 : V (release sur tête rouge à arbre vert) est la base de
        # coupe, mais la suite se prouve contre le vert B qui a validé l'arbre
        # de sa tête M — V n'a pas de suite propre.
        tree_reference = base_reading.get("tree_reference") or green_commit
        tree_proof = tree_green(project.repo, project, head, tree_reference,
                                reading=reading)
        if tree_proof is not None:
            green, green_commit = True, head
    else:
        green_commit = head
    base = green_commit or head
    from . import staging_pending
    active = staging_pending.regressions(project, base, db=db)
    if active:
        current = version_at(project.repo, next(iter(active.values())))
        return Plan(project=project, rules=rules, canon_commit=source.commit,
                    cutoff_at=cutoff_at, cutoff_label=cutoff_label, head=head, ci=reading,
                    green=green, last_green=green_commit, base=base,
                    current_version=current, previous=None, version=current,
                    floor="patch", floor_reason="keep the newer active content",
                    packets=[], direct=[], migrations=[], included=[], excluded=[],
                    notes="", empty=True, kept_active=active)
    current = version_at(project.repo, base)
    previous = latest_tag(project.repo, base)
    if previous and version_tuple(current) < version_tuple(previous["version"]):
        raise ReleaseError("the target %s is behind its tag %s: refusing to release"
                           % (base[:12], previous["tag"]))
    if previous and same_tree(project.repo, previous["commit"], base):
        # N4: a red main whose last green is already tagged (or a main with
        # nothing merged since the tag) is a no-op, never an empty release.
        return Plan(project=project, rules=rules, canon_commit=source.commit,
                    cutoff_at=cutoff_at, cutoff_label=cutoff_label, head=head, ci=reading,
                    green=green, last_green=green_commit, base=base,
                    current_version=current, previous=previous,
                    version=previous["version"], floor="patch",
                    floor_reason="nothing new since %s" % previous["tag"],
                    packets=[], direct=[], migrations=[], included=[], excluded=[],
                    notes="", empty=True)
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
        migrations=migrations,
        runs=[tree_proof_text(head, tree_proof)] if tree_proof else None,
        version_bump=project.version_bump)
    # #3085 : un résultat non concluant ne retient pas la release ; il est
    # cité dans les notes et un lot de correction s'ouvre (règle owner 23/10).
    checked = [(head, reading)]
    if base != head:
        checked.append((base, base_reading))
    for checked_sha, checked_reading in checked:
        if checked_reading.get("inconclusive"):
            notes += ("\n\n## Checks non concluants (non bloquants, règle owner du 23/10)\n\n"
                      "Sur le commit %s : %s. Aucun n'est un rouge prouvé ; ils sont "
                      "consignés pour correction.\n"
                      % (checked_sha[:12], ", ".join(checked_reading["inconclusive"])))
    return Plan(project=project, rules=rules, canon_commit=source.commit,
                cutoff_at=cutoff_at, cutoff_label=cutoff_label, head=head, ci=reading,
                green=green, last_green=green_commit, base=base, current_version=current,
                previous=previous, version=version, floor=floor, floor_reason=floor_reason,
                packets=packets, direct=direct, migrations=migrations,
                included=included, excluded=excluded, notes=notes,
                tree_green=tree_proof)


# --------------------------------------------------------------------------
# release commit, tag, GitHub release
# --------------------------------------------------------------------------

def _open_correction(db, project: "ProjectRelease", plan: "Plan"):
    """#3085 : ouvre le lot de correction d'un check non concluant qui n'a pas
    retenu la release (règle owner du 23/10). Suivi seulement : la release
    n'en dépend pas, et un échec d'écriture ne la défait pas.
    #3209 : un lot par check tant qu'il est ouvert, clé partagée avec le
    paquet (`radar.nonconclusive_source`) ; une nouvelle occurrence y est notée."""
    from . import work
    out = []
    for text in plan.ci.get("inconclusive") or ():
        try:
            out.append(work.add_or_note(
                db,
                title="CI non concluante sur %s@%s : corriger %s"
                      % (project.name, plan.head[:12], text),
                type="bug", priority=2, app=project.name,
                source=radar.nonconclusive_source(project.name, text),
                occurrence=("de nouveau non concluant sur la tête %s releasée en %s : %s"
                            % (plan.head[:12], plan.version, text)),
                body=("Le check suivant n'a pas conclu (ni vert ni négatif) sur la "
                      "tête %s releasée en %s : %s.\n\nRègle owner du 23/10 (canon home "
                      "§3) : un résultat non concluant ne bloque pas la release ; il se "
                      "consigne et ouvre ce lot de correction. Rejouer ou réparer le "
                      "check, sans refaire la release. Tant que ce lot est ouvert, les "
                      "occurrences suivantes y sont notées."
                      % (plan.head, plan.version, text)),
                actor="ameesh"))
        except Exception:  # noqa: BLE001 - la release est écrite, le lot est un suivi
            continue
    return out


def _unconcluded_deviation(plan: "Plan", reruns) -> list:
    """#3186 : les lignes d'écart d'une tête dont un agrégat requis n'a jamais
    conclu, ou dont les checks requis n'ont pas pu être lus (403)."""
    out = []
    if plan.ci.get("required_unread"):
        out.append("checks requis de %s illisibles (droits refusés) : agrégat requis "
                   "jamais conclu non détecté sur la tête %s (décision owner #128512)"
                   % (plan.project.target, plan.head[:12]))
    rows = plan.ci.get("unconcluded") or ()
    if rows:
        out.append("tête %s: agrégat(s) requis jamais conclu(s) (%s) — preuve absente, "
                   "tête non verte (décision owner #128512) ; %s"
                   % (plan.head[:12], ", ".join(row.get("check") or "?" for row in rows),
                      radar.describe_reruns(reruns) or "aucune relance"))
    return out


def rerun_head(project: "ProjectRelease", plan: "Plan") -> list:
    """#3186 (décision owner #128512 du 23/10) : les runs de la tête terminés
    sans avoir conclu (`plan.ci["unconcluded"]`) sont relancés une fois
    (`radar.rerun_unconcluded`) ; la release de cette coupure prend le dernier
    vert, la coupure suivante trouve la preuve. Une relance impossible est
    rapportée, elle ne retient ni ne défait la release."""
    rows = plan.ci.get("unconcluded") or ()
    if not rows:
        return []
    try:
        slug = github_slug(project)
        return radar.rerun_unconcluded(
            slug, plan.head, [(row.get("check") or "?", radar.actions_run_id(slug, row))
                              for row in rows],
            gh=plan_github.Gh())
    except (ReleaseError, plan_github.GithubError, OSError) as exc:
        return [{"check": row.get("check") or "?", "run": row.get("databaseId"),
                 "result": "error", "detail": "rerun unavailable: %s" % exc}
                for row in rows]


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


def _release_env(author: str = "ameesh release") -> dict:
    """One command-local identity for version commits, landing merges and tags."""
    return dict(os.environ,
                GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL="release@ameesh.invalid",
                GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL="release@ameesh.invalid")


def _commit(path: str, message: str, *, author: str = "ameesh release") -> str:
    proc = subprocess.run(["git", "-C", path, "commit", "--no-verify", "-q", "-m", message],
                          capture_output=True, text=True, env=_release_env(author), timeout=120.0)
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
    proc = subprocess.run(
        ["git", "-C", project.repo, "tag", "-a", tag, "-m", "ameesh %s" % version,
         release_sha], capture_output=True, text=True, env=_release_env(), timeout=120.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise ReleaseError("git tag: %s" % (detail or "failed"))
    return tag


def _landing_merge(project: ProjectRelease, plan: Plan, release_sha: str,
                   main: str) -> str:
    """M = merge(main, V), with `[skip ci]`, or a named refusal."""
    folder, worktree = _worktree(project.repo, main)
    try:
        # Git requires an identity even with --no-commit (#3527).
        merged = _git(worktree, "merge", "--no-ff", "--no-commit", release_sha,
                      env=_release_env())
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


def _remove_tree(path: str) -> None:
    """Remove a build tree, read-only entries included (assets are 0444)."""
    def writable(function, name, _info):
        os.chmod(os.path.dirname(name) or ".", 0o700)
        with contextlib.suppress(OSError):
            os.chmod(name, 0o700)
        function(name)
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onerror=writable)


def _verify_sums(folder: str) -> list[str]:
    """The asset paths of `folder`, once SHA256SUMS names and matches each."""
    import hashlib
    listed = {}
    with open(os.path.join(folder, ASSETS_SUMS), encoding="ascii") as stream:
        for line in stream:
            digest, sep, name = line.rstrip("\n").partition("  ")
            if (not sep or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or name in ("", ".", "..", ASSETS_SUMS) or "/" in name or name in listed):
                raise ReleaseError("SHA256SUMS: unreadable line")
            listed[name] = digest
    present = sorted(name for name in os.listdir(folder) if name != ASSETS_SUMS)
    if present != sorted(listed):
        raise ReleaseError("SHA256SUMS does not name exactly the built assets")
    for name in present:
        with open(os.path.join(folder, name), "rb") as stream:
            if hashlib.sha256(stream.read()).hexdigest() != listed[name]:
                raise ReleaseError("SHA256SUMS does not match %s" % name)
    return [os.path.join(folder, name) for name in present + [ASSETS_SUMS]]


def build_release_assets(project: ProjectRelease, sha: str, folder: str) -> list[str]:
    """Build and verify the release assets of `sha` in `folder` (#4786).

    `[]` when the commit carries no asset build; ReleaseError on a failure.
    `folder` is private and its ancestors must not be group/world writable
    (construction-runtime.py refuses them).
    """
    if not sha or any(_git(project.repo, "cat-file", "-e", "%s:%s" % (sha, path)).returncode
                      for path in ASSETS_SOURCES):
        return []
    # the whole directory of the commit: the script loads its siblings
    tools = os.path.join(folder, "outils")
    os.mkdir(tools, 0o700)
    archive = os.path.join(folder, "outils.tar")
    proc = _git(project.repo, "archive", "--format=tar", "-o", archive, sha,
                os.path.dirname(ASSETS_SCRIPT))
    if proc.returncode:
        raise ReleaseError("%s unreadable in %s" % (os.path.dirname(ASSETS_SCRIPT), sha[:12]))
    import tarfile
    with tarfile.open(archive) as tar:
        tar.extractall(tools, filter="data")
    output = os.path.join(folder, "actifs")
    try:
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(tools, ASSETS_SCRIPT),
             "--assets", "--clone", os.path.abspath(project.repo), "--sha", sha,
             "--output", output],
            capture_output=True, text=True, timeout=ASSETS_TIMEOUT_S, cwd=folder)
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError("asset build timed out after %ss" % ASSETS_TIMEOUT_S) from exc
    except OSError as exc:
        raise ReleaseError("asset build could not start: %s" % exc) from exc
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise ReleaseError("asset build failed: %s" % (detail or proc.returncode))
    try:
        return _verify_sums(output)
    except OSError as exc:
        raise ReleaseError("assets unreadable: %s" % exc) from exc


def ensure_github_release(project: ProjectRelease, tag: str, version: str,
                          notes: str, sha: str = "",
                          deviations: list | None = None) -> bool:
    """Create the GitHub release if absent; True when it exists afterwards.

    The assets of `sha` (#4786) are built, verified and attached at creation.
    An asset failure is a deviation, never a held release: the release is
    created without them, and the handoff refuses a release without assets.
    An existing release is left as it is.
    """
    slug = github_slug(project)
    try:
        gh = plan_github.Gh()
        code, _out_text, _err = gh.run(["release", "view", tag, "-R", slug])
        if code == 0:
            return True
        folder = tempfile.mkdtemp(prefix="ameesh-release-notes-")
        path = os.path.join(folder, "notes.md")
        work = None
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(notes)
            assets = []
            try:
                # under HOME: /tmp or a 0775 ~/.cache fails the build's refusal
                # of group/world-writable parents
                work = tempfile.mkdtemp(prefix=".ameesh-actifs-",
                                        dir=os.path.expanduser("~"))
                assets = build_release_assets(project, sha, work)
            except (ReleaseError, OSError) as exc:
                if deviations is not None:
                    deviations.append("release %s sans actifs : %s" % (tag, exc))
            code, out, err = gh.run(["release", "create", tag, "-R", slug, "--verify-tag",
                                     "--title", "ameesh %s" % version,
                                     "--notes-file", path, *assets])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            if work is not None:
                _remove_tree(work)
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


def read_attestation(sha: str, db=None) -> dict | None:
    """The durable attestation of `sha`, if present and complete.

    Two sources, the same proof: the local file (the complete one `actif.py`
    writes, with the emission ledger) and the shared `ameesh_events` event
    (#3981, compact), which makes a sequence run from a VM visible to the
    poste. The most complete one wins and is written back locally, so
    `attestation_path` and the local readers (`staging_pending`) keep their
    file. `db=None` keeps the local-only behaviour (dry runs, tests).

    Complete = the agent VMs and the server proved (the server may be
    reported); the poste either proved itself or is explicitly reported absent
    (`reportes`, #3848). A reported poste does not make the VMs inactive;
    `maybe_staging` reruns the sequence only on the poste, so it catches up
    when it is back.
    """
    local = _read_attestation_file(sha)
    # le fichier local est la preuve COMPLETE : il porte aussi l'etat
    # d'emission (cle de courrier). S'il couvre tous les hotes, il prime et
    # aucune lecture de base n'est utile.
    if local is not None and len(local.get("hotes") or {}) == len(STAGING_HOSTS):
        return local
    best = local
    for data in stored_attestations(db, sha):
        if not _attestation_valid(data, sha):
            continue
        if best is None or len(data.get("hotes") or {}) > len(best.get("hotes") or {}):
            best = data
    if best is None:
        return None
    if best is not local:
        try:
            _write_attestation_file(best)
        except OSError:
            # the proof is still returned; only the local cache is missing
            pass
    return best


def _attestation_valid(data, sha: str) -> bool:
    """The shape and the commit checks of an attestation (file or event)."""
    if not isinstance(data, dict):
        return False
    stored_sha = data.get("sha")
    if not isinstance(stored_sha, str) or stored_sha[:len(sha)] != sha[:len(sha)]:
        return False
    hosts = data.get("hotes")
    reportes = data.get("reportes") or {}
    if not isinstance(hosts, dict) or not isinstance(reportes, dict):
        return False
    if set(reportes) - {STAGING_SERVEUR, STAGING_POSTE}:
        return False
    requis = (set(STAGING_AGENTS) | ({STAGING_SERVEUR} - set(reportes))
              | ({STAGING_POSTE} - set(reportes)))
    if set(hosts) != requis:
        return False
    for host, detail in hosts.items():
        if not isinstance(detail, dict):
            return False
        actif = str(detail.get("actif") or "").lower()
        if not actif or not (sha.startswith(actif) or actif.startswith(sha)):
            return False
    for host, detail in reportes.items():
        if not isinstance(detail, dict) or not str(detail.get("raison") or "").strip():
            return False
    return True


def _read_attestation_file(sha: str) -> dict | None:
    path = attestation_path(sha)
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if _attestation_valid(data, sha) else None


def _write_attestation_file(data: dict) -> str:
    """Materialise a shared attestation under the local state (0600, atomic)."""
    path = attestation_path(str(data["sha"]))
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".actif-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def stored_attestations(db, sha: str | None = None) -> list[dict]:
    """The shared staging proofs of `ameesh_events`, newest first (#3981).

    A read failure never removes the local proof: the caller falls back to the
    file. `object` is the exact commit key `actif.py` writes; without `sha`,
    every recent staging proof is returned (the active-content view).
    """
    if db is None:
        return []
    key = "staging:%s" % sha if sha and len(sha) == 40 else None
    try:
        rows = storage.of(db).events.recent_type(
            STAGING_EVENT, STAGING_EVENT_DAYS, STAGING_EVENT_LIMIT, object=key)
    except Exception:  # noqa: BLE001 — a shared-proof read never blocks the local one
        return []
    out = []
    for row in rows or ():
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict):
            out.append(data)
    return out


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
    for host in STAGING_HOSTS:
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


_UNIT_LIVE = frozenset({"active", "activating", "deactivating", "reloading", "refreshing"})


def _systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _poste_units(pid: int) -> list[str] | None:
    """Live transient workstation units started by the staging run `pid`.

    `staging.sh` names them `poste-maj-staging-$$-<gesture>`; `$$` is the pid
    of the bash started here. The systemd manager runs them, not this process
    group: killing the run leaves them running (#3049). None when their state
    is unreadable; [] without systemctl, where staging.sh starts none.
    """
    if shutil.which("systemctl") is None:
        return []
    proc = _systemctl("list-units", "--all", "--plain", "--no-legend", "--type=service",
                      "poste-maj-staging-%d-*" % pid)
    if proc is None or proc.returncode:
        return None
    units = []
    for line in proc.stdout.splitlines():
        fields = line.split()
        if fields and not fields[0].endswith(".service"):
            fields = fields[1:]  # status bullet of some systemd versions
        if len(fields) >= 3 and fields[2] in _UNIT_LIVE:
            units.append(fields[0])
    return units


def _settle_poste_units(pid: int, wait, grace_s: float | None = None) -> tuple[str, bool]:
    """Let a timed-out run's workstation units end before the worktree goes.

    A restart cut in the middle can leave the executors stopped, so the units
    get `grace_s` to finish on their own; past it they are stopped. `wait`
    pauses between two reads and keeps draining the staging output, which a
    unit started with `--pipe` holds. Returns the note for the report and
    whether the worktree may be dropped: only once no unit is seen alive,
    never on an unreadable state.
    """
    units = _poste_units(pid)
    if units == []:
        return "", True
    if grace_s is None:
        grace_s = STAGING_UNIT_GRACE_S
    seen = set(units or ())
    deadline = time.monotonic() + grace_s
    while units != [] and time.monotonic() < deadline:
        wait(STAGING_UNIT_POLL_S)
        units = _poste_units(pid)
        seen.update(units or ())
    names = ", ".join(sorted(seen)) or "poste-maj-staging-%d-*" % pid
    if units == []:
        return "; workstation unit(s) %s ended before the worktree was dropped" % names, True
    _systemctl("stop", "poste-maj-staging-%d-*" % pid, timeout=180)
    alive = _poste_units(pid)
    if alive == []:
        return ("; workstation unit(s) %s still running after %ss, stopped: "
                "the workstation may be half restarted" % (names, grace_s)), True
    return ("; workstation unit(s) %s %s after %ss and a stop: worktree kept, "
            "check the workstation"
            % (", ".join(alive) if alive else names,
               "still running" if alive else "in an unreadable state", grace_s)), False


def run_staging(project: ProjectRelease, release_sha: str, included: list[int],
                *, state_dir: str, timeout_s: float | None = None,
                db=None) -> dict:
    """Run `staging.sh` from a worktree of the release commit, under its lock."""
    if project.staging_executor:
        from . import config, staging_executor
        if db is None:
            raise TransientReleaseError("elected staging requires the shared database")
        options = staging_executor.settings(os.path.expanduser(project.staging_executor))
        if options["project"] != project.name:
            raise ReleaseError("staging executor configuration belongs to another project")
        try:
            request = staging_executor.submit(db, config.load(), options, release_sha,
                                             included=included)
        except RuntimeError as exc:
            raise ReleaseBusy(str(exc)) from exc
        return {"exit": 75, "duration_s": 0.0, "log": "", "summary": [],
                "attestation": None, "request": request["request"]}
    relative = project.staging
    if os.path.isabs(relative):
        relative = os.path.relpath(relative, project.repo)
    folder, worktree = _worktree(project.repo, release_sha)
    drop = True
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
                                        env=dict(os.environ, STAGING_TIMEOUT_S=str(
                                            timeout_s or project.staging_timeout_s)),
                                        start_new_session=True)
            except OSError as exc:
                raise ReleaseError("staging could not start: %s" % exc) from exc
            try:
                output, _ = proc.communicate(
                    timeout=timeout_s or project.staging_timeout_s)
            except subprocess.TimeoutExpired as exc:
                read = {"output": None, "partial": exc.output}

                def wait(seconds):
                    # a bounded read: a unit started with `--pipe` keeps the
                    # pipe open after the group is dead (#3049)
                    if read["output"] is not None:
                        time.sleep(seconds)
                        return
                    try:
                        read["output"], _ = proc.communicate(timeout=seconds)
                    except subprocess.TimeoutExpired as again:
                        read["partial"] = again.output or read["partial"]

                # `communicate` kills bash only; the sequence's ssh/vm.sh
                # children would survive and keep the worktree/flock. Kill the
                # whole session started for this run, then wait for it.
                _kill_group(proc, signal.SIGTERM)
                wait(STAGING_KILL_GRACE_S)
                if read["output"] is None:
                    _kill_group(proc, signal.SIGKILL)
                # the restart/rollback units of the workstation outlive the
                # group and run from this worktree (#3049)
                note, drop = _settle_poste_units(proc.pid, wait)
                if read["output"] is None:
                    wait(STAGING_DRAIN_S)
                if read["output"] is None:
                    # still held by a survivor: keep what was read, reap bash
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        proc.wait(timeout=STAGING_DRAIN_S)
                text = read["output"] if read["output"] is not None else read["partial"]
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                log.write(text or "")
                raise TransientReleaseError(
                    "staging timed out after %ss (log %s)%s"
                    % (timeout_s or project.staging_timeout_s, log_path, note)) from exc
            log.write(output or "")
        return {"exit": proc.returncode, "duration_s": round(time.time() - started, 1),
                "log": log_path,
                "output_tail": "\n".join((output or "").splitlines()[-20:]),
                "summary": staging_summary(read_journal(since_iso), release_sha),
                "attestation": read_attestation(release_sha, db)}
    finally:
        if drop:
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
    if plan and plan.tree_green:
        proof = plan.tree_green
        lines.append("CI tête: %s ; arbre identique à la tête de paquet %s, "
                     "suite complète verte (preuve %s)."
                     % (ci_state_text(plan.ci), proof["head"][:12], ", ".join(
                         run["url"] or "run %s" % run["id"] for run in proof["runs"])))
    elif plan and plan.ci:
        state = "verte" if reading_is_green(plan.ci) else "non verte"
        caveat = (" ; non concluants %s" % ", ".join(plan.ci["inconclusive"])
                  if plan.ci.get("inconclusive") else "")
        last = (" ; dernier vert %s" % plan.last_green[:12]
                if not reading_is_green(plan.ci) and plan.last_green else "")
        lines.append("CI tête: %s%s%s." % (state, caveat, last))
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


def report_recipients(cfg, db, project: ProjectRelease) -> list[str]:
    """#4219 : the configured recipient, plus the project's pilot.

    The default recipient is the first human, whose mail no agent reads: the
    1.27.0 cutoff report (staging never run) sat undelivered until the pilot
    happened to look. The pilot comes from the shared rule
    (`alert_briefs.project_pilots`, the one base failures use); an unreadable
    role keeps the configured recipient alone."""
    recipients = [project.report]
    try:
        from . import alert_briefs, registry
        pilots = alert_briefs.project_pilots(project.name, list(registry.overview(db)),
                                             cfg=cfg, db=db)
    except Exception:  # noqa: BLE001 - no readable pilot: the configured recipient
        pilots = []
    if pilots and pilots[0] not in recipients:
        recipients.append(pilots[0])
    return recipients


def send_report(cfg, db, project: ProjectRelease, body: str, payload: dict,
                *, urgent: bool = False) -> None:
    payload = {**(payload or {}), **({"urgent": True} if urgent else {})}
    for recipient in report_recipients(cfg, db, project):
        try:
            mail.send(db, work.SYSTEM_SENDER, recipient, body, kind="event",
                      payload=payload)
        except Exception as exc:  # a report failure never undoes a release
            print("release: report not delivered to %s: %s" % (recipient, exc),
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
    if project.model == "nexlink":
        from . import nexlink_release
        return nexlink_release.execute(cfg, db, project, now=now, force=force,
                                       no_staging=no_staging, version=version)
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
        from . import staging_pending
        active = staging_pending.regressions(project, release_sha, db=db)
        if active:
            return _kept_active(cfg, db, project, state, state_slot, at, label,
                                release_sha, active, source.commit)
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
        reconciled = ["reconciled an existing tag created at/after the cutoff"]
        ensure_github_release(project, recent["tag"], version_out, notes,
                              release_sha, reconciled)
        staging = (None if no_staging else
                   _staging_step(project, release_sha, included, cfg.state_dir, db=db))
        report = {"schema": SCHEMA, "result": "reconciled", "project": project.name,
                  "version": version_out, "tag": recent["tag"],
                  "release_sha": release_sha, "target_head": target_head,
                  "cutoff_at": at, "label": label, "canon_commit": source.commit,
                  "previous": previous, "staging": staging,
                  "started_at": started,
                  "deviations": reconciled}
        return finish(cfg, db, project, report, state, state_slot, at, label, rules,
                      previous, None, staging, no_staging)

    plan = build_plan(cfg, project, now=now, version=version, db=db)
    # #3186 : un run de la tête jamais conclu n'est pas une preuve ; il est
    # relancé une fois, la release suit le dernier vert.
    reruns = rerun_head(project, plan)
    if plan.kept_active:
        return _kept_active(cfg, db, project, state, state_slot, at, label,
                            plan.base, plan.kept_active, plan.canon_commit)
    if not plan.green and not plan.last_green:
        state_slot["last_cutoff"] = at
        state_slot["last_result"] = "refused-no-green"
        state_slot["last_label"] = label
        state_slot.pop("last_error", None)
        report = {"schema": SCHEMA, "result": "refused-no-green",
                  "project": project.name, "version": plan.version,
                  "target_head": plan.head, "cutoff_at": at, "label": label,
                  "ci": plan.ci, "started_at": started,
                  "deviations": ["no green first-parent commit: nothing to tag"]
                  + _unconcluded_deviation(plan, reruns),
                  "canon_commit": plan.canon_commit}
        if reruns:
            report["reruns"] = reruns
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

    if plan.empty:
        state_slot["last_cutoff"] = at
        state_slot["last_result"] = "nothing-new"
        state_slot["last_label"] = label
        state_slot.pop("last_error", None)
        report = {"schema": SCHEMA, "result": "nothing-new", "project": project.name,
                  "version": plan.version, "tag": plan.previous["tag"],
                  "release_sha": plan.previous["commit"], "target_head": plan.head,
                  "base": plan.base, "cutoff_at": at, "label": label, "ci": plan.ci,
                  "started_at": started,
                  "deviations": ["nothing new since %s: no tag, no staging"
                                 % plan.previous["tag"]]
                  + _unconcluded_deviation(plan, reruns),
                  "canon_commit": plan.canon_commit}
        if reruns:
            report["reruns"] = reruns
        report["completed_at"] = time.time()
        report["duration_s"] = round(report["completed_at"] - started, 1)
        report["report_path"] = write_report(cfg, project, report)
        save_state(cfg, state)
        if db is not None:
            send_report(cfg, db, project,
                        "Release non faite à la coupure %s : rien de nouveau depuis %s "
                        "(base %s, tête %s, CI %s). Aucun tag, aucun déploiement."
                        % (label, plan.previous["tag"], plan.base[:12], plan.head[:12],
                           "verte" if reading_is_green(plan.ci) else "non verte"),
                        report)
        return report

    deviations = _unconcluded_deviation(plan, reruns)
    if plan.ci.get("inconclusive"):
        deviations.append(
            "tête %s: checks non concluants non bloquants (%s) — consignés, lot de "
            "correction ouvert" % (plan.head[:12], ", ".join(plan.ci["inconclusive"])))
    if plan.tree_green:
        deviations.append(
            "tête %s: CI %s, arbre identique à la tête de paquet %s "
            "(suite complète verte, %s)"
            % (plan.head[:12], ci_state_text(plan.ci), plan.tree_green["head"][:12],
               ", ".join(run["url"] or "run %s" % run["id"]
                         for run in plan.tree_green["runs"])))
    elif not plan.green:
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
    ensure_github_release(project, tag, plan.version, plan.notes, version_sha, deviations)
    if plan.ci.get("inconclusive") and db is not None:
        # #3085 : la release est publiée ; le suivi du non concluant s'ouvre
        # après elle, il ne la retient ni ne la défait.
        _open_correction(db, project, plan)
    # Staging deploys the tag's commit V (the green content), never the main
    # head; `origin/main` now contains the landing merge M when main was red.
    staging = (None if no_staging else
               _staging_step(project, version_sha, plan.included, cfg.state_dir, db=db))
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
              "tree_green": plan.tree_green,
              "deviations": deviations}
    if reruns:
        report["reruns"] = reruns
    return finish(cfg, db, project, report, state, state_slot, at, label, rules,
                  plan.previous, plan, staging, no_staging)


def _kept_active(cfg, db, project, state, slot, at, label, candidate, active,
                 canon_commit):
    """Durable no-op: a cutoff never rolls staging back (#3291)."""
    report = {"schema": SCHEMA, "result": "kept-active", "project": project.name,
              "cutoff_at": at, "label": label, "candidate": candidate,
              "active": active, "canon_commit": canon_commit,
              "completed_at": time.time(),
              "deviations": ["candidate does not contain the active hosts' content; "
                             "no tag, no staging"]}
    slot.update(last_cutoff=at, last_result="kept-active", last_label=label)
    save_state(cfg, state)
    report["report_path"] = write_report(cfg, project, dict(report, version="kept-active"))
    if db is not None:
        send_report(cfg, db, project,
                    "Coupure %s : actif conservé, candidate %s plus ancienne ou divergente. "
                    "Aucun tag, aucun déploiement." % (label, candidate[:12]), report)
    return report


def _staging_step(project: ProjectRelease, release_sha: str, included: list[int],
                  state_dir: str, db=None) -> dict:
    """Run staging; a lock conflict retries, a timeout is reported as a gap."""
    from . import staging_pending
    if staging_pending.regressions(project, release_sha, db=db):
        raise ReleaseError("staging candidate would roll back active content (#3291)")
    try:
        result = maybe_staging(project, release_sha, included, state_dir, db=db)
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


def staging_local_host() -> str:
    """The host `staging.sh` runs on, by its own rule (#3848): HOTE_LOCAL,
    otherwise AMEESH_HOST / AGENT_MESH_HOST, otherwise the config `host`,
    otherwise the short hostname."""
    for name in ("HOTE_LOCAL", "AMEESH_HOST", "AGENT_MESH_HOST"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.lower()
    path = (os.environ.get("AMEESH_CONFIG") or os.environ.get("AGENT_MESH_CONFIG")
            or os.path.expanduser("~/.config/ameesh/config.json"))
    try:
        with open(path, encoding="utf-8") as stream:
            host = json.load(stream).get("host")
    except (OSError, ValueError, AttributeError):
        host = None
    if isinstance(host, str) and host.strip():
        return host.strip().lower()
    return os.uname().nodename.split(".")[0].lower()


def maybe_staging(project: ProjectRelease, release_sha: str, included: list[int],
                  state_dir: str, db=None) -> dict:
    attestation = read_attestation(release_sha, db)
    # Un poste reporte ne prouve pas le staging complet : relancer le met a jour
    # au commit actif a son retour, jamais en dessous (#3848). Lancee ailleurs
    # que sur le poste, la sequence le reporterait encore : relancer referait
    # migrations, controles et sondes des VM deja au commit, pour rien. Un
    # serveur sans acces root reporte ne justifie pas de relancer : il n'a
    # aucun runner, la prochaine release le reprendra s'il redevient joignable.
    if attestation and (STAGING_POSTE not in (attestation.get("reportes") or {})
                        or staging_local_host() != STAGING_POSTE):
        return {"exit": 0, "duration_s": 0.0, "log": "", "summary": [],
                "attestation": attestation, "skipped": "already attested"}
    return run_staging(project, release_sha, included, state_dir=state_dir, db=db)


def finish(cfg, db, project: ProjectRelease, report: dict, state: dict,
           state_slot: dict, at: float, label: str, rules, previous: dict | None,
           plan: Plan | None, staging: dict | None, no_staging: bool) -> dict:
    """Write the durable report, mail the owner, record the cutoff."""
    result = report.get("result")
    # #4219 : a skipped staging is not a successful one. A release is tagged
    # AND deployed to staging (canon §11 bis), and not deployed is not done
    # (§5): `--no-staging` is written down and the report goes out urgent.
    report["staging_ok"] = bool(staging and staging.get("exit") == 0)
    if no_staging:
        report["staging_skipped"] = "--no-staging"
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
        if no_staging:
            body += ("\n\nStaging: non lancé (--no-staging). La release n'est pas "
                     "active en staging (§5, §14) : lancer la séquence staging de %s."
                     % str(report.get("release_sha") or "?")[:12])
        body += "\n\nRapport JSON: %s\nJournal de staging: %s" % (
            path, (staging or {}).get("log", "—"))
        send_report(cfg, db, project, body, report,
                    urgent=not report["staging_ok"] or result == "refused-red")
    return report
