#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3546 : purge rejouable des artefacts d'ameesh selon la rétention en vigueur.

Le quota d'artefacts de l'organisation (500 Mo) était porté par ameesh : 5 297
artefacts, 1 667,9 Mio le 25/10 11:20Z (mesure #2376), dont 1 396,6 Mio de
`mesure-*` restés après des runs `carte` annulés, et des milliers d'envois
d'avant la baisse des rétentions (`flux-*` et `plan` à 14 jours, `leaks` à
90), encore loin de leur échéance d'origine. Ce script supprime ce que la
règle d'aujourd'hui n'aurait pas gardé :

* `mesure-*` : ne sert qu'au job `carte` de son run ; supprimé dès le run
  terminé, sauf dans le cas où le workflow les garde lui-même (hors PR,
  matrice verte, fusion en échec : le job `carte` peut être rejoué seul) ;
* les autres familles : supprimées au-delà de la rétention que déclare
  aujourd'hui leur envoi (`RETENTION_H`, miroir des `retention-days` de
  `.github/workflows/`, tenu par tests/test_3546_purge_artefacts.py).

Toujours épargnés : un artefact d'un run qui n'est pas terminé (paquet en
cours de CI compris), un artefact de moins de `--epargne-heures` (24 par
défaut) hors `mesure-*`, une famille inconnue, un artefact déjà expiré.
Le cliché des runs en cours, pris au départ, ne suffit pas : un run terminé
peut être relancé pendant la purge. Le statut du run est donc relu juste
avant chaque suppression, toutes familles, sans cache ; un run qui n'est
plus `completed`, ou illisible, garde ses artefacts.

Par défaut le script ne fait que compter (avant, après, par famille) ;
`--appliquer` supprime. Il lit et supprime par `gh api` (jeton de `gh`,
droit `actions: write` sur le dépôt).

Usage :
  python3 scripts/purge-artefacts.py [--repo manaty/ameesh] [--epargne-heures 24] [--appliquer]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import subprocess
import sys
import time

#: rétention déclarée aujourd'hui par famille (heures), miroir des
#: `retention-days` des envois de `.github/workflows/` ; None : la famille ne
#: vit que le temps de son run (le job `menage-mesures` de carte.yml la
#: supprime à la fin du run).
RETENTION_H: dict[str, int | None] = {
    "mesure": None,          # carte.yml, mesure-<pilote>-<part>
    "plan-mesure": 3 * 24,   # carte.yml
    "carte": 3 * 24,         # carte.yml (#2520)
    "plan": 3 * 24,          # tests.yml
    "flux-psycopg": 24,      # tests.yml
    "flux-psql": 24,         # tests.yml
    "autre-fs": 14 * 24,     # tests.yml
    "leaks": 15 * 24,        # tests.yml, décompte sur deux semaines (L161i)
    "github-pages": 24,      # actions/upload-pages-artifact (défaut du site)
}

ETATS_EN_COURS = ("queued", "in_progress", "waiting", "requested", "pending")

def famille(nom: str) -> str | None:
    """Famille d'un nom d'artefact : `mesure-psycopg-3` → `mesure`,
    `flux-psql-12` → `flux-psql`, `leaks` → `leaks` ; None si inconnue."""
    if nom.startswith("mesure-"):
        return "mesure"
    base = re.sub(r"-\d+$", "", nom)
    return base if base in RETENTION_H else None


def _date(texte: str) -> dt.datetime:
    return dt.datetime.fromisoformat(texte.replace("Z", "+00:00"))


def jobs_effectifs(jobs: list[dict]) -> list[dict]:
    """Le dernier résultat de chaque job, toutes tentatives confondues.

    `jobs` est la liste de `/jobs?filter=all` : une relance du seul job
    `carte` ne rejoue pas les mesures, dont le résultat reste celui de la
    tentative précédente. `filter=latest` (le défaut de l'API) les omet."""
    dernier: dict[str, dict] = {}
    for j in jobs:
        cle = (j.get("run_attempt") or 1, j.get("id") or 0)
        nom = j.get("name") or ""
        if nom not in dernier or cle > (dernier[nom].get("run_attempt") or 1,
                                        dernier[nom].get("id") or 0):
            dernier[nom] = j
    return list(dernier.values())


def mesures_gardees(run: dict, jobs: list[dict]) -> bool:
    """Le cas où carte.yml garde les mesures de son run : hors PR, toutes les
    parts de mesure vertes, fusion (job `carte`) en échec — sur le dernier
    résultat de chaque job (`jobs_effectifs`). Une preuve incomplète (aucun
    job de mesure, ou pas de `carte` après des mesures vertes) garde aussi :
    la rétention d'un jour fera le reste."""
    if run.get("event") == "pull_request":
        return False
    jobs = jobs_effectifs(jobs)
    mesures = [j for j in jobs if (j.get("name") or "").startswith("mesure ")]
    carte = [j for j in jobs if j.get("name") == "carte"]
    if not mesures:
        return True
    if not all(j.get("conclusion") == "success" for j in mesures):
        return False
    return not carte or carte[0].get("conclusion") == "failure"


def decision(artefact: dict, maintenant: dt.datetime, *, en_cours: set[int],
             epargne_h: float, garde_mesures=lambda run_id: False,
             run_termine=lambda run_id: True) -> tuple[bool, str]:
    """(supprimer, raison) pour un artefact au format de l'API GitHub.

    `en_cours` : identifiants des runs non terminés au départ ;
    `garde_mesures(run_id)` : vrai si le workflow garde les mesures de ce run
    (`mesures_gardees`), appelé seulement pour une mesure d'un run terminé ;
    `run_termine(run_id)` : relecture du statut, vrai seulement si le run est
    encore `completed`, appelée avant toute suppression."""
    supprimer, raison = _decision(artefact, maintenant, en_cours=en_cours,
                                  epargne_h=epargne_h, garde_mesures=garde_mesures)
    if supprimer and not run_termine(artefact["workflow_run"]["id"]):
        return False, "run relancé ou illisible à la relecture"
    return supprimer, raison


def _decision(artefact: dict, maintenant: dt.datetime, *, en_cours: set[int],
              epargne_h: float, garde_mesures) -> tuple[bool, str]:
    if artefact.get("expired"):
        return False, "déjà expiré"
    fam = famille(artefact.get("name") or "")
    if fam is None:
        return False, "famille inconnue"
    run_id = (artefact.get("workflow_run") or {}).get("id")
    if run_id is None:
        return False, "run inconnu"
    if run_id in en_cours:
        return False, "run en cours"
    age_h = (maintenant - _date(artefact["created_at"])).total_seconds() / 3600
    retention = RETENTION_H[fam]
    if retention is None:
        if age_h < 24 and garde_mesures(run_id):
            return False, "gardée pour rejouer la fusion"
        return True, "mesure d'un run terminé"
    if age_h < epargne_h:
        return False, f"moins de {epargne_h:g} h"
    if age_h > retention:
        return True, f"au-delà de {retention} h"
    return False, "dans sa rétention"


def _gh(*args: str, essais: int = 3) -> str:
    for essai in range(essais):
        try:
            return subprocess.run(["gh", "api", *args], check=True, capture_output=True,
                                  text=True).stdout
        except subprocess.CalledProcessError:
            if essai == essais - 1:
                raise
            time.sleep(2 * (essai + 1))
    raise AssertionError("inatteignable")


def _lignes_json(texte: str) -> list:
    return [json.loads(l) for l in texte.splitlines() if l.strip()]


def lire_artefacts(repo: str) -> list[dict]:
    return _lignes_json(_gh("--paginate", f"repos/{repo}/actions/artifacts?per_page=100",
                            "--jq", ".artifacts[]"))


def runs_en_cours(repo: str) -> set[int]:
    ids: set[int] = set()
    for etat in ETATS_EN_COURS:
        ids.update(_lignes_json(_gh("--paginate",
                                    f"repos/{repo}/actions/runs?status={etat}&per_page=100",
                                    "--jq", ".workflow_runs[].id")))
    return ids


class LecteurRuns:
    """Lectures d'un run par l'API, sans cache : chaque suppression s'appuie
    sur un statut lu juste avant elle (relecture cx3 de fb1f3860 : un
    `completed` réutilisé laissait supprimer pendant une relance)."""

    def __init__(self, repo: str, gh=None):
        self.repo, self._gh = repo, gh or _gh

    def run(self, run_id: int) -> dict | None:
        """Le run, relu à chaque appel, ou None s'il est illisible."""
        try:
            return json.loads(self._gh(f"repos/{self.repo}/actions/runs/{run_id}"))
        except (subprocess.CalledProcessError, ValueError) as e:
            print(f"run {run_id} illisible, artefacts gardés : "
                  f"{getattr(e, 'stderr', '') or e}".strip(), file=sys.stderr)
            return None

    def termine(self, run_id: int) -> bool:
        run = self.run(run_id)
        return run is not None and run.get("status") == "completed"

    def garde_mesures(self, run_id: int) -> bool:
        """Vrai (garder) si le run n'est pas terminé, est illisible, ou
        répond au cas de garde du workflow sur toutes ses tentatives."""
        run = self.run(run_id)
        if run is None or run.get("status") != "completed":
            return True
        try:
            jobs = _lignes_json(self._gh(
                "--paginate",
                f"repos/{self.repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
                "--jq", ".jobs[]"))
        except (subprocess.CalledProcessError, ValueError) as e:
            print(f"jobs du run {run_id} illisibles, mesures gardées : "
                  f"{getattr(e, 'stderr', '') or e}".strip(), file=sys.stderr)
            return True
        # un job sans conclusion, ou d'une tentative plus récente que le run
        # lu : relancé entre les deux lectures
        if any(j.get("conclusion") is None
               or (j.get("run_attempt") or 0) > (run.get("run_attempt") or 0)
               for j in jobs):
            return True
        return mesures_gardees(run, jobs)


def bilan(artefacts: list[dict]) -> dict[str, tuple[int, int]]:
    """{famille: (nombre, octets)} des artefacts non expirés."""
    b: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for a in artefacts:
        if a.get("expired"):
            continue
        f = famille(a.get("name") or "") or "(inconnue)"
        b[f][0] += 1
        b[f][1] += a.get("size_in_bytes") or 0
    return {f: (n, o) for f, (n, o) in b.items()}


def _mio(octets: int) -> str:
    return f"{octets / 1048576:,.1f} Mio".replace(",", " ")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--repo", default="manaty/ameesh")
    p.add_argument("--epargne-heures", type=float, default=24.0,
                   help="épargner tout artefact plus jeune, hors mesure-* (défaut 24)")
    p.add_argument("--appliquer", action="store_true",
                   help="supprimer (sans lui : compte seulement)")
    a = p.parse_args(argv)
    maintenant = dt.datetime.now(dt.timezone.utc)
    artefacts = lire_artefacts(a.repo)
    en_cours = runs_en_cours(a.repo)
    runs = LecteurRuns(a.repo)
    supprimes, raisons, echecs = [], collections.Counter(), 0
    for art in artefacts:
        suppr, raison = decision(art, maintenant, en_cours=en_cours,
                                 epargne_h=a.epargne_heures,
                                 garde_mesures=runs.garde_mesures,
                                 run_termine=runs.termine)
        raisons[raison] += 1
        if not suppr:
            continue
        if a.appliquer:
            try:
                _gh("-X", "DELETE", f"repos/{a.repo}/actions/artifacts/{art['id']}")
            except subprocess.CalledProcessError as e:
                echecs += 1
                print(f"échec {art['id']} {art['name']} : {e.stderr.strip()}", file=sys.stderr)
                continue
        supprimes.append(art)
    ids = {s["id"] for s in supprimes}
    avant, apres = bilan(artefacts), bilan([x for x in artefacts if x["id"] not in ids])
    print(f"{a.repo} — {maintenant:%Y-%m-%d %H:%MZ} — "
          f"{'suppression' if a.appliquer else 'compte seul (--appliquer pour supprimer)'}")
    print(f"{'famille':<14} {'avant':>20} {'après':>20}")
    for f in sorted(avant, key=lambda k: -avant[k][1]):
        n, o = avant[f]
        n2, o2 = apres.get(f, (0, 0))
        print(f"{f:<14} {n:>6} {_mio(o):>13} {n2:>6} {_mio(o2):>13}")
    tot = lambda b: (sum(n for n, _ in b.values()), sum(o for _, o in b.values()))
    (n, o), (n2, o2) = tot(avant), tot(apres)
    print(f"{'total':<14} {n:>6} {_mio(o):>13} {n2:>6} {_mio(o2):>13}")
    for raison, compte in raisons.most_common():
        print(f"  {compte:>6}  {raison}")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
