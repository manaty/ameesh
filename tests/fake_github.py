# SPDX-License-Identifier: AGPL-3.0-only
"""Faux GitHub du banc (L29) : ce que `gh pr list` et `gh api` rendraient.

N'appelle jamais GitHub. Utilisé en processus (`FakeGh`, passé à
`plan_github` à la place de `Gh`) et par le faux binaire `tests/fakebin/gh`
(`pr list`, `api`), sur le même état JSON :

    {"prs": {"owner/repo#12": {"state": "MERGED", "headRefName": "lot/x",
                               "body": "...", "mergeCommit": {"oid": "…"},
                               "mergedBy": {"login": "…"}, "mergedAt": "…"}},
     "repos": {"owner/repo": {"labels": [...], "issues": [...],
                              "sub_issues": {"<numéro>": [ids]},
                              "sub_issues_api": true, "next": 1,
                              "visibility": "private",
                              "comments": {"<numéro>": ["texte", …]}}},
     "calls": [[argv…], …]}

L126 : `gh api repos/<o>/<r>` rend la visibilité du dépôt, `gh api -X POST
repos/<o>/<r>/issues/<n>/comments` ajoute un commentaire.

Seule l'expression `--jq '.[] | @json'` est comprise (un objet par ligne).
"""
from __future__ import annotations

import copy
import json
import re

WRITE_METHODS = ("POST", "PATCH", "DELETE", "PUT")


def _repo(state: dict, name: str) -> dict:
    repos = state.setdefault("repos", {})
    repo = repos.setdefault(name, {})
    repo.setdefault("labels", [])
    repo.setdefault("issues", [])
    repo.setdefault("sub_issues", {})
    repo.setdefault("sub_issues_api", True)
    repo.setdefault("next", 1)
    repo.setdefault("visibility", "private")
    repo.setdefault("comments", {})
    return repo


def _option(argv: list, name: str):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def _lines(rows) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _issue_out(issue: dict) -> dict:
    out = copy.deepcopy(issue)
    out["labels"] = [{"name": n} for n in issue.get("labels", [])]
    return out


def handle(state: dict, argv: list, stdin: str | None = None) -> tuple[int, str, str]:
    """Rend (code, stdout, stderr) ; journalise l'appel dans `state["calls"]`."""
    state.setdefault("calls", []).append(list(argv))
    if argv[:2] == ["pr", "list"]:
        repo = _option(argv, "--repo")
        wanted = (_option(argv, "--state") or "open").upper()
        rows = []
        for key, pr in sorted(state.get("prs", {}).items()):
            name, _, number = key.partition("#")
            if name != repo:
                continue
            if wanted != "ALL" and pr.get("state", "OPEN") != wanted:
                continue
            rows.append(dict(pr, number=int(number)))
        return 0, json.dumps(rows), ""
    if not argv or argv[0] != "api":
        return 2, "", "faux gh : commande non prise en charge : %s" % " ".join(argv)
    method = (_option(argv, "-X") or "GET").upper()
    skip = {"-X", "--jq", "--input", "-f", "-F", "-H"}
    path = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in skip:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        path = arg
        break
    if path is None:
        return 2, "", "faux gh : chemin d'API absent"
    route, _, query = path.partition("?")
    body = json.loads(stdin) if stdin else {}
    whole = re.fullmatch(r"repos/([^/]+/[^/]+)", route)
    if whole and method == "GET":
        if whole.group(1) in state.get("missing_repos", []):
            return 1, "", "gh: Not Found (HTTP 404)"
        repo = _repo(state, whole.group(1))
        return 0, json.dumps({"full_name": whole.group(1), "visibility": repo["visibility"],
                              "private": repo["visibility"] != "public"}), ""
    match = re.fullmatch(r"repos/([^/]+/[^/]+)/(labels|issues)(?:/(\d+))?"
                         r"(?:/(sub_issues|sub_issue|comments))?", route)
    if not match:
        return 1, "", "gh: Not Found (HTTP 404)"
    repo = _repo(state, match.group(1))
    kind, number, sub = match.group(2), match.group(3), match.group(4)
    if sub == "comments":
        if int(number) not in {int(i["number"]) for i in repo["issues"]}:
            return 1, "", "gh: Not Found (HTTP 404)"
        if method == "POST":
            repo["comments"].setdefault(str(number), []).append(body.get("body", ""))
            return 0, json.dumps({"id": 1, "body": body.get("body", "")}), ""
        if method == "GET":
            return 0, _lines({"body": b} for b in repo["comments"].get(str(number), [])), ""
    if kind == "labels":
        if method == "GET":
            return 0, _lines({"name": n} for n in repo["labels"]), ""
        if method == "POST":
            if body["name"] in repo["labels"]:
                return 1, "", "gh: Validation Failed (HTTP 422)"
            repo["labels"].append(body["name"])
            return 0, json.dumps({"name": body["name"]}), ""
    by_number = {int(i["number"]): i for i in repo["issues"]}
    by_id = {int(i["id"]): i for i in repo["issues"]}
    if kind == "issues" and number is None:
        if method == "GET":
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            label = params.get("labels")
            rows = [_issue_out(i) for i in repo["issues"]
                    if not label or label in i.get("labels", [])]
            return 0, _lines(rows), ""
        if method == "POST":
            n = repo["next"]
            repo["next"] = n + 1
            for name in body.get("labels", []):
                if name not in repo["labels"]:
                    repo["labels"].append(name)
            issue = {"id": 9000 + n, "number": n, "title": body["title"],
                     "body": body.get("body", ""), "labels": list(body.get("labels", [])),
                     "state": "open", "state_reason": None}
            repo["issues"].append(issue)
            return 0, json.dumps(_issue_out(issue)), ""
    if kind == "issues" and number is not None and sub is None:
        issue = by_number.get(int(number))
        if issue is None:
            return 1, "", "gh: Not Found (HTTP 404)"
        if method == "GET":
            return 0, json.dumps(_issue_out(issue)), ""
        if method == "PATCH":
            for key in ("title", "body", "state", "state_reason"):
                if key in body:
                    issue[key] = body[key]
            if "labels" in body:
                issue["labels"] = list(body["labels"])
            return 0, json.dumps(_issue_out(issue)), ""
    if sub is not None:
        if not repo["sub_issues_api"]:
            return 1, "", "gh: Not Found (HTTP 404)"
        parent = by_number.get(int(number))
        if parent is None:
            return 1, "", "gh: Not Found (HTTP 404)"
        children = repo["sub_issues"].setdefault(str(number), [])
        if sub == "sub_issues" and method == "GET":
            return 0, _lines(_issue_out(by_id[c]) for c in children if c in by_id), ""
        if sub == "sub_issues" and method == "POST":
            child = int(body["sub_issue_id"])
            for other in repo["sub_issues"].values():
                if child in other:
                    other.remove(child)
            children.append(child)
            return 0, json.dumps(_issue_out(parent)), ""
        if sub == "sub_issue" and method == "DELETE":
            child = int(body["sub_issue_id"])
            if child in children:
                children.remove(child)
            return 0, json.dumps(_issue_out(by_id[child])), ""
    return 1, "", "gh: Not Found (HTTP 404)"


class FakeGh:
    """Le faux `gh` en processus : même contrat que `plan_github.Gh.run`."""

    def __init__(self, state: dict | None = None, fail=None):
        self.state = state if state is not None else {"prs": {}, "repos": {}}
        #: L126 : `fail(argv)` vrai → l'appel échoue (panne de GitHub simulée)
        self.fail = fail

    def run(self, argv: list, stdin: str | None = None) -> tuple[int, str, str]:
        if self.fail is not None and self.fail(list(argv)):
            self.state.setdefault("calls", []).append(list(argv))
            return 1, "", "gh: Server Error (HTTP 502)"
        return handle(self.state, list(argv), stdin)

    # -- observation -------------------------------------------------------
    def calls(self) -> list:
        return self.state.get("calls", [])

    def writes(self) -> list:
        return [c for c in self.calls() if c and c[0] == "api"
                and (_option(c, "-X") or "GET").upper() in WRITE_METHODS]

    def reset_calls(self) -> None:
        self.state["calls"] = []

    def repo(self, name: str) -> dict:
        return _repo(self.state, name)

    def issue(self, repo: str, title_prefix: str) -> dict | None:
        for issue in self.repo(repo)["issues"]:
            if issue["title"].startswith(title_prefix):
                return issue
        return None
