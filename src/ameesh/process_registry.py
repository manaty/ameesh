# SPDX-License-Identifier: AGPL-3.0-only
"""Native Process workflow registry: revision/stage and idempotent receipts.

slice B of #1915 (lot #1922). Consumer contract: manaty/ameesh#1379
(body and comment 6207021753). This module is the **only** native write
of a workflow transition; it does not compile the Process document (that is
slice A, `process_workflow`, whose `CompiledWorkflow` artifact satisfies the
`WorkflowSpec` protocol below) and it exposes no public route.

What the registry guarantees, in ONE transaction:

1. **The lot is locked** (`FOR UPDATE`) before any check: two
   commands on the same lot never double up.
2. **Authority is checked inside the transaction**: the actor exists in the
   registry and is not stopped; for a `runner` source, the lease is alive,
   held by that runner at that epoch (registry row locked `FOR SHARE`,
   hence serialized with a lease takeover). A revoked actor, an unknown
   source or a stale epoch are refused **before** any receipt read:
   they never recover a result that was authorized in the past.
3. **The revision is pinned**: the lot's `workflow_ref`/`workflow_version`
   must be those of the compiled artifact; no automatic adoption of a
   new revision. Only `transfer_revision`, explicit and journaled,
   changes the admitted revision.
4. **The optimistic version** is the id of the lot journal's last line
   (`work_item_events`, 0 if empty): any concurrent journal write —
   a registry command or a legacy `work.move` transition — makes
   a command that expected the old version fail (`version-conflict`).
5. **The command key is unique per lot** (partial index): a replay of the
   same intent finds the accepted receipt; the same key with a different
   canonical payload is an `intent-conflict`.
6. **No client boolean replaces a guard**: an advance requires a
   **server-installed** verifier for the edge guard, and
   the entry as well as the exit of a hold require the one installed under the
   reserved name `hold` (slice C: `HOLD_VERIFIER`, the `verify_hold` callable of
   `process_verifiers`); without an installation, `verifier-uninstalled`. No
   bypass: a hold is not wiped by an advance. The
   client provides structured facts (`evidence`), never a decision.
7. **Nothing by halves**: the lot write, the stage/version and the receipt are in
   the same transaction; an error after the start leaves neither receipt nor
   transition (trigger test, `rollback without a half-event`).
8. **Holds survive**: `blocked`/`waiting_human` keep the stage;
   a hold is not crossed — any advance from a held lot is
   refused (`held`), only an explicit `release` gets out of it, checked
   server-side like the entry; `waiting_human` requires a pending native
   decision request (L233, the same query as `work.move`) and its release
   waits for the decision to be answered (`decision-pending`); a terminal core
   never reopens, including through workflow adoption.
   Lots **without a workflow** do not go through here: `workflow-absent`,
   their behavior (`work.move`) is unchanged.
9. **Released cores carry their side effects**: an advance to
   `merged`, `promoted` or `closed` gives the migration reservation back (#956),
   writes its journal line, and wakes the dependants (L385) — the first
   pass only — under the graph lock taken before the lot, like
   `work.close`/`close_merged`. The `closed` core requires the closure in
   the evidence (`close_reason` among `abandoned`, `superseded` — with
   `superseded_by` —, `delivered` — with the head `sha`), sets
   `close_reason`/`superseded_by` and the `closed`/`delivered` milestones: what
   `work.close` now refuses to a pinned lot (N8) is done here, in the
   same transaction. A delivered review-after-merge lot counts its reviews
   first, like `work.close --delivered` (`review_lot.guard_delivery`, #2374,
   #2385): a missing review is `review-undelivered`.

Protocol `WorkflowSpec` (structural, the slice A artifact satisfies it):

    workflow_ref: str            pinned canonical reference (commit/path)
    workflow_version: int        Process revision version (>= 0)
    initial: str                 declared starting status
    statuses() -> Sequence[str]
    core_of(status) -> str       native core (`intake`…`closed`)
    transitions_from(status) -> Sequence[object with .target and .guard]

Artifact trust boundary (N5): `spec` is supplied by the caller — the server
component that compiled the pinned revision. The registry checks that the
artifact `workflow_ref`/`workflow_version` match the command and the lot
(`spec-mismatch`), never its content: the ref names the commit and slice A
publishes no digest, so the compiled artifact is trusted for that ref. The
caller is the installed server component, never a client; if a later revision
of the artifact publishes a digest, the seam must bind it to the command.

Identity and epoch: `Authority(actor, source, epoch, runner_id)` takes up
the bound identity of `identity.Binding` (`runner` with runner_id + lease epoch;
`explicit`/`session` with epoch 0). The `Authority` is built by the
**installed server component** (identity resolution), never by a client:
the registry natively rechecks the agent registry row (existence,
stopped state, live lease held by that runner at that epoch) and refuses before
returning a receipt.

Two distinct versions, never confused:

* `workflow_version` (lot and receipt): the pinned **Process revision**, changed
  only by `transfer_revision` (explicit, journaled, without regression);
* `expected_version` / `Receipt.version`: the **journal's optimistic version**
  of the lot — the id of its last `work_item_events` line (0 if empty), all
  writes included (registry commands as well as legacy `work.move`
  transitions).

A replay (same key, same canonical intent) returns the accepted receipt even after
a revision transfer: it is the acquired result of that key. The same key
with another intent, however, is an `intent-conflict`.

Persisted proof (`work_item_events.evidence`, schema `process-receipt/1`) ::

    {"schema": "process-receipt/1",
     "intent": {"sha256": "…"},                  # JCS canonical payload
     "command": {"action", "guard", "from", "to", "hold",
                 "expected_version", "workflow_ref", "workflow_version"},
     "authority": {"actor", "source", "epoch", "runner_id"},
     "verification": {"required", "verifier", "ok", "reason", "facts"},
     "facts": {…}}                               # client structured facts

Stable refusal codes (`RegistryRefusal.code`): `command-invalid`,
`evidence-invalid`, `authority-invalid`, `authority-source`,
`authority-runner`, `authority-epoch`, `authority-unknown-actor`,
`authority-revoked`, `authority-lease`, `authority-expired`, `spec-invalid`,
`spec-mismatch`, `lot-unknown`, `workflow-absent`, `ref-mismatch`,
`revision-mismatch`, `version-conflict`, `intent-conflict`, `receipt-corrupt`,
`stage-mismatch`, `stage-undeclared`, `edge-undeclared`, `terminal-reopened`,
`held`, `hold-invalid`, `hold-redundant`, `hold-no-decision`, `decision-pending`,
`hold-core-unproven`, `not-held`, `close-reason`, `superseded-unknown`,
`delivered-sha`, `review-undelivered`,
`repair-exhausted`, `revision-regression`, `verifier-uninstalled`,
`verifier-invalid`, `verifier-error`, `verifier-refused`, `verifier-name`.

The C verifiers have no SQL: `read_sources(context)` returns, in the
transaction and under lock, the collections actually collected
(`milestones`, `receipts`, `decisions`, `deliveries`), never an absent
collection (fail-closed).

The registry SQL is here, not in storage: it is an installed server
component, not a view.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import jcs
from .db import Db
from .work import MAX_QA_LOOPS

#: Schema of a receipt's structured proof (SQL constraint of 0452).
RECEIPT_SCHEMA = "process-receipt/1"
#: Domain of the intent digest (domain separation).
INTENT_DOMAIN = b"ameesh-process-command/1\x00"
#: Reserved name of the revision transfer verifier (not a Process guard).
TRANSFER_VERIFIER = "revision_transfer"
#: Reserved name of the hold entry AND exit verifier (not a Process
#: guard): `hold` as well as `release` go through it, with no bypass.
#: It is the name published by slice C (`process_verifiers.HOLD_VERIFIER`);
#: its `verify_hold` callable dispatches on the command's `action`.
HOLD_VERIFIER = "hold"

ACTIONS = ("advance", "hold", "release", "transfer")
HOLDS = (lifecycle.BLOCKED, lifecycle.WAITING_HUMAN)
#: Native cores a stage can project (same as slice A).
CORE_STATES = (
    lifecycle.REQUESTED,
    lifecycle.IN_PROGRESS,
    lifecycle.QA,
    lifecycle.VALIDATED,
    "merged",
    "promoted",
    lifecycle.CLOSED,
)
#: Terminal cores: never reopened by a command.
TERMINAL_CORES = frozenset(("promoted", lifecycle.CLOSED))
#: Cores that release the lot for its dependants and give back its migration
#: reservation (#956): the same as `work_dependencies._DONE`. An advance entering
#: one carries the side effects of the legacy closure, which storage
#: now refuses to pinned lots (#1922/N8): `close_reason`/
#: `superseded_by`, `closed`/`delivered` milestones, reservation given back, wake
#: of the dependants (L385).
RELEASED_CORES = frozenset(("merged", "promoted", "delivered", lifecycle.CLOSED))
#: Closures admitted for a `closed` core (`close_reason` constraint of
#: migration 0430): the command evidence says which one, like the three
#: doors of `work.close` (abandonment, replacement, delivery outside the target).
CLOSE_REASONS = ("abandoned", "superseded", "delivered")
#: Admitted identity sources (`identity.Binding.source`).
SOURCES = ("runner", "explicit", "session")
#: qa → build repairs counted by `work_items.loops` (same bound as work).
REPAIR_CORES = (lifecycle.QA, lifecycle.IN_PROGRESS)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
#: Reviewed head of a delivery outside the target (#988): the same shape as
#: `work._SHA_RE` (7 to 64 hex digits), not the intent digest (64).
_HEAD_RE = re.compile(r"^[0-9a-f]{7,64}$")
_VERIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RegistryRefusal(RuntimeError):
    """Registry refusal: a stable code, a message, an optional detail.

    A refusal writes nothing: it is raised in the transaction, which is rolled back.
    """

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail


# --------------------------------------------------------------------------
# published structures
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Authority:
    """Bound identity that commands: actor, source, lease epoch."""

    actor: str
    source: str = "runner"
    epoch: int = 0
    runner_id: str = ""


@dataclass(frozen=True)
class Command:
    """A native command: an intent, a key, an expected version.

    `evidence` carries the client's **structured** facts (never a decision:
    the installed verifier decides). `from_stage`/`to_stage`/`guard` are the
    explicit inputs of an advance; `hold` the explicit input of a hold.
    """

    work_item_id: int
    command_id: str
    expected_version: int
    authority: Authority
    workflow_ref: str = ""
    workflow_version: int = 0
    action: str = "advance"
    from_stage: str = ""
    to_stage: str = ""
    guard: str = ""
    hold: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verification:
    """Result of an installed verifier: not a client boolean."""

    ok: bool
    reason: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationContext:
    """What an installed verifier sees, in the transaction and under lock."""

    db: Any
    work_item: Mapping[str, Any]
    command: Command
    spec: Any
    from_stage: str
    to_stage: str


@dataclass(frozen=True)
class Receipt:
    """The receipt of an accepted (or replayed) command."""

    accepted: bool
    replayed: bool
    work_item_id: int
    command_id: str
    action: str
    event_id: int
    version: int
    state: str
    workflow_ref: str
    workflow_stage: str | None
    workflow_version: int
    guard: str
    intent_sha256: str

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "replayed": self.replayed,
            "work_item_id": self.work_item_id,
            "command_id": self.command_id,
            "action": self.action,
            "event_id": self.event_id,
            "version": self.version,
            "state": self.state,
            "workflow_ref": self.workflow_ref,
            "workflow_stage": self.workflow_stage,
            "workflow_version": self.workflow_version,
            "guard": self.guard,
            "intent_sha256": self.intent_sha256,
        }


@dataclass(frozen=True)
class RegistryState:
    """Read of a lot's registry: state, revision, journal version."""

    work_item_id: int
    state: str
    workflow_ref: str | None
    workflow_stage: str | None
    workflow_version: int
    version: int
    receipts: int


Verifier = Callable[[VerificationContext], Verification]

#: Server-installed verifiers (slice C): Process guard → callable.
#: Empty by default: no advance is possible without an explicit installation.
INSTALLED_VERIFIERS: dict[str, Verifier] = {}


def install_verifier(name: str, verifier: Verifier, *,
                     replace: bool = False) -> None:
    """Installs a server verifier for a guard (slice C).

    Refuses an out-of-shape name, a non-callable callable, and the silent
    overwrite of an installation by another object (`replace=True` for an
    explicit replacement). Idempotent for the same object.
    """
    if not isinstance(name, str) or not _VERIFIER_RE.match(name):
        raise RegistryRefusal("verifier-name", "invalid verifier name: %r" % (name,))
    if not callable(verifier):
        raise RegistryRefusal("verifier-invalid", "%s is not callable" % name)
    current = INSTALLED_VERIFIERS.get(name)
    if current is not None and current is not verifier and not replace:
        raise RegistryRefusal("verifier-name",
                              "a verifier is already installed for %s" % name)
    INSTALLED_VERIFIERS[name] = verifier


def installed_verifiers() -> dict[str, Verifier]:
    """Copy of the installed registry (read-only for the caller)."""
    return dict(INSTALLED_VERIFIERS)


# --------------------------------------------------------------------------
# validation outside the transaction: shape, canonical intent
# --------------------------------------------------------------------------

def canonical_command_id(value: Any) -> str:
    """Lowercase canonical UUID, or `command-invalid`."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise RegistryRefusal("command-invalid", "command_id is not a UUID: %r" % (value,))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_authority(authority: Any) -> None:
    if not isinstance(authority, Authority):
        raise RegistryRefusal("authority-invalid", "Authority expected")
    if not isinstance(authority.actor, str) or not _NAME_RE.match(authority.actor):
        raise RegistryRefusal("authority-invalid", "invalid actor: %r" % (authority.actor,))
    if authority.source not in SOURCES:
        raise RegistryRefusal("authority-source",
                              "source %r not in %s" % (authority.source, ", ".join(SOURCES)))
    if not _is_int(authority.epoch) or authority.epoch < 0:
        raise RegistryRefusal("authority-epoch", "invalid epoch: %r" % (authority.epoch,))
    if authority.source == "runner":
        if not isinstance(authority.runner_id, str) or not authority.runner_id:
            raise RegistryRefusal("authority-runner", "runner source without runner_id")
    else:
        if authority.epoch != 0:
            raise RegistryRefusal("authority-epoch",
                                  "source %s without a lease: expected epoch 0, got %s"
                                  % (authority.source, authority.epoch))
        if authority.runner_id:
            raise RegistryRefusal("authority-runner",
                                  "source %s with a runner_id" % authority.source)


def _validate_command(command: Any) -> None:
    if not isinstance(command, Command):
        raise RegistryRefusal("command-invalid", "Command expected")
    if not _is_int(command.work_item_id) or command.work_item_id <= 0:
        raise RegistryRefusal("command-invalid", "bad work_item_id: %r" % (command.work_item_id,))
    canonical_command_id(command.command_id)
    if not _is_int(command.expected_version) or command.expected_version < 0:
        raise RegistryRefusal("command-invalid",
                              "invalid expected_version: %r" % (command.expected_version,))
    _validate_authority(command.authority)
    if command.action not in ACTIONS:
        raise RegistryRefusal("command-invalid",
                              "action %r not in %s" % (command.action, ", ".join(ACTIONS)))
    if not isinstance(command.workflow_ref, str) or not command.workflow_ref.strip():
        raise RegistryRefusal("command-invalid", "workflow_ref required")
    if not _is_int(command.workflow_version) or command.workflow_version < 0:
        raise RegistryRefusal("command-invalid",
                              "invalid workflow_version: %r" % (command.workflow_version,))
    if command.evidence is None:
        raise RegistryRefusal("evidence-invalid", "missing evidence")
    if not isinstance(command.evidence, Mapping):
        raise RegistryRefusal("evidence-invalid", "evidence must be a JSON object")
    if command.action == "advance":
        for field_name in ("from_stage", "to_stage", "guard"):
            value = getattr(command, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RegistryRefusal("command-invalid", "%s required to advance" % field_name)
    elif command.action == "hold":
        if command.hold not in HOLDS:
            raise RegistryRefusal("hold-invalid",
                                  "hold %r not in %s" % (command.hold, ", ".join(HOLDS)))
    elif command.action == "release":
        if command.hold:
            raise RegistryRefusal("hold-invalid", "a release carries no hold")


def command_intent(command: Command) -> dict:
    """Canonical payload of the intent: the explicit inputs, nothing else.

    Derived values (current stage, resulting state) are not in it: a
    replay of the same intent recomputes them and must find the same receipt.
    """
    intent: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "action": command.action,
        "work_item_id": int(command.work_item_id),
        "command_id": canonical_command_id(command.command_id),
        "expected_version": int(command.expected_version),
        "workflow_ref": command.workflow_ref,
        "workflow_version": int(command.workflow_version),
        "authority": {
            "actor": command.authority.actor,
            "source": command.authority.source,
            "epoch": int(command.authority.epoch),
            "runner_id": command.authority.runner_id,
        },
        "facts": dict(command.evidence),
    }
    if command.action == "advance":
        intent["from"] = command.from_stage
        intent["to"] = command.to_stage
        intent["guard"] = command.guard
    elif command.action == "hold":
        intent["hold"] = command.hold
    return intent


def intent_sha256(command: Command) -> str:
    """JCS SHA-256 of a command's canonical intent.

    Refuses (`evidence-invalid`) a payload that JCS does not serialize: the same
    intent must produce exactly the same bytes for everyone.
    """
    try:
        canon = jcs.dumps(command_intent(command)).encode("utf-8")
    except jcs.JcsError as exc:
        raise RegistryRefusal("evidence-invalid", "non-canonical payload: %s" % exc) from exc
    return hashlib.sha256(INTENT_DOMAIN + canon).hexdigest()


# --------------------------------------------------------------------------
# compiled artifact protocol (slice A) — structural, without import
# --------------------------------------------------------------------------

def _spec_ref(spec: Any) -> str:
    return str(getattr(spec, "workflow_ref", "") or "")


def _spec_version(spec: Any) -> int:
    value = getattr(spec, "workflow_version", None)
    if not _is_int(value) or value < 0:
        raise RegistryRefusal("spec-invalid", "invalid artifact workflow_version "
                              "(got %r)" % (value,))
    return value


def _validate_spec(spec: Any) -> None:
    if spec is None:
        raise RegistryRefusal("spec-invalid", "compiled artifact required")
    if not _spec_ref(spec):
        raise RegistryRefusal("spec-invalid", "artifact workflow_ref missing")
    _spec_version(spec)
    if not _spec_statuses(spec):
        raise RegistryRefusal("spec-invalid", "artifact without a stage")
    if not isinstance(getattr(spec, "initial", ""), str) or not getattr(spec, "initial", ""):
        raise RegistryRefusal("spec-invalid", "artifact without an initial stage")


def _spec_statuses(spec: Any) -> tuple[str, ...]:
    statuses = getattr(spec, "statuses", None)
    if not callable(statuses):
        raise RegistryRefusal("spec-invalid", "artifact without statuses()")
    try:
        return tuple(str(s) for s in statuses())
    except RegistryRefusal:
        raise
    except Exception as exc:
        raise RegistryRefusal("spec-invalid", "unreadable statuses(): %s" % exc) from exc


def _spec_core(spec: Any, status: str) -> str | None:
    core = getattr(spec, "core_of", None)
    if not callable(core):
        raise RegistryRefusal("spec-invalid", "artifact without core_of()")
    try:
        value = str(core(status))
    except Exception:
        return None
    return value if value in CORE_STATES else None


def _spec_edges(spec: Any, source: str) -> tuple[tuple[str, str], ...]:
    transitions = getattr(spec, "transitions_from", None)
    if not callable(transitions):
        raise RegistryRefusal("spec-invalid", "artifact without transitions_from()")
    try:
        raw = transitions(source)
    except Exception as exc:
        raise RegistryRefusal("spec-invalid", "unreadable transitions_from(): %s" % exc) from exc
    out: list[tuple[str, str]] = []
    for edge in raw:
        target = getattr(edge, "target", None)
        guard = getattr(edge, "guard", None)
        if target is None and isinstance(edge, Sequence) and len(edge) == 2:
            target, guard = edge
        out.append((str(target), str(guard)))
    return tuple(out)


# --------------------------------------------------------------------------
# the transaction
# --------------------------------------------------------------------------

def _registry(verifiers: Mapping[str, Verifier] | None) -> dict[str, Verifier]:
    if verifiers is None:
        return dict(INSTALLED_VERIFIERS)
    if not isinstance(verifiers, Mapping):
        raise RegistryRefusal("verifier-invalid", "verifiers must be a guard → callable table")
    return dict(verifiers)


def _lock_lot(tx: Any, work_item_id: int) -> Mapping[str, Any]:
    """Locks the lot and returns its row, or refuses `lot-unknown`.

    The row is the lot snapshot for the verifier (slice C): it
    carries what C reads (`title`, `closed_at`, `reviewer`, `type`, `loops`…).
    `reviewer` comes from `to_jsonb`, like the other #705 reads: a database
    from before 0421 returns `''` instead of breaking.
    """
    rows = tx.query(
        "SELECT w.id, w.state, w.assignee, w.type, w.app, w.workstream, w.loops,"
        "       w.title, w.closed_at, w.migration, w.migration_repo,"
        "       to_regclass('work_migration_reservations') IS NOT NULL AS migration_slots,"
        "       coalesce(to_jsonb(w)->>'reviewer', '') AS reviewer,"
        "       w.workflow_ref, w.workflow_stage, w.workflow_version,"
        "       to_jsonb(w)->>'legacy_state' AS legacy_state"
        "  FROM work_items w WHERE w.id = %s FOR UPDATE",
        (int(work_item_id),))
    if not rows:
        raise RegistryRefusal("lot-unknown", "lot %s not found" % work_item_id)
    row = dict(rows[0])
    row["state"] = lifecycle.phase(row)
    return row


def _check_authority(tx: Any, authority: Authority) -> dict:
    """Checks the bound identity INSIDE the transaction, registry row locked.

    `FOR SHARE` serializes with a lease takeover or release: either
    the command goes through before the revocation, or it sees the stale epoch
    and refuses — never a result authorized in the past.
    """
    rows = tx.query(
        "SELECT name, status, lease_owner, lease_epoch,"
        "       (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL"
        "        AND lease_expires_at > clock_timestamp()) AS live"
        "  FROM agent_registry WHERE name = %s FOR SHARE",
        (authority.actor,))
    if not rows:
        raise RegistryRefusal("authority-unknown-actor",
                              "actor %s absent from the registry" % authority.actor)
    row = rows[0]
    if row.get("status") in ("stopped", "dead"):
        raise RegistryRefusal("authority-revoked",
                              "actor %s out of service (%s)"
                              % (authority.actor, row.get("status")))
    if authority.source == "runner":
        owner = row.get("lease_owner") or ""
        if owner != authority.runner_id:
            raise RegistryRefusal(
                "authority-lease",
                "lease of %s held by %s, not by %s"
                % (authority.actor, owner or "nobody", authority.runner_id))
        if int(row.get("lease_epoch") or 0) != authority.epoch:
            raise RegistryRefusal(
                "authority-epoch",
                "epoch %s in the registry, %s in the command"
                % (row.get("lease_epoch"), authority.epoch))
        if not row.get("live"):
            raise RegistryRefusal("authority-expired", "lease of %s expired" % authority.actor)
    return {
        "actor": authority.actor,
        "source": authority.source,
        "epoch": int(authority.epoch),
        "runner_id": authority.runner_id,
    }


def _journal_version(tx: Any, work_item_id: int) -> int:
    rows = tx.query(
        "SELECT coalesce(max(id), 0) AS version FROM work_item_events"
        " WHERE work_item_id = %s", (int(work_item_id),))
    return int(rows[0]["version"])


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _stored_intent(evidence: Any) -> str:
    data = _as_json(evidence)
    if not isinstance(data, Mapping):
        return ""
    intent = data.get("intent")
    if not isinstance(intent, Mapping):
        return ""
    value = str(intent.get("sha256") or "")
    return value if _SHA_RE.match(value) else ""


def _replay(tx: Any, command: Command, intent: str) -> Receipt | None:
    """The receipt of this key, if any: a replay if the intent is identical.

    An unreadable receipt or one with another intent is a refusal, never a
    guessed result. Authority has already been checked: a replay by a revoked
    actor does not reach here.
    """
    rows = tx.query(
        "SELECT id, state, workflow_stage, workflow_version, evidence"
        "  FROM work_item_events WHERE work_item_id = %s AND command_id = %s::uuid",
        (int(command.work_item_id), canonical_command_id(command.command_id)))
    if not rows:
        return None
    row = rows[0]
    stored = _stored_intent(row["evidence"])
    if not stored:
        raise RegistryRefusal("receipt-corrupt",
                              "receipt %s of lot %s without a canonical intent"
                              % (command.command_id, command.work_item_id))
    if stored != intent:
        raise RegistryRefusal("intent-conflict",
                              "key %s of lot %s carries another intent"
                              % (command.command_id, command.work_item_id))
    return Receipt(
        accepted=True, replayed=True,
        work_item_id=int(command.work_item_id),
        command_id=canonical_command_id(command.command_id),
        action=command.action,
        event_id=int(row["id"]), version=int(row["id"]),
        state=str(row["state"]),
        workflow_ref=command.workflow_ref,
        workflow_stage=row["workflow_stage"],
        workflow_version=int(row["workflow_version"] or 0),
        guard=command.guard,
        intent_sha256=stored,
    )


def _check_revision(row: Mapping[str, Any], command: Command) -> None:
    """The command targets exactly the lot's admitted revision."""
    if row.get("workflow_ref") is None:
        raise RegistryRefusal("workflow-absent",
                              "lot %s without a workflow: behavior unchanged"
                              % command.work_item_id)
    if str(row["workflow_ref"]) != command.workflow_ref:
        raise RegistryRefusal("ref-mismatch",
                              "lot %s pinned on %r, command on %r"
                              % (command.work_item_id, row["workflow_ref"],
                                 command.workflow_ref))
    if int(row.get("workflow_version") or 0) != command.workflow_version:
        raise RegistryRefusal(
            "revision-mismatch",
            "lot %s at revision %s, command at revision %s (explicit transfer required)"
            % (command.work_item_id, row.get("workflow_version"),
               command.workflow_version))


def _check_spec(spec: Any, command: Command) -> None:
    if _spec_ref(spec) != command.workflow_ref or _spec_version(spec) != command.workflow_version:
        raise RegistryRefusal(
            "spec-mismatch",
            "artifact %s@%s, command %s@%s"
            % (_spec_ref(spec), _spec_version(spec), command.workflow_ref,
               command.workflow_version))


def _check_version(tx: Any, command: Command) -> int:
    version = _journal_version(tx, command.work_item_id)
    if version != command.expected_version:
        raise RegistryRefusal(
            "version-conflict",
            "lot %s at version %s, command on %s"
            % (command.work_item_id, version, command.expected_version))
    return version


@dataclass(frozen=True)
class _Plan:
    state: str
    stage: str
    loops: int
    note: str
    close_reason: str = ""
    superseded_by: int | None = None
    delivered_sha: str = ""


def _close_facts(tx: Any, command: Command) -> tuple[str, int | None, str]:
    """The closure carried by the command evidence, checked against legacy.

    Entering the terminal core `closed` means the lot was abandoned,
    superseded or delivered outside the target (the three doors of
    `work.close`, constraint `close_reason` of 0430). The registry never
    guesses: a missing or unknown reason is refused, `superseded_by` must name
    an existing lot (not itself), and a delivery carries the reviewed head
    (`sha`, 7 to 64 hex) that the `delivered` milestone persists.
    """
    evidence = command.evidence if isinstance(command.evidence, Mapping) else {}
    reason = str(evidence.get("close_reason") or "").strip()
    if reason not in CLOSE_REASONS:
        raise RegistryRefusal(
            "close-reason",
            "an advance to closed requires close_reason in evidence (%s), got %r"
            % (", ".join(CLOSE_REASONS), reason))
    superseded_by: int | None = None
    sha = ""
    if reason == "superseded":
        value = evidence.get("superseded_by")
        if not _is_int(value) or int(value) <= 0:
            raise RegistryRefusal(
                "superseded-unknown",
                "a superseded closure carries a positive superseded_by, got %r" % (value,))
        if int(value) == int(command.work_item_id):
            raise RegistryRefusal("superseded-unknown", "a lot cannot supersede itself")
        if not tx.query("SELECT 1 FROM work_items WHERE id = %s", (int(value),)):
            raise RegistryRefusal("superseded-unknown",
                                  "superseding lot %s not found" % value)
        superseded_by = int(value)
    elif reason == "delivered":
        sha = str(evidence.get("sha") or "").strip().lower()
        if not _HEAD_RE.match(sha):
            raise RegistryRefusal(
                "delivered-sha",
                "a delivered closure carries the reviewed head (sha, 7 to 64 hex), got %r"
                % (evidence.get("sha"),))
    return reason, superseded_by, sha


def _guard_review_delivery(tx: Any, command: Command) -> None:
    """#2385: a delivered closure of a review-after-merge lot counts its
    reviews, like `work.close --delivered` (#2374).

    `review_lot.guard_delivery` reads the structured mark from the source
    lot's journal and counts its `verdict` milestones at the merged sha; run
    here, in the transaction and under the lot lock, a lot pinned on a
    workflow is not closed without its reviews either. Any other lot passes
    through unchanged. A missing review is a refusal: nothing written.
    """
    from . import review_lot, work
    item = work.get(tx, command.work_item_id)
    if item is None:
        raise RegistryRefusal("lot-unknown", "lot %s not found" % command.work_item_id)
    try:
        review_lot.guard_delivery(tx, item)
    except work.WorkError as exc:
        raise RegistryRefusal("review-undelivered", str(exc)) from exc


def _close_label(reason: str, superseded_by: int | None, sha: str) -> str:
    """The closure label of the journal and milestone note (legacy wording)."""
    if reason == "superseded":
        return "superseded by #%s" % superseded_by
    if reason == "delivered":
        return "delivered (head %s)" % sha[:12]
    return "abandoned"


def _plan_advance(tx: Any, row: Mapping[str, Any], command: Command, spec: Any) -> _Plan:
    statuses = _spec_statuses(spec)
    if command.from_stage not in statuses or command.to_stage not in statuses:
        raise RegistryRefusal("edge-undeclared",
                              "undeclared stage: %s → %s"
                              % (command.from_stage, command.to_stage))
    current = row.get("workflow_stage")
    if not current:
        raise RegistryRefusal("stage-mismatch", "lot %s: no current stage" % command.work_item_id)
    if str(current) != command.from_stage:
        raise RegistryRefusal("stage-mismatch",
                              "lot %s at stage %s, command from %s"
                              % (command.work_item_id, current, command.from_stage))
    if row["state"] in TERMINAL_CORES:
        raise RegistryRefusal("terminal-reopened",
                              "lot %s in %s: a terminal does not reopen"
                              % (command.work_item_id, row["state"]))
    if row["state"] in HOLDS:
        raise RegistryRefusal(
            "held",
            "lot %s in %s: an explicit release is required"
            % (command.work_item_id, row["state"]))
    if (command.to_stage, command.guard) not in _spec_edges(spec, command.from_stage):
        raise RegistryRefusal("edge-undeclared",
                              "edge %s → %s (%s) absent from the artifact"
                              % (command.from_stage, command.to_stage, command.guard))
    core_from = _spec_core(spec, command.from_stage)
    core_to = _spec_core(spec, command.to_stage)
    if core_from is None or core_to is None:
        raise RegistryRefusal("spec-invalid", "unknown core for a declared stage")
    loops = 1 if (core_from, core_to) == REPAIR_CORES else 0
    if loops and int(row.get("loops") or 0) >= MAX_QA_LOOPS:
        raise RegistryRefusal(
            "repair-exhausted",
            "qa → build loop exhausted (%s): block the lot or request a decision"
            % MAX_QA_LOOPS)
    note = "%s → %s (%s)" % (command.from_stage, command.to_stage, command.guard)
    close_reason, superseded_by, delivered_sha = "", None, ""
    if core_to == lifecycle.CLOSED:
        close_reason, superseded_by, delivered_sha = _close_facts(tx, command)
        if close_reason == "delivered":
            _guard_review_delivery(tx, command)
        note = "%s — %s" % (note, _close_label(close_reason, superseded_by, delivered_sha))
    return _Plan(state=core_to, stage=command.to_stage, loops=loops, note=note,
                 close_reason=close_reason, superseded_by=superseded_by,
                 delivered_sha=delivered_sha)


def _lock_decisions(tx: Any, work_item_id: int) -> None:
    """Lock the open decision requests of the lot, AFTER the lot row (N12).

    Lock order: the lot first, then the requests — the order of the single
    L233 statement (`work.move(held=True)` locks the lot row, then the open
    requests in its `EXISTS … FOR UPDATE`). The reverse order (requests then
    lot) could deadlock with that statement on the same request; here the
    command waits on the lot holding nothing. The lock keeps an open request
    closed-proof until commit: an answer cannot slip between the check and the
    hold write, and `_pending_decision`, read after both locks, sees every
    request committed while the lot lock was awaited.

    An INSERT committed after that read is not blocked by a row lock
    (PostgreSQL has no gap lock): the ask path must take the lot lock before
    inserting its request for a full serialization — noted for the C/D
    consumer, see the design document.
    """
    tx.query(
        "SELECT 1 FROM agent_mailbox"
        " WHERE delivered_at IS NULL AND kind = 'request'"
        "   AND (payload -> 'decision') IS NOT NULL"
        "   AND btrim(work_item_id) = %s FOR UPDATE",
        (str(int(work_item_id)),))


def _pending_decision(tx: Any, work_item_id: int) -> bool:
    """L233: an open native decision request holds the lot.

    Read after both locks (lot then requests, `_lock_decisions`): every
    request committed while the lot lock was awaited is visible here.
    """
    rows = tx.query(
        "SELECT 1 FROM agent_mailbox r"
        " WHERE r.delivered_at IS NULL AND r.kind = 'request'"
        "   AND (r.payload -> 'decision') IS NOT NULL"
        "   AND btrim(r.work_item_id) = %s",
        (str(int(work_item_id)),))
    return bool(rows)


def _plan_hold(tx: Any, row: Mapping[str, Any], command: Command) -> _Plan:
    if row["state"] in TERMINAL_CORES:
        raise RegistryRefusal("terminal-reopened",
                              "lot %s in %s: a terminal does not reopen"
                              % (command.work_item_id, row["state"]))
    stage = row.get("workflow_stage")
    if not stage:
        raise RegistryRefusal("stage-mismatch", "lot %s: no current stage" % command.work_item_id)
    if row["state"] == command.hold:
        raise RegistryRefusal("hold-redundant",
                              "lot %s already in %s" % (command.work_item_id, command.hold))
    if command.hold == lifecycle.WAITING_HUMAN and not _pending_decision(tx, command.work_item_id):
        raise RegistryRefusal(
            "hold-no-decision",
            "waiting_human requires a pending native decision request (L233)")
    return _Plan(state=command.hold, stage=str(stage), loops=0,
                 note="hold %s (stage %s kept)" % (command.hold, stage))


def _plan_release(tx: Any, row: Mapping[str, Any], command: Command, spec: Any) -> _Plan:
    if row["state"] not in HOLDS:
        raise RegistryRefusal("not-held",
                              "lot %s in %s: no hold to lift"
                              % (command.work_item_id, row["state"]))
    if row["state"] == lifecycle.WAITING_HUMAN and _pending_decision(tx, command.work_item_id):
        raise RegistryRefusal(
            "decision-pending",
            "lot %s in waiting_human: the native decision is still open, "
            "the release waits for the answer (L233)" % command.work_item_id)
    stage = row.get("workflow_stage")
    if not stage:
        raise RegistryRefusal("stage-mismatch", "lot %s: no current stage" % command.work_item_id)
    core = _spec_core(spec, str(stage))
    if core is None:
        raise RegistryRefusal("stage-undeclared",
                              "stage %s absent from the artifact" % stage)
    if core in TERMINAL_CORES:
        raise RegistryRefusal("terminal-reopened", "stage %s is terminal" % stage)
    return _Plan(state=core, stage=str(stage), loops=0,
                 note="release from %s to %s" % (row["state"], stage))


def _pre_hold_core(tx: Any, work_item_id: int) -> str | None:
    """The last core recorded before the hold, read from the lot journal.

    A hold keeps the step but not the core: the row only says `blocked` or
    `waiting_human`, and the core before the hold is not a column anywhere.
    The journal is the verifiable record — every transition writes the state
    it produces, holds included — so the last line that is not a hold is the
    core the lot will resume into. `None` when the journal proves nothing (a
    lot created held, an out-of-band rewrite): fail-closed, the caller
    refuses.
    """
    rows = tx.query(
        "SELECT state FROM work_item_events"
        f" WHERE work_item_id = %s AND state NOT IN ('{lifecycle.BLOCKED}', '{lifecycle.WAITING_HUMAN}')"
        " ORDER BY id DESC LIMIT 1", (int(work_item_id),))
    if not rows:
        return None
    state = str(rows[0]["state"])
    return state if state in CORE_STATES else None


def _check_held_core(tx: Any, command: Command, core: str) -> None:
    """N10: the core before the hold must be proven, never assumed.

    Adopting or transferring a held lot keeps the hold; the core it resumes
    into is the one the new revision projects for the kept (or initial) step.
    Since the row does not carry the pre-hold core, the journal is the only
    witness: without one the registry refuses (`hold-core-unproven`) instead
    of silently resuming elsewhere; with one, a divergence is a
    `stage-mismatch`.
    """
    proof = _pre_hold_core(tx, int(command.work_item_id))
    if proof is None:
        raise RegistryRefusal(
            "hold-core-unproven",
            "lot %s is held and its journal proves no core before the hold: "
            "refusing a step that could resume elsewhere" % command.work_item_id)
    if proof != core:
        raise RegistryRefusal(
            "stage-mismatch",
            "lot %s was %s before the hold, the adopted step projects %s"
            % (command.work_item_id, proof, core))


def _verify(verifiers: Mapping[str, Verifier], guard: str, tx: Any,
            row: Mapping[str, Any], command: Command, spec: Any,
            from_stage: str, to_stage: str) -> Verification | None:
    """Calls the guard's installed verifier. No permissive default."""
    verifier = verifiers.get(guard)
    if verifier is None:
        raise RegistryRefusal("verifier-uninstalled",
                              "no verifier installed for guard %r" % guard)
    context = VerificationContext(db=tx, work_item=dict(row), command=command,
                                  spec=spec, from_stage=from_stage, to_stage=to_stage)
    try:
        result = verifier(context)
    except RegistryRefusal:
        raise
    except Exception as exc:
        raise RegistryRefusal("verifier-error",
                              "%s: %s" % (type(exc).__name__, exc)) from exc
    if not isinstance(result, Verification):
        raise RegistryRefusal(
            "verifier-invalid",
            "verifier %s returned %s: a Verification is expected, never a boolean"
            % (guard, type(result).__name__))
    if not result.ok:
        raise RegistryRefusal("verifier-refused", result.reason or "refused by %s" % guard)
    return result


def _evidence(command: Command, authority: dict, verification: Verification | None,
              intent: str, applied: dict, verifier_name: str) -> str:
    return json.dumps({
        "schema": RECEIPT_SCHEMA,
        "intent": {"sha256": intent},
        "command": applied,
        "authority": authority,
        "verification": ({"required": True, "verifier": verifier_name or None,
                          "ok": verification.ok, "reason": verification.reason,
                          "facts": dict(verification.facts or {})}
                         if verification is not None
                         else {"required": False, "verifier": None, "ok": None,
                               "reason": "", "facts": {}}),
        "facts": dict(command.evidence or {}),
    }, ensure_ascii=False, sort_keys=True)


def _applied(command: Command, plan: _Plan) -> dict:
    return {
        "action": command.action,
        "guard": command.guard or None,
        "from": command.from_stage or None,
        "to": command.to_stage or None,
        "hold": command.hold or None,
        "expected_version": int(command.expected_version),
        "workflow_ref": command.workflow_ref,
        "workflow_version": int(command.workflow_version),
    }


def _insert_receipt(tx: Any, *, work_item_id: int, state: str, note: str, actor: str,
                    command_id: str, stage: str, version: int, evidence: str) -> int:
    rows = tx.query(
        "INSERT INTO work_item_events"
        " (work_item_id, state, note, actor, command_id, workflow_stage,"
        "  workflow_version, evidence)"
        " VALUES (%s, %s, %s, %s, %s::uuid, %s, %s, %s::jsonb)"
        " RETURNING id",
        (int(work_item_id), state, note, actor, canonical_command_id(command_id),
         stage, int(version), evidence))
    return int(rows[0]["id"])


def _release_migration(tx: Any, command: Command, row: Mapping[str, Any],
                       state: str) -> None:
    """#956: a lot entering a released core gives its migration number back.

    The legacy `work.move`/`work.close` released the reservation in their own
    transaction; a pinned lot never reaches those, so the registry carries the
    same gesture — the storage `_release_migration`, including its journal
    line, written before the receipt so that `Receipt.version` stays the last
    id of the lot journal.
    """
    numbers = set()
    if row.get("migration") is not None:
        numbers.add(int(row["migration"]))
    if row.get("migration_slots"):
        numbers.update(int(slot["number"]) for slot in tx.query(
            "SELECT number FROM work_migration_reservations"
            " WHERE work_item_id = %s AND status = 'reserved' ORDER BY number",
            (int(command.work_item_id),)))
    if not numbers:
        return
    if row.get("migration_slots"):
        tx.execute(
            "UPDATE work_migration_reservations SET status = 'released',"
            "       updated_at = now()"
            " WHERE work_item_id = %s AND status = 'reserved'",
            (int(command.work_item_id),))
    tx.execute(
        "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
        "       updated_at = now() WHERE id = %s", (int(command.work_item_id),))
    ordered = sorted(numbers)
    what = ("migration %s" % ordered[0] if len(ordered) == 1
            else "migrations %s" % ", ".join(str(number) for number in ordered))
    tx.execute(
        "INSERT INTO work_item_events (work_item_id, state, note, actor)"
        " VALUES (%s, %s, %s, %s)",
        (int(command.work_item_id), state,
         "%s released (state %s, #956)" % (what, state),
         command.authority.actor))


def _close_milestones(tx: Any, command: Command, plan: _Plan) -> None:
    """The closure milestones the legacy `work.close` wrote: `closed` always,
    `delivered` (with the reviewed head) for a delivery outside the target."""
    tx.execute(
        "INSERT INTO work_item_milestones (work_item_id, kind, actor, note)"
        " VALUES (%s, 'closed', %s, %s) ON CONFLICT DO NOTHING",
        (int(command.work_item_id), command.authority.actor, plan.note))
    if plan.close_reason == "delivered":
        tx.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note)"
            " VALUES (%s, 'delivered', %s, %s, %s) ON CONFLICT DO NOTHING",
            (int(command.work_item_id), plan.delivered_sha,
             command.authority.actor, plan.note))


def _notify_released(tx: Any, command: Command, plan: _Plan, previous: str,
                     threads: list) -> None:
    """L385: the first entry into a released core wakes the dependants.

    Under the dependency graph lock (taken before the lot, as the legacy
    closures do), the mail is committed with the transition: no window where
    the release is committed without its wake. A lot already released does not
    wake twice (a `merged` then `closed`/`promoted` path); a closure carries
    its reason to the message.
    """
    if previous in RELEASED_CORES:
        return
    from . import work_dependencies
    work_dependencies.notify_released(
        tx, int(command.work_item_id), blocker_state=plan.state,
        blocker_reason=plan.close_reason if plan.state == lifecycle.CLOSED else "",
        after_commit=threads)


def _receipt(command: Command, plan: _Plan, event_id: int, version: int,
             intent: str, replayed: bool = False) -> Receipt:
    return Receipt(
        accepted=True, replayed=replayed,
        work_item_id=int(command.work_item_id),
        command_id=canonical_command_id(command.command_id),
        action=command.action,
        event_id=int(event_id), version=int(version),
        state=plan.state, workflow_ref=command.workflow_ref,
        workflow_stage=plan.stage, workflow_version=int(command.workflow_version),
        guard=command.guard, intent_sha256=intent,
    )


# --------------------------------------------------------------------------
# API: apply a command, transfer a revision, read the state
# --------------------------------------------------------------------------

def apply_command(db: Db, command: Command, *, spec: Any) -> Receipt:
    """Applies a native command to the lot, or refuses (nothing written).

    Everything — lot lock, authority, revision, version, guard, lot write
    and receipt — holds in one transaction. A replay of the same intent returns
    the accepted receipt (`replayed=True`); the same key with another intent is
    an `intent-conflict`.

    Verifiers come from the **installed** registry (`install_verifier`):
    the caller never provides the result of a guard. `_apply` reserves
    injection for the test bench.
    """
    return _apply(db, command, spec=spec, verifiers=INSTALLED_VERIFIERS)


def _apply(db: Db, command: Command, *, spec: Any,
           verifiers: Mapping[str, Verifier] | None = None) -> Receipt:
    _validate_command(command)
    if command.action == "transfer":
        raise RegistryRefusal("command-invalid",
                              "action transfer: go through transfer_revision")
    _validate_spec(spec)
    _check_spec(spec, command)
    registry = _registry(verifiers)
    intent = intent_sha256(command)
    # N9: an advance entering a released core decides the wake of the
    # dependants under the graph lock, taken BEFORE the lot — the order of the
    # legacy closures (`work.close`/`close_merged`), which take it
    # then read `released_by`. Without it, two concurrent releases
    # would lose or double the wake; taken after the lot, it
    # would deadlock with a legacy closure.
    released = (command.action == "advance"
                and _spec_core(spec, command.to_stage) in RELEASED_CORES)
    threads: list = []
    with db.transaction() as tx:
        if released:
            from . import work_dependencies
            work_dependencies.lock(tx)
        row = _lock_lot(tx, command.work_item_id)
        if command.action in ("hold", "release"):
            # N12: the lot first, then the open decision requests —
            # the order of the single L233 statement (`work.move(held=True)`
            # locks the lot, then the requests in its EXISTS ... FOR
            # UPDATE). The `_pending_decision` read comes after both
            # locks: a request committed while awaiting the lot is seen.
            _lock_decisions(tx, command.work_item_id)
        authority = _check_authority(tx, command.authority)
        replayed = _replay(tx, command, intent)
        if replayed is not None:
            return replayed
        _check_revision(row, command)
        _check_version(tx, command)

        verification: Verification | None = None
        if command.action == "advance":
            plan = _plan_advance(tx, row, command, spec)
            verification = _verify(registry, command.guard, tx, row, command, spec,
                                   command.from_stage, command.to_stage)
            verifier_name = command.guard
        elif command.action == "hold":
            plan = _plan_hold(tx, row, command)
            verification = _verify(registry, HOLD_VERIFIER, tx, row, command, spec,
                                   plan.stage, plan.stage)
            verifier_name = HOLD_VERIFIER
        else:  # release (transfer is refused above)
            plan = _plan_release(tx, row, command, spec)
            verification = _verify(registry, HOLD_VERIFIER, tx, row, command, spec,
                                   plan.stage, plan.stage)
            verifier_name = HOLD_VERIFIER

        tx.execute(
            "UPDATE work_items"
            "   SET state = %s,"
            "       workflow_stage = %s,"
            "       loops = loops + %s,"
            "       close_reason = CASE WHEN %s = 'closed' THEN %s ELSE close_reason END,"
            "       superseded_by = CASE WHEN %s = 'closed' THEN %s ELSE superseded_by END,"
            "       updated_at = now(),"
            "       closed_at = CASE WHEN %s IN ('promoted', 'closed')"
            "                         THEN now() ELSE closed_at END,"
            "       started_at = CASE WHEN %s IN ('build', 'qa')"
            "                         THEN coalesce(started_at, now()) ELSE started_at END"
            " WHERE id = %s",
            (plan.state, plan.stage, plan.loops, plan.state, plan.close_reason or None,
             plan.state, plan.superseded_by, plan.state, plan.state,
             int(command.work_item_id)))
        if plan.state in RELEASED_CORES:
            _release_migration(tx, command, row, plan.state)
        if plan.state == lifecycle.CLOSED:
            _close_milestones(tx, command, plan)
        evidence = _evidence(command, authority, verification, intent,
                             _applied(command, plan), verifier_name)
        event_id = _insert_receipt(
            tx, work_item_id=command.work_item_id, state=plan.state, note=plan.note,
            actor=command.authority.actor, command_id=command.command_id,
            stage=plan.stage, version=command.workflow_version, evidence=evidence)
        if plan.state in RELEASED_CORES:
            _notify_released(tx, command, plan, str(row["state"]), threads)
        receipt = _receipt(command, plan, event_id, event_id, intent)
    # the thread readable only after the commit: a canceled send leaves nothing
    for record in threads:
        record(db.cfg, db)
    return receipt


def transfer_revision(db: Db, *, work_item_id: int, command_id: str,
                      expected_version: int, workflow_ref: str, workflow_version: int,
                      authority: Authority, spec: Any,
                      evidence: Mapping[str, Any] | None = None) -> Receipt:
    """Pins (or changes) a lot's Process revision — an explicit gesture only.

    First adoption: the artifact's initial stage must project the lot's
    current native core (a hold, however, is kept); a terminal lot
    is never reopened, even by adoption. Transfer: the current stage is
    kept, it must exist in the new artifact and project its current
    core outside a hold; a version regression is refused. A
    `revision_transfer` server verifier must be installed: no
    transfer is possible by default, and the caller never provides its
    result (`_transfer` reserves injection for the test bench).

    #4007: slice C installs it (`process_verifiers.install_verifiers`,
    contract `revision-transfer/1`). Its proof source is the conditional
    `approvals` collection of `read_sources` (approval projection, #4006):
    the current approval of the exact pinned artifact, cited by the
    `evidence` (`approval`, `lot`, `project`). The accepted proof lands in the
    receipt under `verification.facts.proof`.
    """
    return _transfer(db, work_item_id=work_item_id, command_id=command_id,
                     expected_version=expected_version, workflow_ref=workflow_ref,
                     workflow_version=workflow_version, authority=authority,
                     spec=spec, evidence=evidence, verifiers=INSTALLED_VERIFIERS)


def _transfer(db: Db, *, work_item_id: int, command_id: str,
              expected_version: int, workflow_ref: str, workflow_version: int,
              authority: Authority, spec: Any,
              evidence: Mapping[str, Any] | None = None,
              verifiers: Mapping[str, Verifier] | None = None) -> Receipt:
    command = Command(
        work_item_id=int(work_item_id), command_id=command_id,
        expected_version=int(expected_version), authority=authority,
        workflow_ref=workflow_ref, workflow_version=int(workflow_version),
        action="transfer", evidence=dict(evidence or {}))
    _validate_command(command)
    _validate_spec(spec)
    _check_spec(spec, command)
    registry = _registry(verifiers)
    intent = intent_sha256(command)
    with db.transaction() as tx:
        row = _lock_lot(tx, command.work_item_id)
        authority = _check_authority(tx, command.authority)
        replayed = _replay(tx, command, intent)
        if replayed is not None:
            return replayed
        _check_version(tx, command)

        if row.get("workflow_ref") is None:
            if row["state"] in TERMINAL_CORES:
                raise RegistryRefusal(
                    "terminal-reopened",
                    "lot %s in %s: a terminal does not reopen, even by adoption"
                    % (command.work_item_id, row["state"]))
            stage = str(getattr(spec, "initial"))
            core = _spec_core(spec, stage)
            if core is None:
                raise RegistryRefusal("spec-invalid",
                                      "initial stage %r without a core" % stage)
            if core in TERMINAL_CORES:
                raise RegistryRefusal("terminal-reopened",
                                      "initial stage %s already terminal" % stage)
            if row["state"] in HOLDS:
                # N10: the core before the hold is not on the row; the
                # journal must prove it before adopting a stage.
                _check_held_core(tx, command, str(core))
                state = row["state"]
            elif core == str(row["state"]):
                state = core
            else:
                raise RegistryRefusal(
                    "stage-mismatch",
                    "adoption: initial stage %s projects %s, lot in %s"
                    % (stage, core, row["state"]))
        else:
            if int(row.get("workflow_version") or 0) > command.workflow_version:
                raise RegistryRefusal(
                    "revision-regression",
                    "revision %s → %s: a transfer does not go backwards"
                    % (row.get("workflow_version"), command.workflow_version))
            stage = str(row.get("workflow_stage") or getattr(spec, "initial"))
            core = _spec_core(spec, stage)
            if core is None:
                raise RegistryRefusal("stage-undeclared",
                                      "stage %s absent from the artifact" % stage)
            if row["state"] in HOLDS:
                _check_held_core(tx, command, str(core))
            elif core != str(row["state"]):
                raise RegistryRefusal(
                    "stage-mismatch",
                    "transfer: stage %s projects %s, lot in %s"
                    % (stage, core, row["state"]))
            state = row["state"]

        verification = _verify(registry, TRANSFER_VERIFIER, tx, row, command, spec,
                               row.get("workflow_stage") or "", stage)
        plan = _Plan(state=str(state), stage=stage, loops=0,
                     note="transferring revision %s@%s" % (command.workflow_ref,
                                                           command.workflow_version))
        tx.execute(
            "UPDATE work_items"
            "   SET state = %s, workflow_ref = %s, workflow_stage = %s,"
            "       workflow_version = %s, updated_at = now()"
            " WHERE id = %s",
            (plan.state, command.workflow_ref, plan.stage,
             int(command.workflow_version), int(command.work_item_id)))
        applied = _applied(command, plan)
        applied.update({"previous_ref": row.get("workflow_ref"),
                        "previous_version": int(row.get("workflow_version") or 0)})
        payload = _evidence(command, authority, verification, intent, applied,
                            TRANSFER_VERIFIER)
        event_id = _insert_receipt(
            tx, work_item_id=command.work_item_id, state=plan.state, note=plan.note,
            actor=command.authority.actor, command_id=command.command_id,
            stage=plan.stage, version=command.workflow_version, evidence=payload)
        return _receipt(command, plan, event_id, event_id, intent)


def registry_state(db: Db, work_item_id: int) -> RegistryState:
    """State of a lot's registry: revision, stage, journal version, receipts."""
    rows = db.query(
        "SELECT id, state, to_jsonb(work_items)->>'legacy_state' AS legacy_state, workflow_ref, workflow_stage, workflow_version"
        "  FROM work_items WHERE id = %s", (int(work_item_id),))
    if not rows:
        raise RegistryRefusal("lot-unknown", "lot %s not found" % work_item_id)
    row = rows[0]
    version = _journal_version(db, work_item_id)
    receipts = db.query(
        "SELECT count(*)::int AS n FROM work_item_events"
        " WHERE work_item_id = %s AND command_id IS NOT NULL", (int(work_item_id),))
    return RegistryState(
        work_item_id=int(work_item_id), state=lifecycle.phase(row),
        workflow_ref=row["workflow_ref"], workflow_stage=row["workflow_stage"],
        workflow_version=int(row["workflow_version"] or 0),
        version=int(version), receipts=int(receipts[0]["n"]))


def receipt_of(db: Db, work_item_id: int, command_id: str) -> dict | None:
    """The receipt of a key, or None: read-only, without authority or lock."""
    rows = db.query(
        "SELECT id, state, note, actor, workflow_stage, workflow_version, evidence,"
        "       extract(epoch from created_at)::float8 AS created_ts"
        "  FROM work_item_events WHERE work_item_id = %s AND command_id = %s::uuid",
        (int(work_item_id), canonical_command_id(command_id)))
    if not rows:
        return None
    row = dict(rows[0])
    row["evidence"] = _as_json(row["evidence"])
    return row


#: Collections actually collected for slice C. `observations` is conditional:
#: it is present only when the host has a fresh, valid local projection of the
#: IMA observations journal (#4425). `approvals` is conditional too: it is
#: present only when the host has a fresh, valid local projection of the
#: approval records (#4006).
SOURCE_KEYS = ("milestones", "receipts", "observations", "decisions",
               "deliveries", "approvals")


def read_sources(context: VerificationContext) -> dict[str, Any]:
    """The lot's real collections, read inside the verifier's transaction.

    slice C: its verifiers are pure and SQL-free; this reader is
    theirs, called from the installed callable. The mapping declares only what is
    actually collected — `SOURCE_KEYS` — never an absent collection: a verifier
    that requires it refuses `source-unavailable` (fail-closed), never a
    default agreement.

    * `milestones`: `{id, kind, sha, verdict, note, actor}` — the actor is
      projected (#1994): C assigns the verdict per actor (`_current_verdict`),
      and a verdict without an actor is not a review;
    * `receipts`: accepted receipts (`command_id` not null) — `{accepted, guard,
      event_id, command_id, workflow_stage, workflow_version, sha256,
      evidence, facts}`; `guard` is the Process guard, or the reserved name of
      the verifier (`hold`, `revision_transfer`);
    * `decisions`: open native decision requests, `{id, kind, payload,
      work_item_id}` (L233);
    * `deliveries`: native `delivered` milestones, `{id, sha, accepted}` — `id`
      is projected as a string (#1994): it is the client reference that C
      compares;
    * `approvals` (#4006): the local GitHub approval projection,
      `{"projection": {generated_at, expires_at, digest, source_digest, repo,
      issue}, "records": [{id, comment_id, author, artifact, state,
      superseded_by, revoked_by, ...}]}`. The key is **absent** when the
      projection is not configured, missing, stale, unreadable or invalid:
      a verifier that requires approvals then refuses `source-unavailable`,
      never a default agreement. No network read happens here.
    * `observations` (#4425): the current, non-expired records of the local
      observations projection for this lot and project,
      `[{id, kind, env, sha, state, lot, app, actor, observed_at,
      source_revision, provenance}]`. The key is **absent** when the
      projection is not configured, missing, stale, unreadable or invalid;
      a revoked, superseded, expired or future record is never listed. No
      network read happens here.
    """
    if not isinstance(context, VerificationContext):
        raise RegistryRefusal("verifier-invalid", "VerificationContext expected")
    db = context.db
    item_id = int(context.command.work_item_id)
    milestones = [
        {"id": int(row["id"]), "kind": str(row["kind"]),
         "sha": str(row["sha"] or ""), "verdict": row["verdict"],
         "note": str(row["note"] or ""), "actor": str(row["actor"] or "")}
        for row in db.query(
            # #2317 (N5) : un jalon `merged` annulé par une correction
            # `merged → build` n'est plus une révision livrée ; il ne doit ni
            # servir de `merged_of` ni de `revision_of` (fonction 0463).
            "SELECT id, kind, sha, verdict, note, actor FROM work_item_milestones"
            " WHERE work_item_id = %s"
            "   AND (kind <> 'merged'"
            "        OR NOT agent_mesh_merged_annulled(work_item_id, at))"
            " ORDER BY id", (item_id,))]
    receipts: list[dict[str, Any]] = []
    for row in db.query(
            "SELECT id, command_id::text AS command_id, workflow_stage,"
            "       workflow_version, evidence"
            "  FROM work_item_events"
            " WHERE work_item_id = %s AND command_id IS NOT NULL ORDER BY id",
            (item_id,)):
        evidence = _as_json(row["evidence"])
        if not isinstance(evidence, Mapping):
            evidence = {}
        command = evidence.get("command")
        verification = evidence.get("verification")
        guard = ""
        if isinstance(command, Mapping):
            guard = str(command.get("guard") or "")
        if not guard and isinstance(verification, Mapping):
            guard = str(verification.get("verifier") or "")
        facts = evidence.get("facts")
        receipts.append({
            "accepted": True,
            "guard": guard,
            "event_id": int(row["id"]),
            "command_id": str(row["command_id"]),
            "workflow_stage": row["workflow_stage"],
            "workflow_version": int(row["workflow_version"] or 0),
            "sha256": _stored_intent(evidence),
            "evidence": dict(evidence),
            "facts": dict(facts) if isinstance(facts, Mapping) else {},
        })
    decisions = [
        {"id": int(row["id"]), "kind": str(row["kind"]),
         "payload": _as_json(row["payload"]),
         "work_item_id": str(row["work_item_id"] or "")}
        for row in db.query(
            "SELECT id, kind, payload, work_item_id FROM agent_mailbox"
            " WHERE delivered_at IS NULL AND kind = 'request'"
            "   AND (payload -> 'decision') IS NOT NULL"
            "   AND btrim(work_item_id) = %s ORDER BY id", (str(item_id),))]
    deliveries = [
        {"id": str(row["id"]), "sha": str(row["sha"] or ""), "accepted": True}
        for row in db.query(
            "SELECT id, sha FROM work_item_milestones"
            " WHERE work_item_id = %s AND kind = 'delivered' ORDER BY id",
            (item_id,))]
    sources: dict[str, Any] = {"milestones": milestones, "receipts": receipts,
                               "decisions": decisions, "deliveries": deliveries}
    # #4006 : la projection locale est la seule source d'approbation. Toute
    # absence ou peremption la retire du mapping (fail-closed) ; aucune
    # lecture reseau n'a lieu ici.
    from . import process_approvals as approvals_mod
    try:
        projection = approvals_mod.local_projection()
    except approvals_mod.ApprovalError:
        projection = None
    if projection is not None:
        sources["approvals"] = projection.as_source()
    # #4425 : la projection locale du journal d'observations est la seule
    # source des deploiements/QA/acceptations. Toute absence, peremption ou
    # invalidite la retire du mapping (fail-closed) ; aucune lecture reseau
    # n'a lieu ici.
    from . import process_observations as observations_mod
    try:
        observations = observations_mod.local_projection()
    except observations_mod.ObservationError:
        observations = None
    if observations is not None:
        item = context.work_item if isinstance(context.work_item, Mapping) else {}
        try:
            sources["observations"] = observations_mod.read_sources(
                observations, work_item_id=item_id,
                app=str(item.get("app") or ""))
        except (observations_mod.ObservationError, ValueError):
            # Un lot hors forme (app invalide) ne doit pas transformer la
            # garde en `verifier-error` : la source est absente, fail-closed.
            pass
    return sources
