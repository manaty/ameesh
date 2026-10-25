# SPDX-License-Identifier: AGPL-3.0-only
"""H1 (L232a/L232b): the enrolled handoff adapter contract.

The controller calls an adapter for every prerequisite check and host effect.
An effect request carries the operation id, the source lease owner and its
epoch, and the controller epoch; a request missing any of them is refused
before the adapter is entered. The concrete adapter and
``requirements_for(agent, host)`` come from the single pinned factory
(``deploy/mise-a-jour/fabrique-handoff.py``), never from a module named by a
message or an enrollment document.

Proof checks stay in the controller (``handoffs._proof``): this module only
binds the request identity and the method set, so H2/H3 helpers can be
composed without widening what a caller may choose.
"""
from collections.abc import Mapping
from pathlib import Path
import re
from urllib.parse import urlsplit

from .config import NAME_RE


#: Discovery helpers that receive an enrolled host or agent, not an operation.
INVENTORY = "inventory"
REPOSITORY = "repository"
#: Prerequisite check called before each host effect.
REVALIDATE = "revalidate"
#: Host effects, in lifecycle order. ``recover_source`` is the lost-source
#: recovery and ``reconcile_effect`` the explicit retry of an uncertain one.
EFFECTS = ("prepare", "stop_source", "publish", "transfer", "session",
           "admit", "enable", "verify", "restore_source", "recover_source",
           "reconcile_effect")
#: Every method an enrolled adapter must expose.
METHODS = (INVENTORY, REPOSITORY, REVALIDATE) + EFFECTS
#: Methods that receive a durable operation request.
REQUEST_METHODS = (REVALIDATE,) + EFFECTS

#: Operation ids are durable and bounded: no caller-chosen command or path.
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _bounded(value, limit=128):
    """A bounded journal identity: printable, never a control byte."""
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(32 <= ord(char) != 127 for char in value))


class ContractError(ValueError):
    """A request or adapter that does not satisfy the H1 contract."""


def _name(value):
    return isinstance(value, str) and NAME_RE.fullmatch(value) is not None


def _epoch(value, minimum):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


#: The closed set of Git transports an enrolled repository may name. A local
#: path, ``file://``, ``ext::``, an unknown scheme and an option-like value
#: (leading ``-``) are never admitted: the host passes this identity to Git.
REPOSITORY_SCHEMES = ("ssh", "https", "http", "git")
_REPOSITORY_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPOSITORY_IPV6 = re.compile(r"^[0-9A-Fa-f:]*:[0-9A-Fa-f:]*$")


def _repository_host(value):
    """A DNS name or a bracketed IPv6 literal (``urlsplit`` drops brackets)."""
    return (_REPOSITORY_HOST.fullmatch(value) is not None
            or _REPOSITORY_IPV6.fullmatch(value) is not None)


def repository_transport(value):
    """Name the admitted Git transport of a repository identity, or ``None``.

    Only a network transport is admitted: scp-like ``git@host:path`` (SSH) and
    ``ssh://``, ``https://``, ``http://`` or ``git://`` URLs. Userinfo is
    refused except the SSH user ``git`` (HTTP(S) userinfo may carry a token).
    Query strings, fragments, passwords, control bytes, local paths and an
    initial ``-`` (which Git could read as an option) are always refused.
    """
    if (not isinstance(value, str) or not value or len(value) > 1024
            or value.startswith("-")):
        return None
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.password is not None or parsed.query or parsed.fragment:
        return None
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme not in REPOSITORY_SCHEMES:
            return None
        if parsed.username is not None and not (
                scheme == "ssh" and parsed.username == "git"):
            return None
        host = parsed.hostname or ""
        return scheme if _repository_host(host) else None
    # No scheme: only the scp-like SSH form ``git@host:path`` is a transport.
    head, separator, path = value.partition(":")
    if not separator or not path:
        return None
    user, at, host = head.rpartition("@")
    if not at or user != "git" or not _repository_host(host):
        return None
    return "scp"


def repository_identity(value):
    """A bounded enrolled repository identity on one admitted transport."""
    return repository_transport(value) is not None


def _repository(value):
    """The enrollment's repository identity: a bounded reference, not a path."""
    return repository_identity(value)


def validate_request(operation, method):
    """Refuse an effect request missing its operation, lease or epoch identity.

    ``operation`` is the durable journal row the controller is about to act
    on: ``id``, the source lease (``plan.owner`` and ``plan.epoch``) and
    ``controller_epoch``. The adapter never receives a request the controller
    cannot later journal against. A lost-source recovery may have no live
    lease owner; that single case is explicitly allowed.
    """
    if method not in REQUEST_METHODS:
        raise ContractError("unknown handoff method")
    if not isinstance(operation, Mapping):
        raise ContractError("handoff operation request required")
    operation_id = operation.get("id")
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ContractError("operation id required")
    for field in ("agent", "source", "target"):
        if not _name(operation.get(field)):
            raise ContractError("operation %s required" % field)
    plan = operation.get("plan")
    if not isinstance(plan, Mapping):
        raise ContractError("operation plan required")
    if not _repository(plan.get("repository")):
        raise ContractError("operation repository required")
    owner = plan.get("owner")
    lost = plan.get("reason") == "host_loss"
    if not _bounded(owner) and not (lost and owner in (None, "")):
        raise ContractError("source lease owner required")
    if not _epoch(plan.get("epoch"), 0):
        raise ContractError("source lease epoch required")
    if not _epoch(operation.get("controller_epoch"), 1):
        raise ContractError("controller epoch required")
    if not isinstance(operation.get("evidence"), Mapping):
        raise ContractError("operation evidence required")
    if method == "reconcile_effect":
        pending = operation["evidence"].get("pending_effect")
        if not isinstance(pending, Mapping) or not _epoch(pending.get("epoch"), 0):
            raise ContractError("unfinished host effect required")
    return True


def require_methods(adapter, methods=METHODS):
    """Refuse an adapter that does not expose every enrolled method.

    A missing method is a factory admission failure, never a runtime surprise
    or a simulated success. Extra helper methods are left to the factory.
    """
    missing = [name for name in methods if not callable(getattr(adapter, name, None))]
    if missing:
        raise ContractError("missing handoff methods: " + ", ".join(sorted(missing)))
    return adapter


# ---------------------------------------------------------------------------
# H2: the wire contract between the controller-side companion and one host's
# pinned executor. It is deliberately narrower than the controller operation
# contract: a host request names no program, path, unit or command it chooses,
# only the enrolled identity of a preparation/Git effect. The executor journal
# keys receipts and ordering by these exact shapes.
# ---------------------------------------------------------------------------

#: The enrolled SSH endpoint of one other host. The dedicated account and the
#: private key/known-hosts files are enrollment, never request data.
TRANSPORT_KEYS = ("host", "address", "port", "user", "host_key_alias",
                  "known_hosts", "known_hosts_sha256", "identity_file", "seconds")
TRANSPORT_USER = "ameesh-handoff"

#: The preflight's reference grammar: no implicit widening of an account.
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
#: A tool is a program name, never a path the controller could probe.
_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PATH = re.compile(r"^/[A-Za-z0-9/_.~-]+$")
_REASON = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")


def _path(value, *, root):
    """An absolute enrolled file below the fixed private enrollment root."""
    if not isinstance(value, str) or ".." in value.split("/"):
        return False
    if _PATH.fullmatch(value) is None:
        return False
    try:
        Path(value).relative_to(root)
    except ValueError:
        return False
    return True


def validate_transport(value, host, *, root):
    """Refuse an SSH endpoint outside the enrolled names and private files."""
    if not isinstance(value, Mapping) or tuple(sorted(value)) != tuple(sorted(TRANSPORT_KEYS)):
        raise ContractError("transport fields")
    if not _name(host) or value.get("host") != host:
        raise ContractError("transport host")
    if (not isinstance(value.get("address"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["address"]) is None
            or not _name(value.get("host_key_alias"))
            or value.get("user") != TRANSPORT_USER):
        raise ContractError("transport endpoint")
    if (isinstance(value.get("port"), bool) or not isinstance(value.get("port"), int)
            or not 1 <= value["port"] <= 65535
            or isinstance(value.get("seconds"), bool) or not isinstance(value.get("seconds"), int)
            or not 5 <= value["seconds"] <= 360):
        raise ContractError("transport bounds")
    if (not isinstance(value.get("known_hosts_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["known_hosts_sha256"]) is None
            or not _path(value.get("known_hosts"), root=root)
            or not _path(value.get("identity_file"), root=root)):
        raise ContractError("transport files")
    return dict(value)


#: Common fields of every host effect request. The source lease owner and its
#: epoch travel with the controller epoch so a host can refuse a delayed or
#: revoked request even when the operation id and step are unchanged.
_COMMON_REQUEST = ("step", "host", "operation", "action_id", "controller_epoch",
                   "agent", "owner", "lease_epoch")
#: Step-specific request fields; no request carries a command, unit or choice.
EFFECT_FIELDS = {
    "revalidate": ("state",),
    "prepare": ("harness", "account", "tools", "secrets"),
    "stop_source": (),
    "publish": ("repository", "cwd"),
    "transfer": ("repository", "branch", "commit", "project"),
    "session": ("harness", "account", "cwd", "commit"),
}
#: States a read-only revalidation may observe.
HANDOFF_STATES = ("planned", "prepared", "drained", "published", "transferred",
                  "session_ready", "switched", "activated", "held")
INVENTORY_FIELDS = ("step", "host", "tools", "secrets")
#: Common fields of every host receipt.
_COMMON_RECEIPT = ("host", "action_id", "controller_epoch", "ok")
#: Step-specific receipt fields the controller may journal.
EFFECT_RECEIPT_FIELDS = {
    "revalidate": (),
    "prepare": ("release_revision", "credentials_ready", "caps_ready",
                "tooling_ready", "dsh_profile_ready", "executor_enabled"),
    "stop_source": ("unit", "stopped", "quiet", "executor_enabled"),
    "publish": ("repository", "branch", "commit", "remote_commit", "clean"),
    "transfer": ("repository", "branch", "commit", "clean", "cwd"),
    "session": ("mode", "executor_enabled", "oauth_cache_transferred"),
}


def _aliases(value, label, pattern=_ALIAS):
    if not isinstance(value, (list, tuple)):
        raise ContractError(label)
    if any(not isinstance(item, str) or pattern.fullmatch(item) is None
           for item in value):
        raise ContractError(label)
    if len(set(value)) != len(value):
        raise ContractError(label)
    return tuple(sorted(value))


def _tools(value, label):
    return _aliases(value, label, _TOOL)


def _absolute(value, label):
    if (not isinstance(value, str) or ".." in value.split("/")
            or _PATH.fullmatch(value) is None):
        raise ContractError(label)
    return value


def validate_inventory_request(request):
    """A discovery request names one enrolled host and only boolean checks."""
    if (not isinstance(request, Mapping)
            or tuple(sorted(request)) != tuple(sorted(INVENTORY_FIELDS))
            or request.get("step") != INVENTORY):
        raise ContractError("inventory request")
    if not _name(request.get("host")):
        raise ContractError("inventory host")
    return {"step": INVENTORY, "host": request["host"],
            "tools": _tools(request.get("tools"), "inventory tools"),
            "secrets": _aliases(request.get("secrets"), "inventory secrets")}


def validate_effect_request(request):
    """Refuse a host effect request outside the enrolled wire shape."""
    if not isinstance(request, Mapping):
        raise ContractError("effect request")
    step = request.get("step")
    if step not in EFFECT_FIELDS:
        raise ContractError("effect step")
    expected = _COMMON_REQUEST + EFFECT_FIELDS[step]
    if tuple(sorted(request)) != tuple(sorted(expected)):
        raise ContractError("effect fields")
    if (not _name(request.get("host")) or not _name(request.get("agent"))
            or not isinstance(request.get("operation"), str)
            or _OPERATION_ID.fullmatch(request["operation"]) is None
            or request.get("action_id") != request["operation"] + ":" + step
            or not _epoch(request.get("controller_epoch"), 1)
            or not (request.get("owner") == "" or _bounded(request.get("owner")))
            or not _epoch(request.get("lease_epoch"), 0)):
        raise ContractError("effect identity")
    value = {key: request[key] for key in _COMMON_REQUEST}
    if step == "revalidate":
        if request.get("state") not in HANDOFF_STATES:
            raise ContractError("effect state")
        value["state"] = request["state"]
    if step in ("prepare", "session"):
        if (not _name(request.get("harness"))
                or not isinstance(request.get("account"), str)
                or (request["account"] and _ACCOUNT.fullmatch(request["account"]) is None)):
            raise ContractError("effect identity")
        value["harness"] = request["harness"]
        value["account"] = request["account"]
    if step == "prepare":
        value["tools"] = _tools(request.get("tools"), "effect tools")
        value["secrets"] = _aliases(request.get("secrets"), "effect secrets")
    if step in ("publish", "transfer"):
        if not repository_identity(request.get("repository")):
            raise ContractError("effect repository")
        value["repository"] = request["repository"]
    if step == "publish":
        value["cwd"] = _absolute(request.get("cwd"), "effect cwd")
    if step == "transfer":
        branch = request.get("branch")
        if (not isinstance(branch, str) or not 1 <= len(branch) <= 255
                or _BRANCH_FORBIDDEN.search(branch)):
            raise ContractError("effect branch")
        if not _sha(request.get("commit")):
            raise ContractError("effect commit")
        project = request.get("project")
        if not isinstance(project, str) or (project and not _name(project)):
            raise ContractError("effect project")
        value.update(branch=branch, commit=request["commit"], project=project)
    if step == "session":
        value["cwd"] = _absolute(request.get("cwd"), "effect cwd")
        if not _sha(request.get("commit")):
            raise ContractError("effect commit")
        value["commit"] = request["commit"]
    return value


def validate_inventory_receipt(receipt, request):
    """A receipt carries booleans, never a credential, path or diagnostic."""
    keys = ("host", "checked_ts", "connected", "tools", "secrets")
    if (not isinstance(receipt, Mapping) or tuple(sorted(receipt)) != tuple(sorted(keys))
            or receipt.get("host") != request["host"]
            or not isinstance(receipt.get("checked_ts"), (int, float))
            or isinstance(receipt.get("checked_ts"), bool)
            or receipt.get("connected") is not True):
        raise ContractError("inventory receipt")
    for label in ("tools", "secrets"):
        value = receipt.get(label)
        if (not isinstance(value, Mapping)
                or set(value) != set(request[label])
                or any(type(flag) is not bool for flag in value.values())):
            raise ContractError("inventory receipt")
    return dict(receipt, tools=dict(receipt["tools"]), secrets=dict(receipt["secrets"]))


def validate_effect_receipt(receipt, request):
    """Refuse a receipt that does not answer this exact request.

    ``ok: false`` is a definitive refusal with one stable category. A receipt
    that claims completion must carry every allowlisted proof field; the
    adapter still checks their values against the operation.
    """
    if not isinstance(receipt, Mapping) or type(receipt.get("ok")) is not bool:
        raise ContractError("effect receipt")
    step = request["step"]
    if receipt["ok"]:
        expected = _COMMON_RECEIPT + EFFECT_RECEIPT_FIELDS[step]
    else:
        expected = _COMMON_RECEIPT + ("reason",)
    if tuple(sorted(receipt)) != tuple(sorted(expected)):
        raise ContractError("effect receipt fields")
    if (receipt.get("host") != request["host"]
            or receipt.get("action_id") != request["action_id"]
            or receipt.get("controller_epoch") != request["controller_epoch"]):
        raise ContractError("effect receipt identity")
    if not receipt["ok"]:
        if not isinstance(receipt.get("reason"), str) or _REASON.fullmatch(receipt["reason"]) is None:
            raise ContractError("effect receipt reason")
        return dict(receipt)
    value = dict(receipt)
    if step == "prepare":
        if (not _sha(receipt.get("release_revision"))
                or any(type(receipt.get(key)) is not bool for key in
                       ("credentials_ready", "caps_ready", "tooling_ready", "dsh_profile_ready"))
                or receipt.get("executor_enabled") is not False):
            raise ContractError("prepare receipt")
    elif step == "stop_source":
        if (not isinstance(receipt.get("unit"), str)
                or not re.fullmatch(r"ameesh-runner(?:-agent)?@[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.service",
                                    receipt["unit"])
                or any(receipt.get(key) is not True for key in ("stopped", "quiet"))
                or receipt.get("executor_enabled") is not False):
            raise ContractError("stop receipt")
    elif step == "publish":
        if (not _repository(receipt.get("repository"))
                or not isinstance(receipt.get("branch"), str) or not receipt["branch"]
                or _BRANCH_FORBIDDEN.search(receipt["branch"])
                or not _sha(receipt.get("commit")) or not _sha(receipt.get("remote_commit"))
                or receipt.get("clean") is not True):
            raise ContractError("publish receipt")
    elif step == "transfer":
        if (not _repository(receipt.get("repository"))
                or not isinstance(receipt.get("branch"), str) or not receipt["branch"]
                or _BRANCH_FORBIDDEN.search(receipt["branch"])
                or not _sha(receipt.get("commit")) or receipt.get("clean") is not True):
            raise ContractError("transfer receipt")
        value["cwd"] = _absolute(receipt.get("cwd"), "transfer receipt cwd")
    elif step == "session":
        if (receipt.get("mode") not in ("fresh", "brief")
                or receipt.get("executor_enabled") is not False
                or receipt.get("oauth_cache_transferred") is not False):
            raise ContractError("session receipt")
    return value
