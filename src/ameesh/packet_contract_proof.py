# SPDX-License-Identifier: AGPL-3.0-only
"""#6183: journal projection of D's result, never a reusable test attestation."""
from __future__ import annotations

import json
import re

MARK = "paquet-auto: contrats "
SCHEMA = "ameesh-packet-contract-result/1"


def policy(project):
    from . import config, mechanics
    try:
        document = mechanics.configured(config.load())
    except (OSError, ValueError, mechanics.MechanicsError):
        # Like the composer's budget reader, direct calls may lack a canon.
        # Keep D's contracts-first path; never enable legacy proof bypass.
        return "contracts-first"
    return from_document(document, project)


def from_document(document, project):
    override = document.override(project) if hasattr(document, "override") else None
    if override and override.contract_selection:
        return override.contract_selection.publish
    return "contracts-first"


def projection(report, *, base, target):
    def result(run):
        # Negatives have priority over partial coverage/timeouts in other modules.
        # #6238: a skipped test is inconclusive (canon §3), never green.
        state = "red" if run.real_failure and not run.inconclusive else (
            "inconclusive" if run.inconclusive or not run.finished or
            getattr(run, "skipped", 0) > 0 else "green")
        children = [result(child) for child in getattr(run, "children", ())]
        if run.flaky_tests:
            state = "inconclusive"
        elif any(child["state"] == "red" for child in children):
            state = "red"
        elif any(child["state"] != "green" for child in children):
            state = "inconclusive"
        return {"state": state, "modules": list(run.modules), "children": children}
    return {"schema": SCHEMA, "head": report.head, "tree": report.tree,
            "base": base, "target": target, "result": result(report.run)}


def encode(report, *, base, target):
    return MARK + json.dumps(projection(report, base=base, target=target), sort_keys=True)


def _events(item):
    return sorted(item.get("events") or (),
                  key=lambda ev: (ev.get("created_ts") or 0, ev.get("id") or 0))


def latest(item):
    """#6238: the carrier's last projection note, whatever its scope, or None.

    Only this note can be in force (`read` lets any later note supersede), so
    a writer skips a note identical to it: one note per head/tree/target.
    """
    notes = [str(event.get("note") or "") for event in _events(item)]
    return next((note for note in reversed(notes) if note.startswith(MARK)), None)


def read(item, *, head, tree, target):
    """Latest exact-scope projection; absent/invalid evidence remains unknown."""
    found = None
    events = _events(item)
    def valid_result(value):
        return (isinstance(value, dict) and value.get("state") in
                ("green", "red", "inconclusive") and
                isinstance(value.get("modules"), list) and
                all(isinstance(module, str) for module in value["modules"]) and
                isinstance(value.get("children"), list) and
                all(valid_result(child) for child in value["children"]) and
                (value["state"] != "green" or all(
                    child["state"] == "green" for child in value["children"])))
    for event in events:
        note = str(event.get("note") or "")
        if not note.startswith(MARK):
            continue
        # Every later projection supersedes the previous evidence, even when
        # its payload or scope cannot be verified.
        found = None
        try:
            value = json.loads(note[len(MARK):])
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("head") != head:
            continue
        if (value.get("schema") == SCHEMA and value.get("tree") == tree and
                value.get("target") == target and
                isinstance(value.get("base"), str) and
                re.fullmatch(r"[0-9a-f]{40}", value["base"]) and
                valid_result(value.get("result"))):
            found = value
    return found
