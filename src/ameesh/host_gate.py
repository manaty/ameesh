# SPDX-License-Identifier: AGPL-3.0-only
"""Host gate of the classic runner (lot L159f, study L159 sections 1.5 to
1.8): the runner obeys the state a capacity guard writes for its host.

Extracted from the L112 gate of path B (`lot/v170-voie-b`,
`mediated_executor/gate.py` and `host_gate.py`), without what only a
mediated device needs (socket gate, availability relay to the server). Path
B can later import this module instead of its own copy.

A guard (`ameesh guard`, L159e, or Nexlink Compute) writes
`ameesh-host-state/1` to `/run/ameesh-gate/state/state.json` (temporary
file, then `rename`). The runner reads it on inotify, else every 2 s, and
writes `ameesh-host-ack/1` to `/run/ameesh-gate/ack/ack.json`
(`docs/GARDE-DE-CAPACITE.md`, L159a):

* `available`: the runner claims, within `min(caps.max_concurrent,
  max_agents)`;
* `draining`: no new claim; each turn stops at its next safe point (the
  harness alone, as when a turn is closed), at the latest at
  `drain_deadline_ts` (else now + `host_gate_drain`, else the offer's
  `drain_seconds`, else 90 s); the background work of a turn keeps its
  end-of-turn grace, cut at that same deadline; leases are released; then,
  no lease and no background work left, the ack says `drained: true`;
* `stopped`: no turn; a running one is stopped at once;
* `freeze` (`{since_ts, until_ts}`, with `available`): the guard froze the
  agents' units; the runner keeps going, and the frozen time does not count
  in its turn deadlines (`frozen_between`);
* back to `available`: claims resume without a human.

Freshness is the rule of the contract (`capacity_guard.state_problem`,
section 5.3): on a lending host (Host `policy.lend`, or
`host_gate_fallback: stopped`) a state without a finite, near `expires_ts`
is stale. Unreadable, absent or stale: `stopped` on a lending host,
`available` on a classic host with a gate (`host_gate_fallback` overrides).
A lending host has a `file` gate by default; a classic host has none, and
nothing changes there.

The ack echoes the `seq` of the state it answers: the guard counts
`drained` only for the seq it is draining (L159e).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import dataclasses
import json
import math
import os
import select
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from .capacity_guard import (ACK_PATH, FALLBACK_CLASSIC, FALLBACK_LENDING, SCHEMA_HOST_ACK,
                             SCHEMA_HOST_STATE, STATE_PATH, STATES, state_problem)

GATE_KINDS = ("file",)
#: rereading the state without inotify (seconds)
FILE_POLL_S = 2.0
#: drain delay when the state has no `drain_deadline_ts`
DEFAULT_DRAIN_S = 90.0
#: agents at once when `caps.max_concurrent` is absent: no limit of the gate
#: (the host's `max_agents` still applies)
DEFAULT_MAX_CONCURRENT: Optional[int] = None
#: longest freeze counted when `freeze.until_ts` is absent (study 1.6)
FREEZE_MAX_S = 60.0
#: past the drain deadline, delay before the drain is said to be late
OVERDUE_GRACE_S = 30.0
#: longest reason published in a host reading (the guard's text is free)
REASON_MAX = 64
#: largest state read (a huge file is unreadable)
MAX_STATE_BYTES = 65536


def _number(value: Any) -> bool:
    """A finite number (`json` reads `NaN` and `Infinity`; a bool is no number)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


@dataclasses.dataclass(frozen=True)
class GateState:
    """`ameesh-host-state/1`, the fields the runner acts on. `seq` grows with
    each write of the guard."""

    state: str
    seq: int
    until_ts: Optional[float] = None
    drain_deadline_ts: Optional[float] = None
    caps: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    reason: str = "unknown"
    expires_ts: Optional[float] = None
    freeze: Optional[Mapping[str, Any]] = None

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError("unknown gate state: %r" % self.state)

    @property
    def may_claim(self) -> bool:
        return self.state == "available"

    @property
    def max_concurrent(self) -> Optional[int]:
        """`caps.max_concurrent`, else None (no limit of the gate)."""
        cap = self.caps.get("max_concurrent")
        if _number(cap) and int(cap) == cap and cap >= 0:
            return int(cap)
        return DEFAULT_MAX_CONCURRENT

    @property
    def freeze_span(self) -> Optional[tuple]:
        """`(since, end)` of the freeze this state announces (study 1.6, tier
        2), bounded to `FREEZE_MAX_S`; None without one. A freeze only goes
        with `available`."""
        if not self.freeze or self.state != "available":
            return None
        since = self.freeze.get("since_ts")
        if not _number(since):
            return None
        until = self.freeze.get("until_ts")
        end = until if _number(until) else since + FREEZE_MAX_S
        end = min(end, since + FREEZE_MAX_S)
        return (float(since), float(end)) if end > since else None

    def frozen_at(self, now: float) -> bool:
        """The guard froze the agents' units at `now`."""
        span = self.freeze_span
        return span is not None and span[0] <= now < span[1]

    def to_json(self) -> dict:
        body = {"schema": SCHEMA_HOST_STATE, "state": self.state, "seq": self.seq,
                "until_ts": self.until_ts, "drain_deadline_ts": self.drain_deadline_ts,
                "caps": dict(self.caps), "reason": self.reason}
        if self.expires_ts is not None:
            body["expires_ts"] = self.expires_ts
        if self.freeze is not None:
            body["freeze"] = dict(self.freeze)
        return body


@dataclasses.dataclass(frozen=True)
class GateAck:
    """`ameesh-host-ack/1`: what the runner did with state `seq`. `in_turn`:
    agents in a turn; `held`: leases still held; `drained`: after a
    `draining` (or `stopped`) of that seq, no lease held and no background
    work of a turn left."""

    seq: int
    state: str
    in_turn: Sequence[str] = ()
    held: Sequence[str] = ()
    drained: bool = False
    ts: float = 0.0

    def to_json(self) -> dict:
        return {"schema": SCHEMA_HOST_ACK, "seq": self.seq, "state": self.state,
                "in_turn": list(self.in_turn), "held": list(self.held),
                "drained": self.drained, "ts": self.ts}


def parse_state(raw: Any, *, now: Optional[float] = None, lending: bool = False) -> GateState:
    """A state read from the guard; `ValueError` when the runner may not obey
    it (the caller then applies its fallback). Freshness is the contract's
    (`capacity_guard.state_problem`); the fields the runner acts on are
    checked on top. Unknown fields are ignored."""
    now = time.time() if now is None else now
    problem = state_problem(raw, now, lending=lending)
    if problem is not None:
        raise ValueError(problem)
    for key in ("until_ts", "drain_deadline_ts", "expires_ts"):
        if raw.get(key) is not None and not _number(raw.get(key)):
            raise ValueError("%s: finite number or null expected" % key)
    caps = raw.get("caps") or {}
    if not isinstance(caps, Mapping):
        raise ValueError("caps: object expected")
    cap = caps.get("max_concurrent")
    if cap is not None and not (_number(cap) and int(cap) == cap and cap >= 0):
        raise ValueError("caps.max_concurrent: non-negative integer expected")
    freeze = raw.get("freeze")
    if freeze is not None and not isinstance(freeze, Mapping):
        raise ValueError("freeze: object expected")
    return GateState(state=raw["state"], seq=raw["seq"], until_ts=raw.get("until_ts"),
                     drain_deadline_ts=raw.get("drain_deadline_ts"), caps=dict(caps),
                     reason=str(raw.get("reason") or "unknown"),
                     expires_ts=raw.get("expires_ts"),
                     freeze=dict(freeze) if freeze is not None else None)


def fallback_state(fallback: str, seq: int = -1) -> GateState:
    """The state kept when the gate is unreadable, absent or stale."""
    return GateState(state=fallback, seq=seq, reason="unknown")


def _key(state: GateState) -> tuple:
    return (state.state, state.seq, state.reason, state.drain_deadline_ts,
            tuple(sorted(state.caps.items())),
            tuple(sorted((state.freeze or {}).items())))


def _atomic_write(path: str, data: dict) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# inotify (ctypes): optional, polling otherwise
# --------------------------------------------------------------------------

_IN_EVENTS = (0x2 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)


class _Inotify:
    """Watches a folder; `maybe` gives None when inotify is not available."""

    def __init__(self, directory: str):
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        wd = libc.inotify_add_watch(fd, os.fsencode(directory), _IN_EVENTS)
        if wd < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, "inotify_add_watch %s" % directory)
        self.fd = fd

    @classmethod
    def maybe(cls, directory: str) -> "Optional[_Inotify]":
        try:
            return cls(directory)
        except (OSError, AttributeError):
            return None

    def drain(self) -> None:
        while True:
            try:
                if not os.read(self.fd, 4096):
                    return
            except OSError:
                return

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class FileGate:
    """The state in a file, written by the guard with a temporary file and a
    `rename`; reread on inotify, else every `poll` seconds. The ack goes to
    its own folder (the guest may write there, not in the state's)."""

    def __init__(self, path: str = STATE_PATH, ack_path: str = ACK_PATH, *,
                 fallback: str = FALLBACK_CLASSIC, lending: bool = False,
                 poll: float = FILE_POLL_S, use_inotify: bool = True,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        if fallback not in STATES:
            raise ValueError("unknown fallback state: %r" % fallback)
        self.path = path
        self.ack_path = ack_path
        self.fallback = fallback
        self.lending = lending
        self.poll = max(0.05, float(poll))
        self._use_inotify = use_inotify
        self._inotify: _Inotify | None = None
        self._log = log or (lambda _m: None)
        self._clock = clock
        self._closed = threading.Event()
        self._rendered: tuple | None = None
        self._last_seq = -1
        self._last_error = ""
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)

    def _note_error(self, message: str) -> GateState:
        if message != self._last_error:
            self._last_error = message
            self._log("host gate: %s; state kept: %s" % (message, self.fallback))
        return fallback_state(self.fallback, self._last_seq)

    def state(self) -> GateState:
        """The current state, without waiting (the fallback when unreadable)."""
        try:
            with open(self.path, "rb") as fh:
                data = fh.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return self._note_error("no state (%s)" % self.path)
        except OSError as exc:
            return self._note_error("unreadable state (%s)" % exc)
        try:
            if len(data) > MAX_STATE_BYTES:
                raise ValueError("state too large")
            state = parse_state(json.loads(data.decode("utf-8")), now=self._clock(),
                                lending=self.lending)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return self._note_error("unreadable state (%s)" % exc)
        self._last_seq = state.seq
        if self._last_error:
            self._log("host gate readable again: %s (seq %d)" % (state.state, state.seq))
        self._last_error = ""
        return state

    def _sleep(self, seconds: float) -> None:
        if self._use_inotify and self._inotify is None:
            directory = os.path.dirname(self.path) or "."
            if os.path.isdir(directory):
                self._inotify = _Inotify.maybe(directory)
        fds = [self._wake_r] + ([self._inotify.fd] if self._inotify else [])
        try:
            ready, _w, _x = select.select(fds, [], [], max(0.0, seconds))
        except (OSError, ValueError):
            self._closed.wait(seconds)
            return
        if self._inotify is not None and self._inotify.fd in ready:
            self._inotify.drain()
            time.sleep(0.01)  # the guard writes then renames: let the rename land

    def wait_change(self, timeout: float) -> GateState:
        """Waits at most `timeout` seconds for a state other than the last
        one given; gives the current state (unchanged on timeout)."""
        end = time.monotonic() + max(0.0, timeout)
        while True:
            current = self.state()
            if _key(current) != self._rendered:
                self._rendered = _key(current)
                return current
            left = end - time.monotonic()
            if left <= 0 or self._closed.is_set():
                return current
            self._sleep(min(left, self.poll))

    def acknowledge(self, ack: GateAck) -> None:
        """Writes the ack; never raises (the next one replaces a failed one)."""
        body = dataclasses.replace(ack, ts=ack.ts or self._clock()).to_json()
        try:
            _atomic_write(self.ack_path, body)
        except OSError as exc:
            self._log("host gate: ack not written (%s)" % exc)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def default_fallback(cfg, lending: bool = False) -> str:
    """`host_gate_fallback`, else `stopped` on a lending host, `available`
    on a classic one."""
    chosen = str(getattr(cfg, "host_gate_fallback", "") or "").strip()
    if chosen:
        return chosen
    return FALLBACK_LENDING if lending else FALLBACK_CLASSIC


def gate_kind(cfg, lending: bool = False) -> str:
    """The gate of this host: `host_gate`, else `file` on a lending host
    (closed by default: no state, no claim) and '' on a classic one."""
    kind = str(getattr(cfg, "host_gate", "") or "").strip().lower()
    if kind == "none":
        return ""
    if not kind:
        return "file" if lending else ""
    if kind not in GATE_KINDS:
        raise ValueError("host_gate: unknown %r (%s)" % (kind, " | ".join(GATE_KINDS)))
    return kind


def drain_seconds(cfg, offer: Optional[Mapping] = None) -> float:
    """Withdrawal deadline when a `draining` state gives none:
    `host_gate_drain`, else the offer's `drain_seconds`, else 90 s."""
    chosen = float(getattr(cfg, "host_gate_drain", 0) or 0)
    if chosen > 0:
        return chosen
    seconds = (offer or {}).get("drain_seconds")
    return float(seconds) if _number(seconds) and seconds > 0 else DEFAULT_DRAIN_S


def from_config(cfg, *, lending: bool = False,
                log: Callable[[str], None] | None = None) -> Optional[FileGate]:
    """The configured gate, or None (no gate: nothing changes)."""
    if not gate_kind(cfg, lending):
        return None
    fallback = default_fallback(cfg, lending)
    return FileGate(str(getattr(cfg, "host_gate_path", "") or "") or STATE_PATH,
                    str(getattr(cfg, "host_gate_ack", "") or "") or ACK_PATH,
                    fallback=fallback, lending=lending or fallback == FALLBACK_LENDING,
                    log=log)


# --------------------------------------------------------------------------
# controller (in the runner)
# --------------------------------------------------------------------------

class GateController:
    """Makes the runner obey its gate. Read by the runner's sweep (claim?),
    by each worker (new turn? safe point? frozen time?); its thread applies
    the transitions, stops the turns at the deadline and acknowledges.

    Workers are expected to have `name`, `proc` (a turn runs when not None),
    `wake` (an Event), `host_yield` and `yield_to_host(reason)`; the runner
    `workers`, `lock` and `wake_all`."""

    TICK = 0.25
    FREEZES_KEPT = 64

    def __init__(self, runner, gate: FileGate, *, drain_s: float = DEFAULT_DRAIN_S,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.runner = runner
        self.gate = gate
        self.drain_s = max(0.0, float(drain_s))
        self._log = log or (lambda _m: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: GateState = gate.state()
        self._applied: tuple | None = None
        self.deadline: float | None = None
        self._overdue = False
        self._ack_key: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: freezes announced by the guard, `{since: end}` (the last
        #: `FREEZES_KEPT`): frozen time is read from them, not sampled, since
        #: the runner itself is frozen meanwhile
        self._freezes: dict = {}
        self.acks: list[GateAck] = []  # last acks (tests, display)

    # -- reads (any thread) -----------------------------------------------
    @property
    def current(self) -> GateState:
        with self._lock:
            if self._applied is None:
                # nothing applied yet (`--once`): the state of the moment
                self._state = self.gate.state()
            return self._state

    def may_claim(self) -> bool:
        return self.current.may_claim

    def holds_turns(self) -> bool:
        """No new turn (draining, stopped)."""
        return not self.current.may_claim

    def max_concurrent(self) -> Optional[int]:
        return self.current.max_concurrent

    def describe(self) -> str:
        st = self.current
        return "%s (%s, seq %d)" % (st.state, st.reason, st.seq)

    def frozen_between(self, start: float, end: Optional[float] = None) -> float:
        """Seconds of `[start, end]` (end: now) during which the guard kept
        the agents frozen: not counted in a turn's duration (study 1.6)."""
        end = self._clock() if end is None else end
        with self._lock:
            spans = list(self._freezes.items())
        return sum(max(0.0, min(until, end) - max(since, start)) for since, until in spans)

    def snapshot(self) -> dict:
        """The gate in a host reading (`resources.collect`): what relocation
        reads of a destination host."""
        st = self.current
        return {"gate_state": st.state, "gate_seq": st.seq,
                "gate_reason": st.reason[:REASON_MAX]}

    # -- loop ---------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.apply(self.gate.state())
        self._thread = threading.Thread(target=self._loop, daemon=True, name="host-gate")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.gate.wait_change(self.TICK)
                if self._stop.is_set():
                    return
                self.apply(state)
                self.tick()
            except Exception as exc:  # the gate never kills the runner
                self._log("host gate: controller error (%s)" % exc)
                self._stop.wait(self.TICK)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.gate.close()
        finally:
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=2.0)

    def _workers(self) -> list:
        lock = getattr(self.runner, "lock", None)
        workers = getattr(self.runner, "workers", {}) or {}
        if lock is None:
            return list(workers.values())
        with lock:
            return list(workers.values())

    # -- transitions --------------------------------------------------------
    def apply(self, state: GateState) -> bool:
        """Applies a state; True when it changed."""
        now = self._clock()
        with self._lock:
            self._account_freeze(state, now)
            if _key(state) == self._applied:
                return False
            previous = self._state if self._applied is not None else None
            self._applied = _key(state)
            self._state = state
            if state.may_claim:
                self.deadline = None
                self._overdue = False
            else:
                if state.state == "stopped":
                    deadline = now
                elif _number(state.drain_deadline_ts):
                    deadline = float(state.drain_deadline_ts)
                else:
                    deadline = now + self.drain_s
                if previous is not None and not previous.may_claim and self.deadline is not None:
                    deadline = min(deadline, self.deadline)  # never pushed back
                else:
                    self._overdue = False
                self.deadline = deadline
            self._ack_key = None  # a new ack to publish
        self._announce(previous, state, now)
        if state.may_claim:
            wake = getattr(self.runner, "wake_all", None)
            if wake is not None:
                wake.set()  # resume at once, not at the next sweep
        else:
            for worker in self._workers():
                wake = getattr(worker, "wake", None)
                if wake is not None:
                    wake.set()  # an idle worker releases its lease now
        return True

    def _account_freeze(self, state: GateState, now: float) -> None:
        """Notes the freeze a state announces (called under the lock); a
        freeze rewritten with another end replaces the first."""
        span = state.freeze_span
        if span is None or span[0] > now:
            return
        self._freezes[span[0]] = span[1]
        while len(self._freezes) > self.FREEZES_KEPT:
            del self._freezes[min(self._freezes)]

    def _announce(self, previous: GateState | None, state: GateState, now: float) -> None:
        before = previous.state if previous is not None else "start"
        if state.may_claim:
            self._log("host gate: %s -> available (%s, seq %d): claims open%s"
                      % (before, state.reason, state.seq,
                         ", agents frozen" if state.frozen_at(now) else ""))
        elif state.state == "draining":
            self._log("host gate: %s -> draining (%s, seq %d): no claim, turns stop at a "
                      "safe point, at the latest in %ds"
                      % (before, state.reason, state.seq,
                         max(0, int((self.deadline or now) - now))))
        else:
            self._log("host gate: %s -> stopped (%s, seq %d): no turn"
                      % (before, state.reason, state.seq))

    def tick(self) -> GateAck:
        """Drain deadline, lateness, ack; gives the ack."""
        now = self._clock()
        state = self.current
        with self._lock:
            self._account_freeze(state, now)
        workers = [w for w in self._workers() if w.is_alive()]
        if not state.may_claim and self.deadline is not None and now >= self.deadline:
            # at each pass: a turn published just after the deadline is
            # caught at the next one
            for worker in workers:
                if getattr(worker, "proc", None) is not None \
                        and getattr(worker, "host_yield", None) is None:
                    worker.yield_to_host("host drain deadline" if state.state == "draining"
                                         else "host stopped")
        background = getattr(self.runner, "background", None)
        if not state.may_claim and self.deadline is not None and background is not None:
            # the background work of finished turns stops at the deadline too
            background.cap_deadline(self.deadline, "l'hôte se retire (%s)" % state.reason)
        in_turn = sorted(w.name for w in workers if getattr(w, "proc", None) is not None)
        held = sorted(w.name for w in workers)
        left = background.running_count() if background is not None else 0
        drained = (not state.may_claim) and not held and not left
        if not state.may_claim and (held or left) and self.deadline is not None \
                and now >= self.deadline + OVERDUE_GRACE_S and not self._overdue:
            self._overdue = True
            self._log("host gate: drain %ds late, leases still held: %s"
                      % (int(now - self.deadline), ", ".join(held) or "none"))
        ack = GateAck(seq=state.seq, state=state.state, in_turn=tuple(in_turn),
                      held=tuple(held), drained=drained, ts=now)
        key = (ack.seq, ack.state, ack.in_turn, ack.held, ack.drained)
        if key != self._ack_key:
            self._ack_key = key
            self.gate.acknowledge(ack)
            self.acks = (self.acks + [ack])[-20:]
            if drained:
                self._log("host gate: drained (seq %d), no lease held, no background work"
                          % state.seq)
        return ack


def controller_from_config(runner, *, lending: bool = False, offer: Optional[Mapping] = None,
                           log: Callable[[str], None] | None = None) -> Optional[GateController]:
    """The runner's controller, or None without a gate (`gate_kind`)."""
    cfg = runner.cfg
    gate = from_config(cfg, lending=lending, log=log)
    if gate is None:
        return None
    return GateController(runner, gate, drain_s=drain_seconds(cfg, offer), log=log)
