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
  default or function to recreate it is not a break); a command or command
  alias removed, an option string removed from a command (each command has
  its own: removing `show --json` while `list --json` stays is a break), an
  `AMEESH_*` setting no longer read; a commit with the trailer
  `ameesh-breaking: <why>` (an agent contract changed: no script sees that);
- minor: a new migration; a new command, alias or option of a command; a
  new `AMEESH_*` setting read by the package;
- patch: anything else.

The CLI is read from the syntax tree of each module (`add_parser` with its
`aliases`, every option string of `add_argument`), not from the text:
reordering the aliases of an option changes nothing.

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


_SQL_TOKENS = re.compile(
    r"(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?P<dollar>\$(?P<tag>[A-Za-z_]\w*)?\$.*?\$(?P=tag)?\$)"
    r"|(?P<string>'(?:[^']|'')*')"
    r"|(?P<ident>\"(?:[^\"]|\"\")*\")", re.S)


def _sql(text: str) -> str:
    """The SQL without what is not a statement: comments, string literals and
    dollar-quoted bodies (`DEFAULT 'rename'`, a function's `DELETE FROM`),
    quoted identifiers kept as a neutral `"q"` (a column named "rename")."""
    def neutral(found):
        return '"q"' if found.group("ident") else (" " if found.group("comment") else "''")
    return _SQL_TOKENS.sub(neutral, text)


def _strings(node) -> list[str]:
    """The string literals of a list or tuple node (nothing else)."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


class _Parsers(ast.NodeVisitor):
    """The argparse tree of one file, statement after statement: a variable
    bound to `add_parser("x")` is the command path `… x`; `add_subparsers()`
    and argument groups keep the path of their parser; a local function
    called with a parser runs with its parameter bound to that path. A
    variable the file does not bind (a parameter) is the top level. Every
    option string of `add_argument` is kept with its command, so `show
    --json` and `list --json` are two entries."""

    def __init__(self, tree: ast.Module, module: str = "?"):
        self.module = module
        self.commands: set[str] = set()
        self.options: set[str] = set()
        self.functions = {n.name: n for n in tree.body
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.env: dict[str, tuple[str, ...]] = {}
        self.stack: list[str] = []
        #: local functions called within the file: their callers say which
        #: parser they fill, so they are never also read as filling the top
        #: level (inlining such a helper changes nothing)
        self.called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Name) and n.func.id in self.functions}
        #: may an unbound name (a parameter) stand for the top level?
        self.unbound = True

    def path(self, node) -> tuple[str, ...] | None:
        """The command path of a parser expression: a known variable, a call
        that builds a parser, else the top level (an unknown name or
        attribute); None for anything else."""
        if isinstance(node, ast.Name):
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
            if keyword.arg == "prog" and isinstance(keyword.value, ast.Constant) \
                    and isinstance(keyword.value.value, str):
                words = keyword.value.value.split()
                return tuple(words[1:] if words and words[0] in PROGRAMS else words)
        return ("%s:%s" % (self.module, self.stack[-1] if self.stack else "<module>"),)

    def parser_of(self, node) -> tuple[str, ...] | None:
        """The path a call returns when it builds a parser, None otherwise."""
        if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "ArgumentParser"
                or isinstance(node.func, ast.Attribute) and node.func.attr == "ArgumentParser"):
            return self.own_parser(node)
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return None
        base = self.path(node.func.value)
        if base is None:
            return None
        if node.func.attr == "add_parser" and node.args and isinstance(
                node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return base + (node.args[0].value,)
        if node.func.attr in ("add_subparsers", "add_argument_group",
                              "add_mutually_exclusive_group"):
            return base
        return None

    def visit_Assign(self, node):
        self.visit(node.value)
        built = self.parser_of(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if built is None:
                    self.env.pop(target.id, None)
                else:
                    self.env[target.id] = built

    def visit_Call(self, node):
        self.generic_visit(node)
        func = node.func
        if isinstance(func, ast.Attribute):
            base = self.path(func.value)
            if base is None:
                return
            if func.attr == "add_parser" and node.args and isinstance(
                    node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
                for keyword in node.keywords:
                    if keyword.arg == "aliases":
                        names += _strings(keyword.value)
                self.commands.update(" ".join(base + (n,)) for n in names)
            elif func.attr == "add_argument":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and arg.value.startswith("-"):
                        self.options.add(" ".join(base + (arg.value,)))
        elif isinstance(func, ast.Name) and func.id in self.functions \
                and func.id not in self.stack:
            bound = {}
            params = self.functions[func.id].args.args
            for param, arg in zip(params, node.args):
                if isinstance(arg, ast.Name) and arg.id in self.env:
                    bound[param.arg] = self.env[arg.id]
            if bound:
                self.run(self.functions[func.id], bound, unbound=False)

    def visit_FunctionDef(self, node):
        pass  # run from `walk` (top level) or from a call, never in passing

    visit_AsyncFunctionDef = visit_FunctionDef

    def run(self, function, bound: dict[str, tuple[str, ...]], unbound: bool = True):
        saved = self.env, self.unbound
        self.env, self.unbound = dict(bound), unbound
        self.stack.append(function.name)
        for statement in function.body:
            self.visit(statement)
        self.stack.pop()
        self.env, self.unbound = saved

    def walk(self, tree: ast.Module):
        for statement in tree.body:
            self.visit(statement)
        for function in self.functions.values():
            self.run(function, {}, unbound=function.name not in self.called)


def surface(ref: str) -> dict[str, set[str]]:
    """The commands (with their aliases), the option strings of each command
    and the settings of the package at `ref`."""
    out: dict[str, set[str]] = {"command": set(), "option": set(), "setting": set()}
    names = [n for n in git("ls-tree", "-r", "--name-only", ref, "--", SOURCES).split()
             if n.endswith(".py")]
    for name in names:
        text = git("show", "%s:%s" % (ref, name))
        try:
            tree = ast.parse(text, filename=name)
        except SyntaxError as exc:
            raise Error("%s at %s: %s" % (name, ref, exc))
        parsers = _Parsers(tree, name[len(SOURCES):-3].replace("/", "."))
        parsers.walk(tree)
        out["command"].update(parsers.commands)
        out["option"].update(parsers.options)
        if name == SOURCES + "main.py":
            for one, several in _MAIN_COMMAND.findall(text):
                out["command"].update([one] if one else re.findall(r"""["']([\w-]+)["']""",
                                                                    several))
        out["setting"].update(_SETTING.findall(text))
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
        found = _DESTRUCTIVE.search(_sql(git("show", "%s:%s" % (head, name))))
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
