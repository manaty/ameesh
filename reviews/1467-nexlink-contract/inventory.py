#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""List the draft CI governing inputs from existing Git objects, read-only.

This expands a manually declared conservative scope. It neither discovers
arbitrary runtime dependencies nor qualifies the completeness of a suite.
No repository code, package manager, workflow or remote operation is run.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
import subprocess


def inventory(repo, candidate, scope):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    tree = candidate["union_tree"]
    # The existing candidate object is read directly: no merge-tree or new ref.
    entries = {}
    for record in git("ls-tree", "-r", "-z", tree).split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, blob = header.decode("ascii").split()
        entries[raw_path.decode("utf-8")] = (mode, kind, blob)

    def check(path):
        mode, kind, _ = entries[path]
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("governing source is not a regular file: " + path)

    # Preserve the twenty-workflow card's already recorded source checks.
    for path, blob in candidate["sources"].items():
        if path not in entries or entries[path][2] != blob:
            raise ValueError("candidate source differs: " + path)
        check(path)

    groups = {"workflow_and_selection_card": sorted(candidate["sources"])}
    for name, patterns in scope["groups"].items():
        paths = set()
        for pattern in patterns:
            found = {path for path in entries if fnmatch.fnmatchcase(path, pattern)}
            if not found:
                raise ValueError("declared scope matches no source: " + pattern)
            paths.update(found)
        for path in paths:
            check(path)
        groups[name] = sorted(paths)

    return {
        "schema": "ameesh-train-ci-source-inventory-draft/1",
        "status": "unqualified",
        "candidate": {"base": candidate["base"], "lot_1470": candidate["lot_1470"]},
        "groups": groups,
        "unique_paths": len({path for paths in groups.values() for path in paths}),
        "qualified_boundaries": scope["qualified_boundaries"],
        "scope_questions": scope["scope_questions"],
        "external_inputs": scope["external_inputs"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    try:
        result = inventory(args.repo,
                           json.loads((here / "candidate.json").read_text()),
                           json.loads((here / "scope.json").read_text()))
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
