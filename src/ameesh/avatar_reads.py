# SPDX-License-Identifier: AGPL-3.0-only
"""#7859: journal the MCP reads of an avatar, never their content.

Claude Code cannot restrict the tools of an MCP server with `--tools`; the
avatar policy therefore allows an exact list of read tools (see
`avatar_connectors`). This module records each allowed call after the fact:

    agent.read   one read tool called by an avatar: agent, tool, target
                 identifiers (channel, thread, message, file…) and outcome;
                 never the prompt, query, message text or response.

The hook is `agent-mail read-journal`, declared in the avatar's private
settings with a matcher limited to the exact read tools of the card. It reads
the harness hook JSON on stdin and exits 0 on every path: a journal failure
must never fail the read or the turn. `AMEESH_AVATAR_READ_TOOLS` (set by the
runner next to `AMEESH_AVATAR_TOOLS_DIGEST`) is the second guard: a tool that
is not in the applied policy is not recorded, even if the matcher were wrong.
"""
from __future__ import annotations

import json
import os

#: Identifier fields that are a target, never a content. Order matters when a
#: call names several (Gmail: thread_id; Slack: channel_id and thread_ts).
TARGET_KEYS = ("channel_id", "channel", "thread_ts", "thread_id",
               "message_id", "message_ts", "file_id", "attachment_id", "source_id", "item_id",
               "email_id", "draft_id", "user_id", "id", "ts")
TARGET_LIMIT = 3
VALUE_LIMIT = 160


def read_tools(env=None) -> set[str]:
    """The exact read tools of the applied policy (empty: nothing to journal)."""
    raw = (env if env is not None else os.environ).get("AMEESH_AVATAR_READ_TOOLS", "")
    return {value.strip() for value in str(raw).split(",") if value.strip()}


def target(tool_input) -> dict:
    """The identifier fields of a tool input; never a text, query or content."""
    if not isinstance(tool_input, dict):
        return {}
    out = {}
    for key in TARGET_KEYS:
        if len(out) >= TARGET_LIMIT:
            break
        value = tool_input.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if text and len(text) <= VALUE_LIMIT:
            out[key] = text
    return out


def event(data: dict, env=None) -> dict | None:
    """The `agent.read` fields for one hook payload, or None (ignored).

    The hook only receives PostToolUse/PostToolUseFailure after the call: the
    tool was allowed by the PreToolUse refusal hook.
    """
    if not isinstance(data, dict):
        return None
    if (data.get("hook_event_name") or "") not in ("PostToolUse", "PostToolUseFailure"):
        return None
    tool = str(data.get("tool_name") or "")
    if not tool.startswith("mcp__") or tool not in read_tools(env):
        return None
    name = (env if env is not None else os.environ).get("AGENT_MAIL_NAME") or ""
    if not name:
        return None
    return {
        "agent": name,
        "tool": tool,
        "target": target(data.get("tool_input")),
        "outcome": "error" if data.get("hook_event_name") == "PostToolUseFailure" else "ok",
    }


def journal(cfg, raw: str, *, env=None, db=None) -> bool:
    """Record one allowed MCP read; never raises (the hook must not fail)."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return False
    fields = event(data, env)
    if fields is None:
        return False
    opened = None
    try:
        from . import db as db_mod, events
        connection = db
        if connection is None:
            connection = opened = db_mod.connect(cfg)
        events.record(connection, "agent.read", "agent:" + fields["agent"],
                      project=getattr(cfg, "project", None), **fields)
        return True
    except Exception:  # noqa: BLE001 - a read journal never fails the read
        return False
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass
