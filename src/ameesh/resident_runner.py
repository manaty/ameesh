# SPDX-License-Identifier: AGPL-3.0-only
"""The resident process of an agent, across its turns (L76, decision 0035).

`resident.py` (L75) speaks the protocols; this module holds what the runner
needs to keep one harness process open between turns, under the agent's
lease:

* `ResidentProcess`: a started driver, the launch key it was started with,
  its life id (the `AMEESH_TURN_ID` of the process, the label of its
  containers, its turn resource) and the queue of its events, read by the
  turn loop of `runner.AgentWorker._run_turn` exactly as the lines of a turn
  process;
* `launch_key`: what, when it changes, makes the next turn restart the
  process (model, effort, tier, account, working directory, binary);
* `refusal`: why a turn stays at one turn per event.

The runner side keeps the invariants of a turn process: the harness group is
published as `AgentWorker.proc` / `pgid` for the whole life of the process,
so the deadline watchdog, a lost lease and the runner shutdown kill it as
they kill a turn; nothing outlives the lease.

L76 enables the resident mode for `claude-stream-json` only: its events are
the lines of `claude -p --output-format stream-json`, which the ledger
already reads. Codex app-server and ACP report their usage in other messages
(`thread/tokenUsage/updated`, `session/update`); they stay at one turn per
event until L80 counts them.
"""
from __future__ import annotations

import os
import queue
import signal
import threading
import time
import uuid
from typing import Iterator

from . import harness_versions, resident

#: local setting (`ameesh set <agent> resident=on`): off by default, the
#: owner has not chosen which agents are resident (study, question 1)
SETTING = "resident"
#: local setting: inactivity before the process is put to sleep (seconds)
SLEEP_SETTING = "resident_sleep"
#: decision 0035 §5, study §1: 30 min of inactivity by default
DEFAULT_SLEEP_S = 1800
#: protocols whose events the ledger reads as they are (see the module doc)
PROTOCOLS = ("claude-stream-json",)
#: file of the agent's state directory naming the turn in progress, read by
#: the hook instead of `AMEESH_TURN_ID`, fixed when the process started
TURN_FILE = "resident-turn"
#: environment variable that points the hook to `TURN_FILE`
TURN_FILE_ENV = "AMEESH_TURN_ID_FILE"

ON = ("on", "1", "true", "yes")


def current_turn(env=None) -> str:
    """The turn a hook runs in: the content of `TURN_FILE` for a resident
    process (empty between its turns), else `AMEESH_TURN_ID`."""
    env = os.environ if env is None else env
    path = env.get(TURN_FILE_ENV) or ""
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return env.get("AMEESH_TURN_ID") or ""


def write_turn(path: str, turn_id: str) -> None:
    """Names the turn in progress for the hook ('' between turns)."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(turn_id)
    os.replace(tmp, path)


def enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in ON


def sleep_seconds(value: str | None) -> float:
    """The `resident_sleep` setting in seconds; unreadable or empty: default.
    0 = never put to sleep for inactivity."""
    try:
        seconds = float((value or "").strip())
    except ValueError:
        return float(DEFAULT_SLEEP_S)
    if seconds != seconds or seconds < 0:
        return float(DEFAULT_SLEEP_S)
    return seconds


def _binary_stamp(path: str) -> tuple:
    """(path, mtime, size) of the harness binary: a binary replaced on disk
    (update) restarts the process at the next turn (study §1)."""
    try:
        st = os.stat(path)
    except OSError:
        return (path, None, None)
    return (path, st.st_mtime_ns, st.st_size)


def launch_key(adapter, *, model: str | None, effort: str | None, tier: str | None,
               account: str | None, cwd: str) -> tuple:
    return (adapter.descriptor.id, adapter.descriptor.sha256, _binary_stamp(adapter.binary),
            model or "", effort or "", tier or "", account or "", cwd)


_versions: dict[tuple, str | None] = {}
_versions_lock = threading.Lock()


def binary_version(adapter) -> str | None:
    """The version of the harness binary, read once per binary on disk."""
    stamp = _binary_stamp(adapter.binary)
    with _versions_lock:
        if stamp in _versions:
            return _versions[stamp]
    version = harness_versions.probe(adapter.descriptor.id).get("version")
    with _versions_lock:
        _versions[stamp] = version
    return version


def refusal(adapter) -> str:
    """'' if `adapter` can serve this turn resident, else why not."""
    block = adapter.descriptor.resident
    protocol = (block or {}).get("protocol")
    if block and protocol not in PROTOCOLS:
        return ("resident protocol %s: its usage is not counted on the ledger yet "
                "(L80)" % protocol)
    return resident.availability(adapter.descriptor, binary_version(adapter))


class ResidentProcess:
    """One started driver and its events, for the turns it serves."""

    #: grace of a clean stop (SIGTERM to the group, then SIGKILL)
    CLOSE_GRACE = resident.CLOSE_GRACE

    def __init__(self, driver: resident.ResidentDriver, *, key: tuple, life_id: str,
                 stderr=None):
        self.driver = driver
        self.key = key
        self.life_id = life_id
        self._stderr = stderr
        self.events: queue.Queue = queue.Queue()
        self.started = time.monotonic()
        self.last_used = self.started
        self.turns = 0
        #: its `turn_resources` row (under `life_id`) is open
        self.resource_open = False

    @staticmethod
    def new_life_id() -> str:
        return uuid.uuid4().hex

    def on_event(self, raw: dict, parsed: dict) -> None:
        """Callback of the driver (its reader thread): never blocks."""
        self.events.put((raw, parsed))

    @property
    def proc(self):
        return self.driver.proc

    @property
    def pgid(self) -> int | None:
        return self.driver.pgid

    @property
    def alive(self) -> bool:
        return self.driver.alive

    @property
    def session(self) -> str | None:
        return self.driver.session_id

    def drain(self) -> list[tuple[dict, dict]]:
        """Events read outside a turn (late messages of the previous one)."""
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def stream(self, turn: resident.Turn, tick: float = 0.1) -> Iterator[tuple[dict, dict]]:
        """The events of `turn`, until it ends. The driver emits an event
        before it ends the turn, so once the turn is done every event of it
        is already queued."""
        while True:
            try:
                yield self.events.get(timeout=tick)
                continue
            except queue.Empty:
                pass
            if turn.done:
                break
        yield from self.drain()

    def kill_now(self) -> None:
        """SIGKILL to the whole group, nothing written before (lost lease)."""
        pgid = self.pgid
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    def close(self, grace: float) -> None:
        try:
            self.driver.close(grace)
        finally:
            stderr, self._stderr = self._stderr, None
            if stderr is not None:
                try:
                    stderr.close()
                except OSError:
                    pass
