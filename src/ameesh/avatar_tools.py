# SPDX-License-Identifier: AGPL-3.0-only
"""#7630: canonical avatar tools, shared by resident and attached sessions."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass

from . import avatar_connectors, canon as canon_mod, events, spawn


@dataclass(frozen=True)
class Applied:
    args: tuple[str, ...]
    proof: dict


def required(canon) -> bool:
    """An omitted policy preserves the historical, journal-only mesh."""
    extensions = (canon.federation or {}).get('extensions', {})
    if not isinstance(extensions, Mapping):
        raise spawn.SpawnToolsError('avatar-tools-policy-invalid', 'extensions must be an object')
    ameesh = extensions.get('ameesh', {})
    if not isinstance(ameesh, Mapping):
        raise spawn.SpawnToolsError('avatar-tools-policy-invalid', 'extensions.ameesh must be an object')
    policy = ameesh.get('action_gate', {})
    if not isinstance(policy, Mapping):
        raise spawn.SpawnToolsError('avatar-tools-policy-invalid', 'action_gate must be an object')
    mode = policy.get('mode', 'journal')
    if mode not in ('journal', 'enforce'):
        raise spawn.SpawnToolsError('avatar-tools-policy-invalid', 'action_gate.mode must be journal or enforce')
    return mode == 'enforce'


def lists(card, canon):
    """Read raw YAML so a malformed declaration never becomes unrestricted."""
    raw = card.fiche.data
    if 'avatar' not in raw:
        return None
    strict = required(canon)
    if 'tools' not in raw:
        if strict or 'deny_tools' in raw:
            raise spawn.SpawnToolsError('avatar-tools-missing', 'avatar requires an explicit tools list')
        return None
    values = []
    for key in ('tools', 'deny_tools'):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip()
                                             or v.startswith('-') for v in value):
            raise spawn.SpawnToolsError('avatar-tools-invalid', '%s must be a list of nonempty tool names' % key)
        values.append(value)
    tools, mcp = spawn.normalize(values[0], None)
    # #6085/#7859: `connector:<name>` and the exact `mcp__<server>__<tool>`
    # read tools of every declared server.
    tools, mcp_tools, connectors = avatar_connectors.split(tools, mcp, strict=strict)
    return tools, mcp, values[1], mcp_tools, connectors


def declared_cwd(agent, fallback: str | None = None) -> str | None:
    """#8350: the agent's declared directory, never a turn-derived cwd.

    `prepare` is given the directory of the turn: a secondary project, a
    worktree, or a cwd inherited from the session after a `cd`. The MCP source
    (`.mcp.json`) is a property of the agent's project, so it is read from the
    declared directory the registry holds (`ameesh show`: `directory`). The
    turn directory is only a fallback when the agent has none (mediated depot,
    older rows); it never overrides a declared directory.
    """
    declared = agent.get('cwd')
    if declared:
        return os.path.abspath(os.path.expanduser(str(declared)))
    return fallback


def prepare(cfg, agent, state_dir, cwd, *, harness=None,
            mcp_root: str | None = None) -> Applied | None:
    if agent.get('created_by') or (agent.get('canon_ref') is None and not agent.get('canon_governed')):
        return None
    owner = None
    card = None
    for one in canon_mod.load_configured(cfg):
        if one.owns(agent.get('canon')) and one.readable:
            owner = one
            card = one.agent(agent['name'])
            break
    if card is None:
        raise spawn.SpawnToolsError('avatar-tools-canon-unavailable', 'canonical agent card unavailable')
    policy = lists(card, owner)
    if policy is None:
        return None
    tools, mcp, deny, mcp_tools, connectors = policy
    harness = harness or agent.get('harness') or ''
    block = spawn.support(harness)
    # Empty allow-lists still require BOTH mechanisms: no inherited tools/MCP.
    # A read-tool/connector policy also needs the private settings transport.
    if (block is None or not block.tools or not block.mcp
            or (deny and not block.deny_flag)
            or ((connectors or mcp_tools) and not block.connectors_flag)):
        raise spawn.SpawnToolsError('avatar-tools-unsupported',
                                   'harness cannot apply the complete avatar tool policy')
    profile = dict(child=agent['name'], harness=harness, tools=tools, mcp=mcp, deny=deny)
    # #8350: a cwd inherited from a turn (session `cd`, secondary project)
    # must not move the MCP source; the mediated depot is the explicit
    # exception, and only the caller knows it.
    source_dir = mcp_root if mcp_root is not None else declared_cwd(agent, cwd)
    path, mcp_path = spawn.write_profile(state_dir, profile, source_dir)
    args = spawn.argv(profile, path)
    files = [path, mcp_path]
    if connectors or mcp_tools:
        settings_path, args = avatar_connectors.apply(
            block, connectors, mcp, mcp_tools, state_dir, args)
        files.append(settings_path)
    else:
        avatar_connectors.remove(state_dir)
    args = tuple(args)
    proof = dict(contract='ameesh-avatar-tools/1', harness=harness, tools=tools,
                 mcp=mcp, deny_tools=deny, digest=_digest(args, files),
                 card=card.fiche.ref, host=getattr(cfg, 'host', None))
    if connectors:
        # #6085: names only (connector and read tools), never content.
        proof['connectors'] = {name: list(allowed) for name, allowed in connectors.items()}
    if mcp_tools:
        # #7859: the exact read tools of each local MCP server.
        proof['mcp_tools'] = {name: list(allowed) for name, allowed in mcp_tools.items()}
    return Applied(args, proof)


def _digest(args, files) -> str:
    """Hash the arguments and the configuration contents, not just names."""
    digest = hashlib.sha256()
    digest.update(repr(list(args)).encode('utf-8'))
    for file in files:
        with open(file, 'rb') as handle:
            digest.update(handle.read())
    return digest.hexdigest()


def settings_path(applied: Applied | None) -> str | None:
    """The private settings file of an applied policy, or None."""
    if applied is None:
        return None
    args = list(applied.args)
    try:
        index = args.index('--settings')
    except ValueError:
        return None
    return args[index + 1] if index + 1 < len(args) else None


def rehash(applied: Applied, state_dir: str) -> Applied:
    """Recompute the proof digest after a settings hook merge (#7863 attach)."""
    args = list(applied.args)
    files = [os.path.join(state_dir, spawn.PROFILE_FILE)]
    if '--mcp-config' in args:
        files.append(os.path.join(state_dir, spawn.MCP_FILE))
    settings = settings_path(applied)
    if settings:
        files.append(settings)
    proof = dict(applied.proof)
    proof['digest'] = _digest(args, files)
    return Applied(applied.args, proof)


def read_tools(applied: Applied | None) -> list[str]:
    """The exact MCP read tools an applied policy journals (#7859)."""
    if applied is None:
        return []
    proof = applied.proof
    return avatar_connectors.read_tools(proof.get('connectors') or {},
                                        proof.get('mcp_tools') or {})


def record(db, name, applied, mode):
    if applied is not None:
        try:
            events.record(db, 'agent.tools', 'agent:' + name, agent=name, mode=mode, **applied.proof)
        except NotImplementedError:
            raise spawn.SpawnToolsError('avatar-tools-proof-unavailable',
                                       'host cannot journal avatar tool application') from None
