# SPDX-License-Identifier: AGPL-3.0-only
"""Le canon OKF : fiches Agent, Host, Placement, Member, WorkPackage ; validation (C2, C3).

Spec §4. ameesh ne lit que le **frontmatter** des fichiers `.md` d'un bundle
OKF (hors `index.md` et `log.md`) et n'écrit jamais dans le canon.

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

import dataclasses
import datetime as _dt
import fnmatch
import hashlib
import os
import posixpath
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import harnesses
from . import review as review_mod
from .config import NAME_RE

try:  # facultatif : jamais requis
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover - dépend de l'environnement
    _yaml = None

PROFILE_TYPES = ("Agent", "Host", "Placement", "Member", "WorkPackage")
#: les sortes de fiches WorkPackage (plan de travail, L29) et les parents admis
PACKAGE_KINDS = ("milestone", "epic", "lot")
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
KNOWN_CAPABILITIES = ("read", "report-drift", "propose")
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
SCOPE_FIELDS = ("scope",)


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
                    raise YamlError("ligne %d : clé en double %r"
                                    % (key_node.start_mark.line + 1, key))
                seen.add(key)
            except TypeError:
                raise YamlError("ligne %d : clé non scalaire" % (key_node.start_mark.line + 1))
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
                raise YamlError("PyYAML indisponible")
            try:
                return _normalize(_yaml.load(text, Loader=_StrictLoader))  # noqa: S506 (SafeLoader)
            except _yaml.YAMLError as exc:
                raise YamlError(" ".join(str(exc).split())) from exc
        return _MiniYaml(text).parse()
    except RecursionError as exc:
        raise YamlError("imbrication trop profonde") from exc


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
        raise YamlError("valeur non prise en charge : %r" % text[:40])
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
            raise YamlError("échappement inconnu : \\%s" % nxt)
        if c == '"':
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise _Incomplete("chaîne non terminée")


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
            raise YamlError("contenu après une collection en ligne : %r" % self.s[self.i:][:40])
        return value

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def peek(self) -> str:
        if self.i >= len(self.s):
            raise _Incomplete("collection en ligne non terminée")
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
                raise YamlError("alias inconnu")
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
                raise YamlError("commentaire dans une collection en ligne")
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
                raise YamlError("« , » ou « ] » attendu dans une liste en ligne")

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
                raise YamlError("clé non scalaire dans un mapping en ligne")
            self.ws()
            value = None
            if self.peek() == ":":
                self.i += 1
                self.ws()
                if self.peek() not in ",}":
                    value = self.value()
            if key in out:
                raise YamlError("clé en double %r" % key)
            out[key] = value
            self.ws()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "}":
                raise YamlError("« , » ou « } » attendu dans un mapping en ligne")


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
                raise YamlError("ligne %d : tabulation dans l'indentation" % no)
            content = _strip_comment(stripped).rstrip()
            if indent == 0 and content in ("---", "..."):
                if self.lines and any(line.text for line in self.lines):
                    if content == "...":
                        break
                    raise YamlError("ligne %d : plusieurs documents" % no)
                continue
            self.lines.append(_Line(no, indent, content, raw))

    def parse(self) -> Any:
        i = self._skip(0)
        if i >= len(self.lines):
            return None
        value, i = self._node(i)
        i = self._skip(i)
        if i < len(self.lines):
            raise YamlError("ligne %d : contenu inattendu" % self.lines[i].no)
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
                raise YamlError("ligne %d : indentation inattendue" % line.no)
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
                raise YamlError("ligne %d : indentation inattendue" % line.no)
            if _is_item(line.text):
                raise YamlError("ligne %d : élément de liste inattendu" % line.no)
            pair = _split_key(line.text)
            if pair is None:
                raise YamlError("ligne %d : « clé: valeur » attendu" % line.no)
            raw_key, rest = pair
            if raw_key and raw_key[0] in "'\"":
                key, end = _quoted(raw_key, 0)
                if raw_key[end:].strip():
                    raise YamlError("ligne %d : clé illisible" % line.no)
            else:
                key = raw_key
            if not key:
                raise YamlError("ligne %d : clé vide" % line.no)
            if key in out:
                raise YamlError("ligne %d : clé en double %r" % (line.no, key))
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
                        raise YamlError("ligne %d : collection en ligne non terminée" % line.no)
                    text = text + " " + self.lines[j].text
        if text[0] in "'\"":
            try:
                value, end = _quoted(text, 0)
            except _Incomplete:
                raise YamlError("ligne %d : chaîne sur plusieurs lignes non prise en charge"
                                % line.no)
            if text[end:].strip():
                raise YamlError("ligne %d : contenu après une chaîne" % line.no)
            return value, i + 1
        if text[0] == "*":
            name = text[1:].strip()
            if name not in self.anchors:
                raise YamlError("ligne %d : alias inconnu %r" % (line.no, name))
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
                raise YamlError("ligne %d : structure inattendue après un scalaire" % nxt.no)
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
    raise YamlError("frontmatter non fermé (ou plus grand que %d Kio)" % (FRONTMATTER_MAX // 1024))


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
    untrusted: bool = False  # lu hors source approuvée
    package: str = ""       # fiche WorkPackage concernée (ne bloque aucun agent)

    def to_dict(self) -> dict:
        out = {"code": self.code, "severity": self.severity, "member": self.member,
               "path": self.path, "message": self.message}
        if self.agent:
            out["agent"] = self.agent
        if self.host:
            out["host"] = self.host
        if self.package:
            out["package"] = self.package
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
        return "%s : fichiers de travail NON APPROUVÉS (%s)" % (self.member, self.directory)

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


@dataclass
class Member:
    title: str
    roles: list[str] | None
    authenticators: list | None
    fiche: Fiche


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


@dataclass
class HostPolicy:
    harnesses: list[str] | None = None
    providers: list[str] | None = None
    models: list[str] | None = None
    credential_modes: list[str] | None = None
    max_agents: int | None = None
    #: seuils de ressources de l'hôte (L31, 0028) : mapping
    #: `{min_mem_available, max_swap_used, max_load, min_disk_free}` ; None =
    #: valeurs par défaut prudentes (`ameesh.resources`).
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
    #: hôte volatil (L112) : appareil prêté qui peut disparaître à tout
    #: moment (VM Compute) ; son bail est court, 90 s par défaut
    volatile: bool = False
    #: durée du bail des agents de cet hôte (secondes, L112) ; None = 90 s
    #: pour un hôte volatil, sinon la configuration de l'exécuteur
    lease_ttl: float | None = None

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
        return "chemin vide"
    unknown = [m for m in _WORK_TEMPLATE_RE.findall(path) if m not in WORK_TEMPLATE_FIELDS]
    if unknown:
        return "gabarit inconnu %s (seul {agent} est admis)" % ", ".join(
            "{%s}" % u for u in unknown)
    if "{" in _WORK_TEMPLATE_RE.sub("", path) or "}" in _WORK_TEMPLATE_RE.sub("", path):
        return "accolade orpheline"
    return None


#: provenance d'un dossier de travail (L35), pour les diagnostics
WORK_FROM_HOST = ("work_dirs", "work_roots", "work_root")
WORK_FROM_PLACEMENT = "placement"


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
        found = policy.work_dir(agent.team, agent.title)
        if found:
            source = ("work_dirs" if agent.title in dirs else
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


@dataclass
class Canon:
    root: str
    sources: list[Source] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    hosts: list[Host] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    packages: list[WorkPackage] = field(default_factory=list)
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
                              ", par défaut" if self.is_default else "")

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

    def placements_of(self, agent: str) -> list[Placement]:
        return [p for p in self.placements if p.agent == agent]

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
        return "; ".join(source.describe() for source in self.sources) or "aucune source lue"

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
            "members": [dict(title=m.title, roles=m.roles, **fiche(m.fiche))
                        for m in self.members],
            "hosts": [dict(title=h.title, responsible=h.responsible, tags=h.tags,
                           admins=h.admins,
                           policy=h.policy.__dict__.copy(), **fiche(h.fiche))
                      for h in self.hosts],
            "agents": [dict(title=a.title, responsible=a.responsible, team=a.team,
                            capabilities=a.capabilities, harness=a.harness, model=a.model,
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
            "federation": {"id": (self.federation or {}).get("id"),
                           "roles": self.roles, "review_policies": self.review_policies},
            # L43 (0031) : périmètre ameesh par membre, fiches ignorées hors périmètre
            "ameesh_scope": {member: list(dirs) for member, dirs in sorted(self.scopes.items())},
            "ameesh_scope_unread": sorted(self.scope_unread),
            "out_of_scope": dict(sorted(self.out_of_scope.items())),
        }


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
        ("harnesses", "harnais", _get(agent, "harness"), False),
        ("providers", "fournisseur", _get(agent, "provider"), False),
        ("models", "modèle", _get(agent, "model"), True),
        ("credential_modes", "mode d'identifiants", mode, False),
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
        out.append("%s %s non admis par l'hôte %s (admis : %s)" % (
            label, text if text is not None else "non déclaré", host_name,
            ", ".join(allowed) or "aucun"))
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


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _GIT_ENV_DROP and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               LC_ALL="C")
    return env


def _git(repo: str, *args: str, stdin: bytes | None = None,
         timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """git sans crochets, sans fsmonitor, sans objets de remplacement, sans pager."""
    cmd = ["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
           "-c", "core.fsmonitor=false", "-c", "core.hooksPath=%s" % os.devnull,
           *args]
    try:
        return subprocess.run(cmd, cwd=repo, input=stdin, capture_output=True,
                              env=_git_env(), timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise GitError("git introuvable") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("git %s : délai dépassé" % args[0]) from exc
    except OSError as exc:
        # Dossier du dépôt inaccessible (permission refusée, n'est plus un
        # dossier…) : le canon est illisible, jamais une exception qui
        # empêcherait « canon sync » d’enregistrer l’état fermé.
        raise GitError("git %s : %s" % (args[0], exc.strerror or exc)) from exc


def _git_out(repo: str, *args: str) -> str | None:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def _git_message(proc: subprocess.CompletedProcess) -> str:
    """Le message d'erreur de git, ou son code de sortie s'il est muet."""
    text = proc.stderr.decode("utf-8", "replace").strip()
    return text or ("code %d" % proc.returncode)


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
            raise GitError("référence git inconnue : %s" % ref)
        proc = _git(repo, "merge-base", verified, "HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            base = proc.stdout.decode("utf-8", "replace").strip()
        else:
            base = verified
    proc = _git(repo, "diff", "--name-status", "-z", "--find-renames", base)
    if proc.returncode != 0:
        raise GitError("git diff %s : %s" % (base, _git_message(proc)))
    fields = proc.stdout.decode("utf-8", "replace").split("\0")
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
    proc = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    if proc.returncode != 0:
        raise GitError("git ls-files : %s" % _git_message(proc))
    paths.update(name for name in proc.stdout.decode("utf-8", "replace").split("\0") if name)
    return sorted(paths)


def git_toplevel(directory: str) -> str | None:
    """Racine du dépôt git qui contient `directory`, ou None."""
    try:
        out = _git_out(directory, "rev-parse", "--show-toplevel")
    except GitError:
        return None
    return os.path.realpath(out) if out else None


def git_commit(repo: str, rev: str) -> str | None:
    if not rev or rev.startswith("-"):
        return None
    return _git_out(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                    rev + "^{commit}")


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


def _git_list(repo: str, commit: str, prefix: str) -> list[tuple[str, str]]:
    """[(chemin dans le dépôt, sha du blob)] des fichiers ordinaires sous `prefix`."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if prefix:
        args += ["--", prefix]
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise GitError("git ls-tree : %s" % proc.stderr.decode("utf-8", "replace").strip())
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


def _git_blobs(repo: str, shas: Iterable[str]) -> dict[str, bytes]:
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    proc = _git(repo, "cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode())
    if proc.returncode != 0:
        raise GitError("git cat-file : %s" % proc.stderr.decode("utf-8", "replace").strip())
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
        raise GitError("git cat-file : sortie illisible (%s)" % exc) from exc
    return out


def _git_read(repo: str, commit: str, path: str) -> bytes | None:
    proc = _git(repo, "cat-file", "blob", "%s:%s" % (commit, path))
    return proc.stdout if proc.returncode == 0 else None


def git_read(repo: str, commit: str, path: str) -> bytes | None:
    """Contenu de `path` au commit `commit` (objets git), ou None."""
    if not _SHA_RE.fullmatch(commit or ""):
        return None
    try:
        return _git_read(repo, commit, path)
    except GitError:
        return None


def git_remotes(repo: str) -> list[str]:
    try:
        return (_git_out(repo, "remote") or "").split()
    except GitError:
        return []


_BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")


def remote_branch(repo: str, name: str) -> str | None:
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
    elif "/" in text and text.split("/", 1)[0] in git_remotes(repo):
        short = text
    else:
        short = "%s/%s" % (DEFAULT_REMOTE, text)
    if (not _BRANCH_RE.fullmatch(short) or ".." in short or "//" in short
            or short.endswith(("/", ".lock", ".")) or "/" not in short):
        return None
    return short


def _remote_of(repo: str, rev: str) -> str:
    remotes = (_git_out(repo, "remote") or "").split()
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


def git_fetch(repo: str, remote: str) -> str:
    """`git fetch` sans crochets ; renvoie '' ou le message d'erreur."""
    try:
        proc = _git(repo, "fetch", "--quiet", "--no-tags", "--", remote, timeout=FETCH_TIMEOUT)
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
        return None, "dossier ou liste non vide de dossiers attendue"
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            return None, "dossier vide"
        text = item.strip().replace("\\", "/")
        norm = posixpath.normpath(text)
        if text.startswith("/") or norm == ".." or norm.startswith("../"):
            return None, "%r : dossier relatif au bundle attendu (ni absolu, ni `..`)" % item
        if any(ch in norm for ch in "*?[]{}"):
            return None, "%r : motif non admis (un dossier, pas un motif)" % item
        if norm != "." and any(part.startswith(".") for part in norm.split("/")):
            return None, "%r : dossier caché (jamais lu)" % item
        norm = "" if norm == "." else norm
        if norm not in out:
            out.append(norm)
    return out, None


def in_scope(relpath: str, scope: Iterable[str] | None) -> bool:
    """Le chemin (relatif au bundle) est-il dans le périmètre ? None = tout."""
    if scope is None:
        return True
    rel = relpath.replace(os.sep, "/")
    return any(s == "" or rel == s or rel.startswith(s + "/") for s in scope)


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
                 use_pyyaml: bool | None, default: bool = True):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.ref = ref
        self.allow_untrusted = untrusted
        self.fetch = fetch
        self.use_pyyaml = use_pyyaml
        self.canon = Canon(root=self.root, is_default=default)
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
            error = git_fetch(repo, _remote_of(repo, rev))
            if error:
                self.add("canon-fetch-failed", WARNING,
                         "git fetch a échoué (%s) : lecture de la dernière révision connue"
                         % error.splitlines()[0][:200], member=member)
        commit = git_commit(repo, rev)
        if not commit:
            self.add("canon-ref-missing", ERROR,
                     "révision canonique %s introuvable dans %s (git fetch ? %s)"
                     % (rev, repo, "AMEESH_CANON_REF" if explicit
                        else "ou AMEESH_CANON_REF pour une autre branche"),
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
        head = git_commit(source.repo, "HEAD")
        if head and head != source.commit:
            self.add("canon-local-divergence", WARNING,
                     "le clone local (HEAD %s) diffère de %s (%s) : seul %s est lu"
                     % (head[:12], source.rev, source.commit[:12], source.commit[:12]),
                     member=source.member, path=source.prefix)
        args = ["status", "--porcelain", "--untracked-files=normal", "--", source.prefix or "."]
        proc = _git(source.repo, *args)
        if proc.returncode == 0 and proc.stdout.strip():
            self.add("canon-worktree-dirty", WARNING,
                     "modifications locales non commitées sous %s : ignorées (seul %s est lu)"
                     % (source.prefix or ".", source.commit[:12]),
                     member=source.member, path=source.prefix)

    def _fs_source(self, member: str, directory: str) -> Source | None:
        if not self.allow_untrusted:
            self.add("canon-untrusted-refused", ERROR,
                     "%s n'est pas un dépôt git : canon non approuvé, refusé "
                     "(AMEESH_CANON_UNTRUSTED=1 pour les tests et prototypes)" % directory,
                     member=member)
            return None
        self.add("canon-untrusted", WARNING,
                 "%s lu depuis les fichiers de travail : canon NON APPROUVÉ "
                 "(AMEESH_CANON_UNTRUSTED=1)" % directory, member=member)
        return Source(member=member, directory=directory, mode="untrusted")

    def _source(self, member: str, directory: str, rev: str, explicit: bool,
                same_repo_commit: tuple[str, str, str] | None = None) -> Source | None:
        try:
            repo = git_toplevel(directory)
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
        actual = _git_out(source.repo, "remote", "get-url", "--", remote) or ""
        if _norm_url(actual) != _norm_url(declared):
            self.add("member-repository-mismatch", WARNING,
                     "le dépôt %s de %s (%s) n'est pas celui du manifeste (%s)"
                     % (remote, source.member, actual or "absent", declared),
                     member=source.member)

    def _read_manifest(self, source: Source) -> bytes | None:
        if source.mode == "git":
            path = (source.prefix + "/" if source.prefix else "") + "federation.yaml"
            return _git_read(source.repo, source.commit, path)
        path = os.path.join(source.directory, "federation.yaml")
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as fh:
                return fh.read(FRONTMATTER_MAX * 4)
        return None

    def _parse_manifest(self, raw: bytes, member: str) -> dict | None:
        try:
            manifest = load_yaml(raw.decode("utf-8"), use_pyyaml=self.use_pyyaml)
        except (UnicodeDecodeError, YamlError) as exc:
            self.add("federation-invalid", ERROR, "federation.yaml illisible : %s" % exc,
                     member=member, path="federation.yaml")
            return None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("members", []), list):
            self.add("federation-invalid", ERROR,
                     "federation.yaml : mapping avec une liste `members` attendu",
                     member=member, path="federation.yaml")
            return None
        return manifest

    # -- chargement --------------------------------------------------------------
    def _bootstrap_rev(self) -> str:
        """Révision où lire le manifeste quand AMEESH_CANON_REF n'est pas posé."""
        repo = git_toplevel(self.root)
        default = "%s/%s" % (DEFAULT_REMOTE, DEFAULT_BRANCH)
        if repo is None:
            return default
        if self.fetch and repo not in self.fetched:
            self.fetched.add(repo)
            error = git_fetch(repo, DEFAULT_REMOTE)
            if error:
                self.add("canon-fetch-failed", WARNING,
                         "git fetch a échoué (%s) : lecture de la dernière révision connue"
                         % error.splitlines()[0][:200], member="canon")
        for rev in (default, "%s/HEAD" % DEFAULT_REMOTE):
            if git_commit(repo, rev):
                return rev
        return default

    def load(self) -> Canon:
        if not os.path.isdir(self.root):
            self.add("canon-missing", ERROR, "racine du canon introuvable : %s" % self.root)
            return self.canon
        explicit = bool(self.ref)
        rev = self.ref or self._bootstrap_rev()
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
                self.add("federation-invalid", ERROR, "membre sans `id` dans federation.yaml",
                         member=root.member, path="federation.yaml")
                continue
            member = str(entry["id"])
            if member in seen:
                if member != root.member:
                    self.add("federation-invalid", ERROR, "membre %s en double" % member,
                             member=root.member, path="federation.yaml")
                continue
            seen.add(member)
            hint = entry.get("workspace_path")
            if not hint:
                self.add("member-absent", WARNING,
                         "membre %s sans workspace_path : non lu" % member, member=member)
                continue
            bundle = str(entry.get("bundle") or ".")
            checkout = os.path.join(workspace, str(hint))
            directory = os.path.normpath(os.path.join(checkout, bundle))
            if (not _relative_ok(str(hint)) or not _relative_ok(bundle)
                    or not _within(workspace, checkout) or not _within(checkout, directory)):
                self.add("member-path-escape", ERROR,
                         "membre %s : chemin hors du dossier de travail (%s/%s)"
                         % (member, hint, bundle), member=member)
                continue
            if not os.path.isdir(directory):
                self.add("member-absent", WARNING,
                         "membre %s absent localement (%s) : non lu" % (member, directory),
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
                         "`%s` : mapping attendu (`%s: {scope: <dossier>}`) — fiches ameesh "
                         "de %s non lues" % (label, label, root.member),
                         member=root.member, path="federation.yaml")
                self.canon.scope_unread.add(root.member)
                ext = {}
            root_part = {k: v for k, v in ext.items() if k != "members"}
            if root_part:
                declared.append((root.member, root_part, label))
            members = ext.get("members")
            if members is not None and not isinstance(members, dict):
                self.add("ameesh-scope-invalid", ERROR,
                         "`%s.members` : mapping attendu (`<id>: {scope: …}`)" % label,
                         member=root.member, path="federation.yaml")
                members = {}
            known = {str(e.get("id")) for e in manifest["members"] if isinstance(e, dict)}
            for member, raw in sorted((members or {}).items()):
                member = str(member)
                if member == root.member:
                    self.add("ameesh-scope-ignored-key", WARNING,
                             "`%s.members.%s` ignoré : le périmètre du bundle racine se "
                             "déclare dans `%s.scope`" % (label, member, label),
                             member=root.member, path="federation.yaml")
                    continue
                if member not in known:
                    self.add("ameesh-key-unknown", WARNING,
                             "`%s.members.%s` : membre absent de `members` (ignoré)"
                             % (label, member), member=root.member, path="federation.yaml")
                    continue
                declared.append((member, raw, "%s.members.%s" % (label, member)))
        if SCOPE_KEY in manifest:
            self.add("ameesh-scope-legacy", WARNING,
                     "`%s:` au premier niveau de federation.yaml n'est pas conforme au schéma "
                     "OKF Federation : écrire `extensions: {%s: {scope: …}}`"
                     % (SCOPE_KEY, SCOPE_KEY), member=root.member, path="federation.yaml")
            if ext is None:
                declared.append((root.member, manifest.get(SCOPE_KEY), SCOPE_KEY))
        for entry in manifest["members"]:
            if not isinstance(entry, dict) or not entry.get("id") or SCOPE_KEY not in entry:
                continue
            member = str(entry["id"])
            if member == root.member:
                self.add("ameesh-scope-ignored-key", WARNING,
                         "`members[%s].%s` ignoré : le périmètre du bundle racine se déclare "
                         "dans `extensions.%s.scope`" % (member, SCOPE_KEY, SCOPE_KEY),
                         member=root.member, path="federation.yaml")
                continue
            self.add("ameesh-scope-legacy", WARNING,
                     "`members[%s].%s` n'est pas conforme au schéma OKF Federation : écrire "
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
                         "`%s` : mapping attendu (`%s: {scope: <dossier>}`) — fiches ameesh "
                         "de %s non lues" % (label, SCOPE_KEY, member), **where)
                self.canon.scope_unread.add(member)
                continue
            for unknown in sorted(set(map(str, raw)) - set(SCOPE_FIELDS)):
                self.add("ameesh-key-unknown", WARNING,
                         "`%s.%s` : clé inconnue (ignorée)" % (label, unknown), **where)
            scope, problem = parse_scope(raw.get("scope"))
            if problem:
                self.add("ameesh-scope-invalid", ERROR,
                         "`%s.scope` : %s — fiches ameesh de %s non lues"
                         % (label, problem, member), **where)
                self.canon.scope_unread.add(member)
                continue
            if scope is not None:
                self.canon.scopes[member] = scope

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
                 "périmètre ameesh %s absent de %s : fiches ameesh de %s non lues"
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
            listed = _git_list(source.repo, source.commit, source.prefix)
            if not listed:
                self.add("canon-bundle-missing", ERROR,
                         "%s absent de %s (%s) : rien à lire" % (
                             source.prefix or "le bundle", source.rev, source.commit[:12]),
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
            blobs = _git_blobs(source.repo, [sha for _p, _r, sha in entries])
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
                self.add("canon-git-error", ERROR, "lecture de %s : %s" % (source.member, exc),
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
                self._fiche(source, path, content, version)
            if ignored:
                self.canon.out_of_scope[source.member] = len(ignored)
                self.add("ameesh-scope-ignored", INFO,
                         "%d fiche(s) typée(s) Agent/Host/Placement/Member/WorkPackage hors du "
                         "périmètre ameesh (%s) ignorée(s), p. ex. %s"
                         % (len(ignored), ", ".join(s or "." for s in scope),
                            ", ".join(ignored[:3])),
                         member=source.member, path=source.prefix)

    def _fiche(self, source: Source, path: str, content: bytes | None, version: str) -> None:
        where = {"member": source.member, "path": path}
        if content is None:
            self.add("canon-git-error", ERROR, "objet git illisible", **where)
            return
        head = content[:FRONTMATTER_MAX]
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            text = head.decode("utf-8", "replace")
            typed = bool(_TYPED_RE.search(text))
            self.add("frontmatter-invalid", ERROR if typed else WARNING,
                     "fichier non UTF-8", **where)
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
            self.add("frontmatter-invalid", ERROR if typed else WARNING,
                     "frontmatter illisible : %s%s" % (
                         exc, "" if typed else " (type inconnu : fiche ignorée)"),
                     **where, **subject)
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind not in PROFILE_TYPES:
            return
        title = _text(data.get("title"))
        fiche = Fiche(type=kind, title=title or "", member=source.member, path=path,
                      ref=make_ref(self.canon, source.member, path, version), data=data,
                      untrusted=source.mode != "git")
        if not title and kind != "Placement":
            subject = {"package": _package_stem(path)} if kind == "WorkPackage" else {}
            self.add("fiche-title-missing", ERROR, "fiche %s sans `title`" % kind, **where,
                     **subject)
            return
        build = getattr(self, "_build_" + kind.lower())
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
        self.add(code, ERROR, "`%s` : liste de textes attendue" % key, **where, **subject)
        return None

    def _build_member(self, fiche: Fiche, where: dict) -> None:
        roles = self._list(fiche, "roles", where, "member-invalid")
        authenticators = fiche.data.get("authenticators")
        self.canon.members.append(Member(
            title=fiche.title, roles=roles,
            authenticators=authenticators if isinstance(authenticators, list) else None,
            fiche=fiche))

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
                         "budget_usd_per_day illisible : %r (ignoré)" % (budget,), **where,
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
                         "priority illisible : %r (0 par défaut)" % (priority,), **where,
                         **subject)
                priority = None
        memory = fiche.data.get("memory")
        memory_repository = None
        if memory is not None:
            if not isinstance(memory, dict):
                self.add("agent-memory-invalid", WARNING,
                         "`memory` : mapping attendu (ignoré)", **where, **subject)
            else:
                memory_repository = _text(memory.get("repository"))
        self.canon.agents.append(Agent(
            title=fiche.title,
            responsible=_text(fiche.data.get("responsible")),
            team=_text(fiche.data.get("team")),
            capabilities=self._list(fiche, "capabilities", where, "agent-capabilities-invalid",
                                    **subject),
            harness=_text(fiche.data.get("harness")),
            model=_text(fiche.data.get("model")),
            provider=_text(fiche.data.get("provider")),
            credential_mode=_text(fiche.data.get("credential_mode")),
            budget_usd_per_day=budget,
            tools=self._list(fiche, "tools", where, "agent-tools-invalid", **subject),
            reviewers=self._list(fiche, "reviewers", where, "agent-reviewers-invalid", **subject),
            priority=0 if priority is None else priority,
            memory_repository=memory_repository,
            fiche=fiche,
        ))

    def _build_host(self, fiche: Fiche, where: dict) -> None:
        subject = {"host": fiche.title}
        raw = fiche.data.get("policy")
        policy = HostPolicy()
        if raw is not None and not isinstance(raw, dict):
            self.add("host-policy-invalid", ERROR, "`policy` : mapping attendu", **where,
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
                             "`policy.%s` : liste de textes attendue" % key, **where, **subject)
                    value = []
                setattr(policy, key, [str(v).strip() for v in value])
            if raw.get("max_agents") is not None:
                try:
                    policy.max_agents = int(raw["max_agents"])
                    if policy.max_agents < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    self.add("host-policy-invalid", ERROR,
                             "`policy.max_agents` : entier positif attendu", **where, **subject)
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
            # Seuils de ressources (L31, 0028) : un mapping dont chaque clé est
            # validée ici ; une clé illisible est une erreur (fail closed sur le
            # seuil, qui retombe alors sur sa valeur par défaut prudente).
            if raw.get("resources") is not None:
                policy.resources = self._host_resources(raw["resources"], where, subject)
            # Racines de travail (L31, 0029) : le dossier de travail d'une
            # session est un réglage de l'hôte, plus du placement.
            # L35 : `work_dirs` (par agent) et le gabarit `{agent}` ; tout
            # chemin déclaré doit être un texte non vide au gabarit connu (un
            # chemin refusé n'est pas retenu : fail closed, pas de dossier).
            if raw.get("work_root") is not None:
                problem = work_template_problem(raw.get("work_root"))
                if problem:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.work_root` : %s" % problem, **where, **subject)
                else:
                    policy.work_root = str(raw["work_root"]).strip()
            for key, label in (("work_roots", "projet"), ("work_dirs", "agent")):
                mapping = raw.get(key)
                if mapping is None:
                    continue
                if not isinstance(mapping, dict) or not mapping:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.%s` : mapping {%s: chemin} attendu" % (key, label),
                             **where, **subject)
                    continue
                kept: dict = {}
                for k, v in mapping.items():
                    problem = ("clé vide" if not isinstance(k, str) or not k.strip()
                               else work_template_problem(v))
                    if problem:
                        self.add("host-policy-invalid", ERROR,
                                 "`policy.%s.%s` : %s" % (key, k, problem),
                                 **where, **subject)
                        continue
                    kept[k.strip()] = v.strip()
                if kept:
                    setattr(policy, key, kept)
        tags = self._list(fiche, "tags", where, "host-tags-invalid", **subject)
        admins = self._list(fiche, "admins", where, "host-admins-invalid", **subject)
        self.canon.hosts.append(Host(title=fiche.title,
                                     responsible=_text(fiche.data.get("responsible")),
                                     policy=policy, tags=tags, admins=admins, fiche=fiche))

    def _host_resources(self, raw, where: dict, subject: dict) -> dict | None:
        """Seuils `policy.resources` validés, ou None si rien n'est déclaré.

        Chaque clé illisible est une erreur de `canon check` ; les clés
        inconnues sont conservées avec un avertissement (OKF tolère les clés
        inconnues, mais pas dans une politique qui gouverne des tours)."""
        from . import resources as resources_mod  # import tardif : pas de cycle

        if not isinstance(raw, dict):
            self.add("host-policy-invalid", ERROR,
                     "`policy.resources` : mapping attendu", **where, **subject)
            return None
        parsed: dict = {}
        for key in resources_mod.THRESHOLD_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if key == "max_load":
                try:
                    number = float(value)
                    if number < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.max_load` : nombre positif attendu",
                             **where, **subject)
                    continue
                parsed[key] = number
            else:
                octets = resources_mod.parse_bytes(value)
                if octets is None:
                    self.add("host-policy-invalid", ERROR,
                             "`policy.resources.%s` : taille en octets attendue (ex. 1GiB)"
                             % key, **where, **subject)
                    continue
                parsed[key] = octets
        for unknown in sorted(set(raw) - set(resources_mod.THRESHOLD_KEYS)):
            self.add("host-resources-unknown", WARNING,
                     "`policy.resources.%s` : clé inconnue (ignorée)" % unknown,
                     **where, **subject)
        return parsed or None

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
        self.canon.packages.append(WorkPackage(
            id=ident, title=fiche.title,
            kind=_text(fiche.data.get("kind")),
            parent=_text(fiche.data.get("parent")),
            responsible=_text(fiche.data.get("responsible")),
            team=_text(fiche.data.get("team")),
            scope=scope,
            status=_text(fiche.data.get("status")),
            fiche=fiche))


def _package_stem(path: str) -> str:
    """Identifiant par défaut d'une fiche WorkPackage : son nom de fichier."""
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


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
        "%s : identifié par le nom de son dossier, « %s » (déclarer `id` dans "
        "federation.yaml%s)" % (
            canon.root, canon.id,
            " : %r hors de la grammaire [A-Za-z0-9._-]" % (raw,) if raw not in (None, "")
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
         use_pyyaml: bool | None = None, default: bool = True) -> Canon:
    """Lit le canon (§4.1). Les problèmes de lecture sont dans `canon.load_findings`.

    Une erreur git imprévue (dépôt cassé, délai dépassé) rend le canon
    illisible (`readable` faux) au lieu de remonter : `canon sync` peut alors
    enregistrer l'état `unreadable`.

    `default` (L42) : faux pour un canon qui n'est pas le premier configuré —
    ses références sont alors préfixées de son identifiant.
    """
    loader = _Loader(root, ref=ref, untrusted=untrusted, fetch=fetch, use_pyyaml=use_pyyaml,
                     default=default)
    try:
        return loader.load()
    except GitError as exc:
        loader.add("canon-git-error", ERROR, "lecture du canon : %s" % exc)
        loader.canon.readable = False
        return loader.canon
    except OSError as exc:
        # Lecture refusée ou impossible (permissions, fichier disparu) : même
        # traitement fail-closed qu'une erreur git.
        loader.add("canon-io-error", ERROR, "lecture du canon : %s" % (exc.strerror or exc))
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
                entry=None) -> Canon:
    """Le canon de la configuration (`AMEESH_CANON`, `AMEESH_CANON_REF`, …).

    Sans `entry` : le canon par défaut (ou `root`, lu comme canon par défaut).
    `entry` (L42, `config.CanonEntry`) : un autre canon configuré — il n'est le
    canon par défaut que s'il est le premier de la liste."""
    entries = configured(cfg)
    if entry is not None:
        default = bool(entries) and entries[0].path == entry.path
        canon = load(entry.path, ref=entry.ref, untrusted=entry.untrusted, fetch=fetch,
                     default=default)
    else:
        path = root or cfg.canon
        if not path:
            raise CanonError("aucun canon configuré : posez AMEESH_CANON (ou `canon` dans la "
                             "configuration), ou passez --canon DOSSIER")
        canon = load(path, ref=cfg.canon_ref, untrusted=cfg.canon_untrusted, fetch=fetch)
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
                "configuration : deux canons ont l'identifiant « %s » (%s et %s) — "
                "l'`id` de federation.yaml (sinon le nom du dossier) doit être unique "
                "parmi les canons de l'hôte" % (canon.id, other.root, canon.root))
        seen[canon.id] = canon


def load_configured(cfg, *, fetch: bool = False) -> list[Canon]:
    """Les canons configurés qui se laissent charger, le canon par défaut
    d'abord (lecture seule : seuils, hôtes). Une configuration fautive d'un
    canon ne masque pas les autres ; un identifiant en double est écarté."""
    out: list[Canon] = []
    for index, entry in enumerate(configured(cfg)):
        try:
            canon = from_config(cfg, fetch=fetch) if index == 0 \
                else from_config(cfg, entry=entry, fetch=fetch)
        except CanonError:
            continue
        if any(c.id == canon.id for c in out):
            continue
        out.append(canon)
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
        raise CanonError("aucun canon configuré : posez AMEESH_CANON ou AMEESH_CANONS (ou "
                         "`canon` / `canons` dans la configuration)")
    canons = [from_config(cfg, fetch=fetch)]
    canons += [from_config(cfg, entry=entry, fetch=fetch) for entry in entries[1:]]
    check_identities(canons)
    return canons


# ==========================================================================
# Validation (§4.3)
# ==========================================================================

def validate(canon: Canon) -> list[Finding]:
    """Constats de lecture + constats du profil. Une erreur est bloquante."""
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
                        "%s %s déclaré %d fois (%s)" % (
                            kind, title, len(group),
                            ", ".join("%s:%s" % (g.fiche.member, g.fiche.path) for g in group)),
                        item.fiche, **({subject: title} if subject else {}))

    agent_names = {a.title for a in canon.agents}
    for member in canon.members:
        if member.title in agent_names:
            add("member-agent-clash", ERROR,
                "le membre humain %s porte le nom d'un agent : responsable ambigu"
                % member.title, member.fiche, agent=member.title)

    # -- agents ------------------------------------------------------------------
    # L16 (R22) : plus de liste fermée de harnais ; les identifiants valides sont
    # ceux des descripteurs connus (paquet + dossier de l'hôte). Un `harness`
    # inconnu est une ERREUR : l'agent ne serait réclamable nulle part, et le
    # silence ferait croire à un harnais supporté.
    known_harnesses = set(harnesses.known_ids())
    for agent in canon.agents:
        f = agent.fiche
        if not NAME_RE.match(agent.title):
            add("agent-title-invalid", ERROR,
                "nom d'agent %r hors de la grammaire du registre" % agent.title, f)
            continue
        if not agent.responsible:
            add("agent-responsible-missing", ERROR,
                "agent %s sans `responsible` (R14 : tout agent a un humain responsable)"
                % agent.title, f, agent=agent.title)
        elif canon.resolve_human(agent.responsible) is None:
            add("agent-responsible-unresolved", ERROR,
                "responsable %r de %s ne résout pas vers un Member humain (attendu "
                "human:<id> d'une fiche Member unique)" % (agent.responsible, agent.title),
                f, agent=agent.title)
        caps = agent.capabilities or []
        forbidden = [c for c in caps if c.strip().lower() in FORBIDDEN_CAPABILITIES]
        if forbidden:
            add("agent-approve-capability", ERROR,
                "agent %s avec la capacité `approve` : réservée aux humains (R8)"
                % agent.title, f, agent=agent.title)
        unknown = [c for c in caps if c not in KNOWN_CAPABILITIES and c not in forbidden]
        if unknown:
            add("agent-capability-unknown", WARNING,
                "capacités inconnues pour %s : %s" % (agent.title, ", ".join(unknown)),
                f, agent=agent.title)
        if not agent.harness:
            add("agent-harness-missing", WARNING,
                "agent %s sans `harness` : aucun harnais ne sera lancé" % agent.title,
                f, agent=agent.title)
        elif agent.harness not in known_harnesses:
            add("agent-harness-unknown", ERROR,
                "harnais %r de %s sans descripteur connu (%s)"
                % (agent.harness, agent.title, ", ".join(sorted(known_harnesses)) or "aucun"),
                f, agent=agent.title)

    # -- hôtes -----------------------------------------------------------------------
    for host in canon.hosts:
        if not host.responsible:
            add("host-responsible-missing", ERROR,
                "hôte %s sans `responsible`" % host.title, host.fiche, host=host.title)
        elif canon.resolve_human(host.responsible) is None:
            add("host-responsible-unresolved", ERROR,
                "responsable %r de l'hôte %s ne résout pas vers un Member humain"
                % (host.responsible, host.title), host.fiche, host=host.title)
        for name in host.policy.harnesses or []:
            if name not in known_harnesses:
                add("host-harness-unknown", ERROR,
                    "politique de l'hôte %s : harnais %r sans descripteur connu (%s)"
                    % (host.title, name, ", ".join(sorted(known_harnesses)) or "aucun"),
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
            add("placement-incomplete", ERROR, "admission sans `agent`", f)
            continue
        if not placement.host_names() and not placement.tags():
            add("placement-incomplete", ERROR,
                "admission de %s sans `hosts` ni `host_tags`" % placement.agent, f,
                agent=placement.agent)
            continue
        by_agent[placement.agent].append(placement)
        if placement.agent not in agent_names:
            add("placement-agent-unknown", ERROR,
                "admission vers un agent inconnu : %s" % placement.agent, f,
                agent=placement.agent)
        cibles = set(placement.host_names())
        for tag in placement.tags():
            if tag not in known_tags:
                add("admission-tag-unknown", WARNING,
                    "admission de %s : étiquette `%s` ne correspond à aucun hôte"
                    % (placement.agent, tag), f, agent=placement.agent)
            cibles |= {h.title for h in canon.hosts if tag in (h.tags or [])}
        for name in sorted(cibles):
            if name not in host_names:
                add("placement-host-unknown", ERROR,
                    "admission de %s vers un hôte inconnu : %s" % (placement.agent, name),
                    f, agent=placement.agent)
                continue
            by_host[name][id(placement)] = placement
            agent = canon.agent(placement.agent)
            host = canon.host(name)
            if agent is not None and host is not None:
                for reason in placement_violations(agent, host, placement):
                    add("placement-policy-violation", ERROR,
                        "%s sur %s : %s" % (agent.title, host.title, reason), f,
                        agent=agent.title)
                _check_work_dir(add, agent, host, placement)
    for name, group in by_agent.items():
        if len(group) > 1:
            for placement in group:
                add("placement-duplicate", ERROR,
                    "agent %s admis %d fois (%s)" % (
                        name, len(group), ", ".join(p.agent or "?" for p in group)),
                    placement.fiche, agent=name)
    for host in canon.hosts:
        for name in sorted(host.policy.work_dirs or {}):
            if name not in agent_names:
                add("host-work-dir-agent-unknown", WARNING,
                    "politique de l'hôte %s : `work_dirs.%s` ne nomme aucun agent du canon"
                    % (host.title, name), host.fiche, host=host.title)
        limit = host.policy.max_agents
        if limit is not None and len(by_host.get(host.title, {})) > limit:
            add("host-max-agents", ERROR,
                "hôte %s : %d admissions pour max_agents = %d"
                % (host.title, len(by_host[host.title]), limit), host.fiche, host=host.title)

    # -- politique de revue par classe (décision 0018 point 1, R19) ------------
    # La déclaration vit dans `federation.yaml`, sous `review_policies.classes` :
    # `canon check` la valide, `ameesh review-class` la relit avec le même code.
    root_member = canon.sources[0].member if canon.sources else ""
    _policy, policy_problems = review_mod.policy_from_federation(canon.federation)
    for code, severity, message in policy_problems:
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

    # -- avertissements -----------------------------------------------------------------
    for agent in canon.agents:
        if agent.title not in by_agent:
            add("agent-unplaced", WARNING, "agent %s sans placement" % agent.title,
                agent.fiche, agent=agent.title)
    for host in canon.hosts:
        if host.title not in by_host:
            add("host-unplaced", WARNING, "hôte %s sans placement" % host.title,
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
            "`cwd` %r de l'admission de %s ignoré sur %s : la politique de l'hôte donne "
            "%s" % (placement.cwd, agent.title, host.title, found), where,
            agent=agent.title)
    elif placement.cwd:
        add("admission-cwd-inherited", WARNING,
            "%s sur %s : cwd hérité de la fiche Placement (%s), repli transitoire — à "
            "migrer vers `policy.work_dirs` / `work_roots` de l'hôte (repli retiré au "
            "plus tard en v1.5.0)" % (agent.title, host.title, placement.cwd), where,
            agent=agent.title)
    elif not found:
        add("host-work-dir-missing", WARNING,
            "%s sur %s : aucun dossier de travail — déclarer `policy.work_dirs`, "
            "`work_roots` ou `work_root` dans la fiche de l'hôte" % (agent.title, host.title),
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
                    "WorkPackage %s déclaré %d fois (%s)" % (
                        ident, len(group),
                        ", ".join("%s:%s" % (g.fiche.member, g.fiche.path) for g in group)),
                    package.fiche, package=ident)
    for package in canon.packages:
        f, ident = package.fiche, package.id
        if not PACKAGE_ID_RE.match(ident):
            add("package-id-invalid", ERROR,
                "identifiant de WorkPackage %r invalide (lettres, chiffres, . _ -, 64 "
                "caractères au plus)" % ident, f, package=ident)
        if package.kind not in PACKAGE_KINDS:
            add("package-kind-invalid", ERROR,
                "WorkPackage %s : `kind` %r (attendu : %s)"
                % (ident, package.kind, " | ".join(PACKAGE_KINDS)), f, package=ident)
        if not package.responsible:
            add("package-responsible-missing", ERROR,
                "WorkPackage %s sans `responsible` (un humain répond de chaque élément "
                "du plan)" % ident, f, package=ident)
        elif canon.resolve_human(package.responsible) is None:
            add("package-responsible-unresolved", ERROR,
                "responsable %r de %s ne résout pas vers un Member humain"
                % (package.responsible, ident), f, package=ident)
        if package.parent:
            parents = by_id.get(package.parent) or []
            if not parents:
                add("package-parent-unknown", ERROR,
                    "WorkPackage %s : parent inconnu %r" % (ident, package.parent), f,
                    package=ident)
            elif package.kind in PACKAGE_KINDS and len(parents) == 1:
                allowed = PACKAGE_PARENTS[package.kind]
                if parents[0].kind not in allowed:
                    add("package-kind-incoherent", ERROR,
                        "WorkPackage %s (%s) sous %s (%s) : %s" % (
                            ident, package.kind, package.parent, parents[0].kind,
                            "un %s se place sous %s" % (package.kind, " ou ".join(allowed))
                            if allowed else "un jalon n'a pas de parent"),
                        f, package=ident)
        elif package.kind == "lot":
            add("package-lot-orphan", WARNING,
                "lot %s sans parent (un lot se place sous un epic ou un jalon)" % ident,
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
                "cycle dans le plan : %s" % " → ".join(seen + [current]), package.fiche,
                package=package.id)


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == ERROR]


@dataclass
class Blocking:
    """Qui une erreur empêche de réclamer (un canon invalide bloque, §4.1)."""

    global_errors: list[Finding]
    by_agent: dict[str, list[Finding]]
    by_host: dict[str, list[Finding]]

    def reasons(self, agent: str, hosts: Iterable[str] = ()) -> list[str]:
        out = ["%s (%s)" % (f.code, f.where()) for f in self.global_errors]
        out += ["%s (%s)" % (f.code, f.where()) for f in self.by_agent.get(agent, [])]
        for host in hosts:
            out += ["%s (%s)" % (f.code, f.where()) for f in self.by_host.get(host, [])]
        return list(dict.fromkeys(out))


def blocking(findings: Iterable[Finding]) -> Blocking:
    """Une erreur liée à un agent bloque cet agent ; à un hôte, les agents qui y
    sont placés ; à une fiche du plan (WorkPackage, L29), aucun agent ; sinon
    (fédération, frontmatter d'une fiche du profil, …) tous."""
    result = Blocking([], defaultdict(list), defaultdict(list))
    for finding in errors(findings):
        if finding.package:
            continue
        if finding.agent:
            result.by_agent[finding.agent].append(finding)
        elif finding.host:
            result.by_host[finding.host].append(finding)
        else:
            result.global_errors.append(finding)
    return result
