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
   et les modules de test apportés par chaque unité. Une unité dont un module
   rougit est retirée et le paquet est recomposé **une seule fois** ;
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
* `--replay <réf>` : rejoue la table `## Composition` d'un manifeste déjà
  gelé depuis `--base`, reprend le manifeste de `--réf` tel quel (métadonnée,
  aucun effet sur le contenu composé) et compare l'arbre obtenu à
  `git rev-parse <réf>^{tree}`. C'est le témoin de fidélité du script : le
  rejeu de h-31 (`--base f444955b`, `--name h-20261026-31`) doit rendre
  l'arbre de `b57c0380`. Aucune poussée, aucune écriture en base.

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
from dataclasses import dataclass
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
#: une ligne de résumé unittest
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
OK_RE = re.compile(r"^OK(?: \(skipped=(\d+)\))?", re.MULTILINE)


class PaquetAutoError(RuntimeError):
    """Un geste du script a échoué ; le message porte l'état atteint."""


class InputError(PaquetAutoError):
    """Un argument ou un état du dépôt ne permet pas de composer."""


class GitError(PaquetAutoError):
    """Une commande git a échoué."""


class TestsInconclusive(PaquetAutoError):
    """Un test local n'est ni vert ni rouge (environnement, base, timeout)."""


@dataclass
class Candidate:
    """Un lot prêt de la liste calculée, avec la source exacte à composer."""

    lot: int
    source: str
    branch: str = ""
    title: str = ""


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


@dataclass
class TestRun:
    """Une exécution locale de unittest."""

    label: str
    modules: list[str]
    returncode: int | None
    output: str
    seconds: float
    timed_out: bool = False

    @property
    def green(self) -> bool:
        return self.returncode == 0

    @property
    def inconclusive(self) -> bool:
        if self.timed_out or self.returncode not in (0, 1):
            return True
        if self.returncode == 1 and INCONCLUSIVE_RE.search(self.output):
            return True
        return False

    @property
    def tests(self) -> int:
        found = RAN_RE.search(self.output)
        return int(found.group(1)) if found else 0

    @property
    def skipped(self) -> int:
        found = OK_RE.search(self.output)
        return int(found.group(1) or 0) if found else 0


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


def clear_local_branch(repo: Path, branch: str) -> None:
    """Retire une branche locale de recomposition, si elle n'est pas ouverte."""
    if rev(repo, "refs/heads/" + branch) is None:
        return
    listing = out(repo, "worktree", "list", "--porcelain")
    if any(line.strip() == "branch refs/heads/" + branch
           for line in listing.splitlines()):
        raise InputError(
            "branche locale %s encore ouverte dans un worktree : le fermer "
            "avant de recomposer" % branch)
    git(repo, "branch", "-D", branch)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


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
        candidates.append(Candidate(
            lot=lot, source=source, branch=str(entry.get("branch") or ""),
            title=str(entry.get("title") or "")))
    return candidates, payload

def ensure_source(worktree: Path, candidate: Candidate) -> None:
    """Résout la source dans le clone ; la fetch de la branche au besoin."""
    sha = rev(worktree, candidate.source)
    if sha is None and candidate.branch:
        git(worktree, "fetch", "--no-tags", "origin", candidate.branch, check=False)
        sha = rev(worktree, candidate.source)
    if sha is None:
        raise PaquetAutoError(
            "source %s de #%d introuvable dans le clone : la branche n'a pas "
            "été fetchée (ou la source a été réécrite)"
            % (candidate.source[:12], candidate.lot))
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
        return TestRun(label=label, modules=list(modules),
                       returncode=proc.returncode, output=_output(proc),
                       seconds=time.monotonic() - start)
    except PaquetAutoError as exc:
        return TestRun(label=label, modules=list(modules), returncode=None,
                       output=str(exc), seconds=time.monotonic() - start,
                       timed_out=True)


def run_freeze_checks(worktree: Path, *, timeout: int) -> tuple[int, str, float]:
    script = worktree / FREEZE_CHECKS_SCRIPT
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % FREEZE_CHECKS_SCRIPT)
    start = time.monotonic()
    proc = run_command([project_python(worktree), str(script)], cwd=worktree,
                       timeout=timeout, env=python_env(worktree))
    return proc.returncode, _output(proc), time.monotonic() - start


def check_local_tests(worktree: Path, composition: Composition, *,
                      timeout: int, log=print) -> tuple[list[TestRun], list[int]]:
    """Exécute les modules des unités puis les contrats.

    Rend `(exécutions, unités fautives)` ; une unité fautive est celle dont un
    module a rougi. Un résultat non concluant lève, sans retirer d'unité.
    """
    runs: list[TestRun] = []
    faulty: list[int] = []
    for unit in composition.units:
        modules = unit_test_modules(worktree, unit)
        if not modules:
            log("tests unité #%d : aucun module propre" % unit.lot)
            continue
        run = run_tests(worktree, "unité #%d" % unit.lot, modules, timeout=timeout)
        runs.append(run)
        if run.inconclusive:
            raise TestsInconclusive(
                "%s : résultat non concluant (rc %s)\n%s"
                % (run.label, run.returncode, _tail(run.output)))
        if not run.green:
            faulty.append(unit.lot)
            log("tests unité #%d : ROUGE (%d modules)\n%s"
                % (unit.lot, len(modules), _tail(run.output)))
        else:
            log("tests unité #%d : vert (%d modules, %d tests)"
                % (unit.lot, len(modules), run.tests))
    if faulty:
        return runs, faulty
    modules = contract_modules(worktree)
    run = run_tests(worktree, "contrats", modules, timeout=timeout)
    runs.append(run)
    if run.inconclusive:
        raise TestsInconclusive(
            "%s : résultat non concluant (rc %s)\n%s"
            % (run.label, run.returncode, _tail(run.output)))
    if not run.green:
        raise PaquetAutoError(
            "tests de contrat rouges sur la tête composée (aucune unité retirée "
            "automatiquement : l'attribution demande un agent) :\n%s"
            % _tail(run.output))
    log("tests contrats : vert (%d modules, %d tests)" % (len(modules), run.tests))
    return runs, []


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
        "- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le "
        "manifeste (résultat au courrier du pilote).",
        "",
    ]
    return "\n".join(lines)


def _tests_summary(tests: list[TestRun]) -> str:
    modules = sorted({module for run in tests for module in run.modules})
    total = sum(run.tests for run in tests)
    skipped = sum(run.skipped for run in tests)
    failures = sum(1 for run in tests if not run.green)
    return ("%d séries, %d modules, %d tests, %d échec(s), %d sauté(s) : %s"
            % (len(tests), len(modules), total, failures, skipped,
               ", ".join("`%s`" % module for module in modules) or "aucun"))


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


def send_mail(pilot: str, carrier: int, body: str) -> str:
    cmd = ["agent-mail", "send", pilot, "-", "--lot", str(carrier)]
    proc = run_command(cmd, input_text=body, timeout=120)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "courrier au pilote %s : %s" % (pilot, _tail(_output(proc))))
    return _output(proc)


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
    return """\
Paquet {name} composé sans agent : tête `{head}` sur {target} `{base}` (branche `{branch}`, {pr}).

- Unités ({n}) : {units}.
- Écartés ({d}) : {discarded}.
- Tests locaux : {tests}.
- Inclusion : {inclusion}.
- Gel : `ameesh work milestone` posé sur la tête ; manifeste `{manifest}`.

« Verdict non requis » et la ci-full restent tes gestes ; aucune fusion sur {target}.
""".format(
        name=name, head=head, target=target, base=base_sha[:12],
        branch=branch, pr=pr, n=len(composition.units), units=units,
        d=len(composition.discarded), discarded=discarded,
        tests=_tests_summary(tests),
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


def replay(repo: Path, reference: str, name: str, base: str, *,
           compose_script: Path | None = None, keep: bool = False,
           log=print) -> int:
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
        clear_local_branch(repo, branch)
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
            runs, faulty = check_local_tests(
                worktree, composition, timeout=args.tests_timeout)
            if not faulty:
                break
            if recomp >= MAX_RECOMPOSITIONS:
                raise PaquetAutoError(
                    "tests locaux encore rouges après recomposition : %s ; "
                    "retrait automatique épuisé, un agent lit les modules"
                    % ", ".join("#%d" % lot for lot in faulty))
            recomp += 1
            removed = set(faulty)
            reasons = []
            for lot in faulty:
                modules = next(
                    (run.modules for run in runs
                     if run.label == "unité #%d" % lot), [])
                reasons.append(Discarded(
                    lot,
                    "tests locaux rouges (%s), retiré de la recomposition"
                    % (", ".join(modules) or "modules illisibles")))
            print("recomposition : retrait de %s"
                  % ", ".join("#%d" % lot for lot in faulty), flush=True)
            candidates = [candidate for candidate in candidates
                          if candidate.lot not in removed]
            composition = recompose(
                worktree, base_sha, candidates, args.carrier,
                compose_script=compose_script,
                record_package=not args.no_packet_record)
            composition.discarded.extend(reasons)
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
        note = ("paquet %s composé par scripts/paquet-auto.py : %d unités, "
                "%d écartés, tests locaux verts, inclusion verte ; manifeste %s"
                % (args.name, len(composition.units),
                   len(composition.discarded), rel))
        print(freeze_carrier(worktree, args.carrier, head, note), flush=True)
        mail = mail_body(name=args.name, target=args.target, base_sha=base_sha,
                         carrier=args.carrier, head=head, branch=branch,
                         composition=composition, tests=runs,
                         inclusion_rc=inclusion_rc, pr_number=pr_number,
                         pr_url=pr_url)
        try:
            send_mail(args.pilot, args.carrier, mail)
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
    parser.add_argument("--local", action="store_true",
                        help="s'arrêter après le manifeste et packet record")
    parser.add_argument("--force", action="store_true",
                        help="recomposer un paquet existant : réécrit la "
                             "branche distante sous --force-with-lease (retirer "
                             "la CI et le feu avant)")
    parser.add_argument("--replay", default=None, metavar="REF",
                        help="rejouer le manifeste d'une référence et comparer les arbres")
    parser.add_argument("--base", default=None,
                        help="avec --replay : la base du rejeu (seul usage ; "
                             "la composition part toujours de la tête fraîche "
                             "d'origin/<cible>)")
    parser.add_argument("--python", default=None,
                        help="interpréteur des outils enfants (défaut : un "
                             "Python du projet qui porte psycopg)")
    parser.add_argument("--tests-timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="borne d'une exécution unittest (défaut : %d s)"
                        % DEFAULT_TIMEOUT)
    parser.add_argument("--keep-worktree", action="store_true",
                        help="conserver le worktree jetable (diagnostic)")
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
            return replay(Path(args.repo).resolve(), args.replay, args.name,
                          args.base, keep=args.keep_worktree)
        if args.base:
            raise InputError("--base ne s'applique qu'à --replay : la "
                             "composition part de la tête fraîche de la cible")
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
