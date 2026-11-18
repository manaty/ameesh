# SPDX-License-Identifier: AGPL-3.0-only
"""Exact correlation of production workflow runs and pre-act receipts.

The caller must supply the complete set of runs in stable order, including
previously processed runs, so consumption cannot be reset between passes.
"""
from __future__ import annotations

import re

_MARKER = re.compile(r"(?:^|\s)pregeste=([^\s]*)")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def correlate(runs, receipts):
    """Return anomalies; receipt IDs are single-use, never matched by time.

    Each run has a unique stable ``id`` and ``display_title``. Receipts are
    evidence objects, of which only well-formed production receipts count.
    Duplicate receipt IDs are ambiguous and therefore cannot authorize a run.
    """
    counts = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("type") != "pre-act/1" or receipt.get("kind") != "prod":
            continue
        value = receipt.get("pregeste_id")
        if isinstance(value, str) and _UUID4.fullmatch(value):
            counts[value] = counts.get(value, 0) + 1
    consumed = set()
    seen_runs = set()
    anomalies = []
    for run in runs:
        run_id = run["id"]
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        title = run.get("display_title")
        values = _MARKER.findall(title) if isinstance(title, str) else []
        if len(values) != 1 or not _UUID4.fullmatch(values[0]):
            reason = "missing-or-invalid-id"
        elif counts.get(values[0], 0) != 1:
            reason = "unknown-or-ambiguous-id"
        elif values[0] in consumed:
            reason = "already-consumed-id"
        else:
            consumed.add(values[0])
            continue
        anomalies.append({"run_id": run_id, "reason": reason})
    return anomalies
