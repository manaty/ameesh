# SPDX-License-Identifier: AGPL-3.0-only
"""#4037: exact recorded opinions for a host-local reviewed canon move.

References are milestone:<id>, never assertions parsed from prose. Reviewers
can record a canon opinion from another host with work milestone --repo.
The old policy remains a floor: the proposed canon cannot waive its own review.
"""
from __future__ import annotations

import re

from . import (assignments, mechanics, merge_queue, resolutions, review_authors, review_lot,
               review_provenance, storage, train_assembly, work)
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
        if (receipt and receipt["commit"] == gate and receipt["actor"] == mark["actor"]
                and (receipt.get("milestone") or {}).get("verdict") == mark["verdict"]):
            latest[mark["actor"]] = mark
    current = [mark for mark in latest.values() if mark["id"] in selected]
    if not any(_role(cfg, db, mark["actor"]) for mark in current):
        raise TrainError("reviewed requalification requires a current pilot opinion")
    guard = set(changed).intersection(train_assembly.REQUALIFY_GUARD)
    if guard:
        authors = []
        commits = resolutions._run(spec.canon, "rev-list", "--parents", pin + ".." + gate)
        if commits.returncode:
            raise TrainError("cannot read canon authors")
        lines = [line.split() for line in resolutions._text(commits).splitlines()]
        # #4505: a canon gate can cover several PRs. The cited opinions name
        # the lot they were recorded on, which need not be the lot that froze
        # each commit of the range. Read the lots that the exact freeze/verdict
        # receipts of these commits name, so the author fallback below finds
        # the lot where the committer is the reference. These are candidate
        # lots only: each commit still needs its registered Git identity and
        # its family proved by the turn journal.
        range_commits = [fields[0] for fields in lines if fields]
        lots.update(int(lot) for lot in
                    storage.of(db).provenance.origins(range_commits))
        source_items = [work.get(db, lot) for lot in sorted(lots)]
        for fields in lines:
            commit, *parents = fields
            if len(parents) > 1:
                # A hosting merge whose tree is exactly a parent adds no
                # content. A conflict resolution does, and needs provenance.
                trees = resolutions._run(spec.canon, "rev-parse", commit + "^{tree}",
                                         *(parent + "^{tree}" for parent in parents))
                if trees.returncode:
                    raise TrainError("cannot read canon merge trees")
                tree, *parent_trees = resolutions._text(trees).split()
                if tree in parent_trees:
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
