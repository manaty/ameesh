# SPDX-License-Identifier: AGPL-3.0-only
"""Development tooling (L99 T2 #4350, L100 T1 #4382, L101 T4 #4357).

Not a lifecycle module (no descriptor, never selected by the canon): it
provides the `git-merge` action connector, read by the core through
``ameesh.modules.registry.action_connectors()``, and the `git-merge` delivery
connector, read through ``delivery_connectors()``. ``dev`` is a built-in
tooling module with no work item type: it is always active, the canon does
not select it.

L101 T4 (#4357): it also serves the ``workspaces`` capability, the working
directory and housekeeping façade of :mod:`ameesh.workspaces`, obtained by
the core with ``registry.get("dev")``, and the workspaces control of
``doctor`` exposed as the module's ``audit`` hook.

The bare profile (``registry.discover(builtin=False)``) has no ``dev`` module:
the workspaces façade then documents a no-op for housekeeping and worktrees.
"""
from __future__ import annotations

from .git_merge import GitMergeConnector

NAME = "dev"

#: capabilities this module provides (see ``registry.CAPABILITIES``)
CAPABILITIES = ("workspaces", "audit")

def descriptor(options=None):
    """No work item type: tooling lives outside the project lifecycle.

    ``dev`` is always active; selecting it in a project's canon would add no
    type and no rule, so ``declarations()`` refuses it (L101 T4 leaves module
    activation by federation or host to L98 T2)."""
    options = dict(options or {})
    if options:
        raise ValueError("unknown dev module option(s): %s" % ", ".join(sorted(options)))
    return {"types": [], "schemas": {}, "rules": []}


def workspaces():
    """The workspaces façade this module provides (``policy.workspaces``).

    Imported lazily: the core façade reads this module through the registry,
    and importing it at module load would close the core ↔ module cycle. The
    core never imports this implementation (L97); it asks
    ``registry.get("dev").workspaces()``."""
    from ... import workspaces as facade
    return facade


def _workspace_finding(finding, project):
    """One ``canon check`` workspace finding, shaped like a module finding."""
    where = finding.where()
    return {"type": "module_deviation", "module": NAME,
            "rule": finding.code, "project": project, "lot": None,
            "title": where, "agent": finding.agent or None,
            "assignee": finding.agent or None, "lot_state": None,
            "declaration_id": None, "subject": None, "since": None,
            "value": 1, "threshold": 0,
            "reason": "dev:%s:%s" % (finding.code,
                                     finding.host or finding.agent or where),
            "detail": finding.message, "action": finding.message}


def audit(store, project):
    """Doctor's workspaces control (L101 T4): the ``canon check`` findings on
    ``policy.workspaces``, delegated under the Module interface.

    Read-only and thin: the hook reloads the configured canons and keeps the
    existing ``host-work-dir-*`` and ``host-workspaces-unknown`` findings; it
    invents no new check (exploitation alerts such as ``worktree_kept`` stay
    in alerts). No readable canon yields an empty list, like the bare profile
    where this module is absent (``registry.get("dev")`` is None)."""
    from ... import canon as canon_mod, config
    try:
        canons = canon_mod.load_configured(config.load())
    except (OSError, ValueError):
        return []
    out = []
    for canon in canons:
        for finding in canon_mod.validate(canon):
            if (finding.code == "host-workspaces-unknown"
                    or finding.code.startswith("host-work-dir-")):
                out.append(_workspace_finding(finding, project))
    return out


#: connector of a merge on the lot's target; also the gate actions' connector
MERGE_CONNECTOR = "git-merge"


def deliver_git_merge(db, item_id, *, evidence, actor="", **options):
    """A merge on the target, as one of three proofs.

    - ``{"action": <id>}``: a gate action (`git-merge`) confirmed for this lot;
      its merge commit and PR are read from the action, then ``close_merged``
      records them. Returns the ``close_merged`` result (``refused`` included).
    - ``{"seen": <source>[, "sha": S][, "pr": "owner/repo#N"]}``: a merge the
      sweeps and the packet gate observed (`plan_git`, `plan_github`,
      `packet_close`, `merge_pr`, `fire`, L99 T2b #4366); ``close_merged``
      records it, ``options`` are its own (``frozen_id``, ``repo``…).
    - ``{"sha": S}`` or ``{"pr": "owner/repo#N"[, "sha": S]}``: a merge declared
      outside a PR or by a PR of another repository, checked by
      ``work.record_merge`` (`ameesh work merged`); ``options`` are its own."""
    from ... import work
    evidence = dict(evidence or {})
    action_id = evidence.pop("action", None)
    if action_id is not None:
        if evidence or options:
            raise work.WorkError("git-merge: a gate action proof takes nothing else")
        from ... import actions
        action = actions.get(db, action_id)
        if (action is None or action.get("connector") != MERGE_CONNECTOR
                or action.get("state") != "confirmed"
                or str(action.get("work_item") or "") != str(item_id)):
            raise work.WorkError("git-merge: action %s is not a confirmed merge of lot %s"
                                 % (action_id, item_id))
        return work.close_merged(
            db, int(item_id), sha=action.get("external_ref") or "",
            actor=action.get("auth_approver") or actor or MERGE_CONNECTOR,
            source="the gate (action %s)" % action_id, pr_ref=action.get("target"),
            connector=MERGE_CONNECTOR)
    if "seen" in evidence:
        source = evidence.pop("seen")
        extra = set(evidence) - {"sha", "pr"}
        if extra or not source:
            raise work.WorkError("git-merge: an observed merge takes its source, sha "
                                 "and pr, not %s" % (", ".join(sorted(extra)) or "no source"))
        return work.close_merged(
            db, int(item_id), sha=evidence.get("sha") or "", actor=actor, source=source,
            pr_ref=evidence.get("pr"), connector=MERGE_CONNECTOR, **options)
    extra = set(evidence) - {"sha", "pr"}
    if extra:
        raise work.WorkError("git-merge: unknown evidence %s (sha, pr, seen or action)"
                             % ", ".join(sorted(extra)))
    return work.record_merge(db, item_id, sha=evidence.get("sha") or "",
                             pr=evidence.get("pr"), actor=actor, **options)


#: action connectors this provider declares, by name (`connectors.get`)
ACTION_CONNECTORS = {MERGE_CONNECTOR: GitMergeConnector}

#: delivery connectors this provider declares, by name (`work.deliver`)
DELIVERY_CONNECTORS = {MERGE_CONNECTOR: deliver_git_merge}
