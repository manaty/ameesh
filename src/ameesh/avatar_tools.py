# SPDX-License-Identifier: AGPL-3.0-only
"""#7630: canonical avatar tools, shared by resident and attached sessions."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from . import canon as canon_mod, events, spawn


@dataclass(frozen=True)
class Applied:
    args: tuple[str, ...]
    proof: dict


def required(canon) -> bool:
    """An omitted policy preserves the historical, journal-only mesh."""
    policy = (canon.federation or {}).get('action_gate', {})
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
    return *spawn.normalize(values[0], None), values[1]


def prepare(cfg, agent, state_dir, cwd, *, harness=None) -> Applied | None:
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
    tools, mcp, deny = policy
    harness = harness or agent.get('harness') or ''
    block = spawn.support(harness)
    # Empty allow-lists still require BOTH mechanisms: no inherited tools/MCP.
    if block is None or not block.tools or not block.mcp or (deny and not block.deny_flag):
        raise spawn.SpawnToolsError('avatar-tools-unsupported',
                                   'harness cannot apply the complete avatar tool policy')
    profile = dict(child=agent['name'], harness=harness, tools=tools, mcp=mcp, deny=deny)
    path, mcp_path = spawn.write_profile(state_dir, profile, cwd)
    args = tuple(spawn.argv(profile, path))
    # Hash configuration contents, including MCP transport changes, not just names.
    digest = hashlib.sha256()
    digest.update(repr(args).encode('utf-8'))
    for file in (path, mcp_path):
        with open(file, 'rb') as handle:
            digest.update(handle.read())
    proof = dict(contract='ameesh-avatar-tools/1', harness=harness, tools=tools,
                 mcp=mcp, deny_tools=deny, digest=digest.hexdigest(),
                 card=card.fiche.ref, host=getattr(cfg, 'host', None))
    return Applied(args, proof)


def record(db, name, applied, mode):
    if applied is not None:
        try:
            events.record(db, 'agent.tools', 'agent:' + name, agent=name, mode=mode, **applied.proof)
        except NotImplementedError:
            raise spawn.SpawnToolsError('avatar-tools-proof-unavailable',
                                       'host cannot journal avatar tool application') from None
