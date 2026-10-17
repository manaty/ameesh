# SPDX-License-Identifier: AGPL-3.0-only
"""Evidence for attributing a raw tip-to-tip conflict (#2104).

Removing a lot's delta means replacing its tip by its merge base with the
target. If exactly the same conflict survives, it cannot be assigned to
that delta. Compare index stages and rename groups, never just filenames.
Ambiguous evidence remains visible and must not select a resolver.
"""
from __future__ import annotations


def read_merge(proc) -> dict | None:
    """Read `merge-tree --write-tree -z --messages` (without --name-only)."""
    if proc.returncode not in (0, 1):
        return None
    parts = proc.stdout.decode("utf-8", "replace").split("\0")
    if not parts or not parts[0]:
        return None
    stages: dict = {}
    groups = []
    i = 1
    try:
        while parts[i]:
            info, path = parts[i].split("\t", 1)
            mode, blob, stage = info.split()
            stages.setdefault(path, []).append((stage, mode, blob))
            i += 1
        i += 1
        while i < len(parts) and parts[i]:
            count = int(parts[i])
            if count <= 0 or i + count + 2 >= len(parts):
                return None
            paths = set(parts[i + 1:i + 1 + count])
            kind = parts[i + 1 + count]
            if kind.startswith("CONFLICT"):
                groups.append(paths)
            i += count + 3  # count, paths, type, human message
    except (ValueError, IndexError):
        return None
    # One rename conflict may involve old and new paths, and several messages.
    # Keep its paths together so an unchanged half cannot hide the other half.
    for path in stages:
        groups.append({path})
    merged = []
    for group in groups:
        rest = []
        for previous in merged:
            if group & previous:
                group |= previous
            else:
                rest.append(previous)
        merged = rest + [group]
    if proc.returncode == 1 and not merged:
        return None
    return {"groups": [sorted(g) for g in merged],
            "stages": {p: sorted(s) for p, s in stages.items()}}


def classify(one: dict, other: dict, probe) -> dict:
    """`probe(a,b)` uses target attributes, fixed orientation, scratch objects.

    A native interaction is proven only if neither counterfactual conflicts
    on any path of its conflict group. Failure or changed conflict contents
    is uncertainty, not evidence that a pair is clean.
    """
    actual = probe(one["tip"], other["tip"])
    without_other = probe(one["tip"], other["base"])
    without_one = probe(one["base"], other["tip"])
    native, inherited, uncertain = set(), set(), set()
    if actual is None:
        return {"native": [], "inherited": [], "uncertain": None}
    for paths in actual["groups"]:
        group = set(paths)
        witnesses = (without_other, without_one)
        exact = any(
            witness is not None
            and paths in witness["groups"]
            and all(actual["stages"].get(p) == witness["stages"].get(p)
                    for p in paths)
            # A message without index stages is insufficient proof of identity.
            and any(p in actual["stages"] for p in paths)
            for witness in witnesses)
        if exact:
            inherited.update(group)
        elif all(witness is not None
                 and not any(group.intersection(g) for g in witness["groups"])
                 for witness in witnesses):
            native.update(group)
        else:
            uncertain.update(group)
    return {"native": sorted(native), "inherited": sorted(inherited),
            "uncertain": sorted(uncertain)}
