# SPDX-License-Identifier: AGPL-3.0-only
"""Assemble the #1467 train service from the host configuration (#2446).

The engine, the packet reservation and the merge authority already exist on
``main``; no production caller built a :class:`train_runner.Passage`, so
``Runner.train_passages`` stayed empty. This module is that caller, and only
that caller:

* the exact project, target, repository and audited canon commit come from the
  host configuration's ``train`` key; nothing is guessed from the registry, a
  lot note or the current checkout;
* ``processus/mecanique.yaml`` and ``federation.yaml`` are both read at that
  exact commit (``mechanics.load`` and ``train_policy.Versioned``), so the
  packet calendar, the review counts and the automatic merge policy are the
  audited ones;
* the CI contract, the provenance provider and the packet reservation provider
  are named in the configuration and resolved to one exact module; an unknown
  name, an absent piece or a malformed value refuses by name, never by a
  fallback;
* the absolute reservation deadline is compiled from the configuration
  (``deadline_minutes``), never from a hard-coded duration;
* each passage compares the pinned commit with the canon commit the gate reads
  on this host (``train_canon``, #2914): a drift is recorded in the shadow
  plan and alerted to the pilot, and refuses a new active composition.

Two modes are assembled:

``shadow``
    A read-only passage: it computes the packet the train *would* compose at
    each compiled cutoff and publishes it in the host's state directory, then
    compares it with the manual composer's manifest once that packet lands.
    It never reserves, pushes, dispatches CI or merges.
``active``
    The real :class:`train_runner.Passage`: compose, publish ``integration/**``,
    wait for the single packet CI and promote on green. The configuration
    stays explicit; this module never flips the mode by itself.

One host only executes the service: the assembly hands a
:class:`train_runner.HostLease` to the runner, which the service acquires and
renews before any passage. The lease is independent of the per-target merge
reservation, so two agents or two hosts cannot both drive the same train.
"""
from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from functools import partial
from datetime import datetime

from . import (config as config_mod, mechanics, merge_queue, packet_reservation,
               plan_git, radar, resolutions, review_provenance, train,
               train_canon, train_contracts, train_initial, train_policy,
               train_reservation, train_runner, train_shadow)
from .config import NAME_RE

#: schema of the host configuration's ``train`` block
SCHEMA = "ameesh-train-host/1"
#: installed execution modes; ``active`` is a deliberate switch, never a default
MODES = ("shadow", "active")
#: named providers. A name is an explicit selection, not a hint: an unknown
#: name refuses and no adapter is inferred from the project or the registry.
CONTRACTS = {"ameesh": train_contracts.ameesh,
             "nexlink": train_contracts.nexlink}
PROVENANCE = {"review_provenance": review_provenance}
RESERVATIONS = {"packet_reservation": packet_reservation}
#: compiled adapters named by the configuration for the candidate inventory
INITIAL_HISTORY = {"activation-boundary": train_initial.Boundary}

_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_GITHUB = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

_TOP_KEYS = {"schema", "host", "mode", "projects", "role"}
_PROJECT_KEYS = {"repo", "target", "github", "canon", "canon_commit",
                 "ci_contract", "provenance", "reservation", "deadline_minutes",
                 "candidate", "initial_since"}


class ConfigurationError(train.TrainError):
    """The host configuration cannot assemble a passage; nothing is replaced."""


@dataclass(frozen=True)
class ProjectConfig:
    """One project's explicit assembly inputs, validated but not yet opened."""

    name: str
    repo: str
    target: str
    github: str
    canon: str
    canon_commit: str
    ci_contract: str
    provenance: str
    reservation: str
    deadline_minutes: float
    candidate: bool
    initial_since: float | None

    def deadline(self, plan, now):
        """The configured absolute packet-reservation deadline."""
        return float(now) + self.deadline_minutes * 60.0


@dataclass(frozen=True)
class Settings:
    """The validated ``train`` block of the host configuration."""

    host: str
    mode: str
    projects: tuple[ProjectConfig, ...]
    #: L165 (EP5): optional elected role (agent name) that gates every passage.
    #: When set, `train.host` no longer pins the executing host: every host
    #: assembles the service and only the role holder passes.
    role: str | None = None

    @classmethod
    def parse(cls, raw) -> "Settings":
        if not isinstance(raw, dict):
            raise ConfigurationError("`train`: a JSON object is required")
        unknown = sorted(set(raw) - _TOP_KEYS)
        if unknown:
            raise ConfigurationError("`train`: unknown key(s): %s" % ", ".join(unknown))
        missing = sorted({"schema", "host", "mode", "projects"} - set(raw))
        if missing:
            raise ConfigurationError("`train`: missing key(s): %s" % ", ".join(missing))
        if raw["schema"] != SCHEMA:
            raise ConfigurationError("`train.schema`: %r expected" % SCHEMA)
        host = _text(raw["host"], "train.host")
        mode = _text(raw["mode"], "train.mode")
        if mode not in MODES:
            raise ConfigurationError("`train.mode`: %s expected (got %r)"
                                     % (" | ".join(MODES), mode))
        role = raw.get("role")
        if role is not None:
            role = _text(role, "train.role")
            if not NAME_RE.fullmatch(role):
                raise ConfigurationError("`train.role`: an agent name is required")
        projects = raw["projects"]
        if not isinstance(projects, dict) or not projects:
            raise ConfigurationError("`train.projects`: a non-empty object is required")
        parsed = []
        for name, declaration in projects.items():
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise ConfigurationError("`train.projects`: project names are non-empty "
                                         "untrimmed strings")
            parsed.append(_project(name, declaration))
        return cls(host=host, mode=mode, projects=tuple(parsed), role=role)


def _text(value, where):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigurationError("`%s`: a non-empty string is required" % where)
    return value


def _project(name, raw):
    where = "train.projects.%s" % name
    if not isinstance(raw, dict):
        raise ConfigurationError("`%s`: a JSON object is required" % where)
    unknown = sorted(set(raw) - _PROJECT_KEYS)
    if unknown:
        raise ConfigurationError("`%s`: unknown key(s): %s" % (where, ", ".join(unknown)))
    missing = sorted(_PROJECT_KEYS - set(raw))
    if missing:
        raise ConfigurationError("`%s`: missing key(s): %s" % (where, ", ".join(missing)))
    repo = _text(raw["repo"], where + ".repo")
    target = _text(raw["target"], where + ".target")
    github = _text(raw["github"], where + ".github")
    canon = _text(raw["canon"], where + ".canon")
    commit = raw["canon_commit"]
    if not isinstance(commit, str) or not _FULL_SHA.fullmatch(commit):
        raise ConfigurationError("`%s.canon_commit`: a full audited commit id is required"
                                 % where)
    if not _GITHUB.fullmatch(github):
        raise ConfigurationError("`%s.github`: owner/repository expected" % where)
    if not _TARGET.fullmatch(target):
        raise ConfigurationError("`%s.target`: a branch name expected" % where)
    branch = merge_queue._remote_branch(target)
    if not branch or branch == "release" or branch.startswith("release/"):
        raise ConfigurationError("`%s.target`: a release target cannot enter an automatic "
                                 "train" % where)
    if branch.startswith("integration/"):
        raise ConfigurationError("`%s.target`: a train cannot target an integration branch"
                                 % where)
    for key, table in (("ci_contract", CONTRACTS), ("provenance", PROVENANCE),
                       ("reservation", RESERVATIONS)):
        value = _text(raw[key], "%s.%s" % (where, key))
        if value not in table:
            raise ConfigurationError("`%s.%s`: unknown provider %r (known: %s)"
                                     % (where, key, value,
                                        ", ".join(sorted(table))))
    minutes = raw["deadline_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) \
            or not math.isfinite(minutes) or minutes <= 0:
        raise ConfigurationError("`%s.deadline_minutes`: a positive finite number is "
                                 "required" % where)
    candidate = raw["candidate"]
    if type(candidate) is not bool:
        raise ConfigurationError("`%s.candidate`: true or false is required" % where)
    initial_since = raw["initial_since"]
    if candidate:
        initial_since = _timestamp(initial_since, where + ".initial_since")
    elif initial_since is not None:
        raise ConfigurationError("`%s.initial_since`: null expected without candidate "
                                 "enrollment" % where)
    return ProjectConfig(name=name, repo=_expand(repo), target=target, github=github,
                         canon=_expand(canon), canon_commit=commit,
                         ci_contract=raw["ci_contract"], provenance=raw["provenance"],
                         reservation=raw["reservation"], deadline_minutes=float(minutes),
                         candidate=candidate, initial_since=initial_since)


def _timestamp(value, where):
    text = _text(value, where)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError("`%s`: an ISO-8601 instant with a timezone is required"
                                 % where) from exc
    if stamp.tzinfo is None:
        raise ConfigurationError("`%s`: an ISO-8601 instant with a timezone is required"
                                 % where)
    return stamp.timestamp()


def _expand(path):
    return os.path.abspath(os.path.expanduser(path))


@dataclass(frozen=True)
class Assembly:
    """What the runner receives: passages, one service lease, and why."""

    settings: Settings | None
    passages: tuple
    lease: train_runner.HostLease | None
    reason: str
    #: L165 (EP5): the elected role whose lease gates every passage, or None.
    role: str | None = None
    #: #3366: re-read the canonical pin from the host configuration. The
    #: runner's train service calls it before each passage; None on a bench.
    refresh: object | None = None

    @property
    def active(self):
        return bool(self.passages)


def assemble(cfg, *, notify=None) -> Assembly:
    """Validate the configuration and build the passages for this host.

    A missing ``train`` key means no train service, as before. Everything else
    is explicit: a present block that cannot be assembled raises
    :class:`ConfigurationError` naming the missing or unsupported piece. The
    caller (the runner entry point) logs it and keeps serving agents; it never
    enables a fallback passage.
    """
    raw = getattr(cfg, "train", None) or {}
    if not raw:
        return Assembly(None, (), None, "no `train` configuration")
    settings = Settings.parse(raw)
    if settings.role is None and (cfg.host or "").strip() != settings.host:
        return Assembly(settings, (), None,
                        "`train.host` designates %s, this host is %s"
                        % (settings.host, cfg.host))
    report = notify or (lambda message: print("train: " + message, file=sys.stderr))
    passages = tuple(_passage(cfg, project, settings.mode, report)
                     for project in settings.projects)
    lease = train_runner.HostLease(cfg)
    gate = ("role %s" % settings.role) if settings.role else "host pin"

    def refresh():
        # #3366: read the same configuration file the service was assembled
        # from, not whatever the ambient environment names later. The assembled
        # settings stay the reference: anything but the pin changing is a
        # restart, never a silent half-reload of a running service.
        return reload(passages, expected=settings, host=cfg.host,
                      config_path=getattr(cfg, "config_path", "") or "")
    return Assembly(settings, passages, lease, "%s mode, %d passage(s), %s"
                    % (settings.mode, len(passages), gate), role=settings.role,
                    refresh=refresh)


#: #3366: a canon diff touching one of these files changes the train's rules:
#: the pin cannot be requalified mechanically, a pilot must review the change.
REQUALIFY_GUARD = ("processus/mecanique.yaml", "federation.yaml")


def project_of(cfg, name):
    """The validated train project named ``name`` (refusal by name otherwise).

    The host check is the assembly's: without an elected role, a command run
    on another host would edit a configuration whose train service does not
    execute here.
    """
    raw = getattr(cfg, "train", None) or {}
    if not raw:
        raise ConfigurationError("no `train` configuration")
    settings = Settings.parse(raw)
    if settings.role is None and (getattr(cfg, "host", "") or "").strip() != settings.host:
        raise ConfigurationError("`train.host` designates %s, this host is %s"
                                 % (settings.host, getattr(cfg, "host", "") or "?"))
    for spec in settings.projects:
        if spec.name == name:
            return spec
    raise ConfigurationError("`train.projects.%s`: absent from the configuration" % name)


#: #3366: assembled inputs a running service never reloads. Only
#: ``canon_commit`` moves at each passage; changing anything else is a
#: restart, never a silent half-reload of a service that keeps composing.
RESTART_FIELDS = ("repo", "target", "github", "canon", "ci_contract",
                  "provenance", "reservation", "deadline_minutes", "candidate",
                  "initial_since")


def _restart(where, field, before, after):
    raise ConfigurationError(
        "`%s.%s` changed since the service started (%r -> %r): restart the "
        "runner; only `canon_commit` is re-read at each passage"
        % (where, field, before, after))


def _same_assembly(expected, fresh):
    """Refuse any assembly input changed since startup (#3366).

    The pin is the only hot-reloaded value: a service that silently kept its
    old mode, target or provider after a pilot edited the configuration would
    compose a different train than the one declared. Each difference names
    itself and requires a deliberate restart.
    """
    if fresh.mode != expected.mode:
        _restart("train", "mode", expected.mode, fresh.mode)
    if fresh.role != expected.role:
        _restart("train", "role", expected.role, fresh.role)
    if fresh.host != expected.host:
        _restart("train", "host", expected.host, fresh.host)
    before = {spec.name: spec for spec in expected.projects}
    after = {spec.name: spec for spec in fresh.projects}
    if set(before) != set(after):
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        raise ConfigurationError(
            "`train.projects` changed since the service started (added: %s; "
            "removed: %s): restart the runner"
            % (", ".join(added) or "none", ", ".join(removed) or "none"))
    for name in sorted(before):
        for field in RESTART_FIELDS:
            old, new = getattr(before[name], field), getattr(after[name], field)
            if old != new:
                _restart("train.projects.%s" % name, field, old, new)


def reload(passages, *, expected=None, load=None, host=None, config_path=None):
    """#3366: re-read the effective ``train.projects.<p>.canon_commit``.

    The service used to keep the pin assembled at startup, so a requalified pin
    only took effect after a runner restart. Here the **effective** host
    configuration is loaded again (profile inheritance included), its train
    block is validated with the same rules as :func:`assemble`, and every
    passage follows its project's exact commit. A pin that is no longer a
    full commit resolvable with the two regular canon files refuses by name:
    the old pin is never silently kept, and the caller retries.

    Only ``canon_commit`` is refreshed. With ``expected`` (the settings the
    service was assembled from), every other assembly input — repo, canon
    path, target, mode, role, providers, deadline, project set — that changed
    refuses by name and waits for a restart. The whole reload is validated
    before any passage is mutated, so a refused reload leaves every passage
    on its previous pin.
    """
    if load is None:
        env = (dict(os.environ, AMEESH_CONFIG=config_path)
               if config_path else None)
        fresh = config_mod.load(env)
    else:
        fresh = load()
    raw = getattr(fresh, "train", None) or {}
    if not raw:
        raise ConfigurationError("no `train` configuration")
    settings = Settings.parse(raw)
    this_host = str(host if host is not None else getattr(fresh, "host", "") or "").strip()
    if settings.role is None and this_host != settings.host:
        raise ConfigurationError("`train.host` designates %s, this host is %s"
                                 % (settings.host, this_host or "?"))
    if expected is not None:
        _same_assembly(expected, settings)
    by_name = {spec.name: spec for spec in settings.projects}
    pending = []
    for passage in passages or ():
        project = getattr(passage, "project", None)
        name = str(getattr(project, "name", "") or "").strip()
        spec = by_name.get(name)
        if spec is None:
            raise ConfigurationError("`train.projects.%s`: project removed from the "
                                     "configuration" % (name or "?"))
        where = "train.projects.%s" % name
        commit = _canon_commit(spec, where)
        previous = str(getattr(passage, "policy_commit", "") or "")
        if previous == commit and str(getattr(passage, "policy_path", "") or "") == spec.canon:
            continue
        pending.append((passage, name, spec.canon, commit, previous))
    changes = []
    for passage, name, canon_path, commit, previous in pending:
        passage.policy_path = canon_path
        passage.policy_commit = commit
        canon = getattr(passage, "canon", None)
        if canon is not None:
            canon.pin = commit
        changes.append({"project": name, "previous": previous, "pin": commit,
                        "canon": canon_path})
    return changes


def canon_diff(canon, pin, gate):
    """The paths changed by the canon between ``pin`` and ``gate``.

    Both commits must resolve in ``canon``. The command uses the #3366 guard
    on the result: a diff touching ``REQUALIFY_GUARD`` is never applied.
    """
    pin = str(pin or "").strip().lower()
    gate = str(gate or "").strip().lower()
    if not _FULL_SHA.fullmatch(pin) or not _FULL_SHA.fullmatch(gate):
        raise ConfigurationError("canon requalification needs two full commit ids")
    if not os.path.isdir(canon):
        raise ConfigurationError("canon directory not found: %s" % canon)
    proc = resolutions._run(canon, "diff", "--name-only", pin, gate)
    if proc.returncode:
        raise ConfigurationError(
            "cannot read the canon diff %s..%s in %s: %s"
            % (pin[:12], gate[:12], canon,
               " ".join((proc.stderr or proc.stdout or "").split())[:240]))
    return [line.strip() for line in resolutions._text(proc).splitlines() if line.strip()]


def _passage(cfg, spec: ProjectConfig, mode, report):
    """Open the exact repository and canon, then build one passage."""
    where = "train.projects.%s" % spec.name
    if not os.path.isdir(spec.repo):
        raise ConfigurationError("`%s.repo`: directory not found: %s" % (where, spec.repo))
    try:
        repo = resolutions.repo_root(spec.repo)
    except resolutions.ResolveError as exc:
        raise ConfigurationError("`%s.repo`: %s" % (where, exc)) from exc
    commit = _canon_commit(spec, where)
    try:
        rules = mechanics.load(spec.canon, commit, spec.name)
    except mechanics.MechanicsError as exc:
        raise ConfigurationError("`%s`: compiled canon refused: %s" % (where, exc)) from exc
    if (rules.commit, rules.project) != (commit, spec.name):
        raise ConfigurationError("`%s`: compiled canon belongs to another commit or "
                                 "project" % where)
    if not rules.packet_cutoffs:
        raise ConfigurationError("`%s`: compiled canon declares no packet cadence "
                                 "(packets.cutoffs)" % where)
    if rules.ci_packet != "full":
        raise ConfigurationError("`%s`: the train requires full packet CI in the compiled "
                                 "canon" % where)
    try:
        # The same exact commit, read independently: the count/class rules are
        # not taken from the compiled object without this second reader.
        train_policy.Versioned(spec.canon, commit, spec.name)
    except train.TrainError as exc:
        raise ConfigurationError("`%s`: versioned review policy refused: %s"
                                 % (where, exc)) from exc
    project = radar.Project(name=spec.name, repo=repo, target=spec.target,
                            github=spec.github)
    try:
        workflows = CONTRACTS[spec.ci_contract]()
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("`%s.ci_contract`: invalid audited contract: %s"
                                 % (where, exc)) from exc
    provider = PROVENANCE[spec.provenance]
    reservation = train_reservation.Reservation(
        RESERVATIONS[spec.reservation], deadline=spec.deadline)
    candidate = (partial(plan_git.freeze_candidate, repo=repo, target=spec.target)
                 if spec.candidate else None)
    initial = (INITIAL_HISTORY["activation-boundary"](spec.initial_since)
               if spec.candidate else None)
    # #2914: at each passage, the pin is compared with the canon commit the
    # gate reads on this host; never followed automatically (#1467).
    # #3366: the rules closure follows the passage's live pin, not the commit
    # frozen at assembly: a requalified pin must also requalify the rules.
    canon = train_canon.Watch(spec.name, commit,
                              partial(train_canon.gate_commit, cfg=cfg,
                                      project=spec.name),
                              pilot=train_canon.pilot_of(cfg, spec.name))
    if mode == "shadow":
        passage = train_shadow.Passage(
            cfg, project, policy_path=spec.canon, policy_commit=commit,
            workflows=workflows, provider=provider, mechanics=mechanics,
            repository=repo, candidate=candidate, initial=initial, notify=report,
            canon=canon)
    else:
        passage = train_runner.Passage(
            cfg, project, owner=cfg.runner, policy=train.Policy(enabled=True),
            policy_path=spec.canon, policy_commit=commit, workflows=workflows,
            provider=provider, mechanics=mechanics, notify=lambda reason, state: report(reason),
            reservation=reservation, candidate=candidate, initial=initial, canon=canon)

    def rules(db, passage=passage):
        return train_canon.gate_rules_gap(db, cfg, spec.name,
                                          canon_path=passage.policy_path,
                                          pin=passage.canon.pin)
    canon.rules = rules
    return passage


def _canon_commit(spec: ProjectConfig, where):
    """Require the exact configured commit and its two regular canon files."""
    if not os.path.isdir(spec.canon):
        raise ConfigurationError("`%s.canon`: directory not found: %s" % (where, spec.canon))
    proc = resolutions._run(spec.canon, "rev-parse", "--verify", "--quiet",
                            spec.canon_commit + "^{commit}")
    if proc.returncode:
        raise ConfigurationError("`%s.canon_commit`: commit not found in %s"
                                 % (where, spec.canon))
    resolved = resolutions._text(proc)
    if resolved != spec.canon_commit:
        raise ConfigurationError("`%s.canon_commit`: a full commit id is required"
                                 % where)
    for name in ("processus/mecanique.yaml", "federation.yaml"):
        proc = resolutions._run(spec.canon, "ls-tree", spec.canon_commit, "--", name)
        fields = resolutions._text(proc).split()
        if proc.returncode or len(fields) != 4 or fields[0] not in ("100644", "100755") \
                or fields[1] != "blob" or fields[3] != name:
            raise ConfigurationError("`%s.canon_commit`: regular %s required at %s"
                                     % (where, name, spec.canon_commit))
    return spec.canon_commit
