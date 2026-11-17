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
    #: A trigger creation that is not proven harmless for N-1 writes. A new
    #: trigger on a table created by the same migration, or one declared
    #: compatible with previous code, does not set it (#7655).
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


_LITERAL = r"__staging_lit_\d+__"
_LITERAL_RE = re.compile(_LITERAL)


def _code(sql, *, procedural=False, depth=0, literals=None):
    if depth > 20:
        raise Refused("procedural nesting exceeds lexical analysis")
    literals = {} if literals is None else literals
    bodies = []

    def body_token(literal):
        tag = migrations._DOLLAR_TAG.match(literal).group()
        bodies.append(literal[len(tag):-len(tag)])
        return " __staging_body_%d__ " % (len(bodies) - 1)

    def string_token(literal):
        # Un jeton unique par littéral : le classeur range parfois une colonne
        # d'après le défaut, et doit alors comparer de vraies valeurs sans
        # jamais réintroduire le contenu d'une chaîne dans le code analysé.
        token = "__staging_lit_%d__" % len(literals)
        literals[token] = literal
        return " " + token + " "

    _, code, sure, _ = migrations._lex(sql, dollar_handler=body_token, allow_inline_comments=True,
                                        string_handler=string_token)
    if not sure:
        raise Refused("SQL lexical analysis incomplete")
    code = re.sub(r"\s+", " ", code.lower()).strip()
    code = ";".join(part if re.match(r"\s*alter table\b.*\badd column\b", part)
                    else _LITERAL_RE.sub(" ", part)
                    for part in code.split(";"))
    for i, body in enumerate(bodies):
        token = "__staging_body_%d__" % i
        pattern = r"\bdo\s+(?:language\s+plpgsql\s+)?" + token + r"(?:\s+language\s+plpgsql)?"
        if re.search(pattern, code):
            expanded = _code(body, procedural=True, depth=depth + 1, literals=literals)
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


class _Unsupported(Exception):
    """The inline CHECK cannot be proven to accept the default (#7531)."""


# A constant default, as normalized by `_code`: a string literal is an opaque
# token, so a check is evaluated against the real value, never a placeholder.
_CONSTANT = (r"(?:" + _LITERAL + r"|true|false|"
             r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?|array\s*\[\s*\])")
# A domain's implicit cast may reject this constant on an N-1 insert. Restrict
# both the column type and explicit casts to known built-ins.
_BUILTIN = (r"(?:pg_catalog\.)?(?:text|varchar|bool|boolean|int|integer|int2|int4|"
            r"int8|bigint|smallint|numeric|decimal|real|float4|float8|json|jsonb|uuid)")
_CAST = r"(?:\s*::\s*" + _BUILTIN + r"(?:\[\])?)*"
_COLUMN = (r'alter table [\w.\"]+ add column (?:if not exists )?(?P<column>[\w\"]+) '
           + _BUILTIN + r"(?:\[\])?")


def _column_definition(statement):
    """``(default, check, column)`` of an ``ADD COLUMN`` with a constant
    default, else None. ``check`` is the inline CHECK expression, or None.

    Only a CHECK that ends the column definition is read; anything after it
    (another constraint, NOT VALID, …) keeps the statement outside this
    shortcut and therefore destructive.
    """
    head = (_COLUMN + r"(?: not null default (?P<default>" + _CONSTANT + ")" + _CAST
            + r"| default (?P<default2>" + _CONSTANT + ")" + _CAST + r" not null)")
    for tail in ("", r"\s+check\s*\((?P<check>.*)\)"):
        match = re.fullmatch(head + tail, statement, re.I | re.S)
        if match:
            return (match.group("default") or match.group("default2"),
                    match.group("check") if tail else None, match.group("column"))
    return None


def _literal_value(raw):
    if len(raw) < 2 or raw[0] != "'" or raw[-1] != "'":
        raise _Unsupported("literal")
    return raw[1:-1].replace("''", "'")


def _default_value(text, literals):
    """The Python value of a normalized constant default."""
    text = text.strip()
    if _LITERAL_RE.fullmatch(text):
        return _literal_value(literals[text])
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?", low):
        return float(low) if any(ch in low for ch in ".e") else int(low)
    if re.fullmatch(r"array\s*\[\s*\]", low):
        return []
    raise _Unsupported("default")


_COLUMN_REF = object()


def _truth(value):
    """A SQL predicate: True, False or None (NULL); anything else is refused."""
    if value is None or isinstance(value, bool):
        return value
    raise _Unsupported("predicate")


def _same_type(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        if not (isinstance(left, bool) and isinstance(right, bool)):
            raise _Unsupported("types")
    elif isinstance(left, (int, float)):
        if not isinstance(right, (int, float)) or isinstance(right, bool):
            raise _Unsupported("types")
    elif isinstance(left, str):
        if not isinstance(right, str):
            raise _Unsupported("types")
    elif isinstance(left, list):
        if not isinstance(right, list):
            raise _Unsupported("types")
    else:
        raise _Unsupported("types")


def _compare(operator, left, right):
    if left is None or right is None:
        return None
    _same_type(left, right)
    if operator == "=":
        return left == right
    if operator in ("<>", "!="):
        return left != right
    # Text ordering depends on the database collation, and booleans/arrays do
    # not need it here: only numeric order is proven.
    if isinstance(left, (str, bool, list)):
        raise _Unsupported("order")
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    return left >= right


def _in(value, items):
    if value is None:
        return None
    unknown = False
    for item in items:
        compared = _compare("=", value, item)
        if compared is True:
            return True
        if compared is None:
            unknown = True
    return None if unknown else False


def _sql_not(value):
    return None if value is None else not value


def _sql_and(left, right):
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _sql_or(left, right):
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


_CHECK_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<literal>__staging_lit_[0-9]+__)
  | (?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)
  | (?P<word>[a-z_][a-z0-9_$]*)
  | (?P<op><=|>=|<>|!=|=|<|>)
  | (?P<sign>[+-])
  | (?P<punct>[(),])
""", re.X | re.I)


class _CheckParser:
    """A deliberately small, fail-closed evaluator for an inline CHECK.

    Only the added column (bound to its default), literals, comparisons,
    IS [NOT] NULL, IN and the boolean operators are understood. Anything else
    — another column, a function, a cast, another operator — is refused, so
    the caller keeps the statement destructive.
    """

    def __init__(self, expression, column, default, literals):
        self.column = column
        self.default = default
        self.literals = literals
        self.tokens = self._tokenize(expression)
        self.at = 0

    @staticmethod
    def _tokenize(expression):
        tokens, at = [], 0
        while at < len(expression):
            match = _CHECK_TOKEN.match(expression, at)
            if match is None:
                raise _Unsupported("token")
            at = match.end()
            if match.lastgroup != "space":
                tokens.append((match.lastgroup, match.group()))
        return tokens

    def parse(self):
        value = _truth(self._or())
        if self.at != len(self.tokens):
            raise _Unsupported("trailing")
        return value

    def _peek(self):
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _accept(self, kind, text=None):
        token = self._peek()
        if token is None or token[0] != kind or (text is not None and token[1] != text):
            return None
        self.at += 1
        return token

    def _value(self, value):
        return self.default if value is _COLUMN_REF else value

    def _or(self):
        value = self._and()
        while self._accept("word", "or"):
            value = _sql_or(_truth(value), _truth(self._and()))
        return value

    def _and(self):
        value = self._not()
        while self._accept("word", "and"):
            value = _sql_and(_truth(value), _truth(self._not()))
        return value

    def _not(self):
        if self._accept("word", "not"):
            return _sql_not(_truth(self._not()))
        return self._predicate()

    def _predicate(self):
        left = self._primary()
        token = self._peek()
        if token is not None and token[0] == "op":
            self.at += 1
            return _compare(token[1], self._value(left),
                            self._value(self._primary()))
        if self._accept("word", "is"):
            negate = bool(self._accept("word", "not"))
            if not self._accept("word", "null"):
                raise _Unsupported("is")
            value = self._value(left)
            return (value is not None) if negate else (value is None)
        if self._accept("word", "in"):
            if not self._accept("punct", "("):
                raise _Unsupported("in")
            items = [self._primary()]
            while self._accept("punct", ","):
                items.append(self._primary())
            if not self._accept("punct", ")"):
                raise _Unsupported("in")
            return _in(self._value(left), [self._value(item) for item in items])
        return left

    def _primary(self):
        token = self._peek()
        if token is None:
            raise _Unsupported("end")
        self.at += 1
        kind, text = token
        if kind == "literal":
            return _literal_value(self.literals[text])
        if kind == "number":
            return float(text) if any(ch in text for ch in ".e") else int(text)
        if kind == "word":
            if text == "true":
                return True
            if text == "false":
                return False
            if text == "null":
                return None
            if text == self.column:
                return _COLUMN_REF
            raise _Unsupported("identifier")
        if kind == "sign":
            following = self._peek()
            if following is None or following[0] != "number":
                raise _Unsupported("sign")
            number = self._primary()
            return -number if text == "-" else number
        if kind == "punct" and text == "(":
            value = self._or()
            if not self._accept("punct", ")"):
                raise _Unsupported("paren")
            return value
        raise _Unsupported("primary")


def _check_accepts(expression, column, default, literals):
    """True if the CHECK accepts the default, False if it rejects it, None if
    the expression cannot be proven (then the caller keeps it destructive)."""
    try:
        return _CheckParser(expression, column, default, literals).parse() is not False
    except _Unsupported:
        return None


# --- #7655 : preuves déjà revues avec la migration (L151) ------------------
#
# Les marqueurs `-- ameesh: …` sont la déclaration de l'auteur, relue avec la
# migration, que `migrations.non_additive` honore déjà. La porte reprend les
# mêmes marqueurs, sans élargir leur portée :
#   * `constraint not tightened` ne couvre que des CHECK (jamais UNIQUE, PK,
#     FK), et un DROP CONSTRAINT n'est idempotent que si le même nom est
#     rajouté dans la migration ;
#   * `trigger compatible with previous code` couvre la création du trigger et
#     son éventuel DROP IF EXISTS + CREATE du même nom sur la même table.
# Une fonction `CREATE OR REPLACE` n'est additive que si son nom est absent de
# toute migration antérieure du SHA gelé : la preuve se lit dans l'arbre, pas
# dans le fichier.

_ALTER_TABLE_HEAD = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?P<table>[\w.\"]+)\s+", re.I)
_ADD_CONSTRAINT_ACTION = re.compile(
    r"add constraint (?P<name>[\w\"]+) check \((?P<check>.*)\)", re.I | re.S)
_DROP_CONSTRAINT_ACTION = re.compile(
    r"drop constraint (?:if exists )?(?P<name>[\w\"]+)", re.I)
_CREATE_TRIGGER = re.compile(
    r"create (?:or replace )?(?:constraint )?trigger (?P<name>[\w\"]+)\s+"
    r"(?P<rest>.*)", re.I | re.S)
_TRIGGER_TARGET = re.compile(r"\bon (?:only )?([\w.\"]+)", re.I)
_DROP_TRIGGER = re.compile(
    r"drop trigger (if exists )?(?P<name>[\w\"]+) on (?P<table>[\w.\"]+)", re.I)
_CREATE_OR_REPLACE_ROUTINE = re.compile(
    r"create or replace (?P<kind>function|procedure) (?P<name>[\w\"]+)\s*\(", re.I)
_NULLABLE_COLUMN = (
    r"add column (?:if not exists )?(?P<column>[\w\"]+) "
    r"[\w.]+(?:\[\])?(?: null)?")
_NULLABLE_CONSTANT = (
    r"add column (?:if not exists )?(?P<column>[\w\"]+) "
    + _BUILTIN + r"(?:\[\])?"
    + r"(?: null)?(?: default (?P<default>" + _CONSTANT + ")" + _CAST + r")")


def _alter_actions(statement):
    """``(table, actions)`` of an ``ALTER TABLE``, splitting top-level commas."""
    match = _ALTER_TABLE_HEAD.match(statement)
    if not match:
        return None
    text, actions, depth, start = statement, [], 0, match.end()
    for at in range(start, len(text)):
        if text[at] == "(":
            depth += 1
        elif text[at] == ")":
            depth -= 1
            if depth < 0:
                return None
        elif text[at] == "," and depth == 0:
            actions.append(text[start:at].strip())
            start = at + 1
    if depth:
        return None
    actions.append(text[start:].strip())
    if not all(actions):
        return None
    return match.group("table"), actions


def _nullable_column(action, literals):
    """A new nullable column. Without a default any simple type is safe (old
    writers omit it, so the value is NULL); with a constant default the type
    must be a built-in, for the same domain-cast reason as a mandatory column,
    and an inline CHECK must accept the value N-1 writers produce."""
    for tail in ("", r"\s+check\s*\((?P<check>.*)\)"):
        match = re.fullmatch(_NULLABLE_COLUMN + tail, action, re.I | re.S)
        if match is not None:
            check = match.group("check") if tail else None
            if check is None or _check_accepts(
                    check, match.group("column"), None, literals) is True:
                return True
    for tail in ("", r"\s+check\s*\((?P<check>.*)\)"):
        match = re.fullmatch(_NULLABLE_CONSTANT + tail, action, re.I | re.S)
        if match is None:
            continue
        check = match.group("check") if tail else None
        if check is None:
            return True
        raw = match.group("default")
        try:
            value = _default_value(raw, literals)
        except _Unsupported:
            continue
        if _check_accepts(check, match.group("column"), value, literals) is True:
            return True
    return False


def _constant_column(definition, literals):
    """A mandatory column is additive only with a constant default the inline
    CHECK provably accepts (#7531)."""
    default, check, column = definition
    if check is None:
        return True
    try:
        value = _default_value(default, literals)
    except _Unsupported:
        return False
    return _check_accepts(check, column, value, literals) is True


def _additive_column_action(action, literals):
    if _nullable_column(action, literals):
        return True
    definition = _column_definition("alter table t " + action)
    return definition is not None and _constant_column(definition, literals)


def _simple_name(value):
    """The lexer replaces every quoted identifier by the same ``"_"`` token, so
    a quoted name cannot be paired or proven without confusing two objects."""
    return bool(value) and '"' not in value


def _proven_additive(statement, code, literals, *, proves_new, constraint_marker,
                     trigger_marker, added_constraints, created_triggers,
                     unconditional):
    """Reasons this statement cannot break N-1 code, or None.

    Only exact shapes are accepted: every ALTER TABLE action is proven on its
    own, so a single DROP COLUMN or an unmarked constraint keeps the statement
    in the generic (worst-class) path.
    """
    altered = _alter_actions(statement)
    if altered is not None:
        table, actions = altered
        reasons = []
        for action in actions:
            if _additive_column_action(action, literals):
                reasons.append("additive column: " + action)
                continue
            constraint = _ADD_CONSTRAINT_ACTION.fullmatch(action)
            if (constraint is not None and constraint_marker
                    and _simple_name(table) and _simple_name(constraint.group("name"))):
                reasons.append("constraint %s declared not tightened"
                               % constraint.group("name"))
                continue
            dropped = _DROP_CONSTRAINT_ACTION.fullmatch(action)
            if (dropped is not None and constraint_marker and unconditional
                    and _simple_name(table) and _simple_name(dropped.group("name"))
                    and (table, dropped.group("name")) in added_constraints):
                reasons.append("constraint %s replaced, declared not tightened"
                               % dropped.group("name"))
                continue
            return None
        return tuple(reasons) or None
    trigger = _CREATE_TRIGGER.fullmatch(statement)
    if trigger is not None:
        target = _TRIGGER_TARGET.search(trigger.group("rest"))
        table = target.group(1) if target else None
        if trigger_marker and _simple_name(trigger.group("name")) and _simple_name(table):
            return ("trigger %s declared compatible with previous code"
                    % trigger.group("name"),)
        # A CREATE TABLE flattened out of a DO block may be conditional
        # (to_regclass guard): the table is then not proven new.
        if (unconditional and table and _simple_name(table)
                and migrations._proven_new(code, "table", table)):
            return ("trigger %s on a table created by this migration"
                    % trigger.group("name"),)
        return None
    dropped = _DROP_TRIGGER.fullmatch(statement)
    if dropped is not None:
        name, table = dropped.group("name"), dropped.group("table")
        if (dropped.group(1) and unconditional and _simple_name(name)
                and _simple_name(table) and (table, name) in created_triggers
                and (trigger_marker or migrations._proven_new(code, "table", table))):
            return ("trigger %s recreated in this migration" % name,)
        return None
    routine = _CREATE_OR_REPLACE_ROUTINE.match(statement)
    if routine is not None:
        name = routine.group("name")
        if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and proves_new(routine.group("kind"), name):
            return ("routine %s absent from earlier migrations" % name,)
        return None
    return None


def _residual_hits(policy, statement):
    """Project exceptions keep their class, even on a proven-additive shape."""
    hits = []
    for category in ("destructive", "relaxation"):
        for entry in policy["rules"][category]:
            if entry in _DEFAULT_RULES[category]:
                continue
            if re.fullmatch(entry["pattern"], statement, re.I | re.S):
                hits.append(entry["reason"])
    return hits


def classify(sql, policy=None, *, proves_new=None):
    policy = load_policy() if policy is None else policy
    proves_new = (lambda kind, name, signature="": False) if proves_new is None else proves_new
    literals = {}
    try:
        code = _code(sql, literals=literals)
    except Refused as exc:
        return Classification("unknown", (str(exc),))
    creates_trigger = bool(re.search(
        r"(?:^|;)\s*create\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\b", code))
    if not code.strip(" ;"):
        return Classification("unknown", ("no SQL statement",))
    rank = {"additive": 0, "relaxation": 1, "destructive": 2, "unknown": 3}
    unconditional = not re.search(r"\bdo\b", migrations._lex(sql)[1], re.I)
    constraint_marker = bool(migrations._CONSTRAINT_MARKER.search(sql))
    trigger_marker = bool(migrations._TRIGGER_MARKER.search(sql))
    # Names are collected before judging any statement, so a DROP CONSTRAINT
    # or DROP TRIGGER is idempotent only when the same name is recreated.
    added_constraints, created_triggers = set(), set()
    for statement in code.split(";"):
        altered = _alter_actions(statement.strip())
        if altered is not None:
            table, actions = altered
            for action in actions:
                constraint = _ADD_CONSTRAINT_ACTION.fullmatch(action)
                if constraint is not None:
                    added_constraints.add((table, constraint.group("name")))
        trigger = _CREATE_TRIGGER.fullmatch(statement.strip())
        if trigger is not None:
            target = _TRIGGER_TARGET.search(trigger.group("rest"))
            if target is not None:
                created_triggers.add((target.group(1), trigger.group("name")))
    kind, reasons = "additive", []
    unsafe_trigger = False
    for statement in code.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        if re.match(r"(?:execute|call|do)\b", statement):
            return Classification("unknown", ("dynamic SQL not analysed",), creates_trigger)
        if unconditional and _new_routine_revoke(statement, code):
            continue
        proof = _proven_additive(
            statement, code, literals, proves_new=proves_new,
            constraint_marker=constraint_marker, trigger_marker=trigger_marker,
            added_constraints=added_constraints, created_triggers=created_triggers,
            unconditional=unconditional)
        if proof is not None and not _residual_hits(policy, statement):
            # The marker or the frozen proof is the reviewed declaration: an
            # additive file needs no policy entry. Project exceptions keep
            # their class through _residual_hits.
            reasons.extend(proof)
            continue
        if _CREATE_TRIGGER.fullmatch(statement) is not None:
            unsafe_trigger = True
        # Match the entire addition, keeping literal contents opaque. Extra
        # actions, constraints, NULL and computed defaults cannot pass here.
        definition = _column_definition(statement)
        constant_column = False
        if definition is not None:
            default, check, column = definition
            if check is None:
                constant_column = True
            else:
                try:
                    value = _default_value(default, literals)
                except _Unsupported:
                    pass
                else:
                    # A column constraint's CHECK only has to accept the value
                    # N-1 writers produce: the default (they omit the column).
                    # A proven rejection, or an expression we cannot evaluate,
                    # keeps the add destructive (#7531).
                    constant_column = _check_accepts(check, column, value,
                                                     literals) is True
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
    return Classification(kind, tuple(dict.fromkeys(reasons)), unsafe_trigger)


def check(path, data, policy, *, read_file, signed_decision, proves_new=None):
    """The frozen policy entry itself is the reviewed migration declaration."""
    try:
        result = classify(data.decode("utf-8"), policy, proves_new=proves_new)
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
        self._grep_cache = {}
        self.git("rev-parse", "--verify", sha + "^{commit}")

    def git(self, *args):
        try:
            return subprocess.run(["git", "-C", self.repo, *args], capture_output=True,
                                  check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused("frozen Git read failed: %s" % " ".join(args)) from exc

    def grep(self, word):
        """Repository-relative migration paths whose frozen text contains
        `word`, case-insensitively. A fixed-string search: the word is SQL, not
        a pattern. Exit 1 is an honest "no match"; any other failure refuses."""
        if word in self._grep_cache:
            return self._grep_cache[word]
        try:
            result = subprocess.run(
                ["git", "-C", self.repo, "grep", "-l", "-i", "-w", "-F", "-e",
                 word, self.sha, "--", migrations.REPO_PATH],
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused("frozen Git read failed: git grep") from exc
        if result.returncode == 1:
            paths = []
        elif result.returncode != 0:
            raise Refused("frozen Git read failed: git grep")
        else:
            paths = [line.partition(":")[2] for line in
                     result.stdout.decode("utf-8", "replace").splitlines()
                     if line.partition(":")[2]]
        self._grep_cache[word] = paths
        return paths

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


def decision_verifier(db, project, *, public_only=False):
    """Use full authority before migrations, or public consumed proofs for bureau."""
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
            decisions.require_current(db, view, public_only=public_only)
            return decisions.verify_proof(db, view, public_only=public_only)[0]
        except decisions.DecisionError:
            return False
    return verify


def _migration_version(path):
    """Version of a top-level frozen migration file, or None for any other path."""
    if not isinstance(path, str):
        return None
    candidate = PurePosixPath(path)
    if candidate.parent != PurePosixPath(migrations.REPO_PATH):
        return None
    match = re.fullmatch(r"(\d{4})_[A-Za-z0-9._-]+\.sql", candidate.name)
    return int(match.group(1)) if match else None


def _history_prover(tree, path):
    """Prove a routine new from the frozen tree: its name is absent from every
    migration of a *lower* version. Mentions in later files, rollback files and
    the migration itself do not count. Unknown paths or non-simple names stay
    conservative (no proof)."""
    version = _migration_version(path)
    if version is None:
        return None

    def proves_new(kind, name, signature=""):
        if kind not in ("function", "procedure") or not re.fullmatch(
                r"[a-z_][a-z0-9_]*", name):
            return False
        for found in tree.grep(name):
            earlier = _migration_version(found)
            if earlier is not None and earlier < version:
                return False
        return True

    return proves_new


def inventory(tree, paths, *, signed_decision):
    policy, rows, refused = tree.policy(), [], False
    for path in dict.fromkeys(paths):
        try:
            data = tree.read(path)
            classification = check(path, data, policy, read_file=tree.read,
                                   signed_decision=signed_decision,
                                   proves_new=_history_prover(tree, path))
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
    public_only = False

    def signed_decision(identifier, lot, option):
        nonlocal connection, public_only
        # The usual ameesh proof reader is opened only for destructive SQL.
        # No staging credential, SQL execution, or credential CLI option.
        from . import config, db
        if connection is None:
            connection = db.connect(
                cfg or config.load(register_credentials=False), writer=False)
            # Before 0583 the public view does not exist; after installation
            # consumed proofs support both full and limited bureau connections.
            public_only = connection.query(
                "SELECT to_regclass('ameesh_decision_proofs') IS NOT NULL AS present"
            )[0]["present"]
        return decision_verifier(connection, project, public_only=public_only)(
            identifier, lot, option)

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
