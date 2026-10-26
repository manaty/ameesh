# SPDX-License-Identifier: AGPL-3.0-only
"""#3642 — les témoins de #3639 : total qui baisse, lecture via commit_ci,
limite de débit.

Relecture de #3639 (tête d9bf46e5515e) : aucun défaut produit, mais trois
témoins manquaient. Rien ne change dans `radar` ; ces cas figent le
comportement déjà écrit.

1. Le garde `total is not None and data["total_count"] != total` de
   `radar.commit_check_runs` n'était pas tué : `Gh(141, drift=True)` fait
   **monter** le total (141 → 142), et le contrôle final `len(runs) != total`
   lève aussi. Cas couvert ici : le total **baisse** de 142 à 141 et la page 2
   rend 41 lignes (identifiants 101..141) ; le compte final tombe juste (141),
   mais l'identifiant 100 a disparu. Sans le garde, la lecture rend 141 lignes
   et un check rouge peut passer inaperçu (mutant vérifié en relecture).
2. Les témoins de #3639 n'appelaient que `commit_check_runs`, jamais
   `commit_ci` : un retour à une seule page dans `commit_ci` serait resté
   vert. Le témoin lit un commit à 141 checks externes + 1 run, de bout en
   bout, sur deux pages : green et acceptable.
3. Aucun témoin de limite de débit : un 403 sur la page 1 ou la page 2 lève
   `plan_github.GithubError`, jamais une lecture partielle prise pour la CI.
"""
from __future__ import annotations

import json
import unittest

from ameesh import plan_github, radar

SHA = "d" * 40
SLUG = "manaty/ameesh"
APP = 15368


def _check(n: int) -> dict:
    return {"id": n, "name": "part %d" % n, "head_sha": SHA, "app": {"id": APP},
            "status": "completed", "conclusion": "success",
            "started_at": "2026-10-26T03:00:00Z",
            "completed_at": "2026-10-26T03:05:00Z",
            "check_suite": {"id": 7}}


def _run() -> dict:
    at = "2026-10-26T03:00:00Z"
    return {"workflowName": "tests", "name": "tests", "workflowDatabaseId": 1,
            "status": "completed", "conclusion": "success",
            "createdAt": at, "startedAt": at, "updatedAt": at, "headSha": SHA}


class DecreasingTotalGh:
    """La page 1 annonce 142 checks ; le check 100 disparaît entre les deux
    pages : la page 2 annonce 141 et rend les identifiants 101..141."""

    def __init__(self):
        self.paths: list[str] = []

    def run(self, argv, stdin=None):
        path = argv[-1]
        self.paths.append(path)
        if "&page=2" not in path:
            return 0, json.dumps({"total_count": 142,
                                  "check_runs": [_check(n) for n in range(0, 100)]}), ""
        return 0, json.dumps({"total_count": 141,
                              "check_runs": [_check(n) for n in range(101, 142)]}), ""


class CiGh:
    """`commit_ci` : un run vert, les check-runs en pages, aucun statut.
    `fail_page` rend un 403 de quota sur la page demandée."""

    def __init__(self, *, total: int, fail_page: int | None = None):
        self.total = total
        self.fail_page = fail_page
        self.paths: list[str] = []

    def run(self, argv, stdin=None):
        if argv[0] == "run":
            self.paths.append("run list")
            return 0, json.dumps([_run()]), ""
        path = argv[-1]
        self.paths.append(path)
        if "check-runs" in path:
            page = int(path.rsplit("&page=", 1)[1]) if "&page=" in path else 1
            if page == self.fail_page:
                return 1, "", "HTTP 403: API rate limit exceeded for 127.0.0.1"
            start = (page - 1) * radar.CHECK_RUNS_PAGE
            stop = min(start + radar.CHECK_RUNS_PAGE, self.total)
            return 0, json.dumps({"total_count": self.total,
                                  "check_runs": [_check(n) for n in range(start, max(start, stop))]}), ""
        return 0, json.dumps({"sha": SHA, "total_count": 0, "statuses": []}), ""

    def check_pages(self) -> list[str]:
        return [path for path in self.paths if "check-runs" in path]


class DecreasingTotalTest(unittest.TestCase):
    """Le garde de constance du total est tué par un total qui baisse."""

    def test_a_total_that_decreases_while_paging_raises(self):
        gh = DecreasingTotalGh()
        with self.assertRaises(plan_github.GithubError):
            radar.commit_check_runs(gh, SLUG, SHA)
        self.assertEqual(len(gh.paths), 2)
        self.assertIn("&page=2", gh.paths[1])


class ThroughCommitCiTest(unittest.TestCase):
    """La lecture paginée est prouvée de bout en bout par la porte de release."""

    project = radar.Project("ameesh", "/r", github=SLUG, ci_required=True)

    def test_141_external_checks_and_one_run_are_green_through_commit_ci(self):
        gh = CiGh(total=141)
        out = radar.commit_ci(self.project, SHA, gh=gh)
        self.assertTrue(out["green"], out)
        self.assertTrue(out["acceptable"], out)
        self.assertEqual(out["runs"], 142)
        self.assertEqual(out["negative"], [])
        self.assertEqual(out["pending"], [])
        self.assertEqual(len(gh.check_pages()), 2)
        self.assertIn("&page=2", gh.check_pages()[1])


class RateLimitTest(unittest.TestCase):
    """Un 403 de quota lève ; il ne rend jamais une lecture partielle."""

    project = radar.Project("ameesh", "/r", github=SLUG, ci_required=True)

    def test_a_rate_limit_on_the_first_page_raises(self):
        gh = CiGh(total=141, fail_page=1)
        with self.assertRaisesRegex(plan_github.GithubError, "403"):
            radar.commit_ci(self.project, SHA, gh=gh)
        self.assertEqual(len(gh.check_pages()), 1)

    def test_a_rate_limit_on_the_second_page_raises_without_a_partial_read(self):
        gh = CiGh(total=141, fail_page=2)
        with self.assertRaisesRegex(plan_github.GithubError, "403"):
            radar.commit_ci(self.project, SHA, gh=gh)
        # la page 1 a bien été lue, la page 2 a refusé : rien n'est rendu.
        self.assertEqual(len(gh.check_pages()), 2)
        self.assertIn("&page=2", gh.check_pages()[1])


if __name__ == "__main__":
    unittest.main()
