# SPDX-License-Identifier: AGPL-3.0-only
"""Connecteur `git-merge` : fusion d'une PR GitHub par `gh` (spec §7.3).

* opération `merge` (irréversible) ; cible `owner/repo#<numéro>` ;
  args : `method` (`merge` | `squash` | `rebase`, défaut `merge`),
  `match_head_commit` (SHA attendu de la tête de la PR, facultatif mais
  recommandé : la fusion échoue si la PR a bougé depuis l'approbation),
  `delete_branch` (booléen) ;
* `dedupe = none` : `gh pr merge` n'a pas de clé d'idempotence ;
* réconciliation : `gh pr view --json state,mergeCommit` — `MERGED` →
  `confirmed` (réf. = SHA du commit de fusion), `OPEN` ou `CLOSED` →
  `failed` (la fusion n'a pas eu lieu), lecture impossible → `None`.

Issue d'un appel : code 0 → `confirmed` ; code non nul → on relit l'état de
la PR, `MERGED` → `confirmed`, sinon `unknown` (une erreur réseau après
l'envoi ne prouve pas l'absence d'effet : c'est la réconciliation, plus tard,
qui tranche) ; délai dépassé → `unknown` ; binaire introuvable → `failed`
(rien n'a été lancé).

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
    def _merge_ref(view: dict) -> str | None:
        commit = view.get("mergeCommit")
        if isinstance(commit, dict) and isinstance(commit.get("oid"), str):
            return commit["oid"]
        return None

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
        if proc.returncode == 0:
            return Outcome.confirmed(self._merge_ref(view) if view else None,
                                     "PR %s#%d fusionnée" % (repo, number))
        if view and view.get("state") == "MERGED":
            return Outcome.confirmed(self._merge_ref(view),
                                     "PR %s#%d fusionnée (malgré le code %d)"
                                     % (repo, number, proc.returncode))
        error = " ".join((proc.stderr or proc.stdout or "").split())[:500]
        return Outcome.unknown("gh pr merge : code %d : %s" % (proc.returncode, error or "—"))

    def reconcile(self, action: Action) -> Outcome | None:
        try:
            repo, number = parse_target(action.target)
        except ConnectorError:
            return None
        view = self.view(repo, number)
        if view is None:
            return None
        state = view["state"]
        if state == "MERGED":
            return Outcome.confirmed(self._merge_ref(view), "PR %s#%d fusionnée" % (repo, number))
        return Outcome.failed("PR %s#%d %s : la fusion n'a pas eu lieu"
                              % (repo, number, "ouverte" if state == "OPEN" else "fermée"))
