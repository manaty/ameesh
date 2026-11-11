# SPDX-License-Identifier: AGPL-3.0-only
"""Shared, additive contract/impact plan (#6181).

Consumers retain their map freshness and execution policies. This module only
unions obligations; it never uses a map to subtract a declared contract.

A project declares its registry in its canon
(``projects.<project>.contract_selection.registry``) and supplies its plan
adapter inputs (inventory, legacy contracts, impacted modules). The engine
carries no project name, path or test-layout convention: the project comes from
the caller or from ``AMEESH_PROJECT``/the configured project.
"""
from __future__ import annotations

import subprocess

from . import contract_selection as cs, mechanics

SCHEMA = 'ameesh-contract-plan/1'


def project_name(cfg=None) -> str:
    """The project of this run, from configuration, never from a literal.

    ``AMEESH_PROJECT`` or the configured project names the canon override and
    the registry. Empty means unknown: the historical N-1 selection applies.
    """
    if cfg is None:
        from . import config
        try:
            cfg = config.load()
        except (OSError, ValueError):
            return ''
    value = getattr(cfg, 'project', '') or ''
    return value.strip() if isinstance(value, str) else ''


def configured_policy(project: str = ''):
    """The policy declared for ``project`` (default: this run's project).

    N-1, an absent override or an unavailable canon keeps the historical
    selection active: absence is never read as a policy.
    """
    from . import config
    try:
        cfg = config.load()
        project = (project or '').strip() or project_name(cfg)
        if not project:
            return None
        document = mechanics.configured(cfg)
        override = document.override(project)
        return override.contract_selection if override else None
    except (OSError, ValueError, mechanics.MechanicsError):
        return None


def policy_from_canon(repo: str, commit: str, project: str, *, deadline=None):
    """Read ``project``'s policy at an exact canon source, or None.

    A host may carry several canons (decision 0031); the prevalidation path
    passes the source that declared the project's block, so this never falls
    back to another project's canon or to a name hard-coded in the engine.
    """
    project = (project or '').strip()
    if not project:
        return None
    document = mechanics.load_global(repo, commit, deadline=deadline)
    override = document.override(project)
    return override.contract_selection if override else None


def git(repo, *args, deadline=None):
    return subprocess.run(['git', '-C', str(repo), '--no-replace-objects', *args],
                          check=True, capture_output=True, text=True, timeout=min(30, deadline.remaining()) if deadline else 30).stdout


def build(repo, commit, base, inventory, legacy, impacted, policy, *, deadline=None):
    """Build one JSON-serializable plan from exact candidate Git objects.

    No policy means the legacy union, with no new full-suite requirement.
    Rename detection is deliberately disabled: both its deletion and addition
    are judged, even when Git would classify it as a copy or rename.
    """
    if isinstance(inventory, str):
        raise cs.RegistryError('invalid candidate module inventory')
    inventory = sorted(set(inventory))
    if any(not cs.module_name(m) for m in inventory):
        raise cs.RegistryError('invalid candidate module inventory')
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
    # The selector expands to the entire inventory on uncertainty so CI can
    # run the full suite. That expansion is not a contract obligation: local
    # packet/prevalidation passes retain legacy and explicitly matched or
    # universal rules, while full_suite_required carries the CI obligation.
    declared = {module for module, reasons, _ in selection.evidence
                if any(reason.startswith(('rule:', 'universal:'))
                       for reason in reasons)} if selection else set()
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
            # The candidate's complete module universe, bound to the same SHA.
            # A second project's adapter carries its own layout (Jest files,
            # paths); consumers verify the registry against this list instead
            # of re-discovering a Python `tests/` tree.
            'inventory': inventory,
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
    inventory = plan.get('inventory')
    if inventory is not None:
        # Additive: an N-1 block without inventory stays readable; when present
        # it is the exact candidate universe used by the selector.
        if not isinstance(inventory, list) \
                or any(not cs.module_name(m) for m in inventory) \
                or inventory != sorted(set(inventory)):
            raise ValueError('invalid contract plan inventory')
    if set(plan['modules']) != set(plan['contracts']) | set(plan['impacted']):
        raise ValueError('contract plan union differs')
    return tuple(plan['modules'])
