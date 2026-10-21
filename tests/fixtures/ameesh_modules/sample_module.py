# SPDX-License-Identifier: AGPL-3.0-only
"""Test-only external module descriptor for the generic module mechanism.

This file is not installed with ameesh and is not a production descriptor. It
lives under ``tests/fixtures/ameesh_modules/`` with a neighbouring ``.dist-info``
so the test suite can exercise the real ``importlib.metadata`` entry-point
discovery path (group ``ameesh.modules``). The five types are the witness set
of #1915/Q1; a real private descriptor (for example ThePackengers') is installed
the same way, with its own name and types.
"""
NAME = "sample"

TYPES = (
    "docs",
    "investigation",
    "governance-tracking",
    "promotion",
    "human_operation",
)


def descriptor(options=None):
    options = dict(options or {})
    if options:
        raise ValueError("unknown sample module option(s): %s"
                         % ", ".join(sorted(options)))
    return {"types": list(TYPES), "schemas": {}, "rules": []}
