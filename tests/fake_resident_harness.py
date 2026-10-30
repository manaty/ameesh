#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake resident harness of the L75 contract tests: no network, no model, no cost.

It speaks the protocol its arguments name, as observed on the real harnesses
(2026-10-12, Claude Code 2.1.286, Codex CLI 0.159.3):

* `--input-format stream-json` → Claude Code stream-json: `control_request`
  `initialize` and `interrupt`, `user` lines echoed with their `uuid`
  (`--replay-user-messages`), a message written during a turn joins it at the
  next safe point, one `result` per turn with a cumulative `total_cost_usd`;
* `app-server` → Codex app-server, JSON-RPC 2.0 without the `jsonrpc` member:
  `initialize`, `thread/start|resume`, `turn/start`, `turn/steer`,
  `turn/interrupt`, `thread/read`, notifications `turn/started`,
  `item/completed`, `thread/tokenUsage/updated`, `turn/completed`.

The text of an input drives the turn: `SLOW` (a tool call of 0.6 s, inputs
written meanwhile join the turn), `CRASH` (exit 3 mid-turn), `ERROR` (failed
turn), `ASK` (the harness asks the client for a permission and reports the
answer), `FLOOD` (Claude: writes `FAKE_CLAUDE_FLOOD` bytes of output before
reading its input again), `ORPHAN` (a child leaves the group with `setsid`
and keeps the output open; its pid is traced). The answer of a turn is
`ECHO:<text>` per input served. `DEAF` (Claude): the input is never read
again; `ORPHAN_IN`: the orphan also keeps the input; `FAKE_CODEX_STEER_HOLD=1`:
the answer to `turn/steer` waits for the next `turn/interrupt`, then refuses.

Environment: `FAKE_RESIDENT_LOG` (JSONL trace of argv and of what was
received), `FAKE_CLAUDE_QUEUE=1` (an input written during a turn is not
injected: it opens the next harness turn, as when it arrives just after the
last tool call), `FAKE_CLAUDE_PUSH_AFTER` (seconds after an `initialize`
answer before the harness pushes its `system/commands_changed` event, as the
real CLI does asynchronously — #1337), `FAKE_CLAUDE_MODEL_USAGE` (JSON
`modelUsage` map, CTX-03), `FAKE_COMPACT_NO_POST=1` (a compaction boundary
without an after, CTX-03), `FAKE_CODEX_STEER_REFUSE=1` (`turn/steer` always
refused), `FAKE_START_DELAY` (seconds before answering `initialize`).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time

LOG = os.environ.get("FAKE_RESIDENT_LOG")
SLOW_S = 0.6


def trace(entry: dict) -> None:
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def out(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reader(inbox: queue.Queue) -> None:
    for line in sys.stdin:
        line = line.strip()
        if line:
            try:
                inbox.put(json.loads(line))
            except ValueError:
                pass
    inbox.put(None)


class Claude:
    def __init__(self, argv: list[str]):
        self.session = "sess-new"
        for flag in ("--session-id", "--resume"):
            if flag in argv:
                self.session = argv[argv.index(flag) + 1]
        self.cost = 0.5 if "--resume" in argv else 0.0
        self.inbox: queue.Queue = queue.Queue()
        self.queued: list[dict] = []
        self.resume = threading.Event()

    def next_message(self, timeout: float | None) -> dict | None | bool:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return False

    def control(self, message: dict, turn: bool) -> None:
        request = message.get("request") or {}
        trace({"control": request.get("subtype")})
        if request.get("subtype") == "initialize":
            time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))
        out({"type": "control_response", "response": {
            "subtype": "success", "request_id": message.get("request_id"),
            "response": {"still_queued": []}}})
        self.push_after_initialize(request)

    def push_after_initialize(self, request: dict) -> None:
        """#1337 : the CLI pushes `system/commands_changed` asynchronously
        after an `initialize` answer (observed on 2.1.296: ~1.4 s later, well
        after the drain that precedes the next turn's send). The delay makes
        the push land in a turn that is already running."""
        if request.get("subtype") != "initialize":
            return
        try:
            delay = float(os.environ.get("FAKE_CLAUDE_PUSH_AFTER", "0") or 0)
        except ValueError:
            return
        if delay <= 0:
            return
        timer = threading.Timer(delay, self.push)
        timer.daemon = True
        timer.start()

    def push(self) -> None:
        """One fire-and-forget push of the harness, outside any turn."""
        out({"type": "system", "subtype": "commands_changed", "session_id": self.session,
             "uuid": "push-%d" % time.monotonic_ns(),
             "commands": [{"name": "probe-push", "description": "fake"}]})
        trace({"push": "commands_changed"})

    def replay(self, message: dict) -> str:
        out(dict(message, session_id=self.session, isReplay=True))
        content = (message.get("message") or {}).get("content") or [{}]
        return str(content[0].get("text") or "")

    def read_input(self) -> None:
        """The reader of the input. After a `FLOOD` input, it reads nothing
        until the turn has written its output (a harness busy with it)."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self.inbox.put(message)
            if message.get("type") == "user" and "FLOOD" in json.dumps(message):
                self.resume.wait(30)
            if message.get("type") == "user" and "DEAF" in json.dumps(message):
                time.sleep(60)  # never reads its input again
        self.inbox.put(None)

    def run(self) -> None:
        threading.Thread(target=self.read_input, daemon=True).start()
        while True:
            message = self.queued.pop(0) if self.queued else self.next_message(None)
            if message is None:
                return
            if message.get("type") == "control_request":
                self.control(message, False)
            elif message.get("type") == "user":
                self.turn(message)
            elif message.get("type") == "control_response":
                trace({"control_response": message.get("response")})

    def call_usage(self) -> dict:
        """Usage of a model call. `FAKE_CLAUDE_CONTEXT` sets the context the
        harness reports (input + cache read + cache creation), for the L81
        compaction thresholds; without it the historical 10+200+30."""
        total = os.environ.get("FAKE_CLAUDE_CONTEXT")
        if total:
            return {"input_tokens": int(total), "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 7}
        return {"input_tokens": 10, "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 30, "output_tokens": 7}

    def compact(self) -> None:
        """L81 (étude §6.2): the boundary a native `/compact` produces —
        `pre_tokens`/`post_tokens`, unless `FAKE_COMPACT_SKIP=1` (a harness
        that reports none), `FAKE_COMPACT_NO_POST=1` (an after never
        published, CTX-03) or `FAKE_COMPACT_POST` forces the after."""
        if os.environ.get("FAKE_COMPACT_SKIP") == "1":
            return
        pre = int(os.environ.get("FAKE_COMPACT_PRE")
                  or os.environ.get("FAKE_CLAUDE_CONTEXT") or "240")
        post = int(os.environ.get("FAKE_COMPACT_POST") or max(pre // 5, 1))
        meta = {"trigger": "manual", "pre_tokens": pre}
        if os.environ.get("FAKE_COMPACT_NO_POST") != "1":
            meta["post_tokens"] = post
        out({"type": "system", "subtype": "compact_boundary", "session_id": self.session,
             "compact_metadata": meta})

    def turn(self, first: dict) -> None:
        out({"type": "system", "subtype": "init", "session_id": self.session,
             "model": "fake-model"})
        texts = [self.replay(first)]
        trace({"user": texts[0]})
        # #2894 : the harness's own turn document, as handed at launch. Kept
        # in a separate entry so existing exact-entry assertions still hold.
        context_path = os.environ.get("AMEESH_TURN_CONTEXT")
        if context_path:
            try:
                with open(context_path, encoding="utf-8") as fh:
                    trace({"turn_context": json.load(fh)})
            except (OSError, ValueError) as exc:
                trace({"turn_context_error": str(exc)})
        if texts[0].strip() == "/compact":
            self.compact()
        if "ORPHAN" in texts[0]:
            orphan(keep_input="ORPHAN_IN" in texts[0])
        if "FLOOD" in texts[0]:
            # the reader is paused: the input is not read while the output
            # is written (a harness busy with its tool output)
            total, chunk = int(os.environ.get("FAKE_CLAUDE_FLOOD", "4000000")), "x" * 65536
            while total > 0:
                out({"type": "assistant", "session_id": self.session, "message": {
                    "id": "msg-%d" % total, "content": [{"type": "text", "text": chunk}],
                    "usage": {"input_tokens": 1}}})
                total -= len(chunk)
            self.resume.set()
        interrupted = False
        if "CRASH" in texts[0]:
            sys.exit(3)
        if "ASK" in texts[0]:
            out({"type": "control_request", "request_id": "h-1",
                 "request": {"subtype": "can_use_tool", "tool_name": "Bash"}})
            answer = self.next_message(5)
            trace({"ask_answer": answer})
            texts.append("asked:%s" % ((answer or {}).get("response") or {}).get("subtype"))
        if "SLOW" in texts[0]:
            out({"type": "assistant", "session_id": self.session, "message": {
                "id": "msg-tool", "content": [{"type": "tool_use", "name": "Bash"}],
                "usage": {"input_tokens": 5, "cache_read_input_tokens": 100}}})
            deadline = time.monotonic() + SLOW_S
            while time.monotonic() < deadline:
                message = self.next_message(max(deadline - time.monotonic(), 0.01))
                if message is False:
                    continue
                if message is None:
                    return
                if message.get("type") == "control_request":
                    self.control(message, True)
                    if (message.get("request") or {}).get("subtype") == "interrupt":
                        interrupted = True
                        break
                elif message.get("type") == "user":
                    if os.environ.get("FAKE_CLAUDE_QUEUE") == "1":
                        self.queued.append(message)
                    else:
                        injected = self.replay(message)
                        trace({"user": injected, "injected": True})
                        texts.append(injected)
            out({"type": "user", "session_id": self.session, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
        self.cost = round(self.cost + 0.01, 6)
        usage = self.call_usage()
        extra = {}
        window = os.environ.get("FAKE_CLAUDE_WINDOW")
        if os.environ.get("FAKE_CLAUDE_MODEL_USAGE"):
            # CTX-03: an explicit `modelUsage` map (e.g. an auxiliary model
            # first, then the principal announced by the init event).
            extra["modelUsage"] = json.loads(os.environ["FAKE_CLAUDE_MODEL_USAGE"])
        elif window:
            extra["modelUsage"] = {"fake-model": {"contextWindow": int(window)}}
        if interrupted:
            out({"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "result": None, "session_id": self.session, "total_cost_usd": self.cost,
                 "usage": usage, **extra})
            return
        answer = "\n".join("ECHO:%s" % t for t in texts)
        out({"type": "assistant", "session_id": self.session, "message": {
            "id": "msg-final", "content": [{"type": "text", "text": answer}],
            "usage": usage}})
        if "ERROR" in texts[0]:
            out({"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "errors": ["boom"], "session_id": self.session,
                 "total_cost_usd": self.cost, "usage": usage, **extra})
            return
        out({"type": "result", "subtype": "success", "is_error": False, "result": answer,
             "session_id": self.session, "total_cost_usd": self.cost, "usage": usage,
             **extra})


class Codex:
    def __init__(self, argv: list[str]):
        self.inbox: queue.Queue = queue.Queue()
        self.thread = None
        self.turn_seq = 0
        self.active: dict | None = None
        self.next_id = 1000
        self.held_steer = None
        #: L80a: the thread's cumulative usage, as `tokenUsage.total` reports
        #: it (the fake is otherwise stateless across turns)
        self.tokens = {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0}

    def reply(self, mid, result=None, error=None) -> None:
        out({"id": mid, "error": error} if error else {"id": mid, "result": result})

    def notify(self, method: str, params: dict) -> None:
        out({"method": method, "params": dict(params, threadId=self.thread)})

    def run(self) -> None:
        threading.Thread(target=reader, args=(self.inbox,), daemon=True).start()
        while True:
            timeout = None
            if self.active is not None:
                timeout = max(self.active["deadline"] - time.monotonic(), 0.0)
            try:
                message = self.inbox.get(timeout=timeout)
            except queue.Empty:
                self.complete("completed")
                continue
            if message is None:
                return
            if "jsonrpc" in message:
                trace({"error": "jsonrpc member sent"})
            self.handle(message)

    def complete(self, status: str) -> None:
        turn = self.active
        self.active = None
        if status == "completed":
            answer = "\n".join("ECHO:%s" % t for t in turn["texts"])
            self.notify("item/completed", {"turnId": turn["id"], "item": {
                "type": "commandExecution", "id": "i1"}})
            self.notify("item/completed", {"turnId": turn["id"], "item": {
                "type": "agentMessage", "id": "i2", "text": answer}})
        last = {"inputTokens": 300, "cachedInputTokens": 200, "outputTokens": 9,
                "reasoningOutputTokens": 0, "totalTokens": 309}
        for key in self.tokens:
            self.tokens[key] += last[key]
        self.notify("thread/tokenUsage/updated", {"turnId": turn["id"], "tokenUsage": {
            "last": last,
            "total": dict(self.tokens, reasoningOutputTokens=0,
                          totalTokens=self.tokens["inputTokens"] + self.tokens["outputTokens"]),
            "modelContextWindow": 258400}})
        error = {"message": "boom"} if status == "failed" else None
        self.notify("turn/completed", {"turn": {"id": turn["id"], "status": status,
                                                "error": error, "items": []}})

    def handle(self, message: dict) -> None:
        method, mid = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method is None:
            trace({"answer": message.get("result"), "error": message.get("error")})
            if self.active is not None and self.active.get("asking") == mid:
                self.active["texts"].append("asked:%s" % json.dumps(
                    message.get("result"), sort_keys=True))
                self.active["deadline"] = time.monotonic()
            return
        trace({"method": method, "params": params})
        if method == "initialize":
            time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))
            self.reply(mid, {"userAgent": "fake", "codexHome": "/tmp",
                             "platformFamily": "unix", "platformOs": "linux"})
        elif method == "initialized":
            pass
        elif method == "thread/start":
            self.thread = "thr-new"
            self.reply(mid, {"thread": {"id": self.thread}, "model": params.get("model") or "m0",
                             "approvalPolicy": "never", "cwd": params.get("cwd")})
        elif method == "thread/resume":
            self.thread = params.get("threadId")
            self.reply(mid, {"thread": {"id": self.thread}, "model": "m0"})
        elif method == "thread/read":
            self.reply(mid, {"thread": {"id": self.thread}})
        elif method == "turn/start":
            if self.active is not None:
                self.reply(mid, error={"code": -32600, "message": "turn already running"})
                return
            self.turn_seq += 1
            text = params["input"][0]["text"]
            turn = {"id": "turn-%d" % self.turn_seq, "texts": [text],
                    "deadline": time.monotonic() + (SLOW_S if "SLOW" in text else 0.05)}
            self.reply(mid, {"turn": {"id": turn["id"], "status": "inProgress"}})
            self.notify("turn/started", {"turn": {"id": turn["id"], "status": "inProgress"}})
            if "CRASH" in text:
                sys.exit(3)
            if "ORPHAN" in text:
                orphan()
            self.active = turn
            if "ERROR" in text:
                self.notify("error", {"turnId": turn["id"], "willRetry": False,
                                      "error": {"message": "boom"}})
                self.complete("failed")
            elif "ASK" in text:
                self.next_id += 1
                turn["asking"] = self.next_id
                turn["deadline"] = time.monotonic() + 5
                out({"id": self.next_id, "method": "item/commandExecution/requestApproval",
                     "params": {"threadId": self.thread, "turnId": turn["id"],
                                "itemId": "i0", "command": "rm -rf /"}})
        elif method == "turn/steer":
            if os.environ.get("FAKE_CODEX_STEER_HOLD") == "1" and self.active is not None:
                self.held_steer = mid  # refused after the next interrupt
                return
            if os.environ.get("FAKE_CODEX_STEER_REFUSE") == "1" or self.active is None \
                    or params.get("expectedTurnId") != self.active["id"]:
                self.reply(mid, error={"code": -32600, "message": "no active turn to steer"})
                return
            self.active["texts"].append(params["input"][0]["text"])
            self.reply(mid, {"turnId": self.active["id"]})
        elif method == "turn/interrupt":
            self.reply(mid, {})
            if self.active is not None and self.active["id"] == params.get("turnId"):
                self.complete("interrupted")
            if self.held_steer is not None:
                self.reply(self.held_steer,
                           error={"code": -32600, "message": "no active turn to steer"})
                self.held_steer = None
        else:
            self.reply(mid, error={"code": -32601, "message": "unknown method %s" % method})


def orphan(keep_input: bool = False) -> None:
    """A child that leaves the group (setsid) and keeps the output open (and
    the input, never read, with `keep_input`)."""
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True,
                             stdin=None if keep_input else subprocess.DEVNULL)
    trace({"orphan": child.pid})


def main() -> None:
    argv = sys.argv[1:]
    trace({"argv": argv})
    if "app-server" in argv:
        Codex(argv).run()
    elif "--input-format" in argv:
        Claude(argv).run()
    else:
        sys.exit("fake_resident_harness: unknown protocol %r" % argv)


if __name__ == "__main__":
    main()
