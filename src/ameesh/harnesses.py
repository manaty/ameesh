# SPDX-License-Identifier: AGPL-3.0-only
"""Descripteurs de harnais (L16, R22) : manifeste du registre ACP + clés `ameesh`.

Un harnais n'est plus une entrée d'une liste codée en dur : c'est un
**descripteur** JSON, au format du manifeste `agent.json` du registre ACP
(id, name, version, schema_version, description, repository, license,
capabilities, authentication, distribution), **étendu** par un espace de clés
`ameesh` pour ce que l'ACP ne décrit pas :

* pilotage sans interface : `binary`, `binary_env`, `command`, `interactive` ;
* reprise de session hors ACP : `session.flag` ;
* format du flux : `stream` (un lecteur connu, voir `adapters.py`) ;
* hooks : `hooks` (déclaratif, pour le générateur de plugin de L17) ;
* jauges et coût : `cost.source`, `cost.gauges`, `cost.paid_per_token` ;
* permissions : `permissions` (refus par défaut) ;
* réglages : `model`, `effort`, `tier` (`flag` ou livraison par fichier).

Deux sources, dans cet ordre :

1. les descripteurs du **paquet** (`ameesh/harness_descriptors/*.json`) ;
2. le **dossier de descripteurs de l'hôte** (`$AMEESH_HARNESSES_DIR`, sinon
   `~/.config/ameesh/harnesses/`), qui prime pour un même `id` : l'hôte épingle
   sa version et ses options sans attendre une livraison.

Le dossier de l'hôte accepte `*.json` et la disposition du registre ACP
(`<id>/agent.json`). Un descripteur illisible ou invalide n'est **pas** connu :
il est ignoré (fail-closed) et signalé par `ameesh harness list|check`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from string import Formatter
from typing import Any, Iterable, Mapping

#: version du schéma de descripteur que ce code comprend
SCHEMA_VERSION = "1"
#: protocoles de pilotage
PROTOCOLS = ("cli", "acp")
#: lecteurs de flux connus (implémentés dans `adapters.py`)
STREAM_FORMATS = ("claude-stream-json", "codex-json", "dsh-json", "acp-json", "text")
#: catégories de permission ACP (`ToolKind`)
TOOL_KINDS = ("read", "edit", "delete", "move", "search", "execute", "think",
              "fetch", "switch_mode", "other")
#: méthodes ACP décrites par le manifeste du registre (avertissement si inconnue)
ACP_METHODS = (
    "initialize", "authenticate", "logout", "session/new", "session/load",
    "session/resume", "session/close", "session/delete", "session/list",
    "session/prompt", "session/cancel", "session/set_mode",
    "session/set_config_option", "session/update", "session/request_permission",
)
#: types de distribution du registre ACP
DISTRIBUTION_TYPES = ("binary", "npx", "uvx")
#: cibles `binary` du registre ACP
BINARY_TARGETS = ("darwin-aarch64", "darwin-x86_64", "linux-aarch64", "linux-x86_64",
                  "windows-aarch64", "windows-x86_64")
#: types d'authentification ACP
AUTH_TYPES = ("agent", "terminal")
#: clés connues sous `ameesh`
AMEESH_KEYS = ("protocol", "binary", "binary_env", "launcher", "command", "interactive",
               "session", "stream", "env", "model", "effort", "tier", "hooks",
               "cost", "permissions", "acp", "accounts", "install", "backup")
#: clés connues de `ameesh.backup` (L53 : sauvegarde des sessions hors de l'appareil)
BACKUP_KEYS = ("session_files",)
#: clés connues de `ameesh.accounts` (L30 : comptes multiples par fournisseur)
ACCOUNT_KEYS = ("config_env", "default_home", "session_store", "credentials", "key_env")
#: clés connues au premier niveau du manifeste
MANIFEST_KEYS = ("id", "name", "version", "schema_version", "description",
                 "repository", "license", "authors", "icon", "capabilities",
                 "authentication", "distribution", "ameesh")

ERROR = "error"
WARNING = "warning"
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DescriptorError(ValueError):
    """Descripteur absent, illisible ou invalide : on ne lance pas ce harnais."""

    def __init__(self, message: str, findings: Iterable["Finding"] = ()):
        super().__init__(message)
        self.findings = list(findings)


@dataclass(frozen=True)
class Finding:
    """Un constat de validation d'un descripteur (comme ceux du canon)."""

    code: str
    severity: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        return "%s %-32s %s" % (
            "ERREUR " if self.severity == ERROR else "avert. ", self.code, self.message)


@dataclass(frozen=True)
class Setting:
    """Un réglage d'exécution (model, effort, tier) : arguments ou fichier."""

    flag: tuple[str, ...] = ()
    file: Mapping[str, Any] | None = None
    default: str | None = None


@dataclass(frozen=True)
class HarnessDescriptor:
    """Un descripteur chargé et normalisé."""

    id: str
    name: str
    version: str
    schema_version: str
    description: str
    repository: str = ""
    license: str = ""
    authors: tuple[str, ...] = ()
    icon: str = ""
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    authentication: Mapping[str, Any] = field(default_factory=dict)
    distribution: Mapping[str, Any] = field(default_factory=dict)
    ameesh: Mapping[str, Any] = field(default_factory=dict)
    path: str = ""
    source: str = "paquet"
    #: empreinte du fichier chargé (audit de chaîne d'approvisionnement)
    sha256: str = ""
    document: Mapping[str, Any] = field(default_factory=dict)
    # -- dérivés de l'espace `ameesh` --------------------------------------
    protocol: str = "cli"
    binary: str = ""
    binary_env: tuple[str, ...] = ()
    launcher: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    interactive: tuple[str, ...] | None = None
    session_flag: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    stream: str = "text"
    model: Setting = Setting()
    effort: Setting = Setting()
    tier: Setting = Setting()
    hooks: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)
    permissions: Mapping[str, Any] = field(default_factory=dict)
    acp: Mapping[str, Any] = field(default_factory=dict)
    accounts: Mapping[str, str] = field(default_factory=dict)
    #: L53 : motifs (relatifs au dossier du compte) des fichiers d'une session,
    #: `{session}` remplacé par son identifiant ; vide = sauvegarde impossible.
    session_files: tuple[str, ...] = ()

    @property
    def paid_per_token(self) -> bool:
        """Le coût de ce harnais est-il payé au jeton (0019 §2) ?"""
        return bool(self.cost.get("paid_per_token"))

    @property
    def attach(self) -> bool:
        """`ameesh attach` a-t-il une commande interactive déclarée ?"""
        return self.interactive is not None

    def settings(self) -> dict[str, Setting]:
        return {"model": self.model, "effort": self.effort, "tier": self.tier}

    def default_of(self, key: str) -> str:
        setting = self.settings().get(key)
        return (setting.default if setting else "") or ""

    def as_json(self) -> dict:
        """Le document d'origine, pour `harness show --json`."""
        return dict(self.document)


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def package_dir() -> str:
    """Dossier des descripteurs livrés dans le paquet."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness_descriptors")


def host_dir(env: Mapping[str, str] | None = None) -> str:
    """Dossier de descripteurs de l'hôte (`AMEESH_HARNESSES_DIR` en premier)."""
    env = os.environ if env is None else env
    for name in ("AMEESH_HARNESSES_DIR", "AGENT_MESH_HARNESSES_DIR"):
        value = env.get(name)
        if value:
            return os.path.abspath(os.path.expanduser(value))
    return os.path.abspath(os.path.expanduser("~/.config/ameesh/harnesses"))


def _source_of(path: str) -> str:
    """Provenance d'un fichier de descripteur : `hôte` sous le dossier de l'hôte.

    Le préfixe est **littéral** (pas `realpath`) : un chemin qui dit venir du
    dossier de l'hôte y reste rattaché, et `_secure_read` refuse alors le lien
    qui tenterait de le faire sortir. Sinon, un sous-dossier symbolique vers
    l'extérieur serait classé « paquet » et lu sans contrôle (codex2).
    """
    racine = os.path.abspath(os.path.expanduser(host_dir()))
    cible = os.path.abspath(os.path.expanduser(path))
    if cible == racine or cible.startswith(racine + os.sep):
        return "hôte"
    return "paquet"


def _euid() -> int | None:
    return os.geteuid() if hasattr(os, "geteuid") else None


def _component_problem(info: os.stat_result, path: str, *, directory: bool) -> str:
    """Pourquoi un composant du dossier de l'hôte est refusé, ou ''."""
    if stat.S_ISLNK(info.st_mode):
        return "lien symbolique refusé dans le dossier de descripteurs : %s" % path
    if directory and not stat.S_ISDIR(info.st_mode):
        return "composant non dossier : %s" % path
    if not directory and not stat.S_ISREG(info.st_mode):
        return "fichier ordinaire attendu : %s" % path
    euid = _euid()
    if euid is not None and info.st_uid != euid:
        return "composant d'un autre utilisateur (%s) : ignoré" % path
    if directory and info.st_mode & 0o077:
        return ("dossier de descripteurs non privé (%s, mode %o, attendu 700) : ignoré"
                % (path, info.st_mode & 0o777))
    if not directory and info.st_mode & 0o022:
        return ("descripteur modifiable par le groupe ou les autres (%s, mode %o) : ignoré"
                % (path, info.st_mode & 0o777))
    return ""


def host_dir_problem(directory: str) -> str:
    """Pourquoi le dossier de descripteurs de l'hôte est-il refusé, ou ''.

    C'est un point de chaîne d'approvisionnement (décision 0021) : le dossier
    doit appartenir à l'utilisateur de l'exécuteur, être privé (0700) et ne
    **pas** être atteint par un lien. Sinon il est **ignoré** : un descripteur
    déposé par un autre compte (ou via un lien vers un dossier modifiable)
    pourrait lancer un binaire arbitraire.
    """
    if not os.path.lexists(directory):
        return ""
    try:
        info = os.lstat(directory)
    except OSError as exc:
        return "dossier de descripteurs illisible (%s) : %s" % (directory, exc.strerror or exc)
    problem = _component_problem(info, directory, directory=True)
    if problem:
        return problem
    return ""


def host_file_problem(path: str) -> str:
    """Pourquoi un fichier de descripteur de l'hôte est refusé, ou ''. """
    try:
        info = os.lstat(path)
    except OSError as exc:
        return "descripteur illisible (%s) : %s" % (path, exc.strerror or exc)
    return _component_problem(info, path, directory=False)


def _sources(directory: str, findings: list[Finding] | None = None) -> list[str]:
    """Les fichiers `*.json` d'un dossier, plus la disposition `<id>/agent.json`.

    Aucun lien n'est suivi : ni un `*.json` symbolique, ni un sous-dossier
    symbolique (qui pourrait pointer hors du dossier de confiance). Chaque lien
    ignoré est signalé (`harness-host-insecure`) quand `findings` est fourni.
    """
    out: list[str] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        try:
            if entry.is_symlink():
                if findings is not None:
                    findings.append(Finding(
                        "harness-host-insecure", WARNING,
                        "lien symbolique ignoré dans le dossier de descripteurs : %s"
                        % entry.path, entry.path))
                continue
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json"):
                out.append(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                agent = os.path.join(entry.path, "agent.json")
                try:
                    info = os.lstat(agent)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    if findings is not None:
                        findings.append(Finding(
                            "harness-host-insecure", WARNING,
                            "descripteur atteint par un lien symbolique : %s" % agent, agent))
                    continue
                if stat.S_ISREG(info.st_mode):
                    out.append(agent)
        except OSError:
            continue
    return out


def _open_root_dir(root: str) -> int:
    """Ouvre `root` composant par composant depuis la racine, sans suivre de lien.

    Les **ancêtres** du dossier sont donc protégés comme lui : un lien nulle
    part dans le chemin n'est suivi (`O_NOFOLLOW` à chaque composant). Lève
    `OSError` si un composant est un lien, un non-dossier ou est illisible.
    """
    racine = os.path.abspath(root)
    composants = [c for c in racine.split(os.sep) if c]
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for composant in composants:
            suivant = os.open(composant,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = suivant
    except OSError:
        os.close(fd)
        raise
    return fd


def _secure_read(path: str, root: str) -> bytes:
    """Lit un descripteur sous `root` sans suivre aucun lien, et rend ses octets.

    La racine est ouverte une fois par `_open_root_dir` : chacun de ses
    **ancêtres** est parcouru sans suivre de lien. Puis chaque composant est
    ouvert par `dir_fd` + `O_NOFOLLOW` et **contrôlé sur le descripteur
    effectivement acquis** (`fstat`), jamais par un `stat` préalable qui
    pourrait porter sur un autre objet. Le dernier composant est ouvert en
    `O_NONBLOCK` : un type non régulier (FIFO, périphérique) est **refusé sans
    attente d'un écrivain**, et un fichier régulier est repassé en bloquant
    avant lecture. Le dossier parent est refermé sur succès comme sur erreur.
    Les octets rendus sont **exactement** ceux dont l'appelant calcule
    l'empreinte et qu'il analyse — jamais une relecture par nom.
    """
    racine = os.path.abspath(root)
    cible = os.path.abspath(path)
    rel = os.path.relpath(cible, racine)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        pb = "descripteur hors du dossier de l'hôte : %s" % cible
        raise DescriptorError(pb, [Finding("harness-host-insecure", WARNING, pb, cible)])
    composants = [c for c in rel.split(os.sep) if c not in ("", os.curdir)]
    if not composants:
        pb = "descripteur sans chemin de fichier : %s" % cible
        raise DescriptorError(pb, [Finding("harness-host-insecure", WARNING, pb, cible)])
    try:
        fd = _open_root_dir(racine)
    except OSError as exc:
        pb = ("dossier de descripteurs atteint par un lien ou illisible (%s) : %s"
              % (racine, exc))
        raise DescriptorError(pb, [Finding("harness-host-insecure", WARNING, pb, racine)])
    ouvert: int | None = None
    try:
        problem = _component_problem(os.fstat(fd), racine, directory=True)
        if problem:
            raise DescriptorError(problem,
                                  [Finding("harness-host-insecure", WARNING, problem, racine)])
        chemin = racine
        for composant in composants[:-1]:
            chemin = os.path.join(chemin, composant)
            suivant = os.open(composant,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = suivant
            problem = _component_problem(os.fstat(fd), chemin, directory=True)
            if problem:
                raise DescriptorError(
                    problem, [Finding("harness-host-insecure", WARNING, problem, chemin)])
        dernier = os.path.join(chemin, composants[-1])
        # `O_NONBLOCK` à l'acquisition : un type non régulier (FIFO,
        # périphérique) est refusé sans attendre un écrivain ; on repasse en
        # bloquant pour lire un fichier régulier (codex2).
        ouvert = os.open(composants[-1],
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        problem = _component_problem(os.fstat(ouvert), dernier, directory=False)
        if problem:
            raise DescriptorError(problem,
                                  [Finding("harness-host-insecure", WARNING, problem, dernier)])
        os.set_blocking(ouvert, True)
    except DescriptorError:
        if ouvert is not None:
            os.close(ouvert)
        raise
    except OSError as exc:
        if ouvert is not None:
            os.close(ouvert)
        pb = "descripteur atteint par un lien ou un composant interdit (%s) : %s" % (cible, exc)
        raise DescriptorError(pb, [Finding("harness-host-insecure", WARNING, pb, cible)])
    finally:
        # Le dossier parent est toujours refermé : sans ce `finally` il fuitait
        # à chaque lecture réussie (codex2).
        os.close(fd)
    try:
        fh = os.fdopen(ouvert, "rb")
    except (OSError, ValueError):
        os.close(ouvert)
        raise
    with fh:
        return fh.read()


def _read_descriptor(path: str, root: str | None = None) -> bytes:
    """Les octets d'un descripteur, sans suivi de lien s'il vient de l'hôte.

    `root` est la racine d'hôte **explicite** de l'appelant (`scan(host=...)`) :
    quand elle est fournie, la lecture passe toujours par `_secure_read` contre
    cette racine, même si elle diffère du dossier de l'environnement ; le
    paramètre `source` ne sert alors que d'étiquette.
    """
    if root is not None:
        return _secure_read(path, root)
    if _source_of(path) == "hôte":
        return _secure_read(path, host_dir())
    with open(path, "rb") as fh:
        return fh.read()


def fingerprint(path: str) -> str:
    """Empreinte SHA-256 des octets d'un descripteur (audit)."""
    try:
        return hashlib.sha256(_read_descriptor(path)).hexdigest()
    except (DescriptorError, OSError):
        return ""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _text(value: Any, minimum: int = 1) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if len(value) >= minimum else None


def _strings(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return [v for v in value]


def _normalize_capabilities(value: Any) -> dict | None:
    """`capabilities` : objet méthode → booléen, ou liste de méthodes."""
    if isinstance(value, list):
        if not all(isinstance(v, str) and v.strip() for v in value):
            return None
        return {v.strip(): True for v in value}
    if isinstance(value, dict):
        if not all(isinstance(k, str) and isinstance(v, bool) for k, v in value.items()):
            return None
        return dict(value)
    return None


def _normalize_authentication(value: Any) -> dict | None:
    if isinstance(value, list):
        return {"methods": value}
    if isinstance(value, dict):
        return dict(value)
    return None


def _check_distribution(value: Any, findings: list[Finding]) -> None:
    if not isinstance(value, dict):
        findings.append(Finding("harness-distribution-invalid", ERROR,
                                "`distribution` : objet attendu"))
        return
    if not value:
        findings.append(Finding("harness-distribution-empty", WARNING,
                                "`distribution` vide : l'installation du harnais n'est pas décrite"))
        return
    for kind, entry in value.items():
        if kind not in DISTRIBUTION_TYPES:
            findings.append(Finding("harness-distribution-invalid", ERROR,
                                    "`distribution.%s` : type inconnu (%s)"
                                    % (kind, " | ".join(DISTRIBUTION_TYPES))))
            continue
        if not isinstance(entry, dict):
            findings.append(Finding("harness-distribution-invalid", ERROR,
                                    "`distribution.%s` : objet attendu" % kind))
            continue
        if kind == "binary":
            for target, item in entry.items():
                if target not in BINARY_TARGETS:
                    findings.append(Finding("harness-distribution-invalid", ERROR,
                                            "`distribution.binary.%s` : cible inconnue" % target))
                elif not isinstance(item, dict) or not _text(item.get("archive")) \
                        or not _text(item.get("cmd")):
                    findings.append(Finding(
                        "harness-distribution-invalid", ERROR,
                        "`distribution.binary.%s` : `archive` et `cmd` attendus" % target))
                else:
                    _check_args_env(item, "distribution.binary.%s" % target, findings)
        else:
            if not _text(entry.get("package")):
                findings.append(Finding("harness-distribution-invalid", ERROR,
                                        "`distribution.%s.package` attendu" % kind))
            _check_args_env(entry, "distribution.%s" % kind, findings)


def _check_args_env(entry: dict, where: str, findings: list[Finding]) -> None:
    if "args" in entry and _strings(entry.get("args")) is None:
        findings.append(Finding("harness-distribution-invalid", ERROR,
                                "`%s.args` : liste de textes attendue" % where))
    if "env" in entry and not isinstance(entry.get("env"), dict):
        findings.append(Finding("harness-distribution-invalid", ERROR,
                                "`%s.env` : objet attendu" % where))


def _check_setting(key: str, value: Any, findings: list[Finding]) -> None:
    if not isinstance(value, dict):
        findings.append(Finding("harness-option-invalid", ERROR,
                                "`ameesh.%s` : objet attendu" % key))
        return
    unknown = [k for k in value if k not in ("flag", "file", "default")]
    for name in unknown:
        findings.append(Finding("harness-key-unknown", WARNING,
                                "`ameesh.%s.%s` : clé inconnue (ignorée)" % (key, name)))
    flag = value.get("flag")
    if flag is not None and _strings(flag) is None:
        findings.append(Finding("harness-option-invalid", ERROR,
                                "`ameesh.%s.flag` : liste de textes attendue" % key))
    default = value.get("default")
    if default is not None and not isinstance(default, (str, int, float)):
        findings.append(Finding("harness-option-invalid", ERROR,
                                "`ameesh.%s.default` : texte attendu" % key))
    delivery = value.get("file")
    if delivery is None:
        return
    if not isinstance(delivery, dict):
        findings.append(Finding("harness-option-invalid", ERROR,
                                "`ameesh.%s.file` : objet attendu" % key))
        return
    template = delivery.get("template")
    if not isinstance(template, str) or not template:
        findings.append(Finding("harness-option-invalid", ERROR,
                                "`ameesh.%s.file.template` : texte non vide attendu" % key))
        return
    # les placeholders du gabarit sont limités aux valeurs connues : {model},
    # {effort}, {tier}, {path} — une faute de frappe laisserait un patch vide.
    known = {"model", "effort", "tier", "path", "id"}
    for _literal, name, _spec, _conv in Formatter().parse(template):
        if name is not None and name not in known:
            findings.append(Finding("harness-option-invalid", ERROR,
                                    "`ameesh.%s.file.template` : placeholder {%s} inconnu (%s)"
                                    % (key, name, ", ".join(sorted(known)))))
    for name in delivery:
        if name not in ("template",):
            findings.append(Finding("harness-key-unknown", WARNING,
                                    "`ameesh.%s.file.%s` : clé inconnue (ignorée)" % (key, name)))


def validate(document: Any, *, path: str = "") -> list[Finding]:
    """Les constats d'un document de descripteur (erreurs bloquantes + avertissements)."""
    findings: list[Finding] = []
    if not isinstance(document, dict):
        return [Finding("harness-not-object", ERROR, "objet JSON attendu", path)]
    for key in document:
        if key not in MANIFEST_KEYS:
            findings.append(Finding("harness-key-unknown", WARNING,
                                    "clé inconnue au premier niveau : `%s` (ignorée)" % key,
                                    path))
    ident = _text(document.get("id"))
    if ident is None:
        findings.append(Finding("harness-id-missing", ERROR, "`id` requis (texte)", path))
    elif not ID_RE.match(ident):
        findings.append(Finding("harness-id-invalid", ERROR,
                                "`id` %r invalide (minuscules, chiffres, tirets, "
                                "commence par une lettre)" % ident, path))
    for key in ("name", "version", "schema_version", "description"):
        value = document.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if _text(value) is None:
            findings.append(Finding("harness-field-missing", ERROR,
                                    "`%s` requis (texte non vide)" % key, path))
    version = document.get("schema_version")
    version = str(version) if isinstance(version, (int, float)) else version
    if _text(version) and str(version).split(".")[0] not in ("", SCHEMA_VERSION):
        findings.append(Finding("harness-schema-version", WARNING,
                                "`schema_version` %s : ce code comprend la version %s "
                                "(clés inconnues ignorées)" % (version, SCHEMA_VERSION), path))
    if "repository" in document and _text(document.get("repository")) is None:
        findings.append(Finding("harness-field-invalid", ERROR,
                                "`repository` : texte attendu", path))
    if "license" in document and _text(document.get("license")) is None:
        findings.append(Finding("harness-field-invalid", ERROR, "`license` : texte attendu", path))
    if "authors" in document and _strings(document.get("authors")) is None:
        findings.append(Finding("harness-field-invalid", ERROR,
                                "`authors` : liste de textes attendue", path))
    if "capabilities" in document:
        caps = _normalize_capabilities(document.get("capabilities"))
        if caps is None:
            findings.append(Finding("harness-capabilities-invalid", ERROR,
                                    "`capabilities` : objet méthode → booléen, ou liste de "
                                    "méthodes", path))
        else:
            for method in caps:
                if method not in ACP_METHODS:
                    findings.append(Finding("harness-capabilities-invalid", WARNING,
                                            "méthode ACP inconnue : `%s`" % method, path))
    if "authentication" in document:
        auth = _normalize_authentication(document.get("authentication"))
        if auth is None:
            findings.append(Finding("harness-authentication-invalid", ERROR,
                                    "`authentication` : objet ou liste attendue", path))
        else:
            methods = auth.get("methods")
            if methods is not None:
                if not isinstance(methods, list):
                    findings.append(Finding("harness-authentication-invalid", ERROR,
                                            "`authentication.methods` : liste attendue", path))
                else:
                    for item in methods:
                        if not isinstance(item, dict) or not _text(item.get("id")):
                            findings.append(Finding(
                                "harness-authentication-invalid", ERROR,
                                "`authentication.methods` : chaque méthode porte un `id` "
                                "texte", path))
                        elif item.get("type") not in AUTH_TYPES:
                            findings.append(Finding(
                                "harness-authentication-invalid", ERROR,
                                "méthode d'authentification %r : `type` attendu %s"
                                % (item.get("id"), " | ".join(AUTH_TYPES)), path))
    if "distribution" in document:
        _check_distribution(document.get("distribution"), findings)
    if "icon" in document and _text(document.get("icon")) is None:
        findings.append(Finding("harness-field-invalid", ERROR, "`icon` : texte attendu", path))

    ameesh = document.get("ameesh")
    if ameesh is None:
        findings.append(Finding("harness-ameesh-missing", ERROR,
                                "espace `ameesh` requis (pilotage sans interface)", path))
        return findings
    if not isinstance(ameesh, dict):
        findings.append(Finding("harness-ameesh-invalid", ERROR, "`ameesh` : objet attendu", path))
        return findings
    for key in ameesh:
        if key not in AMEESH_KEYS:
            findings.append(Finding("harness-key-unknown", WARNING,
                                    "`ameesh.%s` : clé inconnue (ignorée)" % key, path))
    protocol = ameesh.get("protocol")
    if protocol not in PROTOCOLS:
        findings.append(Finding("harness-protocol-invalid", ERROR,
                                "`ameesh.protocol` : %s attendu"
                                % " | ".join(PROTOCOLS), path))
    binary = _text(ameesh.get("binary"))
    if binary is None:
        findings.append(Finding("harness-binary-missing", ERROR,
                                "`ameesh.binary` requis (nom ou chemin du binaire)", path))
    env_names = ameesh.get("binary_env")
    if env_names is not None:
        names = _strings(env_names)
        if names is None or not all(ENV_RE.match(n) for n in names):
            findings.append(Finding("harness-binary-env-invalid", ERROR,
                                    "`ameesh.binary_env` : noms de variables attendus", path))
    for key in ("launcher", "command"):
        value = ameesh.get(key)
        if value is not None and _strings(value) is None:
            findings.append(Finding("harness-command-invalid", ERROR,
                                    "`ameesh.%s` : liste de textes attendue" % key, path))
    interactive = ameesh.get("interactive", "absent")
    if interactive != "absent" and interactive is not None and _strings(interactive) is None:
        findings.append(Finding("harness-command-invalid", ERROR,
                                "`ameesh.interactive` : liste de textes ou null attendu", path))
    session = ameesh.get("session")
    if session is not None:
        if not isinstance(session, dict):
            findings.append(Finding("harness-session-invalid", ERROR,
                                    "`ameesh.session` : objet attendu", path))
        else:
            for key in session:
                if key != "flag":
                    findings.append(Finding("harness-key-unknown", WARNING,
                                            "`ameesh.session.%s` : clé inconnue (ignorée)" % key,
                                            path))
            if _strings(session.get("flag")) is None:
                findings.append(Finding("harness-session-invalid", ERROR,
                                        "`ameesh.session.flag` : liste de textes attendue", path))
            if protocol == "acp":
                findings.append(Finding("harness-session-invalid", WARNING,
                                        "`ameesh.session` n'a pas de sens en ACP (session JSON-RPC)",
                                        path))
    stream = ameesh.get("stream")
    if protocol == "cli":
        if stream is None:
            findings.append(Finding("harness-stream-unknown", ERROR,
                                    "`ameesh.stream` requis pour un harnais en ligne de commande "
                                    "(%s)" % " | ".join(STREAM_FORMATS), path))
        elif stream not in STREAM_FORMATS:
            findings.append(Finding("harness-stream-unknown", ERROR,
                                    "`ameesh.stream` %r inconnu (%s)"
                                    % (stream, " | ".join(STREAM_FORMATS)), path))
    elif stream is not None and stream != "acp-json":
        findings.append(Finding("harness-stream-unknown", ERROR,
                                "`ameesh.stream` doit valoir `acp-json` en ACP", path))
    env = ameesh.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and ENV_RE.match(k) and isinstance(v, str)
                for k, v in env.items()):
            findings.append(Finding("harness-env-invalid", ERROR,
                                    "`ameesh.env` : objet nom → texte attendu", path))
    files = 0
    for key in ("model", "effort", "tier"):
        if key in ameesh:
            _check_setting(key, ameesh.get(key), findings)
            if isinstance(ameesh.get(key), dict) and ameesh[key].get("file") is not None:
                files += 1
    if files > 1:
        findings.append(Finding("harness-option-invalid", ERROR,
                                "un seul réglage peut être livré par fichier "
                                "(model, effort ou tier)", path))
    _check_shape(ameesh.get("hooks"), "hooks", findings, path)
    cost = ameesh.get("cost")
    if cost is not None:
        if not isinstance(cost, dict):
            findings.append(Finding("harness-cost-invalid", ERROR,
                                    "`ameesh.cost` : objet attendu", path))
        else:
            for key in cost:
                if key not in ("source", "gauges", "paid_per_token"):
                    findings.append(Finding("harness-key-unknown", WARNING,
                                            "`ameesh.cost.%s` : clé inconnue (ignorée)" % key,
                                            path))
            if cost.get("source") is not None and cost.get("source") not in (
                    "stream", "api", "none", "transcript", "sessions"):
                findings.append(Finding("harness-cost-invalid", WARNING,
                                        "`ameesh.cost.source` %r inconnu" % cost.get("source"),
                                        path))
            if "paid_per_token" in cost and not isinstance(cost["paid_per_token"], bool):
                findings.append(Finding("harness-cost-invalid", ERROR,
                                        "`ameesh.cost.paid_per_token` : booléen attendu", path))
    permissions = ameesh.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            findings.append(Finding("harness-permissions-invalid", ERROR,
                                    "`ameesh.permissions` : objet attendu", path))
        else:
            for key in permissions:
                if key not in ("default", "allow_kinds", "deny_kinds"):
                    findings.append(Finding("harness-key-unknown", WARNING,
                                            "`ameesh.permissions.%s` : clé inconnue (ignorée)"
                                            % key, path))
            if permissions.get("default") not in (None, "deny", "allow"):
                findings.append(Finding("harness-permissions-invalid", ERROR,
                                        "`ameesh.permissions.default` : deny | allow attendu", path))
            for key in ("allow_kinds", "deny_kinds"):
                kinds = permissions.get(key)
                if kinds is None:
                    continue
                if not isinstance(kinds, list) or not all(
                        isinstance(k, str) and k in TOOL_KINDS for k in kinds):
                    findings.append(Finding("harness-permissions-invalid", ERROR,
                                            "`ameesh.permissions.%s` : kinds ACP attendus (%s)"
                                            % (key, " | ".join(TOOL_KINDS)), path))
    acp = ameesh.get("acp")
    if acp is not None:
        if not isinstance(acp, dict):
            findings.append(Finding("harness-acp-invalid", ERROR,
                                    "`ameesh.acp` : objet attendu", path))
        else:
            for key in acp:
                if key not in ("auth_method", "config"):
                    findings.append(Finding("harness-key-unknown", WARNING,
                                            "`ameesh.acp.%s` : clé inconnue (ignorée)" % key,
                                            path))
            if acp.get("auth_method") is not None and _text(acp.get("auth_method")) is None:
                findings.append(Finding("harness-acp-invalid", ERROR,
                                        "`ameesh.acp.auth_method` : texte attendu", path))
            config = acp.get("config")
            if config is not None and (not isinstance(config, dict) or not all(
                    isinstance(k, str) and k in ("model", "effort", "tier")
                    and isinstance(v, str) for k, v in config.items())):
                findings.append(Finding("harness-acp-invalid", ERROR,
                                        "`ameesh.acp.config` : objet model|effort|tier → "
                                        "identifiant d'option attendu", path))
    if "install" in ameesh:
        _check_shape(ameesh.get("install"), "install", findings, path)
    backup = ameesh.get("backup")
    if backup is not None:
        if not isinstance(backup, dict):
            findings.append(Finding("harness-backup-invalid", ERROR,
                                    "`ameesh.backup` : objet attendu", path))
        else:
            for key in backup:
                if key not in BACKUP_KEYS:
                    findings.append(Finding("harness-key-unknown", WARNING,
                                            "`ameesh.backup.%s` : clé inconnue (ignorée)" % key,
                                            path))
            files = backup.get("session_files")
            if files is not None and not (
                    isinstance(files, list) and files
                    and all(isinstance(f, str) and "{session}" in f and not f.startswith("/")
                            and ".." not in f.split("/") for f in files)):
                findings.append(Finding(
                    "harness-backup-invalid", ERROR,
                    "`ameesh.backup.session_files` : liste non vide de motifs relatifs au "
                    "dossier du compte, chacun avec `{session}`, sans `..`", path))
    accounts = ameesh.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, dict):
            findings.append(Finding("harness-accounts-invalid", ERROR,
                                    "`ameesh.accounts` : objet attendu", path))
        else:
            for key, value in accounts.items():
                if key not in ACCOUNT_KEYS:
                    findings.append(Finding("harness-key-unknown", WARNING,
                                            "`ameesh.accounts.%s` : clé inconnue (%s)"
                                            % (key, " | ".join(ACCOUNT_KEYS)), path))
                elif not isinstance(value, str) or not value:
                    findings.append(Finding("harness-accounts-invalid", ERROR,
                                            "`ameesh.accounts.%s` : texte non vide attendu" % key,
                                            path))
    return findings


def _check_shape(value: Any, key: str, findings: list[Finding], path: str) -> None:
    """Forme minimale d'un objet déclaratif (`hooks`, `install`)."""
    if value is None:
        return
    if not isinstance(value, dict):
        findings.append(Finding("harness-%s-invalid" % key, ERROR,
                                "`ameesh.%s` : objet attendu" % key, path))


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == ERROR]


# --------------------------------------------------------------------------
# chargement
# --------------------------------------------------------------------------

def dataclass_replace_sha(descriptor: HarnessDescriptor, digest: str) -> HarnessDescriptor:
    """Le descripteur avec son empreinte de fichier (audit)."""
    return dataclasses.replace(descriptor, sha256=digest)


def _setting(value: Any) -> Setting:
    if not isinstance(value, dict):
        return Setting()
    flag = _strings(value.get("flag")) or []
    default = value.get("default")
    default = str(default) if isinstance(default, (str, int, float)) \
        and not isinstance(default, bool) else None
    delivery = value.get("file")
    return Setting(flag=tuple(flag), file=delivery if isinstance(delivery, dict) else None,
                   default=default)


def _build(document: dict, path: str, source: str) -> HarnessDescriptor:
    ameesh = document["ameesh"]
    protocol = ameesh.get("protocol")
    caps = _normalize_capabilities(document.get("capabilities")) if "capabilities" in document \
        else {}
    auth = _normalize_authentication(document.get("authentication")) \
        if "authentication" in document else {}
    interactive: tuple[str, ...] | None
    if "interactive" in ameesh:
        raw = ameesh.get("interactive")
        interactive = None if raw is None else tuple(_strings(raw) or [])
    else:
        # en ACP, l'attachement n'a de sens que s'il est déclaré : un binaire
        # d'agent lancé sans son client attend indéfiniment sur son entrée.
        interactive = None if protocol == "acp" else ()
    session = ameesh.get("session") if isinstance(ameesh.get("session"), dict) else {}
    stream = ameesh.get("stream")
    if protocol == "acp":
        stream = "acp-json"
    env = {k: str(v) for k, v in (ameesh.get("env") or {}).items()} \
        if isinstance(ameesh.get("env"), dict) else {}
    return HarnessDescriptor(
        id=_text(document.get("id")) or "",
        name=_text(document.get("name")) or "",
        version=str(document.get("version") or ""),
        schema_version=str(document.get("schema_version") or ""),
        description=_text(document.get("description")) or "",
        repository=_text(document.get("repository")) or "",
        license=_text(document.get("license")) or "",
        authors=tuple(_strings(document.get("authors")) or ()),
        icon=_text(document.get("icon")) or "",
        capabilities=caps or {},
        authentication=auth or {},
        distribution=document["distribution"] if isinstance(document.get("distribution"), dict)
        else {},
        ameesh=dict(ameesh),
        path=path,
        source=source,
        document=document,
        protocol=protocol if protocol in PROTOCOLS else "cli",
        binary=_text(ameesh.get("binary")) or "",
        binary_env=tuple(_strings(ameesh.get("binary_env")) or ()),
        launcher=tuple(_strings(ameesh.get("launcher")) or ()),
        command=tuple(_strings(ameesh.get("command")) or ()),
        interactive=interactive,
        session_flag=tuple(_strings(session.get("flag")) or []),
        env=env,
        stream=stream if stream in STREAM_FORMATS else "text",
        model=_setting(ameesh.get("model")),
        effort=_setting(ameesh.get("effort")),
        tier=_setting(ameesh.get("tier")),
        hooks=ameesh.get("hooks") if isinstance(ameesh.get("hooks"), dict) else {},
        cost=ameesh.get("cost") if isinstance(ameesh.get("cost"), dict) else {},
        permissions=ameesh.get("permissions")
        if isinstance(ameesh.get("permissions"), dict) else {},
        acp=ameesh.get("acp") if isinstance(ameesh.get("acp"), dict) else {},
        accounts={k: str(v) for k, v in (ameesh.get("accounts") or {}).items()
                  if isinstance(k, str) and isinstance(v, str)}
        if isinstance(ameesh.get("accounts"), dict) else {},
        session_files=tuple(
            f for f in ((ameesh.get("backup") or {}).get("session_files") or [])
            if isinstance(f, str))
        if isinstance(ameesh.get("backup"), dict) else (),
    )


def load(path: str, *, source: str = "",
         root: str | None = None) -> HarnessDescriptor:
    """Charge un descripteur ; lève `DescriptorError` s'il est illisible ou invalide.

    `root` est la racine d'hôte explicite de l'appelant : la lecture sûre se
    fait contre **cette** racine, pas contre `host_dir()` (codex2).
    """
    tag = source or _source_of(path)
    try:
        # Octets lus **une fois**, sans suivi de lien pour l'hôte : l'empreinte
        # et l'analyse portent exactement sur ce qui a été lu (chaîne de
        # confiance complète, verdict codex2).
        brut = _read_descriptor(path, root)
        document = json.loads(brut.decode("utf-8"))
    except DescriptorError:
        raise
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise DescriptorError("descripteur illisible %s : %s" % (path, exc),
                              [Finding("harness-unreadable", ERROR, str(exc), path)])
    findings = validate(document, path=path)
    blocking = errors(findings)
    if blocking:
        raise DescriptorError("descripteur invalide %s : %s"
                              % (path, "; ".join(f.message for f in blocking)), findings)
    descriptor = _build(document, path, tag or "paquet")
    return dataclass_replace_sha(descriptor, hashlib.sha256(brut).hexdigest())


def scan(host: str | None = None) -> tuple[dict[str, HarnessDescriptor], list[Finding]]:
    """Tous les descripteurs connus, et les constats des fichiers ignorés.

    L'hôte prime sur le paquet pour un même `id` (épinglage local). Un fichier
    invalide n'est jamais « connu » : il ne peut donc pas être réclamé par un
    agent, et `harness list|check` le signale.
    """
    out: dict[str, HarnessDescriptor] = {}
    findings: list[Finding] = []
    host = host_dir() if host is None else host
    problem = host_dir_problem(host)
    if problem:
        findings.append(Finding("harness-host-insecure", WARNING, problem, host))
    directories = [(package_dir(), "paquet"), (host, "hôte")]
    seen_host = False
    for directory, source in directories:
        if source == "hôte":
            if seen_host:
                continue
            seen_host = True
            if problem:
                continue  # dossier refusé : aucun descripteur n'en vient
        for path in _sources(directory, findings if source == "hôte" else None):
            if source == "hôte":
                pb = host_file_problem(path)
                if pb:
                    findings.append(Finding("harness-host-insecure", WARNING, pb, path))
                    continue
            try:
                descriptor = load(path, source=source,
                                  root=host if source == "hôte" else None)
            except DescriptorError as exc:
                findings.extend(exc.findings or [Finding("harness-unreadable", ERROR, str(exc), path)])
                continue
            previous = out.get(descriptor.id)
            if previous is not None and previous.source == source:
                findings.append(Finding(
                    "harness-id-duplicate", ERROR,
                    "descripteur %s déclaré deux fois dans la source %s (%s et %s)"
                    % (descriptor.id, source, previous.path, path), path))
                continue
            if previous is not None and previous.source == "paquet" and source == "hôte":
                findings.append(Finding(
                    "harness-host-overrides", WARNING,
                    "le descripteur de l'hôte %s masque celui du paquet %s pour l'id %s"
                    % (path, previous.path, descriptor.id), path))
            out[descriptor.id] = descriptor  # l'hôte écrase le paquet
    return out, findings


def get(harness: str, host: str | None = None) -> HarnessDescriptor | None:
    """Le descripteur d'un identifiant, ou None s'il n'est pas connu."""
    return scan(host=host)[0].get(harness)


def known_ids(host: str | None = None) -> list[str]:
    return sorted(scan(host=host)[0])


def paid_per_token(host: str | None = None) -> tuple[str, ...]:
    """Les harnais dont le descripteur déclare l'usage payé au jeton (0019 §2)."""
    return tuple(sorted(i for i, d in scan(host=host)[0].items() if d.paid_per_token))


def check_path(path: str, root: str | None = None) -> tuple[HarnessDescriptor | None, list[Finding]]:
    """Valide un fichier de descripteur ; renvoie le descripteur s'il est valide.

    `root` force la racine de lecture sûre quand l'appelant la connaît (l'hôte
    explicite) ; sinon c'est la racine de l'environnement qui décide.
    """
    try:
        brut = _read_descriptor(path, root)
        document = json.loads(brut.decode("utf-8"))
    except DescriptorError as exc:
        return None, exc.findings or [Finding("harness-unreadable", ERROR, str(exc), path)]
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, [Finding("harness-unreadable", ERROR, str(exc), path)]
    findings = validate(document, path=path)
    if errors(findings):
        return None, findings
    descriptor = _build(document, path, _source_of(path))
    return dataclass_replace_sha(descriptor, hashlib.sha256(brut).hexdigest()), findings
