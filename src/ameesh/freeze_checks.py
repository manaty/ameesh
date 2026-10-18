# SPDX-License-Identifier: AGPL-3.0-only
"""Contrôles statiques avant le gel (#2542).

`ameesh work milestone <lot> frozen --sha S` passe, avant d'écrire le jalon,
les contrôles statiques que le projet déclare dans son propre arbre : le script
[`SCRIPT`] **du commit S**, lancé à la racine de l'arbre construit de S
(`ci_parts.extract`, jamais l'arbre de travail : ni fichier non suivi ni
modification locale n'y entre). Un script rouge refuse le gel, cause nommée et
sortie du script rendue à l'auteur.

Pourquoi : tout passe par paquet (canon §11 bis), il n'y a plus de CI de lot.
Le 18/10, deux lots (h-0900 test_1272, h-2000 test_1349) liaient au niveau
module une classe de test concrète d'un autre module ; l'attribution #1632 ne
l'a refusé qu'en CI de paquet — regel du paquet et une heure de CI perdue à
chaque fois. Un contrôle statique bon marché se passe avant le gel.

Le dépôt lu est `--repo` s'il est nommé (il doit porter S), sinon le
répertoire courant s'il porte S, sinon le dépôt du projet du lot
(`lot_class.repository`). Un SHA qu'aucun ne porte, un gel sans SHA ou un arbre
sans script n'ont pas de contrôle à passer : le gel s'écrit et le dit.
`--no-static-checks "raison"` passe outre, la raison inscrite dans la note du
jalon.

Contrat du script, pour tout projet : lancé par l'interpréteur d'ameesh, cwd =
racine de l'arbre de S, environnement de l'appelant ; sortie 0 = vert, toute
autre = rouge ; il ne dure pas plus de [`TIMEOUT`] secondes (au-delà, son
groupe de processus est tué et le gel refusé). Le script choisit ses
contrôles : seulement ce que la suite exige déjà, statique et rapide — le gel
n'est pas une CI.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time

from . import work
from .canon import GitError, _git

#: le script de contrôles du projet, chemin dépôt lu au commit gelé
SCRIPT = "scripts/freeze-checks.py"
#: borne de l'exécution entière du script (secondes)
TIMEOUT = 300
#: délai de moisson après la mort du groupe, au-delà du délai du script
REAP_TIMEOUT = 10
#: lignes de la sortie du script rendues dans un refus
TAIL_LINES = 60
#: causes d'un refus de gel ; aucune n'écrit de jalon
CAUSES = (
    "sha-unknown",       # --repo nommé ne porte pas le SHA gelé
    "tree-unreadable",   # l'arbre du SHA ne s'extrait pas
    "checks-red",        # le script a rendu une sortie non nulle
    "checks-incomplete",  # le script n'a pas fini dans le délai, ou ne démarre pas
)
#: rappel de l'échappatoire, à la fin de chaque refus
BYPASS = ('fix the lot and freeze again; to freeze anyway: '
          '--no-static-checks "<reason>" (written in the milestone note)')


class Refused(work.WorkError):
    """Un gel refusé par les contrôles statiques : `cause` dans [`CAUSES`]."""

    def __init__(self, cause: str, why: str):
        super().__init__("freeze refused (%s): %s\n%s" % (cause, why, BYPASS))
        self.cause = cause
        self.why = why


def commit_in(repo: str | None, sha: str) -> str | None:
    """Le SHA complet si `sha` désigne un commit de `repo`, sinon None."""
    if not repo or not sha or not os.path.isdir(repo):
        return None
    try:
        proc = _git(repo, "rev-parse", "--verify", "--quiet", sha + "^{commit}")
    except GitError:
        return None
    out = proc.stdout.decode("ascii", "replace").strip()
    return out if proc.returncode == 0 and out else None


def declared(repo: str, commit: str) -> bool:
    """L'arbre de `commit` porte-t-il le script, en fichier ordinaire ?"""
    proc = _git(repo, "ls-tree", commit, "--", SCRIPT)
    fields = proc.stdout.decode("utf-8", "replace").split(None, 2)
    return proc.returncode == 0 and fields[:2] in (["100644", "blob"], ["100755", "blob"])


def resolve(sha: str, *, repo: str | None = None,
            project_repo: str | None = None) -> tuple[str | None, str | None]:
    """`(dépôt, commit complet)` où lire les contrôles du SHA gelé, ou
    `(None, None)`. Un `repo` explicite qui ne porte pas le SHA refuse."""
    if repo:
        commit = commit_in(repo, sha)
        if commit is None:
            raise Refused("sha-unknown", "%s is not a commit of %s" % (sha, repo))
        return repo, commit
    for candidate in (os.getcwd(), project_repo):
        commit = commit_in(candidate, sha)
        if commit is not None:
            return candidate, commit
    return None, None


def _kill_group(proc) -> None:
    """Le script et ses enfants (un contrôle par processus) partent ensemble."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _tail(output: str) -> str:
    lines = output.rstrip().splitlines()
    if len(lines) > TAIL_LINES:
        lines = ["[… %d lines above]" % (len(lines) - TAIL_LINES)] + lines[-TAIL_LINES:]
    return "\n".join(lines)


def run(repo: str, commit: str, *, timeout: float = TIMEOUT) -> dict:
    """Passe le script de `commit` sur son arbre construit.

    Rend `{"result": "none"}` sans script, `{"result": "green", "seconds",
    "output"}` s'il sort à 0 ; lève [`Refused`] sinon."""
    if not declared(repo, commit):
        return {"result": "none"}
    from . import ci_parts
    with tempfile.TemporaryDirectory(prefix="ameesh-freeze-checks-") as tree:
        try:
            ci_parts.extract(repo, commit, tree)
        except ci_parts.PartsError as exc:
            raise Refused("tree-unreadable", str(exc)) from exc
        start = time.monotonic()
        try:
            proc = subprocess.Popen([sys.executable, SCRIPT], cwd=tree,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise Refused("checks-incomplete", "%s does not start: %s" % (SCRIPT, exc)) from exc
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                out, _ = proc.communicate(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                # un petit-fils échappé du groupe tient encore le tube
                out = b""
            raise Refused("checks-incomplete", "%s of %s did not finish in %d s:\n%s" % (
                SCRIPT, commit[:12], timeout, _tail(out.decode("utf-8", "replace"))))
        seconds = time.monotonic() - start
    output = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise Refused("checks-red", "%s of %s exited %d:\n%s" % (
            SCRIPT, commit[:12], proc.returncode, _tail(output)))
    return {"result": "green", "seconds": seconds, "output": output}


def before_freeze(cfg, db, lot: int, sha: str, *, repo: str | None = None,
                  skip: str | None = None, note: str = "", say=print,
                  warn=None) -> str:
    """Le garde du gel en CLI : passe les contrôles ou lève [`Refused`].

    Rend la note du jalon, complétée de la raison quand `skip` passe outre.
    `say` reçoit le compte rendu, `warn` (stderr par défaut) ce qui n'a pas
    été contrôlé."""
    if warn is None:
        def warn(text):
            print(text, file=sys.stderr)
    item = work.get(db, lot)
    if item is None:
        raise work.WorkError("lot %s not found" % lot)
    if skip is not None:
        reason = skip.strip()
        if not reason:
            raise work.WorkError('--no-static-checks needs a reason: --no-static-checks "<why>"')
        warn("static checks skipped: %s" % reason)
        mark = "static checks skipped: %s" % reason
        return "%s — %s" % (note.strip(), mark) if note.strip() else mark
    sha = (sha or "").strip()
    if not sha:
        warn("static checks not run: no --sha to check")
        return note
    from . import lot_class
    where, commit = resolve(sha, repo=repo, project_repo=lot_class.repository(cfg, item))
    if commit is None:
        warn("static checks not run: %s is a commit neither of the current directory nor "
             "of the lot's project repository (freeze from the lot's worktree, or --repo)"
             % sha[:12])
        return note
    result = run(where, commit)
    if result["result"] == "none":
        say("static checks: none declared at %s (%s)" % (commit[:12], SCRIPT))
    else:
        say("static checks: green at %s in %.0f s" % (commit[:12], result["seconds"]))
        for line in result["output"].rstrip().splitlines():
            say("  " + line)
    return note
