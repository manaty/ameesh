# SPDX-License-Identifier: AGPL-3.0-only
"""Lifecycle extensions: the five documentary and governance work item types.

L317a (#318) installs project-scoped modules by name; the trusted canon selects
them in `extensions.ameesh.projects.<project>.modules`. This descriptor
supplies the five non-code types already admitted by the Process verifier
(`INSTALLED_TYPES`) but not declared by an installed module: `docs`,
`investigation`, `governance-tracking`, `promotion` and `human_operation`.

It adds no structured milestone, no evidence rule and no module-specific
command: completion stays the per-type Process policy, and the SQL guard
`ameesh_module_requirements` admits these types only for a project whose canon
selects this module. Declaring a type here makes it installable, never active
for a project by itself. Consumer policy for these types is not published yet
(#1379), so no milestone schema or lifecycle rule is invented.
"""
from __future__ import annotations

NAME = "lifecycle"

#: The five documentary/governance types of the Process matrix (#1915 Q1).
TYPES = (
    "docs",
    "investigation",
    "governance-tracking",
    "promotion",
    "human_operation",
)


def descriptor(options=None):
    """Declare the five types; this module accepts no option."""
    options = dict(options or {})
    if options:
        raise ValueError("unknown lifecycle module option(s): %s"
                         % ", ".join(sorted(options)))
    return {"types": list(TYPES), "schemas": {}, "rules": []}


def main(db, argv, *, actor):
    """No module-specific command: the descriptor is selected by the canon."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="ameesh module lifecycle",
        description="Declares docs, investigation, governance-tracking, "
                    "promotion and human_operation. It adds no command and no "
                    "milestone rule; enable it per project in the trusted canon.")
    parser.parse_args(argv)
    return 0
