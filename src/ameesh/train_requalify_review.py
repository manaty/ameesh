# SPDX-License-Identifier: AGPL-3.0-only
"""#4037: exact recorded opinions for a host-local reviewed canon move.

References are milestone:<id>, never assertions parsed from prose. Reviewers
can record a canon opinion from another host with work milestone --repo.
The old policy remains a floor: the proposed canon cannot waive its own review.
"""
from __future__ import annotations

import re

from . import (assignments, mechanics, merge_queue, packet_candidates, resolutions,
               review_authors, review_lot, review_provenance, storage, train_assembly,
               work)
from .train import TrainError


def _role(cfg, db, name, *, desk=False):
    try:
        return assignments.is_orchestrator(
            cfg, db, name, roles=(*work.CORRECTOR_ROLES, "bureau") if desk
            else work.CORRECTOR_ROLES, history=False, strict=True)
    except Exception as exc:
        raise TrainError("reviewed requalification authority unreadable: %s" % exc) from exc


def actor(cfg, db):
    """Bound pilot or desk role; no caller-supplied identity or role widening."""
    from . import identity
    binding = identity.resolve_binding(cfg, db)
    if not binding.ok or not binding.name:
        raise TrainError("reviewed requalification requires a valid bound identity")
    if not _role(cfg, db, binding.name, desk=True):
        raise TrainError("reviewed requalification requires a pilot or bureau role")
    return binding.name


def _milestone_receipt(db, number):
    try:
        return review_provenance.receipt_for_milestone(db, number)
    except review_provenance.ProvenanceError as exc:
        raise TrainError("reviewed requalification provenance refused: %s" % exc) from exc


def _clean_merge_tree(repo, first, second):
    """The tree a conflict-free merge of two commits produces, or None.

    ``git merge-tree --write-tree`` (git >= 2.38) replays the hosting forge's
    merge without a working tree. A conflict is not an error: the merge then
    carries its author's resolution and needs provenance. Any other failure
    refuses the qualification instead of guessing.
    """
    trial = packet_candidates.merge_tree(repo, first, second)
    if trial["state"] == "conflict":
        return None
    if trial["state"] != "clean":
        raise TrainError("cannot compute the clean canon merge of %s and %s: %s"
                         % (first[:12], second[:12], trial.get("detail") or "?"))
    return trial["tree"]


def qualify(cfg, db, spec, pin, gate, changed, references):
    """Read exact provenance and current verdicts before any configuration write."""
    if not references:
        raise TrainError("reviewed requalification requires --review milestone:<id>")
    rules = [mechanics.load(spec.canon, commit, spec.name) for commit in (pin, gate)]
    families = tuple(dict.fromkeys(family for rule in rules for family in rule.families))
    selected, lots = {}, set()
    for reference in dict.fromkeys(references):
        match = re.fullmatch(r"milestone:([1-9][0-9]*)", reference)
        if not match:
            raise TrainError("unsupported review reference %r: milestone:<id> required"
                             % reference)
        number = int(match[1])
        receipt = _milestone_receipt(db, number)
        origin = storage.of(db).provenance.get(reference)
        if not receipt or not origin or not origin.get("work_item_id"):
            raise TrainError("%s: exact review provenance missing" % reference)
        mark = receipt.get("milestone") or {}
        if receipt["commit"] != gate or mark.get("verdict") != "ok":
            raise TrainError("%s: an ok opinion on the exact gate commit is required"
                             % reference)
        selected[number] = receipt
        lots.add(int(origin["work_item_id"]))
    # Opposition belongs to the canon commit, even on an uncited source lot.
    marks = storage.of(db).work.verdicts_for_commit(gate)
    # Ask the common gate for opposition on the complete history. Its result
    # is not an authorization here: pilot assent and diff qualification are
    # checked below. This avoids maintaining a second definition of a block.
    opposition = merge_queue.review_gate(
        db, marks, gate, repo=spec.canon, klass={"class": "sensitive"},
        families=families, authors=[], authors_complete=True)
    if opposition["blocked"]:
        raise TrainError("reviewed requalification has a current blocking opinion")
    latest = {}
    for mark in sorted(marks, key=lambda m: (m["at_ts"], m["id"])):
        receipt = selected.get(mark["id"]) or _milestone_receipt(
            db, mark["id"])
        # #4299: a rename rewrites the milestone actor, never the receipt.
        if (receipt and receipt["commit"] == gate
                and (receipt["actor"] == mark["actor"]
                     or storage.of(db).agents.same_identity(receipt["actor"], mark["actor"]))
                and (receipt.get("milestone") or {}).get("verdict") == mark["verdict"]):
            latest[mark["actor"]] = mark
    current = [mark for mark in latest.values() if mark["id"] in selected]
    if not any(_role(cfg, db, mark["actor"]) for mark in current):
        raise TrainError("reviewed requalification requires a current pilot opinion")
    guard = set(changed).intersection(train_assembly.REQUALIFY_GUARD)
    if guard:
        authors = []
        source_items = [work.get(db, lot) for lot in lots]
        commits = resolutions._run(spec.canon, "rev-list", "--parents", pin + ".." + gate)
        if commits.returncode:
            raise TrainError("cannot read canon authors")
        for line in resolutions._text(commits).splitlines():
            commit, *parents = line.split()
            if len(parents) > 1:
                # A hosting merge whose tree is exactly a parent adds no
                # content. So does a clean merge: its tree is the mechanical
                # result of git merge-tree on its two parents, and the parents'
                # own commits are qualified below. Only a merge that git cannot
                # replay (conflict resolution, edited tree) carries content of
                # its own, and needs provenance. An octopus merge (more than
                # two parents) is not a hosting-button merge: it keeps needing
                # provenance unless its tree equals a parent.
                trees = resolutions._run(spec.canon, "rev-parse", commit + "^{tree}",
                                         *(parent + "^{tree}" for parent in parents))
                if trees.returncode:
                    raise TrainError("cannot read canon merge trees")
                tree, *parent_trees = resolutions._text(trees).split()
                if tree in parent_trees:
                    continue
                if (len(parents) == 2
                        and _clean_merge_tree(spec.canon, parents[0], parents[1]) == tree):
                    continue
            receipt = review_provenance.receipt_for_commit(db, commit)
            family = review_lot.family_of((receipt or {}).get("harness"), families)
            if receipt and family:
                authors.append({"name": receipt["actor"], "family": family})
                continue
            if not receipt:
                # Canon commits made outside the hook use the same historical
                # author fallback as the train: registered source-lot author,
                # agent Git identity AND family proved by the turn journal.
                # Neither the Git name alone nor today's harness is evidence.
                for item in source_items:
                    if not item:
                        continue
                    rows, unknown = review_authors._unreceived_authors(
                        db, item, spec.canon, commit,
                        author_name=item.get("assignee") or "", author_family="",
                        reviewers=item.get("reviewers") or [], families=families,
                        carry_receipts=False)
                    if rows and not unknown and all(row.get("family") for row in rows):
                        authors.extend(rows)
                        break
                else:
                    raise TrainError("canon author provenance missing for %s" % commit)
            else:
                raise TrainError("canon author provenance missing for %s" % commit)
        if not authors:
            raise TrainError("canon author provenance missing")
        need = max(rule.reviews_for(changed).reviews for rule in rules)
        verdict = merge_queue.review_gate(
            db, current, gate, repo=spec.canon, klass={"class": "sensitive"},
            families=families, required=need, authors=authors, authors_complete=True,
            exclude_author_family=any(rule.exclude_author_family for rule in rules),
            distinct_families=any(rule.distinct_families for rule in rules))
        if not verdict["ok"]:
            raise TrainError("canon diff reviews refused: %s" % verdict["why"])
    return [{"reference": "milestone:%s" % mark["id"], "actor": mark["actor"],
             "commit": gate, "record": selected[mark["id"]]["record"]}
            for mark in current]
