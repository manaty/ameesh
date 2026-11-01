# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP5) — install the mechanical role timers on a candidate host.

The packet composer, the GitHub watch, the CI watches and the alert delivery
are code. Their timers are installed on **every candidate host** of an elected
role; only the host that holds the live lease runs the command, through
``ameesh role run``. This module renders those user units and can install,
check or remove them.

#3383: the alert delivery (owner pushes, decision requests, agent briefs) was
left in the full ``ameesh-notify`` service, whose routes lived on one host
only (``pc-smichea``): when it was off, nothing was pushed (three times on
2026-10-24). The ``alerts`` pass is now a role timer too; a candidate that
cannot push to the role's responsible (and ``notify.default_human``) through
a remote channel (ntfy, slack, or nexlink with a usable fallback, #3536)
does not install the timer and reports the gap as an advisory warning
(#3402: the missing secret is not a failure, still less a deploy failure).

#3568: that warning was not enough when the *lease holder* was a host without
a route: the `alerts` timer exists only on route-capable candidates, so none
of them ran it and the owner was silent (2026-10-25, agents-1 holder without
ntfy, agents-2 standby with one). The `alerts` service now declares
``--pass alerts``: :mod:`ameesh.role_run` reads the route each candidate
publishes with its standing (``role_standings.notify_ok``) and runs the pass
on the holder when it can push, otherwise on the first candidate that can.
The delivery is elected by route, not only by the lease; the lease itself is
unchanged and a host without a route can still hold it (#3402).

The role carried by default is ``mesh-design``: it is already elected on
several hosts and its lease is the one that moves when a host stops. The
GitHub watcher named by the canon (``mty-amh-docs-01``) remains the recipient
of the intake mail; only the mechanical collection is elected.

Two consequences are made explicit instead of silent:

* the project→repository mappings of the canon's ``github_watch`` cards are
  completed in the host's ``github.projects`` when ``--install`` runs (a backup
  is kept, an existing mapping is never overwritten). The remaining settings
  (workflows, base, exclude_terms) are the operator's; when neither the host
  nor the canon declares a project, ``ci`` and ``issues`` are refused rather
  than left running every minute and reading nothing — the failure that #3327
  recurses on. ``--check`` fails on that gap, naming the missing project;
* when the timers are installed, the full ``ameesh-notify`` service skips the
  passes they carry (``--skip issues,github-watch,ci,alerts``) by a drop-in:
  the same GitHub work must not be done by both the service and the role timer
  (double reading), two delivery states must not push the same alert twice,
  and every other pass — take, budget handoff, WIP sweep — is kept by
  default. ``alerts`` is skipped only where its timer is installed.

#3854: the hourly packet tick of #3845 (``scripts/paquet-horaire.py``) is a
role timer too, under the same ``mesh-design`` lease. Its unit keeps the name
of ``deploy/systemd/ameesh-paquet-horaire.{service,timer}`` so a copy made by
hand is replaced, not doubled; it needs the dedicated clone declared in
``paquet-horaire.env``, and a candidate without it reports the gap as an
advisory warning, like the alerts route.

The units are user units, like ``ameesh-release.timer``. This module never
runs ``systemctl`` during rendering; ``--install``/``--remove``/``--check``
do, unless ``--no-apply`` is given.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import db as db_mod
from . import fingerprints
from . import plan_github
from . import registry

#: the elected role that carries the mechanical observation (EP5)
DEFAULT_ROLE = "mesh-design"
#: installed layout of the command, as the systemd units spell it
DEFAULT_BIN = "%h/.local/share/ameesh/venv/bin/ameesh"
#: override for tests and isolated hosts
SYSTEMD_DIR_ENV = "AMEESH_SYSTEMD_DIR"
#: the longest wait between two ticks of an observation timer. The witness of
#: #3327 is a takeover in less than five minutes; the passes keep their own
#: cadence (persisted cursors for the watch, the configured interval for the
#: projection), so a 90 s wake-up does not multiply GitHub calls.
MAX_OBSERVATION_INTERVAL_S = 90
#: systemd accepts ``OnUnitActiveSec=90s`` but not a bare float
_MIN_INTERVAL_S = 60
#: #3383: the alert delivery keeps the cadence of the full service it
#: replaces (``notify --interval 30``); a decision request is pushed within a
#: tick of the holder, whatever host the owner's post is.
ALERTS_INTERVAL_S = 30
#: the passes the full service always leaves to the role timers (#3327)
_OBSERVATION_PASSES = ("issues", "github-watch", "ci")
#: #4172: the backlog coherence pass closes deterministic findings and sends
#: one digest per project. A six-hour cadence is enough: a closure is a
#: backlog hygiene gesture, not a race, and the digest is idempotent.
COHERENCE_INTERVAL_S = 6 * 3600

#: #3854: the hourly packet tick (#3845), a tick every ten minutes; the script
#: composes at most one carrier per hour and otherwise follows the open one
PAQUET_PASS = "paquet-horaire"
PAQUET_UNIT = "ameesh-paquet-horaire"
PAQUET_INTERVAL_S = 600
PAQUET_ENV = "~/.config/ameesh/paquet-horaire.env"
PAQUET_SCRIPT = "scripts/paquet-horaire.py"
PAQUET_DESCRIPTION = "ameesh paquet horaire sans agent (#3845)"


class TimerError(RuntimeError):
    """The timers cannot be rendered or installed (actionable message)."""


@dataclass(frozen=True)
class TimerSpec:
    """One mechanical pass and the unit that wakes it under the role lease."""

    name: str
    only: str
    interval_s: int
    description: str
    #: non-empty when this pass cannot run on this host (missing settings)
    blocked: str = ""
    detail: str = ""
    #: #3402: blocked for a missing secret (the owner's notify route): reported,
    #: never a failure of --check or --install (canon §3, absent credential)
    advisory: bool = False

    @property
    def unit(self) -> str:
        if self.name == PAQUET_PASS:
            return PAQUET_UNIT
        return "ameesh-role-%s" % self.name

    @property
    def service(self) -> str:
        return self.unit + ".service"

    @property
    def timer(self) -> str:
        return self.unit + ".timer"

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "only": self.only,
            "interval_s": self.interval_s,
            "description": self.description,
            "blocked": self.blocked,
            "detail": self.detail,
            "advisory": self.advisory,
        }


def _clamp_interval(seconds, *, default: int) -> int:
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        value = default
    return max(_MIN_INTERVAL_S, min(MAX_OBSERVATION_INTERVAL_S, value))


def _watch_declarations(cfg, log=None):
    """The canon's ``github_watch`` cards: projects, repositories, cadences.

    The cards are the shared source of truth for the watched repositories;
    a host without the local ``github`` key can still collect them. A canon
    that cannot be read is a gap to report, never a silent empty watch.
    """
    try:
        from . import canon, github_watch
        declarations = github_watch.subscriptions(canon.load_configured(cfg), log)
    except Exception as exc:  # noqa: BLE001 - a declaration error is a gap, not a crash
        return [], "canon github_watch unreadable: %s" % " ".join(str(exc).split())[:200]
    if not declarations:
        return [], "no github_watch declaration in the canon"
    return list(declarations), ""


def _config_path(cfg) -> Path:
    raw = str(getattr(cfg, "config_path", "") or "").strip()
    if not raw:
        raise TimerError("no configuration file is loaded (AMEESH_CONFIG or "
                         "~/.config/ameesh/config.json): cannot sync github.projects")
    return Path(raw).expanduser()


def _sync_path(cfg) -> Path:
    """Write the github key where it is inherited from, not into a profile.

    A profile (#1466) replaces a top-level key as a whole. Adding ``github``
    to a profile that did not declare it would shadow the base's ``host``,
    ``interval``, ``workflows`` and ``base``; the edit therefore goes to the
    inherited base file when the profile leaves ``github`` out.
    """
    path = _config_path(cfg)
    base = str(getattr(cfg, "config_base", "") or "").strip()
    if not base:
        return path
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TimerError("profile configuration unreadable: %s: %s" % (path, exc)) from None
    if isinstance(profile, dict) and "github" not in profile:
        return Path(base).expanduser()
    return path


def _config_bytes(path: Path) -> tuple[bytes, int]:
    """Read a host configuration without following a link, and its mode."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise TimerError("configuration file not found: %s" % path) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TimerError("configuration is not a regular file: %s" % path)
    if info.st_uid != os.geteuid():
        raise TimerError("configuration is not owned by this user: %s" % path)
    if info.st_mode & 0o022:
        raise TimerError("configuration is writable by group or others: %s" % path)
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def _atomic_config_write(path: Path, data: bytes, mode: int) -> None:
    candidate = path.with_name("." + path.name + ".role-timers.tmp")
    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def sync_github_projects(cfg, declarations) -> dict:
    """Add the canon-watched projects missing from the host's ``github`` key.

    A candidate host used to be configured by hand; the canon's
    ``github_watch`` cards name every watched project and repository. Adding
    only a missing ``project: owner/repo`` entry is deterministic, never
    overwrites an existing project, and is backed up before the rewrite. The
    base/workflows settings stay the operator's: this is a repository mapping,
    not a full configuration copy.
    """
    wanted = {}
    for item in declarations or ():
        project = str(getattr(item, "project", "") or "").strip()
        repo = str(getattr(item, "repo", "") or "").strip()
        if project and repo and project.casefold() not in {key.casefold() for key in wanted}:
            wanted[project] = repo
    if not wanted:
        return {"added": [], "conflicts": [], "path": ""}
    path = _sync_path(cfg)
    raw, mode = _config_bytes(path)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TimerError("configuration is not valid JSON: %s: %s" % (path, exc)) from None
    if not isinstance(document, dict):
        raise TimerError("configuration is not a JSON object: %s" % path)
    github = document.setdefault("github", {})
    if github is None:
        github = document["github"] = {}
    if not isinstance(github, dict):
        raise TimerError("`github` is not an object in %s" % path)
    projects = github.setdefault("projects", {})
    if projects is None:
        projects = github["projects"] = {}
    if not isinstance(projects, dict):
        raise TimerError("`github.projects` is not an object in %s" % path)
    existing = {str(key).casefold(): key for key in projects}
    added, conflicts = [], []
    for project, repo in wanted.items():
        key = existing.get(project.casefold())
        if key is not None:
            current = projects[key]
            current_repo = current.get("repo") if isinstance(current, dict) else current
            if isinstance(current_repo, str) and current_repo.strip().casefold() != repo.casefold():
                conflicts.append({"project": key, "kept": current_repo.strip(), "repo": repo})
            continue
        projects[project] = repo
        existing[project.casefold()] = project
        added.append({"project": project, "repo": repo})
    if not added:
        return {"added": [], "conflicts": conflicts, "path": str(path)}
    backup = path.with_name(path.name + ".role-timers.bak")
    if backup.is_symlink() or (backup.exists() and not backup.is_file()):
        raise TimerError("backup path is not a regular file: %s" % backup)
    if backup.exists():
        backup.unlink()
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        os.fchmod(stream.fileno(), 0o600)
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    _atomic_config_write(path, text.encode("utf-8"), mode)
    return {"added": added, "conflicts": conflicts, "path": str(path)}


def candidates(row: dict) -> list[str]:
    """The hosts declared as candidates of ``row`` (elected role)."""
    return [str(value) for value in (row.get("candidats") or ())]


def is_candidate(row: dict, host: str) -> bool:
    """May this host carry the role's timers? Never touches configuration."""
    if str(row.get("releve") or "") == "election":
        return host in candidates(row)
    return str(row.get("host") or "") == host


#: the secrets file the role units load after ``env`` (see render_service)
NOTIFY_ENV = "~/.config/ameesh/notify.env"


def unit_env(path: str | None = None) -> dict:
    """The environment of a role unit: this process's, then ``notify.env``.

    ``EnvironmentFile`` semantics, enough for a check: ``KEY=VALUE`` lines,
    comments and blank lines ignored, one pair of surrounding quotes removed.
    A missing file is the optional ``EnvironmentFile=-`` of the unit. Values
    are never printed.
    """
    return dict(os.environ, **_env_file(path or NOTIFY_ENV))


def _env_file(path: str) -> dict:
    """The ``KEY=VALUE`` pairs of an ``EnvironmentFile`` (empty when absent)."""
    env = {}
    try:
        text = Path(os.path.expanduser(path)).read_text(encoding="utf-8")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def paquet_reach(path: str | None = None, *, git=None) -> tuple[str, str]:
    """#3854: ``(blocked, detail)`` for the hourly packet tick on this host.

    The unit runs the script of a dedicated clone (``AMEESH_PAQUET_REPO`` in
    ``paquet-horaire.env``), advanced on ``origin/<target>`` before each tick.
    Without that file, that clone, or the script on the target (#3845 not
    landed, clone behind), the tick would fail every ten minutes: the pass is
    blocked instead. Empty strings when it can run. The path must be absolute:
    systemd reads ``EnvironmentFile`` literally (no ``~``, no working
    directory). ``origin/<target>`` is what the tick runs, so it is read first;
    the work tree only answers when git cannot.
    """
    target_path = Path(os.path.expanduser(path or PAQUET_ENV))
    if not target_path.is_file():
        return ("%s missing" % (path or PAQUET_ENV),
                "declare AMEESH_PAQUET_REPO, the dedicated clone of the project "
                "(see EXPLOITATION, Pose automatique)")
    # the file alone: the unit does not inherit this shell's environment
    env = _env_file(str(target_path))
    repo = (env.get("AMEESH_PAQUET_REPO") or "").strip()
    if not repo:
        return ("AMEESH_PAQUET_REPO missing",
                "declare it in %s (see EXPLOITATION, Pose automatique)"
                % (path or PAQUET_ENV))
    if any(ch.isspace() for ch in repo):
        return ("AMEESH_PAQUET_REPO contains whitespace", repr(repo))
    if not repo.startswith("/"):
        return ("AMEESH_PAQUET_REPO is not an absolute path",
                "%r: systemd reads paquet-horaire.env literally" % repo)
    root = Path(repo)
    if not (root / ".git").exists():
        return ("AMEESH_PAQUET_REPO is not a git clone", repo)
    target = (env.get("AMEESH_PAQUET_TARGET") or "main").strip() or "main"
    run = git or (lambda *args: subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30))
    try:
        found = run("cat-file", "-e", "origin/%s:%s" % (target, PAQUET_SCRIPT))
    except (OSError, subprocess.SubprocessError):
        found = None
    if found is not None and found.returncode == 0:
        return "", repo
    if found is None and (root / PAQUET_SCRIPT).is_file():
        return "", repo
    return ("%s absent from the clone" % PAQUET_SCRIPT,
            "%s on origin/%s: #3845 not landed, or the clone is behind "
            "(git -C %s pull --ff-only)" % (repo, target, repo))


def notify_reach(cfg, *, responsible=None, env: dict | None = None) -> tuple[list[str], str]:
    """#3383: the humans the alerts timer must push to, or the gap.

    The holder pushes on its own routes, whichever candidate it is. The
    humans required are the role's responsible and ``notify.default_human``;
    each needs, through ``channels_for`` (an empty route excludes, #3007), a
    remote push channel (``ntfy``, ``slack``, ``nexlink``) whose secret
    resolves in the unit's environment; a ``nexlink`` channel whose crypto or
    secret is missing is covered by its configured fallback (#3536).
    ``desktop`` (the holder's graphical session) and ``app`` (nothing pushed)
    do not count. Any gap is named; a candidate that would hold the lease and
    push nothing is the silence #3383 reports.
    """
    from . import notify as notify_mod
    try:
        ncfg = notify_mod.parse_config(getattr(cfg, "notify", None))
    except notify_mod.NotifyConfigError as exc:
        return [], "invalid notify configuration: %s" % " ".join(str(exc).split())[:200]
    required = []
    for value in (responsible, ncfg.default_human):
        human = notify_mod.normalize_human(value)
        if human and human not in required:
            required.append(human)
    if not required:
        return [], "no human to reach: neither a responsible of the role nor notify.default_human"
    env = unit_env() if env is None else env
    gaps = []
    for human in required:
        channels = ncfg.channels_for(human)
        if not channels:
            gaps.append("%s has no route on this host" % human)
            continue
        problems = [notify_mod.remote_push_problem(channel, env, ncfg=ncfg)
                    for channel in channels]
        if all(problems):
            gaps.append("%s has no usable remote push (ntfy, slack, nexlink): %s"
                        % (human, "; ".join(problems)))
    if gaps:
        return [], " ".join("; ".join(gaps).split())[:400]
    return required, ""


#: the exact description of the alerts unit, shared by `plan` and the
#: election's readiness check so a unit left from another card is detected
ALERTS_DESCRIPTION = "ameesh — alert delivery under the elected role lease (#3383)"

#: #4114: the role an installed alerts unit runs, read from its `ExecStart`
_ALERTS_SERVICE_ROLE = re.compile(
    r"^ExecStart=.*\brole run (\S+) --pass alerts -- ", re.MULTILINE)


def alerts_service_role(content: str) -> str:
    """The role an installed ``ameesh-role-alerts.service`` runs, ``""`` if none.

    #4114: there is one alerts unit per host, rendered for the role that
    carries the pass there (``mesh-design`` by default). The election
    evaluates every elected role on the host, so the role being evaluated is
    not necessarily the one the unit was rendered for; the unit's own role is
    the only one whose template can be compared with it.
    """
    match = _ALERTS_SERVICE_ROLE.search(str(content or ""))
    return match.group(1) if match else ""


def alerts_timer_gap(*, role: str, directory: Path | None = None,
                     bin_path: str = DEFAULT_BIN, apply: bool = True) -> str:
    """#3575: why the alerts pass will not run on this host, ``""`` when it will.

    `election._notify_reach` publishes `notify_ok` per standing, and the
    delegate is chosen from those standbys: a route without a timer elects a
    host that never runs the pass, while another routed, equipped candidate
    waits. The check accepts no stale unit either: an `alerts` service
    rendered before `--pass alerts` (#3568) would stand by on every delegate.

    #4114: the unit is compared with the template of the role **it names**,
    not of `role` (the role being evaluated). The pass is global and the unit
    is unique per host: comparing with the evaluated role made every role but
    the installed one (auditeur, orchestrateur) read a false "out of date"
    and publish `notify_ok=false` with an `--install` advice that changed
    nothing.

    ``apply=False`` checks the files only (the install path and the tests);
    the published readiness uses the live ``systemctl --user`` state. Any
    local gap is returned as a sentence, never raised: the caller reports it
    in `notify_diagnostic` without making the role ineligible (#3402).
    """
    if not str(role or "").strip():
        return ("the role name is unknown: cannot check its ameesh-role-alerts"
                ".timer")
    directory = systemd_dir() if directory is None else Path(directory)
    spec = TimerSpec("alerts", "alerts", ALERTS_INTERVAL_S, ALERTS_DESCRIPTION)
    try:
        current = (directory / spec.service).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("the alerts pass would not run here: %s is missing "
                "(ameesh role timers --install)" % spec.service)
    except OSError as exc:
        return "the alerts pass cannot be checked: %s" % exc
    carried = alerts_service_role(current)
    if not carried or current != render_service(spec, role=carried,
                                                bin_path=bin_path):
        return ("the alerts pass would not run here: %s is out of date "
                "(ameesh role timers --install)" % spec.service)
    try:
        current_timer = (directory / spec.timer).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("the alerts pass would not run here: %s is missing "
                "(ameesh role timers --install)" % spec.timer)
    except OSError as exc:
        return "the alerts pass cannot be checked: %s" % exc
    if current_timer != render_timer(spec):
        return ("the alerts pass would not run here: %s is out of date "
                "(ameesh role timers --install)" % spec.timer)
    if not apply:
        return ""
    try:
        if _systemctl("is-enabled", "--quiet", spec.timer,
                      timeout=5.0).returncode != 0:
            return ("the alerts pass would not run here: %s is not enabled "
                    "(ameesh role timers --install)" % spec.timer)
        if _systemctl("is-active", "--quiet", spec.timer,
                      timeout=5.0).returncode != 0:
            return ("the alerts pass would not run here: %s is not active "
                    "(systemctl --user start %s)" % (spec.timer, spec.timer))
    except TimerError as exc:
        return "the alerts pass cannot be checked: %s" % exc
    return ""


def plan(*, host: str, role: str, row: dict, settings, watch_intervals,
         watch_error: str = "", expected_projects=None, notify_humans=None,
         notify_error: str = "", paquet=None, fingerprint_intervals=(),
         fingerprint_error: str = "") -> dict:
    """The timers this host should run, without touching the filesystem.

    ``row`` is the role's ``agent_registry`` row: an elected role is a
    candidate only when the host is in ``candidats``; a pinned role only on
    its host. A non-candidate gets no units at all. ``expected_projects`` is
    the canon's ``{project: repo}`` mapping; a project missing from the local
    ``github.projects`` is reported, never silently ignored. ``notify_humans``
    and ``notify_error`` come from :func:`notify_reach`: without a remote push
    to every required human, the ``alerts`` pass is blocked (#3383).
    ``paquet`` is the ``(blocked, detail)`` of :func:`paquet_reach`; ``None``
    leaves the hourly packet tick out of the plan (#3854), as does any role
    but ``mesh-design``, whose lease carries the tick.
    ``fingerprint_intervals`` are the cadences of the canon's ``fingerprints``
    cards (#3885): at least one declaration installs the pass; none installs
    nothing, and an unreadable declaration is reported blocked.
    """
    declared = candidates(row)
    candidate = is_candidate(row, host)
    report = {
        "schema": "ameesh-role-timers/1",
        "host": host,
        "role": role,
        "releve": str(row.get("releve") or ""),
        "candidates": declared,
        "candidate": candidate,
        "timers": [],
        "missing_projects": [],
    }
    if not candidate:
        return report
    local = {str(name).casefold() for name in
             (getattr(settings, "projects", None) or {})}
    report["missing_projects"] = [
        {"project": str(name), "repo": str(repo)}
        for name, repo in (expected_projects or {}).items()
        if str(name).casefold() not in local]

    reported = []
    watch = tuple(float(value) for value in (watch_intervals or ()))
    if watch:
        interval = _clamp_interval(min(watch), default=900)
        reported.append(TimerSpec(
            "github-watch", "github-watch", interval,
            "ameesh — GitHub watch under the elected role lease (EP5)"))
    else:
        reported.append(TimerSpec(
            "github-watch", "github-watch", 90,
            "ameesh — GitHub watch under the elected role lease (EP5)",
            blocked="no watched repository", detail=watch_error or "no github_watch declaration"))

    projects = getattr(settings, "projects", None) or {}
    if projects:
        configured = getattr(settings, "interval", None) or 900
        reported.append(TimerSpec(
            "issues", "issues", _clamp_interval(configured, default=900),
            "ameesh — GitHub issue projection under the elected role lease (EP5)"))
        reported.append(TimerSpec(
            "ci", "ci", _MIN_INTERVAL_S,
            "ameesh — CI watches under the elected role lease (EP5)"))
    else:
        detail = ("github.projects is missing from this host's configuration; "
                  "copy the github key of the observer (see EXPLOITATION, "
                  "Minuteries de rôle)")
        for name, description in (
                ("issues", "ameesh — GitHub issue projection under the elected role lease (EP5)"),
                ("ci", "ameesh — CI watches under the elected role lease (EP5)")):
            reported.append(TimerSpec(
                name, name, _MIN_INTERVAL_S, description,
                blocked="github.projects missing on this host", detail=detail))

    description = ALERTS_DESCRIPTION
    if fingerprint_error:
        reported.append(TimerSpec(
            "fingerprints", "fingerprints", _MIN_INTERVAL_S,
            "ameesh — failure fingerprints under the elected role lease (#3885)",
            blocked=fingerprint_error))
    elif fingerprint_intervals:
        reported.append(TimerSpec(
            "fingerprints", "fingerprints",
            _clamp_interval(min(float(v) for v in fingerprint_intervals), default=60),
            "ameesh — failure fingerprints under the elected role lease (#3885)"))
    if notify_error or not notify_humans:
        reported.append(TimerSpec(
            "alerts", "alerts", ALERTS_INTERVAL_S, description,
            blocked=notify_error or "no human to reach on this host",
            detail=("the lease holder pushes on its own routes: declare the notify "
                    "key (the owner's ntfy route, secrets in notify.env) as on the "
                    "other candidates (see EXPLOITATION, Alertes poussées)"),
            advisory=True))
    else:
        reported.append(TimerSpec(
            "alerts", "alerts", ALERTS_INTERVAL_S, description,
            detail="reaches %s" % ", ".join(notify_humans)))
    # #4172: backlog coherence, under the pilot's elected lease (like the
    # packet tick): it closes lots, which is a pilot-side hygiene gesture, not
    # an auditor's. It needs no GitHub key: without a clone it still finds
    # duplicates and contradictions, and says the git checks "non vérifié".
    if role == DEFAULT_ROLE:
        reported.append(TimerSpec(
            "coherence", "coherence", COHERENCE_INTERVAL_S,
            "ameesh — backlog coherence pass under the elected role lease (#4172)"))
    if paquet is not None and role == DEFAULT_ROLE:
        blocked, detail = paquet
        # #3854: a missing clone is a host setting, not a negative result (§3)
        reported.append(TimerSpec(
            PAQUET_PASS, PAQUET_PASS, PAQUET_INTERVAL_S, PAQUET_DESCRIPTION,
            blocked=blocked, detail=detail, advisory=bool(blocked)))
    report["timers"] = [item.as_json() for item in reported]
    return report


def _check_bin(bin_path: str) -> str:
    value = (bin_path or "").strip()
    if not value:
        raise TimerError("an ameesh binary path is required")
    if any(ch.isspace() for ch in value) or "\n" in value:
        raise TimerError("the ameesh binary path must not contain whitespace: %r" % value)
    return value


def render_service(spec: TimerSpec, *, role: str, bin_path: str = DEFAULT_BIN) -> str:
    """The oneshot service that runs one pass under ``role run``.

    #3568: the `alerts` pass declares itself (`--pass alerts`) so the wrapper
    can hand it to a candidate that proves a remote route when the lease
    holder cannot push; the other passes stay bound to the holder alone.
    """
    if spec.name == PAQUET_PASS:
        return render_paquet_service(role=role, bin_path=bin_path)
    delegated = " --pass alerts" if spec.name == "alerts" else ""
    # #4172: the coherence pass may walk many lots of several projects; the
    # other passes are single ticks and keep the tighter bound.
    timeout = "30min" if spec.name == "coherence" else "10min"
    return "\n".join((
        "# generated by `ameesh role timers` — do not edit; rerun the command",
        "[Unit]",
        "Description=%s" % spec.description,
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "EnvironmentFile=%h/.config/ameesh/env",
        # the same optional secrets as ameesh-notify (GH_TOKEN=… when gh has
        # no session, webhook tokens): a timer is not a login shell
        "EnvironmentFile=-%h/.config/ameesh/notify.env",
        "ExecStart=%s role run %s%s -- %s notify --once --only %s"
        % (_check_bin(bin_path), role, delegated, _check_bin(bin_path), spec.only),
        "TimeoutStartSec=%s" % timeout,
        "NoNewPrivileges=yes",
        "",
    ))


def render_paquet_service(*, role: str, bin_path: str = DEFAULT_BIN) -> str:
    """#3854: the unit of ``deploy/systemd/ameesh-paquet-horaire.service``.

    The tick reads everything in the database and the forge; it runs the
    script of the target (lesson #3767), so the clone is advanced first and a
    clone that does not advance has no tick. The interpreter is the one of
    the venv that carries ``bin_path``; since #4473 its ``ameesh`` library is
    also the clone's (``PYTHONPATH=<clone>/src``), never the installed one: a
    host rolled back to an older version must not make the target's script
    import an older library.
    """
    binary = _check_bin(bin_path)
    python = binary.rsplit("/", 1)[0] + "/python" if "/" in binary else "python3"
    return "\n".join((
        "# generated by `ameesh role timers` — do not edit; rerun the command",
        "# #3845: composition, ci-full and removal of a red unit, without an agent;",
        "# only the holder of the %s lease ticks (`role run`)." % role,
        "[Unit]",
        "Description=%s" % PAQUET_DESCRIPTION,
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "CacheDirectory=ameesh/paquet-auto",
        "CacheDirectoryMode=0700",
        "Environment=TMPDIR=%C/ameesh/paquet-auto",
        "Environment=PATH=%h/.local/bin:/opt/node/bin:/usr/local/bin:/usr/bin:/bin",
        "Environment=AMEESH_PAQUET_TARGET=main",
        "Environment=AMEESH_PAQUET_PILOT=%s" % DEFAULT_ROLE,
        "EnvironmentFile=%h/.config/ameesh/env",
        "EnvironmentFile=-%h/.config/ameesh/release.env",
        "EnvironmentFile=%s" % PAQUET_ENV.replace("~", "%h", 1),
        "ExecStartPre=/usr/bin/git -C ${AMEESH_PAQUET_REPO} pull --ff-only --quiet "
        "origin ${AMEESH_PAQUET_TARGET}",
        "# #4473: the script AND its `ameesh` library come from the clone; the",
        "# installed venv only supplies the interpreter (and the `role run` gate).",
        "ExecStart=%s role run %s -- /usr/bin/env PYTHONPATH=${AMEESH_PAQUET_REPO}/src "
        "%s ${AMEESH_PAQUET_REPO}/%s tick "
        "--repo ${AMEESH_PAQUET_REPO} --target ${AMEESH_PAQUET_TARGET} "
        "--pilot ${AMEESH_PAQUET_PILOT}" % (binary, role, python, PAQUET_SCRIPT),
        # a composition (local tests included) is bounded to three hours by the script
        "TimeoutStartSec=4h",
        "NoNewPrivileges=yes",
        "",
    ))


def render_timer(spec: TimerSpec) -> str:
    """The timer that wakes ``render_service``; bounded for the takeover."""
    if spec.name == PAQUET_PASS:
        # #3845: on the wall clock, so every candidate ticks at the same minutes
        return "\n".join((
            "# generated by `ameesh role timers` — do not edit; rerun the command",
            "[Unit]",
            "Description=ameesh paquet horaire, tic (#3845)",
            "",
            "[Timer]",
            "OnCalendar=*:0/10",
            "AccuracySec=30s",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ))
    delay = min(20, max(1, spec.interval_s // 10))
    return "\n".join((
        "# generated by `ameesh role timers` — do not edit; rerun the command",
        "[Unit]",
        "Description=ameesh — %s, every %d s (EP5)" % (spec.name, spec.interval_s),
        "",
        "[Timer]",
        "OnBootSec=2min",
        "OnUnitActiveSec=%ds" % spec.interval_s,
        "RandomizedDelaySec=%ds" % delay,
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ))


def dropin_skips(report: dict) -> tuple[str, ...]:
    """The passes the full service leaves to this host's role timers.

    The three observation passes always (#3327: a blocked one reads nothing
    in the service either, the check names it); ``alerts`` only when its timer
    is installed here — otherwise the service keeps delivering (#3383).
    """
    installed = {item["name"] for item in report.get("timers") or ()
                 if not item.get("blocked")}
    return _OBSERVATION_PASSES + (("alerts",) if "alerts" in installed else ())


def render_notify_dropin(bin_path: str = DEFAULT_BIN,
                         skip: tuple = _OBSERVATION_PASSES) -> str:
    """Exclude the role-timer passes from the full notifier.

    The role timers carry ``issues``, ``github-watch``, ``ci`` and, where a
    route exists, ``alerts``; the full service keeps every other pass,
    including the ``take`` work that nothing else runs (auto-take, budget
    handoff, WIP sweep). An exclusion is safer than an allow-list: a new pass
    is kept by default.
    """
    return "\n".join((
        "# generated by `ameesh role timers` — %s carried by the role timers;"
        % ", ".join(skip),
        "# the full service keeps every other pass (take, budget handoff, WIP sweep)",
        "[Service]",
        "ExecStart=",
        "ExecStart=%s notify --interval 30 --skip %s"
        % (_check_bin(bin_path), ",".join(skip)),
        "",
    ))


def systemd_dir(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get(SYSTEMD_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~")) / ".config" / "systemd" / "user"


def _write(path: Path, content: str) -> bool:
    """Atomically write ``content``; return True when the bytes changed."""
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name("." + path.name + ".tmp")
    candidate.write_text(content, encoding="utf-8")
    os.chmod(candidate, 0o644)
    os.replace(candidate, path)
    return True


def _systemctl(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise TimerError("systemctl is not available; use --no-apply to write "
                         "the units only") from None


def _apply(action: str, timers: list[str]) -> None:
    result = _systemctl("daemon-reload")
    if result.returncode != 0:
        raise TimerError("systemctl --user daemon-reload failed: %s"
                         % (result.stderr or result.stdout).strip()[:300])
    if action == "install":
        for timer in timers:
            result = _systemctl("enable", "--now", timer)
            if result.returncode != 0:
                raise TimerError("systemctl --user enable --now %s failed: %s"
                                 % (timer, (result.stderr or result.stdout).strip()[:300]))


def _notify_unit(path: Path) -> bool:
    return (path / "ameesh-notify.service").is_file()


def install(report: dict, *, directory: Path, bin_path: str = DEFAULT_BIN,
            apply: bool = True, out=None) -> dict:
    """Write the units of a candidate host and enable the timers.

    A blocked pass is reported and left without units; existing units of a
    blocked pass are removed so a timer cannot stay green while reading
    nothing. Returns the same report with ``installed``/``blocked`` lists.
    """
    out = out or (lambda _line: None)
    if not report.get("candidate"):
        return dict(report, installed=[], removed=[], applied=False)
    directory.mkdir(parents=True, exist_ok=True)
    installed, removed = [], []
    timers = []
    for item in report.get("timers") or []:
        spec = TimerSpec(item["name"], item["only"], int(item["interval_s"]),
                         item["description"], item.get("blocked") or "",
                         item.get("detail") or "")
        service = directory / spec.service
        timer = directory / spec.timer
        if spec.blocked:
            if apply:
                _systemctl("disable", "--now", spec.timer)
            for path in (service, timer):
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            continue
        if _write(service, render_service(spec, role=report["role"], bin_path=bin_path)):
            installed.append(spec.service)
            out("wrote %s" % spec.service)
        if _write(timer, render_timer(spec)):
            installed.append(spec.timer)
            out("wrote %s" % spec.timer)
        timers.append(spec.timer)

    dropin = directory / "ameesh-notify.service.d" / "10-role-timers.conf"
    if timers and _notify_unit(directory):
        if _write(dropin, render_notify_dropin(bin_path, dropin_skips(report))):
            installed.append(str(dropin.relative_to(directory)))
    elif dropin.exists():
        # no role timer runs here (or the full service disappeared): limiting
        # the notifier to alerts would silence GitHub and CI altogether
        dropin.unlink()
        removed.append(str(dropin.relative_to(directory)))

    applied = False
    if apply and timers:
        _apply("install", timers)
        applied = True
        if _notify_unit(directory):
            active = _systemctl("is-active", "--quiet", "ameesh-notify.service")
            if active.returncode == 0:
                _systemctl("try-restart", "ameesh-notify.service")
    return dict(report, installed=installed, removed=removed, applied=applied)


def check(report: dict, *, directory: Path, apply: bool = True,
          bin_path: str = DEFAULT_BIN) -> list[str]:
    """Problems that would leave the observation silent on this host.

    Also detects a unit left over from another configuration (cadence, role or
    binary): a green timer that runs an old command is the same silence as a
    missing one, so the operator has to rerun ``--install``.
    """
    if not report.get("candidate"):
        return []
    problems = []
    for item in report.get("missing_projects") or []:
        problems.append("github.projects lacks %s (%s); rerun `ameesh role timers "
                        "--install` to add it" % (item["project"], item["repo"]))
    for item in report.get("timers") or []:
        spec = TimerSpec(item["name"], item["only"], int(item["interval_s"]),
                         item["description"], item.get("blocked") or "",
                         item.get("detail") or "")
        if spec.blocked:
            if not item.get("advisory"):
                problems.append("%s: %s — %s" % (spec.name, item["blocked"], item["detail"]))
            continue
        expected = {spec.service: render_service(spec, role=report["role"],
                                                 bin_path=bin_path),
                    spec.timer: render_timer(spec)}
        for name, content in expected.items():
            path = directory / name
            if not path.is_file():
                problems.append("%s: missing %s" % (spec.name, name))
            elif path.read_text(encoding="utf-8") != content:
                problems.append("%s: %s is out of date; rerun `ameesh role timers --install`"
                                % (spec.name, name))
        if apply and (directory / spec.timer).is_file():
            enabled = _systemctl("is-enabled", "--quiet", spec.timer)
            if enabled.returncode != 0:
                problems.append("%s: %s is not enabled" % (spec.name, spec.timer))
            active = _systemctl("is-active", "--quiet", spec.timer)
            if active.returncode != 0:
                problems.append("%s: %s is not active" % (spec.name, spec.timer))
    dropin = directory / "ameesh-notify.service.d" / "10-role-timers.conf"
    has_timer = any(not item.get("blocked") for item in report.get("timers") or ())
    if has_timer and _notify_unit(directory):
        if not dropin.is_file():
            problems.append("ameesh-notify.service: missing the --skip drop-in; "
                            "GitHub would be read twice")
        elif dropin.read_text(encoding="utf-8") != render_notify_dropin(
                bin_path, dropin_skips(report)):
            problems.append("ameesh-notify.service: the --skip drop-in is out of "
                            "date; rerun `ameesh role timers --install`")
    return problems


def paquet_pause_note(db, report: dict, path: str | None = None, *,
                      now: float | None = None) -> str:
    """#4361: the active pause of the hourly packet tick, as a warning.

    The pause (``paquet-horaire.py pause``) keeps the timer active, so
    ``--check`` stays green; it is named here so a staging return check does
    not read a silent tick as healthy. Empty when there is no pause, no
    packet pass, or the log cannot be read (never a failure).
    """
    if not any(item.get("name") == PAQUET_PASS and not item.get("blocked")
               for item in report.get("timers") or ()):
        return ""
    from . import packet_pause
    env = _env_file(os.path.expanduser(path or PAQUET_ENV))
    target = (env.get("AMEESH_PAQUET_TARGET") or "main").strip() or "main"
    try:
        data = packet_pause.active(db, project="ameesh", target=target, now=now)
    except Exception:  # noqa: BLE001 - a log read never fails the check
        return ""
    if not data:
        return ""
    return "%s: %s (timer active, ticks skip)" % (PAQUET_PASS, packet_pause.describe(data))


def warnings(report: dict) -> list[str]:
    """Passes blocked for a missing secret: reported, never a failure (#3402).

    Without the owner's notify route, a candidate cannot push the alerts.
    Under canon §3, that missing credential is not a negative result. It
    must not fail ``--check``, nor therefore ``vm.sh agents controler``,
    which would roll a staging deploy back. The gap stays visible here, and
    through ``notify_undeliverable`` while a decision is pending.
    """
    if not report.get("candidate"):
        return []
    return ["%s: %s — %s" % (item["name"], item["blocked"], item["detail"])
            for item in report.get("timers") or []
            if item.get("blocked") and item.get("advisory")]


def remove(report: dict, *, directory: Path, apply: bool = True) -> dict:
    """Remove the role timers and their notifier drop-in."""
    if not report.get("candidate"):
        return dict(report, removed=[], applied=False)
    removed = []
    names = []
    for item in report.get("timers") or []:
        spec = TimerSpec(item["name"], item["only"], int(item["interval_s"]),
                         item["description"])
        names.append(spec.timer)
        if apply:
            _systemctl("disable", "--now", spec.timer)
        for path in (directory / spec.service, directory / spec.timer):
            if path.exists():
                path.unlink()
                removed.append(path.name)
    dropin_dir = directory / "ameesh-notify.service.d"
    dropin = dropin_dir / "10-role-timers.conf"
    if dropin.exists():
        dropin.unlink()
        removed.append(str(dropin.relative_to(directory)))
        try:
            dropin_dir.rmdir()
        except OSError:
            pass
    applied = False
    if apply and names:
        _systemctl("daemon-reload")
        applied = True
        if _notify_unit(directory):
            active = _systemctl("is-active", "--quiet", "ameesh-notify.service")
            if active.returncode == 0:
                _systemctl("try-restart", "ameesh-notify.service")
    return dict(report, removed=removed, applied=applied)


def _print_report(report: dict, out=sys.stdout) -> None:
    if not report.get("candidate"):
        print("%s: not a candidate of role %s on this host (%s)"
              % (report["host"], report["role"],
                 "elected: " + ", ".join(report.get("candidates") or ())
                 if report.get("releve") == "election" else "pinned role"),
              file=out)
        return
    for item in report.get("timers") or []:
        state = ("BLOCKED: %s — %s" % (item["blocked"], item["detail"])
                 if item.get("blocked") else "every %d s" % item["interval_s"])
        print("%-14s %s" % (item["name"], state), file=out)
    for item in report.get("missing_projects") or []:
        print("missing project: %s (%s)" % (item["project"], item["repo"]), file=out)
    if report.get("installed"):
        print("installed: %s" % ", ".join(report["installed"]), file=out)
    if report.get("removed"):
        print("removed: %s" % ", ".join(report["removed"]), file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh role timers")
    parser.add_argument("--role", default=DEFAULT_ROLE,
                        help="elected role carrying the timers (default: %s)" % DEFAULT_ROLE)
    parser.add_argument("--bin", default=DEFAULT_BIN,
                        help="ameesh binary in the units (default: %(default)s)")
    parser.add_argument("--systemd-dir", default=None,
                        help="user systemd directory (default: ~/.config/systemd/user)")
    parser.add_argument("--install", action="store_true", help="write and enable the timers")
    parser.add_argument("--check", action="store_true", help="verify the timers; exit 1 on a gap")
    parser.add_argument("--remove", action="store_true", help="stop and remove the timers")
    parser.add_argument("--no-apply", action="store_true",
                        help="write/read the files only, no systemctl")
    parser.add_argument("--no-sync", action="store_true",
                        help="do not add canon-watched projects missing from the "
                             "host's github.projects")
    parser.add_argument("--json", action="store_true", help="one JSON object")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.install, args.check, args.remove)) > 1:
        parser.error("choose at most one of --install, --check, --remove")
    cfg = config_mod.load()
    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError as exc:
        print("role timers: %s" % exc, file=sys.stderr)
        return 1
    try:
        try:
            row = registry.get(db, args.role)
        except Exception as exc:  # noqa: BLE001 - database error is a real failure
            print("role timers: cannot read role %s: %s" % (args.role, exc), file=sys.stderr)
            return 1
        if row is None:
            print("role timers: unknown role %s (no agent_registry row)" % args.role,
                  file=sys.stderr)
            return 2
        host = str(getattr(cfg, "host", "") or "")
        if not host:
            print("role timers: this host has no name (AMEESH_HOST or config host)",
                  file=sys.stderr)
            return 2
        # #3327 B/P1: a non-candidate must not read the canon, rewrite the
        # host configuration (sync) or touch a backup. Check candidacy first.
        if not is_candidate(row, host):
            report = plan(host=host, role=args.role, row=row, settings=None,
                          watch_intervals=())
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                _print_report(report)
            return 0
        try:
            settings = plan_github.settings_of(cfg)
        except plan_github.SettingsError as exc:
            print("role timers: invalid github configuration: %s" % exc, file=sys.stderr)
            return 2
        declarations, watch_error = _watch_declarations(
            cfg, lambda text: print("role timers: %s" % text, file=sys.stderr))
        intervals = sorted({float(item.interval) for item in declarations})
        expected = {}
        for item in declarations:
            expected.setdefault(str(item.project), str(item.repo))
        sync = None
        if args.install and not args.no_sync and declarations:
            sync = sync_github_projects(cfg, declarations)
            if sync["added"]:
                cfg = config_mod.load()
                try:
                    settings = plan_github.settings_of(cfg)
                except plan_github.SettingsError as exc:
                    raise TimerError("configuration invalid after adding projects: %s"
                                     % exc) from None
        humans, notify_error = notify_reach(cfg, responsible=row.get("responsible"))
        fingerprint_sources, fingerprint_error = fingerprints.declarations_of_config(
            cfg, lambda text: print("role timers: %s" % text, file=sys.stderr))
        fingerprint_intervals = sorted(
            {float(item.interval) for item in fingerprint_sources})
        report = plan(host=host, role=args.role,
                      row=row, settings=settings, watch_intervals=intervals,
                      watch_error=watch_error, expected_projects=expected,
                      notify_humans=humans, notify_error=notify_error,
                      paquet=paquet_reach() if args.role == DEFAULT_ROLE else None,
                      fingerprint_intervals=fingerprint_intervals,
                      fingerprint_error=fingerprint_error)
        if sync is not None:
            report["github_sync"] = sync
        directory = systemd_dir(args.systemd_dir)
        if args.check:
            problems = check(report, directory=directory, apply=not args.no_apply,
                             bin_path=args.bin)
            notes = warnings(report)
            paused = paquet_pause_note(db, report) if args.role == DEFAULT_ROLE else ""
            if paused:
                notes.append(paused)
            if args.json:
                print(json.dumps(dict(report, problems=problems, warnings=notes),
                                 ensure_ascii=False, sort_keys=True))
            elif problems:
                for problem in problems + ["warning: %s" % note for note in notes]:
                    print("role timers: %s" % problem, file=sys.stderr)
            else:
                for note in notes:
                    print("role timers: warning: %s" % note, file=sys.stderr)
                print("%s: role timers OK (%d pass(es))"
                      % (report["host"], len(report.get("timers") or ())))
            return 1 if problems else 0
        if args.remove:
            result = remove(report, directory=directory, apply=not args.no_apply)
        elif args.install:
            result = install(report, directory=directory, bin_path=args.bin,
                             apply=not args.no_apply,
                             out=(lambda _line: None) if args.json else print)
        else:
            result = report
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_report(result)
        if args.install:
            status = 0
            for item in result.get("timers") or []:
                if item.get("blocked"):
                    print("role timers: %s%s not installed: %s — %s"
                          % ("warning: " if item.get("advisory") else "",
                             item["name"], item["blocked"], item["detail"]),
                          file=sys.stderr)
                    if not item.get("advisory"):
                        status = 1
            for item in result.get("missing_projects") or []:
                print("role timers: github.projects lacks %s (%s); add it or rerun "
                      "without --no-sync" % (item["project"], item["repo"]),
                      file=sys.stderr)
                status = 1
            for item in (result.get("github_sync") or {}).get("conflicts") or []:
                print("role timers: github.projects.%s maps to %s, the canon says %s; "
                      "kept the local value" % (item["project"], item["kept"], item["repo"]),
                      file=sys.stderr)
                status = 1
            return status
        return 0
    except TimerError as exc:
        print("role timers: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("role timers: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
