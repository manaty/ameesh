# SPDX-License-Identifier: AGPL-3.0-only
"""#6085: read-only account connectors (claude.ai Slack, Gmail…) for an avatar.

An avatar card declares a connector and, by exact name, every tool of it the
avatar may call:

    tools:
      - Read
      - mcp:memoria
      - connector:claude_ai_Slack
      - mcp__claude_ai_Slack__slack_read_thread
      - mcp__claude_ai_Slack__slack_search_public_and_private

Why a separate mechanism (Claude Code 2.1.319, checked 2026-11-19):

* claude.ai connectors are account-level, never in `.mcp.json`;
  `--strict-mcp-config` does not load them at all, and
  `ENABLE_CLAUDEAI_MCP_SERVERS` can only switch them off.
* `--tools` filters built-in tools only: every tool of a loaded MCP server
  stays callable, and `--allowedTools` is moot under
  `--dangerously-skip-permissions`.

So, only when a card declares a connector, the strict flag is replaced by a
private settings file (`--settings`, flag settings outrank user settings):

1. `allowedMcpServers` admits the declared `mcp:` servers by name and each
   declared connector by the URL pattern of the harness descriptor; every
   other server (user, project, plugin, other connectors) is not loaded;
2. a `PreToolUse` hook, whose matcher is every `mcp__` tool *except* the
   declared servers and the connector tools named in the card, refuses the
   call (exit 2). The hook runs under bypass permissions. A tool that a
   connector adds later is not in the card, so it is refused.

Known limits (documented, not hidden): a managed policy can still override
flag settings (`allowManagedHooksOnly`, its own `allowedMcpServers`); the
refused tools stay visible to the model, they cannot run.
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
REFUSAL = ("echo 'ameesh: tool not in the avatar card (#6085 connector allow-list)' >&2; "
           "exit 2")


def split(tools: Iterable[str], mcp: Iterable[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Separate built-in tools from connector declarations and their tools.

    Returns (built-in tools, {connector: [allowed tool names]}). A connector
    without a named tool, or a connector tool whose connector is not
    declared, is refused: a declaration never widens to "every tool".
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
    builtin = []
    declared_mcp = set(mcp)
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
        if server not in connectors:
            raise spawn.SpawnToolsError(
                'avatar-connector-tool-orphan',
                'tool %r names connector %r, which the card does not declare (connector:%s)%s'
                % (value, server, server,
                   '; an mcp: server already exposes all its tools' if server in declared_mcp else ''))
        if value not in connectors[server]:
            connectors[server].append(value)
    for name, allowed in connectors.items():
        if not allowed:
            raise spawn.SpawnToolsError(
                'avatar-connector-without-tools',
                'connector %r declares no tool: name each allowed tool '
                '(mcp__%s__<tool>)' % (name, name))
    return builtin, connectors


def _tool_prefix(server: str) -> str:
    """The `mcp__<server>__` prefix Claude Code gives a server's tools."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', server)


def settings(block: spawn.Support, connectors: Mapping[str, list[str]],
             mcp: Iterable[str]) -> dict:
    """The private settings document that applies the connector policy."""
    known = dict(block.connectors_known)
    allowed = [{'serverName': name} for name in mcp]
    for name in connectors:
        if name not in known:
            raise spawn.SpawnToolsError(
                'avatar-connector-unknown',
                'harness %r does not know connector %r (known: %s)'
                % (block.harness, name, ', '.join(sorted(known)) or 'none'))
        allowed.append({'serverUrl': known[name]})
    keep = ['mcp__%s__' % re.escape(_tool_prefix(name)) for name in mcp]
    keep += ['(?:%s)$' % '|'.join(re.escape(tool) for tool in tools)
             for tools in connectors.values()]
    # Every MCP tool is refused unless it starts with a declared server's
    # prefix or is exactly one of the connector tools named in the card.
    matcher = '^mcp__' if not keep else '^(?!%s)mcp__' % '|'.join(keep)
    return {
        'allowedMcpServers': allowed,
        'disableAllHooks': False,
        'hooks': {'PreToolUse': [{'matcher': matcher,
                                  'hooks': [{'type': 'command', 'command': REFUSAL}]}]},
    }


def apply(block: spawn.Support, connectors: Mapping[str, list[str]], mcp: Iterable[str],
          state_dir: str, args: Iterable[str]) -> tuple[str, list[str]]:
    """Write the settings file; return (its path, the harness arguments)."""
    if not block.connectors_flag:
        raise spawn.SpawnToolsError('avatar-tools-unsupported',
                                   'harness %r cannot apply account connectors' % block.harness)
    document = settings(block, connectors, list(mcp))
    path = spawn.write_private(
        os.path.join(state_dir, SETTINGS_FILE),
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    out = [value for value in args if value not in block.connectors_replaces]
    out += [value.format(path=path) for value in block.connectors_flag]
    return path, out


def remove(state_dir: str) -> None:
    """A card that drops its connectors leaves no stale settings behind."""
    try:
        os.unlink(os.path.join(state_dir, SETTINGS_FILE))
    except FileNotFoundError:
        pass
