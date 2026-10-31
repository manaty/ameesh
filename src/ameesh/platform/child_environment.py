# SPDX-License-Identifier: AGPL-3.0-only
"""#5038: service children receive only their required credentials.

Installed at application startup, the Popen boundary also covers subprocess.run,
check_output, third-party helpers and calls with no explicit environment. Parent
credentials remain available to the application; they are never removed from
os.environ while other threads might need them. A prepared harness environment
is granted explicitly, after account selection, rather than by executable name.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping

# Transport credentials, not provider/account credentials. A CLI needs only its
# own authentication. Python/shell wrappers inherit no blanket exemption.
GITHUB_CREDENTIALS = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                                "GITHUB_ENTERPRISE_TOKEN"})
MESH_CREDENTIALS = frozenset({"AMEESH_DSN", "AGENT_MESH_DSN", "AMEESH_DATABASE_URL",
                              "AGENT_MESH_DATABASE_URL", "AMEESH_EXEC_TOKEN",
                              "PGPASSWORD"})
TOOL_CREDENTIALS = {
    "psql": frozenset({"PGPASSWORD"}),
    "pg_dump": frozenset({"PGPASSWORD"}),
    "pg_restore": frozenset({"PGPASSWORD"}),
    "gh": GITHUB_CREDENTIALS,
    "git": GITHUB_CREDENTIALS,
    "scw": frozenset({"SCW_SECRET_KEY"}),
    "gcloud": frozenset({"GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_ACCESS_TOKEN",
                           "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"}),
    "ameesh": MESH_CREDENTIALS,
    "agent-mail": MESH_CREDENTIALS,
    "agent-runner": MESH_CREDENTIALS,
}
SECRET_PARTS = re.compile(r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIALS?)(?:_|$)")
DECLARED_CREDENTIALS: set[str] = set()
SECRET_SUFFIXES = ("_KEY", "_DSN", "_DATABASE_URL")


class GrantedEnvironment(dict):
    """An explicit grant belongs to one spawn, not a mutable global allowlist."""

    def copy(self):
        return GrantedEnvironment(self, self.credentials)

    def __init__(self, environment, names):
        super().__init__(environment)
        self.credentials = frozenset(names)


def grant(environment: Mapping, names) -> GrantedEnvironment:
    return GrantedEnvironment(environment, names)


def secret_name(name) -> bool:
    upper = os.fsdecode(name).upper()
    return (upper in DECLARED_CREDENTIALS or upper in {"PGPASSWORD", "DSN", "DATABASE_URL"}
            or upper.endswith(SECRET_SUFFIXES) or bool(SECRET_PARTS.search(upper)))


def declare_accounts(accounts) -> None:
    """Account sources/destinations may have arbitrary variable names."""
    if isinstance(accounts, dict):
        for key, value in accounts.items():
            if key in ("key_env", "env") and isinstance(value, str) and value:
                DECLARED_CREDENTIALS.add(value.upper())
            elif isinstance(value, (dict, list)):
                declare_accounts(value)
    elif isinstance(accounts, list):
        for value in accounts:
            declare_accounts(value)


def for_child(argv, environment=None, *, executable=None, shell=False) -> dict:
    source = os.environ if environment is None else environment
    allowed = set(getattr(source, "credentials", ()))
    command = executable
    if command is None and not shell:
        command = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
    # A shell command string is never parsed to infer authority.
    if command is not None and not shell:
        allowed.update(TOOL_CREDENTIALS.get(os.path.basename(os.fsdecode(command)), ()))
    return {name: value for name, value in source.items()
            if not secret_name(name) or os.fsdecode(name) in allowed}


def _route_names(value) -> tuple:
    """Channel names of a route, as `notify._names` reads them (never raises)."""
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    if isinstance(value, (list, tuple)):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def notify_credentials(raw) -> frozenset[str]:
    """Register channel secrets, including arbitrary variable names.

    File-backed channels need no environment grant. Registration also keeps
    these credentials out of unrelated children and executor turns.
    """
    names = set()
    if not isinstance(raw, dict):
        return frozenset()
    # Same reading as `notify.parse_config`: null channels/routes are empty,
    # a channel declared null is `{}`, routes may be a comma-separated string.
    channels = raw.get("channels") or {}
    if not isinstance(channels, dict):
        return frozenset()
    channels = {name: {} if channel is None else channel
                for name, channel in channels.items()}
    routes = raw.get("routes") or {}
    references = [raw.get("default") or []]
    if isinstance(routes, dict):
        references.extend(routes.values())
    if any("slack" in _route_names(value) for value in references):
        channels.setdefault("slack", {})
    for name, channel in channels.items():
        if not isinstance(channel, dict):
            continue
        for env_key, file_key in (("token_env", "token_file"),
                                  ("webhook_env", "webhook_file")):
            variable = channel.get(env_key)
            if (env_key == "webhook_env" and not variable
                    and (channel.get("type") or name) == "slack"):
                variable = "AMEESH_SLACK_WEBHOOK"
            if isinstance(variable, str) and variable:
                DECLARED_CREDENTIALS.add(variable.upper())
                if not channel.get(file_key):
                    names.add(variable)
    return frozenset(names)


def role_transport(environment: Mapping, argv, notify_config) -> GrantedEnvironment:
    """Grant channel credentials only to the role's direct notify command."""
    names = notify_credentials(notify_config)
    env = execution_transport(environment)
    if (len(argv) >= 2 and os.path.basename(argv[0]) == "ameesh"
            and argv[1] == "notify"):
        return grant(env, env.credentials | names)
    return env


def execution_transport(environment: Mapping) -> GrantedEnvironment:
    """Mechanical role/deployment scripts need mesh/GitHub, never provider keys."""
    return grant(environment, TOOL_CREDENTIALS["ameesh"] | TOOL_CREDENTIALS["gh"])


def harness(environment: Mapping, family: str, profile=None) -> GrantedEnvironment:
    """Grant the selected account and the turn's execution transports only."""
    from .. import accounts
    names = set(TOOL_CREDENTIALS["ameesh"]) | set(TOOL_CREDENTIALS["gh"])
    variable = getattr(profile, "env", "") or accounts.default_key_env_of(family)
    if variable:
        names.add(variable)
    if family == "claude":
        names.update({"ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                      "CLAUDE_CODE_OAUTH_REFRESH_TOKEN"})
    elif family == "codex":
        names.add("CODEX_API_KEY")
    return grant(environment, names)


def install() -> None:
    """Guard Python's process creation boundary, once, before service threads.

    No preexec_fn: it is unsafe after threads start, and exec would reset the
    parent's dumpable flag anyway. Explicit env mappings are copied, never
    mutated. Grants must be made by the trusted caller that prepared credentials.
    """
    from functools import wraps
    original = subprocess.Popen.__init__
    if getattr(original, "_ameesh_child_environment", False):
        return

    @wraps(original)
    def protected(self, args, *positional, **kwargs):
        # Keep the Popen API, including positional env (argument 10 after args).
        values = list(positional)
        env = values[9] if len(values) > 9 else kwargs.get("env")
        executable = values[1] if len(values) > 1 else kwargs.get("executable")
        shell = values[7] if len(values) > 7 else kwargs.get("shell", False)
        clean = for_child(args, env, executable=executable, shell=shell)
        if len(values) > 9:
            values[9] = clean
        else:
            kwargs["env"] = clean
        original(self, args, *values, **kwargs)

    protected._ameesh_child_environment = True
    # Patch the boundary method, not the class: existing Popen subclasses
    # (the resident's LeaderPopen) must receive exactly the same protection.
    subprocess.Popen.__init__ = protected


def attest_venv() -> None:
    """#5100: a non-dumpable process attests its executable for `controler`.

    Without the attestation `poste.sh controler` stays red; the service itself
    keeps running.
    """
    import sys
    from .process import publish_venv_attestation
    try:
        publish_venv_attestation()
    except OSError as exc:
        print("ameesh: venv attestation not published: %s" % exc, file=sys.stderr)


def harden() -> bool:
    """Protect a long-running service entry before configuration and worker threads."""
    import sys
    from .process import become_undumpable
    try:
        hidden = become_undumpable()
    except OSError as exc:
        print("ameesh: refusing to run: process environment protection failed (%s)" % exc,
              file=sys.stderr)
        return False
    install()
    if hidden:
        attest_venv()
    return True


def managed_entry() -> bool:
    """Filter every child; hide only a verified installed service MainPID.

    INVOCATION_ID is only a cheap hint: children inherit it, so the manager
    snapshot and kernel identity must still identify this exact process.
    Manual CLI calls and service children stay readable to holder scans.
    """
    install()
    if os.environ.get("INVOCATION_ID"):
        from .process import _trusted_executor, executor_processes
        if _trusted_executor(os.getpid(), executor_processes):
            return harden()
    return True
