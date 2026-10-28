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
(`lot_class.repository`). Un SHA complet qu'aucun dépôt ne porte ou un arbre
sans script n'ont pas de contrôle à passer. La CLI exige auparavant un identifiant immuable (#2786, #2863) :
pas de référence symbolique ni de tête vide ; une abréviation de 12+ caractères
hexadécimaux doit se résoudre et est transmise en SHA complet.
`--no-static-checks "raison"` passe outre, la raison inscrite dans la note du
jalon.

Contrat du script, pour tout projet : lancé par l'interpréteur d'ameesh, cwd =
racine de l'arbre de S, environnement de l'appelant ; sortie 0 = vert, toute
autre = rouge ; il ne dure pas plus de [`TIMEOUT`] secondes (au-delà, son
groupe de processus est tué et le gel refusé), ni plus que le temps qui reste
de l'échéance du gel entier quand `before_freeze` en reçoit une (#4073). Le
script choisit ses contrôles : seulement ce que la suite exige déjà, statique
et rapide — le gel n'est pas une CI.

#3687 : le script reçoit aussi, dans [`GATE_ENV`], les chemins du lot que la
politique range avec la porte de fusion (JSON, liste de chemins dépôt) : la
règle de `review_policies.risk_classes` qui classe [`GATE_ANCHOR`] nomme les
chemins « porte de fusion » du §8, lus au canon configuré, sans liste en dur.
`[]` dit que la porte n'est pas touchée ; une variable absente dit que le
garde n'a pas pu le lire (cible, diff ou politique illisibles) — un doute,
que le script traite comme une porte touchée. Le script choisit ce qu'il en
fait (ameesh : les classes sans base de ses modules de contrat de la porte).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from . import work
from .canon import GitError, _git, git_range_paths, git_toplevel
from .deadline import Deadline

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
#: #3687 : le chemin qui désigne la porte de fusion dans la politique ; la
#: règle de classe qui le nomme porte les autres chemins de la porte (§8)
GATE_ANCHOR = "src/ameesh/fire.py"
#: #3687 : la variable où le script lit les chemins de la porte touchés
GATE_ENV = "AMEESH_FREEZE_GATE"
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
    # `--full-tree` : les chemins se lisent depuis la racine du dépôt, même
    # quand `repo` est un sous-répertoire (le gel depuis `src/`, #2567).
    proc = _git(repo, "ls-tree", "--full-tree", commit, "--", SCRIPT)
    fields = proc.stdout.decode("utf-8", "replace").split(None, 2)
    return proc.returncode == 0 and fields[:2] in (["100644", "blob"], ["100755", "blob"])


def _worktree_root(repo: str) -> str:
    """La racine de l'arbre de travail qui contient `repo`, ou `repo` : un
    dépôt nu (sans arbre de travail) et un chemin hors dépôt restent tels
    quels ; un sous-répertoire est ramené à sa racine (#2567)."""
    top = git_toplevel(repo)
    if top and os.path.realpath(repo) != top:
        return top
    return repo


def resolve(sha: str, *, repo: str | None = None,
            project_repo: str | None = None) -> tuple[str | None, str | None]:
    """`(dépôt, commit complet)` où lire les contrôles du SHA gelé, ou
    `(None, None)`. Un `repo` explicite qui ne porte pas le SHA refuse ; un
    sous-répertoire est normalisé vers la racine de son arbre de travail."""
    if repo:
        where = _worktree_root(repo)
        commit = commit_in(where, sha)
        if commit is None:
            raise Refused("sha-unknown", "%s is not a commit of %s" % (sha, repo))
        return where, commit
    for candidate in (os.getcwd(), project_repo):
        if not candidate:
            continue
        where = _worktree_root(candidate)
        commit = commit_in(where, sha)
        if commit is not None:
            return where, commit
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


def gate_rules(canons, project: str | None) -> list:
    """Les règles de classe qui nomment [`GATE_ANCHOR`], tous canons ; [] si
    aucune politique lisible ne le nomme."""
    from . import review
    rules = []
    for canon in canons or ():
        policy, problems = review.policy_from_federation(
            getattr(canon, "federation", None), project)
        if any(problem[1] == "error" for problem in problems):
            continue
        rules.extend(rule for rule in policy.rules
                     if any(rx.match(GATE_ANCHOR) for rx in rule.regexes))
    return rules


def gate_paths(cfg, item: dict, repo: str, commit: str, *,
               canons=None) -> list[str] | None:
    """Les chemins du diff gelé (merge-base avec la cible..`commit`) que la
    politique range avec la porte de fusion (#3687), triés ; None quand la
    cible, le diff ou la politique ne se lisent pas : l'appelant n'affirme
    alors pas que la porte est hors du diff."""
    from . import lot_class
    target = lot_class.default_base(cfg, item, repo)
    if not target:
        return None
    try:
        paths = git_range_paths(repo, lot_class._landing_ref(repo, target), commit)
    except GitError:
        return None
    if canons is None:
        canons = work.configured_canons(cfg)
    rules = gate_rules(canons, str(item.get("app") or "") or None)
    if not rules:
        return None
    return sorted(path for path in paths
                  if any(rx.match(path) for rule in rules for rx in rule.regexes))


def run(repo: str, commit: str, *, timeout: float = TIMEOUT,
        gate: list[str] | None = None) -> dict:
    """Passe le script de `commit` sur son arbre construit.

    `gate` : les chemins de la porte de fusion touchés ([`gate_paths`]),
    transmis au script dans [`GATE_ENV`] ; None ne transmet rien (doute).
    Rend `{"result": "none"}` sans script, `{"result": "green", "seconds",
    "output"}` s'il sort à 0 ; lève [`Refused`] sinon."""
    if not declared(repo, commit):
        return {"result": "none"}
    env = dict(os.environ)
    env.pop(GATE_ENV, None)
    if gate is not None:
        env[GATE_ENV] = json.dumps(list(gate))
    from . import ci_parts
    with tempfile.TemporaryDirectory(prefix="ameesh-freeze-checks-") as tree:
        try:
            ci_parts.extract(repo, commit, tree)
        except ci_parts.PartsError as exc:
            raise Refused("tree-unreadable", str(exc)) from exc
        start = time.monotonic()
        try:
            proc = subprocess.Popen([sys.executable, SCRIPT], cwd=tree, env=env,
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
                  skip: str | None = None, note: str = "",
                  deadline: Deadline | None = None, say=print,
                  warn=None) -> str:
    """Le garde du gel en CLI : passe les contrôles ou lève [`Refused`].

    Rend la note du jalon, complétée de la raison quand `skip` passe outre.
    `say` reçoit le compte rendu, `warn` (stderr par défaut) ce qui n'a pas
    été contrôlé.

    Avec `deadline` (#4073), l'échéance du geste de gel entier, les contrôles
    ne prennent que le temps qui reste : une échéance épuisée refuse le gel
    (`checks-incomplete`) au lieu de dépasser le compositeur."""
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
    if deadline is not None and deadline.expired():
        raise Refused("checks-incomplete",
                      "freeze budget exhausted before the static checks")
    from . import lot_class
    where, commit = resolve(sha, repo=repo, project_repo=lot_class.repository(cfg, item))
    if commit is None:
        warn("static checks not run: %s is a commit neither of the current directory nor "
             "of the lot's project repository (freeze from the lot's worktree, or --repo)"
             % sha[:12])
        return note
    gate = gate_paths(cfg, item, where, commit)
    timeout = TIMEOUT if deadline is None else deadline.timeout(TIMEOUT)
    if timeout <= 0.0:
        raise Refused("checks-incomplete",
                      "freeze budget exhausted before the static checks of %s"
                      % commit[:12])
    result = run(where, commit, gate=gate, timeout=timeout)
    if result["result"] == "none":
        say("static checks: none declared at %s (%s)" % (commit[:12], SCRIPT))
    else:
        say("static checks: green at %s in %.0f s" % (commit[:12], result["seconds"]))
        for line in result["output"].rstrip().splitlines():
            say("  " + line)
    return note
