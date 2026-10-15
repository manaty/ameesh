#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#987 : relancer une exécution « tests » dont un job hébergé a été annulé
faute de runner.

GitHub annule un job hébergé quand aucun runner ne l'a pris après plusieurs
tentatives — annotation « The job was not acquired by Runner of type hosted »,
sous contrainte de capacité (macOS arm64, 13/10/2026, run 38428931108). Le job
`platform-macos` conclut alors `cancelled` : `runner_name` vide, aucune étape
exécutée. L'agrégat requis `tests` (.github/workflows/tests.yml) exige
`needs.platform-macos.result == success`, donc le check requis rougit sans
aucun défaut de code ; et l'API refuse de rejouer le job tant que l'exécution
tourne (« workflow run containing this job is already running »).

`.github/workflows/relance-hosted.yml` se déclenche à la conclusion de
« tests » et appelle ce script après la fin de l'exécution. Si les seuls jobs
en échec sont des jobs hébergés annulés avant leur premier pas, il relance les
jobs en échec (`rerun-failed-jobs`), une seule fois : la tentative suivante
prouve si la capacité est revenue. Un échec réel ailleurs, ou une deuxième
annulation, n'est jamais masqué — la garde ne relance pas, le check reste
rouge et un humain tranche.

La décision est une fonction pure (`decision`), testable sans réseau ; les
appels passent par `urllib` avec le jeton du job (comme `scripts/ci-flow.py`),
sans dépendre de `gh`.

Usage :
    scripts/ci-relance-hosted.py --run-id 38428931108 --tentative 1 --apply
    scripts/ci-relance-hosted.py --jobs jobs.json --tentative 1   # décision
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

#: conclusions qui font échouer une exécution
CONCLUSIONS_ECHEC = ("failure", "cancelled", "timed_out")
#: tentative maximale : la relance n'a lieu qu'avant celle-ci (une seule
#: relance automatique — une deuxième annulation est un incident de capacité
#: qui doit se voir, pas une boucle)
MAX_TENTATIVES = 2
#: jobs qui ne comptent pas comme échec réel : l'agrégat requis `tests`, rouge
#: *parce qu'il* exige le succès d'un job annulé (il repart avec la relance,
#: comme dépendant), et `bilan`, déclaré `continue-on-error`
NON_REQUIS = ("tests", "bilan", "bilan des impactés")


def job_reel(job: dict) -> bool:
    """Vrai pour un job de workflow, faux pour un check run de part.

    Les check runs « tests (flux) » publiés par les parts (L161c) apparaissent
    dans la même liste `.../jobs` que les jobs, avec des étiquettes vides et
    aucune étape (relevé sur le run 38377916850) ; un vrai job porte toujours
    ses étiquettes (`macos-latest`, `self-hosted`…). Sans ce filtre, un check
    run `cancelled` serait pris pour un job hébergé annulé (ou pour un échec
    réel) et fausserait la décision.
    """
    return bool(job.get("labels"))


def jamais_pris(job: dict) -> bool:
    """Vrai pour un job hébergé annulé avant qu'un runner ne le prenne.

    Signature relevée de première source (job 115427733668, run 38428931108) :
    `conclusion == "cancelled"`, `runner_name` vide, aucune étape (`steps`
    vide). Les jobs auto-hébergés (`self-hosted` dans `labels`) sont exclus :
    leur file est la nôtre (#824), pas la capacité hébergée de GitHub.
    """
    return (job_reel(job)
            and job.get("conclusion") == "cancelled"
            and not job.get("runner_name")
            and not job.get("steps")
            and "self-hosted" not in job["labels"])


def decision(jobs: list[dict], tentative: int,
             max_tentatives: int = MAX_TENTATIVES) -> tuple[bool, str]:
    """(relancer, raison) — pure, sans réseau.

    On ne relance que si TOUS les jobs en échec sont des jobs hébergés
    annulés avant leur premier pas (hors agrégat non requis) : un échec réel
    ne doit jamais être noyé dans une relance, et une relance ne doit pas
    consommer la file sans espoir.
    """
    if tentative >= max_tentatives:
        return False, ("tentative %d ≥ %d : une annulation qui se répète est un "
                       "incident de capacité, pas un aléa" % (tentative, max_tentatives))
    echecs = [j for j in jobs
              if job_reel(j) and j.get("conclusion") in CONCLUSIONS_ECHEC]
    bloques = [j for j in echecs if jamais_pris(j)]
    if not bloques:
        return False, "aucun job hébergé annulé faute de runner"
    ids = {id(j) for j in bloques}
    autres = [j for j in echecs
              if id(j) not in ids and j.get("name") not in NON_REQUIS]
    if autres:
        return False, ("échec réel à côté : %s"
                       % ", ".join(sorted(str(j.get("name")) for j in autres)))
    noms = ", ".join(sorted(str(j.get("name")) for j in bloques))
    return True, "job(s) hébergé(s) annulé(s) faute de runner : %s" % noms


class GitHub:
    """Les deux appels utiles, avec le jeton du job (aucun `gh` requis)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.depot = env.get("GITHUB_REPOSITORY", "")
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def call(self, methode: str, chemin: str, corps=None) -> dict:
        donnees = None if corps is None else json.dumps(corps).encode("utf-8")
        req = urllib.request.Request(
            "%s/repos/%s/%s" % (self.api, self.depot, chemin), method=methode,
            data=donnees,
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=30) as rep:
            return json.loads(rep.read().decode("utf-8") or "{}")

    def jobs(self, run_id: str) -> list[dict]:
        """Les jobs de l'exécution, toutes pages (une exécution « tests » en
        compte jusqu'à ~20 ; la pagination évite d'en perdre un)."""
        out: list[dict] = []
        page = 1
        while True:
            rep = self.call("GET", "actions/runs/%s/jobs?per_page=100&page=%d"
                            % (run_id, page))
            lot = rep.get("jobs") or []
            out += lot
            if len(lot) < 100:
                return out
            page += 1

    def relancer(self, run_id: str) -> None:
        """Rejoue les jobs en échec et leurs dépendants (l'agrégat `tests`
        dépend de `platform-macos`) : c'est la reprise documentée d'un job
        annulé, et elle exige l'exécution terminée."""
        self.call("POST", "actions/runs/%s/rerun-failed-jobs" % run_id)


def jobs_du_fichier(chemin: str) -> list[dict]:
    donnees = json.loads(pathlib.Path(chemin).read_text(encoding="utf-8"))
    if isinstance(donnees, dict):
        return donnees.get("jobs") or []
    return donnees


def _annonce(texte: str) -> None:
    """La décision se lit dans le journal du job et dans son résumé : c'est
    la seule trace qu'une relance a eu lieu (ou a été refusée)."""
    print(texte)
    resume = os.environ.get("GITHUB_STEP_SUMMARY")
    if resume:
        try:
            with open(resume, "a", encoding="utf-8") as f:
                f.write(texte + "\n")
        except OSError:
            pass


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.conclusion != "failure":
        print("conclusion %r : rien à relancer" % args.conclusion)
        return 0
    if args.apply and not args.run_id:
        print("--apply exige --run-id", file=sys.stderr)
        return 2
    if args.jobs:
        jobs = jobs_du_fichier(args.jobs)
    elif args.run_id:
        gh = GitHub()
        try:
            jobs = gh.jobs(args.run_id)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print("lecture des jobs impossible : %s" % exc, file=sys.stderr)
            return 2
    else:
        print("--run-id ou --jobs requis", file=sys.stderr)
        return 2
    relancer, raison = decision(jobs, args.tentative)
    _annonce("décision : %s — %s"
             % ("relancer" if relancer else "ne pas relancer", raison))
    if not relancer or not args.apply:
        return 0
    gh = GitHub()
    try:
        gh.relancer(args.run_id)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print("relance impossible : %s" % exc, file=sys.stderr)
        return 2
    _annonce("relance demandée : exécution %s, jobs en échec et dépendants"
             % args.run_id)
    return 0


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-id", help="exécution « tests » à relancer")
    p.add_argument("--jobs", help="fichier JSON des jobs (décision hors ligne)")
    p.add_argument("--tentative", type=int, default=1,
                   help="numéro de tentative de l'exécution (défaut : 1)")
    p.add_argument("--conclusion", default="failure",
                   help="conclusion de l'exécution (défaut : failure)")
    p.add_argument("--apply", action="store_true",
                   help="exécute la relance (sans : décision seule)")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
