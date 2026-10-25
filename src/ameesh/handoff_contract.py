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
import re

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


def _repository(value):
    """The enrollment's repository identity: a bounded reference, not a path."""
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


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
