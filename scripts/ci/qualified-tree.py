#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#5729 — réutiliser la qualification complète d'un paquet sur le push main.

Le push de `main` qui suit la fusion d'une PR `integration/**` relançait la
suite entière sur un arbre déjà testé : le merge commit et la tête de la PR
ont le même arbre, et le run `tests` de la PR est vert (canon §12 : une seule
suite complète par paquet). Ce lecteur en apporte la preuve, sans publier de
check ni toucher un run :

* le commit poussé est exactement le merge commit de la PR fusionnée sur
  `main` (jamais une poussée directe) ;
* l'arbre du commit poussé, lu par `git rev-parse <sha>^{tree}`, est identique
  à l'arbre de la tête de cette PR ;
* le check `tests` de cette tête et le run `tests.yml` qui l'a publié sont
  terminaux et verts, sur la même tête et la même branche.

Toute lecture incomplète, ambiguë, en échec, ou tout autre événement rend
`reuse=false` : la suite complète reste due (fermé par défaut). La décision
est un module pur, testable hors Actions (`tests/test_5729_qualified_tree.py`).

Modèle : `manaty/nexlink` `scripts/ci/qualified-tree.mjs` (#5456).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

#: une empreinte git complète
SHA = re.compile(r"^[0-9a-f]{40}$")
#: `owner/repo`, la forme rendue par GITHUB_REPOSITORY
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: la seule branche dont une poussée peut réutiliser une qualification
MAIN_REF = "refs/heads/main"
#: le workflow dont le run a publié le check `tests`
TESTS_WORKFLOW = ".github/workflows/tests.yml"
#: bornes de lecture : une preuve plus grosse n'est pas lue, donc pas réutilisée
PAGE_SIZE = 100
MAX_PAGES = 5


class ProofError(RuntimeError):
    """Preuve absente, illisible ou ambiguë : jamais un vert par défaut."""


def _require(condition, reason: str) -> None:
    if not condition:
        raise ProofError(reason)


def _no(reason: str) -> dict:
    return {"reuse": False, "reason": reason}


def _time(value) -> datetime.datetime:
    """Une date ISO 8601 de l'API, avec fuseau ; toute autre forme refuse."""
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProofError("timestamp_unreadable") from exc
    if stamp.tzinfo is None:
        raise ProofError("timestamp_without_timezone")
    return stamp


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Un jeton de job ne suit jamais une redirection (lecture seule bornée)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise ProofError("redirect_refused")


class GitHub:
    """Le lecteur borné de l'API GitHub, avec le jeton du job (aucun `gh`)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.api = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get(self, path: str):
        _require(bool(self.jeton), "missing_token")
        request = urllib.request.Request(
            self.api + path,
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "ameesh-qualified-tree"})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ProofError("api_%d" % exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProofError("api_unreachable") from exc
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise ProofError("api_malformed") from exc


def _list_all(get, path: str) -> list:
    """Une liste paginée d'objets (PR associées à un commit)."""
    rows = []
    for page in range(1, MAX_PAGES + 1):
        body = get("%s%sper_page=%d&page=%d"
                   % (path, "&" if "?" in path else "?", PAGE_SIZE, page))
        _require(isinstance(body, list) and len(body) <= PAGE_SIZE,
                 "unreadable_page")
        rows.extend(body)
        if len(body) < PAGE_SIZE:
            return rows
    raise ProofError("proof_too_large")


def _pages(get, path: str, key: str) -> list:
    """Une liste paginée enveloppée (`{total_count, <key>[...]}`)."""
    rows = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        body = get("%s%sper_page=%d&page=%d"
                   % (path, "&" if "?" in path else "?", PAGE_SIZE, page))
        _require(isinstance(body, dict), "api_malformed")
        items = body.get(key)
        _require(isinstance(items, list) and len(items) <= PAGE_SIZE,
                 "unreadable_page")
        _require(type(body.get("total_count")) is int and body["total_count"] >= 0,
                 "missing_total")
        if total is None:
            total = body["total_count"]
        _require(body["total_count"] == total, "changing_total")
        _require(total <= MAX_PAGES * PAGE_SIZE, "proof_too_large")
        rows.extend(items)
        if len(rows) == total:
            return rows
        _require(len(rows) < total, "inconsistent_total")
        if len(items) < PAGE_SIZE:
            raise ProofError("truncated_page")
    raise ProofError("proof_too_large")


def _packet(pr, repo: str, sha: str) -> bool:
    """Une PR `integration/**` fusionnée sur `main`, dont `sha` est le merge.

    L'endpoint « PR associées à un commit » rend `merged` à `null` même pour
    une PR fusionnée (constaté à la forge sur #4349) : la preuve s'appuie sur
    `state`, `merged_at` et `merge_commit_sha`, jamais sur ce booléen.
    """
    if not isinstance(pr, dict):
        return False
    if pr.get("state") != "closed":
        return False
    try:
        _time(pr.get("merged_at"))
    except ProofError:
        return False
    if pr.get("merge_commit_sha") != sha:
        return False
    base, head = pr.get("base"), pr.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        return False
    if base.get("ref") != "main":
        return False
    base_repo, head_repo = base.get("repo"), head.get("repo")
    if not isinstance(base_repo, dict) or base_repo.get("full_name") != repo:
        return False
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != repo:
        return False
    ref = head.get("ref")
    if not isinstance(ref, str) or not ref.startswith("integration/"):
        return False
    if not isinstance(head.get("sha"), str) or not SHA.fullmatch(head["sha"]):
        return False
    return type(pr.get("number")) is int and pr["number"] > 0


def _run_url(server: str, repo: str, run_id: int) -> str:
    return "%s/%s/actions/runs/%d" % (server.rstrip("/"), repo, run_id)


def _read_proof(*, repo: str, sha: str, tree: str, get,
                server: str = "https://github.com") -> dict:
    prefix = "/repos/" + repo
    associated = _list_all(get, prefix + "/commits/" + sha + "/pulls")
    candidates = [pr for pr in associated if _packet(pr, repo, sha)]
    _require(len(candidates) == 1, "no_unique_merged_packet")
    number = candidates[0]["number"]
    # L'endpoint de la PR rend l'identité finale de fusion ; on ne déduit pas
    # la liste des fichiers d'une réponse de recherche.
    pr = get(prefix + "/pulls/%d" % number)
    _require(_packet(pr, repo, sha) and pr.get("number") == number,
             "packet_changed")
    # L'endpoint de la PR complète rend `merged` ; l'endpoint des PR associées
    # ne le rend pas (voir `_packet`). Cette seconde lecture le vérifie.
    _require(pr.get("merged") is True, "packet_not_merged")
    head = pr["head"]["sha"]
    _require(head == candidates[0]["head"]["sha"], "packet_changed")

    commit = get(prefix + "/git/commits/" + head)
    _require(isinstance(commit, dict) and commit.get("sha") == head
             and isinstance(commit.get("tree"), dict)
             and commit["tree"].get("sha") == tree, "different_tree")

    checks = _pages(get, prefix + "/commits/" + head
                    + "/check-runs?check_name=tests&filter=all", "check_runs")
    checks = [check for check in checks
              if isinstance(check, dict) and check.get("name") == "tests"]
    _require(checks, "tests_check_missing")
    finished = []
    for check in checks:
        _require(type(check.get("id")) is int, "unreadable_check")
        _require(check.get("head_sha") == head, "check_other_head")
        started = _time(check.get("started_at"))
        _require(check.get("status") == "completed", "check_not_completed")
        completed = _time(check.get("completed_at"))
        _require(completed >= started, "check_time_unordered")
        finished.append((completed, check))
    latest_at = max(stamp for stamp, _ in finished)
    latest = [check for stamp, check in finished if stamp == latest_at]
    conclusions = {str(check.get("conclusion") or "").lower() for check in latest}
    # Deux essais au même instant avec des conclusions différentes ne se
    # départagent pas : preuve ambiguë, suite complète.
    _require(len(conclusions) == 1, "ambiguous_check")
    check = sorted(latest, key=lambda row: row["id"])[-1]
    _require(str(check.get("conclusion") or "").lower() == "success",
             "tests_check_not_green")
    app = check.get("app")
    _require(isinstance(app, dict) and app.get("slug") == "github-actions",
             "tests_check_not_actions")

    details = check.get("details_url")
    _require(isinstance(details, str) and details, "check_run_url_missing")
    run_prefix = "%s/%s/actions/runs/" % (server.rstrip("/"), repo)
    _require(details.startswith(run_prefix), "status_run_mismatch")
    run_text = details[len(run_prefix):].split("/", 1)[0]
    _require(run_text.isdigit() and int(run_text) > 0, "check_run_url_missing")
    run_id = int(run_text)
    run = get(prefix + "/actions/runs/%d" % run_id)
    _require(isinstance(run, dict) and run.get("id") == run_id,
             "run_unreadable")
    _require(run.get("head_sha") == head, "run_other_head")
    _require(run.get("head_branch") == pr["head"]["ref"], "run_other_branch")
    _require(run.get("event") == "pull_request", "run_not_pull_request")
    _require(run.get("path") == TESTS_WORKFLOW, "run_not_tests")
    _require(run.get("status") == "completed", "run_not_completed")
    _require(str(run.get("conclusion") or "").lower() == "success",
             "run_not_green")
    _require(type(run.get("run_attempt")) is int and run["run_attempt"] >= 1,
             "run_attempt_unknown")
    repository = run.get("repository")
    _require(isinstance(repository, dict)
             and repository.get("full_name") == repo, "run_other_repo")
    return {"reuse": True, "reason": "qualified_packet_tree",
            "packet": number, "head": head, "tree": tree, "run": run_id}


def qualify(*, event: str, ref: str, repo: str, sha: str, tree: str, get,
            server: str = "https://github.com") -> dict:
    """La décision pure : `{reuse, reason, packet?, head?, tree?, run?}`.

    `get` est un lecteur `chemin -> corps JSON` ; une preuve absente ou
    illisible rend `reuse=false`, jamais une exception vers l'appelant.
    """
    if event != "push" or ref != MAIN_REF:
        return _no("ordinary_event")
    if (not REPO.fullmatch(repo or "") or not SHA.fullmatch(sha or "")
            or not SHA.fullmatch(tree or "")):
        return _no("unknown_identity")
    try:
        return _read_proof(repo=repo, sha=sha, tree=tree, get=get, server=server)
    except ProofError as exc:
        return _no(str(exc))
    except Exception:  # noqa: BLE001 - une lecture inattendue est une absence
        return _no("qualification_unavailable")


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True,
                            text=True)
    return result.stdout.strip()


def decide(env=None, *, get=None, git=None) -> dict:
    """Le résultat de la job `qualification`, à partir de l'environnement."""
    env = os.environ if env is None else env
    event = env.get("GITHUB_EVENT_NAME", "")
    ref = env.get("GITHUB_REF", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    sha = env.get("GITHUB_SHA", "")
    if event != "push" or ref != MAIN_REF:
        return _no("ordinary_event")
    git = git or _git
    try:
        head = git("rev-parse", "--verify", "HEAD")
        tree = git("rev-parse", "--verify", "HEAD^{tree}")
    except (OSError, subprocess.CalledProcessError):
        return _no("checkout_unreadable")
    if head != sha:
        return _no("checkout_mismatch")
    if get is None:
        try:
            get = GitHub(env).get
        except ProofError as exc:
            return _no(str(exc))
    result = qualify(event=event, ref=ref, repo=repo, sha=sha, tree=tree,
                     get=get,
                     server=env.get("GITHUB_SERVER_URL") or "https://github.com")
    result["detail"] = _detail(result)
    return result


def _detail(result: dict) -> str:
    if not result.get("reuse"):
        return ""
    return "paquet #%d, tête %s, arbre %s, run tests %d" % (
        result["packet"], result["head"], result["tree"], result["run"])


def _summary(result: dict, repo: str, server: str) -> str:
    if result.get("reuse"):
        return ("Arbre qualifié (#5729) : paquet #%d, tête `%s`, arbre `%s` ; "
                "run `tests` [%d](%s) SUCCESS — la suite complète n'est pas "
                "relancée sur ce push.\n"
                % (result["packet"], result["head"], result["tree"],
                   result["run"], _run_url(server, repo, result["run"])))
    return ("Pas de réutilisation de qualification (#5729) : `%s` — la suite "
            "complète reste due (fermé par défaut).\n" % result.get("reason"))


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(
        description="preuve d'arbre qualifié d'un paquet sur le push main (#5729)")
    parser.add_argument("--github-output", default=None)
    parser.add_argument("--summary", default=None)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    result = decide(env)
    result["detail"] = _detail(result)
    output = args.github_output
    if output is None:
        output = env.get("GITHUB_OUTPUT", "")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            for key in ("reuse", "reason", "packet", "head", "tree", "run",
                        "detail"):
                value = result.get(key, "")
                if isinstance(value, bool):
                    value = "true" if value else "false"
                fh.write("%s=%s\n" % (key, value))
    summary = args.summary
    if summary is None:
        summary = env.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(_summary(result, env.get("GITHUB_REPOSITORY", ""),
                              env.get("GITHUB_SERVER_URL") or "https://github.com"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
