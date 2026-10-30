# SPDX-License-Identifier: AGPL-3.0-only
"""Le canon OKF : fiches Agent, Host, Placement, Member, WorkPackage ; validation (C2, C3).

Spec §4. ameesh ne lit que le **frontmatter** des fichiers `.md` d'un bundle
OKF (hors `index.md` et `log.md`) et n'écrit jamais dans le canon.
`log.md` est facultatif : l'historique d'un canon, ce sont git et les PR
fusionnées ; un canon qui en garde un n'est pas gêné (il reste ignoré).

Source approuvée (§4.1) : le canon est lu **par les objets git, à la révision
fusionnée de la branche canonique** — `AMEESH_CANON_REF` si posé, sinon le
`ref` du membre racine dans `federation.yaml` (`origin/<ref>`), sinon
`origin/main`. Un arbre de travail modifié ou un commit local non poussé n'est
jamais pris en compte (seulement signalé). Un dossier qui n'est pas un dépôt
git n'est lu qu'avec `AMEESH_CANON_UNTRUSTED=1` (tests, prototypes), et chaque
constat le signale.

Ce module dit ce qui est LU. Ce qui FAIT FOI pour le registre des
authentificateurs (§8.2) n'est jamais tiré du commit lu (son propre
manifeste pourrait s'autoriser) : voir `canon_sync.sync_authenticators`.

Fédération : si `federation.yaml` est présent à la racine, ses membres
présents localement (`workspace_path`, relatif au dossier de travail commun,
comme le validateur OKF Federation) sont lus aussi, chacun à `origin/<ref>`
de son propre dépôt. Un membre absent est signalé, jamais deviné.

Périmètre (L43, décision 0031 ; forme L47, conforme au schéma OKF
Federation) : `extensions: {ameesh: {scope: <dossier>|[…]}}` dans
federation.yaml borne les fiches du profil du bundle racine à ces dossiers
(`extensions.ameesh.members.<id>.scope` : celles d'un autre membre). L'ancienne
forme (`ameesh:` au premier niveau, `members[].ameesh`) reste lue, avec un
avertissement.
Hors du périmètre, un fichier de même `type` (sous-agent Claude Code d'un
canon partagé, p. ex.) est ignoré — un constat d'information agrégé, jamais
une erreur. Sans la clé, tout le bundle est lu. Un périmètre illisible ou
introuvable : erreur, et aucune fiche du membre n'est lue (fail closed).

YAML : PyYAML n'est pas une dépendance. Si `yaml` est importable, il est
utilisé (`safe_load`, clés en double refusées) ; sinon un lecteur minimal
couvre le sous-ensemble du profil (scalaires, chaînes entre guillemets,
listes en ligne et à tirets, mappings imbriqués, mappings en ligne, ancres
simples, blocs `|`/`>`).
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import fnmatch
import hashlib
import os
import posixpath
import re
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import harnesses
from . import review as review_mod
from .config import NAME_RE
from .deadline import Deadline

try:  # facultatif : jamais requis
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover - dépend de l'environnement
    _yaml = None

#: `Persona` (L51, 0029/0032) replaces `Agent`; both card types remain
#: readable during the transition. `Process` (#538) carries the working rules
#: (`processus/*.md`), read to open every agent's brief. `Pipeline` (L103,
#: #4395) declares a pipeline (`pipelines/*.md`), a module without code.
PROFILE_TYPES = ("Agent", "Persona", "Host", "Placement", "Member", "WorkPackage",
                 "Process", "Pipeline")
#: #538 : une fiche Process (`processus/*.md`) porte les règles de travail.
PROCESS_TYPE = "Process"
#: L103 (#4395) : une carte Pipeline (`pipelines/*.md`) porte un bloc
#: `pipeline:` (alias transitoire `workflow:`), compilé par `canon check`.
PIPELINE_TYPE = "Pipeline"
PIPELINE_DIR = "pipelines"
#: #3070 (canon §12) : clés d'une fiche Host déclarant l'appareil indisponible
UNAVAILABLE_UNTIL_KEY = "indisponible_jusqu_au"
UNAVAILABLE_REASON_KEY = "indisponible_motif"
#: #538 : dossier du canon où vivent les fiches Process ; un `type: Process`
#: posé ailleurs n'est pas une règle de travail et n'est jamais injecté.
PROCESS_DIR = "processus"
#: #538 : deux pages ≈ 6 000 caractères ; au-delà `canon check` signale un
#: dépassement (avertissement, jamais bloquant) et le brief coupe le texte.
PROCESS_BODY_MAX = 6000
#: #1372 : une carte Process trop longue peut porter une PROJECTION COURTE,
#: délimitée par ces deux marqueurs (chacun sur sa ligne). Le brief injecte la
#: projection À LA PLACE du texte entier quand elle est valide : la fin du canon
#: (§12, §14…) n'est plus mangée par le début. Les sections complètes restent la
#: source ; la projection n'ajoute aucune règle (elle est approuvée avec la
#: carte, par le même responsable).
PROCESS_PROJECTION_START = "<!-- projection courte : début -->"
PROCESS_PROJECTION_END = "<!-- projection courte : fin -->"
#: #3867 : bornes du brief, PARTAGÉES par `reprise` (qui les reprend) et par
#: `canon check` (qui mesure le bloc de règles avec le même calcul). Plafond du
#: brief (octets UTF-8) ; part qu'un texte accompagné de règles garde au plus.
BRIEF_MAX_BYTES = 23500
BRIEF_TEXT_MIN_BYTES = 6000
#: #538 : en-tête et pied du bloc de règles d'un brief.
PROCESS_RULES_HEADER = "## Working rules (canon Process cards)"
PROCESS_RULES_FOOTER = "<!-- end of the canon Process rules -->"
#: #3867 : place du bloc de règles quand le texte du brief (résumé de rotation,
#: brief préparé) prend toute sa réserve — le cas le plus serré de
#: `reprise.with_process_rules` (2 octets pour le `\n\n` qui recolle le texte).
RULES_BLOC_LIMIT = BRIEF_MAX_BYTES - 2 - BRIEF_TEXT_MIN_BYTES
#: #3867 : place des règles elles-mêmes dans ce bloc, sans en-tête ni pied
#: (17 419 octets) : toutes les cartes Process servies, titres et métadonnées
#: compris, doivent y tenir — `canon check` le vérifie (`process-rules-block-*`).
RULES_BLOC_ROOM = (RULES_BLOC_LIMIT - len((PROCESS_RULES_HEADER + "\n").encode("utf-8"))
                   - len(("\n" + PROCESS_RULES_FOOTER).encode("utf-8")))
#: #4250 : plancher de compatibilité du bloc de règles. `canon check` mesure le
#: bloc avec la limite de la PLUS ANCIENNE version d'ameesh encore servie sur
#: les hôtes du canon, jamais avec celle de l'hôte local : pendant un staging,
#: un hôte en 1.26.0 (19 000 − 2 − 4 000 = 14 998) reçoit encore les briefs
#: alors qu'un hôte en 1.27.0 en accepte 17 498. Sans ce plancher, une PR de
#: canon validée sur l'hôte récent coupe la fin des règles sur l'hôte en retard
#: (cas home#209 sur agents-3, 28/10/2026). Ces bornes restent le plancher tant
#: qu'une version installée est inconnue ; les relever quand plus aucun hôte ne
#: peut tourner en 1.26.0 (le plus ancien hôte servi fait foi).
BRIEF_MAX_BYTES_COMPAT = 19000
BRIEF_TEXT_MIN_BYTES_COMPAT = 4000
RULES_BLOC_LIMIT_COMPAT = BRIEF_MAX_BYTES_COMPAT - 2 - BRIEF_TEXT_MIN_BYTES_COMPAT
RULES_BLOC_ROOM_COMPAT = (RULES_BLOC_LIMIT_COMPAT
                          - len((PROCESS_RULES_HEADER + "\n").encode("utf-8"))
                          - len(("\n" + PROCESS_RULES_FOOTER).encode("utf-8")))
#: #1372, #3126, #3867 : plafond d'une projection seule (octets UTF-8) : la place
#: entière des règles. Une projection au-delà est signalée et coupée à ce
#: plafond, jamais silencieusement. Ce n'est PAS un budget suffisant : les
#: autres cartes servies partagent la même place, et c'est le contrôle du bloc
#: entier qui prévient (#3867 : une projection « sous budget » à 12 Ko laissait
#: déborder le bloc de 2 Ko, la marge fixe de 320 octets supposant une carte seule).
PROCESS_PROJECTION_MAX = RULES_BLOC_ROOM
#: les sortes de fiches WorkPackage (plan de travail, L29) et les parents admis
PACKAGE_KINDS = ("milestone", "epic", "lot")
#: L96 : dates d'une fiche WorkPackage (`date` = date d'un jalon, alias de `delivery`)
PACKAGE_DATE_KEYS = ("start", "end", "delivery", "date")
#: L96 : fiches Decision gardées pour proposer la feuille de route (corps borné)
DECISION_TYPE = "Decision"
DECISION_BODY_MAX = 64 * 1024
PACKAGE_PARENTS = {"milestone": (), "epic": ("milestone",), "lot": ("epic", "milestone")}
#: identifiant d'une fiche WorkPackage (clé `id`, sinon nom du fichier sans `.md`)
PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: identifiant d'un canon (L42, décision 0031) : `id` de federation.yaml, sinon
#: le nom du dossier ; jamais de `/` ni de `:` (il préfixe les références)
CANON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: provenance de l'identifiant d'un canon
ID_FROM_FEDERATION = "federation"
ID_FROM_DIRECTORY = "directory"
#: référence d'une fiche (L2, L42) : `[<canon>/]<membre>:<chemin>@<version>`.
#: Le canon PAR DÉFAUT garde le format historique, sans préfixe (aucun
#: changement de données) ; un autre canon préfixe son identifiant.
FICHE_REF_RE = re.compile(
    r"^(?:(?P<canon>[A-Za-z0-9][A-Za-z0-9._-]{0,63})/)?(?P<member>[^:/\s]+):"
    r"(?P<path>\S*)@(?P<version>\S+)$")
KNOWN_CAPABILITIES = ("read", "report-drift", "propose", "integrate", "merge", "release")
#: L150: what each capability allows. None of them carries a decision: R8
#: keeps approval with humans; `merge` and `release` EXECUTE an approval that
#: was already given (approved review, owner's go), they never replace it.
CAPABILITY_MEANINGS = {
    "read": "read the canon, the threads and the repositories",
    "report-drift": "report a gap between the canon and reality",
    "propose": "propose a change (branch, PR, action proposal)",
    "integrate": "assemble others' work: rebase PRs, resolve conflicts, build an "
                 "integration or release branch; never merges into the base branch",
    "merge": "merge an approved PR into the base branch once its required reviews "
             "and CI are green; never approves",
    "release": "tag, publish and deploy a release once the owner's go is recorded; "
               "never approves, never skips the release procedure",
}
#: R8 : l'autorité de décision est réservée aux humains
FORBIDDEN_CAPABILITIES = ("approve",)
EPHEMERAL_CAPABILITIES = ("read", "propose")
SKIPPED_FILES = ("index.md", "log.md")
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
FRONTMATTER_MAX = 256 * 1024
GIT_TIMEOUT = 60.0
FETCH_TIMEOUT = 180.0

ERROR = "error"
WARNING = "warning"
#: L43 (0031) : constat d'information — ni bloquant, ni avertissement (p. ex.
#: les fiches hors du périmètre ameesh d'un canon partagé, ignorées)
INFO = "info"

#: L43 (0031) : clé de federation.yaml (premier niveau pour le bundle racine,
#: entrée `members[]` pour un autre membre) qui borne les fiches du profil
#: ameesh à un ou plusieurs dossiers du bundle : `ameesh: {scope: ameesh}`
SCOPE_KEY = "ameesh"
SCOPE_FIELDS = ("scope", "projects")
#: L243 (#468) : clé du nommage par défaut des nouveaux agents, admise au
#: PREMIER niveau de `extensions.ameesh` seulement — le canon, c'est
#: l'organisation ; l'entrée d'un membre n'a pas de nommage propre.
NAMING_KEY = "naming"
#: L243 (#468) : un segment de nom est en minuscules, 2 à 6 caractères.
#: `\Z` (et non `$`) : `$` accepte une fin de ligne, donc `"mty\n"` passerait
#: et engendrerait un nom invalide.
NAMING_SEGMENT_RE = re.compile(r"^[a-z0-9]{2,6}\Z")
#: L243 (#468) : le numéro de séquence, deux chiffres puis trois après 99.
NAMING_SEQ_RE = re.compile(r"^[0-9]{2,}\Z")
NAMING_FIELDS = ("org", "projects", "roles")


class CanonError(ValueError):
    """Canon introuvable ou illisible (la CLI l'affiche, sans trace)."""


class YamlError(ValueError):
    pass


class _Incomplete(YamlError):
    """Flux ou chaîne non terminés sur la ligne : la suite est peut-être dessous."""


# ==========================================================================
# YAML : PyYAML si présent, sinon lecteur minimal
# ==========================================================================

def _normalize(value: Any, memo: dict | None = None) -> Any:
    """Dates → texte ISO, récursivement : les deux lecteurs rendent la même chose.

    Les objets partagés par des alias restent partagés (mémo par identité) :
    une chaîne d'alias ne se déplie pas en une structure exponentielle.
    """
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if not isinstance(value, (dict, list)):
        return value
    memo = {} if memo is None else memo
    if id(value) in memo:
        return memo[id(value)]
    if isinstance(value, dict):
        out: Any = {}
        memo[id(value)] = out
        for k, v in value.items():
            out[_normalize(k, memo) if not isinstance(k, str) else k] = _normalize(v, memo)
        return out
    out = []
    memo[id(value)] = out
    out.extend(_normalize(v, memo) for v in value)
    return out


if _yaml is not None:
    class _StrictLoader(_yaml.SafeLoader):  # type: ignore[misc,name-defined]
        """SafeLoader qui refuse les clés en double (deux `responsible:` = ambigu)."""

    def _strict_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _value in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                if key in seen:
                    raise YamlError("line %d: duplicate key %r"
                                    % (key_node.start_mark.line + 1, key))
                seen.add(key)
            except TypeError:
                raise YamlError("line %d: non-scalar key" % (key_node.start_mark.line + 1))
        return loader.construct_mapping(node, deep=deep)

    _StrictLoader.add_constructor(
        _yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping)


def load_yaml(text: str, *, use_pyyaml: bool | None = None) -> Any:
    """Document YAML → valeurs Python. `use_pyyaml=False` force le lecteur minimal."""
    if use_pyyaml is None:
        use_pyyaml = _yaml is not None
    try:
        if use_pyyaml:
            if _yaml is None:
                raise YamlError("PyYAML unavailable")
            try:
                return _normalize(_yaml.load(text, Loader=_StrictLoader))  # noqa: S506 (SafeLoader)
            except _yaml.YAMLError as exc:
                raise YamlError(" ".join(str(exc).split())) from exc
        return _MiniYaml(text).parse()
    except RecursionError as exc:
        raise YamlError("nesting too deep") from exc


_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
#: YAML 1.1, comme PyYAML : un point est requis (`1e12` reste un texte)
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+)([eE][-+]\d+)?$")
_BLOCK_RE = re.compile(r"^[|>][-+]?[1-9]?$")
_ANCHOR_RE = re.compile(r"^&([^\s\[\]{},]+)(?:\s+(.*))?$")
_TRUE = ("true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON")
_FALSE = ("false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF")
_NULL = ("", "~", "null", "Null", "NULL")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/", "0": "\0",
            "r": "\r", " ": " ", "a": "\a", "b": "\b", "e": "\x1b", "f": "\f",
            "v": "\v", "N": "\x85", "_": "\xa0"}


def _plain(text: str) -> Any:
    text = text.strip()
    if text in _NULL:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    if text.startswith(("!", "%", "@", "`")):
        raise YamlError("unsupported value: %r" % text[:40])
    return text


def _quote_starts(text: str, i: int) -> bool:
    return text[i] in "'\"" and (i == 0 or text[i - 1] in " \t[{,")


def _strip_comment(text: str) -> str:
    """Retire un commentaire `# …` hors guillemets."""
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                if text[i + 1:i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                quote = None
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        elif _quote_starts(text, i):
            quote = c
        i += 1
    return text


def _split_key(text: str) -> tuple[str, str] | None:
    """`clé: valeur` → (clé brute, valeur brute), hors guillemets et crochets."""
    if not text or text[0] in "[{":
        return None
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                if text[i + 1:i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                quote = None
        elif _quote_starts(text, i):
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].strip(), text[i + 1:].strip()
        i += 1
    return None


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _quoted(text: str, i: int) -> tuple[str, int]:
    """Chaîne entre guillemets à la position i → (valeur, position après)."""
    quote = text[i]
    i += 1
    out: list[str] = []
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                if text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), i + 1
            out.append(c)
            i += 1
            continue
        if c == "\\":
            nxt = text[i + 1:i + 2]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            width = {"x": 2, "u": 4, "U": 8}.get(nxt)
            if width:
                digits = text[i + 2:i + 2 + width]
                if len(digits) == width and all(d in "0123456789abcdefABCDEF" for d in digits):
                    out.append(chr(int(digits, 16)))
                    i += 2 + width
                    continue
            raise YamlError("unknown escape: \\%s" % nxt)
        if c == '"':
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise _Incomplete("unterminated string")


class _Flow:
    """Collections en ligne : `[a, "b, c"]`, `{ by: x, at: y }`."""

    def __init__(self, text: str, anchors: dict):
        self.s = text
        self.i = 0
        self.anchors = anchors

    def parse(self) -> Any:
        value = self.value()
        self.ws()
        if self.i != len(self.s):
            raise YamlError("content after a flow collection: %r" % self.s[self.i:][:40])
        return value

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def peek(self) -> str:
        if self.i >= len(self.s):
            raise _Incomplete("unterminated flow collection")
        return self.s[self.i]

    def value(self, key: bool = False) -> Any:
        self.ws()
        c = self.peek()
        if c == "[":
            return self.seq()
        if c == "{":
            return self.map()
        if c in "'\"":
            text, self.i = _quoted(self.s, self.i)
            return text
        if c == "*":
            match = re.match(r"\*([^\s\[\]{},]+)", self.s[self.i:])
            if not match or match.group(1) not in self.anchors:
                raise YamlError("unknown alias")
            self.i += match.end()
            return self.anchors[match.group(1)]
        start = self.i
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in ",]}":
                break
            if key and c == ":" and (self.i + 1 == len(self.s) or self.s[self.i + 1] in " \t,}"):
                break
            if c == "#" and self.s[self.i - 1:self.i] in (" ", "\t"):
                raise YamlError("comment inside a flow collection")
            self.i += 1
        text = self.s[start:self.i].strip()
        return text if key else _plain(text)

    def seq(self) -> list:
        self.i += 1
        items: list = []
        while True:
            self.ws()
            if self.peek() == "]":
                self.i += 1
                return items
            items.append(self.value())
            self.ws()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "]":
                raise YamlError("\",\" or \"]\" expected in a flow list")

    def map(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            if self.peek() == "}":
                self.i += 1
                return out
            key = self.value(key=True)
            if not isinstance(key, str):
                raise YamlError("non-scalar key in a flow mapping")
            self.ws()
            value = None
            if self.peek() == ":":
                self.i += 1
                self.ws()
                if self.peek() not in ",}":
                    value = self.value()
            if key in out:
                raise YamlError("duplicate key %r" % key)
            out[key] = value
            self.ws()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "}":
                raise YamlError("\",\" or \"}\" expected in a flow mapping")


@dataclass
class _Line:
    no: int
    indent: int
    text: str
    raw: str


class _MiniYaml:
    """Lecteur YAML minimal, par indentation. Tout ce qu'il ne comprend pas est
    une erreur explicite : il ne devine jamais."""

    def __init__(self, text: str):
        self.lines: list[_Line] = []
        self.anchors: dict[str, Any] = {}
        for no, raw in enumerate(text.splitlines(), 1):
            stripped = raw.lstrip(" ")
            indent = len(raw) - len(stripped)
            if stripped.startswith("\t") and stripped.strip():
                raise YamlError("line %d: tab in indentation" % no)
            content = _strip_comment(stripped).rstrip()
            if indent == 0 and content in ("---", "..."):
                if self.lines and any(line.text for line in self.lines):
                    if content == "...":
                        break
                    raise YamlError("line %d: multiple documents" % no)
                continue
            self.lines.append(_Line(no, indent, content, raw))

    def parse(self) -> Any:
        i = self._skip(0)
        if i >= len(self.lines):
            return None
        value, i = self._node(i)
        i = self._skip(i)
        if i < len(self.lines):
            raise YamlError("line %d: unexpected content" % self.lines[i].no)
        return value

    def _skip(self, i: int) -> int:
        while i < len(self.lines) and not self.lines[i].text:
            i += 1
        return i

    def _node(self, i: int) -> tuple[Any, int]:
        line = self.lines[i]
        if _is_item(line.text):
            return self._seq(i, line.indent)
        if _split_key(line.text) is not None:
            return self._map(i, line.indent)
        return self._inline(i, line.text, line.indent - 1)

    def _seq(self, i: int, indent: int) -> tuple[list, int]:
        items: list = []
        while True:
            i = self._skip(i)
            if i >= len(self.lines):
                break
            line = self.lines[i]
            if line.indent < indent or (line.indent == indent and not _is_item(line.text)):
                break
            if line.indent > indent:
                raise YamlError("line %d: unexpected indentation" % line.no)
            rest = line.text[1:].lstrip(" ")
            anchor = None
            match = _ANCHOR_RE.match(rest)
            if match:
                anchor, rest = match.group(1), (match.group(2) or "")
            if not rest:
                j = self._skip(i + 1)
                if j < len(self.lines) and self.lines[j].indent > indent:
                    value, i = self._node(j)
                else:
                    value, i = None, i + 1
            elif _is_item(rest) or _split_key(rest) is not None:
                column = indent + (len(line.text) - len(rest))
                self.lines[i] = _Line(line.no, column, rest, line.raw)
                value, i = self._node(i)
            else:
                value, i = self._inline(i, rest, indent)
            if anchor:
                self.anchors[anchor] = value
            items.append(value)
        return items, i

    def _map(self, i: int, indent: int) -> tuple[dict, int]:
        out: dict = {}
        while True:
            i = self._skip(i)
            if i >= len(self.lines):
                break
            line = self.lines[i]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError("line %d: unexpected indentation" % line.no)
            if _is_item(line.text):
                raise YamlError("line %d: unexpected list item" % line.no)
            pair = _split_key(line.text)
            if pair is None:
                raise YamlError("line %d: \"key: value\" expected" % line.no)
            raw_key, rest = pair
            if raw_key and raw_key[0] in "'\"":
                key, end = _quoted(raw_key, 0)
                if raw_key[end:].strip():
                    raise YamlError("line %d: unreadable key" % line.no)
            else:
                key = raw_key
            if not key:
                raise YamlError("line %d: empty key" % line.no)
            if key in out:
                raise YamlError("line %d: duplicate key %r" % (line.no, key))
            anchor = None
            match = _ANCHOR_RE.match(rest)
            if match:
                anchor, rest = match.group(1), (match.group(2) or "")
            if not rest:
                j = self._skip(i + 1)
                nested = j < len(self.lines) and (
                    self.lines[j].indent > indent
                    or (self.lines[j].indent == indent and _is_item(self.lines[j].text)))
                if nested:
                    value, i = self._node(j)
                else:
                    value, i = None, i + 1
            else:
                value, i = self._inline(i, rest, indent)
            if anchor:
                self.anchors[anchor] = value
            out[key] = value
        return out, i

    def _inline(self, i: int, text: str, parent: int) -> tuple[Any, int]:
        """Valeur écrite sur la ligne i (et ses suites plus indentées)."""
        line = self.lines[i]
        if _BLOCK_RE.match(text):
            return self._block(i, text, parent)
        if text[0] in "[{":
            j = i
            while True:
                try:
                    return _Flow(text, self.anchors).parse(), j + 1
                except _Incomplete:
                    j += 1
                    while j < len(self.lines) and not self.lines[j].text:
                        j += 1
                    if j >= len(self.lines) or self.lines[j].indent <= parent:
                        raise YamlError("line %d: unterminated flow collection" % line.no)
                    text = text + " " + self.lines[j].text
        if text[0] in "'\"":
            try:
                value, end = _quoted(text, 0)
            except _Incomplete:
                raise YamlError("line %d: multi-line string not supported"
                                % line.no)
            if text[end:].strip():
                raise YamlError("line %d: content after a string" % line.no)
            return value, i + 1
        if text[0] == "*":
            name = text[1:].strip()
            if name not in self.anchors:
                raise YamlError("line %d: unknown alias %r" % (line.no, name))
            return self.anchors[name], i + 1
        # scalaire simple, éventuellement replié sur les lignes suivantes
        parts = [text]
        j = i + 1
        while j < len(self.lines):
            nxt = self.lines[j]
            if not nxt.text:
                j += 1
                continue
            if nxt.indent <= parent:
                break
            if _split_key(nxt.text) is not None or _is_item(nxt.text):
                raise YamlError("line %d: unexpected structure after a scalar" % nxt.no)
            parts.append(nxt.text)
            j += 1
        if len(parts) > 1:
            return " ".join(p.strip() for p in parts), j
        return _plain(text), i + 1

    def _block(self, i: int, header: str, parent: int) -> tuple[str, int]:
        """Bloc littéral `|` ou replié `>` (indicateurs -/+ ; indentation automatique)."""
        style, chomp = header[0], (header[1] if len(header) > 1 and header[1] in "-+" else "")
        body: list[str] = []
        j = i + 1
        block_indent = None
        while j < len(self.lines):
            raw = self.lines[j].raw
            if raw.strip():
                indent = len(raw) - len(raw.lstrip(" "))
                if indent <= parent:
                    break
                if block_indent is None:
                    block_indent = indent
                if indent < block_indent:
                    break
                body.append(raw[block_indent:])
            else:
                body.append("")
            j += 1
        while body and body[-1] == "" and chomp != "+":
            body.pop()
        if style == "|":
            text = "\n".join(body)
        else:
            folded: list[str] = []
            for item in body:
                if item == "":
                    folded.append("\n")
                elif folded and not folded[-1].endswith("\n"):
                    folded.append(" " + item)
                else:
                    folded.append(item)
            text = "".join(folded)
        if body and chomp != "-":
            text += "\n"
        return text, j


def split_frontmatter(text: str) -> str | None:
    """Le frontmatter YAML d'un fichier Markdown, ou None s'il n'y en a pas."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r \t") != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r \t") in ("---", "..."):
            return "\n".join(line.rstrip("\r") for line in lines[1:idx])
    raise YamlError("unclosed frontmatter (or larger than %d KiB)" % (FRONTMATTER_MAX // 1024))


def body_of(text: str) -> str:
    """Le corps d'une fiche Markdown : ce qui suit le frontmatter (`---`).

    Sans frontmatter fermé, le texte entier est le corps ; c'est ce que lit le
    brief (#538, fiches Process) et la feuille de route (L96, fiches Decision).
    """
    lines = text.lstrip("﻿").split("\n")
    if not lines or lines[0].rstrip("\r \t") != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r \t") in ("---", "..."):
            return "\n".join(lines[idx + 1:])
    return ""


def process_projection(body: str) -> tuple[str, str]:
    """#1372 : la projection courte portée par le corps d'une carte Process.

    Rend `(projection, code)`. Un marqueur est SEUL sur sa ligne (espaces
    ignorés) : un marqueur suivi d'un texte n'en est pas un (R1 #1372, B1) ;
    le texte entre les deux marqueurs est la projection. `code` vaut "" quand
    il n'y a pas de marqueur ou que la projection est lue, sinon :
    `unterminated` (début sans fin), `stray-end` (fin sans début, ou fin avant
    le début), `multiple` (plus d'un marqueur), `empty` (aucun texte). Une
    projection illisible n'est jamais injectée : l'appelant garde le texte
    entier et le DIT (`process-projection-<code>`).
    """
    lignes = (body or "").splitlines()
    debuts = [i for i, l in enumerate(lignes)
              if l.strip() == PROCESS_PROJECTION_START]
    fins = [i for i, l in enumerate(lignes)
            if l.strip() == PROCESS_PROJECTION_END]
    if not debuts and not fins:
        return "", ""
    if len(debuts) > 1 or len(fins) > 1:
        return "", "multiple"
    if not debuts or not fins:
        return "", ("stray-end" if not debuts else "unterminated")
    debut, fin = debuts[0], fins[0]
    if fin < debut:
        return "", "stray-end"
    texte = "\n".join(lignes[debut + 1:fin]).strip()
    return (texte, "") if texte else ("", "empty")


_TYPED_RE = re.compile(r"^type:\s*[\"']?(%s)[\"']?\s*(#.*)?$" % "|".join(PROFILE_TYPES), re.M)


# ==========================================================================
# Modèle
# ==========================================================================

@dataclass(frozen=True)
class Finding:
    """Un constat de validation, sur le modèle du validateur OKF Federation."""

    code: str
    severity: str           # error | warning
    message: str
    path: str = ""          # fichier (chemin dans le dépôt, ou dans le dossier)
    member: str = ""        # membre de la fédération
    agent: str = ""         # agent concerné (une erreur bloque cet agent)
    host: str = ""          # hôte concerné (une erreur bloque ses agents)
    candidate: str = ""     # L165 : agent élu, sur CET hôte candidat seulement
    untrusted: bool = False  # lu hors source approuvée
    package: str = ""       # fiche WorkPackage concernée (ne bloque aucun agent)
    process: str = ""       # fiche Process concernée (ne bloque aucun agent)
    pipeline: str = ""      # carte Pipeline concernée (ne bloque aucun agent)

    def to_dict(self) -> dict:
        out = {"code": self.code, "severity": self.severity, "member": self.member,
               "path": self.path, "message": self.message}
        if self.agent:
            out["agent"] = self.agent
        if self.host:
            out["host"] = self.host
        if self.candidate:
            out["candidate"] = self.candidate
        if self.package:
            out["package"] = self.package
        if self.process:
            out["process"] = self.process
        if self.pipeline:
            out["pipeline"] = self.pipeline
        if self.untrusted:
            out["untrusted"] = True
        return out

    def where(self) -> str:
        if self.member and self.path:
            return "%s:%s" % (self.member, self.path)
        return self.path or self.member or "canon"


@dataclass
class Source:
    """D'où viennent les fiches d'un membre : un commit, ou des fichiers de travail."""

    member: str
    directory: str
    mode: str = "git"       # git | untrusted
    repo: str = ""
    prefix: str = ""
    rev: str = ""
    commit: str = ""

    def describe(self) -> str:
        if self.mode == "git":
            return "%s @ %s (%s)" % (self.member, self.commit[:12], self.rev)
        return "%s: working files NOT APPROVED (%s)" % (self.member, self.directory)

    def to_dict(self) -> dict:
        return {"member": self.member, "directory": self.directory, "mode": self.mode,
                "repo": self.repo, "prefix": self.prefix, "rev": self.rev,
                "commit": self.commit}


@dataclass
class Fiche:
    type: str
    title: str
    member: str
    path: str
    ref: str                # canon_ref : `<membre>:<chemin>@<commit>` (canon par défaut),
                            # `<canon>/<membre>:<chemin>@<commit>` sinon (L42)
    data: dict              # frontmatter complet (clés inconnues conservées)
    untrusted: bool = False
    #: #538 : chemin RELATIF au bundle (`processus/lots.md`) quand il diffère du
    #: chemin affiché (`knowledge/processus/lots.md` dans un bundle imbriqué) ;
    #: les restrictions de dossier d'une fiche Process portent sur lui.
    bundle_path: str = ""


@dataclass
class Member:
    title: str
    roles: list[str] | None
    authenticators: list | None
    fiche: Fiche
    #: L51 (0032 §2, 0033 §7): humans who can act in their place
    #: (`deputies`) and receive escalations (`superiors`);
    #: references use `human:<id>`.
    deputies: list[str] | None = None
    superiors: list[str] | None = None
    #: L77 (#286): local `user@host` accounts that prove this human on a host.
    #: The declaration bounds the claim to this machine and this Unix user
    #: (decision 0033 §9; OIDC is the v2 path, absent in v1).
    local_accounts: list[str] | None = None


@dataclass
class Agent:
    title: str
    responsible: str | None
    team: str | None
    capabilities: list[str] | None
    harness: str | None
    model: str | None
    provider: str | None
    credential_mode: str | None
    budget_usd_per_day: float | None
    tools: list[str] | None
    reviewers: list[str] | None
    fiche: Fiche
    #: priorité de la persona (L31, 0028) : sous pression critique de l'hôte,
    #: les agents de plus faible priorité sont mis en pause ; 0 par défaut.
    priority: int = 0
    #: dépôt de mémoire de la persona (L31, 0029) : `memory.repository` de la
    #: fiche ; seul renseignement lu par la règle de visibilité (jamais un
    #: secret, jamais un droit).
    memory_repository: str | None = None
    #: L51 (0029 §1): admitted harnesses in preference order (`Persona` card);
    #: `harness` defaults to the first when omitted from the card.
    harnesses: list[str] | None = None
    #: L51 (0032): roles held by the persona, as declared (the role model
    #: belongs to lot L58).
    roles: list[str] | None = None
    #: L243 (#435): the projects the agent works on, as declared
    #: (`projects:`), the first one being its primary project; `team` is then
    #: that first project.
    projects: list[str] | None = None
    #: L243b: alternative spellings of this identity, not new agents.
    aliases: list[str] | None = None
    #: L165 (EP1): how the role is held. `hote` (default) pins the agent to
    #: its placement host; `election` lets any candidate host claim the lease.
    releve: str = "hote"
    #: L165 (EP1): candidate hosts of an elected role. Every one is admitted
    #: by the agent's Placement card, so a claim can only happen on a host
    #: whose current placement verdict admits the agent.
    candidats: list[str] | None = None
    #: #3185: the other projects whose lots this agent may review (`relit:`),
    #: beside its own; only declared projects (`validate`). None: none.
    relit: list[str] | None = None

    @property
    def is_persona(self) -> bool:
        return self.fiche.type == "Persona"

    @property
    def effective_projects(self) -> list[str]:
        """L243: `projects:` when declared, else `[team]`, else []."""
        if self.projects is not None:
            return list(self.projects)
        return [self.team] if self.team else []


@dataclass
class HostPolicy:
    harnesses: list[str] | None = None
    providers: list[str] | None = None
    models: list[str] | None = None
    credential_modes: list[str] | None = None
    max_agents: int | None = None
    #: seuils de ressources de l'hôte (L31, 0028) : mapping
    #: `{min_mem_available, max_swap_used, max_load, min_disk_free}`, plus la
    #: pression (L159d/#2296) `{max_cpu_pressure, max_mem_pressure}`, le
    #: périmètre et les plafonds du cgroup (#2814) `{pressure_scope,
    #: max_cgroup_memory_used, max_cgroup_throttled}`, l'alimentation (L106)
    #: `{min_battery_percent, stop_battery_percent}` et btrfs (#246)
    #: `{min_btrfs_unallocated, min_btrfs_metadata_headroom}` ; None = valeurs
    #: par défaut prudentes (`ameesh.resources`).
    resources: dict | None = None
    #: racine de travail par projet (L31, 0029) : `work_roots[projet]` sinon
    #: `work_root/<projet>` ; remplace le `cwd` d'une admission. None = pas de
    #: dossier déduit.
    work_root: str | None = None
    work_roots: dict | None = None
    #: dossier de travail par agent (L35) : `work_dirs[agent]`, prioritaire sur
    #: `work_roots` et `work_root`. Les trois formes acceptent le gabarit
    #: `{agent}` (un worktree par agent : `~/src/nexlink-{agent}`).
    work_dirs: dict | None = None
    #: ménage (L73) : `{tmp_root, cache_root, tmp_quota, cache_quota,
    #: tmpfs_alert, redirect, xdg_cache, worktrees, orphan_min_size}` validé
    #: par `ameesh.menage.parse_policy` ; None = valeurs par défaut.
    housekeeping: dict | None = None
    #: hôte volatil (L112) : appareil prêté qui peut disparaître à tout
    #: moment (VM Compute) ; son bail est court, 90 s par défaut
    volatile: bool = False
    #: durée du bail des agents de cet hôte (secondes, L112) ; None = 90 s
    #: pour un hôte volatil, sinon la configuration de l'exécuteur
    lease_ttl: float | None = None
    #: offer of a lending host (L159a, docs/GARDE-DE-CAPACITE.md): `{cpu_max,
    #: memory_max, disk_max, …}` validated by `ameesh.capacity_guard.parse_lend`
    #: (sizes in bytes); None = the host lends nothing (absent or unreadable).
    lend: dict | None = None
    #: #4148 (B of #3167): external adapter of the host lifecycle. ameesh runs
    #: `<command> busy|draining|idle-drained <host>` and never touches the
    #: cluster or the cloud itself. None = no adapter, so no automatic release.
    lifecycle_hook: str | None = None

    def work_dir(self, project: str | None, agent: str | None = None) -> str | None:
        """Dossier de travail d'un agent (et de son projet) sur cet hôte, ou None.

        Ordre (L35) : `work_dirs[agent]`, puis `work_roots[projet]`, puis
        `work_root/<projet>` (sans projet, `work_root` lui-même). `{agent}` est
        remplacé par le nom de l'agent ; un gabarit sans agent connu ne donne
        rien (None) plutôt qu'un chemin littéral."""
        dirs = self.work_dirs or {}
        if agent and agent in dirs:
            return fill_work_template(dirs[agent], agent)
        roots = self.work_roots or {}
        if project and project in roots:
            return fill_work_template(roots[project], agent)
        if not self.work_root:
            return None
        base = fill_work_template(self.work_root, agent)
        if base is None:
            return None
        if project:
            return os.path.join(base, project)
        return base


#: seul gabarit admis dans les chemins de travail d'une fiche Host (L35)
WORK_TEMPLATE_FIELDS = ("agent",)
_WORK_TEMPLATE_RE = re.compile(r"\{([^{}]*)\}")


def fill_work_template(path: Any, agent: str | None) -> str | None:
    """Remplace `{agent}` dans un chemin de travail ; None si l'agent manque."""
    text = str(path)
    if "{agent}" in text:
        if not agent:
            return None
        text = text.replace("{agent}", agent)
    return text


def work_template_problem(path: str) -> str | None:
    """Pourquoi un chemin de travail est refusé par `canon check`, ou None (L35).

    Chemin vide, gabarit inconnu (`{projet}`, `{}`) ou accolade orpheline :
    une faute de frappe ne doit pas devenir un dossier littéral `…/{agnet}`."""
    if not isinstance(path, str) or not path.strip():
        return "empty path"
    unknown = [m for m in _WORK_TEMPLATE_RE.findall(path) if m not in WORK_TEMPLATE_FIELDS]
    if unknown:
        return "unknown template %s (only {agent} is allowed)" % ", ".join(
            "{%s}" % u for u in unknown)
    if "{" in _WORK_TEMPLATE_RE.sub("", path) or "}" in _WORK_TEMPLATE_RE.sub("", path):
        return "unmatched brace"
    return None


#: provenance d'un dossier de travail (L35), pour les diagnostics
WORK_FROM_HOST = ("work_dirs", "work_roots", "work_root")
WORK_FROM_PLACEMENT = "placement"

#: L101 : clés de la forme canonique `policy.workspaces` et les anciennes clés
#: qu'elles remplacent, lues comme alias sans date de retrait. `HostPolicy`
#: garde ses champs historiques : la normalisation est faite au parse.
WORKSPACES_FIELDS = ("dirs", "roots", "root", "housekeeping")
LEGACY_WORKSPACES = {"dirs": "work_dirs", "roots": "work_roots",
                     "root": "work_root", "housekeeping": "housekeeping"}


def work_dir_for(host: "Host | None", agent: "Agent", admission: "Placement | None"
                 ) -> tuple[str | None, str | None]:
    """(dossier, provenance) d'un agent sur un hôte (L31, 0029 ; L35).

    La politique de l'hôte gagne toujours. Repli TRANSITOIRE (L35) : si elle ne
    donne rien pour cet agent, le `cwd` de l'ancienne fiche Placement, signalé
    `admission-cwd-inherited` par `canon check` et noté par `canon sync`. Le
    chemin rendu n'est pas développé (`~` reste) : l'appelant s'en charge."""
    if host is not None:
        policy = host.policy
        dirs = policy.work_dirs or {}
        roots = policy.work_roots or {}
        spellings = [agent.title] + list(agent.aliases or [])
        explicit = next((n for n in spellings if n in dirs), None)
        found = policy.work_dir(agent.team, explicit or agent.title)
        if found:
            source = ("work_dirs" if explicit is not None else
                      "work_roots" if agent.team and agent.team in roots else "work_root")
            return found, source
    if admission is not None and admission.cwd:
        return admission.cwd, WORK_FROM_PLACEMENT
    return None, None


@dataclass
class Host:
    title: str
    responsible: str | None
    policy: HostPolicy
    fiche: Fiche
    #: étiquettes de l'hôte (L31, 0029) : une admission peut viser des
    #: `host_tags` au lieu de nommer les hôtes.
    tags: list[str] | None = None
    #: administrateurs humains de l'hôte (L31, 0029) : avec le responsable, ce
    #: sont eux qui doivent avoir accès au dépôt de mémoire d'une persona pour
    #: qu'elle tourne ici.
    admins: list[str] | None = None
    #: humains qui ont l'ACCÈS PHYSIQUE à la machine (L110, 0029), distincts
    #: des `admins` : un enfant qui utilise le portable prêté ne l'administre
    #: pas, mais peut tout lire dans la VM. Ils comptent dans `H(hôte)` comme
    #: le responsable et les admins. Absent : aucun occupant au-delà du
    #: responsable et des admins — seules les personas lisibles par eux
    #: partent sur la machine (défaut sûr : rien n'est supposé de plus).
    occupants: list[str] | None = None
    #: #3070 (canon §12) : l'appareil est déclaré indisponible jusqu'à ce
    #: jour ISO inclus (`indisponible_jusqu_au`), avec son motif
    #: (`indisponible_motif`). Une séance qui l'attend ne retarde rien
    #: (`device_gates`) ; passé ce jour, la déclaration cesse d'elle-même.
    unavailable_until: str | None = None
    unavailable_reason: str | None = None

    def unavailable_end(self) -> float | None:
        """Fin de l'indisponibilité (minuit local du lendemain), ou None."""
        if not self.unavailable_until:
            return None
        day = _dt.date.fromisoformat(self.unavailable_until) + _dt.timedelta(days=1)
        return float(_dt.datetime.combine(day, _dt.time()).timestamp())

    def unavailable_at(self, now: float) -> bool:
        """L'appareil est-il indisponible à l'instant `now` (epoch) ?"""
        end = self.unavailable_end()
        return end is not None and float(now) < end


@dataclass
class Placement:
    """Une ADMISSION (L31, 0029) : persona → hôtes ou étiquettes d'hôtes admis.

    Plus de `cwd` (le dossier de travail est un réglage de l'hôte) ; les
    anciennes fiches qui en portent un sont lues avec un constat
    `admission-cwd-ignored` quand la politique de l'hôte donne un dossier, ou
    `admission-cwd-inherited` quand elle n'en donne pas : le `cwd` sert alors
    de repli transitoire (L35, retiré au plus tard en v1.5.0). `host` reste lu
    comme un hôte admis unique (lecture transitoire des anciennes fiches)."""

    title: str
    agent: str | None
    host: str | None
    credential_mode: str | None
    cwd: str | None
    fiche: Fiche
    #: hôtes admis (L31, 0029) et étiquettes d'hôtes admis ; lus avec l'ancien
    #: `host` (hôte unique) et l'ancien `cwd` (repli transitoire, L35).
    hosts: list[str] | None = None
    host_tags: list[str] | None = None

    def host_names(self) -> list[str]:
        """Hôtes NOMMÉS admis (l'ancien `host` compris), sans doublon."""
        out: list[str] = []
        for name in ([self.host] if self.host else []) + list(self.hosts or []):
            if name and name not in out:
                out.append(name)
        return out

    def tags(self) -> list[str]:
        return [t for t in (self.host_tags or []) if t]


@dataclass
class WorkPackage:
    """Une fiche du plan de travail (L29) : jalon, epic ou lot."""

    id: str
    title: str
    kind: str | None
    parent: str | None
    responsible: str | None
    team: str | None
    scope: list[str] | None
    status: str | None
    fiche: Fiche
    #: L96 : dates déclarées (jours ISO) — début, fin, livraison (ou `date`
    #: d'un jalon)
    start: str | None = None
    end: str | None = None
    delivery: str | None = None


@dataclass
class DecisionNote:
    """L96 : une fiche `Decision` du canon, lue pour proposer des éléments de
    feuille de route (jamais pour décider : ameesh n'en tire aucun droit)."""

    fiche: Fiche
    body: str


@dataclass
class Process:
    """#538 : une fiche `Process` du canon (`processus/*.md`) — les règles de
    travail, lues pour ouvrir le brief des agents et poussées après un sync.

    `valid` est faux quand une clé obligatoire manque ou est illisible : la
    fiche reste listée (`canon show`), jamais injectée dans un brief.

    #1372 : `projection` est la projection courte portée par la carte (entre
    les marqueurs), injectée à la place de `body` ; `projection_error` dit
    pourquoi elle est inutilisable ("unterminated", "multiple", "stray-end",
    "empty"), et vaut "" quand il n'y a pas de projection ou qu'elle est lue.
    """

    fiche: Fiche
    description: str = ""
    responsible: str = ""
    updated: str = ""       # jour ISO (AAAA-MM-JJ), obligatoire
    expire: str = ""        # jour ISO, vide sans échéance
    body: str = ""
    projection: str = ""
    projection_error: str = ""
    valid: bool = True

    @property
    def title(self) -> str:
        return self.fiche.title

    def to_dict(self) -> dict:
        out = {"title": self.title, "member": self.fiche.member,
               "path": self.fiche.path, "ref": self.fiche.ref,
               "description": self.description, "responsible": self.responsible,
               "updated": self.updated, "expire": self.expire,
               "body_bytes": len(self.body.encode("utf-8")), "valid": self.valid}
        if self.projection:
            out["projection_bytes"] = len(self.projection.encode("utf-8"))
        if self.projection_error:
            out["projection_error"] = self.projection_error
        if self.fiche.untrusted:
            out["untrusted"] = True
        return out


@dataclass
class Pipeline:
    """L103 (#4395) : une carte `Pipeline` du canon (`pipelines/*.md`) — un
    pipeline déclaré, compilé par `canon check` en `process-workflow/1`.

    `name` est le nom du module (clé `name`, défaut : nom du fichier sans
    `.md`). `artifact` est l'artefact compilé (`CompiledWorkflow`), None quand
    le bloc est refusé ; `error` dit pourquoi. `valid` est faux sur toute
    erreur de la carte : elle reste listée (`canon show`), sans bloquer aucun
    agent."""

    fiche: Fiche
    name: str = ""
    description: str = ""
    responsible: str = ""
    updated: str = ""
    artifact: Any = None
    error: str = ""
    valid: bool = True

    @property
    def title(self) -> str:
        return self.fiche.title

    def to_dict(self) -> dict:
        out = {"title": self.title, "name": self.name, "member": self.fiche.member,
               "path": self.fiche.path, "ref": self.fiche.ref,
               "description": self.description, "responsible": self.responsible,
               "updated": self.updated, "valid": self.valid}
        if self.artifact is not None:
            out.update({"initial": self.artifact.initial,
                        "types": list(self.artifact.types),
                        "stages": [s.status for s in self.artifact.stages],
                        "transitions": len(self.artifact.transitions)})
        if self.error:
            out["error"] = self.error
        if self.fiche.untrusted:
            out["untrusted"] = True
        return out


@dataclass
class Canon:
    root: str
    sources: list[Source] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    hosts: list[Host] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    packages: list[WorkPackage] = field(default_factory=list)
    #: L96 : fiches Decision (frontmatter et corps), pour `ameesh plan propose`
    decisions: list[DecisionNote] = field(default_factory=list)
    #: #538 : fiches Process (règles de travail), injectées dans les briefs
    processes: list[Process] = field(default_factory=list)
    #: L103 (#4395) : cartes Pipeline (modules déclaratifs), validées par
    #: `canon check`
    pipelines: list[Pipeline] = field(default_factory=list)
    federation: dict | None = None
    #: constats de lecture (source, fédération, frontmatter, champs)
    load_findings: list[Finding] = field(default_factory=list)
    #: faux si la racine n'a pas pu être lue : aucune donnée n'est utilisable
    readable: bool = False
    #: L42 (0031) : identité du canon — `id` de federation.yaml, sinon le nom
    #: du dossier (`id_source` le dit) — et rang : le canon par défaut (le
    #: premier configuré) garde les références et les lignes historiques
    #: (`canon` NULL au registre) ; les autres préfixent leurs références et
    #: écrivent leur identifiant.
    id: str = ""
    id_source: str = ""
    is_default: bool = True
    #: L43 (0031) : périmètre ameesh déclaré, par membre (`ameesh.scope` de
    #: federation.yaml) : dossiers relatifs au bundle du membre ; un membre
    #: absent de ce mapping est lu en entier (comportement historique).
    scopes: dict = field(default_factory=dict)
    #: membres dont le périmètre est invalide ou introuvable : aucune de leurs
    #: fiches n'est lue, et ils ne comptent pas comme LUS (`loaded_members`) —
    #: la synchronisation ne retire ni n'arrête rien pour absence (fail closed).
    scope_unread: set = field(default_factory=set)
    #: fiches du profil ignorées hors du périmètre, par membre (information)
    out_of_scope: dict = field(default_factory=dict)
    #: L243 (#468) : nommage par défaut des nouveaux agents
    #: (`extensions.ameesh.naming`) ; None = aucune convention déclarée.
    naming: "Naming | None" = None
    #: L243 (#468) : révision DEMANDÉE au chargement (`--ref`, `ref` de
    #: l'entrée `canons`, branche du manifeste). Renseignée même quand le canon
    #: n'a pas pu être lu : le refus d'un canon illisible doit nommer la ref à
    #: récupérer (codex r3, #452).
    rev: str = ""

    # -- accès ---------------------------------------------------------------
    @property
    def state_key(self) -> str:
        """Clé de `canon_state.canon` (L42) : '' pour le canon par défaut."""
        return "" if self.is_default else self.id

    @property
    def registry_canon(self) -> str | None:
        """Valeur de la colonne `canon` des lignes qu'il déclare : NULL (None)
        pour le canon par défaut, son identifiant sinon (L42)."""
        return None if self.is_default else self.id

    def owns(self, row_canon: str | None) -> bool:
        """Une ligne (registre, paquet) dont la colonne `canon` vaut
        `row_canon` appartient-elle à ce canon ? NULL = canon par défaut."""
        if row_canon is None or row_canon == "":
            return self.is_default
        return row_canon == self.id

    def member_of(self, ref: str | None) -> str | None:
        """Membre d'une référence de fiche de CE canon, ou None si la
        référence est d'un autre canon (préfixe différent) ou illisible."""
        parsed = split_ref(ref)
        if parsed is None:
            head = (ref or "").split(":", 1)[0]
            return head if self.is_default and head and "/" not in head else None
        prefix, member, _path, _version = parsed
        if prefix is None:
            return member if self.is_default else None
        return member if prefix == self.id else None

    def label(self) -> str:
        """Nom lisible du canon : identifiant et racine."""
        return "%s (%s%s)" % (self.id or "?", self.root,
                              ", default" if self.is_default else "")

    @property
    def untrusted(self) -> bool:
        return any(source.mode != "git" for source in self.sources)

    @property
    def roles(self) -> dict:
        roles = (self.federation or {}).get("roles")
        return roles if isinstance(roles, dict) else {}

    @property
    def review_policies(self) -> dict:
        policies = (self.federation or {}).get("review_policies")
        return policies if isinstance(policies, dict) else {}

    def loaded_members(self) -> set[str]:
        """Membres dont les fiches ont été lues. L43 : un membre au périmètre
        invalide ou introuvable n'en est pas (rien n'est retiré pour absence)."""
        return {source.member for source in self.sources} - set(self.scope_unread)

    def agent(self, name: str) -> Agent | None:
        from .agent_names import resolve
        try:
            name = resolve(name, canon=self)
        except ValueError:
            return None
        for agent in self.agents:
            if agent.title == name:
                return agent
        return None

    def host(self, name: str) -> Host | None:
        for host in self.hosts:
            if host.title == name:
                return host
        return None

    def package(self, ident: str) -> WorkPackage | None:
        for package in self.packages:
            if package.id == ident:
                return package
        return None

    def process(self, title: str) -> Process | None:
        for process in self.processes:
            if process.title == title:
                return process
        return None

    def pipeline(self, name: str) -> Pipeline | None:
        for pipeline in self.pipelines:
            if pipeline.name == name:
                return pipeline
        return None

    def placements_of(self, agent: str) -> list[Placement]:
        from .agent_names import resolve
        try:
            agent = resolve(agent, canon=self)
        except ValueError:
            return []
        return [p for p in self.placements
                if p.agent == agent or p.agent in ((self.agent(agent).aliases or [])
                                                  if self.agent(agent) else [])]

    def resolve_human(self, responsible: str | None) -> str | None:
        """`human:<id>` qui désigne un Member du canon → `human:<id>`, sinon None.

        Strict : un identifiant sans préfixe, un `agent:`, ou un Member en
        double ne résout pas (fail closed).

        L43 (0031) : dans CE canon seulement — un agent, un hôte ou un paquet
        se résout dans le canon qui le déclare ; un Member d'un autre canon de
        l'hôte ne compte pas.
        """
        if not responsible or not responsible.startswith("human:"):
            return None
        ident = responsible[len("human:"):].strip()
        matches = [m for m in self.members if m.title == ident]
        if len(matches) != 1:
            return None
        if any(a.title == ident for a in self.agents):
            return None  # un agent ne se déclare pas humain en prenant un nom de membre
        return "human:%s" % ident

    def source_label(self) -> str:
        return "; ".join(source.describe() for source in self.sources) or "no source read"

    def to_dict(self) -> dict:
        def fiche(f: Fiche) -> dict:
            return {"member": f.member, "path": f.path, "canon_ref": f.ref}

        return {
            "root": self.root,
            "id": self.id,
            "id_source": self.id_source,
            "default": self.is_default,
            "readable": self.readable,
            "untrusted": self.untrusted,
            "sources": [s.to_dict() for s in self.sources],
            "members": [dict(title=m.title, roles=m.roles, deputies=m.deputies,
                             superiors=m.superiors,
                             local_accounts=m.local_accounts, **fiche(m.fiche))
                        for m in self.members],
            "hosts": [dict(title=h.title, responsible=h.responsible, tags=h.tags,
                           admins=h.admins, occupants=h.occupants,
                           unavailable_until=h.unavailable_until,
                           unavailable_reason=h.unavailable_reason,
                           policy=h.policy.__dict__.copy(), **fiche(h.fiche))
                      for h in self.hosts],
            "agents": [dict(title=a.title, aliases=a.aliases or [], type=a.fiche.type, responsible=a.responsible, team=a.team,
                            capabilities=a.capabilities, harness=a.harness, model=a.model,
                            harnesses=a.harnesses, roles=a.roles, projects=a.projects,
                            relit=a.relit,
                            provider=a.provider, credential_mode=a.credential_mode,
                            budget_usd_per_day=a.budget_usd_per_day, tools=a.tools,
                            reviewers=a.reviewers, priority=a.priority,
                            memory_repository=a.memory_repository,
                            hosts=[p.host for p in self.placements_of(a.title)],
                            **fiche(a.fiche))
                       for a in self.agents],
            "placements": [dict(title=p.title, agent=p.agent, host=p.host,
                                hosts=p.hosts, host_tags=p.host_tags,
                                credential_mode=p.credential_mode, cwd=p.cwd, **fiche(p.fiche))
                           for p in self.placements],
            "packages": [dict(id=w.id, title=w.title, kind=w.kind, parent=w.parent,
                              responsible=w.responsible, team=w.team, scope=w.scope,
                              status=w.status, **fiche(w.fiche))
                         for w in self.packages],
            "processes": [p.to_dict() for p in self.processes],
            "pipelines": [p.to_dict() for p in self.pipelines],
            "federation": {"id": (self.federation or {}).get("id"),
                           "roles": self.roles, "review_policies": self.review_policies,
                           "naming": self.naming.to_dict() if self.naming else None},
            # L43 (0031) : périmètre ameesh par membre, fiches ignorées hors périmètre
            "ameesh_scope": {member: list(dirs) for member, dirs in sorted(self.scopes.items())},
            "ameesh_scope_unread": sorted(self.scope_unread),
            "out_of_scope": dict(sorted(self.out_of_scope.items())),
        }


# ==========================================================================
# Charge d'admissions d'un hôte (`max_agents`) — #3101 (suite d'EP1, L165)
# ==========================================================================

def elected_candidates(canon: Canon, placement: Any, covered: Iterable[str]) -> list[str] | None:
    """Hôtes où l'admission d'un rôle élu peut tenir le bail, ou None (épinglé).

    Un agent `releve: election` avec `candidats` ne tourne que là où il tient
    le bail : son admission ne prend qu'UNE place dans tout le maillage, sur
    l'un de ses candidats couverts par l'admission et dont la politique
    l'admet (un candidat refusé ne peut pas tenir le bail). Tout autre agent
    (et un rôle élu sans candidat, déjà en erreur) occupe une place sur chaque
    hôte couvert.
    """
    agent = canon.agent(placement.agent) if placement.agent else None
    if agent is None or agent.releve != "election" or not agent.candidats:
        return None
    covered = set(covered)
    return [name for name in agent.candidats if name in covered
            and canon.host(name) is not None
            and not placement_violations(agent, canon.host(name), placement)]


def seat_elected(free: Mapping[str, int | None],
                 roles: Mapping[str, Sequence[str]]) -> dict[str, str | None]:
    """Asseoir chaque rôle élu sur un seul de ses candidats (#3101).

    `free` : places laissées par les admissions épinglées (None = sans
    maximum) ; `roles` : rôle -> candidats, dans l'ordre déclaré. Couplage
    maximal par chemins augmentants : un rôle déjà assis cède sa place s'il
    peut s'asseoir ailleurs. Rend rôle -> hôte, ou None si aucune place ne
    reste ; déterministe (rôles triés, candidats dans l'ordre).
    """
    seat: dict[str, str | None] = {}
    occupants: dict[str, list[str]] = defaultdict(list)

    def room(host: str) -> bool:
        cap = free.get(host)
        return cap is None or len(occupants[host]) < cap

    def place(role: str, seen: set[str]) -> bool:
        for host in roles[role]:
            if host in seen:
                continue
            seen.add(host)
            if room(host):
                occupants[host].append(role)
                seat[role] = host
                return True
            for other in list(occupants[host]):
                if place(other, seen):
                    occupants[host].remove(other)
                    occupants[host].append(role)
                    seat[role] = host
                    return True
        return False

    for role in sorted(roles):
        if not place(role, set()):
            seat[role] = None
    return seat


def admission_load(canon: Canon, covered: Mapping[str, Iterable[Any]],
                   extra: str | None = None, only: Iterable[str] | None = None,
                   ) -> tuple[dict[str, int], dict[str, str | None]]:
    """Admissions épinglées par hôte et siège de chaque rôle élu (#3101).

    `covered` : hôte -> admissions qui le couvrent ; `extra` : un hôte où
    compter une admission épinglée de plus (proposition de placement) ;
    `only` : les seuls rôles élus à asseoir (les autres ne prennent rien).
    """
    pinned: dict[str, int] = defaultdict(int)
    hosts_of: dict[int, set[str]] = defaultdict(set)
    by_id: dict[int, Any] = {}
    for host, admissions in covered.items():
        for admission in admissions:
            hosts_of[id(admission)].add(host)
            by_id[id(admission)] = admission
    roles: dict[str, list[str]] = {}
    for key, admission in by_id.items():
        candidates = elected_candidates(canon, admission, hosts_of[key])
        if candidates is None:
            for host in hosts_of[key]:
                pinned[host] += 1
        else:
            seats = roles.setdefault(canon.agent(admission.agent).title, [])
            seats += [host for host in candidates if host not in seats]
    if extra is not None:
        pinned[extra] += 1
    if only is not None:
        keep = set(only)
        roles = {role: hosts for role, hosts in roles.items() if role in keep}
    free: dict[str, int | None] = {}
    for host in canon.hosts:
        limit = host.policy.max_agents
        free[host.title] = None if limit is None else max(limit - pinned[host.title], 0)
    return dict(pinned), seat_elected(free, roles)


# ==========================================================================
# Politique d'hôte (C4) — réutilisée par L3 (`placement.evaluate`, dont le
# verdict, écrit par `canon sync`, entre dans la condition de réclamation)
# ==========================================================================

def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def placement_violations(agent: Any, host: Any, placement: Any = None) -> list[str]:
    """Raisons pour lesquelles ce placement viole la politique de l'hôte ([] = admis).

    Accepte les objets du canon (`Agent`, `Host`, `Placement`) ou des mappings
    (ligne du registre, dict de politique). Règles :

    * une clé de politique **absente** admet tout ; une liste **vide** n'admet rien ;
    * une valeur **non déclarée** par l'agent n'est pas admise quand l'hôte
      restreint cette clé (fail closed) ;
    * le mode d'identifiants est celui du placement, sinon celui de l'agent ;
    * `models` accepte des motifs (`deepseek-*`).
    """
    policy = _get(host, "policy")
    if policy is None:
        return []
    host_name = _get(host, "title") or _get(host, "name") or "?"
    mode = _get(placement, "credential_mode") or _get(agent, "credential_mode")
    checks = (
        ("harnesses", "harness", _get(agent, "harness"), False),
        ("providers", "provider", _get(agent, "provider"), False),
        ("models", "model", _get(agent, "model"), True),
        ("credential_modes", "credential mode", mode, False),
    )
    out: list[str] = []
    for key, label, value, pattern in checks:
        allowed = _get(policy, key)
        if allowed is None:
            continue
        if isinstance(allowed, str):
            allowed = [allowed]
        allowed = [str(a) for a in allowed]
        text = None if value is None or value == "" else str(value)
        if text is not None and (
                any(fnmatch.fnmatchcase(text, a) for a in allowed) if pattern
                else text in allowed):
            continue
        out.append("%s %s not allowed by host %s (allowed: %s)" % (
            label, text if text is not None else "undeclared", host_name,
            ", ".join(allowed) or "none"))
    return out


# ==========================================================================
# git : lecture par les objets, jamais par l'arbre de travail
# ==========================================================================

#: variables qui redirigeraient git vers un autre dépôt, index ou objet
_GIT_ENV_DROP = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_REPLACE_REF_BASE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


class GitError(RuntimeError):
    pass


class GitDeadline(RuntimeError):
    """#2665 : l'échéance monotone d'une découverte de canon est épuisée.

    Volontairement **pas** une sous-classe de `GitError` : les lecteurs du
    canon convertissent un `GitError` en constat (`canon-git-error`,
    `readable = False`) et continueraient leur découverte. Une échéance
    épuisée n'est pas un canon illisible : elle remonte jusqu'à l'appelant
    borné, qui la dit `run-incomplete`, jamais « canon sans bloc ».
    """


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _GIT_ENV_DROP and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               LC_ALL="C")
    return env


#: #1749 : une **sonde de présence** (l'objet est-il là ?) ne doit pas
#: réveiller le fetch paresseux d'un clone partiel : il part, échoue, et
#: laisse son transport orphelin, adopté par l'exécuteur puis jamais
#: moissonné (des milliers de `git <defunct>`). Git honore
#: `GIT_NO_LAZY_FETCH` (2.43) ; les lectures qui ont besoin du contenu
#: (patchs, blobs) gardent le fetch.
NO_LAZY_FETCH = {"GIT_NO_LAZY_FETCH": "1"}


#: #3753 : cache, dans le processus, des lectures git qui ne dépendent que du
#: **contenu** du dépôt. Une entrée n'est posée que pour une commande dont
#: toutes les révisions sont des objets adressés par leur SHA complet
#: (`_content_cache_key`) : une tête qui bouge produit une autre clé, et
#: aucune ref symbolique, aucun chemin du dépôt de travail, ni aucune lecture
#: d'état (fetch, statut, remote, refs) n'entre ici. Un échec n'est jamais
#: retenu : un objet absent peut être rapatrié plus tard par un fetch.
#: #3829 : la clé porte aussi l'identité de l'**instance** du dépôt, pas
#: seulement son chemin : un dépôt supprimé puis recréé au même chemin (clone,
#: `init`, `rm -rf` suivi d'un clone hors de `canon._git`) ne doit jamais
#: relire une réponse positive de l'ancien (présence d'un objet, contenu).
_CONTENT_CACHE: dict = {}
_CONTENT_CACHE_LOCK = threading.Lock()
_CONTENT_CACHE_BYTES = 0
_CONTENT_CACHE_HITS = 0
_CONTENT_CACHE_MISSES = 0
CONTENT_CACHE_MAX_ENTRIES = 4096
CONTENT_CACHE_MAX_BYTES = 32 * 1024 * 1024

_FULL_SHA = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
#: une révision **déjà résolue** : un SHA, éventuellement suffixé d'un
#: parcours d'objet (`^{commit}`, `^1`, `~2`, `^@`) ou d'un chemin de blob
#: (`<sha>:dir/fichier`). Rien qui puisse désigner une ref mouvante.
_REV_ATOM_RE = re.compile(
    r"^%s(?:\^[0-9]*|\^[@!]|\^\{[a-z]+\}|~[0-9]*)?(?::.*)?$" % _FULL_SHA)

#: options sans effet sur l'état du dépôt, par sous-commande. Une option
#: inconnue n'est pas cachée (elle pourrait lire l'index, les refs ou un
#: programme externe) : le cache reste exact même quand le code évolue.
_CACHE_OPTIONS = {
    "show": {
        "exact": {"-s", "-p", "-z", "--no-patch", "--no-color", "--no-renames",
                  "--name-only", "--name-status", "--numstat", "--shortstat",
                  "--stat", "--raw", "--patch", "--full-index", "--binary",
                  "--no-ext-diff", "--no-textconv", "--no-prefix",
                  "--default-prefix", "--full-diff"},
        "prefix": ("--format=", "--pretty=", "--date=", "--unified=",
                   "--abbrev=", "--diff-filter=", "--find-renames=",
                   "--stat=", "--stat-width=", "--stat-name-width=",
                   "--stat-count=", "--inter-hunk-context=", "--color=",
                   "--word-diff=", "--src-prefix=", "--dst-prefix=",
                   "--line-prefix=", "--relative=", "--diff-algorithm="),
    },
    "diff": {
        "exact": {"-z", "--name-only", "--name-status", "--numstat",
                  "--shortstat", "--stat", "--raw", "--patch", "--no-color",
                  "--no-renames", "--no-ext-diff", "--no-textconv",
                  "--full-index", "--binary", "--no-prefix",
                  "--default-prefix", "--quiet", "--exit-code",
                  "--find-renames"},
        "prefix": ("--unified=", "--diff-filter=", "--find-renames=",
                   "--stat=", "--stat-width=", "--stat-name-width=",
                   "--stat-count=", "--inter-hunk-context=", "--color=",
                   "--word-diff=", "--src-prefix=", "--dst-prefix=",
                   "--line-prefix=", "--relative=", "--diff-algorithm=",
                   "--abbrev="),
    },
    "log": {
        "exact": {"-p", "-z", "--no-merges", "--merges", "--no-color",
                  "--first-parent", "--reverse", "--no-walk", "--not",
                  "--boundary", "--topo-order", "--date-order",
                  "--author-date-order", "--ancestry-path", "--left-right",
                  "--cherry-pick", "--graph"},
        "prefix": ("--format=", "--pretty=", "--date=", "--max-count=",
                   "--abbrev=", "--author=", "--grep=",
                   "--diff-filter=", "--find-renames="),
    },
    "rev-list": {
        "exact": {"--no-merges", "--merges", "--parents", "--first-parent",
                  "--reverse", "--count", "--left-right", "--cherry-pick",
                  "--no-color", "--not", "--boundary", "--topo-order",
                  "--date-order", "--author-date-order", "--ancestry-path"},
        "prefix": ("--max-count=", "--format=", "--pretty=", "--abbrev=",
                   "--date=", "--diff-filter=", "--find-renames="),
    },
    "ls-tree": {
        "exact": {"-r", "-z", "-l", "-t", "--full-tree", "--name-only",
                  "--name-status", "--object-only", "--full-name", "--long"},
        "prefix": (),
    },
    "merge-base": {
        "exact": {"--is-ancestor", "--octopus", "--independent"},
        "prefix": (),
    },
    "grep": {
        "exact": {"--no-color", "-l", "-I", "-F", "-h", "-a", "-n", "-i",
                  "-w", "-E", "-G", "-P", "--no-line-number", "--no-column",
                  "--full-name", "--null", "-z"},
        "prefix": (),
    },
    "merge-tree": {"exact": {"--write-tree"}, "prefix": ()},
    "rev-parse": {
        "exact": {"--verify", "--quiet", "-q", "--end-of-options"},
        "prefix": (),
    },
}
#: options qui prennent leur valeur dans l'argument suivant ; la valeur n'est
#: pas une révision, elle est consommée avec l'option.
_CACHE_OPTION_VALUES = frozenset(("-n", "-f", "-e", "--max-count", "--format",
                                  "--pretty", "--date", "--unified",
                                  "--diff-filter", "--find-renames", "--abbrev",
                                  "--author", "--grep"))
#: sous-commandes qui peuvent faire disparaître des objets : leurs entrées de
#: cache pour ce dépôt sont oubliées (une lecture d'un objet supprimé ne doit
#: pas survivre au passage qui l'a supprimé).
_CACHE_DROP_COMMANDS = frozenset(("gc", "prune", "repack", "init", "clone"))

#: #3777 : placeholders de format qui lisent autre chose que l'objet — refs
#: (décorations, reflog, signature, notes, ref, `describe`), dates relatives
#: ou humaines, noms et courriels résolus par `.mailmap`. Leur sortie dépend
#: des refs, de l'heure ou du mailmap, pas du seul commit lu : jamais cachée.
_FORMAT_STATE_RE = re.compile(
    r"%(?:d|D|g|G|N|S|[ac][rhNE]|\(describe|\(decorate)")
#: #3777 : dates dont la sortie dépend de l'instant de lecture.
_CACHE_STATEFUL_DATES = frozenset(("relative", "human"))
#: #3829 : options de `clone`/`init` qui consomment l'argument suivant ; sans
#: cette table, la valeur d'une option se lirait comme un chemin à oublier.
#: Une option inconnue est traitée comme sans valeur : au pire un chemin est
#: vidé sans entrée, jamais un dépôt recréé ne garde son ancienne lecture.
_CLONE_VALUE_OPTIONS = frozenset((
    "-b", "--branch", "-o", "--origin", "-u", "--upload-pack", "--template",
    "--reference", "--reference-if-able", "--separate-git-dir", "--depth",
    "--shallow-since", "--shallow-exclude", "--filter", "-c", "--config",
    "-j", "--jobs", "--server-option", "--revision", "--ref-format",
))
_INIT_VALUE_OPTIONS = frozenset((
    "-b", "--initial-branch", "--template", "--separate-git-dir",
    "--object-format", "--ref-format",
))


def _command_positionals(tokens: tuple[str, ...],
                         valued: frozenset[str]) -> list[str]:
    """Les arguments positionnels d'une commande, options et valeurs écartées."""
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            out.extend(tokens[index + 1:])
            break
        if token.startswith("-") and token != "-":
            index += 2 if "=" not in token and token in valued else 1
            continue
        out.append(token)
        index += 1
    return out


def _clone_directory(source: str, *, bare: bool) -> str | None:
    """Le dossier qu'un `git clone <source>` sans destination crée."""
    text = source.rstrip("/")
    if "://" not in text and ":" in text:        # forme scp `hôte:chemin`
        text = text.rsplit(":", 1)[1]
    if text.endswith("/.git"):
        text = os.path.dirname(text)
    name = os.path.basename(text)
    if name.endswith(".git"):
        name = name[:-4]
    if not name or name in (".", ".."):
        return None
    return name + ".git" if bare else name


def _recreated_repository_paths(sub: str, repo: str,
                                tokens: tuple[str, ...]) -> list[str]:
    """Les chemins dont `init`/`clone` viennent de (re)créer le dépôt (#3829).

    `repo` est le cwd de la commande : une destination relative l'est aussi.
    """
    if sub == "init":
        values = _command_positionals(tokens, _INIT_VALUE_OPTIONS)
        created = values[:1]
    elif sub == "clone":
        values = _command_positionals(tokens, _CLONE_VALUE_OPTIONS)
        if len(values) >= 2:
            created = [values[-1]]
        elif len(values) == 1:
            name = _clone_directory(
                values[0], bare=any(flag in tokens for flag in ("--bare", "--mirror")))
            created = [name] if name else []
        else:
            created = []
    else:
        return []
    return [value if os.path.isabs(value) else os.path.join(repo, value)
            for value in created]


def _repo_cache_key(repo: str) -> str:
    """Le dépôt lu, indépendamment d'un chemin relatif ou d'un lien."""
    return os.path.realpath(os.path.abspath(repo or "."))


def _stat_token(path: str):
    """`(périphérique, inode, ctime)` d'un chemin, ou None s'il est illisible."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns)


def _instance_token(path: str):
    """`(périphérique, inode)` : l'instance d'un répertoire, sans son ctime.

    #3834 (N2 de #3829) : le ctime d'un répertoire git change à chaque
    écriture au premier niveau (un `index.lock` créé puis supprimé, un
    `pack-refs`, `gc.pid`, `HEAD` réécrit) ; le garder dans la clé faisait
    perdre les hits d'un clone actif. L'inode ne bouge pas sur ces écritures.
    """
    token = _stat_token(path)
    return token[:2] if token is not None else None


def _objects_identity(objects: str):
    """L'instance du magasin d'objets, sans le ctime de `objects/` lui-même.

    #3834 (N2 de #3829) : `objects/` change de ctime dès qu'un fetch ou un
    commit y crée un nouveau préfixe de deux caractères ; dans un clone
    actif, s'en servir perdait les hits. `objects/info/` est créé avec le
    dépôt puis n'est touché ni par les commits, ni par les fetches, ni par
    `pack-refs` : son ctime date l'instance. L'inode seul ne suffit pas à
    détecter une recréation (constaté : réutilisation immédiate) ; le ctime
    de `objects/info/` si, et il reste stable sur l'activité courante. Un
    dépôt sans `objects/info/` retombe sur `objects/` : mieux vaut un hit
    perdu qu'une lecture périmée.
    """
    base = _instance_token(objects)
    if base is None:
        return None
    born = _stat_token(os.path.join(objects, "info"))
    if born is None:
        born = _stat_token(objects)
    if born is None:
        return None
    return (base, born)


def _looks_like_git_directory(path: str) -> bool:
    """Un répertoire git nu : `HEAD` et un magasin `objects/` à ce chemin."""
    return (os.path.exists(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects")))


def _git_directory(repo: str, env_extra: dict | None = None) -> str | None:
    """Le répertoire git qu'une commande lancée dans `repo` lit effectivement."""
    explicit = (env_extra or {}).get("GIT_DIR")
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(repo, explicit)
        return os.path.realpath(path)
    start = _repo_cache_key(repo)
    # #3834 (N1 de #3829) : git reconnaît d'abord le dépôt nu (ou le
    # répertoire git) placé à ce chemin ; remonter avant lui faisait prendre
    # à un dépôt nu l'identité du `.git` englobant.
    if _looks_like_git_directory(start):
        return start
    directory = start
    while True:
        marker = os.path.join(directory, ".git")
        if os.path.lexists(marker):
            return os.path.realpath(marker)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None


def _repository_identity(repo: str, env_extra: dict | None = None):
    """L'instance du dépôt lu, pas seulement son chemin (#3829, #3834).

    Le contenu d'un objet adressé par un SHA ne change jamais ; sa **présence**
    dépend du dépôt qui est au chemin. Un dépôt recréé au même chemin — y
    compris par un `rm -rf` puis un clone faits hors de `canon._git` — ne doit
    jamais relire une réponse positive de l'ancien ; un dépôt seulement mis à
    jour (fetch, nouveau commit) garde son identité, donc le gain de #3753 est
    conservé.

    L'identité porte l'inode du répertoire git (stable sur les écritures de
    son premier niveau, N2) et la naissance du magasin d'objets (`objects/info`,
    qui ne bouge pas sur un fetch ou un commit). Le ctime du répertoire git
    lui-même est volontairement absent : `index.lock`, `pack-refs`, `gc.pid`
    ou une réécriture de `HEAD` le font changer sans recréer le dépôt.
    """
    gitdir = _git_directory(repo, env_extra)
    if not gitdir:
        return None
    if os.path.isdir(gitdir):
        token = _instance_token(gitdir)
    else:
        # `.git` d'un worktree lié : un fichier que git ne réécrit pas ; son
        # magasin d'objets vit dans le dépôt principal, hors de ce chemin.
        token = _stat_token(gitdir)
    if token is None:
        return None
    objects = (env_extra or {}).get("GIT_OBJECT_DIRECTORY")
    if objects:
        # `plan_git.local_commit` lit les objets d'un autre magasin que le cwd :
        # l'identité doit suivre le magasin réellement lu.
        path = objects if os.path.isabs(objects) else os.path.join(repo, objects)
        object_dir = os.path.realpath(path)
    elif os.path.isdir(gitdir):
        object_dir = os.path.join(gitdir, "objects")
    else:
        return token
    object_token = _objects_identity(object_dir)
    if object_token is None:
        return None
    return (token, object_token)


def _content_cache_clear(repo: str | None = None) -> None:
    """Vide le cache de contenu (tout, ou les seules entrées d'un dépôt).

    Les tests s'en servent pour compter des lancements ; la production n'a
    pas besoin de l'appeler à la main (les commandes destructrices ci-dessus
    oublient d'elles-mêmes le dépôt concerné, et `init`/`clone` aussi leur
    destination, #3829).
    """
    global _CONTENT_CACHE_BYTES
    with _CONTENT_CACHE_LOCK:
        if repo is None:
            _CONTENT_CACHE.clear()
            _CONTENT_CACHE_BYTES = 0
            return
        root = _repo_cache_key(repo)
        for key in [key for key in _CONTENT_CACHE if key[0] == root]:
            _CONTENT_CACHE.pop(key, None)
        _CONTENT_CACHE_BYTES = sum(
            len(value[1]) + len(value[2]) for value in _CONTENT_CACHE.values())


def _content_cache_stats() -> dict:
    """Compteurs du cache (témoins de #3753) ; sans effet sur la lecture."""
    with _CONTENT_CACHE_LOCK:
        return {"entries": len(_CONTENT_CACHE), "bytes": _CONTENT_CACHE_BYTES,
                "hits": _CONTENT_CACHE_HITS, "misses": _CONTENT_CACHE_MISSES}


def _content_cache_take(key):
    global _CONTENT_CACHE_HITS
    with _CONTENT_CACHE_LOCK:
        value = _CONTENT_CACHE.get(key)
        if value is not None:
            _CONTENT_CACHE_HITS += 1
        return value


def _content_cache_put(key, proc: subprocess.CompletedProcess) -> None:
    """Retient une lecture réussie, dans les bornes de mémoire du cache."""
    global _CONTENT_CACHE_BYTES
    size = len(proc.stdout) + len(proc.stderr)
    if size > CONTENT_CACHE_MAX_BYTES:
        return
    with _CONTENT_CACHE_LOCK:
        old = _CONTENT_CACHE.pop(key, None)
        if old is not None:
            _CONTENT_CACHE_BYTES -= len(old[1]) + len(old[2])
        _CONTENT_CACHE[key] = (proc.returncode, proc.stdout, proc.stderr)
        _CONTENT_CACHE_BYTES += size
        while (_CONTENT_CACHE and
               (len(_CONTENT_CACHE) > CONTENT_CACHE_MAX_ENTRIES
                or _CONTENT_CACHE_BYTES > CONTENT_CACHE_MAX_BYTES)):
            oldest = next(iter(_CONTENT_CACHE))
            _rc, out, err = _CONTENT_CACHE.pop(oldest)
            _CONTENT_CACHE_BYTES -= len(out) + len(err)


def _content_rev(text: str) -> bool:
    """L'argument est-il une révision **déjà résolue** (jamais une ref) ?"""
    text = str(text or "")
    if not text:
        return False
    if text.startswith("^") and len(text) > 1:
        text = text[1:]
    for separator in ("...", ".."):
        if separator in text:
            left, _, right = text.partition(separator)
            if _REV_ATOM_RE.match(left) and _REV_ATOM_RE.match(right):
                return True
    return bool(_REV_ATOM_RE.match(text))


def _content_options(sub: str, tokens: list[str]):
    """([positionnels], [chemins après --]) ou None si une option est inconnue."""
    rules = _CACHE_OPTIONS[sub]
    positional: list[str] = []
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            paths = tokens[index + 1:]
            break
        if not token.startswith("-"):
            positional.append(token)
            index += 1
            continue
        if token in rules["exact"]:
            index += 1
            continue
        if any(token.startswith(prefix) for prefix in rules["prefix"]):
            # `%d`/`%D` (décorations), `%g` (reflog), `%G` (signature), `%N`
            # (notes), `%S` (ref), `%ar`/`%ah` (date relative/humaine),
            # `%aN`/`%aE` (mailmap) et `%(describe)`/`%(decorate)` lisent
            # autre chose que l'objet.
            if (token.startswith(("--format=", "--pretty="))
                    and _FORMAT_STATE_RE.search(token)):
                return None
            if (token.startswith("--date=")
                    and token[len("--date="):] in _CACHE_STATEFUL_DATES):
                return None
            index += 1
            continue
        if token in _CACHE_OPTION_VALUES:
            if index + 1 >= len(tokens):
                return None
            if (token in ("--format", "--pretty")
                    and _FORMAT_STATE_RE.search(tokens[index + 1])):
                return None
            if (token == "--date"
                    and tokens[index + 1] in _CACHE_STATEFUL_DATES):
                return None
            if (sub == "grep" and token == "-f"
                    and tokens[index + 1] != "-"):
                # le fichier de motifs n'est pas dans la clé ; `-` (stdin) l'est
                return None
            index += 2
            continue
        if token.startswith("-n") and token[2:].isdigit():
            index += 1
            continue
        if re.fullmatch(r"-\d+", token):
            index += 1
            continue
        if re.fullmatch(r"-[MC]\d*%?", token):
            index += 1
            continue
        return None
    return positional, paths


def _all_sha_lines(stdin: bytes | None) -> bool:
    """Un stdin de `cat-file --batch` ne contient que des SHA complets."""
    if not stdin:
        return False
    try:
        lines = stdin.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return False
    lines = [line.strip() for line in lines if line.strip()]
    return bool(lines) and all(re.fullmatch(_FULL_SHA, line) for line in lines)


def _batch_has_missing(stdout: bytes) -> bool:
    """`cat-file --batch` a-t-il répondu « missing » pour un objet ?

    Une réponse négative n'est pas un contenu : elle peut changer après un
    fetch, donc elle ne se cache pas. On suit les en-têtes, jamais le contenu
    (qui pourrait contenir le mot « missing »).
    """
    position = 0
    try:
        while position < len(stdout):
            end = stdout.index(b"\n", position)
            header = stdout[position:end].split(b" ")
            position = end + 1
            if len(header) < 3:
                return True
            if header[1] == b"missing":
                return True
            size = int(header[2])
            position += size + 1
    except (ValueError, IndexError):
        return True
    return False


def _content_cache_key(repo: str, args: tuple[str, ...],
                       stdin: bytes | None, env_extra: dict | None):
    """La clé de cache d'une lecture de contenu, ou None si non cachable."""
    if not args:
        return None
    sub = args[0]
    tokens = list(args[1:])
    if sub == "cat-file":
        if len(tokens) == 1 and tokens[0] in ("--batch", "--batch-check"):
            if not _all_sha_lines(stdin):
                return None
        elif len(tokens) == 2 and tokens[0] in ("blob", "-e", "-t", "-s", "-p"):
            if not _content_rev(tokens[1]):
                return None
        else:
            return None
        paths: list[str] = []
    elif sub == "patch-id":
        if tokens not in (["--verbatim"], ["--stable"],
                          ["--verbatim", "--stable"]):
            return None
        if stdin is None:
            return None
        paths = []
    elif sub in _CACHE_OPTIONS:
        parsed = _content_options(sub, tokens)
        if parsed is None:
            return None
        positional, paths = parsed
        if sub == "rev-parse":
            if len(positional) != 1 or paths:
                return None
        elif sub == "ls-tree":
            if len(positional) != 1:
                return None
        elif sub == "diff":
            if not (len(positional) == 2
                    or (len(positional) == 1 and ".." in positional[0])):
                return None
        elif sub in ("merge-base", "merge-tree"):
            if len(positional) != 2 or paths:
                return None
        elif sub == "rev-list":
            if not positional or paths:
                return None
        elif sub == "grep":
            # sans révision, `git grep` lit l'arbre de travail : jamais caché
            if len(positional) != 1:
                return None
        elif sub in ("show", "log"):
            if not positional:
                return None
        else:                       # pragma: no cover - table fermée
            return None
        if not all(_content_rev(value) for value in positional):
            return None
    else:
        return None
    identity = _repository_identity(repo, env_extra)
    if identity is None:
        # dépôt introuvable ou illisible : la commande va échouer, et un échec
        # ne se retient pas ; mieux vaut ne rien cacher que deviner l'instance.
        return None
    return (_repo_cache_key(repo), identity, tuple(args), stdin or b"",
            tuple(sorted((env_extra or {}).items())))


def _content_cache_store(args: tuple[str, ...],
                         proc: subprocess.CompletedProcess) -> bool:
    """Le résultat est-il une lecture définitive qui peut être retenue ?"""
    sub = args[0] if args else ""
    if sub == "cat-file" and any(
            flag in args for flag in ("--batch", "--batch-check")):
        return (proc.returncode == 0
                and not _batch_has_missing(proc.stdout))
    if sub == "merge-base" and "--is-ancestor" in args:
        return proc.returncode in (0, 1)
    if sub == "grep":
        # 1 = aucun motif trouvé : une réponse définitive, pas un échec
        return proc.returncode in (0, 1)
    return proc.returncode == 0


def _git(repo: str, *args: str, stdin: bytes | None = None,
         timeout: float = GIT_TIMEOUT, env_extra: dict | None = None,
         deadline: Deadline | None = None,
         ) -> subprocess.CompletedProcess:
    """git sans crochets, sans fsmonitor, sans objets de remplacement, sans pager.

    Avec ``deadline`` (#2665), la commande ne prend que le temps qui reste et
    une échéance épuisée lève `GitDeadline` : la découverte bornée s'arrête,
    elle ne se lit pas comme un canon illisible.
    """
    global _CONTENT_CACHE_MISSES
    if deadline is not None:
        if deadline.expired():
            raise GitDeadline("git %s: canon deadline exhausted"
                              % (args[0] if args else "?"))
        timeout = deadline.timeout(timeout)
    cmd = ["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
           "-c", "core.fsmonitor=false", "-c", "core.hooksPath=%s" % os.devnull,
           *args]
    key = _content_cache_key(repo, args, stdin, env_extra)
    if key is not None:
        hit = _content_cache_take(key)
        if hit is not None:
            return subprocess.CompletedProcess(cmd, hit[0], hit[1], hit[2])
        with _CONTENT_CACHE_LOCK:
            _CONTENT_CACHE_MISSES += 1
    env = _git_env()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(cmd, cwd=repo, input=stdin, capture_output=True,
                              env=env, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        # Deux cas distincts portent le même FileNotFoundError : l'exécutable
        # `git` absent, et le dossier de travail (`cwd=repo`) absent. Le
        # second ne doit jamais se lire « git not found » (#2349).
        if exc.filename and exc.filename != cmd[0]:
            raise GitError("git %s: repository directory %r does not exist"
                           % (args[0] if args else "?", repo)) from exc
        raise GitError("git not found") from exc
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and deadline.expired():
            raise GitDeadline("git %s: canon deadline exhausted" % args[0]) from exc
        raise GitError("git %s: timed out" % args[0]) from exc
    except OSError as exc:
        # Dossier du dépôt inaccessible (permission refusée, n'est plus un
        # dossier…) : le canon est illisible, jamais une exception qui
        # empêcherait « canon sync » d’enregistrer l’état fermé.
        raise GitError("git %s: %s" % (args[0], exc.strerror or exc)) from exc
    if key is not None and _content_cache_store(args, proc):
        _content_cache_put(key, proc)
    if args and args[0] in _CACHE_DROP_COMMANDS and proc.returncode == 0:
        # `gc`/`prune` peuvent supprimer des objets : une lecture retenue pour
        # ce dépôt ne doit pas survivre à leur disparition. `init`/`clone`
        # couvrent un chemin réutilisé par un autre dépôt ; leur destination
        # est un argument (relative au cwd de la commande), pas le cwd (#3829).
        _content_cache_clear(repo)
        for path in _recreated_repository_paths(args[0], repo, args[1:]):
            _content_cache_clear(path)
    return proc


def _git_out(repo: str, *args: str, timeout: float = GIT_TIMEOUT,
             deadline: Deadline | None = None) -> str | None:
    proc = _git(repo, *args, timeout=timeout, deadline=deadline)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def _git_message(proc: subprocess.CompletedProcess) -> str:
    """Le message d'erreur de git, ou son code de sortie s'il est muet."""
    text = proc.stderr.decode("utf-8", "replace").strip()
    return text or ("code %d" % proc.returncode)


def _diff_name_status(repo: str, base: str, head: str | None = None) -> set[str]:
    """Les chemins de `git diff --name-status --find-renames base [head]`.

    Un renommage compte pour ses **deux** chemins, l'ancien et le nouveau,
    parce que la politique de revue les couvre tous les deux. `head` absent :
    l'arbre de travail. Toute erreur git lève [`GitError`].
    """
    args = ["diff", "--name-status", "-z", "--find-renames", base]
    if head is not None:
        args.append(head)
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise GitError("git diff %s: %s" % (base, _git_message(proc)))
    return _name_status_paths(proc.stdout)


def _name_status_paths(raw: bytes) -> set[str]:
    """Chemins d'une sortie `git diff --name-status -z` (renommages : les deux)."""
    paths: set[str] = set()
    fields = raw.decode("utf-8", "replace").split("\0")
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            break
        if status[0] in ("R", "C"):
            if index + 2 >= len(fields):
                break
            paths.add(fields[index + 1])
            paths.add(fields[index + 2])
            index += 3
        else:
            if index + 1 >= len(fields):
                break
            paths.add(fields[index + 1])
            index += 2
    return paths


def git_changed_paths(repo: str, ref: str = "") -> list[str]:
    """Les chemins d'un changement, relatifs à la racine du dépôt (L10).

    Le changement est lu entre **`merge-base(ref, HEAD)`** et **l'arbre de
    travail** (suivi, index et non indexé), plus les fichiers non suivis : c'est
    ce qu'un gel emporterait (mesh-design). Un renommage compte pour ses **deux**
    chemins, l'ancien et le nouveau, parce que la politique de revue les couvre
    tous les deux. `ref` vide vaut `HEAD` (« tout ce qui n'est pas committé »).

    Toute erreur git — référence inconnue, dépôt illisible — lève [`GitError`] :
    une liste vide ou partielle se lirait comme « rien à relire », donc une
    classe rassurante sur un changement qu'on n'a pas su lire (revue codex2, B2).
    """
    paths: set[str] = set()
    base = "HEAD"
    if ref:
        # `merge-base` échoue aussi quand les historiques n'ont pas d'ancêtre
        # commun : là, le repli légitime est la référence elle-même. Mais une
        # référence *inconnue* n'a pas de diff : c'est une erreur, jamais [].
        verified = git_commit(repo, ref)
        if verified is None:
            raise GitError("unknown git reference: %s" % ref)
        proc = _git(repo, "merge-base", verified, "HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            base = proc.stdout.decode("utf-8", "replace").strip()
        else:
            base = verified
    paths.update(_diff_name_status(repo, base))
    proc = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    if proc.returncode != 0:
        raise GitError("git ls-files: %s" % _git_message(proc))
    paths.update(name for name in proc.stdout.decode("utf-8", "replace").split("\0") if name)
    return sorted(paths)


def git_changed_paths_between(repo: str, base: str, tip: str) -> list[str]:
    """Chemins changés **entre deux commits** (L10, #538), sans l'arbre de travail.

    `git_changed_paths` compare une référence à HEAD et à l'arbre de travail :
    c'est ce qu'un gel emporterait. Ici, l'appelant compare un commit déjà
    appliqué (`canon_state.last_good_commit`) à la référence approuvée qui va
    l'être (`source.commit`) : si la référence avance sans que le checkout
    suive, le changement doit quand même être vu. Un renommage compte pour ses
    deux chemins, comme `git_changed_paths`. `GitError` sur référence inconnue
    ou dépôt illisible : jamais une liste vide rassurante."""
    resolved = {}
    for name in (base, tip):
        sha = git_commit(repo, name)
        if sha is None:
            raise GitError("unknown git reference: %s" % name)
        resolved[name] = sha
    proc = _git(repo, "diff", "--name-status", "-z", "--find-renames",
                resolved[base], resolved[tip])
    if proc.returncode != 0:
        raise GitError("git diff %s %s: %s" % (base, tip, _git_message(proc)))
    return sorted(_name_status_paths(proc.stdout))


def git_range_paths(repo: str, base: str, head: str) -> list[str]:
    """Les chemins changés entre `base` et `head` (renommage : les deux).

    `base` est ramené à son **merge-base avec `head`** : c'est le diff qu'un lot
    gelé emporte depuis sa cible, donc ce que sa classe de revue couvre (canon
    §1). Toute erreur git — référence inconnue, dépôt illisible — lève
    [`GitError`] : jamais une liste vide qui se lirait « rien à relire ».
    """
    verified_base = git_commit(repo, base)
    if verified_base is None:
        raise GitError("unknown git reference: %s" % base)
    verified_head = git_commit(repo, head)
    if verified_head is None:
        raise GitError("unknown git reference: %s" % head)
    proc = _git(repo, "merge-base", verified_base, verified_head)
    if proc.returncode == 0 and proc.stdout.strip():
        start = proc.stdout.decode("utf-8", "replace").strip()
    else:
        start = verified_base
    return sorted(_diff_name_status(repo, start, verified_head))


def git_toplevel(directory: str, *, timeout: float = GIT_TIMEOUT,
                 deadline: Deadline | None = None) -> str | None:
    """Racine du dépôt git qui contient `directory`, ou None."""
    try:
        out = _git_out(directory, "rev-parse", "--show-toplevel",
                       timeout=timeout, deadline=deadline)
    except GitError:
        return None
    return os.path.realpath(out) if out else None


def git_commit(repo: str, rev: str, *, timeout: float = GIT_TIMEOUT,
               deadline: Deadline | None = None) -> str | None:
    if not rev or rev.startswith("-"):
        return None
    return _git_out(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                    rev + "^{commit}", timeout=timeout, deadline=deadline)


_SHA_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")


def git_contains(repo: str, ancestor: str, descendant: str) -> bool:
    """`ancestor` (un SHA) est-il contenu dans l'historique de `descendant` (un
    SHA) ? Faux si l'un des deux est inconnu de ce dépôt, ou en cas d'erreur
    git : le doute ne prouve jamais l'appartenance."""
    if not (_SHA_RE.fullmatch(ancestor or "") and _SHA_RE.fullmatch(descendant or "")):
        return False
    if ancestor == descendant:
        return True
    try:
        proc = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    except GitError:
        return False
    return proc.returncode == 0


def git_fast_forward(repo: str, commit: str) -> str:
    """Avance le checkout de `repo` jusqu'à `commit` (fast-forward seulement).

    Rend '' quand le checkout est déjà à `commit` ou qu'il a pu avancer, sinon
    le diagnostic du refus (jamais d'exception git). Un checkout divergé, ou
    des modifications locales que la fusion écraserait, laissent le dépôt
    intact : git refuse lui-même, rien n'est forcé (L814)."""
    target = git_commit(repo, commit)
    if not target:
        return "unknown commit %s in %s" % ((commit or "")[:12], repo)
    head = git_commit(repo, "HEAD")
    if head == target:
        return ""
    if head and not git_contains(repo, head, target):
        return "checkout %s is not a fast-forward of %s" % (head[:12], target[:12])
    try:
        proc = _git(repo, "merge", "--ff-only", "--quiet", target)
    except GitError as exc:
        return str(exc)
    if proc.returncode != 0:
        return _git_message(proc).splitlines()[0][:200]
    return ""


def _git_list(repo: str, commit: str, prefix: str, *,
              timeout: float = GIT_TIMEOUT,
              deadline: Deadline | None = None) -> list[tuple[str, str]]:
    """[(chemin dans le dépôt, sha du blob)] des fichiers ordinaires sous `prefix`."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if prefix:
        args += ["--", prefix]
    proc = _git(repo, *args, timeout=timeout, deadline=deadline)
    if proc.returncode != 0:
        raise GitError("git ls-tree: %s" % proc.stderr.decode("utf-8", "replace").strip())
    out = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _tab, path = entry.partition(b"\t")
        mode, kind, sha = meta.decode().split(" ")
        # ni lien symbolique (120000), ni sous-module (160000)
        if kind == "blob" and mode in ("100644", "100755"):
            out.append((path.decode("utf-8", "replace"), sha))
    return out


def _git_blobs(repo: str, shas: Iterable[str], *,
               timeout: float = GIT_TIMEOUT,
               deadline: Deadline | None = None) -> dict[str, bytes]:
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    proc = _git(repo, "cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode(),
                timeout=timeout, deadline=deadline)
    if proc.returncode != 0:
        raise GitError("git cat-file: %s" % proc.stderr.decode("utf-8", "replace").strip())
    data = proc.stdout
    out: dict[str, bytes] = {}
    pos = 0
    try:
        for sha in shas:
            end = data.index(b"\n", pos)
            header = data[pos:end].decode().split(" ")
            pos = end + 1
            if len(header) < 3 or header[1] == "missing":
                continue
            size = int(header[2])
            out[sha] = data[pos:pos + size]
            pos += size + 1
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitError("git cat-file: unreadable output (%s)" % exc) from exc
    return out


def _git_read(repo: str, commit: str, path: str, *,
              timeout: float = GIT_TIMEOUT,
              deadline: Deadline | None = None) -> bytes | None:
    proc = _git(repo, "cat-file", "blob", "%s:%s" % (commit, path),
                timeout=timeout, deadline=deadline)
    return proc.stdout if proc.returncode == 0 else None


def git_read(repo: str, commit: str, path: str, *,
             timeout: float = GIT_TIMEOUT,
             deadline: Deadline | None = None) -> bytes | None:
    """Contenu de `path` au commit `commit` (objets git), ou None."""
    if not _SHA_RE.fullmatch(commit or ""):
        return None
    try:
        return _git_read(repo, commit, path, timeout=timeout, deadline=deadline)
    except GitError:
        return None


def git_remotes(repo: str, *, deadline: Deadline | None = None) -> list[str]:
    """Les dépôts distants ; avec ``deadline`` (#3572), une échéance épuisée
    lève `GitDeadline` au lieu de se lire « aucun dépôt distant »."""
    try:
        return (_git_out(repo, "remote", deadline=deadline) or "").split()
    except GitError:
        return []


_BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")


def remote_branch(repo: str, name: str, *,
                  deadline: Deadline | None = None) -> str | None:
    """La branche de suivi distant que `name` désigne (`origin/main`), ou None.

    `main` → `origin/main` ; `origin/main` (un dépôt distant connu) et
    `refs/remotes/origin/main` → `origin/main` ; `refs/heads/main` →
    `origin/main`. Un SHA, une expression de révision (`~`, `^`, `@{`, `..`)
    ou un nom vide ne sont pas une branche : None. La résolution se fait
    ensuite par `refs/remotes/<branche>`, jamais par un nom court qu'une
    branche locale ou une étiquette homonyme pourrait masquer.
    """
    text = (name or "").strip()
    if not text or _SHA_RE.fullmatch(text):
        return None
    if text.startswith("refs/remotes/"):
        short = text[len("refs/remotes/"):]
    elif text.startswith("refs/heads/"):
        short = "%s/%s" % (DEFAULT_REMOTE, text[len("refs/heads/"):])
    elif "/" in text and text.split("/", 1)[0] in git_remotes(repo, deadline=deadline):
        short = text
    else:
        short = "%s/%s" % (DEFAULT_REMOTE, text)
    if (not _BRANCH_RE.fullmatch(short) or ".." in short or "//" in short
            or short.endswith(("/", ".lock", ".")) or "/" not in short):
        return None
    return short


def _remote_of(repo: str, rev: str, *, timeout: float = GIT_TIMEOUT,
               deadline: Deadline | None = None) -> str:
    remotes = (_git_out(repo, "remote", timeout=timeout, deadline=deadline) or "").split()
    head = rev.split("/", 1)[0]
    return head if head in remotes else DEFAULT_REMOTE


def _norm_url(url: str) -> str:
    """URL de dépôt comparable : sans schéma, utilisateur, `.git` ni `/` final."""
    text = url.strip()
    scp = re.match(r"^[\w.-]+@([^:/]+):(.*)$", text)
    if scp:
        text = "%s/%s" % (scp.group(1), scp.group(2))
    else:
        text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)
        text = re.sub(r"^[^@/]+@", "", text)
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.lower()


def git_fetch(repo: str, remote: str, *, timeout: float = FETCH_TIMEOUT,
              deadline: Deadline | None = None) -> str:
    """`git fetch` sans crochets ; renvoie '' ou le message d'erreur."""
    try:
        proc = _git(repo, "fetch", "--quiet", "--no-tags", "--", remote,
                    timeout=timeout, deadline=deadline)
    except GitError as exc:
        return str(exc)
    if proc.returncode != 0:
        return proc.stderr.decode("utf-8", "replace").strip() or "code %d" % proc.returncode
    return ""


# ==========================================================================
# Lecture
# ==========================================================================

def _within(base: str, target: str) -> bool:
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base.rstrip(os.sep) + os.sep)


def _relative_ok(path: str) -> bool:
    if not isinstance(path, str) or not path or os.path.isabs(path):
        return False
    norm = os.path.normpath(path)
    return norm != ".." and not norm.startswith(".." + os.sep)


def parse_scope(value: Any) -> tuple[list[str] | None, str | None]:
    """(dossiers, problème) d'un `ameesh.scope` (L43, 0031).

    Une chaîne ou une liste non vide de dossiers RELATIFS au bundle du membre
    (ni absolus, ni `..`, ni motifs, ni dossiers cachés) ; `.` désigne le
    bundle entier (rendu ""). None sans valeur (pas de périmètre)."""
    if value is None:
        return None, None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        return None, "directory or non-empty list of directories expected"
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            return None, "empty directory"
        text = item.strip().replace("\\", "/")
        norm = posixpath.normpath(text)
        if text.startswith("/") or norm == ".." or norm.startswith("../"):
            return None, "%r: directory relative to the bundle expected (neither absolute nor `..`)" % item
        if any(ch in norm for ch in "*?[]{}"):
            return None, "%r: pattern not allowed (a directory, not a pattern)" % item
        if norm != "." and any(part.startswith(".") for part in norm.split("/")):
            return None, "%r: hidden directory (never read)" % item
        norm = "" if norm == "." else norm
        if norm not in out:
            out.append(norm)
    return out, None


def process_path_ok(relpath: str, scope: Iterable[str] | None = None,
                    directory: str = PROCESS_DIR) -> bool:
    """#538, #2973 : le chemin (relatif au bundle) d'une fiche Process est-il
    celui d'une règle ? `processus/…` à la racine du bundle, ou
    `<dossier>/processus/…` quand `<dossier>` est un dossier déclaré du
    périmètre ameesh du membre (la fédération roote le membre à `.` et lit le
    périmètre comme un filtre) ; jamais un autre dossier. L103 (#4395) : même
    contrôle pour une carte Pipeline avec `directory=PIPELINE_DIR`."""
    chemin = relpath.replace("\\", "/").split("/")
    if len(chemin) >= 2 and chemin[0] == directory:
        return True
    for dossier in scope or ():
        if not dossier:
            continue                # `.` : la racine, déjà jugée
        parts = dossier.split("/")
        reste = chemin[len(parts):]
        if chemin[:len(parts)] == parts and len(reste) >= 2 and reste[0] == directory:
            return True
    return False


def in_scope(relpath: str, scope: Iterable[str] | None) -> bool:
    """Le chemin (relatif au bundle) est-il dans le périmètre ? None = tout."""
    if scope is None:
        return True
    rel = relpath.replace(os.sep, "/")
    return any(s == "" or rel == s or rel.startswith(s + "/") for s in scope)


class NamingError(ValueError):
    """Nom par défaut impossible : configuration absente, rôle ou projet sans
    segment, ou forme demandée incohérente (L243, #468)."""


@dataclass(frozen=True)
class Naming:
    """L243 (#468) : segments du nom par défaut `<org>-<repo>-<role>-<seq>`.

    `org` : segment de l'organisation ; `projects` : nom de projet/dépôt →
    segment (un projet absent est réduit à ses 6 premiers caractères) ;
    `roles` : rôle → segment (un rôle absent est REFUSÉ : un nom ne se
    devine pas). Les segments sont en minuscules `[a-z0-9]{2,6}`."""

    org: str
    projects: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"org": self.org, "projects": dict(self.projects),
                "roles": dict(self.roles)}

    def segment(self, project: str) -> str | None:
        """Segment que CE nommage donne à `project`, sans refus.

        Le nom court déclaré, sinon la troncature à 6 caractères si c'est un
        segment, sinon None. Sert à décrire les dépôts d'un AUTRE canon avec
        SES segments (codex r2, #452) : comparer les noms de dépôts ferait
        rater `otherrepo: catalo` face à `catalogue`, ou refuser à tort
        `catalogapi: capi`."""
        declared = self.projects.get(project)
        if declared:
            return declared
        short = str(project)[:6]
        return short if NAMING_SEGMENT_RE.match(short) else None

    def project_segment(self, project: str,
                        known: Mapping[str, str] | None = None) -> str:
        """Segment d'un projet : le nom court déclaré, sinon le nom tronqué à 6.

        Un segment déjà porté par un AUTRE dépôt connu est refusé (deux dépôts
        ne partagent jamais un segment) — que ce segment soit DÉCLARÉ ou
        tronqué (codex r3, #452) : `catalogue: catalo` ici et `otherrepo: catalo`
        ailleurs ne peuvent pas coexister. `known` : les dépôts connus du mesh
        (fiches, demande, `naming.projects` des autres canons) et le segment que
        LEUR nommage leur donne — un mapping dépôt → segment. On compare les
        segments engendrés, jamais les noms de dépôts (codex r2, #452) : le
        canon B peut déclarer `otherrepo: catalo` (même segment que
        `catalogue`, refusé) ou `catalogapi: capi` (aucun rapport)."""
        declared = self.projects.get(project)
        segment = declared or str(project)[:6]
        if not NAMING_SEGMENT_RE.match(segment):
            raise NamingError(
                "project %r has no usable short name (%r): declare "
                "naming.projects.%s in the federation" % (project, segment, project))
        existing = (known or {}).get(project)
        if existing is not None and existing != segment:
            raise NamingError(
                "project %r has several short names in the mesh (%r here, %r "
                "elsewhere): one repo keeps the same segment in every canon"
                % (project, segment, existing))
        others = dict(known or {})
        for key, value in self.projects.items():
            others.setdefault(str(key), value)
        others.pop(project, None)
        clash = sorted(key for key, value in others.items() if value == segment)
        if clash:
            if declared:
                raise NamingError(
                    "project %r: the short name %r is already the segment of %s: "
                    "two repos never share a segment, declare another "
                    "naming.projects.%s"
                    % (project, segment, ", ".join(clash), project))
            raise NamingError(
                "project %r: the 6-character name %r is already the segment of %s: "
                "declare naming.projects.%s in the federation"
                % (project, segment, ", ".join(clash), project))
        return segment

    def role_segment(self, role: str) -> str:
        declared = self.roles.get(role)
        if not declared:
            known = ", ".join(sorted(self.roles)) or "none"
            raise NamingError(
                "role %r has no short name in naming.roles (known: %s)" % (role, known))
        return declared

    def prefix(self, role: str, projects: Iterable[str],
               known: Mapping[str, str] | None = None) -> str:
        """Préfixe du nom : `<org>-<repo>-<role>-` (un projet) ou
        `<org>-<role>-` (plusieurs : le segment de dépôt disparaît)."""
        projects = list(projects)
        if not projects:
            raise NamingError("at least one project is required to name an agent")
        if len(set(projects)) != len(projects):
            raise NamingError("duplicate project in the name request: %s"
                              % ", ".join(projects))
        parts = [self.org]
        if len(projects) == 1:
            parts.append(self.project_segment(projects[0], known))
        parts.append(self.role_segment(role))
        return "-".join(parts) + "-"

    def next_name(self, role: str, projects: Iterable[str], taken: Iterable[str],
                  known: Mapping[str, str] | None = None) -> str:
        """Premier numéro libre pour ce préfixe, sur deux chiffres (`01`).

        `taken` : tous les noms déjà pris (fiches, registre — agents arrêtés
        compris — et alias de #423) : un numéro déjà attribué n'est jamais
        réutilisé tant que le nom reste réservé. `known` : les dépôts connus
        hors `naming.projects` et le segment que leur nommage leur donne
        (mapping dépôt → segment), pour refuser une troncature ambiguë."""
        prefix = self.prefix(role, projects, known)
        used = set()
        for name in taken:
            if not isinstance(name, str) or not name.startswith(prefix):
                continue
            rest = name[len(prefix):]
            if NAMING_SEQ_RE.match(rest):
                used.add(int(rest))
        number = 1
        while number in used:
            number += 1
        return prefix + ("%02d" % number if number < 100 else str(number))

    def conforms(self, title: str) -> bool:
        """Le titre suit-il la convention (forme et segments connus) ?"""
        parts = str(title).split("-")
        if len(parts) not in (3, 4) or parts[0] != self.org:
            return False
        if not NAMING_SEQ_RE.match(parts[-1]):
            return False
        if parts[-2] not in set(self.roles.values()):
            return False
        return all(NAMING_SEGMENT_RE.match(part) for part in parts[1:-1])

    def describe(self) -> str:
        return "<org>-<repo>-<role>-<seq> (org: %s)" % self.org

    def example(self) -> str:
        return "%s-<repo>-<role>-01" % self.org


def parse_naming(value: Any) -> tuple[Naming | None, str | None]:
    """(config, problème) d'un `extensions.ameesh.naming` (L243, #468).

    `org` est obligatoire ; `projects` et `roles` sont des mappings nom →
    segment, facultatifs. Un segment hors grammaire est refusé à la lecture."""
    if not isinstance(value, dict):
        return None, "mapping expected (`naming: {org: …, projects: {…}, roles: {…}}`)"
    unknown = sorted(set(map(str, value)) - set(NAMING_FIELDS))
    if unknown:
        return None, "unknown key(s): %s (expected %s)" % (
            ", ".join(unknown), ", ".join(NAMING_FIELDS))
    org = value.get("org")
    if not isinstance(org, str) or not NAMING_SEGMENT_RE.match(org):
        return None, "`org` missing or not a segment (2-6 characters, [a-z0-9]): %r" % (org,)
    out: dict[str, dict] = {"projects": {}, "roles": {}}
    for name in ("projects", "roles"):
        raw = value.get(name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            return None, "`%s`: mapping expected (name: segment)" % name
        for key, segment in raw.items():
            if not isinstance(key, str) or not key.strip():
                return None, "`%s`: empty name" % name
            if not isinstance(segment, str) or not NAMING_SEGMENT_RE.match(segment):
                return None, "`%s.%s`: %r is not a segment (2-6 characters, [a-z0-9])" % (
                    name, key, segment)
            out[name][key] = segment
        if name == "projects":
            # deux dépôts ne partagent jamais un segment : des noms identiques
            # à un numéro près ne diraient plus sur quel dépôt travaille l'agent.
            seen: dict[str, str] = {}
            for key, segment in out[name].items():
                if segment in seen:
                    return None, ("`projects`: %s and %s share the segment %r: "
                                  "declare a distinct short name for one of them"
                                  % (seen[segment], key, segment))
                seen[segment] = key
    return Naming(org=org, projects=out["projects"], roles=out["roles"]), None


def _segment_of(naming: "Naming | None", project: str) -> str | None:
    """Segment d'un dépôt sous un nommage (sans nommage : la troncature à 6)."""
    if naming is not None:
        return naming.segment(project)
    short = str(project)[:6]
    return short if NAMING_SEGMENT_RE.match(short) else None


def known_projects(canons: Iterable, requested: Iterable[str] = (),
                   naming: "Naming | None" = None) -> dict:
    """Dépôts connus du mesh et le segment qu'ils portent (L243, #468).

    Un mapping dépôt → segment, chacun calculé par le nommage qui le
    GOUVERNE : `naming` (celui qui engendre le nom) pour la demande, le
    nommage de chaque canon pour ses `naming.projects` et les `projects:` de
    ses fiches (`team` sinon). `Naming.project_segment` compare ces segments —
    jamais les noms de dépôts, qui peuvent se ressembler sans collision
    (`catalogue` / `catalogapi`) ou se distinguer avec le même segment
    (`catalogue` / `otherrepo: catalo`) : codex r2, #452.

    TOUS les segments d'un dépôt sont collectés : si deux canons lui donnent
    deux noms courts (`otherrepo: other` chez B, `otherrepo: catalo` chez C),
    garder le premier ferait dépendre le verdict de l'ordre des canons et
    masquerait le conflit de C — on refuse, en parcourant les dépôts et les
    segments TRIÉS pour que le verdict ne dépende pas de l'ordre (codex r3,
    #452)."""
    #: dépôt → {segment : {sources}} : tous les segments vus, pour refuser une
    #: divergence au lieu de garder le premier (`setdefault`).
    collected: dict[str, dict[str, set]] = {}

    def note(project, segment, source: str) -> None:
        if not isinstance(project, str) or not segment:
            return
        collected.setdefault(project, {}).setdefault(segment, set()).add(source)

    for project in requested:
        note(project, _segment_of(naming, project), "the request")
    for canon in canons:
        canon_naming = getattr(canon, "naming", None)
        label = "canon %s" % (canon.id or canon.root)
        if canon_naming is not None:
            for key, segment in canon_naming.projects.items():
                note(key, segment, label)
        for agent in getattr(canon, "agents", ()) or ():
            for project in agent.effective_projects:
                note(project, _segment_of(canon_naming, project),
                     "%s (card %s)" % (label, agent.title))
    known: dict[str, str] = {}
    for project in sorted(collected):
        segments = collected[project]
        if len(segments) > 1:
            raise NamingError(
                "project %r has several short names in the mesh: %s — one repo keeps "
                "the same segment in every canon (otherwise a collision depends on "
                "the canon order): align `naming.projects.%s`"
                % (project,
                   ", ".join("%r (from %s)" % (segment,
                                               ", ".join(sorted(segments[segment])))
                             for segment in sorted(segments)),
                   project))
        known[project] = next(iter(segments))
    return known


def _profile_type(content: bytes | None) -> str | None:
    """Type du profil ameesh déclaré dans le frontmatter, sans le valider
    (compte des fiches ignorées hors périmètre, L43) ; None sinon."""
    if not content:
        return None
    try:
        block = split_frontmatter(content[:FRONTMATTER_MAX].decode("utf-8", "replace"))
    except YamlError:
        return None
    found = _TYPED_RE.search(block or "")
    return found.group(1) if found else None


def _interesting(relpath: str) -> bool:
    parts = relpath.replace(os.sep, "/").split("/")
    if any(part.startswith(".") for part in parts):
        return False
    name = parts[-1]
    return name.endswith(".md") and name not in SKIPPED_FILES


class _Loader:
    def __init__(self, root: str, *, ref: str, untrusted: bool, fetch: bool,
                 use_pyyaml: bool | None, default: bool = True,
                 deadline: Deadline | None = None):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.ref = ref
        self.allow_untrusted = untrusted
        self.fetch = fetch
        self.use_pyyaml = use_pyyaml
        #: #2665 : échéance d'une découverte bornée (prévalidation du train) ;
        #: chaque lecture git de ce chargeur ne prend que le temps qui reste.
        self.deadline = deadline
        self.canon = Canon(root=self.root, is_default=default, rev=ref)
        _identify(self.canon, None)     # repli tant que le manifeste n'est pas lu
        self.fetched: set[str] = set()
        self.findings = self.canon.load_findings

    def add(self, code: str, severity: str, message: str, **where: str) -> None:
        self.findings.append(Finding(code, severity, message, **where))

    # -- sources ---------------------------------------------------------------
    def _git_source(self, member: str, directory: str, repo: str, rev: str,
                    explicit: bool) -> Source | None:
        if self.fetch and repo not in self.fetched:
            self.fetched.add(repo)
            error = git_fetch(repo, _remote_of(repo, rev, deadline=self.deadline),
                              deadline=self.deadline)
            if error:
                self.add("canon-fetch-failed", WARNING,
                         "git fetch failed (%s): reading the last known revision"
                         % error.splitlines()[0][:200], member=member)
        commit = git_commit(repo, rev, deadline=self.deadline)
        if not commit:
            self.add("canon-ref-missing", ERROR,
                     "canonical revision %s not found in %s (git fetch? %s)"
                     % (rev, repo, "AMEESH_CANON_REF" if explicit
                        else "or AMEESH_CANON_REF for another branch"),
                     member=member)
            return None
        prefix = os.path.relpath(os.path.realpath(directory), repo)
        prefix = "" if prefix == "." else prefix.replace(os.sep, "/")
        source = Source(member=member, directory=directory, mode="git", repo=repo,
                        prefix=prefix, rev=rev, commit=commit)
        self._local_state(source)
        return source

    def _local_state(self, source: Source) -> None:
        """Signale (sans jamais en tenir compte) l'écart entre le clone et le canon."""
        head = git_commit(source.repo, "HEAD", deadline=self.deadline)
        if head and head != source.commit:
            self.add("canon-local-divergence", WARNING,
                     "the local clone (HEAD %s) differs from %s (%s): only %s is read"
                     % (head[:12], source.rev, source.commit[:12], source.commit[:12]),
                     member=source.member, path=source.prefix)
        args = ["status", "--porcelain", "--untracked-files=normal", "--", source.prefix or "."]
        proc = _git(source.repo, *args, deadline=self.deadline)
        if proc.returncode == 0 and proc.stdout.strip():
            self.add("canon-worktree-dirty", WARNING,
                     "uncommitted local changes under %s: ignored (only %s is read)"
                     % (source.prefix or ".", source.commit[:12]),
                     member=source.member, path=source.prefix)

    def _fs_source(self, member: str, directory: str) -> Source | None:
        if not self.allow_untrusted:
            self.add("canon-untrusted-refused", ERROR,
                     "%s is not a git repository: canon not approved, refused "
                     "(AMEESH_CANON_UNTRUSTED=1 for tests and prototypes)" % directory,
                     member=member)
            return None
        self.add("canon-untrusted", WARNING,
                 "%s read from working files: canon NOT APPROVED "
                 "(AMEESH_CANON_UNTRUSTED=1)" % directory, member=member)
        return Source(member=member, directory=directory, mode="untrusted")

    def _source(self, member: str, directory: str, rev: str, explicit: bool,
                same_repo_commit: tuple[str, str, str] | None = None) -> Source | None:
        try:
            repo = git_toplevel(directory, deadline=self.deadline)
        except GitError:
            repo = None
        if repo is None:
            return self._fs_source(member, directory)
        if same_repo_commit and same_repo_commit[0] == repo:
            # même dépôt que la racine : même instantané
            prefix = os.path.relpath(os.path.realpath(directory), repo)
            return Source(member=member, directory=directory, mode="git", repo=repo,
                          prefix="" if prefix == "." else prefix.replace(os.sep, "/"),
                          rev=same_repo_commit[1], commit=same_repo_commit[2])
        try:
            return self._git_source(member, directory, repo, rev, explicit)
        except GitError as exc:
            self.add("canon-git-error", ERROR, str(exc), member=member)
            return None

    def _check_repository(self, source: Source, entry: dict | None) -> None:
        """Le dépôt lu est-il celui que le manifeste déclare ? (diagnostic)"""
        declared = str((entry or {}).get("repository") or "")
        if source.mode != "git" or not declared:
            return
        remote = source.rev.split("/", 1)[0] if "/" in source.rev else DEFAULT_REMOTE
        actual = _git_out(source.repo, "remote", "get-url", "--", remote,
                          deadline=self.deadline) or ""
        if _norm_url(actual) != _norm_url(declared):
            self.add("member-repository-mismatch", WARNING,
                     "remote %s of %s (%s) is not the one in the manifest (%s)"
                     % (remote, source.member, actual or "missing", declared),
                     member=source.member)

    def _read_manifest(self, source: Source) -> bytes | None:
        if source.mode == "git":
            path = (source.prefix + "/" if source.prefix else "") + "federation.yaml"
            return _git_read(source.repo, source.commit, path,
                             deadline=self.deadline)
        path = os.path.join(source.directory, "federation.yaml")
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as fh:
                return fh.read(FRONTMATTER_MAX * 4)
        return None

    def _parse_manifest(self, raw: bytes, member: str) -> dict | None:
        try:
            manifest = load_yaml(raw.decode("utf-8"), use_pyyaml=self.use_pyyaml)
        except (UnicodeDecodeError, YamlError) as exc:
            self.add("federation-invalid", ERROR, "unreadable federation.yaml: %s" % exc,
                     member=member, path="federation.yaml")
            return None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("members", []), list):
            self.add("federation-invalid", ERROR,
                     "federation.yaml: mapping with a `members` list expected",
                     member=member, path="federation.yaml")
            return None
        return manifest

    # -- chargement --------------------------------------------------------------
    def _bootstrap_rev(self) -> str:
        """Révision où lire le manifeste quand AMEESH_CANON_REF n'est pas posé."""
        repo = git_toplevel(self.root, deadline=self.deadline)
        default = "%s/%s" % (DEFAULT_REMOTE, DEFAULT_BRANCH)
        if repo is None:
            return default
        if self.fetch and repo not in self.fetched:
            self.fetched.add(repo)
            error = git_fetch(repo, DEFAULT_REMOTE, deadline=self.deadline)
            if error:
                self.add("canon-fetch-failed", WARNING,
                         "git fetch failed (%s): reading the last known revision"
                         % error.splitlines()[0][:200], member="canon")
        for rev in (default, "%s/HEAD" % DEFAULT_REMOTE):
            if git_commit(repo, rev, deadline=self.deadline):
                return rev
        return default

    def load(self) -> Canon:
        if not os.path.isdir(self.root):
            self.add("canon-missing", ERROR, "canon root not found: %s" % self.root)
            return self.canon
        explicit = bool(self.ref)
        rev = self.ref or self._bootstrap_rev()
        self.canon.rev = rev
        root = self._source("canon", self.root, rev, explicit)
        if root is None:
            return self.canon
        manifest = None
        raw = self._read_manifest(root)
        if raw is not None:
            manifest = self._parse_manifest(raw, root.member)
        if manifest is not None:
            root_id = str(manifest.get("root") or "canon")
            root.member = root_id
            root_entry = next((m for m in manifest["members"]
                               if isinstance(m, dict) and str(m.get("id")) == root_id), None)
            branch = str((root_entry or {}).get("ref") or "")
            if (root.mode == "git" and not explicit and branch
                    and "%s/%s" % (DEFAULT_REMOTE, branch) != root.rev):
                # la branche canonique est celle du manifeste
                rev = "%s/%s" % (DEFAULT_REMOTE, branch)
                self.canon.rev = rev
                self.findings[:] = [f for f in self.findings
                                    if f.code not in ("canon-local-divergence",
                                                      "canon-worktree-dirty")]
                root = self._source(root_id, self.root, rev, False)
                if root is None:
                    return self.canon
                raw = self._read_manifest(root)
                manifest = self._parse_manifest(raw, root_id) if raw is not None else None
            self._check_repository(root, root_entry)
        self.canon.federation = manifest
        _identify(self.canon, manifest)   # L42 : avant les fiches (préfixe des références)
        self.canon.sources.append(root)
        if manifest is not None:
            self._members(manifest, root)
            self._scopes(manifest, root)
            self._naming(manifest, root)
        self.canon.readable = True
        self._read_fiches()
        if self.canon.untrusted:
            self.findings[:] = [dataclasses.replace(f, untrusted=True) for f in self.findings]
        return self.canon

    def _workspace(self, manifest: dict, root: Source) -> str:
        """Dossier de travail commun : tel que workspace/<workspace_path>/<bundle> = racine."""
        root_id = root.member
        entry = next((m for m in manifest["members"]
                      if isinstance(m, dict) and str(m.get("id")) == root_id), {}) or {}
        rel = os.path.normpath(os.path.join(str(entry.get("workspace_path") or "."),
                                            str(entry.get("bundle") or ".")))
        base = os.path.realpath(self.root)
        if rel == ".":
            return base
        parts = rel.split(os.sep)
        if _relative_ok(rel) and base.split(os.sep)[-len(parts):] == parts:
            return os.sep.join(base.split(os.sep)[:-len(parts)]) or os.sep
        return os.path.dirname(base)

    def _members(self, manifest: dict, root: Source) -> None:
        workspace = self._workspace(manifest, root)
        seen = {root.member}
        anchor = (root.repo, root.rev, root.commit) if root.mode == "git" else None
        for entry in manifest["members"]:
            if not isinstance(entry, dict) or not entry.get("id"):
                self.add("federation-invalid", ERROR, "member without `id` in federation.yaml",
                         member=root.member, path="federation.yaml")
                continue
            member = str(entry["id"])
            if member in seen:
                if member != root.member:
                    self.add("federation-invalid", ERROR, "duplicate member %s" % member,
                             member=root.member, path="federation.yaml")
                continue
            seen.add(member)
            hint = entry.get("workspace_path")
            if not hint:
                self.add("member-absent", WARNING,
                         "member %s without workspace_path: not read" % member, member=member)
                continue
            bundle = str(entry.get("bundle") or ".")
            checkout = os.path.join(workspace, str(hint))
            directory = os.path.normpath(os.path.join(checkout, bundle))
            if (not _relative_ok(str(hint)) or not _relative_ok(bundle)
                    or not _within(workspace, checkout) or not _within(checkout, directory)):
                self.add("member-path-escape", ERROR,
                         "member %s: path outside the workspace directory (%s/%s)"
                         % (member, hint, bundle), member=member)
                continue
            if not os.path.isdir(directory):
                self.add("member-absent", WARNING,
                         "member %s missing locally (%s): not read" % (member, directory),
                         member=member)
                continue
            if os.path.realpath(directory) == os.path.realpath(self.root):
                continue
            branch = str(entry.get("ref") or DEFAULT_BRANCH)
            source = self._source(member, directory, "%s/%s" % (DEFAULT_REMOTE, branch),
                                  False, same_repo_commit=anchor)
            if source is not None:
                if source.repo != root.repo:
                    self._check_repository(source, entry)
                self.canon.sources.append(source)

    # -- périmètre ameesh (L43, 0031) ----------------------------------------------
    def _scopes(self, manifest: dict, root: Source) -> None:
        """Lit `ameesh: {scope: …}` : au premier niveau de federation.yaml pour
        le bundle racine, dans l'entrée `members[]` d'un autre membre pour le
        sien. Sans la clé, le membre est lu en entier (comportement historique).

        Fail closed : un périmètre illisible est une erreur, et le membre n'est
        pas lu du tout (`scope_unread`) — jamais lu en entier à la place."""
        declared: list[tuple[str, Any, str]] = []
        # L47 : forme conforme au schéma OKF Federation (seule la clé
        # `extensions` est libre) : `extensions: {ameesh: {scope: …,
        # members: {<id>: {scope: …}}}}`. Elle prime sur l'ancienne forme.
        ext = (manifest.get("extensions") or {}) if isinstance(manifest, dict) else {}
        ext = ext.get(SCOPE_KEY) if isinstance(ext, dict) else None
        if ext is not None:
            label = "extensions.%s" % SCOPE_KEY
            if not isinstance(ext, dict):
                self.add("ameesh-scope-invalid", ERROR,
                         "`%s`: mapping expected (`%s: {scope: <dir>}`) — ameesh cards "
                         "of %s not read" % (label, label, root.member),
                         member=root.member, path="federation.yaml")
                self.canon.scope_unread.add(root.member)
                ext = {}
            root_part = {k: v for k, v in ext.items() if k != "members"}
            if root_part:
                declared.append((root.member, root_part, label))
            members = ext.get("members")
            if members is not None and not isinstance(members, dict):
                self.add("ameesh-scope-invalid", ERROR,
                         "`%s.members`: mapping expected (`<id>: {scope: …}`)" % label,
                         member=root.member, path="federation.yaml")
                members = {}
            known = {str(e.get("id")) for e in manifest["members"] if isinstance(e, dict)}
            for member, raw in sorted((members or {}).items()):
                member = str(member)
                if member == root.member:
                    self.add("ameesh-scope-ignored-key", WARNING,
                             "`%s.members.%s` ignored: the root bundle's scope is "
                             "declared in `%s.scope`" % (label, member, label),
                             member=root.member, path="federation.yaml")
                    continue
                if member not in known:
                    self.add("ameesh-key-unknown", WARNING,
                             "`%s.members.%s`: member missing from `members` (ignored)"
                             % (label, member), member=root.member, path="federation.yaml")
                    continue
                declared.append((member, raw, "%s.members.%s" % (label, member)))
        if SCOPE_KEY in manifest:
            self.add("ameesh-scope-legacy", WARNING,
                     "top-level `%s:` in federation.yaml does not conform to the "
                     "OKF Federation schema: write `extensions: {%s: {scope: …}}`"
                     % (SCOPE_KEY, SCOPE_KEY), member=root.member, path="federation.yaml")
            if ext is None:
                declared.append((root.member, manifest.get(SCOPE_KEY), SCOPE_KEY))
        for entry in manifest["members"]:
            if not isinstance(entry, dict) or not entry.get("id") or SCOPE_KEY not in entry:
                continue
            member = str(entry["id"])
            if member == root.member:
                self.add("ameesh-scope-ignored-key", WARNING,
                         "`members[%s].%s` ignored: the root bundle's scope is declared "
                         "in `extensions.%s.scope`" % (member, SCOPE_KEY, SCOPE_KEY),
                         member=root.member, path="federation.yaml")
                continue
            self.add("ameesh-scope-legacy", WARNING,
                     "`members[%s].%s` does not conform to the OKF Federation schema: write "
                     "`extensions.%s.members.%s.scope`" % (member, SCOPE_KEY, SCOPE_KEY, member),
                     member=root.member, path="federation.yaml")
            if ext is None or member not in ((ext or {}).get("members") or {}):
                declared.append((member, entry.get(SCOPE_KEY), "members[%s].%s" % (member,
                                                                                  SCOPE_KEY)))
        for member, raw, label in declared:
            where = {"member": root.member, "path": "federation.yaml"}
            if raw is None:
                continue
            if not isinstance(raw, dict):
                self.add("ameesh-scope-invalid", ERROR,
                         "`%s`: mapping expected (`%s: {scope: <dir>}`) — ameesh cards "
                         "of %s not read" % (label, SCOPE_KEY, member), **where)
                self.canon.scope_unread.add(member)
                continue
            allowed = set(SCOPE_FIELDS)
            if member == root.member:
                allowed.add(NAMING_KEY)     # L243 (#468) : le nommage est à la racine
            for unknown in sorted(set(map(str, raw)) - allowed):
                self.add("ameesh-key-unknown", WARNING,
                         "`%s.%s`: unknown key (ignored)" % (label, unknown), **where)
            scope, problem = parse_scope(raw.get("scope"))
            if problem:
                self.add("ameesh-scope-invalid", ERROR,
                         "`%s.scope`: %s — ameesh cards of %s not read"
                         % (label, problem, member), **where)
                self.canon.scope_unread.add(member)
                continue
            if scope is not None:
                self.canon.scopes[member] = scope

    def _naming(self, manifest: dict, root: Source) -> None:
        """L243 (#468) : `extensions.ameesh.naming` au premier niveau de la
        fédération — le canon, c'est l'organisation. Illisible : erreur, aucun
        nom par défaut n'est produit (un nom ne se devine pas)."""
        ext = (manifest.get("extensions") or {}) if isinstance(manifest, dict) else {}
        ext = ext.get(SCOPE_KEY) if isinstance(ext, dict) else None
        if not isinstance(ext, dict):
            return
        raw = ext.get(NAMING_KEY)
        if NAMING_KEY not in ext:           # absent : aucune convention
            return
        naming, problem = parse_naming(raw)  # `naming:` explicite (null) : invalide
        if problem:
            self.add("naming-invalid", ERROR,
                     "`extensions.%s.%s`: %s — no default agent name is generated"
                     % (SCOPE_KEY, NAMING_KEY, problem),
                     member=root.member, path="federation.yaml")
            return
        self.canon.naming = naming

    def _scope_missing(self, source: Source, present: Iterable[str]) -> bool:
        """Un dossier du périmètre absent de la source : erreur, membre non lu."""
        scope = self.canon.scopes.get(source.member)
        if scope is None:
            return False
        present = list(present)
        missing = [s for s in scope if s and not any(
            p == s or p.startswith(s + "/") for p in present)]
        if not missing:
            return False
        self.add("ameesh-scope-missing", ERROR,
                 "ameesh scope %s missing from %s: ameesh cards of %s not read"
                 % (", ".join(missing), source.describe(), source.member),
                 member=source.member, path=source.prefix)
        self.canon.scope_unread.add(source.member)
        return True

    # -- fiches ------------------------------------------------------------------
    def _files(self, source: Source) -> list[tuple[str, str, bytes | None, str]]:
        """[(chemin affiché, chemin dans le bundle, contenu, empreinte)] des .md
        de la source ; [] si son périmètre ameesh est introuvable (L43)."""
        others = [s for s in self.canon.sources if s is not source]
        if source.mode == "git":
            nested = [s.prefix for s in others if s.mode == "git" and s.repo == source.repo
                      and s.prefix != source.prefix
                      and (not source.prefix or s.prefix.startswith(source.prefix + "/"))]
            entries = []
            listed = _git_list(source.repo, source.commit, source.prefix,
                               deadline=self.deadline)
            if not listed:
                self.add("canon-bundle-missing", ERROR,
                         "%s missing from %s (%s): nothing to read" % (
                             source.prefix or "the bundle", source.rev, source.commit[:12]),
                         member=source.member, path=source.prefix)
            rels = [(path, path[len(source.prefix) + 1:] if source.prefix else path, sha)
                    for path, sha in listed]
            if listed and self._scope_missing(source, (rel for _p, rel, _s in rels)):
                return []
            for path, rel, sha in rels:
                if not _interesting(rel):
                    continue
                if any(path == n or path.startswith(n + "/") for n in nested):
                    continue
                entries.append((path, rel, sha))
            blobs = _git_blobs(source.repo, [sha for _p, _r, sha in entries],
                               deadline=self.deadline)
            return [(path, rel, blobs.get(sha), source.commit) for path, rel, sha in entries]
        if self._scope_missing(source, [
                s for s in self.canon.scopes.get(source.member) or []
                if os.path.isdir(os.path.join(source.directory, s))]):
            return []
        nested_dirs = [os.path.realpath(s.directory) for s in others
                       if _within(source.directory, s.directory)
                       and os.path.realpath(s.directory) != os.path.realpath(source.directory)]
        out = []
        for current, dirs, files in os.walk(source.directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and os.path.realpath(os.path.join(current, d)) not in nested_dirs)
            for name in sorted(files):
                full = os.path.join(current, name)
                rel = os.path.relpath(full, source.directory)
                if not _interesting(rel) or os.path.islink(full):
                    continue
                with open(full, "rb") as fh:
                    content = fh.read(FRONTMATTER_MAX)
                digest = "untrusted:sha256:" + hashlib.sha256(content).hexdigest()
                rel = rel.replace(os.sep, "/")
                out.append((rel, rel, content, digest))
        return out

    def _read_fiches(self) -> None:
        for source in self.canon.sources:
            if source.member in self.canon.scope_unread:
                continue            # L43 : périmètre illisible, rien n'est lu
            try:
                files = self._files(source)
            except (GitError, OSError) as exc:
                self.add("canon-git-error", ERROR, "reading %s: %s" % (source.member, exc),
                         member=source.member)
                continue
            scope = self.canon.scopes.get(source.member)
            ignored: list[str] = []
            for path, rel, content, version in files:
                if not in_scope(rel, scope):
                    # L43 (0031) : hors du périmètre ameesh, une fiche du même
                    # `type` n'est pas une fiche ameesh (sous-agents Claude Code
                    # d'Acme, p. ex.) : ignorée, sans constat bloquant
                    if _profile_type(content):
                        ignored.append(path)
                    continue
                self._fiche(source, path, content, version, bundle_path=rel)
            if ignored:
                self.canon.out_of_scope[source.member] = len(ignored)
                self.add("ameesh-scope-ignored", INFO,
                         "%d typed Agent/Host/Placement/Member/WorkPackage card(s) outside the "
                         "ameesh scope (%s) ignored, e.g. %s"
                         % (len(ignored), ", ".join(s or "." for s in scope),
                            ", ".join(ignored[:3])),
                         member=source.member, path=source.prefix)

    def _fiche(self, source: Source, path: str, content: bytes | None, version: str,
               *, bundle_path: str = "") -> None:
        where = {"member": source.member, "path": path}
        if content is None:
            self.add("canon-git-error", ERROR, "unreadable git object", **where)
            return
        head = content[:FRONTMATTER_MAX]
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            text = head.decode("utf-8", "replace")
            typed = bool(_TYPED_RE.search(text))
            self.add("frontmatter-invalid", ERROR if typed else WARNING,
                     "file is not UTF-8", **where)
            return
        try:
            block = split_frontmatter(text)
            if block is None:
                return
            data = load_yaml(block, use_pyyaml=self.use_pyyaml)
        except YamlError as exc:
            typed = _TYPED_RE.search(text[:FRONTMATTER_MAX])
            # une fiche du plan illisible ne bloque aucun agent (L29)
            subject = ({"package": _package_stem(path)}
                       if typed and typed.group(1) == "WorkPackage" else {})
            if typed and typed.group(1) == PIPELINE_TYPE:
                # L103 (#4395) : une carte Pipeline illisible ne bloque aucun agent
                subject = {"pipeline": path}
            self.add("frontmatter-invalid", ERROR if typed else WARNING,
                     "unreadable frontmatter: %s%s" % (
                         exc, "" if typed else " (unknown type: card ignored)"),
                     **where, **subject)
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == DECISION_TYPE:
            self._decision(source, path, content, version, data)
            return
        if kind not in PROFILE_TYPES:
            return
        title = _text(data.get("title"))
        fiche = Fiche(type=kind, title=title or "", member=source.member, path=path,
                      ref=make_ref(self.canon, source.member, path, version), data=data,
                      untrusted=source.mode != "git", bundle_path=bundle_path or path)
        if not title and kind != "Placement":
            subject = {"package": _package_stem(path)} if kind == "WorkPackage" else {}
            if kind == PROCESS_TYPE:
                # a Process card error blocks no agent (#538): without a title,
                # the card is named by its path
                subject = {"process": path}
            elif kind == PIPELINE_TYPE:
                subject = {"pipeline": path}
            self.add("fiche-title-missing", ERROR, "%s card without `title`" % kind, **where,
                     **subject)
            return
        build = getattr(self, "_build_" + kind.lower())
        if kind == PROCESS_TYPE:
            # #538 : le corps de la fiche (les règles) est lu avec elle
            build(fiche, where, body=body_of(text))
        else:
            build(fiche, where)

    # -- types ---------------------------------------------------------------------
    def _list(self, fiche: Fiche, key: str, where: dict, code: str, **subject) -> list[str] | None:
        value = fiche.data.get(key)
        if value is None:
            return None
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return [str(value).strip()]
        if isinstance(value, list) and all(isinstance(v, (str, int, float))
                                           and not isinstance(v, bool) for v in value):
            return [str(v).strip() for v in value]
        self.add(code, ERROR, "`%s`: list of strings expected" % key, **where, **subject)
        return None

    def _build_member(self, fiche: Fiche, where: dict) -> None:
        roles = self._list(fiche, "roles", where, "member-invalid")
        authenticators = fiche.data.get("authenticators")
        self.canon.members.append(Member(
            title=fiche.title, roles=roles,
            authenticators=authenticators if isinstance(authenticators, list) else None,
            fiche=fiche,
            deputies=self._list(fiche, "deputies", where, "member-deputies-invalid"),
            superiors=self._list(fiche, "superiors", where, "member-superiors-invalid"),
            local_accounts=self._list(fiche, "local_accounts", where,
                                      "member-local-accounts-invalid")))

    def _roles(self, fiche: Fiche, where: dict, subject: dict) -> list[str] | None:
        """L244 (#420) : les rôles déclarés d'une fiche Agent/Persona.

        Lus sous `roles` (liste ou chaîne), ou `role` au singulier ; normalisés
        en minuscules, sans blanc ni doublon : le garde SQL du verdict QA les
        compare tels quels au registre. `roles: [QA]` vaut `['qa']`."""
        roles = self._list(fiche, "roles", where, "agent-roles-invalid", **subject)
        if roles is None:
            roles = self._list(fiche, "role", where, "agent-roles-invalid", **subject)
        if roles is None:
            return None
        out: list[str] = []
        for role in roles:
            name = role.strip().lower()
            if name and name not in out:
                out.append(name)
        return out

    def _build_agent(self, fiche: Fiche, where: dict) -> None:
        subject = {"agent": fiche.title}
        budget = fiche.data.get("budget_usd_per_day")
        if budget is not None:
            try:
                budget = float(budget)
                if not 0 <= budget < 1e8:  # numeric(12,4) au registre
                    raise ValueError
            except (TypeError, ValueError):
                self.add("agent-budget-invalid", WARNING,
                         "unreadable budget_usd_per_day: %r (ignored)" % (budget,), **where,
                         **subject)
                budget = None
        priority = fiche.data.get("priority")
        if priority is not None:
            try:
                priority = int(priority)
                if priority < 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.add("agent-priority-invalid", WARNING,
                         "unreadable priority: %r (default 0)" % (priority,), **where,
                         **subject)
                priority = None
        memory = fiche.data.get("memory")
        memory_repository = None
        if memory is not None:
            if not isinstance(memory, dict):
                self.add("agent-memory-invalid", WARNING,
                         "`memory`: mapping expected (ignored)", **where, **subject)
            else:
                memory_repository = _text(memory.get("repository"))
        harnesses = self._list(fiche, "harnesses", where, "agent-harnesses-invalid", **subject)
        harness = _text(fiche.data.get("harness")) or (harnesses[0] if harnesses else None)
        # L243 (#435): `projects:` wins over `team:`, which becomes its first;
        # a list only — `projects: a, b` is one string, never two projects
        if fiche.data.get("projects") is not None \
                and not isinstance(fiche.data.get("projects"), list):
            self.add("agent-projects-invalid", ERROR,
                     "`projects`: list expected (`projects: [a, b]`), not %r"
                     % (fiche.data.get("projects"),), **where, **subject)
            projects = None
        else:
            projects = self._list(fiche, "projects", where, "agent-projects-invalid",
                                  **subject)
        if projects is not None and not all(projects):
            self.add("agent-projects-invalid", ERROR, "`projects`: empty project name",
                     **where, **subject)
            projects = [p for p in projects if p]
        team = _text(fiche.data.get("team"))
        if projects is not None:
            team = projects[0] if projects else None
        alias_value = fiche.data.get("aliases")
        if alias_value is not None and (not isinstance(alias_value, list)
                                       or not all(isinstance(n, str) for n in alias_value)):
            self.add("agent-aliases-invalid", ERROR, "`aliases`: list of names expected",
                     **where, **subject)
            agent_aliases = []
        else:
            agent_aliases = list(alias_value or [])
        # L165 (EP1) : `releve: hote` (défaut) ou `election` ; les candidats
        # sont les hôtes qui peuvent prendre le bail, tous admis par la fiche
        # Placement (contrôlé par `validate`, qui connaît les hôtes).
        releve = (_text(fiche.data.get("releve")) or "hote").strip().lower()
        if releve not in ("hote", "election"):
            # Valeur illisible : repli sur `election` (fail closed). Un repli
            # sur `hote` rouvrirait la ligne sur l'hôte courant, alors que la
            # fiche est fautive ; `election` exige un verdict candidat, que la
            # fiche en erreur ferme partout.
            self.add("agent-releve-invalid", ERROR,
                     "`releve`: `hote` or `election` expected, not %r "
                     "(treated as `election`, not claimable)"
                     % fiche.data.get("releve"), **where, **subject)
            releve = "election"
        candidats = self._list(fiche, "candidats", where, "agent-candidats-invalid", **subject)
        if candidats is not None:
            candidats = list(dict.fromkeys(c for c in candidats if c))
        # #3185 : projets relus hors équipe ; une liste seulement, comme
        # `projects` (`relit: a, b` est une chaîne, jamais deux projets)
        if fiche.data.get("relit") is not None \
                and not isinstance(fiche.data.get("relit"), list):
            self.add("agent-relit-invalid", ERROR,
                     "`relit`: list expected (`relit: [a, b]`), not %r"
                     % (fiche.data.get("relit"),), **where, **subject)
            relit = None
        else:
            relit = self._list(fiche, "relit", where, "agent-relit-invalid", **subject)
        if relit is not None:
            relit = list(dict.fromkeys(p for p in relit if p))
        self.canon.agents.append(Agent(
            title=fiche.title,
            responsible=_text(fiche.data.get("responsible")),
            team=team,
            capabilities=self._list(fiche, "capabilities", where, "agent-capabilities-invalid",
                                    **subject),
            harness=harness,
            model=_text(fiche.data.get("model")),
            provider=_text(fiche.data.get("provider")),
            credential_mode=_text(fiche.data.get("credential_mode")),
            budget_usd_per_day=budget,
            tools=self._list(fiche, "tools", where, "agent-tools-invalid", **subject),
            reviewers=self._list(fiche, "reviewers", where, "agent-reviewers-invalid", **subject),
            priority=0 if priority is None else priority,
            memory_repository=memory_repository,
            fiche=fiche,
            harnesses=harnesses,
            roles=self._roles(fiche, where, subject),
            projects=projects,
            aliases=agent_aliases,
            releve=releve,
            candidats=candidats,
            relit=relit,
        ))

    def _build_persona(self, fiche: Fiche, where: dict) -> None:
        """L51: read a `Persona` card like an `Agent` card (0029, 0032)."""
        self._build_agent(fiche, where)

    def _build_host(self, fiche: Fiche, where: dict) -> None:
        subject = {"host": fiche.title}
        raw = fiche.data.get("policy")
        policy = HostPolicy()
        if raw is not None and not isinstance(raw, dict):
            self.add("host-policy-invalid", ERROR, "`policy`: mapping expected", **where,
                     **subject)
            # politique illisible : rien n'est admis (fail closed)
            policy = HostPolicy(harnesses=[], providers=[], credential_modes=[])
        elif isinstance(raw, dict):
            for key in ("harnesses", "providers", "models", "credential_modes"):
                value = raw.get(key)
                if value is None:
                    continue
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list) or not all(isinstance(v, (str, int, float))
                                                          for v in value):
                    self.add("host-policy-invalid", ERROR,
                             "`policy.%s`: list of strings expected" % key, **where, **subject)
                    value = []
                setattr(policy, key, [str(v).strip() for v in value])
            if raw.get("max_agents") is not None:
                try:
                    policy.max_agents = int(raw["max_agents"])
                    if policy.max_agents < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    self.add("host-policy-invalid", ERROR,
                             "`policy.max_agents`: positive integer expected", **where, **subject)
                    policy.max_agents = 0
            # L112 : hôte volatil et durée de son bail
            if raw.get("volatile") is not None:
                if isinstance(raw["volatile"], bool):
                    policy.volatile = raw["volatile"]
                else:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.volatile` : booléen attendu", **where, **subject)
                    policy.volatile = True  # prudence : bail court
            if raw.get("lease_ttl") is not None:
                value = raw["lease_ttl"]
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and 10 <= value <= 3600:
                    policy.lease_ttl = float(value)
                else:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.lease_ttl` : nombre de secondes entre 10 et 3600 "
                             "attendu", **where, **subject)
            # #4148: the adapter command of the host lifecycle. A malformed
            # value is an error but no command: an unreadable string must not
            # become a hook that ameesh executes.
            if raw.get("lifecycle_hook") is not None:
                value = raw["lifecycle_hook"]
                if (isinstance(value, str) and value.strip()
                        and "\x00" not in value and len(value.strip()) <= 1000):
                    policy.lifecycle_hook = value.strip()
                else:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.lifecycle_hook`: a non-empty command line expected",
                             **where, **subject)
            # Seuils de ressources (L31, 0028) : un mapping dont chaque clé est
            # validée ici ; une clé illisible est une erreur (fail closed sur le
            # seuil, qui retombe alors sur sa valeur par défaut prudente).
            if raw.get("resources") is not None:
                policy.resources = self._host_resources(raw["resources"], where, subject)
            # L159a: the offer of a lending host; unreadable, it is dropped
            # whole (fail closed: the host lends nothing)
            if raw.get("lend") is not None:
                from . import capacity_guard  # late import: no cycle
                # L479 : les formes héritées de `disk_paths` que main
                # acceptait sont normalisées, pas refusées — un avertissement
                # dit à l'opérateur de corriger la fiche (codex #459 r3)
                legacy: list[str] = []
                offer, problems, unknown = capacity_guard.parse_lend(raw["lend"], legacy)
                for problem in problems:
                    self.add("host-policy-invalid", ERROR, "`policy.lend`: %s" % problem,
                             **where, **subject)
                for warning in legacy:
                    self.add("host-lend-legacy", WARNING, "`policy.lend`: %s" % warning,
                             **where, **subject)
                for key in unknown:
                    self.add("host-lend-unknown", WARNING,
                             "`policy.lend.%s`: unknown key (ignored)" % key,
                             **where, **subject)
                policy.lend = offer
            # L101 : espace de travail et ménage, forme canonique
            # `policy.workspaces` ; les anciennes clés restent lues en alias.
            self._host_workspaces(raw, policy, where, subject)
        tags = self._list(fiche, "tags", where, "host-tags-invalid", **subject)
        admins = self._list(fiche, "admins", where, "host-admins-invalid", **subject)
        occupants = self._list(fiche, "occupants", where, "host-occupants-invalid", **subject)
        until, reason = self._host_unavailability(fiche, where, subject)
        self.canon.hosts.append(Host(title=fiche.title,
                                     responsible=_text(fiche.data.get("responsible")),
                                     policy=policy, tags=tags, admins=admins,
                                     occupants=occupants, fiche=fiche,
                                     unavailable_until=until, unavailable_reason=reason))

    def _host_unavailability(self, fiche: Fiche, where: dict,
                             subject: dict) -> tuple[str | None, str | None]:
        """#3070 : `indisponible_jusqu_au` (jour ISO) et `indisponible_motif`.

        Une date illisible est une erreur et ne déclare rien (une faute de
        frappe ne lève aucune porte) ; un motif sans date est signalé ; une
        date passée est une simple information : la règle a cessé d'elle-même,
        la ligne peut être retirée de la fiche."""
        raw = fiche.data.get(UNAVAILABLE_UNTIL_KEY)
        reason = _text(fiche.data.get(UNAVAILABLE_REASON_KEY))
        if raw is None:
            if reason:
                self.add("host-unavailable-invalid", WARNING,
                         "`%s` without `%s`: the device is not declared unavailable"
                         % (UNAVAILABLE_REASON_KEY, UNAVAILABLE_UNTIL_KEY),
                         **where, **subject)
            return None, None
        if isinstance(raw, _dt.datetime):
            raw = raw.date()
        try:
            day = (raw if isinstance(raw, _dt.date)
                   else _dt.date.fromisoformat(str(raw).strip()))
        except ValueError:
            day = None
        if day is None:
            self.add("host-unavailable-invalid", ERROR,
                     "`%s`: ISO day expected (YYYY-MM-DD), got %r"
                     % (UNAVAILABLE_UNTIL_KEY, raw), **where, **subject)
            return None, None
        if day < _dt.date.today():
            self.add("host-unavailable-past", INFO,
                     "`%s` %s is past: the device is available again, the line can go"
                     % (UNAVAILABLE_UNTIL_KEY, day.isoformat()), **where, **subject)
        return day.isoformat(), reason

    def _host_resources(self, raw, where: dict, subject: dict) -> dict | None:
        """Seuils `policy.resources` validés, ou None si rien n'est déclaré.

        Chaque clé illisible est une erreur de `canon check` ; les clés
        inconnues sont conservées avec un avertissement (OKF tolère les clés
        inconnues, mais pas dans une politique qui gouverne des tours)."""
        from . import resources as resources_mod  # import tardif : pas de cycle

        if not isinstance(raw, dict):
            self.add("host-policy-invalid", ERROR,
                     "`policy.resources`: mapping expected", **where, **subject)
            return None
        parsed: dict = {}
        # #2814: the pressure scope is a name, not a number; it is read first
        # so that an unknown value is an error, never a silent fallback.
        scope = raw.get(resources_mod.PRESSURE_SCOPE_KEY)
        if scope is not None:
            if not isinstance(scope, str) or scope not in resources_mod.PRESSURE_SCOPES:
                self.add("host-policy-invalid", ERROR,
                         "`policy.resources.pressure_scope`: one of %s expected"
                         % ", ".join(resources_mod.PRESSURE_SCOPES), **where, **subject)
            else:
                parsed[resources_mod.PRESSURE_SCOPE_KEY] = scope
        for key in resources_mod.THRESHOLD_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if key == "max_load":
                # codex #212 r4: inf, nan, 1 followed by 400 zeros refused
                number = resources_mod.finite_float(value)
                if number is None or number < 0:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.max_load`: finite positive number expected",
                             **where, **subject)
                    continue
                parsed[key] = number
            elif key in resources_mod.ZOMBIE_KEYS:
                # #2022: a count of processes
                count = resources_mod.parse_count(value)
                if count is None:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.%s`: positive integer expected" % key,
                             **where, **subject)
                    continue
                parsed[key] = count
            elif key in resources_mod.FRACTION_KEYS:
                part = resources_mod.parse_fraction(value)
                if part is None:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.%s`: fraction expected (0 < x <= 1, or \"80%%\")"
                             % key, **where, **subject)
                    continue
                parsed[key] = part
            elif key in resources_mod.POWER_KEYS + resources_mod.PRESSURE_KEYS:
                # L106 : seuils de batterie, en pourcentage ; L159d : pression CPU
                percent = resources_mod.parse_percent(value)
                if percent is None:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.%s`: percentage from 0 to 100 expected"
                             % key, **where, **subject)
                    continue
                parsed[key] = percent
            else:
                octets = resources_mod.parse_bytes(value)
                if octets is None:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.%s`: size in bytes expected (e.g. 1GiB)"
                             % key, **where, **subject)
                    continue
                parsed[key] = octets
        # L159c : plafonds mémoire des unités d'agent (tailles, > 0)
        for key in resources_mod.AGENT_MEMORY_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if key == "agent_memory_max":
                octets = resources_mod.parse_bytes(value)
                if not octets:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.agent_memory_max`: size expected (e.g. 6G)",
                             **where, **subject)
                    continue
                parsed[key] = octets
                continue
            caps = {}
            if isinstance(value, dict):
                caps = {str(agent): resources_mod.parse_bytes(size)
                        for agent, size in value.items()}
            if not isinstance(value, dict) or not all(caps.values()):
                self.add("host-policy-invalid", ERROR,
                         "`policy.resources.agent_memory_max_by_agent`: mapping "
                         "{agent: size} expected (e.g. mesh-design: 8G)", **where, **subject)
                continue
            parsed[key] = caps
        for unknown in sorted(set(raw) - set(resources_mod.THRESHOLD_KEYS)
                              - set(resources_mod.AGENT_MEMORY_KEYS)
                              - {resources_mod.PRESSURE_SCOPE_KEY}):
            self.add("host-resources-unknown", WARNING,
                     "`policy.resources.%s`: unknown key (ignored)" % unknown,
                     **where, **subject)
        return parsed or None

    def _host_workspaces(self, raw: dict, policy: HostPolicy, where: dict,
                         subject: dict) -> None:
        """L101 : `policy.workspaces`, les anciennes clés en alias.

        Forme canonique : `workspaces: {dirs, roots, root, housekeeping}`.
        Les clés historiques `work_dirs`, `work_roots`, `work_root` et
        `housekeeping` restent lues (avertissement `host-policy-legacy-key`,
        sans date de retrait). Donner les deux formes pour la même clé est
        ambigu : erreur `host-policy-invalid`, et la clé n'est pas retenue.
        `HostPolicy` garde ses champs historiques : la normalisation est ici.
        """
        workspaces = raw.get("workspaces")
        if workspaces is not None and not isinstance(workspaces, dict):
            self.add("host-policy-invalid", ERROR,
                     "`policy.workspaces`: mapping expected", **where, **subject)
            workspaces = None
        if isinstance(workspaces, dict):
            for key in sorted(set(map(str, workspaces)) - set(WORKSPACES_FIELDS)):
                self.add("host-workspaces-unknown", WARNING,
                         "`policy.workspaces.%s`: unknown key (ignored)" % key,
                         **where, **subject)

        def value(field: str) -> tuple[Any, str | None]:
            """(valeur, chemin YAML) de la forme neuve ou de son ancien alias."""
            legacy = LEGACY_WORKSPACES[field]
            new_value = workspaces.get(field) if isinstance(workspaces, dict) else None
            old_value = raw.get(legacy)
            if new_value is not None and old_value is not None:
                self.add("host-policy-invalid", ERROR,
                         "`policy.workspaces.%s` and the legacy `policy.%s` both "
                         "declare the same setting: keep one form" % (field, legacy),
                         **where, **subject)
                return None, None
            if old_value is not None:
                self.add("host-policy-legacy-key", WARNING,
                         "`policy.%s` is a legacy key: write `policy.workspaces.%s`"
                         % (legacy, field), **where, **subject)
                return old_value, "policy.%s" % legacy
            if new_value is not None:
                return new_value, "policy.workspaces.%s" % field
            return None, None

        # Racines de travail (L31, 0029) : le dossier de travail d'une session
        # est un réglage de l'hôte, plus du placement. L35 : `dirs` (par agent)
        # et le gabarit `{agent}` ; tout chemin déclaré doit être un texte non
        # vide au gabarit connu (un chemin refusé n'est pas retenu : fail
        # closed, pas de dossier).
        root, path = value("root")
        if root is not None:
            problem = work_template_problem(root)
            if problem:
                self.add("host-policy-invalid", ERROR,
                         "`%s`: %s" % (path, problem), **where, **subject)
            else:
                policy.work_root = str(root).strip()
        for field, label in (("roots", "project"), ("dirs", "agent")):
            mapping, path = value(field)
            if mapping is None:
                continue
            if not isinstance(mapping, dict) or not mapping:
                self.add("host-policy-invalid", ERROR,
                         "`%s`: mapping {%s: path} expected" % (path, label),
                         **where, **subject)
                continue
            kept: dict = {}
            for k, v in mapping.items():
                problem = ("empty key" if not isinstance(k, str) or not k.strip()
                           else work_template_problem(v))
                if problem:
                    self.add("host-policy-invalid", ERROR,
                             "`%s.%s`: %s" % (path, k, problem),
                             **where, **subject)
                    continue
                kept[k.strip()] = v.strip()
            if kept:
                setattr(policy, LEGACY_WORKSPACES[field], kept)
        # Ménage (L73) : dossiers gérés, quotas, alerte tmpfs ; une clé
        # illisible est une erreur et prend sa valeur par défaut.
        housekeeping, path = value("housekeeping")
        if housekeeping is not None:
            from . import menage as menage_mod  # import tardif : pas de cycle
            parsed, problems, unknown = menage_mod.parse_policy(housekeeping)
            for problem in problems:
                self.add("host-policy-invalid", ERROR,
                         "`%s`: %s" % (path, problem), **where, **subject)
            for key in unknown:
                self.add("host-housekeeping-unknown", WARNING,
                         "`%s.%s`: unknown key (ignored)" % (path, key),
                         **where, **subject)
            policy.housekeeping = parsed or None

    def _build_placement(self, fiche: Fiche, where: dict) -> None:
        subject = {"agent": _text(fiche.data.get("agent")) or fiche.title}
        # L31 (0029) : le dossier de travail est un réglage de l'hôte. Le `cwd`
        # d'une ancienne admission est conservé ici ; `validate` dit, hôte par
        # hôte, s'il est ignoré (la politique de l'hôte donne un dossier) ou
        # hérité en repli transitoire (L35 : elle n'en donne pas).
        cwd = _text(fiche.data.get("cwd"))
        self.canon.placements.append(Placement(
            title=fiche.title,
            agent=_text(fiche.data.get("agent")),
            host=_text(fiche.data.get("host")),
            hosts=self._list(fiche, "hosts", where, "admission-hosts-invalid", **subject),
            host_tags=self._list(fiche, "host_tags", where, "admission-tags-invalid",
                                 **subject),
            credential_mode=_text(fiche.data.get("credential_mode")),
            cwd=cwd,
            fiche=fiche,
        ))


    def _build_workpackage(self, fiche: Fiche, where: dict) -> None:
        ident = _text(fiche.data.get("id")) or _package_stem(fiche.path)
        subject = {"package": ident}
        scope = self._list(fiche, "scope", where, "package-scope-invalid", **subject)
        dates = {key: self._day(fiche, key, where, **subject) for key in PACKAGE_DATE_KEYS}
        self.canon.packages.append(WorkPackage(
            id=ident, title=fiche.title,
            kind=_text(fiche.data.get("kind")),
            parent=_text(fiche.data.get("parent")),
            responsible=_text(fiche.data.get("responsible")),
            team=_text(fiche.data.get("team")),
            scope=scope,
            status=_text(fiche.data.get("status")),
            fiche=fiche,
            start=dates["start"], end=dates["end"],
            # `date` : la date d'un jalon, alias de `delivery`
            delivery=dates["delivery"] or dates["date"]))

    def _day(self, fiche: Fiche, key: str, where: dict, **subject) -> str | None:
        """L96 : une date de fiche (`AAAA-MM-JJ`), ou None ; illisible =
        avertissement et date ignorée (le plan reste lu)."""
        value = fiche.data.get(key)
        if value is None or value == "":
            return None
        text = str(value).strip()[:10]
        try:
            return _dt.date.fromisoformat(text).isoformat()
        except ValueError:
            self.add("package-date-invalid", WARNING,
                     "`%s`: unreadable date %r (expected YYYY-MM-DD) — ignored" % (key, value),
                     **where, **subject)
            return None

    def _build_process(self, fiche: Fiche, where: dict, *, body: str = "") -> None:
        """#538 : une fiche Process (`processus/*.md`) — `title`, `updated` et
        `responsible` sont obligatoires. Une clé manquante ou illisible rend la
        fiche invalide (elle n'est jamais injectée dans un brief), sans bloquer
        aucun agent : c'est une règle, pas un droit.

        Le chemin est contraint, RELATIF AU BUNDLE : `type: Process` dans
        `agents/…` n'est pas une règle (constat, jamais injecté), mais un canon
        imbriqué (`knowledge/`) garde ses `processus/*.md` (verdict codex
        #510 r2, P2). #2973 : dans un bundle à périmètre ameesh déclaré
        (`extensions.ameesh.scope`), `<dossier>/processus/*.md` est aussi une
        règle quand `<dossier>` est l'un des dossiers du périmètre — rien
        au-delà. Le texte est rendu encodable en UTF-8 (un surrogate YAML
        isolé ne doit pas faire échouer le brief ni un envoi)."""
        subject = {"process": fiche.title}
        if not process_path_ok(fiche.bundle_path or fiche.path,
                               self.canon.scopes.get(fiche.member)):
            scope = [d for d in self.canon.scopes.get(fiche.member) or () if d]
            dossiers = "`, `".join([PROCESS_DIR + "/"]
                                   + ["%s/%s/" % (d, PROCESS_DIR) for d in scope])
            self.add("process-path", ERROR,
                     "Process card outside `%s`: %s (never injected as a working "
                     "rule)" % (dossiers, fiche.path), **where, **subject)
            fiche = dataclasses.replace(fiche, title=_utf8_safe(fiche.title))
            self.canon.processes.append(Process(
                fiche=fiche, description=_utf8_safe(_text(fiche.data.get("description")) or ""),
                responsible="", updated="", expire="",
                body=_utf8_safe(body), valid=False))
            return
        fiche = dataclasses.replace(fiche, title=_utf8_safe(fiche.title))
        responsible = _text(fiche.data.get("responsible"))
        responsible = _utf8_safe(responsible) if responsible else responsible
        if not responsible:
            self.add("process-responsible-missing", ERROR,
                     "Process card without `responsible` (the human who answers "
                     "for the rule)", **where, **subject)
        updated = self._process_day(fiche, "updated", where, required=True, **subject)
        expire = self._process_day(fiche, "expire", where, required=False, **subject)
        declared_expire = fiche.data.get("expire")
        valid = bool(responsible) and updated is not None \
            and (declared_expire in (None, "") or expire is not None)
        # #1372 : la projection courte est lue ici (jamais inventée) ; ses
        # défauts de forme sont des constats de `canon check`, pas une erreur
        # bloquante : la carte reste lue, le brief garde le texte entier.
        contenu = _utf8_safe(body)
        projection, projection_error = process_projection(contenu)
        self.canon.processes.append(Process(
            fiche=fiche,
            description=_utf8_safe(_text(fiche.data.get("description")) or ""),
            responsible=responsible or "", updated=updated or "", expire=expire or "",
            body=contenu, projection=projection, projection_error=projection_error,
            valid=valid))

    def _build_pipeline(self, fiche: Fiche, where: dict) -> None:
        """L103 (#4395) : une carte Pipeline (`pipelines/*.md`) — `title`,
        `responsible` et `updated` obligatoires, `name` facultatif (défaut :
        nom du fichier, grammaire des noms de module), bloc `pipeline:` (alias
        transitoire `workflow:`, jamais les deux) compilé par l'encodeur de
        `process_card`. Chemin contraint comme une carte Process (bundle
        imbriqué et périmètre déclaré compris). Toute erreur rend la carte
        invalide et porte `pipeline` : `canon check` échoue, aucun agent n'est
        bloqué. Le responsable est résolu dans `_validate_pipelines`."""
        from . import modules as modules_mod
        from . import process_card
        from .process_workflow import WorkflowRefusal

        stem = posixpath.splitext(posixpath.basename(fiche.path))[0]
        name = _text(fiche.data.get("name"))
        name = name if name is not None else stem
        subject = {"pipeline": name or stem}
        valid = True
        if not process_path_ok(fiche.bundle_path or fiche.path,
                               self.canon.scopes.get(fiche.member), PIPELINE_DIR):
            scope = [d for d in self.canon.scopes.get(fiche.member) or () if d]
            dossiers = "`, `".join([PIPELINE_DIR + "/"]
                                   + ["%s/%s/" % (d, PIPELINE_DIR) for d in scope])
            self.add("pipeline-path", ERROR,
                     "Pipeline card outside `%s`: %s" % (dossiers, fiche.path),
                     **where, **subject)
            valid = False
        if not isinstance(fiche.data.get("name") or "", str) \
                or not modules_mod.NAME.fullmatch(name or ""):
            self.add("pipeline-name-invalid", ERROR,
                     "Pipeline card: name %r is not a module name (letters, digits, "
                     "`.`, `_`, `-`; 64 characters at most)" % fiche.data.get("name", name),
                     **where, **subject)
            valid = False
        responsible = _text(fiche.data.get("responsible"))
        if not responsible:
            self.add("pipeline-responsible-missing", ERROR,
                     "Pipeline card without `responsible` (the human who answers "
                     "for the pipeline)", **where, **subject)
            valid = False
        updated = self._process_day(fiche, "updated", where, required=True,
                                    prefix="pipeline", label="Pipeline", **subject)
        valid = valid and updated is not None
        artifact, error = None, ""
        try:
            block = process_card.pipeline_block(fiche.data)
            artifact, _encoding = process_card.compile_block(block, ref=fiche.ref)
        except WorkflowRefusal as exc:
            error = str(exc)
            self.add("pipeline-invalid", ERROR,
                     "Pipeline card %s: %s" % (name or stem, error), **where, **subject)
            valid = False
        self.canon.pipelines.append(Pipeline(
            fiche=fiche, name=name or "",
            description=_text(fiche.data.get("description")) or "",
            responsible=responsible or "", updated=updated or "",
            artifact=artifact, error=error, valid=valid))

    def _process_day(self, fiche: Fiche, key: str, where: dict, *, required: bool,
                     prefix: str = "process", label: str = "Process",
                     **subject) -> str | None:
        """#538 : un jour ISO d'une fiche Process (`AAAA-MM-JJ`). Vide : erreur
        si la clé est obligatoire, silence sinon ; illisible : erreur. L103 :
        `prefix`/`label` nomment le constat d'une carte Pipeline."""
        value = fiche.data.get(key)
        if value is None or value == "":
            if required:
                self.add("%s-%s-missing" % (prefix, key), ERROR,
                         "%s card without `%s` (expected YYYY-MM-DD)" % (label, key),
                         **where, **subject)
            return None
        text = str(value).strip()[:10]
        try:
            return _dt.date.fromisoformat(text).isoformat()
        except ValueError:
            self.add("%s-%s-invalid" % (prefix, key), ERROR,
                     "`%s`: unreadable date %r (expected YYYY-MM-DD)" % (key, value),
                     **where, **subject)
            return None

    def _decision(self, source, path: str, content: bytes, version: str, data: dict) -> None:
        """L96 : garde une fiche Decision (frontmatter et corps, borné)."""
        title = _text(data.get("title")) or _package_stem(path)
        body = body_of(content[:DECISION_BODY_MAX].decode("utf-8", "replace"))
        fiche = Fiche(type=DECISION_TYPE, title=title, member=source.member, path=path,
                      ref=make_ref(self.canon, source.member, path, version), data=data,
                      untrusted=source.mode != "git")
        self.canon.decisions.append(DecisionNote(fiche=fiche, body=body))


def _package_stem(path: str) -> str:
    """Identifiant par défaut d'une fiche WorkPackage : son nom de fichier."""
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _utf8_safe(text: str) -> str:
    """Un texte encodable en UTF-8 (#538) : un échappement YAML peut produire un
    surrogate UTF-16 isolé (`title: "\\uD800"`), qui passe la lecture puis fait
    échouer tout encodage — brief, fil, envoi. Remplacé ici, à la lecture."""
    return text.encode("utf-8", "replace").decode("utf-8")


# ==========================================================================
# Identité d'un canon et références qualifiées (L42, décision 0031)
# ==========================================================================

def _directory_id(root: str) -> str:
    """Identifiant de repli : le nom du dossier, ramené à CANON_ID_RE."""
    base = os.path.basename(os.path.normpath(root or "")) or "canon"
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", base).lstrip("._-")[:64]
    return text if CANON_ID_RE.match(text) else "canon"


def _identify(canon: Canon, manifest: dict | None) -> None:
    """Pose `canon.id` : `id` de federation.yaml s'il est valide, sinon le nom
    du dossier (`id_source` = directory ; le constat n'est ajouté qu'en
    configuration à plusieurs canons, voir `note_identity`)."""
    raw = (manifest or {}).get("id") if isinstance(manifest, dict) else None
    text = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) \
        else ""
    if text and CANON_ID_RE.match(text):
        canon.id, canon.id_source = text, ID_FROM_FEDERATION
    else:
        canon.id, canon.id_source = _directory_id(canon.root), ID_FROM_DIRECTORY


def note_identity(canon: Canon) -> None:
    """Constat (avertissement) d'un canon sans `id` valide dans federation.yaml,
    identifié par le nom de son dossier. Ajouté seulement quand plusieurs
    canons sont configurés : seul, un canon n'a pas besoin d'identité."""
    if canon.id_source == ID_FROM_FEDERATION:
        return
    raw = (canon.federation or {}).get("id") if isinstance(canon.federation, dict) else None
    code = "canon-id-invalid" if raw not in (None, "") else "canon-id-missing"
    canon.load_findings.append(Finding(
        code, WARNING,
        "%s: identified by its directory name, \"%s\" (declare `id` in "
        "federation.yaml%s)" % (
            canon.root, canon.id,
            ": %r outside the grammar [A-Za-z0-9._-]" % (raw,) if raw not in (None, "")
            else ""),
        path="federation.yaml", untrusted=canon.untrusted))


def make_ref(canon: Canon, member: str, path: str, version: str) -> str:
    """Référence d'une fiche : historique (`membre:chemin@version`) pour le
    canon par défaut, préfixée de l'identifiant du canon pour les autres —
    les deux fédérations de l'hôte peuvent nommer leur racine `home`."""
    ref = "%s:%s@%s" % (member, path, version)
    return ref if canon.is_default else "%s/%s" % (canon.id, ref)


def split_ref(ref: str | None) -> tuple[str | None, str, str, str] | None:
    """(canon ou None, membre, chemin, version) d'une référence de fiche, ou None."""
    match = FICHE_REF_RE.fullmatch(ref or "")
    if not match:
        return None
    return match.group("canon"), match.group("member"), match.group("path"), \
        match.group("version")


def load(root: str, *, ref: str = "", untrusted: bool = False, fetch: bool = False,
         use_pyyaml: bool | None = None, default: bool = True,
         deadline: Deadline | None = None) -> Canon:
    """Lit le canon (§4.1). Les problèmes de lecture sont dans `canon.load_findings`.

    Une erreur git imprévue (dépôt cassé, délai dépassé) rend le canon
    illisible (`readable` faux) au lieu de remonter : `canon sync` peut alors
    enregistrer l'état `unreadable`.

    `default` (L42) : faux pour un canon qui n'est pas le premier configuré —
    ses références sont alors préfixées de son identifiant.

    ``deadline`` (#2665) : la découverte ne prend que le temps qui reste ;
    une échéance épuisée lève `GitDeadline`, jamais un canon « illisible ».
    """
    loader = _Loader(root, ref=ref, untrusted=untrusted, fetch=fetch, use_pyyaml=use_pyyaml,
                     default=default, deadline=deadline)
    try:
        return loader.load()
    except GitError as exc:
        loader.add("canon-git-error", ERROR, "reading the canon: %s" % exc)
        loader.canon.readable = False
        return loader.canon
    except OSError as exc:
        # Lecture refusée ou impossible (permissions, fichier disparu) : même
        # traitement fail-closed qu'une erreur git.
        loader.add("canon-io-error", ERROR, "reading the canon: %s" % (exc.strerror or exc))
        loader.canon.readable = False
        return loader.canon


def configured(cfg) -> tuple:
    """Canons configurés (`config.CanonEntry`), le canon par défaut d'abord (L42)."""
    entries = getattr(cfg, "canon_entries", None)
    if entries is None:                 # configuration factice (tests)
        path = getattr(cfg, "canon", "")
        if not path:
            return ()
        from .config import CanonEntry
        return (CanonEntry(path, getattr(cfg, "canon_ref", ""),
                           bool(getattr(cfg, "canon_untrusted", False))),)
    return tuple(entries)


def from_config(cfg, *, root: str | None = None, fetch: bool = False,
                entry=None, deadline: Deadline | None = None) -> Canon:
    """Le canon de la configuration (`AMEESH_CANON`, `AMEESH_CANON_REF`, …).

    Sans `entry` : le canon par défaut (ou `root`, lu comme canon par défaut).
    `entry` (L42, `config.CanonEntry`) : un autre canon configuré — il n'est le
    canon par défaut que s'il est le premier de la liste."""
    entries = configured(cfg)
    if entry is not None:
        default = bool(entries) and entries[0].path == entry.path
        canon = load(entry.path, ref=entry.ref, untrusted=entry.untrusted, fetch=fetch,
                     default=default, deadline=deadline)
    else:
        path = root or cfg.canon
        if not path:
            raise CanonError("no canon configured: set AMEESH_CANON (or `canon` in the "
                             "configuration), or pass --canon DIR")
        canon = load(path, ref=cfg.canon_ref, untrusted=cfg.canon_untrusted, fetch=fetch,
                     deadline=deadline)
    if len(entries) > 1:
        note_identity(canon)
    return canon


def check_identities(canons: list[Canon]) -> None:
    """Deux canons configurés avec le même identifiant : erreur de configuration."""
    seen: dict[str, Canon] = {}
    for canon in canons:
        other = seen.get(canon.id)
        if other is not None:
            raise CanonError(
                "configuration: two canons have the identifier \"%s\" (%s and %s) — "
                "the federation.yaml `id` (otherwise the directory name) must be unique "
                "among the host's canons" % (canon.id, other.root, canon.root))
        seen[canon.id] = canon


#: Canons read inside an explicit, short-lived window (`configured_cache`).
#: A read command asks for them several times — once per alert family, and
#: once per dry pass of `db.batched` — and every `load_configured` re-reads
#: git and the fiches, which dominated `alerts` (L521).
_CANONS = threading.local()


@contextlib.contextmanager
def configured_cache(cfg=None, canons=None):
    """Read the configured canons once inside the block.

    The cache never outlives the block: a long-lived process (`alerts
    --follow`, the runner) must see canon changes at its next read.

    ``cfg``/``canons`` seed the window with a list the caller has already
    read (#4178): a projection that received its canons does not read them
    again for each delegated authority check."""
    previous = getattr(_CANONS, "loaded", None)
    _CANONS.loaded = {}
    if cfg is not None and canons is not None:
        _CANONS.loaded[_configured_key(cfg, False)] = list(canons)
    try:
        yield
    finally:
        _CANONS.loaded = previous


def _configured_key(cfg, fetch: bool) -> tuple:
    return (tuple((entry.path, getattr(entry, "ref", ""),
                   bool(getattr(entry, "untrusted", False)))
                  for entry in configured(cfg)), bool(fetch))


def load_configured(cfg, *, fetch: bool = False,
                    deadline: Deadline | None = None) -> list[Canon]:
    """Les canons configurés qui se laissent charger, le canon par défaut
    d'abord (lecture seule : seuils, hôtes). Une configuration fautive d'un
    canon ne masque pas les autres ; un identifiant en double est écarté.

    ``deadline`` (#2665) borne la découverte elle-même — résolution du
    dépôt, références, manifeste et fiches : chaque lecture git ne prend que
    le temps qui reste, et une échéance épuisée lève `GitDeadline` au lieu de
    rendre une liste partielle ou vide lue comme « canon sans bloc ».
    """
    if deadline is not None and deadline.expired():
        raise GitDeadline("canon discovery: deadline exhausted")
    cache = getattr(_CANONS, "loaded", None)
    key = _configured_key(cfg, fetch) if cache is not None else None
    if cache is not None and key in cache:
        return list(cache[key])
    out: list[Canon] = []
    for index, entry in enumerate(configured(cfg)):
        if deadline is not None and deadline.expired():
            raise GitDeadline("canon discovery: deadline exhausted")
        try:
            canon = from_config(cfg, fetch=fetch, deadline=deadline) if index == 0 \
                else from_config(cfg, entry=entry, fetch=fetch, deadline=deadline)
        except CanonError:
            continue
        if deadline is not None and deadline.expired():
            raise GitDeadline("canon discovery: deadline exhausted")
        if any(c.id == canon.id for c in out):
            continue
        out.append(canon)
    if cache is not None:
        cache[key] = out
    return out


def find_host(canons: Iterable[Canon], name: str) -> Host | None:
    """Fiche Host `name` du premier canon qui la déclare (le canon par défaut
    d'abord). L43 (0031) : à n'utiliser que pour l'AFFICHAGE (responsable) ;
    l'admission d'un agent se juge avec la fiche de SON canon, et les limites
    physiques avec `host_fiches` / `resources.host_limits`."""
    for canon in canons:
        host = canon.host(name)
        if host is not None:
            return host
    return None


def host_fiches(canons: Iterable[Canon], name: str) -> list[tuple[Canon, Host]]:
    """(canon, fiche Host `name`) de chaque canon qui décrit cet hôte (L43),
    le canon par défaut d'abord."""
    out: list[tuple[Canon, Host]] = []
    for canon in canons:
        host = canon.host(name) if hasattr(canon, "host") else None
        if host is not None:
            out.append((canon, host))
    return out


def from_config_all(cfg, *, fetch: bool = False) -> list[Canon]:
    """Tous les canons configurés (L42, décision 0031), le canon par défaut
    d'abord. Identifiants en double : CanonError (aucun canon rendu)."""
    entries = configured(cfg)
    if not entries:
        raise CanonError("no canon configured: set AMEESH_CANON or AMEESH_CANONS (or "
                         "`canon` / `canons` in the configuration)")
    canons = [from_config(cfg, fetch=fetch)]
    canons += [from_config(cfg, entry=entry, fetch=fetch) for entry in entries[1:]]
    check_identities(canons)
    return canons


# ==========================================================================
# Validation (§4.3)
# ==========================================================================

def _relationship_cycles(graph):
    """Directed back-edge cycles, without recursion on untrusted member chains."""
    done = set()
    for start in graph:
        if start in done:
            continue
        path = [start]
        positions = {start: 0}
        stack = [(start, iter(graph[start]))]
        while stack:
            node, edges = stack[-1]
            target = next(edges, None)
            if target is None:
                stack.pop()
                path.pop()
                positions.pop(node)
                done.add(node)
            elif target in positions:
                yield path[positions[target]:] + [target]
            elif target not in done:
                positions[target] = len(path)
                path.append(target)
                stack.append((target, iter(graph.get(target, ()))))


def validate(canon: Canon, known_projects: Iterable[str] | None = None) -> list[Finding]:
    """Constats de lecture + constats du profil. Une erreur est bloquante.

    `known_projects` (L243) : les projets connus hors du canon (`github.projects`
    de l'hôte, `app` des lots) ; None, sans base : `agent-project-unknown`
    n'est pas vérifié."""
    findings: list[Finding] = list(canon.load_findings)
    untrusted = canon.untrusted

    def add(code: str, severity: str, message: str, fiche: Fiche | None = None,
            **subject: str) -> None:
        where = {"member": fiche.member, "path": fiche.path} if fiche else {}
        findings.append(Finding(code, severity, message, untrusted=untrusted,
                                **where, **subject))

    if not canon.readable:
        return findings

    # -- doublons --------------------------------------------------------------
    for kind, items, subject in (("member", canon.members, None),
                                 ("agent", canon.agents, "agent"),
                                 ("host", canon.hosts, "host")):
        by_title: dict[str, list] = defaultdict(list)
        for item in items:
            by_title[item.title].append(item)
        for title, group in by_title.items():
            if len(group) > 1:
                for item in group:
                    add("%s-duplicate" % kind, ERROR,
                        "%s %s declared %d times (%s)" % (
                            kind, title, len(group),
                            ", ".join("%s:%s" % (g.fiche.member, g.fiche.path) for g in group)),
                        item.fiche, **({subject: title} if subject else {}))

    # Names share one namespace, including every human Member in the canon.
    owners = defaultdict(list)
    for agent in canon.agents:
        for name in [agent.title] + list(agent.aliases or []):
            owners[name].append(agent)
            if not NAME_RE.fullmatch(name) or (name == "all" and name != agent.title):
                add("agent-alias-invalid", ERROR, "invalid agent name or alias %r" % name,
                    agent.fiche, agent=agent.title)
    human_names = {m.title for m in canon.members}
    for name, agents in owners.items():
        if len(agents) > 1 or name in human_names:
            for agent in agents:
                add("agent-alias-conflict", ERROR,
                    "name or alias %s belongs to more than one identity" % name,
                    agent.fiche, agent=agent.title)

    agent_names = set(owners)
    for member in canon.members:
        if member.title in agent_names:
            add("member-agent-clash", ERROR,
                "human member %s has the name of an agent: ambiguous responsible"
                % member.title, member.fiche, agent=member.title)

    # -- agents ------------------------------------------------------------------
    # L16 (R22) : plus de liste fermée de harnais ; les identifiants valides sont
    # ceux des descripteurs connus (paquet + dossier de l'hôte). Un `harness`
    # inconnu est une ERREUR : l'agent ne serait réclamable nulle part, et le
    # silence ferait croire à un harnais supporté.
    known_harnesses = set(harnesses.known_ids())
    known = None if known_projects is None else (
        set(known_projects) | {p.team for p in canon.packages if p.team})
    # #4803 : les projets que le canon déclare lui-même. `relit` ne peut nommer
    # que ceux-là : `canon sync` valide sans base, et un projet connu de la
    # seule base (lots, `github.projects`) y serait `agent-relit-unknown` —
    # `canon check` doit rendre le même verdict. `risk_classes.projects` est la
    # déclaration de projet de `federation.yaml` (cf.
    # `mechanics.declared_projects` et `review.scoped_from_federation`), sans
    # donner ce projet à un agent.
    declared_projects = {p.team for p in canon.packages if p.team} | {
        project for agent in canon.agents for project in agent.effective_projects} | set(
            review_mod.scoped_from_federation(canon.federation).projects)
    for agent in canon.agents:
        f = agent.fiche
        if not NAME_RE.match(agent.title):
            add("agent-title-invalid", ERROR,
                "agent name %r outside the registry grammar" % agent.title, f)
            continue
        if canon.naming is not None and not canon.naming.conforms(agent.title):
            # L243 (#468) : on ne dit rien des noms existants (ils seront
            # renommés par #423) ; seul un titre qui TENTE la convention
            # (préfixe de l'organisation) et la rate est signalé.
            if str(agent.title).startswith(canon.naming.org + "-"):
                add("agent-name-convention", WARNING,
                    "agent %s does not follow the naming convention %s: a new agent "
                    "is named %s (existing names are not renamed here: see #423)"
                    % (agent.title, canon.naming.describe(), canon.naming.example()),
                    f, agent=agent.title)
        if not agent.responsible:
            add("agent-responsible-missing", ERROR,
                "agent %s without `responsible` (R14: every agent has a responsible human)"
                % agent.title, f, agent=agent.title)
        elif canon.resolve_human(agent.responsible) is None:
            add("agent-responsible-unresolved", ERROR,
                "responsible %r of %s does not resolve to a human Member (expected "
                "human:<id> of a unique Member card)" % (agent.responsible, agent.title),
                f, agent=agent.title)
        caps = agent.capabilities or []
        forbidden = [c for c in caps if c.strip().lower() in FORBIDDEN_CAPABILITIES]
        if forbidden:
            add("agent-approve-capability", ERROR,
                "agent %s with the `approve` capability: reserved for humans (R8)"
                % agent.title, f, agent=agent.title)
        unknown = [c for c in caps if c not in KNOWN_CAPABILITIES and c not in forbidden]
        if unknown:
            add("agent-capability-unknown", WARNING,
                "unknown capabilities for %s: %s" % (agent.title, ", ".join(unknown)),
                f, agent=agent.title)
        if not agent.harness:
            add("agent-harness-missing", WARNING,
                "agent %s without `harness`: no harness will be started" % agent.title,
                f, agent=agent.title)
        elif agent.harness not in known_harnesses:
            add("agent-harness-unknown", ERROR,
                "harness %r of %s has no known descriptor (%s)"
                % (agent.harness, agent.title, ", ".join(sorted(known_harnesses)) or "none"),
                f, agent=agent.title)
        for name in agent.harnesses or []:
            if name != agent.harness and name not in known_harnesses:
                add("agent-harness-unknown", ERROR,
                    "harness %r (`harnesses`) of %s has no known descriptor" % (name, agent.title),
                    f, agent=agent.title)
        if agent.harnesses and agent.harness and agent.harness not in agent.harnesses:
            add("agent-harness-outside-list", WARNING,
                "`harness` %r of %s is absent from `harnesses` (%s)"
                % (agent.harness, agent.title, ", ".join(agent.harnesses)),
                f, agent=agent.title)
        # L243 (#435): one truth per card — `team`, when kept beside
        # `projects`, is its first project
        if agent.projects is not None:
            twice = sorted({p for p in agent.projects if agent.projects.count(p) > 1})
            if twice:
                add("agent-project-duplicate", ERROR,
                    "`projects` of %s lists %s more than once" % (agent.title, ", ".join(twice)),
                    f, agent=agent.title)
            team = _text(f.data.get("team"))
            if team is not None and team != agent.team:
                add("agent-team-mismatch", ERROR,
                    "`team` %r of %s is not its primary project (first of `projects`: %s)"
                    % (team, agent.title, agent.team or "none"), f, agent=agent.title)
            if known is not None:
                unknown = [p for p in dict.fromkeys(agent.projects) if p not in known]
                if unknown:
                    add("agent-project-unknown", WARNING,
                        "unknown projects for %s: %s (neither a work package's team, "
                        "`github.projects` nor a lot's project)"
                        % (agent.title, ", ".join(unknown)), f, agent=agent.title)
        # #3185/#4803 : `relit` ne nomme que les projets déclarés par le canon
        # (équipes des paquets, projets des fiches, `risk_classes.projects`) ;
        # les projets connus de la seule base ne comptent pas, sinon
        # `canon check` accepte ce que `canon sync` refuse sans base
        if agent.relit:
            stray = [p for p in agent.relit if p not in declared_projects]
            if stray:
                add("agent-relit-unknown", ERROR,
                    "`relit` of %s names undeclared projects: %s"
                    % (agent.title, ", ".join(stray)), f, agent=agent.title)

    # -- human deputies and superiors (L51, 0032 §2, 0033 §7) --------------------
    for member in canon.members:
        for key, values in (("deputies", member.deputies), ("superiors", member.superiors)):
            for ref in values or []:
                resolved = canon.resolve_human(ref)
                if resolved is None:
                    add("member-%s-unresolved" % key, ERROR,
                        "%s %r of %s does not resolve to a human Member (expected "
                        "human:<id> of a unique Member card)" % (key, ref, member.title),
                        member.fiche)
                elif resolved == "human:%s" % member.title:
                    add("member-%s-self" % key, ERROR,
                        "%s of %s: a human cannot be their own %s"
                        % (key, member.title, "deputy" if key == "deputies" else "superior"),
                        member.fiche)

    members_by_ref = {}
    for member in canon.members:
        ref = canon.resolve_human("human:%s" % member.title)
        if ref is not None:
            members_by_ref[ref] = member
    for key in ("deputies", "superiors"):
        graph = {}
        for ref, member in members_by_ref.items():
            graph[ref] = []
            for target in getattr(member, key) or []:
                resolved = canon.resolve_human(target)
                if resolved in members_by_ref and resolved != ref:
                    graph[ref].append(resolved)
        for cycle in _relationship_cycles(graph):
            add("member-%s-cycle" % key, ERROR,
                "cycle in human %s: %s" % (key, " → ".join(cycle)),
                members_by_ref[cycle[0]].fiche)

    # -- hôtes -----------------------------------------------------------------------
    for host in canon.hosts:
        if not host.responsible:
            add("host-responsible-missing", ERROR,
                "host %s without `responsible`" % host.title, host.fiche, host=host.title)
        elif canon.resolve_human(host.responsible) is None:
            add("host-responsible-unresolved", ERROR,
                "responsible %r of host %s does not resolve to a human Member"
                % (host.responsible, host.title), host.fiche, host=host.title)
        for ref in host.occupants or []:
            # L110 (0029) : un occupant qui ne résout pas rend la règle de
            # visibilité illisible pour cet hôte (refus) ; autant le dire ici
            if canon.resolve_human(ref) is None:
                add("host-occupant-unresolved", ERROR,
                    "occupant %r de l'hôte %s ne résout pas vers un Member humain"
                    % (ref, host.title), host.fiche, host=host.title)
        for name in host.policy.harnesses or []:
            if name not in known_harnesses:
                add("host-harness-unknown", ERROR,
                    "policy of host %s: harness %r has no known descriptor (%s)"
                    % (host.title, name, ", ".join(sorted(known_harnesses)) or "none"),
                    host.fiche, host=host.title)

    # -- admissions (ex-placements, L31/0029) --------------------------------------------
    host_names = {h.title for h in canon.hosts}
    known_tags = {tag for h in canon.hosts for tag in (h.tags or [])}
    by_agent: dict[str, list[Placement]] = defaultdict(list)
    #: hôte -> {id(admission): admission} (un hôte nommé ET étiqueté ne compte qu'une fois)
    by_host: dict[str, dict[int, Placement]] = defaultdict(dict)
    for placement in canon.placements:
        f = placement.fiche
        if not placement.agent:
            add("placement-incomplete", ERROR, "admission without `agent`", f)
            continue
        if not placement.host_names() and not placement.tags():
            add("placement-incomplete", ERROR,
                "admission of %s without `hosts` or `host_tags`" % placement.agent, f,
                agent=placement.agent)
            continue
        declared_agent = canon.agent(placement.agent)
        by_agent[declared_agent.title if declared_agent else placement.agent].append(placement)
        if placement.agent not in agent_names:
            add("placement-agent-unknown", ERROR,
                "admission for an unknown agent: %s" % placement.agent, f,
                agent=placement.agent)
        cibles = set(placement.host_names())
        for tag in placement.tags():
            if tag not in known_tags:
                add("admission-tag-unknown", WARNING,
                    "admission of %s: tag `%s` matches no host"
                    % (placement.agent, tag), f, agent=placement.agent)
            cibles |= {h.title for h in canon.hosts if tag in (h.tags or [])}
        for name in sorted(cibles):
            if name not in host_names:
                add("placement-host-unknown", ERROR,
                    "admission of %s to an unknown host: %s" % (placement.agent, name),
                    f, agent=placement.agent)
                continue
            by_host[name][id(placement)] = placement
            agent = canon.agent(placement.agent)
            host = canon.host(name)
            if agent is not None and host is not None:
                for reason in placement_violations(agent, host, placement):
                    # L165 (EP1) : pour un rôle élu, une politique d'hôte qui
                    # refuse l'agent ne ferme QUE ce candidat ; les autres
                    # hôtes candidats gardent leur propre verdict. Un agent
                    # épinglé, lui, reste bloqué partout par son erreur.
                    if agent.releve == "election":
                        # Un candidat refusé ne ferme que CE candidat : ni
                        # l'hôte (les agents épinglés y restent réclamables),
                        # ni les autres hôtes candidats.
                        add("placement-policy-violation", ERROR,
                            "%s on %s: %s" % (agent.title, host.title, reason), f,
                            candidate=agent.title, host=name)
                    else:
                        add("placement-policy-violation", ERROR,
                            "%s on %s: %s" % (agent.title, host.title, reason), f,
                            agent=agent.title)
                _check_work_dir(add, agent, host, placement)
    for name, group in by_agent.items():
        if len(group) > 1:
            for placement in group:
                add("placement-duplicate", ERROR,
                    "agent %s admitted %d times (%s)" % (
                        name, len(group), ", ".join(p.agent or "?" for p in group)),
                    placement.fiche, agent=name)
    # L165 (EP1) : les candidats d'un rôle élu sont des hôtes du canon, tous
    # admis par l'admission de l'agent. Sans candidat, le rôle ne peut être
    # tenu nulle part ; un candidat non admis serait refusé au verdict de
    # placement, donc la fiche est fautive dès `canon check`.
    for agent in canon.agents:
        if agent.releve != "election":
            if agent.candidats:
                add("agent-candidats-ignored", WARNING,
                    "`candidats` declared without `releve: election` (ignored)",
                    agent.fiche, agent=agent.title)
            continue
        if not agent.candidats:
            add("agent-election-candidats-missing", ERROR,
                "agent %s is `releve: election` without `candidats`" % agent.title,
                agent.fiche, agent=agent.title)
            continue
        admis: set[str] = set()
        for placement in by_agent.get(agent.title, []):
            admis |= set(placement.host_names())
            for tag in placement.tags():
                admis |= {host.title for host in canon.hosts if tag in (host.tags or [])}
        for name in agent.candidats:
            if canon.host(name) is None:
                add("agent-election-host-unknown", ERROR,
                    "agent %s is a candidate on unknown host %s" % (agent.title, name),
                    agent.fiche, agent=agent.title)
            elif name not in admis:
                add("agent-election-host-not-admitted", ERROR,
                    "agent %s is a candidate on %s, which its admission does not cover"
                    % (agent.title, name), agent.fiche, agent=agent.title)
    for host in canon.hosts:
        for agent in canon.agents:
            paths = {host.policy.work_dirs[n] for n in [agent.title] + list(agent.aliases or [])
                     if n in (host.policy.work_dirs or {})}
            if len(paths) > 1:
                add("host-work-dir-alias-conflict", ERROR,
                    "host %s declares different work directories for %s and its aliases"
                    % (host.title, agent.title), host.fiche, host=host.title, agent=agent.title)
        for name in sorted(host.policy.work_dirs or {}):
            if name not in agent_names:
                add("host-work-dir-agent-unknown", WARNING,
                    "policy of host %s: `workspaces.dirs.%s` names no agent of the canon"
                    % (host.title, name), host.fiche, host=host.title)
    # #3101 : une admission épinglée prend une place sur chaque hôte couvert ;
    # un rôle élu n'en prend qu'une, sur l'un de ses candidats (seul l'hôte
    # du bail le fait tourner).
    pinned, seats = admission_load(
        canon, {name: group.values() for name, group in by_host.items()})
    for host in canon.hosts:
        limit = host.policy.max_agents
        if limit is not None and pinned.get(host.title, 0) > limit:
            add("host-max-agents", ERROR,
                "host %s: %d admissions for max_agents = %d"
                % (host.title, pinned[host.title], limit), host.fiche, host=host.title)
    for role, seat in sorted(seats.items()):
        if seat is None:
            agent = canon.agent(role)
            add("agent-election-max-agents", ERROR,
                "elected agent %s: no candidate (%s) has a free place under max_agents"
                % (role, ", ".join(agent.candidats)), agent.fiche, agent=role)

    # -- politique de revue par classe (décision 0018 point 1, R19) ------------
    # La déclaration vit dans `federation.yaml`, sous `review_policies.classes`,
    # éventuellement par projet (`risk_classes.projects.<nom>`, §8) :
    # `canon check` la valide en entier, `ameesh review-class` la relit.
    root_member = canon.sources[0].member if canon.sources else ""
    from . import modules as project_modules
    resolution = None
    try:
        resolution = project_modules.resolve(canon.federation, canon)
    except ValueError as exc:
        add("project-modules-invalid", ERROR, str(exc),
            **({"member": root_member, "path": "federation.yaml"} if root_member else {}))
    if resolution is not None:
        # L98 T2a decision 3: a refused entry point no scope activates is a
        # warning; the same entry activated above is `project-modules-invalid`.
        for warning in resolution.warnings:
            if warning["code"] == "pipeline-module-duplicate":
                # L103 T2 (#4396): a Pipeline card taking the name of an
                # installed module (or of another card) is refused even when
                # no project selects it; it fails `canon check` without
                # blocking any agent (`pipeline` subject).
                card = canon.pipeline(warning["name"])
                add("pipeline-module-duplicate", ERROR, warning["message"],
                    card.fiche if card is not None else None, pipeline=warning["name"])
                continue
            add("module-refused", WARNING, warning["message"],
                **({"member": root_member, "path": "federation.yaml"} if root_member else {}))
    _scoped = review_mod.scoped_from_federation(canon.federation)
    for code, severity, message in _scoped.problems:
        add(code, severity, message,
            **({"member": root_member, "path": "federation.yaml"} if root_member else {}))

    # -- descripteurs de harnais (L16) --------------------------------------------------
    # Les diagnostics du dossier de l'hôte (chaîne d'approvisionnement) sont
    # remontés par `canon check` : un dossier non privé, un descripteur ignoré,
    # un id de l'hôte qui masque celui du paquet.
    for hf in harnesses.scan()[1]:
        add(hf.code, hf.severity, hf.message, path=hf.path or None)

    # -- plan de travail (L29) ----------------------------------------------------------
    _validate_packages(canon, add)

    # -- règles de travail (#538) -------------------------------------------------------
    _validate_processes(canon, add)

    # -- pipelines déclarés (L103, #4395) -----------------------------------------------
    _validate_pipelines(canon, add)

    # -- avertissements -----------------------------------------------------------------
    for agent in canon.agents:
        if agent.title not in by_agent:
            add("agent-unplaced", WARNING, "agent %s without placement" % agent.title,
                agent.fiche, agent=agent.title)
    for host in canon.hosts:
        if host.title not in by_host:
            add("host-unplaced", WARNING, "host %s without placement" % host.title,
                host.fiche, host=host.title)
    return findings


def _check_work_dir(add, agent: Agent, host: Host, placement: Placement) -> None:
    """Dossier de travail d'un agent admis sur un hôte (L31, 0029 ; L35).

    - la politique de l'hôte donne un dossier et l'admission porte un `cwd` :
      `admission-cwd-ignored` (la politique gagne) ;
    - elle n'en donne pas et l'admission porte un `cwd` :
      `admission-cwd-inherited` (repli transitoire, à migrer) ;
    - ni l'un ni l'autre : `host-work-dir-missing` (l'exécuteur n'aurait aucun
      dossier où lancer le harnais : la panne silencieuse de la bascule v1.3.0).
    """
    found = host.policy.work_dir(agent.team, agent.title)
    where = placement.fiche
    if found and placement.cwd:
        add("admission-cwd-ignored", WARNING,
            "`cwd` %r of the admission of %s ignored on %s: the host policy gives "
            "%s" % (placement.cwd, agent.title, host.title, found), where,
            agent=agent.title)
    elif placement.cwd:
        add("admission-cwd-inherited", WARNING,
            "%s on %s: cwd inherited from the Placement card (%s), transitional fallback — "
            "migrate to the host's `policy.workspaces.dirs` / `roots` (legacy `work_dirs` / "
            "`work_roots` still read; fallback removed in v1.5.0 at the latest)"
            % (agent.title, host.title, placement.cwd), where,
            agent=agent.title)
    elif not found:
        add("host-work-dir-missing", WARNING,
            "%s on %s: no working directory — declare `policy.workspaces.dirs`, "
            "`roots` or `root` in the host card" % (agent.title, host.title),
            host.fiche, agent=agent.title, host=host.title)


def _validate_packages(canon: Canon, add) -> None:
    """Fiches WorkPackage : identifiant, sorte, parent existant et cohérent,
    pas de cycle, responsable résolu. Leurs constats portent `package` : ils
    ne bloquent la réclamation d'aucun agent (voir `blocking`)."""
    by_id: dict[str, list[WorkPackage]] = defaultdict(list)
    for package in canon.packages:
        by_id[package.id].append(package)
    for ident, group in by_id.items():
        if len(group) > 1:
            for package in group:
                add("package-duplicate", ERROR,
                    "WorkPackage %s declared %d times (%s)" % (
                        ident, len(group),
                        ", ".join("%s:%s" % (g.fiche.member, g.fiche.path) for g in group)),
                    package.fiche, package=ident)
    for package in canon.packages:
        f, ident = package.fiche, package.id
        if not PACKAGE_ID_RE.match(ident):
            add("package-id-invalid", ERROR,
                "invalid WorkPackage identifier %r (letters, digits, . _ -, 64 "
                "characters at most)" % ident, f, package=ident)
        if package.kind not in PACKAGE_KINDS:
            add("package-kind-invalid", ERROR,
                "WorkPackage %s: `kind` %r (expected: %s)"
                % (ident, package.kind, " | ".join(PACKAGE_KINDS)), f, package=ident)
        if not package.responsible:
            add("package-responsible-missing", ERROR,
                "WorkPackage %s without `responsible` (a human is accountable for every item "
                "of the plan)" % ident, f, package=ident)
        elif canon.resolve_human(package.responsible) is None:
            add("package-responsible-unresolved", ERROR,
                "responsible %r of %s does not resolve to a human Member"
                % (package.responsible, ident), f, package=ident)
        if package.parent:
            parents = by_id.get(package.parent) or []
            if not parents:
                add("package-parent-unknown", ERROR,
                    "WorkPackage %s: unknown parent %r" % (ident, package.parent), f,
                    package=ident)
            elif package.kind in PACKAGE_KINDS and len(parents) == 1:
                allowed = PACKAGE_PARENTS[package.kind]
                if parents[0].kind not in allowed:
                    add("package-kind-incoherent", ERROR,
                        "WorkPackage %s (%s) under %s (%s): %s" % (
                            ident, package.kind, package.parent, parents[0].kind,
                            "a %s goes under %s" % (package.kind, " or ".join(allowed))
                            if allowed else "a milestone has no parent"),
                        f, package=ident)
        elif package.kind == "lot":
            add("package-lot-orphan", WARNING,
                "lot %s without parent (a lot goes under an epic or a milestone)" % ident,
                f, package=ident)
    # cycles : suivre les parents depuis chaque fiche
    reported: set[str] = set()
    for package in canon.packages:
        seen: list[str] = []
        current: str | None = package.id
        while current and current not in seen:
            seen.append(current)
            nxt = by_id.get(current) or []
            current = nxt[0].parent if len(nxt) == 1 else None
        if current and current == package.id and package.id not in reported:
            reported.update(seen)
            add("package-cycle", ERROR,
                "cycle in the plan: %s" % " → ".join(seen + [current]), package.fiche,
                package=package.id)


def _validate_processes(canon: Canon, add) -> None:
    """#538 : fiches Process (`processus/*.md`) — les règles de travail.

    Un texte plus long que `PROCESS_BODY_MAX` (environ deux pages) et une
    échéance dépassée sont des avertissements : ils ne bloquent la réclamation
    d'aucun agent (voir `blocking`). Les clés obligatoires sont vérifiées à la
    lecture (`_build_process`), qui marque la fiche invalide.

    #1372 : une carte longue qui porte une projection courte VALIDE n'est plus
    signalée — c'est la projection qui ouvre les briefs, le texte entier reste
    la source. Sans projection utilisable, `process-too-long` dit que le brief
    ne garde que le début et comment y remédier ; une projection illisible ou
    au-delà de son budget a son propre constat."""
    today = _dt.date.today().isoformat()
    for process in canon.processes:
        where = process.fiche
        subject = {"process": process.title}
        taille = len(process.body.encode("utf-8"))
        proj = len(process.projection.encode("utf-8"))
        if process.projection_error:
            add("process-projection-%s" % process.projection_error, WARNING,
                "Process card %s: unusable short projection (%s) — the whole text "
                "is injected and cut" % (process.title, process.projection_error),
                where, **subject)
        elif process.projection and proj > PROCESS_PROJECTION_MAX:
            add("process-projection-too-long", WARNING,
                "Process card %s: short projection of %d bytes (limit %s) — a "
                "brief cuts its end" % (process.title, proj, PROCESS_PROJECTION_MAX),
                where, **subject)
        elif not process.projection and taille > PROCESS_BODY_MAX:
            add("process-too-long", WARNING,
                "Process card %s: %d bytes of rules (limit %s, about two pages); "
                "a brief keeps only the beginning — add a short projection between "
                "`%s` and `%s` to inject the whole rules"
                % (process.title, taille, PROCESS_BODY_MAX,
                   PROCESS_PROJECTION_START, PROCESS_PROJECTION_END),
                where, **subject)
        if process.expire and process.expire < today:
            add("process-expired", WARNING,
                "Process card %s marked `expire: %s`, a past date: the rule is "
                "still read, review it" % (process.title, process.expire),
                where, **subject)
    _validate_process_block(canon, add)


def _validate_pipelines(canon: Canon, add) -> None:
    """L103 (#4395) : cartes Pipeline — nom unique dans le canon, responsable
    résolu en Member humain. Les constats portent `pipeline` : ils font
    échouer `canon check` sans bloquer aucun agent (voir `blocking`). La forme
    de la carte et son bloc sont vérifiés à la lecture (`_build_pipeline`)."""
    by_name: dict[str, list[Pipeline]] = defaultdict(list)
    for pipeline in canon.pipelines:
        if pipeline.name:
            by_name[pipeline.name].append(pipeline)
    for name, group in by_name.items():
        if len(group) > 1:
            for pipeline in group:
                add("pipeline-duplicate", ERROR,
                    "pipeline %s declared %d times (%s)" % (
                        name, len(group),
                        ", ".join("%s:%s" % (p.fiche.member, p.fiche.path) for p in group)),
                    pipeline.fiche, pipeline=name)
                pipeline.valid = False
    for pipeline in canon.pipelines:
        if pipeline.responsible and canon.resolve_human(pipeline.responsible) is None:
            add("pipeline-responsible-unresolved", ERROR,
                "Pipeline card %s: responsible %s is not a human Member of the canon "
                "(expected human:<id>)" % (pipeline.name, pipeline.responsible),
                pipeline.fiche, pipeline=pipeline.name)
            pipeline.valid = False


def _validate_process_block(canon: Canon, add) -> None:
    """#3867 : le bloc de règles RÉELLEMENT injecté tient-il dans sa place ?

    Le bloc porte toutes les cartes Process valides du canon (projection ou
    texte entier, titres et lignes de métadonnées, diagnostics) : chaque agent
    de ce canon reçoit le même. Il est construit par `reprise` (même code que
    le brief) et mesuré dans le cas le plus serré de la PLUS ANCIENNE version
    servie (`RULES_BLOC_LIMIT_COMPAT`, #4250), pas seulement de l'hôte local :
    pendant un staging, un hôte en retard coupe les briefs à l'ancienne limite
    alors que l'hôte récent valide la PR. Dès 90 % de la place : avertissement ;
    au-delà (le brief coupe la fin des règles) : erreur, qui ne bloque aucun
    agent (`blocking` : sujet Process) mais fait échouer `canon check`. Le
    constat nomme les cartes et leur poids, de la plus lourde à la plus légère."""
    from . import reprise
    servies = [p for p in canon.processes if p.valid]
    if not servies:
        return
    fragments, diagnostics = [], []
    for process in servies:
        texte, diag = reprise._process_fragment(process)
        if diag:
            diagnostics.append(diag)
        fragments.append((process, texte))
    rules = "\n\n".join(t for _, t in fragments if t).strip()
    if not rules:
        return
    bloc = reprise._rules_bloc(rules, RULES_BLOC_LIMIT_COMPAT, diagnostics)
    taille = len(bloc.encode("utf-8"))
    cartes = ", ".join(
        "%s (%d)" % (p.title, len(t.encode("utf-8")))
        for p, t in sorted(fragments, key=lambda f: -len(f[1].encode("utf-8"))))
    sujet = {"process": ", ".join(p.title for p, _ in fragments)}
    if rules not in bloc:
        add("process-rules-block-too-long", ERROR,
            "Process rules block: %d bytes of rules for %d bytes of room (oldest "
            "served version's brief block limit %d) — a brief with a full text cuts "
            "the end of the rules; shorten the cards: %s"
            % (len(rules.encode("utf-8")), RULES_BLOC_ROOM_COMPAT,
               RULES_BLOC_LIMIT_COMPAT, cartes),
            servies[0].fiche, **sujet)
    elif taille * 10 >= RULES_BLOC_LIMIT_COMPAT * 9:
        add("process-rules-block-near-limit", WARNING,
            "Process rules block: %d bytes of %d for the oldest served version (at "
            "least 90%%) — shorten the cards before a brief cuts the end of the "
            "rules: %s"
            % (taille, RULES_BLOC_LIMIT_COMPAT, cartes), servies[0].fiche, **sujet)


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == ERROR]


@dataclass
class Blocking:
    """Qui une erreur empêche de réclamer (un canon invalide bloque, §4.1)."""

    global_errors: list[Finding]
    by_agent: dict[str, list[Finding]]
    by_host: dict[str, list[Finding]]
    #: L165 : erreurs qui ne ferment qu'un couple (agent élu, hôte candidat).
    by_candidate: dict[tuple[str, str], list[Finding]]

    def reasons(self, agent: str, hosts: Iterable[str] = ()) -> list[str]:
        out = ["%s (%s)" % (f.code, f.where()) for f in self.global_errors]
        out += ["%s (%s)" % (f.code, f.where()) for f in self.by_agent.get(agent, [])]
        for host in hosts:
            out += ["%s (%s)" % (f.code, f.where()) for f in self.by_host.get(host, [])]
            out += ["%s (%s)" % (f.code, f.where())
                    for f in self.by_candidate.get((agent, host), [])]
        return list(dict.fromkeys(out))


def blocking(findings: Iterable[Finding]) -> Blocking:
    """Une erreur liée à un agent bloque cet agent ; à un hôte, les agents qui y
    sont placés ; à une fiche du plan (WorkPackage, L29) ou à une règle de
    travail (Process, #538) ou à une carte Pipeline (L103), aucun agent ; sinon
    (fédération, frontmatter d'une fiche du profil, …) tous."""
    result = Blocking([], defaultdict(list), defaultdict(list), defaultdict(list))
    for finding in errors(findings):
        if finding.package or finding.process or finding.pipeline:
            continue
        if finding.candidate and finding.host:
            result.by_candidate[(finding.candidate, finding.host)].append(finding)
        elif finding.agent:
            result.by_agent[finding.agent].append(finding)
        elif finding.host:
            result.by_host[finding.host].append(finding)
        else:
            result.global_errors.append(finding)
    return result
