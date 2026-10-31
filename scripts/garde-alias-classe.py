#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#4521 — un alias de module ne peut pas affaiblir la classe de relecture.

Le canon §8 range des chemins du cœur en classe sensible (porte de fusion,
SQL, droits, argent…). Quand L100 déplace une implémentation dans
``src/ameesh/modules/dev/``, le chemin d'implémentation tombe au défaut de la
politique — souvent ``light`` — alors que l'ancien chemin reste déclaré
``sensitive``. Le déplacement affaiblit alors la porte sans qu'aucun diff ne
le dise : #4520 a dû corriger le canon à la main après coup (merge_pr,
lot_class, review*, plan_git).

Cette garde rend le constat déterministe, avant le gel (§7) : elle lit les
alias **déclarés** et compare la classe de l'ancien chemin (le fichier qui
porte ``sys.modules[__name__] = …``) à celle de l'implémentation qu'il
désigne, sous la politique du canon configuré. Une implémentation plus faible
est refusée, avec le chemin et la règle du canon à compléter.

Les anciens chemins sont ceux de ``tests/test_generic_core.py`` : ses
``exceptions`` de ``module-import`` nomment les fichiers qui importent une
implémentation de module (L100). Le fichier n'est pas modifié ici ; il est lu,
parce que le test le tient déjà à jour (une exception obsolète ou un import
non déclaré le font rougir). Un scan de ``src/ameesh`` ajoute un alias
``sys.modules`` oublié dans cette déclaration : la suite le refuserait de
toute façon, le gel le voit avant le paquet. Un fichier qui ne porte pas
d'alias ``sys.modules``, ou dont la cible n'est pas un module de
``ameesh.modules``, n'est pas un alias de module : il est ignoré (L148,
``menage``/``grand_livre``, pointe vers des modules du cœur).

La politique vient du canon configuré, au même sens que ``ameesh
review-class`` : tous les canons configurés, le maximum l'emporte, un projet
qu'aucun canon ne déclare ou une politique illisible est un doute (refus). Au
gel, ``scripts/freeze-checks.py`` est lancé dans l'arbre du commit, avec
``PYTHONPATH`` sur son ``src/`` : la garde lit le code gelé et le canon de
l'hôte. Sans canon configuré, elle refuse plutôt que de supposer une classe.

En CI, le canon de l'hôte n'est pas lisible (``manaty/home`` est privé) : la
garde lit une **politique épinglée** versionnée, ``scripts/alias-review-
classes.json``, générée depuis le canon audité (``--write-policy``). Le gel
vérifie que la classe épinglée de chaque alias n'est pas plus faible que
celle du canon de l'hôte (``--check-policy``) ; les ``rule`` peuvent bouger
sans changer la classe sans refuser les gels. La CI ne devine donc jamais
une classe, et le gel reste la source de vérité.

Usage : scripts/garde-alias-classe.py [--root REPO] [--project ameesh]
        [--canon DIR --ref REV] [--policy FILE]
        [--write-policy FILE | --check-policy FILE]
Sortie : 0 tous les alias sont couverts, 1 au moins un alias affaiblit la
classe (ou la politique épinglée est plus faible que le canon), 2 lecture
impossible (déclaration, alias, canon ou politique).
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
from dataclasses import dataclass

#: le fichier qui déclare les anciens chemins d'implémentation (L100)
DECLARATIONS = "tests/test_generic_core.py"
#: le test dont les ``exceptions`` sont la déclaration
DECLARING_TEST = "test_core_does_not_import_module_implementations"
#: racine des sources du cœur, relativement au dépôt
SOURCE = "src/ameesh"
#: paquet des implémentations déplacées (le seul que la garde compare)
MODULES = "ameesh.modules."
#: rangs de la politique de revue ; inconnu = doute, jamais « light »
RANK = {"light": 0, "normal": 1, "sensitive": 2}
#: motif des exceptions qui déclarent un import d'implémentation
MODULE_IMPORT = "module-import"


class GuardError(Exception):
    """Lecture impossible : la garde refuse de conclure au vert."""


@dataclass(frozen=True)
class Alias:
    """Un alias de module : l'ancien chemin (fichier Python) et son implémentation."""

    old: str
    new: str


@dataclass(frozen=True)
class Finding:
    """Un alias dont l'implémentation demande une revue plus faible."""

    alias: Alias
    old_class: str
    old_rule: tuple | None
    new_class: str

    def render(self) -> str:
        source = ("règle %s" % ", ".join(self.old_rule)
                  if self.old_rule else "défaut de la politique")
        return (
            "REFUSÉ  %s (%s) < %s (%s, %s)\n"
            "        ajouter « %s » au canon, classe %s"
            % (self.alias.new, self.new_class, self.alias.old, self.old_class,
               source, self.alias.new, self.old_class))


def _relative(path: str) -> str:
    """Un chemin de dépôt, séparateurs POSIX."""
    return path.replace(os.sep, "/")


def _value(node, env: dict | None = None):
    """La valeur d'un littéral de la déclaration (dict ou compréhension).

    Volontairement étroit : constantes, noms de la compréhension, tuples,
    listes, ``+`` et ``%`` sur des chaînes. Une autre forme est un doute —
    la garde ne devine pas une déclaration qu'elle ne sait pas lire.
    """
    env = env or {}
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_value(item, env) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _value(node.left, env) + _value(node.right, env)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _value(node.left, env) % _value(node.right, env)
    raise GuardError("déclaration illisible : %s" % ast.dump(node, annotate_fields=False))


def _counter_argument(node):
    """L'argument de ``Counter(...)``, ou le mapping nu."""
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "Counter":
            raise GuardError("exceptions : Counter(...) attendu")
        if not node.args:
            return ast.Dict(keys=[], values=[])
        if len(node.args) != 1 or node.keywords:
            raise GuardError("exceptions : un seul argument attendu")
        return node.args[0]
    return node


def _constants(tree) -> dict:
    """Les dicts littéraux de tête du module, réduits au tuple de leurs clés (#4414)."""
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and all(isinstance(target, ast.Name) for target in node.targets)
                and all(key is not None for key in node.value.keys)):
            keys = tuple(_value(key) for key in node.value.keys)
            found.update({target.id: keys for target in node.targets})
    return found


def _declared_keys(node, constants=None) -> list:
    """Les clés ``(chemin, motif)`` d'un dict littéral ou d'une compréhension."""
    if isinstance(node, ast.Dict):
        return [_value(key) for key in node.keys]
    if isinstance(node, ast.DictComp):
        if len(node.generators) != 1 or node.generators[0].ifs:
            raise GuardError("exceptions : compréhension non prise en charge")
        generator = node.generators[0]
        if not isinstance(generator.target, ast.Name):
            raise GuardError("exceptions : variable de compréhension attendue")
        values = _value(generator.iter, constants or {})
        if not isinstance(values, tuple):
            raise GuardError("exceptions : itération non prise en charge")
        return [_value(node.key, {generator.target.id: value}) for value in values]
    raise GuardError("exceptions : dict ou compréhension attendue")


def _tuple_key(key) -> tuple:
    if not isinstance(key, tuple) or len(key) != 2:
        raise GuardError("exceptions : clé (chemin, motif) attendue")
    return key


def declared_paths(root: str) -> list:
    """Les anciens chemins de ``tests/test_generic_core.py``, triés.

    Seules les exceptions ``module-import`` déclarent une implémentation de
    module. Une déclaration absente ou d'une forme inconnue refuse : la garde
    ne peut pas prouver l'absence d'alias qu'elle ne sait pas lire.
    """
    path = os.path.join(root, *DECLARATIONS.split("/"))
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError, ValueError) as exc:
        raise GuardError("déclaration illisible (%s) : %s" % (DECLARATIONS, exc)) from exc
    test = next((node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == DECLARING_TEST), None)
    if test is None:
        raise GuardError("test %s absent de %s" % (DECLARING_TEST, DECLARATIONS))
    exceptions = None
    for node in ast.walk(test):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "exceptions"
               for target in targets):
            exceptions = node.value
            break
    if exceptions is None:
        raise GuardError("variable `exceptions` absente de %s" % DECLARING_TEST)
    paths = []
    for key in _declared_keys(_counter_argument(exceptions), _constants(tree)):
        declared, motif = _tuple_key(key)
        if motif == MODULE_IMPORT and isinstance(declared, str):
            paths.append(declared)
    return sorted(set(paths))


def _package(relative: str) -> str:
    """Le paquet d'un fichier de ``src/ameesh`` (parents du fichier)."""
    parent = os.path.dirname(relative)
    return "ameesh" + ("." + parent.replace("/", ".") if parent else "")


def _imports(tree, relative: str) -> dict:
    """``nom lié -> [module candidat, module de base]`` pour chaque import.

    ``from .modules.dev import plan_git`` lie ``plan_git`` au sous-module
    ``ameesh.modules.dev.plan_git`` s'il existe, sinon à l'attribut du paquet
    ``ameesh.modules.dev``. Les deux candidats sont rendus dans cet ordre.
    """
    package = _package(_relative(relative))
    bindings: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = (importlib.util.resolve_name(
                "." * node.level + (node.module or ""), package)
                if node.level else (node.module or ""))
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                bindings[name] = [base + "." + alias.name, base]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = [alias.name]
                else:
                    head = alias.name.split(".", 1)[0]
                    bindings[head] = [head]
    return bindings


def _module_file(root: str, module: str) -> str | None:
    """Le fichier de dépôt d'un module ``ameesh.*``, ou None s'il n'existe pas."""
    if not module.startswith("ameesh."):
        return None
    relative = module[len("ameesh."):].replace(".", "/")
    for candidate in (SOURCE + "/" + relative + ".py",
                      SOURCE + "/" + relative + "/__init__.py"):
        if os.path.isfile(os.path.join(root, *candidate.split("/"))):
            return candidate
    return None


def _self_alias(tree):
    """La valeur de ``sys.modules[__name__] = …``, ou None."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            value = target.value
            if not (isinstance(value, ast.Attribute) and value.attr == "modules"
                    and isinstance(value.value, ast.Name) and value.value.id == "sys"):
                continue
            index = target.slice
            if isinstance(index, ast.Index):  # Python < 3.9
                index = index.value
            if isinstance(index, ast.Name) and index.id == "__name__":
                return node.value
    return None


def _alias_target(root: str, relative: str) -> str | None:
    """Le fichier d'implémentation désigné par l'alias, ou None si ce n'est pas
    un alias de module (pas de ``sys.modules``, ou cible hors ``ameesh.modules``)."""
    path = os.path.join(root, *relative.split("/"))
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError, ValueError) as exc:
        raise GuardError("alias illisible (%s) : %s" % (relative, exc)) from exc
    node = _self_alias(tree)
    if node is None:
        return None
    # `relative` est le chemin du dépôt ; les imports se lisent depuis `src/ameesh`
    bindings = _imports(tree, relative[len(SOURCE) + 1:])
    if not isinstance(node, ast.Name) or node.id not in bindings:
        raise GuardError("%s : cible d'alias non résolue (%s)"
                         % (relative, ast.dump(node, annotate_fields=False)))
    for module in bindings[node.id]:
        if not module.startswith(MODULES):
            continue
        found = _module_file(root, module)
        if found:
            return found
    if any(module.startswith(MODULES) for module in bindings[node.id]):
        raise GuardError("%s : implémentation absente (%s)"
                         % (relative, ", ".join(bindings[node.id])))
    return None


def _source_files(root: str):
    """Les fichiers Python de ``src/ameesh``, chemins de dépôt triés."""
    base = os.path.join(root, *SOURCE.split("/"))
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield _relative(os.path.relpath(os.path.join(dirpath, name), root))


def _mentions_alias(root: str, relative: str) -> bool:
    """Préfiltre bon marché : le fichier nomme ``sys.modules`` quelque part."""
    path = os.path.join(root, *relative.split("/"))
    try:
        with open(path, encoding="utf-8") as fh:
            return "sys.modules" in fh.read()
    except OSError:
        return False


def aliases(root: str) -> list:
    """Les alias de module, triés par ancien chemin.

    Les anciens chemins déclarés dans ``tests/test_generic_core.py`` sont lus
    en premier (un fichier déclaré absent est un refus). Le scan de
    ``src/ameesh`` attrape en plus un alias ``sys.modules`` dont la déclaration
    aurait été oubliée : la suite le refuserait de toute façon, mais le gel le
    voit avant le paquet.
    """
    declared = []
    for path in declared_paths(root):
        if path.startswith("/") or ".." in path.split("/"):
            raise GuardError("exception hors du dépôt : %r" % path)
        declared.append(SOURCE + "/" + path)
    declared_set = set(declared)
    candidates = declared + [path for path in _source_files(root)
                             if path not in declared_set]
    found = []
    for relative in candidates:
        if relative in declared_set:
            if not os.path.isfile(os.path.join(root, *relative.split("/"))):
                raise GuardError("ancien chemin déclaré absent : %s" % relative)
        elif not _mentions_alias(root, relative):
            continue
        new = _alias_target(root, relative)
        if new is None or new == relative:
            continue
        found.append(Alias(relative, new))
    return sorted(found, key=lambda alias: alias.old)


def check(declared, class_of) -> list:
    """Les alias dont l'implémentation est classée plus bas que l'ancien chemin.

    ``class_of(chemin) -> (classe, règle)`` : la politique de revue du canon ;
    une classe inconnue est un doute, jamais un vert.
    """
    findings = []
    for alias in declared:
        old_class, old_rule = class_of(alias.old)
        new_class, _new_rule = class_of(alias.new)
        for name in (old_class, new_class):
            if name not in RANK:
                raise GuardError("classe de revue inconnue : %r" % (name,))
        if RANK[new_class] < RANK[old_class]:
            findings.append(Finding(alias, old_class, old_rule, new_class))
    return findings


def _policy(canons, project: str):
    """La politique de revue d'une liste de canons déjà lus.

    Rend ``(class_of, ref)`` : ``ref`` porte le commit du canon lu. Un projet
    non déclaré ou une politique illisible refusent.
    """
    from ameesh import lot_class

    ref = [""]

    def class_of(path: str) -> tuple:
        result = lot_class.class_of_paths(canons, project, [path])
        if result.get("doubt"):
            raise GuardError("politique de revue illisible : %s"
                             % (result.get("why") or "doute"))
        ref[0] = result.get("policy_ref") or ref[0]
        entry = next((item for item in result.get("files") or ()
                      if item.get("path") == path), None)
        if entry is None:
            raise GuardError("chemin non classé par le canon : %s" % path)
        return entry["class"], tuple(entry["rule"]) if entry.get("rule") else None

    return class_of, ref


def configured_canons(project: str):
    """Les canons configurés de l'hôte, comme ``ameesh review-class``."""
    from ameesh import canon as canon_mod
    from ameesh import config as config_mod

    try:
        cfg = config_mod.load()
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # config ou canon illisible : pas de classe supposée
        raise GuardError("canon configuré illisible : %s" % exc) from exc
    if not canons:
        raise GuardError("aucun canon configuré : politique de %r inconnue" % project)
    for loaded in canons:
        # un canon déclaré mais dont le chemin manque est rendu sans
        # fédération (`federation=None`, `sources=[]`) : ce n'est pas une
        # politique, et `class_of_paths` le lirait comme « normal », donc
        # comme un vert. Refus fermé (#4521, B/P2 de cx2).
        if not getattr(loaded, "federation", None):
            raise GuardError("canon illisible : %s (fédération absente)"
                             % (getattr(loaded, "root", "") or "canon"))
    return canons


def explicit_canon(root: str, ref: str):
    """Un canon épinglé lu hors configuration de l'hôte (dépôt git)."""
    from ameesh import canon as canon_mod

    try:
        loaded = canon_mod.load(root, ref=ref or "HEAD")
    except Exception as exc:  # racine, git ou ref illisible : pas de classe supposée
        raise GuardError("canon épinglé illisible : %s (%s)" % (root, exc)) from exc
    if not getattr(loaded, "readable", False):
        raise GuardError("canon épinglé illisible : %s" % root)
    if not getattr(loaded, "federation", None):
        raise GuardError("canon épinglé illisible : %s (fédération absente)" % root)
    return [loaded]


def configured_class_of(project: str):
    """La politique du canon configuré, comme ``ameesh review-class``.

    Rend ``(class_of, ref)`` : ``ref`` porte le commit du canon lu. Un canon
    absent, un projet non déclaré ou une politique illisible refusent.
    """
    return _policy(configured_canons(project), project)


def explicit_class_of(root: str, ref: str, project: str):
    """La politique d'un canon épinglé, hors configuration de l'hôte."""
    return _policy(explicit_canon(root, ref), project)


#: version du format de la politique épinglée (JSON)
POLICY_VERSION = 1


def policy_for(canons, project: str, declared) -> dict:
    """La politique épinglée des alias déclarés, lue sur les canons chargés.

    Le fichier produit est la source que la CI peut lire sans canon : les
    classes des anciens chemins et de leurs implémentations, telles que le
    canon audité les donne. ``policy_ref`` rappelle le canon lu ; la
    vérification au gel compare les classes au canon de l'hôte.
    """
    class_of, ref = _policy(canons, project)
    classes = {}
    for alias in declared:
        for path in (alias.old, alias.new):
            cls, rule = class_of(path)
            classes[path] = {"class": cls, "rule": list(rule) if rule else []}
    return {"version": POLICY_VERSION, "project": project,
            "policy_ref": ref[0], "classes": classes}


def write_policy(path: str, policy: dict) -> None:
    """Écrit la politique épinglée, triée et stable (diff lisible)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def read_policy(path: str) -> dict:
    """Lit une politique épinglée ; une forme inconnue refuse."""
    try:
        with open(path, encoding="utf-8") as fh:
            policy = json.load(fh)
    except OSError as exc:
        raise GuardError("politique épinglée illisible : %s" % exc) from exc
    except ValueError as exc:
        raise GuardError("politique épinglée invalide : %s" % exc) from exc
    if not isinstance(policy, dict) or not isinstance(policy.get("classes"), dict):
        raise GuardError("politique épinglée invalide : classes absentes")
    return policy


def pinned_class_of(policy: dict):
    """``class_of`` depuis la politique épinglée : aucun canon, aucun réseau."""
    classes = policy["classes"]

    def class_of(path: str) -> tuple:
        entry = classes.get(path)
        if not isinstance(entry, dict) or entry.get("class") not in RANK:
            raise GuardError("chemin absent ou classe inconnue dans la politique "
                             "épinglée : %s" % path)
        return entry["class"], tuple(entry.get("rule") or ())

    return class_of


def policy_gaps(path: str, expected: dict) -> list:
    """Les chemins dont la classe épinglée est plus faible que le canon.

    Seule la classe est comparée : le canon ajoute souvent un chemin sans
    rapport aux règles (le §8 bouge), et une ``rule`` différente sans
    changement de classe ne doit pas refuser le gel de chaque lot (B/P2
    auditeur). Les ``rule`` restent dans le fichier pour les messages. Une
    classe épinglée plus forte que le canon est acceptée : la CI est alors
    plus stricte que le gel, jamais plus permissive.
    """
    classes = read_policy(path)["classes"]
    gaps = []
    for source, wanted in sorted(expected["classes"].items()):
        entry = classes.get(source)
        pinned = entry.get("class") if isinstance(entry, dict) else None
        canon = wanted.get("class")
        if pinned not in RANK or canon not in RANK:
            gaps.append((source, entry, wanted))
        elif RANK[pinned] < RANK[canon]:
            gaps.append((source, entry, wanted))
    return gaps


def _class_name(entry) -> str:
    """Une entrée de politique lisible dans un message de divergence."""
    if not isinstance(entry, dict):
        return "absent"
    return "%s (%s)" % (entry.get("class"),
                        ", ".join(entry.get("rule") or ()) or "défaut")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="racine du dépôt (défaut : .)")
    parser.add_argument("--project", default="ameesh", help="projet du canon (défaut : ameesh)")
    parser.add_argument("--canon", default="",
                        help="canon épinglé (dépôt git) ; sinon canon configuré de l'hôte")
    parser.add_argument("--ref", default="HEAD",
                        help="référence du canon épinglé (défaut : HEAD)")
    parser.add_argument("--policy", default="",
                        help="politique épinglée (JSON) ; sinon canon configuré")
    parser.add_argument("--write-policy", default="",
                        help="écrit la politique épinglée depuis le canon lu")
    parser.add_argument("--check-policy", default="",
                        help="vérifie la politique épinglée contre le canon lu")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    try:
        declared = aliases(root)
    except GuardError as exc:
        print("garde alias/classe #4521 : %s" % exc, file=sys.stderr)
        return 2
    if args.write_policy or args.check_policy:
        try:
            canons = (explicit_canon(args.canon, args.ref) if args.canon
                      else configured_canons(args.project))
            expected = policy_for(canons, args.project, declared)
            if args.write_policy:
                write_policy(args.write_policy, expected)
                print("garde alias/classe #4521 : politique épinglée écrite dans %s "
                      "(%d chemins, canon %s)"
                      % (args.write_policy, len(expected["classes"]),
                         expected["policy_ref"] or "?"))
                return 0
            gaps = policy_gaps(args.check_policy, expected)
        except GuardError as exc:
            print("garde alias/classe #4521 : %s" % exc, file=sys.stderr)
            return 2
        if gaps:
            for source, actual, wanted in gaps:
                print("AFFAIBLIE  %s (politique : %s ; canon : %s)"
                      % (source, _class_name(actual), _class_name(wanted)))
            print("garde alias/classe #4521 : politique épinglée plus faible "
                  "que le canon (%d chemin(s)) — régénérer : "
                  "scripts/garde-alias-classe.py --root . "
                  "--write-policy %s" % (len(gaps), args.check_policy))
            return 1
        print("garde alias/classe #4521 : politique épinglée à jour (%d chemins, "
              "canon %s)" % (len(expected["classes"]),
                             expected["policy_ref"] or "?"))
        return 0
    if not declared:
        print("garde alias/classe #4521 : aucun alias de module déclaré")
        return 0
    try:
        if args.policy:
            policy = read_policy(args.policy)
            class_of = pinned_class_of(policy)
            ref = [policy.get("policy_ref") or ""]
        elif args.canon:
            class_of, ref = explicit_class_of(args.canon, args.ref, args.project)
        else:
            class_of, ref = configured_class_of(args.project)
        findings = check(declared, class_of)
    except GuardError as exc:
        print("garde alias/classe #4521 : %s" % exc, file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(finding.render())
        print("garde alias/classe #4521 : %d alias affaibli(s) sur %d — "
              "compléter le canon avant le gel" % (len(findings), len(declared)))
        return 1
    print("garde alias/classe #4521 : %d alias, classe de relecture conservée"
          % len(declared) + (" (canon %s)" % ref[0] if ref[0] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
