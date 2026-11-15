# SPDX-License-Identifier: AGPL-3.0-only
"""#6777 — déclencher la mesure de la carte d'impact dans sa fenêtre.

Le cron GitHub de ``carte.yml`` est démarré avec six à huit heures de retard
chaque jour : le garde de fenêtre de #6157 le saute donc systématiquement, et
la carte n'est plus jamais remesurée. La minuterie de rôle ``ameesh-carte``
(``ameesh role timers``) appelle ``ameesh carte dispatch`` à 01:00Z, dans la
fenêtre [00:00, 07:00)Z, sur l'hôte qui détient le bail du rôle élu. Le cron
reste un filet de secours, et le garde du workflow
(``scripts/carte-fenetre.py``) reste le seul juge : carte fraîche, paquet en
cours ou autre mesure le font sauter ; une carte de plus de 48 h l'emporte sur
la fenêtre.

``ameesh carte dispatch`` fait donc un ``workflow_dispatch`` de ``carte.yml``
avec ``planifie=true``, sans dupliquer la décision de mesurer. Il parcourt les
dépôts de ``github.projects`` (la branche de base déclarée, sinon ``main``) et
ignore un dépôt qui ne porte pas ce workflow. ``--repo``, ``--project`` et
``--branch`` restreignent la cible ; ``--manuel`` envoie ``planifie=false``
(dispatch manuel, toujours mesuré) ; ``--dry-run`` n'écrit rien.

#7430 : le premier tic du paquet horaire appelle ``--horaire`` chaque
heure, hors 01Z laissé à la minuterie carte quotidienne. À 01:00Z,
le dispatch quotidien reste systématique. Aux autres heures, seule une
carte connue de plus de ``exploitation.MAP_STALE_S`` sur la branche visée
provoque une demande. Une lecture absente ou impossible ne prouve pas cet
âge. Le dispatcher ne consulte ni les paquets ni les autres mesures : le
garde du workflow reste seul juge, et une demande sautée à H est retentée
à H+1 tant que la carte reste périmée.

Sortie : 0 si chaque dépôt visé a été dispatché ou ignoré faute de workflow,
1 si un dispatch a échoué (le journal de la minuterie le nomme), 2 sur une
configuration ou un usage invalide.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import config as config_mod
from . import plan_github

#: le workflow de mesure (L161e), sur la branche de base du dépôt
WORKFLOW = "carte.yml"
#: schéma de la sortie `--json`
SCHEMA = "ameesh-carte-dispatch/1"
#: branche de base quand la configuration n'en déclare pas
BRANCHE_DEFAUT = "main"


class CarteError(RuntimeError):
    """Un dispatch ne peut pas être préparé ou envoyé (message actionnable)."""


def cibles(settings, *, projets=(), depots=(), branche: str = "") -> list[dict]:
    """Les dépôts à dispatcher, avec leur branche de base.

    Sans sélection, tous les ``github.projects``. Un ``--repo`` explicite est
    accepté même hors configuration (geste manuel), sa branche retombant sur
    ``main`` ; un projet ou un dépôt en double n'est visé qu'une fois.
    """
    voulus: list[tuple[str, str]] = []
    if depots:
        connus = {repo.casefold(): (name, repo)
                  for name, repo in (settings.projects or {}).items()}
        for depot in depots:
            name, repo = connus.get(depot.casefold(), ("", depot))
            voulus.append((name, repo))
    elif projets:
        for projet in projets:
            name = settings.project_name(projet)
            if name is None:
                raise CarteError("projet %r absent de github.projects" % projet)
            voulus.append((name, settings.projects[name]))
    else:
        voulus = sorted((settings.projects or {}).items(),
                        key=lambda item: item[0].casefold())
    out, vus = [], set()
    for name, repo in voulus:
        cle = repo.casefold()
        if cle in vus:
            continue
        vus.add(cle)
        base = branche or (settings.bases_for(repo) or (BRANCHE_DEFAUT,))[0]
        out.append({"projet": name, "depot": repo, "branche": base})
    return out


def _erreur(code: int, out: str, err: str) -> str:
    return " ".join((err or out or "exit code %d" % code).split())[:300]


def porte(gh, depot: str, branche: str, *, workflow: str = WORKFLOW) -> bool:
    """Le dépôt porte-t-il ``carte.yml`` sur cette branche ?

    Une absence (404) est un non, pas une erreur : la minuterie vise tous les
    projets et la plupart ne mesurent pas. Toute autre lecture impossible est
    une erreur, pour ne pas confondre un dépôt sans carte avec un jeton ou un
    réseau en panne.
    """
    code, out, err = gh.run([
        "api", "repos/%s/contents/.github/workflows/%s?ref=%s"
        % (depot, workflow, branche)])
    if code == 0:
        return True
    message = _erreur(code, out, err)
    if "404" in message or "Not Found" in message:
        return False
    raise CarteError("lecture de %s sur %s impossible : %s"
                     % (workflow, depot, message))


def dispatcher(gh, depot: str, branche: str, *, planifie: bool = True) -> None:
    """Envoie le ``workflow_dispatch`` de ``carte.yml`` sur ``branche``."""
    charge = {"ref": branche,
              "inputs": {"planifie": "true" if planifie else "false"}}
    code, out, err = gh.run([
        "api", "-X", "POST",
        "repos/%s/actions/workflows/%s/dispatches" % (depot, WORKFLOW),
        "--input", "-"], json.dumps(charge))
    if code:
        raise CarteError("dispatch de %s sur %s refusé : %s"
                         % (WORKFLOW, depot, _erreur(code, out, err)))


def _sortie(resultats: list[dict], *, json_sortie: bool) -> None:
    if json_sortie:
        print(json.dumps({"schema": SCHEMA, "results": resultats},
                         ensure_ascii=False, sort_keys=True))
        return
    for item in resultats:
        cible = "%s@%s" % (item["depot"], item["branche"])
        if item["action"] == "dispatch":
            print("carte: %s — dispatch planifie=%s"
                  % (cible, "true" if item.get("planifie") else "false"))
        elif item["action"] == "dry-run":
            print("carte: %s — dispatch à venir (dry-run)" % cible)
        elif item["action"] == "skip":
            print("carte: %s — ignoré : %s" % (cible, item.get("reason") or "rien à faire"))
        else:
            print("carte: %s — échec : %s"
                  % (cible, item.get("reason") or "dispatch impossible"), file=sys.stderr)


def demande_horaire(cible, cartes, maintenant):
    """Choose when to ask; packet/measurement arbitration stays in carte.yml."""
    maintenant = maintenant.astimezone(dt.timezone.utc)
    if maintenant.hour == 1:
        return True
    from .exploitation import MAP_STALE_S
    for row in cartes:
        if (row["repo"].casefold() == cible["depot"].casefold()
                and row.get("branch") == cible["branche"]):
            published = row.get("published_ts")
            return (published is not None
                    and maintenant.timestamp() - float(published) > MAP_STALE_S)
    return False


def cartes_connues(cfg):
    from . import db as db_mod, storage
    db = db_mod.connect(cfg)
    try:
        return storage.of(db).ci.maps()
    finally:
        db.close()


def main(argv=None, *, gh=None, cfg=None, maintenant=None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh carte")
    sous = parser.add_subparsers(dest="action")
    dispatch = sous.add_parser(
        "dispatch", help="dispatcher carte.yml (planifie=true) dans sa fenêtre")
    dispatch.add_argument("--repo", action="append", default=[],
                          help="dépôt owner/repo (répétable ; défaut : github.projects)")
    dispatch.add_argument("--project", action="append", default=[],
                          help="projet de github.projects (répétable)")
    dispatch.add_argument("--branch", default="",
                          help="branche de base (défaut : celle du dépôt, sinon main)")
    dispatch.add_argument("--horaire", action="store_true",
                          help="01:00Z quotidien ; sinon carte connue de plus de 48 h")
    dispatch.add_argument("--manuel", action="store_true",
                          help="dispatch manuel (planifie=false, toujours mesuré)")
    dispatch.add_argument("--dry-run", action="store_true",
                          help="lire et dire, sans rien envoyer")
    dispatch.add_argument("--json", action="store_true", help="un objet JSON")
    args = parser.parse_args(argv)
    if getattr(args, "action", None) is None:
        parser.print_help()
        return 0

    if args.horaire and args.manuel:
        parser.error("--horaire et --manuel sont incompatibles")
    maintenant = (maintenant or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    cfg = config_mod.load() if cfg is None else cfg
    try:
        settings = plan_github.settings_of(cfg)
    except plan_github.SettingsError as exc:
        print("carte: %s" % exc, file=sys.stderr)
        return 2
    try:
        visees = cibles(settings, projets=args.project, depots=args.repo,
                        branche=args.branch)
    except CarteError as exc:
        print("carte: %s" % exc, file=sys.stderr)
        return 2

    if gh is None:
        try:
            gh = plan_github.Gh()
        except plan_github.GithubError as exc:
            print("carte: %s" % exc, file=sys.stderr)
            return 1

    cartes = []
    if args.horaire and maintenant.hour != 1:
        try:
            cartes = cartes_connues(cfg)
        except Exception as exc:  # unknown age never requests hourly measurement
            print("carte: âge inconnu : %s" % exc, file=sys.stderr)
    resultats, echec = [], False
    if not visees:
        print("carte: aucun projet dans github.projects — rien à dispatcher",
              file=sys.stderr)
        return 1
    for cible in visees:
        item = dict(cible, action="", planifie=not args.manuel)
        try:
            if args.horaire and not demande_horaire(cible, cartes, maintenant):
                item.update(action="skip", reason="pas de rattrapage horaire dû")
            elif not porte(gh, cible["depot"], cible["branche"]):
                item.update(action="skip", reason="workflow %s absent" % WORKFLOW)
            elif args.dry_run:
                item["action"] = "dry-run"
            else:
                dispatcher(gh, cible["depot"], cible["branche"],
                           planifie=not args.manuel)
                item["action"] = "dispatch"
        except CarteError as exc:
            echec = True
            item.update(action="error", reason=str(exc))
        resultats.append(item)
    _sortie(resultats, json_sortie=args.json)
    return 1 if echec else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
