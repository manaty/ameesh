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
#: local setting: cumulative re-read of a resident session before rotation
#: (L81, study §6.2); 0 = never rotate on the cumulative re-read alone
REREAD_MAX_SETTING = "resident_reread_max"
#: study §6.2: a session that re-read about 50 M tokens is rotated
DEFAULT_REREAD_MAX = 50_000_000
#: study §6.2: native compaction when the context reaches half the window
COMPACT_SHARE = 0.5
#: L81 (étude §6.2) : après une demande de compaction, on laisse ce délai
#: avant d'en redemander une tant que le contexte mesuré n'est pas retombé —
#: une compaction qui n'aboutit pas ne tourne pas en boucle
COMPACT_RETRY_S = 600.0
#: study §6.3: never during a turn, and not within 2 minutes after a human
#: message — except for a hard overrun
QUIET_S = 120.0
#: what a finished resident turn asks for (L81, study §6.2)
COMPACT = "compact"
#: the message that asks Claude Code for a native compaction (study §6.2)
COMPACT_COMMAND = "/compact"
#: the session's cumulative re-read passed its threshold (study §6.2): this
#: rotation waits for a quiet moment, unlike the hard L60 ceiling
ALERT = "reread-alert"
ROTATE = "rotate"

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


def reread_max(value: str | None) -> float:
    """The `resident_reread_max` setting in tokens; unreadable or empty:
    default. 0 = never rotate on the cumulative re-read alone."""
    try:
        tokens = float((value or "").strip())
    except ValueError:
        return float(DEFAULT_REREAD_MAX)
    if tokens != tokens or tokens < 0:
        return float(DEFAULT_REREAD_MAX)
    return tokens


def turn_reread(usage: dict | None) -> int:
    """Tokens a turn re-read (sum of its model calls): the measure of the L60
    context ceiling, carried by `TurnResult.usage`."""
    return sum(int((usage or {}).get(key) or 0) for key in
               ("input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens"))


def maintenance(*, context: int | None, window: int | None, reread: int,
                session_reread: int, cap: float = 0, reread_max: float = 0,
                share: float = COMPACT_SHARE, since_compact: float | None = None,
                compact_retry_s: float = 0.0) -> str:
    """What a finished resident session asks for (L81, study §6.2).

    `ROTATE`: the per-agent L60 ceiling is reached on the turn's re-read — a
    hard bound, acted on at the first passage between turns. `ALERT`: the
    session's cumulative re-read passed `reread_max` (the study's alert
    threshold) — acted on when quiet. `COMPACT`: the context fills `share` of
    its window — acted on when quiet too, and not again within
    `compact_retry_s` of the last request (`since_compact`), so that a
    compaction which does not land never loops. Rotation wins over
    compaction: a compaction would not bring the session back under a hard
    bound."""
    if cap and reread >= cap:
        return ROTATE
    if reread_max and session_reread >= reread_max:
        return ALERT
    if context and window and share > 0 and context >= share * window:
        if (since_compact is not None and compact_retry_s > 0
                and since_compact < compact_retry_s):
            return ""
        return COMPACT
    return ""


def _binary_stamp(path: str) -> tuple:
    """(path, mtime, size) of the harness binary: a binary replaced on disk
    (update) restarts the process at the next turn (study §1)."""
    try:
        st = os.stat(path)
    except OSError:
        return (path, None, None)
    return (path, st.st_mtime_ns, st.st_size)


#: variables of a turn's environment that name the turn itself: replaced by
#: the life id in the resident's environment, never part of its launch key
PER_TURN_ENV = ("AMEESH_TURN_ID", "AMEESH_CONTAINER_LABELS")


def _env_digest(env) -> str:
    import hashlib
    items = sorted((k, v) for k, v in (env or {}).items() if k not in PER_TURN_ENV)
    return hashlib.sha256(repr(items).encode("utf-8", "surrogateescape")).hexdigest()


def launch_key(adapter, *, model: str | None, effort: str | None, tier: str | None,
               account: str | None, cwd: str, env=None) -> tuple:
    """What the process was started with. `env`: the turn's environment — its
    hook reads settings there (`AMEESH_TURN_MAIL_MAX`…), frozen at launch:
    one that changes restarts the process (codex r1, #417)."""
    return (adapter.descriptor.id, adapter.descriptor.sha256, _binary_stamp(adapter.binary),
            model or "", effort or "", tier or "", account or "", cwd, _env_digest(env))


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
        #: L81 measurement (study §6.1): context of the last call, its window,
        #: tokens re-read by the last turn and since the session opened
        self.last_context: int | None = None
        self.context_window: int | None = None
        self.last_reread = 0
        self.session_reread = 0
        #: L81 (étude §6.2) : compaction native — jetons avant/après la
        #: dernière observée, nombre de compactions vues, et instant de la
        #: dernière demande (le délai `COMPACT_RETRY_S` empêche la boucle)
        self.compactions = 0
        self.compact_pre: int | None = None
        self.compact_post: int | None = None
        self.last_compact = 0.0
        #: its `turn_resources` row (under `life_id`) is open
        self.resource_open = False
        #: a close is under way (`AgentWorker._close_resident`)
        self.closing = False

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

    def record_turn(self, result) -> None:
        """L81 measurement of a finished turn (study §6.1): the context of its
        last call, the window when the harness gives it, the tokens the turn
        re-read and the cumulative re-read of the session. A native compaction
        reported in the turn (study §6.2) is recorded too: the context then
        follows its `post`."""
        usage = getattr(result, "usage", None) or {}
        context = usage.get("context_tokens")
        if context is not None:
            self.last_context = int(context)
        window = usage.get("context_window")
        if window:
            self.context_window = int(window)
        self.last_reread = turn_reread(usage)
        self.session_reread += self.last_reread
        compact = getattr(result, "compact", None)
        if isinstance(compact, dict):
            self.compactions += 1
            self.compact_pre, self.compact_post = compact.get("pre"), compact.get("post")
            self.last_compact = time.monotonic()
            if compact.get("post") is not None:
                self.last_context = int(compact["post"])

    def compact_requested(self) -> None:
        """L81 (étude §6.2) : une compaction native vient d'être demandée — le
        délai de reprise court, qu'elle aboutisse ou non (pas de boucle)."""
        self.last_compact = time.monotonic()

    def maintenance_due(self, *, cap: float = 0, reread_max: float = 0,
                        share: float = COMPACT_SHARE,
                        compact_retry_s: float = COMPACT_RETRY_S) -> str:
        """What this session asks for after its last turn (L81): '' while it
        may go on, else `COMPACT`, `ALERT` or `ROTATE` (study §6.2)."""
        since = time.monotonic() - self.last_compact if self.last_compact else None
        return maintenance(context=self.last_context, window=self.context_window,
                           reread=self.last_reread, session_reread=self.session_reread,
                           cap=cap, reread_max=reread_max, share=share,
                           since_compact=since, compact_retry_s=compact_retry_s)

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
        """SIGKILL to the whole group, nothing written before (lost lease).

        Never a reused group (codex r1 and r2, #417): the leader is a
        `resident.LeaderPopen`, reaped only after its group is killed, under
        the same lock as this signal — while unreaped, its pid (the group's
        number) cannot be given to another process."""
        proc = self.proc
        if proc is None:
            return
        signal_group = getattr(proc, "signal_group", None)
        if signal_group is not None:
            signal_group(signal.SIGKILL)
        elif proc.returncode is None and self.pgid:
            # a plain Popen (no WNOWAIT on this system): best effort
            try:
                os.killpg(self.pgid, signal.SIGKILL)
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
