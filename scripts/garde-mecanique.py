#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2449 — garde mécanique : refuser un paramètre codé en dur hors du canon.

Le canon §7 (manaty/home#86, owner 15/10/2026 11:50) : « les paramètres de ces
gestes viennent du canon. Chaque règle mécanique a ici un bloc lisible par
machine, d'où l'outillage est compilé ou configuré. » `processus/mecanique.yaml`
est lu par `ameesh.mechanics` (`load`, `load_global`) ; aucun consommateur ne
doit réappliquer une valeur de ce document.

Cette garde est déterministe : elle analyse les sources suivies par Git (AST
Python et motifs shell/systemd/workflows, aucun import du code scanné,
aucun réseau, aucune horloge), hors
`tests/`, `reviews/`, `src/ameesh/mechanics.py` (le lecteur du canon) et ce
script (il porte les motifs et les exemptions). Pour chaque clé du schéma
`ameesh-mechanics` elle refuse les motifs littéraux connus — coupures et
fuseau, cadence des paquets, plafond de lots en build, politique CI, familles
et indépendance des avis, politique de fusion, seuils et plafonds de
prévalidation, seuil de lot du backlog (#2584), seuil et marges du staging
hors créneau (#3291), rythme des comptes de forfait (#3647), capacité au repos (#3256) — et exige une exemption nommée et datée pour un littéral
historique.

Une exemption vit dans `EXEMPTIONS` ci-dessous : chemin, paramètre, littéral
(`*` pour toutes les occurrences du paramètre dans le fichier), nom, date ISO
et raison. Elle est visible du relecteur ; elle ne couvre jamais un autre
paramètre ni un autre fichier. Une exemption inutilisée est signalée (le témoin
`tests/test_2449_garde_mecanique.py` refuse de la laisser traîner).

Ne sont pas des replis, et ne sont donc pas signalés : les contrôles de
capacité qui comparent une valeur déjà compilée (`rules.ci_packet != "full"`)
et les commentaires ou docstrings. La garde ne refuse que les littéraux qui
*fournissent* un paramètre — affectation, défaut de fonction, argument nommé,
mapping ou calendrier recopié.

#2457 : shell (.sh/.bash ou shebang sans suffixe), timers .timer et workflows
.github/workflows/*.{yml,yaml} : motifs 11:00/23:00, paire 11/23, cron à
minute zéro et OnCalendar. Contexte actif ou chemin de runner release/paquet/
train/coupure requis ; commentaires et liens symboliques exclus. Analyse
lexicale : pas d’expansion shell, de calcul, d’include ou de décodage YAML.

Usage :
  scripts/garde-mecanique.py [--root REPO] [--json]
Sortie : 0 si aucun littéral non exempté, 1 si au moins un, 2 en cas d'erreur
d'usage ou d'exemption mal formée.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime
import json
import pathlib
import re
import subprocess
import sys

#: racine du dépôt (le script vit dans scripts/)
REPO = pathlib.Path(__file__).resolve().parent.parent
#: chemin du script, jamais scanné (il porte les motifs et les exemptions)
GUARD_REL = "scripts/garde-mecanique.py"
#: le lecteur du canon est la source, pas un consommateur à contrôler
MECHANICS_REL = "src/ameesh/mechanics.py"
#: suites et artefacts de relecture : ni code de production ni consommateur
EXCLUDED_PREFIXES = ("tests/", "reviews/")

#: les clés du schéma `ameesh-mechanics` (document global, surcharges,
#: prévalidation et staging). Maintien : `tests/test_2449_garde_mecanique.py`
#: compare cette liste aux constantes du lecteur `src/ameesh/mechanics.py` ;
#: une clé ajoutée ou retirée là-bas sans la garde fait rougir le témoin.
SCHEMA_KEYS = (
    "schema",
    "timezone",
    "cutoffs",
    "packets.cutoffs",
    "build.max_in_build",
    "ci.packet",
    "ci.incident_label",
    "ci.environment_red",
    "ci.faulty_lot",
    "reviews.families",
    "reviews.exclude_author_family",
    "reviews.distinct_families",
    "reviews.light_reviews",
    "merge.named_fire",
    "merge.repeat_freeze",
    "merge.on_green_packet",
    "merge.post_merge_red",
    "prevalidation.classes",
    "prevalidation.contracts",
    "prevalidation.hosts",
    "prevalidation.parts_script",
    "prevalidation.timeout_seconds",
    "backlog.lot_min_value",
    "staging.waiting_lots_min",
    "staging.before_cutoff_seconds",
    "staging.before_packet_seconds",
    "accounts.pace_multipliers",
    "accounts.underused_loss",
    "paquet.attente_mediane_s",
    "paquet.max_paquets",
    "packet_tests.unit_timeout_seconds",
    "packet_tests.parallel",
    "packet_tests.total_timeout_seconds",
    "file.repos_part",
    "file.repos_s",
    "file.wake_mail_s",
    "auto_take.excluded_families",
    "auto_take.expire",
    "session.max_tokens",
    "session.max_tokens_by_harness",
    "production.branch",
    "projects",
)

#: familles de harnais connues du mesh (une liste `families` sans l'une
#: d'elles n'est pas la politique de revue : elle n'est pas signalée)
FAMILY_NAMES = frozenset({"claude", "codex", "deepseek"})
#: classes du plan L161f acceptées par le bloc `prevalidation`
PREVALIDATION_CLASSES = frozenset({"P0", "P1", "P2", "P3"})
#: valeurs de `ci.packet`
CI_PACKETS = frozenset({"full", "impacted", "none"})
_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_HOURLY = re.compile(r"^(?:[01][0-9]|2[0-3]):00$")
#: une coupure 11:00/23:00 recopiée dans une expression cron locale
_CRON_CUTOFFS = re.compile(r"\b0\s+11\s*,\s*23\s+\*\s+\*\s+\*")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: noms de variable, paramètre ou argument qui portent une clé du schéma.
#: Le nom seul ne suffit pas : `_valid_binding` vérifie la valeur (un
#: `packet=None` d'une API n'est pas un `ci.packet`).
BINDINGS = {
    "max_in_build": "build.max_in_build",
    "max_build": "build.max_in_build",
    "ci_packet": "ci.packet",
    "packet": "ci.packet",
    "incident_label": "ci.incident_label",
    "environment_red": "ci.environment_red",
    "faulty_lot": "ci.faulty_lot",
    "post_merge_red": "merge.post_merge_red",
    "named_fire": "merge.named_fire",
    "repeat_freeze": "merge.repeat_freeze",
    "on_green_packet": "merge.on_green_packet",
    "exclude_author_family": "reviews.exclude_author_family",
    "distinct_families": "reviews.distinct_families",
    "families": "reviews.families",
    "light_reviews": "reviews.light_reviews",
    "cutoffs": "cutoffs",
    "timezone": "timezone",
    "classes": "prevalidation.classes",
    "contracts": "prevalidation.contracts",
    "hosts": "prevalidation.hosts",
    "parts_script": "prevalidation.parts_script",
    "timeout_seconds": "prevalidation.timeout_seconds",
    "lot_min_value": "backlog.lot_min_value",
    "waiting_lots_min": "staging.waiting_lots_min",
    "before_cutoff_seconds": "staging.before_cutoff_seconds",
    "before_packet_seconds": "staging.before_packet_seconds",
    "pace_multipliers": "accounts.pace_multipliers",
    "underused_loss": "accounts.underused_loss",
    "attente_mediane_s": "paquet.attente_mediane_s",
    "max_paquets": "paquet.max_paquets",
    "unit_timeout_seconds": "packet_tests.unit_timeout_seconds",
    "unit_tests_parallel": "packet_tests.parallel",
    "total_timeout_seconds": "packet_tests.total_timeout_seconds",
    "repos_part": "file.repos_part",
    "repos_s": "file.repos_s",
    "wake_mail_s": "file.wake_mail_s",
    "production_branch": "production.branch",
    # #4448 : le seuil commun et sa table par harnais
    "auto_take_excluded_families": "auto_take.excluded_families",
    "excluded_families": "auto_take.excluded_families",
    "auto_take_expire": "auto_take.expire",
    "session_max_tokens": "session.max_tokens",
    "session_max_tokens_by_harness": "session.max_tokens_by_harness",
    "max_tokens_by_harness": "session.max_tokens_by_harness",
}

#: constantes d'implémentation historiques dont le nom ne porte pas la clé
#: complète (le `§4` nomme `BUILD_LIMIT`, `§11 bis` nomme `FAMILIES`…)
UPPER_BINDINGS = {
    "BUILD_LIMIT": "build.max_in_build",
    "MAX_IN_BUILD": "build.max_in_build",
    "FAMILIES": "reviews.families",
    "MECHANICS_FAMILIES": "reviews.families",
    "TZ": "timezone",
    "TIMEZONE": "timezone",
    "MECHANICS_SCHEMA": "schema",
    "SCHEMA": "schema",
    "PROJECT_OVERRIDES": "projects",
    "MECHANICS_PROJECTS": "projects",
}

#: clés de dictionnaire (configuration compilée recopiée en dur). `packet` et
#: `schema` y sont bornés par leur valeur (`full|impacted|none`, préfixe
#: `ameesh-mechanics/`) pour ne pas confondre un mapping étranger.
DICT_BINDINGS = {
    "max_in_build": "build.max_in_build",
    "packet": "ci.packet",
    "incident_label": "ci.incident_label",
    "environment_red": "ci.environment_red",
    "faulty_lot": "ci.faulty_lot",
    "post_merge_red": "merge.post_merge_red",
    "named_fire": "merge.named_fire",
    "repeat_freeze": "merge.repeat_freeze",
    "on_green_packet": "merge.on_green_packet",
    "exclude_author_family": "reviews.exclude_author_family",
    "distinct_families": "reviews.distinct_families",
    "families": "reviews.families",
    "light_reviews": "reviews.light_reviews",
    "cutoffs": "cutoffs",
    "timezone": "timezone",
    "schema": "schema",
    "lot_min_value": "backlog.lot_min_value",
    "waiting_lots_min": "staging.waiting_lots_min",
    "before_cutoff_seconds": "staging.before_cutoff_seconds",
    "before_packet_seconds": "staging.before_packet_seconds",
    "pace_multipliers": "accounts.pace_multipliers",
    "underused_loss": "accounts.underused_loss",
    "attente_mediane_s": "paquet.attente_mediane_s",
    "max_paquets": "paquet.max_paquets",
    "unit_timeout_seconds": "packet_tests.unit_timeout_seconds",
    "total_timeout_seconds": "packet_tests.total_timeout_seconds",
    "repos_part": "file.repos_part",
    "repos_s": "file.repos_s",
    "wake_mail_s": "file.wake_mail_s",
    # #4448 : une table de seuils par harnais recopiée dans un mapping
    "max_tokens_by_harness": "session.max_tokens_by_harness",
}
#: clés du bloc `prevalidation` dans un mapping, seulement dans un fichier
#: qui parle de prévalidation
PREVALIDATION_DICT_BINDINGS = {
    "classes": "prevalidation.classes",
    "contracts": "prevalidation.contracts",
    "hosts": "prevalidation.hosts",
    "parts_script": "prevalidation.parts_script",
    "timeout_seconds": "prevalidation.timeout_seconds",
}


class GuardError(ValueError):
    """Exemption mal formée : la garde refuse de deviner."""


@dataclasses.dataclass(frozen=True)
class Finding:
    """Un littéral mécanique trouvé dans une source de production."""

    path: str
    line: int
    parameter: str
    literal: str

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line,
                "parameter": self.parameter, "literal": self.literal}

    def render(self) -> str:
        return "%s:%d: %s = %s" % (self.path, self.line,
                                   self.parameter, self.literal)


@dataclasses.dataclass(frozen=True)
class Exemption:
    """Une exception nommée et datée à la garde, visible du relecteur."""

    path: str
    parameter: str
    literal: str
    name: str
    date: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or self.path.startswith("/") \
                or ".." in pathlib.PurePosixPath(self.path).parts:
            raise GuardError("exemption path: a repository-relative path is required")
        if self.parameter not in SCHEMA_KEYS:
            raise GuardError("exemption %s: unknown schema key %r"
                             % (self.path, self.parameter))
        if not isinstance(self.literal, str) or not self.literal:
            raise GuardError("exemption %s/%s: a literal (or *) is required"
                             % (self.path, self.parameter))
        if not isinstance(self.name, str) or not self.name.strip() \
                or not re.search(r"[0-9A-Za-z]", self.name):
            raise GuardError("exemption %s/%s: a name is required"
                             % (self.path, self.parameter))
        if not isinstance(self.date, str) or not _DATE.fullmatch(self.date):
            raise GuardError("exemption %s/%s: an ISO date (YYYY-MM-DD) is required"
                             % (self.path, self.parameter))
        try:
            datetime.date.fromisoformat(self.date)
        except ValueError as exc:
            raise GuardError("exemption %s/%s: invalid date %r"
                             % (self.path, self.parameter, self.date)) from exc
        if not isinstance(self.reason, str) or not self.reason.strip() \
                or not re.search(r"[0-9A-Za-z]", self.reason):
            raise GuardError("exemption %s/%s: a reason is required"
                             % (self.path, self.parameter))

    def covers(self, finding: Finding) -> bool:
        return (finding.path == self.path
                and finding.parameter == self.parameter
                and (self.literal == "*" or finding.literal == self.literal))

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


#: les littéraux historiques encore présents, chacun nommé et daté. Toute
#: nouvelle entrée passe par une relecture de ce fichier ; une exemption
#: inutilisée est signalée et le témoin refuse de la laisser traîner.
EXEMPTIONS = (
    Exemption(
        path="src/ameesh/review_lot.py",
        parameter="reviews.light_reviews",
        literal="1",
        name="lot #2642 (mty-amh-impl-08), 19/10",
        date="2026-10-19",
        reason="relecture après fusion : plancher historique d'un avis pour un "
               "lot léger, repli d'une classe qui ne porte pas encore le "
               "paramètre ; la valeur du canon (`light_reviews`) fait foi dès "
               "qu'elle est déclarée (retrait : PR home)",
    ),
    Exemption(
        path="src/ameesh/modules/dev/lot_class.py",
        parameter="reviews.exclude_author_family",
        literal="True",
        name="lot #2726 (ameesh-dev3), 20/10",
        date="2026-10-20",
        reason="repli en doute quand le canon est illisible : la direction "
               "stricte (True), jamais une valeur du canon, toujours lue par "
               "`_review_flags` puis écrasée. Valeur exacte : une exemption "
               "large couvrirait un False affaiblissant. Retrait quand "
               "lot_class lira un repli nommé du lecteur.",
    ),
    Exemption(
        path="src/ameesh/modules/dev/lot_class.py",
        parameter="reviews.exclude_author_family",
        literal="False",
        name="lot #2726 (ameesh-dev3), 20/10",
        date="2026-10-20",
        reason="état initial d'`of_lot` avant la lecture du canon, toujours "
               "écrasé par `class_of_paths` dès qu'un canon est lisible ; les "
               "retours précoces qui le gardent sont des doutes `sensitive`, "
               "où la porte applique déjà l'exclusion de famille. Valeur "
               "exacte : aucune autre valeur n'est couverte.",
    ),
    Exemption(
        path="src/ameesh/modules/dev/lot_class.py",
        parameter="reviews.distinct_families",
        literal="True",
        name="lot #2726 (ameesh-dev3), 20/10",
        date="2026-10-20",
        reason="repli en doute quand le canon est illisible (True) et état "
               "initial d'`of_lot` avant la lecture (True) : ces littéraux ne "
               "fournissent jamais la valeur du canon, toujours lue par "
               "`_review_flags` puis écrasée ; la direction est stricte (deux "
               "familles). Valeur exacte : une exemption large couvrirait un "
               "False affaiblissant.",
    ),
    Exemption(
        path="src/ameesh/config.py",
        parameter="session.max_tokens",
        literal="150000.0",
        name="lot #4448 (mty-amh-impl-05), 29/10",
        date="2026-10-29",
        reason="repli commun de l'exécuteur (AMEESH_SESSION_MAX_TOKENS) quand "
               "le canon ne déclare pas le bloc `session` ou est illisible ; "
               "la valeur du canon (commune ou par harnais) fait foi dès "
               "qu'elle est déclarée. Aucune valeur propre à un harnais ici.",
    ),
)


# --------------------------------------------------------------------------
# analyse AST
# --------------------------------------------------------------------------

def _docstrings(tree: ast.AST) -> set[int]:
    """Identités des constantes qui sont des docstrings (jamais du code)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _literal(node: ast.AST):
    """La valeur d'un nœud si elle est entièrement littérale, sinon (False, None).

    Les tuples/listes/ensembles de constantes et `frozenset({…})` sont des
    littéraux : c'est la forme d'une politique recopiée. Un nom, un appel
    ordinaire ou un chemin calculé ne l'est pas.
    """
    if isinstance(node, ast.Constant):
        # #3647 : un réel aussi (`underused_loss`, une part de quota) ; chaque
        # clé borne ensuite le type qu'elle accepte (`_valid_binding`)
        if isinstance(node.value, (str, int, bool, float)) or node.value is None:
            return True, node.value
        return False, None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = []
        for element in node.elts:
            ok, value = _literal(element)
            if not ok:
                return False, None
            values.append(value)
        if isinstance(node, ast.Set):
            values.sort(key=repr)
        return True, tuple(values)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "tuple")
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))):
        ok, value = _literal(node.args[0])
        if ok and node.func.id in ("frozenset", "set"):
            value = tuple(sorted(value, key=repr))
        return ok, value
    if isinstance(node, ast.Dict):
        return True, "<dict>"
    return False, None


def _literal_text(value) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_hourly(value) -> bool:
    """Une cadence horaire recopiée : au moins six `HH:00` distinctes."""
    if not isinstance(value, tuple) or len(value) < 6:
        return False
    if not all(isinstance(item, str) and _HOURLY.fullmatch(item) for item in value):
        return False
    return len(set(value)) >= 6


def _is_cutoff_literal(value) -> bool:
    """Une heure ou une liste d'heures de coupure, sous l'une de ses formes.

    `11`, `"11:00"`, `(11, 23)` et `["11:00", "23:00"]` sont la même règle
    recopiée : le nom (`CUTOFF…`, `cutoffs`) ne suffit pas, la valeur doit
    être une heure 0..23 ou un `HH:MM`.
    """
    if isinstance(value, str):
        return bool(_TIME.fullmatch(value))
    if type(value) is int:
        return 0 <= value <= 23
    if isinstance(value, tuple) and value:
        return all((isinstance(item, str) and _TIME.fullmatch(item))
                   or (type(item) is int and 0 <= item <= 23)
                   for item in value)
    return False


def _is_cutoff_pair(value) -> bool:
    """La paire historique 11/23, même sans nom de variable parlant."""
    if not isinstance(value, tuple) or len(value) < 2:
        return False
    if not all(type(item) is int and 0 <= item <= 23 for item in value):
        return False
    return 11 in value and 23 in value


def _valid_binding(key: str, value, name: str, train_context: bool,
                   prevalidation_context: bool) -> bool:
    """La valeur porte-t-elle bien le paramètre nommé par le binding ?"""
    if key == "build.max_in_build":
        return type(value) is int and value > 0
    if key in ("reviews.exclude_author_family", "reviews.distinct_families",
               "merge.named_fire", "merge.repeat_freeze",
               "merge.on_green_packet"):
        return type(value) is bool
    if key in ("ci.packet", "ci.incident_label", "ci.environment_red",
               "ci.faulty_lot", "merge.post_merge_red"):
        if not isinstance(value, str) or not value:
            return False
        if name == "packet":
            return train_context and value in CI_PACKETS
        return True
    if key == "reviews.families":
        return (isinstance(value, tuple) and bool(value)
                and bool(set(value) & FAMILY_NAMES))
    if key == "reviews.light_reviews":
        # §2 (#2642) : 0 n'est admis que le temps de la PR home, 1 au canon.
        return type(value) is int and value in (0, 1)
    if key in ("cutoffs", "packets.cutoffs"):
        return _is_cutoff_literal(value)
    if key == "timezone":
        return value == "Europe/Paris" and train_context
    if key == "schema":
        return isinstance(value, str) and value.startswith("ameesh-mechanics/")
    if key == "projects":
        return value == "<dict>"
    if key == "prevalidation.classes":
        return (prevalidation_context and isinstance(value, tuple) and bool(value)
                and all(item in PREVALIDATION_CLASSES for item in value))
    if key in ("prevalidation.contracts", "prevalidation.hosts"):
        return (prevalidation_context and isinstance(value, tuple) and bool(value)
                and all(isinstance(item, str) and item for item in value))
    if key == "prevalidation.parts_script":
        return (prevalidation_context and isinstance(value, str)
                and bool(value) and "/" in value)
    if key == "prevalidation.timeout_seconds":
        return prevalidation_context and type(value) is int and value > 0
    if key == "backlog.lot_min_value":
        # #2584 : un score de valeur (1 à 100), le seuil d'un lot du canon §2
        return type(value) is int and 1 <= value <= 100
    if key.startswith("staging.") or key.startswith("packet_tests."):
        # #3291 : un seuil de lots ou une marge en secondes, entiers positifs
        return type(value) is int and value > 0
    if key == "accounts.underused_loss":
        # #3647 : la perte prévue au reset qui relève le rythme, une part ]0, 1[
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and 0 < value < 1)
    if key == "accounts.pace_multipliers":
        # #3647 : la table des multiplicateurs par compte, recopiée en mapping
        return value == "<dict>"
    if key == "paquet.attente_mediane_s":
        # #3256 C : une durée en secondes, strictement positive
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value > 0)
    if key == "paquet.max_paquets":
        # #3256 C : le nombre de paquets simultanés, au moins deux
        return type(value) is int and value >= 2
    if key == "file.repos_part":
        # #3256 : la part d'agents au repos qui dit la file vide, ]0, 1]
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and 0 < value <= 1)
    if key == "file.repos_s":
        # #3256 : la durée de repos, en secondes
        return type(value) is int and value > 0
    if key == "file.wake_mail_s":
        # #4610 : la fenêtre du courrier récent, en secondes
        return type(value) is int and value > 0
    if key == "auto_take.excluded_families":
        return isinstance(value, tuple) and bool(value) and all(isinstance(v, str) for v in value)
    if key == "auto_take.expire":
        return isinstance(value, str) and bool(_DATE.fullmatch(value))
    if key == "session.max_tokens":
        # #4448 : le seuil commun de rotation, en jetons de session
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value > 0)
    if key == "session.max_tokens_by_harness":
        # #4448 : la table des seuils par harnais, recopiée en mapping
        return value == "<dict>"
    if key == "production.branch":
        # #3022 : la branche de prod d'un projet se lit dans le canon
        # (`projects.<projet>.production.branch`), jamais recopiée en dur.
        return isinstance(value, str) and bool(value.strip())
    return False


def _binding_key(name: str, value, train_context: bool,
                 prevalidation_context: bool) -> str | None:
    lower = name.lower()
    key = UPPER_BINDINGS.get(name)
    if key is None:
        key = BINDINGS.get(lower)
    # constantes d'implémentation : le nom porte le paramètre sans le répéter
    if key is None and lower.endswith("families"):
        key = "reviews.families"
    if key is None and (lower.endswith("build_limit")
                        or lower.endswith("max_in_build")
                        or lower.endswith("max_build")):
        key = "build.max_in_build"
    if key is None and (lower in ("tz", "timezone") or lower.endswith("_tz")
                        or lower.endswith("_timezone")):
        key = "timezone"
    if key is None and (lower == "schema" or lower.endswith("_schema")):
        key = "schema"
    if key is None and "cutoff" in lower:
        key = "packets.cutoffs" if "packet" in lower else "cutoffs"
    if key is None and lower.endswith("project_overrides"):
        key = "projects"
    if key is None and lower.endswith("lot_min_value"):
        key = "backlog.lot_min_value"
    if key is None and lower.endswith("underused_loss"):
        key = "accounts.underused_loss"
    if key is None and lower.endswith("pace_multipliers"):
        key = "accounts.pace_multipliers"
    if key is None and lower.endswith("attente_mediane_s"):
        key = "paquet.attente_mediane_s"
    if key is None and lower.endswith("max_paquets"):
        key = "paquet.max_paquets"
    if key is None and lower.endswith("unit_timeout_seconds"):
        key = "packet_tests.unit_timeout_seconds"
    if key is None and lower.endswith("unit_tests_parallel"):
        key = "packet_tests.parallel"
    if key is None and lower.endswith("repos_part"):
        key = "file.repos_part"
    if key is None and lower.endswith("repos_s"):
        key = "file.repos_s"
    if key is None and lower.endswith("wake_mail_s"):
        key = "file.wake_mail_s"
    if key is None and lower.endswith("session_max_tokens_by_harness"):
        key = "session.max_tokens_by_harness"
    if key is None and lower.endswith("session_max_tokens"):
        key = "session.max_tokens"
    if key is None:
        return None
    if key == "timezone" and not train_context:
        return None
    if key == "build.max_in_build" and type(value) is not int:
        return None
    return key if _valid_binding(key, value, lower, train_context,
                                 prevalidation_context) else None


def inspect_source(path: str, text: str) -> list[Finding]:
    """Tous les littéraux mécaniques connus d'une source Python."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "source", "syntax-error")]
    docstrings = _docstrings(tree)
    # le dépôt s'écrit en français : un runner de release (#2448) parle de
    # « coupures » sans jamais écrire `cutoff`
    train_context = bool(re.search(r"\b(?:cutoffs?|coupures?|packets?|train)\b",
                                   text, re.IGNORECASE))
    # heures de coupure (paire 11/23, cron, `time(11)`) : contexte plus large,
    # une heure n'y a pas d'autre lecture ; le fuseau garde le contexte étroit
    # (le jour budgétaire des comptes est aussi en Europe/Paris)
    cutoff_context = train_context or bool(re.search(
        r"\b(?:paquets?|releases?)\b", text, re.IGNORECASE))
    prevalidation_context = "prevalidation" in text.casefold()
    found: list[Finding] = []

    def add(line: int, key: str, literal: str) -> None:
        found.append(Finding(path, line, key, literal))

    for node in ast.walk(tree):
        # -- valeurs littérales reconnaissables seules --------------------
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            value = node.value
            if value in ("11:00", "23:00", "11:00:00", "23:00:00"):
                add(node.lineno, "cutoffs", value[:5])
                add(node.lineno, "packets.cutoffs", value[:5])
            elif value == "Europe/Paris" and train_context:
                add(node.lineno, "timezone", value)
            elif value.startswith("ameesh-mechanics/"):
                add(node.lineno, "schema", value)
            elif cutoff_context and _CRON_CUTOFFS.search(value):
                add(node.lineno, "cutoffs", "cron 11,23")

        # -- affectations : A = littéral, self.A = littéral ---------------
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.value
            if target is None:
                continue
            ok, value = _literal(target)
            if not ok:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for item in targets:
                name = _target_name(item)
                if not name:
                    continue
                key = _binding_key(name, value, train_context,
                                   prevalidation_context)
                if key:
                    add(node.lineno, key, _literal_text(value))

        # -- defaults de fonction/méthode/lambda --------------------------
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = list(node.args.posonlyargs) + list(node.args.args)
            defaults = list(node.args.defaults)
            pairs = []
            if defaults:
                pairs.extend(zip(arguments[-len(defaults):], defaults))
            pairs.extend((arg, default)
                         for arg, default in zip(node.args.kwonlyargs,
                                                 node.args.kw_defaults or [])
                         if default is not None)
            for arg, default in pairs:
                ok, value = _literal(default)
                if not ok:
                    continue
                key = _binding_key(arg.arg, value, train_context,
                                   prevalidation_context)
                if key:
                    add(getattr(default, "lineno", node.lineno), key,
                        _literal_text(value))

        # -- arguments nommés d'un appel ----------------------------------
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                ok, value = _literal(keyword.value)
                if not ok:
                    continue
                key = _binding_key(keyword.arg, value, train_context,
                                   prevalidation_context)
                if key:
                    add(getattr(keyword.value, "lineno", node.lineno), key,
                        _literal_text(value))

        # -- mapping littéral : politique recopiée -------------------------
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                if not isinstance(key_node, ast.Constant) \
                        or not isinstance(key_node.value, str):
                    continue
                name = key_node.value
                key = DICT_BINDINGS.get(name)
                if key is None and prevalidation_context:
                    key = PREVALIDATION_DICT_BINDINGS.get(name)
                if key is None:
                    continue
                ok, value = _literal(value_node)
                if not ok:
                    continue
                if _valid_binding(key, value, name, train_context,
                                  prevalidation_context):
                    add(key_node.lineno, key, _literal_text(value))

        # -- calendrier historique : heure 11/23 ou cadence horaire --------
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) \
                and node.left.attr == "hour":
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) \
                        and comparator.value in (11, 23):
                    add(node.lineno, "cutoffs", str(comparator.value))
        if isinstance(node, ast.keyword) and node.arg == "hour" \
                and isinstance(node.value, ast.Constant) \
                and node.value.value in (11, 23):
            add(node.lineno, "cutoffs", str(node.value.value))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            ok, value = _literal(node)
            if ok and _is_hourly(value):
                add(node.lineno, "packets.cutoffs", "hourly")
            if ok and cutoff_context and _is_cutoff_pair(value):
                add(node.lineno, "cutoffs", "hours 11/23")

        # -- `time(11, 0)` / `time(23)` : heure de coupure positionnelle ---
        if (isinstance(node, ast.Call) and cutoff_context and node.args
                and _target_name(node.func) == "time"
                and isinstance(node.args[0], ast.Constant)
                and type(node.args[0].value) is int
                and node.args[0].value in (11, 23)):
            add(node.lineno, "cutoffs", str(node.args[0].value))

        # -- `range(24)` : cadence horaire déduite d'une durée -------------
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "range" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == 24 and train_context):
            add(node.lineno, "packets.cutoffs", "range(24)")

    # une même ligne peut être vue par deux règles (valeur + binding)
    unique = {}
    for finding in found:
        unique.setdefault((finding.path, finding.line, finding.parameter,
                           finding.literal), finding)
    return sorted(unique.values(),
                  key=lambda item: (item.path, item.line, item.parameter,
                                    item.literal))


# #2457 : formats non-Python. Analyse lexicale volontairement bornée : les
# heures 11/23 ne sont une règle mécanique que dans un runner dont le chemin
# ou le code actif parle de coupures, paquets, train ou release. Pas de shell
# exécuté, pas de YAML chargé, pas d'expansion des variables ni des includes.
_RUNNER_CONTEXT = re.compile(
    r"(?<![a-z])(?:cutoffs?|coupures?|packets?|paquets?|train|releases?)(?![a-z])",
    re.IGNORECASE)
_CLOCK_CUTOFF = re.compile(r"(?<![\d:])(11|23):00(?::00)?(?![\d:])")
_HOUR_PAIR = re.compile(r"(?<!\d)(?:11[\s,|]+23|23[\s,|]+11)(?![\d:])")
_ANY_CRON = re.compile(r"(?<!\S)[\d*,/\-]+\s+[\d*,/\-]+\s+[\w*,/\-]+\s+[\w*,/\-]+\s+[\w*,/\-]+(?!\S)")
_CRON = re.compile(
    r"(?<!\S)0\s+(?:11|23|11,23|23,11)\s+[\w*,/\-]+\s+[\w*,/\-]+\s+[\w*,/\-]+(?!\S)")


def _active_line(line: str) -> str:
    """Retire les commentaires hors guillemets, conserve le texte et ses quotes.

    Shell/YAML : # démarre un commentaire après un blanc ; dans une chaîne
    ou un mot (URL, fragment, expansion shell), il reste une donnée.
    """
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def inspect_runner(path: str, text: str) -> list[Finding]:
    """Coupures littérales dans shell, OnCalendar et cron des workflows.

    Les commentaires seuls ne donnent jamais le contexte. Pour systemd, les
    commentaires ; sont également exclus ; seule la section [Timer] fournit
    un calendrier. Dans un workflow, les clés cron et les blocs run sont lus
    lexicalement ; les chaînes multilignes sont couvertes ligne par ligne.
    Les horaires génériques (sauvegarde, budget…) et les heures différentes
    (9/17), ainsi que les expressions calculées, restent hors de ces motifs.
    """
    timer = path.endswith(".timer")
    lines = ["" if timer and line.lstrip().startswith(";")
             else _active_line(line) for line in text.splitlines()]
    if not _RUNNER_CONTEXT.search(path + "\n" + "\n".join(lines)):
        return []
    found = []
    section = ""
    for number, line in enumerate(lines, 1):
        if timer:
            if line.strip().startswith("["):
                section = line.strip()
            if section != "[Timer]":
                continue
            match = re.match(r"\s*OnCalendar\s*=\s*(.*)", line)
            if not match:
                continue
            value = match[1]
            # Formes systemd : 11:00, 23:00, 11,23:00, avec date et TZ
            # éventuels. Une minute/seconde non nulle n'est pas une coupure.
            if re.search(r"(?<![\d:,])(?:11,23|23,11):00(?::00)?(?![\d:])", value):
                found.append(Finding(path, number, "cutoffs", "OnCalendar 11,23"))
            else:
                for match in _CLOCK_CUTOFF.finditer(value):
                    found.append(Finding(path, number, "cutoffs", match[1] + ":00"))
            continue
        # Les expressions cron sont souvent entre guillemets (YAML/shell).
        value = line.replace('"', " ").replace("'", " ")
        if _CRON.search(value):
            found.append(Finding(path, number, "cutoffs", "cron 11/23"))
            continue
        if _ANY_CRON.search(value):
            continue  # autre minute/heure cron : ne pas relire sa paire d’heures
        for match in _CLOCK_CUTOFF.finditer(line):
            found.append(Finding(path, number, "cutoffs", match[1] + ":00"))
        if _HOUR_PAIR.search(line):
            found.append(Finding(path, number, "cutoffs", "hours 11/23"))
        if re.search(r"(?i)\b[a-z_]*hour[a-z_]*[}\"' ]*"
                     r"(?:-eq|==|=)\s*['\"]?(?:11|23)(?![\d:])", line):
            found.append(Finding(path, number, "cutoffs", "hour comparison 11/23"))
        # Constantes shell CUTOFF_HOUR=11 / RELEASE_HOUR=23 ; une simple
        # comparaison de nombres sans contexte local n'est pas interprétée.
        if re.search(r"(?i)(?:cutoff|coupure|release)[a-z_]*=\s*['\"]?(?:11|23)(?![\d:])", line):
            found.append(Finding(path, number, "cutoffs", "hour 11/23"))
    return found


def _source_kind(path: str, source: pathlib.Path) -> str | None:
    if path.endswith(".py"):
        return "python"
    if path.endswith((".sh", ".bash", ".timer")):
        return "runner"
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        return "runner"
    # Scripts shell suivis sans suffixe (bin/* notamment).
    if not pathlib.PurePosixPath(path).suffix:
        try:
            with source.open("rb") as stream:
                first = stream.readline(256).decode("ascii", "ignore").strip()
            if re.fullmatch(r"#!\s*(?:/usr/bin/env\s+(?:-S\s+)?|/(?:usr/)?bin/)(?:ba|da|k|z)?sh(?:\s+.*)?", first):
                return "runner"
        except OSError:
            pass
    return None


# --------------------------------------------------------------------------
# fichiers, exemptions, sortie
# --------------------------------------------------------------------------

def _excluded(path: str) -> bool:
    return (path in (GUARD_REL, MECHANICS_REL)
            or any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES))


def source_files(root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """Sources Python, shell, timers et workflows suivis par Git, triées.

    `git ls-files` écarte les worktrees et fichiers non suivis d'un clone
    partagé ; hors dépôt Git (témoin), on retombe sur une marche triée.
    """
    root = pathlib.Path(root)
    names: list[str] = []
    git = None
    try:
        # un `--root` temporaire dans un dépôt parent ne doit pas faire lire
        # les fichiers du parent : le listing Git ne vaut que pour SA racine.
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, check=False)
        if top.returncode == 0 and pathlib.Path(
                top.stdout.decode("utf-8", "surrogateescape").strip()).resolve() \
                == root.resolve():
            git = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                 capture_output=True, check=False)
    except OSError:
        git = None
    if git is not None and git.returncode == 0:
        names = [name for name in git.stdout.decode("utf-8", "surrogateescape")
                 .split("\0") if name]
    else:
        names = [str(item.relative_to(root))
                 for item in sorted(root.rglob("*")) if item.is_file()
                 and ".git" not in item.relative_to(root).parts]
    files = []
    for name in sorted(set(names)):
        rel = pathlib.PurePosixPath(name).as_posix()
        if _excluded(rel):
            continue
        path = root / rel
        if not path.is_symlink() and path.is_file() and _source_kind(rel, path):
            files.append((rel, path))
    return files


def python_files(root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """Compatibilité des consommateurs de #2449 : seulement les .py."""
    return [(rel, path) for rel, path in source_files(root) if rel.endswith(".py")]


@dataclasses.dataclass(frozen=True)
class Scan:
    root: str
    findings: tuple[Finding, ...]
    used: tuple[Exemption, ...]
    unused: tuple[Exemption, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {"root": self.root, "ok": self.ok,
                "findings": [item.as_dict() for item in self.findings],
                "used_exemptions": [item.as_dict() for item in self.used],
                "unused_exemptions": [item.as_dict() for item in self.unused]}


def apply_exemptions(findings: list[Finding], exemptions=EXEMPTIONS):
    """Sépare les constats couverts, les exemptions utilisées et les inutiles."""
    used: set[int] = set()
    remaining: list[Finding] = []
    for finding in findings:
        match = next((number for number, exemption in enumerate(exemptions)
                      if exemption.covers(finding)), None)
        if match is None:
            remaining.append(finding)
        else:
            used.add(match)
    return (remaining,
            tuple(exemption for number, exemption in enumerate(exemptions)
                  if number in used),
            tuple(exemption for number, exemption in enumerate(exemptions)
                  if number not in used))


def scan_repository(root: pathlib.Path = REPO, exemptions=EXEMPTIONS) -> Scan:
    findings: list[Finding] = []
    for rel, path in source_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(Finding(rel, 0, "source", "unreadable: %s" % exc))
            continue
        findings.extend(inspect_source(rel, text) if rel.endswith(".py")
                        else inspect_runner(rel, text))
    findings.sort(key=lambda item: (item.path, item.line, item.parameter,
                                    item.literal))
    remaining, used, unused = apply_exemptions(findings, exemptions)
    return Scan(str(root), tuple(remaining), used, unused)


def _print_scan(scan: Scan, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(scan.as_dict(), ensure_ascii=False, sort_keys=True))
        return
    if scan.findings:
        count = len(scan.findings)
        noun = "littéral mécanique codé" if count == 1 else "littéraux mécaniques codés"
        print("refus: %d %s en dur hors du canon compilé:" % (count, noun))
        for finding in scan.findings:
            print("  " + finding.render())
        print("Déclarer le paramètre dans processus/mecanique.yaml et le lire "
              "par ameesh.mechanics (`load`/`load_global`), ou ajouter une "
              "exemption nommée et datée dans EXEMPTIONS "
              "(scripts/garde-mecanique.py) avec sa raison.")
    else:
        print("garde mécanique: aucun littéral hors canon "
              "(%d exemption(s) nommée(s) et datée(s))." % len(scan.used))
    if scan.unused:
        print("note: exemption(s) inutilisée(s), à retirer:")
        for exemption in scan.unused:
            print("  %s/%s (%s, %s)" % (exemption.path, exemption.parameter,
                                        exemption.name, exemption.date))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="garde-mecanique.py",
        description="Refuse un paramètre mécanique codé en dur hors du canon "
                    "compilé (#2449).")
    parser.add_argument("--root", default=str(REPO),
                        help="racine du dépôt (défaut : %(default)s)")
    parser.add_argument("--json", action="store_true",
                        help="sortie JSON (constats, exemptions utilisées et inutiles)")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root)
    if not root.is_dir():
        parser.error("--root: a directory is required")
    try:
        scan = scan_repository(root)
    except GuardError as exc:
        print("garde mécanique: exemption invalide: %s" % exc, file=sys.stderr)
        return 2
    _print_scan(scan, as_json=args.json)
    return 0 if scan.ok else 1


if __name__ == "__main__":
    sys.exit(main())
