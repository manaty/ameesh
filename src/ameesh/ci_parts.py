# SPDX-License-Identifier: AGPL-3.0-only
"""L1651 (#1650) — les parts qu'un change set gelé rend dues.

La porte de file lit la CI du commit gelé (`radar.commit_ci`) : chaque run,
check et statut que GitHub rend pour ce SHA exact doit être terminé et
passant. Une part que le change set rend **due mais qui n'a jamais été
dispatchée** ne laisse aucun statut : un ensemble non vide de statuts verts se
lit vert, et la part manquante est invisible. C'est le trou mesuré sur #1470
(tête gelée antérieure à #1368, sans job `lot-plan`) : cinq parts dues, une
seule annoncée, `radar.commit_ci` vert (#1650, constat première main
mty-nxl-impl-01 du 15/10/2026).

Ce module ferme ce trou : les parts dues ne se déduisent plus des statuts
présents, elles sont **recalculées** par le sélecteur du dépôt, lu à une
référence **versionnée** (`ci_parts.rev`, la cible par défaut) résolue en
identifiant de commit, et alimenté du change set de la tête gelée
(merge-base avec cette référence..tête). Une branche ne peut donc pas
supprimer une part en éditant ses propres workflows, et une tête ancienne qui
ne porte pas le job de plan est jugée contre le contrat que sa cible porte
aujourd'hui — le contrat n'est jamais déduit des seuls statuts présents.

Contrat (`radar.projects.<nom>.ci_parts`, configuration de l'hôte) :

    "ci_parts": {
      "selector": "scripts/ci/lot-plan.mjs",   # relatif au dépôt, lu à `rev`
      "rev": "develop",                        # défaut : la cible du projet
      "argv": ["node", "{selector}", "--files", "{files}", "--out", "{out}"],
      "schema": "nexlink-lot-plan/1",          # facultatif, vérifié
      "due": "due"                             # champ JSON de la liste due
    }

`{selector}`, `{root}` (l'arbre extrait), `{files}` (les noms changés,
séparés par NUL, tels que `git diff --name-only -z` les écrit) et `{out}` (le
plan JSON) sont substitués dans `argv` ; la commande tourne dans l'arbre
extrait, **jamais** par un shell. Le plan doit porter un `schema` non vide et
le champ `due` : une liste de noms de parts non vides, sans doublon.

Rien n'est coché ni écrit dans le dépôt du projet : l'arbre du commit épinglé
est extrait (`git archive`) dans un répertoire temporaire, la commande y
tourne, le répertoire est supprimé. Toute incertitude — configuration
illisible, sélecteur absent de l'arbre épinglé, erreur de git, commande qui
échoue ou dépasse son délai, plan absent, illisible, d'un autre schéma ou
mal formé — lève `PartsError` : un contrat inconnu n'est pas un vert.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

from . import plan_git
from .canon import GIT_TIMEOUT, GitError, _git, _git_env

#: plafond d'exécution du sélecteur : un sélecteur bloqué ne retient pas la
#: passe du radar des heures (canon §3, plafonds de la CI)
PARTS_TIMEOUT = 300
#: les espaces de noms substituables dans `argv` : `radar.parse_ci_parts` les
#: refuse à la déclaration, `due` les substitue
PLACEHOLDERS = ("selector", "root", "files", "out")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


class PartsError(RuntimeError):
    """Les parts dues d'une tête gelée ne peuvent pas être prouvées : message
    actionnable, jamais un vert."""


def _fail(what: str, proc) -> str:
    return "%s: %s" % (what, (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:])


def change_set(repo: str, base: str, head: str) -> bytes:
    """Les noms changés de `head` sur sa base versionnée `base`, séparés par
    NUL (`git diff --no-renames --name-only -z`), la seule lecture sans perte
    d'un nom (B1 de la R1 #1368, auditeur)."""
    proc = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, head)
    if proc.returncode != 0:
        raise PartsError(_fail("cannot read the change set %s..%s" % (base[:12], head[:12]),
                               proc))
    return proc.stdout


def merge_base(repo: str, base: str, head: str) -> str:
    """Le merge-base de la tête gelée et de la référence versionnée : base du
    change set, comme le job de plan (`origin/develop`)."""
    proc = _git(repo, "merge-base", base, head, timeout=GIT_TIMEOUT)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise PartsError(_fail("no merge base between %s and the frozen head %s"
                               % (base[:12], head[:12]), proc))
    return proc.stdout.decode("ascii", "replace").strip()


def _member_path(dest: str, name: str) -> str:
    """Le chemin absolu d'une entrée d'archive sous `dest`, ou `PartsError` :
    aucun nom absolu, aucun `..`, rien hors de l'arbre extrait."""
    clean = name.rstrip("/")
    if not clean or clean.startswith("/") or os.path.isabs(clean) \
            or any(part in ("", ".", "..") for part in clean.split("/")):
        raise PartsError("archive entry escapes the extracted tree: %r" % name)
    path = os.path.realpath(os.path.join(dest, *clean.split("/")))
    if path != dest and not path.startswith(dest + os.sep):
        raise PartsError("archive entry escapes the extracted tree: %r" % name)
    return path


def extract(repo: str, commit: str, dest: str) -> None:
    """L'arbre de `commit`, extrait dans `dest` depuis les objets Git
    (`git archive`), sans toucher au dépôt ni à un checkout. Seuls les
    fichiers, répertoires et liens symboliques internes entrent ; une entrée
    d'un autre type, ou un lien qui sort de l'arbre, refuse."""
    if not os.path.isdir(dest):
        os.makedirs(dest)
    argv = ["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
            "-c", "core.fsmonitor=false", "-c", "core.hooksPath=%s" % os.devnull,
            "archive", "--format=tar", commit]
    try:
        proc = subprocess.Popen(argv, cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_git_env())
    except OSError as exc:
        raise PartsError("git archive %s: %s" % (commit[:12], exc)) from exc
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                path = _member_path(dest, member.name)
                if member.isdir():
                    os.makedirs(path, exist_ok=True)
                    continue
                if member.issym():
                    target = os.path.realpath(os.path.join(os.path.dirname(path),
                                                           member.linkname))
                    if not target.startswith(dest + os.sep):
                        raise PartsError("archive entry escapes the extracted tree: %r"
                                         % member.name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if os.path.lexists(path):
                        os.remove(path)
                    os.symlink(member.linkname, path)
                    continue
                if not member.isfile():
                    raise PartsError("unsupported archive entry %r (only files, directories "
                                     "and internal symlinks)" % member.name)
                source = tar.extractfile(member)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(path, member.mode & 0o777)
        # le lecteur d'archive s'arrête à la fin d'archive : vider le reste du
        # flux avant de fermer, sinon git peut mourir sur SIGPIPE et un
        # archivage pourtant complet se lit comme un échec (témoin instable)
        proc.stdout.read()
    except (tarfile.TarError, OSError) as exc:
        raise PartsError("git archive %s: unreadable archive (%s)" % (commit[:12], exc)) from exc
    finally:
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        code = proc.wait()
    if code != 0:
        raise PartsError("git archive %s: %s" % (commit[:12],
                                                 err.decode("utf-8", "replace").strip()[-300:]))


def selector_env(root: str, home: str) -> dict:
    """Keep the selector's environment minimal, without host tokens or DSNs.

    PATH finds the runtime; HOME and TMPDIR stay in the temporary tree.
    LD_LIBRARY_PATH keeps relocated runtimes working: Actions Python has a
    RUNPATH under /opt/hostedtoolcache, so removing the runtime library path
    can load the system libpython alongside a different stdlib and crash
    instead of rejecting invalid JSON (#1938).
    """
    env = {"PATH": os.environ.get("PATH") or os.defpath, "HOME": home,
           "TMPDIR": home, "GITHUB_WORKSPACE": root, "LC_ALL": "C"}
    if os.environ.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    return env


def _plan_file(path: str, plan) -> tuple:
    """Le champ `due` du plan écrit à `path`, vérifié (schéma, liste de noms
    non vides, sans doublon)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PartsError("the selector wrote no readable plan at %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise PartsError("the selector's plan is not a JSON object")
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema.strip():
        raise PartsError("the selector's plan declares no schema: it cannot be a proof")
    if plan.schema and schema != plan.schema:
        raise PartsError("the selector's plan schema is %r, expected %r" % (schema, plan.schema))
    names = data.get(plan.due)
    if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names):
        raise PartsError("the selector's plan field %r is not a list of part names" % plan.due)
    clean = [name.strip() for name in names]
    if len(set(clean)) != len(clean):
        raise PartsError("the selector's plan field %r repeats a part" % plan.due)
    return schema, tuple(clean)


def due(project, sha: str, base_sha: str, repo: str) -> dict:
    """Les parts que le change set de la tête gelée `sha` rend dues, calculées
    par le sélecteur versionné de `project` (`ci_parts`), lu au commit
    `base_sha` de `repo` et alimenté du diff `merge-base(base_sha, sha)..sha`.

    Rend `{schema, due, selector, base, merge_base, frozen}` ; lève
    `PartsError` dès que la preuve manque (sélecteur absent de l'arbre
    épinglé, change set inconnu, commande en échec, plan mal formé)."""
    plan = getattr(project, "ci_parts", None)
    if plan is None:
        raise PartsError("project %s declares no ci_parts contract" % project.name)
    if plan.selector.startswith("/") or any(part in ("", ".", "..")
                                            for part in plan.selector.split("/")):
        # la déclaration est validée à la lecture de la configuration ; le
        # contrat construit à la main ne contourne pas la règle (défense en
        # profondeur : rien n'est lu hors de l'arbre extrait)
        raise PartsError("the selection source %r is not a path inside the repository"
                         % plan.selector)
    try:
        frozen = plan_git._commit(repo, sha)
        base = plan_git._commit(repo, base_sha)
    except GitError as exc:
        raise PartsError("cannot read %s in %s: %s" % ((sha or "?")[:12], repo, exc)) from exc
    if not frozen:
        raise PartsError("the frozen commit %s is not in %s" % ((sha or "?")[:12], repo))
    if not base:
        raise PartsError("the versioned selection source %r is not a commit of %s: the due "
                         "parts cannot be proven" % (base_sha, repo))
    try:
        parent = merge_base(repo, base, frozen)
        changed = change_set(repo, parent, frozen)
    except GitError as exc:
        raise PartsError("cannot read the change set of %s in %s: %s"
                         % (frozen[:12], repo, exc)) from exc
    tmp = tempfile.mkdtemp(prefix="ameesh-ci-parts-")
    try:
        root = os.path.join(tmp, "tree")
        extract(repo, base, root)
        selector = os.path.join(root, *plan.selector.split("/"))
        if not os.path.isfile(selector):
            raise PartsError("the selection source %s is not in the tree of %s (%s): the due "
                             "parts cannot be proven" % (plan.selector, base_sha[:12], base[:12]))
        files = os.path.join(tmp, "changed.nul")
        with open(files, "wb") as out:
            out.write(changed)
        result = os.path.join(tmp, "plan.json")
        values = {"selector": selector, "root": root, "files": files, "out": result}
        try:
            argv = [part.format(**values) for part in plan.argv]
        except (KeyError, IndexError, ValueError) as exc:
            raise PartsError("the ci_parts command uses an unknown placeholder: %s" % exc) from exc
        env = selector_env(root, tmp)
        try:
            proc = subprocess.run(argv, cwd=root, capture_output=True, timeout=PARTS_TIMEOUT,
                                  check=False, env=env)
        except subprocess.TimeoutExpired as exc:
            raise PartsError("the selector %s timed out after %d s"
                             % (plan.selector, PARTS_TIMEOUT)) from exc
        except OSError as exc:
            raise PartsError("the selector %s cannot start (%s: %s)"
                             % (plan.selector, argv[0], exc)) from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()[-300:]
            raise PartsError("the selector %s failed (exit %d): %s"
                             % (plan.selector, proc.returncode, tail))
        schema, names = _plan_file(result, plan)
        return {"schema": schema, "due": names, "selector": plan.selector,
                "base": base, "merge_base": parent, "frozen": frozen}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
