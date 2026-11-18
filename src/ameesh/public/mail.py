# SPDX-License-Identifier: AGPL-3.0-only
"""Public mail surface, contract v1.3 (tranches E2a/E2b, #7636/#5827/#7706).

An installed external module already trusted with a database identity submits
typed mail, reads its own bounded history, lists the requests a live turn is
holding and answers one of them through this module instead of writing
``agent_mailbox`` SQL.  The written contract is
``docs/api/module-contract-v1.md``; internal helpers of this package are not
part of it.

What the core owns here:

* the **actor** is fixed by the call — a submission is always deposited with
  ``sender = actor``; a caller cannot name another sender, and the core's own
  sender identities (``ameesh``, ``veille``, the canon's role-holding
  Agent/Persona names, human and chat prefixes) are refused before alias
  resolution or deposit; a canon that is configured but cannot be read
  completely refuses the call rather than dropping its roles;
* **idempotency** is scoped to ``(actor, recipient)``: the same key with the
  same content reuses the durable message id, the same key with another
  content refuses, and concurrent retries end on one durable row (the unique
  index of migration 0497 is the authority);
* **history** is bounded by a cursor and restricted to conversations the actor
  takes part in;
* **pending requests** are the ones reserved as the prompt of the actor's
  current native turn (the ``spend_pending`` marker is the authority) and not
  yet answered; the caller presents the live lease it acts under, and a stale
  or replaced owner/epoch refuses;
* **reply correlation**: one reply per request, tied by ``in_reply_to`` and
  protected by the unique index of migration 0594; an identical retry reuses
  the durable reply, a different content refuses, and the reply is addressed
  to the request's sender, never to a caller-named recipient;
* the module-owned payload is a JSON object under the imposed
  ``payload["module"]`` namespace; every other top-level key is core-owned and
  refused, so a core marker added in a later release is closed to modules by
  default.  ``payload.attribution`` is the one admitted core key, validated
  and never authority.

The database credential remains the trust boundary: this surface scopes a
caller that already holds the database identity, it does not authenticate a
remote client.  A process running under a mesh runner must use the identity
that runner verified.  The mediated executor contract is not served here.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from typing import Any, Mapping, Sequence

from .. import agent_names, cost_attribution, fil, mail, registry, storage
from ..config import NAME_RE
from ..db import Db, DbError, is_unique_violation

#: revision of the written contract implemented by this module
CONTRACT_VERSION = "1.3"

#: maximum size of one body, in UTF-8 bytes
MAX_BODY_BYTES = 64 * 1024
#: maximum size of one serialized module payload, in UTF-8 bytes
MAX_PAYLOAD_BYTES = 256 * 1024
#: maximum number of submissions in one :func:`submit_many` call
MAX_BATCH = 50
#: maximum number of entries read by one :func:`history` page
MAX_HISTORY = 200
#: default number of entries read by one :func:`history` page
DEFAULT_HISTORY = 100
#: maximum number of requests read by one :func:`pending` page
MAX_PENDING = 200
#: default number of requests read by one :func:`pending` page
DEFAULT_PENDING = 50

#: mail natures admitted by the public surface
KINDS = ("request", "reply", "notify", "event")

#: shape of a client idempotency key (same as the core mailbox)
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

#: unique index that carries idempotency in the core mailbox (migration 0497)
_IDEMPOTENCY_INDEX = "agent_mailbox_idempotency_key_idx"

#: unique index that carries reply correlation (migration 0594, E2b)
_REPLY_INDEX = "agent_mailbox_in_reply_to_idx"

#: known payload keys the core reads for delivery, review, decisions,
#: accounting or threading.  They are listed for diagnostics and for the
#: precise refusal message; admission is the allow-list below, so a marker
#: added later is refused even before this list knows it.
RESERVED_PAYLOAD_KEYS = frozenset((
    "idempotency_key",
    "report",
    "report_fingerprint",
    "urgent",
    "ack",
    "info",
    "cc",
    "broadcast",
    "radar",
    "ci_passif",
    "human",
    "human_account",
    "verdict",
    "arbitration",
    "reassigned",
    "request_expires_ts",
    # Core control markers reaching beyond delivery: the mailbox chooses a
    # review-request rewrite on `review_designation`/`review_request`
    # (`Mailbox.send`/`_send_review`), a decision is recognised and closed on
    # `decision`/`decision_answer`, and the auditor pass, the CI queue and the
    # staging emission are keyed by `pass`, `ci` and `staging_actif`.  A
    # module payload is data: it may never select one of those core paths.
    "review_designation",
    "review_request",
    "decision",
    "decision_answer",
    "pass",
    "ci",
    "staging_actif",
))

#: the one top-level payload namespace a module owns; its value is the module's
#: JSON object.  Every other top-level key is core-owned and refused.
MODULE_PAYLOAD_KEY = "module"

#: top-level payload keys admitted by the public surface.  This is an
#: allow-list, not a deny-list: a core marker added to the mailbox in a later
#: release is refused here by default, even before RESERVED_PAYLOAD_KEYS names
#: it.  ``attribution`` is validated by the closed caller-attribution schema.
ADMITTED_PAYLOAD_KEYS = frozenset((MODULE_PAYLOAD_KEY, "attribution"))

#: Sender identities the core owns statically: the system sender, the watch
#: sender and the unbound actor.  A public module submits under its own
#: identity; these names are refused before alias resolution, identity checks
#: and any deposit.  The governance identities are **not** named here: they
#: are read from the configured canon (:func:`reserved_actors`), so a mesh's
#: roles stay in its canon (#7637/#7660).  Their sources are cross-checked by
#: tests/test_7706_mail_contract.py so drift is caught.
RESERVED_ACTORS = frozenset((
    "ameesh",      # work.SYSTEM_SENDER: system events, CI, deep analysis
    "veille",      # watch.SENDER: periodic watch reports
    "inconnu",     # work.UNKNOWN_ACTOR: no bound identity
))

#: Prefixes owned by the core: human identities and chat agents.
RESERVED_ACTOR_PREFIXES = ("human:", "chat-")

#: How long the canon's role-holding identities are reused before a new read:
#: a module may submit often, a canon changes rarely.
_CANON_ACTORS_TTL_S = 60.0
#: configured canon key -> (monotonic read time, reserved titles)
_CANON_ACTORS: dict[tuple, tuple[float, frozenset[str]]] = {}


# ---------------------------------------------------------------------------
# typed values
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Submission:
    """One message to deposit.

    ``recipient`` must be a registered agent.  ``kind`` is one of
    :data:`KINDS`.  ``payload`` is the module-owned JSON object, carried under
    the imposed ``payload["module"]`` namespace (a top-level key outside that
    namespace and ``attribution`` is refused); ``work_item_id`` optionally
    links the message to a lot; ``thread=False`` keeps private module content
    out of the readable project thread (the mailbox row and its history
    remain).  ``idempotency_key`` scopes retries to ``(actor, recipient)``.
    """

    recipient: str
    body: str
    kind: str = "notify"
    payload: Mapping[str, Any] | None = None
    work_item_id: str | int | None = None
    thread: bool = True
    idempotency_key: str | None = None


@dataclasses.dataclass(frozen=True)
class Receipt:
    """The durable result of one submission or reply.

    ``id`` is the mailbox row id, stable across retries; for a ``request`` it
    is also the request id a reply correlates with.  ``reused`` is true when
    this call returned an already deposited submission instead of creating one.
    For a ``reply``, ``in_reply_to`` is the answered request id; it is None for
    every other nature.  ``request_id`` keeps its E2a meaning: the id of this
    message when it is a request, None otherwise.
    """

    id: int
    actor: str
    recipient: str
    kind: str
    created_ts: float
    reused: bool
    in_reply_to: int | None = None

    @property
    def request_id(self) -> int | None:
        """The durable request id, or None when the message is not a request."""
        return self.id if self.kind == "request" else None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "in_reply_to": self.in_reply_to,
            "actor": self.actor,
            "recipient": self.recipient,
            "kind": self.kind,
            "created_ts": self.created_ts,
            "reused": self.reused,
        }


@dataclasses.dataclass(frozen=True)
class HistoryEntry:
    """One message involving the actor, as read by :func:`history`."""

    id: int
    sender: str
    recipient: str
    body: str
    kind: str
    payload: dict
    work_item_id: str
    created_ts: float
    delivered_ts: float | None
    in_reply_to: int | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "kind": self.kind,
            "payload": dict(self.payload),
            "work_item_id": self.work_item_id,
            "created_ts": self.created_ts,
            "delivered_ts": self.delivered_ts,
            "in_reply_to": self.in_reply_to,
        }


@dataclasses.dataclass(frozen=True)
class Lease:
    """The native lease a caller acts under: runner id and epoch.

    Build it from ``ameesh turn current --json`` (``lease.owner`` and
    ``lease.epoch``).  It is a claim the core rechecks against the registry
    under a row lock; it never grants authority by itself.
    """

    owner: str
    epoch: int

    def as_dict(self) -> dict:
        return {"owner": self.owner, "epoch": self.epoch}


@dataclasses.dataclass(frozen=True)
class Reply:
    """The content of one answer to a pending request.

    ``payload`` is the module-owned JSON object, validated like a submission's.
    ``thread=False`` keeps the answer out of the readable project thread.  The
    recipient is the request's sender and the lot is the request's lot; neither
    is chosen here.
    """

    body: str
    payload: Mapping[str, Any] | None = None
    thread: bool = True


@dataclasses.dataclass(frozen=True)
class PendingRequest:
    """One request held by the actor's live lease and current native turn."""

    id: int
    sender: str
    recipient: str
    body: str
    payload: dict
    work_item_id: str
    created_ts: float
    delivered_ts: float | None
    turn_id: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "payload": dict(self.payload),
            "work_item_id": self.work_item_id,
            "created_ts": self.created_ts,
            "delivered_ts": self.delivered_ts,
            "turn_id": self.turn_id,
        }


@dataclasses.dataclass(frozen=True)
class PendingPage:
    """One bounded page of :func:`pending`, oldest request first.

    ``has_more`` says another request exists after this page; call
    :func:`pending` again after answering to get it.  There is no cursor:
    pending is a live set, not an append-only history.
    """

    entries: tuple[PendingRequest, ...]
    has_more: bool

    def as_dict(self) -> dict:
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "has_more": self.has_more,
        }


@dataclasses.dataclass(frozen=True)
class HistoryPage:
    """One bounded page of :func:`history`, oldest entry first.

    ``next_cursor`` is the last returned id (or the caller's cursor when the
    page is empty): pass it back as ``after_id``.  ``has_more`` says another
    page exists after this one.
    """

    entries: tuple[HistoryEntry, ...]
    next_cursor: int | None
    has_more: bool

    def as_dict(self) -> dict:
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


@dataclasses.dataclass(frozen=True)
class SubmissionResult:
    """The outcome of one item of a bounded batch (``receipt`` XOR ``error``)."""

    index: int
    receipt: Receipt | None = None
    error: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.receipt is not None

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "ok": self.ok,
            "receipt": None if self.receipt is None else self.receipt.as_dict(),
            "error": self.error,
            "detail": self.detail,
        }


@dataclasses.dataclass(frozen=True)
class BatchResult:
    """Ordered outcomes of one :func:`submit_many` call.

    A batch is bounded but not atomic: each item keeps its own durable
    submission (or its own refusal), like the per-turn commits of a dispatch.
    """

    results: tuple[SubmissionResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "results": [result.as_dict() for result in self.results],
        }


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

class PublicMailError(Exception):
    """Base refusal of the public mail surface."""

    #: stable machine code carried by a batch result
    code = "error"


class InvalidSubmission(PublicMailError, ValueError):
    """The submission is malformed or exceeds a published bound."""

    code = "invalid"


class IdempotencyConflict(PublicMailError, ValueError):
    """The idempotency key already names a submission with other content."""

    code = "conflict"


class ReplyConflict(PublicMailError, ValueError):
    """The request already has one correlated reply with other content.

    A retry of the same reply returns its durable receipt with ``reused=True``;
    a different body or payload is refused instead of being deposited as a
    second answer.
    """

    code = "conflict"


class ScopeRefused(PublicMailError, PermissionError):
    """The actor, its lease or the target request is outside this scope.

    Covers a lease whose owner/epoch was replaced or has expired, a request
    that is not the actor's live current-turn prompt, and a request already
    answered when the caller is not retrying the same reply.
    """

    code = "refused"


class CanonInventoryUnavailable(ScopeRefused):
    """The configured canon inventory cannot be read completely.

    A canon that is configured but unreadable, partially loaded or invalid
    could omit a governance role: the surface refuses instead of treating the
    missing names as ordinary service identities.
    """

    code = "refused"


class RecipientRefused(PublicMailError):
    """The recipient is unknown or retired; nothing is deposited."""

    code = "refused"


class UnsupportedStorage(PublicMailError, NotImplementedError):
    """The public mail surface needs the direct driver, not a mediated one."""

    code = "unsupported"


# ---------------------------------------------------------------------------
# prepared, validated values
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Prepared:
    actor: str
    recipient: str
    body: str
    kind: str
    payload: dict
    work_item_id: str | None
    thread: bool
    idempotency_key: str | None


@dataclasses.dataclass(frozen=True)
class _PreparedReply:
    body: str
    payload: dict
    thread: bool


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _direct(db: Db) -> None:
    if storage.is_remote(db) or getattr(storage.of(db), "driver", "") == "mediated":
        raise UnsupportedStorage(
            "the public mail surface requires the direct database driver; "
            "the mediated executor contract does not serve it")


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidSubmission("%s must be a string" % what)
    value = value.strip()
    if not NAME_RE.match(value):
        raise InvalidSubmission(
            "%s is not a valid agent name (letters, digits, . _ -): %r"
            % (what, value))
    return value


def _resolve(db: Db, name: str, operation: str) -> str:
    cfg = getattr(db, "cfg", None)
    return agent_names.resolve(name, db=db, cfg=cfg, operation=operation)


def _canon_key(entries) -> tuple:
    """Identity of the configured canons, for the short read cache."""
    return tuple((str(getattr(entry, "path", "")),
                  str(getattr(entry, "ref", "")),
                  bool(getattr(entry, "untrusted", False)))
                 for entry in entries)


def _canon_actor_names(db: Db) -> frozenset[str]:
    """Titles of the canon Agent/Persona cards that declare a role.

    The core never names a mesh's governance roles (#7637/#7660): a card that
    declares ``roles:`` is a governed identity, whatever its labels are
    (orchestrator, auditor, bureau, pilot, composer, watch...).  Reading the
    cards therefore covers a role added later or declared by another canon.

    No configured canon is an explicit absence: only the static
    :data:`RESERVED_ACTORS` and the prefixes apply.  A canon that **is**
    configured but cannot be read completely is not an absence of roles: the
    read is refused with :class:`CanonInventoryUnavailable`, because guessing
    an empty inventory would admit an omitted governance name as a service
    identity.  Only a complete, readable inventory is cached.
    """
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        return frozenset()
    from .. import canon as canon_mod

    try:
        entries = canon_mod.configured(cfg)
    except Exception as exc:  # noqa: BLE001 - refus explicite, pas une absence
        raise CanonInventoryUnavailable(
            "the configured canon inventory cannot be listed: %s" % exc) from exc
    if not entries:
        return frozenset()
    key = _canon_key(entries)
    now = time.monotonic()
    cached = _CANON_ACTORS.get(key)
    if cached is not None and now - cached[0] < _CANON_ACTORS_TTL_S:
        return cached[1]
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001 - refus explicite, pas une absence
        raise CanonInventoryUnavailable(
            "the configured canon inventory cannot be read: %s" % exc) from exc
    if len(canons) != len(entries):
        raise CanonInventoryUnavailable(
            "the configured canon inventory is incomplete: %d of %d canon(s) "
            "read" % (len(canons), len(entries)))
    for canon in canons:
        if not getattr(canon, "readable", False):
            raise CanonInventoryUnavailable(
                "the configured canon %s is unreadable"
                % (getattr(canon, "root", "") or "?"))
        broken = canon_mod.errors(getattr(canon, "load_findings", ()) or ())
        if broken:
            raise CanonInventoryUnavailable(
                "the configured canon %s is invalid (%s)"
                % (getattr(canon, "root", "") or "?", broken[0].code))
    names = frozenset(
        name for name in (
            str(getattr(card, "title", "") or "").strip().casefold()
            for canon in canons
            for card in getattr(canon, "agents", ()) or ()
            if getattr(card, "roles", None))
        if name)
    _CANON_ACTORS[key] = (now, names)
    return names


def reserved_actors(db: Db) -> frozenset[str]:
    """The sender names this deployment refuses on the public surface.

    The static system senders plus the role-holding Agent/Persona titles of
    the configured canon, case-folded.  The reserved **prefixes** are separate
    and listed by :data:`RESERVED_ACTOR_PREFIXES`.  Raises
    :class:`CanonInventoryUnavailable` (a :class:`ScopeRefused`) when a canon
    is configured but its complete, readable inventory cannot be established.
    """
    return frozenset(RESERVED_ACTORS | _canon_actor_names(db))


def _reset_canon_cache() -> None:
    """Forget the canon identities read so far (tests, explicit reload)."""
    _CANON_ACTORS.clear()


def _reserved_actor(db: Db, name: str) -> bool:
    """``name`` is one of the core's own sender identities (case-insensitive)."""
    folded = name.casefold()
    return (folded in RESERVED_ACTORS
            or folded.startswith(RESERVED_ACTOR_PREFIXES)
            or folded in _canon_actor_names(db))


def _actor(db: Db, actor: Any) -> str:
    """Resolve the actor and check the identity this process declares.

    A caller that holds the database identity is trusted; what this check adds
    is that a process run by a mesh runner cannot submit under another name.
    Processes with no declared identity (an installed module service) keep the
    actor they name — the database credential is their trust boundary.

    The core's own sender identities are never available to this surface: a
    module payload that named ``ameesh`` (or a canon role-holder, human or
    chat name) would inherit the paths and authority those senders carry.  The
    governance names are read from the configured canon, not written here.  The
    check runs on the supplied spelling **and** its resolved primary, so an
    alias cannot launder a reserved name.
    """
    raw = _name(actor, "actor")
    if raw == "all":
        raise InvalidSubmission("actor cannot be \"all\": name the sender identity")
    name = _resolve(db, raw, "public_mail.actor")
    if _reserved_actor(db, raw) or _reserved_actor(db, name):
        raise ScopeRefused(
            "actor %s is a sender identity reserved by the core: nothing "
            "submitted" % (name if raw == name else "%s (%s)" % (raw, name)))
    if not (os.environ.get("AGENT_MAIL_NAME") or os.environ.get("AMEESH_RUNNER_ID")):
        return name
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        return name
    from .. import identity

    binding = identity.resolve_binding(cfg, db)
    if not binding.ok:
        raise ScopeRefused(
            "the declared process identity is not verified (%s): nothing submitted"
            % (binding.reason or "unbound"))
    if binding.name != name:
        raise ScopeRefused(
            "actor %s is not the verified process identity %s: nothing submitted"
            % (name, binding.name))
    return name


def _sender(db: Db, name: str) -> str:
    """A registered sender must be live; a service identity may stay unknown.

    The reserved-identity guard is repeated here as the last line of defence:
    ``_actor`` already refused those names before any preparation, but a future
    caller of this helper must not be able to deposit under one.
    """
    if _reserved_actor(db, name):
        raise ScopeRefused(
            "sender %s is a sender identity reserved by the core: nothing "
            "submitted" % name)
    row = storage.of(db).agents.get(name)
    if row is None:
        return name
    if row.get("retired_ts"):
        raise ScopeRefused(
            "sender %s is retired: nothing submitted" % name)
    if row.get("status") == "stopped":
        raise ScopeRefused(
            "sender %s is stopped: nothing submitted" % name)
    return name


def _recipient(db: Db, value: Any) -> tuple[str, dict]:
    name = _resolve(db, _name(value, "recipient"), "public_mail.recipient")
    row = storage.of(db).agents.get(name)
    if row is None:
        raise RecipientRefused(
            "unknown recipient %s: nothing deposited" % name)
    if row.get("retired_ts"):
        raise RecipientRefused(
            "recipient %s is retired: nothing deposited" % name)
    return name, row


def _body(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidSubmission("body must be a string")
    body = value.strip()
    if not body:
        raise InvalidSubmission("body is empty")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise InvalidSubmission(
            "body is larger than %d bytes" % MAX_BODY_BYTES)
    reason = fil.unreadable_reason(body)
    if reason:
        raise InvalidSubmission("body is not readable text — %s" % reason)
    return body


def _payload(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidSubmission("payload must be a JSON object")
    for key in value:
        if not isinstance(key, str) or not key:
            raise InvalidSubmission("payload keys must be non-empty strings")
    try:
        text = _canonical(dict(value))
    except (TypeError, ValueError) as exc:
        raise InvalidSubmission(
            "payload is not a bounded JSON object: %s" % exc)
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise InvalidSubmission(
            "payload is larger than %d bytes" % MAX_PAYLOAD_BYTES)
    payload = json.loads(text)
    # Validate the serialized object, not only the caller's mapping: a custom
    # Mapping could hide a key from iteration while ``dict()`` keeps it, and the
    # core reads the stored JSON, never the caller's object.
    for key in payload:
        if key in RESERVED_PAYLOAD_KEYS:
            raise InvalidSubmission(
                "payload key %r is reserved by the core" % key)
        if key not in ADMITTED_PAYLOAD_KEYS:
            raise InvalidSubmission(
                "payload key %r is not admitted: module data goes under "
                "payload[%r], the imposed module namespace"
                % (key, MODULE_PAYLOAD_KEY))
    if MODULE_PAYLOAD_KEY in payload and not isinstance(
            payload[MODULE_PAYLOAD_KEY], dict):
        raise InvalidSubmission(
            "payload[%r] must be a JSON object" % MODULE_PAYLOAD_KEY)
    attribution = payload.get("attribution")
    if attribution is not None:
        try:
            normalized = cost_attribution.normalize(attribution)
        except cost_attribution.AttributionError as exc:
            raise InvalidSubmission("payload.attribution: %s" % exc)
        if normalized is None:
            payload.pop("attribution", None)
        else:
            payload["attribution"] = normalized
    return payload


def _work_item(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidSubmission("work_item_id must be a lot number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise InvalidSubmission("work_item_id must be a positive lot number")
    if number <= 0:
        raise InvalidSubmission("work_item_id must be a positive lot number")
    return str(number)


def _key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSubmission("idempotency_key must be a string")
    key = value.strip()
    if not _KEY_RE.match(key):
        raise InvalidSubmission(
            "invalid idempotency_key: %r (letters, digits, . _ : -, "
            "200 characters max)" % value)
    return key


def _prepare(db: Db, actor: str, submission: Any) -> _Prepared:
    if not isinstance(submission, Submission):
        raise InvalidSubmission("submission must be a public mail Submission")
    _sender(db, actor)
    recipient, _row = _recipient(db, submission.recipient)
    if submission.kind not in KINDS:
        raise InvalidSubmission(
            "invalid kind: %r (%s)" % (submission.kind, "|".join(KINDS)))
    if not isinstance(submission.thread, bool):
        raise InvalidSubmission("thread must be true or false")
    return _Prepared(
        actor=actor,
        recipient=recipient,
        body=_body(submission.body),
        kind=submission.kind,
        payload=_payload(submission.payload),
        work_item_id=_work_item(submission.work_item_id),
        thread=submission.thread,
        idempotency_key=_key(submission.idempotency_key),
    )


def _lease(value: Any) -> Lease:
    """Validate the caller's native lease claim (rechecked against the registry)."""
    if not isinstance(value, Lease):
        raise InvalidSubmission("lease must be a public mail Lease")
    owner = value.owner
    if not isinstance(owner, str):
        raise InvalidSubmission("lease.owner must be a string")
    owner = owner.strip()
    if not owner or len(owner) > 200 or any(ch.isspace() or not ch.isprintable()
                                            for ch in owner):
        raise InvalidSubmission(
            "lease.owner must be a non-empty runner id without whitespace")
    epoch = value.epoch
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise InvalidSubmission("lease.epoch must be a positive integer")
    return Lease(owner=owner, epoch=epoch)


def _request_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidSubmission("request_id must be a positive mailbox id")
    return value


def _prepare_reply(reply: Any) -> _PreparedReply:
    if not isinstance(reply, Reply):
        raise InvalidSubmission("reply must be a public mail Reply")
    if not isinstance(reply.thread, bool):
        raise InvalidSubmission("thread must be true or false")
    return _PreparedReply(
        body=_body(reply.body),
        payload=_payload(reply.payload),
        thread=reply.thread,
    )


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

def _row_of(db: Db, message_id: int) -> dict:
    row = storage.of(db).mailbox.get(int(message_id))
    if row is None:
        raise PublicMailError(
            "message %s disappeared after deposit" % message_id)
    return row


def _same_content(row: dict, prepared: _Prepared) -> bool:
    if (row.get("body") or "") != prepared.body:
        return False
    if (row.get("kind") or "") != prepared.kind:
        return False
    if (row.get("work_item_id") or "") != (prepared.work_item_id or ""):
        return False
    stored = dict(row.get("payload") or {})
    # The core stores the scoped idempotency key beside the module payload;
    # it is not part of the content the caller supplied.
    stored.pop("idempotency_key", None)
    try:
        return _canonical(stored) == _canonical(prepared.payload)
    except (TypeError, ValueError):
        return False


def _receipt(row: dict, *, reused: bool) -> Receipt:
    replied_to = row.get("in_reply_to")
    return Receipt(
        id=int(row["id"]),
        actor=str(row.get("sender") or ""),
        recipient=str(row.get("recipient") or ""),
        kind=str(row.get("kind") or ""),
        created_ts=float(row.get("created_ts") or 0.0),
        reused=bool(reused),
        in_reply_to=None if replied_to is None else int(replied_to),
    )


def _existing(db: Db, prepared: _Prepared) -> Receipt | None:
    """The already deposited submission for this key, checked for content."""
    mailbox = storage.of(db).mailbox
    message_id = mailbox.by_idempotency_key(
        prepared.actor, prepared.recipient, prepared.idempotency_key)
    if message_id is None:
        return None
    row = mailbox.get(int(message_id))
    if row is None:
        return None
    if not _same_content(row, prepared):
        raise IdempotencyConflict(
            "idempotency key %r already names message %s with another content"
            % (prepared.idempotency_key, message_id))
    return _receipt(row, reused=True)


def _submit_one(db: Db, prepared: _Prepared) -> Receipt:
    if prepared.idempotency_key is not None:
        existing = _existing(db, prepared)
        if existing is not None:
            return existing
    payload = dict(prepared.payload)
    if prepared.idempotency_key is not None:
        # Keep the core pre-check of ``mail.send`` out of this path: the unique
        # index must be the only authority.  With the key passed as a
        # parameter, ``mail.send`` could return an older row on its own body+
        # lot comparison and hide a payload conflict; here every replay goes
        # to the insert and its conflict is compared below.
        payload["idempotency_key"] = prepared.idempotency_key
    cfg = getattr(db, "cfg", None)
    try:
        message_id = mail.send(
            db, prepared.actor, prepared.recipient, prepared.body,
            host=getattr(cfg, "host", None), kind=prepared.kind,
            payload=payload, work_item_id=prepared.work_item_id,
            thread=prepared.thread)
    except DbError as exc:
        if (prepared.idempotency_key is None
                or not is_unique_violation(exc, _IDEMPOTENCY_INDEX)):
            raise
        # A concurrent retry committed first.  Re-read and compare: identical
        # content reuses its id, different content is the deterministic
        # conflict the contract promises.
        existing = _existing(db, prepared)
        if existing is None:
            raise
        return existing
    except storage.RecipientRetired as exc:
        raise RecipientRefused(str(exc)) from exc
    except fil.UnreadableBody as exc:
        raise InvalidSubmission(str(exc)) from exc
    if message_id is None:
        raise PublicMailError("the core mailbox refused the submission")
    return _receipt(_row_of(db, int(message_id)), reused=False)


# ---------------------------------------------------------------------------
# public operations
# ---------------------------------------------------------------------------

def submit(db: Db, actor: str, submission: Submission) -> Receipt:
    """Deposit one typed message for ``actor`` and return its durable receipt.

    Idempotency is scoped to ``(actor, recipient)``.  The same key and the
    same content (body, kind, payload, lot) return the same id with
    ``reused=True``; the same key with other content raises
    :class:`IdempotencyConflict`; concurrent retries leave one durable row.
    """
    _direct(db)
    name = _actor(db, actor)
    return _submit_one(db, _prepare(db, name, submission))


def submit_many(db: Db, actor: str, submissions: Sequence[Submission]) -> BatchResult:
    """Deposit up to :data:`MAX_BATCH` submissions, bounded and non-atomic.

    Each item commits or is refused on its own, in input order; a refusal is
    reported in its :class:`SubmissionResult` and does not hide the other
    outcomes.  Exceeding :data:`MAX_BATCH` refuses the whole call before
    anything is deposited.
    """
    _direct(db)
    if isinstance(submissions, (str, bytes)) or not isinstance(submissions, Sequence):
        raise InvalidSubmission("submissions must be a sequence of Submission")
    items = tuple(submissions)
    if len(items) > MAX_BATCH:
        raise InvalidSubmission(
            "at most %d submissions per batch (got %d)" % (MAX_BATCH, len(items)))
    name = _actor(db, actor)
    results: list[SubmissionResult] = []
    for index, item in enumerate(items):
        try:
            receipt = _submit_one(db, _prepare(db, name, item))
        except PublicMailError as exc:
            results.append(SubmissionResult(
                index=index, error=exc.code, detail=str(exc)))
        else:
            results.append(SubmissionResult(index=index, receipt=receipt))
    return BatchResult(tuple(results))


def find(db: Db, actor: str, recipient: str, idempotency_key: str) -> Receipt | None:
    """The durable receipt of ``actor``'s submission under this key, or None.

    The lookup is scoped to the same ``(actor, recipient)`` pair as the
    submission; a key used by another actor or for another recipient is not
    visible here.
    """
    _direct(db)
    name = _actor(db, actor)
    to = _resolve(db, _name(recipient, "recipient"), "public_mail.find")
    key = _key(idempotency_key)
    if key is None:
        raise InvalidSubmission("idempotency_key is required")
    mailbox = storage.of(db).mailbox
    message_id = mailbox.by_idempotency_key(name, to, key)
    if message_id is None:
        return None
    row = mailbox.get(int(message_id))
    if row is None or str(row.get("sender") or "") != name:
        return None
    return _receipt(row, reused=True)


def _history_entry(row: dict) -> HistoryEntry:
    payload = row.get("payload")
    replied_to = row.get("in_reply_to")
    return HistoryEntry(
        id=int(row["id"]),
        sender=str(row.get("sender") or ""),
        recipient=str(row.get("recipient") or ""),
        body=str(row.get("body") or ""),
        kind=str(row.get("kind") or ""),
        payload=dict(payload) if isinstance(payload, Mapping) else {},
        work_item_id=str(row.get("work_item_id") or ""),
        created_ts=float(row.get("created_ts") or 0.0),
        delivered_ts=(None if row.get("delivered_ts") is None
                      else float(row["delivered_ts"])),
        in_reply_to=None if replied_to is None else int(replied_to),
    )


def _instant(value: Any, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSubmission("%s must be an epoch timestamp" % what)
    value = float(value)
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        raise InvalidSubmission("%s must be a finite positive timestamp" % what)
    return value


def history(db: Db, actor: str, *, peer: str | None = None,
            after_id: int | None = None, limit: int = DEFAULT_HISTORY,
            since_ts: float | None = None, until_ts: float | None = None,
            kind: str | None = None) -> HistoryPage:
    """One bounded page of the messages ``actor`` sent or received.

    Entries are ordered by durable id, oldest first, and start after
    ``after_id`` (exclusive).  ``peer`` restricts the page to the two-party
    conversation between the actor and that agent; without it the page is the
    actor's whole participation.  ``limit`` is bounded by
    :data:`MAX_HISTORY`.  ``since_ts``/``until_ts`` bound the message creation
    time.  The returned ``next_cursor`` is stable across new mail and is the
    value to pass back as ``after_id``.
    """
    _direct(db)
    name = _actor(db, actor)
    if peer is not None:
        peer = _resolve(db, _name(peer, "peer"), "public_mail.history")
    if after_id is not None:
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise InvalidSubmission("after_id must be a non-negative id")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidSubmission("limit must be an integer")
    if limit < 1 or limit > MAX_HISTORY:
        raise InvalidSubmission(
            "limit must be between 1 and %d" % MAX_HISTORY)
    since = _instant(since_ts, "since_ts")
    until = _instant(until_ts, "until_ts")
    if since is not None and until is not None and until < since:
        raise InvalidSubmission("until_ts is before since_ts")
    if kind is not None and kind not in KINDS:
        raise InvalidSubmission(
            "invalid kind: %r (%s)" % (kind, "|".join(KINDS)))
    rows = storage.of(db).mailbox.history_page(
        name, peer=peer, after_id=after_id, limit=limit,
        since_ts=since, until_ts=until, kind=kind)
    has_more = len(rows) > limit
    rows = rows[:limit]
    entries = tuple(_history_entry(row) for row in rows)
    cursor = entries[-1].id if entries else after_id
    return HistoryPage(entries=entries, next_cursor=cursor, has_more=has_more)


def _pending_entry(row: dict) -> PendingRequest:
    payload = row.get("payload")
    return PendingRequest(
        id=int(row["id"]),
        sender=str(row.get("sender") or ""),
        recipient=str(row.get("recipient") or ""),
        body=str(row.get("body") or ""),
        payload=dict(payload) if isinstance(payload, Mapping) else {},
        work_item_id=str(row.get("work_item_id") or ""),
        created_ts=float(row.get("created_ts") or 0.0),
        delivered_ts=(None if row.get("delivered_ts") is None
                      else float(row["delivered_ts"])),
        turn_id=str(row.get("turn_id") or ""),
    )


def pending(db: Db, actor: str, lease: Lease, *, sender: str | None = None,
            limit: int = DEFAULT_PENDING) -> PendingPage:
    """Requests held for ``actor`` by this live lease and its native turn.

    Only requests reserved as the prompt of the current turn
    (``meta.consigne``) under the live lease and the current accounting marker
    (``spend_pending``) are returned, oldest first; an unanswered request
    delivered in an earlier turn is not current and never appears.  The
    native spend marker stays authoritative: this call only observes it, it
    never reserves, settles or spends.  A stale or replaced lease raises
    :class:`ScopeRefused`; an absent current-turn marker yields an empty page.
    ``sender`` restricts the page to requests from that agent; ``limit`` is
    bounded by :data:`MAX_PENDING`.
    """
    _direct(db)
    name = _actor(db, actor)
    claim = _lease(lease)
    if sender is not None:
        sender = _resolve(db, _name(sender, "sender"), "public_mail.pending")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidSubmission("limit must be an integer")
    if limit < 1 or limit > MAX_PENDING:
        raise InvalidSubmission(
            "limit must be between 1 and %d" % MAX_PENDING)
    ok, reason, _row = registry.lease_agent(db, name, claim.owner, claim.epoch)
    if not ok:
        raise ScopeRefused(
            "lease refused for %s: %s" % (name, reason))
    rows = storage.of(db).mailbox.pending_requests(
        name, claim.owner, claim.epoch, sender=sender, limit=limit)
    has_more = len(rows) > limit
    return PendingPage(
        entries=tuple(_pending_entry(row) for row in rows[:limit]),
        has_more=has_more,
    )


def _same_reply(row: dict, prepared: _PreparedReply) -> bool:
    if (row.get("body") or "") != prepared.body:
        return False
    stored = dict(row.get("payload") or {})
    try:
        return _canonical(stored) == _canonical(prepared.payload)
    except (TypeError, ValueError):
        return False


def _correlated(replies: Sequence[dict], prepared: _PreparedReply) -> Receipt:
    """The durable receipt of the one reply, after checking its content."""
    if not replies:
        raise PublicMailError(
            "the correlated reply disappeared after its insert")
    existing = replies[0]
    if not _same_reply(existing, prepared):
        raise ReplyConflict(
            "request %s already has another reply (message %s)"
            % (existing.get("in_reply_to"), existing.get("id")))
    return _receipt(existing, reused=True)


def _check_reply_scope(request: dict, name: str, claim: Lease) -> None:
    """Refuse a request that is not this lease's live current-turn prompt."""
    ident = request.get("id")
    if (request.get("kind") or "") != "request":
        raise ScopeRefused("message %s is not a request" % ident)
    if (request.get("recipient") or "") != name:
        raise ScopeRefused("request %s is not addressed to %s" % (ident, name))
    if request.get("withdrawn"):
        raise ScopeRefused("request %s was withdrawn" % ident)
    if not request.get("marker_present"):
        raise ScopeRefused(
            "no live accounting marker: request %s is not held by a turn" % ident)
    if (request.get("consigne_runner") or "") != claim.owner:
        raise ScopeRefused(
            "request %s is held by %s, not %s"
            % (ident, request.get("consigne_runner") or "nobody", claim.owner))
    if str(request.get("consigne_epoch") or "") != str(claim.epoch):
        raise ScopeRefused(
            "request %s was reserved at epoch %s, not %s"
            % (ident, request.get("consigne_epoch"), claim.epoch))
    if (not request.get("consigne_turn")
            or request.get("consigne_turn") != request.get("marker_turn_id")):
        raise ScopeRefused(
            "request %s is not the prompt of the current native turn" % ident)
    delivered = request.get("delivered_ts")
    marker = request.get("marker_created_ts")
    if delivered is not None and (marker is None or float(delivered) < float(marker)):
        raise ScopeRefused(
            "request %s was delivered before the current turn" % ident)


def reply(db: Db, actor: str, request_id: int, answer: Reply, *,
          lease: Lease) -> Receipt:
    """Answer one pending request under the caller's live lease.

    The request must be addressed to ``actor`` and be the current native
    turn's prompt under the same live ``(owner, epoch)`` lease; an expired or
    replaced lease, an old delivery or a withdrawn request raises
    :class:`ScopeRefused`.  The reply is deposited to the request's sender,
    linked to the request's lot, and correlated by ``in_reply_to``; one
    request accepts one reply.  An identical retry returns the durable
    receipt with ``reused=True``; another body or payload raises
    :class:`ReplyConflict`.  The native spend marker and reservation remain
    authoritative: this surface never reserves or settles spend.
    """
    _direct(db)
    name = _actor(db, actor)
    claim = _lease(lease)
    ident = _request_id(request_id)
    prepared = _prepare_reply(answer)
    cfg = getattr(db, "cfg", None)
    try:
        with db.transaction() as tx:
            ok, reason, _row = registry.lease_agent_locked(
                tx, name, claim.owner, claim.epoch)
            if not ok:
                raise ScopeRefused(
                    "lease refused for %s: %s" % (name, reason))
            target = storage.of(tx).mailbox.reply_target(ident)
            if target is None:
                raise ScopeRefused(
                    "request %s is not pending for %s" % (ident, name))
            request = target["request"]
            _check_reply_scope(request, name, claim)
            if target["replies"]:
                return _correlated(target["replies"], prepared)
            try:
                message_id = mail.send(
                    tx, name, str(request.get("sender") or ""), prepared.body,
                    host=getattr(cfg, "host", None), kind="reply",
                    payload=prepared.payload,
                    work_item_id=(request.get("work_item_id") or None),
                    thread=prepared.thread, in_reply_to=ident)
            except storage.RecipientRetired as exc:
                raise RecipientRefused(str(exc)) from exc
            except fil.UnreadableBody as exc:
                raise InvalidSubmission(str(exc)) from exc
            if message_id is None:
                raise PublicMailError("the core mailbox refused the reply")
            return _receipt(_row_of(tx, int(message_id)), reused=False)
    except DbError as exc:
        if not is_unique_violation(exc, _REPLY_INDEX):
            raise
    # A concurrent writer committed the one reply between our read and our
    # insert.  Re-read outside the aborted transaction: identical content
    # reuses its id, different content is the deterministic conflict.
    target = storage.of(db).mailbox.reply_target(ident)
    if target is None:
        raise ScopeRefused(
            "request %s is not pending for %s" % (ident, name))
    return _correlated(target["replies"], prepared)


__all__ = [
    "CONTRACT_VERSION", "MAX_BODY_BYTES", "MAX_PAYLOAD_BYTES", "MAX_BATCH",
    "MAX_HISTORY", "DEFAULT_HISTORY", "MAX_PENDING", "DEFAULT_PENDING",
    "KINDS", "RESERVED_PAYLOAD_KEYS", "MODULE_PAYLOAD_KEY",
    "ADMITTED_PAYLOAD_KEYS", "RESERVED_ACTORS", "RESERVED_ACTOR_PREFIXES",
    "reserved_actors",
    "Submission", "Receipt", "HistoryEntry", "HistoryPage", "SubmissionResult",
    "BatchResult", "Lease", "Reply", "PendingRequest", "PendingPage",
    "PublicMailError", "InvalidSubmission", "IdempotencyConflict",
    "ReplyConflict", "ScopeRefused", "RecipientRefused", "UnsupportedStorage",
    "submit", "submit_many", "find", "history", "pending", "reply",
]
