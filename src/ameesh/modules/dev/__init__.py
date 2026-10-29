# SPDX-License-Identifier: AGPL-3.0-only
"""Development tooling (L99 T2, #4350): the `git-merge` delivery connector.

Not a lifecycle module (no descriptor, never selected by the canon): it only
provides delivery connectors, read by the core through
``ameesh.modules.registry.delivery_connectors()``.
"""
from __future__ import annotations

NAME = "dev"

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


#: connectors this provider declares, by name
DELIVERY_CONNECTORS = {MERGE_CONNECTOR: deliver_git_merge}
