# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic, narrowly scoped public release roadmap entries."""
from __future__ import annotations

import ast
import re
import unicodedata

BEGIN = '<!-- generated releases: begin -->'
END = '<!-- generated releases: end -->'
ROADMAP = 'site/docs/roadmap.md'
# The grammar is also the contract for accepting a cutoff-only diff.
LINE = re.compile(r'^- \*\*v[0-9]+\.[0-9]+\.[0-9]+\*\* \([0-9]{4}-[0-9]{2}-[0-9]{2}\): (?:[+A-Za-z0-9][A-Za-z0-9 ,.+;:/_-]{0,499})\.$')
SUMMARY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ,.+;:/_-]{0,119}$')


def _words(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(re.findall(r'[^\W_]+', value, re.UNICODE))


def safe_summary(value: str, project_names: set[str] | None) -> str | None:
    """Fail closed if names cannot be read; match complete normalized words."""
    if project_names is None or not SUMMARY.fullmatch(value):
        return None
    normalized = ' ' + _words(value) + ' '
    for name in project_names:
        needle = _words(name)
        if needle and ' ' + needle + ' ' in normalized:
            return None
    return value


def render(version: str, date: str, migrations: list[str], summaries: list[str],
           internal: int, commands: list[str] = ()) -> str:
    facts = []
    numbers = sorted({m[:4] for m in migrations if re.match(r'^\d{4}_.+\.sql$', m)})
    if numbers:
        facts.append('Migrations ' + ', '.join(numbers))
    if commands:
        facts.append('Commands ' + ', '.join(commands))
    facts.extend(summaries)
    if internal:
        facts.append('+' + str(internal) + ' internal lots')
    line = '- **v%s** (%s): %s.' % (version, date, '; '.join(facts) or 'Maintenance')
    if not LINE.fullmatch(line):
        # Bound the public line without blocking a cut on an oversized batch.
        return '- **v%s** (%s): +%d internal lots.' % (version, date, internal + len(summaries))
    return line


def insert(page: str, line: str) -> str:
    if not LINE.fullmatch(line) or page.count(BEGIN) != 1 or page.count(END) != 1:
        raise ValueError('invalid generated roadmap block')
    start = page.index(BEGIN) + len(BEGIN)
    end = page.index(END)
    if start >= end:
        raise ValueError('invalid generated roadmap block order')
    block = page[start:end]
    lines = [entry for entry in block.splitlines() if entry]
    if any(not LINE.fullmatch(entry) for entry in lines):
        raise ValueError('invalid existing roadmap entry')
    prefix = line.split('**', 2)[1]
    same = [entry for entry in lines if entry.split('**', 2)[1] == prefix]
    if same:
        if same != [line]:
            raise ValueError('version already has another roadmap entry')
        return page
    return page[:end] + line + '\n' + page[end:]


def valid_page(page: str) -> bool:
    if page.count(BEGIN) != 1 or page.count(END) != 1:
        return False
    start, end = page.index(BEGIN) + len(BEGIN), page.index(END)
    return start < end and all(not line or LINE.fullmatch(line)
                               for line in page[start:end].splitlines())


def valid_patch(patch: str) -> bool:
    added, removed = [], []
    lines = patch.splitlines()
    headers = [i for i, line in enumerate(lines) if line.startswith('@@ ')]
    if not headers:
        return False
    for line in lines[headers[0]:]:
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return not removed and len(added) == 1 and bool(LINE.fullmatch(added[0]))


def parser_commands(source: str) -> set[str]:
    """Names explicitly declared with argparse add_parser, without executing code."""
    tree = ast.parse(source)
    result = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'add_parser' and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and re.fullmatch(r'[a-z][a-z0-9-]*', node.args[0].value)):
            result.add(node.args[0].value)
    return result
