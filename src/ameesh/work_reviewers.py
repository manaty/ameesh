# SPDX-License-Identifier: AGPL-3.0-only
"""Designated reviewers, including rows read before migration 0456."""
from __future__ import annotations


def designated(item: dict) -> list[str]:
    from .storage.postgres.work import REVIEWER_NONE

    names = item.get("reviewers")
    if names is None:
        names = [item.get("reviewer")]
    return list(dict.fromkeys(n for n in names
                             if n and n != REVIEWER_NONE and n != item.get("assignee")))
