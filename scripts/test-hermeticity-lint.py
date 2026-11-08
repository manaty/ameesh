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
  H4 seuil-mural-sous-la-seconde
     une durée mesurée à l'horloge réelle, gardée dans une variable
     (`wake_s = time.monotonic() - début`), est comparée à une constante
     inférieure à 1 s : le verdict dépend de l'ordonnancement du runner, pas
     du code (#5302). L'assertion directe reste H1 ; H4 couvre la variable.
  H5 etat-disque
     le test lit le disque de l'hôte (`shutil.disk_usage`) : le verdict
     dépend du runner et des jobs voisins (#5321).
  H6 seuil-de-plan-sous-la-seconde
     un champ de temps d'un plan (`Execution Time`, `Planning Time` d'un
     `EXPLAIN (ANALYZE)`) est comparé à un seuil inférieur à une seconde
     (moins de 1 000 ms, ou moins de 1 s après conversion) : ce temps est
     celui du serveur sous la charge de l'hôte, pas une preuve du plan
     (#5607 : #5582 à 142 ms pour un plafond de 100 ms). La garde de plan se
     juge sur la structure (index, scans, lignes, boucles, tampons), le temps
     ne sert qu'en plafond large (≥ 1 s). Modèle sans seuil de temps :
     tests/test_5555_gauge_plans.py.
  H7 remplissage-volumineux-hors-fill
     un `INSERT … generate_series` d'au moins 10 000 lignes estimées part par
     `execute` dans un test PostgreSQL : il partage le statement_timeout et les
     triggers par ligne du code mesuré, et tombe sous la charge de la CI en
     restant vert en local (#5599, #5585, #5582). `PgTestCase.fill` lève le
     délai pour ce seul remplissage et coupe les triggers nommés (#5641).
     L'estimation multiplie les `generate_series(a, b)` dont les bornes sont
     des entiers littéraux, des paramètres `%s`/`%d` liés à des constantes, ou
     des constantes de module ou de classe ; une borne inconnue n'est pas
     comptée (aucun constat sans preuve du volume).
  H8 sous-processus-ameesh-sans-doublure
     un test lance le binaire `ameesh` (`subprocess.run(["ameesh", …])`,
     `os.system("ameesh …")`, un chemin terminé par `bin/ameesh`) sans
     environnement explicite (`env=…`) ni binaire substitué (`executable=…`) :
     l'enfant hérite du processus de la suite, donc du DSN et du ~/.pgpass
     réels si `tests.support` ne les a pas posés (#6246 : six notes factices
     écrites sur le lot réel #99). La doublure explicite est l'environnement
     passé au banc (`env=child_env(…)`, `env=self.env()`) ou l'`executable=`
     qui remplace le binaire ; `env=None` et `executable=None` ne sont pas
     des doublures. Pour `os.spawn*`, le programme suit le mode ; `spawnve`
     et `spawnvpe` acceptent aussi l'environnement en quatrième position.
     Le témoin qui doit hériter s'exempte avec un motif.

Exemption motivée : un commentaire `# hermeticity: allow — <raison>` sur
l'instruction visée, ou sur la ligne juste au-dessus, l'exempte ; la raison
contient au moins une lettre ou un chiffre et reste visible du relecteur.
Un marqueur dans une chaîne de code, ou un commentaire sans raison,
n'exempte pas (R1 #707, N1).

Usage :
  scripts/test-hermeticity-lint.py [FICHIERS…]        # défaut : tests/**/test_*.py
  scripts/test-hermeticity-lint.py --diff origin/main # seuls les tests changés
  scripts/test-hermeticity-lint.py --rules H4,H5,H6   # un sous-ensemble de règles
  scripts/test-hermeticity-lint.py --max-findings 10  # texte tronqué, verdict entier
  scripts/test-hermeticity-lint.py --json
Sortie : 0 si aucun constat, 1 si au moins un, 2 en cas d'erreur d'usage.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import pathlib
import re
import subprocess
import sys
import tokenize

REPO = pathlib.Path(__file__).resolve().parent.parent
#: Exemption motivée : la ponctuation seule ne constitue pas une raison.
MARKER_RE = re.compile(r"hermeticity:\s*allow\s*[—–-][^\r\n]*[^\W_]")

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
#: lectures du disque de l'hôte (#5321) : règle H5, distincte de H2 pour que
#: l'avertissement de gel puisse ne montrer que les constats neufs.
DISK = {"shutil.disk_usage"}
#: comparateurs d'une durée à un seuil (H4) ; l'ordre des opérandes est libre.
COMPARATORS = {"assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual"}
#: au-delà de cette borne (en valeur absolue), une durée n'est plus un seuil
#: d'ordonnancement : c'est une garde de temps, qui peut rester au banc.
SUBSECOND_S = 1.0
#: champs de temps d'un plan `EXPLAIN (ANALYZE, FORMAT JSON)` : le serveur les
#: rend en millisecondes. Un seuil sous la seconde vaut donc moins de 1 000.
PLAN_TIME_FIELDS = ("Execution Time", "Planning Time")
SUBSECOND_PLAN_MS = 1000.0
#: appels qui reçoivent un chemin : un littéral `/proc/…` y est global.
PATH_CALLS = {"open", "os.listdir", "os.scandir", "os.stat", "os.readlink",
              "pathlib.Path", "Path", "io.open"}
RULES = {
    "H0": "source-illisible",
    "H1": "horloge-reelle-assertion",
    "H2": "etat-global-machine",
    "H3": "fixture-dependante-du-jour",
    "H4": "seuil-mural-sous-la-seconde",
    "H5": "etat-disque",
    "H6": "seuil-de-plan-sous-la-seconde",
    "H7": "remplissage-volumineux-hors-fill",
    "H8": "sous-processus-ameesh-sans-doublure",
}
#: #5641 : un remplissage de fixture à partir de ce volume passe par `fill`.
FILL_ROWS = 10_000
#: appels qui envoient une instruction à la base ; `fill` n'en fait pas partie.
EXECUTE_CALLS = {"execute": 0, "execute_without_statement_timeout": 1}
#: #6246 : appels qui lancent un programme extérieur, par leur nom canonique.
SUBPROCESS_CALLS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.system", "os.popen",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
}
#: le nom du vrai binaire du mesh (H8) : un enfant sans environnement explicite
#: hérite du DSN et du ~/.pgpass du processus de la suite.
AMEESH_PROGRAM = "ameesh"
_SERIES = re.compile(r"generate_series\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*[,)]",
                     re.IGNORECASE)


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


def _is_ameesh_program(node: ast.AST) -> bool:
    """le nœud désigne le binaire `ameesh` par un littéral (H8).

    Reconnus : `"ameesh"`, `"bin/ameesh"`, `os.path.join(REPO, "bin", "ameesh")`
    et la commande chaîne d'`os.system` (`"ameesh work note …"`). Un programme
    construit dans une variable (`[str(tool)]`, un faux binaire temporaire)
    n'est pas le binaire du dépôt : c'est déjà une doublure."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words = node.value.strip().split()
        return bool(words) and words[0].rsplit("/", 1)[-1] == AMEESH_PROGRAM
    if isinstance(node, ast.Call):
        name = _dotted(node.func) or ""
        short = name.rsplit(".", 1)[-1]
        if short in ("join", "Path"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.rsplit("/", 1)[-1] == AMEESH_PROGRAM:
                    return True
        if short == "str" and node.args:
            return _is_ameesh_program(node.args[0])
    return False


def _argv_ameesh(node: ast.AST) -> bool:
    """le premier argument d'un appel nomme le binaire `ameesh` (H8)."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return bool(node.elts) and _is_ameesh_program(node.elts[0])
    return _is_ameesh_program(node)


def _explicit_program_double(call: ast.Call, name: str) -> bool:
    """`env=` (environnement du banc) ou `executable=` (binaire substitué).

    C'est la doublure explicite de H8 : le test décide de l'environnement de
    l'enfant, ou il exécute autre chose que l'argv[0]. `env=None` n'est pas une
    doublure : c'est le défaut fautif. `executable=None` garde argv[0]."""
    if name in {"os.spawnve", "os.spawnvpe"} and len(call.args) > 3:
        env = call.args[3]
        if not (isinstance(env, ast.Constant) and env.value is None):
            return True
    for keyword in call.keywords:
        if keyword.arg in {"env", "executable"} and not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
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


def _comments(source: str) -> dict[int, str]:
    """Les commentaires du source, par ligne : seuls eux peuvent exempter."""
    out: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                out.setdefault(token.start[0], token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    return out


def _exempt(node: ast.AST, comments: dict[int, str]) -> bool:
    """Exemption motivée sur l'instruction ou la ligne juste au-dessus.

    Seuls les vrais commentaires comptent, avec une lettre ou un chiffre
    (`# hermeticity: allow — <raison>`) : un marqueur dans une chaîne, ou un
    commentaire sans motif, n'exempte pas (R1 #707, N1 ; lot #857)."""
    start, end = getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)
    for line in range(max(1, start - 1), end + 1):
        if MARKER_RE.search(comments.get(line, "")):
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


class _Scopes:
    """La portée la plus interne d'une ligne : fonction, et classe.

    Une table par ligne, construite une fois : `_innermost` linéaire sur
    chaque affectation rendait H4 quadratique sur les gros modules (47 s pour
    la suite, contre 3 s avec la table)."""

    def __init__(self, tree: ast.Module) -> None:
        self.module = tree
        self.functions: list = []
        self.classes: list = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(node)
            elif isinstance(node, ast.ClassDef):
                self.classes.append(node)
        self._function = self._index(self.functions)
        self._class = self._index(self.classes)

    @staticmethod
    def _index(nodes: list) -> dict[int, ast.AST]:
        best: dict[int, tuple] = {}
        for node in nodes:
            span = node.end_lineno - node.lineno
            for line in range(node.lineno, node.end_lineno + 1):
                current = best.get(line)
                if current is None or span < current[0]:
                    best[line] = (span, node)
        return {line: node for line, (_span, node) in best.items()}

    def function(self, node: ast.AST) -> ast.AST:
        return self._function.get(node.lineno, self.module)

    def cls(self, node: ast.AST) -> ast.AST | None:
        return self._class.get(node.lineno)


def _unwrap(node: ast.AST) -> ast.AST:
    """`abs(elapsed)`, `round(elapsed, 3)` : la mesure reste la même."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in ("abs", "round", "float", "int") and node.args:
        node = node.args[0]
    return node


def _has_clock(node: ast.AST, alias: dict[str, str]) -> bool:
    return any(_called(sub, alias, CLOCK) for sub in ast.walk(node))


def _is_duration(node: ast.AST, alias: dict[str, str], durations: dict,
                 attributes: dict, name_scope: ast.AST | None,
                 class_scope: ast.AST | None) -> bool:
    """`time.monotonic() - début`, ou une variable qui en vient (chaîne)."""
    node = _unwrap(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        if _has_clock(node.left, alias) or _has_clock(node.right, alias):
            return True
        for side in (node.left, node.right):
            if _is_duration_ref(side, durations, attributes, name_scope, class_scope):
                return True
    return _is_duration_ref(node, durations, attributes, name_scope, class_scope)


def _is_duration_ref(node: ast.AST, durations: dict, attributes: dict,
                     name_scope: ast.AST | None,
                     class_scope: ast.AST | None) -> bool:
    """Une variable ou un attribut `self.…` déjà reconnu comme durée."""
    node = _unwrap(node)
    if isinstance(node, ast.Name):
        return node.id in durations.get(name_scope, ())
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        return class_scope is not None and node.attr in attributes.get(class_scope, ())
    return False


def _numeric(node: ast.AST, constants: dict[str, float]) -> float | None:
    """La valeur numérique d'un littéral (signe compris) ou d'une constante."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _numeric(node.operand, constants)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _module_constants(tree: ast.Module) -> dict[str, float]:
    """Les constantes numériques du module, pour un seuil nommé."""
    values: dict[str, float] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = _numeric(node.value, {})
            if value is not None:
                values[node.targets[0].id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            value = _numeric(node.value, {})
            if value is not None:
                values[node.target.id] = value
    return values


def _duration_scopes(tree: ast.Module, alias: dict[str, str],
                     scopes: _Scopes) -> tuple[dict, dict]:
    """Les variables et attributs `self.…` qui portent une durée mesurée.

    Analyse de flux volontairement simple (affectations, chaînes, `abs`/
    `round`) : elle ne prétend pas suivre un retour de fonction. Le point est
    de rattraper `wake_s = time.monotonic() - début` puis
    `assertLess(wake_s, 0.05)`, que H1 ne voit pas (#5302)."""
    durations: dict[ast.AST, set[str]] = {}
    attributes: dict[ast.AST, set[str]] = {}
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        assignments.append((scopes.function(node), scopes.cls(node), targets, value))
    changed = True
    while changed:
        changed = False
        for name_scope, class_scope, targets, value in assignments:
            if not _is_duration(value, alias, durations, attributes,
                                name_scope, class_scope):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    names = durations.setdefault(name_scope, set())
                    if target.id not in names:
                        names.add(target.id)
                        changed = True
                elif isinstance(target, ast.Attribute) \
                        and isinstance(target.value, ast.Name) \
                        and target.value.id == "self":
                    if class_scope is None:
                        continue
                    attrs = attributes.setdefault(class_scope, set())
                    if target.attr not in attrs:
                        attrs.add(target.attr)
                        changed = True
    return durations, attributes


def _measured_operand(call: ast.Call, durations: dict, attributes: dict,
                      name_scope: ast.AST | None, class_scope: ast.AST | None,
                      constants: dict[str, float]) -> tuple:
    """`(opérande mesuré, seuil)` si la comparaison oppose une durée à un seuil
    sous la seconde, sinon `(None, None)`."""
    if len(call.args) < 2:
        return None, None
    left, right = call.args[0], call.args[1]
    for measured, threshold in ((left, right), (right, left)):
        value = _numeric(threshold, constants)
        # 0 est une assertion de logique (« plus de temps restant »), pas un
        # seuil d'ordonnancement : seule une borne strictement positive et
        # sous la seconde dépend de la charge.
        if value is not None and 0 < abs(value) < SUBSECOND_S \
                and _is_duration_ref(measured, durations, attributes,
                                     name_scope, class_scope):
            return measured, value
    return None, None


def _walk_scope(node: ast.AST):
    """Les nœuds du corps d'un scope, sans entrer dans ses définitions."""
    stack = list(node.body)
    while stack:
        child = stack.pop()
        yield child
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(child))


def _scope_assignments(tree: ast.Module) -> list[tuple]:
    """`(scope, cibles, valeur)` des affectations du module, des classes et
    des fonctions, sans descendre dans une définition imbriquée."""
    scopes = [tree] + [node for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                            ast.ClassDef))]
    out = []
    for scope in scopes:
        for node in _walk_scope(scope):
            if isinstance(node, ast.Assign):
                out.append((scope, node.targets, node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                out.append((scope, [node.target], node.value))
    return out


def _class_scope_of(scopes: _Scopes, scope: ast.AST) -> ast.AST | None:
    return None if isinstance(scope, ast.Module) else scopes.cls(scope)


def _call_key(call: ast.Call, class_scope: ast.AST | None) -> tuple | None:
    """La clé d'une fonction appelée : (classe, nom), ou (None, nom)."""
    func = call.func
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "self":
            name = class_scope.name if isinstance(class_scope, ast.ClassDef) else None
            return name, func.attr
        return None, func.attr
    return None


def _plan_field(node: ast.AST) -> str | None:
    """Le champ de temps de plan lu par un nœud, None sinon."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and key.value in PLAN_TIME_FIELDS:
            return key.value
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" and node.args:
        key = node.args[0]
        if isinstance(key, ast.Constant) and key.value in PLAN_TIME_FIELDS:
            return key.value
    return None


def _preferred_plan_info(infos: set) -> tuple:
    """Une information stable quand une cible en porte plusieurs.

    Les millisecondes passent avant les secondes : leur plafond est plus
    large, donc le constat est le plus prudent. L'ordre est total (champ
    ensuite), pour que le lint ne dépende pas du hasard des ensembles."""
    return min(infos, key=lambda info: (info[1] != "ms", info[0]))


def _plan_time_value(node: ast.AST, returns: dict, values: dict, attributes: dict,
                     name_scope: ast.AST, class_scope: ast.AST | None) -> dict:
    """`{slot: (champ, unité)}` des temps de plan portés par un nœud.

    `()` désigne la valeur elle-même ; `(i,)` l'élément i d'un tuple rendu par
    une fonction (`return plan["Plan"], plan["Execution Time"]`). L'unité est
    `ms` pour le champ brut, `s` après la conversion `/ 1000` ou `* 0.001`."""
    node = _unwrap(node)
    field = _plan_field(node)
    if field is not None:
        return {(): (field, "ms")}
    if isinstance(node, ast.BinOp):
        for measured, factor in ((node.left, node.right), (node.right, node.left)):
            inner = _plan_time_value(measured, returns, values, attributes,
                                     name_scope, class_scope)
            if () not in inner:
                continue
            number = _numeric(factor, {})
            if number is None:
                continue
            if isinstance(node.op, (ast.Div, ast.FloorDiv)) \
                    and abs(number - SUBSECOND_PLAN_MS) < 1e-9:
                return {(): (inner[()][0], "s")}
            if isinstance(node.op, ast.Mult) and abs(number - 0.001) < 1e-12:
                return {(): (inner[()][0], "s")}
        return {}
    if isinstance(node, ast.Name):
        infos = values.get(name_scope, {}).get(node.id)
        return {(): _preferred_plan_info(infos)} if infos else {}
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        infos = attributes.get(class_scope, {}).get(node.attr)
        return {(): _preferred_plan_info(infos)} if infos else {}
    if isinstance(node, ast.Call):
        key = _call_key(node, class_scope)
        found = returns.get(key) if key is not None else None
        if found is None and key is not None and key[0] is not None:
            found = returns.get((None, key[1]))
        return dict(found or {})
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
            and isinstance(node.slice.value, int) \
            and not isinstance(node.slice.value, bool):
        inner = _plan_time_value(node.value, returns, values, attributes,
                                 name_scope, class_scope)
        info = inner.get((node.slice.value,))
        return {(): info} if info is not None else {}
    if isinstance(node, (ast.Tuple, ast.List)):
        out = {}
        for index, element in enumerate(node.elts):
            for slot, info in _plan_time_value(element, returns, values,
                                               attributes, name_scope,
                                               class_scope).items():
                out[(index,) + slot] = info
        return out
    return {}


def _return_times(tree: ast.Module, scopes: _Scopes) -> dict:
    """Les fonctions qui rendent un temps de plan : `{clé: {slot: info}}`."""
    returns: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        class_scope = scopes.cls(node)
        key = (class_scope.name if isinstance(class_scope, ast.ClassDef) else None,
               node.name)
        slots: dict = {}
        for child in _walk_scope(node):
            if not isinstance(child, ast.Return) or child.value is None:
                continue
            slots.update(_plan_time_value(child.value, {}, {}, {}, node,
                                          class_scope))
        if slots:
            returns.setdefault(key, {}).update(slots)
    return returns


def _plan_time_scopes(tree: ast.Module, scopes: _Scopes,
                      returns: dict) -> tuple[dict, dict]:
    """Les variables et attributs `self.…` qui portent un temps de plan.

    Même analyse de flux volontairement simple que [`_duration_scopes`] : les
    affectations et le dépaquetage d'un tuple rendu par une fonction
    (`root, elapsed_ms = self._plan()`), jusqu'à point fixe.

    Le point fixe est monotone : chaque cible accumule les informations
    possibles et n'en perd jamais (B/P2 #5607). Une même variable affectée
    deux fois (`elapsed = plan["Execution Time"]` puis
    `elapsed = plan["Planning Time"]`) ne réécrit donc pas la table à chaque
    passe : les ensembles ne font que croître, et ils sont bornés par les
    deux champs et les deux unités, donc la boucle termine."""
    values: dict = {}
    attributes: dict = {}
    assignments = [(scope, _class_scope_of(scopes, scope), targets, value)
                   for scope, targets, value in _scope_assignments(tree)]
    changed = True
    while changed:
        changed = False
        for scope, class_scope, targets, value in assignments:
            incoming = _plan_time_value(value, returns, values, attributes,
                                        scope, class_scope)
            if not incoming:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    info = incoming.get(())
                    if info is None:
                        continue
                    infos = values.setdefault(scope, {}).setdefault(target.id, set())
                    if info not in infos:
                        infos.add(info)
                        changed = True
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for index, element in enumerate(target.elts):
                        info = incoming.get((index,))
                        if info is None:
                            continue
                        if isinstance(element, ast.Name):
                            infos = values.setdefault(scope, {}).setdefault(
                                element.id, set())
                            if info not in infos:
                                infos.add(info)
                                changed = True
                        elif isinstance(element, ast.Attribute) \
                                and isinstance(element.value, ast.Name) \
                                and element.value.id == "self" \
                                and class_scope is not None:
                            infos = attributes.setdefault(class_scope, {}).setdefault(
                                element.attr, set())
                            if info not in infos:
                                infos.add(info)
                                changed = True
                elif isinstance(target, ast.Attribute) \
                        and isinstance(target.value, ast.Name) \
                        and target.value.id == "self" and class_scope is not None:
                    info = incoming.get(())
                    if info is None:
                        continue
                    infos = attributes.setdefault(class_scope, {}).setdefault(
                        target.attr, set())
                    if info not in infos:
                        infos.add(info)
                        changed = True
    return values, attributes


def _scope_constants(tree: ast.Module) -> dict:
    """Les constantes numériques par scope : module, classe, fonction."""
    constants: dict = {}
    for scope, targets, value in _scope_assignments(tree):
        number = _numeric(value, {})
        if number is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants.setdefault(scope, {})[target.id] = number
    return constants


def _threshold_number(node: ast.AST, constants: dict,
                      class_constants: dict) -> float | None:
    """La valeur d'un seuil, y compris `self.MAX_MS` d'une constante de classe."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        return class_constants.get(node.attr)
    return _numeric(node, constants)


def _subsecond_plan_threshold(left: ast.AST, right: ast.AST, returns: dict,
                              values: dict, attributes: dict, constants: dict,
                              class_constants: dict, name_scope: ast.AST,
                              class_scope: ast.AST | None) -> tuple | None:
    """`(opérande, (champ, unité), seuil)` si la comparaison borne un temps de
    plan par un seuil sous la seconde, sinon None."""
    for measured, threshold in ((left, right), (right, left)):
        info = _plan_time_value(measured, returns, values, attributes,
                                name_scope, class_scope).get(())
        if info is None:
            continue
        value = _threshold_number(threshold, constants, class_constants)
        if value is None:
            continue
        limit = SUBSECOND_PLAN_MS if info[1] == "ms" else SUBSECOND_S
        if 0 < abs(value) < limit:
            return measured, info, value
    return None


def _constant_value(node: ast.AST, constants: dict, class_constants: dict):
    """Un entier ou une chaîne connus à la lecture, None sinon (#5641)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id in ("self", "cls"):
        value = class_constants.get(node.attr)
    elif isinstance(node, ast.Name):
        value = constants.get(node.id)
    else:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) else None


def _fill_sql(call: ast.Call, constants: dict, class_constants: dict) -> str | None:
    """Le SQL d'un appel `execute`, paramètres connus substitués (#5641).

    Une borne inconnue reste un marqueur (`?`), qui n'entre pas dans le
    compte : le constat ne naît que d'un volume prouvé."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if name not in EXECUTE_CALLS or len(call.args) <= EXECUTE_CALLS[name]:
        return None
    node = call.args[EXECUTE_CALLS[name]]
    fmt_args = None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        node, fmt_args = node.left, node.right
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    sql = node.value
    if "generate_series" not in sql.lower() or "insert" not in sql.lower():
        return None

    def values(arg):
        items = arg.elts if isinstance(arg, (ast.Tuple, ast.List)) else [arg]
        return [_constant_value(item, constants, class_constants) for item in items]

    def substitute(text, found, marker):
        parts = re.split(marker, text)
        out = [parts[0]]
        for index, part in enumerate(parts[1:]):
            value = found[index] if index < len(found) else None
            out.append("?" if value is None else str(value))
            out.append(part)
        return "".join(out)

    if fmt_args is not None:
        sql = substitute(sql.replace("%%", "\0"), values(fmt_args), r"%[sd]").replace("\0", "%")
    if name == "execute" and len(call.args) > 1:
        sql = substitute(sql, values(call.args[1]), r"%s")
    return sql


def _fill_rows(sql: str) -> int | None:
    """Le produit des `generate_series(a, b)` aux bornes connues."""
    total = None
    for low, high in _SERIES.findall(sql):
        try:
            low_value = int(low.split("::")[0])
            high_value = int(high.split("::")[0])
        except ValueError:
            continue
        total = (total or 1) * max(0, high_value - low_value + 1)
    return total


def lint_source(path: str, source: str, rules: set[str] | None = None) -> list[Finding]:
    """Les constats d'un fichier ; `rules` restreint les analyses lancées.

    Le filtre n'est pas cosmétique : H1/H4 parcourent chaque assertion et
    coûtent l'essentiel du lint d'une suite entière. L'appelant qui ne veut
    que H3 (passe horloge) ou H4/H5/H6 (avertissement du gel) ne paie pas
    les autres règles."""
    wanted = set(RULES) if rules is None else set(rules)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, exc.offset or 0, "H0",
                        "source illisible : %s" % exc.msg, "<module>")]
    alias = _aliases(tree)
    comments = _comments(source)
    funcs = _functions(tree)
    findings: list[Finding] = []
    clock_lines: set[int] = set()

    if "H1" in wanted:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assert, ast.Call)) and _is_assertion(node):
                body = node.test if isinstance(node, ast.Assert) else node
                for sub in ast.walk(body):
                    name = _called(sub, alias, CLOCK)
                    if name and _in_subtraction(sub, body) \
                            and not _exempt(node, comments):
                        clock_lines.add(id(sub))
                        findings.append(Finding(
                            path, sub.lineno, sub.col_offset + 1, "H1",
                            "durée mesurée à l'horloge réelle dans une assertion : %s()"
                            % name, _enclosing(funcs, sub)))

    if "H2" in wanted:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called(node, alias, MACHINE)
            if name and not _exempt(node, comments):
                findings.append(Finding(path, node.lineno, node.col_offset + 1, "H2",
                                        "état global de la machine : %s()" % name,
                                        _enclosing(funcs, node)))
            elif _proc_literal(node) and not _exempt(node, comments):
                findings.append(Finding(path, node.lineno, node.col_offset + 1, "H2",
                                        "état global de la machine : chemin /proc/…",
                                        _enclosing(funcs, node)))

    if "H3" in wanted:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in clock_lines:
                continue
            name = _called(node, alias, CALENDAR)
            if name and not _exempt(node, comments):
                findings.append(Finding(path, node.lineno, node.col_offset + 1, "H3",
                                        "fixture dépendante du jour : %s()" % name,
                                        _enclosing(funcs, node)))

    if "H4" in wanted or "H6" in wanted:
        scopes = _Scopes(tree)

    if "H4" in wanted:
        # H4 : la durée a quitté l'assertion (`wake_s = … - début`) ; H1 ne la
        # voit plus. Seules les variables reconnues sont retenues : la forme
        # directe reste H1, pour un seul constat par mesure.
        constants = _module_constants(tree)
        durations, attributes = _duration_scopes(tree, alias, scopes)
        for node in ast.walk(tree):
            if not _is_assertion(node) or _exempt(node, comments):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call) \
                        or not isinstance(sub.func, ast.Attribute) \
                        or sub.func.attr not in COMPARATORS:
                    continue
                measured, threshold = _measured_operand(
                    sub, durations, attributes, scopes.function(sub),
                    scopes.cls(sub), constants)
                if measured is None:
                    continue
                findings.append(Finding(
                    path, sub.lineno, sub.col_offset + 1, "H4",
                    "durée murale comparée à un seuil sous la seconde : %s < %g s"
                    % (ast.unparse(measured), threshold), _enclosing(funcs, sub)))

    if "H5" in wanted:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called(node, alias, DISK)
            if name and not _exempt(node, comments):
                findings.append(Finding(path, node.lineno, node.col_offset + 1, "H5",
                                        "état du disque de l'hôte : %s()" % name,
                                        _enclosing(funcs, node)))

    if "H6" in wanted and ("Execution Time" in source or "Planning Time" in source):
        # H6 : le temps d'un plan (`EXPLAIN ANALYZE`) dépend de la charge de
        # l'hôte ; sous la seconde, ce n'est pas une garde. La lecture directe
        # du champ, une variable ou un attribut qui en vient, et le
        # dépaquetage d'une fonction qui le rend, sont suivis.
        returns = _return_times(tree, scopes)
        values, attributes = _plan_time_scopes(tree, scopes, returns)
        constants = _scope_constants(tree)
        seen: set[tuple] = set()
        for node in ast.walk(tree):
            if not _is_assertion(node) or _exempt(node, comments):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                        and sub.func.attr in COMPARATORS and len(sub.args) >= 2:
                    pairs = ((sub.args[0], sub.args[1]),)
                elif isinstance(sub, ast.Compare):
                    operands = [sub.left] + list(sub.comparators)
                    pairs = tuple(zip(operands, operands[1:]))
                else:
                    continue
                name_scope = scopes.function(sub)
                class_scope = scopes.cls(sub)
                merged = dict(constants.get(scopes.module, {}))
                merged.update(constants.get(class_scope, {}))
                merged.update(constants.get(name_scope, {}))
                for left, right in pairs:
                    found = _subsecond_plan_threshold(
                        left, right, returns, values, attributes, merged,
                        constants.get(class_scope, {}), name_scope, class_scope)
                    if found is None:
                        continue
                    measured, (field, unit), threshold = found
                    key = (sub.lineno, sub.col_offset)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(Finding(
                        path, sub.lineno, sub.col_offset + 1, "H6",
                        "champ de plan comparé à un seuil sous la seconde : %s "
                        "(%s) contre %g %s — la structure est la preuve, le "
                        "temps ne sert qu'en plafond large (>= 1 s)"
                        % (ast.unparse(measured), field, threshold, unit),
                        _enclosing(funcs, sub)))

    if "H7" in wanted and "generate_series" in source:
        # H7 : un gros remplissage par `execute` partage le délai et les
        # triggers du code mesuré (#5641) ; `fill` est l'aide commune.
        h7_scopes = _Scopes(tree)
        constants = _scope_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _exempt(node, comments):
                continue
            class_scope = h7_scopes.cls(node)
            merged = dict(constants.get(h7_scopes.module, {}))
            merged.update(constants.get(class_scope, {}))
            merged.update(constants.get(h7_scopes.function(node), {}))
            sql = _fill_sql(node, merged, constants.get(class_scope, {}))
            rows = _fill_rows(sql) if sql else None
            if rows is not None and rows >= FILL_ROWS:
                findings.append(Finding(
                    path, node.lineno, node.col_offset + 1, "H7",
                    "remplissage de %d lignes estimées hors de PgTestCase.fill : "
                    "délai et triggers par ligne du code mesuré (#5641)" % rows,
                    _enclosing(funcs, node)))

    if "H8" in wanted:
        # H8 : le vrai binaire `ameesh` lancé sans environnement explicite
        # hérite du DSN et du ~/.pgpass du processus de la suite — la base du
        # mesh, pas celle du banc (#6246). `env=`/`executable=` sont la
        # doublure ; un faux binaire construit dans une variable n'est pas
        # reconnu, donc pas signalé.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _exempt(node, comments):
                continue
            name = _called(node, alias, SUBPROCESS_CALLS)
            if not name or not node.args:
                continue
            # spawn*(mode, path, argv[, env]); other supported APIs take
            # the program/command first. `name` is canonical even for aliases.
            program_index = 1 if name.startswith("os.spawn") else 0
            if len(node.args) <= program_index:
                continue
            if (not _argv_ameesh(node.args[program_index])
                    or _explicit_program_double(node, name)):
                continue
            findings.append(Finding(
                path, node.lineno, node.col_offset + 1, "H8",
                "sous-processus au binaire ameesh sans environnement explicite : "
                "%s() hérite du processus de la suite (#6246)" % name,
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
        description="lint d'herméticité des tests (#707) : horloge, machine, "
                    "calendrier, sous-processus (#6246)")
    parser.add_argument("files", nargs="*", help="fichiers à analyser")
    parser.add_argument("--diff", metavar="REF",
                        help="n'analyser que les tests changés depuis REF (git diff REF...HEAD)")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--rules", metavar="H4,H5,H6",
                        help="ne garder que ces règles (virgules), pour un "
                             "avertissement ciblé (gel, passe horloge)")
    parser.add_argument("--max-findings", type=int, metavar="N",
                        help="n'afficher que les N premiers constats (texte)")
    args = parser.parse_args(argv)
    if args.files and args.diff:
        print("donnez des FICHIERS ou --diff, pas les deux", file=sys.stderr)
        return 2
    selected = None
    if args.rules:
        selected = {rule.strip() for rule in args.rules.split(",") if rule.strip()}
        unknown = sorted(selected - set(RULES))
        if not selected or unknown:
            print("règle(s) inconnue(s) : %s" % (", ".join(unknown) or "(aucune)"),
                  file=sys.stderr)
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
        findings.extend(lint_source(rel, path.read_text(encoding="utf-8"),
                                    rules=selected))
    if selected is not None:
        findings = [finding for finding in findings if finding.rule in selected]
    findings.sort(key=lambda f: (f.path, f.lineno, f.col, f.rule))

    if args.json:
        print(json.dumps({"schema": "ameesh-hermeticity-lint/1",
                          "findings": [f.as_dict() for f in findings],
                          "summary": {"files": len(files), "findings": len(findings),
                                      "by_rule": {r: sum(1 for f in findings if f.rule == r)
                                                  for r in sorted(RULES)}}},
                         ensure_ascii=False, indent=2))
    else:
        shown = findings
        if args.max_findings is not None and args.max_findings >= 0:
            shown = findings[:args.max_findings]
        for finding in shown:
            print(finding.render())
        if len(shown) < len(findings):
            print("… %d constat(s) de plus (--max-findings)" % (len(findings) - len(shown)))
        by_rule = ", ".join("%s %d" % (r, sum(1 for f in findings if f.rule == r))
                            for r in sorted(RULES))
        print("%d constat(s) dans %d fichier(s) — %s"
              % (len(findings), len(files), by_rule))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
