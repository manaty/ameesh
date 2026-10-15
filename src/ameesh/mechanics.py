# SPDX-License-Identifier: AGPL-3.0-only
"""Paramètres mécaniques du processus, compilés depuis le canon (#1606 §7, #1616).

Le canon (`processus/mecanique.yaml`, manaty/home) déclare, un bloc par règle
mécanique, ce que l'outillage exécute sans agent :

- `cutoffs` et `timezone` : les coupures du train, en heure locale IANA ;
- `build.max_in_build` : le plafond de lots en développement par agent ;
- `ci.packet`, `ci.incident_label`, `ci.environment_red`, `ci.faulty_lot` :
  une suite par paquet, l'étiquette qui autorise exceptionnellement la CI d'un
  lot, le sort d'un rouge seulement en CI (vert en local) et celui d'un lot
  fautif ;
- `reviews.families`, `exclude_author_family`, `distinct_families` : les
  familles de modèle et l'indépendance des avis ;
- `merge.named_fire`, `repeat_freeze`, `on_green_packet`, `post_merge_red` :
  la fusion du paquet dès la suite verte, sans feu nominatif ni gel répété.

Les classes sensibles et le nombre d'avis par chemin ne sont pas répétés : ils
viennent de `federation.yaml` (`review_policies.risk_classes` et
`review_policies.train_reviews`, PR85), lus au **même commit exact**.

Ce module est le lecteur strict : [`load`] et [`load_global`] refusent (aucun
repli permissif) un document absent, un schéma inconnu, une règle manquante ou
mal formée. `ameesh canon sync` compile le document global pour le signaler
(constat, jamais bloquant) ; un consommateur raccordé — le train #1467, la
désignation #1617 — appelle [`load`] avec son projet et reçoit un objet
immuable qui porte le commit audité.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import canon, review

#: version du contrat `processus/mecanique.yaml`
SCHEMA = "ameesh-mechanics/1"
#: chemin du document dans le canon
PATH = "processus/mecanique.yaml"
#: déclaration des politiques de revue, lue au même commit
FEDERATION_PATH = "federation.yaml"
#: version du contrat des nombres d'avis versionnés (home PR85)
TRAIN_REVIEWS_SCHEMA = "ameesh-train-reviews-policy/1"
#: modes de CI par paquet acceptés
CI_PACKETS = ("full", "impacted", "none")
#: sort d'un test rouge seulement en CI et vert en local (§11 bis, 15/10/2026 14:45)
ENVIRONMENT_RED = ("does_not_block", "blocks")
#: sort d'un lot fautif dans la CI du paquet (§11 bis)
FAULTY_LOT = ("removed_without_new_review", "reviewed")
#: sort d'un rouge découvert après la fusion (§11 bis)
POST_MERGE_RED = ("forward_or_revert", "freeze")

_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")

_DOCUMENT_KEYS = ("schema", "timezone", "cutoffs", "build", "ci", "reviews", "merge", "projects")
_OVERRIDE_KEYS = ("timezone", "cutoffs", "build", "ci")
_BUILD_KEYS = ("max_in_build",)
_CI_KEYS = ("packet", "incident_label", "environment_red", "faulty_lot")
_REVIEWS_KEYS = ("families", "exclude_author_family", "distinct_families")
_MERGE_KEYS = ("named_fire", "repeat_freeze", "on_green_packet", "post_merge_red")


class MechanicsError(canon.CanonError):
    """Règle absente, inconnue ou mal formée : le consommateur refuse."""


class MechanicsAbsent(MechanicsError):
    """Le document n'est pas un fichier ordinaire au commit exact."""


@dataclass(frozen=True)
class ProjectOverride:
    """Les blocs qu'un projet remplace, tels que déclarés (None : non déclaré)."""

    name: str
    timezone: str | None = None
    cutoffs: tuple[str, ...] | None = None
    max_in_build: int | None = None
    ci_packet: str | None = None
    ci_incident_label: str | None = None
    ci_environment_red: str | None = None
    ci_faulty_lot: str | None = None


@dataclass(frozen=True)
class Document:
    """Le document global compilé et validé, sans politique de revue."""

    schema: str
    timezone: str
    cutoffs: tuple[str, ...]
    max_in_build: int
    ci_packet: str
    ci_incident_label: str
    ci_environment_red: str
    ci_faulty_lot: str
    families: tuple[str, ...]
    exclude_author_family: bool
    distinct_families: bool
    merge_named_fire: bool
    merge_repeat_freeze: bool
    merge_on_green_packet: bool
    merge_post_merge_red: str
    overrides: tuple[ProjectOverride, ...] = ()

    def override(self, project: str) -> ProjectOverride | None:
        for item in self.overrides:
            if item.name == project:
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "timezone": self.timezone,
            "cutoffs": list(self.cutoffs),
            "build": {"max_in_build": self.max_in_build},
            "ci": {"packet": self.ci_packet,
                   "incident_label": self.ci_incident_label,
                   "environment_red": self.ci_environment_red,
                   "faulty_lot": self.ci_faulty_lot},
            "reviews": {"families": list(self.families),
                        "exclude_author_family": self.exclude_author_family,
                        "distinct_families": self.distinct_families},
            "merge": {"named_fire": self.merge_named_fire,
                      "repeat_freeze": self.merge_repeat_freeze,
                      "on_green_packet": self.merge_on_green_packet,
                      "post_merge_red": self.merge_post_merge_red},
            "projects": sorted(item.name for item in self.overrides),
        }


@dataclass(frozen=True)
class ReviewRule:
    """Un nombre d'avis par motif de chemin (`train_reviews`, PR85)."""

    paths: tuple[str, ...]
    reviews: int
    regexes: tuple = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class ReviewNeed:
    """Ce qu'un changement demande : classe de risque et nombre d'avis."""

    risk: str
    reviews: int


@dataclass(frozen=True)
class Mechanics:
    """Les paramètres compilés d'un projet, immuables, liés à un commit exact."""

    schema: str
    commit: str
    project: str
    timezone: str
    cutoffs: tuple[str, ...]
    max_in_build: int
    ci_packet: str
    ci_incident_label: str
    ci_environment_red: str
    ci_faulty_lot: str
    families: tuple[str, ...]
    exclude_author_family: bool
    distinct_families: bool
    merge_named_fire: bool
    merge_repeat_freeze: bool
    merge_on_green_packet: bool
    merge_post_merge_red: str
    review_policy: review.ReviewPolicy
    rules: tuple[ReviewRule, ...]
    source: str = ""

    # -- coupures -----------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def cutoffs_on(self, day: date) -> tuple[tuple[float, str], ...]:
        """Les coupures d'un jour local, `(epoch UTC, libellé HH:MM)`."""
        zone = self.zone()
        out = []
        for label in self.cutoffs:
            stamp = datetime(day.year, day.month, day.day,
                             int(label[:2]), int(label[3:]), tzinfo=zone)
            out.append((stamp.timestamp(), label))
        return tuple(out)

    def cutoff(self, now: float) -> float:
        """Dernière coupure locale à `now` ou avant (même contrat que `train.cutoff`)."""
        local = datetime.fromtimestamp(now, self.zone())
        for offset in (0, -1):
            before = [stamp for stamp, _ in self.cutoffs_on(local.date() + timedelta(days=offset))
                      if stamp <= now]
            if before:
                return max(before)
        raise MechanicsError("no cutoff declared before %.3f" % now)

    def next_cutoff(self, now: float) -> float:
        """Première coupure locale strictement après `now`."""
        local = datetime.fromtimestamp(now, self.zone())
        for offset in (0, 1):
            after = [stamp for stamp, _ in self.cutoffs_on(local.date() + timedelta(days=offset))
                     if stamp > now]
            if after:
                return min(after)
        raise MechanicsError("no cutoff declared after %.3f" % now)

    # -- revue --------------------------------------------------------------

    def reviews_for(self, paths) -> ReviewNeed:
        """Classe et nombre d'avis d'un changement décrit par ses chemins.

        Un chemin non léger sans nombre d'avis déclaré fait refuser : le
        lecteur ne suppose jamais un repli (`train_reviews`, PR85).
        """
        if isinstance(paths, (str, bytes)):
            raise MechanicsError("a list of repository paths is required")
        try:
            paths = list(paths)
        except TypeError as exc:
            # une entrée non itérable (None, un nombre…) est un appel hors
            # contrat : elle refuse par MechanicsError, jamais par TypeError
            raise MechanicsError("a list of repository paths is required") from exc
        if not paths:
            # un changement sans fichier ne se classe pas : zéro avis serait un repli
            raise MechanicsError("no path to classify")
        risk, reviews = "light", 0
        for path in paths:
            if not isinstance(path, str):
                raise MechanicsError("path is not a repository path: %r" % path)
            try:
                path.encode("utf-8")
                review.classify(self.review_policy, [path])
            except (UnicodeError, review.ReviewError) as exc:
                raise MechanicsError("path is not a repository path: %r" % path) from exc
            clean = review.normalize_path(path)
            if clean != path or "\ufffd" in path or "\0" in path:
                # un chemin non décodable (octets remplacés) ne se classe pas :
                # le deviner ferait passer un fichier sensible pour léger
                raise MechanicsError("path is not a repository path: %r" % path)
            cls, _rule = self.review_policy.class_of(clean)
            count = max((rule.reviews for rule in self.rules
                         if any(regex.fullmatch(clean) for regex in rule.regexes)),
                        default=0)
            if cls != "light" and not count:
                raise MechanicsError("review count unknown for non-light path: %s" % clean)
            if count:
                cls = "sensitive"
            if review.RANK[cls] > review.RANK[risk]:
                risk = cls
            reviews = max(reviews, count)
        return ReviewNeed(risk, reviews)

    def to_dict(self) -> dict:
        payload = {
            "schema": self.schema,
            "commit": self.commit,
            "project": self.project,
            "timezone": self.timezone,
            "cutoffs": list(self.cutoffs),
            "build": {"max_in_build": self.max_in_build},
            "ci": {"packet": self.ci_packet,
                   "incident_label": self.ci_incident_label,
                   "environment_red": self.ci_environment_red,
                   "faulty_lot": self.ci_faulty_lot},
            "reviews": {"families": list(self.families),
                        "exclude_author_family": self.exclude_author_family,
                        "distinct_families": self.distinct_families},
            "merge": {"named_fire": self.merge_named_fire,
                      "repeat_freeze": self.merge_repeat_freeze,
                      "on_green_packet": self.merge_on_green_packet,
                      "post_merge_red": self.merge_post_merge_red},
            "review_rules": [{"paths": list(rule.paths), "reviews": rule.reviews}
                             for rule in self.rules],
        }
        if self.source:
            payload["source"] = self.source
        return payload


# --------------------------------------------------------------------------
# validation stricte du document
# --------------------------------------------------------------------------

def _mapping(value, where: str, keys) -> dict:
    if not isinstance(value, dict):
        raise MechanicsError("%s: a mapping is required" % where)
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise MechanicsError("%s: unknown key(s): %s" % (where, ", ".join(unknown)))
    return value


def _cutoffs(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MechanicsError("%s: a non-empty list of \"HH:MM\" is required" % where)
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _TIME.fullmatch(item):
            raise MechanicsError("%s: local time \"HH:MM\" expected, got %r" % (where, item))
        if item in seen:
            raise MechanicsError("%s: duplicate cutoff %s" % (where, item))
        seen.add(item)
        out.append(item)
    return tuple(out)


def _positive_int(value, where: str) -> int:
    if type(value) is not int or value <= 0:
        raise MechanicsError("%s: a positive integer is required" % where)
    return value


def _boolean(value, where: str) -> bool:
    if type(value) is not bool:
        raise MechanicsError("%s: a boolean is required" % where)
    return value


def _timezone(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise MechanicsError("%s: an IANA timezone is required" % where)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise MechanicsError("%s: unknown IANA timezone %r" % (where, value)) from exc
    return value


def _ci(value, where: str) -> tuple[str, str, str, str]:
    data = _mapping(value, where, _CI_KEYS)
    packet = data.get("packet")
    if packet not in CI_PACKETS:
        raise MechanicsError("%s.packet: %s expected" % (where, " | ".join(CI_PACKETS)))
    label = data.get("incident_label")
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise MechanicsError("%s.incident_label: a label is required" % where)
    environment = data.get("environment_red")
    if environment not in ENVIRONMENT_RED:
        raise MechanicsError("%s.environment_red: %s expected"
                             % (where, " | ".join(ENVIRONMENT_RED)))
    faulty = data.get("faulty_lot")
    if faulty not in FAULTY_LOT:
        raise MechanicsError("%s.faulty_lot: %s expected" % (where, " | ".join(FAULTY_LOT)))
    return packet, label, environment, faulty


def _merge(value, where: str) -> tuple[bool, bool, bool, str]:
    """La règle de fusion du paquet (§11 bis, allègements 15/10/2026 14:45)."""
    data = _mapping(value, where, _MERGE_KEYS)
    post = data.get("post_merge_red")
    if post not in POST_MERGE_RED:
        raise MechanicsError("%s.post_merge_red: %s expected"
                             % (where, " | ".join(POST_MERGE_RED)))
    return (_boolean(data.get("named_fire"), where + ".named_fire"),
            _boolean(data.get("repeat_freeze"), where + ".repeat_freeze"),
            _boolean(data.get("on_green_packet"), where + ".on_green_packet"),
            post)


def _families(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MechanicsError("%s: a non-empty list of families is required" % where)
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise MechanicsError("%s: a family name is required" % where)
        if item in out:
            raise MechanicsError("%s: duplicate family %s" % (where, item))
        out.append(item)
    return tuple(out)


def _overrides(value, where: str) -> tuple[ProjectOverride, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise MechanicsError("%s: a mapping is required" % where)
    out = []
    for name, block in value.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise MechanicsError("%s: non-empty project names are required" % where)
        scope = "%s.%s" % (where, name)
        data = _mapping(block, scope, _OVERRIDE_KEYS)
        timezone = cutoffs = None
        max_in_build = None
        packet = label = environment = faulty = None
        if "timezone" in data:
            timezone = _timezone(data["timezone"], scope + ".timezone")
        if "cutoffs" in data:
            cutoffs = _cutoffs(data["cutoffs"], scope + ".cutoffs")
        if "build" in data:
            build = _mapping(data["build"], scope + ".build", _BUILD_KEYS)
            max_in_build = _positive_int(build.get("max_in_build"),
                                         scope + ".build.max_in_build")
        if "ci" in data:
            packet, label, environment, faulty = _ci(data["ci"], scope + ".ci")
        out.append(ProjectOverride(name=name, timezone=timezone, cutoffs=cutoffs,
                                   max_in_build=max_in_build, ci_packet=packet,
                                   ci_incident_label=label,
                                   ci_environment_red=environment,
                                   ci_faulty_lot=faulty))
    return tuple(out)


def compile_document(text: str, *, source: str = PATH) -> Document:
    """Compile le document global. Strict : toute règle manquante ou inconnue refuse."""
    try:
        # lecteur minimal forcé : la compilation est identique avec et sans PyYAML
        parsed = canon.load_yaml(text, use_pyyaml=False)
    except canon.YamlError as exc:
        raise MechanicsError("%s: unreadable YAML: %s" % (source, exc)) from exc
    if not isinstance(parsed, dict):
        raise MechanicsError("%s: a mapping is required" % source)
    unknown = sorted(set(parsed) - set(_DOCUMENT_KEYS))
    if unknown:
        raise MechanicsError("%s: unknown key(s): %s" % (source, ", ".join(unknown)))
    if parsed.get("schema") != SCHEMA:
        raise MechanicsError("%s: schema %s is required, got %r"
                             % (source, SCHEMA, parsed.get("schema")))
    build = _mapping(parsed.get("build"), source + ".build", _BUILD_KEYS)
    reviews = _mapping(parsed.get("reviews"), source + ".reviews", _REVIEWS_KEYS)
    packet, label, environment, faulty = _ci(parsed.get("ci"), source + ".ci")
    named_fire, repeat_freeze, on_green, post_merge = _merge(parsed.get("merge"),
                                                             source + ".merge")
    return Document(
        schema=SCHEMA,
        timezone=_timezone(parsed.get("timezone"), source + ".timezone"),
        cutoffs=_cutoffs(parsed.get("cutoffs"), source + ".cutoffs"),
        max_in_build=_positive_int(build.get("max_in_build"),
                                   source + ".build.max_in_build"),
        ci_packet=packet,
        ci_incident_label=label,
        ci_environment_red=environment,
        ci_faulty_lot=faulty,
        families=_families(reviews.get("families"), source + ".reviews.families"),
        exclude_author_family=_boolean(reviews.get("exclude_author_family"),
                                       source + ".reviews.exclude_author_family"),
        distinct_families=_boolean(reviews.get("distinct_families"),
                                   source + ".reviews.distinct_families"),
        merge_named_fire=named_fire,
        merge_repeat_freeze=repeat_freeze,
        merge_on_green_packet=on_green,
        merge_post_merge_red=post_merge,
        overrides=_overrides(parsed.get("projects"), source + ".projects"),
    )


# --------------------------------------------------------------------------
# lecture au commit exact
# --------------------------------------------------------------------------

def _exact_commit(repo: str, commit: str) -> str:
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise MechanicsError("an exact canon commit is required")
    try:
        resolved = canon.git_commit(repo, commit)
    except (canon.GitError, OSError) as exc:
        raise MechanicsError("cannot read the canon repository: %s" % exc) from exc
    if resolved != commit:
        raise MechanicsError("an exact canon commit is required")
    return commit


def _regular_blob(repo: str, commit: str, path: str) -> str:
    """Le texte d'un fichier ordinaire (`path`) au commit exact, sinon refuse.

    `_git_list` est le lecteur d'arbre du canon : il ne retient que les
    fichiers ordinaires (ni lien symbolique, ni sous-module), donc un chemin
    qui n'en est pas un est refusé comme absent.
    """
    try:
        listed = canon._git_list(repo, commit, path)
    except (canon.GitError, OSError) as exc:
        raise MechanicsError("cannot read the canon tree: %s" % exc) from exc
    if len([sha for name, sha in listed if name == path]) != 1:
        raise MechanicsAbsent("%s is absent at %s" % (path, commit))
    raw = canon.git_read(repo, commit, path)
    if raw is None:
        raise MechanicsAbsent("%s is unreadable at %s" % (path, commit))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MechanicsError("%s is not UTF-8 at %s" % (path, commit)) from exc


def load_global(repo: str, commit: str) -> Document:
    """Le document global (`processus/mecanique.yaml`) au commit exact."""
    commit = _exact_commit(repo, commit)
    return compile_document(_regular_blob(repo, commit, PATH), source=PATH)


def _federation(repo: str, commit: str) -> dict:
    try:
        parsed = canon.load_yaml(_regular_blob(repo, commit, FEDERATION_PATH),
                                 use_pyyaml=False)
    except canon.YamlError as exc:
        raise MechanicsError("%s: unreadable YAML: %s" % (FEDERATION_PATH, exc)) from exc
    if not isinstance(parsed, dict):
        raise MechanicsError("%s: a mapping is required" % FEDERATION_PATH)
    return parsed


def _declared(mapping, key: str, where: str) -> dict:
    if not isinstance(mapping, dict) or key not in mapping or not isinstance(mapping[key], dict):
        raise MechanicsError("%s is required" % where)
    return mapping[key]


def _review_policy(federation: dict, project: str, commit: str
                   ) -> tuple[review.ReviewPolicy, tuple[ReviewRule, ...]]:
    """`risk_classes` et `train_reviews` du projet, au même commit exact."""
    policies = _declared(federation, "review_policies", "review_policies")
    classes = _declared(policies, "risk_classes", "review_policies.risk_classes")
    scoped = _declared(classes, "projects", "review_policies.risk_classes.projects")
    if project not in scoped:
        raise MechanicsError("project %r is not declared under risk_classes.projects"
                             % project)
    declaration = scoped[project]
    # `default` seul est une politique complète (review.parse) ; `rules` absent = []
    if not isinstance(declaration, dict) or "default" not in declaration \
            or set(declaration) - {"default", "rules"}:
        raise MechanicsError("complete risk classification required for project %r" % project)
    risk_rules = declaration.get("rules", [])
    if not isinstance(risk_rules, list) or any(
            not isinstance(rule, dict) or set(rule) != {"paths", "class"}
            for rule in risk_rules):
        raise MechanicsError("explicit risk rules required for project %r" % project)
    policy, problems = review.parse(declaration)
    if problems:
        raise MechanicsError("risk classification of %r is unusable: %s"
                             % (project, "; ".join(sorted({text for _code, _sev, text
                                                            in problems}))))
    counts = _declared(policies, "train_reviews", "review_policies.train_reviews")
    if set(counts) != {"schema", "projects"} or counts.get("schema") != TRAIN_REVIEWS_SCHEMA:
        raise MechanicsError("unknown train review policy schema: %r"
                             % (counts.get("schema") if isinstance(counts, dict) else counts))
    projects = _declared(counts, "projects", "review_policies.train_reviews.projects")
    if project not in projects:
        raise MechanicsError("project %r has no declared train review rules" % project)
    entry = projects[project]
    if not isinstance(entry, dict) or set(entry) != {"rules"} \
            or not isinstance(entry["rules"], list):
        raise MechanicsError("explicit train review rules required for project %r" % project)
    rules: list[ReviewRule] = []
    for index, rule in enumerate(entry["rules"]):
        where = "review_policies.train_reviews.projects.%s.rules[%d]" % (project, index)
        if not isinstance(rule, dict) or set(rule) != {"paths", "reviews"}:
            raise MechanicsError("%s: `paths` and `reviews` are required" % where)
        if type(rule["reviews"]) is not int or rule["reviews"] not in (1, 2):
            raise MechanicsError("%s.reviews: 1 or 2 expected" % where)
        paths = rule["paths"]
        if not isinstance(paths, list) or not paths \
                or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise MechanicsError("%s.paths: a non-empty list of patterns is required" % where)
        try:
            regexes = tuple(review.compile_glob(p) for p in paths)
        except re.error as exc:
            raise MechanicsError("%s.paths: invalid pattern (%s)" % (where, exc)) from exc
        rules.append(ReviewRule(paths=tuple(paths), reviews=rule["reviews"],
                                regexes=regexes))
    return policy, tuple(rules)


def load(repo: str, commit: str, project: str) -> Mechanics:
    """Compile les paramètres d'un projet depuis le canon, au commit exact.

    Aucun repli : projet inconnu, bloc absent, schéma inconnu ou règle mal
    formée refusent, et `commit` doit être un commit exact (jamais une
    branche ou un tag).
    """
    if not isinstance(project, str) or not project.strip() or project != project.strip():
        raise MechanicsError("an explicit project is required")
    commit = _exact_commit(repo, commit)
    document = compile_document(_regular_blob(repo, commit, PATH), source=PATH)
    override = document.override(project)
    federation = _federation(repo, commit)
    policy, rules = _review_policy(federation, project, commit)
    return Mechanics(
        schema=document.schema,
        commit=commit,
        project=project,
        timezone=(override.timezone if override and override.timezone
                  else document.timezone),
        cutoffs=(override.cutoffs if override and override.cutoffs
                 else document.cutoffs),
        max_in_build=(override.max_in_build if override and override.max_in_build
                      else document.max_in_build),
        ci_packet=(override.ci_packet if override and override.ci_packet
                   else document.ci_packet),
        ci_incident_label=(override.ci_incident_label if override
                           and override.ci_incident_label
                           else document.ci_incident_label),
        ci_environment_red=(override.ci_environment_red if override
                            and override.ci_environment_red
                            else document.ci_environment_red),
        ci_faulty_lot=(override.ci_faulty_lot if override and override.ci_faulty_lot
                       else document.ci_faulty_lot),
        families=document.families,
        exclude_author_family=document.exclude_author_family,
        distinct_families=document.distinct_families,
        merge_named_fire=document.merge_named_fire,
        merge_repeat_freeze=document.merge_repeat_freeze,
        merge_on_green_packet=document.merge_on_green_packet,
        merge_post_merge_red=document.merge_post_merge_red,
        review_policy=policy,
        rules=rules,
        source="%s@%s:%s" % (repo, commit, PATH),
    )
