# SPDX-License-Identifier: AGPL-3.0-only
"""Shared, additive contract/impact plan (#6181).

Consumers retain their map freshness and execution policies. This module only
unions obligations; it never uses a map to subtract a declared contract.
"""
from __future__ import annotations

import subprocess

from . import contract_selection as cs, mechanics

SCHEMA = 'ameesh-contract-plan/1'


def configured_policy(project: str):
    """N-1 or an unavailable canon keeps the historical selection active."""
    from . import config
    try:
        document = mechanics.configured(config.load())
        override = document.override(project)
        return override.contract_selection if override else None
    except (OSError, ValueError, mechanics.MechanicsError):
        return None


def git(repo, *args, deadline=None):
    return subprocess.run(['git', '-C', str(repo), '--no-replace-objects', *args],
                          check=True, capture_output=True, text=True, timeout=min(30, deadline.remaining()) if deadline else 30).stdout


def build(repo, commit, base, inventory, legacy, impacted, policy, *, deadline=None):
    """Build one JSON-serializable plan from exact candidate Git objects.

    No policy means the legacy union, with no new full-suite requirement.
    Rename detection is deliberately disabled: both its deletion and addition
    are judged, even when Git would classify it as a copy or rename.
    """
    inventory = sorted(set(inventory))
    legacy, impacted = set(legacy), set(impacted)
    selection = None
    if policy is not None:
        try:
            diff = git(repo, 'diff', '--name-status', '-z', '--no-renames', base, commit, deadline=deadline)
            fields = diff.rstrip('\0').split('\0') if diff else []
            changes = [cs.Change(fields[i], fields[i + 1])
                       for i in range(0, len(fields), 2)]
            selection = cs.load_and_select(str(repo), commit, policy, changes, inventory,
                                           deadline=deadline)
        except (OSError, subprocess.SubprocessError, IndexError, mechanics.MechanicsError,
                cs.RegistryError):
            selection = cs.select(None, (), inventory, commit=commit,
                                  registry_error='candidate-input-unreadable')
    declared = set(selection.modules) if selection else set()
    contracts = sorted(legacy | declared)
    evidence = selection.to_dict()['evidence'] if selection else {}
    for module in contracts:
        entry = evidence.setdefault(module, {'reasons': [], 'paths': []})
        if module in legacy:
            entry['reasons'] = sorted(set(entry['reasons']) | {'legacy-contract'})
    for module in impacted:
        entry = evidence.setdefault(module, {'reasons': [], 'paths': []})
        entry['reasons'] = sorted(set(entry['reasons']) | {'map:P0/P1'})
    return {'schema': SCHEMA, 'commit': commit, 'base': base,
            'registry_digest': selection.registry_digest if selection else None,
            'active': policy is not None, 'contracts': contracts,
            'impacted': sorted(impacted), 'modules': sorted(set(contracts) | impacted),
            'evidence': {m: evidence[m] for m in sorted(evidence)},
            'fallback_reasons': list(selection.fallback_reasons) if selection else [],
            'full_suite_required': bool(selection and selection.full_suite_required)}


def consume(plan):
    """Read the common block; reject a malformed/partially dropped union."""
    if not isinstance(plan, dict) or plan.get('schema') != SCHEMA:
        raise ValueError('invalid contract plan schema')
    for key in ('contracts', 'impacted', 'modules'):
        names = plan.get(key)
        if not isinstance(names, list) or any(not isinstance(m, str) for m in names) \
                or names != sorted(set(names)):
            raise ValueError('invalid contract plan ' + key)
    if set(plan['modules']) != set(plan['contracts']) | set(plan['impacted']):
        raise ValueError('contract plan union differs')
    return tuple(plan['modules'])
