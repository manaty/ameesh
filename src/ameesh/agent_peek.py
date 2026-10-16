# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, sanitised runner snapshots for the responsible human (#1349).

The producer runs on the agent's host. Readers never open another host's
workspace or transcript. Only public assistant text and tool names/states
enter the snapshot; thoughts, prompts, arguments and tool output do not.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from . import activity, conversation, db as db_mod, storage

SCHEMA = "ameesh-agent-peek/1"
MAX_ITEMS = 50
TAIL_BYTES = 262144
MAX_LINE = 65536
INTERVAL = 15
STALE_AFTER = 45
_ASSIGNMENT = re.compile(r"\b[A-Za-z_][A-Za-z_0-9]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_COMMANDS = {"git": {"status", "diff", "log", "show", "fetch", "add", "commit", "push", "rebase", "merge"},
             "npm": {"test", "run", "ci", "install"}, "python": set(), "python3": set(),
             "pytest": set(), "rg": set(), "ls": set(), "sed": set(), "curl": set(),
             "ameesh": {"work", "mail", "agent", "deps"}, "agent-mail": {"send", "inbox", "status"}}


def command_label(raw, name):
    """A finite vocabulary of command names/verbs; no free-form argument escapes."""
    item = raw.get("item") or (raw.get("params") or {}).get("item") or {}
    command = item.get("command") if isinstance(item, dict) else None
    if not isinstance(command, str):
        return name
    try:
        words = shlex.split(command)
        if len(words) >= 3 and os.path.basename(words[0]) in ("bash", "sh", "zsh") \
                and words[1] in ("-c", "-lc"):
            words = shlex.split(words[2])
        while words and "=" in words[0]:
            words.pop(0)
        program = os.path.basename(words[0]) if words else ""
        if program in _COMMANDS:
            verb = words[1] if len(words) > 1 and words[1] in _COMMANDS[program] else ""
            return program + (" " + verb if verb else "")
    except ValueError:
        pass
    return name


def clean(value, limit=320):
    # Redact before truncation so a clipped quote/key never escapes its rule.
    text = activity.redact(str(value or ""))
    text = _URL_CREDENTIALS.sub(r"\1[redacted]@", text)
    text = _JWT.sub("[redacted]", text)
    text = _ASSIGNMENT.sub("[redacted assignment]", text)
    text = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def selected_events(path, reader, *, limit=MAX_ITEMS):
    """Latest bounded tail, complete JSON lines only, no protocol guessing."""
    items = deque(maxlen=limit)
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            data = stream.read(TAIL_BYTES)
        lines = data.splitlines()
        if size > TAIL_BYTES:
            lines = lines[1:]  # may start inside a JSON record
    except OSError:
        return []
    if conversation._reader_protocol(reader) is None:
        return []
    for line in lines:
        if len(line) > MAX_LINE:
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict) or raw.get("parent_tool_use_id"):
                continue
            parsed = reader.parse_event(raw)
            for text in conversation.agent_text(raw, parsed, reader=reader):
                if text.strip():
                    items.append({"kind": "message", "summary": clean(text)})
            for name, status in conversation.tool_events(raw):
                items.append({"kind": "tool", "summary": clean(command_label(raw, name), 80),
                              "status": clean(status, 40)})
        except (ValueError, TypeError, AttributeError):
            continue
    return list(items)


def workspace(cwd):
    """Read tracked changes only; no hooks, external diff, textconv or locks."""
    def git(*args):
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", cwd, *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise ValueError("workspace unavailable")
        return result.stdout

    try:
        if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError("workspace unavailable")
        branch = git("branch", "--show-current").strip() or "(detached HEAD)"
        stat = git("diff", "--no-ext-diff", "--no-textconv", "--stat=80,40,20", "HEAD", "--")
        return {"branch": clean(branch, 160), "diff_stat":
                "\n".join(clean(line, 160) for line in stat.splitlines()[:22]),
                "available": True, "scope": "tracked changes against HEAD"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"branch": None, "diff_stat": "", "available": False,
                "scope": "tracked changes against HEAD"}


def sanitise(payload):
    """Allowlist and bounds, applied on publication AND on export."""
    payload = payload if isinstance(payload, dict) else {}
    ws = payload.get("workspace") or {}
    ws = ws if isinstance(ws, dict) else {}
    items = []
    events = payload.get("events")
    for item in (events if isinstance(events, list) else [])[-MAX_ITEMS:]:
        if isinstance(item, dict) and item.get("kind") in ("message", "tool"):
            items.append({"kind": item["kind"], "summary": clean(item.get("summary")),
                          **({"status": clean(item.get("status"), 40)}
                             if item["kind"] == "tool" else {})})
    return {"workspace": {"branch": clean(ws.get("branch"), 160) or None,
                          "diff_stat": "\n".join(clean(line, 160) for line in
                                                 str(ws.get("diff_stat") or "").splitlines()[:22]),
                          "available": ws.get("available") is True,
                          "scope": "tracked changes against HEAD"},
            "events": items}


class Publisher:
    """One daemon per live worker, own bounded-time connection, no model calls."""
    def __init__(self, cfg, *, agent, owner, epoch, cwd, reader, warn=None):
        self.cfg, self.agent, self.owner, self.epoch = cfg, agent, owner, epoch
        self.cwd, self.reader, self.warn = cwd, reader, warn or (lambda _: None)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="peek-" + agent, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)

    def _run(self):
        cfg = dataclasses.replace(self.cfg, statement_timeout_ms=3000, connect_timeout=3)
        while True:
            db = None
            try:
                db = db_mod.connect(cfg)
                if storage.is_remote(db):
                    db.book.hold(self.agent, self.owner, self.epoch)
                payload = sanitise({"workspace": workspace(self.cwd()), "events":
                    selected_events(os.path.join(cfg.agent_dir(self.agent), "events.jsonl"), self.reader())})
                if not storage.of(db).operations.publish_peek(
                        self.agent, self.owner, self.epoch, payload):
                    break  # lease fenced: never overwrite a successor's snapshot
            except Exception:
                # No exception text: a transport error can contain credentials.
                self.warn("agent peek %s: snapshot unavailable" % self.agent)
            finally:
                if db is not None:
                    db.close()
            if self.stop.is_set():
                break
            self.stop.wait(INTERVAL)  # close wakes one final capture, still fenced


def snapshot(db, agent, *, human, limit=10, now=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("limit must be between 1 and 50")
    # The store checks responsibility before selecting private snapshot data.
    row = storage.of(db).operations.read_peek(agent, human)
    if row is None:
        raise PermissionError("unknown agent or reader is not its responsible human")
    payload = sanitise(row.get("payload"))
    ts = row.get("captured_ts")
    now = time.time() if now is None else now
    return {"schema": SCHEMA, "agent": clean(agent, 128), "host": clean(row.get("host"), 128),
            "status": clean(row.get("status"), 80), "status_text": clean(row.get("status_text")),
            "lot": {"id": row.get("lot_id"), "title": clean(row.get("lot_title")),
                    "state": clean(row.get("lot_state"), 40)},
            "captured_ts": ts, "stale": ts is None or now - ts > STALE_AFTER,
            "workspace": payload["workspace"], "events": payload["events"][-limit:]}


def render(data):
    lines = ["%s — %s on %s" % (data["agent"], data["status"], data["host"])]
    if data["status_text"]:
        lines.append("  " + data["status_text"])
    lot = data["lot"]
    lines.append("lot: #%s %s (%s)" % (lot["id"], lot["title"], lot["state"])
                 if lot["id"] else "lot: none recorded")
    lines.append("snapshot: %s%s" % (data["captured_ts"] or "unavailable",
                                    " (stale)" if data["stale"] else ""))
    ws = data["workspace"]
    lines.append("branch: " + (ws["branch"] or "unavailable"))
    if ws["available"]:
        lines.append(ws["diff_stat"] or "tracked changes: none")
    lines.extend("  %s: %s%s" % (e["kind"], e["summary"],
                 " (%s)" % e["status"] if e.get("status") else "") for e in data["events"])
    return "\n".join(lines)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_api(url, token_file, agent, limit):
    """Read the mesh server with its existing human Bearer credential."""
    from . import httpkit
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/", "/api/v1")
            or (parsed.scheme == "http" and not httpkit.is_loopback(parsed.hostname))):
        raise ValueError("server URL requires HTTPS (HTTP allowed on loopback only), without credentials or query")
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    token = httpkit.read_token_file(token_file, what="mesh read API token")
    request = urllib.request.Request(base + "/api/v1/agents/" +
              urllib.parse.quote(agent, safe="") + "/peek?limit=" + str(limit),
              headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError("snapshot response too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError("invalid snapshot response")
        return data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ValueError("mesh snapshot request refused or unavailable") from exc


def main(argv):
    from . import decisions
    from .config import load
    parser = argparse.ArgumentParser(prog="ameesh agent peek")
    parser.add_argument("agent")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--url", help="mesh read API base URL; authenticates with its human token")
    parser.add_argument("--token-file", help="0600 mesh read API token file (with --url)")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= MAX_ITEMS:
        parser.error("limit must be between 1 and 50")
    if args.token_file and not args.url:
        parser.error("--token-file requires --url")
    cfg, db = None, None
    try:
        if args.url:
            if decisions.agent_in_ancestry():
                raise PermissionError("peek is for a human reader outside an agent session")
            from . import serve
            data = read_api(args.url, args.token_file or serve.token_file(), args.agent, args.limit)
        else:
            cfg = load()
            db = db_mod.connect(cfg)
            human = decisions.human_session(cfg, db)
            data = snapshot(db, args.agent, human=human, limit=args.limit)
    except (decisions.DecisionError, PermissionError, ValueError) as exc:
        print("ameesh agent peek: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.DbError:
        print("ameesh agent peek: snapshot service unavailable", file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()
    print(json.dumps(data, ensure_ascii=False) if args.json else render(data))
    return 0
