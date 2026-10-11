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
  removed; a new migration that drops a table, column, schema or type,
  renames, changes a column type, adds `NOT NULL`, truncates or deletes rows;
  a command or a long option removed from the CLI; a commit with the
  trailer `ameesh-breaking: <why>` (an agent contract changed: no script
  sees that);
- minor: a new migration; a new command or long option; a new `AMEESH_*`
  setting read by the package;
- patch: anything else.

The number must be the next one at its own level (1.9.0 → 1.9.1, 1.10.0 or
2.0.0), and that level must not be below the floor. A level above the floor
passes, with a note: a new function without a new command is minor, and no
script sees it; the auditor reads the notes. Exit code 0 when it matches (or
not a version PR), 1 when it does not, 2 on a usage or git error.
"""
from __future__ import annotations

import re
import subprocess
import sys

VERSION_FILE = "pyproject.toml"
MIGRATIONS = "src/ameesh/migrations/"
SOURCES = "src/ameesh/"
LEVELS = ("patch", "minor", "major")
TRAILER = "ameesh-breaking"
#: the repository (None: the current directory); the tests point it elsewhere
REPO = None
_VERSION = re.compile(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)[^"]*"\s*$', re.M)
_DESTRUCTIVE = re.compile(
    r"\b(drop\s+(table|column|schema|type)|rename\b|alter\s+column\s+\w+\s+"
    r"(set\s+data\s+)?type\b|set\s+not\s+null|truncate|delete\s+from)\b", re.I)
_COMMAND = re.compile(r"""add_parser\(\s*["']([\w-]+)["']""")
_MAIN_COMMAND = re.compile(r"""command\s*(?:==\s*["']([\w-]+)["']|in\s*\(([^)]*)\))""")
_OPTION = re.compile(r"""add_argument\(\s*(?:["']-\w["']\s*,\s*)?["'](--[\w-]+)["']""")
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


def _sql(text: str) -> str:
    """The SQL without its comments."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def surface(ref: str) -> dict[str, set[str]]:
    """The commands, long options and settings of the package at `ref`."""
    out: dict[str, set[str]] = {"command": set(), "option": set(), "setting": set()}
    names = [n for n in git("ls-tree", "-r", "--name-only", ref, "--", SOURCES).split()
             if n.endswith(".py")]
    for name in names:
        text = git("show", "%s:%s" % (ref, name))
        out["command"].update(_COMMAND.findall(text))
        if name == SOURCES + "main.py":
            for one, several in _MAIN_COMMAND.findall(text):
                out["command"].update([one] if one else re.findall(r"""["']([\w-]+)["']""",
                                                                    several))
        out["option"].update(_OPTION.findall(text))
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
    for kind in ("command", "option"):
        for name in sorted(before[kind] - after[kind]):
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
