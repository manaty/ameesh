# SPDX-License-Identifier: AGPL-3.0-only
"""Qualified composer handoffs, separate from historical lot admissions.

The explicitly injected reader resolves existing manifest/journal evidence;
this module never turns a ref, a pair resolution or a note into approval.
It checks the exact Git context and delta covered by that reader's receipt.
"""
from __future__ import annotations

import copy
import hashlib
import re

from . import plan_git, resolutions
from .train import TrainError, digest, identity
from .train_review import SHA

SCHEMA = "ameesh-train-transport/1"
FIELDS = {"schema", "source", "target", "admission", "base", "head", "tree",
          "delta", "ref", "qualification"}


def binding(receipt, *, source, target, row, base):
    if not isinstance(receipt, dict) or set(receipt) != FIELDS \
            or receipt["schema"] != SCHEMA:
        raise TrainError("structured qualified transport receipt required")
    if (receipt["source"], receipt["target"], receipt["base"]) != (source, target, base) \
            or not isinstance(receipt["admission"], (list, tuple)) \
            or tuple(receipt["admission"]) != identity(row):
        raise TrainError("transport names another source, target, admission or intermediate base")
    if any(not isinstance(receipt[k], str) or not SHA.fullmatch(receipt[k])
           for k in ("base", "head", "tree")) \
            or not isinstance(receipt["delta"], str) \
            or re.fullmatch(r"[0-9a-f]{64}", receipt["delta"]) is None:
        raise TrainError("exact transport objects and delta required")
    ref = receipt["ref"]
    prefix = "refs/ameesh/resolutions/%d/" % row["work_item_id"]
    if not isinstance(ref, str) or not ref.startswith(prefix) or len(ref) <= len(prefix):
        raise TrainError("transport ref must belong to this lot")
    try:
        if plan_git.check_ref(ref, "transport ref") != ref:
            raise ValueError("transport ref changed by normalization")
    except ValueError as exc:
        raise TrainError("invalid transport ref") from exc
    qualified = receipt["qualification"]
    if not isinstance(qualified, dict) or set(qualified) != {"kind", "references"} \
            or qualified["kind"] not in ("mechanical", "adapted") \
            or not isinstance(qualified["references"], list) or not qualified["references"]:
        raise TrainError("existing mechanical or adapted qualification references required")
    for ref in qualified["references"]:
        if not isinstance(ref, dict):
            raise TrainError("structured existing qualification reference required")
        journal = (set(ref) == {"kind", "id"} and ref["kind"] in
                   ("work-event", "milestone", "mail") and type(ref["id"]) is int
                   and ref["id"] > 0)
        manifest = (set(ref) == {"kind", "commit", "path"} and ref["kind"] == "manifest"
                    and isinstance(ref["commit"], str) and SHA.fullmatch(ref["commit"])
                    and isinstance(ref["path"], str) and bool(ref["path"].strip()))
        if not journal and not manifest:
            raise TrainError("exact existing journal or manifest reference required")


def lookup(reader, *, source, target, row, base, path, expected=None):
    if expected is not None:
        binding(expected, source=source, target=target, row=row, base=base)
    if reader is None:
        if expected is not None:
            raise TrainError("qualified transport reader unavailable")
        return None
    try:
        receipt = reader(source=source, target=target, row=copy.deepcopy(row),
                         base=base, path=path, expected=copy.deepcopy(expected))
    except Exception as exc:
        raise TrainError("cannot read existing transport qualification: %s" % exc) from exc
    if receipt is not None:
        binding(receipt, source=source, target=target, row=row, base=base)
    if expected is not None and digest(receipt) != digest(expected):
        raise TrainError("qualified transport withdrawn or changed")
    return copy.deepcopy(receipt)


def for_plan(plan):
    receipts = plan.get("transports", [])
    if not isinstance(receipts, list):
        raise TrainError("structured transport list required")
    members = {row["work_item_id"]: row for row in plan["members"]}
    found = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("admission"), (list, tuple)) \
                or len(receipt["admission"]) != 6 or type(receipt["admission"][0]) is not int:
            raise TrainError("transport has no source admission identity")
        lot = receipt["admission"][0]
        if lot not in members or lot in found:
            raise TrainError("transport names an absent or duplicate lot #%s" % lot)
        binding(receipt, source=plan["source"], target=plan["target"], row=members[lot],
                base=receipt.get("base"))
        found[lot] = receipt
    return found


def delta(path, base, head):
    # Complete blob IDs bind binary contents too. Raw NUL-delimited records
    # avoid local hunk algorithms, quoting, prefixes and colour; sorting paths
    # also removes diff.orderFile from the qualification's identity.
    proc = resolutions._run(path, "--no-replace-objects", "diff", "--raw", "-z",
                            "--no-abbrev", "--no-color", "--no-relative", "--no-renames",
                            "--ignore-submodules=none", "--no-ext-diff", "--no-textconv",
                            base, head, "--")
    if proc.returncode:
        raise TrainError("cannot read exact transport delta")
    fields = proc.stdout.split(b"\0")
    if fields[-1] != b"" or (len(fields) - 1) % 2:
        raise TrainError("unreadable exact transport delta")
    records = sorted(zip(fields[:-1:2], fields[1:-1:2]), key=lambda record: record[1])
    canonical = b"".join(header + b"\0" + path + b"\0" for header, path in records)
    return hashlib.sha256(canonical).hexdigest()


def verify(path, receipt, row):
    """No qualification is inferred from ancestry or a clean merge alone."""
    head, base = receipt["head"], receipt["base"]
    tree = plan_git._out(path, "--no-replace-objects", "rev-parse", "--verify", head + "^{tree}")
    parents = (plan_git._out(path, "--no-replace-objects", "rev-list", "--parents", "-n", "1", head)
               or "").split()
    if tree != receipt["tree"] or len(parents) < 3 or parents[1] != base:
        raise TrainError("transport tree or first parent differs from its exact context")
    for ancestor in (base, row["frozen_sha"]):
        if resolutions._run(path, "--no-replace-objects", "merge-base", "--is-ancestor", ancestor, head).returncode:
            raise TrainError("transport does not preserve its base and source frozen head")
    if delta(path, base, head) != receipt["delta"]:
        raise TrainError("transport delta differs from the existing qualification")
