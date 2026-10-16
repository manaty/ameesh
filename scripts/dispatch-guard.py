#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Un lancement à la main de tests.yml peut-il publier le check `tests` ? (#1635)

    scripts/dispatch-guard.py   (GITHUB_REF, GITHUB_SHA, GITHUB_REPOSITORY, GH_TOKEN)

Le check requis de main est `tests`, émis par GitHub Actions (app 15368, non
strict) : il se lie au **commit**, pas à l'évènement. Un `workflow_dispatch`
qui conclut vert sur la tête d'une PR ouverte satisfait donc le check requis de
cette PR, sans merge-ref ni porte de gel (constaté : run 38556384176 sur
lot/1211, seul check `tests` du commit 859e0658 de la PR #923).

Deux refus, chacun suffisant :

- la référence lancée n'est ni `main`, ni `release/**`, ni `integration/**`
  (branches de paquet, §12), ni `validation/**` (validations de déploiement du
  bureau) : une branche de lot obtient sa CI par sa PR ;
- le commit lancé est la tête d'une PR ouverte, quelle que soit la branche
  lancée (une branche admise posée sur la tête d'un lot).

Au moindre doute (API illisible, réponse inattendue), c'est un refus : le job
`plan` échoue et l'agrégat `tests` rougit sans exécuter de tests. Code de
sortie 0 si admis, 1 sinon ; le motif va sur la sortie standard, en
annotation `::error::` quand c'est un refus.

Le workflow lancé est celui de la référence lancée : la garde protège un
lancement d'une branche qui porte le workflow de main ; une branche qui la
retire le fait dans un diff relu, comme pour `pull_request`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Callable, Iterable

#: les références qu'un lancement à la main peut viser
ADMITTED = re.compile(r"refs/heads/(main|(release|integration|validation)/.+)")
#: pages de PR ouvertes lues au plus (100 par page)
MAX_PAGES = 20
API = "https://api.github.com"


class GuardError(Exception):
    """Réponse de l'API illisible : refus, jamais admission par défaut."""


def ref_admitted(ref: str) -> bool:
    return bool(ADMITTED.fullmatch(ref or ""))


def _get(url: str, token: str) -> list:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise GuardError(f"unexpected answer from {url}")
    return data


def open_pr_heads(repo: str, token: str,
                  fetch: Callable[[str, str], list] = _get) -> Iterable[tuple[int, str]]:
    """`(numéro, SHA de tête)` de chaque PR ouverte du dépôt."""
    for page in range(1, MAX_PAGES + 1):
        rows = fetch(f"{API}/repos/{repo}/pulls?state=open&per_page=100&page={page}", token)
        for row in rows:
            try:
                number, head = row["number"], row["head"]["sha"]
                if (type(number) is not int or number <= 0 or not isinstance(head, str)
                        or not re.fullmatch(r"[0-9a-f]{40}", head)):
                    raise GuardError("unreadable pull request number or head SHA")
                yield number, head
            except (KeyError, TypeError, ValueError) as exc:
                raise GuardError(f"unreadable pull request: {exc}") from None
        if len(rows) < 100:
            return
    raise GuardError(f"more than {MAX_PAGES * 100} open pull requests")


def check(ref: str, sha: str, repo: str, token: str,
          fetch: Callable[[str, str], list] = _get) -> tuple[bool, str]:
    if not ref_admitted(ref):
        return False, (f"workflow_dispatch on '{ref}': only main, release/**, "
                       f"integration/** and validation/** may be run by hand (#1635); "
                       f"a lot branch gets its CI through its pull request, nothing run")
    if not re.fullmatch(r"[0-9a-f]{40}", sha or "") or not repo or not token:
        return False, "workflow_dispatch without commit, repository or token: nothing run"
    try:
        heads = [n for n, head in open_pr_heads(repo, token, fetch) if head == sha]
    except Exception as exc:  # doute : refus
        return False, (f"open pull requests unreadable ({type(exc).__name__}: {exc}): "
                       f"nothing run")
    if heads:
        return False, (f"workflow_dispatch on {sha[:12]}, the head of open pull request(s) "
                       f"{', '.join('#%d' % n for n in heads)}: its check would stand for "
                       f"theirs (#1635), nothing run")
    return True, f"workflow_dispatch on {ref} at {sha[:12]}: admitted"


def main() -> int:
    ok, why = check(os.environ.get("GITHUB_REF", ""), os.environ.get("GITHUB_SHA", ""),
                    os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
    print(why if ok else f"::error::{why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
