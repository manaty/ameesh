# SPDX-License-Identifier: AGPL-3.0-only
"""L97: report lexical coupling and enforce structural module boundaries (#114).

Vocabulary counts are diagnostic only: concurrent core work may change them.
Direct imports of module implementations remain a blocking architectural check.
Comments and Python docstrings are excluded from the lexical report.
"""
from __future__ import annotations

import ast
from collections import Counter
import importlib.util
from pathlib import Path
import re
import sys
import unittest

from . import support  # isolate the runner's environment before product imports
from ameesh import work, work_lifecycle
from ameesh.modules import registry

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"
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
#: L98 (#4308): the registry is the core's interface to modules, not one of them.
INTERFACES = {"registry"}
MODULE_PACKAGES = {p.stem for p in (SOURCE / "modules").iterdir()
                   if ((p.is_dir() and p.name != "__pycache__")
                       or (p.suffix == ".py" and p.stem != "__init__"))
                   and p.stem not in INTERFACES}


def _module_implementation(module: str) -> bool:
    if not module.startswith("ameesh.modules."):
        return False
    return module.split(".")[2] not in INTERFACES


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
                if _module_implementation(alias.name):
                    counts["module-import"] += 1
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""),
                                                 package) if node.level else node.module or ""
            for alias in node.names:
                counts.update(motifs(alias.name))
                if alias.asname:
                    counts.update(motifs(alias.asname))
                if _module_implementation(module) or (
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


def report() -> None:
    """Print current file/motif counts without a lexical pass/fail threshold."""
    for (filename, motif), count in sorted(scan_core().items()):
        print(f"{filename} : {motif} : {count}")


class GenericCoreTest(unittest.TestCase):
    def test_core_does_not_import_module_implementations(self):
        # L98 T3 (#4406): the legacy adapters read through the registry.
        # L100 T1/T2/T4: temporary identity aliases, removed by L104.
        exceptions = Counter({(name + ".py", "module-import"): 1
                              for name in ("plan_github", "plan_git", "merge_pr",
                                           "connectors/git_merge", "review",
                                           "lot_class", "review_target")})
        actual = Counter({key: count for key, count in scan_core().items()
                          if key[1] == "module-import"})
        self.assertFalse(actual - exceptions,
                         "New core module imports; use the registry: %s" % (actual - exceptions))
        self.assertFalse(exceptions - actual,
                         "Remove obsolete module-import exceptions: %s" % (exceptions - actual))

    def test_scanner_distinguishes_docs_from_executable_vocabulary(self):
        counts = scan_python('"""github worktree"""\n# pnpm\n'
                             'value = "github"\nworktree_root = "qa"\n')
        self.assertEqual(counts, {"github": 1, "worktree": 1, "state:qa": 1})
        # Additional executable occurrences remain visible in the report.
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
        # L98: the registry is the sanctioned way in, in every import form.
        for source in ("from .modules import registry",
                       "from .modules.registry import discover",
                       "import ameesh.modules.registry"):
            with self.subTest(source=source):
                self.assertEqual(scan_python(source)["module-import"], 0)

    def test_template_includes_javascript_state_literals(self):
        self.assertEqual(scan_template("const state = 'qa'; const merged = true;"),
                         {"state:qa": 1})

    def test_the_workspaces_facade_is_read_through_the_registry(self):
        """L101 T4: the core asks the registry for `dev`, never imports it."""
        source = (SOURCE / "workspaces.py").read_text()
        self.assertIn('registry.get("dev")', source)
        self.assertEqual(scan_python(source, "workspaces.py")["module-import"], 0)

    def test_delivery_core_keeps_generic_state_groups(self):
        self.assertEqual(work.STATES, ("intake", "build", "qa", "validated", "delivered",
                                       "blocked", "waiting_human", "closed"))
        self.assertEqual(work_lifecycle.canonical("merged"), "delivered")
        self.assertEqual(work_lifecycle.canonical("promoted"), "delivered")
        self.assertEqual(work.DELIVERED, work.MERGED_STATES)
        self.assertEqual(work.ACTIVE, ("intake", "build"))
        self.assertEqual(work.WAITING, {"blocked", "waiting_human"})
        self.assertEqual(work.TO_VALIDATE, ("qa", "validated"))

    @support.needs_builtin_modules
    def test_delivery_core_preserves_development_stage_transitions(self):
        self.assertEqual(registry.merge_connector(), "git-merge")
        self.assertFalse(work.TRANSITIONS["promoted"])
        self.assertIn("promoted", work.TRANSITIONS["merged"])
        self.assertEqual(registry.development_workflow().STAGES,
                         {"merged": "delivered", "promoted": "delivered"})


if __name__ == "__main__":
    if sys.argv[1:] == ["--report"]:
        report()
    else:
        unittest.main()
