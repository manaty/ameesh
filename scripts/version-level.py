#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Does a version number match its content? (lot L158, CONTRIBUTING.md
"Numérotation des versions")

    scripts/version-level.py pr [BASE [HEAD]]   a pull request (CI)
    scripts/version-level.py release vX.Y.Z     a published version (auditor)
    scripts/version-level.py level [BASE [HEAD]]   the level of a range only

`pr` compares `BASE` and `HEAD` (default `HEAD^1` and `HEAD`: the merge
commit of a PR that `actions/checkout` extracts, against its target). If the
`version = "…"` line of `pyproject.toml` is unchanged, it is not a version
PR: nothing to check. Otherwise the content is the range from the last `v*`
tag reachable from `BASE` to `HEAD`. `release` takes the range from the tag
before `vX.Y.Z` to `vX.Y.Z`.

The level found in the range is a floor, from what a script can see:

- major: a migration that existed in the previous version changed or
  removed; a new migration that drops a table, column (`DROP x` included),
  schema, type or sequence, renames, changes a column type, adds `NOT NULL`,
  truncates or deletes rows (dropping a view, trigger, index, constraint,
  default or function to recreate it is not a break, nor is the `ON COMMIT
  DROP` of a temporary table: it holds no data); a command or command
  alias removed, an option string removed from a command (each command has
  its own: removing `show --json` while `list --json` stays is a break), an
  `AMEESH_*` setting no longer read; a commit with the trailer
  `ameesh-breaking: <why>` (an agent contract changed: no script sees that);
- minor: a new migration; a new command, alias or option of a command; a
  new `AMEESH_*` setting read by the package;
- patch: anything else.

The CLI is read from the syntax tree of each module (`add_parser` with its
`aliases`, every option string of `add_argument`), not from the text:
reordering the aliases of an option changes nothing. A parser built by
`argparse.ArgumentParser` or by the package's own subclass
(`HiddenAliasParser`, L148) is read the same way. A parser handed to a
function, of the same module or another, by position or by name, fills the
caller's command; a loop over literal strings counts each of them. In a new
migration, comments, strings and function bodies are not statements; the
body of a `DO` block is (in dollars or in a plain string), with the strings,
plain or in dollars, it hands to `EXECUTE` or `format(…)`. A package module
is reached by alias, by its full name (`ameesh.plan_cli.add_parsers`) or
through a module that imports a helper and hands it on; an old path that only
aliases the module a command moved to (`sys.modules[__name__] = moved`,
#4382) is read at the module it hands back; an import inside a
function or a class body binds only there (not in the functions or methods
it nests, nor around it), and hides a module function of the same name.

Known limits (the script helps the CI and the auditor, it does not replace
the review): strings are read with `standard_conforming_strings` on; the
escapes of an `E''` string are not decoded (`E'\x44ROP'` is not seen as
`DROP`); a dollar tag is ASCII; a `U&"…"` identifier is not decoded; a
function body is not a statement even when the migration runs it later
(`SELECT f()`). In Python, a scope's imports hold for the whole scope, not
from their line on; a helper rebound by assignment, `global` or `nonlocal`,
and a nested function's own calls, are not followed.

The number must be the next one at its own level (1.9.0 → 1.9.1, 1.10.0 or
2.0.0), and that level must not be below the floor. A level above the floor
passes, with a note: a new function without a new command is minor, and no
script sees it; the auditor reads the notes. Exit code 0 when it matches (or
not a version PR), 1 when it does not, 2 on a usage or git error. Git runs
in the checkout that holds this script, whatever the current directory.
"""
from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

VERSION_FILE = "pyproject.toml"
MIGRATIONS = "src/ameesh/migrations/"
SOURCES = "src/ameesh/"
LEVELS = ("patch", "minor", "major")
TRAILER = "ameesh-breaking"
#: program names a parser's `prog` starts with (the top level)
PROGRAMS = ("ameesh", "agent-mesh")
#: parser classes read as `argparse.ArgumentParser`: the project's own
#: subclass (`HiddenAliasParser`, L148) keeps the command tree readable
PARSER_CLASSES = ("ArgumentParser", "HiddenAliasParser")
#: the repository: the checkout this script belongs to, whatever the caller's
#: directory (`python3 ameesh/scripts/version-level.py` from its parent works);
#: the tests point it elsewhere
REPO = str(pathlib.Path(__file__).resolve().parent.parent)
_VERSION = re.compile(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)[^"]*"\s*$', re.M)
#: in a new migration: drops anything but what is recreated or holds no data
#: (`ALTER TABLE a DROP x` drops a column), renames, changes a column type
#: (`COLUMN` is optional, the name may be quoted), adds `NOT NULL`, empties
_IDENT = r'(?:"(?:[^"]|"")+"|\w+)'
_DESTRUCTIVE = re.compile(
    r"\bdrop\s+(?!(?:constraint|trigger|view|materialized\s+view|index|default|not\s+null"
    r"|function|procedure|policy|rule|identity|expression)\b)\S+"
    r"|\brename\b"
    r"|\balter\s+(?:column\s+)?%s\s+(?:set\s+data\s+)?type\b"
    r"|\bset\s+not\s+null\b|\btruncate\b|\bdelete\s+from\b" % _IDENT, re.I)
#: a temporary table dropped at the end of the migration's transaction
#: (`CREATE TEMP TABLE t ON COMMIT DROP AS …`, 0460, #2470): not a drop
_ON_COMMIT_DROP = re.compile(r"\bon\s+commit\s+drop\b", re.I)
_MAIN_COMMAND = re.compile(r"""command\s*(?:==\s*["']([\w-]+)["']|in\s*\(([^)]*)\))""")
_SETTING = re.compile(r"""["'](AMEESH_[A-Z0-9_]+)["']""")


class Error(RuntimeError):
    pass


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO)
    if proc.returncode:
        raise Error("git %s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def version_at(ref: str) -> tuple[int, int, int]:
    found = _VERSION.search(git("show", "%s:%s" % (ref, VERSION_FILE)))
    if not found:
        raise Error("no version line in %s at %s" % (VERSION_FILE, ref))
    return tuple(int(n) for n in found.groups())  # type: ignore[return-value]


def show(version) -> str:
    return "%d.%d.%d" % tuple(version)


def last_tag(ref: str) -> str:
    return git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", ref).strip()


_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
#: what precedes the body of an anonymous block, executed by the migration
_DO = re.compile(r'^\s*do(?:\s+language\s+(?:\w+|"q"))?\s*$', re.I)
#: in an anonymous block: a string run as a statement
_DYNAMIC = re.compile(r"(?:\bexecute|\bformat\s*\()\s*$", re.I)


def _sql(text: str, block: bool = False) -> str:
    """The SQL without what is not a statement: comments, string literals
    (`E'it\\'s'` included) and dollar-quoted bodies (`DEFAULT 'rename'`, a
    function's `DELETE FROM`), quoted identifiers kept as a neutral `"q"` (a
    column named "rename"). A dollar body closes on its own tag only. The
    body of a `DO` block runs at migration time: it is read as SQL, and so
    are the strings it `EXECUTE`s (`block`)."""
    out: list[str] = []
    statement = ""  # the current statement, as kept so far
    i, n = 0, len(text)

    def keep(piece: str) -> None:
        nonlocal statement
        out.append(piece)
        statement += piece

    while i < n:
        c = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            keep(" ")
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            keep(" ")
        elif c == "'" or c in "Ee" and text.startswith("'", i + 1) \
                and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$")):
            escapes = c != "'"
            i += 2 if escapes else 1
            body = []
            while i < n:
                if escapes and text[i] == "\\" and i + 1 < n:
                    body.append(text[i + 1])
                    i += 2
                elif text.startswith("''", i):
                    body.append("'")
                    i += 2
                elif text[i] == "'":
                    i += 1
                    break
                else:
                    body.append(text[i])
                    i += 1
            if _DO.match(statement):  # `DO 'BEGIN … END'`
                keep(" %s " % _sql("".join(body), block=True))
            elif block and _DYNAMIC.search(statement):
                keep(" %s " % _sql("".join(body)))
            else:
                keep("''")
        elif c == '"':
            end = i + 1
            while True:
                end = text.find('"', end)
                if end < 0 or not text.startswith('""', end):
                    break
                end += 2
            i = n if end < 0 else end + 1
            keep('"q"')
        elif c == "$" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$")) \
                and _DOLLAR.match(text, i):
            tag = _DOLLAR.match(text, i).group(0)
            end = text.find(tag, i + len(tag))
            body = text[i + len(tag):n if end < 0 else end]
            i = n if end < 0 else end + len(tag)
            if _DO.match(statement):
                keep(" %s " % _sql(body, block=True))
            elif block and _DYNAMIC.search(statement):  # `EXECUTE $$…$$`
                keep(" %s " % _sql(body))
            else:
                keep("''")
        elif c == ";":
            out.append(c)
            statement = ""
            i += 1
        else:
            keep(c)
            i += 1
    return "".join(out)


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _scope(nodes):
    """The nodes of one scope: a nested function or class is its own scope,
    its imports bind nothing here."""
    todo = list(nodes)
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, _NESTED):
            todo.extend(ast.iter_child_nodes(node))


def _alias(tree: ast.Module, modules: dict[str, str]) -> str | None:
    """The module this file hands back for its own name, when the old import
    path only aliases a moved one (`sys.modules[__name__] = _moved`, #4382):
    a caller's `from . import moved` then gets the real module, parser
    included."""
    for node in _scope(tree.body):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not (isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "modules"
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "sys"
                and isinstance(target.slice, ast.Name) and target.slice.id == "__name__"):
            continue
        if isinstance(value, ast.Name) and value.id in modules:
            return modules[value.id]
    return None


def _imports(nodes, module: str, init: bool = False,
             ) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """The package modules imported in `nodes` (one scope: a module without
    its functions, or a function): {alias: module} (`from . import budget as
    budget_mod`; `import ameesh.plan_cli` binds `ameesh` to the package,
    ""), and the functions imported by name: {alias: (module, function)}
    (`from .plan_cli import add_parsers`)."""
    package = (module.split(".") if module else []) if init else module.split(".")[:-1]
    modules: dict[str, str] = {}
    functions: dict[str, tuple[str, str]] = {}
    for node in _scope(nodes):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                if node.level - 1 > len(package):
                    continue
            elif (node.module or "").split(".")[0] in PROGRAMS[:1]:
                base = []
            else:
                continue
            words = (node.module or "").split(".") if node.module else []
            if not node.level:
                words = words[1:]  # `from ameesh.x import y`
            for name in node.names:
                alias = name.asname or name.name
                modules[alias] = ".".join(base + words + [name.name])
                if words or base:
                    functions[alias] = (".".join(base + words), name.name)
        elif isinstance(node, ast.Import):
            for name in node.names:
                words = name.name.split(".")
                if words[0] == PROGRAMS[0] and len(words) > 1 and name.asname:
                    modules[name.asname] = ".".join(words[1:])
                elif words[0] == PROGRAMS[0] and not name.asname:
                    modules[words[0]] = ""  # `ameesh.plan_cli.add_parsers(…)`
    return modules, functions


class _Package:
    """Every module of the package at one ref: a parser handed to a function
    of another module (`plan_cli.add_parsers(work_sub, cmd_work)`) fills the
    caller's command, not the top level."""

    def __init__(self, trees: dict[str, tuple[ast.Module, bool]]):
        self.commands: set[str] = set()
        self.options: set[str] = set()
        self.files = {module: _Parsers(tree, module, self, init)
                      for module, (tree, init) in trees.items()}
        #: a deprecated import path aliasing the module it moved to
        #: (`sys.modules[__name__]`, #4382): the parser it registers is the
        #: real module's
        self.redirects = {}
        for module, parsers in self.files.items():
            target = _alias(parsers.tree, parsers.top[0])
            if target is not None and target != module:
                self.redirects[module] = target
        #: (module, function) running, against recursion across modules
        self.stack: list[tuple[str, str]] = []
        #: functions called somewhere in the package: their callers say
        #: which parser they fill, so they are never also read as filling the
        #: top level (inlining such a helper, or moving it, changes nothing)
        self.called = {self.resolve(callee) for parsers in self.files.values()
                       for callee in parsers.calls()} - {None}

    def redirect(self, module: str) -> str:
        """The module an alias path hands back, following the chain."""
        seen = set()
        while module in self.redirects and module not in seen:
            seen.add(module)
            module = self.redirects[module]
        return module

    def resolve(self, callee: tuple[str, str]) -> tuple[str, str] | None:
        """Where a function is defined: a module may import it from another
        and hand it on (`bridge.fill`, imported in `bridge` from `plan_cli`),
        or alias the module it moved to (`sys.modules[__name__]`, #4382)."""
        seen = set()
        while callee is not None and callee not in seen:
            seen.add(callee)
            callee = (self.redirect(callee[0]), callee[1])
            parsers = self.files.get(callee[0])
            if parsers is None:
                return None
            if callee[1] in parsers.functions:
                return callee
            callee = parsers.top[1].get(callee[1])
        return None

    def function(self, callee: tuple[str, str]):
        callee = self.resolve(callee)
        if callee is None:
            return None, None
        parsers = self.files[callee[0]]
        return parsers, parsers.functions[callee[1]]

    def walk(self) -> None:
        for parsers in self.files.values():
            parsers.walk()


class _Parsers(ast.NodeVisitor):
    """The argparse tree of one file, statement after statement: a variable
    bound to `add_parser("x")` is the command path `… x`; `add_subparsers()`
    and argument groups keep the path of their parser; a function of the
    package called with a parser (by position or by name, from this file or
    another) runs with its parameter bound to that path; a loop over literal
    strings runs once per string. A variable the file does not bind (a
    parameter) is the top level. Every option string of `add_argument` is
    kept with its command, so `show --json` and `list --json` are two
    entries."""

    def __init__(self, tree: ast.Module, module: str = "?", package: "_Package | None" = None,
                 init: bool = False):
        self.tree = tree
        self.module = module
        self.init = init
        self.package = package
        self.functions = {n.name: n for n in tree.body
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        #: imports per scope: the module (without its functions), and each
        #: function, which sees the module's and its own (`import` in a
        #: function binds nothing elsewhere)
        self.top = _imports([n for n in tree.body if n not in self.functions.values()],
                            module, init)
        self.scopes = {}
        #: names a function imports itself: they hide the module's functions
        self.own: dict[str, set[str]] = {}
        for name, function in self.functions.items():
            modules, imported = _imports(function.body, module, init)
            self.scopes[name] = (dict(self.top[0], **modules), dict(self.top[1], **imported))
            self.own[name] = set(modules) | set(imported)
        self.modules, self.imported = self.top
        self.local: set[str] = set()
        self.env: dict[str, tuple[str, ...]] = {}
        #: names bound to a literal string (a loop over literals, `x = "y"`)
        self.consts: dict[str, str] = {}
        #: may an unbound name (a parameter) stand for the top level?
        self.unbound = True
        #: in a class body, the scope around the outermost class (env, consts,
        #: modules, imported, local): a class's names are seen by no scope it
        #: nests, neither method, lambda nor inner class
        self.outer = None

    @property
    def commands(self) -> set[str]:
        return self.package.commands

    @property
    def options(self) -> set[str]:
        return self.package.options

    def calls(self):
        """The callees of every call of the file, each read in its scope."""
        scopes = [(None, [n for n in self.tree.body if n not in self.functions.values()])]
        scopes += [(name, function.body) for name, function in self.functions.items()]
        saved = self.modules, self.imported, self.local
        found: list[tuple[str, str]] = []
        try:
            for name, nodes in scopes:
                self.modules, self.imported = self.scopes.get(name, self.top)
                self.local = self.own.get(name, set())
                self._calls(nodes, found)
        finally:
            self.modules, self.imported, self.local = saved
        return found

    def _calls(self, nodes, found: list, outer=None) -> None:
        """The callees of the calls in `nodes`; a class body is read with its
        own imports, its methods, lambdas and inner classes with the scope
        around the outermost class (`outer`)."""
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                self._calls(node.decorator_list + node.bases + node.keywords, found, outer)
                around = self.modules, self.imported, self.local
                outer = outer or around
                self.modules, self.imported, self.local = outer
                self._enter_class(node)
                try:
                    self._calls(node.body, found, outer)
                finally:
                    self.modules, self.imported, self.local = around
                continue
            if outer and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inside = self.modules, self.imported, self.local
                self.modules, self.imported, self.local = outer
                try:
                    self._calls(ast.iter_child_nodes(node), found)
                finally:
                    self.modules, self.imported, self.local = inside
                continue
            if isinstance(node, ast.Call) and self.callee(node.func):
                found.append(self.callee(node.func))
            self._calls(ast.iter_child_nodes(node), found, outer)

    def _enter_class(self, node) -> None:
        """The scope of a class body: the one around it, with its own imports
        on top (they hide a module function of the same name)."""
        modules, imported = _imports(node.body, self.module, self.init)
        self.modules = dict(self.modules, **modules)
        self.imported = dict(self.imported, **imported)
        self.local = self.local | set(modules) | set(imported)

    def callee(self, func) -> tuple[str, str] | None:
        """The (module, function) of the package a call targets, if known
        (`plan_cli.add_parsers`, `ameesh.plan_cli.add_parsers`)."""
        if isinstance(func, ast.Name):
            if func.id in self.functions and func.id not in self.local:
                return self.module, func.id
            return self.imported.get(func.id)
        if not isinstance(func, ast.Attribute):
            return None
        words, value = [], func.value
        while isinstance(value, ast.Attribute):
            words.insert(0, value.attr)
            value = value.value
        if isinstance(value, ast.Name) and value.id in self.modules:
            return ".".join(filter(None, [self.modules[value.id]] + words)), func.attr
        return None

    def text(self, node) -> str | None:
        """A literal string, or a name bound to one."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        return None

    def strings(self, node) -> list[str]:
        """The strings of a list or tuple node (nothing else)."""
        if isinstance(node, (ast.List, ast.Tuple)):
            return [t for t in map(self.text, node.elts) if t is not None]
        return []

    def path(self, node) -> tuple[str, ...] | None:
        """The command path of a parser expression: a known variable, a call
        that builds a parser, else the top level (an unknown name or
        attribute); None for anything else."""
        if isinstance(node, ast.Name):
            if node.id in self.consts:
                return None
            return self.env.get(node.id, () if self.unbound else None)
        if isinstance(node, ast.Attribute):
            return () if self.unbound else None
        return self.parser_of(node)  # `sub.add_parser("x").add_subparsers()`

    def own_parser(self, node) -> tuple[str, ...]:
        """The path of a separately routed `ArgumentParser(prog=…)`: its
        `prog` without the program name (`ameesh attach` → `attach`), else
        the function that builds it. `ameesh run --wait` and `ameesh attach
        --wait` are then two options, not one global `--wait`."""
        for keyword in node.keywords:
            if keyword.arg == "prog" and self.text(keyword.value) is not None:
                words = self.text(keyword.value).split()
                return tuple(words[1:] if words and words[0] in PROGRAMS else words)
        stack = self.package.stack if self.package else []
        return ("%s:%s" % (self.module, stack[-1][1] if stack else "<module>"),)

    def parser_of(self, node) -> tuple[str, ...] | None:
        """The path a call returns when it builds a parser, None otherwise."""
        if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in PARSER_CLASSES
                or isinstance(node.func, ast.Attribute) and node.func.attr in PARSER_CLASSES):
            return self.own_parser(node)
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return None
        base = self.path(node.func.value)
        if base is None:
            return None
        if node.func.attr == "add_parser" and node.args and self.text(node.args[0]) is not None:
            return base + (self.text(node.args[0]),)
        if node.func.attr in ("add_subparsers", "add_argument_group",
                              "add_mutually_exclusive_group"):
            return base
        return None

    def visit_Assign(self, node):
        self.visit(node.value)
        built = self.parser_of(node.value)
        value = self.text(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.env.pop(target.id, None)
                self.consts.pop(target.id, None)
                if built is not None:
                    self.env[target.id] = built
                elif value is not None:
                    self.consts[target.id] = value

    def _bind(self, target, node, consts: dict[str, str]) -> None:
        if isinstance(target, ast.Name) and self.text(node) is not None:
            consts[target.id] = self.text(node)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(node, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(node.elts):
            for one, value in zip(target.elts, node.elts):
                self._bind(one, value, consts)

    def visit_For(self, node):
        if not isinstance(node.iter, (ast.List, ast.Tuple)):
            self.generic_visit(node)
            return
        saved = dict(self.consts)
        for element in node.iter.elts:  # a loop over literals: once per value
            consts: dict[str, str] = {}
            self._bind(node.target, element, consts)
            self.consts = dict(saved, **consts)
            for statement in node.body:
                self.visit(statement)
        self.consts = saved
        for statement in node.orelse:
            self.visit(statement)

    def visit_Call(self, node):
        self.generic_visit(node)
        func = node.func
        callee = self.callee(func)
        if callee is not None and self.package is not None:
            callee = self.package.resolve(callee)
            parsers, function = self.package.function(callee) if callee else (None, None)
            if function is None or callee in self.package.stack:
                return
            arguments = function.args
            params = [a.arg for a in arguments.posonlyargs + arguments.args]
            named = set(params) | {a.arg for a in arguments.kwonlyargs}
            bound = {}
            for param, arg in zip(params, node.args):
                if not isinstance(arg, ast.Starred) and self.path(arg) is not None:
                    bound[param] = self.path(arg)
            for keyword in node.keywords:
                if keyword.arg in named and self.path(keyword.value) is not None:
                    bound[keyword.arg] = self.path(keyword.value)
            if bound:
                parsers.run(function, bound, unbound=False)
            return
        if isinstance(func, ast.Attribute):
            base = self.path(func.value)
            if base is None:
                return
            if func.attr == "add_parser" and node.args and self.text(node.args[0]) is not None:
                names = [self.text(node.args[0])]
                for keyword in node.keywords:
                    if keyword.arg == "aliases":
                        names += self.strings(keyword.value)
                self.commands.update(" ".join(base + (n,)) for n in names)
            elif func.attr == "add_argument":
                for arg in node.args:
                    option = self.text(arg)
                    if option is not None and option.startswith("-"):
                        self.options.add(" ".join(base + (option,)))

    def visit_FunctionDef(self, node):
        pass  # run from `walk` (top level) or from a call, never in passing

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        """A class body runs where it stands, in its own scope: its imports
        and names stay in it, an inner class starts again from the scope
        around the outermost class (its methods are not run)."""
        for one in node.decorator_list + node.bases + node.keywords:
            self.visit(one)
        saved = (self.env, self.consts, self.modules, self.imported, self.local, self.outer)
        self.outer = self.outer or saved[:5]
        env, consts, self.modules, self.imported, self.local = self.outer
        self.env, self.consts = dict(env), dict(consts)
        self._enter_class(node)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            (self.env, self.consts, self.modules, self.imported, self.local,
             self.outer) = saved

    def visit_Lambda(self, node):
        """A lambda in a class body sees the scope around the class."""
        if self.outer is None:
            self.generic_visit(node)
            return
        saved = (self.env, self.consts, self.modules, self.imported, self.local, self.outer)
        self.env, self.consts, self.modules, self.imported, self.local = self.outer
        self.outer = None
        try:
            self.generic_visit(node)
        finally:
            (self.env, self.consts, self.modules, self.imported, self.local,
             self.outer) = saved

    def run(self, function, bound: dict[str, tuple[str, ...]], unbound: bool = True):
        saved = (self.env, self.consts, self.unbound, self.modules, self.imported,
                 self.local, self.outer)
        self.env, self.consts, self.unbound, self.outer = dict(bound), {}, unbound, None
        self.modules, self.imported = self.scopes[function.name]
        self.local = self.own[function.name]
        self.package.stack.append((self.module, function.name))
        try:
            for statement in function.body:
                self.visit(statement)
        finally:
            self.package.stack.pop()
            (self.env, self.consts, self.unbound, self.modules, self.imported,
             self.local, self.outer) = saved

    def walk(self):
        for statement in self.tree.body:
            self.visit(statement)
        for function in self.functions.values():
            self.run(function, {},
                     unbound=(self.module, function.name) not in self.package.called)


def surface(ref: str) -> dict[str, set[str]]:
    """The commands (with their aliases), the option strings of each command
    and the settings of the package at `ref`."""
    out: dict[str, set[str]] = {"command": set(), "option": set(), "setting": set()}
    names = [n for n in git("ls-tree", "-r", "--name-only", ref, "--", SOURCES).split()
             if n.endswith(".py")]
    trees = {}
    for name in names:
        text = git("show", "%s:%s" % (ref, name))
        try:
            tree = ast.parse(text, filename=name)
        except SyntaxError as exc:
            raise Error("%s at %s: %s" % (name, ref, exc))
        module = name[len(SOURCES):-3].replace("/", ".")
        init = module == "__init__" or module.endswith(".__init__")
        trees[module[:-len("__init__")].rstrip(".") if init else module] = (tree, init)
        if name == SOURCES + "main.py":
            for one, several in _MAIN_COMMAND.findall(text):
                out["command"].update([one] if one else re.findall(r"""["']([\w-]+)["']""",
                                                                    several))
        out["setting"].update(_SETTING.findall(text))
    package = _Package(trees)
    package.walk()
    out["command"].update(package.commands)
    out["option"].update(package.options)
    return out


def level(base: str, head: str) -> tuple[str, list[str]]:
    """(level, reasons): the floor of the range `base..head`."""
    reasons: dict[str, list[str]] = {k: [] for k in LEVELS}
    for line in git("diff", "--name-status", "--no-renames", base, head,
                    "--", MIGRATIONS).splitlines():
        status, _, name = line.partition("\t")
        if not name.endswith(".sql"):
            continue
        if status != "A":
            reasons["major"].append("migration %s %s" % (
                name.rsplit("/", 1)[-1], "removed" if status == "D" else "changed"))
            continue
        found = _DESTRUCTIVE.search(_ON_COMMIT_DROP.sub(
            " ", _sql(git("show", "%s:%s" % (head, name)))))
        if found:
            reasons["major"].append("migration %s: %s" % (
                name.rsplit("/", 1)[-1], " ".join(found.group(0).lower().split())))
        else:
            reasons["minor"].append("migration %s added" % name.rsplit("/", 1)[-1])
    before, after = surface(base), surface(head)
    gone = before["command"] - after["command"]
    for kind in ("command", "option", "setting"):
        for name in sorted(before[kind] - after[kind]):
            if kind == "option" and name.rpartition(" ")[0] in gone:
                continue  # its command is reported
            reasons["major"].append("%s %s removed" % (kind, name))
    for kind in ("command", "option", "setting"):
        for name in sorted(after[kind] - before[kind]):
            reasons["minor"].append("%s %s added" % (kind, name))
    for body in git("log", "--format=%H %s%n%b%x00", "%s..%s" % (base, head)).split("\0"):
        found = re.search(r"^%s:\s*(.+)$" % TRAILER, body, re.M | re.I)
        if found:
            reasons["major"].append("%s %s: %s" % (body.split()[0][:12], TRAILER,
                                                   found.group(1).strip()))
    if git("diff", "--name-only", base, head, "--", SOURCES).strip():
        reasons["patch"].append("package code changed")
    for one in reversed(LEVELS):
        if reasons[one]:
            return one, reasons[one]
    return "patch", []


def bump_level(old, new) -> str | None:
    """The level of `old` → `new` if `new` is the next number at that level,
    None otherwise."""
    for index, name in ((0, "major"), (1, "minor"), (2, "patch")):
        expected = tuple(old[:index]) + (old[index] + 1,) + (0,) * (2 - index)
        if tuple(new) == expected:
            return name
    return None


def verdict(old, new, floor: str, reasons: list[str]) -> tuple[bool, str]:
    declared = bump_level(old, new)
    why = "; ".join(reasons[:10]) + (" (+%d)" % (len(reasons) - 10) if len(reasons) > 10 else "")
    if declared is None:
        return False, "%s → %s is not the next number at any level (next: %s, %s or %s)" % (
            show(old), show(new), show((old[0], old[1], old[2] + 1)),
            show((old[0], old[1] + 1, 0)), show((old[0] + 1, 0, 0)))
    if LEVELS.index(declared) < LEVELS.index(floor):
        return False, "%s → %s is a %s, the content needs a %s: %s" % (
            show(old), show(new), declared, floor, why)
    if declared != floor:
        return True, "%s → %s is a %s, above the %s found (%s): say why in the release notes" % (
            show(old), show(new), declared, floor, why or "nothing found")
    return True, "%s → %s is a %s, as its content: %s" % (
        show(old), show(new), declared, why or "nothing found")


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("pr", "release", "level"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, args = argv[0], argv[1:]
    try:
        if mode == "release":
            if len(args) != 1:
                print("usage: version-level.py release vX.Y.Z", file=sys.stderr)
                return 2
            head = args[0]
            base = last_tag(head + "^")
        else:
            base = args[0] if args else "HEAD^1"
            head = args[1] if len(args) > 1 else "HEAD"
            if mode == "level":
                floor, reasons = level(base, head)
                print("%s: %s" % (floor, "; ".join(reasons) or "nothing found"))
                return 0
            if version_at(base) == version_at(head):
                print("not a version PR: %s unchanged (%s)" % (
                    VERSION_FILE, show(version_at(head))))
                return 0
            base = last_tag(base)
        floor, reasons = level(base, head)
        ok, message = verdict(version_at(base), version_at(head), floor, reasons)
    except Error as exc:
        print("version-level: %s" % exc, file=sys.stderr)
        return 2
    print("%s (since %s): %s" % ("ok" if ok else "WRONG NUMBER", base, message))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
