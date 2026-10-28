#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Composition scriptée du paquet horaire, sans jugement (#3807).

    scripts/paquet-auto.py --target main --name h-20261027-01 --carrier 3808
    scripts/paquet-auto.py --replay b57c0380 --base f444955b \\
        --name h-20261026-31

Le paquet horaire du canon §11 bis/§12 était composé à la main, en enchaînant
des outils qui existaient déjà (constat h-30/h-31 du 26/10 : h-30 a coûté
5 h 12 et quatre compositions). Ce script enchaîne les mêmes gestes, dans
l'ordre, sans agent :

1. un *worktree* jetable à la tête exacte d'`origin/<cible>` (SHA noté) ;
2. `scripts/paquet-candidats.py`, exécuté **depuis ce worktree** (le code de
   la cible, leçon #3767/#3782), avec `--base` égal à cette tête : la liste
   calculée, P1 d'abord, est la seule source des admissions ;
3. pour chaque candidat, `scripts/paquet-compose.py --lot N --source S
   --record-package <porteur>` ; un conflit est annulé (`git merge --abort`),
   le lot est écarté avec le motif « conflit, coupure suivante » et le script
   passe au suivant. Aucune résolution : résoudre est un jugement, il reste à
   un agent ;
4. les contrôles locaux : `scripts/freeze-checks.py`, puis
   `tests/parts/toujours.txt`, `tests.test_l161f_plan`, `tests.test_cluster`
   et les modules de test apportés par chaque unité. Un test ciblé **rouge**
   est attribué par la même lecture que #3801 (`packet_red`) : l'unité
   désignée par une preuve forte est retirée, son auteur est prévenu, et le
   paquet est recomposé **une seule fois** sans elle (#3931). Un rouge non
   attribuable garde le geste antérieur : une unité dont le module propre a
   rougi est retirée, un contrat rouge arrête (l'attribution demande un
   agent). Un run **non concluant** (délai, base absente, psql/psycopg
   indisponible, rc inattendu) est consigné au manifeste, à la PR et au
   courrier, sans retirer d'unité ni arrêter le paquet : le canon §3 le veut,
   la ci-full tranche ;
5. le manifeste `reviews/integration-paquet-<nom>.md` : base et SHA de la
   cible, table `## Composition` produite par `paquet-compose.py --units`
   (gabarit #3629), admissions, écartés et leur motif, contrôles locaux ;
6. `scripts/paquet-inclusion.py` sur la tête finale, puis `ameesh work packet
   record` (liaison des unités au porteur) ;
7. poussée de `integration/paquet-<nom>`, PR ouverte ou mise à jour, gel
   `ameesh work milestone <porteur> frozen --sha <tête>`, courrier au pilote.

Ce qui reste au pilote, hors de ce script : « verdict non requis », la CI
complète (`ameesh ci run --full`), le feu et toute fusion sur la cible. Le
script ne fusionne jamais la cible et ne déclare aucun verdict.

Modes :

* normal : composition réelle, poussée, PR, gel et courrier ;
* `--local` : s'arrête après le manifeste et `packet record`, sans poussée,
  PR, gel ni courrier (composition de contrôle) ;
* `--replay <réf> [--candidates-json <sortie paquet-candidats --json>]` :
  rejoue la table `## Composition` d'un manifeste déjà gelé depuis `--base`,
  reprend le manifeste de `--réf` tel quel (métadonnée, aucun effet sur le
  contenu composé) et compare l'arbre obtenu à `git rev-parse <réf>^{tree}`.
  Avec `--candidates-json`, chaque unité doit être prête et aucune en erreur
  (#3813) : h-31 v3 (`b57c0380`, #3506 en erreur) est refusé, h-31 v4
  (`fc59a14e`, 6 unités) passe et rend l'arbre `2d15e224…` de `fc59a14e`.
  Sans le JSON, c'est le témoin de fidélité d'arbre seul. Aucune poussée,
  aucune écriture en base.

Codes de retour : 0 composition réussie (ou aucun lot prêt), 1 échec
opérationnel (git, test, contrôle, poussée, PR, gel, courrier), 2 entrée
invalide.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

#: branche de paquet et manifeste, pour un `--name` (ex. `h-20261027-01`)
PACKAGE_PREFIX = "integration/paquet-"
MANIFEST_DIR = "reviews"
MANIFEST_PREFIX = "integration-paquet-"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"[0-9a-f]{7,64}", re.IGNORECASE)
CONTRACT_ALWAYS = "tests/parts/toujours.txt"
CONTRACT_MODULES = ("tests.test_l161f_plan", "tests.test_cluster")
COMPOSE_SCRIPT = "scripts/paquet-compose.py"
CANDIDATES_SCRIPT = "scripts/paquet-candidats.py"
INCLUSION_SCRIPT = "scripts/paquet-inclusion.py"
#: #3916 : les remises (`ameesh.resolutions.REF_PREFIX`) et leur copie locale
REMISE_PREFIX = "refs/ameesh/resolutions/"
REMISE_LOCAL = "refs/ameesh/paquet-auto/remise/"
FREEZE_CHECKS_SCRIPT = "scripts/freeze-checks.py"
DEFAULT_TIMEOUT = 1800
FREEZE_TIMEOUT = 600
GIT_TIMEOUT = 300
#: un rouge local n'est retiré du paquet qu'une fois ; au-delà, un agent lit.
MAX_RECOMPOSITIONS = 1
#: sorties qui ne prouvent pas un rouge du lot (environnement, base absente)
INCONCLUSIVE_RE = re.compile(
    r"(OperationalError|could not connect|connection refused|"
    r"server closed the connection|database system is starting up|"
    r"psql: error|No such file or directory: 'psql'|"
    r"could not translate host name|Temporary failure in name resolution|"
    r"ModuleNotFoundError: No module named 'psycopg)",
    re.IGNORECASE,
)
#: le **message** d'une exception d'environnement (sans les noms de types :
#: un `AssertionError: expected OperationalError` reste un rouge réel)
ENV_MESSAGE_RE = re.compile(
    r"could not connect|connection refused|server closed the connection|"
    r"database system is starting up|psql: error|"
    r"No such file or directory: 'psql'|"
    r"could not translate host name|Temporary failure in name resolution|"
    r"No module named 'psycopg'",
    re.IGNORECASE,
)
#: une ligne de résumé unittest
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
OK_RE = re.compile(r"^OK(?: \(skipped=(\d+)\))?", re.MULTILINE)
FAILED_SUMMARY_RE = re.compile(r"^FAILED \((.*?)\)", re.MULTILINE)
FAIL_HEADER_RE = re.compile(r"^FAIL: ", re.MULTILINE)
ERROR_HEADER_RE = re.compile(r"^ERROR: .*$", re.MULTILINE)
EXCEPTION_LINE_RE = re.compile(
    r"^([\w.]+(?:Error|Exception)):\s*(.*)$", re.MULTILINE)
BLOCK_END_RE = re.compile(r"^=+$", re.MULTILINE)
#: le seuil de preuve forte de #3801, le même que `paquet_horaire.classify`
STRONG_EVIDENCE = 4
#: l'outil qui rend l'attribution `packet_red` d'un rouge de composition
ATTRIBUTION_SCRIPT = "scripts/paquet-attribution.py"
#: #4025 : budget, en secondes (durées psycopg de la carte), des modules que
#: la carte d'impact ajoute à une unité ; les modules `support`, déterministes,
#: ne sont pas plafonnés
IMPACT_BUDGET_S = 180
#: un en-tête d'échec unittest (`FAIL: test (tests.test_x.T.t)`) ; une ligne
#: `FAIL:` produite par le test lui-même n'a pas ce nom pointé et est ignorée
FAIL_LINE_RE = re.compile(
    r"^(FAIL|ERROR):\s+.+?\(((?:tests\.|unittest\.loader\.)[^()]*)\)",
    re.MULTILINE)
TEST_MODULE_RE = re.compile(r"(?:^|\.)tests\.(test_[A-Za-z0-9_]+)")


class PaquetAutoError(RuntimeError):
    """Un geste du script a échoué ; le message porte l'état atteint."""


class InputError(PaquetAutoError):
    """Un argument ou un état du dépôt ne permet pas de composer."""


class GitError(PaquetAutoError):
    """Une commande git a échoué."""


@dataclass
class Candidate:
    """Un lot prêt de la liste calculée, avec la source exacte à composer."""

    lot: int
    source: str
    branch: str = ""
    title: str = ""
    #: #3916 : la ref de la remise (`refs/ameesh/resolutions/<lot>/…`) quand
    #: la source en est une ; elle n'est sur aucune branche, on la fetch.
    remise_ref: str = ""


@dataclass
class Unit:
    """Une unité de fusion composée."""

    lot: int
    source: str
    commit: str


@dataclass
class Discarded:
    """Un lot prêt non composé, avec le motif écrit au manifeste."""

    lot: int
    reason: str
    detail: str = ""


@dataclass
class Composition:
    units: list[Unit]
    discarded: list[Discarded]
    head: str
    #: #4025 : la provenance de la sélection des modules voisins (carte)
    selection: str = ""


@dataclass
class Removal:
    """Une unité retirée sur un test ciblé rouge, avec sa preuve."""

    lot: int
    tests: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    attributed: bool = False


@dataclass
class TestRun:
    """Une exécution locale de unittest."""

    label: str
    modules: list[str]
    returncode: int | None
    output: str
    seconds: float
    timed_out: bool = False
    #: #4025 : les modules voisins ajoutés (`module`, `reason`, `files`)
    added: list = field(default_factory=list)
    #: Rouge écarté après rejeu des seuls voisins sur la base (#4048).
    ignored_red: bool = False
    log_path: str = ""

    @property
    def green(self) -> bool:
        return self.returncode == 0

    def _summary(self) -> dict[str, int]:
        found = FAILED_SUMMARY_RE.search(self.output)
        if not found:
            return {}
        return {name: int(value) for name, value in
                re.findall(r"(\w+)=(\d+)", found.group(1))}

    @staticmethod
    def _error_is_environment(block: str) -> bool:
        """La dernière exception du bloc est-elle une panne d'environnement ?

        Le **nom du type** ne suffit pas : `AssertionError: expected
        OperationalError` est un rouge réel. On lit le message de la dernière
        exception, et `AssertionError` est toujours réelle. Sans ligne
        d'exception (sortie psql), on cherche un motif d'infrastructure.
        """
        lines = EXCEPTION_LINE_RE.findall(block)
        if lines:
            kind, message = lines[-1]
            name = kind.rsplit(".", 1)[-1]
            if name == "AssertionError":
                return False
            if ENV_MESSAGE_RE.search(message):
                return True
            if name in ("ModuleNotFoundError", "ImportError") and "psycopg" in message:
                return True
            return False
        return bool(INCONCLUSIVE_RE.search(block))

    @property
    def real_failure(self) -> bool:
        """Un test négatif réel, même mêlé à une erreur d'environnement.

        Un `FAIL:` (assertion) est toujours réel. Un `ERROR:` n'est une panne
        d'environnement que si **sa** dernière exception matche les motifs
        d'infrastructure ; sinon il est réel. Un run mixte
        `FAIL + OperationalError` est donc rouge, jamais « non concluant ».
        """
        if self.returncode == 0:
            return False
        if self.timed_out or self.returncode != 1:
            return False
        if FAIL_HEADER_RE.search(self.output):
            return True
        headers = list(ERROR_HEADER_RE.finditer(self.output))
        env_error = False
        for header in headers:
            rest = self.output[header.end():]
            end = BLOCK_END_RE.search(rest)
            block = rest[:end.start()] if end else rest
            if self._error_is_environment(block):
                env_error = True
            else:
                return True
        summary = self._summary()
        failures = int(summary.get("failures", 0))
        errors = int(summary.get("errors", 0))
        if failures:
            return True
        if errors and not env_error:
            return True
        if not headers and not summary and not INCONCLUSIVE_RE.search(self.output):
            # rc 1 sans en-tête ni résumé ni motif connu : un rouge, jamais
            # un non-concluant présumé.
            return True
        return False

    @property
    def inconclusive(self) -> bool:
        if self.timed_out or self.returncode not in (0, 1):
            return True
        if self.returncode == 1:
            return not self.real_failure
        return False

    @property
    def tests(self) -> int:
        found = RAN_RE.search(self.output)
        return int(found.group(1)) if found else 0

    @property
    def skipped(self) -> int:
        found = OK_RE.search(self.output)
        return int(found.group(1) or 0) if found else 0

    def motive(self) -> str:
        """Pourquoi un run est non concluant, en une ligne."""
        if self.timed_out:
            return "délai dépassé"
        if self.returncode not in (0, 1):
            return "code de retour %s" % self.returncode
        found = INCONCLUSIVE_RE.search(self.output)
        return _one_line(found.group(0)) if found else "résultat non concluant"

    def excerpt(self, lines: int = 3) -> str:
        """L'extrait : la ligne qui porte le motif, puis la fin de la sortie."""
        raw = [line.strip() for line in self.output.splitlines() if line.strip()]
        found = INCONCLUSIVE_RE.search(self.output)
        chosen: list[str] = []
        if found:
            chosen = [line for line in raw if found.group(0) in line][:1]
        chosen += raw[-lines:]
        return " | ".join(dict.fromkeys(chosen))


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def git(repo: Path, *args: str, check: bool = True,
        input_text: str | None = None, timeout: int = GIT_TIMEOUT
        ) -> subprocess.CompletedProcess:
    """Lance `git -C repo …`, sans terminal interactif."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git introuvable : %s" % exc) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("git %s : délai de %d s dépassé" % (" ".join(args), timeout)) from exc
    if check and proc.returncode != 0:
        raise GitError(
            "git %s : %s" % (" ".join(args), _decode(proc.stderr).strip() or
                             "code %d" % proc.returncode))
    return proc


def out(repo: Path, *args: str, check: bool = True) -> str:
    return _decode(git(repo, *args, check=check).stdout).strip()


def rev(repo: Path, ref: str) -> str | None:
    """Le commit désigné par `ref`, ou None s'il n'existe pas."""
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}",
               check=False)
    return _decode(proc.stdout).strip() if proc.returncode == 0 else None


def tree_of(repo: Path, ref: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{tree}",
               check=False)
    return _decode(proc.stdout).strip() if proc.returncode == 0 else None


def run_command(cmd: list[str], *, cwd: Path | None = None,
                input_text: str | None = None, timeout: int | None = None,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """Lance une commande non-git, sortie texte, sans shell."""
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise PaquetAutoError("commande introuvable : %s" % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise PaquetAutoError(
            "commande %s : délai de %s s dépassé" % (cmd[0], timeout)) from exc


def _output(proc: subprocess.CompletedProcess) -> str:
    return (_decode(proc.stdout) + _decode(proc.stderr)).strip()


def _tail(text: str, lines: int = 40) -> str:
    kept = text.rstrip().splitlines()
    if len(kept) > lines:
        kept = ["[… %d lignes au-dessus]" % (len(kept) - lines)] + kept[-lines:]
    return "\n".join(kept)


def python_env(worktree: Path) -> dict:
    """L'environnement des outils exécutés depuis l'arbre jetable."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


_PYTHON_INTERPRETER: str | None = None
_PYTHON_OVERRIDE: str | None = None


def _supports_psycopg(python: str) -> bool:
    try:
        proc = subprocess.run([python, "-c", "import psycopg"],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def project_python(worktree: Path) -> str:
    """Un interpréteur qui porte psycopg, pour les outils de l'arbre.

    Le script lui-même n'a que la bibliothèque standard ; ses enfants
    (`paquet-candidats`, `paquet-compose`, les tests) importent le projet.
    Un agent qui lance `python3 scripts/paquet-auto.py` avec le Python du
    système n'a pas psycopg : on prend le `.venv` de l'arbre, celui du
    worktree principal, ou le Python du venv d'`ameesh` (le wrapper de la
    session). À défaut, `sys.executable` : `paquet-candidats` rendra son
    refus, sans le maquiller.
    """
    global _PYTHON_INTERPRETER
    if _PYTHON_OVERRIDE:
        return _PYTHON_OVERRIDE
    if _PYTHON_INTERPRETER:
        return _PYTHON_INTERPRETER
    candidates: list[str] = []
    env = os.environ.get("AMEESH_PYTHON")
    if env:
        candidates.append(env)
    candidates.append(sys.executable)
    candidates.append(str(worktree / ".venv" / "bin" / "python"))
    listed = git(worktree, "worktree", "list", "--porcelain", check=False)
    for line in _decode(listed.stdout).splitlines():
        if line.startswith("worktree "):
            candidates.append(str(Path(line[len("worktree "):]) /
                                 ".venv" / "bin" / "python"))
    wrapper = shutil.which("ameesh")
    if wrapper:
        try:
            text = Path(wrapper).read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = re.search(r"exec\s+(\S+)/ameesh\b", text)
        if match:
            candidates.append(match.group(1) + "/python")
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate).is_file() and _supports_psycopg(candidate):
            _PYTHON_INTERPRETER = candidate
            return candidate
    _PYTHON_INTERPRETER = sys.executable
    return _PYTHON_INTERPRETER


# ---------------------------------------------------------------------------
# Entrées
# ---------------------------------------------------------------------------


def package_branch(name: str) -> str:
    return PACKAGE_PREFIX + name


def manifest_rel(name: str) -> str:
    return "%s/%s%s.md" % (MANIFEST_DIR, MANIFEST_PREFIX, name)


def validate_name(name: str) -> None:
    if not name or not NAME_RE.fullmatch(name) or ".." in name:
        raise InputError(
            "--name : un nom de paquet sûr attendu (lettres, chiffres, `.`, `_`, "
            "`-` ; pas de `..`), reçu %r" % name)


def validate_branch(repo: Path, branch: str, label: str = "branche") -> None:
    if not branch or branch.startswith("-"):
        raise InputError("%s : une branche attendue, reçu %r" % (label, branch))
    proc = git(repo, "check-ref-format", "--branch", branch, check=False)
    if proc.returncode != 0:
        raise InputError("%s : branche invalide %r" % (label, branch))


def validate_target(repo: Path, target: str) -> None:
    validate_branch(repo, target, "--target")


def fresh_base(repo: Path, target: str) -> str:
    """La tête fraîche d'`origin/<cible>` ; le SHA exact est noté."""
    proc = git(repo, "fetch", "--no-tags", "origin", target, check=False,
               timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "fetch origin %s : %s" % (target, _decode(proc.stderr).strip()))
    sha = rev(repo, "origin/" + target) or rev(repo, "FETCH_HEAD")
    if not sha:
        raise PaquetAutoError("origin/%s introuvable après fetch" % target)
    return sha


def remote_branch(repo: Path, branch: str) -> str | None:
    """La tête distante de `branch`, ou None (réseau indisponible = doute)."""
    proc = git(repo, "ls-remote", "--heads", "origin",
               "refs/heads/" + branch, check=False, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "ls-remote origin %s : %s" % (branch, _decode(proc.stderr).strip()))
    text = _decode(proc.stdout).strip()
    return text.split()[0] if text else None


# ---------------------------------------------------------------------------
# Worktree jetable
# ---------------------------------------------------------------------------


def make_worktree(repo: Path, base_sha: str, branch: str) -> Path:
    if rev(repo, "refs/heads/" + branch):
        raise InputError(
            "branche locale %s déjà présente : la supprimer ou choisir un autre "
            "--name" % branch)
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-", dir=str(parent)))
    path.rmdir()
    proc = git(repo, "worktree", "add", "-b", branch, str(path), base_sha,
               check=False)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise PaquetAutoError(
            "worktree %s : %s" % (path, _decode(proc.stderr).strip()))
    return path


def _worktrees_on(repo: Path, branch: str) -> list[Path]:
    """Les worktrees du dépôt ouverts sur la branche locale `branch`."""
    listing = out(repo, "worktree", "list", "--porcelain")
    paths: list[Path] = []
    path = None
    for line in listing.splitlines():
        line = line.strip()
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == "branch refs/heads/" + branch and path is not None:
            paths.append(path)
    return paths


def clear_local_branch(repo: Path, branch: str, log=print) -> None:
    """Retire une branche locale de recomposition.

    #3916 : un worktree `paquet-auto-*` gardé par la tentative précédente sur
    cette branche (diagnostic d'échec, déjà dans son courrier) est fermé ;
    tout autre worktree ouvert dessus refuse la recomposition.
    """
    if rev(repo, "refs/heads/" + branch) is None:
        return
    for path in _worktrees_on(repo, branch):
        if path == repo or not path.name.startswith("paquet-auto-"):
            raise InputError(
                "branche locale %s encore ouverte dans un worktree (%s) : le "
                "fermer avant de recomposer" % (branch, path))
        log("worktree gardé par la tentative précédente fermé : %s" % path)
        remove_worktree(repo, path)
    if _worktrees_on(repo, branch):
        raise InputError(
            "branche locale %s encore ouverte dans un worktree : le fermer "
            "avant de recomposer" % branch)
    git(repo, "branch", "-D", branch)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def prune_stale_worktrees(repo: Path, max_age_hours: float,
                          *, now: float | None = None) -> list[str]:
    """N1 (revue #3807) : borne les worktrees d'échec gardés pour diagnostic.

    Un échec garde son worktree (`--keep-worktree` ou diagnostic) ; une
    minuterie horaire ne doit pas accumuler des checkouts complets. Seuls les
    chemins `paquet-auto-*` **du dépôt** sont retirés, au-delà de
    `max_age_hours` (0 = jamais), puis `worktree prune` ; la branche locale
    reste (petite), le prochain run peut la supprimer avec `--force`.
    """
    if max_age_hours <= 0:
        return []
    cutoff = (time.time() if now is None else float(now)) - max_age_hours * 3600
    listing = out(repo, "worktree", "list", "--porcelain", check=False)
    removed: list[str] = []
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        if path == repo or not path.name.startswith("paquet-auto-"):
            continue
        try:
            stamp = max(path.stat().st_ctime, path.stat().st_mtime)
        except OSError:
            continue
        if stamp >= cutoff:
            continue
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
    if removed:
        git(repo, "worktree", "prune", check=False)
    return removed


# ---------------------------------------------------------------------------
# Liste calculée
# ---------------------------------------------------------------------------


def _errors_by_lot(payload: dict) -> list[Discarded]:
    """Les lots en erreur de lecture du nouveau format (#3813)."""
    entries = payload.get("errors_by_lot")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise PaquetAutoError(
            "paquet-candidats : errors_by_lot n'est pas une liste")
    out: list[Discarded] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PaquetAutoError(
                "paquet-candidats : entrée errors_by_lot illisible : %r" % (entry,))
        try:
            lot = int(entry["work_item"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaquetAutoError(
                "paquet-candidats : errors_by_lot sans work_item : %r" % (entry,)) from exc
        if lot <= 0:
            raise PaquetAutoError(
                "paquet-candidats : errors_by_lot, lot invalide %r" % lot)
        error = " ".join(str(entry.get("error") or "").split())
        out.append(Discarded(
            lot, "erreur paquet-candidats, jamais composé",
            error or "motif non fourni"))
    return out


def error_discards(payload: dict) -> list[Discarded]:
    """Les lots que `paquet-candidats` rend en erreur, jamais composés."""
    return _errors_by_lot(payload)


def compute_candidates(worktree: Path, target: str, base_sha: str,
                       ready_out: Path) -> tuple[list[Candidate], dict]:
    """`paquet-candidats.py` **du worktree**, à la base exacte du paquet.

    #3813 : rc 3 est une liste utilisable avec des lots en erreur ; ces lots
    sont écartés (`errors_by_lot`), jamais composés. rc 1/2, ou un rc 3 sans
    `errors_by_lot`, interrompent : on ne compose pas sur une liste dont la
    lecture globale a échoué.
    """
    script = worktree / CANDIDATES_SCRIPT
    if not script.is_file():
        raise PaquetAutoError(
            "%s absent de l'arbre %s : la liste calculée ne peut pas être lue"
            % (CANDIDATES_SCRIPT, worktree))
    python = project_python(worktree)
    cmd = [python, str(script), "--repo", str(worktree),
           "--target", target, "--base", base_sha, "--json",
           "--ready-out", str(ready_out)]
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    stdout = _decode(proc.stdout)
    payload = None
    if stdout.lstrip().startswith("{"):
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
    if proc.returncode not in (0, 3):
        if payload and payload.get("errors"):
            raise PaquetAutoError(
                "paquet-candidats : liste inutilisable (rc %d), on ne compose "
                "pas dessus : %s"
                % (proc.returncode,
                   "; ".join(str(error) for error in payload["errors"])))
        raise PaquetAutoError(
            "paquet-candidats a refusé (rc %d) : %s"
            % (proc.returncode, _tail(_output(proc))))
    if payload is None:
        raise PaquetAutoError(
            "paquet-candidats : sortie JSON illisible : %s"
            % _tail(stdout or _decode(proc.stderr)))
    if payload.get("schema") != "ameesh-packet-candidates/1":
        raise PaquetAutoError(
            "paquet-candidats : schéma inattendu %r" % payload.get("schema"))
    errored = _errors_by_lot(payload)
    if proc.returncode == 3 and not errored:
        raise PaquetAutoError(
            "paquet-candidats : rc 3 sans errors_by_lot (format antérieur à "
            "#3813), on ne compose pas sur une liste dont l'export est partiel")
    if proc.returncode == 0 and payload.get("errors"):
        raise PaquetAutoError(
            "paquet-candidats : liste incohérente (rc 0 avec des erreurs), on "
            "ne compose pas dessus : %s"
            % "; ".join(str(error) for error in payload["errors"]))
    if payload.get("base_source") != "option":
        raise PaquetAutoError(
            "paquet-candidats n'a pas lu la base imposée (%r) : la liste ne "
            "correspond pas au worktree" % payload.get("base_source"))
    error_lots = {item.lot for item in errored}
    by_id = {int(entry["work_item"]): entry for entry in payload.get("candidates") or []}
    candidates: list[Candidate] = []
    for lot in payload.get("ready") or []:
        lot = int(lot)
        if lot in error_lots:
            continue
        entry = by_id.get(lot)
        if entry is None:
            raise PaquetAutoError("paquet-candidats : #%d prêt sans entrée" % lot)
        source = str(entry.get("source") or entry.get("published_head") or "").strip()
        if not source:
            raise PaquetAutoError("paquet-candidats : #%d prêt sans source" % lot)
        remise = entry.get("remise")
        remise_ref = str(remise.get("ref") or "") if isinstance(remise, dict) else ""
        candidates.append(Candidate(
            lot=lot, source=source, branch=str(entry.get("branch") or ""),
            title=str(entry.get("title") or ""), remise_ref=remise_ref))
    return candidates, payload

def validate_remise_ref(repo: Path, ref: str, lot: int) -> None:
    """#3916 : une remise du lot, sous `refs/ameesh/resolutions/<lot>/`."""
    prefix = "%s%d/" % (REMISE_PREFIX, lot)
    if not ref.startswith(prefix) or len(ref) == len(prefix):
        raise InputError("remise du candidat #%d : ref inattendue %r" % (lot, ref))
    if git(repo, "check-ref-format", ref, check=False).returncode != 0:
        raise InputError("remise du candidat #%d : ref invalide %r" % (lot, ref))


def ensure_source(worktree: Path, candidate: Candidate) -> None:
    """Résout la source dans le clone ; la fetch au besoin (remise, sinon
    branche)."""
    sha = rev(worktree, candidate.source)
    if sha is None and candidate.remise_ref:
        # #3916 : une remise n'est sur aucune branche : fetcher la branche ne
        # l'apporte jamais ; on fetch sa ref, validée, sous un nom local.
        validate_remise_ref(worktree, candidate.remise_ref, candidate.lot)
        local = REMISE_LOCAL + candidate.remise_ref[len(REMISE_PREFIX):]
        git(worktree, "fetch", "--no-tags", "origin",
            "+%s:%s" % (candidate.remise_ref, local), check=False,
            timeout=GIT_TIMEOUT)
        sha = rev(worktree, candidate.source)
    if sha is None and candidate.branch:
        # N2 (revue #3807) : la branche vient de la base, jamais d'un argument
        # de commande ; `git fetch` la reçoit après validation, pas avant.
        validate_branch(worktree, candidate.branch,
                        "branche du candidat #%d" % candidate.lot)
        git(worktree, "fetch", "--no-tags", "origin", candidate.branch, check=False)
        sha = rev(worktree, candidate.source)
    if sha is None:
        raise PaquetAutoError(
            "source %s de #%d introuvable dans le clone : la branche%s n'a "
            "pas été fetchée (ou la source a été réécrite)"
            % (candidate.source[:12], candidate.lot,
               " ou la remise %s" % candidate.remise_ref
               if candidate.remise_ref else ""))
    candidate.source = sha


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _compose(worktree: Path, candidate: Candidate, carrier: int,
             compose_script: Path, record_package: bool) -> subprocess.CompletedProcess:
    cmd = [project_python(worktree), str(compose_script), "--repo", str(worktree),
           "--lot", str(candidate.lot), "--source", candidate.source]
    if record_package:
        cmd += ["--record-package", str(carrier)]
    return run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))


def _abort_merge(worktree: Path, before: str) -> None:
    proc = git(worktree, "merge", "--abort", check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "conflit : `git merge --abort` a échoué : %s"
            % _decode(proc.stderr).strip())
    head = rev(worktree, "HEAD")
    if head != before:
        raise PaquetAutoError(
            "conflit : la tête a bougé pendant l'annulation (%s → %s)"
            % (before[:12], (head or "?")[:12]))
    if rev(worktree, "MERGE_HEAD") is not None:
        raise PaquetAutoError("conflit : MERGE_HEAD subsiste après l'annulation")
    status = out(worktree, "status", "--porcelain")
    if status:
        raise PaquetAutoError(
            "conflit : l'arbre n'est pas propre après l'annulation :\n%s" % status)


def compose_candidates(worktree: Path, candidates: list[Candidate], carrier: int,
                       *, compose_script: Path, record_package: bool,
                       log=print) -> Composition:
    """Compose les candidats dans l'ordre ; un conflit est écarté, jamais résolu."""
    units: list[Unit] = []
    discarded: list[Discarded] = []
    for candidate in candidates:
        ensure_source(worktree, candidate)
        before = rev(worktree, "HEAD")
        if before is None:
            raise PaquetAutoError("worktree sans commit")
        if git(worktree, "merge-base", "--is-ancestor", candidate.source, before,
               check=False).returncode == 0:
            discarded.append(Discarded(
                candidate.lot,
                "source déjà portée par une unité précédente",
                "aucune unité propre : le contenu est déjà dans le paquet"))
            log("écarté #%d : source déjà portée par une unité précédente"
                % candidate.lot)
            continue
        proc = _compose(worktree, candidate, carrier, compose_script, record_package)
        if proc.returncode == 0:
            commit = rev(worktree, "HEAD")
            if commit is None or commit == before:
                raise PaquetAutoError(
                    "#%d : paquet-compose a rendu 0 sans nouveau commit" % candidate.lot)
            units.append(Unit(candidate.lot, candidate.source, commit))
            log("unité %d/%d : #%d %s → %s"
                % (len(units), len(candidates), candidate.lot,
                   candidate.source[:12], commit[:12]))
            continue
        stderr = _decode(proc.stderr).strip()
        if proc.returncode == 1:
            files = [line.split(":", 1)[1].strip()
                     for line in stderr.splitlines() if line.strip().startswith("conflit :")]
            _abort_merge(worktree, before)
            detail = "fichiers : %s" % ", ".join(files) if files else ""
            discarded.append(Discarded(candidate.lot, "conflit, coupure suivante", detail))
            log("écarté #%d : conflit, coupure suivante%s"
                % (candidate.lot, (" (%s)" % detail) if detail else ""))
            continue
        after = rev(worktree, "HEAD")
        # Un échec de la seule liaison en base ne défait pas la composition
        # (#2443) : le commit existe, `packet record` le reprendra à la fin.
        if after != before and "liaison au paquet" in stderr:
            units.append(Unit(candidate.lot, candidate.source, after))
            log("unité %d/%d : #%d %s → %s (liaison en base différée)"
                % (len(units), len(candidates), candidate.lot,
                   candidate.source[:12], after[:12]))
            continue
        raise PaquetAutoError(
            "#%d : paquet-compose a refusé (rc %d) : %s"
            % (candidate.lot, proc.returncode, _tail(stderr)))
    head = rev(worktree, "HEAD")
    if head is None:
        raise PaquetAutoError("worktree sans commit après composition")
    return Composition(units=units, discarded=discarded, head=head)


def recompose(worktree: Path, base_sha: str, candidates: list[Candidate],
              carrier: int, *, compose_script: Path, record_package: bool,
              log=print) -> Composition:
    """Repart de la base exacte et recompose, sans les unités fautives."""
    git(worktree, "reset", "--hard", base_sha)
    git(worktree, "clean", "-fd")
    return compose_candidates(worktree, candidates, carrier,
                              compose_script=compose_script,
                              record_package=record_package, log=log)


# ---------------------------------------------------------------------------
# Contrôles locaux
# ---------------------------------------------------------------------------


def contract_modules(worktree: Path) -> list[str]:
    """La liste « toujours », `test_l161f_plan` et `test_cluster`."""
    path = worktree / CONTRACT_ALWAYS
    if not path.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % CONTRACT_ALWAYS)
    modules: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        modules.append(name if name.startswith("tests.") else "tests." + name)
    modules.extend(CONTRACT_MODULES)
    return _dedupe_modules(worktree, modules)


def unit_test_modules(worktree: Path, unit: Unit) -> list[str]:
    """Les modules `tests/test_*.py` apportés par la fusion de l'unité.

    La fusion est comparée à sa première parente : c'est le delta propre de
    l'unité dans le paquet (aucune résolution locale, par construction). Un
    module supprimé par l'unité n'est pas exécuté.
    """
    proc = git(worktree, "diff", "--name-only", unit.commit + "^1", unit.commit,
               check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "unité #%d : diff illisible : %s"
            % (unit.lot, _decode(proc.stderr).strip()))
    modules = []
    for path in _decode(proc.stdout).splitlines():
        path = path.strip()
        if re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", path) \
                and (worktree / path).is_file():
            modules.append(path[:-3].replace("/", "."))
    return _dedupe_modules(worktree, modules)


def neighbor_modules(worktree: Path, composition: Composition, base_sha: str,
                     *, budget: int = IMPACT_BUDGET_S) -> tuple[dict, str]:
    """#4025 : les modules voisins de chaque unité, et la note de la carte.

    `paquet-attribution.py --select` de l'arbre composé rend, par unité, les
    modules qui importent un module de test qu'elle modifie (`support`) et
    ceux que la carte d'impact relie aux blocs `src/` qu'elle change
    (`carte`, dans `budget` secondes). Un outil absent ou illisible n'ajoute
    rien et le dit : les tests propres et les contrats restent joués, la
    ci-full tranche.
    """
    script = worktree / ATTRIBUTION_SCRIPT
    if not composition.units:
        return {}, ""
    if not script.is_file():
        return {}, "modules voisins non sélectionnés : %s absent" % ATTRIBUTION_SCRIPT
    payload = {
        "schema": "ameesh-paquet-selection-request/1",
        "base": base_sha, "budget": budget,
        "units": [{"lot": unit.lot, "unit_sha": unit.commit}
                  for unit in composition.units],
    }
    try:
        proc = run_command(
            [project_python(worktree), str(script), "--select",
             "--repo", str(worktree)],
            cwd=worktree, input_text=json.dumps(payload),
            timeout=GIT_TIMEOUT, env=python_env(worktree))
        report = json.loads(_decode(proc.stdout).strip() or "{}")
    except (PaquetAutoError, ValueError) as exc:
        return {}, "modules voisins non sélectionnés : %s" % _one_line(str(exc))[:200]
    if proc.returncode != 0 or not isinstance(report, dict) \
            or report.get("status") != "ok":
        why = (report.get("why") if isinstance(report, dict) else "") \
            or _tail(_output(proc), 3) or "sortie illisible"
        return {}, "modules voisins non sélectionnés : %s" % _one_line(why)[:200]
    out: dict[int, list[dict]] = {}
    dropped = 0
    spent = 0.0
    for entry in report.get("units") or []:
        lot = int(entry.get("lot") or 0)
        rows = []
        for row in entry.get("added") or []:
            module = str(row.get("module") or "")
            if not re.fullmatch(r"tests\.test_[A-Za-z0-9_]+", module) \
                    or not (worktree / (module.replace(".", "/") + ".py")).is_file():
                continue
            rows.append({"module": module, "reason": str(row.get("reason") or ""),
                         "files": [str(path) for path in row.get("files") or []]})
        out.setdefault(lot, []).extend(rows)
        dropped += int(entry.get("dropped") or 0)
        spent += float(entry.get("seconds") or 0)
    card = report.get("card") or {}
    if card.get("commit"):
        note = "carte d'impact `%s` (%s, %s)" % (
            str(card["commit"])[:12], card.get("status") or "lue",
            {"à jour": "à la base", "antérieure": "antérieure à la base",
             }.get(card.get("base"), "%s par rapport à la base"
                   % (card.get("base") or "inconnue")))
    else:
        note = "carte d'impact absente (%s) : seuls les modules `support` " \
            "sont ajoutés" % (card.get("why") or card.get("status") or "?")
    note += " ; budget de carte %d s par unité, %.0f s pris" % (budget, spent)
    if dropped:
        note += ", %d module(s) au-delà du budget non joué(s)" % dropped
    return out, note


def base_failures(worktree: Path, base_sha: str, modules: list[str], *,
                  timeout: int) -> set[str] | None:
    """#4025 : les tests de `modules` rouges sur la base, ou None (non concluant).

    Un module voisin rouge ne désigne l'unité que s'il est vert sur la base :
    un rouge aussi présent sur la base vient de l'environnement de l'hôte ou
    lui préexiste. Les modules sont rejoués dans un worktree détaché de la base.
    """
    if not base_sha or not modules:
        return None
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-base-", dir=str(parent)))
    path.rmdir()
    proc = git(worktree, "worktree", "add", "--detach", str(path), base_sha,
               check=False)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None
    try:
        present = [module for module in modules
                   if (path / (module.replace(".", "/") + ".py")).is_file()]
        if not present:
            return set()
        run = run_tests(path, "base", present, timeout=timeout)
        if run.inconclusive:
            return None
        return {failure["test"] for failure in unittest_failures(run.output)}
    finally:
        remove_worktree(worktree, path)


def _dedupe_modules(worktree: Path, modules) -> list[str]:
    out: list[str] = []
    for module in modules:
        if module in out:
            continue
        path = worktree / (module.replace(".", "/") + ".py")
        if not path.is_file():
            raise PaquetAutoError(
                "module de test %s absent de l'arbre composé (%s)" % (module, path))
        out.append(module)
    return out


def save_test_log(run: TestRun) -> TestRun:
    """Preuve locale privée, hors arbre jetable ; 48 h et 100 séries au plus."""
    from ameesh.config import load

    root = Path(load().state_dir) / "roles" / "mesh-design" / "test-logs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = sorted(root.glob("series-*.log"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    cutoff = time.time() - 48 * 3600
    for index, path in enumerate(files):
        if index >= 99 or path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="series-",
                                     suffix=".log", dir=root, delete=False) as log:
        log.write("%s\nmodules: %s\nreturncode: %s\nseconds: %.3f\n\n%s"
                  % (run.label, ", ".join(run.modules), run.returncode,
                     run.seconds, run.output))
        run.log_path = log.name
    return run


def run_tests(worktree: Path, label: str, modules: list[str], *,
              timeout: int) -> TestRun:
    if not modules:
        return TestRun(label=label, modules=[], returncode=0, output="",
                       seconds=0.0)
    start = time.monotonic()
    try:
        proc = run_command(
            [project_python(worktree), "-m", "unittest", "-q", *modules],
            cwd=worktree, timeout=timeout, env=python_env(worktree))
        result = TestRun(label=label, modules=list(modules),
                       returncode=proc.returncode, output=_output(proc),
                       seconds=time.monotonic() - start)
    except PaquetAutoError as exc:
        result = TestRun(label=label, modules=list(modules), returncode=None,
                       output=str(exc), seconds=time.monotonic() - start,
                       timed_out=True)
    return save_test_log(result)


def run_freeze_checks(worktree: Path, *, timeout: int) -> tuple[int, str, float]:
    script = worktree / FREEZE_CHECKS_SCRIPT
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % FREEZE_CHECKS_SCRIPT)
    start = time.monotonic()
    proc = run_command([project_python(worktree), str(script)], cwd=worktree,
                       timeout=timeout, env=python_env(worktree))
    return proc.returncode, _output(proc), time.monotonic() - start


def unittest_failures(output: str) -> list[dict]:
    """Les échecs d'une sortie unittest, au format des échecs de CI (#3801).

    `FAIL: test_x (tests.test_module.Class.test_x)` et
    `ERROR: setUpClass (tests.test_module.Class)` donnent le même nom pointé
    que la CI ; un sous-test (`... (i=1)`) ne change pas le nom. Une
    `ERROR:` d'environnement (psql/psycopg absent) n'est pas un rouge du lot :
    elle est ignorée, comme `TestRun.real_failure` la classe.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for match in FAIL_LINE_RE.finditer(output or ""):
        kind, test = match.group(1), match.group(2).strip()
        if not test or test in seen:
            continue
        if kind == "ERROR":
            rest = (output or "")[match.end():]
            end = BLOCK_END_RE.search(rest)
            block = rest[:end.start()] if end else rest
            if TestRun._error_is_environment(block):
                continue
        seen.add(test)
        module = ""
        found = TEST_MODULE_RE.search(test)
        if found:
            module = found.group(1)
        out.append({"test": test, "module": module})
    return out


def attribute_composition(worktree: Path, composition: Composition,
                          failures: list[dict]) -> dict:
    """La lecture #3801 du rouge, par l'outil de l'arbre composé (#3931).

    Le helper lit la carte d'impact localement et applique
    `packet_red.attribute_tests` : même lecture que le rouge de ci-full. Un
    helper absent ou illisible ne conclut pas (`status` le dit) ; l'appelant
    garde alors l'échec actuel, sans retrait inventé.
    """
    script = worktree / ATTRIBUTION_SCRIPT
    if not script.is_file():
        return {"status": "no-helper", "tests": [],
                "why": "%s absent de l'arbre composé" % ATTRIBUTION_SCRIPT}
    payload = {
        "schema": "ameesh-paquet-attribution-request/1",
        "units": [{"lot": unit.lot, "unit_sha": unit.commit}
                  for unit in composition.units],
        "failures": failures,
    }
    try:
        proc = run_command(
            [project_python(worktree), str(script), "--repo", str(worktree)],
            cwd=worktree, input_text=json.dumps(payload),
            timeout=GIT_TIMEOUT, env=python_env(worktree))
    except PaquetAutoError as exc:
        return {"status": "error", "tests": [], "why": str(exc)}
    text = _decode(proc.stdout).strip()
    try:
        report = json.loads(text) if text else {}
    except ValueError:
        report = {}
    if proc.returncode != 0 or not isinstance(report, dict):
        return {"status": "error", "tests": [],
                "why": _tail(_output(proc), 5) or "sortie illisible"}
    return report


def designated_removals(attribution: dict, failures: list[dict], *,
                        known_lots=None) -> tuple[list[Removal], str]:
    """Les unités désignées sans ambiguïté pour **tous** les échecs.

    Même règle que `paquet_horaire.classify` (#3801) : chaque échec est
    `probable`, sa meilleure preuve atteint `STRONG_EVIDENCE`, et une seule
    unité est à égalité à ce poids. Un seul échec non attribuable annule le
    retrait : on ne retire pas une unité sur une lecture partielle.
    `known_lots` refuse une unité qui ne fait pas partie de la composition.
    """
    if attribution.get("status") != "ok":
        return [], (str(attribution.get("why") or "")
                    or "attribution %s" % (attribution.get("status") or "absente"))
    entries = attribution.get("tests") or []
    if len(entries) != len(failures):
        return [], ("attribution incomplète (%d échec(s), %d entrée(s))"
                    % (len(failures), len(entries)))
    expected = {str(failure.get("test") or "") for failure in failures}
    seen: set[str] = set()
    by_lot: dict[int, Removal] = {}
    for entry in entries:
        name = str(entry.get("test") or "?")
        if name not in expected:
            return [], "entrée %s hors des échecs" % name
        if name in seen:
            return [], "entrée %s en double" % name
        seen.add(name)
        if entry.get("status") != "probable":
            return [], "test %s non attribuable" % name
        units = entry.get("units") or []
        top = max((int(unit.get("best") or 0) for unit in units), default=0)
        best = [unit for unit in units if int(unit.get("best") or 0) == top]
        if top < STRONG_EVIDENCE:
            return [], "test %s : preuve faible (poids %d)" % (name, top)
        if len(best) != 1:
            return [], "test %s : %d unités à égalité" % (name, len(best))
        unit = best[0]
        lot = int(unit["lot"])
        if known_lots is not None and lot not in known_lots:
            return [], "unité #%d hors de la composition" % lot
        removal = by_lot.setdefault(lot, Removal(lot=lot, attributed=True))
        if name not in removal.tests:
            removal.tests.append(name)
        for item in unit.get("evidence") or []:
            label = " ".join((str(item.get("kind") or ""),
                              str(item.get("file") or ""))).strip()
            if label and label not in removal.evidence:
                removal.evidence.append(label)
    return list(by_lot.values()), ""


def check_local_tests(worktree: Path, composition: Composition, *,
                      timeout: int, log=print,
                      attribute=None, base_sha: str = "",
                      select=None, on_base=None) -> tuple[list[TestRun], list[int],
                                                          list[Removal]]:
    """Exécute les modules des unités puis les contrats.

    Rend `(exécutions, unités fautives, retraits)`. Une unité est fautive
    quand un test ciblé la désigne :
    * par attribution #3801 (`packet_red`) quand **chaque** échec est attribué
      sans ambiguïté à une seule unité avec une preuve forte (#3931) ;
    * sinon par le geste antérieur : le module propre de l'unité a rougi, et un
      contrat rouge non attribuable arrête.

    Un résultat non concluant (délai, base absente, psql/psycopg indisponible,
    rc inattendu) est **consigné** dans `runs` et ne retire aucune unité : le
    canon §3 veut qu'il ne retienne ni paquet ni release ; la ci-full tranche.
    `attribute` est injectable pour les témoins (`attribute_composition`).

    #4025 : la série d'une unité joue aussi ses modules voisins
    (`neighbor_modules`, injectable par `select`). Dans le geste antérieur,
    un rouge d'un module `carte` ne désigne l'unité que si aucune autre ne
    l'a sélectionné ; un rouge partagé reste à l'attribution et à la ci-full.
    Un rouge de module voisin aussi rouge sur la base (`base_failures`,
    injectable par `on_base`) ne compte pas : environnement ou préexistant.
    """
    runs: list[TestRun] = []
    red: list[tuple[TestRun, list[dict]]] = []
    own: dict[int, list[dict]] = {}
    neighbors, composition.selection = (select or neighbor_modules)(
        worktree, composition, base_sha)
    shared: dict[str, int] = {}
    for rows in neighbors.values():
        for module in {row["module"] for row in rows}:
            shared[module] = shared.get(module, 0) + 1
    if composition.selection:
        log("tests voisins : %s" % composition.selection)
    pending: list[tuple[Unit, TestRun, list[dict], set[str], set[str]]] = []
    for unit in composition.units:
        proper = unit_test_modules(worktree, unit)
        added = []
        for row in neighbors.get(unit.lot, []):
            if row["module"] not in proper \
                    and all(row["module"] != done["module"] for done in added):
                added.append(row)
        modules = proper + [row["module"] for row in added]
        if not modules:
            log("tests unité #%d : aucun module propre" % unit.lot)
            continue
        if added:
            log("tests unité #%d : %d module(s) voisin(s) : %s"
                % (unit.lot, len(added),
                   ", ".join("%s (%s)" % (row["module"], row["reason"])
                             for row in added)))
        run = run_tests(worktree, "unité #%d" % unit.lot, modules, timeout=timeout)
        run.added = added
        runs.append(run)
        if run.inconclusive:
            log("tests unité #%d : NON CONCLUANT (%s) — consigné, le paquet "
                "continue" % (unit.lot, run.motive()))
            continue
        if not run.green:
            found = unittest_failures(run.output)
            mine = {module.split(".", 1)[1] for module in proper}
            neighbor = {row["module"].split(".", 1)[1] for row in added}
            mine |= {row["module"].split(".", 1)[1] for row in added
                     if row["reason"] != "carte" or shared.get(row["module"]) == 1}
            pending.append((unit, run, found, mine, neighbor))
            log("tests unité #%d : ROUGE (%d modules)\n%s"
                % (unit.lot, len(modules), _tail(run.output)))
        else:
            log("tests unité #%d : vert (%d modules, %d tests)"
                % (unit.lot, len(modules), run.tests))
    neighbor_red = sorted({"tests." + failure["module"]
                           for _unit, _run, found, _mine, neighbor in pending
                           for failure in found
                           if failure.get("module") in neighbor})
    at_base: set[str] | None = set()
    if neighbor_red:
        at_base = (on_base or base_failures)(worktree, base_sha, neighbor_red,
                                             timeout=timeout)
        log("tests voisins rouges rejoués sur la base %s : %s"
            % (base_sha[:12] or "?", "non concluant" if at_base is None
               else "%d rouge(s) aussi sur la base" % len(at_base)))
    for unit, run, found, mine, neighbor in pending:
        kept = []
        for failure in found:
            if failure.get("module") in neighbor \
                    and (at_base is None or failure["test"] in at_base):
                continue
            kept.append(failure)
        owned = [failure for failure in kept
                 if not failure.get("module") or failure["module"] in mine]
        if owned or not found:
            own[unit.lot] = owned
        if kept or not found:
            red.append((run, kept))
        else:
            run.ignored_red = True
            log("tests unité #%d : rouge des seuls voisins, aussi rouges sur la "
                "base ou non rejoués — sans effet" % unit.lot)
    modules = contract_modules(worktree)
    contract = run_tests(worktree, "contrats", modules, timeout=timeout)
    runs.append(contract)
    contract_red = False
    if contract.inconclusive:
        log("tests contrats : NON CONCLUANT (%s) — consigné, le paquet "
            "continue" % contract.motive())
    elif not contract.green:
        contract_red = True
        red.append((contract, unittest_failures(contract.output)))
        log("tests contrats : ROUGE (%d modules)\n%s"
            % (len(modules), _tail(contract.output)))
    else:
        log("tests contrats : vert (%d modules, %d tests)"
            % (len(modules), contract.tests))
    if not red:
        return runs, [], []
    failures: list[dict] = []
    seen_failures: set[str] = set()
    for _run, found in red:
        for failure in found:
            name = str(failure.get("test") or "")
            if name and name in seen_failures:
                continue
            if name:
                seen_failures.add(name)
            failures.append(failure)
    why = ""
    removals: list[Removal] = []
    if failures and all(found for _run, found in red):
        reader = attribute or attribute_composition
        report = reader(worktree, composition, failures)
        removals, why = designated_removals(
            report, failures,
            known_lots={unit.lot for unit in composition.units})
    else:
        why = "un rouge sans nom de test attribuable"
    if removals:
        log("tests ciblés : retrait de %s (%s)"
            % (", ".join("#%d" % removal.lot for removal in removals),
               why or "attribution #3801"))
        return runs, [removal.lot for removal in removals], removals
    if contract_red and not own:
        raise PaquetAutoError(
            "tests de contrat rouges sur la tête composée (aucune unité retirée "
            "automatiquement : %s) :\n%s"
            % (why or "attribution incertaine", _tail(contract.output)))
    removals = [Removal(lot=lot, tests=[failure["test"] for failure in found])
                for lot, found in own.items()]
    if contract_red:
        log("tests contrats : rouge non attribuable (%s) — retrait des unités "
            "aux tests propres rouges, puis recomposition"
            % (why or "attribution incertaine"))
    if removals:
        log("tests ciblés : retrait de %s (module propre rouge, sans "
            "attribution sûre)"
            % ", ".join("#%d" % removal.lot for removal in removals))
    return runs, [removal.lot for removal in removals], removals


# ---------------------------------------------------------------------------
# Manifeste
# ---------------------------------------------------------------------------


def composition_table(worktree: Path, base_sha: str,
                      compose_script: Path | None = None) -> str:
    """La table `## Composition` produite par `paquet-compose.py --units`."""
    script = Path(compose_script) if compose_script else (worktree / COMPOSE_SCRIPT)
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % COMPOSE_SCRIPT)
    proc = run_command([project_python(worktree), str(script), "--repo",
                        str(worktree), "--units", "--base", base_sha],
                       cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    if proc.returncode != 0:
        raise PaquetAutoError(
            "paquet-compose --units a échoué : %s" % _tail(_output(proc)))
    text = _decode(proc.stdout)
    start = text.find("## Composition")
    if start < 0:
        raise PaquetAutoError(
            "paquet-compose --units n'a pas rendu la table ## Composition :\n%s"
            % _tail(text))
    return text[start:].rstrip() + "\n"


def _one_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _cell(value: str) -> str:
    """Une cellule de tableau markdown : ni saut de ligne ni `|`."""
    return _one_line(value).replace("|", "/")


def build_manifest(*, name: str, target: str, base_sha: str, carrier: int,
                   composition: Composition, table: str,
                   tests: list[TestRun], freeze_seconds: float,
                   ready_count: int = 0, waiting: int = 0,
                   errors: int = 0) -> str:
    """Le manifeste §12, lisible par `paquet-inclusion.py` (#1644/#2409).

    La table d'admissions ne porte que le lot et sa source : un id cité dans
    un objet (`#N`) serait relu par le contrôle d'inclusion comme un lot
    attendu. Les objets et les classes restent à la PR et au courrier.
    """
    lots = len({unit.lot for unit in composition.units})
    lines = [
        "# Paquet %s — composition (§12), sur %s `%s`"
        % (package_branch(name), target, base_sha[:12]),
        "",
        "Base : `%s` **`%s`** (tête exacte d'`origin/%s`). Branche `%s`, cible `%s`."
        % (target, base_sha, target, package_branch(name), target),
        "Porteur : **#%d**. Composeur : `scripts/paquet-auto.py` (sans agent)."
        % carrier,
        "",
        "## Admissions (%d unités, %d lots)" % (len(composition.units), lots),
        "",
        "| lot | source composée |",
        "|---|---|",
    ]
    for unit in composition.units:
        lines.append("| #%d | `%s` |" % (unit.lot, unit.source))
    lines += ["", table.rstrip(), ""]
    lines += [
        "## Écartés",
        "",
        "| lot | motif |",
        "|---|---|",
    ]
    if composition.discarded:
        for item in composition.discarded:
            motive = item.reason
            if item.detail:
                motive += " — " + _cell(item.detail)
            lines.append("| #%d | %s |" % (item.lot, motive))
    else:
        lines.append("| — | aucun |")
    lines += [
        "",
        "Liste calculée : %d lot(s) prêt(s), %d en attente, %d en erreur "
        "(les erreurs sont écartées ci-dessus, jamais composées)."
        % (ready_count, waiting, errors),
        "",
        "## Contrôles locaux",
        "",
        "- `scripts/freeze-checks.py` : vert en %.1f s." % freeze_seconds,
        "- Tests locaux : %s." % _tests_summary(tests),
    ]
    lines += _neighbor_lines(composition, tests)
    inconclusive = _inconclusive_lines(tests)
    if inconclusive:
        lines += [
            "",
            "Non concluants (canon §3 : ne retiennent ni paquet ni release) :",
            "",
        ] + inconclusive
    lines += [
        "",
        "- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le "
        "manifeste (résultat au courrier du pilote).",
        "",
    ]
    return "\n".join(lines)


def _tests_summary(tests: list[TestRun]) -> str:
    modules = sorted({module for run in tests for module in run.modules})
    total = sum(run.tests for run in tests)
    skipped = sum(run.skipped for run in tests)
    red = sum(1 for run in tests if not run.green and not run.inconclusive
              and not run.ignored_red)
    ignored = sum(1 for run in tests if run.ignored_red)
    inconclusive = sum(1 for run in tests if run.inconclusive)
    return ("%d séries, %d modules, %d tests exécutés, %d échec(s), "
            "%d run(s) rouge(s) sans effet, %d non concluant(s), %d sauté(s) : %s"
            % (len(tests), len(modules), total, red, ignored, inconclusive, skipped,
               ", ".join("`%s`" % module for module in modules) or "aucun"))


def _neighbor_lines(composition: Composition, tests: list[TestRun]) -> list[str]:
    """#4025 : les modules voisins joués par unité, avec leur raison."""
    rows = []
    for run in tests:
        for row in run.added:
            rows.append("| %s | `%s` | %s | %s |" % (
                run.label.replace("#", ""), row["module"], row["reason"],
                _cell(", ".join("`%s`" % path for path in row["files"][:3])
                      + (" …" if len(row["files"]) > 3 else ""))))
    if not rows and not composition.selection:
        return []
    lines = ["", "Modules voisins joués (support importé, carte d'impact) : %s."
             % (composition.selection or "sélection sans note"), ""]
    if rows:
        lines += ["| unité | module | raison | fichiers de l'unité |",
                  "|---|---|---|---|"] + rows
    else:
        lines.append("Aucun module voisin ajouté.")
    return lines


def _inconclusive_lines(tests: list[TestRun]) -> list[str]:
    """Une ligne par run non concluant : module, motif, extrait (canon §3)."""
    lines: list[str] = []
    for run in tests:
        if not run.inconclusive:
            continue
        modules = ", ".join(run.modules) or "aucun module"
        lines.append("- NON CONCLUANT : %s — modules %s — %s — extrait : %s%s"
                     % (run.label.replace("#", ""), modules, run.motive(),
                        _one_line(run.excerpt())[:240],
                        (" — journal local : " + run.log_path) if run.log_path else ""))
    return lines


def write_manifest(worktree: Path, rel: str, text: str, *,
                   name: str, units: int, discarded: int) -> str:
    path = worktree / rel
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    git(worktree, "add", "--", rel)
    message = ("paquet %s : manifeste — %d unité(s), %d écarté(s)"
               % (name, units, discarded))
    proc = git(worktree, "commit", "-m", message, check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "commit du manifeste : %s" % _decode(proc.stderr).strip())
    head = rev(worktree, "HEAD")
    if head is None:
        raise PaquetAutoError("manifeste commité sans tête")
    return head


# ---------------------------------------------------------------------------
# Inclusion, paquet, poussée, PR, gel, courrier
# ---------------------------------------------------------------------------


def run_inclusion(worktree: Path, manifest: Path, head: str,
                  ready_file: Path, expected_file: Path) -> tuple[int, str]:
    script = worktree / INCLUSION_SCRIPT
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % INCLUSION_SCRIPT)
    cmd = [project_python(worktree), str(script), "--repo", str(worktree),
           "--manifest", str(manifest), "--head", head,
           "--ready-file", str(ready_file), "--expected-file", str(expected_file)]
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    return proc.returncode, _output(proc)


def record_packet(worktree: Path, carrier: int, base_sha: str, head: str,
                  branch: str, target: str) -> str:
    cmd = ["ameesh", "work", "packet", "record", str(carrier),
           "--repo", str(worktree), "--base", base_sha, "--head", head,
           "--branch", branch, "--target", target]
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "ameesh work packet record a échoué (rc %d) : %s"
            % (proc.returncode, _tail(_output(proc))))
    return _output(proc)


def ensure_carrier_branch(repo: Path, carrier: int, branch: str,
                          target: str) -> None:
    """Vérifie que le porteur est sur la branche du paquet ; la pose si vide."""
    proc = run_command(["ameesh", "work", "show", str(carrier), "--json"],
                       cwd=repo, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "porteur #%d illisible : %s" % (carrier, _tail(_output(proc))))
    try:
        item = json.loads(_decode(proc.stdout))
    except ValueError as exc:
        raise PaquetAutoError("porteur #%d : JSON illisible (%s)" % (carrier, exc)) from exc
    current = str(item.get("branch") or "").strip()
    if current == branch:
        return
    if current:
        raise InputError(
            "porteur #%d est déjà sur %s, pas sur %s : détacher sa branche ou "
            "créer un autre porteur" % (carrier, current, branch))
    cmd = ["ameesh", "work", "branch", str(carrier), branch, "--target", target]
    proc = run_command(cmd, cwd=repo, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "ameesh work branch #%d %s a échoué : %s"
            % (carrier, branch, _tail(_output(proc))))


def push_branch(worktree: Path, branch: str,
                force_lease: str | None = None) -> str:
    cmd = ["push"]
    if force_lease:
        cmd.append("--force-with-lease=refs/heads/%s:%s" % (branch, force_lease))
    cmd += ["--set-upstream", "origin", branch]
    proc = git(worktree, *cmd, check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "git push origin %s : %s" % (branch, _tail(_decode(proc.stderr))))
    return _decode(proc.stderr).strip()


def _gh(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT
        ) -> subprocess.CompletedProcess:
    return run_command(["gh", *args], cwd=cwd, timeout=timeout)


def ensure_pull_request(worktree: Path, branch: str, target: str,
                        title: str, body: str) -> tuple[int | None, str]:
    """Ouvre ou met à jour la PR ; rend `(numéro, url)`."""
    view = _gh(worktree, "pr", "view", branch,
               "--json", "number,url,state")
    number: int | None = None
    url = ""
    if view.returncode == 0:
        try:
            data = json.loads(_decode(view.stdout))
            number, url = int(data["number"]), str(data.get("url") or "")
            state = str(data.get("state") or "").upper()
        except (ValueError, KeyError, TypeError) as exc:
            raise PaquetAutoError("gh pr view : réponse illisible (%s)" % exc) from exc
        if state and state != "OPEN":
            raise PaquetAutoError(
                "PR #%d pour %s est %s, pas ouverte : choisir un autre --name "
                "(un paquet fusionné ou fermé ne se réécrit pas)"
                % (number, branch, state))
    else:
        text = _output(view)
        if "no pull requests found" not in text.lower() \
                and "not found" not in text.lower():
            raise PaquetAutoError(
                "gh pr view %s a échoué : %s" % (branch, _tail(text)))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md",
                                     delete=False, dir=os.environ.get("TMPDIR")) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        if number is not None:
            proc = _gh(worktree, "pr", "edit", str(number), "--title", title,
                       "--body-file", str(body_path))
            if proc.returncode != 0:
                raise PaquetAutoError(
                    "gh pr edit #%d : %s" % (number, _tail(_output(proc))))
            return number, url
        proc = _gh(worktree, "pr", "create", "--base", target, "--head", branch,
                   "--title", title, "--body-file", str(body_path))
        if proc.returncode != 0:
            raise PaquetAutoError(
                "gh pr create : %s" % _tail(_output(proc)))
        url = _output(proc)
        if url.startswith("http"):
            view = _gh(worktree, "pr", "view", branch, "--json", "number")
            if view.returncode == 0:
                try:
                    number = int(json.loads(_decode(view.stdout))["number"])
                except (ValueError, KeyError, TypeError):
                    number = None
        return number, url
    finally:
        body_path.unlink(missing_ok=True)


def freeze_carrier(worktree: Path, carrier: int, head: str, note: str) -> str:
    cmd = ["ameesh", "work", "milestone", str(carrier), "frozen",
           "--sha", head, "--repo", str(worktree), "--note", note]
    proc = run_command(cmd, cwd=worktree, timeout=FREEZE_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "gel du porteur #%d refusé : %s" % (carrier, _tail(_output(proc))))
    return _output(proc)


def send_mail(recipient: str, lot: int, body: str,
              sender: str | None = None) -> str:
    cmd = ["agent-mail", "send", recipient, "-", "--lot", str(lot)]
    if sender:
        # #3845 : sous `ameesh role run`, AGENT_MAIL_NAME est retiré ; sans
        # --from, agent-mail refuse (« unbound sender »).
        cmd += ["--from", sender]
    proc = run_command(cmd, input_text=body, timeout=120)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "courrier à %s : %s" % (recipient, _tail(_output(proc))))
    return _output(proc)


def lot_author(lot: int, repo: Path) -> str:
    """L'assigné du lot, ou "" si la base ne le dit pas (jamais bloquant)."""
    try:
        proc = run_command(["ameesh", "work", "show", str(lot), "--json"],
                           cwd=repo, timeout=GIT_TIMEOUT)
    except PaquetAutoError:
        return ""
    if proc.returncode != 0:
        return ""
    try:
        item = json.loads(_decode(proc.stdout))
    except ValueError:
        return ""
    return str(item.get("assignee") or "").strip()


def notify_removals(repo: Path, name: str, removals: list[Removal], *,
                    sender: str | None, log=print) -> list[str]:
    """Prévient l'auteur de chaque unité retirée (§11 bis, #3931).

    Un courrier qui ne part pas n'annule pas la recomposition : il est
    consigné, et le courrier du pilote porte les lots retirés. Rend les
    échecs, pour la trace du tic.
    """
    failed: list[str] = []
    for removal in removals:
        author = lot_author(removal.lot, repo)
        if not author:
            log("auteur de #%d inconnu : courrier non envoyé" % removal.lot)
            continue
        tests = ", ".join(removal.tests[:3]) or "tests ciblés"
        if removal.attributed:
            why = ("les tests ciblés de la composition ont rougi sur la tête "
                   "composée, et la lecture #3801 désigne ton unité (%s)"
                   % (" ; ".join(removal.evidence[:3]) or "attribution #3801"))
        else:
            why = ("les tests ciblés de la composition ont rougi sur la tête "
                   "composée ; le module propre de ton unité est rouge "
                   "(retrait de repli, sans attribution #3801 sûre)")
        body = (
            "#%d retiré du paquet %s : %s. Tests : %s. Le paquet est recomposé "
            "sans ton lot ; corrige puis regèle, il reviendra à une coupure "
            "suivante."
            % (removal.lot, name, why, tests))
        try:
            send_mail(author, removal.lot, body, sender)
        except PaquetAutoError as exc:
            failed.append("#%d : %s" % (removal.lot, exc))
            log("courrier à %s (#%d) non envoyé : %s"
                % (author, removal.lot, exc))
        else:
            log("courrier à %s (#%d) envoyé" % (author, removal.lot))
    return failed


def pr_body(*, name: str, target: str, base_sha: str, carrier: int,
            composition: Composition, candidates: list[Candidate],
            tests: list[TestRun], inclusion_rc: int, freeze_seconds: float) -> str:
    by_lot = {candidate.lot: candidate for candidate in candidates}
    units = ", ".join("#%d" % unit.lot for unit in composition.units) or "aucun"
    discarded = ", ".join("#%d" % item.lot for item in composition.discarded) or "aucun"
    lines = [
        "ameesh-work: %d" % carrier,
        "",
        "Paquet horaire %s (§12), base `%s` `%s`, **%d unité(s)**."
        % (name, target, base_sha[:12], len(composition.units)),
        "",
        "- Unités : %s." % units,
        "- Écartés : %s." % discarded,
    ]
    for item in composition.discarded:
        detail = (" — " + _one_line(item.detail)) if item.detail else ""
        lines.append("  - #%d : %s%s." % (item.lot, item.reason, detail))
    for unit in composition.units:
        candidate = by_lot.get(unit.lot)
        title = _one_line(candidate.title if candidate else "")[:160]
        lines.append("- #%d `%s`%s" % (
            unit.lot, unit.source[:12], (": " + title) if title else ""))
    lines += [
        "- Tests locaux : %s." % _tests_summary(tests),
    ]
    lines += ["  " + line for line in _inconclusive_lines(tests)]
    lines += [
        "- Contrôle d'inclusion : %s." % ("vert" if inclusion_rc == 0 else "à lire"),
        "- Freeze-checks : vert (%.1f s)." % freeze_seconds,
        "- Manifeste : `%s`." % manifest_rel(name),
        "",
        "Porteur : #%d. « Verdict non requis » et CI complète restent au pilote."
        % carrier,
        "",
    ]
    return "\n".join(lines)


def mail_body(*, name: str, target: str, base_sha: str, carrier: int,
              head: str, branch: str, composition: Composition,
              tests: list[TestRun], inclusion_rc: int, pr_number: int | None,
              pr_url: str) -> str:
    units = ", ".join("#%d" % unit.lot for unit in composition.units) or "aucun"
    discarded = "; ".join(
        "#%d (%s)" % (item.lot, item.reason) for item in composition.discarded
    ) or "aucun"
    pr = ("PR #%d %s" % (pr_number, pr_url)) if pr_number else (pr_url or "PR non lue")
    inconclusive = _inconclusive_lines(tests)
    non_section = ("Non concluants (canon §3, consignés) :\n"
                   + "\n".join("  " + line for line in inconclusive)) \
        if inconclusive else "Non concluants : aucun."
    return """\
Paquet {name} composé sans agent : tête `{head}` sur {target} `{base}` (branche `{branch}`, {pr}).

- Unités ({n}) : {units}.
- Écartés ({d}) : {discarded}.
- Tests locaux : {tests}.
- {non_section}
- Inclusion : {inclusion}.
- Gel : `ameesh work milestone` posé sur la tête ; manifeste `{manifest}`.

« Verdict non requis » et la ci-full restent tes gestes ; aucune fusion sur {target}.
""".format(
        name=name, head=head, target=target, base=base_sha[:12],
        branch=branch, pr=pr, n=len(composition.units), units=units,
        d=len(composition.discarded), discarded=discarded,
        tests=_tests_summary(tests), non_section=non_section,
        inclusion="vert" if inclusion_rc == 0 else "à lire",
        manifest=manifest_rel(name),
    )


# ---------------------------------------------------------------------------
# Rejeu (témoin)
# ---------------------------------------------------------------------------


def parse_composition_table(text: str) -> list[tuple[list[int], str]]:
    """[(lots, source)] de la table `## Composition` d'un manifeste.

    Seule la forme du gabarit #3629 est lue (colonnes `lot(s)`, `source`,
    `commit`) ; toute autre table est ignorée.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("## Composition"):
            start = index
            break
    if start is None:
        raise InputError("manifeste de rejeu : aucune section `## Composition`")
    header = None
    for index in range(start, len(lines)):
        if lines[index].strip().startswith("|"):
            header = [cell.strip().lower() for cell in lines[index].strip().strip("|").split("|")]
            if any("lot" in cell for cell in header) \
                    and any("source" in cell for cell in header):
                start = index + 2
                break
            header = None
    if header is None:
        raise InputError("manifeste de rejeu : table `## Composition` introuvable")
    rows: list[tuple[list[int], str]] = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        lots = [int(found) for found in re.findall(r"#(\d+)", cells[1])]
        source_match = SHA_RE.search(cells[2])
        if not lots or not source_match:
            raise InputError("manifeste de rejeu : ligne de composition illisible : %s"
                             % line)
        rows.append((lots, source_match.group(0)))
    if not rows:
        raise InputError("manifeste de rejeu : table de composition vide")
    return rows


def _legacy_error_discards(payload: dict) -> list[Discarded]:
    """Les `errors` du format antérieur à #3813, quand ils nomment un lot."""
    out: list[Discarded] = []
    for text in payload.get("errors") or []:
        match = re.match(r"^\s*[#!](\d+)\s*:?\s*(.*)$", str(text))
        if not match:
            raise PaquetAutoError(
                "--candidates-json : erreur globale, la liste n'est pas "
                "utilisable : %s" % text)
        out.append(Discarded(
            int(match.group(1)), "erreur paquet-candidats, jamais composé",
            " ".join(match.group(2).split()) or str(text)))
    return out


def check_replay_admission(specs: list[tuple[list[int], str]], payload: dict,
                           base_sha: str, repo: Path) -> None:
    """#3813 : aucune unité du manifeste n'est en erreur ou hors des prêts."""
    stated = str(payload.get("base") or "").strip()
    if stated:
        resolved = rev(repo, stated)
        if resolved and resolved != base_sha:
            raise InputError(
                "--candidates-json : base %s, rejeu sur %s"
                % (resolved[:12], base_sha[:12]))
        if not resolved and not (stated.startswith(base_sha)
                                 or base_sha.startswith(stated)):
            raise InputError(
                "--candidates-json : base %r, rejeu sur %s"
                % (stated, base_sha[:12]))
    errors = _errors_by_lot(payload)
    if not errors:
        errors = _legacy_error_discards(payload)
    by_lot = {item.lot: item for item in errors}
    ready = {int(lot) for lot in payload.get("ready") or []}
    for lots, _source in specs:
        for lot in lots:
            if lot in by_lot:
                raise PaquetAutoError(
                    "rejeu refusé : #%d est en erreur dans paquet-candidats "
                    "(jamais composé) : %s"
                    % (lot, by_lot[lot].detail or by_lot[lot].reason))
            if lot not in ready:
                raise PaquetAutoError(
                    "rejeu refusé : #%d n'est pas dans la liste des lots prêts "
                    "de --candidates-json" % lot)


def replay(repo: Path, reference: str, name: str, base: str, *,
           compose_script: Path | None = None, keep: bool = False,
           candidates_json: Path | None = None, log=print) -> int:
    """Rejoue un manifeste gelé et compare l'arbre obtenu à celui de `reference`."""
    ref = rev(repo, reference)
    if ref is None:
        raise InputError("référence %r introuvable" % reference)
    manifest_path = manifest_rel(name)
    show = git(repo, "show", "%s:%s" % (ref, manifest_path), check=False)
    if show.returncode != 0:
        raise InputError(
            "manifeste %s introuvable dans %s : %s"
            % (manifest_path, ref[:12], _decode(show.stderr).strip()))
    manifest_text = _decode(show.stdout)
    specs = parse_composition_table(manifest_text)
    base_sha = rev(repo, base)
    if base_sha is None:
        raise InputError("base %r introuvable" % base)
    if candidates_json is not None:
        try:
            payload = json.loads(Path(candidates_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(
                "--candidates-json %s illisible : %s" % (candidates_json, exc)) from exc
        if not isinstance(payload, dict):
            raise InputError("--candidates-json : un objet JSON attendu")
        check_replay_admission(specs, payload, base_sha, repo)
    branch = "tmp/paquet-auto-rejeu-%d" % os.getpid()
    worktree = make_worktree(repo, base_sha, branch)
    try:
        script = compose_script or (worktree / COMPOSE_SCRIPT)
        if not Path(script).is_file():
            raise PaquetAutoError("%s introuvable pour le rejeu" % script)
        for lots, source in specs:
            candidate = Candidate(lot=lots[0], source=source)
            ensure_source(worktree, candidate)
            cmd = [project_python(worktree), str(script), "--repo", str(worktree),
                   "--source", candidate.source]
            for lot in lots:
                cmd += ["--lot", str(lot)]
            proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                               env=python_env(worktree))
            if proc.returncode != 0:
                raise PaquetAutoError(
                    "rejeu : composition de %s refusée : %s"
                    % ("/".join("#%d" % lot for lot in lots), _tail(_output(proc))))
            log("rejeu : %s ← %s" % ("/".join("#%d" % lot for lot in lots),
                                     candidate.source[:12]))
        path = worktree / manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest_text, encoding="utf-8")
        git(worktree, "add", "--", manifest_path)
        proc = git(worktree, "commit", "-m",
                   "paquet %s : manifeste de rejeu (%s)" % (name, ref[:12]),
                   check=False)
        if proc.returncode != 0:
            raise PaquetAutoError(
                "rejeu : commit du manifeste : %s" % _decode(proc.stderr).strip())
        head = rev(worktree, "HEAD")
        got = tree_of(worktree, "HEAD")
        expected = tree_of(repo, ref)
        if got is None or expected is None:
            raise PaquetAutoError("rejeu : arbre illisible")
        if got != expected:
            diff = git(worktree, "diff", "--stat", ref, "HEAD", check=False)
            raise PaquetAutoError(
                "rejeu : arbre différent de %s\nattendu %s\nobtenu  %s\n%s"
                % (ref[:12], expected, got, _decode(diff.stdout).strip()))
        log("rejeu conforme : tête %s, arbre %s = %s" % (head[:12], got, ref[:12]))
        return 0
    finally:
        if keep:
            log("worktree de rejeu conservé : %s" % worktree)
        else:
            remove_worktree(repo, worktree)
            git(repo, "branch", "-D", branch, check=False)


# ---------------------------------------------------------------------------
# Flux normal
# ---------------------------------------------------------------------------


def _write_id_file(path: Path, lots) -> None:
    path.write_text("\n".join("#%d" % lot for lot in lots) + ("\n" if lots else ""),
                    encoding="utf-8")


def compose_packet(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    validate_name(args.name)
    validate_target(repo, args.target)
    if args.carrier <= 0:
        raise InputError("--carrier : un identifiant de lot positif attendu")
    branch = package_branch(args.name)
    validate_branch(repo, branch, "--name")
    rel = manifest_rel(args.name)
    base_sha = fresh_base(repo, args.target)
    print("paquet %s : base %s %s" % (args.name, args.target, base_sha), flush=True)
    remote_sha: str | None = None
    if not args.no_packet_record:
        remote_sha = remote_branch(repo, branch)
        if remote_sha and not args.force:
            raise InputError(
                "%s existe déjà sur origin (%s) : un paquet n'est pas réécrit "
                "sans --force (recomposition) ; choisir un autre --name ou "
                "inspecter la PR en cours" % (branch, remote_sha[:12]))
        if remote_sha:
            git(repo, "fetch", "--no-tags", "origin", branch, check=False)
            remote_sha = rev(repo, "origin/" + branch) or remote_sha
        ensure_carrier_branch(repo, args.carrier, branch, args.target)
    if args.force:
        clear_local_branch(repo, branch, log=lambda line: print(line, flush=True))
    stale = prune_stale_worktrees(repo, args.keep_failed_hours)
    for path in stale:
        print("worktree d'échec purgé : %s" % path, flush=True)
    worktree = make_worktree(repo, base_sha, branch)
    keep_worktree = bool(args.keep_worktree)
    success = False
    empty = False
    tmp = tempfile.TemporaryDirectory(prefix="paquet-auto-",
                                      dir=os.environ.get("TMPDIR"))
    try:
        ready_out = Path(tmp.name) / "prets.txt"
        candidates, payload = compute_candidates(worktree, args.target,
                                                 base_sha, ready_out)
        errors = error_discards(payload)
        error_lots = {item.lot for item in errors}
        # #3845 : un lot retiré sur un rouge de ci-full (paquet-horaire.py,
        # unité nommée par #3801) n'est pas recomposé ; il reste au manifeste.
        excluded = [lot for lot in getattr(args, "exclude", None) or []
                    if lot not in error_lots]
        if excluded:
            candidates = [candidate for candidate in candidates
                          if candidate.lot not in set(excluded)]
            errors = errors + [Discarded(
                lot, "retiré : ci-full rouge attribuée à son unité (#3801)",
                "--exclude, recomposition sans lui") for lot in excluded]
            error_lots |= set(excluded)
        for item in errors:
            print("écarté #%d : %s — %s"
                  % (item.lot, item.reason, item.detail), flush=True)
        print("liste calculée : %d lot(s) prêt(s), %d en attente, %d en erreur"
              % (len(candidates), len(payload.get("waiting") or []),
                 len(errors)), flush=True)
        if not candidates:
            print("aucun lot prêt : rien à composer", flush=True)
            success = True
            empty = True
            return 0
        compose_script = worktree / COMPOSE_SCRIPT
        if not compose_script.is_file():
            raise PaquetAutoError("%s absent de l'arbre %s"
                                  % (COMPOSE_SCRIPT, worktree))
        composition = compose_candidates(
            worktree, candidates, args.carrier,
            compose_script=compose_script,
            record_package=not args.no_packet_record)
        if not composition.units:
            print("aucune unité composée : %s"
                  % ", ".join("#%d (%s)" % (item.lot, item.reason)
                              for item in composition.discarded), flush=True)
            success = True
            empty = True
            return 0
        runs: list[TestRun] = []
        freeze_seconds = 0.0
        recomp = 0
        while True:
            freeze_rc, freeze_output, freeze_seconds = run_freeze_checks(
                worktree, timeout=FREEZE_TIMEOUT)
            if freeze_rc != 0:
                raise PaquetAutoError(
                    "freeze-checks rouge sur la tête composée :\n%s"
                    % _tail(freeze_output))
            print("freeze-checks : vert (%.1f s)" % freeze_seconds, flush=True)
            runs, faulty, removals = check_local_tests(
                worktree, composition, timeout=args.tests_timeout,
                base_sha=base_sha)
            if not faulty:
                break
            if recomp >= MAX_RECOMPOSITIONS:
                raise PaquetAutoError(
                    "tests locaux encore rouges après recomposition : %s ; "
                    "retrait automatique épuisé, un agent lit les modules"
                    % ", ".join("#%d" % lot for lot in faulty))
            recomp += 1
            removed = set(faulty)
            if not removals:
                removals = [Removal(lot) for lot in faulty]
            reasons = []
            for removal in removals:
                modules = next(
                    (run.modules for run in runs
                     if run.label == "unité #%d" % removal.lot), [])
                if removal.attributed:
                    reason = ("tests ciblés rouges (%s), attribués à son "
                              "unité (#3801), retirés de la recomposition"
                              % (", ".join(removal.tests[:3])
                                 or "tests illisibles"))
                    detail = " ; ".join(removal.evidence[:3])
                else:
                    reason = ("tests locaux rouges (%s), retiré de la "
                              "recomposition"
                              % (", ".join(modules) or "modules illisibles"))
                    detail = ""
                reasons.append(Discarded(removal.lot, reason, detail))
            print("recomposition : retrait de %s"
                  % ", ".join("#%d" % lot for lot in faulty), flush=True)
            candidates = [candidate for candidate in candidates
                          if candidate.lot not in removed]
            composition = recompose(
                worktree, base_sha, candidates, args.carrier,
                compose_script=compose_script,
                record_package=not args.no_packet_record)
            composition.discarded.extend(reasons)
            # `--local` est une composition de contrôle : aucun courrier.
            failed: list[str] = []
            if not getattr(args, "local", False):
                failed = notify_removals(
                    worktree, args.name, removals,
                    sender=getattr(args, "sender", None))
            if failed:
                print("courriers d'auteur non envoyés : %s"
                      % " ; ".join(failed), file=sys.stderr, flush=True)
            if not composition.units:
                print("aucune unité après recomposition : %s"
                      % ", ".join("#%d" % item.lot
                                  for item in composition.discarded), flush=True)
                success = True
                empty = True
                return 0
        composition.discarded = errors + composition.discarded
        table = composition_table(worktree, base_sha)
        ready_count = len([lot for lot in (payload.get("ready") or [])
                           if int(lot) not in error_lots])
        manifest_text = build_manifest(
            name=args.name, target=args.target, base_sha=base_sha,
            carrier=args.carrier, composition=composition, table=table,
            tests=runs, freeze_seconds=freeze_seconds,
            ready_count=ready_count,
            waiting=len(payload.get("waiting") or []),
            errors=len(errors))
        head = write_manifest(worktree, rel, manifest_text, name=args.name,
                              units=len(composition.units),
                              discarded=len(composition.discarded))
        print("manifeste %s : tête %s" % (rel, head), flush=True)
        if not args.no_packet_record:
            print(record_packet(worktree, args.carrier, base_sha, head,
                                branch, args.target), flush=True)
        expected_file = Path(tmp.name) / "attendus.txt"
        _write_id_file(expected_file, [unit.lot for unit in composition.units])
        inclusion_rc, inclusion_output = run_inclusion(
            worktree, worktree / rel, head, ready_out, expected_file)
        print(inclusion_output, flush=True)
        if inclusion_rc != 0:
            raise PaquetAutoError(
                "contrôle d'inclusion rouge sur la tête %s : voir le rapport "
                "ci-dessus" % head[:12])
        print("inclusion : vert", flush=True)
        if args.local:
            print("paquet %s prêt localement : branche %s, tête %s"
                  % (args.name, branch, head), flush=True)
            success = True
            return 0
        push_branch(worktree, branch, force_lease=remote_sha)
        print("poussée : %s%s" % (branch, " (recomposition)"
                                  if remote_sha else ""), flush=True)
        title = "Paquet %s — %d unités sur %s %s" % (
            args.name, len(composition.units), args.target, base_sha[:12])
        body = pr_body(name=args.name, target=args.target, base_sha=base_sha,
                       carrier=args.carrier, composition=composition,
                       candidates=candidates, tests=runs,
                       inclusion_rc=inclusion_rc,
                       freeze_seconds=freeze_seconds)
        pr_number, pr_url = ensure_pull_request(
            worktree, branch, args.target, title, body)
        print("PR : %s" % (pr_url or pr_number or "?"), flush=True)
        inconclusive = sum(1 for run in runs if run.inconclusive)
        tests_note = ("verts" if not inconclusive
                      else "verts + %d non concluant(s) consigné(s)" % inconclusive)
        note = ("paquet %s composé par scripts/paquet-auto.py : %d unités, "
                "%d écartés, tests locaux %s, inclusion verte ; manifeste %s"
                % (args.name, len(composition.units),
                   len(composition.discarded), tests_note, rel))
        print(freeze_carrier(worktree, args.carrier, head, note), flush=True)
        mail = mail_body(name=args.name, target=args.target, base_sha=base_sha,
                         carrier=args.carrier, head=head, branch=branch,
                         composition=composition, tests=runs,
                         inclusion_rc=inclusion_rc, pr_number=pr_number,
                         pr_url=pr_url)
        try:
            send_mail(args.pilot, args.carrier, mail, getattr(args, "sender", None))
        except PaquetAutoError as error:
            print("courrier non envoyé : %s\n\n%s" % (error, mail),
                  file=sys.stderr)
            raise
        print("courrier envoyé à %s" % args.pilot, flush=True)
        success = True
        return 0
    finally:
        if success and not keep_worktree:
            remove_worktree(repo, worktree)
            if empty:
                git(repo, "branch", "-D", branch, check=False)
        elif not success:
            print("worktree conservé pour diagnostic : %s" % worktree,
                  file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose le paquet horaire sans agent (#3807).")
    parser.add_argument("--repo", default=".", help="clone du projet (défaut : .)")
    parser.add_argument("--target", default="main", help="branche cible (défaut : main)")
    parser.add_argument("--name", required=True, help="nom du paquet (h-AAAAMMJJ-NN)")
    parser.add_argument("--carrier", type=int, default=0,
                        help="lot porteur du paquet")
    parser.add_argument("--pilot", default="mesh-design",
                        help="destinataire du courrier (défaut : mesh-design)")
    parser.add_argument("--from", dest="sender",
                        default=os.environ.get("AMEESH_ROLE") or None,
                        help="expéditeur du courrier (défaut : AMEESH_ROLE, sinon "
                             "l'identité liée)")
    parser.add_argument("--local", action="store_true",
                        help="s'arrêter après le manifeste et packet record")
    parser.add_argument("--force", action="store_true",
                        help="recomposer un paquet existant : réécrit la "
                             "branche distante sous --force-with-lease (retirer "
                             "la CI et le feu avant)")
    parser.add_argument("--exclude", type=int, action="append", default=[],
                        metavar="LOT",
                        help="#3845 : ne pas composer ce lot prêt (répétable) ; "
                             "il est écrit aux écartés du manifeste")
    parser.add_argument("--replay", default=None, metavar="REF",
                        help="rejouer le manifeste d'une référence et comparer "
                             "les arbres (avec --candidates-json : refuser une "
                             "unité en erreur ou non prête, #3813)")
    parser.add_argument("--base", default=None,
                        help="avec --replay : la base du rejeu (seul usage ; "
                             "la composition part toujours de la tête fraîche "
                             "d'origin/<cible>)")
    parser.add_argument("--candidates-json", default=None,
                        help="avec --replay : la sortie `paquet-candidats "
                             "--json` pour refuser une unité en erreur ou "
                             "non prête (#3813)")
    parser.add_argument("--python", default=None,
                        help="interpréteur des outils enfants (défaut : un "
                             "Python du projet qui porte psycopg)")
    parser.add_argument("--tests-timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="borne d'une exécution unittest (défaut : %d s)"
                        % DEFAULT_TIMEOUT)
    parser.add_argument("--keep-worktree", action="store_true",
                        help="conserver le worktree jetable (diagnostic)")
    parser.add_argument("--keep-failed-hours", type=float, default=24.0,
                        help="N1 : purger un worktree d'échec au-delà de N "
                             "heures (défaut : 24 ; 0 = jamais)")
    parser.add_argument("--no-packet-record", action="store_true",
                        help="banc : ne pas lire/écrire la base (porteur, packet record)")
    args = parser.parse_args(argv)
    global _PYTHON_OVERRIDE
    if args.python:
        if not Path(args.python).is_file():
            print("paquet-auto : --python : %s introuvable" % args.python,
                  file=sys.stderr)
            return 2
        _PYTHON_OVERRIDE = str(Path(args.python).resolve())

    try:
        if args.replay:
            if args.carrier or args.local:
                raise InputError("--replay : ni --carrier ni --local")
            if not args.base:
                raise InputError("--replay exige --base")
            if args.candidates_json:
                candidates_json = Path(args.candidates_json)
            else:
                candidates_json = None
            return replay(Path(args.repo).resolve(), args.replay, args.name,
                          args.base, keep=args.keep_worktree,
                          candidates_json=candidates_json)
        if args.base:
            raise InputError("--base ne s'applique qu'à --replay : la "
                             "composition part de la tête fraîche de la cible")
        if args.candidates_json:
            raise InputError("--candidates-json ne s'applique qu'à --replay")
        if not args.carrier:
            raise InputError("--carrier : le lot porteur est requis")
        if args.no_packet_record and not args.local:
            raise InputError("--no-packet-record est réservé au banc avec --local")
        return compose_packet(args)
    except InputError as error:
        print("paquet-auto : %s" % error, file=sys.stderr)
        return 2
    except PaquetAutoError as error:
        print("paquet-auto : %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("paquet-auto : interrompu", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
