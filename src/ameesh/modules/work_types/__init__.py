# SPDX-License-Identifier: AGPL-3.0-only
"""L103 (#5001): the non-code work item types, without behaviour of their own.

The SQL guard admits ``bug``, ``evolution`` and ``improvement`` everywhere and
any other type only through a module active in the lot's project
(``docs/design/process-workflow-registry.md``). This built-in module declares
the other installable types of the process compiler, so a standard install
can host a Pipeline card covering them (``examples/pipelines/
support-work-delivery.md``). It stores no schema and no rule, is never
``ALWAYS_ACTIVE``, and a project gets it only by selecting it in the canon
(``extensions.ameesh.projects.<project>.modules: [work-types]``).
"""
from __future__ import annotations

NAME = "work-types"
TYPES = ("docs", "investigation", "governance-tracking", "promotion",
         "human_operation")


def descriptor(options=None):
    if options:
        raise ValueError("unknown work-types module option(s): %s"
                         % ", ".join(sorted(options)))
    return {"types": list(TYPES), "schemas": {}, "rules": []}
