#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Test plan from the measured test → code map (L161f).

    scripts/test-plan.py plan --out plan.json [--map carte.json.gz] [--event EVENT]
                              [--base SHA] [--ref REF]
                              [--base-ref B] [--head-ref H]
                              [--github-output F] [--summary F] [--annotations]
    scripts/test-plan.py leaks plan.json FLUX.jsonl [FLUX.jsonl…] [--out leaks.json]
                               [--summary F]
    scripts/test-plan.py tally leaks.json [leaks.json…]
    scripts/test-plan.py couverture [--repo OWNER/REPO] [--ref REFS] [--sha SHA]
                                    [--api URL] [--github-output F] [--summary F]

`plan` runs once per CI run (job `plan` of .github/workflows/tests.yml) and
decides, for every test module, its class and the parts it runs in; every
part then reads the same `plan.json` (`scripts/test-parts.py run --plan`),
so the split never differs between parts.

**Classes** (docs/design/etudes/l161-tests-par-impact.md, section 2):

* P0 — the test module itself changed, or it is unknown to the map (new);
* P1 — impacted, from the map (`scripts/map.py`): a changed or removed
  code block it executed, a data file it read, a directory it listed (file
  added or removed there), a table touched by a new migration; plus the
  "always" list (`tests/parts/toujours.txt`) when a migration, `db.py` or
  `deploy/sql/` changes;
* P2 — failed under measure in the last 7 days (map history);
* P3 — the rest.

**Lot branches: impacted + contract (L1211, canon §3).** On a pull request
whose source and target are lot branches, only the impacted modules (P0, P1)
and the contract list (`tests/parts/toujours.txt`: droits, rôles, migrations,
objets globaux du cluster) run; any doubt — no map, a stale or unsure
selection, a changed block without a test, test infrastructure, a removed
file outside documentation, an empty contract list — gives the full suite
(`lot_selection`). On `main` every module runs, P0 and
P1 first, then P2, then P3: the phase 1 order. A lot branch runs one part per
driver, and one more per `PR_TARGET_PART_S` of estimate, instead of the full
matrix.

**Release pull requests keep the full suite (canon §3).** A pull request
whose source or target is `release/**` or the package integration branch
(§12) is a release PR: like the `push` to those branches, it runs the full
suite under both drivers (`PARTS` in full), with no version-only or
documentation-only fast lane and without the psql pilot list — the branch
context comes from the workflow (`--base-ref`, `--head-ref`, L1211). The
same holds for the fallback plan, so an unreadable map never narrows a
release to the pilot list. Only the full suite prepares the merge of a
release.

**Documentation-only** (L161a): a pull request whose whole diff is
documentation, and a push to `main` whose whole pushed range (`--base`,
`github.event.before`, to the new head) is documentation, runs only its
declared readers (`tests/parts/lecteurs-doc.txt`, plus the impacted ones)
and skips the parts. A push to any other ref keeps the full suite (release
branches in particular); so do a branch created by the push, a rewritten
history and any event but `pull_request` and `push` on `main`. A pushed
version number never takes the lane: L157 is a pull-request rule.

**The full suite is the fallback.** The changed files are taken from the
map commit to HEAD, not from the merge base: whatever changed on main since
the map counts as changed, so the selection widens, never narrows. Map
missing, unreadable, older than 48 h, not an ancestor of HEAD, or built on
unreadable measure artefacts (`measure.artefacts_incomplets`, #1690/#1716):
the order falls back to the textual heuristic of L161b (`--base`, `HEAD^1` by
default) and the plan is marked unsure. A file the map cannot judge (Python
outside `src/ameesh`, a non-documentation file no test reads, a syntax
error) uses the textual heuristic for that file and marks the plan unsure;
test infrastructure (`tests/support.py`, `tests/fakebin/`, the CI scripts,
`.github/`, `pyproject.toml` beyond its version line) marks it unsure too,
unless the target already changed it since the map and the branch does not
change it itself (the merge base with the target to the head): the target's
own full suite validated that change, and a late map must never send every
lot branch back to the full suite (#1314). The branch's own change to the
foundation still marks the plan unsure, and the version-only exemption of
`pyproject.toml` (L157) is judged on that own range.
A file removed outside documentation marks it unsure as well: the map knows
the readers it measured, never the ones it cannot see (an import by a
computed name, a config read by the code).

**A block without a measure does not force the full suite** (#1339). A new
block (a file the map does not know, a function or a class it has never
seen) and a changed block no test executes are covered as soon as a test is
*proven* to load their file: the modules the map measured on it, or — when
none has executed it — the tests the *guaranteed* import graph reaches
(`src/ameesh/**` and `tests/**` at HEAD; a test that imports a changed
module which imports the new file counts, through that module's measured
importers; only the imports the module body executes itself count). Every
test that *may* load the file joins the selection too, the complete import
graph's lazy and conditional imports included: a new block has no measured
caller, and a test can reach it by a computed name (`dir()` then `getattr`)
as soon as it has loaded the file. The selection falls back to the full
suite when no test is proven to load such a file: a dynamic import
(`importlib.import_module`) leaves no static edge, an import under `if
TYPE_CHECKING:` never runs at runtime, a lazy, conditional or `with`-guarded
import is only a candidate, and a from-import proves the module it names,
never a member as a file (`from ameesh import new_feature` loads nothing
when the package `__init__` binds `new_feature = object()`; `import
ameesh.new_feature` and `from ameesh.new_feature import x` keep the proof) —
none of them certifies that a test loads the file, so the full suite stays
(`phase2_why` names it).

Any error while reading the map gives the fallback plan, never a failure.

**Changed lines without a test**: an existing block whose fingerprint
changed and that no test executes is listed in the plan, in the job summary,
and as a GitHub annotation on the PR diff (`--annotations`).

**Provisional green**: each part writes a `impacted` event to its stream
once its P0 and P1 modules are done (`scripts/test-parts.py`); when every
part says `green`, P0 and P1 passed. It is a signal for the agent, never a
merge condition (`tests` stays the required check).

**Leaks** (`leaks`): a failure of a module the map did not select (P2 or
P3). `leaks` reads the plan and the part streams of a run and writes one
record (`ameesh-test-leaks/1`), kept as the `leaks` artifact; `tally`
aggregates records for the two-week review (L161i): phase 2 requires fewer
than 1 leak per 50 PRs.

**Coverage of a package push** (`couverture`, canon §12). A package
integration branch pushed while its PR is open used to launch two full
suites — the `push integration/**` run and the `pull_request` run of
tests.yml — both carrying the required `tests` check on the same head; the
cancelled one left its aggregate red (incident #1342, PR #1050). tests.yml
no longer triggers on `push integration/**`; the gate
`.github/workflows/paquet.yml` reads the branch's open pull requests (GitHub
API, read-only) and asks this command whether one carries exactly the pushed
head (`head.sha == github.sha`, base `main`, `release/**` or
`integration/**`). If so, the suite is already carried by that PR's
`pull_request` run and the gate calls nothing; otherwise — no PR, another
head, a base outside tests.yml's scope, unreadable API, incomplete context —
it calls tests.yml (`workflow_call`) so a push without a PR keeps its full
suite. The decision never reads a run: no success is inferred from another
run's existence, and the fallback is always "the suite runs".
"""
from __future__ import annotations

import argparse
import ast
import builtins
import collections
import datetime
import importlib.util
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
PLAN_SCHEMA = "ameesh-test-plan/1"
LEAKS_SCHEMA = "ameesh-test-leaks/1"
ALWAYS_FILE = REPO / "tests" / "parts" / "toujours.txt"
PILOT_FILE = REPO / "tests" / "parts" / "pilote-psql.txt"

#: map older than this: fallback order, plan unsure
MAX_AGE_H = 48
#: phase 2 also requires a map younger than this
PHASE2_MAX_AGE_H = 24
#: failures under measure younger than this make a module P2
RECENT_FAILURE_DAYS = 7
#: parts per driver (pull request, other events). Le pilote psql d'une PR est
#: en trois parts depuis #947 : sur les runners auto-hébergés, huit jobs se
#: partagent un hôte, la part unique pesait 21,7-26,6 min au repos (médiane
#: 24,5) pour un plafond de 30 min et l'atteignait dès que la flotte était
#: saturée (24/24 runners) ; trois parts pèsent ~8 min chacune, ce qui tient au
#: ralentissement mesuré de 2,5-3x. psycopg garde quatre parts (max 17,7 min
#: saturé) ; la suite psql complète passe à neuf parts (#1692) : sur le run
#: 38736559499 attempt 2, les six parts ont pesé 26-45,5 min sous saturation
#: (2/6 annulée au plafond de 45 min alors qu'elle passait encore des tests),
#: soit ≥ 227 min de travail au total ; neuf parts visent ~30 min au pire
#: mesuré, huit en laisseraient ~34.
#: L1211 : ces nombres sont des plafonds. Une branche de lot ne lance que les
#: impactés et le contrat : une part par pilote suffit, et une part de plus par
#: PR_TARGET_PART_S d'estimation, sans jamais dépasser le plafond.
PARTS = {"psycopg": (4, 4), "psql": (3, 9)}
#: L1211 : budget nominal d'une part sur une branche de lot, en secondes. À
#: 2,5-3x sous saturation, 300 s tiennent dans le plafond de 30 min de psycopg
#: comme dans celui de 60 min de psql (#1251).
PR_TARGET_PART_S = 300
#: L1211 : une PR dont la source ou la cible est sur ces branches est une PR
#: de release (canon §3, §12) : suite complète, les deux pilotes, sans voie
#: version/doc ni liste pilote psql.
RELEASE_PREFIXES = ("release/", "integration/")
#: entries listed in the job summary, per category
SUMMARY_LIMIT = 25

MIGRATIONS_DIR = "src/ameesh/migrations/"
CODE_ROOT = "src/ameesh/"
#: changes that trigger the "always" list (#749: the runner drives turns,
#: leases and orphan settlement through objects global to the cluster, which
#: the per-schema measure of the map cannot see)
ALWAYS_TRIGGERS = (MIGRATIONS_DIR, "src/ameesh/db.py", "deploy/sql/",
                   "src/ameesh/runner.py")
#: test infrastructure: the map cannot judge a change there
FOUNDATION = ("tests/support.py", "tests/__init__.py", "tests/fakebin/", ".github/",
              "scripts/test-parts.py", "scripts/test-plan.py", "scripts/map.py",
              "scripts/map_measure.py", "scripts/ci-flow.py", "scripts/version-only.py")
#: a migration that is not purely additive impacts every database test
#: (any `drop`, `alter table t drop c` included; `create or replace` rewrites)
_NOT_ADDITIVE = re.compile(
    r"\b(drop|create\s+or\s+replace"
    r"|rename\s+(to|column)|alter\s+column\s+[\w\"]+\s+(set\s+data\s+)?type"
    r"|truncate)\b", re.I)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parts = _load("test_parts", "test-parts.py")
version_only = _load("version_only", "version-only.py")
sys.path.insert(0, str(SCRIPTS))
import map as map_mod  # noqa: E402  scripts/map.py


# -- git ------------------------------------------------------------------

def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


def changed_files(base: str, head: str = "HEAD",
                  repo: pathlib.Path = REPO) -> list[tuple[str, str]] | None:
    """[(status, path)] between two commits (A, M, D, T…), None if unreadable."""
    try:
        out = _git(repo, "diff", "--name-status", "--no-renames", base, head)
    except (OSError, subprocess.CalledProcessError):
        return None
    result = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if path:
            result.append((status[:1], path))
    return result


def is_ancestor(commit: str, head: str = "HEAD", repo: pathlib.Path = REPO) -> bool:
    try:
        subprocess.run(["git", "merge-base", "--is-ancestor", commit, head], cwd=repo,
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def is_merge_commit(head: str = "HEAD", repo: pathlib.Path = REPO) -> bool:
    """True when `head` is a merge commit: two parents or more."""
    try:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", head).split()
    except (OSError, subprocess.CalledProcessError):
        return False
    return len(parents) > 2


def own_changes(base: str, head: str = "HEAD",
                repo: pathlib.Path = REPO) -> set[str] | None:
    """Paths this branch changes itself: the base it merges (its target's tip,
    `HEAD^1` of the merge commit GitHub tested) to its head.

    `HEAD^1` is that base only on a pull request's merge commit; on a plain
    commit it is the branch's previous commit, and its diff would not cover the
    whole branch: the range is then refused, and the caller keeps its
    conservative doubt. None as well when the diff cannot be read.
    """
    if base == "HEAD^1" and not is_merge_commit(head, repo):
        return None
    changes = changed_files(base, head, repo)
    return None if changes is None else {path for _, path in changes}


def show(path: str, rev: str = "HEAD", repo: pathlib.Path = REPO) -> str | None:
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except (OSError, subprocess.CalledProcessError):
        return None


def existed(path: str, rev: str, repo: pathlib.Path = REPO) -> bool:
    try:
        subprocess.run(["git", "cat-file", "-e", f"{rev}:{path}"], cwd=repo, check=True,
                       capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


# -- static imports (#1339) -----------------------------------------------

def _dotted(path: str) -> str:
    """Dotted module name of a repository Python file, `""` outside the two
    measured packages: `src/ameesh/x/y.py` → `ameesh.x.y`, `tests/test_a.py` →
    `tests.test_a`; an `__init__.py` names its package."""
    p = pathlib.PurePosixPath(path)
    if p.suffix != ".py":
        return ""
    parts = list(p.parts)
    if parts[:2] == ["src", "ameesh"]:
        parts = parts[1:]
    elif parts[:1] != ["tests"]:
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _module_files(name: str, known: set[str]) -> set[str]:
    """The repository files a dotted name may designate: the module itself, its
    package `__init__` and every parent package (importing a submodule runs
    each of them). `known` limits the result to the files of this tree."""
    if not name:
        return set()
    parts = name.split(".")
    root = "src" if parts[0] == "ameesh" else ""
    found: set[str] = set()
    for i in range(1, len(parts) + 1):
        base = "/".join(([root] if root else []) + parts[:i])
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in known:
                found.add(candidate)
    return found


def _type_checking_names(tree: ast.AST) -> set[str]:
    """Names bound to `typing.TYPE_CHECKING`: the plain one and any alias
    (`from typing import TYPE_CHECKING as TC`)."""
    names = {"TYPE_CHECKING"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "typing":
            for alias in node.names:
                if alias.name == "TYPE_CHECKING":
                    names.add(alias.asname or alias.name)
    return names


def _never_runs(test: ast.expr, names: set[str]) -> bool:
    """Whether an `if` guard is false when the code runs: `TYPE_CHECKING`
    (always false at runtime, whatever `typing` calls it) or a false constant.
    `not TYPE_CHECKING` is true, so it is not caught here."""
    if isinstance(test, ast.Constant):
        return not test.value
    if isinstance(test, ast.Name):
        return test.id in names
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_never_runs(value, names) for value in test.values)
    return False


def _getattr_member(node: ast.Call) -> tuple[str, str] | None:
    """`getattr(<name>, '<constant>', ...)` lu statiquement : le couple
    (porteur, membre), ou `None` (forme dynamique, argument manquant). Le
    défaut éventuel est sans effet ici : `sys.exit`/`os._exit` existent
    toujours, donc l'appel termine quel que soit le défaut. Le porteur peut
    être affecté par walrus (`getattr((s := sys), 'exit')`, #1906) : on
    déballe `ast.NamedExpr` avant le test de nom."""
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
        return None
    if len(node.args) < 2:
        return None
    owner, attr = node.args[0], node.args[1]
    if isinstance(owner, ast.NamedExpr):
        owner = owner.value
    if (isinstance(owner, ast.Name) and isinstance(attr, ast.Constant)
            and isinstance(attr.value, str)):
        return owner.id, attr.value
    return None


def _is_exit_func(node: ast.expr, exit_names: set[str],
                  sys_names: set[str], os_names: set[str]) -> bool:
    """Whether an expression designates a known import-time termination: a
    bound name, `sys.exit`/`os._exit`, or their static `getattr` read (#1878)
    (aliases of `sys`, `os` and of the imported names included). Le porteur
    peut être affecté par walrus (`(s := sys).exit(1)`, #1906)."""
    if isinstance(node, ast.NamedExpr):
        # `(e := sys.exit)(1)` calls the assigned value immediately (#1860).
        return _is_exit_func(node.value, exit_names, sys_names, os_names)
    if isinstance(node, ast.Name):
        return node.id in exit_names
    if isinstance(node, ast.Attribute):
        owner = node.value.value if isinstance(node.value, ast.NamedExpr) else node.value
        if isinstance(owner, ast.Name):
            if node.attr == "exit":
                return owner.id in sys_names
            if node.attr == "_exit":
                return owner.id in os_names
        return False
    if isinstance(node, ast.Call):
        read = _getattr_member(node)
        if read is None:
            return False
        owner, attr = read
        return ((attr == "exit" and owner in sys_names)
                or (attr == "_exit" and owner in os_names))
    return False


def _is_exit_call(node: ast.Call, exit_names: set[str],
                  sys_names: set[str], os_names: set[str]) -> bool:
    """Whether a call is a known import-time termination: the builtins
    `exit()`/`quit()`, `sys.exit()`/`os._exit()` (aliases of `sys`, `os` and of
    the imported names included)."""
    return _is_exit_func(node.func, exit_names, sys_names, os_names)


def _may_terminate(node: ast.AST, names: set[str], exit_names: set[str],
                   sys_names: set[str], os_names: set[str]) -> bool:
    """Whether running this statement may end the import: a reachable `raise`,
    a false `assert`, or an exit call (#1627).

    Deliberately conservative — a `raise` caught by an enclosing `try` still
    counts (a proof lost, never a false proof) — and the body of an
    `if TYPE_CHECKING:` / `if False:` is dead, as in `_never_runs`. A function
    or lambda body runs later, not at import, so it terminates nothing here."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return False
    if isinstance(node, ast.Raise):
        return True
    if isinstance(node, ast.Assert):
        return _never_runs(node.test, names)
    if isinstance(node, ast.Call) and _is_exit_call(node, exit_names, sys_names, os_names):
        return True
    if isinstance(node, ast.If) and _never_runs(node.test, names):
        children: list[ast.AST] = list(node.orelse)  # seul le `else` s'exécute
    else:
        children = list(ast.iter_child_nodes(node))
    return any(_may_terminate(child, names, exit_names, sys_names, os_names)
               for child in children)


class _RuntimeImports(ast.NodeVisitor):
    """The import statements that may run, and those the module certainly runs
    when it is imported.

    The body of `if TYPE_CHECKING:` — and of any other guard that never runs,
    `if False:` — loads nothing and is left out; everything else stays, lazy
    imports included: they may run. `guaranteed` keeps only the imports the
    module body executes itself: at the top level (a class body runs at
    import, a function body does not), outside any condition, loop, `try` or
    `with` — a `with` body may never run (its `__enter__` may fail, or an
    earlier statement may raise and be suppressed), so it is no proof.
    An unconditional module-level `raise` or `sys.exit` also leaves every
    later import without proof, while keeping its possible selection edge.
    A lazy or conditional import alone is a possible load, never a proof
    (relecture Codex, B2 résiduel). A reachable termination — a guarded
    `raise`, an `except` that exits, `exit()`/`sys.exit()`/`os._exit()`, a
    false `assert` — stops the proofs too: the imports after it may never run
    (#1627). A name bound to such a callable by assignment or by a star import
    (`e = sys.exit`, `from os import *` then `_exit()`) terminates as well, the
    binding being followed conservatively (#1860)."""

    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.imports: list[ast.stmt] = []
        self.guaranteed: list[ast.stmt] = []
        self._deferred = 0  # inside a function or lambda body: not run at import
        self._guarded = 0   # inside a condition, a loop or a try: may not run
        self._terminated = False  # module execution cannot reach later imports
        # #1627 : une terminaison atteignable arrête les preuves du corps
        # (module ou classe) : les imports suivants restent possibles seulement.
        self._exit_names = {"exit", "quit"}  # builtins, `from sys import exit`
        self._sys_names = {"sys"}            # `sys.exit`, alias compris
        self._os_names = {"os"}              # `os._exit`, alias compris

    def _visit_function(self, node: ast.AST) -> None:
        # Décorateurs, valeurs par défaut et annotations s'évaluent à la
        # définition, donc à l'import : seul le corps est différé (#1474).
        body = node.body if isinstance(node.body, list) else [node.body]
        for child in ast.iter_child_nodes(node):
            if not any(child is statement for statement in body):
                self.visit(child)
        self._deferred += 1
        for child in body:
            self.visit(child)
        self._deferred -= 1

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def _visit_guarded(self, node: ast.AST) -> None:
        self._guarded += 1
        self.generic_visit(node)
        self._guarded -= 1

    visit_For = _visit_guarded
    visit_AsyncFor = _visit_guarded
    visit_While = _visit_guarded
    visit_Try = _visit_guarded
    visit_TryStar = _visit_guarded
    visit_Match = _visit_guarded
    visit_With = _visit_guarded
    visit_AsyncWith = _visit_guarded

    def visit_Raise(self, node: ast.Raise) -> None:
        self.generic_visit(node)
        if not self._deferred and not self._guarded:
            self._terminated = True

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        if not self._deferred and _is_exit_call(
                node, self._exit_names, self._sys_names, self._os_names):
            self._terminated = True

    def _target_names(self, target: ast.expr) -> list[str]:
        """Les noms qu'une cible d'affectation lie vraiment : `e`, `e, f`,
        `(e, *f)`. Un abonnement ou un attribut (`d[k]`, `obj.handler`) ne
        rebaptise aucun nom et n'en lie aucun."""
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, ast.Starred):
            return self._target_names(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return [name for element in target.elts
                    for name in self._target_names(element)]
        return []

    def _learn_exit_names(self, target: ast.expr, value: ast.expr | None) -> None:
        """Un nom affecté d'une sortie connue en devient une (#1860) :
        `e = sys.exit` puis `e()`. Un nom affecté d'un module `sys`/`os`
        devient un de ses alias (#1878) : `s = sys` puis `s.exit()`. Les
        ensembles ne rétrécissent jamais — une réaffectation peut être
        conditionnelle (#1474) : une preuve perdue n'est jamais une fausse
        preuve. Une cible multiple lisible est suivie élément par élément ;
        toute autre forme est gardée entière dès que la valeur mentionne une
        sortie ou un de ces modules (conservateur)."""
        if value is None:
            return
        if (isinstance(target, (ast.Tuple, ast.List))
                and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)):
            for sub_target, sub_value in zip(target.elts, value.elts):
                self._learn_exit_names(sub_target, sub_value)
            return
        names = self._target_names(target)
        if not names:
            return
        mentioned = [node.id for node in ast.walk(value)
                     if isinstance(node, ast.Name)]
        if any(name in self._sys_names for name in mentioned):
            self._sys_names.update(names)
        if any(name in self._os_names for name in mentioned):
            self._os_names.update(names)
        if any(_is_exit_func(node, self._exit_names, self._sys_names,
                             self._os_names)
               for node in ast.walk(value)):
            self._exit_names.update(names)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._learn_exit_names(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        self._learn_exit_names(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        self._learn_exit_names(node.target, node.value)

    def visit_If(self, node: ast.If) -> None:
        if _never_runs(node.test, self.names):
            # the guard never runs: only its `else` does (at module level)
            for child in node.orelse:
                self.visit(child)
            return
        self._visit_guarded(node)

    def _visit_body(self, body: list[ast.stmt]) -> None:
        """The body of a module or a class runs at import, in order: a
        reachable termination cuts the proofs that follow it (#1627)."""
        for child in body:
            self.visit(child)
            if not self._deferred and not self._terminated and _may_terminate(
                    child, self.names, self._exit_names, self._sys_names, self._os_names):
                self._terminated = True

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_body(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Ces expressions s'évaluent avant le corps : une sortie y interdit
        # aussi de certifier les imports du corps, pas seulement ceux d'après.
        for expression in (*node.decorator_list, *node.bases,
                           *(keyword.value for keyword in node.keywords)):
            self.visit(expression)
        self._visit_body(node.body)

    def _record(self, node: ast.stmt) -> None:
        self.imports.append(node)
        if not self._deferred and not self._guarded and not self._terminated:
            self.guaranteed.append(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "sys":
                self._sys_names.add(alias.asname or "sys")
            elif alias.name == "os":
                self._os_names.add(alias.asname or "os")
        self._record(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "sys":
            self._exit_names.update(alias.asname or alias.name
                                    for alias in node.names if alias.name == "exit")
        elif module == "os":
            self._exit_names.update(alias.asname or alias.name
                                    for alias in node.names if alias.name == "_exit")
        if any(alias.name == "*" for alias in node.names):
            # #1860 : `from os import *` lie `_exit` au corps du module (les
            # builtins `exit`/`quit` et `from sys import *` sont déjà couverts).
            if module == "os":
                self._exit_names.add("_exit")
            elif module == "sys":
                self._exit_names.add("exit")
        self._record(node)


def _imported_files(source: str, path: str, known: set[str], *,
                    guaranteed: bool = False) -> set[str]:
    """The repository files `source` may load at runtime: any depth, a lazy
    import included (it may run). An import under `if TYPE_CHECKING:` runs
    never — the flag is false at runtime — so it leaves no edge and never
    certifies that a test loads a new file (relecture Codex, B2). With
    `guaranteed`, only the imports executed by the module body itself are
    kept: a lazy, conditional or `with`-guarded import may load the file, it
    does not prove that importing `source` loads it (B2 résiduel). A
    from-import (`from ameesh import db`) proves its resolved module (the
    package `ameesh`, whose `__init__` runs), never the member as a file:
    the package may already bind the name (`db = object()`), and then the
    submodule is not imported at all (B2 résiduel) — `import ameesh.db` and
    `from ameesh.db import x` name the module itself and keep the proof.
    Only `ameesh.*` and `tests.*` names are resolved; a syntax error leaves
    no edge."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    dotted = _dotted(path)
    if pathlib.PurePosixPath(path).name == "__init__.py":
        package = dotted
    else:
        package = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    names: set[str] = set()
    collector = _RuntimeImports(_type_checking_names(tree))
    collector.visit(tree)
    for node in (collector.guaranteed if guaranteed else collector.imports):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                module = f"{base}.{module}" if module else base
            if module:
                names.add(module)
                # Un from-import charge son module ; un membre peut déjà être
                # un objet du module (`from ameesh import db` avec
                # `db = object()` dans `__init__`), donc un fichier enfant
                # n'est qu'une cible possible, jamais une preuve.
                if not guaranteed:
                    names.update(f"{module}.{alias.name}" for alias in node.names
                                 if alias.name != "*")
    return {f for name in names if name.split(".")[0] in ("ameesh", "tests")
            for f in _module_files(name, known)}


def _tree_python_files(head: str, repo: pathlib.Path) -> list[str]:
    """Python files of the measured packages at `head` (git, not the working
    tree: a documentation plan judges its base as well)."""
    try:
        out = _git(repo, "ls-tree", "-r", "--name-only", head)
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(p for p in out.splitlines()
                  if p.endswith(".py")
                  and (p.startswith("src/ameesh/") or p.startswith("tests/")))


def _tree_sources(head: str, paths: list[str], repo: pathlib.Path) -> dict[str, str]:
    """Content of `paths` at `head`, in one `git cat-file --batch` call."""
    if not paths:
        return {}
    try:
        proc = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, check=True,
                              input="".join(f"{head}:{p}\n" for p in paths).encode(),
                              capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return {}
    out, pos, sources = proc.stdout, 0, {}
    for path in paths:
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 2 and header[1] == b"missing":
            continue  # a missing object: `--batch` prints `<name> missing`
        if len(header) != 3:
            break
        try:
            size = int(header[2])
        except ValueError:
            break
        chunk = out[pos:pos + size]
        if header[1] == b"blob":
            sources[path] = chunk.decode("utf-8", "replace")
        pos += size + 1
    return sources


def _import_graph(head: str, repo: pathlib.Path, *,
                  guaranteed: bool = False) -> dict[str, set[str]]:
    """Reverse static import graph at `head`: for each file, the files whose
    source imports it. A dynamic import (`importlib.import_module`) leaves no
    edge: a new file no test imports keeps the full suite (#1339). With
    `guaranteed`, only the edges the importing module executes itself are
    kept: importing that module certainly loads the target, while a lazy or
    conditional import only may."""
    files = _tree_python_files(head, repo)
    known = set(files)
    reverse: dict[str, set[str]] = {}
    for path, source in _tree_sources(head, files, repo).items():
        for target in _imported_files(source, path, known, guaranteed=guaranteed):
            if target != path:
                reverse.setdefault(target, set()).add(path)
    return reverse


def _reverse_closure(reverse: dict[str, set[str]], path: str) -> set[str]:
    """`path` and every file that imports it, directly or not."""
    seen, stack = {path}, [path]
    while stack:
        for source in reverse.get(stack.pop(), ()):
            if source not in seen:
                seen.add(source)
                stack.append(source)
    return seen


# -- the map --------------------------------------------------------------

def ignored_coverages(card: dict) -> list:
    """Couvertures que la fusion de la carte a écartées (#1257). Une carte
    antérieure à la clé n'en porte pas : sa mesure est complète par
    construction (l'ancien code échouait sur un fichier illisible au lieu de
    l'ignorer)."""
    mesure = card.get("measure") or card.get("mesure") or {}
    if not isinstance(mesure, dict):
        return []
    ignorees = mesure.get("couverture_ignoree")
    if not ignorees:
        return []
    return ignorees if isinstance(ignorees, list) else [ignorees]


def map_problem(card: dict | None, now: datetime.datetime, head: str = "HEAD",
                repo: pathlib.Path = REPO) -> tuple[str, float | None]:
    """Why the map cannot be used ("" if it can), and its age in hours."""
    if card is None:
        return "no map", None
    ignorees = ignored_coverages(card)
    if ignorees:
        # #1257 (R1 B3) : une couverture écartée à la fusion rend la mesure
        # partielle — les modules que le processus perdu mesurait manquent, et
        # les durées connues feraient passer la sélection pour sûre. La carte ne
        # juge plus la restriction : repli suite complète, jamais une décision
        # sur une carte partielle.
        return (f"map measured with {len(ignorees)} ignored coverage file(s) "
                "(partial measure)"), None
    try:
        generated = datetime.datetime.fromisoformat(card["genere"])
    except (KeyError, TypeError, ValueError):
        return "map without a generation date", None
    age = (now - generated).total_seconds() / 3600
    if age > MAX_AGE_H:
        return f"map older than {MAX_AGE_H} h ({age:.0f} h)", age
    if not card.get("commit") or not is_ancestor(card["commit"], head, repo):
        return f"map commit {str(card.get('commit'))[:12]} is not an ancestor of {head}", age
    # #1716 : #1690 écarte un artefact de mesure illisible et le nomme dans
    # `artefacts_incomplets`, mais la carte reste amputée des tests que cet
    # artefact portait : un bloc peut ressortir couvert par le seul artefact
    # lisible, et la sélection se réduire à l'insu du plan. La carte entière
    # est suspecte — on ne sait pas quels blocs l'artefact perdu couvrait —
    # donc ordre complet, comme pour toute carte inutilisable.
    incomplets = (card.get("measure") or card.get("mesure") or {}).get(
        "artefacts_incomplets") or []
    if incomplets:
        fichiers = ", ".join(str(i.get("fichier", "?")) for i in incomplets[:3]
                             if isinstance(i, dict))
        return (f"map with {len(incomplets)} unreadable measure artefact(s)"
                + (f" ({fichiers})" if fichiers else "")), age
    return "", age


def is_doc(path: str) -> bool:
    return version_only.is_doc(path)


def is_foundation(path: str) -> bool:
    return path.startswith(FOUNDATION) or path == "pyproject.toml"


def read_list(path: pathlib.Path, known: list[str], required: bool = False) -> list[str]:
    """`parts.read_list`, whose SystemExit (stale entry) becomes a
    ValueError: `main` catches it and falls back to the full suite."""
    if not path.exists() and not required:
        return []
    try:
        return parts.read_list(path, known)
    except SystemExit as exc:
        raise ValueError(str(exc)) from None


class Impact:
    """What a set of changes impacts, judged against the map."""

    def __init__(self) -> None:
        self.p0: dict[str, str] = {}
        self.p1: dict[str, str] = {}
        self.untested: list[dict] = []       # changed blocks no test executes
        self.unknown_blocks: list[str] = []  # blocks the map has never seen
        self.unsure: list[str] = []          # why the map cannot judge everything
        self.textual: list[str] = []         # files left to the L161b heuristic
        self.foundation: list[str] = []
        self.removed: list[str] = []         # files removed outside documentation
        #: #1339 : blocs sans mesure couverts par les tests qui importent leur
        #: fichier (fichier → modules), et fichiers qu'aucun test n'importe
        self.importers: dict[str, list[str]] = {}
        #: #1339 : sous-ensemble des importateurs qui *prouvent* le chargement
        #: (mesure, ou import exécuté au niveau module) ; les autres ne sont que
        #: des candidats de sélection (relecture Codex, B2 résiduel)
        self.certified: dict[str, list[str]] = {}
        self.uncovered: list[str] = []

    def hit(self, modules, reason: str) -> None:
        for m in modules:
            self.p1.setdefault(m, reason)

    def doubt(self, reason: str, path: str | None = None) -> None:
        if reason not in self.unsure:
            self.unsure.append(reason)
        if path and path not in self.textual:
            self.textual.append(path)


def _measured_importers(card: dict, path: str, modules: list[str]) -> set[str]:
    """Test modules the map saw load `path`: any block of the file executed by
    a test means that test imported it (a subprocess run included)."""
    found: set[str] = set()
    for info in card.get("blocs", {}).get(path, {}).values():
        found |= set(info.get("modules", ()))
    return found & set(modules)


def _reaching_tests(card: dict, path: str, modules: list[str],
                    reverse: dict[str, set[str]]) -> set[str]:
    """Tests that load `path` at HEAD: those that reach it through the static
    import graph, plus, for every file of that closure, the tests the map
    measured on it — a test that imports a changed module which imports the
    new one (#1339). The graph given decides the strength: the complete one
    selects every possible importer, the guaranteed one proves the load."""
    reached = _reverse_closure(reverse, path)
    found = {m for m in modules if f"tests/{m}.py" in reached}
    for source in reached:
        found |= _measured_importers(card, source, modules)
    return found


def _resolve_unmeasured(card: dict, impact: Impact, modules: list[str],
                        head: str, repo: pathlib.Path) -> None:
    """#1339 : un bloc nouveau ou modifié sans couverture mesurée ne force plus
    la suite complète dès qu'un test charge son fichier.

    Deux ensembles se séparent (relecture Codex, B2 résiduel) :

    - la **sélection** : tous les tests qui peuvent charger le fichier — les
      modules mesurés dessus, et ceux du graphe d'import complet (imports
      paresseux et conditionnels compris) — rejoignent les impactés. Un bloc
      nouveau n'a par définition aucun appelant mesuré, et un test peut
      l'atteindre par un nom calculé (`dir()` puis `getattr`), donc chaque
      test qui a chargé le fichier peut l'exécuter (B1) ; élargir ne nuit pas.
    - la **preuve** : la mesure, ou un chargement garanti au niveau module (le
      graphe des imports que le corps du fichier exécute à l'import). Un test
      n'est certifié que par une preuve : un import paresseux, conditionnel ou
      sous `with` seul ne lève pas le doute, et un from-import ne prouve que
      son module résolu — pas le membre, que le paquet peut lier à un objet
      (`from ameesh import new_feature` avec `new_feature = object()` dans
      `__init__`). `uncovered` porte le fichier et la suite complète reste —
      au lieu de certifier une couverture qu'un test dynamique (`importlib`)
      assurerait sans être sélectionné (B2).
    """
    unknown_files = {b.split("::", 1)[0] for b in impact.unknown_blocks}
    untested_files = {u["file"] for u in impact.untested}
    targets = sorted(unknown_files | untested_files)
    if not targets:
        return
    reverse: dict[str, set[str]] | None = None
    proven_reverse: dict[str, set[str]] | None = None
    for path in targets:
        measured = _measured_importers(card, path, modules)
        if reverse is None:
            reverse = _import_graph(head, repo)
        #: sélection : les importateurs mesurés, plus tous ceux qui *peuvent*
        #: charger le fichier (le graphe complet : paresseux et conditionnels)
        potential = measured | _reaching_tests(card, path, modules, reverse)
        if measured:
            proven = measured
        else:
            if proven_reverse is None:
                proven_reverse = _import_graph(head, repo, guaranteed=True)
            proven = _reaching_tests(card, path, modules, proven_reverse)
        if potential:
            for m in sorted(potential):
                impact.p1.setdefault(m, f"{path} (imported)")
            impact.importers[path] = sorted(potential)
        if not proven:
            impact.uncovered.append(path)
            continue
        impact.certified[path] = sorted(proven)


def _db_modules(card: dict) -> set[str]:
    return {m for ms in card.get("tables", {}).values() for m in ms}


def _dir_listers(card: dict, path: str, map_commit: str, repo: pathlib.Path) -> tuple[set, str]:
    """Modules that listed a directory above `path`, which appeared or
    vanished: every ancestor, since a directory may appear or vanish with it
    (the only file of `docs/sub`). The reason names the first ancestor that
    existed at the map commit."""
    listers: set[str] = set()
    where = None
    for parent in pathlib.PurePosixPath(path).parents:
        parent = str(parent)
        listers |= set(card.get("dossiers", {}).get(parent, ()))
        if where is None and (parent in ("", ".") or existed(parent, map_commit, repo)):
            where = parent
    return listers, where or "."


def _python(card: dict, impact: Impact, status: str, path: str, head: str,
            repo: pathlib.Path) -> None:
    old = card.get("blocs", {}).get(path)
    if status == "D":
        listers, where = _dir_listers(card, path, card["commit"], repo)
        impact.hit(listers, f"{path} (removed from {where})")
        if old is not None:
            impact.hit({m for b in old.values() for m in b.get("modules", ())},
                       f"{path} (removed)")
        return
    source = show(path, head, repo)
    if source is None:
        impact.doubt(f"{path}: unreadable at {head}", path)
        return
    try:
        new, _ = map_mod.blocks(source)
    except (SyntaxError, ValueError):
        impact.doubt(f"{path}: syntax error", path)
        return
    if old is None:
        if status != "A":
            impact.doubt(f"{path}: code file unknown to the map", path)
            return
        impact.unknown_blocks.extend(f"{path}::{b}" for b in sorted(new))
        listers, where = _dir_listers(card, path, card["commit"], repo)
        impact.hit(listers, f"{path} (new file in {where})")
        return
    added = set(new) - set(old)
    stripped = _without_new_functions(source, added) if added else new
    for name, info in sorted(old.items()):
        after = new.get(name)
        if after is not None and after["empreinte"] == info.get("empreinte"):
            continue
        if stripped.get(name, {}).get("empreinte") == info.get("empreinte"):
            continue  # only new functions were added: their callers changed
        modules = info.get("modules", ())
        impact.hit(modules, f"{path}::{name}")
        if not modules and after is not None:
            impact.untested.append({"file": path, "block": name, "lines": after["lignes"]})
    impact.unknown_blocks.extend(f"{path}::{b}" for b in sorted(added))


def _dynamic(tree: ast.AST) -> bool:
    """Whether the module may reach a function by a computed name: `import *`,
    `globals()`, `vars()`, `locals()`, or `getattr` with a name that is not a
    literal (`getattr(self, "_" + kind)`). A new function is then never taken out."""
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in ("globals", "vars", "locals"):
                return True
            if n.func.id in ("getattr", "hasattr") and len(n.args) > 1 \
                    and not isinstance(n.args[1], ast.Constant):
                return True
    return False


def _without_new_functions(source: str, added: set[str]) -> dict:
    """The blocks of `source` once its new, plain module-level functions are
    taken out.

    A new function runs only when a caller runs it, and that caller changed:
    its tests are found there (study, section 1). Taking it out keeps its
    module unchanged for the map. A function stays in (its module counts as
    changed) when it is decorated (a decorator runs at import: registration),
    a dunder, or a name the module also binds otherwise or a builtin it may
    shadow (`def list()`). Nothing is taken out of a module that may call a
    function by a computed name (`_dynamic`). New methods and new classes
    always count: a method may override a base class method or be dispatched
    by name (`do_DELETE` of BaseHTTPRequestHandler), a class body runs at import.
    """
    tree = ast.parse(source)
    if _dynamic(tree):
        return map_mod.blocks(source)[0]
    bound = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
             and isinstance(n.ctx, ast.Store)}
    bound |= {(a.asname or a.name).split(".")[0] for n in ast.walk(tree)
              if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    bound |= set(dir(builtins))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.name in added and not node.decorator_list
                         and not node.name.startswith("__") and node.name not in bound)]
    return map_mod.blocks(ast.unparse(tree))[0]


def _migration(card: dict, impact: Impact, status: str, path: str, head: str,
               repo: pathlib.Path) -> None:
    db = _db_modules(card)
    if status != "A" or path in card.get("migrations", {}):
        impact.hit(db, f"{path} (existing migration changed)")
        impact.doubt(f"{path}: an existing migration changed")
        return
    listers, _ = _dir_listers(card, path, card["commit"], repo)
    impact.hit(listers, f"{path} (new migration)")
    impact.hit({m for f, ms in card.get("donnees", {}).items() if f.startswith("deploy/sql/")
                for m in ms}, f"{path} (deploy/sql readers)")
    sql = show(path, head, repo) or ""
    tables = map_mod.migration_tables(sql)
    if not tables or _NOT_ADDITIVE.search(re.sub(r"--[^\n]*", "", sql)):
        impact.hit(db, f"{path} (not additive, or tables unknown)")
        return
    for table in tables:
        impact.hit(card.get("tables", {}).get(table, ()), f"{path} (table {table})")


def _data(card: dict, impact: Impact, status: str, path: str, repo: pathlib.Path) -> None:
    readers = set(card.get("donnees", {}).get(path, ()))
    impact.hit(readers, path)
    listers: set[str] = set()
    if status in ("A", "D"):
        listers, where = _dir_listers(card, path, card["commit"], repo)
        impact.hit(listers, f"{path} ({'added to' if status == 'A' else 'removed from'} {where})")
    # a removal outside documentation is already a doubt, raised by `judge`:
    # only a removed document stays quiet here (the docs lane)
    if not readers and not listers and not is_doc(path) and status != "D":
        impact.doubt(f"{path}: read by no measured test", path)


def judge(card: dict, changes: list[tuple[str, str]], modules: list[str],
          head: str = "HEAD", repo: pathlib.Path = REPO,
          own: set[str] | None = None, own_base: str | None = None) -> Impact:
    """The impact of `changes` (since the map commit) on the test modules.

    `own` is the set of paths the branch itself changes — `own_base` (the base
    it merges, `HEAD^1` of a pull request's merge commit) to `head` — and
    `None` when that range cannot be read or is not trustworthy. A file of the
    CI foundation (`is_foundation`) that the target already changed since the
    map commit, and that this branch does not change, raises no doubt: the
    target's own full suite validated it, and a late map must not send every
    lot branch back to the full suite (#1314). The version-only exemption of
    `pyproject.toml` (L157) is judged on that same own range, so a target
    change to the file no longer breaks it.
    """
    impact = Impact()
    known_modules = set(card.get("modules", {}))
    for m in modules:
        if m not in known_modules:
            impact.p0[m] = "unknown to the map"
    for status, path in changes:
        # a removal has no new blocks to judge, and the map only knows the
        # readers it measured: an import by a computed name, a config read by
        # the code, a template — the removal is a doubt, never a silent
        # selection (R2 bureau, B1). `--no-renames`: a rename is D + A here.
        if status == "D" and not is_doc(path):
            impact.removed.append(path)
            impact.unsure.append(f"file removed: {path}")
        # a file read as data (a .py, a test module, a migration as well): its
        # readers, whatever the kind of file and even when its blocks did not change
        impact.hit(card.get("donnees", {}).get(path, ()), path)
        stem = pathlib.PurePosixPath(path)
        if path.startswith("tests/") and stem.parent.as_posix() == "tests" \
                and stem.name.startswith("test") and stem.suffix == ".py":
            if status != "D" and stem.stem in modules:
                impact.p0[stem.stem] = path
            if status in ("A", "D"):  # the listing of tests/ changed
                listers, where = _dir_listers(card, path, card["commit"], repo)
                impact.hit(listers, f"{path} ({'added to' if status == 'A' else 'removed from'} "
                                    f"{where})")
            continue
        if is_foundation(path):
            # #1314 : un fichier du socle déjà changé par la cible depuis la
            # carte ne compte que s'il diffère entre la base de fusion et la
            # tête du lot ; sinon une carte en retard ferait retomber toute
            # branche de lot sur la suite complète.
            if own is not None and path not in own:
                impact.hit(card.get("donnees", {}).get(path, ()), path)
                continue
            version_base = own_base if own is not None else card["commit"]
            if path == "pyproject.toml" and _version_line_only(head, version_base, repo):
                impact.hit(card.get("donnees", {}).get(path, ()), path)
                continue
            impact.foundation.append(path)
            impact.hit(card.get("donnees", {}).get(path, ()), path)
            impact.doubt(f"test infrastructure changed: {path}", path)
            continue
        if path.startswith(CODE_ROOT) and path.endswith(".py"):
            _python(card, impact, status, path, head, repo)
        elif path.endswith(".py"):
            impact.doubt(f"{path}: Python outside {CODE_ROOT} is not measured", path)
        elif path.startswith(MIGRATIONS_DIR) and path.endswith(".sql"):
            _migration(card, impact, status, path, head, repo)
        else:
            _data(card, impact, status, path, repo)
    #: #1339 : les blocs sans mesure sont couverts par les tests qui importent
    #: leur fichier (sélection élargie), la preuve étant la mesure ou un import
    #: exécuté au niveau module (relecture Codex, B2 résiduel).
    _resolve_unmeasured(card, impact, modules, head, repo)
    return impact


def _version_line_only(head: str, base: str, repo: pathlib.Path) -> bool:
    try:
        patch = _git(repo, "diff", "-U0", base, head, "--", "pyproject.toml")
    except (OSError, subprocess.CalledProcessError):
        return False
    ok, _ = version_only.version_only(["pyproject.toml"], patch)
    return ok


def always_hits(changes: list[tuple[str, str]], always: list[str]) -> dict[str, str]:
    trigger = next((p for _, p in changes if p.startswith(ALWAYS_TRIGGERS)), None)
    return {m: f"always list ({trigger})" for m in always} if trigger else {}


def psql_pr_modules(pilot: list[str], always: list[str]) -> list[str]:
    """#749 : modules du pilote psql d'une PR, augmentes de la liste « always »
    declenchee par le diff (contrat de base, baux et orphelins). Le pilote psql
    d'une PR ne voit sinon que `tests/parts/pilote-psql.txt` : une regression
    qu'il est le seul a voir, dans un module hors liste, attendait la suite
    complete de main (rouge de release de l'incident #749)."""
    return sorted(set(pilot) | set(always))


def recent_failures(card: dict | None, now: datetime.datetime) -> dict[str, str]:
    if not card:
        return {}
    limit = (now - datetime.timedelta(days=RECENT_FAILURE_DAYS)).isoformat()
    result = {}
    for module, info in card.get("modules", {}).items():
        recent = [e for e in info.get("echecs", []) if e.get("ts", "") >= limit]
        if recent:
            result[module] = f"failed under measure {recent[-1].get('ts', '')[:10]}"
    return result


def textual(modules: list[str], files: list[str]) -> dict[str, str]:
    """The L161b heuristic (the test text names the file), P1 only."""
    if not files:
        return {}
    classes = parts.classify(modules, files, ambigus=parts.ambiguous_names())
    hits = {m: r for m, (c, r) in classes.items() if c == "P1"}
    for f in files:  # a test helper: `from tests.fake_github import …`
        p = pathlib.PurePosixPath(f)
        if p.parts[0] == "tests" and p.suffix == ".py":
            pattern = re.compile(r"\b" + re.escape(p.stem) + r"\b")
            for m in modules:
                if m not in hits and pattern.search(
                        (parts.TESTS / f"{m}.py").read_text(encoding="utf-8")):
                    hits[m] = f
    return hits


# -- the plan -------------------------------------------------------------

def phase2_why(out: dict, impact: "Impact | None", age: float | None) -> str:
    """Why the impacted-only selection would not be safe for this change
    (L1211): the plan then runs the full suite rather than decide on a doubt."""
    if impact is not None and impact.removed:
        return f"file removed: {impact.removed[0]}"
    if not out["sure"]:
        return out["unsure"][0] if out["unsure"] else "selection unsure"
    if age is None:
        return "map without a generation date"
    if age > PHASE2_MAX_AGE_H:
        return f"map older than {PHASE2_MAX_AGE_H} h ({age:.0f} h)"
    if impact is None:
        return "selection unsure"
    if impact.uncovered:
        # #1339 : le bloc sans mesure ne force plus la suite complète dès
        # qu'un test charge son fichier ; sans importateur *prouvé*, si — un
        # import paresseux ou conditionnel ne prouve rien (Codex, B2 résiduel).
        return (f"{len(impact.uncovered)} file(s) with unmeasured blocks no test "
                f"is guaranteed to load: {impact.uncovered[0]}")
    if impact.foundation:
        return f"test infrastructure changed: {impact.foundation[0]}"
    return "selection unsafe"


def classify(modules: list[str], card: dict | None, now: datetime.datetime,
             base_fallback: str = "HEAD^1", head: str = "HEAD",
             repo: pathlib.Path = REPO, *, docs_only: bool = False) -> dict:
    """Classes of every module, and what the plan knows about its own safety."""
    always = read_list(ALWAYS_FILE, modules)
    problem, age = map_problem(card, now, head, repo)
    out = {"map": None, "sure": False, "phase2_safe": False, "phase2_why": "",
           "unsure": [], "contract": list(always),
           "untested": [], "unknown_blocks": [], "foundation": [], "removed": [],
           "importers": {}, "certified": {}, "uncovered": [], "changed": 0}
    if card is not None:
        out["map"] = {"commit": card.get("commit"), "generated": card.get("genere"),
                      "age_h": None if age is None else round(age, 1),
                      "used": not problem, "problem": problem}
    impact = None
    if not problem:
        # A documentation-only range has already been checked by nature().
        # CI changes on its base do not belong to that range; use the map to
        # find readers of its documents, not to rejudge the base's code.
        diff_base = base_fallback if docs_only else card["commit"]
        changes = changed_files(diff_base, head, repo)
        if changes is None:
            kind = "documentation base" if docs_only else "map commit"
            problem = f"diff from {kind} {diff_base[:12]} unreadable"
            out["map"]["used"], out["map"]["problem"] = False, problem
        else:
            # #1314 : le range propre de la branche (base de fusion → tête)
            # distingue ses changements de ceux déjà portés par la cible. Pour
            # la voie documentation, `changes` est déjà ce range ; s'il est
            # illisible ou non fiable (HEAD^1 hors commit de fusion), le doute
            # du socle est conservé (repli sûr).
            own_paths = ({path for _, path in changes} if docs_only
                         else own_changes(base_fallback, head, repo))
            try:
                impact = judge(card, changes, modules, head, repo, own=own_paths,
                               own_base=base_fallback)
                if docs_only:
                    inherited = changed_files(card["commit"], base_fallback, repo)
                    if inherited is None:
                        raise ValueError("documentation base diff unreadable")
                    baseline = judge(card, inherited, modules, base_fallback, repo)
                    # Keep readers that changed on the base: the old map may
                    # not yet record their new documentation dependencies.
                    impact.p0.update(baseline.p0)
                    for module, reason in baseline.p1.items():
                        impact.p1.setdefault(module, reason)
                    infrastructure = {f"test infrastructure changed: {path}"
                                      for path in baseline.foundation}
                    for reason in baseline.unsure:
                        if reason not in infrastructure:
                            impact.doubt(reason)
                    if baseline.unknown_blocks or baseline.untested:
                        impact.doubt("documentation base contains unmeasured code")
            except Exception as exc:  # any doubt: fallback, never a failed plan
                problem = f"map unusable ({type(exc).__name__}: {exc})"
                out["map"]["used"], out["map"]["problem"] = False, problem
    if impact is None:
        changes = changed_files(base_fallback, head, repo)
        classes = parts.classify(modules, [p for _, p in changes],
                                ambigus=parts.ambiguous_names()) if changes is not None else {}
        out["unsure"] = [problem or "no map"]
    else:
        classes = {}
        p1 = dict(impact.p1)
        for m, r in textual(modules, impact.textual).items():
            p1.setdefault(m, r)
        for m in modules:
            if m in impact.p0:
                classes[m] = ("P0", impact.p0[m])
            elif m in p1:
                classes[m] = ("P1", p1[m])
        out.update(unsure=impact.unsure, untested=impact.untested,
                   unknown_blocks=impact.unknown_blocks, foundation=impact.foundation,
                   removed=impact.removed, importers=impact.importers,
                   certified=impact.certified, uncovered=impact.uncovered,
                   sure=not impact.unsure)
        out["phase2_safe"] = (out["sure"] and age is not None and age <= PHASE2_MAX_AGE_H
                              and not impact.uncovered and not impact.foundation
                              and not impact.removed)
    changes = changes or []
    out["changed"] = len(changes)
    out["phase2_why"] = "" if out["phase2_safe"] else phase2_why(out, impact, age)
    triggered = always_hits(changes, always)
    #: #749 : la liste declenchee est publiee pour que le pilote psql d'une PR
    #: la rejoigne (make_plan), au lieu d'attendre la suite complete de main.
    out["always"] = sorted(triggered)
    for m, r in triggered.items():
        if classes.get(m, ("P3",))[0] not in ("P0", "P1"):
            classes[m] = ("P1", r)
    for m, r in recent_failures(card, now).items():
        if m in modules and classes.get(m, ("P3",))[0] == "P3":
            classes[m] = ("P2", r)
    out["classes"] = {m: list(classes.get(m, ("P3", ""))) for m in modules}
    return out


def weights(modules: list[str], driver: str, card: dict | None) -> dict[str, float]:
    table = None
    if card:
        table = {}
        for module, info in card.get("modules", {}).items():
            # L148 stage 6 : `durations` (anglais) puis l'ancienne `durees`.
            par_pilote = info.get("durations")
            if par_pilote is None:
                par_pilote = info.get("durees", {})
            for d, s in par_pilote.items():
                table.setdefault(d, {})[module] = s
        if not table.get(driver):
            table = None
    weighed, _ = parts.weight(modules, driver, table)
    return weighed


def split(modules: list[str], driver: str, n: int, card: dict | None,
          classes: dict[str, tuple[str, str]]) -> list[dict]:
    if not modules:
        return []
    weighed = weights(modules, driver, card)
    result = []
    for i, part in enumerate(parts.split(weighed, n), 1):
        if part:
            result.append({"part": i, "of": n, "label": f"{driver} {i}/{n}",
                           "modules": parts.order(part, weighed, classes),
                           "estimate_s": round(sum(weighed[m] for m in part), 1)})
    return result


def _branch(ref: str | None) -> str:
    """A branch name, from `GITHUB_BASE_REF`/`GITHUB_HEAD_REF` or `refs/heads/…`."""
    ref = ref or ""
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def _env_ref(name: str, value: str | None) -> str:
    """The explicit branch ref, or the environment's (`GITHUB_*`)."""
    return os.environ.get(name, "") if value is None else value


def release_pr(base_ref: str | None = None, head_ref: str | None = None) -> str:
    """Why this pull request is a release one (canon §3), or "".

    A source or target branch under `release/**` or `integration/**` is a
    release PR: the full suite, both drivers, no version/documentation fast
    lane and no psql pilot list — the target of a lot PR, `main`, keeps the
    selection. The reason goes to the plan and the summary.
    """
    for role, ref in (("target", _branch(base_ref)), ("source", _branch(head_ref))):
        if ref.startswith(RELEASE_PREFIXES):
            return (f"{role} branch {ref}: release/** and integration/** run the "
                    f"full suite (canon §3)")
    return ""


def lot_selection(event: str, state: dict, modules: list[str],
                  classes: dict[str, tuple[str, str]],
                  release: str = "") -> tuple[list[str] | None, str]:
    """Modules a lot branch runs (L1211): impacted plus contract.

    `None` means the full suite: an event other than a pull request, a
    release pull request (`release`, `release_pr`), or any doubt the plan
    cannot decide through — no usable map, a selection that is not safe
    (stale map, unmeasured blocks no test is guaranteed to load, test
    infrastructure, a removed file), an empty contract list. The reason goes
    to the summary.
    """
    if event != "pull_request":
        return None, "main, release/** and the integration branch run the full suite"
    if release:
        return None, release
    contract = [m for m in state.get("contract", []) if m in modules]
    if not contract:
        return None, "contract list (tests/parts/toujours.txt) empty or unreadable"
    if not (state.get("map") and state["map"].get("used")):
        unsure = state.get("unsure") or ["no map"]
        return None, f"map not usable ({unsure[0]})"
    if not state.get("phase2_safe"):
        return None, f"selection unsure ({state.get('phase2_why') or 'no reason'})"
    return sorted({m for m in modules if classes[m][0] in ("P0", "P1")}
                  | set(contract)), ""


def lot_part_count(driver: str, modules: list[str], card: dict | None) -> int:
    """Parts of a lot branch for `driver` (L1211): one, plus one per
    `PR_TARGET_PART_S` of estimate; `make_plan` caps it at `PARTS`."""
    if not modules:
        return 1
    estimate = sum(weights(modules, driver, card).values())
    return max(1, math.ceil(estimate / PR_TARGET_PART_S))


#: the branch whose documentation-only pushes take the fast lane (L161a)
DOC_PUSH_REF = "main"
#: git's null object id: `github.event.before` of a branch created by the push
NULL_SHA = "0" * 40


def pushed_range(base: str, head: str, repo: pathlib.Path = REPO) -> bool:
    """True if `base` is the tip before a push to `head` (fast-forward).

    `github.event.before` on a branch created by the push is the null sha,
    and a rewritten history leaves a tip that is not an ancestor: both keep
    the full suite."""
    if base in ("", NULL_SHA) or not re.fullmatch(r"[0-9a-fA-F]{40}", base):
        return False
    return is_ancestor(base, head, repo)


def nature(event: str, base: str = "HEAD^1", head: str = "HEAD",
           repo: pathlib.Path = REPO, ref: str = "") -> dict:
    """Version-only pull request (L157), documentation-only change (L161a).

    On a push, `base` is the tip before the push (`github.event.before`), so
    the diff covers every pushed commit. Only a push to `main` takes the
    documentation lane: a push to a release branch keeps the full suite, and
    version-only stays a pull-request rule (a pushed version always runs)."""
    out = {"version_only": False, "docs_only": False, "why": "not a pull request"}
    if event == "push":
        if ref.removeprefix("refs/heads/") != DOC_PUSH_REF:
            out["why"] = f"push to {ref or 'an unknown ref'}: full suite"
            return out
        if not pushed_range(base, head, repo):
            out["why"] = ("pushed range unknown (new branch or rewritten history): "
                          "full suite")
            return out
    elif event != "pull_request":
        return out
    try:
        names = _git(repo, "diff", "--name-only", "--no-renames", base, head).splitlines()
        patch = _git(repo, "diff", "-U0", base, head, "--", version_only.VERSION_FILE)
    except (OSError, subprocess.CalledProcessError) as exc:
        out["why"] = f"diff unreadable ({exc})"
        return out
    version_why = ""
    if event == "pull_request":
        out["version_only"], version_why = version_only.version_only(names, patch)
    out["docs_only"], doc_why = version_only.docs_only(names)
    out["why"] = version_why if out["version_only"] else doc_why
    out["files"] = names
    return out


# -- paquet : une seule suite par tête (canon §12) --------------------------
#
# Une poussée sur la branche d'intégration d'un paquet et la PR ouverte sur
# cette branche déclenchaient deux suites complètes, deux runs portant le même
# check requis `tests` sur la même tête : annuler l'un laissait l'agrégat rouge
# de l'autre (incident #1342, PR #1050 bloquée par le `tests` du run push
# annulé). tests.yml ne se déclenche plus sur `push integration/**` ; le
# portier .github/workflows/paquet.yml lit les PR ouvertes et n'appelle la
# suite que si aucune ne couvre la tête exacte. La décision ne regarde jamais
# un run : aucune réussite n'est déduite de l'existence d'un autre run.

#: préfixe des branches d'intégration de paquet (canon §12)
PACKAGE_PREFIX = "integration/"
#: la base d'une PR de paquet (canon §12)
PR_BASE = "main"
#: une PR dont la source ou la cible est sous ces préfixes est une PR de
#: release : elle déclenche tests.yml (canon §3)
PR_BASE_PREFIXES = ("release/", "integration/")


def pr_base_covered(base: str | None) -> bool:
    """True si une PR sur cette base déclenche tests.yml (canon §3, §12)."""
    base = (base or "").strip()
    return base == PR_BASE or base.startswith(PR_BASE_PREFIXES)


def couverture_decision(pulls, sha: str) -> dict:
    """La PR ouverte qui couvre exactement la tête `sha`, sinon rien.

    Ne lit que des PR ouvertes : aucune lecture de run, donc aucune réussite
    déduite d'un autre run. Une PR dont la tête n'est pas ce commit exact ne
    couvre pas la poussée (une nouvelle tête reprend sa décision), et une PR
    dont la base ne déclenche pas tests.yml ne couvre rien : dans tous les
    autres cas la suite tourne (repli sûr).
    """
    sha = (sha or "").strip()
    same_head = []
    for pr in pulls or []:
        if (pr.get("state") or "open") != "open":
            continue
        head = ((pr.get("head") or {}).get("sha") or "").strip()
        if head != sha:
            continue
        same_head.append(pr)
        base = (pr.get("base") or {}).get("ref") or ""
        if not pr_base_covered(base):
            continue
        number = pr.get("number")
        return {"covered": True, "pr": number,
                "why": f"PR #{number} (base {base}) couvre la tête {sha[:12]}"}
    if same_head:
        pr = same_head[0]
        base = (pr.get("base") or {}).get("ref") or "?"
        return {"covered": False, "pr": None,
                "why": (f"PR #{pr.get('number')} est sur cette tête mais sa base "
                        f"{base} ne déclenche pas tests.yml : la suite tourne")}
    if pulls:
        return {"covered": False, "pr": None,
                "why": "PR ouvertes mais aucune sur cette tête exacte : la suite tourne"}
    return {"covered": False, "pr": None,
            "why": "aucune PR ouverte sur cette branche : la suite tourne"}


def pulls_open(repo: str, branch: str, api: str, token: str, opener=None) -> list:
    """Les PR ouvertes dont la tête est `branch` (API GitHub, lecture seule).

    `head=owner:branch` filtre côté serveur ; 100 par page suffit pour une
    branche d'intégration, et une lecture incomplète fait tourner la suite
    (jamais l'inverse). Une erreur d'API ou de réseau remonte à l'appelant,
    qui la traduit en repli sûr.
    """
    owner = repo.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}",
                                    "per_page": "100"})
    url = f"{api.rstrip('/')}/repos/{repo}/pulls?{query}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ameesh-tests-plan",
    })
    lire = opener or urllib.request.urlopen
    with lire(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"payload inattendu ({type(data).__name__})")
    return data


def couverture_texte(out: dict, ref: str) -> str:
    """Le résumé lisible de la décision, pour le journal du portier."""
    etat = "couverte" if out["covered"] else "non couverte"
    suite = ("la suite complète est portée par le run `pull_request` de tests.yml"
             if out["covered"] else
             "la suite complète tourne dans ce run (`paquet.yml`)")
    return f"poussée {ref} : {etat} — {out['why']}\n{suite}\n"


def couverture(args) -> int:
    """Le portier `paquet.yml` : la poussée est-elle couverte par une PR ?

    Repli sûr : contexte incomplet, ref hors `integration/**`, API illisible
    ou non listée — la suite tourne, jamais l'inverse. Sortie toujours 0 :
    l'échec du portier ne doit pas supprimer la validation de la branche.
    """
    out = {"covered": False, "pr": None, "why": ""}
    branch = (args.ref or "").removeprefix("refs/heads/")
    if not (args.repo and branch and args.sha):
        out["why"] = "contexte incomplet (repo, ref ou sha) : la suite tourne"
    elif not branch.startswith(PACKAGE_PREFIX):
        out["why"] = f"poussée hors {PACKAGE_PREFIX}** : rien à dédupliquer"
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        try:
            pulls = pulls_open(args.repo, branch, args.api, token)
            out = couverture_decision(pulls, args.sha)
        except Exception as exc:  # noqa: BLE001 - repli sûr : la suite tourne
            out = {"covered": False, "pr": None,
                   "why": (f"PR illisibles ({type(exc).__name__}: {exc}) : "
                           "la suite tourne")}
    text = couverture_texte(out, args.ref or branch)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"covered={'true' if out['covered'] else 'false'}\n")
            fh.write(f"covered_pr={out['pr'] if out['pr'] is not None else ''}\n")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Couverture de la poussée\n\n{text}")
    print(text, end="")
    return 0


def doc_readers(classes: dict, card: dict,
                modules: list[str]) -> tuple[list[str], list[str]]:
    """Readers of a documentation-only change, judged with a usable map: the
    declared ones (`tests/parts/lecteurs-doc.txt`) plus every P0 or P1 module
    of the plan (data readers, listers of a directory where a document
    appeared or vanished); and those of them that need the database, which
    the doc-readers job (no Postgres) cannot run."""
    declared = {n.split(".", 1)[1] for n in version_only.doc_readers()}
    measured = {m for m, c in classes.items() if c[0] in ("P0", "P1")}
    # a P0 module is unknown to the map: whether it needs the database is unknown too
    db = sorted(m for m in measured - declared if m in modules
                and (m in _db_modules(card) or classes[m][0] == "P0"))
    return sorted(m for m in declared | measured if m in modules), db


def make_plan(event: str, card: dict | None, now: datetime.datetime | None = None,
              head: str = "HEAD", repo: pathlib.Path = REPO,
              base: str = "HEAD^1", ref: str = "",
              base_ref: str | None = None, head_ref: str | None = None) -> dict:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    base_ref = _env_ref("GITHUB_BASE_REF", base_ref)
    head_ref = _env_ref("GITHUB_HEAD_REF", head_ref)
    release = release_pr(base_ref, head_ref) if event == "pull_request" else ""
    modules = parts.modules()
    plan = {"schema": PLAN_SCHEMA, "event": event, "generated": now.isoformat(timespec="seconds"),
            "head": os.environ.get("AMEESH_CI_TETE") or _rev(head, repo),
            "tested": _rev(head, repo), "release": release,
            "refs": {"base": base_ref, "head": head_ref},
            **nature(event, base=base, head=head, repo=repo, ref=ref)}
    plan.update(classify(modules, card, now, base_fallback=base, head=head, repo=repo,
                         docs_only=bool(plan["docs_only"] and not release)))
    classes = {m: tuple(c) for m, c in plan["classes"].items()}
    #: #749 : modules « always » declenches par le diff (contrat de base, baux
    #: et orphelins), a rejoindre au pilote psql sur une PR.
    always = [m for m in plan.get("always", []) if m in modules]
    plan["doc_readers"] = []
    if plan["docs_only"] and not release:
        # The fast lane needs a usable map and a sure judgement of the actual
        # documentation range. Missing readers or a bad map keep the full suite.
        full = None
        if not (plan["map"] and plan["map"]["used"]):
            reason = next(iter(plan["unsure"]), "map unusable")
            full = f"documentation, map not usable ({reason}): full suite"
        elif not plan["sure"]:
            reason = next(iter(plan["unsure"]), "selection unsure")
            full = f"documentation, selection unsure ({reason}): full suite"
        else:
            plan["doc_readers"], db_readers = doc_readers(plan["classes"], card, modules)
            if db_readers:  # doc-readers has no Postgres: the full parts run them
                full = (f"documentation read by database tests "
                        f"({', '.join(db_readers)}): full suite")
        if full:
            plan["docs_only"], plan["why"], plan["doc_readers"] = False, full, []
    if release:
        #: canon §3 : une PR de release passe la suite complète, les deux
        #: pilotes, sans voie version seule ni documentation seule.
        plan["version_only"] = plan["docs_only"] = False
        plan["doc_readers"] = []
        plan["why"] = release
    skip = plan["version_only"] or plan["docs_only"]
    pr = event == "pull_request"
    #: L1211 : la sélection impactés+contrat est réservée aux PR de lot ; une
    #: PR de release garde la suite complète, y compris dans le repli.
    lot_pr = pr and not release
    #: L1211 : une branche de lot ne lance que les impactés et le contrat ;
    #: tout doute rend la suite complète (lot_selection). La sélection se
    #: contente d'une part par pilote tant que l'estimation le permet.
    selection, why = ((None, "") if skip
                      else lot_selection(event, plan, modules, classes, release))
    chosen_set = None if selection is None else set(selection)
    contract = [m for m in plan.get("contract", []) if m in modules]
    plan["parts"] = {}
    for driver, (n_pr, n_other) in PARTS.items():
        selected = modules
        if driver == "psql" and lot_pr:
            selected = psql_pr_modules(read_list(PILOT_FILE, modules, required=True),
                                       always)
        if chosen_set is not None:
            selected = [m for m in selected if m in chosen_set]
        n = n_pr if lot_pr else n_other
        if chosen_set is not None:
            n = min(n, lot_part_count(driver, selected, card))
        plan["parts"][driver] = [] if skip else split(selected, driver, n, card, classes)
    every = [m for m in modules]
    weighed = weights(every, "psycopg", card)
    chosen = [m for m in every if classes[m][0] in ("P0", "P1")]
    ran = sorted({m for ps in plan["parts"].values() for p in ps for m in p["modules"]})
    plan["selection"] = {
        "mode": "skipped" if skip else ("impacted" if chosen_set is not None else "full"),
        "why": plan["why"] if skip else why,
        "modules": len(every), "impacted": len(chosen), "contract": len(contract),
        "selected": len(ran), "skipped": len(every) - len(ran),
        "impacted_estimate_s": round(sum(weighed[m] for m in chosen), 1),
        "total_estimate_s": round(sum(weighed.values()), 1)}
    return plan


def _rev(rev: str, repo: pathlib.Path) -> str:
    try:
        return _git(repo, "rev-parse", rev).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def fallback_plan(event: str, error: str, base_ref: str | None = None,
                  head_ref: str | None = None) -> dict:
    """The plan when planning itself failed: full suite, alphabetical order."""
    base_ref = _env_ref("GITHUB_BASE_REF", base_ref)
    head_ref = _env_ref("GITHUB_HEAD_REF", head_ref)
    release = release_pr(base_ref, head_ref) if event == "pull_request" else ""
    pr = event == "pull_request"
    #: L1211 : le repli d'une PR de release garde les deux pilotes complets,
    #: jamais la liste pilote psql d'une PR de lot.
    lot_pr = pr and not release
    why = "planning failed" + (f"; {release}" if release else "")
    modules = parts.modules()
    plan = {"schema": PLAN_SCHEMA, "event": event, "head": "", "tested": "",
            "version_only": False, "docs_only": False, "why": why,
            "release": release, "refs": {"base": base_ref, "head": head_ref},
            "map": None, "sure": False, "phase2_safe": False, "phase2_why": "",
            "unsure": [f"planning failed ({error}); full suite"], "untested": [],
            "unknown_blocks": [], "foundation": [], "removed": [], "changed": 0,
            "importers": {}, "certified": {}, "uncovered": [],
            "doc_readers": [],
            "contract": [], "classes": {m: ["P3", ""] for m in modules}, "parts": {}}
    for driver, (n_pr, n_other) in PARTS.items():
        n = n_pr if lot_pr else n_other
        # no module list: scripts/test-parts.py splits the suite itself (L116)
        plan["parts"][driver] = [
            {"part": i, "of": n, "label": f"{driver} {i}/{n}", "modules": None,
             "pilot_list": driver == "psql" and lot_pr, "estimate_s": 0}
            for i in range(1, n + 1)]
    plan["selection"] = {"mode": "full", "why": why, "modules": len(modules),
                         "impacted": 0, "contract": 0, "selected": len(modules), "skipped": 0,
                         "impacted_estimate_s": 0, "total_estimate_s": 0}
    return plan


def outputs(plan: dict) -> dict[str, str]:
    """Job outputs (`$GITHUB_OUTPUT`) read by the parts through `fromJSON`."""
    out = {"version_only": str(plan["version_only"]).lower(),
           "docs_only": str(plan["docs_only"]).lower(),
           "doc_readers": " ".join(f"tests.{m}" for m in plan["doc_readers"]),
           "code": str(not (plan["version_only"] or plan["docs_only"])).lower()}
    for driver, ps in plan["parts"].items():
        out[driver] = json.dumps([p["part"] for p in ps])
        out[f"{driver}_parts"] = str(ps[0]["of"] if ps else 0)
    return out


def annotations(plan: dict) -> list[str]:
    sha = ((plan.get("map") or {}).get("commit") or "")[:12]
    return [f"::notice file={u['file']},line={u['lines'][0]},endLine={u['lines'][1]},"
            f"title=Changed lines without a test::{u['block']} changed and no test "
            f"executes it (map {sha})" for u in plan.get("untested", [])]


def _listing(items: list[str]) -> str:
    shown = ", ".join(items[:SUMMARY_LIMIT])
    return shown + (f" … and {len(items) - SUMMARY_LIMIT} more" if len(items) > SUMMARY_LIMIT
                    else "")


def summary(plan: dict) -> str:
    lines = ["### Test plan", ""]
    m = plan.get("map")
    if m is None:
        lines.append("* map: none (full suite, L161b order)")
    else:
        state = "used" if m["used"] else f"not used: {m['problem']}"
        lines.append(f"* map `{(m['commit'] or '')[:12]}`, {m['age_h']} h old: {state}")
    if plan["version_only"] or plan["docs_only"]:
        lines.append(f"* {plan['why']}: parts skipped"
                     + (f"; doc readers: {', '.join(plan['doc_readers'])}"
                        if plan["docs_only"] else ""))
    sel = plan["selection"]
    lines.append(f"* {plan['changed']} file(s) changed since the "
                 f"{'map commit' if m and m['used'] else 'parent commit'}; impacted (P0, P1): "
                 f"{sel['impacted']} of {sel['modules']} modules, ~{sel['impacted_estimate_s']:.0f}"
                 f" s of ~{sel['total_estimate_s']:.0f} s (psycopg)")
    if sel.get("mode") == "impacted":
        lines.append(f"* lot branch (L1211): impacted + {sel['contract']} contract module(s), "
                     f"{sel['selected']} module(s) run, {sel['skipped']} left to main, "
                     f"release/** and the integration branch")
    elif sel.get("mode") == "skipped":
        lines.append("* parts skipped (see above)")
    else:
        lines.append(f"* full suite: {sel['selected']} module(s) run"
                     + (f" ({sel['why']})" if sel.get("why") else ""))
    lines.append(f"* selection sure: {'yes' if plan['sure'] else 'no'}; impacted-only safe: "
                 f"{'yes' if plan['phase2_safe'] else 'no'}"
                 + (f" ({plan['phase2_why']})" if plan.get("phase2_why") else ""))
    for reason in plan["unsure"][:SUMMARY_LIMIT]:
        lines.append(f"  * {reason}")
    by_class = collections.defaultdict(list)
    for mod, (cls, reason) in plan["classes"].items():
        by_class[cls].append(f"`{mod}` ({reason})" if reason else f"`{mod}`")
    for cls in ("P0", "P1", "P2"):
        if by_class[cls]:
            lines.append(f"* {cls}: {_listing(by_class[cls])}")
    if plan["untested"]:
        lines.append("* **changed lines without a test**: " + _listing(
            [f"`{u['file']}::{u['block']}` (lines {u['lines'][0]}-{u['lines'][1]})"
             for u in plan["untested"]]))
    if plan["unknown_blocks"]:
        lines.append("* new blocks, unknown to the map: " + _listing(
            [f"`{b}`" for b in plan["unknown_blocks"]]))
    if plan.get("certified"):
        lines.append("* unmeasured blocks covered by their importers: " + _listing(
            [f"`{f}` ({len(ms)} module(s))" for f, ms in sorted(plan["certified"].items())]))
    if plan.get("uncovered"):
        files = []
        for f in plan["uncovered"]:
            candidates = plan.get("importers", {}).get(f, [])
            files.append(f"`{f}`" + (f" (candidates: {', '.join(f'`{m}`' for m in candidates)})"
                                     if candidates else ""))
        lines.append("* **unmeasured blocks no test is guaranteed to load** (full suite): "
                     + _listing(files))
    return "\n".join(lines) + "\n"


# -- leaks ----------------------------------------------------------------

def _read_stream(path: str) -> list[dict]:
    events = []
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _ts(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def leaks(plan: dict, streams: list[str]) -> dict:
    """One run: failures the map did not select, and the provisional green."""
    classes = {m: c for m, c in plan.get("classes", {}).items()}
    expected = {p["label"] for ps in plan.get("parts", {}).values() for p in ps}
    failed, seen, starts, greens = {}, set(), [], {}
    context = {}
    for path in streams:
        for ev in _read_stream(path):
            part = ev.get("part", "")
            context = context or {k: ev.get(k) for k in ("depot", "tete", "fusion", "execution")}
            if ev.get("type") == "debut":
                seen.add(part)
                if _ts(ev.get("ts", "")):
                    starts.append(_ts(ev["ts"]))
            elif ev.get("type") == "impacted":
                greens[part] = ev
            elif ev.get("type") == "test" and ev.get("statut") in ("echec", "erreur"):
                failed.setdefault(ev.get("module", "?"), {"test": ev.get("test", ""),
                                                          "part": part})
    leaked = [{"module": m, "class": classes.get(m, ["P3", ""])[0], **info}
              for m, info in sorted(failed.items())
              if classes.get(m, ["P3", ""])[0] not in ("P0", "P1")]
    missing = sorted(expected - seen)
    if missing or set(greens) != expected:
        green = {"status": "incomplete", "missing": sorted(expected - set(greens))}
    else:
        status = "green" if all(e.get("statut") == "vert" for e in greens.values()) else "red"
        at = [_ts(e.get("ts", "")) for e in greens.values()]
        after = (max(at) - min(starts)).total_seconds() if starts and all(at) else None
        green = {"status": status, "after_s": None if after is None else round(after, 1)}
    m = plan.get("map") or {}
    return {"schema": LEAKS_SCHEMA, "event": plan.get("event"), "head": plan.get("head"),
            **context, "map_commit": m.get("commit"), "map_used": bool(m.get("used")),
            "sure": plan.get("sure"), "phase2_safe": plan.get("phase2_safe"),
            "selection": plan.get("selection"), "parts_missing": missing,
            "failed": sorted(failed), "leaks": leaked, "provisional_green": green}


def leaks_summary(record: dict) -> str:
    g = record["provisional_green"]
    lines = ["### Impacted tests: leaks and provisional green", ""]
    if g["status"] == "incomplete":
        lines.append(f"* provisional green: incomplete (no `impacted` event from "
                     f"{', '.join(g['missing']) or 'some parts'})")
    else:
        after = f" after {g['after_s']:.0f} s" if g.get("after_s") is not None else ""
        lines.append(f"* provisional green: P0 and P1 {g['status']}{after}")
    if not record["map_used"]:
        lines.append("* map not used: leaks are not counted for this run")
    elif record["leaks"]:
        lines.append("* **leaks** (failed, not selected by the map): " + ", ".join(
            f"`{x['module']}` ({x['class']}, {x['part']})" for x in record["leaks"]))
    else:
        lines.append(f"* no leak ({len(record['failed'])} failed module(s), all selected)")
    return "\n".join(lines) + "\n"


def tally(records: list[dict]) -> dict:
    """Leaks over many runs (lot L161i): phase 2 needs < 1 per 50 PRs."""
    counted = [r for r in records if r.get("map_used")]
    out = {}
    for event in ("pull_request", "push"):
        rs = [r for r in counted if r.get("event") == event]
        with_leak = [r for r in rs if r.get("leaks")]
        out[event] = {"runs": len(rs), "runs_with_leaks": len(with_leak),
                      "leaks": sum(len(r["leaks"]) for r in rs),
                      "per_50": round(50 * len(with_leak) / len(rs), 2) if rs else None,
                      "sure": sum(1 for r in rs if r.get("sure")),
                      "phase2_safe": sum(1 for r in rs if r.get("phase2_safe"))}
    out["not_counted"] = len(records) - len(counted)
    return out


# -- command line ---------------------------------------------------------

def _load_map(path: str | None) -> tuple[dict | None, str]:
    if not path or not os.path.exists(path):
        return None, f"{path or 'no path'}: absent"
    try:
        return map_mod.read(path), ""
    except (OSError, ValueError, EOFError) as exc:
        return None, f"{path}: {exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="classes and parts of this run")
    p.add_argument("--out", required=True)
    p.add_argument("--map", help="carte.json.gz (scripts/map.py)")
    p.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    p.add_argument("--base", default="HEAD^1",
                   help="tip before a push (github.event.before); HEAD^1 by default, "
                        "the first parent of a pull request's merge commit")
    p.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""),
                   help="github.ref; a documentation-only push takes the fast lane "
                        "on main only")
    p.add_argument("--base-ref", default=None,
                   help="target branch of a pull request (default: $GITHUB_BASE_REF)")
    p.add_argument("--head-ref", default=None,
                   help="source branch of a pull request (default: $GITHUB_HEAD_REF)")
    p.add_argument("--github-output", help="appends the job outputs to this file")
    p.add_argument("--summary", help="appends the Markdown summary to this file")
    p.add_argument("--annotations", action="store_true",
                   help="prints GitHub annotations for changed lines without a test")
    p = sub.add_parser("leaks", help="leaks and provisional green of one run")
    p.add_argument("plan")
    p.add_argument("streams", nargs="*")
    p.add_argument("--out")
    p.add_argument("--summary")
    p = sub.add_parser("tally", help="leaks over many runs")
    p.add_argument("records", nargs="+")
    p = sub.add_parser("couverture",
                       help="cette poussée de paquet est-elle couverte par une PR ouverte ?")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                   help="owner/repo (défaut : $GITHUB_REPOSITORY)")
    p.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""),
                   help="github.ref de la poussée (défaut : $GITHUB_REF)")
    p.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""),
                   help="tête poussée (défaut : $GITHUB_SHA)")
    p.add_argument("--api", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                   help="racine de l'API GitHub (défaut : $GITHUB_API_URL)")
    p.add_argument("--github-output", help="appends the job outputs to this file")
    p.add_argument("--summary", help="appends the Markdown summary to this file")
    args = ap.parse_args(argv)

    if args.command == "couverture":
        return couverture(args)
    if args.command == "plan":
        card, why = _load_map(args.map)
        if card is None and args.map:
            print(f"map: {why}", file=sys.stderr)
        try:
            plan = make_plan(args.event, card, base=args.base, ref=args.ref,
                             base_ref=args.base_ref, head_ref=args.head_ref)
        except Exception as exc:  # planning must never block the suite
            print(f"plan: {type(exc).__name__}: {exc}; fallback plan", file=sys.stderr)
            plan = fallback_plan(args.event, f"{type(exc).__name__}: {exc}",
                                 base_ref=args.base_ref, head_ref=args.head_ref)
        pathlib.Path(args.out).write_text(json.dumps(plan, indent=1, ensure_ascii=False)
                                          + "\n", encoding="utf-8")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as fh:
                for k, v in outputs(plan).items():
                    fh.write(f"{k}={v}\n")
        text = summary(plan)
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(text)
        print(text, end="")
        if args.annotations:
            for line in annotations(plan):
                print(line)
        return 0
    if args.command == "leaks":
        try:
            plan = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"plan {args.plan} unreadable ({exc}): no leak count", file=sys.stderr)
            return 0
        record = leaks(plan, args.streams)
        if args.out:
            pathlib.Path(args.out).write_text(json.dumps(record, indent=1) + "\n",
                                              encoding="utf-8")
        text = leaks_summary(record)
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(text)
        print(text, end="")
        return 0
    records = []
    for path in args.records:
        try:
            records.append(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            print(f"{path}: unreadable, ignored", file=sys.stderr)
    print(json.dumps(tally(records), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
