# SPDX-License-Identifier: AGPL-3.0-only
"""Politique de revue par classe de risque (décision 0018 point 1, R19 ; spec §4.2).

Le canon (OKF Federation, clé `review_policies` de `federation.yaml`) déclare,
**par portée de fichiers**, la revue qu'un changement demande :

- `light` (léger) : fusion dès que les tests ciblés sont verts, relecture après
  coup ;
- `normal` : une revue d'un autre éditeur, les détails ne bloquent pas ;
- `sensitive` (sensible) : gel, revue avant fusion, accord explicite — SQL,
  sécurité, natif, contrats, production.

En cas de doute, la **classe supérieure** : la classe d'un fichier est la plus
haute des règles qui matchent, et la classe d'un changement est la plus haute de
ses fichiers. Une portée qui ne matche rien prend `default` (`normal` si la clé
est absente).

Ce module est pur (aucune E/S) : `canon check` valide la déclaration avec
[`parse`], `ameesh review-class` calcule une classe avec [`classify`], et les
deux partagent les mêmes règles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: les trois classes, de la plus légère à la plus lourde
CLASSES = ("light", "normal", "sensitive")
#: la classe d'un fichier qu'aucune portée ne nomme
DEFAULT_CLASS = "normal"
#: rang de gravité : la classe d'un changement est le maximum
RANK = {name: index for index, name in enumerate(CLASSES)}
#: libellés français (CLI et messages)
DISPLAY = {"light": "léger", "normal": "normal", "sensitive": "sensible"}
#: codes de constat produits par [`parse`] (préfixe des constats du canon)
INVALID = "review-policies-invalid"
UNKNOWN_CLASS = "review-policies-class-unknown"
BAD_RULE = "review-policies-rule-invalid"
BAD_GLOB = "review-policies-glob-invalid"
#: une politique existe mais ne dit pas sa classe par défaut (avertissement)
NO_DEFAULT = "review-policies-default-missing"

_SEVERITY = {"error", "warning"}
#: un chemin absolu Windows (`C:/…`) n'est pas un chemin de dépôt non plus
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")


class ReviewError(ValueError):
    """Entrée refusée par la CLI (chemin hors dépôt, liste vide)."""


@dataclass(frozen=True)
class Rule:
    """Une portée déclarée : des motifs de chemins et la classe qu'ils demandent."""

    paths: tuple[str, ...]
    cls: str
    #: les motifs compilés, dans l'ordre déclaré
    regexes: tuple = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class ReviewPolicy:
    """La politique lue au canon, prête à classer."""

    default: str = DEFAULT_CLASS
    rules: tuple[Rule, ...] = ()

    def class_of(self, path: str) -> tuple[str, Rule | None]:
        """La classe d'un fichier et la règle qui l'a donnée (None : défaut)."""
        best: Rule | None = None
        for rule in self.rules:
            if any(regex.match(path) for regex in rule.regexes):
                if best is None or RANK[rule.cls] > RANK[best.cls]:
                    best = rule
        return (best.cls if best else self.default), best


def normalize_path(path: str) -> str:
    """Chemin de dépôt : séparateurs POSIX, segments `.` et vides retirés.

    Les `..` **intérieurs** sont résolus (`a/../b` → `b`), un `..` qui sortirait
    du dépôt est conservé pour que [`classify`] le refuse, et un `/` de tête
    aussi (un chemin absolu n'est pas un chemin de dépôt).
    """
    value = str(path).strip().replace("\\", "/")
    prefix = "/" if value.startswith("/") else ""
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return prefix + "/".join(parts)


def compile_glob(pattern: str) -> re.Pattern:
    """Un motif de portée en expression régulière, ancrée sur le chemin entier.

    Sémantique volontairement proche d'un `.gitignore` :
    - `**` traverse les séparateurs **et tous les caractères**, sauts de ligne
      compris (un nom de dossier peut contenir un LF) ;
    - `*` et `?` non ;
    - `**/` en tête (ou après un `/`) vaut « zéro ou plusieurs dossiers » ;
    - `[...]` est une classe de caractères (`[!a]` = négation) ;
    - tout le reste est littéral, et le motif couvre le chemin **entier**
      (`docs` ne matche pas `docs/x.md` : écrire `docs/**`).
    """
    text = normalize_path(pattern)
    if text.startswith("/"):        # un motif `/x` est ancré à la racine
        text = text[1:]
    if text.endswith("/"):
        text += "**"
    out: list[str] = ["^"]
    i = 0
    while i < len(text):
        char = text[i]
        if char == "*":
            if text[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if text[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        if char == "[":
            end = text.find("]", i + 1)
            if end == -1:
                out.append(re.escape(char))
                i += 1
                continue
            body = text[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[%s]" % body.replace("\\", "\\\\"))
            i = end + 1
            continue
        out.append(re.escape(char))
        i += 1
    out.append("$")
    # DOTALL : sans lui, `.*` (donc `**`) s'arrêtait au saut de ligne et un
    # chemin comme `db/line\nbreak/new.sql` échappait à `**/*.sql` — classé
    # par le défaut au lieu de la portée (revue codex2, B1).
    return re.compile("".join(out), re.DOTALL)


def parse(policies) -> tuple[ReviewPolicy, list[tuple[str, str, str]]]:
    """`(politique, constats)` — ne lève jamais, pour que `canon check` rapporte.

    Chaque constat est `(code, gravité, message)`, la gravité valant `error`
    (déclaration inutilisable) ou `warning` (règle ignorée, le reste s'applique).
    """
    problems: list[tuple[str, str, str]] = []
    if policies is None:
        return ReviewPolicy(), problems
    if not isinstance(policies, dict):
        return ReviewPolicy(), [(INVALID, "error",
                                 "review_policies doit être un mapping")]

    default = policies.get("default", DEFAULT_CLASS)
    if policies and "default" not in policies:
        # mesh-design : une politique qui existe doit dire sa classe par défaut,
        # sinon un fichier hors portée est classé par un défaut implicite. Un
        # mapping vide n'est pas une politique : pas d'avertissement.
        problems.append((NO_DEFAULT, "warning",
                         "review_policies.risk_classes sans `default` : %s par défaut"
                         % DEFAULT_CLASS))
    if not isinstance(default, str) or default not in CLASSES:
        problems.append((UNKNOWN_CLASS, "error",
                         "classe par défaut inconnue : %r (attendu : %s)"
                         % (default, ", ".join(CLASSES))))
        default = DEFAULT_CLASS

    raw_rules = policies.get("rules", [])
    if not isinstance(raw_rules, list):
        problems.append((BAD_RULE, "error",
                         "review_policies.rules doit être une liste"))
        raw_rules = []

    rules: list[Rule] = []
    for index, entry in enumerate(raw_rules):
        where = "règle %d" % (index + 1)
        if not isinstance(entry, dict):
            problems.append((BAD_RULE, "error", "%s : mapping attendu" % where))
            continue
        paths = entry.get("paths")
        if not isinstance(paths, list) or not paths:
            problems.append((BAD_RULE, "error",
                             "%s : `paths` doit être une liste non vide" % where))
            continue
        if any(not isinstance(p, str) or not p.strip() for p in paths):
            problems.append((BAD_RULE, "error",
                             "%s : chaque `paths` doit être un motif non vide" % where))
            continue
        if "class" not in entry:
            problems.append((BAD_RULE, "error", "%s : `class` manquante" % where))
            continue
        cls = entry.get("class")
        if not isinstance(cls, str) or cls not in CLASSES:
            problems.append((UNKNOWN_CLASS, "error",
                             "%s : classe inconnue %r (attendu : %s)"
                             % (where, cls, ", ".join(CLASSES))))
            continue
        regexes = []
        failed = False
        for pattern in paths:
            try:
                regexes.append(compile_glob(pattern))
            except re.error as exc:
                problems.append((BAD_GLOB, "error",
                                 "%s : motif %r invalide (%s)" % (where, pattern, exc)))
                failed = True
        if failed:
            continue
        rules.append(Rule(paths=tuple(paths), cls=cls, regexes=tuple(regexes)))
    return ReviewPolicy(default=default, rules=tuple(rules)), problems


def policy_from_federation(federation) -> tuple[ReviewPolicy, list[tuple[str, str, str]]]:
    """La politique de revue lue au canon (profil ameesh, spec §4.2).

    Elle vit sous `review_policies.risk_classes` : `review_policies` porte aussi
    les politiques d'OKF Federation (par exemple `self_approval`), qu'ameesh ne
    redéfinit pas et n'interprète pas ici — un manifeste qui ne déclare pas
    `risk_classes` garde la politique par défaut, sans constat.
    """
    if not isinstance(federation, dict):
        return ReviewPolicy(), []
    policies = federation.get("review_policies")
    if policies is None:
        return ReviewPolicy(), []
    if not isinstance(policies, dict):
        return ReviewPolicy(), [(INVALID, "error",
                                 "review_policies doit être un mapping")]
    if "risk_classes" not in policies:
        return ReviewPolicy(), []
    return parse(policies["risk_classes"])


def classify(policy: ReviewPolicy, paths) -> dict:
    """La classe d'un changement : la plus haute de ses fichiers.

    `paths` est une liste de chemins (relatifs au dépôt). Un chemin hors dépôt
    (`..`, absolu) est refusé par [`ReviewError`] : la politique ne saurait pas
    le classer, et le silence vaudrait une classe par défaut rassurante.
    """
    files = []
    for raw in paths:
        value = str(raw).strip()
        if value.startswith("/") or _WINDOWS_ABSOLUTE.match(value):
            raise ReviewError("chemin absolu : %r" % raw)
        path = normalize_path(value)
        if not path or path == ".." or path.startswith("../"):
            raise ReviewError("chemin hors dépôt : %r" % raw)
        cls, rule = policy.class_of(path)
        files.append({"path": path, "class": cls,
                      "rule": list(rule.paths) if rule else None})
    if not files:
        raise ReviewError("aucun fichier à classer")
    files.sort(key=lambda item: item["path"])
    overall = max((item["class"] for item in files), key=lambda name: RANK[name])
    return {
        "class": overall,
        "class_display": DISPLAY[overall],
        "default_used": any(item["rule"] is None for item in files),
        "files": files,
    }
