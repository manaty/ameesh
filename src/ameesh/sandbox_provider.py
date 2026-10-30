# SPDX-License-Identifier: AGPL-3.0-only
"""Job-provider contract for a bounded external-sandbox turn (#2202 S4a).

The sandbox supervisor of S4b owns one Kubernetes Job per turn. This module
fixes the boundary between that supervisor and the cluster:

* :class:`JobIdentity` derives the deterministic Job name from the turn id
  (``ameesh-<first 12 hex of sha256(turn_id)>``) and the five ``ameesh.dev/*``
  labels of the design (section 3). A retried create cannot produce two Jobs:
  the name is unique in the namespace, and a conflict is resolved by reading
  the existing Job;
* :class:`JobSpec` is the admitted execution envelope: pinned image digest,
  argv, Job deadline and TTL. It carries **no secret**: the prompt and the
  relay token are delivered outside this interface, never in the Job argv;
* :class:`JobProvider` is the five-call interface (create, get, list, logs,
  delete). A real Kubernetes adapter is S4b's scope; this module ships none;
* :class:`FakeJobProvider` is the in-memory double used by the supervisor and
  crash-window tests. It is deterministic, thread-safe and never contacts a
  cluster.

No S1 table, no database, no cluster credential and no secret is read or
written here. The provider only models the Job facts the supervisor needs:
namespace/name/UID, labels, phase, image, timestamps, message and logs.

Phases use the Kubernetes Job vocabulary (``Pending``, ``Running``,
``Succeeded``, ``Failed``). A Job is terminal exactly when it succeeded or
failed; a Job with no reported phase yet is live.
"""
from __future__ import annotations

import abc
import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Optional

#: prefix of every sandbox Job name
JOB_NAME_PREFIX = "ameesh-"

#: number of hex characters of SHA-256(turn_id) kept in the name
JOB_NAME_HEX = 12

#: Kubernetes label keys of the correlation, exactly as designed in section 3
LABEL_TURN_ID = "ameesh.dev/turn-id"
LABEL_AGENT = "ameesh.dev/agent"
LABEL_EPOCH = "ameesh.dev/epoch"
LABEL_PLACEMENT = "ameesh.dev/placement"
LABEL_PROFILE = "ameesh.dev/profile"

#: Kubernetes Job phases
PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_SUCCEEDED = "Succeeded"
PHASE_FAILED = "Failed"

PHASES = frozenset((PHASE_PENDING, PHASE_RUNNING, PHASE_SUCCEEDED, PHASE_FAILED))

#: only these phases release the concurrency slot of the turn
TERMINAL_PHASES = frozenset((PHASE_SUCCEEDED, PHASE_FAILED))

#: Kubernetes limit for a label value
MAX_LABEL_VALUE = 63

#: Kubernetes limit for a namespace / DNS-1123 label
MAX_DNS_LABEL = 63

_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
#: an image reference pinned by digest (a tag is not enough)
_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


class InvalidJob(ValueError):
    """A local Job value (identity, spec or observation) is not usable.

    This is a programming error: the supervisor builds these values from the
    admitted placement, so an invalid one is refused before any provider call.
    """


class ProviderError(Exception):
    """A provider call failed.

    ``code`` is stable and may be logged; ``retryable`` tells the supervisor
    whether calling the same operation again may succeed. Nothing in this
    module writes a secret to the message.
    """

    code = "provider-error"
    retryable = False

    def __init__(self, message: str = "", *, name: Optional[str] = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.name = name


class JobAlreadyExists(ProviderError):
    """``create`` found a Job with the same namespace/name (HTTP 409)."""

    code = "already-exists"


class JobNotFound(ProviderError):
    """``get``/``logs``/``delete`` named a Job that does not exist."""

    code = "not-found"


class ProviderForbidden(ProviderError):
    """The provider refused the call (bad or missing credential); retrying fails."""

    code = "forbidden"


class ProviderUnavailable(ProviderError):
    """The provider or the cluster is temporarily unreachable; retry later."""

    code = "unavailable"
    retryable = True


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InvalidJob("%s must be a non-empty string" % name)
    return value


def _label_value(name: str, value: object) -> str:
    _text(name, value)
    if len(value) > MAX_LABEL_VALUE or not _LABEL_VALUE.match(value):
        raise InvalidJob(
            "%s is not a valid Kubernetes label value: %r" % (name, value))
    return value


def _dns_label(name: str, value: object) -> str:
    _text(name, value)
    if len(value) > MAX_DNS_LABEL or not _DNS_LABEL.match(value):
        raise InvalidJob("%s is not a valid DNS-1123 label: %r" % (name, value))
    return value


def _epoch(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidJob("epoch must be a non-negative integer")
    return value


def _seconds(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidJob("%s must be an integer >= %d" % (name, minimum))
    return value


def _timestamp(name: str, value: object, *, optional: bool = False) -> Optional[float]:
    if value is None:
        if optional:
            return None
        raise InvalidJob("%s is required" % name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidJob("%s must be an epoch number" % name)
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise InvalidJob("%s must be a finite non-negative epoch" % name)
    return number


def job_name(turn_id: str) -> str:
    """The deterministic Job name of a turn.

    ``ameesh-<first 12 hex of sha256(turn_id)>`` as fixed by the design. The
    derivation only depends on the turn id: a supervisor restart, a retried
    create or a second supervisor all address the same Job. Names are unique
    in a namespace, so two Jobs can never exist for one turn.
    """
    _text("turn_id", turn_id)
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    return JOB_NAME_PREFIX + digest[:JOB_NAME_HEX]


def job_labels(turn_id: str, agent: str, epoch: int, placement: str,
               profile: str) -> dict[str, str]:
    """The five ``ameesh.dev/*`` labels of a sandbox Job.

    Values are validated with the Kubernetes label-value grammar; a value
    that cannot be represented is refused instead of silently truncated or
    escaped. ``epoch`` is rendered in decimal.
    """
    _epoch(epoch)
    return {
        LABEL_TURN_ID: _label_value("turn-id", turn_id),
        LABEL_AGENT: _label_value("agent", agent),
        LABEL_EPOCH: _label_value("epoch", str(epoch)),
        LABEL_PLACEMENT: _label_value("placement", placement),
        LABEL_PROFILE: _label_value("profile", profile),
    }


@dataclass(frozen=True)
class JobIdentity:
    """The deterministic identity of one Job.

    ``namespace`` is the placement namespace (the Job lives there), the name
    is derived from ``turn_id`` and the labels carry the correlation fields.
    The namespace is part of the identity because a name is only unique
    inside one namespace.
    """

    turn_id: str
    agent: str
    epoch: int
    placement: str
    profile: str
    namespace: str

    def __post_init__(self) -> None:
        _label_value("turn-id", self.turn_id)
        _label_value("agent", self.agent)
        _epoch(self.epoch)
        _label_value("placement", self.placement)
        _label_value("profile", self.profile)
        _dns_label("namespace", self.namespace)

    @property
    def name(self) -> str:
        """The deterministic Job name (``ameesh-<12 hex>``)."""
        return job_name(self.turn_id)

    @property
    def labels(self) -> dict[str, str]:
        """The five ``ameesh.dev/*`` labels of this Job."""
        return job_labels(self.turn_id, self.agent, self.epoch,
                          self.placement, self.profile)


@dataclass(frozen=True)
class JobSpec:
    """The execution envelope of one sandbox Job.

    ``active_deadline_seconds`` and ``ttl_seconds_after_finished`` come from
    the admitted Host placement; they have no compiled default here. The TTL
    must be positive: a zero TTL would delete the Job before a restarted
    supervisor can adopt it and read its result.

    ``argv`` overrides the image entrypoint when non-empty. It must never
    contain a prompt, a relay token, a provider key or any other secret; those
    travel outside this contract.
    """

    identity: JobIdentity
    image: str
    active_deadline_seconds: int
    ttl_seconds_after_finished: int
    argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, JobIdentity):
            raise InvalidJob("identity must be a JobIdentity")
        if (not isinstance(self.image, str)
                or not _PINNED_IMAGE.match(self.image)):
            raise InvalidJob(
                "image must be pinned by digest (registry/name@sha256:<64 hex>)")
        argv = self.argv
        if isinstance(argv, str) or not isinstance(argv, Sequence):
            raise InvalidJob("argv must be a sequence of strings")
        argv = tuple(argv)
        for index, argument in enumerate(argv):
            if not isinstance(argument, str) or not argument:
                raise InvalidJob(
                    "argv[%d] must be a non-empty string" % index)
        object.__setattr__(self, "argv", argv)
        _seconds("active_deadline_seconds", self.active_deadline_seconds,
                 minimum=1)
        _seconds("ttl_seconds_after_finished",
                 self.ttl_seconds_after_finished, minimum=1)


@dataclass(frozen=True)
class Job:
    """A Job as observed from the provider.

    Immutable snapshot: a later observation is a fresh object. ``phase`` is
    ``None`` when the cluster has not reported a phase yet. ``message`` is the
    Job's status/termination message, never a secret.
    """

    namespace: str
    name: str
    uid: str
    labels: Mapping[str, str]
    phase: Optional[str]
    image: str
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    message: str = ""

    def __post_init__(self) -> None:
        _dns_label("namespace", self.namespace)
        _text("name", self.name)
        _text("uid", self.uid)
        if not isinstance(self.labels, Mapping):
            raise InvalidJob("labels must be a mapping")
        labels = {}
        for key, value in self.labels.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise InvalidJob("labels must map strings to strings")
            labels[key] = value
        object.__setattr__(self, "labels", labels)
        if self.phase is not None and self.phase not in PHASES:
            raise InvalidJob("unknown Job phase: %r" % (self.phase,))
        if (not isinstance(self.image, str)
                or not _PINNED_IMAGE.match(self.image)):
            raise InvalidJob("job image must be pinned by digest")
        object.__setattr__(
            self, "created_at", _timestamp("created_at", self.created_at))
        object.__setattr__(
            self, "started_at", _timestamp("started_at", self.started_at,
                                           optional=True))
        object.__setattr__(
            self, "completed_at", _timestamp("completed_at", self.completed_at,
                                             optional=True))
        if not isinstance(self.message, str):
            raise InvalidJob("message must be a string")

    @property
    def terminal(self) -> bool:
        """True exactly for ``Succeeded`` and ``Failed``."""
        return self.phase in TERMINAL_PHASES


class JobProvider(abc.ABC):
    """The five operations the sandbox supervisor needs from a cluster.

    Contract shared by the Kubernetes adapter (S4b) and
    :class:`FakeJobProvider`:

    * ``create`` creates exactly the Job named by ``spec.identity``; a name
      conflict raises :class:`JobAlreadyExists` and never replaces the
      existing Job. A transient failure raises :class:`ProviderUnavailable`.
    * ``get`` returns the current observation, or ``None`` when no Job has
      that namespace/name. Absence is not an error.
    * ``list`` returns every Job of the namespace whose labels contain all
      the given pairs, sorted by name. An empty/absent filter lists all.
    * ``logs`` returns the Job's current log text. The caller bounds and
      validates it; this interface never returns a secret and never writes one.
    * ``delete`` asks for deletion and returns ``True`` when a Job existed,
      ``False`` when it was already absent. It does not wait: the supervisor
      observes termination with ``get``.

    Providers are not required to be thread-safe; the supervisor serializes
    its calls. The fake is, so concurrent tests are possible.
    """

    @abc.abstractmethod
    def create(self, spec: JobSpec) -> Job:
        """Create the Job of ``spec.identity`` and return its observation."""
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, namespace: str, name: str) -> Optional[Job]:
        """Return the Job, or ``None`` if it does not exist."""
        raise NotImplementedError

    @abc.abstractmethod
    def list(self, namespace: str,
             labels: Optional[Mapping[str, str]] = None) -> list[Job]:
        """List the namespace's Jobs matching every label, sorted by name."""
        raise NotImplementedError

    @abc.abstractmethod
    def logs(self, namespace: str, name: str) -> str:
        """Return the Job's log text; raise :class:`JobNotFound` if absent."""
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, namespace: str, name: str) -> bool:
        """Request deletion; return whether a Job existed. Do not wait."""
        raise NotImplementedError


@dataclass
class _Entry:
    job: Job
    logs: list[str]


class FakeJobProvider(JobProvider):
    """An in-memory :class:`JobProvider` for tests; no cluster, no credential.

    ``create`` starts a Job in ``Pending``; tests drive the lifecycle with
    :meth:`set_phase` and :meth:`append_log`. UIDs are unique per provider
    instance and change when a Job is deleted and recreated under the same
    name, so a test can distinguish a re-created Job from an adopted one.
    ``clock`` is injectable to keep timestamps deterministic.
    """

    def __init__(self, *, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock if clock is not None else time.time
        self._jobs: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()
        self._uid = 0

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _snapshot(job: Job) -> Job:
        # A caller can mutate the returned label dict; the stored Job must not
        # change. Rebuilding through the dataclass also re-validates the copy.
        return replace(job, labels=dict(job.labels))

    def create(self, spec: JobSpec) -> Job:
        if not isinstance(spec, JobSpec):
            raise InvalidJob("create() requires a JobSpec")
        key = (spec.identity.namespace, spec.identity.name)
        with self._lock:
            if key in self._jobs:
                raise JobAlreadyExists(
                    "job %s/%s already exists" % key, name=spec.identity.name)
            self._uid += 1
            job = Job(
                namespace=spec.identity.namespace,
                name=spec.identity.name,
                uid="fake-uid-%d" % self._uid,
                labels=spec.identity.labels,
                phase=PHASE_PENDING,
                image=spec.image,
                created_at=self._now(),
            )
            self._jobs[key] = _Entry(job=job, logs=[])
            return self._snapshot(job)

    def get(self, namespace: str, name: str) -> Optional[Job]:
        with self._lock:
            entry = self._jobs.get((namespace, name))
            return None if entry is None else self._snapshot(entry.job)

    def list(self, namespace: str,
             labels: Optional[Mapping[str, str]] = None) -> list[Job]:
        if labels is not None and not isinstance(labels, Mapping):
            raise InvalidJob("labels must be a mapping")
        wanted = dict(labels or {})
        with self._lock:
            jobs = [
                entry.job
                for (job_namespace, _), entry in self._jobs.items()
                if job_namespace == namespace
                and all(entry.job.labels.get(key) == value
                        for key, value in wanted.items())
            ]
            return [self._snapshot(job)
                    for job in sorted(jobs, key=lambda item: item.name)]

    def logs(self, namespace: str, name: str) -> str:
        with self._lock:
            entry = self._jobs.get((namespace, name))
            if entry is None:
                raise JobNotFound(
                    "job %s/%s does not exist" % (namespace, name), name=name)
            return "".join(entry.logs)

    def delete(self, namespace: str, name: str) -> bool:
        with self._lock:
            return self._jobs.pop((namespace, name), None) is not None

    # -- test helpers (not part of the provider contract) -------------------

    def set_phase(self, namespace: str, name: str, phase: str, *,
                  message: Optional[str] = None) -> Job:
        """Move the Job to ``phase`` and update its timestamps.

        The first running/terminal phase sets ``started_at``; the first
        terminal phase sets ``completed_at``. ``message`` keeps the current
        message when omitted.
        """
        if phase not in PHASES:
            raise InvalidJob("unknown Job phase: %r" % (phase,))
        if message is not None and not isinstance(message, str):
            raise InvalidJob("message must be a string")
        with self._lock:
            entry = self._jobs.get((namespace, name))
            if entry is None:
                raise JobNotFound(
                    "job %s/%s does not exist" % (namespace, name), name=name)
            current = entry.job
            if current.terminal and phase != current.phase:
                raise InvalidJob(
                    "terminal Job %s/%s cannot change phase" % (namespace, name))
            now = self._now()
            started = current.started_at
            if started is None and phase in (
                    PHASE_RUNNING, PHASE_SUCCEEDED, PHASE_FAILED):
                started = now
            completed = current.completed_at
            if completed is None and phase in TERMINAL_PHASES:
                completed = now
            entry.job = replace(
                current,
                phase=phase,
                started_at=started,
                completed_at=completed,
                message=current.message if message is None else message,
            )
            return self._snapshot(entry.job)

    def append_log(self, namespace: str, name: str, text: str) -> str:
        """Append ``text`` to the Job log and return the full log."""
        if not isinstance(text, str):
            raise InvalidJob("log text must be a string")
        with self._lock:
            entry = self._jobs.get((namespace, name))
            if entry is None:
                raise JobNotFound(
                    "job %s/%s does not exist" % (namespace, name), name=name)
            entry.logs.append(text)
            return "".join(entry.logs)
