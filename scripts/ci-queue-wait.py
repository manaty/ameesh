#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Attente en file des jobs de CI, par classe de run (lot #1468)

    scripts/ci-queue-wait.py [--repo manaty/ameesh] [--workflow tests.yml]
                             [--runs 60] [--since ISO] [--until ISO] [--json]
    scripts/ci-queue-wait.py --from-file jobs.json [--json]

Mesure, pour chaque job auto-hébergé des derniers runs du workflow,
l'attente entre sa mise en file (`created_at`) et sa prise par un runner
(`started_at`), puis la résume (n, médiane, p80, max, en secondes) par
classe de run :

  - `prio`     : poussée ou appel sur `integration/**` ou `release/**`, PR
                 dont la source ou la cible est l'une de ces branches, ou PR
                 marquée prioritaire (`--prio-prs`, la liste de
                 `vars.AMEESH_CI_PRIO_PRS`) — les runs que le routage de
                 `tests.yml` envoie au pool prioritaire quand l'interrupteur
                 `vars.AMEESH_CI_PRIO` vaut `on` ;
  - `ordinary` : tout le reste (PR de lot, main, dispatch).

et par groupe de runners (`reserved` : les runners réservés, `--reserved`,
par défaut `ameesh-ci-3-1` à `ameesh-ci-3-4` ; sinon `general` — les labels
d'un job sont ceux qu'il demande, pas ceux du runner qui l'a pris). La même commande, lancée avant puis après
l'activation sur des fenêtres comparables (`--since`/`--until`), donne la
mesure avant/après demandée par le lot. Lecture seule : `gh api` en GET, rien
n'est écrit côté GitHub. Les jobs macOS et ceux d'une fourche (hébergés) sont
exclus : ils ne passent pas par nos runners.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys

PRIO_BRANCHES = ("integration/", "release/")
SELF_HOSTED = "self-hosted"
RESERVED_RUNNERS = frozenset("ameesh-ci-3-%d" % i for i in range(1, 5))


def _gh(path: str) -> dict:
    out = subprocess.run(["gh", "api", "-X", "GET", path], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out)


def _ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_class(run: dict, prio_prs: frozenset[int] = frozenset()) -> str:
    """`prio` ou `ordinary`, avec la règle de routage de tests.yml."""
    event = run.get("event") or ""
    branch = run.get("head_branch") or ""
    if event in ("push", "workflow_dispatch", "workflow_call") \
            and branch.startswith(PRIO_BRANCHES):
        return "prio"
    if event == "pull_request":
        if branch.startswith(PRIO_BRANCHES):
            return "prio"
        for pr in run.get("pull_requests") or ():
            base = ((pr.get("base") or {}).get("ref")) or ""
            if base.startswith(PRIO_BRANCHES) or pr.get("number") in prio_prs:
                return "prio"
    return "ordinary"


def waits(runs: list[dict], jobs_by_run: dict[int, list[dict]],
          prio_prs: frozenset[int] = frozenset(),
          reserved: frozenset[str] = RESERVED_RUNNERS) -> list[dict]:
    """Une ligne par job auto-hébergé démarré : classe, groupe, attente."""
    rows = []
    for run in runs:
        cls = run_class(run, prio_prs)
        for job in jobs_by_run.get(run["id"], ()):
            labels = job.get("labels") or []
            if SELF_HOSTED not in labels:
                continue
            created, started = _ts(job.get("created_at")), _ts(job.get("started_at"))
            if created is None or started is None or not job.get("runner_name"):
                continue  # jamais pris par un runner (sauté, annulé en file)
            rows.append({
                "run": run["id"], "job": job.get("name"), "class": cls,
                "group": "reserved" if job.get("runner_name") in reserved else "general",
                "runner": job.get("runner_name"),
                "wait": max(0.0, (started - created).total_seconds()),
                "created_at": job.get("created_at"),
            })
    return rows


def _quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    i = q * (len(s) - 1)
    lo = int(i)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def summary(rows: list[dict]) -> dict:
    out: dict = {}
    for key in ("class", "group"):
        for name in sorted({r[key] for r in rows}):
            w = [r["wait"] for r in rows if r[key] == name]
            out["%s=%s" % (key, name)] = {
                "n": len(w), "median": round(_quantile(w, 0.5), 1),
                "p80": round(_quantile(w, 0.8), 1), "max": round(max(w), 1)}
    return out


def fetch(repo: str, workflow: str, count: int, since: str | None,
          until: str | None) -> tuple[list[dict], dict[int, list[dict]]]:
    runs: list[dict] = []
    page = 1
    window = ""
    if since or until:
        window = "&created=%s..%s" % (since or "*", until or "*")
    while len(runs) < count:
        data = _gh("repos/%s/actions/workflows/%s/runs?per_page=100&page=%d%s"
                   % (repo, workflow, page, window))
        batch = [r for r in data.get("workflow_runs", ()) if r.get("status") == "completed"]
        if not data.get("workflow_runs"):
            break
        runs.extend(batch)
        page += 1
    runs = runs[:count]
    jobs = {}
    for run in runs:
        jobs[run["id"]] = _gh("repos/%s/actions/runs/%d/jobs?per_page=100&filter=latest"
                              % (repo, run["id"])).get("jobs", [])
    return runs, jobs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="manaty/ameesh")
    ap.add_argument("--workflow", default="tests.yml")
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--prio-prs", default="[]",
                    help="JSON list of PR numbers marked priority (vars.AMEESH_CI_PRIO_PRS)")
    ap.add_argument("--reserved", default=",".join(sorted(RESERVED_RUNNERS)),
                    help="comma-separated names of the reserved runners")
    ap.add_argument("--from-file", help="saved {runs, jobs} instead of the API")
    ap.add_argument("--save", help="write the fetched {runs, jobs} here")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    prio_prs = frozenset(int(n) for n in json.loads(args.prio_prs))
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        runs, jobs = saved["runs"], {int(k): v for k, v in saved["jobs"].items()}
    else:
        runs, jobs = fetch(args.repo, args.workflow, args.runs, args.since, args.until)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"runs": runs, "jobs": jobs}, fh)
    rows = waits(runs, jobs, prio_prs, frozenset(filter(None, args.reserved.split(","))))
    result = {"runs": len(runs), "jobs": len(rows), "summary": summary(rows)}
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print("%d runs, %d self-hosted jobs started" % (result["runs"], result["jobs"]))
        for key, s in result["summary"].items():
            print("  %-16s n=%-4d median=%7.1fs p80=%7.1fs max=%7.1fs"
                  % (key, s["n"], s["median"], s["p80"], s["max"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
