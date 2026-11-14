# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative SQL gate on a frozen applicator inventory (#7364).

This module only reads Git objects and proofs. It never executes SQL.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
import copy
import subprocess
import argparse
import sys

from . import migrations

POLICY_PATH = ".ameesh/staging-migrations.json"

_DEFAULT_RULES = {
    'additive': [
        {"pattern": r'(?:begin|commit)', "reason": 'transaction'},
        {"pattern": r'set local (?:search_path|lock_timeout)\s*=.*', "reason": 'settings'},
        {"pattern": r'create table (?:if not exists )?[\w.\"]+\s*\([^;]*\)', "reason": 'new table'},
        {"pattern": r'create (?:unique )?index (?:concurrently )?(?:if not exists )?[\w.\"]+ on [^;]+', "reason": 'new index'},
        {"pattern": r'alter table [\w.\"]+ add column (?:if not exists )?[\w\"]+ [\w.]+(?:\[\])?(?: null)?', "reason": 'new column'},
        {"pattern": r'comment on [^;]+ is\s*', "reason": 'documentation'},
        {"pattern": r'create (?:function|procedure)\s+[^;]+', "reason": 'new routine'},
        {"pattern": r'grant\s+[^;]+', "reason": 'privilege added'},
    ],
    'relaxation': [
        {"pattern": r'create (?:constraint )?trigger\s+[^;]+', "reason": 'new trigger may reject previous-version writes'},
        {"pattern": r'alter table [\w.\"]+ drop constraint (?:if exists )?[\w\"]+', "reason": 'constraint removed'},
        {"pattern": r'create or replace (?:function|procedure)\s+[^;]+', "reason": 'routine replaced'},
        {"pattern": r'create or replace (?:trigger|rule|view)\s+[^;]+', "reason": 'guard or view replaced'},
    ],
    'destructive': [
        {"pattern": r'drop (?:table|schema|type|view|function|procedure|trigger|policy)\s+[^;]+', "reason": 'object removed'},
        {"pattern": r'alter table [^;]*\bdrop column\b[^;]*', "reason": 'column removed'},
        {"pattern": r'revoke\s+[^;]+', "reason": 'privilege removed'},
        {"pattern": r'alter type\s+[^;]+', "reason": 'type changed'},
        {"pattern": r'(?:delete from|update|truncate)\s+[^;]+', "reason": 'data changed'},
        {"pattern": r'alter table [^;]*\bset not null\b[^;]*', "reason": 'column mandatory'},
        {"pattern": r'alter table [^;]*\badd (?:constraint|primary key|foreign key|unique|check)\b[^;]*', "reason": 'constraint added'},
        {"pattern": r'alter table [^;]*\balter column\b[^;]*\btype\b[^;]*', "reason": 'column type changed'},
        {"pattern": r'alter table [^;]*\badd column\b[^;]*\bnot null\b[^;]*', "reason": 'mandatory column'},
    ],
}


class Refused(ValueError):
    pass


@dataclass(frozen=True)
class Classification:
    kind: str
    reasons: tuple[str, ...]
    creates_trigger: bool = False


def load_policy(text=None):
    """Absent project policy means SQL defaults, with no declarations/exceptions."""
    try:
        policy = ({"schema": "ameesh-staging-migrations/1"} if text is None
                  else json.loads(text))
        if not isinstance(policy, dict) or policy.get("schema") != "ameesh-staging-migrations/1":
            raise ValueError("unsupported schema")
        rules = copy.deepcopy(_DEFAULT_RULES)
        exceptions = policy.get("exceptions", {})
        if not isinstance(exceptions, dict) or set(exceptions) - set(rules):
            raise ValueError("invalid exception classes")
        for kind, entries in exceptions.items():
            if not isinstance(entries, list):
                raise ValueError("exception rules must be a list")
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) != {"pattern", "reason"}
                        or not isinstance(entry["reason"], str) or not entry["reason"].strip()
                        or not isinstance(entry["pattern"], str) or not entry["pattern"]):
                    raise ValueError("rule needs pattern and reason")
                re.compile(entry["pattern"], re.I | re.S)
            rules[kind].extend(entries)
        if "rules" in policy:
            raise ValueError("SQL defaults live in ameesh; use project exceptions")
        if not isinstance(policy.get("declarations", {}), dict):
            raise ValueError("declarations must be an object")
        return dict(policy, declarations=policy.get("declarations", {}), rules=rules)
    except (ValueError, TypeError, KeyError, re.error) as exc:
        raise Refused("staging migration policy unreadable: %s" % exc) from exc


def _code(sql, *, procedural=False, depth=0):
    if depth > 20:
        raise Refused("procedural nesting exceeds lexical analysis")
    bodies = []

    def body_token(literal):
        tag = migrations._DOLLAR_TAG.match(literal).group()
        bodies.append(literal[len(tag):-len(tag)])
        return " __staging_body_%d__ " % (len(bodies) - 1)

    _, code, sure, _ = migrations._lex(sql, dollar_handler=body_token, allow_inline_comments=True,
                                        string_handler=lambda literal: " '_' ")
    if not sure:
        raise Refused("SQL lexical analysis incomplete")
    code = re.sub(r"\s+", " ", code.lower()).strip()
    code = ";".join(part if re.match(r"\s*alter table\b.*\badd column\b", part)
                    else part.replace("'_'", " ")
                    for part in code.split(";"))
    for i, body in enumerate(bodies):
        token = "__staging_body_%d__" % i
        pattern = r"\bdo\s+(?:language\s+plpgsql\s+)?" + token + r"(?:\s+language\s+plpgsql)?"
        if re.search(pattern, code):
            expanded = _code(body, procedural=True, depth=depth + 1)
            code = re.sub(pattern, lambda m: expanded, code, count=1)
        else:
            # Function definitions store their bodies; they do not execute them.
            # Other dollar literals are not admitted as executable statements.
            statements = [part for part in code.split(";") if token in part]
            if not all(re.search(r"\bcreate\s+(?:or\s+replace\s+)?(?:function|procedure)\b", part)
                       for part in statements):
                raise Refused("dollar body outside a routine definition or DO")
            code = code.replace(token, " ")
    if not procedural:
        return code
    # Only understood control structures are stripped. All branch SQL remains
    # in the output, so a guard never hides a destructive operation.
    stack, result = [], []
    for statement in code.split(";"):
        statement = statement.strip()
        while statement:
            match = re.match(r"begin\b\s*", statement)
            if match:
                stack.append("begin")
            elif re.match(r"if\b", statement):
                match = re.match(r"if\s+(?:not\s+)?to_reg(?:class|namespace)\s*\(\s*\)"
                                 r"\s+is\s+(?:not\s+)?null\s+then\b\s*", statement)
                if not match:
                    raise Refused("procedural condition not analysed")
                stack.append("if")
            elif re.match(r"end\b", statement):
                match = re.match(r"end(?:\s+(if))?\b\s*", statement)
                expected = "if" if match.group(1) else "begin"
                if not stack or stack.pop() != expected:
                    raise Refused("unbalanced procedural block")
            elif re.match(r"else\b", statement):
                if not stack or stack[-1] != "if":
                    raise Refused("ELSE outside understood IF")
                match = re.match(r"else\b\s*", statement)
            else:
                break
            statement = statement[match.end():].strip()
        if statement and statement not in ("return", "null") and not re.fullmatch(r"raise notice\s*", statement):
            result.append(statement)
    if stack:
        raise Refused("unclosed procedural block")
    return "; ".join(result)


def _new_routine_revoke(statement, code):
    """Revoking implicit PUBLIC grants on routines created here is additive."""
    match = re.fullmatch(r"revoke\s+all\s+on\s+function\s+(.+?)\s+from\s+[\w, ]+", statement)
    if not match:
        return False
    targets, end = [], 0
    for target in re.finditer(r"([\w.]+)\s*(\([^()]*\))", match[1]):
        if match[1][end:target.start()].strip(" ,"):
            return False
        targets.append(target.groups())
        end = target.end()
    return bool(targets) and not match[1][end:].strip() and all(
        migrations._proven_new(code, "function", name, signature)
        for name, signature in targets)


def classify(sql, policy=None):
    policy = load_policy() if policy is None else policy
    try:
        code = _code(sql)
    except Refused as exc:
        return Classification("unknown", (str(exc),))
    creates_trigger = bool(re.search(
        r"(?:^|;)\s*create\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\b", code))
    if not code.strip(" ;"):
        return Classification("unknown", ("no SQL statement",))
    rank = {"additive": 0, "relaxation": 1, "destructive": 2, "unknown": 3}
    unconditional = not re.search(r"\bdo\b", migrations._lex(sql)[1], re.I)
    kind, reasons = "additive", []
    for statement in code.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        if re.match(r"(?:execute|call|do)\b", statement):
            return Classification("unknown", ("dynamic SQL not analysed",), creates_trigger)
        if unconditional and _new_routine_revoke(statement, code):
            continue
        # Match the entire addition, keeping literal contents opaque. Extra
        # actions, constraints, NULL and computed defaults cannot pass here.
        constant = r"(?:'_'|true|false|[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?|array\s*\[\s*\])"
        # A domain's implicit cast may reject this constant on an N-1 insert.
        # Restrict both the column type and explicit casts to known built-ins.
        builtin = r"(?:pg_catalog\.)?(?:text|varchar|bool|boolean|int|integer|int2|int4|int8|bigint|smallint|numeric|decimal|real|float4|float8|json|jsonb|uuid)"
        cast = r"(?:\s*::\s*" + builtin + r"(?:\[\])?)*"
        column = r'alter table [\w.\"]+ add column (?:if not exists )?[\w\"]+ ' + builtin + r"(?:\[\])?"
        constant_column = re.fullmatch(
            column + r"(?: not null default " + constant + cast
            + r"| default " + constant + cast + r" not null)", statement)
        found = None
        for category in ("destructive", "relaxation", "additive"):
            hits = [entry["reason"] for entry in policy["rules"][category]
                    if re.fullmatch(entry["pattern"], statement, re.I | re.S)
                    and not (constant_column and entry in _DEFAULT_RULES["destructive"]
                             and entry["reason"] == "mandatory column")]
            if category == "additive" and constant_column:
                hits.append("constant default for new mandatory column")
            if hits:
                found = category
                if rank[category] > rank[kind]:
                    kind = category
                reasons.extend(hits if category != "additive" else [])
                break
        if found is None:
            return Classification("unknown", ("statement outside SQL rules: " + statement[:120],), creates_trigger)
    return Classification(kind, tuple(dict.fromkeys(reasons)), creates_trigger)


def check(path, data, policy, *, read_file, signed_decision):
    """The frozen policy entry itself is the reviewed migration declaration."""
    try:
        result = classify(data.decode("utf-8"), policy)
    except UnicodeError:
        result = Classification("unknown", ("SQL is not UTF-8",))
    if result.kind == "additive" and not result.creates_trigger:
        return result
    reason = "%s: %s (%s)" % (path, result.kind, "; ".join(result.reasons))
    checksum = hashlib.sha256(data).hexdigest()
    declaration = policy.get("declarations", {}).get(path, {})
    if not isinstance(declaration, dict) or declaration.get("checksum") != checksum:
        raise Refused(reason + "; missing declaration for this checksum")
    lot = declaration.get("lot")
    if type(lot) is not int or lot <= 0:
        raise Refused(reason + "; carrier lot required")
    rejects_previous_writes = False
    if result.creates_trigger:
        rejects_previous_writes = declaration.get("rejects_previous_writes")
        justification = declaration.get("previous_writes_justification")
        if (type(rejects_previous_writes) is not bool
                or not isinstance(justification, str) or not justification.strip()):
            raise Refused(reason + "; trigger requires boolean rejects_previous_writes and previous_writes_justification")
    if result.kind in ("destructive", "unknown") or rejects_previous_writes:
        identifier, option = declaration.get("decision"), declaration.get("approval_option")
        if (type(identifier) is not int or identifier <= 0 or not isinstance(option, str) or not option
                or not signed_decision(identifier, lot, option)):
            raise Refused(reason + "; signed owner agreement attached to carrier lot required")
        return result
    restore = declaration.get("restore")
    if (not isinstance(restore, str) or not restore.endswith("_restore.sql")
            or PurePosixPath(restore).is_absolute() or ".." in PurePosixPath(restore).parts
            or not read_file(restore).strip()):
        raise Refused(reason + "; nonempty frozen *_restore.sql required")
    return result


class GitTree:
    """Read regular files at one exact commit; no checkout or staging access."""
    def __init__(self, repo, sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise Refused("a full frozen commit SHA is required")
        self.repo, self.sha = repo, sha
        self.git("rev-parse", "--verify", sha + "^{commit}")

    def git(self, *args):
        try:
            return subprocess.run(["git", "-C", self.repo, *args], capture_output=True,
                                  check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused("frozen Git read failed: %s" % " ".join(args)) from exc

    def read(self, path, *, optional=False):
        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
                or any(part in ("..", ".", "") for part in path.split("/"))
                or "\n" in path or "\r" in path):
            raise Refused("invalid repository-relative file: %s" % path)
        entries = self.git("ls-tree", "-z", self.sha, "--", path).split(b"\0")
        entries = [entry for entry in entries if entry]
        if not entries and optional:
            return None
        if (len(entries) != 1 or b"\t" not in entries[0]
                or entries[0].split(b"\t", 1)[1] != path.encode()
                or entries[0].split(b" ", 1)[0] not in (b"100644", b"100755")):
            raise Refused("%s: regular frozen file required" % path)
        return self.git("show", self.sha + ":" + path)

    def policy(self):
        data = self.read(POLICY_PATH, optional=True)
        try:
            return load_policy(None if data is None else data.decode("utf-8"))
        except UnicodeError as exc:
            raise Refused("staging migration policy is not UTF-8") from exc


def decision_verifier(db, project):
    def verify(identifier, lot, option):
        from . import decisions
        view = decisions.get(db, identifier)
        answer = (view or {}).get("answer") or {}
        if not isinstance(answer, dict):
            return False
        if (not view or view.get("state") != "answered" or view.get("lot") != lot
                or view.get("project") != project or answer.get("option") != option
                or option not in {entry.get("key") for entry in (view.get("options") or [])
                                  if isinstance(entry, dict)}):
            return False
        try:
            decisions.require_current(db, view)
            return decisions.verify_proof(db, view)[0]
        except decisions.DecisionError:
            return False
    return verify


def inventory(tree, paths, *, signed_decision):
    policy, rows, refused = tree.policy(), [], False
    for path in dict.fromkeys(paths):
        try:
            data = tree.read(path)
            classification = check(path, data, policy, read_file=tree.read,
                                   signed_decision=signed_decision)
            rows.append(dict(path=path, checksum=hashlib.sha256(data).hexdigest(),
                             classification=classification.kind,
                             reasons=list(classification.reasons), allowed=True))
        except Refused as exc:
            refused = True
            rows.append(dict(path=path, allowed=False, reason=str(exc)))
    return {"schema": "ameesh-staging-migration-check/1", "sha": tree.sha,
            "allowed": not refused, "files": rows}


def check_inventory(repo, sha, project, paths, *, cfg=None):
    """Classify one explicit frozen inventory, opening the proof reader lazily.

    `cfg` is the ordinary ameesh configuration used for signed decisions; it is
    optional so a caller that already has one (the staging applicator, #7368)
    does not reload the environment. No staging credential is read.
    """
    connection = None

    def signed_decision(identifier, lot, option):
        nonlocal connection
        # The usual ameesh proof reader is opened only for destructive SQL.
        # No staging credential, SQL execution, or credential CLI option.
        from . import config, db
        if connection is None:
            connection = db.connect(
                cfg or config.load(register_credentials=False), writer=False)
        return decision_verifier(connection, project)(identifier, lot, option)

    try:
        return inventory(GitTree(repo, sha), paths, signed_decision=signed_decision)
    finally:
        if connection is not None:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only check of the applicator's explicit pending SQL inventory")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True, help="full frozen commit SHA")
    parser.add_argument("--project", required=True, help="ameesh project owning carrier lots")
    parser.add_argument("--file", action="append", default=[], help="pending repository-relative SQL file (repeatable)")
    parser.add_argument("--files-from", help="UTF-8 file with one pending path per line")
    args = parser.parse_args(argv)
    try:
        paths = list(args.file)
        if args.files_from:
            from pathlib import Path
            paths.extend(line for line in Path(args.files_from).read_text(encoding="utf-8").splitlines() if line)
        if not paths and args.files_from is None:
            raise Refused("explicit pending inventory required (--file or --files-from)")
        report = check_inventory(args.repo, args.sha, args.project, paths)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["allowed"] else 1
    except (Refused, OSError, UnicodeError, ValueError, migrations.DbError) as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
