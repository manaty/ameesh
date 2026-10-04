# SPDX-License-Identifier: AGPL-3.0-only
"""Connecteur `git-merge` : fusion d'une PR GitHub par `gh` (spec §7.3).

* opération `merge` (irréversible) ; cible `owner/repo#<numéro>` ;
  args : `method` (`merge` | `squash` | `rebase`, défaut `merge`),
  `match_head_commit` (SHA attendu de la tête de la PR, facultatif mais
  recommandé : la fusion échoue si la PR a bougé depuis l'approbation),
  `delete_branch` (booléen) ;
* `dedupe = none` : `gh pr merge` n'a pas de clé d'idempotence ;
* réconciliation : `gh pr view --json state,mergeCommit`.

Règle (R5 : jamais d'issue présumée) — `confirmed` seulement sur PREUVE
certaine : la PR est `MERGED` avec un commit de fusion (réf. = son SHA).
`failed` seulement sur échec CERTAIN : refus explicite et documenté de gh
ou de GitHub (`is not mergeable` — contrôle préalable de gh, la mutation
n'est pas envoyée —, `Head branch was modified`, `Could not resolve to a
PullRequest`), ou PR `CLOSED` sans fusion après une erreur (une PR fermée
sort de la file de fusion et ne peut plus être fusionnée). Tout le reste
reste `unknown`, et la réconciliation ultérieure tranche :

* code 0 mais PR `OPEN` : file de fusion (merge queue) ou fusion automatique,
  la fusion n'a pas (encore) eu lieu — elle peut encore avoir lieu ;
* code non nul sans refus explicite (coupure, réponse perdue) et PR `OPEN`
  ou illisible : la demande a peut-être été reçue (mise en file) ;
* délai dépassé ; PR `MERGED` sans commit de fusion lisible.

À la réconciliation : `MERGED` avec commit → `confirmed` ; `CLOSED` sans
fusion → `failed` ; `OPEN` → l'issue reste inconnue (une PR ouverte ne
prouve pas l'absence d'effet : elle peut être en file de fusion) ; lecture
impossible → `None`. Binaire introuvable → `failed` (rien n'a été lancé).

Le binaire est résolu par `AMEESH_GH_BIN`, puis `AMEESH_BIN_DIR/gh`, puis le
PATH : les tests utilisent un faux `gh`, jamais le vrai.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import Action, ConnectorError, Outcome

_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/([A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,9})$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
METHODS = ("merge", "squash", "rebase")
#: refus explicites et documentés de `gh pr merge` : la demande de fusion n'a
#: pas été envoyée (contrôle préalable de gh) ou GitHub l'a rejetée — aucun effet
REFUSALS = (
    re.compile(r"pull request (?:\S+ )?is not mergeable", re.I),  # gh / GitHub : non fusionnable
    re.compile(r"head branch was modified", re.I),                # --match-head-commit dépassé
    re.compile(r"could not resolve to a pullrequest", re.I),      # PR inexistante
)
BIN_ENV = ("AMEESH_GH_BIN", "AGENT_MESH_GH_BIN")
BIN_DIR_ENV = ("AMEESH_BIN_DIR", "AGENT_MESH_BIN_DIR")

OPERATIONS = {"merge": "irreversible", "view": "read"}


def resolve_gh(override: str | None = None, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    candidates = [override] if override else []
    candidates += [env[key] for key in BIN_ENV if env.get(key)]
    candidates += [os.path.join(env[key], "gh") for key in BIN_DIR_ENV if env.get(key)]
    for candidate in candidates:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        if os.path.sep in path:
            raise ConnectorError("git-merge : binaire gh introuvable ou non exécutable : %s" % path)
    found = shutil.which("gh", path=env.get("PATH"))
    if not found:
        raise ConnectorError("git-merge : gh introuvable (AMEESH_GH_BIN, AMEESH_BIN_DIR ou PATH)")
    return found


def parse_target(target: str) -> tuple[str, int]:
    match = _TARGET_RE.fullmatch(target or "")
    if not match:
        raise ConnectorError("git-merge : cible « owner/repo#<numéro> » attendue, pas %r" % (target,))
    return "%s/%s" % (match.group(1), match.group(2)), int(match.group(3))


class GitMergeConnector:
    name = "git-merge"
    dedupe = "none"

    def __init__(self, gh: str | None = None, env: dict | None = None, timeout: float = 120.0):
        self._gh = gh
        self.env = dict(os.environ if env is None else env)
        self.env.setdefault("GH_PROMPT_DISABLED", "1")
        self.env.setdefault("NO_COLOR", "1")
        self.timeout = float(timeout)

    @property
    def gh(self) -> str:
        return resolve_gh(self._gh, self.env)

    def classify(self, operation: str, args: dict) -> str:
        return OPERATIONS.get(operation, "irreversible")

    # -- gh ----------------------------------------------------------------
    def _run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.gh, *argv], capture_output=True, text=True, timeout=timeout,
            env=self.env, stdin=subprocess.DEVNULL)

    def view(self, repo: str, number: int) -> dict | None:
        """État de la PR (`state`, `mergeCommit`), ou None si illisible."""
        try:
            proc = self._run(["pr", "view", str(number), "--repo", repo,
                              "--json", "state,mergeCommit"], timeout=min(self.timeout, 60.0))
        except (subprocess.TimeoutExpired, OSError, ConnectorError):
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("state") not in ("OPEN", "CLOSED", "MERGED"):
            return None
        return data

    @staticmethod
    def _merge_ref(view: dict | None) -> str | None:
        """SHA du commit de fusion si la PR est `MERGED` : la seule preuve certaine."""
        if not view or view.get("state") != "MERGED":
            return None
        commit = view.get("mergeCommit")
        if isinstance(commit, dict) and isinstance(commit.get("oid"), str) \
                and _SHA_RE.fullmatch(commit["oid"]):
            return commit["oid"]
        return None

    @staticmethod
    def _seen(view: dict | None) -> str:
        if view is None:
            return "illisible"
        return {"OPEN": "ouverte", "CLOSED": "fermée sans fusion",
                "MERGED": "MERGED sans commit de fusion lisible"}[view["state"]]

    # -- protocole ---------------------------------------------------------
    def execute(self, action: Action, idempotency_key: str) -> Outcome:
        if action.operation != "merge":
            return Outcome.failed("git-merge : opération %r non prise en charge" % action.operation)
        args = action.args if isinstance(action.args, dict) else {}
        try:
            repo, number = parse_target(action.target)
        except ConnectorError as exc:
            return Outcome.failed(str(exc))
        method = args.get("method", "merge")
        if method not in METHODS:
            return Outcome.failed("git-merge : méthode %r (%s)" % (method, ", ".join(METHODS)))
        argv = ["pr", "merge", str(number), "--repo", repo, "--" + method]
        head = args.get("match_head_commit")
        if head is not None:
            if not isinstance(head, str) or not _SHA_RE.fullmatch(head):
                return Outcome.failed("git-merge : match_head_commit doit être un SHA de 40 hex")
            argv += ["--match-head-commit", head]
        if args.get("delete_branch") is True:
            argv.append("--delete-branch")
        try:
            gh = self.gh
        except ConnectorError as exc:
            return Outcome.failed(str(exc))
        try:
            proc = subprocess.run(
                [gh, *argv], capture_output=True, text=True, timeout=self.timeout,
                env=self.env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return Outcome.unknown("gh pr merge : délai dépassé (%ss)" % self.timeout)
        except FileNotFoundError as exc:
            return Outcome.failed("gh n'a pas pu démarrer : %s" % exc)
        except OSError as exc:
            return Outcome.unknown("gh pr merge : %s" % exc)
        view = self.view(repo, number)
        pr = "PR %s#%d" % (repo, number)
        merged = self._merge_ref(view)
        if merged:
            return Outcome.confirmed(merged, "%s fusionnée (commit %s)%s" % (
                pr, merged[:12],
                " malgré le code %d" % proc.returncode if proc.returncode else ""))
        output = " ".join((proc.stderr or proc.stdout or "").split())[:500] or "—"
        if proc.returncode == 0:
            # file de fusion, fusion automatique : gh a répondu, la fusion n'a pas
            # (encore) eu lieu et peut encore avoir lieu
            return Outcome.unknown("gh pr merge : code 0 mais %s %s, fusion non constatée "
                                   "(file de fusion ?) : issue inconnue, la réconciliation "
                                   "tranchera — %s" % (pr, self._seen(view), output))
        if any(pattern.search(proc.stderr or "") for pattern in REFUSALS):
            return Outcome.failed("gh pr merge : refus explicite (code %d) : %s"
                                  % (proc.returncode, output))
        if view is not None and view["state"] == "CLOSED":
            return Outcome.failed("%s fermée sans fusion (gh pr merge : code %d : %s)"
                                  % (pr, proc.returncode, output))
        return Outcome.unknown("gh pr merge : code %d, %s %s : issue inconnue, la "
                               "réconciliation tranchera — %s"
                               % (proc.returncode, pr, self._seen(view), output))

    def reconcile(self, action: Action) -> Outcome | None:
        try:
            repo, number = parse_target(action.target)
        except ConnectorError:
            return None
        view = self.view(repo, number)
        if view is None:
            return None
        pr = "PR %s#%d" % (repo, number)
        merged = self._merge_ref(view)
        if merged:
            return Outcome.confirmed(merged, "%s fusionnée (commit %s)" % (pr, merged[:12]))
        if view["state"] == "CLOSED":
            return Outcome.failed("%s fermée sans fusion : la fusion n'a pas eu lieu" % pr)
        # OPEN : peut-être en file de fusion, la fusion peut encore avoir lieu
        return Outcome.unknown("%s %s : fusion non constatée (file de fusion ?), issue "
                               "toujours inconnue" % (pr, self._seen(view)))
