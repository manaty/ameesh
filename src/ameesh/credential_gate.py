# SPDX-License-Identifier: AGPL-3.0-only
"""Imposed credential path for KMS/IAM and GitHub production acts (#7767).

The first pre-act tranche (#7761) checks the lot thread and mailbox just
before an act, but an agent that holds a credential can still call the cloud
or GitHub directly and never run that check. This module makes the credential
itself reachable only through the check:

* a dedicated Unix account (the gate account) owns the cloud and GitHub
  credential stores; the agent account does not;
* the gate account runs ``ameesh gate broker serve``, listening on a Unix
  socket whose group admits the agent accounts;
* ``ameesh gate act`` sends one command to that broker; the broker checks the
  caller with ``SO_PEERCRED``, verifies the command against a root-owned
  allow-list, runs the pre-act check itself, and only then executes the
  command with the gate account's environment;
* a production ``gh workflow run`` is dispatched with ``pregeste=<id>`` added
  by the broker (never by the caller), on the production branch and workflow
  declared by the applied canon;
* the broker writes two pieces of evidence on the lot (``pre-act/2`` start and
  end) beside the ``pre-act/1`` receipt, so the KMS/IAM or GitHub act is
  auditable without any secret in the journal.

Threat model and limits. The gate account is not root and its home is not
readable by the agent account; the policy file is root-owned and read-only for
the gate. A member of the client group can request any command the policy
admits, and each request needs a clean pre-act check; the policy is the
security boundary and must be reviewed like code. A compromised gate account,
root, or a kernel-level attacker is outside this boundary. The socket protocol
carries no credential: the command runs inside the broker, only its output
comes back. This is a host control: installing it, moving the credentials and
revoking the old access is an owner decision and is not done by the code.

The broker never reads a secret value. It points ``gcloud`` and ``gh`` at the
gate account's own ``CLOUDSDK_CONFIG`` and ``GH_CONFIG_DIR`` directories,
which are ``0700`` and owned by that account; the credential files inside
them stay ``0600``. Only absolute paths are accepted in the policy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import grp
import hashlib
import json
import os
import pwd
import re
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass

from . import config, db as db_mod, mechanics, pre_act, storage

#: policy contract version
SCHEMA = "ameesh-credential-gate/1"
#: request/response contract versions
REQUEST_SCHEMA = "ameesh-gate-request/1"
RESULT_SCHEMA = "ameesh-gate-result/1"
#: default locations (the systemd unit sets the policy explicitly)
DEFAULT_SOCKET = "/run/ameesh-gate/broker.sock"
DEFAULT_POLICY = os.environ.get("AMEESH_CREDENTIAL_GATE", "/etc/ameesh-gate/broker.json")
#: one request line, bounded before JSON parsing
MAX_REQUEST = 64 * 1024
#: one command line, bounded before it reaches a rule
MAX_ARGV = 64
MAX_ARG = 4096
MAX_ARGV_BYTES = 16 * 1024
#: a target is a label, never a secret
MAX_TARGET = 512
#: the pre-act receipt must be followed by the act within this window
FRESH_SECONDS = 30
#: flags that would print or redirect a credential, refused everywhere
FORBIDDEN_FLAGS = (
    "--log-http",
    "--trace-token",
    "--access-token-file",
    "--credential-file-override",
    "--impersonate-service-account",
    # A flags file is parsed by gcloud after the gate: it can carry any of the
    # flags above plus --project/--location/--keyring. Refuse it everywhere.
    "--flags-file",
)
#: flag values that turn a client into a debug dump of its requests
FORBIDDEN_VALUES = {
    "--verbosity": {"debug", "trace"},
}
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FLAG = re.compile(r"^--[a-z0-9][a-z0-9-]*$")
_GROUP = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: environment keys a policy may set; all values are paths or plain settings
ALLOWED_ENV = (
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "NO_COLOR",
    "GIT_TERMINAL_PROMPT",
    "CLOUDSDK_CONFIG",
    "CLOUDSDK_CORE_PROJECT",
    "CLOUDSDK_COMPUTE_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GH_CONFIG_DIR",
    "GH_HOST",
)
#: the only kinds the pre-act check knows
KINDS = ("kms", "key", "prod")


class GateError(Exception):
    """A refusal with a stable, value-free message."""

    def __init__(self, message):
        super().__init__(message)
        self.category = "gate"


class Refused(GateError):
    """The request is refused; nothing was executed."""

    def __init__(self, message):
        super().__init__(message)
        self.category = "refused"


class PolicyError(GateError):
    """The policy is absent, unreadable or malformed: fail closed."""

    def __init__(self, message):
        super().__init__(message)
        self.category = "policy"


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """One admitted command: exact executable and argv prefix."""

    id: str
    kind: str
    exe: str
    argv: tuple[str, ...]
    project: str = ""
    repo: str = ""
    inputs: tuple[str, ...] = ()
    fixed: tuple[tuple[str, str], ...] = ()
    allow: tuple[tuple[str, tuple[str, ...]], ...] = ()
    required: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    #: flags admitted with any value (booleans, formatting); all others refused
    free: tuple[str, ...] = ()
    #: exact positional arguments after the argv prefix; None leaves them free
    targets: tuple = None

    @property
    def injects_pregeste(self):
        return self.kind == "prod"


@dataclass(frozen=True)
class Policy:
    path: str
    socket: str
    socket_group: str
    agent: str
    callers_uids: tuple[int, ...]
    callers_groups: tuple[str, ...]
    rules: tuple[Rule, ...]
    env: tuple[tuple[str, str], ...]
    max_wait_seconds: int
    command_timeout_seconds: int
    max_output_bytes: int


def _mapping(value, where, keys):
    if not isinstance(value, dict):
        raise PolicyError("%s: object required" % where)
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise PolicyError("%s: unknown key(s) %s" % (where, ", ".join(unknown)))
    return value


def _absolute(value, where):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PolicyError("%s: non-empty path required" % where)
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise PolicyError("%s: absolute, normalized path required" % where)
    return value


def _regular(path, where, *, owner_ok, executable=False, follow=False):
    """Refuse a missing, non-regular, writable-by-others file.

    ``follow`` is for an admitted executable: a symlink placed by root in a
    root-owned directory is resolved, but the resolved file must still be
    regular and not writable by group or others. The policy file itself never
    follows a link.
    """
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError as exc:
        raise PolicyError("%s: unreadable (%s)" % (where, exc.strerror)) from exc
    if not follow and stat.S_ISLNK(st.st_mode):
        raise PolicyError("%s: symbolic link refused" % where)
    if not stat.S_ISREG(st.st_mode):
        raise PolicyError("%s: regular file required" % where)
    if st.st_uid not in (0, os.geteuid()):
        raise PolicyError("%s: owner must be root or the gate account" % where)
    if st.st_mode & 0o022:
        raise PolicyError("%s: writable by group or others" % where)
    if executable and not os.access(path, os.X_OK):
        raise PolicyError("%s: not executable" % where)


def _directory(path, where):
    """Refuse a missing directory owned or writable outside root/the gate."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise PolicyError("%s: unreadable (%s)" % (where, exc.strerror)) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise PolicyError("%s: directory required" % where)
    if st.st_uid not in (0, os.geteuid()):
        raise PolicyError("%s: owner must be root or the gate account" % where)
    if st.st_mode & 0o022:
        raise PolicyError("%s: writable by group or others" % where)
    return path


def _private_file(path, where):
    """A credential file: root or gate owned, mode 0600."""
    _regular(path, where, owner_ok=True)
    if os.stat(path).st_mode & 0o077:
        raise PolicyError("%s: mode 0600 required" % where)
    return path


def _load_json(path):
    _regular(path, "policy", owner_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise PolicyError("policy: unreadable or invalid JSON (%s)" % exc) from exc


def _int(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise PolicyError("%s: integer between %d and %d required" % (where, low, high))
    return value


def _rule(value, where):
    data = _mapping(value, where, ("id", "kind", "exe", "argv", "project", "repo",
                                   "inputs", "fixed", "allow", "required", "forbid",
                                   "free", "targets"))
    ident = data.get("id")
    if not isinstance(ident, str) or not _NAME.fullmatch(ident):
        raise PolicyError("%s.id: a simple name is required" % where)
    kind = data.get("kind")
    if kind not in KINDS:
        raise PolicyError("%s.kind: kms, key or prod required" % where)
    exe = _absolute(data.get("exe"), where + ".exe")
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise PolicyError("%s.argv: a non-empty list of non-empty strings is required" % where)
    for arg in argv:
        if len(arg) > MAX_ARG or "\x00" in arg or "\n" in arg or "\r" in arg:
            raise PolicyError("%s.argv: an argument is empty, too long or multiline" % where)
    project = data.get("project", "")
    repo = data.get("repo", "")
    inputs = data.get("inputs", [])
    if not isinstance(inputs, list) or not all(
            isinstance(name, str) and _NAME.fullmatch(name) and name != "pregeste"
            for name in inputs):
        raise PolicyError("%s.inputs: names other than pregeste are required" % where)
    if kind == "prod":
        if not _NAME.fullmatch(project or ""):
            raise PolicyError("%s.project: a canon project is required for prod" % where)
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise PolicyError("%s.repo: owner/name required for prod" % where)
    else:
        if project or repo or inputs:
            raise PolicyError("%s: project, repo and inputs belong to prod rules only" % where)
    fixed = data.get("fixed", {})
    if not isinstance(fixed, dict):
        raise PolicyError("%s.fixed: object required" % where)
    fixed_items = []
    for flag, wanted in fixed.items():
        if not isinstance(flag, str) or not _FLAG.fullmatch(flag):
            raise PolicyError("%s.fixed: --flag keys only" % where)
        if not isinstance(wanted, str) or not wanted or "\x00" in wanted or "\n" in wanted:
            raise PolicyError("%s.fixed.%s: a single-line value is required" % (where, flag))
        fixed_items.append((flag, wanted))
    allow = data.get("allow", {})
    if not isinstance(allow, dict):
        raise PolicyError("%s.allow: object required" % where)
    allow_items = []
    for flag, values in allow.items():
        if not isinstance(flag, str) or not _FLAG.fullmatch(flag):
            raise PolicyError("%s.allow: --flag keys only" % where)
        if not isinstance(values, list) or not values or not all(
                isinstance(v, str) and v and "\x00" not in v and "\n" not in v for v in values):
            raise PolicyError("%s.allow.%s: a non-empty list of values is required" % (where, flag))
        allow_items.append((flag, tuple(values)))
    required = data.get("required", [])
    if not isinstance(required, list) or not all(
            isinstance(flag, str) and _FLAG.fullmatch(flag) for flag in required):
        raise PolicyError("%s.required: --flag names only" % where)
    forbid = data.get("forbid", [])
    if not isinstance(forbid, list) or not all(
            isinstance(flag, str) and flag.startswith("-") and "\x00" not in flag for flag in forbid):
        raise PolicyError("%s.forbid: flag names only" % where)
    free = data.get("free", [])
    if not isinstance(free, list) or not all(
            isinstance(flag, str) and _FLAG.fullmatch(flag) for flag in free):
        raise PolicyError("%s.free: --flag names only" % where)
    targets = data.get("targets")
    if targets is not None:
        if not isinstance(targets, list) or not all(
                isinstance(target, str) and target and "\x00" not in target
                and "\n" not in target for target in targets):
            raise PolicyError("%s.targets: a list of single-line values is required" % where)
        targets = tuple(targets)
    if kind == "prod" and (fixed_items or allow_items or required or forbid or free
                           or targets is not None):
        raise PolicyError("%s: prod rules take no fixed/allow/required/forbid/free/targets" % where)
    return Rule(ident, kind, exe, tuple(argv), project, repo, tuple(inputs),
                tuple(fixed_items), tuple(allow_items), tuple(required), tuple(forbid),
                tuple(free), targets)


def load_policy(path=DEFAULT_POLICY):
    """Load and validate the root-owned policy; no fallback ever."""
    data = _mapping(_load_json(path), "policy",
                    ("schema", "socket", "socket_group", "agent", "callers", "env",
                     "max_wait_seconds", "command_timeout_seconds", "max_output_bytes",
                     "rules"))
    if data.get("schema") != SCHEMA:
        raise PolicyError("policy.schema: %s required" % SCHEMA)
    sock = _absolute(data.get("socket"), "policy.socket")
    if len(os.fsencode(sock)) >= 108:
        raise PolicyError("policy.socket: the path is too long for a Unix socket")
    group = data.get("socket_group")
    if not isinstance(group, str) or not _GROUP.fullmatch(group):
        raise PolicyError("policy.socket_group: a group name is required")
    try:
        grp.getgrnam(group)
    except KeyError as exc:
        raise PolicyError("policy.socket_group: unknown group %s" % group) from exc
    agent = data.get("agent")
    if not isinstance(agent, str) or not _NAME.fullmatch(agent):
        raise PolicyError("policy.agent: a mesh agent name is required")
    callers = _mapping(data.get("callers", {}), "policy.callers", ("uids", "groups"))
    uids = callers.get("uids", [])
    if not isinstance(uids, list) or not all(type(uid) is int and uid >= 0 for uid in uids):
        raise PolicyError("policy.callers.uids: integer uids required")
    groups = callers.get("groups", [])
    if not isinstance(groups, list) or not all(
            isinstance(name, str) and _GROUP.fullmatch(name) for name in groups):
        raise PolicyError("policy.callers.groups: group names required")
    for name in groups:
        try:
            grp.getgrnam(name)
        except KeyError as exc:
            raise PolicyError("policy.callers.groups: unknown group %s" % name) from exc
    if not uids and not groups:
        raise PolicyError("policy.callers: at least one uid or group is required")
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise PolicyError("policy.env: object required")
    env_items = []
    for key, value in env.items():
        if key not in ALLOWED_ENV or not _ENV_KEY.fullmatch(key):
            raise PolicyError("policy.env: key %r is not admitted" % key)
        if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
            raise PolicyError("policy.env.%s: a single-line value is required" % key)
        if key in ("HOME", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR",
                   "GOOGLE_APPLICATION_CREDENTIALS") and not os.path.isabs(value):
            raise PolicyError("policy.env.%s: an absolute path is required" % key)
        env_items.append((key, value))
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise PolicyError("policy.rules: a non-empty list is required")
    compiled = tuple(_rule(rule, "policy.rules[%d]" % index)
                     for index, rule in enumerate(rules))
    if len({rule.id for rule in compiled}) != len(compiled):
        raise PolicyError("policy.rules: duplicate rule id")
    max_wait = _int(data.get("max_wait_seconds", 86400), "policy.max_wait_seconds",
                    0, 7 * 86400)
    timeout = _int(data.get("command_timeout_seconds", 900),
                   "policy.command_timeout_seconds", 1, 24 * 3600)
    output = _int(data.get("max_output_bytes", 1024 * 1024), "policy.max_output_bytes",
                  1024, 64 * 1024 * 1024)
    return Policy(path, sock, group, agent, tuple(uids), tuple(groups), compiled,
                  tuple(env_items), max_wait, timeout, output)


# --------------------------------------------------------------------------
# command validation
# --------------------------------------------------------------------------

def _flags(args):
    """Yield ``(flag, value, index, count)`` for long flags.

    A ``--flag=value`` form is one token; ``--flag value`` is two. A boolean
    flag has an empty value and count 1. The caller only uses this to enforce
    fixed and allowed values; unknown flags stay untouched in the argv.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return
        if arg.startswith("--") and len(arg) > 2:
            if "=" in arg:
                flag, value = arg.split("=", 1)
                yield flag, value, index, 1
                index += 1
                continue
            flag = arg
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                yield flag, args[index + 1], index, 2
                index += 2
                continue
            yield flag, "", index, 1
            index += 1
            continue
        index += 1


def _positionals(args):
    """Return the positional arguments of ``args``, skipping long flag values.

    The heuristic is the one of :func:`_flags`: a value that starts with ``-``
    is not consumed by a flag. Rules that accept positional targets name them
    exactly; the parser must not mistake a flag value for a target.
    """
    found = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            found.extend(args[index + 1:])
            break
        if arg.startswith("--") and len(arg) > 2:
            if "=" in arg:
                index += 1
            elif index + 1 < len(args) and not args[index + 1].startswith("-"):
                index += 2
            else:
                index += 1
            continue
        found.append(arg)
        index += 1
    return found


def _check_argv(argv):
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV:
        raise Refused("a bounded, non-empty command is required")
    total = 0
    for arg in argv:
        if not isinstance(arg, str) or not arg:
            raise Refused("every command argument must be a non-empty string")
        if len(arg) > MAX_ARG or "\x00" in arg or "\n" in arg or "\r" in arg:
            raise Refused("a command argument is too long or multiline")
        total += len(arg)
    if total > MAX_ARGV_BYTES:
        raise Refused("command is too long")
    for arg in argv:
        if arg in FORBIDDEN_FLAGS or arg.split("=", 1)[0] in FORBIDDEN_FLAGS:
            raise Refused("a credential-dumping flag is refused")
    for flag, value, _index, _count in _flags(argv):
        if flag in FORBIDDEN_VALUES and value in FORBIDDEN_VALUES[flag]:
            raise Refused("a debug flag is refused")


def _match_rule(policy, kind, argv):
    candidates = [rule for rule in policy.rules
                  if rule.kind == kind and argv and argv[0] == rule.exe
                  and tuple(argv[1:1 + len(rule.argv)]) == rule.argv]
    if not candidates:
        raise Refused("command is not admitted by the credential gate")
    candidates.sort(key=lambda rule: len(rule.argv), reverse=True)
    if len(candidates) > 1 and len(candidates[0].argv) == len(candidates[1].argv):
        raise Refused("command matches two credential-gate rules")
    return candidates[0]


def _apply_rule(policy, rule, argv):
    """Check a non-prod command, append fixed flags, return the final argv."""
    rest = argv[1 + len(rule.argv):]
    if rule.targets is not None and tuple(_positionals(rest)) != rule.targets:
        raise Refused("positional target is not admitted by rule %s" % rule.id)
    for flag in rule.forbid:
        for arg in rest:
            if arg == flag or arg.startswith(flag + "="):
                raise Refused("a flag refused by rule %s" % rule.id)
    known = ({flag for flag, _wanted in rule.fixed}
             | {flag for flag, _values in rule.allow}
             | set(rule.required) | set(rule.free))
    present = {}
    for arg in rest:
        if arg.startswith("-") and not arg.startswith("--") and arg != "-":
            raise Refused("short flag %s is not admitted by rule %s" % (arg, rule.id))
    for flag, value, index, count in _flags(rest):
        if flag not in known:
            raise Refused("flag %s is not admitted by rule %s" % (flag, rule.id))
        present.setdefault(flag, []).append((value, count, index))
        allowed = dict(rule.allow)
        if flag in allowed:
            if not value:
                raise Refused("rule %s requires a value for %s" % (rule.id, flag))
            if value not in allowed[flag]:
                raise Refused("value of %s is not admitted by rule %s" % (flag, rule.id))
    for flag, wanted in rule.fixed:
        values = present.get(flag, [])
        if values and any(value != wanted or not value for value, _count, _index in values):
            raise Refused("fixed flag %s disagrees with rule %s" % (flag, rule.id))
    for flag in rule.required:
        if flag not in present or any(not value for value, _count, _index
                                      in present[flag]):
            raise Refused("flag %s requires a value by rule %s" % (flag, rule.id))
    final = list(argv)
    extra = []
    for flag, wanted in rule.fixed:
        if flag not in present:
            extra.append("%s=%s" % (flag, wanted))
    if extra:
        final = final[:1 + len(rule.argv)] + extra + final[1 + len(rule.argv):]
    return final


def _prod_spec(document, rule):
    """The canon production block for this rule, or a refusal."""
    if document is None:
        raise Refused("the applied canon is unavailable")
    try:
        spec = document.override(rule.project)
    except Exception as exc:
        raise Refused("the applied canon cannot be read") from exc
    if spec is None or not spec.production_branch:
        raise Refused("project %s declares no production" % rule.project)
    workflows = tuple(spec.production_workflows or ())
    if not workflows:
        raise Refused("project %s declares no production workflow" % rule.project)
    return spec.production_branch, workflows


def _parse_prod(rule, argv, document):
    """Validate ``gh workflow run`` and return (argv, target, workflow, ref)."""
    rest = argv[1 + len(rule.argv):]
    if not rest or rest[0].startswith("-"):
        raise Refused("a workflow must be named first")
    workflow = rest[0]
    branch, workflows = _prod_spec(document, rule)
    files = {name: name for name in workflows}
    files.update({name[:-4]: name for name in workflows if name.endswith(".yml")})
    files.update({name[:-5]: name for name in workflows if name.endswith(".yaml")})
    if workflow not in files:
        raise Refused("workflow is not a declared production workflow")
    workflow_file = files[workflow]
    repo = ref = None
    inputs = {}
    index = 1
    while index < len(rest):
        arg = rest[index]
        if arg in ("--repo", "-R"):
            if index + 1 >= len(rest):
                raise Refused("--repo needs a value")
            if repo is not None:
                raise Refused("--repo is given twice")
            repo = rest[index + 1]
            index += 2
        elif arg.startswith("--repo="):
            if repo is not None:
                raise Refused("--repo is given twice")
            repo = arg.split("=", 1)[1]
            index += 1
        elif arg == "--ref":
            if index + 1 >= len(rest):
                raise Refused("--ref needs a value")
            if ref is not None:
                raise Refused("--ref is given twice")
            ref = rest[index + 1]
            index += 2
        elif arg.startswith("--ref="):
            if ref is not None:
                raise Refused("--ref is given twice")
            ref = arg.split("=", 1)[1]
            index += 1
        elif arg in ("-f", "--raw-field"):
            if index + 1 >= len(rest):
                raise Refused("%s needs name=value" % arg)
            name, sep, value = rest[index + 1].partition("=")
            if not sep or not name or not value:
                raise Refused("%s needs name=value" % arg)
            if name == "pregeste":
                raise Refused("the pregeste input is written by the gate only")
            if name not in rule.inputs:
                raise Refused("input %s is not admitted for this workflow" % name)
            if name in inputs:
                raise Refused("input %s is given twice" % name)
            inputs[name] = value
            index += 2
        elif arg.startswith("--raw-field=") or arg.startswith("-f"):
            raise Refused("use the separated -f name=value form")
        else:
            raise Refused("workflow dispatch flag %s is not admitted" % arg.split("=", 1)[0])
    if repo != rule.repo:
        raise Refused("--repo must be %s" % rule.repo)
    if ref != branch:
        raise Refused("--ref must be the production branch %s" % branch)
    target = "%s#%s@%s" % (rule.repo, workflow_file, branch)
    return list(argv), target


# --------------------------------------------------------------------------
# broker
# --------------------------------------------------------------------------

class _Unavailable(GateError):
    """A dependency is missing; the request is refused and nothing ran."""

    def __init__(self, message):
        super().__init__(message)
        self.category = "unavailable"


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


class Broker:
    """Validate one request, run the pre-act check, execute the command."""

    def __init__(self, policy, *, cfg=None, db_factory=None, document=None,
                 runner=None, clock=time.time, monotonic=time.monotonic,
                 sleep=time.sleep):
        self.policy = policy
        self.cfg = cfg
        self._db_factory = db_factory or (lambda: db_mod.open_db(self.cfg))
        self._document_factory = document
        self._runner = runner or self._execute
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._document = None
        self._document_lock = threading.Lock()
        # One act at a time: the duplicate check and the start event must not
        # interleave, or two concurrent copies of one request could both run.
        self._act_lock = threading.Lock()

    # -- policy checks -----------------------------------------------------

    def _document_value(self):
        with self._document_lock:
            if self._document is None:
                try:
                    if self._document_factory is not None:
                        self._document = (self._document_factory()
                                          if callable(self._document_factory)
                                          else self._document_factory)
                    else:
                        if self.cfg is None:
                            raise _Unavailable("the applied canon is unavailable")
                        self._document = mechanics.configured(self.cfg)
                except _Unavailable:
                    raise
                except Exception as exc:
                    raise _Unavailable("the applied canon is unavailable") from exc
            return self._document

    def _authorize(self, uid, gid):
        if uid in self.policy.callers_uids:
            return
        try:
            username = pwd.getpwuid(uid).pw_name
            gids = set(os.getgrouplist(username, gid))
        except (KeyError, OSError):
            gids = {gid}
        allowed = set()
        for name in self.policy.callers_groups:
            try:
                allowed.add(grp.getgrnam(name).gr_gid)
            except KeyError:
                raise _Unavailable("a caller group in the policy no longer exists")
        if not gids & allowed:
            raise Refused("this account may not use the credential gate")

    def _validate(self, request):
        _check_argv(request["argv"])
        rule = _match_rule(self.policy, request["kind"], request["argv"])
        if rule.kind == "prod":
            argv, target = _parse_prod(rule, request["argv"], self._document_value())
            if request["target"] and request["target"] != target:
                raise Refused("--target must match the dispatch target")
            return rule, argv, target, True
        final = _apply_rule(self.policy, rule, request["argv"])
        target = request["target"] or rule.id
        return rule, final, target, False

    # -- pre-act and evidence ---------------------------------------------

    def _database(self):
        if self.cfg is None:
            raise _Unavailable("the credential gate has no database configuration")
        try:
            db = self._db_factory()
        except Exception as exc:
            raise _Unavailable("the mesh database is unavailable") from exc
        if storage.is_remote(db):
            db.close()
            raise _Unavailable("a transactional database connection is required")
        return db

    def _refuse_duplicate(self, tx, request):
        rows = tx.query(
            "SELECT id FROM work_item_events "
            "WHERE work_item_id = %s AND evidence->>'type' = %s "
            "AND evidence->>'request_id' = %s ORDER BY id DESC LIMIT 1",
            (request["lot"], pre_act.EFFECT_TYPE, request["request_id"]))
        if rows:
            raise Refused("this request was already consumed")

    def _database_now(self):
        """The reading bound taken on the database clock (#7815).

        The journal and mailbox `created_at` are written by the database; a
        bound taken on the host clock can miss a STOP written during the
        wait. Read `now()` in the same clock before the wait, close this
        connection, and let the check reopen a fresh one, as in #7775.
        """
        db = self._database()
        try:
            return pre_act.database_now(db)
        except pre_act.Refused as exc:
            raise _Unavailable("the mesh database clock is unavailable") from exc
        finally:
            db.close()

    def _check(self, request, target, since):
        db = self._database()
        try:
            with db.transaction(isolation="READ COMMITTED") as tx:
                self._refuse_duplicate(tx, request)
                try:
                    return pre_act.inspect(
                        tx, request["lot"], self.policy.agent, since, request["kind"],
                        target, request_id=request["request_id"], cfg=self.cfg)
                except pre_act.Refused as exc:
                    raise Refused(str(exc)) from exc
        finally:
            db.close()

    def _start(self, request, rule, target, argv, pregeste, started):
        db = self._database()
        try:
            with db.transaction(isolation="READ COMMITTED") as tx:
                pre_act.record_effect_start(
                    tx, request["lot"], self.policy.agent, pregeste,
                    request["request_id"], request["kind"], target, rule.id,
                    argv, started)
        finally:
            db.close()

    def _end(self, request, pregeste, target, exit_code, timed_out, started,
             finished, out, err):
        db = self._database()
        try:
            with db.transaction(isolation="READ COMMITTED") as tx:
                pre_act.record_effect_end(
                    tx, request["lot"], self.policy.agent, pregeste,
                    request["request_id"], request["kind"], target,
                    exit_code, timed_out, started, finished,
                    _sha256(out) if out is not None else None,
                    _sha256(err) if err is not None else None)
        finally:
            db.close()

    # -- execution ---------------------------------------------------------

    def _environment(self):
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", pwd.getpwuid(os.geteuid()).pw_dir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        }
        env.update(dict(self.policy.env))
        return env

    def _execute(self, argv):
        """Run one admitted command; return (exit, timed_out, stdout, stderr)."""
        env = self._environment()
        try:
            process = subprocess.Popen(
                argv, cwd="/", env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, close_fds=True)
        except OSError as exc:
            raise _Unavailable("the admitted executable cannot start") from exc
        timed_out = False
        try:
            out, err = process.communicate(timeout=self.policy.command_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            out, err = process.communicate()
        code = process.returncode
        if code is not None and code < 0:
            code = 128 - code
        return code, timed_out, out, err

    def _bounded(self, out):
        limit = self.policy.max_output_bytes
        if len(out) <= limit:
            return out, False
        return out[:limit], True

    def handle(self, value, uid, gid):
        """Process one decoded request; return a JSON-serializable result."""
        request = None
        started = self._clock()
        pregeste = None
        try:
            self._authorize(uid, gid)
            request = _parse_request(value)
            rule, argv, target, inject = self._validate(request)
            wait_until = request["wait_until_epoch"]
            # The bound is read before the wait and in the clock of the
            # `created_at` it is compared to; the wait itself stays on the
            # host clock, as the caller's absolute deadline was formed there
            # (#7815).
            since = self._database_now()
            if wait_until is not None:
                delay = wait_until - self._clock()
                if delay > self.policy.max_wait_seconds:
                    raise Refused("the scheduled wait is longer than the gate admits")
                if delay > 0:
                    self._sleep(delay)
            with self._act_lock:
                checked = self._monotonic()
                pregeste = self._check(request, target, since)
                if self._monotonic() - checked > FRESH_SECONDS:
                    raise Refused("the pre-act check is older than the admitted window")
                if inject:
                    argv = argv + ["-f", "pregeste=" + pregeste]
                # Everything up to here is a refusal: no act has run. The
                # start event is written before the command and the end event
                # after it.
                started = self._clock()
                self._start(request, rule, target, argv, pregeste, started)
            try:
                code, timed_out, out, err = self._runner(argv)
            except Exception:
                finished = self._clock()
                try:
                    self._end(request, pregeste, target, None, False, started,
                              finished, None, None)
                except Exception:
                    traceback.print_exc()
                raise
            finished = self._clock()
            out, out_cut = self._bounded(out or b"")
            err, err_cut = self._bounded(err or b"")
            audit = "recorded"
            try:
                self._end(request, pregeste, target, code, timed_out, started,
                          finished, out, err)
            except Exception:
                audit = "end-unrecorded"
                traceback.print_exc()
            result = {
                "schema": RESULT_SCHEMA,
                "ok": True,
                "request_id": request["request_id"],
                "pregeste": pregeste,
                "target": target,
                "rule": rule.id,
                "exit": code,
                "timed_out": bool(timed_out),
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace"),
                "truncated": bool(out_cut or err_cut),
                "audit": audit,
            }
            return result
        except GateError as exc:
            return {"schema": RESULT_SCHEMA, "ok": False, "error": str(exc),
                    "request_id": (request or {}).get("request_id", "")}
        except Exception:
            traceback.print_exc()
            return {"schema": RESULT_SCHEMA, "ok": False,
                    "error": "credential gate internal error",
                    "request_id": (request or {}).get("request_id", "")}


# --------------------------------------------------------------------------
# request parsing and audit
# --------------------------------------------------------------------------

def _parse_request(value):
    if not isinstance(value, dict):
        raise Refused("request must be an object")
    unknown = sorted(set(value) - {"schema", "request_id", "lot", "kind", "target",
                                   "wait_until", "argv"})
    if unknown:
        raise Refused("unknown request key(s) %s" % ", ".join(unknown))
    if value.get("schema") != REQUEST_SCHEMA:
        raise Refused("request schema is not %s" % REQUEST_SCHEMA)
    request_id = value.get("request_id")
    try:
        parsed = uuid.UUID(request_id)
    except (AttributeError, TypeError, ValueError):
        raise Refused("a UUID request_id is required")
    if str(parsed) != request_id:
        raise Refused("a canonical UUID request_id is required")
    lot = value.get("lot")
    if type(lot) is not int or lot <= 0:
        raise Refused("a positive lot is required")
    kind = value.get("kind")
    if kind not in KINDS:
        raise Refused("kind must be kms, key or prod")
    target = value.get("target", "")
    if not isinstance(target, str) or len(target) > MAX_TARGET or any(
            ord(char) < 32 for char in target):
        raise Refused("target is invalid")
    wait_until = value.get("wait_until")
    epoch = None
    if wait_until is not None:
        if not isinstance(wait_until, str):
            raise Refused("wait_until must be an ISO timestamp or null")
        epoch = pre_act.parse_since(wait_until)
    argv = value.get("argv")
    _check_argv(argv)
    return {"schema": REQUEST_SCHEMA, "request_id": request_id, "lot": lot,
            "kind": kind, "target": target, "wait_until": wait_until,
            "wait_until_epoch": epoch, "argv": list(argv)}


def audit_events(events, now, *, stale_seconds=3600):
    """Anomalies in the ``pre-act/2`` trace of the broker.

    A start without an end after the stale delay is an act whose outcome was
    never recorded; an end without a start is forged or truncated. A receipt
    without an act is deliberately not an anomaly here: the first tranche
    also writes receipts for direct guarded commands.
    """
    starts = {}
    ends = {}
    anomalies = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != pre_act.EFFECT_TYPE:
            continue
        pregeste = event.get("pregeste_id")
        if not isinstance(pregeste, str) or not pregeste:
            anomalies.append({"kind": "missing-receipt", "pregeste": ""})
            continue
        phase = event.get("phase")
        if phase == "start":
            if pregeste in starts:
                anomalies.append({"kind": "duplicate-start", "pregeste": pregeste})
            starts[pregeste] = event
        elif phase == "end":
            if pregeste in ends:
                anomalies.append({"kind": "duplicate-end", "pregeste": pregeste})
            ends[pregeste] = event
        else:
            anomalies.append({"kind": "unknown-phase", "pregeste": pregeste})
    for pregeste, start in starts.items():
        if pregeste in ends:
            continue
        try:
            age = float(now) - float(start.get("started"))
        except (TypeError, ValueError):
            age = 0.0
        if age >= stale_seconds:
            anomalies.append({"kind": "unpaired-start", "pregeste": pregeste,
                              "lot": start.get("lot"), "act_kind": start.get("kind"),
                              "started": start.get("started")})
    for pregeste, end in ends.items():
        if pregeste not in starts:
            anomalies.append({"kind": "unpaired-end", "pregeste": pregeste,
                              "lot": end.get("lot"), "act_kind": end.get("kind")})
    return anomalies


# --------------------------------------------------------------------------
# Unix socket server
# --------------------------------------------------------------------------

def _group_gid(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError as exc:
        raise PolicyError("unknown group %s" % name) from exc


def _check_socket_dir(path):
    directory = os.path.dirname(path)
    if not directory:
        raise PolicyError("socket: an absolute path with a directory is required")
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise PolicyError("socket directory: unreadable (%s)" % exc.strerror) from exc
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise PolicyError("socket directory: a real directory is required")
    if st.st_uid not in (0, os.geteuid()):
        raise PolicyError("socket directory: owner must be root or the gate account")
    if st.st_mode & 0o022:
        raise PolicyError("socket directory: writable by group or others")


def _bind_socket(path, group_gid):
    _check_socket_dir(path)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
            raise PolicyError("socket: an existing foreign file is in the way")
        os.unlink(path)
    old = os.umask(0o007)
    try:
        server = socketserver.ThreadingUnixStreamServer(path, _Handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o660)
    try:
        os.chown(path, -1, group_gid)
    except OSError as exc:
        server.server_close()
        os.unlink(path)
        raise PolicyError("socket: cannot set the client group (%s)" % exc.strerror) from exc
    server.daemon_threads = True
    return server


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not hasattr(socket, "SO_PEERCRED"):
            self.request.sendall(json.dumps({
                "schema": RESULT_SCHEMA, "ok": False,
                "error": "peer credentials are unavailable on this platform"}).encode() + b"\n")
            return
        raw = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                      struct.calcsize("3i"))
        _pid, uid, gid = struct.unpack("3i", raw)
        try:
            self.request.settimeout(30)
            chunks = []
            size = 0
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_REQUEST:
                    raise Refused("request is too large")
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            body, _sep, _rest = b"".join(chunks).partition(b"\n")
            if not body:
                raise Refused("empty request")
            value = json.loads(body.decode("utf-8"))
        except GateError as exc:
            result = {"schema": RESULT_SCHEMA, "ok": False, "error": str(exc)}
        except (OSError, ValueError, UnicodeDecodeError):
            result = {"schema": RESULT_SCHEMA, "ok": False,
                      "error": "unreadable request"}
        else:
            result = self.server.broker.handle(value, uid, gid)
        try:
            self.request.sendall(json.dumps(result, sort_keys=True).encode("utf-8") + b"\n")
        except OSError:
            pass


def serve(policy, *, cfg=None, broker=None):
    """Serve until SIGTERM/SIGINT; return only after closing the socket."""
    broker = broker or Broker(policy, cfg=cfg)
    server = _bind_socket(policy.socket, _group_gid(policy.socket_group))
    server.broker = broker
    stopping = threading.Event()

    def stop(_signum=None, _frame=None):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, stop)
        except (ValueError, OSError):
            pass
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        server.server_close()
        try:
            os.unlink(policy.socket)
        except OSError:
            pass
    return 0


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

def _socket_path(value=""):
    return value or os.environ.get("AMEESH_GATE_SOCKET") or DEFAULT_SOCKET


def call(path, request, *, timeout=None):
    """Send one request and return the decoded result; never inspects a secret."""
    data = json.dumps(request, sort_keys=True).encode("utf-8") + b"\n"
    if len(data) > MAX_REQUEST:
        raise Refused("request is too large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if timeout is not None:
        client.settimeout(timeout)
    try:
        client.connect(path)
        client.sendall(data)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(part) for part in chunks) > 4 * 1024 * 1024:
                raise Refused("credential gate response is too large")
    except OSError as exc:
        raise Refused("credential gate unavailable (%s)" % exc.strerror) from exc
    finally:
        client.close()
    body = b"".join(chunks).partition(b"\n")[0]
    if not body:
        raise Refused("credential gate closed without an answer")
    try:
        result = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused("credential gate answer is unreadable") from exc
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
        raise Refused("credential gate answer has an unknown schema")
    return result


def _iso_utc(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def client_main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh gate act")
    parser.add_argument("--lot", type=int, required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--target", default="")
    parser.add_argument("--wait-until", help="ISO timestamp with timezone: the gate waits, then checks")
    parser.add_argument("--socket", default="")
    parser.add_argument("--json", action="store_true", help="print the result object")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.lot <= 0:
        print("credential gate refused: invalid lot", file=sys.stderr)
        return 1
    wait_until = None
    if args.wait_until:
        try:
            wait_until = _iso_utc(pre_act.parse_since(args.wait_until))
        except pre_act.Refused as exc:
            print("credential gate refused: %s" % exc, file=sys.stderr)
            return 1
    request = {
        "schema": REQUEST_SCHEMA,
        "request_id": str(uuid.uuid4()),
        "lot": args.lot,
        "kind": args.kind,
        "target": args.target.strip(),
        "wait_until": wait_until,
        "argv": command,
    }
    try:
        result = call(_socket_path(args.socket), request)
    except GateError as exc:
        print("credential gate refused: %s" % exc, file=sys.stderr)
        return 1
    if not result.get("ok"):
        print("credential gate refused: %s" % result.get("error", "unknown refusal"),
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        if result.get("stdout"):
            sys.stdout.buffer.write(result["stdout"].encode("utf-8"))
            sys.stdout.buffer.flush()
        if result.get("stderr"):
            sys.stderr.buffer.write(result["stderr"].encode("utf-8"))
            sys.stderr.buffer.flush()
        if result.get("audit") != "recorded":
            print("credential gate: the act was not completely recorded",
                  file=sys.stderr)
    return int(result.get("exit") or 0)


# --------------------------------------------------------------------------
# broker CLI and audit CLI
# --------------------------------------------------------------------------

def _broker_config():
    return config.load()


def _check_policy(policy, cfg):
    """Read-only checks before the service starts; returns a report."""
    report = {"schema": "ameesh-gate-check/1", "policy": policy.path,
              "socket": policy.socket, "agent": policy.agent, "rules": len(policy.rules),
              "canon": "", "database": False}
    _check_socket_dir(policy.socket)
    for key, value in policy.env:
        if key in ("HOME", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR"):
            _directory(value, "policy.env.%s" % key)
        elif key == "GOOGLE_APPLICATION_CREDENTIALS":
            _private_file(value, "policy.env.%s" % key)
    for rule in policy.rules:
        _regular(rule.exe, "rule %s executable" % rule.id, owner_ok=True,
                 executable=True, follow=True)
    if cfg is not None:
        if cfg.canon or cfg.extra_canons:
            try:
                document = mechanics.configured(cfg)
            except Exception as exc:
                raise PolicyError("canon: unavailable (%s)" % type(exc).__name__) from exc
            for rule in policy.rules:
                if rule.kind == "prod":
                    _prod_spec(document, rule)
            report["canon"] = document.commit
        elif any(rule.kind == "prod" for rule in policy.rules):
            raise PolicyError("canon: a production rule needs an applied canon")
        db = None
        try:
            db = db_mod.open_db(cfg)
            if storage.is_remote(db):
                raise PolicyError("database: a transactional connection is required")
            report["database"] = True
        except PolicyError:
            raise
        except Exception as exc:
            raise PolicyError("database: unavailable (%s)" % type(exc).__name__) from exc
        finally:
            if db is not None:
                db.close()
    return report


def broker_main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh gate broker")
    parser.add_argument("action", choices=("serve", "check"))
    parser.add_argument("--config", default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.config)
        cfg = _broker_config()
        if args.action == "check":
            report = _check_policy(policy, cfg)
            if args.json:
                print(json.dumps(report, sort_keys=True))
            else:
                print("policy: %s" % report["policy"])
                print("socket: %s (%s)" % (report["socket"], policy.socket_group))
                print("agent: %s, rules: %d, canon: %s, database: %s" % (
                    report["agent"], report["rules"], report["canon"] or "none",
                    "ok" if report["database"] else "refused"))
            return 0
        if cfg is None:
            raise PolicyError("no configuration")
        report = _check_policy(policy, cfg)
        print("credential gate: %d rule(s), canon %s" % (
            report["rules"], report["canon"] or "none"), file=sys.stderr)
        return serve(policy, cfg=cfg)
    except GateError as exc:
        print("credential gate broker: %s" % exc, file=sys.stderr)
        return 1


def audit_main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh gate audit")
    parser.add_argument("--since", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--stale-seconds", type=int, default=3600)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        since = pre_act.parse_since(args.since)
        cfg = _broker_config()
        db = db_mod.open_db(cfg)
        try:
            rows = db.query(
                "SELECT evidence, extract(epoch from created_at)::float8 AS created_ts "
                "FROM work_item_events WHERE evidence->>'type' = %s "
                "AND created_at >= to_timestamp(%s) ORDER BY id",
                (pre_act.EFFECT_TYPE, since))
        finally:
            db.close()
        events = [row.get("evidence") if isinstance(row.get("evidence"), dict)
                  else json.loads(row["evidence"]) for row in rows]
        anomalies = audit_events(events, time.time(), stale_seconds=args.stale_seconds)
    except (GateError, ValueError, TypeError) as exc:
        print("credential gate audit: %s" % exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(anomalies, sort_keys=True))
    else:
        for anomaly in anomalies:
            print("%(kind)s %(pregeste)s lot=%(lot)s" % {
                "kind": anomaly.get("kind", "?"),
                "pregeste": anomaly.get("pregeste", "") or "-",
                "lot": anomaly.get("lot", "-")})
        if not anomalies:
            print("credential gate audit: no anomaly")
    return 2 if anomalies else 0
