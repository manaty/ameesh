# SPDX-License-Identifier: AGPL-3.0-only
"""Resident harness protocols (L75, decision 0035).

A resident agent keeps one harness process open between turns. This module
is the protocol layer only: one driver per protocol, all with the same
interface, chosen by the `ameesh.resident` block of the harness descriptor.
The runner side (lease, heartbeat, sleep and wake-up, restart) is L76; the
input queue and its priorities are L77.

* `claude-stream-json`: `claude -p --input-format stream-json` (Claude Code).
  An input is a `user` line on standard input; `--replay-user-messages`
  echoes each one, with its `uuid`, when the harness reads it. A message
  written during a turn is read at the next safe point (end of the tool call
  in progress) and answered in the same `result`. Interruption:
  `control_request` `interrupt`, without killing the process.
* `codex-app-server`: `codex app-server`, JSON-RPC 2.0 without the `jsonrpc`
  member. `initialize`, `thread/start` or `thread/resume`, then one
  `turn/start` per turn; `turn/steer` during a turn, `turn/interrupt`; usage
  per model call in `thread/tokenUsage/updated`.
* `acp`: several `session/prompt` on one ACP session (dsh `--profile acp`, any
  ACP agent), through the client of the ACP bridge (`acp.AcpTurn`: same
  permission policy, same `fs/*` bounded to the working directory). ACP
  refuses a second prompt during a turn and has no steer: steering is
  `session/cancel`, then a new prompt with the steered input.

Interface (`ResidentDriver`): `start()` opens or resumes the session;
`send(text)` starts a turn and returns its `Turn` handle (`ResidentBusy` if
one is running); `steer(text)` serves an input at the safe point of the
running turn (outside a turn, it is `send`); `interrupt()` ends the running
turn without killing the process; `probe()` checks that the harness answers;
`close()` stops the process group. A turn steered one or more times stays
**one** logical turn: its `TurnResult` lists every input it served, whatever
the protocol does underneath (a second `result` for Claude, a second
`turn/start` for Codex when the steer lost the race against the end of the
turn, a second `session/prompt` for ACP).

Every event read from the harness goes to `on_event(raw, parsed)`: `raw` is
the harness's own message, `parsed` the contract of `adapters.StreamReader`
(`session`, `display`, `final`, `usage`, `call_usage`, `cost`, `safe_point`,
`error`, `model`), so that the runner can log and display it as it does for a
turn process.

The harness runs in its own process group (`pgid`), which the runner records
and kills as a whole. No lock is held while waiting for the harness: the
reader thread takes the same lock to record what it reads.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import __version__, acp, adapters, harness_versions
from .adapters import HarnessAdapter

#: start of the harness up to an open session (hooks, MCP servers, login)
INIT_TIMEOUT = 120.0
#: answer to a control request (`interrupt`, `turn/start`, `turn/steer`)
CONTROL_TIMEOUT = 15.0
#: answer to a liveness probe
PROBE_TIMEOUT = 5.0
#: delay between closing standard input, SIGTERM and SIGKILL to the group
CLOSE_GRACE = 2.0
#: largest line read from the harness (memory guard)
MAX_LINE_CHARS = acp.MAX_MESSAGE_CHARS
#: final text kept per turn
FINAL_MAX_CHARS = 100_000

COMPLETED = "completed"
INTERRUPTED = "interrupted"
FAILED = "failed"

#: Codex items that are not an action (no safe point after them)
_CODEX_NOT_ACTIONS = ("userMessage", "hookPrompt", "agentMessage", "reasoning", "plan",
                      "contextCompaction", "enteredReviewMode", "exitedReviewMode")


class ResidentError(RuntimeError):
    """The resident harness cannot do what was asked (start, protocol, dead)."""


class ResidentBusy(ResidentError):
    """A turn is running: the input waits for its end (or is steered)."""


def log(message: str) -> None:
    try:
        sys.stderr.write("[resident] %s\n" % message)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class TurnResult:
    """End of a logical turn.

    `usage`: tokens of the turn (`input_tokens`, `cache_read_input_tokens`,
    `cache_creation_input_tokens`, `output_tokens`, summed over its model
    calls), plus `context_tokens` (context of its last call) and
    `context_window` when the harness gives them. `cost`: USD of the turn
    when it can be told apart from the session's cumulative cost
    (`cost_total`)."""

    status: str
    final: str = ""
    error: str | None = None
    session: str | None = None
    inputs: tuple[str, ...] = ()
    usage: dict = field(default_factory=dict)
    cost: float | None = None
    cost_total: float | None = None
    duration_s: float = 0.0


class Turn:
    """Handle of a logical turn: its first input and the inputs steered into it."""

    def __init__(self, input_id: str):
        self.id = new_id()
        self.inputs: list[str] = [input_id]
        #: interrupted by the caller: an input refused later is never served
        #: by a new turn in its name (codex r2, #325)
        self.interrupted = False
        self.started = time.monotonic()
        self.result: TurnResult | None = None
        self._done = threading.Event()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> TurnResult | None:
        """The result, or None if the turn is still running after `timeout`."""
        self._done.wait(timeout)
        return self.result

    def _set(self, result: TurnResult) -> None:
        self.result = result
        self._done.set()


def availability(descriptor, version: str | None) -> str:
    """'' if `descriptor` can run resident with this binary `version`, else
    why the agent stays at one turn per event (decision 0035 §1): no
    `ameesh.resident` block, version unreadable while a minimum is pinned, or
    version below the pinned minimum."""
    block = descriptor.resident
    if not block:
        return "descriptor %s declares no resident protocol" % descriptor.id
    minimum = block.get("min_version")
    if not minimum:
        return ""
    if not version:
        return ("version of %s unreadable: the pinned minimum %s cannot be checked"
                % (descriptor.id, minimum))
    if harness_versions.version_key(version) < harness_versions.version_key(minimum):
        return "%s %s is below the pinned minimum %s of the resident protocol" % (
            descriptor.id, version, minimum)
    return ""


def _number(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(int(value), 0)
    return 0


def _kill_group(proc: subprocess.Popen, grace: float, *, close_input: bool = True) -> None:
    """Close standard input (unless a writer may still use it), then SIGTERM
    and SIGKILL to the harness group."""
    try:
        if close_input and proc.stdin is not None:
            proc.stdin.close()
    except (OSError, ValueError):
        pass
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.send_signal(sig)
                except OSError:
                    pass
        try:
            proc.wait(timeout=max(grace, 0.1) if sig != signal.SIGKILL else 1.0)
            break
        except subprocess.TimeoutExpired:
            continue
    if proc.poll() is not None:
        # the leader is gone; what it left in the group goes too
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # the output is NOT closed here: the reader holds it until EOF, and a
    # child that left the group (setsid) may keep it open (codex r1, #325);
    # the reader closes it at EOF


def _close_output(proc: subprocess.Popen) -> None:
    """Close the harness's output, by its reader once at EOF."""
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except (OSError, ValueError):
        pass


class ResidentDriver:
    """Common part of the drivers: turn state, results, process group."""

    protocol = ""
    #: `_steer` does not wait for the harness: it runs under the lock, so
    #: that a steered input never reaches the harness after its turn ended
    _steer_locked = True

    def __init__(self, adapter: HarnessAdapter, *, cwd: str, session: str | None = None,
                 model: str | None = None, effort: str | None = None,
                 tier: str | None = None,
                 on_event: Callable[[dict, dict], None] | None = None,
                 env: Mapping[str, str] | None = None, stderr: Any = None,
                 init_timeout: float = INIT_TIMEOUT, cost_base: float | None = None,
                 spawn: Callable[..., subprocess.Popen] | None = None):
        self.adapter = adapter
        self.descriptor = adapter.descriptor
        self.block = dict(self.descriptor.resident)
        if self.block.get("protocol") != self.protocol:
            raise ResidentError("descriptor %s: resident protocol %r, not %s"
                                % (self.descriptor.id, self.block.get("protocol"), self.protocol))
        self.cwd = cwd
        self.session_id = session
        self.resumed = bool(session)
        self.model = model
        self.effort = effort
        self.tier = tier
        self.on_event = on_event
        self.extra_env = dict(env or {})
        self.stderr = stderr
        self.init_timeout = max(init_timeout, 0.1)
        #: L76: starts the harness (`subprocess.Popen` by default); the runner
        #: passes its own, under its launch lock
        self.spawn = spawn or subprocess.Popen
        #: cumulative session cost before this process (resumed session)
        self.cost_base = cost_base if cost_base is not None else (None if session else 0.0)
        self.last_activity = time.monotonic()
        self._lock = threading.RLock()
        self._turn: Turn | None = None
        self._followups: list[tuple[str, str]] = []
        #: steered inputs not yet accepted by the harness
        self._unconfirmed: set[str] = set()
        self._interrupting = False
        self._closed = False
        self._dead: str | None = None
        self._reset_turn()

    # -- environment -------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.descriptor.env)
        env.update(self.adapter.env())
        env.update(self.extra_env)
        return env

    # -- public interface --------------------------------------------------
    @property
    def proc(self) -> subprocess.Popen | None:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        proc = self.proc
        return proc.pid if proc is not None else None

    #: the harness leads its own group (`start_new_session`)
    pgid = pid

    @property
    def alive(self) -> bool:
        proc = self.proc
        return (proc is not None and proc.poll() is None and not self._closed
                and self._dead is None)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._turn is not None

    @property
    def current(self) -> Turn | None:
        with self._lock:
            return self._turn

    def start(self) -> str:
        """Start the harness and open (or resume) the session: its id."""
        raise NotImplementedError

    def send(self, text: str, input_id: str | None = None) -> Turn:
        """Start a turn with `text`; `ResidentBusy` if a turn is running."""
        with self._lock:
            self._usable()
            if self._turn is not None:
                raise ResidentBusy("a turn is running")
            turn = Turn(input_id or new_id())
            self._turn = turn
            self._reset_turn()
        try:
            self._begin(text, turn.inputs[0])
        except BaseException as exc:
            self._finish(FAILED, "cannot start the turn: %s" % exc, turn=turn)
            if isinstance(exc, ResidentError):
                raise
            raise ResidentError("cannot start the turn: %s" % exc) from exc
        return turn

    def steer(self, text: str, input_id: str | None = None) -> Turn:
        """Serve `text` at the safe point of the running turn (outside a
        turn: a new turn). The input joins the running turn's inputs."""
        input_id = input_id or new_id()
        with self._lock:
            self._usable()
            turn = self._turn
            if turn is not None:
                turn.inputs.append(input_id)
                if self._steer_locked:
                    # written before the turn can end: never into the next one
                    try:
                        self._steer(turn, text, input_id)
                    except BaseException:
                        turn.inputs.remove(input_id)
                        raise
                    return turn
                # served only once the harness accepts it (`_finish` leaves
                # it out until then: retried rather than lost)
                self._unconfirmed.add(input_id)
        if turn is None:
            return self.send(text, input_id)
        return self._steer(turn, text, input_id) or turn

    def interrupt(self) -> bool:
        """End the running turn without killing the harness (False: no turn)."""
        with self._lock:
            turn = self._turn
            if turn is None:
                return False
            # dropped follow-ups were never given to the harness: not served
            # (codex r1, #325), the caller serves them again
            self._unconfirmed.update(input_id for input_id, _text in self._followups)
            self._followups.clear()
            self._interrupting = True
            turn.interrupted = True
        return self._interrupt(turn)

    def probe(self, timeout: float = PROBE_TIMEOUT) -> bool:
        """Does the harness answer? (process alive at least)"""
        return self.alive

    def close(self, grace: float = CLOSE_GRACE) -> None:
        """Stop the harness group; a running turn ends in failure."""
        self._closed = True
        proc = self.proc
        if proc is not None:
            input_free = self._stop_writer(grace)
            _kill_group(proc, grace, close_input=input_free)
            if not input_free:
                self._close_input_late(grace)
            # the reader stops even if a child that left the group keeps the
            # output open (codex r2, #325); it closes the output itself
            self._stop_reader(grace)
        self._finish(FAILED, "resident harness closed")

    def _stop_writer(self, grace: float) -> bool:
        """Stop what writes on the harness's input: True once nothing does,
        so that its descriptor can be closed (never under a writer)."""
        return True

    def _close_input_late(self, grace: float) -> None:
        """After the group is killed: close the input once its writer is gone."""

    def _stop_reader(self, grace: float) -> None:
        """Stop the reader of the harness's output, waiting at most `grace`."""

    # -- for the drivers ---------------------------------------------------
    def _usable(self) -> None:
        if self._closed:
            raise ResidentError("resident harness closed")
        if self._dead is not None:
            raise ResidentError(self._dead)
        if self.proc is None:
            raise ResidentError("resident harness not started")
        if self.proc.poll() is not None:
            raise ResidentError("resident harness exited (code %s)" % self.proc.returncode)

    def _reset_turn(self) -> None:
        self._final = ""
        self._error: str | None = None
        self._usage = {"input_tokens": 0, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0, "output_tokens": 0}
        self._context: int | None = None
        self._window: int | None = None
        self._cost: float | None = None
        self._cost_total: float | None = None

    def _add_usage(self, input_tokens: int = 0, cache_read: int = 0, cache_creation: int = 0,
                   output_tokens: int = 0) -> None:
        with self._lock:
            self._usage["input_tokens"] += input_tokens
            self._usage["cache_read_input_tokens"] += cache_read
            self._usage["cache_creation_input_tokens"] += cache_creation
            self._usage["output_tokens"] += output_tokens

    def _emit(self, raw: dict, parsed: dict) -> None:
        self.last_activity = time.monotonic()
        callback = self.on_event
        if callback is None:
            return
        try:
            callback(raw, parsed)
        except Exception as exc:  # noqa: BLE001 — a display failure never stops the harness
            log("on_event failed: %r" % (exc,))

    def _finish(self, status: str, error: str | None = None, *,
                turn: Turn | None = None) -> None:
        """End the logical turn (`turn`: only if it is still the running one)."""
        with self._lock:
            current = self._turn
            if current is None or (turn is not None and current is not turn):
                return
            usage = {k: v for k, v in self._usage.items()}
            if self._context is not None:
                usage["context_tokens"] = self._context
            if self._window is not None:
                usage["context_window"] = self._window
            result = TurnResult(
                status=status, final=self._final[-FINAL_MAX_CHARS:],
                error=error if error is not None else self._error,
                session=self.session_id, usage=usage,
                inputs=tuple(i for i in current.inputs if i not in self._unconfirmed),
                cost=self._cost, cost_total=self._cost_total,
                duration_s=round(time.monotonic() - current.started, 3))
            self._turn = None
            self._followups.clear()
            self._unconfirmed.clear()
            self._interrupting = False
        current._set(result)

    def _died(self, reason: str) -> None:
        """The harness can no longer serve (exit, broken protocol)."""
        if self._dead is None and not self._closed:
            self._dead = reason
            log(reason)
        self._finish(FAILED, reason)

    def _take_followup(self) -> tuple[str, str] | None:
        """Next steered input to serve in the same logical turn (lock held)."""
        if self._interrupting or not self._followups:
            return None
        return self._followups.pop(0)

    # -- per protocol ------------------------------------------------------
    def _begin(self, text: str, input_id: str) -> None:
        raise NotImplementedError

    def _steer(self, turn: Turn, text: str, input_id: str) -> Turn | None:
        """Serve the input in `turn`; another turn if it serves it instead."""
        raise NotImplementedError

    def _interrupt(self, turn: Turn) -> bool:
        """Interrupt `turn`, never the turn after it."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Claude Code: stream-json in and out
# --------------------------------------------------------------------------

class ClaudeStreamJson(ResidentDriver):
    """`claude -p --input-format stream-json --output-format stream-json`.

    The end of a logical turn is the `result` after which every input written
    has been read (echoed by `--replay-user-messages`): an input steered just
    as the turn ends is read after the `result` and opens another turn of the
    harness, which belongs to the same logical turn."""

    protocol = "claude-stream-json"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proc: subprocess.Popen | None = None
        #: lines for the writer thread: never written under the lock (a large
        #: input and a harness that fills its output first would block both
        #: pipes, codex r1, #325); None stops it
        self._outbox: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        #: ends the reader after close (EOF may never come)
        self._stop_read = threading.Event()
        #: wire uuid → input id, for the inputs not read when a turn ends
        self._wire_inputs: dict[str, str] = {}
        self._controls: dict[str, dict] = {}
        self._control_seq = 0
        #: uuids written in the logical turn and not yet read by the harness
        self._unread: set[str] = set()
        self._reader: threading.Thread | None = None

    @property
    def proc(self) -> subprocess.Popen | None:
        return self._proc

    def argv(self) -> list[str]:
        adapter = self.adapter
        argv = [adapter.binary, *self.block["launcher"],
                *adapter._options(self.model, self.effort, self.tier, None),
                *self.block["command"]]
        if self.session_id:
            argv += [*self.descriptor.session_flag, self.session_id]
        elif self.block.get("new_session"):
            # the session id is known before the first turn
            self.session_id = new_id()
            argv += [*self.block["new_session"], self.session_id]
        return argv

    def start(self) -> str:
        argv = self.argv()
        try:
            self._proc = self.spawn(
                argv, cwd=self.cwd, env=self._env(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1,
                start_new_session=True)
        except OSError as exc:
            raise ResidentError("cannot start %s: %s" % (self.descriptor.id, exc))
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="resident-claude-reader")
        self._reader.start()
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name="resident-claude-writer")
        self._writer.start()
        # handshake: the harness answers once its startup hooks have run
        self._control({"subtype": "initialize"}, self.init_timeout)
        return self.session_id or ""

    def _stop_writer(self, grace: float) -> bool:
        self._outbox.put(None)
        writer = self._writer
        if writer is not None:
            writer.join(grace)
            if writer.is_alive():
                return False  # blocked in a write: the input stays open for now
        return True

    def _close_input_late(self, grace: float) -> None:
        # the group is killed: a write blocked on it fails (EPIPE) and the
        # writer ends; only then is the descriptor closed, never under it
        writer, proc = self._writer, self._proc
        if writer is not None:
            writer.join(grace)
        if writer is not None and writer.is_alive():
            log("writer still blocked (a child that left the group holds the input): "
                "the input is left open")
            return
        try:
            if proc is not None and proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _stop_reader(self, grace: float) -> None:
        self._stop_read.set()
        if self._reader is not None:
            self._reader.join(grace)

    def _died(self, reason: str) -> None:
        super()._died(reason)
        self._outbox.put(None)  # the writer never waits for a line that cannot come

    # -- writing -----------------------------------------------------------
    def _write(self, message: dict) -> None:
        """Queue a line for the writer thread (never blocks): lines reach the
        harness in the order they were queued."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None \
                or self._dead is not None or self._closed:
            raise ResidentError("resident harness is not running")
        self._outbox.put((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def _write_loop(self) -> None:
        """Writes the queued lines on the harness's input, by its descriptor:
        no Python buffer lock, so that closing the input never waits for a
        blocked write."""
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        fd = proc.stdin.fileno()
        while True:
            data = self._outbox.get()
            if data is None or self._closed:
                return
            try:
                view = memoryview(data)
                while view and not self._closed:
                    view = view[os.write(fd, view):]
            except (OSError, ValueError) as exc:
                self._died("cannot write to the harness: %s" % exc)
                return

    def _control(self, request: dict, timeout: float, *,
                 turn: Turn | None = None) -> dict | None:
        """Send a control request and wait for its answer; with `turn`, only
        if it is still the running turn (None otherwise)."""
        with self._lock:
            if turn is not None and self._turn is not turn:
                return None
            self._control_seq += 1
            request_id = "ameesh-%d" % self._control_seq
            slot = {"event": threading.Event(), "response": None}
            self._controls[request_id] = slot
            try:
                self._write({"type": "control_request", "request_id": request_id,
                             "request": request})
            except ResidentError:
                self._controls.pop(request_id, None)
                raise
        try:
            deadline = time.monotonic() + timeout
            while not slot["event"].wait(0.1):
                if self._dead is not None or (self._proc and self._proc.poll() is not None):
                    raise ResidentError("harness exited before answering %s"
                                        % request.get("subtype"))
                if time.monotonic() >= deadline:
                    raise ResidentError("no answer to %s within %ds"
                                        % (request.get("subtype"), int(timeout)))
        finally:
            with self._lock:
                self._controls.pop(request_id, None)
        response = slot["response"] or {}
        if response.get("subtype") != "success":
            raise ResidentError("%s refused: %s" % (request.get("subtype"),
                                                     response.get("error") or response))
        return response

    def _user(self, text: str, wire: str, input_id: str) -> None:
        with self._lock:
            self._unread.add(wire)
            self._wire_inputs[wire] = input_id
        self._write({"type": "user", "uuid": wire, "session_id": self.session_id or "",
                     "parent_tool_use_id": None,
                     "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    # -- turn --------------------------------------------------------------
    def _begin(self, text: str, input_id: str) -> None:
        with self._lock:
            self._unread.clear()
            self._wire_inputs.clear()
        self._user(text, new_id(), input_id)

    def _steer(self, turn: Turn, text: str, input_id: str) -> None:
        # queued under the lock (unread before the turn can end), written at
        # once by the writer: the harness reads it at the next safe point
        self._user(text, new_id(), input_id)

    def _interrupt(self, turn: Turn) -> bool:
        try:
            return self._control({"subtype": "interrupt"}, CONTROL_TIMEOUT,
                                 turn=turn) is not None
        except ResidentError as exc:
            log("interrupt: %s" % exc)
            return False

    # -- reading -----------------------------------------------------------
    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in acp.stoppable_lines(proc.stdout.fileno(), MAX_LINE_CHARS,
                                            self._stop_read):
                if len(line) > MAX_LINE_CHARS:
                    self._died("harness line too large (> %d characters)" % MAX_LINE_CHARS)
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    log("unreadable line from the harness: %s" % line[:200])
                    continue
                if isinstance(event, dict):
                    self._on_event(event, line)
        except Exception as exc:  # noqa: BLE001 — never a silent dead reader
            self._died("resident reader failed: %r" % (exc,))
        if self._stop_read.is_set():
            _close_output(proc)  # stopped by close(): the group was killed
            self._died("resident harness closed")
        else:
            code = proc.wait()
            _close_output(proc)
            self._died("resident harness exited (code %s)" % code)
        with self._lock:
            slots = list(self._controls.values())
        for slot in slots:
            slot["event"].set()

    def _on_event(self, event: dict, line: str) -> None:
        kind = event.get("type")
        if kind == "control_response":
            response = event.get("response") or {}
            with self._lock:
                slot = self._controls.get(str(response.get("request_id") or ""))
            if slot is not None:
                slot["response"] = response
                slot["event"].set()
            return
        if kind == "control_request":
            # the harness asks the client (permission tool, hook callback):
            # nothing is granted from here
            request_id = event.get("request_id")
            subtype = (event.get("request") or {}).get("subtype")
            log("control request %r from the harness refused" % subtype)
            try:
                self._write({"type": "control_response", "response": {
                    "subtype": "error", "request_id": request_id,
                    "error": "not supported by the ameesh resident client"}})
            except ResidentError:
                pass
            return
        parsed = self.adapter.parse(line)
        if parsed.get("session"):
            self.session_id = str(parsed["session"])
        principal = not event.get("parent_tool_use_id")
        if kind == "user" and event.get("isReplay"):
            with self._lock:
                self._unread.discard(str(event.get("uuid") or ""))
        elif kind == "assistant" and principal and isinstance(parsed.get("call_usage"), dict):
            usage = parsed["call_usage"]
            with self._lock:
                self._context = (_number(usage.get("input_tokens"))
                                 + _number(usage.get("cache_read_input_tokens"))
                                 + _number(usage.get("cache_creation_input_tokens")))
        self._emit(event, parsed)
        if kind == "result" and principal:
            self._on_result(event)

    def _on_result(self, event: dict) -> None:
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        self._add_usage(_number(usage.get("input_tokens")),
                        _number(usage.get("cache_read_input_tokens")),
                        _number(usage.get("cache_creation_input_tokens")),
                        _number(usage.get("output_tokens")))
        with self._lock:
            total = event.get("total_cost_usd")
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                # cumulative over the session, even across a --resume
                if self.cost_base is not None and self._turn is not None:
                    self._cost = (self._cost or 0.0) + max(float(total) - self.cost_base, 0.0)
                self.cost_base = float(total)
                self._cost_total = float(total)
            if self._turn is None:
                return
            if event.get("result"):
                self._final = str(event["result"])
            if self._unread and not self._interrupting:
                return  # a steered input opens the next harness turn
            # interrupted with inputs still unread: not served by this turn
            # (codex r1, #325), the caller serves them again
            self._unconfirmed.update(self._wire_inputs[w] for w in self._unread
                                     if w in self._wire_inputs)
            if not event.get("is_error"):
                status, error = COMPLETED, None
            elif self._interrupting:
                status, error = INTERRUPTED, None
            else:
                errors = event.get("errors")
                status = FAILED
                error = "; ".join(str(e) for e in errors) if isinstance(errors, list) \
                    and errors else str(event.get("subtype") or "harness error")
            # under the lock: a steer either was written before (and read) or
            # finds no turn and opens the next one
            self._finish(status, error)


# --------------------------------------------------------------------------
# Codex: app-server (JSON-RPC 2.0 without the `jsonrpc` member)
# --------------------------------------------------------------------------

class CodexAppServer(ResidentDriver):
    """`codex app-server`: one thread, one `turn/start` per turn."""

    protocol = "codex-app-server"
    #: `turn/steer` waits for the server's answer
    _steer_locked = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.peer: acp.JsonRpcPeer | None = None
        self._turn_id: str | None = None
        #: number of the last `turn/start`: a late answer to an earlier one
        #: never overwrites the turn id (codex r1, #325)
        self._start_seq = 0
        self._watch: threading.Thread | None = None

    @property
    def proc(self) -> subprocess.Popen | None:
        return self.peer.proc if self.peer is not None else None

    @property
    def alive(self) -> bool:
        return super().alive and self.peer is not None and self.peer.fatal is None

    def argv(self) -> list[str]:
        return [self.adapter.binary, *self.block["launcher"], *self.block["command"]]

    def _thread_params(self) -> dict:
        params: dict = {"cwd": self.cwd}
        params.update(self.block.get("thread") or {})
        if self.model:
            params["model"] = self.model
        if self.tier:
            params["serviceTier"] = self.tier
        return params

    def start(self) -> str:
        try:
            self.peer = acp.JsonRpcPeer(self.argv(), cwd=self.cwd, env=self._env(),
                                        header=False, new_session=True, stderr=self.stderr,
                                        stoppable=True)
        except OSError as exc:
            raise ResidentError("cannot start %s: %s" % (self.descriptor.id, exc))
        self.peer.set_handlers(self._on_request, self._on_notification)
        self._watch = threading.Thread(target=self._watch_exit, daemon=True,
                                       name="resident-codex-watch")
        self._watch.start()
        try:
            self.peer.request("initialize", {
                "clientInfo": {"name": "ameesh", "title": "ameesh", "version": __version__},
            }, timeout=self.init_timeout)
            self.peer.notify("initialized")
            if self.session_id:
                # without its history: a long thread exceeds the message limit
                result = self.peer.request("thread/resume", dict(
                    self._thread_params(), threadId=self.session_id, excludeTurns=True),
                    timeout=self.init_timeout)
            else:
                result = self.peer.request("thread/start", self._thread_params(),
                                           timeout=self.init_timeout)
        except acp.AcpError as exc:
            raise ResidentError("codex app-server: %s" % exc)
        thread = (result or {}).get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or not thread.get("id"):
            raise ResidentError("codex app-server: thread without id")
        self.session_id = str(thread["id"])
        parsed = {"session": self.session_id}
        if result.get("model"):
            parsed["model"] = str(result["model"])
        self._emit({"method": "thread/opened", "params": {"threadId": self.session_id}}, parsed)
        return self.session_id

    def _stop_reader(self, grace: float) -> None:
        peer = self.peer
        if peer is not None:
            peer.stop_reading()
            peer._reader.join(grace)
        if self._watch is not None:
            self._watch.join(grace)

    def _watch_exit(self) -> None:
        peer = self.peer
        assert peer is not None
        while True:
            if peer._eof.wait(0.2) or peer.fatal is not None:
                break
        if peer.fatal is not None:
            self._died("codex app-server protocol error: %s" % peer.fatal)
        else:
            self._died("resident harness exited (code %s)" % peer.proc.wait())
            _close_output(peer.proc)  # the reader is at EOF

    # -- turn --------------------------------------------------------------
    def _input(self, text: str) -> list[dict]:
        return [{"type": "text", "text": text}]

    def _turn_start(self, text: str) -> None:
        assert self.peer is not None
        params: dict = {"threadId": self.session_id, "input": self._input(text)}
        if self.effort:
            params["effort"] = self.effort
        with self._lock:
            self._start_seq += 1
            seq = self._start_seq
        try:
            result = self.peer.request("turn/start", params, timeout=CONTROL_TIMEOUT)
        except acp.AcpError as exc:
            raise ResidentError("turn/start: %s" % exc)
        turn = (result or {}).get("turn") if isinstance(result, dict) else None
        if isinstance(turn, dict) and turn.get("id"):
            with self._lock:
                if seq == self._start_seq and self._turn_id is None:
                    self._turn_id = str(turn["id"])

    def _begin(self, text: str, input_id: str) -> None:
        with self._lock:
            self._turn_id = None
        self._turn_start(text)

    def _steer(self, turn: Turn, text: str, input_id: str) -> Turn | None:
        assert self.peer is not None
        deadline = time.monotonic() + CONTROL_TIMEOUT
        while True:
            with self._lock:
                turn_id = self._turn_id
                running = self._turn is turn
            if turn_id or not running or time.monotonic() >= deadline:
                break
            time.sleep(0.05)  # turn/start not answered yet
        if running and turn_id:
            try:
                self.peer.request("turn/steer", {"threadId": self.session_id,
                                                 "expectedTurnId": turn_id,
                                                 "input": self._input(text)},
                                  timeout=CONTROL_TIMEOUT)
            except acp.AcpError as exc:
                log("turn/steer refused (%s): the input follows in the same turn" % exc)
            else:
                with self._lock:
                    self._unconfirmed.discard(input_id)
                    result = turn.result
                    if result is not None and input_id not in result.inputs:
                        # accepted just before the end of the turn: served by it
                        result.inputs = (*result.inputs, input_id)
                return
        with self._lock:
            if turn.interrupted:
                # interrupted meanwhile, even if its end was already recorded:
                # not served, never a new turn in its name (left unconfirmed)
                return None
            self._unconfirmed.discard(input_id)
            if self._turn is turn:
                # served by a new harness turn when the current one completes
                self._followups.append((input_id, text))
                return None
        # the turn ended without it (left out of its inputs): a new turn
        return self.send(text, input_id)

    def _interrupt(self, turn: Turn) -> bool:
        assert self.peer is not None
        deadline = time.monotonic() + CONTROL_TIMEOUT
        while True:
            with self._lock:
                turn_id = self._turn_id
                running = self._turn is turn
            if turn_id or not running or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if not (running and turn_id):
            return False
        try:
            self.peer.request("turn/interrupt", {"threadId": self.session_id,
                                                 "turnId": turn_id}, timeout=CONTROL_TIMEOUT)
        except acp.AcpError as exc:
            log("turn/interrupt: %s" % exc)
            return False
        return True

    def probe(self, timeout: float = PROBE_TIMEOUT) -> bool:
        if not self.alive or self.peer is None:
            return False
        try:
            self.peer.request("thread/read", {"threadId": self.session_id},
                              timeout=timeout)
        except acp.AcpRpcError:
            return True  # it answered
        except acp.AcpError:
            return False
        return True

    # -- harness → client --------------------------------------------------
    def _on_request(self, method: str, params: dict) -> Any:
        """Nothing is granted from here: the thread runs without approvals
        (`ameesh.resident.thread`), so a request is unexpected and refused."""
        decisions = {"item/commandExecution/requestApproval": {"decision": "decline"},
                     "item/fileChange/requestApproval": {"decision": "decline"},
                     "execCommandApproval": {"decision": "denied"},
                     "applyPatchApproval": {"decision": "denied"},
                     "mcpServer/elicitation/request": {"action": "decline"}}
        self._emit({"method": method, "params": params},
                   {"display": ["[permission refused] %s" % method]})
        log("request %s from the harness refused" % method)
        if method in decisions:
            return decisions[method]
        raise acp.AcpRpcError(-32601, "not supported by the ameesh resident client: %s"
                              % method)

    def _on_notification(self, method: str, params: dict) -> None:
        raw = {"method": method, "params": params}
        thread_id = params.get("threadId")
        if thread_id and self.session_id and thread_id != self.session_id:
            self._emit(raw, {})  # another thread (sub-agent): shown, not counted
            return
        parsed: dict = {}
        if method == "turn/started":
            turn = params.get("turn") or {}
            with self._lock:
                if self._turn is not None and turn.get("id"):
                    self._turn_id = str(turn["id"])
        elif method == "item/completed":
            item = params.get("item") or {}
            kind = item.get("type")
            if kind == "agentMessage":
                text = str(item.get("text") or "")
                parsed["final"] = text
                if text:
                    parsed["display"] = [text]
                with self._lock:
                    self._final = text  # the last message is the answer (L218)
            elif kind not in _CODEX_NOT_ACTIONS and kind:
                parsed["safe_point"] = True
                parsed["display"] = ["[tool] %s" % kind]
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") if isinstance(usage.get("last"), dict) else {}
            call = {"input_tokens": _number(last.get("inputTokens")),
                    "cached_input_tokens": _number(last.get("cachedInputTokens")),
                    "output_tokens": _number(last.get("outputTokens"))}
            parsed["call_usage"] = call
            parsed["usage"] = call
            # Codex counts the cache inside the input (`adapters.reread_tokens`)
            self._add_usage(max(call["input_tokens"] - call["cached_input_tokens"], 0),
                            call["cached_input_tokens"], 0, call["output_tokens"])
            with self._lock:
                self._context = call["input_tokens"]
                if _number(usage.get("modelContextWindow")):
                    self._window = _number(usage.get("modelContextWindow"))
        elif method == "error":
            error = params.get("error") or {}
            if not params.get("willRetry"):
                parsed["error"] = str(error.get("message") or "harness error")
                with self._lock:
                    self._error = parsed["error"]
        self._emit(raw, parsed)
        if method == "turn/completed":
            self._on_turn_completed(params.get("turn") or {})

    def _on_turn_completed(self, turn: dict) -> None:
        status = turn.get("status")
        error = (turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) \
            else None
        # decided and ended under the lock: a steer either joins as a
        # follow-up or finds no turn
        with self._lock:
            if self._turn is None:
                return
            if turn.get("id") and self._turn_id and turn["id"] != self._turn_id:
                return  # end of a turn that is not ours
            followup = self._take_followup()
            self._turn_id = None
            if followup is not None:
                # not from the reader thread: it must stay free to read the answer
                threading.Thread(target=self._serve_followup, args=(followup,),
                                 daemon=True, name="resident-codex-followup").start()
            elif status == "completed":
                self._finish(COMPLETED)
            elif status == "interrupted" or self._interrupting:
                self._finish(INTERRUPTED, error)
            else:
                self._finish(FAILED, error or "turn %s" % (status or "ended without status"))

    def _serve_followup(self, followup: tuple[str, str]) -> None:
        with self._lock:
            turn = self._turn
        try:
            self._turn_start(followup[1])
        except ResidentError as exc:
            self._finish(FAILED, str(exc), turn=turn)


# --------------------------------------------------------------------------
# ACP: several session/prompt on one session
# --------------------------------------------------------------------------

class _AcpClient(acp.AcpTurn):
    """The client of the ACP bridge (permissions, `fs/*`, settings, usage),
    kept open across prompts; its events go to the driver."""

    def __init__(self, driver: "AcpResident"):
        super().__init__(driver.descriptor, driver.adapter.binary, prompt="",
                         session=driver.session_id, model=driver.model, effort=driver.effort,
                         tier=driver.tier, cwd=driver.cwd, init_timeout=driver.init_timeout)
        self._driver = driver

    def emit(self, event: dict) -> None:
        self._driver._on_bridge_event(event)

    def _start(self) -> None:
        driver = self._driver
        argv = [self.agent_binary, *driver.block["launcher"], *driver.block["command"]]
        try:
            self.peer = acp.JsonRpcPeer(argv, cwd=self.cwd, env=driver._env(),
                                        new_session=True, stderr=driver.stderr,
                                        stoppable=True)
        except OSError as exc:
            raise acp.AcpError("cannot start the ACP agent: %s" % exc)
        self.peer.set_handlers(self._on_request, self._on_notification)

    def reset_prompt(self) -> None:
        """Counters of one `session/prompt`."""
        self._cancel.clear()
        self.final_text = ""
        self.max_used = None
        self._first_cost = None
        self._last_cost = None
        self._cost_readings = 0


class AcpResident(ResidentDriver):
    """Multi-prompt ACP (dsh `--profile acp`, any ACP agent)."""

    protocol = "acp"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client: _AcpClient | None = None
        self._stream = adapters.AcpStream()
        self._watch: threading.Thread | None = None

    @property
    def proc(self) -> subprocess.Popen | None:
        peer = self.client.peer if self.client is not None else None
        return peer.proc if peer is not None else None

    @property
    def alive(self) -> bool:
        peer = self.client.peer if self.client is not None else None
        return super().alive and peer is not None and peer.fatal is None

    def start(self) -> str:
        client = _AcpClient(self)
        self.client = client
        try:
            client._root_fd = acp._open_root(self.cwd)
            client._start()
            self._watch = threading.Thread(target=self._watch_exit, daemon=True,
                                           name="resident-acp-watch")
            self._watch.start()
            client._initialize()
            client._session()
            client._apply_settings()
        except (acp.AcpError, OSError) as exc:
            raise ResidentError("ACP agent: %s" % exc)
        self.session_id = client.session_id
        return self.session_id or ""

    def _stop_reader(self, grace: float) -> None:
        peer = self.client.peer if self.client is not None else None
        if peer is not None:
            peer.stop_reading()
            peer._reader.join(grace)
        if self._watch is not None:
            self._watch.join(grace)

    def _watch_exit(self) -> None:
        peer = self.client.peer if self.client is not None else None
        assert peer is not None
        while not (peer._eof.wait(0.2) or peer.fatal is not None):
            pass
        if peer.fatal is not None:
            self._died("ACP protocol error: %s" % peer.fatal)
        else:
            self._died("resident harness exited (code %s)" % peer.proc.wait())
            _close_output(peer.proc)  # the reader is at EOF

    def close(self, grace: float = CLOSE_GRACE) -> None:
        super().close(grace)
        client = self.client
        if client is not None and client._root_fd is not None:
            try:
                os.close(client._root_fd)
            except OSError:
                pass
            client._root_fd = None

    def _on_bridge_event(self, event: dict) -> None:
        parsed = self._stream.parse_event(event)
        if event.get("type") == "session" and event.get("sessionId"):
            self.session_id = str(event["sessionId"])
        self._emit(event, parsed)

    # -- turn --------------------------------------------------------------
    def _prompt(self, text: str) -> None:
        client = self.client
        assert client is not None and client.peer is not None
        client.reset_prompt()
        try:
            pending = client.peer.send_request("session/prompt", {
                "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]})
        except acp.AcpError as exc:
            raise ResidentError("session/prompt: %s" % exc)
        with self._lock:
            turn = self._turn
        threading.Thread(target=self._wait_prompt, args=(pending, turn), daemon=True,
                         name="resident-acp-prompt").start()

    def _begin(self, text: str, input_id: str) -> None:
        self._prompt(text)

    def _wait_prompt(self, pending: acp.Pending, turn: Turn | None) -> None:
        client = self.client
        assert client is not None and client.peer is not None
        try:
            client.peer.wait(pending)
            result = pending.raise_for_error()
            stop = str((result or {}).get("stopReason") or "end_turn") \
                if isinstance(result, dict) else "end_turn"
            error = None
        except acp.AcpError as exc:
            stop, error = "error", str(exc)
        self._end_prompt(stop, error, turn)

    def _end_prompt(self, stop: str, error: str | None, turn: Turn | None) -> None:
        client = self.client
        assert client is not None
        with self._lock:
            if self._turn is None or self._turn is not turn:
                return
            if client.final_text:
                self._final = client.final_text
            if client.max_used is not None:
                self._context = client.max_used
                self._usage["input_tokens"] += client.max_used
            if client.size is not None:
                self._window = client.size
            if client._last_cost is not None:
                # cumulative over the session: this prompt's cost is the
                # difference with the reading before it (codex r1, #325), as
                # for Claude; unknown for a resumed session without a base
                if self.cost_base is not None:
                    self._cost = (self._cost or 0.0) + max(
                        client._last_cost - self.cost_base, 0.0)
                self.cost_base = client._last_cost
                self._cost_total = client._last_cost
        parsed = {"display": ["%s (%s)" % (adapters.END_OF_TURN_MARK, stop)]}
        if error:
            parsed["error"] = error
        self._emit({"type": "prompt_end", "stopReason": stop}, parsed)
        # decided and ended under the lock: a steer either joins as a
        # follow-up (its `session/cancel` already written) or finds no turn
        with self._lock:
            if self._turn is not turn:
                return
            followup = self._take_followup()
            if followup is not None:
                try:
                    self._prompt(followup[1])
                except ResidentError as exc:
                    self._finish(FAILED, str(exc), turn=turn)
            elif stop == "end_turn":
                self._finish(COMPLETED, turn=turn)
            elif stop == "cancelled" or self._interrupting:
                self._finish(INTERRUPTED, turn=turn)
            else:
                self._finish(FAILED, error or "ACP turn ended: %s" % stop, turn=turn)

    def _cancel(self) -> bool:
        client = self.client
        if client is None or client.peer is None:
            return False
        client.set_cancel()  # pending permission requests answer `cancelled`
        try:
            client.peer.notify("session/cancel", {"sessionId": self.session_id})
        except acp.AcpError as exc:
            log("session/cancel: %s" % exc)
            return False
        return True

    def _steer(self, turn: Turn, text: str, input_id: str) -> None:
        # under the lock, turn running: ACP has no steer, so the prompt in
        # flight is cancelled and the input follows in a new prompt
        self._followups.append((input_id, text))
        self._cancel()

    def _interrupt(self, turn: Turn) -> bool:
        with self._lock:
            if self._turn is not turn:
                return False  # ended meanwhile: the next turn is not touched
            return self._cancel()

    def probe(self, timeout: float = PROBE_TIMEOUT) -> bool:
        client = self.client
        if not self.alive or client is None or client.peer is None:
            return False
        try:
            client.peer.request("session/list", {}, timeout=timeout)
        except acp.AcpRpcError:
            return True  # it answered (method not announced)
        except acp.AcpError:
            return False
        return True


#: drivers by `ameesh.resident.protocol`
DRIVERS: dict[str, type[ResidentDriver]] = {
    ClaudeStreamJson.protocol: ClaudeStreamJson,
    CodexAppServer.protocol: CodexAppServer,
    AcpResident.protocol: AcpResident,
}


def driver_for(adapter: HarnessAdapter, **kwargs) -> ResidentDriver:
    """The driver of `adapter`'s resident protocol (not started)."""
    protocol = adapter.descriptor.resident.get("protocol")
    driver = DRIVERS.get(protocol or "")
    if driver is None:
        raise ResidentError("descriptor %s declares no resident protocol" % adapter.key)
    return driver(adapter, **kwargs)
