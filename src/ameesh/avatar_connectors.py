# SPDX-License-Identifier: AGPL-3.0-only
"""#6085/#7859: read-only MCP tools and account connectors for an avatar.

An avatar card declares the MCP servers it may use, and, tool by tool, the
read-only tools it may call:

    tools:
      - Read
      - mcp:memoria
      - mcp__memoria__inbox
      - mcp__memoria__search
      - mcp__memoria__ask
      - mcp__memoria__read
      - connector:claude_ai_Slack
      - mcp__claude_ai_Slack__slack_read_thread
      - mcp__claude_ai_Slack__slack_search_public_and_private

Why a separate mechanism (Claude Code 2.1.319/2.1.328, checked 2026-11-19):

* claude.ai connectors are account-level, never in `.mcp.json`;
  `--strict-mcp-config` does not load them at all, and
  `ENABLE_CLAUDEAI_MCP_SERVERS` can only switch them off.
* `--tools` filters built-in tools only: every tool of a loaded MCP server
  stays callable, and `--allowedTools` is moot under
  `--dangerously-skip-permissions`.

So the strict flag is replaced by a private settings file (`--settings`, flag
settings outrank user settings) as soon as the card declares an exact read
tool or a connector:

1. `allowedMcpServers` admits the declared `mcp:` servers by name and each
   declared connector by the URL pattern of the harness descriptor; every
   other server (user, project, plugin, other connectors) is not loaded;
2. a `PreToolUse` hook, whose matcher is every `mcp__` tool *except* the
   declared read tools, refuses the call (exit 2). The hook runs under bypass
   permissions. A tool that a server or connector adds later is not in the
   card, so it is refused: a server upgrade cannot silently add a write tool.
3. a `PostToolUse` hook journals the allowed reads through
   `agent-mail read-journal` (tool, target identifiers and date, never the
   content).

In `enforce` mode every declared `mcp:` server must name at least one exact
tool: a whole-server allow-list would let a later version add a write tool
without a canon change. In `journal` mode a server without exact tools keeps
the historical prefix behavior.

Known limits (documented, not hidden): a managed policy can still override
flag settings (`allowManagedHooksOnly`, its own `allowedMcpServers`); refused
tools stay visible to the model, they cannot run.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping

from . import spawn

SETTINGS_FILE = "avatar-connectors.json"
PREFIX = "connector:"
_CONNECTOR_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOOL_RE = re.compile(r"mcp__([A-Za-z0-9_-]+?)__([A-Za-z0-9_-]+)")
REFUSAL = ("echo 'ameesh: tool not in the avatar card (#6085/#7859 read-only allow-list)' >&2; "
           "exit 2")
JOURNAL = "agent-mail read-journal"


def split(tools: Iterable[str], mcp: Iterable[str], *, strict: bool = False
          ) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Separate built-in tools, exact MCP tool lists and connectors.

    Returns (built-in tools, {mcp server: [exact tools]},
    {connector: [exact tools]}). A connector without a named tool, or an
    `mcp__<server>__<tool>` whose server is not declared, is refused: a
    declaration never widens to "every tool". In strict (`enforce`) mode an
    `mcp:` server must name its read tools too.
    """
    tools = list(tools)
    connectors: dict[str, list[str]] = {}
    for value in tools:
        if value.startswith(PREFIX):
            name = value[len(PREFIX):]
            if not _CONNECTOR_RE.fullmatch(name):
                raise spawn.SpawnToolsError('avatar-connector-invalid',
                                           'connector name is invalid: %r' % name)
            connectors.setdefault(name, [])
    servers = set(mcp)
    if servers & set(connectors):
        both = ', '.join(sorted(servers & set(connectors)))
        raise spawn.SpawnToolsError(
            'avatar-connector-conflict',
            '%s declared both as mcp: and connector:; one transport per server' % both)
    mcp_tools: dict[str, list[str]] = {}
    builtin = []
    for value in tools:
        if value.startswith(PREFIX):
            continue
        match = _TOOL_RE.fullmatch(value) if value.startswith('mcp__') else None
        if value.startswith('mcp__') and match is None:
            raise spawn.SpawnToolsError('avatar-connector-tool-invalid',
                                       'MCP tool name is invalid: %r' % value)
        if match is None:
            builtin.append(value)
            continue
        server = match.group(1)
        if server in connectors:
            if value not in connectors[server]:
                connectors[server].append(value)
            continue
        if server in servers:
            mcp_tools.setdefault(server, []).append(value)
            continue
        raise spawn.SpawnToolsError(
            'avatar-connector-tool-orphan',
            'tool %r names server %r, which the card does not declare '
            '(mcp:%s or connector:%s)' % (value, server, server, server))
    for name, allowed in connectors.items():
        if not allowed:
            raise spawn.SpawnToolsError(
                'avatar-connector-without-tools',
                'connector %r declares no tool: name each allowed tool '
                '(mcp__%s__<tool>)' % (name, name))
    if strict:
        for name in sorted(servers - set(mcp_tools)):
            raise spawn.SpawnToolsError(
                'avatar-mcp-without-tools',
                'mcp server %r declares no read tool: in enforce mode name each '
                'allowed tool (mcp__%s__<tool>)' % (name, name))
    return builtin, mcp_tools, connectors


def _tool_prefix(server: str) -> str:
    """The `mcp__<server>__` prefix Claude Code gives a server's tools."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', server)


def _exact(names: Iterable[str]) -> str:
    values = sorted(set(re.escape(value) for value in names))
    return '(?:%s)$' % '|'.join(values) if values else ''


def settings(block: spawn.Support, connectors: Mapping[str, list[str]],
             mcp: Iterable[str], mcp_tools: Mapping[str, list[str]]) -> dict:
    """The private settings document that applies the read-only policy."""
    known = dict(block.connectors_known)
    mcp = list(mcp)
    allowed = [{'serverName': name} for name in mcp]
    for name in connectors:
        if name not in known:
            raise spawn.SpawnToolsError(
                'avatar-connector-unknown',
                'harness %r does not know connector %r (known: %s)'
                % (block.harness, name, ', '.join(sorted(known)) or 'none'))
        allowed.append({'serverUrl': known[name]})
    # Exact tools are allowed by name; a journal-mode `mcp:` server without an
    # exact list keeps the historical prefix (its whole tool set).
    exact = []
    keep = []
    for name in mcp:
        tools = list(mcp_tools.get(name) or ())
        if tools:
            exact.extend(tools)
            keep.append(_exact(tools))
        else:
            keep.append('mcp__%s__' % re.escape(_tool_prefix(name)))
    for tools in connectors.values():
        exact.extend(tools)
        keep.append(_exact(tools))
    matcher = '^mcp__' if not keep else '^(?!%s)mcp__' % '|'.join(keep)
    hooks: dict[str, list] = {
        'PreToolUse': [{'matcher': matcher,
                        'hooks': [{'type': 'command', 'command': REFUSAL}]}],
    }
    if exact:
        # PostToolUse only runs for an allowed call; the command records the
        # tool and its identifiers, never the arguments' content.
        hooks['PostToolUse'] = [{
            'matcher': _exact(exact),
            'hooks': [{'type': 'command', 'command': JOURNAL, 'timeout': 10}],
        }]
    return {
        'allowedMcpServers': allowed,
        'disableAllHooks': False,
        'hooks': hooks,
    }


def apply(block: spawn.Support, connectors: Mapping[str, list[str]],
          mcp: Iterable[str], mcp_tools: Mapping[str, list[str]],
          state_dir: str, args: Iterable[str]) -> tuple[str, list[str]]:
    """Write the settings file; return (its path, the harness arguments)."""
    if not block.connectors_flag:
        raise spawn.SpawnToolsError(
            'avatar-tools-unsupported',
            'harness %r cannot apply connector/read-tool settings' % block.harness)
    document = settings(block, connectors, list(mcp), mcp_tools)
    path = spawn.write_private(
        os.path.join(state_dir, SETTINGS_FILE),
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    if connectors:
        # Claude does not load claude.ai connectors under --strict-mcp-config.
        out = [value for value in args if value not in block.connectors_replaces]
    else:
        # Exact mcp: read tools do not need the connector transport: keep the
        # strict flag that already excludes every other server.
        out = list(args)
    out += [value.format(path=path) for value in block.connectors_flag]
    return path, out


def read_tools(connectors: Mapping[str, list[str]],
               mcp_tools: Mapping[str, list[str]]) -> list[str]:
    """The exact read tools journaled for this policy, in card order."""
    names = []
    for tools in mcp_tools.values():
        names.extend(tools)
    for tools in connectors.values():
        names.extend(tools)
    return list(dict.fromkeys(names))


def merge(path: str, extra: Mapping) -> None:
    """Append hooks (an attach wake, #7863) to an existing settings file.

    The avatar settings file is the only `--settings` argument a Claude session
    accepts in practice; the attach wake therefore merges its SessionStart and
    Stop hooks here instead of asking for a second flag. The caller rehashes
    the applied policy after the merge.
    """
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("avatar settings is not an object")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("avatar settings `hooks` is not an object")
    for event, entries in (extra.get("hooks") or {}).items():
        if not isinstance(entries, list):
            raise ValueError("hook entries are not a list")
        target = hooks.setdefault(event, [])
        if not isinstance(target, list):
            raise ValueError("existing hook entries are not a list")
        target.extend(entries)
    spawn.write_private(path, json.dumps(document, ensure_ascii=False,
                                        sort_keys=True, indent=1) + "\n")


def remove(state_dir: str) -> None:
    """A card that drops its read policy leaves no stale settings behind."""
    try:
        os.unlink(os.path.join(state_dir, SETTINGS_FILE))
    except FileNotFoundError:
        pass
