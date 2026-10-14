#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#707 : lint d'herméticité des tests.

Repère, par analyse statique (AST, aucun import des tests), les schémas qui
rendent un test dépendant de son environnement — horloge murale, état global
de la machine, calendrier. Règle du canon §3 (bureau, 13/10/2026) : un test
qui dépend de l'environnement (horloge, mtime, système de fichiers, machine)
est un défaut bloquant, à corriger à la source et non à relancer.

Règles :
  H1 horloge-reelle-assertion
     une assertion (`assertX(...)` ou `assert`) mesure une durée avec
     l'horloge réelle (`time.time() - début`, `datetime.now()`… soustrait) :
     le verdict dépend de la durée réelle du passage, donc de la charge.
  H2 etat-global-machine
     le test lit l'état global de l'hôte (`platform.process_table`,
     `psutil.process_iter|pids|cpu_count|getloadavg`, `os.cpu_count`,
     `os.getloadavg`, `os.sched_getaffinity`, un chemin `/proc/…`) : le
     verdict dépend de ce que fait la machine, donc du runner et des jobs
     voisins.
  H3 fixture-dependante-du-jour
     une fixture est construite avec le jour courant (`date.today`,
     `datetime.now|today|utcnow`) : bombe à retardement au changement de jour.

Exemption motivée : un commentaire `# hermeticity: allow — <raison>` sur
l'instruction visée, ou sur la ligne juste au-dessus, l'exempte ; la raison
reste visible du relecteur.

Usage :
  scripts/test-hermeticity-lint.py [FICHIERS…]        # défaut : tests/**/test_*.py
  scripts/test-hermeticity-lint.py --diff origin/main # seuls les tests changés
  scripts/test-hermeticity-lint.py --json
Sortie : 0 si aucun constat, 1 si au moins un, 2 en cas d'erreur d'usage.
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MARKER = "hermeticity: allow"

#: lectures de l'horloge réelle (nom canonique importé).
CLOCK = {
    "time.time", "time.time_ns", "time.monotonic", "time.monotonic_ns",
    "time.perf_counter", "time.perf_counter_ns", "time.process_time",
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
    "datetime.date.today",
}
#: lectures liées au jour courant (sous-ensemble calendaire de CLOCK).
CALENDAR = {
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
    "datetime.date.today",
}
#: lectures de l'état global de l'hôte.
MACHINE = {
    "psutil.process_iter", "psutil.pids", "psutil.cpu_count", "psutil.getloadavg",
    "os.cpu_count", "os.getloadavg", "os.sched_getaffinity",
    "platform.process_table", "platform.has_psutil",
}
#: appels qui reçoivent un chemin : un littéral `/proc/…` y est global.
PATH_CALLS = {"open", "os.listdir", "os.scandir", "os.stat", "os.readlink",
              "pathlib.Path", "Path", "io.open"}
RULES = {
    "H0": "source-illisible",
    "H1": "horloge-reelle-assertion",
    "H2": "etat-global-machine",
    "H3": "fixture-dependante-du-jour",
}


class Finding:
    __slots__ = ("path", "lineno", "col", "rule", "message", "function")

    def __init__(self, path: str, lineno: int, col: int, rule: str,
                 message: str, function: str) -> None:
        self.path = path
        self.lineno = lineno
        self.col = col
        self.rule = rule
        self.message = message
        self.function = function

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.lineno, "column": self.col,
                "rule": self.rule, "name": RULES[self.rule],
                "message": self.message, "function": self.function}

    def render(self) -> str:
        return "%s:%d:%d: %s %s — %s" % (self.path, self.lineno, self.col,
                                         self.rule, RULES[self.rule], self.message)


def _aliases(tree: ast.AST) -> dict[str, str]:
    """nom local -> nom canonique, pour `import x as y` et `from x import y`."""
    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                alias[name.asname or name.name.split(".")[0]] = name.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                alias[name.asname or name.name] = "%s.%s" % (node.module, name.name)
    return alias


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` pour une chaîne d'attributs/noms, None sinon."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _canonical(node: ast.AST, alias: dict[str, str]) -> str | None:
    dotted = _dotted(node)
    if dotted is None:
        return None
    head, _, tail = dotted.partition(".")
    base = alias.get(head)
    if base is None:
        return dotted
    return base + ("." + tail if tail else "")


def _match(name: str | None, table: set[str]) -> str | None:
    """le nom canonique, ou son suffixe `module.attribut`, s'il est dans `table`."""
    if not name:
        return None
    if name in table:
        return name
    short = ".".join(name.split(".")[-2:])
    return short if short in table else None


def _called(node: ast.AST, alias: dict[str, str], table: set[str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return _match(_canonical(node.func, alias), table)


def _proc_literal(node: ast.AST) -> bool:
    """un littéral `/proc/…` (même composé : `%`, f-string) passé à un appel."""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func) or ""
    if name not in PATH_CALLS and name.rsplit(".", 1)[-1] not in PATH_CALLS:
        return False
    for arg in list(node.args) + [kw.value for kw in node.keywords]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and "/proc" in sub.value:
                return True
    return False


def _in_subtraction(call: ast.AST, root: ast.AST) -> bool:
    """le call est un opérande (direct ou imbriqué) d'une soustraction sous `root`."""
    for sub in ast.walk(root):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub):
            for side in (sub.left, sub.right):
                if any(x is call for x in ast.walk(side)):
                    return True
    return False


def _exempt(node: ast.AST, lines: list[str]) -> bool:
    """marqueur sur l'instruction englobante ou la ligne juste au-dessus."""
    top: ast.AST = node
    start, end = getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)
    for line in lines[max(0, start - 2):end]:
        if MARKER in line:
            return True
    return False


def _functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node))
    return out


def _enclosing(funcs: list[tuple[str, ast.AST]], node: ast.AST) -> str:
    best, best_span = "<module>", None
    for name, func in funcs:
        if func.lineno <= getattr(node, "lineno", 0) <= getattr(func, "end_lineno", 0):
            span = func.end_lineno - func.lineno
            if best_span is None or span < best_span:
                best, best_span = name, span
    return best


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
        and node.func.attr.startswith("assert")


def lint_source(path: str, source: str) -> list[Finding]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, exc.offset or 0, "H0",
                        "source illisible : %s" % exc.msg, "<module>")]
    alias = _aliases(tree)
    lines = source.splitlines()
    funcs = _functions(tree)
    findings: list[Finding] = []
    clock_lines: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assert, ast.Call)) and _is_assertion(node):
            body = node.test if isinstance(node, ast.Assert) else node
            for sub in ast.walk(body):
                name = _called(sub, alias, CLOCK)
                if name and _in_subtraction(sub, body) and not _exempt(node, lines):
                    clock_lines.add(id(sub))
                    findings.append(Finding(
                        path, sub.lineno, sub.col_offset + 1, "H1",
                        "durée mesurée à l'horloge réelle dans une assertion : %s()" % name,
                        _enclosing(funcs, sub)))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called(node, alias, MACHINE)
        if name and not _exempt(node, lines):
            findings.append(Finding(path, node.lineno, node.col_offset + 1, "H2",
                                    "état global de la machine : %s()" % name,
                                    _enclosing(funcs, node)))
        elif _proc_literal(node) and not _exempt(node, lines):
            findings.append(Finding(path, node.lineno, node.col_offset + 1, "H2",
                                    "état global de la machine : chemin /proc/…",
                                    _enclosing(funcs, node)))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in clock_lines:
            continue
        name = _called(node, alias, CALENDAR)
        if name and not _exempt(node, lines):
            findings.append(Finding(path, node.lineno, node.col_offset + 1, "H3",
                                    "fixture dépendante du jour : %s()" % name,
                                    _enclosing(funcs, node)))
    return sorted(findings, key=lambda f: (f.lineno, f.col, f.rule))


def default_files(repo: pathlib.Path) -> list[str]:
    return [str(p.relative_to(repo)) for p in sorted(repo.glob("tests/**/test_*.py"))]


def diff_files(repo: pathlib.Path, ref: str) -> list[str]:
    cmd = ["git", "diff", "--name-only", "--diff-filter=ACMR", "%s...HEAD" % ref,
           "--", "tests"]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("git diff %s...HEAD a échoué : %s"
                         % (ref, proc.stderr.strip()[:200]))
    return [line for line in proc.stdout.splitlines()
            if line.endswith(".py") and pathlib.PurePath(line).name.startswith("test_")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="test-hermeticity-lint.py",
        description="lint d'herméticité des tests (#707) : horloge, machine, calendrier")
    parser.add_argument("files", nargs="*", help="fichiers à analyser")
    parser.add_argument("--diff", metavar="REF",
                        help="n'analyser que les tests changés depuis REF (git diff REF...HEAD)")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    args = parser.parse_args(argv)
    if args.files and args.diff:
        print("donnez des FICHIERS ou --diff, pas les deux", file=sys.stderr)
        return 2
    try:
        files = args.files or (diff_files(REPO, args.diff) if args.diff
                               else default_files(REPO))
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    findings: list[Finding] = []
    for rel in files:
        path = REPO / rel
        if not path.is_file():
            continue
        findings.extend(lint_source(rel, path.read_text(encoding="utf-8")))
    findings.sort(key=lambda f: (f.path, f.lineno, f.col, f.rule))

    if args.json:
        print(json.dumps({"schema": "ameesh-hermeticity-lint/1",
                          "findings": [f.as_dict() for f in findings],
                          "summary": {"files": len(files), "findings": len(findings),
                                      "by_rule": {r: sum(1 for f in findings if f.rule == r)
                                                  for r in sorted(RULES)}}},
                         ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        by_rule = ", ".join("%s %d" % (r, sum(1 for f in findings if f.rule == r))
                            for r in sorted(RULES))
        print("%d constat(s) dans %d fichier(s) — %s"
              % (len(findings), len(files), by_rule))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
