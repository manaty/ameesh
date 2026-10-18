# SPDX-License-Identifier: AGPL-3.0-only
"""L97: ratchet development vocabulary out of the generic core (#114).

The baseline records occurrences per file and motif, never a file-wide
exemption. New occurrences fail even in a file with legacy coupling. Removing
an occurrence also fails until its allowance is reduced. Comments and Python
docstrings are explanations rather than executable vocabulary and are skipped.
Module code is outside this boundary; git used as infrastructure stays visible
in the baseline with its own reason. This lexical check complements review;
it cannot establish that an unchanged count still represents the same concept.
"""
from __future__ import annotations

import ast
from collections import Counter
import importlib.util
from pathlib import Path
import re
import unittest

from . import support  # isolate the runner's environment before product imports
from ameesh import work, work_lifecycle

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"
BASELINE = Path(__file__).with_name("generic_core_exceptions.txt")
PATTERNS = {
    "worktree": re.compile(r"worktree", re.I),
    "pr_ref": re.compile(r"pr_ref", re.I),
    "sha": re.compile(r"(?<![a-z])sha(?![a-z])", re.I),
    "git-merge": re.compile(r"git-merge"),
    "gh": re.compile(r"(?<![a-z])gh(?![a-z])", re.I),
    "github": re.compile(r"github", re.I),
    "npm": re.compile(r"(?<![a-z])npm(?![a-z])", re.I),
    "pnpm": re.compile(r"pnpm", re.I),
    "yarn": re.compile(r"yarn", re.I),
    "docker": re.compile(r"docker", re.I),
    "podman": re.compile(r"podman", re.I),
    "review-policy": re.compile(r"review_polic", re.I),
    "PR": re.compile(r"\bPR\b"),
    "gel": re.compile(r"\bgel\b", re.I),
    "fusion": re.compile(r"fusion", re.I),
}
LEGACY_STATES = {"intake", "build", "qa", "merged", "promoted"}
MODULE_PACKAGES = {p.stem for p in (SOURCE / "modules").iterdir()
                   if (p.is_dir() and p.name != "__pycache__")
                   or (p.suffix == ".py" and p.stem != "__init__")}


def motifs(text: str) -> Counter:
    return Counter({name: len(pattern.findall(text))
                    for name, pattern in PATTERNS.items() if pattern.search(text)})


def scan_python(source: str, filename: str = "reader.py") -> Counter:
    tree = ast.parse(source, filename=filename)
    # Skip only actual docstrings, never executable multiline constants.
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                       ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)
                  and isinstance(node.body[0].value.value, str)}
    package = "ameesh" + ("." + ".".join(Path(filename).parent.parts)
                          if str(Path(filename).parent) != "." else "")
    counts = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                counts.update(motifs(node.value))
                if node.value in LEGACY_STATES:
                    counts["state:" + node.value] += 1
        elif isinstance(node, (ast.Name, ast.Attribute, ast.arg)):
            counts.update(motifs(node.id if isinstance(node, ast.Name)
                                 else node.attr if isinstance(node, ast.Attribute)
                                 else node.arg))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts.update(motifs(node.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                counts.update(motifs(alias.name))
                if alias.asname:
                    counts.update(motifs(alias.asname))
                if alias.name.startswith("ameesh.modules."):
                    counts["module-import"] += 1
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""),
                                                 package) if node.level else node.module or ""
            for alias in node.names:
                counts.update(motifs(alias.name))
                if alias.asname:
                    counts.update(motifs(alias.asname))
                if module.startswith("ameesh.modules.") or (
                        module == "ameesh.modules" and alias.name in MODULE_PACKAGES):
                    counts["module-import"] += 1
    return counts


def scan_template(source: str) -> Counter:
    counts = motifs(source)
    for _, state in re.findall(r"""(["'])(intake|build|qa|merged|promoted)\1""", source):
        counts["state:" + state] += 1
    return counts


def scan_core() -> Counter:
    counts = Counter()
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE)
        if relative.parts[0] == "modules":
            continue
        for motif, count in scan_python(path.read_text(), relative.as_posix()).items():
            counts[relative.as_posix(), motif] = count
    # The template also contains script and style vocabulary; count all of it.
    path = SOURCE / "progress_page.html"
    counts.update({(path.name, motif): count for motif, count in scan_template(path.read_text()).items()})
    return counts


def read_baseline(text: str) -> Counter:
    counts = Counter()
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        filename, motif, count, reason = line.split(" : ", 3)
        key = filename, motif
        if key in counts or not reason.strip() or int(count) <= 0:
            raise ValueError("duplicate or invalid generic-core allowance: " + line)
        counts[key] = int(count)
    return counts


class GenericCoreTest(unittest.TestCase):
    def test_legacy_coupling_does_not_grow(self):
        actual = scan_core()
        expected = read_baseline(BASELINE.read_text())
        added = actual - expected
        removed = expected - actual
        self.assertFalse(added, "New core coupling; use a module or generic hook: %s" % added)
        self.assertFalse(removed, "Reduce obsolete generic-core allowances: %s" % removed)

    def test_scanner_distinguishes_docs_from_executable_vocabulary(self):
        counts = scan_python('"""github worktree"""\n# pnpm\n'
                             'value = "github"\nworktree_root = "qa"\n')
        self.assertEqual(counts, {"github": 1, "worktree": 1, "state:qa": 1})
        # A new occurrence in an already excepted file is still detected.
        self.assertEqual(scan_python('x = "github"\ny = "github"\n') - counts,
                         {"github": 1})

    def test_direct_module_imports_include_relative_and_aliased_forms(self):
        for source, filename in (
                ("import ameesh.modules.it as proof", "reader.py"),
                ("from ameesh.modules.it import descriptor", "reader.py"),
                ("from .modules import it as proof", "reader.py"),
                ("from .modules.storage import ModuleStore", "reader.py"),
                ("from ..modules.it import descriptor", "nested/reader.py")):
            with self.subTest(source=source):
                self.assertEqual(scan_python(source, filename)["module-import"], 1)
        self.assertEqual(scan_python("from . import modules")["module-import"], 0)
        self.assertEqual(scan_python("from .modules import installed")["module-import"], 0)

    def test_template_includes_javascript_state_literals(self):
        self.assertEqual(scan_template("const state = 'qa'; const merged = true;"),
                         {"state:qa": 1})

    def test_baseline_rejects_duplicate_and_empty_allowances(self):
        for text in ("reader.py : gh : 0 : legacy", "reader.py : gh : 1 : ",
                     "reader.py : gh : 1 : legacy\nreader.py : gh : 1 : legacy"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_baseline(text)

    def test_legacy_terminal_and_completed_semantics_remain_distinct(self):
        self.assertEqual(work.STATES, ("intake", "build", "qa", "merged", "promoted",
                                       "blocked", "waiting_human", "closed"))
        self.assertEqual(work.TERMINAL, ("promoted", "closed"))
        self.assertEqual(work.COMPLETED, ("merged", "promoted", "closed"))
        self.assertEqual(work.DELIVERED, work.MERGED_STATES)
        self.assertEqual(work.ACTIVE, ("intake", "build"))
        self.assertEqual(work.WAITING, {"blocked", "waiting_human"})
        self.assertEqual(work.TO_VALIDATE, "qa")
        self.assertEqual(work.delivery_connector(), "git-merge")
        for state in work.TERMINAL:
            self.assertFalse(work.TRANSITIONS[state])
        self.assertIn(work_lifecycle.PROMOTED, work.TRANSITIONS[work_lifecycle.DELIVERY_RECORDED])
