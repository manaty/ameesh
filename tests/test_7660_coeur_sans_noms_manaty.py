# SPDX-License-Identifier: AGPL-3.0-only
"""#7660 (#7637 tranche 1): no Manaty name enters the generic core.

The inventory is ``docs/design/7637-inventaire-noms-manaty.md``. This guard
scans executable core code, not tests, not the ``modules/`` packages, and not
notes: Python docstrings and comments, SQL comments and ``COMMENT ON`` bodies,
and JavaScript/HTML comments are excluded. It fails in two directions:

* a role, host, person or project name appears where the exception list does
  not allow it: use the canon, a Host/Agent card, or a module instead;
* an exception is no longer needed: the list only shrinks. Lower the count or
  delete the entry; never raise one to make a new occurrence pass.

Each exception belongs to one tranche: ``#7634`` for ``bureau``, ``#7661`` for
roles, ``#7662`` for hosts and persons, ``#7663`` for the Nexlink module.
``manaty`` has no announced tranche yet; it is recorded as such on purpose.
The scanner is the search method of the inventory: keep both in step.
"""
from __future__ import annotations

import ast
from collections import Counter
from functools import lru_cache
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"

#: The Manaty names of the inventory, by family. Each value is the regex of the
#: name and its inflections; the key is the canonical spelling counted below.
FAMILIES = {
    "role": {
        "mesh-design": r"mesh-designs?",
        "auditeur": r"auditeurs?",
        "orchestrateur": r"orchestrateurs?",
        "bureau": r"bureaux?",
    },
    "host": {
        "ameesh-agents-1": r"ameesh-agents-1",
        "ameesh-agents-2": r"ameesh-agents-2",
        "ameesh-agents-3": r"ameesh-agents-3",
        "manaty-mesh-1": r"manaty-mesh-1",
        "pc-smichea": r"pc-smichea",
        "bureau-goal": r"bureau-goal",
    },
    "person": {
        "smichea": r"smichea",
    },
    "project": {
        "nexlink": r"nexlink",
        "manaty": r"manaty",
    },
}

#: Lot that removes the name from the core. ``None`` means no announced tranche.
TRANCHE = {
    "bureau": "#7634",
    "mesh-design": "#7661",
    "auditeur": "#7661",
    "orchestrateur": "#7661",
    "ameesh-agents-1": "#7662",
    "ameesh-agents-2": "#7662",
    "ameesh-agents-3": "#7662",
    "manaty-mesh-1": "#7662",
    "pc-smichea": "#7662",
    "bureau-goal": "#7662",
    "smichea": "#7662",
    "nexlink": "#7663",
    "manaty": None,
}

#: Historical ceiling of the exception list, per tranche, frozen. A tranche
#: lowers EXCEPTIONS; it never edits this table and never raises a count.
RECORDED_BY_TRANCHE = {
    "#7634": 135,
    "#7661": 54,
    "#7662": 14,
    "#7663": 228,
    None: 9,
}

#: (path relative to src/ameesh, canonical name) -> allowed occurrences.
#: Generated from the inventory; each entry is a debt the owning tranche pays.
EXCEPTIONS = {
    # -- le nom « bureau » sort du cœur (#7634) : 135
    ("alert_briefs.py", "bureau"): 58,
    ("canon.py", "bureau"): 4,
    ("ci_disk.py", "bureau"): 1,
    ("cycle_budget.py", "bureau"): 3,
    ("debit.py", "bureau"): 6,
    ("decisions.py", "bureau"): 6,
    ("exploitation.py", "bureau"): 3,
    ("hostcheck.py", "bureau"): 1,
    ("mesh_cli.py", "bureau"): 1,
    ("migrations/0583_bureau_staging_catchup.sql", "bureau"): 1,
    ("migrations/0584_bureau_catchup_timestamp.sql", "bureau"): 1,
    ("migrations/rollback/0584_restore.sql", "bureau"): 1,
    ("netcheck.py", "bureau"): 1,
    ("notify.py", "bureau"): 5,
    ("notify_gap.py", "bureau"): 2,
    ("packet_guard.py", "bureau"): 1,
    ("planned_waits.py", "bureau"): 6,
    ("release.py", "bureau"): 9,
    ("role.py", "bureau"): 1,
    ("slo.py", "bureau"): 2,
    ("staging_deferred.py", "bureau"): 4,
    ("staging_pending.py", "bureau"): 1,
    ("stagnation.py", "bureau"): 1,
    ("tenancy.py", "bureau"): 13,
    ("train_requalify_review.py", "bureau"): 2,
    ("work.py", "bureau"): 1,
    # -- rôles lus à la fiche du canon (#7661) : 54
    ("alert_briefs.py", "auditeur"): 5,
    ("alert_briefs.py", "mesh-design"): 2,
    ("auditor_pass.py", "auditeur"): 2,
    ("backlog.py", "auditeur"): 3,
    ("backlog.py", "orchestrateur"): 1,
    ("canon.py", "mesh-design"): 1,
    ("context_profile.py", "auditeur"): 1,
    ("context_profile.py", "orchestrateur"): 1,
    ("debit.py", "auditeur"): 1,
    ("deep_analysis.py", "auditeur"): 1,
    ("exploitation.py", "auditeur"): 1,
    ("file_decoupage.py", "auditeur"): 2,
    ("handoff.py", "orchestrateur"): 2,
    ("hostcheck.py", "mesh-design"): 2,
    ("platform/child_environment.py", "auditeur"): 1,
    ("release_cutoff.py", "mesh-design"): 1,
    ("review_designation_activation.py", "mesh-design"): 1,
    ("role_timers.py", "auditeur"): 4,
    ("role_timers.py", "mesh-design"): 1,
    ("runner.py", "auditeur"): 6,
    ("slo.py", "auditeur"): 2,
    ("sous_utilisation.py", "auditeur"): 3,
    ("sous_utilisation.py", "orchestrateur"): 3,
    ("staging_executor.py", "mesh-design"): 2,
    ("undeliverable.py", "orchestrateur"): 1,
    ("unrecorded.py", "orchestrateur"): 1,
    ("work.py", "auditeur"): 2,
    ("work.py", "orchestrateur"): 1,
    # -- hôtes et personnes lus au canon (#7662) : 6
    ("migrations/0364_key_holders.sql", "smichea"): 2,
    ("process_observations_cli.py", "bureau-goal"): 1,
    ("release_cutoff.py", "smichea"): 2,
    ("storage/postgres/authority.py", "smichea"): 1,
    # -- release et canal Nexlink dans le module Nexlink (#7663) : 228
    ("approve/config.py", "nexlink"): 2,
    ("ci.py", "nexlink"): 2,
    ("cli.py", "nexlink"): 9,
    ("cost.py", "nexlink"): 1,
    ("fingerprints.py", "nexlink"): 1,
    ("host_cli.py", "nexlink"): 2,
    ("incident.py", "nexlink"): 2,
    ("migrations.py", "nexlink"): 2,
    ("nexlink_release.py", "nexlink"): 51,
    ("notify.py", "nexlink"): 78,
    ("release.py", "nexlink"): 26,
    ("release_cutoff.py", "nexlink"): 18,
    ("role_timers.py", "nexlink"): 1,
    ("server_faults.py", "nexlink"): 1,
    ("train_assembly.py", "nexlink"): 2,
    ("train_contracts.py", "nexlink"): 28,
    ("work.py", "nexlink"): 2,
    # -- aucune tranche annoncée : organisation Manaty, à décider : 9
    ("alert_briefs.py", "manaty"): 2,
    ("incident.py", "manaty"): 2,
    ("migrations.py", "manaty"): 3,
    ("packet_guard.py", "manaty"): 2,
}

_FILE_SUFFIXES = (".py", ".sql", ".html", ".js", ".cjs")
_ORDER = sorted(
    ((name, family, pattern) for family, names in FAMILIES.items()
     for name, pattern in names.items()),
    key=lambda item: -len(item[0]),
)
_PATTERNS = [(name, family,
              re.compile(r"(?i)(?<![a-z0-9])" + fragment + r"(?![a-z0-9])"))
             for name, family, fragment in _ORDER]


def _docstrings(tree: ast.AST) -> set:
    """Ids of actual docstrings, never executable multiline constants."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(id(first.value))
    return found


def _find(text: str) -> list:
    """Matches as ``(canonical name, family, actual word, offset)``.

    Longer names are masked first: ``pc-smichea`` is one host, not a host plus
    the person ``smichea``; ``bureau-goal`` is a host, not the role ``bureau``.
    """
    masked = text
    found = []
    for name, family, pattern in _PATTERNS:
        for match in pattern.finditer(masked):
            found.append((name, family, match.group(0), match.start()))
            masked = (masked[:match.start()] + "#" * len(match.group(0))
                      + masked[match.end():])
    return found


def _strip_sql_comments(text: str) -> str:
    """Blank SQL comments, keeping every newline and the string literals."""
    out = []
    index = 0
    quoted = False
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    out.append(text[index + 1])
                    index += 2
                    continue
                quoted = False
            index += 1
            continue
        if char == "'":
            quoted = True
            out.append(char)
            index += 1
            continue
        if text.startswith("--", index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            out.append(" " * (end - index))
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) - 2 if end < 0 else end
            chunk = text[index:end + 2]
            out.append("".join("\n" if item == "\n" else " " for item in chunk))
            index = end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_comment_on(text: str) -> str:
    """Blank ``COMMENT ON … IS '…';`` statements: database notes, not code."""
    out = list(text)
    pattern = re.compile(r"\bcomment\s+on\b", re.I)
    start = 0
    while True:
        match = pattern.search(text, start)
        if not match:
            return "".join(out)
        # Only a statement of its own, never the words inside a literal.
        prefix = text[:match.start()].rstrip()
        if prefix and not prefix.endswith(";"):
            start = match.end()
            continue
        index = match.end()
        quoted = False
        while index < len(text):
            char = text[index]
            if quoted:
                if char == "'":
                    if index + 1 < len(text) and text[index + 1] == "'":
                        index += 2
                        continue
                    quoted = False
            elif char == "'":
                quoted = True
            elif char == ";":
                break
            index += 1
        for position in range(match.start(), min(index + 1, len(text))):
            if out[position] != "\n":
                out[position] = " "
        start = index + 1


def _strip_js_comments(text: str) -> str:
    """Blank JavaScript comments, keeping strings and template literals."""
    out = []
    index = 0
    quote = None
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'", "`"):
            quote = char
            out.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            out.append(" " * (end - index))
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) - 2 if end < 0 else end
            chunk = text[index:end + 2]
            out.append("".join("\n" if item == "\n" else " " for item in chunk))
            index = end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_html_comments(text: str) -> str:
    out = []
    index = 0
    while index < len(text):
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            end = len(text) - 3 if end < 0 else end
            chunk = text[index:end + 3]
            out.append("".join("\n" if item == "\n" else " " for item in chunk))
            index = end + 3
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _strip_script_comments(text: str) -> str:
    """JS comments inside ``<script>`` blocks only, not in HTML attributes."""
    def blank(match):
        return (match.group(1) + _strip_js_comments(match.group(2))
                + match.group(3))
    return re.sub(r"(?is)(<script[^>]*>)(.*?)(</script>)", blank, text)


def _python_matches(source: str) -> list:
    """``(line, name, family, actual word, kind)`` in executable Python."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    rows = []
    for node in ast.walk(tree):
        text = None
        kind = "identifier"
        base_line = getattr(node, "lineno", 0)
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            text = node.value
            kind = "message" if re.search(r"\s", text.strip()) else "constant"
            if "\n" in text and re.search(
                    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b",
                    text, re.I):
                text = _strip_sql_comments(text)
        elif isinstance(node, (ast.Name, ast.Attribute, ast.arg)):
            text = (node.id if isinstance(node, ast.Name)
                    else node.attr if isinstance(node, ast.Attribute)
                    else node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            text = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            text = node.name
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for alias in node.names:
                names.extend(name for name in (alias.name, alias.asname) if name)
            for name in names:
                for canonical, family, actual, _ in _find(name):
                    rows.append((base_line, canonical, family, actual, "import"))
        if text is None:
            continue
        for canonical, family, actual, offset in _find(text):
            line = base_line + (text[:offset].count("\n")
                                if isinstance(text, str) else 0)
            rows.append((line, canonical, family, actual, kind))
    return sorted(rows)


def _sql_matches(source: str) -> list:
    """``(line, name, family, actual word, kind)`` in executable SQL."""
    text = _strip_comment_on(_strip_sql_comments(source))
    rows = []
    for line_number, line_text in enumerate(text.splitlines(), 1):
        for canonical, family, actual, _ in _find(line_text):
            rows.append((line_number, canonical, family, actual, "sql"))
    return rows


def _script_matches(source: str, html: bool = False) -> list:
    """``(line, name, family, actual word, kind)`` in JS/HTML code."""
    if html:
        text = _strip_script_comments(_strip_html_comments(source))
    else:
        text = _strip_js_comments(source)
    rows = []
    for line_number, line_text in enumerate(text.splitlines(), 1):
        for canonical, family, actual, _ in _find(line_text):
            rows.append((line_number, canonical, family, actual, "script"))
    return rows


def _rows(relative: str, matches: list) -> list:
    return [(relative, line, name, family, actual, kind)
            for line, name, family, actual, kind in matches]


def _python_occurrences(path: Path) -> list:
    return _rows(path.relative_to(SOURCE).as_posix(),
                 _python_matches(path.read_text()))


def _sql_occurrences(path: Path) -> list:
    return _rows(path.relative_to(SOURCE).as_posix(),
                 _sql_matches(path.read_text()))


def _script_occurrences(path: Path, html: bool = False) -> list:
    return _rows(path.relative_to(SOURCE).as_posix(),
                 _script_matches(path.read_text(), html=html))


@lru_cache(maxsize=1)
def occurrences() -> list:
    """Every executable Manaty name of the core, with its file and line."""
    rows = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file() or path.suffix not in _FILE_SUFFIXES:
            continue
        if path.relative_to(SOURCE).parts[0] == "modules":
            continue
        if path.suffix == ".py":
            rows.extend(_python_occurrences(path))
        elif path.suffix == ".sql":
            rows.extend(_sql_occurrences(path))
        elif path.suffix == ".html":
            rows.extend(_script_occurrences(path, html=True))
        else:
            rows.extend(_script_occurrences(path))
    return rows


@lru_cache(maxsize=1)
def actual() -> Counter:
    return Counter((row[0], row[2]) for row in occurrences())


def allowed() -> Counter:
    return Counter(EXCEPTIONS)


def _detail(rows) -> str:
    return "\n".join("  %s:%d: %r (%s)" % (path, line, actual, family)
                     for path, line, _, family, actual, _ in rows)


class CoreWithoutManatyNamesTest(unittest.TestCase):
    def test_no_new_manaty_name_enters_the_core(self):
        new = actual() - allowed()
        details = [row for row in occurrences() if (row[0], row[2]) in new]
        self.assertFalse(new,
                         "New Manaty name(s) in the core; read the name from the "
                         "canon/a Host or Agent card, or move the code to a "
                         "module:\n%s" % _detail(details))

    def test_exception_list_only_shrinks(self):
        stale = allowed() - actual()
        self.assertFalse(stale,
                         "Obsolete exception(s): lower the count or delete the "
                         "entry, never raise it:\n%s"
                         % "\n".join("  %s: %s (%d allowed, %d found)"
                                     % (path, name, allowed()[(path, name)],
                                        actual()[(path, name)])
                                     for path, name in sorted(stale)))

    def test_exceptions_are_counted_and_owned_by_a_tranche(self):
        for (path, name), count in EXCEPTIONS.items():
            with self.subTest(path=path, name=name):
                self.assertGreater(count, 0)
                self.assertTrue((SOURCE / path).is_file(), path)
                self.assertIn(name, TRANCHE)
        total = sum(EXCEPTIONS.values())
        self.assertLessEqual(total, sum(RECORDED_BY_TRANCHE.values()),
                             "the exception list grew; it may only shrink")
        per_tranche = Counter()
        for (_, name), count in EXCEPTIONS.items():
            per_tranche[TRANCHE[name]] += count
        self.assertLessEqual(per_tranche, Counter(RECORDED_BY_TRANCHE),
                             "a tranche has more exceptions than recorded")

    def test_scanner_reads_code_and_skips_notes(self):
        self.assertEqual(_find('name = "bureau"'),
                         [("bureau", "role", "bureau", 8)])
        self.assertEqual(_find("pc-smichea"),
                         [("pc-smichea", "host", "pc-smichea", 0)])
        self.assertEqual(_find("manaty-mesh-1"),
                         [("manaty-mesh-1", "host", "manaty-mesh-1", 0)])
        self.assertEqual(_find("bureau-goal"),
                         [("bureau-goal", "host", "bureau-goal", 0)])
        self.assertEqual(_find("auditeurs")[0][0], "auditeur")
        self.assertEqual(_find("bureaux")[0][0], "bureau")
        python = ('"""bureau mesh-design"""\n'
                  "# auditeur\n"
                  'value = "mesh-design"\n'
                  "from . import nexlink_release\n")
        self.assertEqual(_python_matches(python), [
            (3, "mesh-design", "role", "mesh-design", "constant"),
            (4, "nexlink", "project", "nexlink", "import"),
        ])
        sql = ("-- bureau\n"
               "select 'bureau'; -- auditeur\n"
               "comment on table t is 'nexlink';\n"
               "select 'mesh-design' where x = 'bureau';\n")
        self.assertEqual(_sql_matches(sql), [
            (2, "bureau", "role", "bureau", "sql"),
            (4, "mesh-design", "role", "mesh-design", "sql"),
            (4, "bureau", "role", "bureau", "sql"),
        ])
        self.assertEqual(_sql_matches("select 'comment on x is y; bureau';"),
                         [(1, "bureau", "role", "bureau", "sql")])
        js = '// bureau\nvar x = "mesh-design"; /* nexlink */\n'
        self.assertEqual(_script_matches(js), [
            (2, "mesh-design", "role", "mesh-design", "script"),
        ])
        html = ('<!-- bureau -->\n'
                '<a href="https://x">mesh-design</a>\n'
                '<script>// smichea\nvar h = "bureau";</script>\n')
        self.assertEqual(_script_matches(html, html=True), [
            (2, "mesh-design", "role", "mesh-design", "script"),
            (4, "bureau", "role", "bureau", "script"),
        ])

    def test_inventory_is_exhaustive_for_the_declared_lexicon(self):
        self.assertEqual(set(TRANCHE),
                         set().union(*(set(names) for names in FAMILIES.values())))
        self.assertEqual(set(RECORDED_BY_TRANCHE),
                         {"#7634", "#7661", "#7662", "#7663", None})
        self.assertEqual(sum(RECORDED_BY_TRANCHE.values()), 440)


if __name__ == "__main__":
    unittest.main()
