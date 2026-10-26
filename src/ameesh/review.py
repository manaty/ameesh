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

La déclaration peut être **par projet** : `risk_classes.projects.<nom>` porte la
politique d'un dépôt, les clés restées au premier niveau (`default`, `rules`)
celle d'un projet non listé. Les globs d'un projet ne classent donc jamais les
fichiers d'un autre (§8 du canon : « chaque dépôt déclare ses chemins
sensibles ») ; la forme globale d'avant reste lue telle quelle.

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
DISPLAY = {"light": "light", "normal": "normal", "sensitive": "sensitive"}
#: codes de constat produits par [`parse`] (préfixe des constats du canon)
INVALID = "review-policies-invalid"
UNKNOWN_CLASS = "review-policies-class-unknown"
BAD_RULE = "review-policies-rule-invalid"
BAD_GLOB = "review-policies-glob-invalid"
#: une politique existe mais ne dit pas sa classe par défaut (avertissement)
NO_DEFAULT = "review-policies-default-missing"
#: la clé qui porte les politiques par projet
PROJECTS_KEY = "projects"
#: un projet demandé n'est pas déclaré sous `risk_classes.projects` (avertissement)
UNKNOWN_PROJECT = "review-policies-project-unknown"
#: `risk_classes.projects` n'est pas un mapping nom → politique
BAD_PROJECT = "review-policies-project-invalid"

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
    #: SQL, sécurité ou prod : deux familles, même hors motifs de secours.
    critical: bool = False


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

    Un backslash **n'est pas** converti en séparateur : sur un dépôt POSIX
    c'est un caractère de nom de fichier comme un autre, et le relire en `/`
    ferait matcher une portée qui ne couvre pas ce fichier (revue #544 r1).
    [`classify`] refuse un tel chemin, comme un `..` ou un composant vide.

    Les `..` **intérieurs** sont résolus (`a/../b` → `b`), un `..` qui sortirait
    du dépôt est conservé pour que [`classify`] le refuse, et un `/` de tête
    aussi (un chemin absolu n'est pas un chemin de dépôt).
    """
    value = str(path).strip()
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


def is_catch_all(pattern: str) -> bool:
    """Le motif est-il une portée générique, sans aucun littéral ?

    Un motif non vide composé uniquement de `*`, `?` et `/` (`*`, `*/*`, `?`,
    `**`, `**/*`, `***`, `*/**`…) ne nomme aucun chemin : il couvre une portée
    générique, peut relever un compte d'avis, jamais abaisser le plancher
    critique (#3542, #3562). [`compile_glob`] fait de `**` et de `**/*` des
    motifs qui traversent les séparateurs ; `*` et `*/*` n'y couvrent qu'un ou
    deux composants, mais ne nomment pas davantage le chemin qu'ils matchent.
    Un littéral (`deploy/**`, `**/*.sql`, `src/ameesh/**`, `*.py`) nomme, lui,
    la portée qu'il couvre. Un `/` de tête est ancré à la racine, les
    composants vides sont retirés.
    """
    text = normalize_path(str(pattern or ""))
    if text.startswith("/"):
        text = text[1:]
    return bool(text) and all(char in "*/?" for char in text)


def declared_reviews(rules, paths) -> tuple[dict, int]:
    """Les avis déclarés par chemin (`train_reviews`), nommés vs attrape-tout.

    `rules` est une suite de `(motifs, avis)` : les motifs sont les chaînes du
    canon, lues au même commit. Rend `(nommés, attrape-tout)` :
    `nommés[chemin]` est le plus grand nombre d'avis d'une règle qui **nomme**
    ce chemin (au moins un de ses motifs, autre qu'un attrape-tout, le couvre) ;
    `attrape-tout` est le plus grand nombre porté par une règle sans littéral
    (`**`, `*`, `*/*`…) qui **couvre effectivement** au moins un chemin changé.
    Un `**` couvre tous les chemins sans en nommer aucun : il peut relever un
    compte, jamais abaisser le plancher critique (#3542). Un attrape-tout plus
    étroit (`*`, `*/*`, `*/**`, `*/*/**`) ne relève de même que s'il matche un
    chemin du diff : sa portée se lit, elle ne se suppose pas (#3562). Un
    motif invalide lève `re.error`, comme [`compile_glob`] : l'appelant en fait
    un doute, jamais un repli rassurant.
    """
    wanted = [str(path) for path in paths or () if str(path or "")]
    named: dict[str, int] = {}
    wide = 0
    for patterns, reviews in rules or ():
        count = int(reviews)
        compiled = []
        catch = []
        for pattern in patterns or ():
            regex = compile_glob(pattern)
            if is_catch_all(pattern):
                catch.append(regex)
            else:
                compiled.append(regex)
        if wanted and any(regex.fullmatch(path)
                          for regex in catch for path in wanted):
            wide = max(wide, count)
        for path in wanted:
            if any(regex.fullmatch(path) for regex in compiled):
                named[path] = max(named.get(path, 0), count)
    return named, wide


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
                                 "review_policies must be a mapping")]

    default = policies.get("default", DEFAULT_CLASS)
    if policies and "default" not in policies:
        # mesh-design : une politique qui existe doit dire sa classe par défaut,
        # sinon un fichier hors portée est classé par un défaut implicite. Un
        # mapping vide n'est pas une politique : pas d'avertissement.
        problems.append((NO_DEFAULT, "warning",
                         "review_policies.risk_classes without `default`: %s by default"
                         % DEFAULT_CLASS))
    if not isinstance(default, str) or default not in CLASSES:
        problems.append((UNKNOWN_CLASS, "error",
                         "unknown default class: %r (expected: %s)"
                         % (default, ", ".join(CLASSES))))
        default = DEFAULT_CLASS

    raw_rules = policies.get("rules", [])
    if not isinstance(raw_rules, list):
        problems.append((BAD_RULE, "error",
                         "review_policies.rules must be a list"))
        raw_rules = []

    rules: list[Rule] = []
    for index, entry in enumerate(raw_rules):
        where = "rule %d" % (index + 1)
        if not isinstance(entry, dict):
            problems.append((BAD_RULE, "error", "%s: mapping expected" % where))
            continue
        paths = entry.get("paths")
        if not isinstance(paths, list) or not paths:
            problems.append((BAD_RULE, "error",
                             "%s: `paths` must be a non-empty list" % where))
            continue
        if any(not isinstance(p, str) or not p.strip() for p in paths):
            problems.append((BAD_RULE, "error",
                             "%s: each `paths` entry must be a non-empty pattern" % where))
            continue
        if "class" not in entry:
            problems.append((BAD_RULE, "error", "%s: `class` missing" % where))
            continue
        cls = entry.get("class")
        if not isinstance(cls, str) or cls not in CLASSES:
            problems.append((UNKNOWN_CLASS, "error",
                             "%s: unknown class %r (expected: %s)"
                             % (where, cls, ", ".join(CLASSES))))
            continue
        critical = entry.get("critical", False)
        if not isinstance(critical, bool) or (critical and cls != "sensitive"):
            problems.append((BAD_RULE, "error",
                             "%s: `critical` must be a boolean, and true requires "
                             "class sensitive" % where))
            continue
        regexes = []
        failed = False
        for pattern in paths:
            try:
                regexes.append(compile_glob(pattern))
            except re.error as exc:
                problems.append((BAD_GLOB, "error",
                                 "%s: invalid pattern %r (%s)" % (where, pattern, exc)))
                failed = True
        if failed:
            continue
        rules.append(Rule(paths=tuple(paths), cls=cls, regexes=tuple(regexes),
                          critical=critical))
    return ReviewPolicy(default=default, rules=tuple(rules)), problems


@dataclass(frozen=True)
class ScopedPolicies:
    """`review_policies.risk_classes` : les politiques par projet, et le repli.

    `problems` porte les constats de **toute** la déclaration (le nom du projet
    préfixe les siens), pour que `canon check` les rapporte d'un coup.
    """

    projects: dict = field(default_factory=dict)
    fallback: ReviewPolicy = ReviewPolicy()
    #: le repli est déclaré (`default`/`rules` au premier niveau) : il classe
    #: vraiment un projet non listé. Sinon un projet inconnu est un doute.
    fallback_declared: bool = False
    problems: tuple = ()

    def policy_of(self, project=None) -> tuple[ReviewPolicy, list[tuple[str, str, str]]]:
        """La politique de `project` (None : le repli), et ses constats.

        Un projet nommé mais non déclaré n'est classable que si le repli est
        déclaré : sinon c'est un **doute** (constat `error`), pour qu'un
        appelant prudent — la fusion automatique d'un lot, un transfert pour
        budget — refuse au lieu de classer avec une politique qui n'est pas
        celle du projet.
        """
        problems = list(self.problems)
        if project is None or not self.projects:
            # Pas de projet demandé, ou déclaration globale d'avant (aucun
            # projet déclaré) : le repli est la politique.
            return self.fallback, problems
        if project in self.projects:
            return self.projects[project], problems
        if self.fallback_declared:
            problems.append((UNKNOWN_PROJECT, "warning",
                             "project %r is not declared under risk_classes.projects "
                             "(the declared fallback policy applies)" % (project,)))
        else:
            problems.append((UNKNOWN_PROJECT, "error",
                             "project %r is not declared under risk_classes.projects "
                             "and no fallback policy is declared" % (project,)))
        return self.fallback, problems


def parse_scoped(raw) -> ScopedPolicies:
    """`risk_classes` en politiques par projet, sans jamais lever.

    Deux formes cohabitent :
    - `projects: {nom: politique}` : chaque politique est validée comme une
      politique globale, ses constats préfixés du nom du projet ;
    - les clés `default` et `rules` restées au premier niveau forment le repli,
      celui d'un projet non listé — c'est la forme globale d'avant, inchangée.
    """
    if raw is None:
        return ScopedPolicies()
    if not isinstance(raw, dict):
        return ScopedPolicies(
            problems=((INVALID, "error",
                       "review_policies.risk_classes must be a mapping"),))
    rest = {key: value for key, value in raw.items() if key != PROJECTS_KEY}
    fallback, problems = parse(rest if rest else None)
    found: list[tuple[str, str, str]] = list(problems)
    projects: dict[str, ReviewPolicy] = {}
    raw_projects = raw.get(PROJECTS_KEY)
    if raw_projects is not None:
        if not isinstance(raw_projects, dict):
            found.append((BAD_PROJECT, "error",
                          "review_policies.risk_classes.projects must be a mapping"))
        else:
            for name in sorted(raw_projects, key=str):
                if not isinstance(name, str) or not name.strip():
                    found.append((BAD_PROJECT, "error",
                                  "project names must be non-empty strings: %r" % (name,)))
                    continue
                policy, project_problems = parse(raw_projects[name])
                projects[name] = policy
                found.extend((code, severity, "project %s: %s" % (name, message))
                             for code, severity, message in project_problems)
    return ScopedPolicies(projects=projects, fallback=fallback,
                          fallback_declared=bool(rest), problems=tuple(found))


def scoped_from_federation(federation) -> ScopedPolicies:
    """`risk_classes` lu au canon, par projet (profil ameesh, spec §4.2)."""
    if not isinstance(federation, dict):
        return ScopedPolicies()
    policies = federation.get("review_policies")
    if policies is None:
        return ScopedPolicies()
    if not isinstance(policies, dict):
        return ScopedPolicies(problems=((INVALID, "error",
                                         "review_policies must be a mapping"),))
    if "risk_classes" not in policies:
        return ScopedPolicies()
    return parse_scoped(policies["risk_classes"])


def policy_from_federation(federation, project=None) -> tuple[ReviewPolicy, list[tuple[str, str, str]]]:
    """La politique de revue d'un projet lue au canon (profil ameesh, spec §4.2).

    Elle vit sous `review_policies.risk_classes` : `review_policies` porte aussi
    les politiques d'OKF Federation (par exemple `self_approval`), qu'ameesh ne
    redéfinit pas et n'interprète pas ici — un manifeste qui ne déclare pas
    `risk_classes` garde la politique par défaut, sans constat. `project` absent
    ou non déclaré : la politique de repli (`ScopedPolicies.policy_of`).
    """
    return scoped_from_federation(federation).policy_of(project)


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
            raise ReviewError("absolute path: %r" % raw)
        if "\\" in value or any(part in ("", "..") for part in value.split("/")):
            # Un backslash n'est pas un séparateur POSIX ; `..` et un composant
            # vide ne sont pas des chemins de dépôt. Les relire en un autre
            # chemin (`.github/workflows/..\\..\\docs/x` → `docs/x`) ferait
            # matcher une portée plus légère : refus, donc doute (revue #544 r1).
            raise ReviewError("path is not a repository path: %r" % raw)
        path = normalize_path(value)
        if not path or path == ".." or path.startswith("../"):
            raise ReviewError("path outside the repository: %r" % raw)
        cls, rule = policy.class_of(path)
        files.append({"path": path, "class": cls,
                      "rule": list(rule.paths) if rule else None})
        # Une autre règle de même classe peut porter la criticité : la
        # première règle gagnante n'a pas seule autorité pour l'abaisser.
        if any(rule.critical and any(rx.match(path) for rx in rule.regexes)
               for rule in policy.rules):
            files[-1]["critical"] = True
    if not files:
        raise ReviewError("no file to classify")
    files.sort(key=lambda item: item["path"])
    overall = max((item["class"] for item in files), key=lambda name: RANK[name])
    return {
        "class": overall,
        "class_display": DISPLAY[overall],
        "default_used": any(item["rule"] is None for item in files),
        "files": files,
    }
