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
missing, unreadable, older than 48 h, or not an ancestor of HEAD: the order
falls back to the textual heuristic of L161b (`--base`, `HEAD^1` by
default) and the plan is marked unsure. A file the map cannot judge (Python
outside `src/ameesh`, a non-documentation file no test reads, a syntax
error) uses the textual heuristic for that file and marks the plan unsure;
test infrastructure (`tests/support.py`, `tests/fakebin/`, the CI scripts,
`.github/`, `pyproject.toml` beyond its version line) marks it unsure too.
A file removed outside documentation marks it unsure as well: the map knows
the readers it measured, never the ones it cannot see (an import by a
computed name, a config read by the code).
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
#: saturé) ; la suite psql complète reste en six parts.
#: L1211 : ces nombres sont des plafonds. Une branche de lot ne lance que les
#: impactés et le contrat : une part par pilote suffit, et une part de plus par
#: PR_TARGET_PART_S d'estimation, sans jamais dépasser le plafond.
PARTS = {"psycopg": (4, 4), "psql": (3, 6)}
#: L1211 : budget nominal d'une part sur une branche de lot, en secondes. À
#: 2,5-3x sous saturation, 300 s tiennent dans le plafond de 30 min de psycopg
#: comme dans celui de 45 min de psql.
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
    return "", age


def is_doc(path: str) -> bool:
    return version_only.is_doc(path)


def is_foundation(path: str) -> bool:
    return path.startswith(FOUNDATION) or path == "pyproject.toml"


def read_list(path: pathlib.Path, known: list[str], required: bool = False) -> list[str]:
    """`parts.read_list`, whose SystemExit (stale or unsorted entry) becomes a
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

    def hit(self, modules, reason: str) -> None:
        for m in modules:
            self.p1.setdefault(m, reason)

    def doubt(self, reason: str, path: str | None = None) -> None:
        if reason not in self.unsure:
            self.unsure.append(reason)
        if path and path not in self.textual:
            self.textual.append(path)


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
          head: str = "HEAD", repo: pathlib.Path = REPO) -> Impact:
    """The impact of `changes` (since the map commit) on the test modules."""
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
            if path == "pyproject.toml" and _version_line_only(head, card["commit"], repo):
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
    if impact.unknown_blocks:
        return f"{len(impact.unknown_blocks)} new block(s) unknown to the map"
    if impact.foundation:
        return f"test infrastructure changed: {impact.foundation[0]}"
    if impact.untested:
        return f"{len(impact.untested)} changed block(s) without a test"
    return "selection unsafe"


def classify(modules: list[str], card: dict | None, now: datetime.datetime,
             base_fallback: str = "HEAD^1", head: str = "HEAD",
             repo: pathlib.Path = REPO) -> dict:
    """Classes of every module, and what the plan knows about its own safety."""
    always = read_list(ALWAYS_FILE, modules)
    problem, age = map_problem(card, now, head, repo)
    out = {"map": None, "sure": False, "phase2_safe": False, "phase2_why": "",
           "unsure": [], "contract": list(always),
           "untested": [], "unknown_blocks": [], "foundation": [], "removed": [],
           "changed": 0}
    if card is not None:
        out["map"] = {"commit": card.get("commit"), "generated": card.get("genere"),
                      "age_h": None if age is None else round(age, 1),
                      "used": not problem, "problem": problem}
    impact = None
    if not problem:
        changes = changed_files(card["commit"], head, repo)
        if changes is None:
            problem = f"diff from map commit {card['commit'][:12]} unreadable"
            out["map"]["used"], out["map"]["problem"] = False, problem
        else:
            try:
                impact = judge(card, changes, modules, head, repo)
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
                   removed=impact.removed, sure=not impact.unsure)
        out["phase2_safe"] = (out["sure"] and age is not None and age <= PHASE2_MAX_AGE_H
                              and not impact.unknown_blocks and not impact.foundation
                              and not impact.untested and not impact.removed)
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
    (stale map, a new block, test infrastructure, changed lines without a
    test), an empty contract list. The reason goes to the summary.
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
    plan.update(classify(modules, card, now, base_fallback=base, head=head, repo=repo))
    classes = {m: tuple(c) for m, c in plan["classes"].items()}
    #: #749 : modules « always » declenches par le diff (contrat de base, baux
    #: et orphelins), a rejoindre au pilote psql sur une PR.
    always = [m for m in plan.get("always", []) if m in modules]
    plan["doc_readers"] = []
    if plan["docs_only"] and not release:
        # the fast lane needs a map that judged everything: a missing, stale or
        # unusable map, or an unsure selection, runs the full suite
        full = None
        if not (plan["map"] and plan["map"]["used"]):
            full = f"documentation, map not usable ({plan['unsure'][0]}): full suite"
        elif not plan["sure"]:
            full = f"documentation, selection unsure ({plan['unsure'][0]}): full suite"
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
    args = ap.parse_args(argv)

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
