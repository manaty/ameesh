# SPDX-License-Identifier: AGPL-3.0-only
"""L78: shared conversation detail, R12 outbox and nonblocking harness sink.

`Log` is the synchronous application API (L77 inputs, L79 history).
`Recorder.on_event(raw, parsed)` is safe for L75 reader threads: it only
queues bounded, selected fields. Its writer owns a separate connection.
No tool arguments, output, reasoning or protocol envelope are persisted.
"""
from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections.abc import Mapping

from . import db as db_mod, fil, storage

SCHEMA = "ameesh-conversation/1"
CHANNEL = "ameesh_chat"
TEXT_LIMIT = 20_000
THREAD_LIMIT = 100_000
DETAIL_DAYS = 90
SLICE_SECONDS = 0.5
KINDS = frozenset(("human_message", "input", "text", "tool", "turn_end", "state"))


def identifier(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or "\0" in value:
        raise ValueError("conversation identifier must contain 1 to 256 characters")
    return value


def bounded(text, limit=TEXT_LIMIT):
    value = str(text or "").replace("\0", "�")
    return value[:limit], len(value) > limit


def metadata(value):
    """Explicit allowlist: no raw protocol dictionaries or tool outputs."""
    value = value or {}
    out = {}
    for key in ("status", "tool"):
        if isinstance(value.get(key), str):
            out[key] = bounded(value[key], 256)[0]
    for key in ("cost_usd", "duration_s", "dropped"):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) \
                and math.isfinite(number) and number >= 0:
            out[key] = number
    if isinstance(value.get("usage"), Mapping):
        usage = {}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "cached_input_tokens",
                    "context_tokens", "context_window"):
            number = value["usage"].get(key)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                usage[key] = number
        out["usage"] = usage
    return out


class Log:
    def __init__(self, db, cfg, *, agent, project=None, lot=None):
        self.db, self.cfg = db, cfg
        self.agent = identifier(agent)
        self.project = identifier(project or fil.project_for(cfg))
        self.lot = identifier(str(lot) if lot is not None else None, optional=True)
        self.store = storage.of(db).conversations

    def append(self, kind, text="", *, author=None, session=None, turn=None,
               event_id=None, meta=None, thread_text=None, truncated=False):
        if kind not in KINDS:
            raise ValueError("unknown conversation kind: %s" % kind)
        if thread_text is not None and kind not in ("human_message", "turn_end"):
            raise ValueError("only human messages and final answers go to the R12 outbox")
        if kind == "human_message" and thread_text is None:
            thread_text = text
        short, cut = bounded(text)
        full, thread_cut = bounded(thread_text, THREAD_LIMIT)
        if thread_cut:
            full = full[:THREAD_LIMIT - 40] + "\n[conversation text truncated]"
        return self.store.append(
            agent=self.agent, kind=kind, text=short, truncated=cut or thread_cut or truncated,
            author=identifier(author or "agent:" + self.agent),
            session=identifier(session, optional=True), turn=identifier(turn, optional=True),
            event_id=identifier(event_id, optional=True), meta=metadata(meta),
            project=self.project, lot=self.lot,
            thread_text=full if thread_text is not None else None)

    def drain_threads(self, *, limit=32):
        """At-least-once delivery; a failed post remains pending past retention."""
        done = 0
        with self.db.transaction() as tx:
            store = storage.of(tx).conversations
            for row in store.pending_threads(self.agent, limit=limit):
                entry = fil.record(
                    self.cfg, tx, sender=row["author"],
                    recipients=[self.agent] if row["kind"] == "human_message" else [],
                    text=row["thread_text"], ts=row["ts"], project=row["project"], lot=row["lot"],
                    meta={"conversation": {"agent": self.agent, "seq": row["seq"],
                                            "turn": row["turn"]}})
                if entry is None:
                    break
                store.thread_delivered(self.agent, int(row["seq"]), entry)
                done += 1
        return done

    def prune(self, *, now=None, days=DETAIL_DAYS):
        if not isinstance(days, (int, float)) or not math.isfinite(days) or days <= 0:
            raise ValueError("conversation retention must be positive")
        return self.store.prune(self.agent, before_ts=(time.time() if now is None else now)
                                - days * 86400)


def history(db, agent, *, after=0, limit=100):
    identifier(agent)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ValueError("conversation cursor must be a nonnegative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("conversation page limit must be between 1 and 500")
    page = storage.of(db).conversations.read(agent, after=after, limit=limit)
    oldest, last = page["oldest_seq"], int(page["last_seq"])
    entries = page["entries"]
    return dict(page, schema=SCHEMA, agent=agent,
                next_seq=int(entries[-1]["seq"]) if entries else max(after, last),
                gap=bool(after < last and (oldest is None or after < int(oldest) - 1)))


def attach_progress(db, agents, *, limit=20):
    """One bounded read for all cards; only their requested agents are returned."""
    pages = storage.of(db).conversations.recent([a["name"] for a in agents], limit=limit)
    grouped = {page["agent"]: page for page in pages}
    for agent in agents:
        page = grouped.get(agent["name"], {"entries": [], "last_seq": 0})
        agent["conversation"] = {"schema": SCHEMA, "entries": page["entries"],
                                 "last_seq": int(page["last_seq"]),
                                 "limit": limit}


def tool_events(raw):
    """Select names/states only, across Claude, Codex and the ACP bridge."""
    if not isinstance(raw, Mapping):
        return []
    if raw.get("type") == "assistant":
        message = raw.get("message") or {}
        if not isinstance(message, Mapping):
            return []
        return [(str(c.get("name") or "tool")[:256], "started")
                for c in message.get("content") or [] if isinstance(c, Mapping)
                and c.get("type") == "tool_use"]
    if raw.get("type") == "user":
        message = raw.get("message") or {}
        if not isinstance(message, Mapping):
            return []
        return [("tool", "completed") for c in message.get("content") or []
                if isinstance(c, Mapping) and c.get("type") == "tool_result"]
    if raw.get("type") == "tool":
        return [(str(raw.get("name") or raw.get("title") or "tool")[:256],
                 str(raw.get("status") or "updated")[:256])]
    method = raw.get("method") or raw.get("type") or ""
    if method in ("item.started", "item.completed", "item/started", "item/completed"):
        container = raw.get("params") or raw
        if not isinstance(container, Mapping):
            return []
        item = container.get("item") or {}
        if not isinstance(item, Mapping):
            return []
        kind = item.get("type") or ""
        if kind and kind not in ("agentMessage", "agent_message", "userMessage",
                                 "reasoning", "plan", "contextCompaction"):
            return [(str(item.get("name") or kind)[:256],
                     "completed" if method.endswith("completed") else "started")]
    return []


class Recorder:
    """One turn, one bounded queue and one writer-owned connection.

    On saturation, live detail can be dropped with a counted state entry.
    Inputs/final answers use `append`/`finish` outside the reader callback;
    they wait for queue capacity instead of being dropped as live deltas.
    """
    def __init__(self, cfg, *, agent, turn, session=None, project=None, lot=None,
                 connect=None, capacity=256, warn=None):
        if not isinstance(capacity, int) or not 1 <= capacity <= 4096:
            raise ValueError("conversation queue capacity must be between 1 and 4096")
        self.cfg, self.agent, self.turn = cfg, identifier(agent), identifier(turn)
        self.session = identifier(session, optional=True)
        self.project, self.lot = project, lot
        self.connect = connect or (lambda: db_mod.connect(cfg))
        self.warn = warn or fil.warn
        self.queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self.dropped = 0
        self.closed = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name="conversation-" + agent,
                                       daemon=True)
        self.thread.start()

    def _put(self, event, *, reliable=False):
        with self._lock:
            if self.closed or self.error is not None:
                return False
        try:
            if reliable:
                self.queue.put(event, timeout=5)
            else:
                self.queue.put_nowait(event)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def on_event(self, raw, parsed):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = {}
        # Subagent output is not the principal's conversation.
        if isinstance(raw, Mapping) and raw.get("parent_tool_use_id"):
            return
        session = parsed.get("session")
        if isinstance(session, str):
            self._put(("session", session[:256]))
        tools = tool_events(raw)
        for name, status in tools:
            self._put(("tool", name, {"tool": name, "status": status}))
        # Codex reports `final` on each completed agent message (including
        # commentary). Keep it live; only finish publishes the final to R12.
        texts = ([parsed["final"]] if "final" in parsed else parsed.get("display") or [])
        for text in texts:
            if not str(text).startswith(("[tool]", "[permission", "thinking:")):
                self._put(("text", *bounded(text)))

    def append(self, kind, text, **kwargs):
        short, cut = bounded(text)
        if kind == "human_message":
            full, full_cut = bounded(text, THREAD_LIMIT)
            kwargs["thread_text"] = full
            cut = cut or full_cut
        kwargs["truncated"] = cut
        return self._put(("entry", kind, short, kwargs), reliable=True)

    def finish(self, *, final="", status="completed", cost=None, usage=None, duration_s=None):
        full, cut = bounded(final, THREAD_LIMIT)
        if cut:
            full = full[:THREAD_LIMIT - 40] + "\n[conversation text truncated]"
        meta = metadata({"status": status, "cost_usd": cost, "usage": usage,
                         "duration_s": duration_s})
        accepted = self._put(("finish", full, meta, cut), reliable=True)
        if not accepted:
            self.warn("conversation %s: final answer not queued" % self.agent)
        return accepted

    def close(self, timeout=10):
        # Closing can wait at a turn boundary; on_event never waits.
        with self._lock:
            if self.closed:
                return not self.thread.is_alive()
            self.closed = True
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            self.warn("conversation %s: writer still draining" % self.agent)
        return not self.thread.is_alive()

    def _run(self):
        db = None
        try:
            db = self.connect()
            log = Log(db, self.cfg, agent=self.agent, project=self.project, lot=self.lot)
            log.drain_threads()
            log.prune()
            parts, size, deadline, clipped = [], 0, None, False

            def flush():
                nonlocal parts, size, deadline, clipped
                if parts:
                    log.append("text", "".join(parts), session=self.session, turn=self.turn,
                               truncated=clipped)
                parts, size, deadline, clipped = [], 0, None, False

            while True:
                timeout = max(0, deadline - time.monotonic()) if deadline else SLICE_SECONDS
                try:
                    event = self.queue.get(timeout=timeout)
                except queue.Empty:
                    flush()
                    with self._lock:
                        if self.closed and self.queue.empty():
                            break
                    continue
                kind = event[0]
                if kind == "text":
                    text = event[1]
                    if size + len(text) > TEXT_LIMIT:
                        flush()
                    parts.append(text)
                    clipped = clipped or event[2]
                    size += len(text)
                    if deadline is None:
                        deadline = time.monotonic() + SLICE_SECONDS
                    if size >= TEXT_LIMIT or time.monotonic() >= deadline:
                        flush()
                    continue
                flush()
                if kind == "session":
                    self.session = event[1]
                elif kind == "tool":
                    log.append("tool", event[1], meta=event[2],
                               session=self.session, turn=self.turn)
                elif kind == "entry":
                    log.append(event[1], event[2], session=self.session, turn=self.turn,
                               **event[3])
                    log.drain_threads()
                elif kind == "finish":
                    final, meta = event[1:3]
                    log.append("turn_end", final, meta=meta, session=self.session, turn=self.turn,
                               event_id="final:" + self.turn[:250], truncated=event[3],
                               thread_text=final if final.strip() else None)
                    log.drain_threads()
            if self.dropped:
                log.append("state", "%d live events dropped: queue saturated" % self.dropped,
                           meta={"dropped": self.dropped}, session=self.session, turn=self.turn)
        except Exception as exc:
            self.error = str(exc)
            self.warn("conversation %s unavailable: %s" % (self.agent, db_mod.explain(exc, 200)))
        finally:
            if db is not None:
                db.close()
