# SPDX-License-Identifier: AGPL-3.0-only
"""Project-scoped action policies compiled from trusted module selections.

The reserved rule is compiled by the core, never supplied by a descriptor.
It shares the existing applied module-policy store and has no lifecycle types.
"""
from __future__ import annotations

RULE = 'ameesh.action-classes'
# Existing development connector predates project-scoped module actions.
# Its default classification and always-active tooling policy remain compatible.
LEGACY = frozenset(('git-merge',))


def compile_policy(name, module, descriptor, classes):
    from .actions import ACTION_CLASSES, _IDENT_RE
    if any(rule.get('kind') == RULE for rule in descriptor['rules']):
        raise ValueError('module %s: action classes must come from the canon' % name)
    if not isinstance(classes, dict):
        raise ValueError('module %s: action_classes must be a mapping' % name)
    declared = getattr(module, 'ACTION_CONNECTORS', {})
    for connector, operations in classes.items():
        if connector not in declared or not isinstance(operations, dict):
            raise ValueError('module %s: invalid action_classes connector %r' % (name, connector))
        for operation, action_class in operations.items():
            if (not isinstance(operation, str) or not _IDENT_RE.fullmatch(operation)
                    or action_class not in ACTION_CLASSES):
                raise ValueError('module %s: invalid action class for %s/%s'
                                 % (name, connector, operation))
    if not classes:
        return descriptor
    return dict(descriptor, rules=[*descriptor['rules'],
                                  dict(kind=RULE, types=[], connectors=classes)])


def _provider_of(connector, *, require_installed=True):
    """Provider serving a module connector name; None for core and legacy APIs.

    An installed provider that differs from the wrapper's is always refused:
    the action would be read from another module's connector. With
    `require_installed=False` (reconciliation), a name no longer served by any
    module is accepted: the outcome already happened and reading it must stay
    possible after the module is uninstalled.
    """
    from . import actions, connectors
    from .modules import registry
    if connector.name in connectors.CORE_NAMES:
        return None
    entry = registry.action_connectors().get(connector.name)
    if entry is None:
        if require_installed and isinstance(connector, connectors.ModuleConnector):
            raise actions.ActionError(actions.CONNECTOR, 'module connector is no longer installed')
        return None  # Existing direct-connector API, no module implementation.
    provider, _factory = entry
    if isinstance(connector, connectors.ModuleConnector) and provider != connector.provider:
        raise actions.ActionError(actions.CONNECTOR, 'module connector provider changed')
    return provider


def policy_class(db, connector, project, operation):
    """Return a module operation's applied class; unregistered legacy APIs keep defaults."""
    from . import actions
    from .modules import registry
    provider = _provider_of(connector)
    if provider is None:
        return None
    policy = registry.action_policy(db, project, provider)
    if policy is None and connector.name in LEGACY:
        return None
    if policy is None:
        raise actions.ActionError(actions.CONNECTOR,
                                  'module %s is not active for project %s' % (provider, project))
    classes = [rule['connectors'].get(connector.name, {}).get(operation)
               for rule in policy['rules'] if rule.get('kind') == RULE]
    if len(classes) != 1 or classes[0] not in actions.ACTION_CLASSES:
        raise actions.ActionError(actions.CLASS, 'no canon class for module operation %s/%s'
                                  % (connector.name, operation))
    return classes[0]


def check_action(db, action, connector=None):
    """Recheck activation and class before approval and launch."""
    from . import actions
    from types import SimpleNamespace
    chosen = policy_class(db, connector or SimpleNamespace(name=action['connector']),
                          action['project'], action['operation'])
    if chosen is not None and chosen != action['class']:
        raise actions.ActionError(actions.CLASS, 'module operation class changed; propose a new action')


def check_reconcile(action, connector=None):
    """Recheck only the provider before a read-only reconciliation.

    Reconciliation observes an outcome that already happened: withdrawing the
    project policy, changing the operation's class or uninstalling the module
    must not leave an `unknown` action forever. Approval and launch keep the
    full activation check (`check_action`); a connector name now served by
    another provider is still refused.
    """
    from types import SimpleNamespace
    _provider_of(connector or SimpleNamespace(name=action['connector']),
                 require_installed=False)
