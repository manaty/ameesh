#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#6157 — la mesure planifiée de la carte n'est due que dans sa fenêtre.

Constat (pilote, 07/11) : le cron `30 3 * * *` de carte.yml est démarré par
GitHub avec six à huit heures de retard, chaque jour (05/11 11:25Z, 06/11
11:24Z, 07/11 10:37Z) ; ses 18 parts de mesure tenaient 11 des 12 runners
`ameesh-ci` pendant que la ci-full du paquet h-20261107-13 restait en file.
L'heure de 03:30Z, choisie « loin des coupures et des paquets » (#3343),
n'était donc jamais l'heure réelle.

La mesure planifiée part désormais d'une minuterie d'hôte
(deploy/systemd/ameesh-carte.timer, `workflow_dispatch` avec
`planifie=true` à 03:30Z, fiable) ; le cron GitHub reste un filet de secours.
Les deux passent par ce garde, exécuté par le job `fenetre` sur un runner
hébergé (il ne prend jamais un runner du pool) :

  - pas due : une carte de main a été publiée il y a moins de 12 h, ou une
    mesure plus ancienne de la carte est en file ou en cours (le cron en
    retard derrière une mesure de la minuterie ; de deux gardes simultanés,
    l'aîné mesure) ;
  - hors fenêtre : l'heure UTC n'est pas dans [00:00, 07:00) — loin des
    coupures 10:00Z/22:00Z (11:00/23:00 Europe/Paris) et des heures de
    paquets de jour ;
  - paquet en cours : une exécution d'une branche `integration/*` est en file
    ou en cours ; la mesure lui prendrait ses runners.

Une mesure sautée n'est pas un échec (le job est vert, la raison est en
annotation) : la carte reste sous 48 h par la mesure du lendemain, et le plan
signale une carte périmée. Un dispatch manuel (sans `planifie`) et une PR qui
touche la mesure mesurent toujours. Une lecture d'API impossible est non
concluante (canon §3) : elle n'empêche pas la mesure, la fenêtre horaire
suffit à la borner.

Sortie : `mesurer=true|false` dans `$GITHUB_OUTPUT`, raison en annotation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

#: fenêtre horaire UTC de la mesure planifiée, [début, fin)
FENETRE_UTC = (0, 7)
#: une carte de main plus jeune que ceci rend la mesure planifiée « pas due »
FRAICHEUR_H = 12
#: préfixe des branches de paquet (paquet.yml : `integration/**`)
PREFIXE_PAQUET = "integration/"


def decider(*, evenement: str, planifie: bool, maintenant: dt.datetime,
            derniere_carte: dt.datetime | None,
            paquet_actif: bool | None,
            autre_carte: bool | None = None) -> tuple[bool, str]:
    """(mesurer, raison). `None` = information non lue (non concluant)."""
    if evenement not in ("schedule", "workflow_dispatch"):
        return True, "%s : mesure de contrôle" % evenement
    if evenement == "workflow_dispatch" and not planifie:
        return True, "dispatch manuel : mesure sans garde"
    if derniere_carte is not None:
        age_h = (maintenant - derniere_carte).total_seconds() / 3600
        if age_h < FRAICHEUR_H:
            return False, ("pas due : carte de main publiée il y a %.1f h "
                           "(< %d h)" % (age_h, FRAICHEUR_H))
    if autre_carte:
        return False, "pas due : une autre mesure de la carte est en file ou en cours"
    debut, fin = FENETRE_UTC
    if not debut <= maintenant.hour < fin:
        return False, ("hors fenêtre : %s, mesure planifiée due entre "
                       "%02d:00Z et %02d:00Z (retard du planificateur)"
                       % (maintenant.strftime("%H:%MZ"), debut, fin))
    if paquet_actif:
        return False, "paquet en cours : la mesure lui laisse les runners"
    return True, "mesure planifiée dans sa fenêtre"


def _date(texte: str) -> dt.datetime:
    return dt.datetime.fromisoformat(texte.replace("Z", "+00:00"))


class GitHub:
    """Deux lectures, avec le jeton du job (aucun `gh` requis)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.depot = env.get("GITHUB_REPOSITORY", "")
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def get(self, chemin: str) -> dict:
        req = urllib.request.Request(
            "%s/repos/%s/%s" % (self.api, self.depot, chemin),
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=30) as rep:
            return json.loads(rep.read().decode("utf-8") or "{}")

    def derniere_carte(self) -> dt.datetime | None:
        """Création du plus récent cache `carte-*` de main (job `carte`)."""
        page = self.get("actions/caches?key=carte-&ref=refs/heads/main"
                        "&sort=created_at&direction=desc&per_page=20")
        dates = [_date(c["created_at"]) for c in page.get("actions_caches", [])
                 if c.get("key", "").startswith("carte-") and c.get("created_at")]
        return max(dates) if dates else None

    def en_cours(self) -> list[dict]:
        """Les exécutions en file ou en cours du dépôt."""
        runs: list[dict] = []
        for statut in ("queued", "in_progress"):
            page = self.get("actions/runs?status=%s&per_page=100" % statut)
            runs.extend(page.get("workflow_runs", []))
        return runs


def paquet_actif(runs: list[dict]) -> bool:
    return any(str(r.get("head_branch") or "").startswith(PREFIXE_PAQUET)
               for r in runs)


def autre_carte(runs: list[dict], run_id: str) -> bool:
    """Une exécution de carte.yml plus ancienne que celle du garde.

    Priorité déterministe (relecture #6157) : deux gardes simultanés se voient
    l'un l'autre ; seul le plus récent (identifiant de run plus grand) cède,
    l'aîné mesure. Un identifiant illisible compte comme plus ancien."""
    def rang(valeur) -> int:
        try:
            return int(valeur)
        except (TypeError, ValueError):
            return -1
    soi = rang(run_id)
    return any(str(r.get("path") or "").endswith("/carte.yml")
               and str(r.get("id")) != str(run_id)
               and (soi < 0 or rang(r.get("id")) < soi) for r in runs)


def _lire(fonction, quoi: str):
    try:
        return fonction()
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print("::warning::%s illisible (%s) : non concluant, ignoré" % (quoi, exc))
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--event", required=True)
    p.add_argument("--planifie", default="false")
    p.add_argument("--maintenant", help="ISO 8601 (tests) ; défaut : maintenant")
    args = p.parse_args(argv)
    maintenant = (_date(args.maintenant) if args.maintenant
                  else dt.datetime.now(dt.timezone.utc))
    planifie = args.planifie.strip().lower() == "true"
    garde = args.event == "schedule" or (args.event == "workflow_dispatch" and planifie)
    gh = GitHub()
    derniere = runs = None
    if garde:
        derniere = _lire(gh.derniere_carte, "caches de la carte")
        runs = _lire(gh.en_cours, "exécutions en cours")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    mesurer, raison = decider(
        evenement=args.event, planifie=planifie, maintenant=maintenant,
        derniere_carte=derniere,
        paquet_actif=None if runs is None else paquet_actif(runs),
        autre_carte=None if runs is None else autre_carte(runs, run_id))
    print(("::notice::" if mesurer else "::warning::mesure sautée — ") + raison)
    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write("mesurer=%s\n" % ("true" if mesurer else "false"))
    resume = os.environ.get("GITHUB_STEP_SUMMARY")
    if resume:
        with open(resume, "a", encoding="utf-8") as f:
            f.write("fenêtre de la carte : %s — %s\n"
                    % ("mesure" if mesurer else "sautée", raison))
    return 0


if __name__ == "__main__":
    sys.exit(main())
