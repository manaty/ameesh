# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative, repository-policy driven staging migration preflight (#7364).

This module only reads Git objects and proofs. It never executes SQL.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from . import migrations

POLICY_PATH = ".ameesh/staging-migrations.json"
NOTE_PREFIX = "staging-migration:"


class Refused(ValueError):
    pass


@dataclass(frozen=True)
class Classification:
    kind: str
    reasons: tuple[str, ...]


def load_policy(text):
    try:
        policy = json.loads(text)
        if not isinstance(policy, dict) or policy.get("schema") != "ameesh-staging-migrations/1":
            raise ValueError("unsupported schema")
        rules = policy["rules"]
        if not isinstance(rules, dict) or set(rules) != {"additive", "relaxation", "destructive"}:
            raise ValueError("three rule classes required")
        for kind, entries in rules.items():
            if not isinstance(entries, list) or not entries:
                raise ValueError("empty rules: " + kind)
            for entry in entries:
                if set(entry) != {"pattern", "reason"} or not entry["reason"]:
                    raise ValueError("rule needs pattern and reason")
                re.compile(entry["pattern"], re.I | re.S)
        if not isinstance(policy.get("declarations", {}), dict):
            raise ValueError("declarations must be an object")
        return policy
    except (ValueError, TypeError, KeyError, re.error) as exc:
        raise Refused("staging migration policy unreadable: %s" % exc) from exc


def classify(sql, policy):
    """Only whole statements admitted by policy are additive.

    Strings and quoted identifiers are lexed before matching; executable dollar
    bodies and dynamic SQL cannot silently become additive. Non-additive rules
    have priority over the allowlist. Unknown syntax always refuses.
    """
    strings, code, sure, _ = migrations._lex(sql)
    if not sure:
        return Classification("unknown", ("SQL lexical analysis incomplete",))
    code = re.sub(r"\s+", " ", code.lower()).strip()
    if not code.strip(" ;"):
        return Classification("unknown", ("no SQL statement",))
    matched = {}
    for kind in ("destructive", "relaxation"):
        matched[kind] = tuple(entry["reason"] for entry in policy["rules"][kind]
                              if re.search(entry["pattern"], code, re.I | re.S))
    # A dollar body may contain DDL/DML or EXECUTE; do not guess its effect.
    # A replacement's body is part of the declared relaxation, rather than
    # SQL executed by applying the CREATE itself. Anonymous DO still refuses.
    if re.search(r"\b(?:do|execute|call)\b", code):
        return Classification("unknown", ("procedural or dynamic SQL not analysed",))
    if matched["destructive"] or matched["relaxation"]:
        # Every statement still has to have a known shape. A replacement
        # beside an unknown command cannot grant that command permission.
        allowed = sum((policy["rules"][key] for key in
                       ("additive", "relaxation", "destructive")), [])
        kind = "destructive" if matched["destructive"] else "relaxation"
        reasons = matched[kind]
    else:
        if any(value.startswith("$") for value in strings):
            return Classification("unknown", ("dollar body not analysed",))
        allowed, kind, reasons = policy["rules"]["additive"], "additive", ()
    for statement in code.split(";"):
        if statement.strip() and not any(re.fullmatch(entry["pattern"], statement.strip(),
                                                     re.I | re.S) for entry in allowed):
            return Classification("unknown", ("statement outside policy allowlist: "
                                               + statement.strip()[:120],))
    return Classification(kind, reasons)


def declaration_text(path, checksum):
    return "%s%s:%s" % (NOTE_PREFIX, path, checksum)


def check(path, data, policy, *, read_file, read_notes, signed_decision):
    """Validate one frozen file, returning an inspectable classification.

    Callbacks read the frozen tree, the carrier lot's notes, and a verified
    signed decision respectively. No note or filename alone grants authority.
    """
    try:
        result = classify(data.decode("utf-8"), policy)
    except UnicodeError as exc:
        raise Refused("%s: SQL is not UTF-8" % path) from exc
    if result.kind == "additive":
        return result
    reason = "%s: %s (%s)" % (path, result.kind, "; ".join(result.reasons))
    if result.kind == "unknown":
        raise Refused(reason)
    checksum = hashlib.sha256(data).hexdigest()
    declaration = policy.get("declarations", {}).get(path, {})
    if not isinstance(declaration, dict) or declaration.get("checksum") != checksum:
        raise Refused(reason + "; missing declaration for this checksum")
    text = declaration_text(path, checksum)
    if result.kind == "destructive":
        identifier = declaration.get("decision")
        if (type(identifier) is not int or identifier <= 0
                or not signed_decision(identifier, "allow-" + text)):
            raise Refused(reason + "; signed owner decision required for this checksum")
        return result
    lot, restore = declaration.get("lot"), declaration.get("restore")
    if type(lot) is not int or lot <= 0 or text not in read_notes(lot):
        raise Refused(reason + "; carrier lot declaration required for this checksum")
    if (not isinstance(restore, str) or not restore.endswith("_restore.sql")
            or PurePosixPath(restore).is_absolute() or ".." in PurePosixPath(restore).parts
            or not read_file(restore).strip()):
        raise Refused(reason + "; nonempty frozen *_restore.sql required")
    return result
